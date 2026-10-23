"""Diagnostics for the Telenot integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_AES_KEY, CONF_FREISCHALTCODE, CONF_KEYPAD_CODE
from .coordinator import TelenotConfigEntry

TO_REDACT = {CONF_AES_KEY, CONF_FREISCHALTCODE, CONF_KEYPAD_CODE, CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TelenotConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "connected": coordinator.connected,
        "text_crc": coordinator.client.text_crc,
        "alarm_state": coordinator.alarm_state(),
        "state": asdict(coordinator.client.state),
        "labels": coordinator.labels.as_dict(),
    }
