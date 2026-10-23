"""Connection handling and shared state for the Telenot integration."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ACCESS_INSTALLER,
    ACCESS_OPERATOR,
    ACCESS_REMOTE_CONTROL,
    CONF_ACCESS_LEVEL,
    CONF_AES_KEY,
    CONF_ARM_SEQUENCE,
    CONF_EXTENDED_HEADER,
    CONF_ARMED_AWAY_SOURCE,
    CONF_ARMED_HOME_SOURCE,
    CONF_CONNECTION_MODE,
    CONF_FREISCHALTCODE,
    CONF_IDLE_TIMEOUT,
    CONF_KEY_NUMBER,
    CONF_KEYPAD_ADDRESS,
    CONF_KEYPAD_CODE,
    CONF_SCAN_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    MODE_ON_DEMAND,
    SEQUENCE_AUTO,
    SEQUENCE_KEY_ONLY,
    SOURCE_AUTO,
    SOURCE_LCD,
    SOURCE_LCD_LATCHED,
    SOURCE_NONE,
)
from .pytelenot import (
    AccessLevel,
    BuildSecClient,
    Key,
    Labels,
    PanelState,
    TelenotAuthError,
    TelenotError,
    area_lock_key,
    switch_key,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SNAPSHOT_DELAY = 3.0
PROMPT_TIMEOUT = 4.0
MAX_BACKOFF = 300
ARMING_TIMEOUT = 120

_ACCESS_LEVEL = {
    ACCESS_OPERATOR: AccessLevel.OPERATOR,
    ACCESS_INSTALLER: AccessLevel.INSTALLER,
    ACCESS_REMOTE_CONTROL: AccessLevel.REMOTE_CONTROL,
}
_CODE_PROMPT = re.compile(r"code|kennw|passw|pin|geheimz", re.IGNORECASE)
_REFUSED = re.compile(r"falsch|nicht m.glich|verweigert|gesperrt|wrong|denied", re.IGNORECASE)
_AWAY = re.compile(r"(extern|ext\.).*scharf|scharf.*(extern|ext\.)|armed away", re.IGNORECASE)
_HOME = re.compile(r"(intern|int\.).*scharf|scharf.*(intern|int\.)|armed home", re.IGNORECASE)
_DISARMED = re.compile(r"unscharf|disarmed", re.IGNORECASE)

type TelenotConfigEntry = ConfigEntry[TelenotCoordinator]


def create_client(data: dict[str, Any], options: dict[str, Any] | None = None) -> BuildSecClient:
    """Build a protocol client from config entry data and options."""
    level = _ACCESS_LEVEL[data[CONF_ACCESS_LEVEL]]
    kwargs: dict[str, Any] = {}
    if level == AccessLevel.OPERATOR:
        kwargs["freischaltcode"] = bytes.fromhex(data[CONF_FREISCHALTCODE])
    else:
        kwargs["key_number"] = int(data[CONF_KEY_NUMBER])
        kwargs["aes_key"] = bytes.fromhex(data[CONF_AES_KEY])
    return BuildSecClient(
        data[CONF_HOST],
        int(data[CONF_PORT]),
        code=data[CONF_KEYPAD_CODE],
        access_level=level,
        keypad_address=int(data[CONF_KEYPAD_ADDRESS]),
        extended_header=bool((options or {}).get(CONF_EXTENDED_HEADER, False)),
        **kwargs,
    )


class TelenotCoordinator(DataUpdateCoordinator[PanelState]):
    """Owns the remote keypad session and turns pushed keypad state into HA updates."""

    config_entry: TelenotConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TelenotConfigEntry) -> None:
        """Initialise from the config entry."""
        self.options: dict[str, Any] = {**DEFAULT_OPTIONS, **entry.options}
        self.on_demand = self.options[CONF_CONNECTION_MODE] == MODE_ON_DEMAND
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_HOST]}",
            update_interval=(
                timedelta(seconds=self.options[CONF_SCAN_INTERVAL]) if self.on_demand else None
            ),
            always_update=True,
        )
        self.client = create_client(dict(entry.data), dict(entry.options))
        self.labels = Labels()
        self.arming_target: AlarmControlPanelState | None = None
        # last arm state reported by the panel display (never assumed from our own commands)
        self.latched_state: AlarmControlPanelState | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._disconnected = asyncio.Event()
        self._idle_unsub: CALLBACK_TYPE | None = None
        self._arming_unsub: CALLBACK_TYPE | None = None
        self._labels_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._unsubs = [
            self.client.add_listener(self._on_client_state),
            self.client.add_disconnect_listener(self._on_client_disconnect),
        ]
        self.data = self.client.state

    # ------------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Load cached labels and start the connection."""
        if stored := await self._store.async_load():
            self.labels = Labels.from_dict(stored.get("labels", stored))
            self.client.labels = self.labels
            if latched := stored.get("latched_state"):
                self.latched_state = AlarmControlPanelState(latched)
        if self.on_demand:
            await self.async_config_entry_first_refresh()
            return
        try:
            await self._connect()
        except TelenotAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TelenotError as err:
            _LOGGER.warning("Telenot panel not reachable yet, will retry: %s", err)
        self.config_entry.async_create_background_task(
            self.hass, self._connection_loop(), f"{DOMAIN}-connection"
        )

    async def async_shutdown(self) -> None:
        """Close the session when the entry is unloaded."""
        self._shutting_down = True
        self._cancel_idle()
        if self._arming_unsub is not None:
            self._arming_unsub()
            self._arming_unsub = None
        if self._labels_task is not None:
            self._labels_task.cancel()
        for unsub in self._unsubs:
            unsub()
        await super().async_shutdown()
        await self.client.disconnect()

    @property
    def connected(self) -> bool:
        """Return True while the remote keypad session is open."""
        return self.client.connected

    @property
    def available(self) -> bool:
        """Entities are available while connected (persistent) or last poll succeeded."""
        if self.on_demand:
            return self.last_update_success
        return self.client.connected

    async def _connection_loop(self) -> None:
        backoff = 5
        while not self._shutting_down:
            if self.client.connected:
                await self._disconnected.wait()
                if self._shutting_down:
                    return
                _LOGGER.info("Connection to Telenot panel lost, reconnecting")
            await asyncio.sleep(backoff)
            try:
                await self._connect()
            except TelenotAuthError as err:
                _LOGGER.error("Telenot panel rejected the keypad code: %s", err)
                self.config_entry.async_start_reauth(self.hass)
                return
            except TelenotError as err:
                _LOGGER.debug("Reconnect failed: %s", err)
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                backoff = 5

    async def _connect(self) -> None:
        async with self._lock:
            if self.client.connected:
                return
            self._disconnected.clear()
            await self.client.connect()
        _LOGGER.debug(
            "Connected to Telenot panel (family=%s firmware=%s text crc=%s)",
            self.client.state.device_family,
            self.client.state.firmware,
            self.client.text_crc,
        )
        self._update_device_info()
        self.async_set_updated_data(self.client.state)
        crc = self.client.text_crc
        if crc and crc != self.labels.crc and self._labels_task is None:
            self._labels_task = self.config_entry.async_create_background_task(
                self.hass, self._refresh_labels(), f"{DOMAIN}-labels"
            )

    def _update_device_info(self) -> None:
        state = self.client.state
        if state.firmware is None:
            return
        registry = dr.async_get(self.hass)
        base = self.config_entry.unique_id or self.config_entry.entry_id
        if device := registry.async_get_device(identifiers={(DOMAIN, base)}):
            registry.async_update_device(device.id, sw_version=state.firmware)

    async def _refresh_labels(self) -> None:
        try:
            labels = await self.client.load_labels()
        except (TelenotError, TimeoutError) as err:
            _LOGGER.warning("Could not download label texts from the panel: %s", err)
            return
        finally:
            self._labels_task = None
        self.labels = labels
        await self._store.async_save(self._store_data())
        self._enable_labelled_entities()
        self.async_update_listeners()
        if self.on_demand:
            self._schedule_idle_disconnect()

    def _store_data(self) -> dict[str, Any]:
        return {"labels": self.labels.as_dict(), "latched_state": self.latched_state}

    def _set_latched(self, state: AlarmControlPanelState) -> None:
        if state != self.latched_state:
            self.latched_state = state
            self._store.async_delay_save(self._store_data, 1)

    def _enable_labelled_entities(self) -> None:
        """Enable entities that were created disabled before their label was known."""
        registry = er.async_get(self.hass)
        labelled = {
            *(f"area_info_{i}" for i in self.labels.areas),
            *(f"area_lock_{i}" for i in self.labels.areas),
            *(f"switch_{i}" for i in self.labels.switches),
        }
        for entity in er.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            suffix = entity.unique_id.rsplit(":", 1)[-1]
            if suffix in labelled and entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                registry.async_update_entity(entity.entity_id, disabled_by=None)

    @callback
    def _on_client_state(self) -> None:
        text = " ".join(self.client.state.display)
        if _AWAY.search(text):
            self._set_latched(AlarmControlPanelState.ARMED_AWAY)
        elif _HOME.search(text):
            self._set_latched(AlarmControlPanelState.ARMED_HOME)
        elif _DISARMED.search(text):
            self._set_latched(AlarmControlPanelState.DISARMED)
        if self.arming_target is not None and self.alarm_state() == self.arming_target:
            self.arming_target = None
        self.async_set_updated_data(self.client.state)

    @callback
    def _on_client_disconnect(self, error: Exception | None) -> None:
        self._disconnected.set()
        if not self._shutting_down:
            _LOGGER.debug("Telenot session closed: %s", error)
            self.async_update_listeners()

    # ------------------------------------------------------------------ on-demand mode

    async def _async_update_data(self) -> PanelState:
        """Poll in on-demand mode: connect, collect the pushed state, disconnect later."""
        try:
            await self._ensure_connected()
        except TelenotAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TelenotError as err:
            raise UpdateFailed(str(err)) from err
        await asyncio.sleep(SNAPSHOT_DELAY)
        self._schedule_idle_disconnect()
        return self.client.state

    async def _ensure_connected(self) -> None:
        self._cancel_idle()
        if not self.client.connected:
            await self._connect()

    def _schedule_idle_disconnect(self) -> None:
        if not self.on_demand:
            return
        self._cancel_idle()
        self._idle_unsub = self.hass.loop.call_later(
            self.options[CONF_IDLE_TIMEOUT],
            lambda: self.hass.async_create_task(self._idle_disconnect()),
        ).cancel

    def _cancel_idle(self) -> None:
        if self._idle_unsub is not None:
            self._idle_unsub()
            self._idle_unsub = None

    async def _idle_disconnect(self) -> None:
        self._idle_unsub = None
        if self._labels_task is not None or self._lock.locked() or self._command_lock.locked():
            self._schedule_idle_disconnect()
            return
        await self.client.disconnect()

    # ------------------------------------------------------------------ commands

    async def _command(self, action: Callable[[], Awaitable[Any]]) -> Any:
        async with self._command_lock:
            return await self._run_command(action)

    async def _run_command(self, action: Callable[[], Awaitable[Any]]) -> Any:
        try:
            await self._ensure_connected()
            return await action()
        except TelenotAuthError as err:
            raise HomeAssistantError(f"Panel rejected the keypad code: {err}") from err
        except (TelenotError, TimeoutError) as err:
            raise HomeAssistantError(f"Telenot command failed: {err}") from err
        finally:
            self._schedule_idle_disconnect()

    async def async_press_keys(self, keys: list[int]) -> None:
        """Press a sequence of keypad keys."""

        async def run() -> None:
            for key in keys:
                await self.client.press(key)

        await self._command(run)

    async def async_set_alarm(self, target: AlarmControlPanelState, code: str | None) -> None:
        """Arm or disarm via the keypad dialogue (ES/IS/US key, code prompt, OK)."""
        key = {
            AlarmControlPanelState.ARMED_AWAY: Key.ARM_AWAY,
            AlarmControlPanelState.ARMED_HOME: Key.ARM_HOME,
            AlarmControlPanelState.DISARMED: Key.DISARM,
        }[target]
        code = code or self.config_entry.data[CONF_KEYPAD_CODE]
        sequence = self.options[CONF_ARM_SEQUENCE]

        async def run() -> None:
            await self.client.press(key)
            if sequence == SEQUENCE_KEY_ONLY:
                return
            if sequence == SEQUENCE_AUTO:
                await self.client.wait_display_change(PROMPT_TIMEOUT)
                if not _CODE_PROMPT.search(" ".join(self.client.state.display)):
                    return
            for digit in code:
                await self.client.press(ord(digit))
            await self.client.press(Key.OK)
            if await self.client.wait_display_change(PROMPT_TIMEOUT):
                text = " ".join(self.client.state.display)
                if _REFUSED.search(text):
                    raise HomeAssistantError(f"Panel refused: {text}")

        if target != AlarmControlPanelState.DISARMED:
            self.arming_target = target
            self.async_update_listeners()
            if self._arming_unsub is not None:
                self._arming_unsub()
            self._arming_unsub = self.hass.loop.call_later(
                ARMING_TIMEOUT, self._clear_arming, target
            ).cancel
        try:
            await self._command(run)
        except HomeAssistantError:
            self.arming_target = None
            self.async_update_listeners()
            raise

    @callback
    def _clear_arming(self, target: AlarmControlPanelState) -> None:
        self._arming_unsub = None
        if self.arming_target == target:
            self.arming_target = None
            self.async_update_listeners()

    async def async_toggle_switch(self, index: int) -> None:
        """Toggle switching function ``index``."""
        response = await self._command(lambda: self.client.press_and_confirm(switch_key(index)))
        if response == 2:
            raise HomeAssistantError("Operation not possible, function disabled in the panel")

    async def async_toggle_area_lock(self, index: int) -> None:
        """Lock/unlock (bypass) Meldebereich ``index``."""
        response = await self._command(lambda: self.client.press_and_confirm(area_lock_key(index)))
        if response == 2:
            raise HomeAssistantError("Operation not possible, function disabled in the panel")

    # ------------------------------------------------------------------ derived state

    def _indicator(self, source: str) -> bool | None:
        """Evaluate an indicator source; None if not resolvable."""
        state = self.client.state
        kind, _, number = source.partition("_")
        if not number.isdigit():
            return None
        idx = int(number) - 1
        if kind == "led":
            return state.free_leds[idx].active
        if kind == "area":
            return state.area_info[idx].active
        if kind == "switch":
            return state.switches[idx]
        return None

    def _auto_source(self, pattern: re.Pattern[str]) -> str:
        for idx, text in sorted(self.labels.free_leds.items()):
            if pattern.search(text):
                return f"led_{idx}"
        for idx, text in sorted(self.labels.areas.items()):
            if pattern.search(text):
                return f"area_{idx}"
        for idx, text in sorted(self.labels.switches.items()):
            if pattern.search(text):
                return f"switch_{idx}"
        return SOURCE_LCD_LATCHED

    def resolved_source(self, option: str) -> str:
        """Return the indicator actually used for an armed state."""
        source = self.options[option]
        if source == SOURCE_AUTO:
            return self._auto_source(_AWAY if option == CONF_ARMED_AWAY_SOURCE else _HOME)
        return source

    def _is_armed(self, option: str) -> bool:
        source = self.resolved_source(option)
        if source == SOURCE_NONE:
            return False
        if source == SOURCE_LCD_LATCHED:
            wanted = (
                AlarmControlPanelState.ARMED_AWAY
                if option == CONF_ARMED_AWAY_SOURCE
                else AlarmControlPanelState.ARMED_HOME
            )
            return self.latched_state == wanted
        if source == SOURCE_LCD:
            pattern = _AWAY if option == CONF_ARMED_AWAY_SOURCE else _HOME
            return bool(pattern.search(" ".join(self.client.state.display)))
        return bool(self._indicator(source))

    def alarm_state(self) -> AlarmControlPanelState:
        """Map keypad indicators to an alarm control panel state."""
        if self.client.state.alarm.active:
            return AlarmControlPanelState.TRIGGERED
        if self._is_armed(CONF_ARMED_AWAY_SOURCE):
            return AlarmControlPanelState.ARMED_AWAY
        if self._is_armed(CONF_ARMED_HOME_SOURCE):
            return AlarmControlPanelState.ARMED_HOME
        if self.arming_target is not None:
            return AlarmControlPanelState.ARMING
        return AlarmControlPanelState.DISARMED
