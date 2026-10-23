"""Alarm control panel for Telenot (arm/disarm through the virtual keypad)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ACTION_ARM_AWAY,
    ACTION_ARM_HOME,
    ACTION_DISARM,
    CONF_ALARM_CODE,
    CONF_ARMED_AWAY_SOURCE,
    CONF_ARMED_HOME_SOURCE,
)
from .coordinator import TelenotConfigEntry
from .entity import TelenotEntity
from .pytelenot import Key

PARALLEL_UPDATES = 1

SERVICE_SEND_KEYS = "send_keys"
ATTR_KEYS = "keys"

KEY_NAMES: dict[str, int] = {
    "ok": Key.OK,
    "esc": Key.ESC,
    "up": Key.UP,
    "down": Key.DOWN,
    "arm_away": Key.ARM_AWAY,
    "arm_home": Key.ARM_HOME,
    "disarm": Key.DISARM,
    "function": Key.FUNCTION,
}


def parse_keys(value: str | list[str]) -> list[int]:
    """Parse '1234 ok' / ['arm_away', '1234', 'ok'] / '0x45' into key codes."""
    tokens = value.replace(",", " ").split() if isinstance(value, str) else value
    keys: list[int] = []
    for token in tokens:
        token = str(token).strip().lower()
        if token in KEY_NAMES:
            keys.append(KEY_NAMES[token])
        elif token.isdigit():
            keys.extend(ord(c) for c in token)
        elif token.startswith("0x"):
            keys.append(int(token, 16) & 0xFF)
        else:
            raise ServiceValidationError(f"Unknown key '{token}'")
    return keys


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TelenotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the alarm control panel."""
    async_add_entities([TelenotAlarmPanel(entry.runtime_data)])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SEND_KEYS,
        {vol.Required(ATTR_KEYS): vol.Any(cv.string, [cv.string])},
        "async_send_keys",
    )


class TelenotAlarmPanel(TelenotEntity, AlarmControlPanelEntity):
    """The panel, operated like the BuildSec virtual keypad."""

    _attr_name = None
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY | AlarmControlPanelEntityFeature.ARM_HOME
    )

    def __init__(self, coordinator) -> None:
        """Initialise."""
        super().__init__(coordinator, "alarm")
        # The alarm code is checked by Home Assistant and never sent to the panel.
        # HA has a single "code needed to arm" flag: if either arm mode needs the code,
        # the keypad opens for both, but only the selected mode checks it.
        actions = (ACTION_ARM_AWAY, ACTION_ARM_HOME, ACTION_DISARM)
        if any(coordinator.code_required(action) for action in actions):
            code = str(coordinator.options[CONF_ALARM_CODE])
            self._attr_code_format = CodeFormat.NUMBER if code.isdigit() else CodeFormat.TEXT
        else:
            self._attr_code_format = None
        self._attr_code_arm_required = coordinator.code_required(
            ACTION_ARM_AWAY
        ) or coordinator.code_required(ACTION_ARM_HOME)

    @property
    def alarm_state(self) -> AlarmControlPanelState:
        """Current state derived from the keypad indicators."""
        return self.coordinator.alarm_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the keypad display and the indicators used for the state."""
        state = self.coordinator.client.state
        return {
            "display": "\n".join(state.display),
            "armed_away_indicator": self.coordinator.resolved_source(CONF_ARMED_AWAY_SOURCE),
            "armed_home_indicator": self.coordinator.resolved_source(CONF_ARMED_HOME_SOURCE),
        }

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arm externally (ES key)."""
        await self.coordinator.async_set_alarm(AlarmControlPanelState.ARMED_AWAY, code)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Arm internally (IS key)."""
        await self.coordinator.async_set_alarm(AlarmControlPanelState.ARMED_HOME, code)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Disarm (US key)."""
        await self.coordinator.async_set_alarm(AlarmControlPanelState.DISARMED, code)

    async def async_send_keys(self, keys: str | list[str]) -> None:
        """Press arbitrary keypad keys."""
        await self.coordinator.async_press_keys(parse_keys(keys))
