"""Binary sensors for the Telenot keypad indicators."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import NUM_AREAS
from .coordinator import TelenotConfigEntry, TelenotCoordinator
from .entity import TelenotEntity
from .pytelenot import Led, PanelState

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TelenotLedDescription(BinarySensorEntityDescription):
    """Describes a keypad LED binary sensor."""

    led: Callable[[PanelState], Led]


LED_SENSORS: tuple[TelenotLedDescription, ...] = (
    TelenotLedDescription(
        key="alarm",
        translation_key="alarm",
        device_class=BinarySensorDeviceClass.SAFETY,
        led=lambda s: s.alarm,
    ),
    TelenotLedDescription(
        key="fault",
        translation_key="fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        led=lambda s: s.fault,
    ),
    TelenotLedDescription(
        key="walk_test",
        translation_key="walk_test",
        entity_category=EntityCategory.DIAGNOSTIC,
        led=lambda s: s.walk_test,
    ),
    TelenotLedDescription(
        key="operation",
        translation_key="operation",
        device_class=BinarySensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        led=lambda s: s.operation,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TelenotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        TelenotLedSensor(coordinator, description) for description in LED_SENSORS
    ]
    entities.append(TelenotConnectionSensor(coordinator))
    entities.extend(TelenotAreaSensor(coordinator, i) for i in range(1, NUM_AREAS + 1))
    async_add_entities(entities)


class TelenotLedSensor(TelenotEntity, BinarySensorEntity):
    """A fixed keypad LED (alarm, fault, walk test, operation)."""

    entity_description: TelenotLedDescription

    def __init__(self, coordinator: TelenotCoordinator, description: TelenotLedDescription) -> None:
        """Initialise."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """LED lit or blinking."""
        return self.entity_description.led(self.coordinator.client.state).active

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Whether the LED is blinking."""
        return {"blinking": self.entity_description.led(self.coordinator.client.state).blinking}


class TelenotConnectionSensor(TelenotEntity, BinarySensorEntity):
    """Whether the remote keypad session is open."""

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TelenotCoordinator) -> None:
        """Initialise."""
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        """Always available."""
        return True

    @property
    def is_on(self) -> bool:
        """Session open."""
        return self.coordinator.connected


class TelenotAreaSensor(TelenotEntity, BinarySensorEntity):
    """Red indicator LED of a Meldebereich (meaning depends on panel programming)."""

    _attr_translation_key = "area"

    def __init__(self, coordinator: TelenotCoordinator, index: int) -> None:
        """Initialise."""
        super().__init__(coordinator, f"area_info_{index}")
        self._index = index
        self._attr_entity_registry_enabled_default = index in coordinator.labels.areas
        self._attr_translation_placeholders = {"index": str(index)}

    @property
    def name(self) -> str | None:
        """Use the panel's label when known."""
        if label := self.coordinator.labels.areas.get(self._index):
            return label
        return super().name

    @property
    def is_on(self) -> bool:
        """Indicator lit or blinking."""
        return self.coordinator.client.state.area_info[self._index - 1].active

    @property
    def extra_state_attributes(self) -> dict[str, bool | int]:
        """Blink and lock state."""
        state = self.coordinator.client.state
        return {
            "index": self._index,
            "blinking": state.area_info[self._index - 1].blinking,
            "locked": state.area_locked[self._index - 1].active,
        }
