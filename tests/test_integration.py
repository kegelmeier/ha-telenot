"""Home Assistant level tests against the fake panel."""

from __future__ import annotations

import asyncio

from homeassistant import config_entries
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telenot.const import (
    CONF_ACCESS_LEVEL,
    CONF_FREISCHALTCODE,
    CONF_KEYPAD_ADDRESS,
    CONF_KEYPAD_CODE,
    DOMAIN,
)

from .fake_panel import FakePanel

FSC = "0123456789abcdef"


def entry_data(panel: FakePanel, code: str = "123456") -> dict:
    return {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: panel.port,
        CONF_ACCESS_LEVEL: "operator",
        CONF_KEYPAD_CODE: code,
        CONF_KEYPAD_ADDRESS: 15,
        CONF_FREISCHALTCODE: FSC,
    }


async def wait_for(predicate, timeout: float = 5) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_config_flow(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "127.0.0.1",
            CONF_PORT: fake_panel.port,
            CONF_ACCESS_LEVEL: "operator",
            CONF_KEYPAD_CODE: "123456",
            CONF_KEYPAD_ADDRESS: 15,
        },
    )
    assert result["step_id"] == "operator"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FREISCHALTCODE: "zz"}
    )
    assert result["errors"] == {CONF_FREISCHALTCODE: "invalid_freischaltcode"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FREISCHALTCODE: FSC.upper()}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FREISCHALTCODE] == FSC
    await hass.async_block_till_done()
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)


async def test_config_flow_wrong_code(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    data = entry_data(fake_panel, code="999")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            k: data[k]
            for k in (
                CONF_HOST,
                CONF_PORT,
                CONF_ACCESS_LEVEL,
                CONF_KEYPAD_CODE,
                CONF_KEYPAD_ADDRESS,
            )
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FREISCHALTCODE: FSC}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_setup_and_control(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Telenot 127.0.0.1", data=entry_data(fake_panel), unique_id="panel"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    # labels are downloaded in the background and enable labelled entities
    await wait_for(lambda: coordinator.labels.crc == 0x1234)
    assert coordinator.labels.free_leds == {1: "Extern scharf"}
    registry = er.async_get(hass)
    await hass.async_block_till_done()
    area1 = registry.async_get_entity_id("binary_sensor", DOMAIN, "panel:area_info_1")
    area2 = registry.async_get_entity_id("binary_sensor", DOMAIN, "panel:area_info_2")
    assert registry.async_get(area1).disabled_by is None
    assert registry.async_get(area2).disabled_by is er.RegistryEntryDisabler.INTEGRATION

    state = hass.states.get("alarm_control_panel.telenot_127_0_0_1")
    assert state is not None
    assert state.state == AlarmControlPanelState.DISARMED
    assert state.attributes["armed_away_indicator"] == "led_1"

    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_away",
        {"entity_id": "alarm_control_panel.telenot_127_0_0_1"},
        blocking=True,
    )
    await wait_for(
        lambda: (
            hass.states.get("alarm_control_panel.telenot_127_0_0_1").state
            == AlarmControlPanelState.ARMED_AWAY
        )
    )
    assert fake_panel.received_keys[:8] == [0x45, *b"123456", 0x42]

    assert hass.states.get("sensor.telenot_127_0_0_1_display_line_2").state == "Extern scharf"

    fake_panel.trigger_alarm()
    await wait_for(
        lambda: (
            hass.states.get("alarm_control_panel.telenot_127_0_0_1").state
            == AlarmControlPanelState.TRIGGERED
        )
    )
    assert hass.states.get("binary_sensor.telenot_127_0_0_1_alarm").state == "on"

    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_disarm",
        {"entity_id": "alarm_control_panel.telenot_127_0_0_1"},
        blocking=True,
    )
    await wait_for(
        lambda: (
            hass.states.get("alarm_control_panel.telenot_127_0_0_1").state
            == AlarmControlPanelState.DISARMED
        )
    )

    switch_id = registry.async_get_entity_id("switch", DOMAIN, "panel:switch_1")
    assert switch_id is not None
    # labelled switch was enabled -> reload happens via the registry; call directly
    await coordinator.async_toggle_switch(1)
    await wait_for(lambda: coordinator.client.state.switches[0])

    await hass.services.async_call(
        DOMAIN,
        "send_keys",
        {"entity_id": "alarm_control_panel.telenot_127_0_0_1", "keys": "esc up 0x4e"},
        blocking=True,
    )
    await wait_for(lambda: fake_panel.received_keys[-3:] == [0x53, 0x2D, 0x4E])

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.wait_for(fake_panel.session_closed.wait(), 5)


async def test_reconnect(hass: HomeAssistant, fake_panel: FakePanel, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.telenot.coordinator.asyncio.sleep", _fast_sleep)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Telenot 127.0.0.1", data=entry_data(fake_panel), unique_id="panel"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.connected
    await fake_panel.drop_connection()
    await wait_for(lambda: not coordinator.connected)
    assert hass.states.get("alarm_control_panel.telenot_127_0_0_1").state == "unavailable"
    await wait_for(lambda: coordinator.connected, timeout=10)
    await hass.async_block_till_done()
    assert hass.states.get("alarm_control_panel.telenot_127_0_0_1").state == "disarmed"
    assert await hass.config_entries.async_unload(entry.entry_id)


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *args, **kwargs):
    return await _real_sleep(min(delay, 0.1), *args, **kwargs)


async def test_auth_failure_starts_reauth(hass: HomeAssistant, fake_panel: FakePanel) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(fake_panel, "111111"), unique_id="p")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_on_demand_mode(hass: HomeAssistant, fake_panel: FakePanel, monkeypatch) -> None:
    monkeypatch.setattr("custom_components.telenot.coordinator.asyncio.sleep", _fast_sleep)
    monkeypatch.setattr("custom_components.telenot.coordinator.SNAPSHOT_DELAY", 0.3)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telenot 127.0.0.1",
        data=entry_data(fake_panel),
        options={"connection_mode": "on_demand", "idle_timeout": 0.2},
        unique_id="panel",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert hass.states.get("alarm_control_panel.telenot_127_0_0_1").state == "disarmed"
    assert hass.states.get("sensor.telenot_127_0_0_1_display_line_2").state == "Unscharf"
    # idle disconnect (labels download keeps it open first)
    await wait_for(lambda: not coordinator.connected, timeout=10)
    assert hass.states.get("alarm_control_panel.telenot_127_0_0_1").state == "disarmed"
    # a command reconnects on demand
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_home",
        {"entity_id": "alarm_control_panel.telenot_127_0_0_1"},
        blocking=True,
    )
    await wait_for(
        lambda: hass.states.get("alarm_control_panel.telenot_127_0_0_1").state
        == AlarmControlPanelState.ARMED_HOME
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
