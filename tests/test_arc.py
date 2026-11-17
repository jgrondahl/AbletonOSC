"""
The arc handlers against a RUNNING Live (upstream's test style: a real Live with this fork selected
as a Control Surface, queried over OSC on localhost). It answers what arc_tests/ cannot - whether
real Live behaves the way the simulated Live there assumes:

  * Track.insert_device exists and takes (device_name, index)             (Live 12.3+)
  * Track.create_midi_clip / duplicate_clip_to_arrangement / delete_clip   (arrangement edits)
  * add_new_notes returns note ids; apply_note_modifications accepts the vector
    get_notes_extended returned, with its notes edited in place            (every note edit)
  * Clip.create_automation_envelope + insert_step                          (unverified by the docs)
  * parameter displays parse ("1.20 kHz", "-6.0 dB")                        (set by display)
  * a clip envelope leaves automation_state at 0                           (section edits)

It builds its own MIDI track named TRACK, edits only that, and deletes it at the end. It does not
touch anything else in the open set.

    python -m pytest tests/test_arc.py      # on the Mac, with Live 12.3+ running
"""
from . import client, wait_one_tick
import json
import pytest

TRACK = "arc-live-test"
SLOW = 2.0          # seconds: building a structure snapshot takes longer than one tick


def arc(client, path, *params, timeout=SLOW):
    reply = client.query("/live/arc/" + path, params, timeout=timeout)
    assert reply[0] == "ok", reply
    return reply[1:]


def arc_error(client, path, *params):
    reply = client.query("/live/arc/" + path, params, timeout=SLOW)
    assert reply[0] == "error", reply
    return reply[1]


@pytest.fixture(scope="module", autouse=True)
def scratch_track(client):
    client.send_message("/live/song/create_midi_track", (-1,))
    wait_one_tick()
    count = client.query("/live/song/get/num_tracks")[0]
    client.send_message("/live/track/set/name", (count - 1, TRACK))
    wait_one_tick()
    yield
    names = client.query("/live/song/get/track_names", timeout=SLOW)
    if TRACK in names:
        client.send_message("/live/song/delete_track", (names.index(TRACK),))
        wait_one_tick()


def test_version_is_12_3_or_later(client):
    api, major, minor, bugfix, has_insert_device = arc(client, "get/version")
    assert api == 1 and (major, minor) >= (12, 3) and has_insert_device


def test_arrangement_clip_and_notes(client):
    assert arc(client, "track/create_midi_clip", TRACK, 0.0, 16.0)[0] == 0
    added = arc(client, "clip/add/notes", TRACK, 0, 36, 0.0, 0.25, 100, False, 38, 1.0, 0.25, 100, False)
    assert added[0] == 2 and len(added) == 3, "add_new_notes did not return note ids"

    count, delta = arc(client, "clip/shift_notes", TRACK, 0, 12.0, "ms", 38, 1, -8192.0, 16384.0)
    tempo = client.query("/live/song/get/tempo")[0]
    assert count == 1 and delta == pytest.approx(12 * tempo / 60000.0, rel=1e-4)
    notes = arc(client, "clip/get/notes", TRACK, 0)
    starts = {notes[2 + 6 * i]: notes[3 + 6 * i] for i in range(notes[0])}
    assert starts[36] == pytest.approx(0.0) and starts[38] == pytest.approx(1.0 + delta, abs=1e-5)

    assert arc(client, "clip/transpose_notes", TRACK, 0, 2, 36, 1, -8192.0, 16384.0) == (1,)
    assert arc(client, "clip/quantize", TRACK, 0, "1/16", 1.0) == ("1/16", 1.0)
    notes = arc(client, "clip/get/notes", TRACK, 0)
    assert sorted(notes[2 + 6 * i] for i in range(notes[0])) == [38, 38]
    assert sorted(notes[3 + 6 * i] for i in range(notes[0])) == [pytest.approx(0.0), pytest.approx(1.0)]


def test_refusal_leaves_the_clip_alone(client):
    before = arc(client, "clip/get/notes", TRACK, 0)
    assert "outside the clip" in arc_error(client, "clip/shift_notes", TRACK, 0, -500.0, "ms")
    assert arc(client, "clip/get/notes", TRACK, 0) == before


def test_duplicate_move_and_delete(client):
    assert arc(client, "track/duplicate_clip", TRACK, 0, 32.0)[1:] == (32.0, 48.0)
    assert arc(client, "track/move_clip", TRACK, 1, 36.0)[1:] == (36.0, 52.0)   # overlaps its own span
    assert arc(client, "track/delete_clip", TRACK, 1)[1] == 1


def test_insert_device_and_set_parameters(client):
    devices = arc(client, "track/insert_device", TRACK, "EQ Eight")
    assert "EQ Eight" in devices[1:]
    _, shown, achieved = arc(client, "device/set/parameter_display", TRACK, "EQ Eight", "1 Frequency A", 1200.0)
    assert achieved == pytest.approx(1200.0, rel=0.01), shown
    assert arc(client, "device/set/parameter_item", TRACK, "EQ Eight", "1 Filter Type A", "Bell")[1] == "Bell"
    _, shown, achieved = arc(client, "device/set/parameter_display", TRACK, "@mixer", "Track Volume", -6.0)
    assert achieved == pytest.approx(-6.0, abs=0.1), shown


def test_clip_envelope(client):
    arc(client, "clip/envelope/insert_step", TRACK, 0, "@mixer", "Track Volume", 0.0, 8.0, 0.5)
    kind, value = arc(client, "clip/envelope/get", TRACK, 0, "@mixer", "Track Volume", 4.0)
    assert kind == "envelope" and value == pytest.approx(0.5, abs=1e-3)

    # The section edits refuse when automation_state is non-zero, on the reading that it flags
    # ARRANGEMENT automation only. If a clip envelope also sets it, they would refuse far too often.
    request_id, count, _, _ = arc(client, "get/structure", 8000)
    snapshot = json.loads("".join(arc(client, "get/structure_chunk", request_id, i)[2] for i in range(count)))
    track = next(t for t in snapshot["tracks"] if t["name"] == TRACK)
    assert track["mixer"][0]["name"] == "Track Volume"
    assert track["mixer"][0]["automation"] == 0, "a clip envelope set automation_state"

    arc(client, "clip/envelope/clear", TRACK, 0, "@mixer", "Track Volume")


def test_structure_snapshot(client):
    request_id, count, total, _ = arc(client, "get/structure", 8000)
    text = "".join(arc(client, "get/structure_chunk", request_id, i)[2] for i in range(count))
    assert len(text) == total and '"name":"%s"' % TRACK in text
