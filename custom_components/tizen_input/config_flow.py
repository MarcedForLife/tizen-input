"""Config flow for Tizen Input."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_MODEL, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .client import Source, fetch_device_info
from .const import CONF_DEFAULT_ENTITY, CONF_DEFAULT_INPUT, CONF_SOURCES, DEFAULT_ENTITY_SERVICES, DOMAIN
from .cycle import cycle_position
from .power import PowerControlError, RemoteControlDenied, RemoteControlTimedOut, RemoteControlUnreachable, pair

# Pairing is optional, so a display can be added while it's off and paired later from Reconfigure.
SKIP_PAIRING = "skip_pairing"

PAIRING_ERRORS: dict[type[PowerControlError], str] = {
    RemoteControlDenied: "pairing_denied",
    RemoteControlTimedOut: "pairing_timeout",
    RemoteControlUnreachable: "pairing_cannot_connect",
}


class TizenInputConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Samsung Tizen display by address, then pair it for power control."""

    VERSION = 1

    def __init__(self) -> None:
        self._title = ""
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TizenInputOptionsFlow:
        return TizenInputOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                device = await fetch_device_info(async_get_clientsession(self.hass), host)
            except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                if device.get("OS") != "Tizen":
                    errors["base"] = "not_tizen"
                else:
                    await self.async_set_unique_id(device["id"])
                    self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                    self._title = device.get("name") or device.get("modelName") or host
                    self._data = {CONF_HOST: host, CONF_MODEL: device.get("modelName"), CONF_MAC: device.get("wifiMac")}
                    return await self.async_step_pair()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Get a remote control token, which power control needs, unless the user skips it to pair later."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(SKIP_PAIRING):
                return self.async_create_entry(title=self._title, data=self._data)
            if token := await self._async_pair(self._data[CONF_HOST], errors):
                return self.async_create_entry(title=self._title, data={**self._data, CONF_TOKEN: token})
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({vol.Optional(SKIP_PAIRING, default=False): bool}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pair an existing entry, such as one added before power control."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None and (token := await self._async_pair(entry.data[CONF_HOST], errors)):
            return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})
        return self.async_show_form(
            step_id="reconfigure", description_placeholders={"name": entry.title}, errors=errors
        )

    async def _async_pair(self, host: str, errors: dict[str, str]) -> str | None:
        """The token the display grants, or None with the reason in errors."""
        try:
            return await pair(async_get_clientsession(self.hass), host)
        except PowerControlError as error:
            errors["base"] = PAIRING_ERRORS.get(type(error), "unknown")
            return None


def input_choices(entry: ConfigEntry) -> dict[str, Source]:
    """The display's known inputs in cycle order, keyed by type and number since labels can repeat."""
    sources = [Source.from_data(stored) for stored in entry.data.get(CONF_SOURCES) or [] if Source.valid(stored)]
    return {f"{source.type} {source.number}": source for source in sorted(sources, key=cycle_position)}


def options_schema(choices: dict[str, Source]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_DEFAULT_ENTITY): EntitySelector(
                EntitySelectorConfig(filter={"domain": list(DEFAULT_ENTITY_SERVICES)})
            ),
            vol.Optional(CONF_DEFAULT_INPUT): SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=value, label=source.label) for value, source in choices.items()],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class TizenInputOptionsFlow(OptionsFlow):
    """Choose what a Cycle press falls back to when no input has a signal."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        choices = input_choices(self.config_entry)
        if user_input is not None:
            options: dict[str, Any] = {}
            if entity_id := user_input.get(CONF_DEFAULT_ENTITY):
                options[CONF_DEFAULT_ENTITY] = entity_id
            if choice := user_input.get(CONF_DEFAULT_INPUT):
                options[CONF_DEFAULT_INPUT] = choices[choice].key
            return self.async_create_entry(data=options)
        default_input = self.config_entry.options.get(CONF_DEFAULT_INPUT)
        suggested = {
            CONF_DEFAULT_ENTITY: self.config_entry.options.get(CONF_DEFAULT_ENTITY),
            CONF_DEFAULT_INPUT: next((value for value, source in choices.items() if source.key == default_input), None),
        }
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(options_schema(choices), suggested)
        )
