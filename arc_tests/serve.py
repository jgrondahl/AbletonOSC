"""
Serve the simulated Live over UDP, so the rig in AI-music-producer-coach can test its OSC client and
its edits against the REAL arc handlers without a Mac (that repo's
apps/web/tests/unit/daw-live-sim.test.ts starts this).

    python arc_tests/serve.py --port 11000 --reply-port 11001

It runs AbletonOSC's own OSCServer with the arc handlers over fake_live.build_set(), the set
arc_tests/ already tests against. Upstream's song handler is not loadable without Live, so this adds
the slice of it the rig uses: /live/song/get|set/<property> (a set sends no reply, as upstream's does
not), /live/song/start_playing and stop_playing. Prints "ready <port>" once listening, then serves
until stdin closes or it receives SIGTERM.
"""
import argparse
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fake_live  # noqa: E402
from conftest import FakeManager, arc  # noqa: E402  (installs the stubs and loads the package)

from AbletonOSC.abletonosc.osc_server import OSCServer  # noqa: E402

SONG_PROPERTIES = ("tempo", "loop", "loop_start", "loop_length", "current_song_time", "record_mode",
                   "session_record", "is_playing", "signature_numerator", "signature_denominator")


def add_song_slice(server, song):
    for prop in SONG_PROPERTIES:
        server.add_handler("/live/song/get/%s" % prop, lambda _params, p=prop: (getattr(song, p),))

        def setter(params, p=prop):
            setattr(song, p, params[0])
            return None
        server.add_handler("/live/song/set/%s" % prop, setter)
    server.add_handler("/live/song/start_playing", lambda _p: setattr(song, "is_playing", True))
    server.add_handler("/live/song/stop_playing", lambda _p: setattr(song, "is_playing", False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11000)
    parser.add_argument("--reply-port", type=int, default=11001)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    song = fake_live.build_set()
    for prop, value in (("current_song_time", 0.0), ("record_mode", 0), ("session_record", False), ("is_playing", False)):
        if not hasattr(song, prop):
            setattr(song, prop, value)

    server = OSCServer(local_addr=(args.host, args.port), remote_addr=(args.host, args.reply_port))
    arc.ArcHandler(FakeManager(server))
    add_song_slice(server, song)
    print("ready %d" % server._socket.getsockname()[1], flush=True)

    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    while not stop.is_set():
        server.process()
        time.sleep(0.005)
    server.shutdown()


if __name__ == "__main__":
    main()
