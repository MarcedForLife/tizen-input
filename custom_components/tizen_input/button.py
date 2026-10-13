"""Cycle button for a Samsung Tizen display."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import aiohttp

from homeassistant.components.button import ButtonEntity
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, split_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TizenInputConfigEntry
from .client import Source
from .const import CONF_DEFAULT_ENTITY, CONF_DEFAULT_INPUT, DEFAULT_ENTITY_SERVICES, DOMAIN, TURN_ON_SERVICE
from .cycle import CycleAction, CycleStep, DisplayPower, choose_step
from .entity import TizenInputEntity
from .power import PowerControlError

_LOGGER = logging.getLogger(__name__)

LIST_REPLY_SECONDS = 2
# How long a press waits for the app to rejoin the channel after turning the display on,
# before launching it.
APP_RETURN_SECONDS = 15
# DisplayPort takes a couple of seconds to signal once the panel is on, so after a wake a
# press re-reads a list with no signal this many times, this often.
WAKE_SIGNAL_CHECKS = 5
SIGNAL_CHECK_SECONDS = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TizenInputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TizenInputCycleButton(entry)])


class TizenInputCycleButton(TizenInputEntity, ButtonEntity):
    """Moves the display to its next input with a signal, turning it on or off when a press calls for it."""

    _attr_translation_key = "cycle"

    def __init__(self, entry: TizenInputConfigEntry) -> None:
        super().__init__(entry, "cycle")
        self._entry = entry
        self._client = entry.runtime_data.client
        self._power = entry.runtime_data.power
        self._pressing = asyncio.Lock()

    async def async_press(self) -> None:
        """Drops a press that arrives while another runs, rather than queueing it."""
        if self._pressing.locked():
            _LOGGER.debug("Dropped a Cycle press on %s while another was running", self._entry.title)
            return
        async with self._pressing:
            try:
                await self._cycle()
            except (PowerControlError, aiohttp.ClientError, ConnectionError, TimeoutError) as error:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="cycle_failed",
                    translation_placeholders={"error": str(error) or type(error).__name__},
                ) from error

    async def _cycle(self) -> None:
        step = await self._decide(await self._display_power(), after_wake=False)
        if step.action is CycleAction.TURN_ON:
            await self._power.turn_on()
            with contextlib.suppress(TimeoutError):
                await self._client.wait_for_app(APP_RETURN_SECONDS)
            step = await self._decide(DisplayPower.KEEP, after_wake=True)
        _LOGGER.debug("Cycle press on %s: %s", self._entry.title, step)
        if step.reselect:
            await self._client.set_source(step.reselect)
        match step.action:
            case CycleAction.SELECT:
                await self._client.set_source(step.source)
            case CycleAction.TURN_OFF:
                await self._power.turn_off()
            case CycleAction.RUN_DEFAULTS:
                await self._run_defaults()

    async def _display_power(self) -> DisplayPower:
        if not self._power.paired:
            return DisplayPower.KEEP
        return DisplayPower.ON if await self._power.refresh() == "on" else DisplayPower.OFF

    async def _decide(self, power: DisplayPower, after_wake: bool) -> CycleStep:
        """Decide on a fresh input list, starting the app to read it when it isn't on the channel.

        Raises rather than fall back to the stored list. An off display turns on whatever its
        inputs, so its list isn't read.
        """
        app_launched = False
        if power is not DisplayPower.OFF:
            app_launched = await self._client.start_app()
            await self._client.refresh_sources(LIST_REPLY_SECONDS)
            if after_wake:
                await self._wait_for_a_signal()
        return choose_step(
            power,
            self._client.current_source,
            self._client.sources,
            has_default_entity=CONF_DEFAULT_ENTITY in self._entry.options,
            has_default_input=self._default_input() is not None,
            app_launched=app_launched,
        )

    async def _wait_for_a_signal(self) -> None:
        for _check in range(WAKE_SIGNAL_CHECKS):
            if any(source.connected for source in self._client.sources):
                return
            await asyncio.sleep(SIGNAL_CHECK_SECONDS)
            await self._client.refresh_sources(LIST_REPLY_SECONDS)

    async def _run_defaults(self) -> None:
        """Run the default entity, then select the default input, whichever are set."""
        if entity_id := self._entry.options.get(CONF_DEFAULT_ENTITY):
            domain, service = DEFAULT_ENTITY_SERVICES.get(split_entity_id(entity_id)[0], TURN_ON_SERVICE)
            await self.hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True, context=self._context
            )
        if default_input := self._default_input():
            await self._client.set_source(default_input)

    def _default_input(self) -> Source | None:
        stored = self._entry.options.get(CONF_DEFAULT_INPUT)
        return Source.from_data(stored) if Source.valid(stored) else None
