"""Base entity for the Telenot integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import TelenotCoordinator

DEVICE_FAMILIES = {
    0xB9: "hiplex",
    0xF7: "hiplex subcontroller",
}


class TelenotEntity(CoordinatorEntity[TelenotCoordinator]):
    """Entity bound to one Telenot panel (one config entry)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TelenotCoordinator, key: str) -> None:
        """Initialise with a unique key per entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}:{key}"
        state = coordinator.client.state
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, base)},
            manufacturer=MANUFACTURER,
            model=DEVICE_FAMILIES.get(state.device_family or -1, "Intrusion alarm panel"),
            name=entry.title,
            sw_version=state.firmware,
        )

    @property
    def available(self) -> bool:
        """Available while the keypad session is usable."""
        return self.coordinator.available
