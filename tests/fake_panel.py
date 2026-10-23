"""Minimal simulated Telenot panel (S2 master + BuildSec keypad) for tests.

Implements the master side independently from the client code, following the
protocol notes in docs/PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

from custom_components.telenot_buildsec.pytelenot.crypto import (
    derive_betreiber_key,
    seal,
    unseal,
)

LCD_IDLE = ("  Telenot       ", "  Unscharf      ")


def lcd_block(line1: str, line2: str) -> bytes:
    return (
        b"\x54\x1b\x80"
        + line1.ljust(16)[:16].encode("latin-1")
        + b"\x1b\xc0"
        + line2.ljust(16)[:16].encode("latin-1")
    )


def rec(block: bytes) -> bytes:
    """com2BUS block -> record [len][kennung][payload]."""
    return bytes([len(block) - 1]) + block


def st70(block: bytes, gf: int = 0x10) -> bytes:
    return bytes([len(block) + 1, 0x70, gf]) + block


class FakePanel:
    """Accepts one client at a time and behaves like a complex400 keypad."""

    def __init__(
        self,
        *,
        key_number: int = 9997,
        freischaltcode: bytes = bytes.fromhex("0123456789abcdef"),
        aes_key: bytes | None = None,
        code: str = "123456",
        busy: bool = False,
        poll_interval: float = 0.05,
        reject_modern: bool = False,
    ) -> None:
        self.key_number = key_number
        self.freischaltcode = freischaltcode
        self.aes_key = aes_key
        self.code = code
        self.busy = busy
        self.poll_interval = poll_interval
        self.reject_modern = reject_modern
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.received_keys: list[int] = []
        self.received_records: list[bytes] = []
        self.session_closed = asyncio.Event()
        self.armed = "disarmed"
        self.switches = 0
        self.alarm = False
        self._entering: int | None = None
        self._digits = ""
        self._outbox: list[bytes] = []
        self._writer: asyncio.StreamWriter | None = None
        self.client_pk: int | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def drop_connection(self) -> None:
        if self._writer is not None:
            self._writer.close()

    # ---------------------------------------------------------------- panel logic

    def push(self, *blocks: bytes) -> None:
        """Queue com2BUS blocks to send inside a 0x99 answer."""
        payload = b"".join(rec(b) for b in blocks)
        self._outbox.append(st70(b"\x99\x0f" + payload))

    def led_block(self) -> bytes:
        free1 = {"armed_away": 2, "armed_home": 0}.get(self.armed, 0)
        free2 = 3 if self.armed == "armed_home" else 0
        b2 = 0x01 | (0x02 if self.alarm else 0) | (free1 << 4) | (free2 << 6)
        return bytes([0xC0, 0x00, b2, 0x00, 0b0000_0101, 0, 0b0000_0010, 0, 0, 0, 0, 0])

    def state_blocks(self) -> list[bytes]:
        text = {
            "disarmed": "  Unscharf",
            "armed_away": "  Extern scharf",
            "armed_home": "  Intern scharf",
        }[self.armed]
        return [
            lcd_block("  Telenot", text),
            self.led_block(),
            bytes([0xCA, self.switches & 0xFF, self.switches >> 8]),
        ]

    def on_record(self, data: bytes) -> None:
        if len(data) == 1:
            self.on_key(data[0])
            return
        self.received_records.append(data)
        block = data[3:]
        kind = block[0]
        expected_code = self._code_bytes()
        if kind == 0x8E:
            assert block[1:4] == expected_code
            self.session_closed.set()
            return
        if kind in (0x92, 0x94, 0x9E, 0x8A, 0x8C):
            if self.reject_modern and kind in (0x92, 0x94):
                self._outbox.append(st70(b"\x23\x14"))
                return
            if block[1:4] != expected_code:
                self._outbox.append(st70(b"\x23\x10"))
                return
            reply = {0x92: 0x93, 0x94: 0x95, 0x9E: 0x9F, 0x8A: 0x8B, 0x8C: 0x8D}[kind]
            self._outbox.append(st70(bytes([reply, block[4], 0x12, 0x34])))
            self.push(*self.state_blocks())
        elif kind == 0x18:
            self._outbox.append(st70(bytes([0x19, 0xB9, 2, 5, 0xFF]) + bytes(12), 0x00))
        elif kind == 0x98 and block[-1] == 0xEA:
            self.push(self.label(1, 0, "Extern", "scharf"))
            self.push(self.label(0, 4, "Erdgeschoss", ""), self.label(0, 36, "Licht", ""))
            self.push(self.label(0, 37, "Ein", ""), self.label(0, 38, "Aus", ""))
            self.push(bytes([0xCD, 0x02, 0x12, 0x34]))

    @staticmethod
    def label(puk: int, index: int, t1: str, t2: str) -> bytes:
        a, b = t1.encode("latin-1"), t2.encode("latin-1")
        return bytes([0xCD, puk, index, len(a), len(b)]) + a + b

    def _code_bytes(self) -> bytes:
        from custom_components.telenot_buildsec.pytelenot.buildsec import encode_code

        return encode_code(self.code)

    def on_key(self, key: int) -> None:
        self.received_keys.append(key)
        if key in (0x45, 0x49, 0x41):
            self._entering = key
            self._digits = ""
            self.push(lcd_block("Code eingeben:", ""), bytes([0xCE, key]))
        elif 0x30 <= key <= 0x39 and self._entering is not None:
            self._digits += chr(key)
            self.push(lcd_block("Code eingeben:", "*" * len(self._digits)), bytes([0xCE, key]))
        elif key == 0x42 and self._entering is not None:
            if self._digits == self.code:
                self.armed = {0x45: "armed_away", 0x49: "armed_home", 0x41: "disarmed"}[
                    self._entering
                ]
                if self.armed == "disarmed":
                    self.alarm = False
                self.push(bytes([0xCE, key]), *self.state_blocks())
            else:
                self.push(bytes([0xCE, key]), lcd_block("Code falsch", ""))
            self._entering = None
        elif 0xD0 <= key <= 0xDF:
            self.switches ^= 1 << (key - 0xD0)
            self.push(
                bytes([0xCE, key]),
                bytes([0xCF, 0]),
                bytes([0xCA, self.switches & 0xFF, self.switches >> 8]),
            )
        elif 0x80 <= key <= 0x9F:
            self.push(bytes([0xCE, key]), bytes([0xCF, 2]))
        else:
            self.push(bytes([0xCE, key]))

    def trigger_alarm(self) -> None:
        self.alarm = True
        self.push(self.led_block())

    # ---------------------------------------------------------------- S2 master

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        try:
            await self._session(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _read(self, reader, key):
        k, sl = struct.unpack(">HH", await reader.readexactly(4))
        raw = unseal(k, await reader.readexactly(sl), key)
        assert raw is not None, "bad checksum from client"
        tc, _crc, rc, ik, pk, ln = struct.unpack_from(">IHIBBB", raw)
        if self.client_pk is None:
            self.client_pk = pk  # mirror the header the slave chose
        assert pk == self.client_pk, "slave switched header"
        if pk == 0x01:
            if ik == 7:
                assert tc == 0 and rc == 0, "VdS SERVICE_REQUEST must carry TC=RC=0"
            return k, tc, rc, ik, 1, raw[13 : 13 + ln]
        assert pk == 0x82 and ln == 4
        nl, v, nk = struct.unpack_from(">HBB", raw, 13)
        return k, tc, rc, ik, nk, raw[17 : 17 + nl]

    async def _read_reply(self, reader, key):
        """Read the slave's reply, skipping SERVICE_REQUESTs (we poll right away anyway)."""
        while True:
            result = await self._read(reader, key)
            if result[3] != 7:
                return result

    async def _session(self, reader, writer) -> None:
        self.client_pk = None
        m = 0x10203040
        window = 0xFF if self.busy else 0x01
        sync = struct.pack(">IHIBBB", m, 0, 0, 1, 0x01, 1) + bytes([window])
        writer.write(seal(sync, 0, None))
        await writer.drain()
        if self.busy:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader.read(1), 1)
            return
        key = (
            derive_betreiber_key(self.freischaltcode, m)
            if self.key_number == 9997
            else self.aes_key
        )
        try:
            k, tc, rc, ik, nk, data = await asyncio.wait_for(self._read(reader, key), 2)
        except AssertionError:
            # wrong key: keep repeating SYNC_REQUEST like a real master
            for _ in range(4):
                writer.write(seal(sync, 0, None))
                await writer.drain()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(reader.readexactly(4 + 128), 1)
            return
        assert k == self.key_number and ik == 2 and rc == m + 1
        my_tc = m
        slave_tc = tc
        while True:
            my_tc += 1
            if self._outbox:
                data_out, ik_out = self._outbox.pop(0), 4
            else:
                data_out, ik_out = b"", 3
            if self.client_pk == 0x01:
                tel = (
                    struct.pack(">IHIBBB", my_tc, 0, slave_tc + 1, ik_out, 0x01, len(data_out))
                    + data_out
                )
            else:
                tel = (
                    struct.pack(
                        ">IHIBBBHBB", my_tc, 0, slave_tc + 1, ik_out, 0x82, 4, len(data_out), 3, 1
                    )
                    + data_out
                )
            writer.write(seal(tel, self.key_number, key))
            await writer.drain()
            k, tc, rc, ik, nk, data = await self._read_reply(reader, key)
            assert tc == slave_tc + 1, (hex(tc), hex(slave_tc), ik, nk)
            assert rc == my_tc + 1
            slave_tc = tc
            if nk == 0x09 and self.client_pk == 0x82:
                self.session_closed.set()
                return
            if ik == 4 and nk == 0x05:
                ack = struct.pack(">IHIBBBHBB", my_tc + 1, 0, slave_tc + 1, 3, 0x82, 4, 0, 3, 6)
                my_tc += 1
                writer.write(seal(ack, self.key_number, key))
                await writer.drain()
                k, tc, rc, ik, nk, data = await self._read_reply(reader, key)
                assert tc == slave_tc + 1
                slave_tc = tc
                if ik == 4 and nk == 1:
                    self.on_record(data)
            elif ik == 4 and nk == 1:
                self.on_record(data)
            if not self._outbox:
                await self._wait_poll(reader)

    async def _wait_poll(self, reader) -> None:
        await asyncio.sleep(self.poll_interval)
