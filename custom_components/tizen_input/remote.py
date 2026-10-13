"""Remote for a Samsung Tizen display."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    DEFAULT_NUM_REPEATS,
    RemoteEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TizenInputConfigEntry
from .entity import TizenInputEntity
from .power import PowerControlError, PowerController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TizenInputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if (power := entry.runtime_data.power).paired:
        async_add_entities([TizenInputRemote(entry, power)])


class TizenInputRemote(TizenInputEntity, RemoteEntity):
    """Presses remote keys such as KEY_INFO, including ones the stock remote lacks, over the paired remote control channel."""

    _attr_name = None

    def __init__(self, entry: TizenInputConfigEntry, power: PowerController) -> None:
        super().__init__(entry, "remote")
        self._power = power

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._power.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._power.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._power.turn_on()
        except PowerControlError as error:
            raise HomeAssistantError(f"Couldn't turn the display on: {error}") from error

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._power.turn_off()
        except PowerControlError as error:
            raise HomeAssistantError(f"Couldn't turn the display off: {error}") from error

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Press the keys in order over one connection, delay_secs apart."""
        keys = list(command) * kwargs.get(ATTR_NUM_REPEATS, DEFAULT_NUM_REPEATS)
        try:
            await self._power.send_keys(keys, kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS))
        except PowerControlError as error:
            raise HomeAssistantError(f"Couldn't send {', '.join(keys)} to the display: {error}") from error
