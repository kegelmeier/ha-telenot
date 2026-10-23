"""Python implementation of the Telenot BuildSec remote keypad protocol.

Reverse engineered from the Telenot BuildSec Android app (com.TelenotElectronic.BuildSec).
"""

from .buildsec import (
    AccessLevel,
    BuildSecClient,
    Buzzer,
    Key,
    Labels,
    Led,
    LedColor,
    PanelState,
    area_lock_key,
    encode_code,
    switch_key,
)
from .crypto import KEY_NUMBER_BETREIBER, RESERVED_KEY_NUMBERS
from .exceptions import (
    TelenotAuthError,
    TelenotBusyError,
    TelenotConnectionError,
    TelenotError,
    TelenotKeyError,
    TelenotPanelError,
    TelenotProtocolError,
)

__all__ = [
    "KEY_NUMBER_BETREIBER",
    "RESERVED_KEY_NUMBERS",
    "AccessLevel",
    "BuildSecClient",
    "Buzzer",
    "Key",
    "Labels",
    "Led",
    "LedColor",
    "PanelState",
    "TelenotAuthError",
    "TelenotBusyError",
    "TelenotConnectionError",
    "TelenotError",
    "TelenotKeyError",
    "TelenotPanelError",
    "TelenotProtocolError",
    "area_lock_key",
    "encode_code",
    "switch_key",
]
