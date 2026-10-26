"""Alarm code per action, event log, area child devices and helpers."""

from __future__ import annotations

import pytest
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telenot_buildsec.binary_sensor import guess_device_class
from custom_components.telenot_buildsec.const import CARD_URL, DOMAIN
from custom_components.telenot_buildsec.pytelenot.buildsec import lcd_decode

from .fake_panel import FakePanel
from .test_integration import _fast_sleep, _real_sleep, entry_data, wait_for, wait_for_reload

PANEL = "alarm_control_panel.telenot_127_0_0_1"
EVENT_LOG = "event.telenot_127_0_0_1_event_log"


async def _setup(hass: HomeAssistant, panel: FakePanel, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telenot 127.0.0.1",
        data=entry_data(panel),
        options=options or {},
        unique_id="panel",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    await wait_for(lambda: coordinator.labels.crc == 0x1234)
    await wait_for_reload(hass, entry, coordinator)
    return entry


async def _call(hass: HomeAssistant, service: str, code: str | None = None) -> None:
    data = {"entity_id": PANEL}
    if code is not None:
        data["code"] = code
    await hass.services.async_call("alarm_control_panel", service, data, blocking=True)


async def _state_is(hass: HomeAssistant, state: AlarmControlPanelState) -> None:
    await wait_for(lambda: hass.states.get(PANEL).state == state)


async def test_alarm_code_per_action(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    entry = await _setup(hass, fake_panel, {"alarm_code": "9999", "code_required_for": ["disarm"]})
    attributes = hass.states.get(PANEL).attributes
    assert attributes["code_format"] == "number"
    assert attributes["code_arm_required"] is False

    await _call(hass, "alarm_arm_away")  # no code needed for arming
    await _state_is(hass, AlarmControlPanelState.ARMED_AWAY)
    keys_before = list(fake_panel.received_keys)

    for code in (None, "1234"):
        with pytest.raises(ServiceValidationError):
            await _call(hass, "alarm_disarm", code)
    assert fake_panel.received_keys == keys_before  # nothing reached the panel
    event = hass.states.get(EVENT_LOG)
    assert event.attributes["event_type"] == "command_failed"
    assert event.attributes["reason"] == "wrong code"

    await _call(hass, "alarm_disarm", "9999")
    await _state_is(hass, AlarmControlPanelState.DISARMED)
    # the panel got the stored keypad code, never the Home Assistant alarm code
    sent = bytes(fake_panel.received_keys[len(keys_before) :])
    assert b"123456" in sent and b"9999" not in sent
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_event_log_follows_panel(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    entry = await _setup(hass, fake_panel)
    await _call(hass, "alarm_arm_home")
    await _state_is(hass, AlarmControlPanelState.ARMED_HOME)
    await wait_for(lambda: hass.states.get(EVENT_LOG).attributes.get("event_type") == "armed_home")
    assert hass.states.get(EVENT_LOG).attributes["source"] == "panel"

    fake_panel.trigger_alarm()
    await wait_for(lambda: hass.states.get(EVENT_LOG).attributes.get("event_type") == "triggered")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_labelled_area_is_child_device(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    entry = await _setup(hass, fake_panel)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    area_entity = entities.async_get(
        entities.async_get_entity_id("binary_sensor", DOMAIN, "panel:area_info_1")
    )
    lock_entity = entities.async_get(
        entities.async_get_entity_id("switch", DOMAIN, "panel:area_lock_1")
    )
    area_device = devices.async_get(area_entity.device_id)
    assert area_device.name == "Erdgeschoss"
    assert area_device.parent_device_id == entry.runtime_data.panel_device_id
    assert lock_entity.device_id == area_device.id
    # unlabelled areas stay on the panel device
    other = entities.async_get(
        entities.async_get_entity_id("binary_sensor", DOMAIN, "panel:area_info_2")
    )
    assert other.device_id == entry.runtime_data.panel_device_id
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Fenster Küche", BinarySensorDeviceClass.WINDOW),
        ("Haustür", BinarySensorDeviceClass.DOOR),
        ("BWM Flur", BinarySensorDeviceClass.MOTION),
        ("Rauchmelder OG", BinarySensorDeviceClass.SMOKE),
        ("Garagentor", BinarySensorDeviceClass.GARAGE_DOOR),
        ("Sabotage", BinarySensorDeviceClass.TAMPER),
        ("Erdgeschoss", None),
        (None, None),
    ],
)
def test_guess_device_class(label: str | None, expected) -> None:
    assert guess_device_class(label) == expected


def test_lcd_sharp_s() -> None:
    assert lcd_decode(b"Stra\xe2e") == "Straße"


async def test_pause_releases_remote_access(
    hass: HomeAssistant, fake_panel: FakePanel, monkeypatch
) -> None:
    monkeypatch.setattr("custom_components.telenot_buildsec.coordinator.asyncio.sleep", _fast_sleep)
    entry = await _setup(hass, fake_panel)
    coordinator = entry.runtime_data
    remote = "switch.telenot_127_0_0_1_remote_access"
    assert hass.states.get(remote).state == "on"

    fake_panel.session_closed.clear()
    await hass.services.async_call("switch", "turn_off", {"entity_id": remote}, blocking=True)
    await wait_for(fake_panel.session_closed.is_set)  # session ended properly (0x8E)
    assert not coordinator.connected
    await hass.async_block_till_done()
    assert hass.states.get(remote).state == "off"
    assert hass.states.get(PANEL).state == "unavailable"
    await _real_sleep(0.5)  # the reconnect loop must stay away while paused
    assert not coordinator.connected
    keys_before = list(fake_panel.received_keys)
    await _call(hass, "alarm_arm_away")  # entity unavailable: Home Assistant skips it
    with pytest.raises(ServiceValidationError):  # automations calling in directly are refused
        await coordinator.async_press_keys([0x31])
    assert fake_panel.received_keys == keys_before

    # paused survives a reload
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.paused and not coordinator.connected
    assert hass.states.get(remote).state == "off"

    await hass.services.async_call("switch", "turn_on", {"entity_id": remote}, blocking=True)
    await wait_for(lambda: coordinator.connected, timeout=10)
    await _state_is(hass, AlarmControlPanelState.DISARMED)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_led_labels_from_options(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    entry = await _setup(
        hass, fake_panel, {"led_labels": {"led_2": "Intern Scharf", "led_3": "Extern Bereit"}}
    )
    state = hass.states.get(PANEL)
    # option label wins over an empty panel text and drives the automatic detection
    assert state.attributes["armed_home_indicator"] == "led_2"
    assert state.attributes["armed_away_indicator"] == "led_1"  # panel text "Extern scharf"
    assert hass.states.get("sensor.telenot_127_0_0_1_intern_scharf") is not None

    keypad = state.attributes["keypad"]
    assert [led["label"] for led in keypad["leds"]] == [
        "Extern scharf",
        "Intern Scharf",
        "Extern Bereit",
        None,
    ]
    assert keypad["leds"][1]["color"] == "off"
    assert keypad["alarm"] == {
        "color": "off",
        "blinking": False,
        "entity_id": "binary_sensor.telenot_127_0_0_1_alarm",
    }
    # the card opens the more-info dialog of each LED
    assert keypad["leds"][1]["entity_id"] == "sensor.telenot_127_0_0_1_intern_scharf"
    assert keypad["display_entity_id"] == "sensor.telenot_127_0_0_1_display_line_1"
    assert len(keypad["display"]) == 2

    await _call(hass, "alarm_arm_home")
    await _state_is(hass, AlarmControlPanelState.ARMED_HOME)
    await wait_for(
        lambda: hass.states.get(PANEL).attributes["keypad"]["leds"][1]["color"] == "yellow"
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_led_labels(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    entry = await _setup(hass, fake_panel)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "connection_mode": "persistent",
            "scan_interval": 300,
            "idle_timeout": 60,
            "armed_away_source": "auto",
            "armed_home_source": "auto",
            "arm_sequence": "auto",
            "code_required_for": [],
            "extended_header": False,
            "led_labels": {"led_1": " Intern Bereit ", "led_2": "", "led_4": "Extern Scharf"},
        },
    )
    assert result["type"] == "create_entry"
    assert entry.options["led_labels"] == {"led_1": "Intern Bereit", "led_4": "Extern Scharf"}
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_keypad_card_is_served(
    hass: HomeAssistant, fake_panel: FakePanel, hass_client
) -> None:
    assert await async_setup_component(hass, "http", {})
    entry = await _setup(hass, fake_panel)
    client = await hass_client()
    response = await client.get(CARD_URL)
    assert response.status == 200
    assert "telenot-keypad-card" in await response.text()
    assert await hass.config_entries.async_unload(entry.entry_id)


OPEN_SENSOR = "sensor.telenot_127_0_0_1_open_messages"
WINDOW = "binary_sensor.telenot_127_0_0_1_kippfenster_re_og"
MOTION = "binary_sensor.telenot_127_0_0_1_melder_flur_eg"


async def test_message_list_detectors(
    hass: HomeAssistant, fake_panel: FakePanel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.telenot_buildsec.coordinator.READ_DELAY", 0.05)
    entry = await _setup(hass, fake_panel, {"led_labels": {"led_3": "Intern Bereit"}})
    coordinator = entry.runtime_data
    await wait_for(lambda: coordinator.open_detectors == set())
    assert coordinator.ready is True
    await wait_for(lambda: hass.states.get(OPEN_SENSOR).state == "0")
    keys_before = len(fake_panel.received_keys)

    # a window and a motion detector open: the ready LED goes out, the list is read
    fake_panel.set_messages([("Kippfenster re.", "OG"), ("Melder Flur", "EG")])
    await wait_for(lambda: hass.states.get(MOTION) is not None)
    await hass.async_block_till_done()
    assert hass.states.get(WINDOW).state == "on"
    assert hass.states.get(WINDOW).attributes["device_class"] == BinarySensorDeviceClass.WINDOW
    assert hass.states.get(WINDOW).attributes["location"] == "OG"
    assert hass.states.get(MOTION).attributes["device_class"] == BinarySensorDeviceClass.MOTION
    state = hass.states.get(OPEN_SENSOR)
    assert state.state == "2"
    assert state.attributes["detectors"] == ["Kippfenster re. OG", "Melder Flur EG"]
    assert hass.states.get(PANEL).attributes["keypad"]["open_detectors"] == [
        "Kippfenster re. OG",
        "Melder Flur EG",
    ]
    event = hass.states.get(EVENT_LOG)
    assert event.attributes["event_type"] == "detector_opened"
    assert event.attributes["detector"] == "Melder Flur EG"
    # DOWN through header, two entries and the end marker, then ESC back to idle
    assert fake_panel.received_keys[keys_before:] == [0x2B] * 4 + [0x53, 0x53]
    assert fake_panel._list_pos is None

    # the motion detector clears while the window stays open: read on request
    fake_panel.messages = [("Kippfenster re.", "OG")]
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.telenot_127_0_0_1_read_message_list"},
        blocking=True,
    )
    assert hass.states.get(MOTION).state == "off"
    assert hass.states.get(WINDOW).state == "on"
    assert hass.states.get(EVENT_LOG).attributes["event_type"] == "detector_closed"

    # everything closed: the ready LED is enough, no scrolling
    keys_before = len(fake_panel.received_keys)
    fake_panel.set_messages([])
    await wait_for(lambda: hass.states.get(WINDOW).state == "off")
    assert hass.states.get(OPEN_SENSOR).state == "0"
    assert len(fake_panel.received_keys) == keys_before

    # detectors found once survive a restart
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(MOTION) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_message_list_only_when_disarmed(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    entry = await _setup(hass, fake_panel)
    coordinator = entry.runtime_data
    assert coordinator.ready is None  # no LED labelled "Bereit": no automatic reading
    await _call(hass, "alarm_arm_away")
    await _state_is(hass, AlarmControlPanelState.ARMED_AWAY)
    keys_before = list(fake_panel.received_keys)
    with pytest.raises(ServiceValidationError):
        await coordinator.async_read_messages()
    assert fake_panel.received_keys == keys_before
    assert await hass.config_entries.async_unload(entry.entry_id)
