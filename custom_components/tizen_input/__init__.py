"""Switch the input of a Samsung Tizen display through the Tizen Input app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import TizenInputClient
from .const import CONF_SOURCES, CONF_UNREADABLE_SOURCE
from .power import PowerController

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.MEDIA_PLAYER, Platform.REMOTE]


@dataclass
class TizenInputRuntimeData:
    """The input channel client and the power controller, which turns the display on and off once it's paired."""

    client: TizenInputClient
    power: PowerController


type TizenInputConfigEntry = ConfigEntry[TizenInputRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: TizenInputConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    host = entry.data[CONF_HOST]
    client = TizenInputClient(
        session,
        host,
        sources=entry.data.get(CONF_SOURCES),
        unreadable_source=entry.data.get(CONF_UNREADABLE_SOURCE),
    )
    power = PowerController(
        session, host, entry.data.get(CONF_TOKEN), mac=entry.data.get(CONF_MAC), after_turn_on=client.reconnect_now
    )
    entry.runtime_data = TizenInputRuntimeData(client, power)

    @callback
    def remember(remembered: dict[str, Any]) -> None:
        """Keep what the display reports across restarts."""
        if any(entry.data.get(key) != value for key, value in remembered.items()):
            hass.config_entries.async_update_entry(entry, data={**entry.data, **remembered})

    @callback
    def remember_sources() -> None:
        """The input list and the learned unreadable source."""
        remember(
            {
                CONF_SOURCES: [source.stored for source in client.sources],
                CONF_UNREADABLE_SOURCE: client.unreadable_source,
            }
        )

    entry.async_on_unload(client.add_listener(remember_sources))
    entry.async_on_unload(power.add_listener(lambda: remember({CONF_TOKEN: power.token})))
    entry.async_create_background_task(hass, client.run(), f"tizen_input channel {host}")
    entry.async_create_background_task(hass, power.run(), f"tizen_input power {host}")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TizenInputConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
