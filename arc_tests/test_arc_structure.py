"""The set snapshot the rig reads before editing and matches against to restore its baseline."""
import hashlib
import json


def fetch(rig, chunk_chars=None):
    reply = rig.ok("get/structure", *([chunk_chars] if chunk_chars else []))
    request_id, count, total, digest = reply
    text = "".join(rig.ok("get/structure_chunk", request_id, i)[2] for i in range(count))
    assert len(text) == total and hashlib.sha1(text.encode("ascii")).hexdigest() == digest
    return json.loads(text), request_id, count


def test_the_snapshot_covers_tracks_returns_and_master(rig):
    snapshot, _, _ = fetch(rig)
    kinds = [(t["kind"], t["name"]) for t in snapshot["tracks"]]
    assert kinds == [("track", "Drums"), ("track", "Kick"), ("track", "Snare"), ("track", "Bass"),
                     ("track", "Lead"), ("return", "A-Reverb"), ("master", "Master")]
    assert snapshot["tempo"] == 124.0 and snapshot["live"] == [12, 3, 1] and snapshot["arcApi"] == 1
    assert [c["name"] for c in snapshot["cuePoints"]] == ["intro", "drop", "breakdown", "drop 2"]


def test_the_snapshot_carries_devices_parameters_and_clips(rig):
    snapshot, _, _ = fetch(rig)
    master = snapshot["tracks"][-1]
    capture = next(d for d in master["devices"] if d["name"] == "ARC Capture")
    assert [p["name"] for p in capture["parameters"]] == ["ARC Remote Command", "ARC Remote State"]
    assert capture["parameters"][0]["display"] == "none"
    kick = snapshot["tracks"][1]
    assert kick["group"] == "Drums" and snapshot["tracks"][0]["isGroup"] is True
    assert kick["devices"][0]["chains"][0]["name"] == "Kick"
    clip = kick["arrangementClips"][0]
    assert (clip["name"], clip["start"], clip["end"], clip["notes"]["count"]) == ("intro", 0.0, 32.0, 32)
    assert kick["mixer"][0]["name"] == "Track Volume"


def test_a_note_edit_changes_the_digest_and_undoing_it_restores_it(rig):
    # How the rig proves it is back at its baseline: the snapshot matches, not a count of undos.
    baseline, _, _ = fetch(rig)
    rig.ok("clip/shift_notes", "Snare", 0, 12, "ms")
    edited, _, _ = fetch(rig)
    rig.ok("clip/shift_notes", "Snare", 0, -12, "ms")
    restored, _, _ = fetch(rig)
    digest = lambda s: s["tracks"][2]["arrangementClips"][0]["notes"]["digest"]
    assert digest(edited) != digest(baseline)
    assert digest(restored) == digest(baseline)


def test_small_chunks_reassemble(rig):
    whole, _, _ = fetch(rig)
    chunked, _, count = fetch(rig, 1000)
    assert count > 1 and chunked == whole


def test_only_the_latest_snapshot_is_cached(rig):
    _, first, _ = fetch(rig)
    fetch(rig)
    assert "no longer cached" in rig.error("get/structure_chunk", first, 0)


def test_chunk_requests_are_checked(rig):
    _, request_id, count = fetch(rig)
    assert "chunk(s)" in rig.error("get/structure_chunk", request_id, count)
    assert "between 1000 and 60000" in rig.error("get/structure", 10)
