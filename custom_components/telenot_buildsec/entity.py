"""Base entity for the Telenot integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import ChildDeviceInfo, DeviceInfo
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

    def __init__(self, coordinator: TelenotCoordinator, key: str, area: int | None = None) -> None:
        """Initialise with a unique key per entity.

        Entities of a labelled indicator area (``area``) belong to a child device of
        the panel, named as in the panel.
        """
        super().__init__(coordinator)
        entry = coordinator.config_entry
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}:{key}"
        state = coordinator.client.state
        if (
            area is not None
            and coordinator.panel_device_id is not None
            and (label := coordinator.labels.areas.get(area))
        ):
            self._attr_device_info = ChildDeviceInfo(
                identifiers={(DOMAIN, f"{base}:area_{area}")},
                name=label,
                parent_device_id=coordinator.panel_device_id,
            )
            return
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, base)},
            manufacturer=MANUFACTURER,
            model=DEVICE_FAMILIES.get(state.device_family or -1, "Intrusion alarm panel"),
            name=entry.title,
            sw_version=state.firmware,
        )

    @property
    def has_area_device(self) -> bool:
        """Whether this entity sits on its own indicator-area device."""
        return any(":area_" in ident for _, ident in self._attr_device_info["identifiers"])

    @property
    def available(self) -> bool:
        """Available while the keypad session is usable."""
        return self.coordinator.available
