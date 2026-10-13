"""Power controller tests against a fake display's REST API, remote control channel and magic packet listener.

The controller has no Home Assistant imports, so these run without Home Assistant installed.
"""

import asyncio
import ssl
import subprocess
import time

import aiohttp
import pytest
from aiohttp import web

from tizen_input import power as power_module

TOKEN = "11111111"
DISPLAY_MAC = "a0:d0:5b:12:34:56"
WAKE_PAYLOAD = bytes.fromhex("ff" * 6 + "a0d05b123456" * 16)
KEY_EFFECTS = {
    "KEY_POWER": lambda state: "standby" if state == "on" else "on",
}
# What the display sends a client without a valid token, by how its Allow prompt is answered.
PROMPT_EVENTS = {"deny": "ms.channel.unauthorized", "time_out": "ms.channel.timeOut"}


class FakeDisplay(asyncio.DatagramProtocol):
    """A display that answers REST and remote keys unless it's off the network, where power_state is None.

    Magic packets bring it back as wakes_to, or never when that's None.
    """

    def __init__(self, power_state="standby", prompt_answer="allow", wakes_to="on", reports_power_state=True):
        self.power_state = power_state
        self.prompt_answer = prompt_answer
        self.wakes_to = wakes_to
        self.reports_power_state = reports_power_state
        self.accepted_tokens = {TOKEN}
        self.issued_token = TOKEN
        # How long the user takes to select Allow on a prompt.
        self.allow_delay = 0
        self.answer_delays = []
        self.client_names = []
        self.messages = []
        self.magic_packets = 0

    @property
    def keys(self):
        return [message["params"]["DataOfCmd"] for message in self.messages]

    async def device(self, request):
        power_state = self.power_state
        if self.answer_delays:
            await asyncio.sleep(self.answer_delays.pop(0))
        if power_state is None:
            raise web.HTTPServiceUnavailable()
        device = {"id": "uuid:test", "OS": "Tizen"}
        if self.reports_power_state:
            device["PowerState"] = power_state
        return web.json_response({"device": device})

    async def remote_control(self, request):
        if self.power_state is None:
            raise web.HTTPServiceUnavailable()
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.client_names.append(request.query.get("name"))
        prompted = request.query.get("token") not in self.accepted_tokens
        answer = self.prompt_answer if prompted else "allow"
        if answer == "allow":
            if prompted:
                await asyncio.sleep(self.allow_delay)
            self.accepted_tokens.add(self.issued_token)
            await socket.send_json({"event": "ms.channel.connect", "data": {"token": self.issued_token, "clients": []}})
        elif answer in PROMPT_EVENTS:
            await socket.send_json({"event": PROMPT_EVENTS[answer], "data": {}})
        async for message in socket:
            self.messages.append(message.json())
            if effect := KEY_EFFECTS.get(self.keys[-1]):
                self.power_state = effect(self.power_state)
        return socket

    def datagram_received(self, data, sender):
        if data == WAKE_PAYLOAD:
            self.magic_packets += 1
            if self.power_state is None:
                self.power_state = self.wakes_to


@pytest.fixture(scope="module")
def display_tls(tmp_path_factory):
    """A self-signed certificate like the display's, which the controller mustn't verify."""
    directory = tmp_path_factory.mktemp("tls")
    certificate, key = directory / "certificate.pem", directory / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=display", "-days", "1"]
        + ["-keyout", str(key), "-out", str(certificate)],
        check=True,
        capture_output=True,
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certificate, key)
    return context


@pytest.fixture
def run_with_controller(monkeypatch, display_tls):
    """Run a scenario against a fake display, with each turn on rung shortened to a fraction of a second."""
    monkeypatch.setattr(power_module, "SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(power_module, "SETTLE_POLL_SECONDS", 0.05)

    def run(display, scenario, **controller_options):
        async def main():
            app = web.Application()
            app.router.add_get("/api/v2/", display.device)
            app.router.add_get(f"/api/v2/channels/{power_module.REMOTE_CHANNEL}", display.remote_control)
            runner = web.AppRunner(app)
            await runner.setup()
            api = web.TCPSite(runner, "127.0.0.1", 0)
            remote = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=display_tls)
            await api.start()
            await remote.start()
            wake, _protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: display, local_addr=("127.0.0.1", 0)
            )
            monkeypatch.setattr(power_module, "MAGIC_PACKET_PORTS", (wake.get_extra_info("sockname")[1],))
            remote_port = remote._server.sockets[0].getsockname()[1]
            async with aiohttp.ClientSession() as session:
                controller = power_module.PowerController(
                    session,
                    "127.0.0.1",
                    **{"token": TOKEN, "mac": DISPLAY_MAC, **controller_options},
                    port=api._server.sockets[0].getsockname()[1],
                    remote_port=remote_port,
                )
                try:
                    await scenario(controller, session, remote_port)
                finally:
                    wake.close()
                    await runner.cleanup()

        asyncio.run(main())

    return run


def test_reads_the_power_state(run_with_controller, unused_port):
    display = FakeDisplay(power_state="on")

    async def scenario(controller, session, remote_port):
        changes = []
        controller.add_listener(lambda: changes.append(controller.state))
        assert await controller.refresh() == "on"
        assert await controller.refresh() == "on"
        display.power_state = "standby"
        assert await controller.refresh() == "standby"
        display.power_state = None
        assert await controller.refresh() is None
        assert changes == ["on", "standby", None]
        assert await power_module.fetch_power_state(session, "127.0.0.1", unused_port) is None

    run_with_controller(display, scenario)


def test_a_slow_reading_doesnt_overwrite_a_newer_one(run_with_controller):
    display = FakeDisplay(power_state=None)
    display.answer_delays = [0.3]

    async def scenario(controller, session, remote_port):
        slow_reading = asyncio.create_task(controller.refresh())
        await asyncio.sleep(0.1)
        display.power_state = "on"
        assert await controller.refresh() == "on"
        assert await slow_reading is None
        assert controller.state == "on"

    run_with_controller(display, scenario)


def test_a_display_without_power_state_counts_as_on_while_it_answers(run_with_controller):
    async def scenario(controller, session, remote_port):
        assert await controller.refresh() == "on"

    run_with_controller(FakeDisplay(reports_power_state=False), scenario)


def test_turn_on_presses_power_in_light_standby(run_with_controller):
    display = FakeDisplay(power_state="standby")
    reconnects = []

    async def scenario(controller, session, remote_port):
        await controller.turn_on()
        assert controller.is_on
        assert display.magic_packets == 0
        assert display.client_names == ["SG9tZSBBc3Npc3RhbnQ="]
        assert display.messages == [
            {
                "method": "ms.remote.control",
                "params": {
                    "Cmd": "Click",
                    "DataOfCmd": "KEY_POWER",
                    "Option": "false",
                    "TypeOfRemote": "SendRemoteKey",
                },
            }
        ]
        assert reconnects == [True]

    run_with_controller(display, scenario, after_turn_on=lambda: reconnects.append(True))


def test_turn_on_sends_magic_packets_off_the_network(run_with_controller):
    display = FakeDisplay(power_state=None)

    async def scenario(controller, session, remote_port):
        await controller.turn_on()
        assert controller.is_on
        assert display.keys == []
        # One burst. The listener only hears the packets sent to the display's own address.
        assert display.magic_packets == power_module.MAGIC_PACKET_REPEATS

    run_with_controller(display, scenario)


def test_turn_on_waits_rather_than_pressing_power_while_magic_packets_land(run_with_controller):
    display = FakeDisplay(power_state=None, wakes_to="standby")

    async def scenario(controller, session, remote_port):
        with pytest.raises(power_module.TurnOnFailed):
            await controller.turn_on()
        assert display.keys == []
        assert display.magic_packets == power_module.MAGIC_PACKET_REPEATS

    run_with_controller(display, scenario)


def test_turn_on_presses_power_once_magic_packets_had_time_to_land(run_with_controller, monkeypatch):
    monkeypatch.setattr(power_module, "BURST_LANDING_SECONDS", 0.2)
    display = FakeDisplay(power_state=None, wakes_to="standby")

    async def scenario(controller, session, remote_port):
        await controller.turn_on()
        assert controller.is_on
        assert display.keys == ["KEY_POWER"]

    run_with_controller(display, scenario)


def test_turn_on_gives_up_after_three_rungs(run_with_controller):
    display = FakeDisplay(power_state=None, wakes_to=None)
    reconnects = []

    async def scenario(controller, session, remote_port):
        with pytest.raises(power_module.TurnOnFailed):
            await controller.turn_on()
        assert display.magic_packets == 3 * power_module.MAGIC_PACKET_REPEATS
        assert reconnects == []

    run_with_controller(display, scenario, after_turn_on=lambda: reconnects.append(True))


def test_turn_on_skips_magic_packets_without_a_real_mac(run_with_controller):
    display = FakeDisplay(power_state=None)

    async def scenario(controller, session, remote_port):
        with pytest.raises(power_module.TurnOnFailed):
            await controller.turn_on()
        assert display.magic_packets == 0

    run_with_controller(display, scenario, mac="none")


def test_turn_on_reports_a_rejected_token(run_with_controller):
    display = FakeDisplay(power_state="standby", prompt_answer="deny")
    display.accepted_tokens = set()

    async def scenario(controller, session, remote_port):
        with pytest.raises(power_module.RemoteControlDenied):
            await controller.turn_on()

    run_with_controller(display, scenario)


@pytest.mark.parametrize(
    ("power_state", "keys", "final_state"),
    [("on", ["KEY_POWER"], "standby"), ("standby", [], "standby"), (None, [], None)],
)
def test_turn_off_presses_power_only_when_on(run_with_controller, power_state, keys, final_state):
    display = FakeDisplay(power_state=power_state)

    async def scenario(controller, session, remote_port):
        await controller.turn_off()
        assert display.keys == keys
        assert controller.state == final_state

    run_with_controller(display, scenario)


def test_an_unpaired_display_reports_power_but_isnt_turned_on_or_off(run_with_controller):
    display = FakeDisplay(power_state=None)

    async def scenario(controller, session, remote_port):
        assert not controller.paired
        assert await controller.refresh() is None
        for command in (controller.turn_on, controller.turn_off, lambda: controller.send_keys(["KEY_INFO"])):
            with pytest.raises(power_module.PowerControlError, match="isn't paired"):
                await command()
        assert display.client_names == []
        assert display.magic_packets == 0

    run_with_controller(display, scenario, token=None)


def test_sends_keys_over_one_connection_with_a_delay_between(run_with_controller):
    display = FakeDisplay(power_state="on")

    async def scenario(controller, session, remote_port):
        started = time.monotonic()
        await controller.send_keys(["KEY_INFO", "KEY_FACTORY"], delay_seconds=0.2)
        assert time.monotonic() - started >= 0.2
        deadline = time.monotonic() + 2
        while display.keys != ["KEY_INFO", "KEY_FACTORY"]:
            assert time.monotonic() < deadline, display.keys
            await asyncio.sleep(0.01)
        assert len(display.client_names) == 1
        assert display.power_state == "on"

    run_with_controller(display, scenario)


def test_keeps_the_latest_token(run_with_controller):
    display = FakeDisplay(power_state="standby")
    display.issued_token = "22222222"

    async def scenario(controller, session, remote_port):
        tokens = []
        controller.add_listener(lambda: tokens.append(controller.token))
        await controller.send_key("KEY_INFO")
        assert tokens == ["22222222"]

    run_with_controller(display, scenario)


def test_a_key_press_waits_for_allow_on_an_on_display_that_lost_the_token(run_with_controller, monkeypatch):
    monkeypatch.setattr(power_module, "KEY_ACCEPT_SECONDS", 0.1)
    display = FakeDisplay(power_state="on")
    display.accepted_tokens = set()
    display.issued_token = "22222222"
    display.allow_delay = 0.3

    async def scenario(controller, session, remote_port):
        await controller.refresh()
        await controller.send_key("KEY_INFO")
        assert controller.token == "22222222"
        assert display.keys == ["KEY_INFO"]

    run_with_controller(display, scenario)


def test_a_key_press_doesnt_wait_for_allow_while_the_display_is_off(run_with_controller, monkeypatch):
    monkeypatch.setattr(power_module, "KEY_ACCEPT_SECONDS", 0.1)
    display = FakeDisplay(power_state="standby")
    display.accepted_tokens = set()
    display.allow_delay = 0.3

    async def scenario(controller, session, remote_port):
        await controller.refresh()
        with pytest.raises(power_module.RemoteControlTimedOut):
            await controller.send_key("KEY_INFO")
        assert controller.token == TOKEN

    run_with_controller(display, scenario)


def test_pairing_returns_the_granted_token(run_with_controller):
    display = FakeDisplay(power_state="on")
    display.issued_token = "33333333"

    async def scenario(controller, session, remote_port):
        assert await power_module.pair(session, "127.0.0.1", remote_port) == "33333333"

    run_with_controller(display, scenario)


@pytest.mark.parametrize(
    ("prompt_answer", "error"),
    [
        ("deny", power_module.RemoteControlDenied),
        ("time_out", power_module.RemoteControlTimedOut),
        ("ignore", power_module.RemoteControlTimedOut),
    ],
)
def test_pairing_fails_unless_the_prompt_is_accepted(run_with_controller, monkeypatch, prompt_answer, error):
    monkeypatch.setattr(power_module, "PAIRING_SECONDS", 0.2)
    display = FakeDisplay(power_state="on", prompt_answer=prompt_answer)

    async def scenario(controller, session, remote_port):
        with pytest.raises(error):
            await power_module.pair(session, "127.0.0.1", remote_port)

    run_with_controller(display, scenario)


def test_pairing_fails_when_the_display_is_unreachable(unused_port):
    async def main():
        async with aiohttp.ClientSession() as session:
            with pytest.raises(power_module.RemoteControlUnreachable):
                await power_module.pair(session, "127.0.0.1", unused_port)

    asyncio.run(main())


def test_magic_packet_payload_and_targets():
    assert power_module.magic_packet(power_module.parse_mac("A0-D0-5B-12-34-56")) == WAKE_PAYLOAD
    assert power_module.magic_packet_targets("192.168.1.20") == [
        ("192.168.1.255", 9),
        ("192.168.1.255", 7),
        ("192.168.1.20", 9),
        ("192.168.1.20", 7),
    ]
    assert power_module.magic_packet_targets("display.lan") == [("display.lan", 9), ("display.lan", 7)]


@pytest.mark.parametrize("mac", [None, "", "none", "a0:d0:5b:12:34"])
def test_ignores_missing_or_placeholder_macs(mac):
    assert power_module.parse_mac(mac) is None
