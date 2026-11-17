"""
Loads the real AbletonOSC package with Live's modules replaced by arc_tests/fake_live.py, so the
arc handlers run under plain pytest on any machine. No Ableton Live, no network.

    python -m pytest arc_tests

(tests/ is upstream's suite. It drives a RUNNING Live and connects at import, so run it only on the
machine with Live: see tests/test_arc.py for the arc part of it.)
"""
import importlib.util
import pathlib
import sys
import types

import pytest

import fake_live

REPO = pathlib.Path(__file__).resolve().parent.parent


def _install_stubs():
    sys.modules["Live"] = fake_live.make_live_module()
    for name in ("ableton", "ableton.v2", "ableton.v2.control_surface"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["ableton.v2.control_surface.component"] = fake_live.make_component_module()


def _load_package():
    """Import the repository as package "AbletonOSC", whatever its checkout directory is called."""
    if "AbletonOSC" in sys.modules:
        return sys.modules["AbletonOSC"]
    spec = importlib.util.spec_from_file_location("AbletonOSC", REPO / "__init__.py",
                                                  submodule_search_locations=[str(REPO)])
    package = importlib.util.module_from_spec(spec)
    sys.modules["AbletonOSC"] = package
    spec.loader.exec_module(package)        # manager.py fails to import without Live; __init__ allows that
    return package


_install_stubs()
_load_package()
arc = importlib.import_module("AbletonOSC.abletonosc.arc")


class FakeOSCServer:
    def __init__(self):
        self.handlers = {}
        self.sent = []

    def add_handler(self, address, handler):
        self.handlers[address] = handler

    def send(self, address, params=(), remote_addr=None):
        self.sent.append((address, tuple(params)))


class FakeManager:
    def __init__(self, osc_server):
        self.osc_server = osc_server


class Rig:
    """One fake set and an ArcHandler on it. rig("track/get", "Kick") calls /live/arc/track/get."""

    def __init__(self, track_class=fake_live.FakeTrack, version=(12, 3, 1)):
        self.song = fake_live.build_set(track_class, version)
        self.app = fake_live.State.app
        self.osc = FakeOSCServer()
        self.handler = arc.ArcHandler(FakeManager(self.osc))

    def __call__(self, path, *params):
        # Live hands every handler the one open song; a test may hold two rigs, so make this one current.
        fake_live.State.song, fake_live.State.app = self.song, self.app
        return self.osc.handlers[arc.PREFIX + path](params)

    def ok(self, path, *params):
        reply = self(path, *params)
        assert reply[0] == "ok", reply
        return reply[1:]

    def error(self, path, *params):
        reply = self(path, *params)
        assert reply[0] == "error", reply
        return reply[1]

    def track(self, name):
        if name == "@master":
            return self.song.master_track
        return next(t for t in self.song.tracks + self.song.return_tracks if t.name == name)


@pytest.fixture
def rig():
    return Rig()


@pytest.fixture
def legacy_rig():
    """Live before 12.3: no Track.insert_device."""
    return Rig(fake_live.FakeTrackLegacy, (12, 2, 5))
