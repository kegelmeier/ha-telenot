"""Alarm code per action, event log, area child devices and helpers."""

from __future__ import annotations

import pytest
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telenot_buildsec.binary_sensor import guess_device_class
from custom_components.telenot_buildsec.const import DOMAIN
from custom_components.telenot_buildsec.pytelenot.buildsec import lcd_decode

from .fake_panel import FakePanel
from .test_integration import entry_data, wait_for, wait_for_reload

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
