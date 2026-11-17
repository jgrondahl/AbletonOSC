"""
arc.py - the Live actions the Audio Reference Coach (ARC) end-to-end rig needs and upstream
AbletonOSC does not have.

The rig lives in the AI-music-producer-coach repository (docs/ops/ableton-e2e-rig.md: section 3.2
lists the primitives, 7.4 this fork). It makes DAW edits of KNOWN size and checks that ARC's worker
measured them, so every edit here has to land exactly where it was aimed or refuse.

Everything lives under /live/arc/ so upstream addresses are untouched and syncing upstream never
conflicts. The conventions differ from upstream on purpose:

  * Targets are addressed by NAME, never by index. An index shifts the moment a device is inserted,
    and an edit aimed by a stale index lands on the wrong thing without any error.
      track      its name; "@master" for the master track; return tracks by name
      device     its name on that track; "EQ Eight#2" for the second of two with that name;
                 "Drum Rack>Kick>Simpler" to reach inside a rack (device>chain>device...);
                 "@mixer" for the track's mixer (Track Volume, Track Panning, sends)
      parameter  its name on that device
    A missing or ambiguous name is refused, and the refusal lists the names that do exist.
  * Arrangement clips are addressed by their index in track.arrangement_clips (start-time order),
    which the rig reads from /live/arc/get/structure first. "session:N" addresses clip slot N.
  * Every reply leads with "ok" or "error". A handler checks everything it can before its first
    change, so a refusal leaves the set as it was.
  * Times are in beats unless an argument says otherwise.

Pure Live Object Model calls only: no threads, no sockets of its own, nothing outside the set.
"""
import hashlib
import json
import math
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import Live

from .handler import AbletonOSCHandler

ARC_API_VERSION = 1
PREFIX = "/live/arc/"

# Note queries: every pitch, and the same time window upstream's clip handlers use.
ALL_PITCHES = (0, 128)
ALL_TIME = (-8192.0, 16384.0)
MAX_NOTES_PER_REPLY = 1500                 # 6 values per note must fit one UDP datagram
DEFAULT_CHUNK_CHARS = 16000                # structure JSON per reply (ASCII, so chars == bytes)
DISPLAY_SEARCH_STEPS = 60
ITEM_SCAN_STEPS = 1000

QUANTIZE_GRIDS = {
    "1/4": "rec_q_quarter",
    "1/8": "rec_q_eight",
    "1/8t": "rec_q_eight_triplet",
    "1/8+1/8t": "rec_q_eight_eight_triplet",
    "1/16": "rec_q_sixtenth",
    "1/16t": "rec_q_sixtenth_triplet",
    "1/16+1/16t": "rec_q_sixtenth_sixtenth_triplet",
    "1/32": "rec_q_thirtysecond",
}

BROWSER_CATEGORIES = ("instruments", "audio_effects", "midi_effects", "drums", "sounds", "samples",
                      "plugins", "packs", "user_library", "current_project", "max_for_live", "clips")

CLIP_PROPS_RW = ("name", "muted", "gain", "start_marker", "end_marker", "loop_start", "loop_end",
                 "looping", "pitch_coarse", "color")
TRACK_PROPS_RW = ("mute", "solo")

# Display units the parameter-by-display setter understands, scaled to a base unit
# (dB, Hz, ms, %). A display with any other unit is refused rather than misread.
UNIT_SCALE = {"": 1.0, "db": 1.0, "hz": 1.0, "khz": 1000.0, "ms": 1.0, "s": 1000.0, "%": 1.0}
_DISPLAY_RE = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([a-zA-Z%]*)")
_NTH_RE = re.compile(r"^(.*)#(\d+)$")


class ArcError(Exception):
    """A refusal: the request cannot be carried out as asked, and nothing was changed."""


#--------------------------------------------------------------------------------
# Argument helpers. OSC clients send numbers as int or float32 and booleans as T/F or 0/1.
#--------------------------------------------------------------------------------
def _num(value, what: str) -> float:
    if isinstance(value, bool):
        raise ArcError("%s must be a number, got %r" % (what, value))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ArcError("%s must be a number, got %r" % (what, value))
    if math.isnan(number) or math.isinf(number):
        raise ArcError("%s must be finite, got %r" % (what, value))
    return number


def _int(value, what: str) -> int:
    number = _num(value, what)
    if number != int(number):
        raise ArcError("%s must be a whole number, got %r" % (what, value))
    return int(number)


def _bool(value, what: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
        return value.lower() in ("true", "1")
    raise ArcError("%s must be true or false, got %r" % (what, value))


def _str(value, what: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ArcError("%s must be a non-empty name, got %r" % (what, value))
    return value


def _need(params: Tuple, count: int, usage: str) -> None:
    if len(params) < count:
        raise ArcError("expects %s" % usage)


def _safe(fn: Callable, default=None):
    """Read a Live property that may raise on objects it does not apply to."""
    try:
        return fn()
    except Exception:
        return default


def parse_display(text: str) -> float:
    """The number in a parameter display ("-6.0 dB", "1.20 kHz", "450 ms"), in base units."""
    cleaned = (text or "").strip().replace("−", "-")
    if cleaned.lower().startswith("-inf"):
        return -math.inf
    match = _DISPLAY_RE.match(cleaned)
    if match is None:
        raise ArcError("cannot read a number from the display %r" % text)
    unit = match.group(2).lower()
    if unit not in UNIT_SCALE:
        raise ArcError("the display %r has a unit (%r) this setter does not convert" % (text, match.group(2)))
    return float(match.group(1)) * UNIT_SCALE[unit]


def _names(items) -> str:
    names = [getattr(item, "name", "?") for item in items]
    shown = ", ".join(repr(n) for n in names[:24])
    return shown + (", ..." if len(names) > 24 else "") if names else "none"


def pick_by_name(items, name: str, what: str, where: str):
    """
    The one item called `name`, or the nth with "Name#n" (1-based). An exact match on the full
    string wins first, so a device literally named "Bus#2" is still reachable.
    """
    items = list(items)
    exact = [item for item in items if item.name == name]
    if len(exact) == 1:
        return exact[0]
    base, nth = name, None
    nth_match = _NTH_RE.match(name)
    if not exact and nth_match:
        base, nth = nth_match.group(1), int(nth_match.group(2))
    matches = exact if exact else [item for item in items if item.name == base]
    if nth is None:
        if not matches:
            raise ArcError("no %s named %r on %s (there: %s)" % (what, name, where, _names(items)))
        raise ArcError("%d %ss are named %r on %s - say which one: %s#1 .. %s#%d"
                       % (len(matches), what, base, where, base, base, len(matches)))
    if 1 <= nth <= len(matches):
        return matches[nth - 1]
    raise ArcError("%s %r has %d match(es) on %s, not %d" % (what, base, len(matches), where, nth))


def note_digest(notes) -> str:
    """A short fingerprint of a clip's notes, so a snapshot can tell an edited clip from the baseline."""
    rows = sorted((n.pitch, round(n.start_time, 6), round(n.duration, 6), round(float(n.velocity), 3),
                   bool(n.mute)) for n in notes)
    return hashlib.sha1(repr(rows).encode("ascii")).hexdigest()[:16]


class _Mixer:
    """A track's mixer, shaped like a device so parameters resolve the same way ("@mixer")."""

    def __init__(self, track):
        mixer = track.mixer_device
        params = []
        for attr in ("volume", "panning"):
            parameter = _safe(lambda: getattr(mixer, attr))
            if parameter is not None:
                params.append(parameter)
        params.extend(_safe(lambda: list(mixer.sends), []))
        self.name = "@mixer"
        self.class_name = "MixerDevice"
        self.parameters = params
        self.chains = ()


class ArcHandler(AbletonOSCHandler):
    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "arc"

    #--------------------------------------------------------------------------------
    # Registration. Each route: (address under /live/arc/, function(params) -> tuple or None).
    #--------------------------------------------------------------------------------
    def init_api(self):
        self._structure_cache = None             # (request_id, [chunk, ...]) - the latest only
        self._structure_seq = 0
        routes = [
            ("get/version", self._get_version),
            ("get/structure", self._get_structure),
            ("get/structure_chunk", self._get_structure_chunk),

            ("track/get", self._track_get),
            ("track/set", self._track_set),
            ("track/get/arrangement_clips", self._track_arrangement_clips),
            ("track/create_midi_clip", self._track_create_midi_clip),
            ("track/duplicate_clip", self._track_duplicate_clip),
            ("track/move_clip", self._track_move_clip),
            ("track/delete_clip", self._track_delete_clip),
            ("track/insert_device", self._track_insert_device),
            ("track/delete_device", self._track_delete_device),

            ("device/get/parameters", self._device_parameters),
            ("device/get/chains", self._device_chains),
            ("device/get/parameter", self._param_get),
            ("device/set/parameter", self._param_set),
            ("device/set/parameter_display", self._param_set_display),
            ("device/set/parameter_item", self._param_set_item),

            ("clip/get", self._clip_get),
            ("clip/set", self._clip_set),
            ("clip/get/notes", self._notes_get),
            ("clip/add/notes", self._notes_add),
            ("clip/remove/notes", self._notes_remove),
            ("clip/remove/notes_by_id", self._notes_remove_by_id),
            ("clip/modify/notes", self._notes_modify),
            ("clip/shift_notes", self._notes_shift),
            ("clip/transpose_notes", self._notes_transpose),
            ("clip/quantize", self._clip_quantize),
            ("clip/duplicate_region", self._clip_duplicate_region),
            ("clip/crop", self._clip_crop),
            ("clip/envelope/insert_step", self._envelope_insert_step),
            ("clip/envelope/get", self._envelope_get),
            ("clip/envelope/clear", self._envelope_clear),

            ("arrangement/delete_time", self._arrangement_delete_time),
            ("arrangement/insert_copy", self._arrangement_insert_copy),

            ("browser/list", self._browser_list),
            ("browser/load", self._browser_load),
        ]
        for path, fn in routes:
            self.osc_server.add_handler(PREFIX + path, self._guarded(PREFIX + path, fn))

    def _guarded(self, address: str, fn: Callable) -> Callable:
        def callback(params):
            try:
                rv = fn(tuple(params))
            except ArcError as e:
                self.logger.warning("arc %s refused: %s" % (address, e))
                return ("error", str(e))
            except Exception as e:                       # Live itself refused (RuntimeError etc.)
                self.logger.warning("arc %s failed: %s: %s" % (address, type(e).__name__, e))
                return ("error", "%s: %s" % (type(e).__name__, e))
            return ("ok",) + (tuple(rv) if rv is not None else ())
        return callback

    #--------------------------------------------------------------------------------
    # Resolution
    #--------------------------------------------------------------------------------
    def _all_tracks(self) -> List:
        return list(self.song.tracks) + list(self.song.return_tracks) + [self.song.master_track]

    def _track(self, ref) -> Any:
        name = _str(ref, "track")
        if name == "@master":
            return self.song.master_track
        candidates = list(self.song.tracks) + list(self.song.return_tracks)
        try:
            return pick_by_name(candidates, name, "track", "the set")
        except ArcError as e:
            if name.lower() == "master":
                raise ArcError("%s - the master track is \"@master\"" % e)
            raise

    def _device(self, track, ref) -> Any:
        selector = _str(ref, "device")
        if selector == "@mixer":
            return _Mixer(track)
        parts = selector.split(">")
        if len(parts) % 2 == 0:
            raise ArcError("device path %r must alternate device>chain>device" % selector)
        where = "track %r" % track.name
        device = pick_by_name(track.devices, parts[0], "device", where)
        for i in range(1, len(parts), 2):
            chains = _safe(lambda: list(device.chains), [])
            chain = pick_by_name(chains, parts[i], "chain", "device %r" % device.name)
            device = pick_by_name(chain.devices, parts[i + 1], "device", "chain %r" % chain.name)
        return device

    def _parameter(self, params: Tuple, usage: str):
        _need(params, 3, usage)
        track = self._track(params[0])
        device = self._device(track, params[1])
        parameter = pick_by_name(device.parameters, _str(params[2], "parameter"), "parameter",
                                 "device %r" % device.name)
        return track, device, parameter

    def _clip(self, track, ref) -> Any:
        if isinstance(ref, str) and ref.startswith("session:"):
            slot_index = _int(ref[len("session:"):], "clip slot")
            slots = list(track.clip_slots)
            if not 0 <= slot_index < len(slots) or slots[slot_index].clip is None:
                raise ArcError("track %r has no session clip in slot %d" % (track.name, slot_index))
            return slots[slot_index].clip
        index = _int(ref, "arrangement clip index")
        clips = self._arrangement(track)
        if not 0 <= index < len(clips):
            raise ArcError("track %r has %d arrangement clip(s); there is no index %d"
                           % (track.name, len(clips), index))
        return clips[index]

    def _arrangement(self, track) -> List:
        return list(_safe(lambda: track.arrangement_clips, ()) or ())

    def _clip_index(self, track, clip) -> int:
        for i, candidate in enumerate(self._arrangement(track)):
            if candidate == clip:
                return i
        return -1

    def _midi_clip(self, params: Tuple, usage: str):
        _need(params, 2, usage)
        track = self._track(params[0])
        clip = self._clip(track, params[1])
        if not _safe(lambda: clip.is_midi_clip, False):
            raise ArcError("clip %r on track %r is not a MIDI clip" % (clip.name, track.name))
        return track, clip

    #--------------------------------------------------------------------------------
    # Version and the set snapshot
    #--------------------------------------------------------------------------------
    def _live_version(self) -> Tuple[int, int, int]:
        app = Live.Application.get_application()
        return (app.get_major_version(), app.get_minor_version(),
                _safe(app.get_bugfix_version, 0))

    def _has_insert_device(self) -> bool:
        return hasattr(self.song.master_track, "insert_device")

    def _get_version(self, params):
        major, minor, bugfix = self._live_version()
        return ARC_API_VERSION, major, minor, bugfix, self._has_insert_device()

    def _get_structure(self, params):
        """
        Build the snapshot, cache it in chunks, and reply with how to fetch it:
        ("ok", request_id, chunk_count, total_chars, sha1). Chunks come one per request from
        get/structure_chunk, so a chunk lost on the network is re-asked for, not the whole set.
        """
        chunk_chars = _int(params[0], "chunk size") if params else DEFAULT_CHUNK_CHARS
        if not 1000 <= chunk_chars <= 60000:
            raise ArcError("chunk size must be between 1000 and 60000 characters")
        text = json.dumps(self.structure(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        chunks = [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)] or [""]
        self._structure_seq += 1
        self._structure_cache = (self._structure_seq, chunks)
        digest = hashlib.sha1(text.encode("ascii")).hexdigest()
        return self._structure_seq, len(chunks), len(text), digest

    def _get_structure_chunk(self, params):
        _need(params, 2, "(request_id, chunk_index)")
        request_id, index = _int(params[0], "request id"), _int(params[1], "chunk index")
        if self._structure_cache is None or self._structure_cache[0] != request_id:
            raise ArcError("structure %d is no longer cached - ask get/structure again" % request_id)
        chunks = self._structure_cache[1]
        if not 0 <= index < len(chunks):
            raise ArcError("structure %d has %d chunk(s)" % (request_id, len(chunks)))
        return request_id, index, chunks[index]

    def structure(self) -> Dict:
        song = self.song
        tracks = []
        for kind, group in (("track", list(song.tracks)), ("return", list(song.return_tracks)),
                            ("master", [song.master_track])):
            for index, track in enumerate(group):
                tracks.append(self._track_snapshot(track, kind, index))
        major, minor, bugfix = self._live_version()
        return {
            "arcApi": ARC_API_VERSION,
            "live": [major, minor, bugfix],
            "tempo": song.tempo,
            "signature": [_safe(lambda: song.signature_numerator), _safe(lambda: song.signature_denominator)],
            "loop": {"on": _safe(lambda: bool(song.loop)), "start": _safe(lambda: song.loop_start),
                     "length": _safe(lambda: song.loop_length)},
            "songLength": _safe(lambda: song.song_length),
            "cuePoints": [{"name": c.name, "time": c.time} for c in _safe(lambda: list(song.cue_points), [])],
            "tracks": tracks,
        }

    def _track_snapshot(self, track, kind: str, index: int) -> Dict:
        group_track = _safe(lambda: track.group_track)
        return {
            "kind": kind,
            "index": index,
            "name": track.name,
            "isGroup": bool(_safe(lambda: track.is_foldable, False)),
            "group": group_track.name if group_track is not None else None,
            "mute": _safe(lambda: bool(track.mute)),
            "solo": _safe(lambda: bool(track.solo)),
            "mixer": [self._param_snapshot(p) for p in _Mixer(track).parameters],
            "devices": [self._device_snapshot(d) for d in _safe(lambda: list(track.devices), [])],
            "arrangementClips": [self._clip_snapshot(i, c) for i, c in enumerate(self._arrangement(track))],
        }

    def _device_snapshot(self, device) -> Dict:
        return {
            "name": device.name,
            "className": _safe(lambda: device.class_name),
            "type": _safe(lambda: int(device.type)),
            "parameters": [self._param_snapshot(p) for p in _safe(lambda: list(device.parameters), [])],
            "chains": [{"name": chain.name,
                        "devices": [self._device_snapshot(d) for d in _safe(lambda: list(chain.devices), [])]}
                       for chain in _safe(lambda: list(device.chains), [])],
        }

    def _param_snapshot(self, parameter) -> Dict:
        return {
            "name": parameter.name,
            "value": parameter.value,
            "display": _safe(lambda: parameter.str_for_value(parameter.value)),
            "min": parameter.min,
            "max": parameter.max,
            "quantized": bool(_safe(lambda: parameter.is_quantized, False)),
            "automation": _safe(lambda: int(parameter.automation_state), 0),
        }

    def _clip_snapshot(self, index: int, clip) -> Dict:
        is_midi = bool(_safe(lambda: clip.is_midi_clip, False))
        snapshot = {
            "index": index,
            "name": clip.name,
            "start": clip.start_time,
            "end": clip.end_time,
            "muted": bool(_safe(lambda: clip.muted, False)),
            "gain": _safe(lambda: clip.gain),
            "isMidi": is_midi,
            "looping": _safe(lambda: bool(clip.looping)),
        }
        if is_midi:
            notes = list(clip.get_notes_extended(ALL_PITCHES[0], ALL_PITCHES[1], ALL_TIME[0], ALL_TIME[1]))
            snapshot["notes"] = {"count": len(notes), "digest": note_digest(notes)}
        return snapshot

    #--------------------------------------------------------------------------------
    # Tracks and the arrangement
    #--------------------------------------------------------------------------------
    def _track_get(self, params):
        _need(params, 1, "(track)")
        track = self._track(params[0])
        mixer = _Mixer(track)
        volume = mixer.parameters[0] if mixer.parameters else None
        return (track.name, _safe(lambda: bool(track.mute)), _safe(lambda: bool(track.solo)),
                volume.value if volume else None,
                _safe(lambda: volume.str_for_value(volume.value)) if volume else None)

    def _track_set(self, params):
        _need(params, 3, "(track, property, value)")
        track = self._track(params[0])
        prop = _str(params[1], "property")
        if prop not in TRACK_PROPS_RW:
            raise ArcError("track property %r is not settable here (only %s)" % (prop, ", ".join(TRACK_PROPS_RW)))
        if track == self.song.master_track:
            raise ArcError("the master track has no %s" % prop)
        setattr(track, prop, _bool(params[2], prop))
        return track.name, prop, bool(getattr(track, prop))

    def _track_arrangement_clips(self, params):
        _need(params, 1, "(track)")
        track = self._track(params[0])
        rv = [track.name]
        for i, clip in enumerate(self._arrangement(track)):
            rv += [i, clip.name, clip.start_time, clip.end_time, bool(_safe(lambda: clip.muted, False))]
        return tuple(rv)

    def _track_create_midi_clip(self, params):
        _need(params, 3, "(track, start_time, length)")
        track = self._track(params[0])
        start, length = _num(params[1], "start_time"), _num(params[2], "length")
        if length <= 0:
            raise ArcError("length must be positive")
        if not hasattr(track, "create_midi_clip"):
            raise ArcError("Track.create_midi_clip is not available in this Live version")
        self._refuse_overlap(track, start, start + length, ignore=None)
        clip = track.create_midi_clip(start, length)
        clip = clip if clip is not None else self._clip_at(track, start)
        return self._clip_index(track, clip), clip.start_time, clip.end_time

    def _track_duplicate_clip(self, params):
        _need(params, 3, "(track, clip, destination_time)")
        track = self._track(params[0])
        clip = self._clip(track, params[1])
        destination = _num(params[2], "destination_time")
        self._refuse_overlap(track, destination, destination + (clip.end_time - clip.start_time), ignore=None)
        copy = self._duplicate(track, clip, destination)
        return self._clip_index(track, copy), copy.start_time, copy.end_time

    def _track_move_clip(self, params):
        _need(params, 3, "(track, clip, new_start_time)")
        track = self._track(params[0])
        clip = self._clip(track, params[1])
        new_start = _num(params[2], "new_start_time")
        self._refuse_overlap(track, new_start, new_start + (clip.end_time - clip.start_time), ignore=clip)
        moved = self._move(track, clip, new_start, self._scratch_time())
        return self._clip_index(track, moved), moved.start_time, moved.end_time

    def _track_delete_clip(self, params):
        _need(params, 2, "(track, clip)")
        track = self._track(params[0])
        clip = self._clip(track, params[1])
        name = clip.name
        track.delete_clip(clip)
        return name, len(self._arrangement(track))

    def _track_insert_device(self, params):
        _need(params, 2, "(track, device_name[, index])")
        track = self._track(params[0])
        device_name = _str(params[1], "device name")
        count = len(list(track.devices))
        index = _int(params[2], "index") if len(params) > 2 else count
        if index < 0:
            index = count
        if not 0 <= index <= count:
            raise ArcError("index %d is outside 0..%d for track %r" % (index, count, track.name))
        if not hasattr(track, "insert_device"):
            raise ArcError("Track.insert_device needs Live 12.3 or later")
        track.insert_device(device_name, index)
        return (index,) + tuple(d.name for d in track.devices)

    def _track_delete_device(self, params):
        _need(params, 2, "(track, device)")
        track = self._track(params[0])
        selector = _str(params[1], "device")
        if ">" in selector or selector == "@mixer":
            raise ArcError("only a device directly on the track can be deleted here")
        device = self._device(track, params[1])
        devices = list(track.devices)
        track.delete_device(devices.index(device))
        return tuple(d.name for d in track.devices)

    def _clip_at(self, track, start: float):
        for clip in self._arrangement(track):
            if abs(clip.start_time - start) < 1e-6:
                return clip
        raise ArcError("Live did not place a clip at %g on track %r" % (start, track.name))

    def _duplicate(self, track, clip, destination: float):
        copy = track.duplicate_clip_to_arrangement(clip, destination)
        return copy if copy is not None else self._clip_at(track, destination)

    def _move(self, track, clip, new_start: float, scratch: float):
        """
        A clip's start_time is read-only, so a move is duplicate + delete. Through a scratch spot
        past the end of everything, because duplicating onto a range the original still covers
        would overwrite part of the original first.
        """
        parked = self._duplicate(track, clip, scratch)
        track.delete_clip(clip)
        moved = self._duplicate(track, parked, new_start)
        track.delete_clip(parked)
        return moved

    def _scratch_time(self) -> float:
        end = 0.0
        for track in self.song.tracks:
            for clip in self._arrangement(track):
                end = max(end, clip.end_time)
        end = max(end, _safe(lambda: float(self.song.song_length), 0.0) or 0.0)
        return math.ceil((end + 256.0) / 4.0) * 4.0

    def _refuse_overlap(self, track, start: float, end: float, ignore) -> None:
        for clip in self._arrangement(track):
            if clip == ignore:
                continue
            if clip.start_time < end - 1e-9 and start < clip.end_time - 1e-9:
                raise ArcError("%g..%g on track %r overlaps clip %r (%g..%g); Live would overwrite it"
                               % (start, end, track.name, clip.name, clip.start_time, clip.end_time))

    #--------------------------------------------------------------------------------
    # Whole-arrangement section edits
    #--------------------------------------------------------------------------------
    def _section_args(self, params, usage):
        _need(params, 2, usage)
        start, length = _num(params[0], "start"), _num(params[1], "length")
        if length <= 0:
            raise ArcError("length must be positive")
        allow_automation = _bool(params[2], "allow_automation") if len(params) > 2 else False
        return start, length, allow_automation

    def _refuse_section(self, start: float, end: float, allow_automation: bool) -> None:
        for track in self.song.tracks:
            for clip in self._arrangement(track):
                for boundary in (start, end):
                    if clip.start_time < boundary - 1e-9 < clip.end_time - 2e-9:
                        raise ArcError("clip %r on track %r (%g..%g) crosses %g - split it at the section "
                                       "boundary first; arc never cuts a clip for you"
                                       % (clip.name, track.name, clip.start_time, clip.end_time, boundary))
        if not allow_automation:
            automated = self._automated_parameters()
            if automated:
                raise ArcError("arrangement automation would not move with the clips (%s); pass "
                               "allow_automation=true to edit anyway" % ", ".join(automated[:8]))

    def _automated_parameters(self) -> List[str]:
        found = []

        def visit(track_name, devices):
            for device in devices:
                for parameter in _safe(lambda: list(device.parameters), []):
                    if _safe(lambda: int(parameter.automation_state), 0) != 0:
                        found.append("%s/%s/%s" % (track_name, device.name, parameter.name))
                for chain in _safe(lambda: list(device.chains), []):
                    visit(track_name, _safe(lambda: list(chain.devices), []))

        for track in self._all_tracks():
            visit(track.name, [_Mixer(track)] + _safe(lambda: list(track.devices), []))
        return found

    def _cue_points_from(self, time: float) -> int:
        return sum(1 for c in _safe(lambda: list(self.song.cue_points), []) if c.time >= time - 1e-9)

    def _arrangement_delete_time(self, params):
        """
        Remove start..start+length from the arrangement: the clips inside it are deleted and every
        later clip moves earlier by `length`, on every track. Cue points do not move (Live's API
        cannot move them), so the reply says how many sit at or after the cut.
        """
        start, length, allow_automation = self._section_args(params, "(start, length[, allow_automation])")
        end = start + length
        self._refuse_section(start, end, allow_automation)
        scratch = self._scratch_time()
        deleted = moved = 0
        for track in self.song.tracks:
            clips = self._arrangement(track)
            for clip in [c for c in clips if c.start_time >= start - 1e-9 and c.end_time <= end + 1e-9]:
                track.delete_clip(clip)
                deleted += 1
            for clip in sorted([c for c in clips if c.start_time >= end - 1e-9], key=lambda c: c.start_time):
                self._move(track, clip, clip.start_time - length, scratch)
                moved += 1
        return deleted, moved, self._cue_points_from(end)

    def _arrangement_insert_copy(self, params):
        """
        Insert a copy of start..start+length right after itself: every later clip moves later by
        `length` (latest first, so nothing is overwritten), then the section's clips are duplicated.
        """
        start, length, allow_automation = self._section_args(params, "(start, length[, allow_automation])")
        end = start + length
        self._refuse_section(start, end, allow_automation)
        scratch = self._scratch_time() + length
        copied = moved = 0
        for track in self.song.tracks:
            clips = self._arrangement(track)
            later = sorted([c for c in clips if c.start_time >= end - 1e-9], key=lambda c: -c.start_time)
            inside = [c for c in clips if c.start_time >= start - 1e-9 and c.end_time <= end + 1e-9]
            for clip in later:
                self._move(track, clip, clip.start_time + length, scratch)
                moved += 1
            for clip in inside:
                self._duplicate(track, clip, clip.start_time + length)
                copied += 1
        return copied, moved, self._cue_points_from(end)

    #--------------------------------------------------------------------------------
    # Devices and parameters
    #--------------------------------------------------------------------------------
    def _device_parameters(self, params):
        _need(params, 2, "(track, device)")
        track = self._track(params[0])
        device = self._device(track, params[1])
        return (device.name,) + tuple(p.name for p in device.parameters)

    def _device_chains(self, params):
        _need(params, 2, "(track, device)")
        track = self._track(params[0])
        device = self._device(track, params[1])
        return (device.name,) + tuple(c.name for c in _safe(lambda: list(device.chains), []))

    def _describe(self, parameter) -> Tuple:
        items = tuple(str(i) for i in (_safe(lambda: list(parameter.value_items), []) or []))
        return (parameter.value, parameter.min, parameter.max, bool(_safe(lambda: parameter.is_quantized, False)),
                _safe(lambda: parameter.str_for_value(parameter.value), "")) + items

    def _param_get(self, params):
        _, _, parameter = self._parameter(params, "(track, device, parameter)")
        return self._describe(parameter)

    def _param_set(self, params):
        _need(params, 4, "(track, device, parameter, value)")
        _, _, parameter = self._parameter(params, "(track, device, parameter, value)")
        value = _num(params[3], "value")
        if not parameter.min - 1e-9 <= value <= parameter.max + 1e-9:
            raise ArcError("%g is outside %r's range %g..%g" % (value, parameter.name, parameter.min, parameter.max))
        if not _safe(lambda: parameter.is_enabled, True):
            raise ArcError("parameter %r is disabled (macro-mapped or locked)" % parameter.name)
        parameter.value = value
        return parameter.value, _safe(lambda: parameter.str_for_value(parameter.value), "")

    def _param_set_display(self, params):
        """
        Set a continuous parameter so its DISPLAY reads `target` in base units (dB, Hz, ms, %).
        Parameter values are rarely in display units (a mixer fader is 0..1 for -inf..+6 dB), so this
        searches the value range for the display, then reports what the display actually reads.
        Reply: ("ok", value, display, achieved_number).
        """
        _need(params, 4, "(track, device, parameter, target)")
        _, _, parameter = self._parameter(params, "(track, device, parameter, target)")
        target = _num(params[3], "target")
        if _safe(lambda: parameter.is_quantized, False):
            raise ArcError("%r is stepped - set it with device/set/parameter_item" % parameter.name)

        def reading(value):
            return parse_display(parameter.str_for_value(value))

        lo, hi = float(parameter.min), float(parameter.max)
        at_lo, at_hi = reading(lo), reading(hi)
        if not min(at_lo, at_hi) <= target <= max(at_lo, at_hi):
            raise ArcError("%r displays %g..%g; %g is outside it" % (parameter.name, at_lo, at_hi, target))
        rising = at_hi >= at_lo
        for _ in range(DISPLAY_SEARCH_STEPS):
            mid = (lo + hi) / 2.0
            if (reading(mid) < target) == rising:
                lo = mid
            else:
                hi = mid
        best = min((lo, hi), key=lambda v: abs(reading(v) - target))
        parameter.value = best
        display = parameter.str_for_value(parameter.value)
        return parameter.value, display, parse_display(display)

    def _param_set_item(self, params):
        """
        Set a stepped parameter to the entry whose display is `item` ("arm", "Hi Shelf", "On").
        Native stepped parameters list their entries; a plug-in's choice parameter may not, so the
        value range is scanned for the stretch that displays `item` and its middle is used.
        """
        _need(params, 4, "(track, device, parameter, item)")
        _, _, parameter = self._parameter(params, "(track, device, parameter, item)")
        item = _str(params[3], "item")
        items = [str(i) for i in (_safe(lambda: list(parameter.value_items), []) or [])]
        candidate = None
        if item in items:
            value = parameter.min + items.index(item)
            if _safe(lambda: parameter.str_for_value(value)) == item:
                candidate = value
        if candidate is None:
            lo, hi = float(parameter.min), float(parameter.max)
            run = []
            for k in range(ITEM_SCAN_STEPS + 1):
                value = lo + (hi - lo) * k / ITEM_SCAN_STEPS
                if _safe(lambda: parameter.str_for_value(value)) == item:
                    run.append(value)
                elif run:
                    break
            if not run:
                shown = items or sorted({_safe(lambda: parameter.str_for_value(lo + (hi - lo) * k / 20), "?")
                                         for k in range(21)})
                raise ArcError("%r never displays %r (it shows: %s)" % (parameter.name, item, ", ".join(shown)))
            candidate = run[len(run) // 2]
        parameter.value = candidate
        display = parameter.str_for_value(parameter.value)
        if display != item:
            raise ArcError("set %r but it reads %r" % (parameter.name, display))
        return parameter.value, display

    #--------------------------------------------------------------------------------
    # Clips and notes
    #--------------------------------------------------------------------------------
    def _clip_get(self, params):
        _need(params, 2, "(track, clip)")
        track = self._track(params[0])
        clip = self._clip(track, params[1])
        return (clip.name, clip.start_time, clip.end_time, bool(_safe(lambda: clip.muted, False)),
                _safe(lambda: clip.gain), bool(_safe(lambda: clip.is_midi_clip, False)),
                _safe(lambda: bool(clip.looping)), clip.loop_start, clip.loop_end,
                clip.start_marker, clip.end_marker)

    def _clip_set(self, params):
        _need(params, 4, "(track, clip, property, value)")
        track = self._track(params[0])
        clip = self._clip(track, params[1])
        prop = _str(params[2], "property")
        if prop not in CLIP_PROPS_RW:
            raise ArcError("clip property %r is not settable here (only %s)" % (prop, ", ".join(CLIP_PROPS_RW)))
        if prop == "name":
            value = _str(params[3], "name")
        elif prop in ("muted", "looping"):
            value = _bool(params[3], prop)
        elif prop in ("pitch_coarse", "color"):
            value = _int(params[3], prop)
        else:
            value = _num(params[3], prop)
        setattr(clip, prop, value)
        return prop, getattr(clip, prop)

    def _range(self, params: Tuple, offset: int) -> Tuple[float, float, float, float]:
        """Optional (pitch_from, pitch_span, time_from, time_span) at params[offset:]."""
        if len(params) <= offset:
            return ALL_PITCHES[0], ALL_PITCHES[1], ALL_TIME[0], ALL_TIME[1]
        _need(params, offset + 4, "a range of 4 values: pitch_from, pitch_span, time_from, time_span")
        return (_int(params[offset], "pitch_from"), _int(params[offset + 1], "pitch_span"),
                _num(params[offset + 2], "time_from"), _num(params[offset + 3], "time_span"))

    def _notes(self, clip, rng):
        """
        The note vector Live returns - kept AS the vector, never copied into a list, because
        apply_note_modifications is documented to take exactly what get_notes_extended returned.
        """
        return clip.get_notes_extended(rng[0], rng[1], rng[2], rng[3])

    def _notes_get(self, params):
        _, clip = self._midi_clip(params, "(track, clip[, pitch_from, pitch_span, time_from, time_span])")
        notes = self._notes(clip, self._range(params, 2))
        if len(notes) > MAX_NOTES_PER_REPLY:
            raise ArcError("%d notes do not fit one reply - narrow the range" % len(notes))
        rv = [len(notes)]
        for n in notes:
            rv += [n.note_id, n.pitch, n.start_time, n.duration, n.velocity, bool(n.mute)]
        return tuple(rv)

    def _notes_add(self, params):
        usage = "(track, clip, then pitch, start_time, duration, velocity, mute per note)"
        _, clip = self._midi_clip(params, usage)
        values = params[2:]
        if not values or len(values) % 5 != 0:
            raise ArcError("expects %s" % usage)
        specs = []
        for i in range(0, len(values), 5):
            pitch = _int(values[i], "pitch")
            start, duration = _num(values[i + 1], "start_time"), _num(values[i + 2], "duration")
            velocity, mute = _num(values[i + 3], "velocity"), _bool(values[i + 4], "mute")
            if not 0 <= pitch <= 127 or duration <= 0 or not 0 < velocity <= 127:
                raise ArcError("note %d is not a playable note (pitch %d, duration %g, velocity %g)"
                               % (i // 5, pitch, duration, velocity))
            specs.append(Live.Clip.MidiNoteSpecification(pitch=pitch, start_time=start, duration=duration,
                                                         velocity=velocity, mute=mute))
        ids = clip.add_new_notes(tuple(specs))
        return (len(specs),) + tuple(ids or ())

    def _notes_remove(self, params):
        _, clip = self._midi_clip(params, "(track, clip[, pitch_from, pitch_span, time_from, time_span])")
        rng = self._range(params, 2)
        count = len(self._notes(clip, rng))
        clip.remove_notes_extended(rng[0], rng[1], rng[2], rng[3])
        return (count,)

    def _notes_remove_by_id(self, params):
        _, clip = self._midi_clip(params, "(track, clip, note_id...)")
        ids = [_int(v, "note id") for v in params[2:]]
        existing = {n.note_id for n in self._notes(clip, ALL_PITCHES + ALL_TIME)}
        missing = [i for i in ids if i not in existing]
        if not ids or missing:
            raise ArcError("no such note id(s): %s" % (missing or "none given"))
        clip.remove_notes_by_id(tuple(ids))
        return (len(ids),)

    def _region(self, clip) -> Tuple[float, float]:
        """Where a note can sound in this clip, in clip time."""
        if _safe(lambda: clip.looping, False):
            return clip.loop_start, clip.loop_end
        return clip.start_marker, clip.end_marker

    def _apply(self, clip, notes, edit: Callable, what: str) -> int:
        """
        Edit the notes `edit` chooses (it returns True for a note it changed), check every changed
        note still sounds inside the clip, and only then hand the vector back to Live. A refusal
        raises before apply_note_modifications, so the clip is untouched: the edits so far were made
        to Live's copy of the notes, not to the clip.
        """
        lo, hi = self._region(clip)
        changed = 0
        for note in notes:
            if not edit(note):
                continue
            changed += 1
            if not 0 <= note.pitch <= 127:
                raise ArcError("%s would push a note to pitch %d" % (what, note.pitch))
            if note.start_time < lo - 1e-9 or note.start_time >= hi - 1e-9:
                raise ArcError("%s would move a note to %g, outside the clip's %g..%g" % (what, note.start_time, lo, hi))
            if note.duration <= 0:
                raise ArcError("%s would leave a note with no duration" % what)
        if changed:
            clip.apply_note_modifications(notes)
        return changed

    def _notes_modify(self, params):
        """Rewrite notes by id: (note_id, pitch, start_time, duration, velocity, mute) per note."""
        usage = "(track, clip, then note_id, pitch, start_time, duration, velocity, mute per note)"
        _, clip = self._midi_clip(params, usage)
        values = params[2:]
        if not values or len(values) % 6 != 0:
            raise ArcError("expects %s" % usage)
        wanted = {}
        for i in range(0, len(values), 6):
            wanted[_int(values[i], "note id")] = (
                _int(values[i + 1], "pitch"), _num(values[i + 2], "start_time"), _num(values[i + 3], "duration"),
                _num(values[i + 4], "velocity"), _bool(values[i + 5], "mute"))
        notes = self._notes(clip, ALL_PITCHES + ALL_TIME)
        missing = sorted(set(wanted) - {n.note_id for n in notes})
        if missing:
            raise ArcError("no such note id(s): %s" % missing)

        def edit(note):
            if note.note_id not in wanted:
                return False
            note.pitch, note.start_time, note.duration, note.velocity, note.mute = wanted[note.note_id]
            return True
        return (self._apply(clip, notes, edit, "that modification"),)

    def _notes_shift(self, params):
        """
        Move notes in time by `amount` in "beats" or "ms". Milliseconds convert at the set's tempo
        when the edit runs, so a rig asking for +12 ms gets 12 ms at whatever tempo the set has.
        """
        _need(params, 4, "(track, clip, amount, unit[, pitch_from, pitch_span, time_from, time_span])")
        _, clip = self._midi_clip(params, "(track, clip, amount, unit, ...)")
        amount, unit = _num(params[2], "amount"), _str(params[3], "unit")
        if unit == "beats":
            delta = amount
        elif unit == "ms":
            delta = amount * float(self.song.tempo) / 60000.0
        else:
            raise ArcError("unit must be \"beats\" or \"ms\", got %r" % unit)
        notes = self._notes(clip, self._range(params, 4))

        def edit(note):
            note.start_time = note.start_time + delta
            return True
        return self._apply(clip, notes, edit, "that shift"), delta

    def _notes_transpose(self, params):
        _need(params, 3, "(track, clip, semitones[, pitch_from, pitch_span, time_from, time_span])")
        _, clip = self._midi_clip(params, "(track, clip, semitones, ...)")
        semitones = _int(params[2], "semitones")
        notes = self._notes(clip, self._range(params, 3))

        def edit(note):
            note.pitch = note.pitch + semitones
            return True
        return (self._apply(clip, notes, edit, "that transpose"),)

    def _clip_quantize(self, params):
        _need(params, 4, "(track, clip, grid, amount)")
        _, clip = self._midi_clip(params, "(track, clip, grid, amount)")
        grid_name = _str(params[2], "grid")
        if grid_name not in QUANTIZE_GRIDS:
            raise ArcError("grid must be one of %s" % ", ".join(QUANTIZE_GRIDS))
        grid = getattr(Live.Song.RecordingQuantization, QUANTIZE_GRIDS[grid_name], None)
        if grid is None:
            raise ArcError("this Live version has no %s grid" % grid_name)
        amount = _num(params[3], "amount")
        if not 0.0 <= amount <= 1.0:
            raise ArcError("amount must be 0..1")
        clip.quantize(grid, amount)
        return grid_name, amount

    def _clip_duplicate_region(self, params):
        _need(params, 5, "(track, clip, region_start, region_length, destination[, pitch, transposition])")
        _, clip = self._midi_clip(params, "(track, clip, region_start, region_length, destination, ...)")
        region_start, region_length = _num(params[2], "region_start"), _num(params[3], "region_length")
        destination = _num(params[4], "destination")
        pitch = _int(params[5], "pitch") if len(params) > 5 else -1
        transposition = _int(params[6], "transposition") if len(params) > 6 else 0
        if region_length <= 0:
            raise ArcError("region_length must be positive")
        clip.duplicate_region(region_start, region_length, destination, pitch, transposition)
        return (len(self._notes(clip, ALL_PITCHES + ALL_TIME)),)

    def _clip_crop(self, params):
        _need(params, 2, "(track, clip)")
        track = self._track(params[0])
        clip = self._clip(track, params[1])
        clip.crop()
        return clip.start_marker, clip.end_marker

    #--------------------------------------------------------------------------------
    # Clip automation envelopes
    #--------------------------------------------------------------------------------
    def _envelope_target(self, params, count, usage):
        _need(params, count, usage)
        track = self._track(params[0])
        clip = self._clip(track, params[1])
        device = self._device(track, params[2])
        parameter = pick_by_name(device.parameters, _str(params[3], "parameter"), "parameter",
                                 "device %r" % device.name)
        return clip, parameter

    def _envelope_insert_step(self, params):
        clip, parameter = self._envelope_target(
            params, 7, "(track, clip, device, parameter, time, length, value)")
        time, length, value = _num(params[4], "time"), _num(params[5], "length"), _num(params[6], "value")
        if length <= 0:
            raise ArcError("length must be positive")
        if not parameter.min - 1e-9 <= value <= parameter.max + 1e-9:
            raise ArcError("%g is outside %r's range %g..%g" % (value, parameter.name, parameter.min, parameter.max))
        envelope = clip.automation_envelope(parameter)
        if envelope is None:
            if not hasattr(clip, "create_automation_envelope"):
                raise ArcError("Clip.create_automation_envelope is not available in this Live version")
            envelope = clip.create_automation_envelope(parameter)
        if envelope is None:
            raise ArcError("Live gave no envelope for %r on clip %r" % (parameter.name, clip.name))
        envelope.insert_step(time, length, value)
        return time, length, value

    def _envelope_get(self, params):
        clip, parameter = self._envelope_target(params, 5, "(track, clip, device, parameter, time)")
        envelope = clip.automation_envelope(parameter)
        if envelope is None:
            return ("none",)
        return ("envelope", envelope.value_at_time(_num(params[4], "time")))

    def _envelope_clear(self, params):
        clip, parameter = self._envelope_target(params, 4, "(track, clip, device, parameter)")
        clip.clear_envelope(parameter)
        return (parameter.name,)

    #--------------------------------------------------------------------------------
    # The browser (presets, samples, plug-ins). Python Remote Script only: Max's LiveAPI cannot.
    #--------------------------------------------------------------------------------
    def _browser_item(self, params: Tuple, offset: int):
        _need(params, offset + 1, "(category, name, name, ...)")
        category = _str(params[offset], "category")
        if category not in BROWSER_CATEGORIES:
            raise ArcError("category must be one of %s" % ", ".join(BROWSER_CATEGORIES))
        browser = Live.Application.get_application().browser
        item = getattr(browser, category, None)
        if item is None:
            raise ArcError("this Live version's browser has no %r" % category)
        path = [category]
        for name in params[offset + 1:]:
            item = pick_by_name(list(item.children), _str(name, "browser item"), "browser item", "/".join(path))
            path.append(item.name)
        return item

    def _browser_list(self, params):
        item = self._browser_item(params, 0)
        return tuple(child.name for child in item.children)

    def _browser_load(self, params):
        _need(params, 3, "(track, category, name, ...)")
        track = self._track(params[0])
        item = self._browser_item(params, 1)
        if not item.is_loadable:
            raise ArcError("browser item %r is a folder, not something Live can load" % item.name)
        self.song.view.selected_track = track
        Live.Application.get_application().browser.load_item(item)
        return tuple(d.name for d in track.devices)
