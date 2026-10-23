"""Buttons for the virtual keypad keys (disabled by default)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TelenotConfigEntry, TelenotCoordinator
from .entity import TelenotEntity
from .pytelenot import Key

PARALLEL_UPDATES = 1

BUTTONS: dict[str, Key] = {
    **{f"key_{d}": Key(0x30 + d) for d in range(10)},
    "key_ok": Key.OK,
    "key_esc": Key.ESC,
    "key_up": Key.UP,
    "key_down": Key.DOWN,
    "key_arm_away": Key.ARM_AWAY,
    "key_arm_home": Key.ARM_HOME,
    "key_disarm": Key.DISARM,
    "key_function": Key.FUNCTION,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TelenotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up keypad buttons."""
    coordinator = entry.runtime_data
    async_add_entities(TelenotKeyButton(coordinator, k, v) for k, v in BUTTONS.items())


class TelenotKeyButton(TelenotEntity, ButtonEntity):
    """One key of the virtual keypad."""

    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: TelenotCoordinator, key: str, code: Key) -> None:
        """Initialise."""
        super().__init__(coordinator, key)
        self._code = code
        if key.startswith("key_") and key[4:].isdigit():
            self._attr_translation_key = "key_digit"
            self._attr_translation_placeholders = {"digit": key[4:]}
        else:
            self._attr_translation_key = key

    async def async_press(self) -> None:
        """Press the key."""
        await self.coordinator.async_press_keys([self._code])
