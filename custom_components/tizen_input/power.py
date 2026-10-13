"""Power control for a Samsung Tizen display, with remote keys and magic packets (Wake-on-LAN).

Kept free of Home Assistant imports so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import aiohttp

from .client import API_PORT, ENCODED_CLIENT_NAME, fetch_device_info

_LOGGER = logging.getLogger(__name__)

REMOTE_PORT = 8002
REMOTE_CHANNEL = "samsung.remote.control"
POLL_SECONDS = 5
STATE_TIMEOUT_SECONDS = 2
CONNECT_SECONDS = 5
# A paired client is accepted at once. A display that has lost the token prompts again,
# which nobody sees while it's off, so a key press waits this long unless it's on.
KEY_ACCEPT_SECONDS = 5
PAIRING_SECONDS = 30
TURN_ON_RUNGS = 3
# How long each rung waits for the display to report on, and how often it checks.
SETTLE_SECONDS = 10
SETTLE_POLL_SECONDS = 1
# The display can report standby while a magic packet wake lands, and KEY_POWER toggles,
# so it would switch the display straight back off.
BURST_LANDING_SECONDS = 6
MAGIC_PACKET_PORTS = (9, 7)
MAGIC_PACKET_REPEATS = 3
MAC_DIGITS = re.compile(r"[0-9A-Fa-f]{12}")


class PowerControlError(Exception):
    """The display didn't carry out a power command."""


class RemoteControlDenied(PowerControlError):
    """The display refused the remote control connection."""


class RemoteControlTimedOut(PowerControlError):
    """The display didn't accept the remote control connection in time, usually an unanswered prompt."""


class RemoteControlUnreachable(PowerControlError):
    """The display's remote control channel couldn't be reached."""


class TurnOnFailed(PowerControlError):
    """The display still wasn't on after every rung."""


# Events the display sends instead of accepting a remote control connection.
REFUSALS: dict[str, type[PowerControlError]] = {
    "ms.channel.unauthorized": RemoteControlDenied,
    "ms.channel.timeOut": RemoteControlTimedOut,
}


async def fetch_power_state(session: aiohttp.ClientSession, host: str, port: int = API_PORT) -> str | None:
    """The display's PowerState, "on" or "standby", or None when it doesn't answer.

    A display that answers without reporting PowerState counts as on.
    """
    try:
        async with asyncio.timeout(STATE_TIMEOUT_SECONDS):
            device = await fetch_device_info(session, host, port)
    except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
        return None
    return device.get("PowerState", "on")


def parse_mac(mac: str | None) -> bytes | None:
    """The MAC's six bytes, or None when it's missing or a placeholder like "none"."""
    digits = re.sub(r"[:.-]", "", mac or "")
    return bytes.fromhex(digits) if MAC_DIGITS.fullmatch(digits) else None


def magic_packet(mac: bytes) -> bytes:
    return b"\xff" * 6 + mac * 16


def magic_packet_targets(host: str) -> list[tuple[str, int]]:
    """The subnet broadcast, when the host is an IPv4 address, and the display itself."""
    addresses = [host]
    with contextlib.suppress(ValueError):
        addresses.insert(0, str(ipaddress.IPv4Network(f"{host}/24", strict=False).broadcast_address))
    return [(address, port) for address in addresses for port in MAGIC_PACKET_PORTS]


def _send_datagrams(payload: bytes, targets: list[tuple[str, int]]) -> None:
    """Blocking, so it runs in the executor.

    A failed target doesn't stop the rest, since a display off the network can make its own
    address unroutable for a while.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _repeat in range(MAGIC_PACKET_REPEATS):
            for target in targets:
                try:
                    sender.sendto(payload, target)
                except OSError as error:
                    _LOGGER.debug("Magic packet to %s failed: %s", target, error)


def remote_control_url(host: str, port: int, token: str | None) -> str:
    url = f"wss://{host}:{port}/api/v2/channels/{REMOTE_CHANNEL}?name={ENCODED_CLIENT_NAME}"
    return f"{url}&token={token}" if token else url


def key_press(key: str) -> dict[str, Any]:
    params = {"Cmd": "Click", "DataOfCmd": key, "Option": "false", "TypeOfRemote": "SendRemoteKey"}
    return {"method": "ms.remote.control", "params": params}


@contextlib.asynccontextmanager
async def open_remote_control(
    session: aiohttp.ClientSession,
    host: str,
    token: str | None,
    accept_seconds: float,
    port: int = REMOTE_PORT,
) -> AsyncIterator[tuple[aiohttp.ClientWebSocketResponse, str | None]]:
    """Connect to the remote control channel and wait for the display to accept.

    Yields the socket and the token the display accepted with. The display's certificate is
    self-signed, so it isn't verified.
    """
    try:
        async with asyncio.timeout(CONNECT_SECONDS):
            websocket = await session.ws_connect(remote_control_url(host, port, token), ssl=False)
        async with websocket:
            yield websocket, await _wait_for_acceptance(websocket, accept_seconds)
    except (aiohttp.ClientError, OSError) as error:
        raise RemoteControlUnreachable(f"Couldn't reach the remote control channel on {host}: {error}") from error


async def _wait_for_acceptance(websocket: aiohttp.ClientWebSocketResponse, accept_seconds: float) -> str | None:
    """The token the display accepts with, raising if it refuses or stays silent."""
    try:
        async with asyncio.timeout(accept_seconds):
            async for message in websocket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    payload = json.loads(message.data)
                except ValueError:
                    continue
                event = payload.get("event")
                if event == "ms.channel.connect":
                    return payload.get("data", {}).get("token")
                if event in REFUSALS:
                    raise REFUSALS[event](f"The display sent {event}")
    except TimeoutError as error:
        raise RemoteControlTimedOut(f"The display didn't accept within {accept_seconds} seconds") from error
    raise RemoteControlUnreachable("The display closed the remote control connection")


async def pair(session: aiohttp.ClientSession, host: str, port: int = REMOTE_PORT) -> str:
    """Connect without a token so the display prompts to allow Home Assistant, and return the token it grants."""
    async with open_remote_control(session, host, None, PAIRING_SECONDS, port) as (_websocket, token):
        if not token:
            raise PowerControlError("The display accepted without granting a token")
        return token


class PowerController:
    """Tracks the display's power state over REST and, once it's paired, turns it on and off."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        token: str | None,
        mac: str | None = None,
        after_turn_on: Callable[[], None] | None = None,
        port: int = API_PORT,
        remote_port: int = REMOTE_PORT,
    ) -> None:
        self._session = session
        self._host = host
        self._mac = parse_mac(mac)
        self._after_turn_on = after_turn_on
        self._port = port
        self._remote_port = remote_port
        self._listeners: list[Callable[[], None]] = []
        self._command_lock = asyncio.Lock()
        self._last_burst_at: float | None = None
        # Readings overlap while polling and commands run, so a reading only counts if no
        # later one has finished first.
        self._readings_started = 0
        self._reading_applied = 0
        self.token = token
        self.state: str | None = None

    @property
    def is_on(self) -> bool:
        return self.state == "on"

    @property
    def paired(self) -> bool:
        return self.token is not None

    def _require_pairing(self) -> None:
        """Remote keys without a token would make the display prompt to allow Home Assistant."""
        if not self.paired:
            raise PowerControlError(f"{self._host} isn't paired for power control")

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def run(self) -> None:
        """Poll the power state until cancelled."""
        while True:
            await self.refresh()
            await asyncio.sleep(POLL_SECONDS)

    async def refresh(self) -> str | None:
        """Read the power state now, notifying listeners when it changes."""
        self._readings_started += 1
        reading = self._readings_started
        state = await fetch_power_state(self._session, self._host, self._port)
        if reading > self._reading_applied:
            self._reading_applied = reading
            if state != self.state:
                self.state = state
                self._notify()
        return state

    async def turn_on(self) -> None:
        """Wake the display from light or deep standby, then have the channel client reconnect.

        Each rung reads the state, nudges the display the way that state needs and waits for it
        to report on.
        """
        self._require_pairing()
        async with self._command_lock:
            for _rung in range(TURN_ON_RUNGS):
                state = await self.refresh()
                if state == "on":
                    break
                try:
                    await self._nudge(state)
                except RemoteControlUnreachable as error:
                    # The display can drop off the network between the reading and the key press.
                    _LOGGER.debug("KEY_POWER to %s failed: %s", self._host, error)
                    continue
                if await self._wait_for(lambda reading: reading == "on"):
                    break
            else:
                raise TurnOnFailed(f"{self._host} didn't turn on after {TURN_ON_RUNGS} attempts")
        if self._after_turn_on:
            self._after_turn_on()

    async def turn_off(self) -> None:
        """Press KEY_POWER if the display is on, then wait for it to report off.

        KEY_POWER toggles, so the on reading guards it. The Odyssey G8 ignores KEY_POWEROFF.
        """
        self._require_pairing()
        async with self._command_lock:
            if await self.refresh() != "on":
                return
            await self.send_key("KEY_POWER")
            await self._wait_for(lambda reading: reading != "on")

    async def _nudge(self, state: str | None) -> None:
        """Magic packets off the network, KEY_POWER in light standby unless a wake is landing."""
        if state is None:
            await self.send_magic_packets()
        elif self._burst_is_landing():
            _LOGGER.debug("Waiting for magic packets to wake %s rather than pressing KEY_POWER", self._host)
        else:
            await self.send_key("KEY_POWER")

    def _burst_is_landing(self) -> bool:
        return self._last_burst_at is not None and time.monotonic() - self._last_burst_at < BURST_LANDING_SECONDS

    async def _wait_for(self, reached: Callable[[str | None], bool]) -> bool:
        """Re-read the state every SETTLE_POLL_SECONDS until it's reached or SETTLE_SECONDS pass."""
        deadline = time.monotonic() + SETTLE_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(SETTLE_POLL_SECONDS)
            if reached(await self.refresh()):
                return True
        return False

    async def send_key(self, key: str) -> None:
        await self.send_keys([key])

    async def send_keys(self, keys: Sequence[str], delay_seconds: float = 0) -> None:
        """Press remote keys over one connection, delay_seconds apart, keeping any new token the display sends.

        An on display that has lost the token shows the Allow prompt, so the press waits as long as
        pairing does and keeps the token the display then grants.
        """
        self._require_pairing()
        accept_seconds = PAIRING_SECONDS if self.is_on else KEY_ACCEPT_SECONDS
        async with open_remote_control(
            self._session, self._host, self.token, accept_seconds, self._remote_port
        ) as (websocket, token):
            if token and token != self.token:
                _LOGGER.warning("%s granted a new remote control token", self._host)
                self.token = token
                self._notify()
            for index, key in enumerate(keys):
                if index:
                    await asyncio.sleep(delay_seconds)
                await websocket.send_json(key_press(key))

    async def send_magic_packets(self) -> None:
        """Wake the display from deep standby, when its MAC is known."""
        if self._mac is None:
            _LOGGER.debug("%s has no usable MAC, so it can't be woken from deep standby", self._host)
            return
        payload, targets = magic_packet(self._mac), magic_packet_targets(self._host)
        await asyncio.get_running_loop().run_in_executor(None, _send_datagrams, payload, targets)
        self._last_burst_at = time.monotonic()
