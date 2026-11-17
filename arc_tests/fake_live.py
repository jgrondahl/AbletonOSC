"""
A simulated Live Object Model: just enough of Live's Python API for arc.py's handlers to run
without Ableton Live.

It is strict where real Live is strict and where a mistake would hide in a lenient fake:
  * apply_note_modifications accepts only the vector get_notes_extended returned (Live's documented
    contract), not a list copied out of it;
  * get_notes_extended hands out COPIES, so an edit that is never applied never reaches the clip;
  * placing a clip over another raises, because real Live silently overwrites the overlap - an
    overlap arc.py must never cause;
  * parameters raise on an out-of-range value, as Live does.

What it cannot tell you is whether real Live behaves this way. arc_tests/README.md lists the
assumptions only a run inside Live can confirm, and tests/test_arc.py is that run.
"""
import itertools
import math
import types


class FakeParameter:
    def __init__(self, name, value=0.0, min=0.0, max=1.0, quantized=False, items=(), display=None,
                 automation_state=0, enabled=True):
        self.name = name
        self._value = value
        self.min = min
        self.max = max
        self.is_quantized = quantized
        self.value_items = tuple(items)
        self._display = display
        self.automation_state = automation_state
        self.is_enabled = enabled
        self.writes = []

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if not self.min - 1e-9 <= value <= self.max + 1e-9:
            raise RuntimeError("Invalid value")
        self._value = value
        self.writes.append(value)

    def str_for_value(self, value):
        if self._display is not None:
            return self._display(value)
        if self.value_items:
            return self.value_items[int(round(value - self.min))]
        return "%.2f" % value


# Displays shaped like Live's: monotonic, unit-bearing, with the unit switching mid-range.
def fader_display(value):
    if value <= 0:
        return "-inf dB"
    return "%.1f dB" % (6.0 + 40.0 * math.log10(value))


def frequency_display(value):
    hz = 20.0 * (1000.0 ** value)
    return "%.0f Hz" % hz if hz < 1000 else "%.2f kHz" % (hz / 1000.0)


def gain_display(value):
    return "%.1f dB" % (35.0 * value)


def time_display(value):
    ms = 1.0 + 2999.0 * value
    return "%.1f ms" % ms if ms < 1000 else "%.2f s" % (ms / 1000.0)


REMOTE_COMMANDS = ("none", "arm", "send", "export_locators", "disarm")


def plugin_choice_display(value):
    """A VST3 choice parameter as a host sees it: normalized 0..1, no value_items."""
    return REMOTE_COMMANDS[int(round(value * (len(REMOTE_COMMANDS) - 1)))]


class FakeChain:
    def __init__(self, name, devices=()):
        self.name = name
        self.devices = list(devices)


class FakeDevice:
    def __init__(self, name, class_name="", type=1, parameters=(), chains=()):
        self.name = name
        self.class_name = class_name or name.replace(" ", "")
        self.type = type
        self.parameters = list(parameters) or [FakeParameter("Device On", 1.0, 0.0, 1.0, quantized=True,
                                                             items=("Off", "On"))]
        self.chains = list(chains)
        self.can_have_chains = bool(chains)


class FakeMixer:
    def __init__(self, volume=0.85, sends=()):
        self.volume = FakeParameter("Track Volume", volume, 0.0, 1.0, display=fader_display)
        self.panning = FakeParameter("Track Panning", 0.0, -1.0, 1.0)
        self.sends = list(sends)


_note_ids = itertools.count(1000)


class FakeNote:
    def __init__(self, note_id, pitch, start_time, duration, velocity=100, mute=False):
        self.note_id = note_id
        self.pitch = pitch
        self.start_time = start_time
        self.duration = duration
        self.velocity = velocity
        self.mute = mute

    def copy(self):
        return FakeNote(self.note_id, self.pitch, self.start_time, self.duration, self.velocity, self.mute)


class FakeNoteVector(list):
    """What get_notes_extended returns. Live's apply_note_modifications takes only this."""


class FakeNoteSpecification:
    def __init__(self, pitch, start_time, duration, velocity=100, mute=False):
        self.pitch, self.start_time, self.duration = pitch, start_time, duration
        self.velocity, self.mute = velocity, mute


class FakeEnvelope:
    def __init__(self, default):
        self.default = default
        self.steps = []

    def insert_step(self, time, length, value):
        self.steps.append((time, length, value))

    def value_at_time(self, time):
        value = self.default
        for start, length, step_value in self.steps:
            if start <= time < start + length:
                value = step_value
        return value


GRID_STEP_BEATS = {1: 1.0, 2: 0.5, 3: 1.0 / 3, 5: 0.25, 6: 1.0 / 6, 8: 0.125}


class FakeClip:
    def __init__(self, name, start_time, length, midi=True, notes=(), looping=False):
        self.name = name
        self.start_time = start_time
        self.length = length
        self.is_midi_clip = midi
        self.muted = False
        self.gain = 0.4
        self.looping = looping
        self.loop_start = 0.0
        self.loop_end = length
        self.start_marker = 0.0
        self.end_marker = length
        self.pitch_coarse = 0
        self.color = 0
        self._notes = [FakeNote(next(_note_ids), *n) for n in notes]
        self.envelopes = {}
        self.apply_calls = 0

    @property
    def end_time(self):
        return self.start_time + self.length

    def _in(self, note, from_pitch, pitch_span, from_time, time_span):
        return (from_pitch <= note.pitch < from_pitch + pitch_span
                and from_time <= note.start_time < from_time + time_span)

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return FakeNoteVector(n.copy() for n in self._notes if self._in(n, from_pitch, pitch_span, from_time, time_span))

    def add_new_notes(self, specifications):
        ids = []
        for spec in specifications:
            note_id = next(_note_ids)
            self._notes.append(FakeNote(note_id, spec.pitch, spec.start_time, spec.duration, spec.velocity, spec.mute))
            ids.append(note_id)
        return tuple(ids)

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        self._notes = [n for n in self._notes if not self._in(n, from_pitch, pitch_span, from_time, time_span)]

    def remove_notes_by_id(self, ids):
        self._notes = [n for n in self._notes if n.note_id not in set(ids)]

    def apply_note_modifications(self, notes):
        if not isinstance(notes, FakeNoteVector):
            raise TypeError("apply_note_modifications takes the vector get_notes_extended returned")
        by_id = {n.note_id: n for n in notes}
        self._notes = [by_id[n.note_id].copy() if n.note_id in by_id else n for n in self._notes]
        self.apply_calls += 1

    def quantize(self, grid, amount):
        step = GRID_STEP_BEATS[grid]
        for note in self._notes:
            note.start_time += (round(note.start_time / step) * step - note.start_time) * amount

    def duplicate_region(self, region_start, region_length, destination_time, pitch, transposition_amount):
        offset = destination_time - region_start
        for note in list(self._notes):
            if region_start <= note.start_time < region_start + region_length and pitch in (-1, note.pitch):
                self._notes.append(FakeNote(next(_note_ids), note.pitch + transposition_amount,
                                            note.start_time + offset, note.duration, note.velocity, note.mute))

    def crop(self):
        self.start_marker, self.end_marker = self.loop_start, self.loop_end

    def automation_envelope(self, parameter):
        return self.envelopes.get(id(parameter))

    def create_automation_envelope(self, parameter):
        envelope = FakeEnvelope(parameter.value)
        self.envelopes[id(parameter)] = envelope
        return envelope

    def clear_envelope(self, parameter):
        self.envelopes.pop(id(parameter), None)

    def copy_at(self, start_time):
        copy = FakeClip(self.name, start_time, self.length, self.is_midi_clip, looping=self.looping)
        copy._notes = [FakeNote(next(_note_ids), n.pitch, n.start_time, n.duration, n.velocity, n.mute)
                       for n in self._notes]
        copy.muted, copy.gain = self.muted, self.gain
        copy.loop_start, copy.loop_end = self.loop_start, self.loop_end
        copy.start_marker, copy.end_marker = self.start_marker, self.end_marker
        return copy

    def all_notes(self):
        return sorted(((n.pitch, round(n.start_time, 9)) for n in self._notes))


class FakeClipSlot:
    def __init__(self, clip=None):
        self.clip = clip


class FakeTrackLegacy:
    """A track as Live before 12.3 has it: no insert_device."""

    def __init__(self, name, devices=(), clips=(), mixer=None, is_foldable=False, group_track=None, slots=()):
        self.name = name
        self.devices = list(devices)
        self._clips = []
        for clip in clips:
            self._place(clip)
        self.mixer_device = mixer or FakeMixer()
        self.is_foldable = is_foldable
        self.group_track = group_track
        self.mute = False
        self.solo = False
        self.clip_slots = [FakeClipSlot(c) for c in slots]

    @property
    def arrangement_clips(self):
        return tuple(sorted(self._clips, key=lambda c: c.start_time))

    def _place(self, clip):
        for other in self._clips:
            if other.start_time < clip.end_time - 1e-9 and clip.start_time < other.end_time - 1e-9:
                raise AssertionError("placed %r at %g..%g over %r at %g..%g on %r - real Live would "
                                     "silently overwrite it" % (clip.name, clip.start_time, clip.end_time,
                                                                 other.name, other.start_time, other.end_time,
                                                                 self.name))
        self._clips.append(clip)

    def create_midi_clip(self, start_time, length):
        clip = FakeClip("", start_time, length)
        self._place(clip)
        return clip

    def duplicate_clip_to_arrangement(self, clip, destination_time):
        copy = clip.copy_at(destination_time)
        self._place(copy)
        return copy

    def delete_clip(self, clip):
        self._clips.remove(clip)

    def delete_device(self, index):
        del self.devices[index]


class FakeTrack(FakeTrackLegacy):
    """A track as Live 12.3+ has it."""

    def insert_device(self, device_name, target_index):
        self.devices.insert(target_index, FakeDevice(device_name))


class FakeCuePoint:
    def __init__(self, name, time):
        self.name, self.time = name, time


class FakeView:
    def __init__(self):
        self.selected_track = None


class FakeSong:
    def __init__(self, tracks, return_tracks, master_track, tempo=124.0, cue_points=()):
        self.tracks = list(tracks)
        self.return_tracks = list(return_tracks)
        self.master_track = master_track
        self.tempo = tempo
        self.signature_numerator, self.signature_denominator = 4, 4
        self.loop, self.loop_start, self.loop_length = True, 0.0, 192.0
        self.song_length = 192.0
        self.cue_points = list(cue_points)
        self.view = FakeView()


class FakeBrowserItem:
    def __init__(self, name, children=(), loadable=False):
        self.name = name
        self.children = list(children)
        self.is_loadable = loadable


class FakeBrowser:
    def __init__(self, song_ref):
        self._song_ref = song_ref
        self.instruments = FakeBrowserItem("Instruments", [
            FakeBrowserItem("Operator", [FakeBrowserItem("Bass", [FakeBrowserItem("Sub Bass.adg", loadable=True)])]),
        ])
        self.audio_effects = FakeBrowserItem("Audio Effects", [FakeBrowserItem("Saturator", loadable=True)])
        self.loaded = []

    def load_item(self, item):
        track = self._song_ref().view.selected_track
        track.devices.append(FakeDevice(item.name.replace(".adg", "")))
        self.loaded.append((track.name, item.name))


class FakeApplication:
    def __init__(self, song_ref, version=(12, 3, 1)):
        self.version = version
        self.browser = FakeBrowser(song_ref)

    def get_major_version(self):
        return self.version[0]

    def get_minor_version(self):
        return self.version[1]

    def get_bugfix_version(self):
        return self.version[2]


class State:
    """The song and application the stubbed Live module and Component hand out."""
    song = None
    app = None


def make_live_module():
    live = types.ModuleType("Live")
    live.Clip = types.SimpleNamespace(MidiNoteSpecification=FakeNoteSpecification)
    live.Song = types.SimpleNamespace(RecordingQuantization=types.SimpleNamespace(
        rec_q_no_q=0, rec_q_quarter=1, rec_q_eight=2, rec_q_eight_triplet=3, rec_q_eight_eight_triplet=4,
        rec_q_sixtenth=5, rec_q_sixtenth_triplet=6, rec_q_sixtenth_sixtenth_triplet=7, rec_q_thirtysecond=8))
    live.Application = types.SimpleNamespace(get_application=lambda: State.app)
    return live


def make_component_module():
    module = types.ModuleType("ableton.v2.control_surface.component")

    class Component:
        def __init__(self, *args, **kwargs):
            pass

        @property
        def song(self):
            return State.song

    module.Component = Component
    return module


#--------------------------------------------------------------------------------
# The fixture set, shaped like the rig's arc-e2e.als: 124 BPM, sections of 32 beats.
#--------------------------------------------------------------------------------
KICK, SNARE, BASS_ROOT = 36, 38, 33
SECTIONS = (("intro", 0.0), ("drop", 32.0), ("breakdown", 64.0), ("drop 2", 96.0))


def four_on_the_floor(beats=32):
    return [(KICK, float(b), 0.25, 110) for b in range(beats)]


def backbeat(beats=32):
    return [(SNARE, float(b), 0.25, 100) for b in range(1, beats, 2)]


def bassline(beats=32):
    return [(BASS_ROOT, b + 0.5, 0.5, 96) for b in range(beats)]


def build_set(track_class=FakeTrack, version=(12, 3, 1)):
    eq = lambda name="EQ Eight": FakeDevice(name, "Eq8", 2, [
        FakeParameter("Device On", 1.0, 0.0, 1.0, quantized=True, items=("Off", "On")),
        FakeParameter("1 Frequency A", 0.3, 0.0, 1.0, display=frequency_display),
        FakeParameter("1 Gain A", 0.0, -1.0, 1.0, display=gain_display),
        FakeParameter("1 Filter Type A", 0.0, 0.0, 7.0, quantized=True,
                      items=("Low Cut 48", "Low Cut 12", "Low Shelf", "Bell", "Notch", "High Shelf",
                             "High Cut 12", "High Cut 48")),
    ])
    compressor = lambda: FakeDevice("Compressor", "Compressor2", 2, [
        FakeParameter("Threshold", 0.5, 0.0, 1.0, display=gain_display),
        FakeParameter("Release", 0.05, 0.0, 1.0, display=time_display),
    ])
    simpler = FakeDevice("Simpler", "OriginalSimpler", 1, [FakeParameter("Transpose", 0.0, -48.0, 48.0)])
    drum_rack = FakeDevice("Drum Rack", "DrumGroupDevice", 1, chains=[
        FakeChain("Kick", [simpler, eq()]),
        FakeChain("Snare", [FakeDevice("Simpler", "OriginalSimpler", 1, [FakeParameter("Transpose", 0.0, -48.0, 48.0)])]),
    ])

    drums = track_class("Drums", is_foldable=True)
    kick = track_class("Kick", [drum_rack, eq(), compressor()],
                       [FakeClip(name, start, 32.0, notes=four_on_the_floor()) for name, start in SECTIONS],
                       group_track=drums)
    snare = track_class("Snare", [eq()],
                        [FakeClip(name, start, 32.0, notes=backbeat()) for name, start in SECTIONS],
                        group_track=drums)
    bass = track_class("Bass", [eq(), compressor()],
                       [FakeClip(name, start, 32.0, notes=bassline()) for name, start in SECTIONS])
    lead = track_class("Lead", [eq(), eq()],                 # two "EQ Eight"s: ambiguous by design
                       [FakeClip("hook", 32.0, 32.0, notes=[(72, 0.0, 1.0, 90), (76, 1.0, 1.0, 90)])],
                       slots=[FakeClip("idea", 0.0, 4.0, notes=[(60, 0.0, 1.0, 90)])])
    reverb = track_class("A-Reverb", [FakeDevice("Reverb", "Reverb", 2)])
    master = track_class("Master", [
        FakeDevice("Utility", "StereoGain", 2, [
            FakeParameter("Gain", 0.0, -1.0, 1.0, display=gain_display),
            FakeParameter("Width", 0.5, 0.0, 1.0),
            FakeParameter("Bass Mono", 0.0, 0.0, 1.0, quantized=True, items=("Off", "On")),
        ]),
        eq(),
        FakeDevice("ARC Capture", "PluginDevice", 2, [
            FakeParameter("ARC Remote Command", 0.0, 0.0, 1.0, quantized=True, display=plugin_choice_display),
            FakeParameter("ARC Remote State", 0.0, 0.0, 1.0, quantized=True),
        ]),
    ])
    song = FakeSong([drums, kick, snare, bass, lead], [reverb], master,
                    cue_points=[FakeCuePoint(name, start) for name, start in SECTIONS])
    State.song = song
    State.app = FakeApplication(lambda: State.song, version)
    return song
