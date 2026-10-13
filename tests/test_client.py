"""Channel client tests against a fake Samsung multiscreen server and Tizen Input app.

The client has no Home Assistant imports, so these run without Home Assistant installed.
"""

import asyncio

import pytest
from aiohttp import web

from tizen_input import client as client_module

APP_CLIENT_ID = "app-1"
SOURCES = [
    {"type": "HDMI", "number": 2, "name": None, "connected": True},
    {"type": "DP", "number": 1, "name": "PC", "connected": True},
]


class FakeDisplay:
    """A channel server with the Tizen Input app as host. HDMI 2 reads back as null, like USB-C."""

    def __init__(self, app_running: bool = True) -> None:
        self.app_running = app_running
        self.answers_lists = True
        self.sources = SOURCES
        self.current = {"type": "DP", "number": 1, "signal": True}
        self.sockets: list[web.WebSocketResponse] = []
        self.received: list[dict] = []
        self.launches = 0

    def state(self):
        return None if self.current["type"] == "HDMI" else self.current

    async def broadcast(self, event, data, sender=APP_CLIENT_ID):
        for socket in self.sockets:
            await socket.send_json({"event": event, "data": data, "from": sender})

    async def channel(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.sockets.append(socket)
        clients = [{"id": "ha-1", "isHost": False}]
        if self.app_running:
            clients.insert(0, {"id": APP_CLIENT_ID, "isHost": True})
        await socket.send_json({"event": "ms.channel.connect", "data": {"id": "ha-1", "clients": clients}})
        async for message in socket:
            params = message.json()["params"]
            self.received.append(params)
            await self.app_receives(params["event"], params["data"])
        return socket

    async def app_receives(self, event, data):
        if not self.app_running:
            return
        if event == "source.list" and self.answers_lists:
            await self.broadcast("source.list", self.sources)
        elif event == "source.get":
            await self.broadcast("source.state", self.state())
        elif event == "source.set":
            self.current = {**data, "signal": True}
            await self.broadcast("source.state", self.state())

    async def launch(self, request):
        self.launches += 1
        self.app_running = True
        for socket in self.sockets:
            await socket.send_json({"event": "ms.channel.clientConnect", "data": {"id": APP_CLIENT_ID, "isHost": True}})
        return web.json_response(True)

    async def device(self, request):
        return web.json_response({"device": {"id": "uuid:test", "name": "Monitor", "OS": "Tizen"}})


async def start(display, port=0):
    app = web.Application()
    app.router.add_get("/api/v2/", display.device)
    app.router.add_get(f"/api/v2/channels/{client_module.CHANNEL}", display.channel)
    app.router.add_post(f"/api/v2/applications/{client_module.APP_ID}", display.launch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def settle(condition, seconds=2.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


def run_with_client(display, scenario, **client_options):
    async def main():
        runner, port = await start(display)
        async with client_module.aiohttp.ClientSession() as session:
            client = client_module.TizenInputClient(session, "127.0.0.1", port=port, **client_options)
            task = asyncio.create_task(client.run())
            try:
                await scenario(client, session, port)
            finally:
                task.cancel()
                await runner.cleanup()

    asyncio.run(main())


def test_reads_sources_and_state_from_the_app():
    async def scenario(client, session, port):
        await settle(lambda: client.sources and client.state_known)
        assert [source.label for source in client.sources] == ["HDMI 2", "PC"]
        assert client.current_source.label == "PC"
        device = await client_module.fetch_device_info(session, "127.0.0.1", port)
        assert device["id"] == "uuid:test"

    run_with_client(FakeDisplay(), scenario)


def test_ignores_state_from_other_clients():
    display = FakeDisplay()

    async def scenario(client, session, port):
        await settle(lambda: client.state_known)
        await display.broadcast("source.state", {"type": "HDMI", "number": 1}, sender="intruder")
        await display.broadcast("source.list", [], sender="intruder")
        await asyncio.sleep(0.1)
        assert client.current_source.label == "PC"
        assert len(client.sources) == 2

    run_with_client(display, scenario)


def test_learns_the_unreadable_source_after_selecting_it():
    display = FakeDisplay()

    async def scenario(client, session, port):
        await settle(lambda: client.sources and client.state_known)
        await client.set_source(client.sources[0])
        await settle(lambda: client.unreadable_source is not None)
        assert display.received[-1] == {"event": "source.set", "data": {"type": "HDMI", "number": 2}, "to": "host"}
        assert client.state is None
        assert client.current_source.label == "HDMI 2"

    run_with_client(display, scenario)


def test_launches_the_app_before_selecting_when_it_is_absent():
    display = FakeDisplay(app_running=False)

    async def scenario(client, session, port):
        await settle(lambda: client.connected)
        assert not client.app_present
        assert client.current_source is None
        await client.set_source(client.sources[1])
        await settle(lambda: client.current_source is not None)
        assert display.launches == 1
        assert client.current_source.label == "PC"

    run_with_client(display, scenario, sources=[{"type": "HDMI", "number": 2, "name": None}, {"type": "DP", "number": 1, "name": "PC"}])


def test_starts_the_app_only_when_it_is_absent():
    display = FakeDisplay(app_running=False)

    async def scenario(client, session, port):
        await settle(lambda: client.connected)
        assert await client.start_app() is True
        assert client.app_present
        assert await client.start_app() is False
        assert display.launches == 1

    run_with_client(display, scenario)


def test_refreshes_sources_on_request():
    display = FakeDisplay()

    async def scenario(client, session, port):
        await settle(lambda: client.sources and client.state_known)
        display.sources = [{**SOURCES[0], "connected": False}, SOURCES[1]]
        await client.refresh_sources(1)
        assert [source.connected for source in client.sources] == [False, True]

    run_with_client(display, scenario)


def test_keeps_the_last_sources_when_the_app_doesnt_reply():
    display = FakeDisplay()

    async def scenario(client, session, port):
        await settle(lambda: client.sources and client.state_known)
        display.answers_lists = False
        display.sources = []
        with pytest.raises(TimeoutError):
            await client.refresh_sources(0.1)
        assert [source.label for source in client.sources] == ["HDMI 2", "PC"]

    run_with_client(display, scenario)


def test_refreshing_sources_needs_the_app():
    async def scenario(client, session, port):
        await settle(lambda: client.connected)
        with pytest.raises(ConnectionError):
            await client.refresh_sources(1)
        with pytest.raises(TimeoutError):
            await client.wait_for_app(0.1)

    run_with_client(FakeDisplay(app_running=False), scenario)


def test_sources_are_known_only_once_the_running_app_lists_them():
    display = FakeDisplay(app_running=False)

    async def scenario(client, session, port):
        await settle(lambda: client.connected)
        assert len(client.sources) == 2
        assert not client.sources_known
        await client.start_app()
        await settle(lambda: client.sources_known)
        for socket in display.sockets:
            await socket.send_json({"event": "ms.channel.clientDisconnect", "data": {"id": APP_CLIENT_ID}})
        await settle(lambda: not client.sources_known)

    run_with_client(display, scenario, sources=SOURCES)


def test_reconnects_at_once_when_asked(unused_port):
    async def main():
        async with client_module.aiohttp.ClientSession() as session:
            client = client_module.TizenInputClient(session, "127.0.0.1", port=unused_port)
            task = asyncio.create_task(client.run())
            # Nothing listens yet, so the first attempt fails and the client waits to retry.
            await asyncio.sleep(0.2)
            runner, _port = await start(FakeDisplay(), unused_port)
            try:
                client.reconnect_now()
                await settle(lambda: client.connected, seconds=client_module.RECONNECT_SECONDS / 2)
            finally:
                task.cancel()
                await runner.cleanup()

    asyncio.run(main())
