"""Binary sensors for the Telenot keypad indicators."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from .const import NUM_AREAS
from .coordinator import TelenotConfigEntry, TelenotCoordinator
from .entity import TelenotEntity
from .pytelenot import Led, PanelState

PARALLEL_UPDATES = 0

# Guess what an indicator area is from its label; users can change it with "Show as".
_DEVICE_CLASS_HINTS: tuple[tuple[re.Pattern[str], BinarySensorDeviceClass], ...] = (
    (re.compile(r"sabot|tamper", re.I), BinarySensorDeviceClass.TAMPER),
    (re.compile(r"rauch|smoke|brand|feuer|fire", re.I), BinarySensorDeviceClass.SMOKE),
    (re.compile(r"wasser|water|leck|leak|flut", re.I), BinarySensorDeviceClass.MOISTURE),
    (re.compile(r"fenster|window|dachluk|oberlicht", re.I), BinarySensorDeviceClass.WINDOW),
    (re.compile(r"garage|\btor\b|gate", re.I), BinarySensorDeviceClass.GARAGE_DOOR),
    (re.compile(r"t(ü|ue|u)r|door|eingang|entrance", re.I), BinarySensorDeviceClass.DOOR),
    (
        re.compile(r"bewegung|\bbwm\b|\bpir\b|motion|präsenz|presence|^melder\b", re.I),
        BinarySensorDeviceClass.MOTION,
    ),
    (re.compile(r"kontakt|contact|magnet|reed", re.I), BinarySensorDeviceClass.OPENING),
    (re.compile(r"glas|glass|erschütter|vibration", re.I), BinarySensorDeviceClass.VIBRATION),
)


def guess_device_class(label: str | None) -> BinarySensorDeviceClass | None:
    """Device class from an indicator-area label, or None for a generic sensor."""
    if label:
        for pattern, device_class in _DEVICE_CLASS_HINTS:
            if pattern.search(label):
                return device_class
    return None


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
    entities.extend(TelenotDetectorSensor(coordinator, name) for name in coordinator.detectors)
    async_add_entities(entities)

    @callback
    def add_detectors(names: list[str]) -> None:
        async_add_entities(TelenotDetectorSensor(coordinator, name) for name in names)

    entry.async_on_unload(coordinator.add_detector_listener(add_detectors))


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
        return self.entity_description.led(self.coordinator.state).active

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Whether the LED is blinking."""
        return {"blinking": self.entity_description.led(self.coordinator.state).blinking}


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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """When the session started and why the last one ended (since Home Assistant start)."""
        coordinator = self.coordinator
        return {
            "connected_since": coordinator.connected_since,
            "disconnects": coordinator.disconnects,
            "last_disconnect": coordinator.last_disconnect,
            "last_disconnect_reason": coordinator.last_disconnect_reason,
        }


class TelenotAreaSensor(TelenotEntity, BinarySensorEntity):
    """Red indicator LED of a Meldebereich (meaning depends on panel programming)."""

    def __init__(self, coordinator: TelenotCoordinator, index: int) -> None:
        """Initialise; labelled areas get their own device named as in the panel."""
        super().__init__(coordinator, f"area_info_{index}", area=index)
        self._index = index
        label = coordinator.labels.areas.get(index)
        self._attr_entity_registry_enabled_default = label is not None
        self._attr_translation_key = "area_state" if self.has_area_device else "area"
        self._attr_translation_placeholders = {"index": str(index)}
        self._attr_device_class = guess_device_class(label)

    @property
    def is_on(self) -> bool:
        """Indicator lit or blinking."""
        return self.coordinator.state.area_info[self._index - 1].active

    @property
    def extra_state_attributes(self) -> dict[str, bool | int]:
        """Blink and lock state."""
        state = self.coordinator.state
        return {
            "index": self._index,
            "blinking": state.area_info[self._index - 1].blinking,
            "locked": state.area_locked[self._index - 1].active,
        }


class TelenotDetectorSensor(TelenotEntity, BinarySensorEntity):
    """A detector found in the keypad message list; on while the panel lists it.

    Created automatically the first time a detector shows up in the list.
    """

    def __init__(self, coordinator: TelenotCoordinator, name: str) -> None:
        """Initialise."""
        super().__init__(coordinator, f"detector_{slugify(name)}")
        self._detector = name
        self._attr_name = name
        self._attr_device_class = guess_device_class(name)

    @property
    def is_on(self) -> bool | None:
        """Listed as open; unknown until the list was read."""
        if self.coordinator.open_detectors is None:
            return None
        return self._detector in self.coordinator.open_detectors

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Panel texts of the detector."""
        info = self.coordinator.detectors.get(self._detector, {})
        return {"text": info.get("text", ""), "location": info.get("location", "")}
