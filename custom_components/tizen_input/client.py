"""Client for the Tizen Input app's multiscreen channel on a Samsung display.

Kept free of Home Assistant imports so the protocol can be tested on its own.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

APP_ID = "TizenInput.TizenInput"
CHANNEL = "tizeninput.control"
CLIENT_NAME = "Home Assistant"
ENCODED_CLIENT_NAME = base64.b64encode(CLIENT_NAME.encode()).decode()
API_PORT = 8001
# A display in deep standby drops off the network and is back within seconds of a
# wake, so retry often rather than backing off.
RECONNECT_SECONDS = 5
APP_START_SECONDS = 15
# An unreadable state within this window of a source.set is credited to that source.
UNREADABLE_LEARN_SECONDS = 10
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass(frozen=True)
class Source:
    """A video source as the app lists it."""

    type: str
    number: int
    name: str | None = None
    connected: bool = False

    @property
    def label(self) -> str:
        return self.name or f"{self.type} {self.number}"

    @property
    def key(self) -> dict[str, Any]:
        return {"type": self.type, "number": self.number}

    @property
    def stored(self) -> dict[str, Any]:
        """Fields worth persisting. Connection changes often, so it's left out."""
        return {"type": self.type, "number": self.number, "name": self.name}

    @staticmethod
    def valid(entry: Any) -> bool:
        return isinstance(entry, dict) and isinstance(entry.get("type"), str) and isinstance(entry.get("number"), int)

    @classmethod
    def from_data(cls, entry: dict[str, Any]) -> Source:
        return cls(entry["type"], entry["number"], entry.get("name"), bool(entry.get("connected")))


async def fetch_device_info(session: aiohttp.ClientSession, host: str, port: int = API_PORT) -> dict[str, Any]:
    """Read the display's own description from its local REST API."""
    async with session.get(f"http://{host}:{port}/api/v2/", timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        body = await response.json(content_type=None)
    return body["device"]


class TizenInputClient:
    """Holds the channel open and tracks the sources and state the app reports."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        sources: list[dict[str, Any]] | None = None,
        unreadable_source: dict[str, Any] | None = None,
        port: int = API_PORT,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._app_client_id: str | None = None
        self._app_present = asyncio.Event()
        self._sources_received = asyncio.Event()
        self._reconnect_requested = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []
        self._last_set: tuple[dict[str, Any], float] | None = None
        self.connected = False
        # Restored sources stay selectable while the app isn't running.
        self.sources: list[Source] = [Source.from_data(entry) for entry in sources or [] if Source.valid(entry)]
        # Whether the running app has listed the sources, so their signals are current.
        self.sources_known = False
        self.state: dict[str, Any] | None = None
        self.state_known = False
        self.unreadable_source = unreadable_source

    @property
    def app_present(self) -> bool:
        return self._app_client_id is not None

    @property
    def current_source(self) -> Source | None:
        """The selected source, inferring the unreadable one once it has been learned."""
        if not self.state_known:
            return None
        key = self.unreadable_source if self.state is None else self.state
        if key is None:
            return None
        return next(
            (source for source in self.sources if source.type == key.get("type") and source.number == key.get("number")),
            None,
        )

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _channel_url(self) -> str:
        return f"ws://{self._host}:{self._port}/api/v2/channels/{CHANNEL}?name={ENCODED_CLIENT_NAME}"

    def reconnect_now(self) -> None:
        """Skip the rest of the wait before the next connection attempt, for a display that just woke."""
        if self._socket is None:
            self._reconnect_requested.set()

    async def run(self) -> None:
        """Keep the channel connected until cancelled."""
        while True:
            try:
                async with self._session.ws_connect(self._channel_url(), heartbeat=30) as socket:
                    self._socket = socket
                    await self._receive(socket)
            except (aiohttp.ClientError, TimeoutError, OSError) as error:
                _LOGGER.debug("Channel connection to %s failed: %s", self._host, error)
            finally:
                self._socket = None
                self._set_app_client(None)
                if self.connected:
                    self.connected = False
                    self._notify()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._reconnect_requested.wait(), RECONNECT_SECONDS)
            self._reconnect_requested.clear()

    async def _receive(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        handlers = {
            "ms.channel.connect": self._on_channel_connect,
            "ms.channel.clientConnect": self._on_client_connect,
            "ms.channel.clientDisconnect": self._on_client_disconnect,
            "source.state": self._on_state,
            "source.list": self._on_list,
        }
        async for message in socket:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                payload = json.loads(message.data)
            except ValueError:
                continue
            event = payload.get("event")
            if event and event.startswith("ms."):
                _LOGGER.debug("Channel event %s: %s", event, payload.get("data", {}).get("id"))
            handler = handlers.get(event)
            if handler:
                await handler(payload)

    async def _on_channel_connect(self, payload: dict[str, Any]) -> None:
        self.connected = True
        clients = payload.get("data", {}).get("clients", [])
        host = next((client for client in clients if client.get("isHost")), None)
        self._set_app_client(host.get("id") if host else None)
        if host:
            await self._request_snapshot()
        self._notify()

    async def _on_client_connect(self, payload: dict[str, Any]) -> None:
        client = payload.get("data", {})
        if client.get("isHost"):
            self._set_app_client(client.get("id"))
            await self._request_snapshot()
            self._notify()

    async def _on_client_disconnect(self, payload: dict[str, Any]) -> None:
        if payload.get("data", {}).get("id") == self._app_client_id:
            self._set_app_client(None)
            self._notify()

    def _from_app(self, payload: dict[str, Any]) -> bool:
        """Any LAN client can join the channel, so only the app's own messages count."""
        return self._app_client_id is not None and payload.get("from") == self._app_client_id

    async def _on_state(self, payload: dict[str, Any]) -> None:
        if not self._from_app(payload):
            return
        self.state = payload.get("data")
        self.state_known = True
        if self.state is None and self._last_set is not None:
            target, sent_at = self._last_set
            if time.monotonic() - sent_at <= UNREADABLE_LEARN_SECONDS:
                self.unreadable_source = target
        self._notify()

    async def _on_list(self, payload: dict[str, Any]) -> None:
        if not self._from_app(payload) or not isinstance(payload.get("data"), list):
            return
        self.sources = [Source.from_data(entry) for entry in payload["data"] if Source.valid(entry)]
        self.sources_known = True
        self._sources_received.set()
        self._notify()

    def _set_app_client(self, client_id: str | None) -> None:
        self._app_client_id = client_id
        self.sources_known = False
        if client_id is None:
            self._app_present.clear()
            self.state_known = False
        else:
            self._app_present.set()

    async def _request_snapshot(self) -> None:
        await self._emit("source.list")
        await self._emit("source.get")

    async def _emit(self, event: str, data: Any = None) -> None:
        if self._socket is None or self._socket.closed:
            raise ConnectionError("Channel is not connected")
        await self._socket.send_json({"method": "ms.channel.emit", "params": {"event": event, "data": data, "to": "host"}})

    async def wait_for_app(self, timeout: float) -> None:
        """Wait for the app to be on the channel, raising TimeoutError after timeout seconds."""
        await asyncio.wait_for(self._app_present.wait(), timeout)

    async def refresh_sources(self, timeout: float) -> None:
        """Request the source list and wait up to timeout seconds for the app's reply.

        Raises ConnectionError when the app isn't on the channel and TimeoutError when it doesn't
        reply. Either way the last known list stays.
        """
        if not self.app_present:
            raise ConnectionError("The app isn't on the channel")
        self._sources_received.clear()
        await self._emit("source.list")
        await asyncio.wait_for(self._sources_received.wait(), timeout)

    async def start_app(self) -> bool:
        """Launch the app unless it's on the channel, and wait for it to join. Returns whether it was launched."""
        if self.app_present:
            return False
        await self.launch_app()
        await self.wait_for_app(APP_START_SECONDS)
        return True

    async def set_source(self, source: Source) -> None:
        """Select a source, starting the app first if it isn't on the channel."""
        await self.start_app()
        self._last_set = (source.key, time.monotonic())
        await self._emit("source.set", source.key)

    async def launch_app(self) -> None:
        url = f"http://{self._host}:{self._port}/api/v2/applications/{APP_ID}"
        async with self._session.post(url, timeout=REQUEST_TIMEOUT) as response:
            response.raise_for_status()
