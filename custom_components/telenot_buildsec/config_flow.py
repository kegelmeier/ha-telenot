"""Config flow for the Telenot integration."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    ACCESS_LEVELS,
    ACCESS_OPERATOR,
    CODE_ACTIONS,
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
    DEFAULT_KEYPAD_ADDRESS,
    DEFAULT_OPTIONS,
    DOMAIN,
    MODE_ON_DEMAND,
    MODE_PERSISTENT,
    NUM_FREE_LEDS,
    SEQUENCE_AUTO,
    SEQUENCE_CODE,
    SEQUENCE_KEY_ONLY,
    state_sources,
)
from .coordinator import create_client
from .pytelenot import (
    RESERVED_KEY_NUMBERS,
    TelenotAuthError,
    TelenotBusyError,
    TelenotConnectionError,
    TelenotError,
    TelenotKeyError,
    TelenotPanelError,
)

_LOGGER = logging.getLogger(__name__)

_HEX16 = re.compile(r"^[0-9a-fA-F]{16}$")
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_CODE = re.compile(r"^[0-9]{1,6}$")

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, vol.UNDEFINED)): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(
                CONF_ACCESS_LEVEL, default=defaults.get(CONF_ACCESS_LEVEL, ACCESS_OPERATOR)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=ACCESS_LEVELS,
                    translation_key=CONF_ACCESS_LEVEL,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_KEYPAD_CODE): PASSWORD,
            vol.Required(
                CONF_KEYPAD_ADDRESS,
                default=defaults.get(CONF_KEYPAD_ADDRESS, DEFAULT_KEYPAD_ADDRESS),
            ): NumberSelector(NumberSelectorConfig(min=0, max=15, mode=NumberSelectorMode.BOX)),
        }
    )


OPERATOR_SCHEMA = vol.Schema({vol.Required(CONF_FREISCHALTCODE): PASSWORD})
KEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_KEY_NUMBER): NumberSelector(
            NumberSelectorConfig(min=1, max=65534, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_AES_KEY): PASSWORD,
    }
)


async def validate_connection(data: dict[str, Any]) -> dict[str, Any]:
    """Open a remote keypad session once; return device info."""
    client = create_client(data)
    await client.connect()
    try:
        return {"firmware": client.state.firmware, "family": client.state.device_family}
    finally:
        await client.disconnect()


def _error_key(err: Exception) -> str:
    if isinstance(err, TelenotAuthError):
        return "invalid_auth"
    if isinstance(err, TelenotBusyError):
        return "busy"
    if isinstance(err, TelenotKeyError):
        return "invalid_key"
    if isinstance(err, TelenotPanelError):
        return {
            0x1B: "remote_disabled",
            0x1D: "invalid_keypad_address",
            0x23: "keypad_in_use",
            0x26: "lan_not_allowed",
            0x29: "ip_not_allowed",
        }.get(err.reason, "panel_error")
    if isinstance(err, (TelenotConnectionError, TimeoutError)):
        return "cannot_connect"
    return "unknown"


class TelenotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Telenot panel reached directly over LAN/WAN."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TelenotOptionsFlow:
        """Options."""
        return TelenotOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Host, port, access level and keypad code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not _CODE.match(user_input[CONF_KEYPAD_CODE]):
                errors[CONF_KEYPAD_CODE] = "invalid_code_format"
            else:
                self._data = {
                    **user_input,
                    CONF_HOST: user_input[CONF_HOST].strip(),
                    CONF_PORT: int(user_input[CONF_PORT]),
                    CONF_KEYPAD_ADDRESS: int(user_input[CONF_KEYPAD_ADDRESS]),
                }
                await self.async_set_unique_id(
                    f"{self._data[CONF_HOST]}:{self._data[CONF_PORT]}:{self._data[CONF_KEYPAD_ADDRESS]}"
                )
                self._abort_if_unique_id_configured()
                if self._data[CONF_ACCESS_LEVEL] == ACCESS_OPERATOR:
                    return await self.async_step_operator()
                return await self.async_step_key()
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(user_input or {}), errors=errors
        )

    async def async_step_operator(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Operator access: Freischaltcode (licence key) from the panel."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_FREISCHALTCODE].strip().replace(" ", "").replace("-", "")
            if not _HEX16.match(code):
                errors[CONF_FREISCHALTCODE] = "invalid_freischaltcode"
            else:
                self._data[CONF_FREISCHALTCODE] = code.lower()
                return await self._async_validate_and_create("operator", errors)
        return self.async_show_form(step_id="operator", data_schema=OPERATOR_SCHEMA, errors=errors)

    async def async_step_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Installer / remote control access: VdS key number and AES key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_AES_KEY].strip().replace(" ", "")
            number = int(user_input[CONF_KEY_NUMBER])
            if number in RESERVED_KEY_NUMBERS:
                errors[CONF_KEY_NUMBER] = "reserved_key_number"
            elif not _HEX32.match(key):
                errors[CONF_AES_KEY] = "invalid_aes_key"
            else:
                self._data[CONF_KEY_NUMBER] = number
                self._data[CONF_AES_KEY] = key.lower()
                return await self._async_validate_and_create("key", errors)
        return self.async_show_form(step_id="key", data_schema=KEY_SCHEMA, errors=errors)

    async def _async_validate_and_create(
        self, step_id: str, errors: dict[str, str]
    ) -> ConfigFlowResult:
        try:
            info = await validate_connection(self._data)
        except (TelenotError, TimeoutError) as err:
            _LOGGER.debug("Validation failed: %s", err)
            errors["base"] = _error_key(err)
        except Exception:
            _LOGGER.exception("Unexpected error validating the Telenot connection")
            errors["base"] = "unknown"
        else:
            _LOGGER.debug("Connected to panel: %s", info)
            if self.source == "reauth":
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(), data_updates=self._data
                )
            return self.async_create_entry(
                title=f"Telenot {self._data[CONF_HOST]}", data=self._data
            )
        schema = OPERATOR_SCHEMA if step_id == "operator" else KEY_SCHEMA
        if self.source == "reauth":
            step_id, schema = (
                "reauth_confirm",
                vol.Schema({vol.Required(CONF_KEYPAD_CODE): PASSWORD}),
            )
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The panel rejected the keypad code."""
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new keypad code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not _CODE.match(user_input[CONF_KEYPAD_CODE]):
                errors[CONF_KEYPAD_CODE] = "invalid_code_format"
            else:
                self._data[CONF_KEYPAD_CODE] = user_input[CONF_KEYPAD_CODE]
                return await self._async_validate_and_create("reauth_confirm", errors)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_KEYPAD_CODE): PASSWORD}),
            errors=errors,
        )


class TelenotOptionsFlow(OptionsFlowWithReload):
    """Connection mode and how the armed state is detected."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Single options form."""
        if user_input is not None:
            for key in (CONF_IDLE_TIMEOUT, CONF_SCAN_INTERVAL):
                user_input[key] = int(user_input[key])
            user_input[CONF_ALARM_CODE] = str(user_input.get(CONF_ALARM_CODE) or "").strip()
            user_input[CONF_LED_LABELS] = {
                key: text
                for key, value in (user_input.get(CONF_LED_LABELS) or {}).items()
                if (text := str(value or "").strip())
            }
            return self.async_create_entry(data=user_input)
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        sources = SelectSelector(
            SelectSelectorConfig(
                options=state_sources(),
                translation_key="state_source",
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CONNECTION_MODE, default=current[CONF_CONNECTION_MODE]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[MODE_PERSISTENT, MODE_ON_DEMAND],
                        translation_key=CONF_CONNECTION_MODE,
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=current[CONF_SCAN_INTERVAL]
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=30, max=86400, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_IDLE_TIMEOUT, default=current[CONF_IDLE_TIMEOUT]): NumberSelector(
                    NumberSelectorConfig(
                        min=10, max=3600, unit_of_measurement="s", mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_ARMED_AWAY_SOURCE, default=current[CONF_ARMED_AWAY_SOURCE]
                ): sources,
                vol.Required(
                    CONF_ARMED_HOME_SOURCE, default=current[CONF_ARMED_HOME_SOURCE]
                ): sources,
                vol.Required(CONF_ARM_SEQUENCE, default=current[CONF_ARM_SEQUENCE]): SelectSelector(
                    SelectSelectorConfig(
                        options=[SEQUENCE_AUTO, SEQUENCE_CODE, SEQUENCE_KEY_ONLY],
                        translation_key=CONF_ARM_SEQUENCE,
                    )
                ),
                vol.Optional(
                    CONF_ALARM_CODE,
                    description={"suggested_value": current[CONF_ALARM_CODE] or None},
                ): PASSWORD,
                vol.Required(
                    CONF_CODE_REQUIRED_FOR, default=current[CONF_CODE_REQUIRED_FOR]
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=CODE_ACTIONS,
                        multiple=True,
                        translation_key=CONF_CODE_REQUIRED_FOR,
                    )
                ),
                vol.Required(
                    CONF_EXTENDED_HEADER, default=current[CONF_EXTENDED_HEADER]
                ): BooleanSelector(),
                vol.Required(CONF_LED_LABELS): section(
                    vol.Schema(
                        {
                            vol.Optional(
                                f"led_{i}",
                                description={
                                    "suggested_value": current[CONF_LED_LABELS].get(f"led_{i}")
                                },
                            ): TextSelector()
                            for i in range(1, NUM_FREE_LEDS + 1)
                        }
                    ),
                    {"collapsed": False},
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
