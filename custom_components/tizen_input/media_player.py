"""Media player for a Samsung Tizen display."""

from __future__ import annotations

from typing import Any

import aiohttp

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TizenInputConfigEntry
from .entity import TizenInputEntity
from .power import PowerControlError

POWER_FEATURES = MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TizenInputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TizenInputMediaPlayer(entry)])


class TizenInputMediaPlayer(TizenInputEntity, MediaPlayerEntity):
    """The display's power, as its REST API reports it, and its input, as the Tizen Input app reports it.

    Stays available when the display drops off the network in deep standby, since magic packets
    still turn it on from there. Turning it on and off needs pairing.
    """

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.TV

    def __init__(self, entry: TizenInputConfigEntry) -> None:
        super().__init__(entry, "media_player")
        self._client = entry.runtime_data.client
        self._power = entry.runtime_data.power

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._client.add_listener(self.async_write_ha_state))
        self.async_on_remove(self._power.add_listener(self.async_write_ha_state))

    @property
    def state(self) -> MediaPlayerState:
        return MediaPlayerState.ON if self._power.is_on else MediaPlayerState.OFF

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        if self._power.paired:
            return MediaPlayerEntityFeature.SELECT_SOURCE | POWER_FEATURES
        return MediaPlayerEntityFeature.SELECT_SOURCE

    @property
    def source_list(self) -> list[str]:
        return [source.label for source in self._client.sources]

    @property
    def source(self) -> str | None:
        current = self._client.current_source
        return current.label if current else None

    async def async_select_source(self, source: str) -> None:
        target = next((candidate for candidate in self._client.sources if candidate.label == source), None)
        if target is None:
            raise HomeAssistantError(f"Unknown input {source}")
        try:
            await self._client.set_source(target)
        except (aiohttp.ClientError, ConnectionError, TimeoutError) as error:
            raise HomeAssistantError(f"Couldn't switch the display to {source}: {error}") from error

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
