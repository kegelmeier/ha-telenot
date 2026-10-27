"""Connection handling and shared state for the Telenot integration."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ACCESS_INSTALLER,
    ACCESS_OPERATOR,
    ACCESS_REMOTE_CONTROL,
    ACTION_ARM_AWAY,
    ACTION_ARM_HOME,
    ACTION_DISARM,
    CONF_ACCESS_LEVEL,
    CONF_AES_KEY,
    CONF_ALARM_CODE,
    CONF_ARM_SEQUENCE,
    CONF_ARMED_AWAY_SOURCE,
    CONF_ARMED_HOME_SOURCE,
    CONF_CODE_REQUIRED_FOR,
    CONF_CONNECTION_MODE,
    CONF_EXTENDED_HEADER,
    CONF_FREISCHALTCODE,
    CONF_IDLE_TIMEOUT,
    CONF_KEY_NUMBER,
    CONF_KEYPAD_ADDRESS,
    CONF_KEYPAD_CODE,
    CONF_LED_LABELS,
    CONF_SCAN_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    MODE_ON_DEMAND,
    NUM_FREE_LEDS,
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
    TelenotKeyError,
    area_lock_key,
    switch_key,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SNAPSHOT_DELAY = 3.0
PROMPT_TIMEOUT = 4.0
MAX_BACKOFF = 300
# The panel closes the remote session now and then on its own (observed about every
# 35 minutes). Reconnect right away and keep showing the last complete state for up to
# RECONNECT_GRACE seconds, so such a hand-over does not make every entity unavailable.
RECONNECT_DELAY = 1.0
RECONNECT_GRACE = 60.0
ARMING_TIMEOUT = 120
# message list reader: scroll the keypad's open-detector list (DOWN key) when the
# "ready" LEDs go out, and again every MESSAGES_INTERVAL while something stays open
READ_DELAY = 3.0
MESSAGES_INTERVAL = 120
USER_IDLE = 30.0  # never scroll while someone is operating the keypad
MAX_MESSAGES = 64

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
_READY = re.compile(r"bereit|ready", re.IGNORECASE)
_LIST_END = re.compile(
    r"keine\s+(weiteren\s+)?meldung|no\s+(more\s+)?(messages|entries)", re.IGNORECASE
)

# Event log (event entity) types
EVENT_ARMED_AWAY = "armed_away"
EVENT_ARMED_HOME = "armed_home"
EVENT_DISARMED = "disarmed"
EVENT_TRIGGERED = "triggered"
EVENT_FAULT = "fault"
EVENT_FAULT_CLEARED = "fault_cleared"
EVENT_COMMAND_FAILED = "command_failed"
EVENT_DETECTOR_OPENED = "detector_opened"
EVENT_DETECTOR_CLOSED = "detector_closed"
EVENT_TYPES = [
    EVENT_ARMED_AWAY,
    EVENT_ARMED_HOME,
    EVENT_DISARMED,
    EVENT_TRIGGERED,
    EVENT_FAULT,
    EVENT_FAULT_CLEARED,
    EVENT_COMMAND_FAILED,
    EVENT_DETECTOR_OPENED,
    EVENT_DETECTOR_CLOSED,
]
_STATE_EVENTS = {
    AlarmControlPanelState.ARMED_AWAY: EVENT_ARMED_AWAY,
    AlarmControlPanelState.ARMED_HOME: EVENT_ARMED_HOME,
    AlarmControlPanelState.DISARMED: EVENT_DISARMED,
    AlarmControlPanelState.TRIGGERED: EVENT_TRIGGERED,
}
_TARGET_ACTIONS = {
    AlarmControlPanelState.ARMED_AWAY: ACTION_ARM_AWAY,
    AlarmControlPanelState.ARMED_HOME: ACTION_ARM_HOME,
    AlarmControlPanelState.DISARMED: ACTION_DISARM,
}

type TelenotConfigEntry = ConfigEntry[TelenotCoordinator]

_UNSET: Any = object()


def _duration(start: datetime | None, end: datetime) -> str:
    if start is None:
        return "an unknown time"
    seconds = int((end - start).total_seconds())
    return f"{seconds // 60} min {seconds % 60} s"


def detector_key(line1: str, line2: str) -> str:
    """Name of a detector from its message-list screen ("Kippfenster re." + "OG")."""
    return " ".join(f"{line1} {line2}".split())


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
        # remote access released for the BuildSec app (switch entity), kept across restarts
        self.paused = False
        self._resumed = asyncio.Event()
        self.panel_device_id: str | None = None
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        # last panel-reported state, None until a complete snapshot after (re)connect
        self._last_state: AlarmControlPanelState | None = None
        self._last_fault: bool | None = None
        # detectors ever seen in the keypad message list: name -> {"text", "location"}
        self.detectors: dict[str, dict[str, str]] = {}
        # detectors currently listed, None until the list was read once
        self.open_detectors: set[str] | None = None
        self.messages_read_at: float | None = None
        self._detector_listeners: list[Callable[[list[str]], None]] = []
        self._ready: Any = _UNSET
        self._read_unsub: CALLBACK_TYPE | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._last_user_key = 0.0
        # last complete state, shown while a dropped session is being re-established
        self._held_state: PanelState | None = None
        self._grace_unsub: CALLBACK_TYPE | None = None
        # connection statistics (also exposed on the connection sensor)
        self.connected_since: datetime | None = None
        self.last_disconnect: datetime | None = None
        self.last_disconnect_reason: str | None = None
        self.disconnects = 0
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
            self.paused = bool(stored.get("paused", False))
            self.detectors = dict(stored.get("detectors") or {})
        if self.paused:
            _LOGGER.info("Remote access is paused, not connecting to the Telenot panel")
        if self.on_demand:
            if not self.paused:
                await self.async_config_entry_first_refresh()
            return
        try:
            if not self.paused:
                await self._connect()
        except (TelenotAuthError, TelenotKeyError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TelenotError as err:
            _LOGGER.warning("Telenot panel not reachable yet, will retry: %s", err)
        self.config_entry.async_create_background_task(
            self.hass, self._connection_loop(), f"{DOMAIN}-connection"
        )

    async def async_shutdown(self) -> None:
        """Close the session when the entry is unloaded."""
        self._shutting_down = True
        self._resumed.set()
        self._cancel_idle()
        self._cancel_read()
        if self._read_task is not None:
            self._read_task.cancel()
        if self._arming_unsub is not None:
            self._arming_unsub()
            self._arming_unsub = None
        self._end_grace()
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
        """Available once the panel sent its full status (persistent) or the last poll succeeded.

        After a (re)connect, entities stay unavailable until display and LEDs have been
        received, so a stale state is never shown as current.
        """
        if self.paused:
            return False
        if self.on_demand:
            return self.last_update_success
        if self.client.connected and self.client.state.complete:
            return True
        return self._held_state is not None

    @property
    def state(self) -> PanelState:
        """Keypad state for the entities: live, or the held-over one during a reconnect."""
        if self._held_state is not None and not self.client.state.complete:
            return self._held_state
        return self.client.state

    # ------------------------------------------------------------------ event log

    def add_event_listener(
        self, callback_: Callable[[str, dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Subscribe to event-log entries; returns an unsubscribe function."""
        self._event_listeners.append(callback_)
        return lambda: (
            self._event_listeners.remove(callback_) if callback_ in self._event_listeners else None
        )

    @callback
    def _emit(self, event_type: str, **attributes: Any) -> None:
        for listener in list(self._event_listeners):
            listener(event_type, attributes)

    @callback
    def _track_events(self) -> None:
        """Turn panel-reported state changes into event-log entries."""
        state = self.client.state
        if not (self.client.connected and state.complete):
            return
        display = " / ".join(line for line in state.display if line)
        current = self.alarm_state()
        if current in _STATE_EVENTS:  # ARMING is our own transient state
            if self._last_state is not None and current != self._last_state:
                self._emit(_STATE_EVENTS[current], source="panel", display=display)
            self._last_state = current
        fault = state.fault.active
        if self._last_fault is not None and fault != self._last_fault:
            self._emit(
                EVENT_FAULT if fault else EVENT_FAULT_CLEARED, source="panel", display=display
            )
        self._last_fault = fault

    async def _connection_loop(self) -> None:
        backoff = 5
        while not self._shutting_down:
            if self.client.connected:
                await self._disconnected.wait()
                if self._shutting_down:
                    return
                if not self.paused:
                    backoff = RECONNECT_DELAY
            if self.paused:
                await self._resumed.wait()
                if self._shutting_down:
                    return
                backoff = 5
                continue
            await asyncio.sleep(backoff)
            if self.paused:
                continue
            try:
                await self._connect()
            except (TelenotAuthError, TelenotKeyError) as err:
                # Never keep retrying with wrong credentials: panels count failed
                # attempts and may lock out or report manipulation.
                _LOGGER.error("Telenot panel rejected the credentials: %s", err)
                self.config_entry.async_start_reauth(self.hass)
                return
            except TelenotError as err:
                _LOGGER.debug("Reconnect failed: %s", err)
                # 1, 2, 4, ... s after a panel-side drop (the panel takes a few seconds
                # before it accepts the next session), 5, 10, ... s otherwise.
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                backoff = 5

    async def _connect(self) -> None:
        async with self._lock:
            if self.client.connected:
                return
            if self.paused:
                raise TelenotError("Remote access is paused in Home Assistant")
            self._disconnected.clear()
            await self.client.connect()
            if self.paused:  # paused while the session was being opened
                await self.client.disconnect()
                raise TelenotError("Remote access is paused in Home Assistant")
        _LOGGER.debug(
            "Connected to Telenot panel (family=%s firmware=%s text crc=%s)",
            self.client.state.device_family,
            self.client.state.firmware,
            self.client.text_crc,
        )
        self.connected_since = dt_util.utcnow()
        if self.last_disconnect is not None and not self.on_demand:
            _LOGGER.info(
                "Reconnected to Telenot panel after %.1f s",
                (self.connected_since - self.last_disconnect).total_seconds(),
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
        if self.panel_device_id is not None:
            dr.async_get(self.hass).async_update_device(
                self.panel_device_id, sw_version=state.firmware
            )

    async def _refresh_labels(self) -> None:
        try:
            labels = await self.client.load_labels()
        except (TelenotError, TimeoutError) as err:
            _LOGGER.warning("Could not download label texts from the panel: %s", err)
            return
        finally:
            self._labels_task = None
        areas_changed = labels.areas != self.labels.areas
        self.labels = labels
        await self._store.async_save(self._store_data())
        self._enable_labelled_entities()
        self.async_update_listeners()
        if areas_changed and not self._shutting_down:
            # Area devices are created from the labels at setup.
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        if self.on_demand:
            self._schedule_idle_disconnect()

    def _store_data(self) -> dict[str, Any]:
        return {
            "labels": self.labels.as_dict(),
            "latched_state": self.latched_state,
            "paused": self.paused,
            "detectors": self.detectors,
        }

    # ------------------------------------------------------------------ pause

    async def async_pause(self) -> None:
        """Release the remote access so the BuildSec app can connect."""
        if self.paused:
            return
        self.paused = True
        self._resumed.clear()
        self._cancel_idle()
        self._cancel_read()
        self._end_grace()
        await self._store.async_save(self._store_data())
        async with self._command_lock:  # let a running command finish first
            await self.client.disconnect()
        _LOGGER.info("Remote access paused, Telenot session closed")
        self.async_update_listeners()

    async def async_resume(self) -> None:
        """Take the remote access back."""
        if not self.paused:
            return
        self.paused = False
        await self._store.async_save(self._store_data())
        _LOGGER.info("Remote access resumed, reconnecting to the Telenot panel")
        self.async_update_listeners()
        if self.on_demand:
            await self.async_request_refresh()
        else:
            self._resumed.set()

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
        if not self.client.connected:
            # The client reports the dropped session before the disconnect listeners
            # run; hold the state now so entities never flash unavailable.
            self._hold_state()
        elif self._held_state is not None and self.client.state.complete:
            self._end_grace()
        text = " ".join(self.client.state.display)
        if _AWAY.search(text):
            self._set_latched(AlarmControlPanelState.ARMED_AWAY)
        elif _HOME.search(text):
            self._set_latched(AlarmControlPanelState.ARMED_HOME)
        elif _DISARMED.search(text):
            self._set_latched(AlarmControlPanelState.DISARMED)
        if self.arming_target is not None and self.alarm_state() == self.arming_target:
            self.arming_target = None
        self._track_events()
        self._track_ready()
        self.async_set_updated_data(self.client.state)

    @callback
    def _on_client_disconnect(self, error: Exception | None) -> None:
        self._disconnected.set()
        self._ready = _UNSET
        self._cancel_read()
        if self._shutting_down:
            return
        now = dt_util.utcnow()
        unexpected = error is not None and not self.paused and not self.on_demand
        if unexpected:
            self.disconnects += 1
            self.last_disconnect = now
            self.last_disconnect_reason = str(error)
            _LOGGER.info(
                "Telenot panel ended the session after %s (%s), reconnecting",
                _duration(self.connected_since, now),
                error,
            )
        else:
            _LOGGER.debug("Telenot session closed: %s", error)
        self.connected_since = None
        if unexpected:
            self._hold_state()
        else:
            self._end_grace()
        if self._held_state is None:
            self._last_state = None
            self._last_fault = None
        self.async_update_listeners()

    @callback
    def _hold_state(self) -> None:
        """Keep the last complete state (and the event baseline) through a short reconnect."""
        if (
            self._held_state is not None
            or not self.client.state.complete
            or self.paused
            or self.on_demand
            or self._shutting_down
        ):
            return
        self._held_state = self.client.state
        self._grace_unsub = async_call_later(self.hass, RECONNECT_GRACE, self._grace_expired)

    @callback
    def _grace_expired(self, _now: datetime) -> None:
        self._grace_unsub = None
        if self._held_state is None:
            return
        _LOGGER.warning(
            "Telenot panel still not reachable %.0f s after the session ended", RECONNECT_GRACE
        )
        self._end_grace()
        if not self.client.state.complete:
            self._last_state = None
            self._last_fault = None
        self.async_update_listeners()

    @callback
    def _end_grace(self) -> None:
        self._held_state = None
        if self._grace_unsub is not None:
            self._grace_unsub()
            self._grace_unsub = None

    # ------------------------------------------------------------------ on-demand mode

    async def _async_update_data(self) -> PanelState:
        """Poll in on-demand mode: connect, collect the pushed state, disconnect later."""
        if self.paused:
            raise UpdateFailed("Remote access is paused in Home Assistant")
        try:
            await self._ensure_connected()
        except (TelenotAuthError, TelenotKeyError) as err:
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
        if not self.on_demand or self._shutting_down:
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
        if self.paused:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="paused")
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

        self._user_activity()
        await self._command(run)

    def _user_activity(self) -> None:
        self._last_user_key = time.monotonic()

    async def async_set_alarm(self, target: AlarmControlPanelState, code: str | None) -> None:
        """Arm or disarm via the keypad dialogue (ES/IS/US key, code prompt, OK).

        ``code`` is the Home Assistant alarm code. It is checked here and never sent to
        the panel; the panel always gets the stored keypad code.
        """
        action = _TARGET_ACTIONS[target]
        self.check_code(action, code)
        key = {
            AlarmControlPanelState.ARMED_AWAY: Key.ARM_AWAY,
            AlarmControlPanelState.ARMED_HOME: Key.ARM_HOME,
            AlarmControlPanelState.DISARMED: Key.DISARM,
        }[target]
        code = self.config_entry.data[CONF_KEYPAD_CODE]
        sequence = self.options[CONF_ARM_SEQUENCE]
        self._user_activity()

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
        except HomeAssistantError as err:
            self.arming_target = None
            self.async_update_listeners()
            self._emit(
                EVENT_COMMAND_FAILED, source="home_assistant", action=action, reason=str(err)
            )
            raise

    def code_required(self, action: str) -> bool:
        """Whether Home Assistant asks for the alarm code for ``action``."""
        return (
            bool(self.options[CONF_ALARM_CODE]) and action in self.options[CONF_CODE_REQUIRED_FOR]
        )

    def check_code(self, action: str, code: str | None) -> None:
        """Reject a missing or wrong alarm code before anything reaches the panel."""
        if not self.code_required(action):
            return
        expected = str(self.options[CONF_ALARM_CODE])
        if code and hmac.compare_digest(code.encode(), expected.encode()):
            return
        self._emit(
            EVENT_COMMAND_FAILED, source="home_assistant", action=action, reason="wrong code"
        )
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="wrong_code")

    @callback
    def _clear_arming(self, target: AlarmControlPanelState) -> None:
        self._arming_unsub = None
        if self.arming_target == target:
            self.arming_target = None
            self.async_update_listeners()

    async def async_toggle_switch(self, index: int) -> None:
        """Toggle switching function ``index``."""
        await self._toggle(f"switch_{index}", switch_key(index))

    async def async_toggle_area_lock(self, index: int) -> None:
        """Lock/unlock (bypass) Meldebereich ``index``."""
        await self._toggle(f"area_lock_{index}", area_lock_key(index))

    async def _toggle(self, action: str, key: int) -> None:
        self._user_activity()
        try:
            response = await self._command(lambda: self.client.press_and_confirm(key))
            if response == 2:
                raise HomeAssistantError("Operation not possible, function disabled in the panel")
        except HomeAssistantError as err:
            self._emit(
                EVENT_COMMAND_FAILED, source="home_assistant", action=action, reason=str(err)
            )
            raise

    # ------------------------------------------------------------------ derived state

    def _indicator(self, source: str) -> bool | None:
        """Evaluate an indicator source; None if not resolvable."""
        state = self.state
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

    def led_label(self, index: int) -> str | None:
        """Label of free LED ``index``: options first, then the panel's text."""
        custom = str((self.options.get(CONF_LED_LABELS) or {}).get(f"led_{index}") or "")
        return custom.strip() or self.labels.free_leds.get(index)

    def _auto_source(self, pattern: re.Pattern[str]) -> str:
        for idx in range(1, NUM_FREE_LEDS + 1):
            if (text := self.led_label(idx)) and pattern.search(text):
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
            return bool(pattern.search(" ".join(self.state.display)))
        return bool(self._indicator(source))

    def alarm_state(self) -> AlarmControlPanelState:
        """Map keypad indicators to an alarm control panel state."""
        if self.state.alarm.active:
            return AlarmControlPanelState.TRIGGERED
        if self._is_armed(CONF_ARMED_AWAY_SOURCE):
            return AlarmControlPanelState.ARMED_AWAY
        if self._is_armed(CONF_ARMED_HOME_SOURCE):
            return AlarmControlPanelState.ARMED_HOME
        if self.arming_target is not None:
            return AlarmControlPanelState.ARMING
        return AlarmControlPanelState.DISARMED

    # ------------------------------------------------------------------ message list

    @property
    def ready(self) -> bool | None:
        """Ready to arm: all LEDs labelled "Bereit" lit; None if no LED is labelled so."""
        state = self.state
        leds = [
            state.free_leds[i - 1]
            for i in range(1, NUM_FREE_LEDS + 1)
            if (label := self.led_label(i)) and _READY.search(label)
        ]
        if not leds:
            return None
        return all(led.active for led in leds)

    def add_detector_listener(self, callback_: Callable[[list[str]], None]) -> Callable[[], None]:
        """Subscribe to newly discovered detectors; returns an unsubscribe function."""
        self._detector_listeners.append(callback_)
        return lambda: (
            self._detector_listeners.remove(callback_)
            if callback_ in self._detector_listeners
            else None
        )

    @callback
    def _track_ready(self) -> None:
        """Read the message list when the ready LEDs go out; clear it when they light up."""
        state = self.client.state
        if not (state.connected and state.complete) or self.paused:
            return
        ready = self.ready
        if ready == self._ready:
            return
        self._ready = ready
        if ready is None:  # no LED labelled "Bereit": read only on request
            return
        if ready:
            self._cancel_read()
            self._apply_messages([])
        else:
            self._schedule_read(READ_DELAY)

    def _schedule_read(self, delay: float) -> None:
        if self.on_demand or self._shutting_down or self.paused:
            return
        self._cancel_read()
        self._read_unsub = self.hass.loop.call_later(delay, self._start_read).cancel

    def _cancel_read(self) -> None:
        if self._read_unsub is not None:
            self._read_unsub()
            self._read_unsub = None

    @callback
    def _start_read(self) -> None:
        self._read_unsub = None
        if self._read_task is None and not self._shutting_down:
            self._read_task = self.config_entry.async_create_background_task(
                self.hass, self._auto_read(), f"{DOMAIN}-messages"
            )

    async def _auto_read(self) -> None:
        try:
            if self.paused or not (self.client.connected and self.client.state.complete):
                return
            busy = USER_IDLE - (time.monotonic() - self._last_user_key)
            if busy > 0 or self._command_lock.locked():
                self._schedule_read(max(busy, READ_DELAY))
                return
            if self.alarm_state() == AlarmControlPanelState.DISARMED:
                try:
                    await self.async_read_messages()
                except HomeAssistantError as err:
                    _LOGGER.debug("Reading the message list failed: %s", err)
            if self._ready is not True:
                self._schedule_read(MESSAGES_INTERVAL)
        finally:
            self._read_task = None

    async def async_read_messages(self) -> list[str]:
        """Scroll through the keypad message list and update the detector entities."""
        if self.alarm_state() != AlarmControlPanelState.DISARMED:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_disarmed")
        entries = await self._command(self._scroll_messages)
        if entries is None:
            raise HomeAssistantError("The keypad did not show the message list")
        if self._ready is True:  # everything closed while we were scrolling
            entries = []
        return self._apply_messages(entries)

    async def _scroll_messages(self) -> list[tuple[str, str]] | None:
        """DOWN through "<area>/unscharf", the open detectors, "keine weiteren Meldungen".

        Returns None if the display did not behave like the message list.
        """
        client = self.client
        entries: list[tuple[str, str]] = []
        try:
            for step in range(MAX_MESSAGES + 1):
                await client.press(Key.DOWN)
                if not await client.wait_display_change(PROMPT_TIMEOUT):
                    return None
                line1, line2 = (" ".join(line.split()) for line in client.state.display)
                if _LIST_END.search(f"{line1} {line2}"):
                    return entries
                if _DISARMED.search(line2):  # area header ("Kegelmann" / "unscharf")
                    continue
                if step == 0:
                    return None  # not the message list (menu open?)
                entries.append((line1, line2))
            return None
        finally:
            await self._leave_message_list()

    async def _leave_message_list(self) -> None:
        """ESC goes back to the area header, a second ESC to the idle screen."""
        for _ in range(2):
            if not self.client.connected:
                return
            await self.client.press(Key.ESC)
            if not await self.client.wait_display_change(PROMPT_TIMEOUT):
                return
            display = self.client.state.display
            if not (_DISARMED.search(display[1]) or _LIST_END.search(" ".join(display))):
                return

    @callback
    def _apply_messages(self, entries: list[tuple[str, str]]) -> list[str]:
        found: dict[str, tuple[str, str]] = {}
        for line1, line2 in entries:
            found.setdefault(detector_key(line1, line2), (line1, line2))
        new = [name for name in found if name not in self.detectors]
        for name in new:
            text, location = found[name]
            self.detectors[name] = {"text": text, "location": location}
        previous, current = self.open_detectors, set(found)
        self.open_detectors = current
        self.messages_read_at = time.time()
        if new:
            self._store.async_delay_save(self._store_data, 1)
            for listener in list(self._detector_listeners):
                listener(new)
        if previous is not None:
            for name in sorted(current - previous):
                self._emit(EVENT_DETECTOR_OPENED, source="panel", detector=name)
            for name in sorted(previous - current):
                self._emit(EVENT_DETECTOR_CLOSED, source="panel", detector=name)
        self.async_update_listeners()
        return sorted(current)
