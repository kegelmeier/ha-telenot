"""Event log of the Telenot panel (arm/disarm, alarm, fault, failed commands)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EVENT_TYPES, TelenotConfigEntry, TelenotCoordinator
from .entity import TelenotEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TelenotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the event log entity."""
    async_add_entities([TelenotEventLog(entry.runtime_data)])


class TelenotEventLog(TelenotEntity, EventEntity):
    """Panel-reported state changes and commands from Home Assistant that failed.

    Arm/disarm/alarm entries come from what the panel shows, never from the command
    that was sent.
    """

    _attr_translation_key = "event_log"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: TelenotCoordinator) -> None:
        """Initialise."""
        super().__init__(coordinator, "event_log")

    @property
    def available(self) -> bool:
        """The log keeps its last entry while the panel is unreachable."""
        return True

    async def async_added_to_hass(self) -> None:
        """Subscribe to the coordinator's event log."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event_type: str, attributes: dict[str, Any]) -> None:
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
