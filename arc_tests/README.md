# arc_tests: the arc handlers against a simulated Live

```bash
python -m pytest arc_tests
```

`conftest.py` loads the real AbletonOSC package with Live's modules replaced by `fake_live.py`, so the
handlers in `abletonosc/arc.py` run under plain pytest with no Ableton Live and no network. It loads
the repository as package `AbletonOSC` whatever its checkout directory is called.

`test_arc_wire.py` also runs the handlers through AbletonOSC's own `OSCServer` on a loopback socket,
with messages built and parsed by the bundled pythonosc. That catches an argument that does not
survive OSC encoding, which calling handlers directly cannot.

## The simulated set

`fake_live.build_set()` is shaped like the rig's `arc-e2e.als`. It runs at 124 BPM, with sections of
32 beats: intro, drop, breakdown and drop 2. It has a Drums group, Kick with a Drum Rack, Snare,
Bass with a Compressor, and Lead with two `EQ Eight`s, which makes the name ambiguous on purpose.
There is an `A-Reverb` return. The master has Utility, EQ Eight and ARC Capture. ARC Capture's
`ARC Remote Command` is modeled as a host sees a VST3 choice: normalized 0 to 1, with no entries
listed.

## Where the simulation is strict on purpose

- `apply_note_modifications` takes only the vector `get_notes_extended` returned, which is Live's
  documented contract. A list copied out of it is rejected.
- `get_notes_extended` hands out copies, so an edit that is never applied never reaches the clip.
  That is what makes refusals all-or-nothing.
- Placing a clip over another raises. Real Live silently overwrites the overlap, which arc must never
  cause.
- Parameters raise on an out-of-range value, as Live does.

Disabling any of arc's protections makes at least one test fail. That has been checked for the
scratch-spot move, keeping the note vector, earliest-first section moves, the overlap refusal and
the automation refusal.

## What only real Live can confirm

The simulation encodes these assumptions. `tests/test_arc.py` checks them against a running Live.
Until it has passed, treat them as unverified.

1. `Track.insert_device(device_name, index)` exists in Live 12.3 and later.
2. `Track.create_midi_clip`, `duplicate_clip_to_arrangement` and `delete_clip` behave as the Live
   Object Model reference describes, and a duplicate returns the new clip or lands at the requested
   time.
3. `add_new_notes` returns the new note ids.
4. Editing notes while iterating the vector `get_notes_extended` returned changes that vector, and
   `apply_note_modifications` applies it.
5. `Clip.create_automation_envelope` and `AutomationEnvelope.insert_step` work. The research pass
   could not load their documentation page.
6. Parameter displays have the shapes `parse_display` reads, such as `-6.0 dB`, `1.20 kHz`, `450 ms`
   and `1.50 s`.
7. ARC Capture's two parameters are reachable through Live's plug-in parameter list, and their
   entries display as `none`, `arm`, and so on. That may need the device's **Configure** step first.
8. `automation_state` is non-zero only for arrangement automation, not for clip envelopes.
   Otherwise the section edits would refuse whenever a clip has an envelope.
