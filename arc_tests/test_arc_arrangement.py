"""Arrangement edits: clips, moves, and whole sections across every track."""
import fake_live


def _layout(rig, track):
    return [(c.name, c.start_time, c.end_time) for c in rig.track(track).arrangement_clips]


def test_arrangement_clips_are_listed_in_start_order(rig):
    reply = rig.ok("track/get/arrangement_clips", "Kick")
    assert reply[0] == "Kick"
    assert reply[1:6] == (0, "intro", 0.0, 32.0, False)
    assert reply[16:21] == (3, "drop 2", 96.0, 128.0, False)


def test_create_a_midi_clip(rig):
    assert rig.ok("track/create_midi_clip", "Kick", 128.0, 32.0) == (4, 128.0, 160.0)
    assert "overlaps clip 'drop 2'" in rig.error("track/create_midi_clip", "Kick", 120.0, 16.0)


def test_duplicate_a_clip(rig):
    assert rig.ok("track/duplicate_clip", "Bass", 1, 128.0) == (4, 128.0, 160.0)
    copy = rig.track("Bass").arrangement_clips[4]
    assert copy.name == "drop" and len(copy.all_notes()) == 32
    assert "overlaps" in rig.error("track/duplicate_clip", "Bass", 1, 0.0)


def test_move_a_clip_onto_part_of_its_own_span(rig):
    # start_time is read-only, so a move is duplicate + delete; the fake raises if that ever
    # duplicates onto a range a clip still covers, which is what a naive move would do here.
    notes = rig.track("Kick").arrangement_clips[3].all_notes()
    assert rig.ok("track/move_clip", "Kick", 3, 100.0) == (3, 100.0, 132.0)
    moved = rig.track("Kick").arrangement_clips[3]
    assert moved.name == "drop 2" and len(moved.all_notes()) == len(notes)
    assert _layout(rig, "Kick")[:3] == [("intro", 0.0, 32.0), ("drop", 32.0, 64.0), ("breakdown", 64.0, 96.0)]


def test_move_onto_another_clip_is_refused(rig):
    before = _layout(rig, "Kick")
    assert "overlaps clip 'breakdown'" in rig.error("track/move_clip", "Kick", 3, 80.0)
    assert _layout(rig, "Kick") == before


def test_delete_a_clip(rig):
    assert rig.ok("track/delete_clip", "Snare", 2) == ("breakdown", 3)


def test_delete_time_removes_a_section_and_closes_the_gap(rig):
    # The calibration's "delete the breakdown" row.
    deleted, moved, cues_after = rig.ok("arrangement/delete_time", 64.0, 32.0)
    assert (deleted, moved) == (3, 3)                       # breakdown and drop 2 on Kick, Snare, Bass
    for track in ("Kick", "Snare", "Bass"):
        assert _layout(rig, track) == [("intro", 0.0, 32.0), ("drop", 32.0, 64.0), ("drop 2", 64.0, 96.0)]
    assert _layout(rig, "Lead") == [("hook", 32.0, 64.0)]  # nothing of the lead was in or after it
    assert cues_after == 1                                  # "drop 2" at 96: Live cannot move cue points


def test_delete_time_moves_later_clips_earliest_first(rig):
    # Two clips follow the cut. Moving the later one first would land it on the earlier one, which
    # is still in place; the fake raises on that overlap, as real Live would silently overwrite.
    assert rig.ok("arrangement/delete_time", 32.0, 32.0)[:2] == (4, 6)   # the drop on 3 tracks + the hook
    assert _layout(rig, "Kick") == [("intro", 0.0, 32.0), ("breakdown", 32.0, 64.0), ("drop 2", 64.0, 96.0)]
    assert _layout(rig, "Lead") == []                       # the hook sat inside the cut


def test_delete_time_refuses_a_clip_crossing_the_boundary_and_changes_nothing(rig):
    before = {t: _layout(rig, t) for t in ("Kick", "Snare", "Bass", "Lead")}
    message = rig.error("arrangement/delete_time", 48.0, 32.0)
    assert "crosses 48" in message and "never cuts a clip" in message
    assert {t: _layout(rig, t) for t in before} == before


def test_delete_time_refuses_when_arrangement_automation_would_not_follow(rig):
    rig.track("Bass").devices[1].parameters[0].automation_state = 1
    message = rig.error("arrangement/delete_time", 64.0, 32.0)
    assert "Bass/Compressor/Threshold" in message and "allow_automation" in message
    assert rig.ok("arrangement/delete_time", 64.0, 32.0, True)[:2] == (3, 3)


def test_insert_copy_repeats_a_section_and_pushes_the_rest_later(rig):
    # The calibration's "duplicate the drop" row.
    copied, moved, _ = rig.ok("arrangement/insert_copy", 32.0, 32.0)
    assert (copied, moved) == (4, 6)
    for track in ("Kick", "Snare", "Bass"):
        assert _layout(rig, track) == [("intro", 0.0, 32.0), ("drop", 32.0, 64.0), ("drop", 64.0, 96.0),
                                       ("breakdown", 96.0, 128.0), ("drop 2", 128.0, 160.0)]
    assert _layout(rig, "Lead") == [("hook", 32.0, 64.0), ("hook", 64.0, 96.0)]


def test_section_edits_refuse_a_non_positive_length(rig):
    assert "length must be positive" in rig.error("arrangement/delete_time", 64.0, 0.0)
