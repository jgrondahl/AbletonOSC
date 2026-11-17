"""MIDI note edits: the calibration's pocket, transpose, density and quantize rows."""
import pytest

import fake_live
from fake_live import BASS_ROOT, KICK, SNARE


def _clip(rig, track, index):
    return rig.track(track).arrangement_clips[index]


def test_get_notes_returns_id_pitch_start_duration_velocity_mute(rig):
    reply = rig.ok("clip/get/notes", "Kick", 0)
    assert reply[0] == 32 and len(reply) == 1 + 32 * 6
    note_id, pitch, start, duration, velocity, mute = reply[1:7]
    assert (pitch, start, duration, velocity, mute) == (KICK, 0.0, 0.25, 110, False)


def test_get_notes_in_a_range_only(rig):
    assert rig.ok("clip/get/notes", "Kick", 0, 0, 128, 0.0, 4.0)[0] == 4


def test_a_session_clip_is_addressed_by_slot(rig):
    assert rig.ok("clip/get/notes", "Lead", "session:0")[0] == 1
    assert "no session clip in slot 3" in rig.error("clip/get/notes", "Lead", "session:3")


def test_a_missing_clip_index_is_refused(rig):
    assert "has 4 arrangement clip(s); there is no index 9" in rig.error("clip/get/notes", "Kick", 9)


def test_an_audio_clip_is_refused_for_note_edits(rig):
    _clip(rig, "Bass", 0).is_midi_clip = False
    assert "not a MIDI clip" in rig.error("clip/get/notes", "Bass", 0)


def test_too_many_notes_for_one_reply_is_refused(rig):
    clip = _clip(rig, "Kick", 0)
    clip.add_new_notes([fake_live.FakeNoteSpecification(60, i / 64.0, 0.01) for i in range(1500)])
    assert "narrow the range" in rig.error("clip/get/notes", "Kick", 0)


def test_add_notes_returns_their_ids(rig):
    reply = rig.ok("clip/add/notes", "Kick", 0, 42, 0.5, 0.25, 80, False, 42, 1.5, 0.25, 80, False)
    assert reply[0] == 2 and len(reply) == 3
    assert rig.ok("clip/get/notes", "Kick", 0, 42, 1, -8192.0, 16384.0)[0] == 2


def test_add_notes_refuses_an_unplayable_note_and_adds_none(rig):
    before = _clip(rig, "Kick", 0).all_notes()
    assert "not a playable note" in rig.error("clip/add/notes", "Kick", 0, 42, 0.5, 0.25, 80, False, 200, 1.0, 0.25, 80, False)
    assert _clip(rig, "Kick", 0).all_notes() == before


def test_remove_notes_in_a_range(rig):
    assert rig.ok("clip/remove/notes", "Kick", 0, 0, 128, 0.0, 8.0) == (8,)
    assert rig.ok("clip/get/notes", "Kick", 0)[0] == 24


def test_remove_notes_by_id_refuses_an_unknown_id_and_removes_nothing(rig):
    first_id = rig.ok("clip/get/notes", "Kick", 0)[1]
    assert "no such note id" in rig.error("clip/remove/notes_by_id", "Kick", 0, first_id, 999999)
    assert rig.ok("clip/get/notes", "Kick", 0)[0] == 32
    assert rig.ok("clip/remove/notes_by_id", "Kick", 0, first_id) == (1,)
    assert rig.ok("clip/get/notes", "Kick", 0)[0] == 31


def test_modify_notes_by_id(rig):
    note_id = rig.ok("clip/get/notes", "Kick", 0)[1]
    assert rig.ok("clip/modify/notes", "Kick", 0, note_id, 37, 0.5, 0.5, 64, True) == (1,)
    reply = rig.ok("clip/get/notes", "Kick", 0, 37, 1, -8192.0, 16384.0)
    assert reply[2:] == (37, 0.5, 0.5, 64, True)


def test_shift_by_milliseconds_converts_at_the_set_tempo(rig):
    # The calibration's pocket row: every snare note +12 ms. At 124 BPM that is 0.0248 beats.
    count, delta = rig.ok("clip/shift_notes", "Snare", 0, 12, "ms")
    assert count == 16 and delta == pytest.approx(12 * 124 / 60000.0)
    starts = [n[1] for n in _clip(rig, "Snare", 0).all_notes()]
    assert starts == [pytest.approx(b + delta) for b in range(1, 32, 2)]


def test_shift_follows_a_tempo_change(rig):
    rig.song.tempo = 118.0
    _, delta = rig.ok("clip/shift_notes", "Snare", 0, 12, "ms")
    assert delta == pytest.approx(12 * 118 / 60000.0)


def test_shift_one_pitch_leaves_the_others_where_they_were(rig):
    # Invariance at the edit: a snare-only shift on a clip holding kick and snare must not move a kick.
    clip = _clip(rig, "Kick", 0)
    clip.add_new_notes([fake_live.FakeNoteSpecification(SNARE, float(b), 0.25) for b in range(1, 32, 2)])
    kicks_before = [n for n in clip.all_notes() if n[0] == KICK]
    count, _ = rig.ok("clip/shift_notes", "Kick", 0, 25, "ms", SNARE, 1, -8192.0, 16384.0)
    assert count == 16
    assert [n for n in clip.all_notes() if n[0] == KICK] == kicks_before


def test_a_shift_that_would_push_a_note_out_of_the_clip_is_refused_whole(rig):
    clip = _clip(rig, "Kick", 0)
    before = clip.all_notes()
    message = rig.error("clip/shift_notes", "Kick", 0, -12, "ms")
    assert "outside the clip's 0..32" in message
    assert clip.all_notes() == before and clip.apply_calls == 0


def test_shift_refuses_an_unknown_unit(rig):
    assert "unit must be" in rig.error("clip/shift_notes", "Snare", 0, 1, "ticks")


def test_transpose_moves_every_note_by_semitones(rig):
    assert rig.ok("clip/transpose_notes", "Bass", 0, 3) == (32,)
    assert {n[0] for n in _clip(rig, "Bass", 0).all_notes()} == {BASS_ROOT + 3}


def test_transpose_past_the_midi_range_is_refused_whole(rig):
    before = _clip(rig, "Bass", 0).all_notes()
    assert "pitch 133" in rig.error("clip/transpose_notes", "Bass", 0, 100)
    assert _clip(rig, "Bass", 0).all_notes() == before


def test_quantize_to_a_named_grid(rig):
    clip = _clip(rig, "Snare", 0)
    rig.ok("clip/shift_notes", "Snare", 0, 25, "ms")
    assert rig.ok("clip/quantize", "Snare", 0, "1/16", 1.0) == ("1/16", 1.0)
    assert [n[1] for n in clip.all_notes()] == [pytest.approx(float(b)) for b in range(1, 32, 2)]


def test_quantize_refuses_an_unknown_grid_or_amount(rig):
    assert "grid must be one of" in rig.error("clip/quantize", "Snare", 0, "1/12", 1.0)
    assert "amount must be 0..1" in rig.error("clip/quantize", "Snare", 0, "1/16", 1.5)


def test_duplicate_region_copies_notes_inside_the_clip(rig):
    assert rig.ok("clip/duplicate_region", "Lead", 0, 0.0, 2.0, 4.0) == (4,)


def test_clip_properties(rig):
    assert rig.ok("clip/set", "Lead", 0, "muted", True) == ("muted", True)
    assert rig.ok("clip/get", "Lead", 0)[3] is True
    assert "only name, muted" in rig.error("clip/set", "Lead", 0, "start_time", 4.0)
