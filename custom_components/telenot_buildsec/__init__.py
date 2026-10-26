"""The Telenot (BuildSec) integration."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import CARD_URL, DOMAIN, MANUFACTURER
from .coordinator import TelenotConfigEntry, TelenotCoordinator

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_FILE = Path(__file__).parent / "frontend" / "telenot-keypad-card.js"

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the keypad card and load it in the frontend (no manual resource needed)."""
    if hass.http is None:
        return True
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=True)]
    )
    version = (await async_get_integration(hass, DOMAIN)).version
    url = f"{CARD_URL}?v={version}"
    if not await _async_register_card_resource(hass, url) and "frontend" in hass.config.components:
        # YAML-mode resources: fall back to a script tag in the frontend page.
        add_extra_js_url(hass, url)
    return True


async def _async_register_card_resource(hass: HomeAssistant, url: str) -> bool:
    """Register the card as a Lovelace resource (storage mode); False if not possible.

    Dashboards load their resources every time they open, whereas an extra JS URL is
    only part of the frontend page when it was served after this integration set up.
    """
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None or lovelace.resource_mode != MODE_STORAGE:
        return False
    resources = lovelace.resources
    await resources.async_get_info()  # loads the collection from storage
    for item in resources.async_items():
        if item["url"].split("?")[0] == CARD_URL:
            if item["url"] != url:
                await resources.async_update_item(item["id"], {"res_type": "module", "url": url})
            return True
    await resources.async_create_item({"res_type": "module", "url": url})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TelenotConfigEntry) -> bool:
    """Set up Telenot from a config entry."""
    coordinator = TelenotCoordinator(hass, entry)
    # Register the panel first: indicator areas are child devices of it.
    panel = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        manufacturer=MANUFACTURER,
        name=entry.title,
    )
    coordinator.panel_device_id = panel.id
    await coordinator.async_start()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TelenotConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
