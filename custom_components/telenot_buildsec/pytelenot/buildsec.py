"""Telenot BuildSec remote keypad ("Bedienteil") protocol on top of S2.

After the S2 session is up, the client opens a remote service session with a
Satztyp 0x70 block (operator 0x92 / installer 0x94 / Hipas remote control 0x9E)
containing the keypad code. The panel then *pushes* tunnelled com2BUS keypad
telegrams (LCD, LEDs, buzzer, switching-function states). Commands are plain
one-byte key codes, exactly like a physical keypad.

Reimplemented from Telenot.BuildSec.Data.DataTransfer (DeviceOperations,
DatenblockToBedienteilTranslator, Datenbloecke.*) of the BuildSec app.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from .crypto import KEY_NUMBER_BETREIBER, KEY_NUMBER_PLAIN
from .exceptions import (
    TelenotAuthError,
    TelenotConnectionError,
    TelenotError,
    TelenotPanelError,
    TelenotProtocolError,
)
from .s2 import S2Session

_LOGGER = logging.getLogger(__name__)

SATZTYP_TRANSPARENT = 0x70
GERAET_FREI = 0x10

NUM_FREE_LEDS = 4
NUM_AREAS = 32  # "Meldebereiche"
NUM_SWITCHES = 16  # "Schaltfunktionen"


class AccessLevel(IntEnum):
    """Access level (BerechtigungsEbene) of the remote session."""

    INSTALLER = 0  # Errichter
    OPERATOR = 1  # Betreiber
    REMOTE_CONTROL = 2  # Hipas Fernbedienung


# St70 Telenot datablock ids ------------------------------------------------------
ST70_GTYP_REQUEST = 0x18
ST70_GTYP_DATA = 0x19
ST70_ERROR = 0x23
ST70_FERNVERB_OK = 0x4B
ST70_END_SESSION = 0x8E  # sent by the iOS app before it disconnects (observed)
ST70_COM2BUS_REQUEST = 0x98
ST70_COM2BUS_ANSWER = 0x99

START_BLOCK = {
    AccessLevel.OPERATOR: 0x92,
    AccessLevel.INSTALLER: 0x94,
    AccessLevel.REMOTE_CONTROL: 0x9E,
}
LEGACY_START_BLOCK = {AccessLevel.OPERATOR: 0x8A, AccessLevel.INSTALLER: 0x8C}
STARTED_BLOCKS = frozenset({0x93, 0x95, 0x9F, 0x8B, 0x8D})

# com2BUS keypad datablocks --------------------------------------------------------
C2B_LCD_TEXT = 0x54
C2B_LEDS_STEADY = 0xC0
C2B_LEDS_BLINK = 0xC1
C2B_SWITCH_STATE = 0xCA
C2B_LABELS = 0xCD
C2B_KEY_ACK = 0xCE
C2B_PANEL_RESPONSE = 0xCF
C2B_TEXT_REQUEST = 0xEA

PANEL_RESPONSE_FUNCTION_DISABLED = 2

ERROR_REASONS: dict[int, str] = {
    0x10: "Code incorrect",
    0x11: "Checksum incorrect",
    0x13: "Wrong record length",
    0x14: "Unknown identifier",
    0x1B: "App access (remote service) is disabled in the panel",
    0x1D: "Invalid keypad address",
    0x1E: "com2BUS of the transmitter is not activated for the panel",
    0x1F: "com2BUS fault towards the panel",
    0x20: "Code disabled",
    0x21: "Access code is not enabled for this keypad",
    0x22: "Operating level too low",
    0x23: "Keypad address already in use - choose another keypad address",
    0x24: "Authorisation failed",
    0x25: "Input locked",
    0x26: "LAN connection not allowed",
    0x27: "WAN connection not allowed",
    0x28: "hiXserver connection not allowed",
    0x29: "IP address not authorised",
    0x30: "Locked by time model",
    0x99: "Unknown error",
}
AUTH_REASONS = frozenset({0x10, 0x20, 0x21, 0x22, 0x24, 0x25, 0x30})
LEGACY_FALLBACK_REASONS = frozenset({0x14, 0x1B})


class Key(IntEnum):
    """Key codes of the virtual keypad (res/layout/fragment_bedienteil.xml)."""

    DIGIT_0 = 0x30
    DIGIT_1 = 0x31
    DIGIT_2 = 0x32
    DIGIT_3 = 0x33
    DIGIT_4 = 0x34
    DIGIT_5 = 0x35
    DIGIT_6 = 0x36
    DIGIT_7 = 0x37
    DIGIT_8 = 0x38
    DIGIT_9 = 0x39
    UP = 0x2D
    DOWN = 0x2B
    OK = 0x42
    ESC = 0x53
    ARM_AWAY = 0x45  # "ES" extern scharf
    ARM_HOME = 0x49  # "IS" intern scharf
    DISARM = 0x41  # "US" unscharf
    FUNCTION = 0x4E  # freely programmable key "O"


def switch_key(index: int) -> int:
    """Key code toggling switching function ``index`` (1-16)."""
    if not 1 <= index <= NUM_SWITCHES:
        raise ValueError("switch index out of range")
    return 0xD0 + index - 1


def area_lock_key(index: int) -> int:
    """Key code toggling the lock (bypass) of Meldebereich ``index`` (1-32)."""
    if not 1 <= index <= NUM_AREAS:
        raise ValueError("area index out of range")
    return 0x80 + index - 1


class LedColor(StrEnum):
    """Colour of a free keypad LED."""

    OFF = "off"
    GREEN = "green"
    RED = "red"
    YELLOW = "yellow"


_COLORS = (LedColor.OFF, LedColor.GREEN, LedColor.RED, LedColor.YELLOW)


class Buzzer(StrEnum):
    """Buzzer pattern (highest priority wins, as in the app)."""

    OFF = "off"
    SHORT = "1s"
    LONG = "3s"
    INTERMITTENT = "intermittent"
    BEEP_PAUSE = "beep_pause"
    CONTINUOUS = "continuous"


# priority order from DatenblockToBedienteilTranslator (last wins)
_BUZZER_BITS = (
    (0x01, Buzzer.SHORT),
    (0x08, Buzzer.BEEP_PAUSE),
    (0x10, Buzzer.CONTINUOUS),
    (0x04, Buzzer.INTERMITTENT),
    (0x02, Buzzer.LONG),
)

_LCD_FIX = {0xE1: 0xE4, 0xE2: 0xDF, 0xEF: 0xF6, 0xF5: 0xFC, 0xE8: 0x76}  # ä ß ö ü v
_LCD_TEST = b"\xff" * 16


def lcd_decode(raw: bytes) -> str:
    """Decode keypad LCD bytes (ISO-8859-1 with HD44780 umlauts)."""
    return bytes(_LCD_FIX.get(b, b) for b in raw).decode("latin-1").strip().strip("\0").strip()


def encode_code(code: str) -> bytes:
    """Encode a keypad code as 3 bytes packed BCD, low nibble first, 0xF padded."""
    if not code.isdigit() or not 1 <= len(code) <= 6:
        raise ValueError("The keypad code must have 1 to 6 digits")
    out = bytearray(b"\xff" * 3)
    for i in range(len(code) // 2):
        out[i] = (int(code[2 * i + 1]) << 4) | int(code[2 * i])
    if len(code) % 2:
        out[len(code) // 2] = 0xF0 | int(code[-1])
    return bytes(out)


def st70_record(datablock: bytes, geraet_frei: int = GERAET_FREI) -> bytes:
    """Wrap a Telenot datablock in a VdS 2465 Satztyp 0x70 record."""
    return bytes([len(datablock) + 1, SATZTYP_TRANSPARENT, geraet_frei]) + datablock


def split_records(data: bytes) -> list[bytes]:
    """Split back-to-back records ``[len][type][len bytes]`` (returns ``type+payload``)."""
    out = []
    i = 0
    while i + 1 < len(data):
        length = data[i]
        out.append(data[i + 1 : i + 2 + length])
        i += 2 + length
    return out


@dataclass(slots=True)
class Led:
    """A keypad LED: steady state from 0xC0 overlaid by blinking from 0xC1."""

    on: bool = False
    blinking: bool = False
    color: LedColor = LedColor.OFF

    @property
    def active(self) -> bool:
        """LED lit (steady or blinking)."""
        return self.on or self.blinking


@dataclass(slots=True)
class Labels:
    """Label texts downloaded from the panel (Beschriftungstexte)."""

    crc: int = 0
    free_leds: dict[int, str] = field(default_factory=dict)
    areas: dict[int, str] = field(default_factory=dict)
    switches: dict[int, str] = field(default_factory=dict)
    switch_on_text: dict[int, str] = field(default_factory=dict)
    switch_off_text: dict[int, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """Serialise for storage."""
        return {
            "crc": self.crc,
            "free_leds": self.free_leds,
            "areas": self.areas,
            "switches": self.switches,
            "switch_on_text": self.switch_on_text,
            "switch_off_text": self.switch_off_text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Labels:
        """Load from storage (JSON turns int keys into strings)."""

        def ints(d: dict | None) -> dict[int, str]:
            return {int(k): v for k, v in (d or {}).items()}

        return cls(
            crc=int(data.get("crc", 0)),
            free_leds=ints(data.get("free_leds")),
            areas=ints(data.get("areas")),
            switches=ints(data.get("switches")),
            switch_on_text=ints(data.get("switch_on_text")),
            switch_off_text=ints(data.get("switch_off_text")),
        )

    def apply(self, index: int, text1: str | None, text2: str | None) -> None:
        """Store one label record (EBeschriftungIndex)."""
        joined = " ".join(t for t in (text1, text2) if t).strip()
        if 0 <= index <= 3:
            target, key = self.free_leds, index + 1
        elif 4 <= index <= 35:
            target, key = self.areas, index - 3
        elif 36 <= index <= 83:
            n, kind = divmod(index - 36, 3)
            target = (self.switches, self.switch_on_text, self.switch_off_text)[kind]
            key = n + 1
        else:
            _LOGGER.debug("Ignoring label with unknown index %s", index)
            return
        if joined:
            target[key] = joined
        else:
            target.pop(key, None)


@dataclass(slots=True)
class PanelState:
    """Everything the virtual keypad shows."""

    connected: bool = False
    # True once the panel has sent display text and LEDs after the (re)connect
    complete: bool = False
    display: list[str] = field(default_factory=lambda: ["", ""])
    buzzer: Buzzer = Buzzer.OFF
    operation: Led = field(default_factory=Led)  # Betrieb
    alarm: Led = field(default_factory=Led)
    fault: Led = field(default_factory=Led)  # Störung
    walk_test: Led = field(default_factory=Led)  # Gehtest
    free_leds: list[Led] = field(default_factory=lambda: [Led() for _ in range(NUM_FREE_LEDS)])
    area_info: list[Led] = field(default_factory=lambda: [Led() for _ in range(NUM_AREAS)])
    area_locked: list[Led] = field(default_factory=lambda: [Led() for _ in range(NUM_AREAS)])
    switches: list[bool] = field(default_factory=lambda: [False] * NUM_SWITCHES)
    device_family: int | None = None
    firmware: str | None = None
    legacy: bool = False


class BuildSecClient:
    """Remote keypad session with a Telenot panel."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        code: str,
        access_level: AccessLevel = AccessLevel.OPERATOR,
        keypad_address: int = 0,
        key_number: int = KEY_NUMBER_PLAIN,
        aes_key: bytes | None = None,
        freischaltcode: bytes | None = None,
        extended_header: bool = False,
    ) -> None:
        """Initialise. Operator access uses key 9997 derived from the Freischaltcode."""
        if not 0 <= keypad_address <= 15:
            raise ValueError("Keypad address must be 0..15")
        self._code = encode_code(code)
        self._access_level = access_level
        self._keypad_address = keypad_address
        if access_level == AccessLevel.OPERATOR:
            key_number = KEY_NUMBER_BETREIBER
        self._s2_args = {
            "key_number": key_number,
            "aes_key": aes_key,
            "freischaltcode": freischaltcode,
            "extended_header": extended_header,
        }
        self._extended_header = extended_header
        self._host = host
        self._port = port
        self._s2: S2Session | None = None

        self.state = PanelState()
        self.labels = Labels()
        self.text_crc: int | None = None

        self._listeners: list[Callable[[], None]] = []
        self._disconnect_listeners: list[Callable[[Exception | None], None]] = []
        self._started: asyncio.Future[bytes] | None = None
        self._gtyp: asyncio.Future[bytes] | None = None
        self._labels_done: asyncio.Future[int] | None = None
        self._pending_labels: Labels | None = None
        self._key_ack: asyncio.Future[int] | None = None
        self._panel_response = 0
        self._display_changed = asyncio.Event()
        self._seen: set[int] = set()

    # ------------------------------------------------------------------ lifecycle

    @property
    def connected(self) -> bool:
        """Return True while the remote session is open."""
        return self._s2 is not None and self._s2.connected and self.state.connected

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a state-change callback; returns an unsubscribe function."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def add_disconnect_listener(
        self, callback: Callable[[Exception | None], None]
    ) -> Callable[[], None]:
        """Register a callback for (unexpected) session end."""
        self._disconnect_listeners.append(callback)
        return lambda: (
            self._disconnect_listeners.remove(callback)
            if callback in self._disconnect_listeners
            else None
        )

    async def connect(self, *, timeout: float = 30) -> None:
        """Open TCP/S2, start the remote service session and read the device type."""
        self.state = PanelState()
        self._seen: set[int] = set()
        loop = asyncio.get_running_loop()
        self._started = loop.create_future()
        session: S2Session | None = None

        def on_userdata(data: bytes) -> None:
            if session is self._s2:
                self._on_userdata(data)

        def on_closed(error: Exception | None) -> None:
            if session is self._s2:
                self._on_closed(error)

        session = S2Session(
            self._host,
            self._port,
            on_userdata=on_userdata,
            on_closed=on_closed,
            **self._s2_args,
        )
        self._s2 = session
        try:
            await session.connect()
        except BaseException:
            self._s2 = None
            raise
        try:
            block = await self._start_session(START_BLOCK[self._access_level], timeout)
        except TelenotPanelError as err:
            if (
                err.reason not in LEGACY_FALLBACK_REASONS
                or self._access_level not in LEGACY_START_BLOCK
            ):
                await self.disconnect()
                raise
            _LOGGER.debug("Panel rejected modern start (%s); trying legacy mode", err)
            self.state.legacy = True
            try:
                block = await self._start_session(LEGACY_START_BLOCK[self._access_level], timeout)
            except BaseException:
                await self.disconnect()
                raise
        except BaseException:
            await self.disconnect()
            raise
        self.text_crc = (block[2] << 8) | block[3] if len(block) >= 4 else 0
        self.state.connected = True
        await self._read_device_type()
        self._notify()

    async def disconnect(self) -> None:
        """End the remote session and close the socket."""
        if self._s2 is not None and self.state.connected and not self._extended_header:
            # Like the iOS app: announce the end of the session, then close.
            try:
                await asyncio.wait_for(
                    self._s2.send(st70_record(bytes([ST70_END_SESSION]) + self._code)), 5
                )
            except (TimeoutError, TelenotError) as err:
                _LOGGER.debug("End-of-session block not acknowledged: %s", err)
        s2, self._s2 = self._s2, None
        was_connected = self.state.connected
        self.state.connected = False
        for fut in (self._started, self._gtyp, self._labels_done, self._key_ack):
            if fut is not None and not fut.done():
                fut.set_exception(TelenotConnectionError("Disconnected"))
        if was_connected:
            self._notify()
        if s2 is not None:
            await s2.close()

    async def _start_session(self, block_id: int, timeout: float) -> bytes:
        assert self._s2 is not None
        self._started = asyncio.get_running_loop().create_future()
        datablock = bytes([block_id]) + self._code + bytes([self._keypad_address])
        await self._s2.send(st70_record(datablock))
        return await asyncio.wait_for(self._started, timeout)

    async def _read_device_type(self) -> None:
        assert self._s2 is not None
        self._gtyp = asyncio.get_running_loop().create_future()
        try:
            await self._s2.send(st70_record(bytes([ST70_GTYP_REQUEST]) + self._code, 0x00))
            block = await asyncio.wait_for(self._gtyp, 10)
        except (TimeoutError, TelenotPanelError) as err:
            _LOGGER.debug("Device type not available: %s", err)
            return
        if len(block) >= 5:
            self.state.device_family = block[1]
            self.state.firmware = f"{block[2]}.{block[3]:02d}" + (
                f" beta {block[4]}" if block[4] != 0xFF else ""
            )

    # ------------------------------------------------------------------ commands

    async def press(self, key: int) -> None:
        """Send one key code (fire and forget, as the app's keypad does)."""
        if self._s2 is None:
            raise TelenotConnectionError("Not connected")
        self._display_changed.clear()
        await self._s2.send(bytes([key]))

    async def press_and_confirm(self, key: int, timeout: float = 20) -> int:
        """Send a key code, wait for the panel's key ack (0xCE) and return its response (0xCF).

        A response of 2 means "operation not possible, function disabled".
        """
        if self._s2 is None:
            raise TelenotConnectionError("Not connected")
        self._panel_response = 0
        self._key_ack = asyncio.get_running_loop().create_future()
        await self._s2.send(bytes([key]))
        try:
            await asyncio.wait_for(self._key_ack, timeout)
        finally:
            self._key_ack = None
        # the 0xCF response may follow the ack in the same or the next telegram
        await asyncio.sleep(0.3)
        return self._panel_response

    async def wait_display_change(self, timeout: float) -> bool:
        """Wait until the LCD content changes; return False on timeout."""
        try:
            await asyncio.wait_for(self._display_changed.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def load_labels(self, timeout: float = 300) -> Labels:
        """Download the label texts (free LEDs, areas, switching functions)."""
        if self._s2 is None:
            raise TelenotConnectionError("Not connected")
        self._pending_labels = Labels()
        self._labels_done = asyncio.get_running_loop().create_future()
        datablock = (
            bytes([ST70_COM2BUS_REQUEST])
            + self._code
            + bytes([self._keypad_address, 0x00, C2B_TEXT_REQUEST])
        )
        await self._s2.send(st70_record(datablock))
        try:
            crc = await asyncio.wait_for(self._labels_done, timeout)
        finally:
            self._labels_done = None
        labels, self._pending_labels = self._pending_labels, None
        labels.crc = crc if crc is not None else (self.text_crc or 0)
        self.labels = labels
        self._notify()
        return labels

    # ------------------------------------------------------------------ receive path

    def _on_closed(self, error: Exception | None) -> None:
        was_connected = self.state.connected
        self.state.connected = False
        for fut in (self._started, self._gtyp, self._labels_done, self._key_ack):
            if fut is not None and not fut.done():
                fut.set_exception(error or TelenotConnectionError("Session closed"))
        if was_connected:
            self._notify()
        for callback in list(self._disconnect_listeners):
            callback(error)

    def _on_userdata(self, data: bytes) -> None:
        if len(data) == 1:
            _LOGGER.debug("Ignoring single byte user data %02X", data[0])
            return
        if len(data) > 3 and data[1] == SATZTYP_TRANSPARENT:
            self._handle_st70(data[3 : 2 + data[0]])
            return
        if self.state.legacy:
            changed = False
            for record in split_records(data):
                changed |= self._handle_com2bus(record)
            if changed:
                self._notify()
            return
        _LOGGER.debug("Ignoring user data %s", data.hex(" "))

    def _handle_st70(self, block: bytes) -> None:
        if not block:
            return
        kind = block[0]
        if kind in STARTED_BLOCKS:
            self._resolve(self._started, block)
        elif kind == ST70_GTYP_DATA:
            self._resolve(self._gtyp, block)
        elif kind == ST70_ERROR:
            reason = block[1] if len(block) > 1 else 0x99
            message = ERROR_REASONS.get(reason, "Unknown error")
            exc_type = TelenotAuthError if reason in AUTH_REASONS else TelenotPanelError
            error = exc_type(reason, message)
            _LOGGER.debug("Panel error: %s", error)
            for fut in (self._started, self._gtyp, self._labels_done):
                if fut is not None and not fut.done():
                    fut.set_exception(error)
                    break
        elif kind == ST70_FERNVERB_OK:
            _LOGGER.debug("FernVerbAbgebOk")
        elif kind == ST70_COM2BUS_ANSWER and len(block) > 2:
            changed = False
            for record in split_records(block[2:]):
                changed |= self._handle_com2bus(record)
            if changed:
                self._notify()
        else:
            _LOGGER.debug("Unknown St70 block %s", block.hex(" "))

    @staticmethod
    def _resolve(fut: asyncio.Future | None, value: object) -> None:
        if fut is not None and not fut.done():
            fut.set_result(value)

    def _handle_com2bus(self, block: bytes) -> bool:
        """Apply one com2BUS datablock; return True if visible state changed."""
        if not block:
            return False
        kind = block[0]
        if kind == C2B_LCD_TEXT:
            self._seen.add(kind)
            return self._apply_lcd(block) | self._check_complete()
        if kind == C2B_LEDS_STEADY:
            self._seen.add(kind)
            return self._apply_leds(block, blink=False) | self._check_complete()
        if kind == C2B_LEDS_BLINK:
            return self._apply_leds(block, blink=True)
        if kind == C2B_SWITCH_STATE and len(block) >= 3:
            bits = block[1] | (block[2] << 8)
            new = [bool(bits >> i & 1) for i in range(NUM_SWITCHES)]
            changed = new != self.state.switches
            self.state.switches = new
            return changed
        if kind == C2B_LABELS:
            self._apply_label(block)
            return False
        if kind == C2B_KEY_ACK:
            self._resolve(self._key_ack, block[1] if len(block) > 1 else 0)
            return False
        if kind == C2B_PANEL_RESPONSE and len(block) > 1:
            self._panel_response = block[1]
            return False
        _LOGGER.debug("Unhandled com2BUS block %s", block.hex(" "))
        return False

    def _check_complete(self) -> bool:
        if self.state.complete or not {C2B_LCD_TEXT, C2B_LEDS_STEADY} <= self._seen:
            return False
        self.state.complete = True
        return True

    def _apply_lcd(self, block: bytes) -> bool:
        changed = False
        for marker, line in ((0x80, 0), (0xC0, 1)):
            idx = block.find(bytes([0x1B, marker]))
            if idx < 0:
                continue
            raw = block[idx + 2 : idx + 18]
            text = "" if raw == _LCD_TEST else lcd_decode(raw)
            if self.state.display[line] != text:
                self.state.display[line] = text
                changed = True
        if changed:
            self._display_changed.set()
        return changed

    def _apply_leds(self, block: bytes, *, blink: bool) -> bool:
        state = self.state
        before = repr(
            (
                state.operation,
                state.alarm,
                state.fault,
                state.walk_test,
                state.free_leds,
                state.area_info,
                state.area_locked,
                state.buzzer,
            )
        )
        n = len(block)

        def set_led(led: Led, value: bool, color: LedColor) -> None:
            if blink:
                led.blinking = value
                if value:
                    led.color = color
            else:
                led.on = value
                if value or not led.blinking:
                    led.color = color if value else LedColor.OFF

        def set_free(led: Led, color: LedColor) -> None:
            set_led(led, color != LedColor.OFF, color)

        if n >= 2 and not blink:
            buzzer = Buzzer.OFF
            for bit, pattern in _BUZZER_BITS:
                if block[1] & bit:
                    buzzer = pattern
            state.buzzer = buzzer
        if n >= 3:
            b = block[2]
            set_led(state.operation, bool(b & 0x01), LedColor.GREEN)
            set_led(state.alarm, bool(b & 0x02), LedColor.RED)
            set_led(state.fault, bool(b & 0x04), LedColor.YELLOW)
            set_led(state.walk_test, bool(b & 0x08), LedColor.GREEN)
            set_free(state.free_leds[0], _COLORS[(b >> 4) & 3])
            set_free(state.free_leds[1], _COLORS[(b >> 6) & 3])
        if n >= 4:
            set_free(state.free_leds[2], _COLORS[block[3] & 3])
            set_free(state.free_leds[3], _COLORS[(block[3] >> 2) & 3])
        for offset, leds, base, color in (
            (4, state.area_info, 0, LedColor.RED),
            (6, state.area_locked, 0, LedColor.YELLOW),
            (8, state.area_info, 16, LedColor.RED),
            (10, state.area_locked, 16, LedColor.YELLOW),
        ):
            for byte_no in range(2):
                pos = offset + byte_no  # block[0] is the Kennung, block[1] the buzzer
                if pos >= n:
                    break
                for bit in range(8):
                    set_led(leds[base + byte_no * 8 + bit], bool(block[pos] >> bit & 1), color)
        after = repr(
            (
                state.operation,
                state.alarm,
                state.fault,
                state.walk_test,
                state.free_leds,
                state.area_info,
                state.area_locked,
                state.buzzer,
            )
        )
        return before != after

    def _apply_label(self, block: bytes) -> None:
        labels = self._pending_labels
        if len(block) <= 2 or block[1] == 2:
            crc = (block[-2] << 8) | block[-1] if len(block) >= 4 else None
            self._resolve(self._labels_done, crc)
            return
        if labels is not None:
            self._parse_label_record(labels, block)

    @staticmethod
    def _parse_label_record(labels: Labels, block: bytes) -> None:
        if len(block) < 5:
            return
        index, len1, len2 = block[2], block[3], block[4]
        text1 = lcd_decode(block[5 : 5 + len1]) if len1 else None
        text2 = lcd_decode(block[5 + len1 : 5 + len1 + len2]) if len2 else None
        labels.apply(index, text1, text2)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error in state listener")


__all__ = [
    "AccessLevel",
    "BuildSecClient",
    "Buzzer",
    "Key",
    "Labels",
    "Led",
    "LedColor",
    "PanelState",
    "TelenotAuthError",
    "TelenotConnectionError",
    "TelenotError",
    "TelenotPanelError",
    "TelenotProtocolError",
    "area_lock_key",
    "encode_code",
    "switch_key",
]
