"""Exceptions raised by the Telenot BuildSec protocol implementation."""

from __future__ import annotations


class TelenotError(Exception):
    """Base class for all Telenot errors."""


class TelenotConnectionError(TelenotError):
    """TCP connection could not be opened or was lost."""


class TelenotProtocolError(TelenotError):
    """The S2 / VdS 2465 protocol was violated."""


class TelenotBusyError(TelenotError):
    """Remote access is already in use by another client (SYNC window 0xFF)."""


class TelenotKeyError(TelenotError):
    """The AES key or key number is wrong or unknown to the panel."""


class TelenotPanelError(TelenotError):
    """The panel rejected a request with a Satztyp 70 error block (0x23)."""

    def __init__(self, reason: int, message: str) -> None:
        super().__init__(f"{message} (0x{reason:02X})")
        self.reason = reason


class TelenotAuthError(TelenotPanelError):
    """The panel rejected the keypad code or the access level."""
