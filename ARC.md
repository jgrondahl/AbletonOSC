# ARC rig additions

This fork adds one handler module, `abletonosc/arc.py`, for the Ableton end-to-end rig in
**AI-music-producer-coach** (`docs/ops/ableton-e2e-rig.md` there). The rig makes edits of known size
in Live, captures the result through the ARC Capture plugin, and checks that ARC's worker measured
the edit. So every edit here either lands exactly where it was aimed or is refused.

Upstream's addresses are unchanged. Everything new lives under `/live/arc/`, so a sync with upstream
never conflicts. The only upstream files touched are `abletonosc/__init__.py` and `manager.py`, three
lines that register and reload the handler.

**These addresses are a cross-repo contract.** The rig's Playwright fixtures call them by name. Change
one here and the rig changes in the same week. `arc_tests/` pins them.

## Conventions

- **Targets by name, never by index.** An index shifts when a device is inserted, and an edit aimed
  by a stale index lands on the wrong thing with no error.

  | Target | How to name it |
  |---|---|
  | track | its name. `@master` is the master track. Return tracks go by name. |
  | device | its name on that track. `EQ Eight#2` is the second of two. `Drum Rack>Kick>Simpler` reaches inside a rack, alternating device and chain. `@mixer` is the track's mixer. |
  | parameter | its name on that device. The mixer's are `Track Volume`, `Track Panning`, and the sends. |
  | arrangement clip | its index in start-time order, read from `get/structure` first. `session:N` is clip slot N. |

  A missing or ambiguous name is refused, and the refusal lists the names that do exist.
- **Every reply starts with `ok` or `error`.** An error carries the reason. Each handler checks
  everything it can before its first change, so a refusal leaves the set as it was.
- **Times are in beats** unless an argument says otherwise.

## Addresses

Arguments in brackets are optional. A range is `pitch_from, pitch_span, time_from, time_span`, and
leaving it out means every note.

| Address | Arguments | Reply after `ok` |
|---|---|---|
| `get/version` | | arc API version, Live major, minor, bugfix, insert_device available |
| `get/structure` | [chunk size] | request id, chunk count, total chars, sha1 |
| `get/structure_chunk` | request id, index | request id, index, JSON text |
| `track/get` | track | name, mute, solo, volume value, volume display |
| `track/set` | track, `mute`\|`solo`, value | name, property, value read back |
| `track/get/arrangement_clips` | track | name, then index, name, start, end, muted per clip |
| `track/create_midi_clip` | track, start, length | clip index, start, end |
| `track/duplicate_clip` | track, clip, destination | new clip index, start, end |
| `track/move_clip` | track, clip, new start | clip index, start, end |
| `track/delete_clip` | track, clip | deleted clip's name, clips left |
| `track/insert_device` | track, Live device name, [index] | index, then the track's device names. Needs Live 12.3+. |
| `track/delete_device` | track, device | the track's device names |
| `device/get/parameters` | track, device | device name, parameter names |
| `device/get/chains` | track, device | device name, chain names |
| `device/get/parameter` | track, device, parameter | value, min, max, quantized, display, then its entries if stepped |
| `device/set/parameter` | track, device, parameter, value | value, display |
| `device/set/parameter_display` | track, device, parameter, target | value, display, the number the display reads |
| `device/set/parameter_item` | track, device, parameter, entry | value, display |
| `clip/get` | track, clip | name, start, end, muted, gain, is MIDI, looping, loop start, loop end, start marker, end marker |
| `clip/set` | track, clip, property, value | property, value read back |
| `clip/get/notes` | track, clip, [range] | count, then id, pitch, start, duration, velocity, mute per note |
| `clip/add/notes` | track, clip, then pitch, start, duration, velocity, mute per note | count, then the new note ids |
| `clip/remove/notes` | track, clip, [range] | count removed |
| `clip/remove/notes_by_id` | track, clip, ids | count removed |
| `clip/modify/notes` | track, clip, then id, pitch, start, duration, velocity, mute per note | count changed |
| `clip/shift_notes` | track, clip, amount, `beats`\|`ms`, [range] | count moved, the shift in beats |
| `clip/transpose_notes` | track, clip, semitones, [range] | count moved |
| `clip/quantize` | track, clip, grid, amount 0..1 | grid, amount |
| `clip/duplicate_region` | track, clip, region start, region length, destination, [pitch, transposition] | notes in the clip |
| `clip/crop` | track, clip | start marker, end marker |
| `clip/envelope/insert_step` | track, clip, device, parameter, time, length, value | time, length, value |
| `clip/envelope/get` | track, clip, device, parameter, time | `none`, or `envelope` and the value at that time |
| `clip/envelope/clear` | track, clip, device, parameter | parameter name |
| `arrangement/delete_time` | start, length, [allow_automation] | clips deleted, clips moved, cue points at or after the cut |
| `arrangement/insert_copy` | start, length, [allow_automation] | clips copied, clips moved, cue points at or after the section |
| `browser/list` | category, then names down the tree | the children's names |
| `browser/load` | track, category, then names down the tree | the track's device names |

### Notes on the less obvious ones

- **`device/set/parameter_display`** sets a parameter so its display reads a physical value: dB, Hz,
  ms or %, with kHz and s converted. Live's parameter values are rarely in display units. A fader is
  0 to 1 for minus infinity to +6 dB. So this searches the value range and reports what the display
  then reads. The rig checks that number.
- **`device/set/parameter_item`** sets a stepped parameter by the entry it displays. A plug-in's
  choice parameter may list no entries at all, so the value range is scanned for the stretch that
  shows the entry. This is how the rig writes `arm` to ARC Capture's `ARC Remote Command`.
- **`clip/shift_notes`** in `ms` converts at the set's tempo when the edit runs.
- **Note edits are all-or-nothing.** A shift or transpose that would push any note outside the clip
  or the MIDI range is refused before Live applies anything.
- **A clip move goes through a scratch spot** past the end of everything. A clip's start time is
  read-only, so a move is duplicate plus delete. Duplicating onto a range the original still covers
  would overwrite part of it first.
- **`arrangement/delete_time` and `insert_copy`** work on every track. They refuse a clip that
  crosses the section boundary, because arc never cuts a clip for you. They also refuse when any
  parameter has arrangement automation, because that automation would not move with the clips.
  `allow_automation` overrides that. Cue points do not move, because Live's API cannot move them, and
  the reply counts the ones affected.
- **`get/structure`** is a snapshot of the whole set: tracks, the mixer, devices with every
  parameter, arrangement clips with a note count and digest, tempo, loop and cue points. It comes in
  chunks, one per request, so a chunk lost over UDP is asked for again on its own. Only the latest
  snapshot is cached. The rig restores its baseline by matching this snapshot, never by counting
  undos.

## Tests

| Suite | Runs | Needs |
|---|---|---|
| `arc_tests/` | every handler against a simulated Live, plus a loopback UDP round trip through AbletonOSC's own server | Python and pytest |
| `tests/test_arc.py` | the same calls against a running Live, on a scratch track it creates and deletes | Live 12.3+ with this fork as the Control Surface |

```bash
python -m pytest arc_tests
```

```bash
python -m pytest tests/test_arc.py
```

`arc_tests/README.md` lists what the simulated Live assumes about real Live. `tests/test_arc.py`
checks those assumptions. Until it has passed on the Mac, treat them as unverified.
