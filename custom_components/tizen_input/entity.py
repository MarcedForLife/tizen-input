"""Base entity for a Samsung Tizen display."""

from __future__ import annotations

import re

from homeassistant.const import CONF_MAC, CONF_MODEL
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.entity import Entity

from . import TizenInputConfigEntry
from .const import DOMAIN

MAC_PATTERN = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


class TizenInputEntity(Entity):
    """An entity on the display's device, updated by push."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: TizenInputConfigEntry, key: str) -> None:
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        # Some displays report "none" for their MAC, so only a real one links devices.
        mac = format_mac(entry.data.get(CONF_MAC) or "")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            connections={(CONNECTION_NETWORK_MAC, mac)} if MAC_PATTERN.fullmatch(mac) else set(),
            manufacturer="Samsung",
            model=entry.data.get(CONF_MODEL),
            name=entry.title,
        )
