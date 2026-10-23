"""VdS 2465-S2 transport with the Telenot protocol extension (slave side).

The panel is the S2 *master* even though we open the TCP connection: after
connecting we wait for its SYNC_REQUEST, then answer every telegram it sends
with exactly one telegram of our own (POLL, or DATA when something is queued).
Acknowledgement is implicit via the transmission/receive counters.

Reimplemented from Telenot.Communication (S2ProtocolTelenotAsync,
S2ProcedureSlave, S2Telegram) of the BuildSec app.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import struct
from collections.abc import Callable
from dataclasses import dataclass

from .crypto import (
    KEY_NUMBER_BETREIBER,
    KEY_NUMBER_PLAIN,
    KEY_NUMBER_UNKNOWN,
    derive_betreiber_key,
    seal,
    unseal,
)
from .exceptions import (
    TelenotBusyError,
    TelenotConnectionError,
    TelenotKeyError,
    TelenotProtocolError,
)

_LOGGER = logging.getLogger(__name__)

MASK32 = 0xFFFFFFFF

PK_VDS = 0x01
PK_TELENOT = 0x82

IK_SYNC_REQUEST = 1
IK_SYNC_RESPONSE = 2
IK_POLL = 3
IK_DATA = 4
IK_ERROR_UNKNOWN_IK = 5
IK_ERROR_UNKNOWN_PK = 6
IK_SERVICE_REQUEST = 7

NK_USERDATA = 0x01
NK_POLLTIME_SET = 0x05
NK_POLLTIME_ACK = 0x06
NK_SESSION_CLOSE = 0x09
NK_SESSION_CANCELLED = 0xFC
NK_VERSION_MISMATCH = 0xFD
NK_INVALID_ID = 0xFE

PROTOCOL_VERSION = 3
SYNC_WINDOW_BUSY = 0xFF
MAX_REPETITIONS = 3
MAX_SYNC_REPETITIONS = 2
MAX_VDS_USERDATA = 247

CONNECT_TIMEOUT = 15.0
FUNCTION_MONITORING_TIMEOUT = 24.0
SLAVE_POLL_DELAY = 0.05
DEFAULT_POLL_TIME_MS = 2000


@dataclass(slots=True)
class S2Telegram:
    """Plaintext S2 telegram."""

    tc: int
    rc: int
    ik: int
    pk: int = PK_TELENOT
    nk: int = NK_USERDATA
    version: int = PROTOCOL_VERSION
    data: bytes = b""

    def encode(self) -> bytes:
        """Serialise (checksum left zero; it is filled in by ``seal``)."""
        if self.pk == PK_VDS:
            return (
                struct.pack(">IHIBBB", self.tc, 0, self.rc, self.ik, PK_VDS, len(self.data))
                + self.data
            )
        return (
            struct.pack(
                ">IHIBBBHBB",
                self.tc,
                0,
                self.rc,
                self.ik,
                PK_TELENOT,
                4,
                len(self.data),
                self.version,
                self.nk,
            )
            + self.data
        )

    @classmethod
    def decode(cls, raw: bytes) -> S2Telegram:
        """Parse an unpadded plaintext telegram."""
        if len(raw) < 13:
            raise TelenotProtocolError("Telegram shorter than VdS frame")
        tc, _crc, rc, ik, pk, length = struct.unpack_from(">IHIBBB", raw)
        if pk == PK_VDS:
            return cls(tc, rc, ik, pk, NK_USERDATA, 0, raw[13 : 13 + length])
        if pk == PK_TELENOT:
            if length != 4 or len(raw) < 17:
                raise TelenotProtocolError("Invalid Telenot extension length")
            nl, version, nk = struct.unpack_from(">HBB", raw, 13)
            return cls(tc, rc, ik, pk, nk, version, raw[17 : 17 + nl])
        raise TelenotProtocolError(f"Unknown protocol id 0x{pk:02X}")

    def __str__(self) -> str:
        return (
            f"TC={self.tc:08X} RC={self.rc:08X} IK={self.ik} PK=0x{self.pk:02X} "
            f"V={self.version} NK=0x{self.nk:02X} data={self.data.hex(' ')}"
        )


@dataclass(slots=True)
class _TxJob:
    data: bytes
    nk: int
    future: asyncio.Future[None]


class S2Session:
    """One S2 session over one TCP connection (sessions cannot be reused)."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        key_number: int = KEY_NUMBER_PLAIN,
        aes_key: bytes | None = None,
        freischaltcode: bytes | None = None,
        poll_time_ms: int = DEFAULT_POLL_TIME_MS,
        on_userdata: Callable[[bytes], None] | None = None,
        on_closed: Callable[[Exception | None], None] | None = None,
    ) -> None:
        """Initialise; ``freischaltcode`` is required for key number 9997."""
        if key_number == KEY_NUMBER_BETREIBER and (
            freischaltcode is None or len(freischaltcode) != 8
        ):
            raise ValueError("Key number 9997 requires an 8 byte Freischaltcode")
        if key_number not in (KEY_NUMBER_PLAIN, KEY_NUMBER_BETREIBER) and (
            aes_key is None or len(aes_key) != 16
        ):
            raise ValueError("A 16 byte AES key is required")
        self._host = host
        self._port = port
        self._key_number = key_number
        self._static_key = aes_key
        self._freischaltcode = freischaltcode
        self._poll_time_ms = poll_time_ms
        self._on_userdata = on_userdata
        self._on_closed = on_closed

        self._key: bytes | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tc = 0  # our last transmission counter
        self._rc = 0  # receive counter we sent last (= next TC expected from master)
        self._version = PROTOCOL_VERSION
        self._last_sent: S2Telegram | None = None
        self._repetitions = 0
        self._inflight: _TxJob | None = None

        self._tx_queue: asyncio.Queue[_TxJob] = asyncio.Queue()
        self._rx_envelopes: asyncio.Queue[tuple[int, bytes] | Exception] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._closing = asyncio.Event()
        self._tx_signal = asyncio.Event()
        self._get_task: asyncio.Future | None = None
        self._closed = False
        self._polltime_acked = False

    @property
    def connected(self) -> bool:
        """Return True while the session loop is running."""
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def protocol_version(self) -> int:
        """Negotiated Telenot extension version."""
        return self._version

    # ------------------------------------------------------------------ public API

    async def connect(self) -> None:
        """Open TCP, synchronise the session and start the background loop."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError) as err:
            raise TelenotConnectionError(
                f"Cannot connect to {self._host}:{self._port}: {err}"
            ) from err
        self._reader_task = asyncio.create_task(self._read_envelopes(), name="telenot-s2-reader")
        try:
            await self._synchronise()
        except BaseException:
            await self._teardown()
            raise
        self._loop_task = asyncio.create_task(self._run(), name="telenot-s2-loop")

    async def send(self, userdata: bytes) -> None:
        """Queue VdS user data and wait until the panel acknowledged it."""
        if len(userdata) > MAX_VDS_USERDATA:
            raise ValueError("User data exceeds 247 bytes")
        await self._submit(userdata, NK_USERDATA)

    async def close(self) -> None:
        """End the session gracefully (SESSION_CLOSE) and close the socket."""
        if self._closed:
            return
        self._closing.set()
        self._tx_signal.set()
        if self._loop_task is not None and not self._loop_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._loop_task), 5)
            except (TimeoutError, Exception):  # noqa: BLE001 - best effort shutdown
                self._loop_task.cancel()
        await self._teardown()

    # ------------------------------------------------------------------ internals

    async def _submit(self, data: bytes, nk: int) -> None:
        if self._closed or self._closing.is_set():
            raise TelenotConnectionError("Session is closed")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._tx_queue.put(_TxJob(data, nk, future))
        self._tx_signal.set()
        await future

    async def _read_envelopes(self) -> None:
        assert self._reader is not None
        try:
            while True:
                header = await self._reader.readexactly(4)
                key_number, length = struct.unpack(">HH", header)
                if length < 13:
                    raise TelenotProtocolError(f"Envelope length {length} < 13")
                payload = await self._reader.readexactly(length)
                await self._rx_envelopes.put((key_number, payload))
        except asyncio.IncompleteReadError:
            await self._rx_envelopes.put(TelenotConnectionError("Connection closed by panel"))
        except OSError as err:
            await self._rx_envelopes.put(TelenotConnectionError(str(err)))
        except TelenotProtocolError as err:
            await self._rx_envelopes.put(err)

    async def _next_envelope(self, timeout: float) -> tuple[int, bytes]:
        try:
            item = await asyncio.wait_for(self._rx_envelopes.get(), timeout)
        except TimeoutError as err:
            raise TelenotConnectionError("No telegram from panel (function monitoring)") from err
        if isinstance(item, Exception):
            raise item
        return item

    def _decode(self, key_number: int, payload: bytes) -> S2Telegram | None:
        if key_number == KEY_NUMBER_UNKNOWN:
            raise TelenotKeyError("The panel does not know this key number")
        key = None if key_number == KEY_NUMBER_PLAIN else self._key
        if key_number not in (KEY_NUMBER_PLAIN, self._key_number):
            _LOGGER.debug("Ignoring telegram with unexpected key number %s", key_number)
            return None
        if key_number != KEY_NUMBER_PLAIN and key is None:
            return None
        raw = unseal(key_number, payload, key)
        if raw is None:
            _LOGGER.debug("Ignoring telegram with bad checksum")
            return None
        telegram = S2Telegram.decode(raw)
        _LOGGER.debug("<<< %s", telegram)
        return telegram

    async def _write(self, telegram: S2Telegram) -> None:
        assert self._writer is not None
        key_number = self._key_number if self._key is not None or self._key_number == 0 else 0
        _LOGGER.debug(">>> %s", telegram)
        self._writer.write(seal(telegram.encode(), key_number, self._key))
        try:
            await asyncio.wait_for(self._writer.drain(), 2)
        except (OSError, TimeoutError) as err:
            raise TelenotConnectionError(f"Send failed: {err}") from err

    async def _send_new(self, ik: int, nk: int = NK_USERDATA, data: bytes = b"") -> None:
        self._tc = (self._tc + 1) & MASK32
        self._rc = (self._rc + 1) & MASK32
        self._last_sent = S2Telegram(self._tc, self._rc, ik, PK_TELENOT, nk, self._version, data)
        self._repetitions = 0
        await self._write(self._last_sent)

    async def _resend(self, limit: int) -> None:
        self._repetitions += 1
        if self._repetitions > limit:
            raise TelenotProtocolError("Panel exceeded the permitted number of repetitions")
        assert self._last_sent is not None
        await self._write(self._last_sent)

    def _classify(self, telegram: S2Telegram) -> str:
        """Return 'accept', 'resend' or 'ignore' according to the counters."""
        if telegram.rc == (self._tc + 1) & MASK32 and telegram.tc == self._rc:
            return "accept"
        if telegram.rc == self._tc and telegram.tc == (self._rc - 1) & MASK32:
            return "resend"
        return "ignore"

    async def _synchronise(self) -> None:
        # 1. Plaintext SYNC_REQUEST from the panel.
        while True:
            key_number, payload = await self._next_envelope(FUNCTION_MONITORING_TIMEOUT)
            if key_number != KEY_NUMBER_PLAIN:
                continue
            telegram = self._decode(key_number, payload)
            if telegram is not None and telegram.ik == IK_SYNC_REQUEST:
                break
        if telegram.pk != PK_VDS or len(telegram.data) != 1:
            raise TelenotProtocolError("Malformed SYNC_REQUEST")
        if telegram.data[0] == SYNC_WINDOW_BUSY:
            raise TelenotBusyError("Remote access is already in use")
        if telegram.data[0] != 1:
            raise TelenotProtocolError(f"Unsupported window size {telegram.data[0]}")

        if self._key_number == KEY_NUMBER_BETREIBER:
            assert self._freischaltcode is not None
            self._key = derive_betreiber_key(self._freischaltcode, telegram.tc)
        elif self._key_number != KEY_NUMBER_PLAIN:
            self._key = self._static_key
        self._rc = telegram.tc
        self._tc = secrets.randbits(32)

        # 2. SYNC_RESPONSE, then wait for the first POLL.
        await self._send_new(IK_SYNC_RESPONSE, NK_USERDATA, b"\x01")
        while True:
            key_number, payload = await self._next_envelope(FUNCTION_MONITORING_TIMEOUT)
            telegram = self._decode(key_number, payload)
            if telegram is None:
                continue
            if telegram.ik == IK_SYNC_REQUEST:
                if telegram.tc == (self._rc - 1) & MASK32:
                    if self._repetitions >= MAX_SYNC_REPETITIONS and self._key_number:
                        raise TelenotKeyError(
                            "Panel keeps repeating SYNC_REQUEST - check the AES key"
                        )
                    await self._resend(MAX_SYNC_REPETITIONS + 1)
                    continue
                raise TelenotProtocolError("Panel restarted synchronisation")
            verdict = self._classify(telegram)
            if verdict == "resend":
                await self._resend(MAX_SYNC_REPETITIONS)
                continue
            if verdict == "ignore":
                continue
            if (
                telegram.ik == IK_POLL
                and telegram.nk == NK_VERSION_MISMATCH
                and 1 <= telegram.version < 3
            ):
                self._version = telegram.version
                _LOGGER.debug("Panel requested protocol version %s", self._version)
                break
            if telegram.ik == IK_POLL and telegram.nk == NK_USERDATA:
                break
            raise TelenotProtocolError(f"Unexpected telegram during sync: {telegram}")

        # 3. Ask the panel to poll us every poll_time_ms (V >= 2).
        if self._version >= 2:
            await self._send_new(IK_DATA, NK_POLLTIME_SET, self._poll_time_ms.to_bytes(2, "big"))
        else:
            await self._send_new(IK_POLL)

    async def _wait_master(self) -> S2Telegram:
        """Wait for the next accepted master telegram; request service when work is queued."""
        service_requested = False
        while True:
            if self._get_task is None:
                self._get_task = asyncio.ensure_future(self._rx_envelopes.get())
            waiters: set[asyncio.Future] = {self._get_task}
            signal_task = None
            if not service_requested and self._inflight is None:
                signal_task = asyncio.ensure_future(self._tx_signal.wait())
                waiters.add(signal_task)
            done, _ = await asyncio.wait(
                waiters, timeout=FUNCTION_MONITORING_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
            )
            if signal_task is not None and not signal_task.done():
                signal_task.cancel()
            if self._get_task not in done:
                if signal_task is not None and signal_task in done:
                    # Ask the master to poll us now instead of after the poll time.
                    service_requested = True
                    await self._write(
                        S2Telegram(self._tc, self._rc, IK_SERVICE_REQUEST, version=self._version)
                    )
                    continue
                raise TelenotConnectionError("No telegram from panel (function monitoring)")
            item = self._get_task.result()
            self._get_task = None
            if isinstance(item, Exception):
                raise item
            telegram = self._decode(*item)
            if telegram is None:
                continue
            verdict = self._classify(telegram)
            if verdict == "accept":
                return telegram
            if verdict == "resend":
                await self._resend(MAX_REPETITIONS)

    async def _run(self) -> None:
        error: Exception | None = None
        try:
            while True:
                telegram = await self._wait_master()
                # Every accepted telegram acknowledges our previous one.
                if self._inflight is not None:
                    if not self._inflight.future.done():
                        self._inflight.future.set_result(None)
                    self._inflight = None
                self._handle_master(telegram)

                if self._closing.is_set():
                    if self._version >= 3:
                        await self._send_new(IK_POLL, NK_SESSION_CLOSE)
                    await asyncio.sleep(SLAVE_POLL_DELAY)
                    return
                job = await self._next_job()
                if job is None:
                    await self._send_new(IK_POLL)
                else:
                    self._inflight = job
                    await self._send_new(IK_DATA, job.nk, job.data)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - reported via on_closed
            error = err
            _LOGGER.debug("S2 session ended: %s", err)
        finally:
            self._fail_pending(error or TelenotConnectionError("Session closed"))
            await self._teardown()
            if self._on_closed is not None:
                self._on_closed(error)

    async def _next_job(self) -> _TxJob | None:
        try:
            job = await asyncio.wait_for(self._tx_queue.get(), SLAVE_POLL_DELAY)
        except TimeoutError:
            job = None
        if self._tx_queue.empty() and not self._closing.is_set():
            self._tx_signal.clear()
        return job

    def _handle_master(self, telegram: S2Telegram) -> None:
        if telegram.ik in (IK_ERROR_UNKNOWN_IK, IK_ERROR_UNKNOWN_PK):
            raise TelenotProtocolError(f"Panel reported protocol error IK={telegram.ik}")
        if telegram.nk in (NK_SESSION_CANCELLED, NK_VERSION_MISMATCH, NK_INVALID_ID):
            raise TelenotProtocolError(f"Panel cancelled the session (NK=0x{telegram.nk:02X})")
        if telegram.nk == NK_SESSION_CLOSE:
            raise TelenotConnectionError("Panel closed the session")
        if telegram.ik == IK_POLL and telegram.nk == NK_POLLTIME_ACK:
            self._polltime_acked = True
            return
        if telegram.ik == IK_DATA and telegram.nk == NK_USERDATA and telegram.data:
            if self._on_userdata is not None:
                try:
                    self._on_userdata(telegram.data)
                except Exception:  # noqa: BLE001 - never kill the transport
                    _LOGGER.exception("Error handling user data %s", telegram.data.hex())

    def _fail_pending(self, error: Exception) -> None:
        jobs = [self._inflight] if self._inflight else []
        while not self._tx_queue.empty():
            jobs.append(self._tx_queue.get_nowait())
        self._inflight = None
        for job in jobs:
            if not job.future.done():
                job.future.set_exception(error)

    async def _teardown(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [t for t in (self._reader_task, self._get_task) if t is not None]
        for task in tasks:
            task.cancel()
        if self._writer is not None:
            self._writer.close()
            try:
                await asyncio.wait_for(self._writer.wait_closed(), 2)
            except (OSError, TimeoutError):
                pass
        current = asyncio.current_task()
        for task in tasks:
            if task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
