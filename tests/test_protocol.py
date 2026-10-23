"""Tests for the Telenot S2 / BuildSec protocol implementation."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.telenot.pytelenot import (
    AccessLevel,
    BuildSecClient,
    Key,
    LedColor,
    TelenotAuthError,
    TelenotBusyError,
    TelenotKeyError,
    encode_code,
    switch_key,
)
from custom_components.telenot.pytelenot.crypto import (
    derive_betreiber_key,
    internet_checksum,
    s2_checksum,
    seal,
    unseal,
)

from .fake_panel import FakePanel

FSC = bytes.fromhex("0123456789abcdef")


def test_encode_code() -> None:
    assert encode_code("123456") == bytes.fromhex("214365")
    assert encode_code("1234") == bytes.fromhex("2143ff")
    assert encode_code("12345") == bytes.fromhex("2143f5")
    assert encode_code("999999") == bytes.fromhex("999999")
    with pytest.raises(ValueError):
        encode_code("1234567")


def test_internet_checksum_rfc1071() -> None:
    # RFC 1071 example: 0001 f203 f4f5 f6f7 -> sum ddf2 -> checksum 220d
    assert internet_checksum(bytes.fromhex("0001f203f4f5f6f7")) == 0x220D
    assert internet_checksum(b"\x01") == (~0x0100) & 0xFFFF


def test_derive_key_9997() -> None:
    assert derive_betreiber_key(FSC, 0x12345678).hex() == "1317131f9abcdef096f3fafafea2d7be"


def test_seal_roundtrip_encrypted() -> None:
    key = bytes(range(16))
    telegram = bytes.fromhex("00000001 0000 00000002 04 82 04 0003 03 01 aabbcc".replace(" ", ""))
    env = seal(telegram, 42, key)
    assert env[:2] == b"\x00\x2a"
    assert int.from_bytes(env[2:4], "big") == 128
    out = unseal(42, env[4:], key)
    assert out is not None and out[6:] == telegram[6:]
    assert s2_checksum(out) == int.from_bytes(out[4:6], "big")
    assert unseal(42, env[4:], bytes(16)) is None


@pytest.fixture(autouse=True)
def _sockets(socket_enabled):
    """The HA test plugin blocks sockets; the fake panel needs localhost TCP."""


@pytest.fixture
async def panel():
    p = FakePanel(code="123456")
    await p.start()
    yield p
    await p.stop()


def make_client(panel: FakePanel, **kwargs) -> BuildSecClient:
    args = {"code": "123456", "keypad_address": 15, "freischaltcode": FSC}
    args.update(kwargs)
    return BuildSecClient("127.0.0.1", panel.port, **args)


async def test_connect_and_initial_state(panel: FakePanel) -> None:
    client = make_client(panel)
    await client.connect()
    try:
        assert client.connected
        assert client.text_crc == 0x1234
        assert client.state.device_family == 0xB9
        assert client.state.firmware == "2.05"
        # login record as documented: 06 70 10 92 <code> <bt>
        assert panel.received_records[0] == bytes.fromhex("0670109221436 50f".replace(" ", ""))
        await asyncio.sleep(0.3)
        assert client.state.display == ["Telenot", "Unscharf"]
        assert client.state.operation.on
        assert not client.state.alarm.active
        assert client.state.area_info[0].on and client.state.area_info[2].on
        assert not client.state.area_info[1].on
        assert client.state.area_locked[1].on
    finally:
        await client.disconnect()
    await asyncio.wait_for(panel.session_closed.wait(), 3)


async def test_wrong_code(panel: FakePanel) -> None:
    client = make_client(panel, code="111111")
    with pytest.raises(TelenotAuthError) as err:
        await client.connect()
    assert err.value.reason == 0x10


async def test_wrong_key(panel: FakePanel) -> None:
    client = make_client(panel, freischaltcode=bytes(8))
    with pytest.raises((TelenotKeyError, Exception)):
        await client.connect(timeout=5)


async def test_busy() -> None:
    panel = FakePanel(busy=True)
    await panel.start()
    try:
        with pytest.raises(TelenotBusyError):
            await make_client(panel).connect()
    finally:
        await panel.stop()


async def test_legacy_fallback() -> None:
    panel = FakePanel(reject_modern=True)
    await panel.start()
    try:
        client = make_client(panel)
        await client.connect()
        assert client.state.legacy
        assert client.text_crc == 0x1234
        await client.disconnect()
    finally:
        await panel.stop()


async def test_labels(panel: FakePanel) -> None:
    client = make_client(panel)
    await client.connect()
    try:
        labels = await client.load_labels(timeout=5)
        assert labels.crc == 0x1234
        assert labels.free_leds == {1: "Extern scharf"}
        assert labels.areas == {1: "Erdgeschoss"}
        assert labels.switches == {1: "Licht"}
        assert labels.switch_on_text == {1: "Ein"}
        assert labels.switch_off_text == {1: "Aus"}
    finally:
        await client.disconnect()


async def test_arm_with_code_dialogue(panel: FakePanel) -> None:
    client = make_client(panel)
    await client.connect()
    try:
        await client.press(Key.ARM_AWAY)
        assert await client.wait_display_change(3)
        assert client.state.display[0] == "Code eingeben:"
        for digit in "123456":
            await client.press(ord(digit))
        await client.press(Key.OK)
        for _ in range(50):
            if client.state.free_leds[0].color == LedColor.RED:
                break
            await asyncio.sleep(0.05)
        assert client.state.free_leds[0].color == LedColor.RED
        assert client.state.display[1] == "Extern scharf"
        panel.trigger_alarm()
        await asyncio.sleep(0.3)
        assert client.state.alarm.on
    finally:
        await client.disconnect()
    assert panel.received_keys == [0x45, *b"123456", 0x42]


async def test_switch_toggle_and_lock(panel: FakePanel) -> None:
    client = make_client(panel)
    await client.connect()
    try:
        assert await client.press_and_confirm(switch_key(3), timeout=3) == 0
        await asyncio.sleep(0.2)
        assert client.state.switches[2]
        assert await client.press_and_confirm(0x80, timeout=3) == 2
    finally:
        await client.disconnect()


async def test_installer_static_key() -> None:
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    panel = FakePanel(key_number=1234, aes_key=key)
    await panel.start()
    try:
        client = BuildSecClient(
            "127.0.0.1",
            panel.port,
            code="123456",
            access_level=AccessLevel.INSTALLER,
            key_number=1234,
            aes_key=key,
        )
        await client.connect()
        assert panel.received_records[0][3] == 0x94
        await client.disconnect()
    finally:
        await panel.stop()


async def test_disconnect_detected(panel: FakePanel) -> None:
    client = make_client(panel)
    lost = asyncio.Event()
    client.add_disconnect_listener(lambda err: lost.set())
    await client.connect()
    await panel.drop_connection()
    await asyncio.wait_for(lost.wait(), 5)
    assert not client.connected


async def test_default_uses_plain_vds_header_and_end_block(panel: FakePanel) -> None:
    """Matches the iOS app on a real panel: PK=0x01 and St70 0x8E before closing."""
    client = make_client(panel)
    await client.connect()
    await client.press(Key.OK)
    assert panel.client_pk == 0x01
    await client.disconnect()
    await asyncio.wait_for(panel.session_closed.wait(), 3)


async def test_extended_header(panel: FakePanel) -> None:
    client = make_client(panel, extended_header=True)
    await client.connect()
    try:
        assert panel.client_pk == 0x82
        assert client.state.firmware == "2.05"
    finally:
        await client.disconnect()
    await asyncio.wait_for(panel.session_closed.wait(), 3)
