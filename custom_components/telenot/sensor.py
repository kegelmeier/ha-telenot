"""Sensors: keypad display, buzzer and free LEDs."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import NUM_FREE_LEDS
from .coordinator import TelenotConfigEntry, TelenotCoordinator
from .entity import TelenotEntity
from .pytelenot import Buzzer, LedColor

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TelenotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        TelenotDisplaySensor(coordinator, 1),
        TelenotDisplaySensor(coordinator, 2),
        TelenotBuzzerSensor(coordinator),
    ]
    entities.extend(TelenotFreeLedSensor(coordinator, i) for i in range(1, NUM_FREE_LEDS + 1))
    async_add_entities(entities)


class TelenotDisplaySensor(TelenotEntity, SensorEntity):
    """One line of the 2x16 keypad LCD."""

    _attr_translation_key = "display_line"

    def __init__(self, coordinator: TelenotCoordinator, line: int) -> None:
        """Initialise."""
        super().__init__(coordinator, f"display_{line}")
        self._line = line
        self._attr_translation_placeholders = {"line": str(line)}

    @property
    def native_value(self) -> str:
        """Display text."""
        return self.coordinator.client.state.display[self._line - 1]


class TelenotBuzzerSensor(TelenotEntity, SensorEntity):
    """Keypad buzzer pattern (entry/exit delay, alarm, ...)."""

    _attr_translation_key = "buzzer"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [b.value for b in Buzzer]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TelenotCoordinator) -> None:
        """Initialise."""
        super().__init__(coordinator, "buzzer")

    @property
    def native_value(self) -> str:
        """Buzzer pattern."""
        return self.coordinator.client.state.buzzer.value


class TelenotFreeLedSensor(TelenotEntity, SensorEntity):
    """Freely programmable keypad LED (colour)."""

    _attr_translation_key = "free_led"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [c.value for c in LedColor]

    def __init__(self, coordinator: TelenotCoordinator, index: int) -> None:
        """Initialise."""
        super().__init__(coordinator, f"led_{index}")
        self._index = index
        self._attr_translation_placeholders = {"index": str(index)}

    @property
    def name(self) -> str | None:
        """Use the panel's label when known."""
        if label := self.coordinator.labels.free_leds.get(self._index):
            return label
        return super().name

    @property
    def native_value(self) -> str:
        """LED colour (off if dark)."""
        led = self.coordinator.client.state.free_leds[self._index - 1]
        return led.color.value if led.active else LedColor.OFF.value

    @property
    def extra_state_attributes(self) -> dict[str, bool | int]:
        """Blinking flag."""
        led = self.coordinator.client.state.free_leds[self._index - 1]
        return {"index": self._index, "blinking": led.blinking}
