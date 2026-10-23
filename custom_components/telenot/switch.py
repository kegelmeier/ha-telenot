"""Switches: switching functions (Schaltfunktionen) and area locks (Meldebereich sperren)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import NUM_AREAS, NUM_SWITCHES
from .coordinator import TelenotConfigEntry, TelenotCoordinator
from .entity import TelenotEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TelenotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        TelenotFunctionSwitch(coordinator, i) for i in range(1, NUM_SWITCHES + 1)
    ]
    entities.extend(TelenotAreaLockSwitch(coordinator, i) for i in range(1, NUM_AREAS + 1))
    async_add_entities(entities)


class TelenotFunctionSwitch(TelenotEntity, SwitchEntity):
    """A switching function; the key toggles it, the panel reports the state (0xCA)."""

    _attr_translation_key = "switching_function"

    def __init__(self, coordinator: TelenotCoordinator, index: int) -> None:
        """Initialise."""
        super().__init__(coordinator, f"switch_{index}")
        self._index = index
        self._attr_entity_registry_enabled_default = index in coordinator.labels.switches
        self._attr_translation_placeholders = {"index": str(index)}

    @property
    def name(self) -> str | None:
        """Use the panel's label when known."""
        if label := self.coordinator.labels.switches.get(self._index):
            return label
        return super().name

    @property
    def is_on(self) -> bool:
        """Switching function active."""
        return self.coordinator.client.state.switches[self._index - 1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Action texts from the panel."""
        labels = self.coordinator.labels
        return {
            "index": self._index,
            "on_text": labels.switch_on_text.get(self._index),
            "off_text": labels.switch_off_text.get(self._index),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Toggle if currently off."""
        if not self.is_on:
            await self.coordinator.async_toggle_switch(self._index)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Toggle if currently on."""
        if self.is_on:
            await self.coordinator.async_toggle_switch(self._index)


class TelenotAreaLockSwitch(TelenotEntity, SwitchEntity):
    """Lock (bypass) of a Meldebereich; yellow keypad LED."""

    _attr_translation_key = "area_lock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: TelenotCoordinator, index: int) -> None:
        """Initialise."""
        super().__init__(coordinator, f"area_lock_{index}")
        self._index = index
        self._attr_entity_registry_enabled_default = index in coordinator.labels.areas
        self._attr_translation_placeholders = {"index": str(index)}

    @property
    def name(self) -> str | None:
        """Use the panel's label when known."""
        if label := self.coordinator.labels.areas.get(self._index):
            return f"{label} locked"
        return super().name

    @property
    def is_on(self) -> bool:
        """Area locked."""
        return self.coordinator.client.state.area_locked[self._index - 1].active

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock the area."""
        if not self.is_on:
            await self.coordinator.async_toggle_area_lock(self._index)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unlock the area."""
        if self.is_on:
            await self.coordinator.async_toggle_area_lock(self._index)
