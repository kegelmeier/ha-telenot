"""Constants for the Telenot integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "telenot"
MANUFACTURER: Final = "Telenot"

CONF_ACCESS_LEVEL: Final = "access_level"
CONF_KEYPAD_CODE: Final = "keypad_code"
CONF_KEYPAD_ADDRESS: Final = "keypad_address"
CONF_FREISCHALTCODE: Final = "freischaltcode"
CONF_KEY_NUMBER: Final = "key_number"
CONF_AES_KEY: Final = "aes_key"

ACCESS_OPERATOR: Final = "operator"
ACCESS_INSTALLER: Final = "installer"
ACCESS_REMOTE_CONTROL: Final = "remote_control"
ACCESS_LEVELS: Final = [ACCESS_OPERATOR, ACCESS_INSTALLER, ACCESS_REMOTE_CONTROL]

# options
CONF_CONNECTION_MODE: Final = "connection_mode"
CONF_IDLE_TIMEOUT: Final = "idle_timeout"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_ARMED_AWAY_SOURCE: Final = "armed_away_source"
CONF_ARMED_HOME_SOURCE: Final = "armed_home_source"
CONF_REQUIRE_CODE: Final = "require_code"
CONF_ARM_SEQUENCE: Final = "arm_sequence"
CONF_EXTENDED_HEADER: Final = "extended_header"

MODE_PERSISTENT: Final = "persistent"
MODE_ON_DEMAND: Final = "on_demand"

SEQUENCE_AUTO: Final = "auto"
SEQUENCE_CODE: Final = "code"
SEQUENCE_KEY_ONLY: Final = "key_only"

SOURCE_AUTO: Final = "auto"
SOURCE_LCD: Final = "lcd"
SOURCE_LCD_LATCHED: Final = "lcd_latched"
SOURCE_NONE: Final = "none"

DEFAULT_KEYPAD_ADDRESS: Final = 15
DEFAULT_IDLE_TIMEOUT: Final = 60
DEFAULT_SCAN_INTERVAL: Final = 300

DEFAULT_OPTIONS: Final = {
    CONF_CONNECTION_MODE: MODE_PERSISTENT,
    CONF_IDLE_TIMEOUT: DEFAULT_IDLE_TIMEOUT,
    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    CONF_ARMED_AWAY_SOURCE: SOURCE_AUTO,
    CONF_ARMED_HOME_SOURCE: SOURCE_AUTO,
    CONF_REQUIRE_CODE: False,
    CONF_ARM_SEQUENCE: SEQUENCE_AUTO,
    CONF_EXTENDED_HEADER: False,
}

NUM_FREE_LEDS: Final = 4
NUM_AREAS: Final = 32
NUM_SWITCHES: Final = 16


def state_sources() -> list[str]:
    """All selectable indicator sources for armed states."""
    return [
        SOURCE_AUTO,
        SOURCE_LCD_LATCHED,
        SOURCE_LCD,
        SOURCE_NONE,
        *(f"led_{i}" for i in range(1, NUM_FREE_LEDS + 1)),
        *(f"area_{i}" for i in range(1, NUM_AREAS + 1)),
        *(f"switch_{i}" for i in range(1, NUM_SWITCHES + 1)),
    ]
