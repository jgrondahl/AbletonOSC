"""
Over the real wire: AbletonOSC's own OSCServer on a loopback UDP socket, messages built and parsed
with the bundled pythonosc. Catches what calling handlers directly cannot - an argument type that
does not survive OSC encoding, or a reply that never leaves.
"""
import json
import pathlib
import socket

import pytest

import fake_live
from conftest import FakeManager, REPO, arc

from AbletonOSC.abletonosc.osc_server import OSCServer
from AbletonOSC.pythonosc.osc_message import OscMessage
from AbletonOSC.pythonosc.osc_message_builder import OscMessageBuilder


@pytest.fixture
def wire():
    fake_live.build_set()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(2.0)
    server = OSCServer(local_addr=("127.0.0.1", 0), remote_addr=("127.0.0.1", client.getsockname()[1]))
    arc.ArcHandler(FakeManager(server))
    server_addr = server._socket.getsockname()

    def call(address, *params):
        builder = OscMessageBuilder(address)
        for param in params:
            builder.add_arg(param)
        client.sendto(builder.build().dgram, server_addr)
        server.process()
        data, _ = client.recvfrom(65536)
        message = OscMessage(data)
        assert message.address == address
        return tuple(message.params)

    yield call
    server.shutdown()
    client.close()


def test_version_over_the_wire(wire):
    assert wire("/live/arc/get/version") == ("ok", 1, 12, 3, 1, True)


def test_a_refusal_over_the_wire(wire):
    status, message = wire("/live/arc/track/get", "Hats")
    assert status == "error" and "no track named 'Hats'" in message


def test_float32_arguments_survive(wire):
    # OSC floats are 32-bit; a millisecond shift must still land within a hair of the intent.
    status, count, delta = wire("/live/arc/clip/shift_notes", "Snare", 0, 12.0, "ms")
    assert status == "ok" and count == 16 and delta == pytest.approx(12 * 124 / 60000.0, rel=1e-6)


def test_a_plugin_parameter_set_by_entry_name_over_the_wire(wire):
    status, value, shown = wire("/live/arc/device/set/parameter_item", "@master", "ARC Capture",
                                "ARC Remote Command", "arm")
    assert (status, shown) == ("ok", "arm")


def test_the_structure_reassembles_over_the_wire(wire):
    status, request_id, count, total, _ = wire("/live/arc/get/structure", 4000)
    assert status == "ok" and count > 1
    text = "".join(wire("/live/arc/get/structure_chunk", request_id, i)[3] for i in range(count))
    assert len(text) == total and json.loads(text)["tracks"][-1]["kind"] == "master"


def test_the_manager_registers_and_reloads_the_arc_handler():
    # Wiring that no handler test sees: without these lines Live loads the fork and arc never answers.
    manager = (REPO / "manager.py").read_text(encoding="utf-8")
    assert "abletonosc.ArcHandler(self)" in manager
    assert "importlib.reload(abletonosc.arc)" in manager
    init = (REPO / "abletonosc" / "__init__.py").read_text(encoding="utf-8")
    assert "from .arc import ArcHandler" in init
