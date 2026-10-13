"""Signal sensors for a Samsung Tizen display's inputs."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TizenInputConfigEntry
from .client import Source
from .entity import TizenInputEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TizenInputConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a sensor for each input when it's first listed, so inputs the display gains later get one too."""
    client = entry.runtime_data.client
    added: set[tuple[str, int]] = set()

    @callback
    def add_new_inputs() -> None:
        new_sources = [source for source in client.sources if (source.type, source.number) not in added]
        added.update((source.type, source.number) for source in new_sources)
        if new_sources:
            async_add_entities(TizenInputSignalSensor(entry, source) for source in new_sources)

    add_new_inputs()
    entry.async_on_unload(client.add_listener(add_new_inputs))


class TizenInputSignalSensor(TizenInputEntity, BinarySensorEntity):
    """On while the input has a signal, which it only has while its device sends video."""

    _attr_translation_key = "signal"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: TizenInputConfigEntry, source: Source) -> None:
        super().__init__(entry, f"signal_{source.type}_{source.number}")
        self._attr_translation_placeholders = {"input": source.label}
        self._client = entry.runtime_data.client
        self._type = source.type
        self._number = source.number

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._client.add_listener(self.async_write_ha_state))

    def _source(self) -> Source | None:
        return next(
            (source for source in self._client.sources if source.type == self._type and source.number == self._number),
            None,
        )

    @property
    def available(self) -> bool:
        """Signals are only current once the running app has listed the inputs."""
        return self._client.sources_known and self._source() is not None

    @property
    def is_on(self) -> bool | None:
        source = self._source()
        return source.connected if source else None
