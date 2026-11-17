"""Setting parameters: raw values, physical units through the display, and stepped entries by name."""
import pytest

import fake_live


def _param(rig, track, device, name):
    target = rig.track(track)
    for d in target.devices:
        if d.name == device:
            return next(p for p in d.parameters if p.name == name)
    raise KeyError(device)


def test_get_describes_value_range_and_display(rig):
    value, lo, hi, quantized, display = rig.ok("device/get/parameter", "@master", "Utility", "Gain")
    assert (value, lo, hi, quantized, display) == (0.0, -1.0, 1.0, False, "0.0 dB")


def test_get_lists_a_stepped_parameter_entries(rig):
    reply = rig.ok("device/get/parameter", "Bass", "EQ Eight", "1 Filter Type A")
    assert reply[3] is True and reply[5:] == ("Low Cut 48", "Low Cut 12", "Low Shelf", "Bell", "Notch",
                                              "High Shelf", "High Cut 12", "High Cut 48")


def test_set_writes_and_reads_back(rig):
    assert rig.ok("device/set/parameter", "@master", "Utility", "Width", 0.0) == (0.0, "0.00")


def test_an_out_of_range_value_is_refused_and_nothing_is_written(rig):
    width = _param(rig, "@master", "Utility", "Width")
    assert "outside 'Width''s range" in rig.error("device/set/parameter", "@master", "Utility", "Width", 1.5)
    assert width.writes == [] and width.value == 0.5


def test_a_disabled_parameter_is_refused(rig):
    _param(rig, "@master", "Utility", "Width").is_enabled = False
    assert "disabled" in rig.error("device/set/parameter", "@master", "Utility", "Width", 0.2)


@pytest.mark.parametrize("track, device, name, target, display", [
    ("@master", "Utility", "Gain", -6.0, "-6.0 dB"),                 # the calibration's loudness edit
    ("@master", "Utility", "Gain", -3.0, "-3.0 dB"),
    ("Bass", "EQ Eight", "1 Frequency A", 1200.0, "1.20 kHz"),       # unit switches mid-range
    ("Bass", "EQ Eight", "1 Frequency A", 120.0, "120 Hz"),
    ("Bass", "Compressor", "Release", 1500.0, "1.50 s"),             # target in ms, display in s
    ("Bass", "Compressor", "Release", 60.0, "60.0 ms"),
])
def test_set_by_display_lands_on_the_physical_value(rig, track, device, name, target, display):
    value, shown, achieved = rig.ok("device/set/parameter_display", track, device, name, target)
    assert shown == display
    assert achieved == pytest.approx(target)
    assert _param(rig, track, device, name).value == value


def test_set_by_display_drives_a_fader_from_minus_infinity(rig):
    _, shown, achieved = rig.ok("device/set/parameter_display", "Snare", "@mixer", "Track Volume", -3.0)
    assert shown == "-3.0 dB" and achieved == -3.0


def test_set_by_display_refuses_a_target_outside_the_range(rig):
    gain = _param(rig, "@master", "Utility", "Gain")
    assert "outside it" in rig.error("device/set/parameter_display", "@master", "Utility", "Gain", -40.0)
    assert gain.writes == []


def test_set_by_display_refuses_a_stepped_parameter(rig):
    assert "parameter_item" in rig.error("device/set/parameter_display", "Bass", "EQ Eight", "1 Filter Type A", 3)


def test_set_by_display_refuses_a_unit_it_cannot_convert(rig):
    _param(rig, "@master", "Utility", "Width")._display = lambda v: "%d%% L" % (v * 100) if v else "C"
    message = rig.error("device/set/parameter_display", "@master", "Utility", "Width", 50.0)
    assert "cannot read a number" in message or "does not convert" in message


def test_set_item_on_a_native_stepped_parameter(rig):
    assert rig.ok("device/set/parameter_item", "Bass", "EQ Eight", "1 Filter Type A", "Bell") == (3.0, "Bell")


def test_set_item_on_a_plugin_choice_without_value_items(rig):
    # ARC Capture's remote command as a host sees a VST3 choice: normalized 0..1, no entries listed.
    value, shown = rig.ok("device/set/parameter_item", "@master", "ARC Capture", "ARC Remote Command", "arm")
    assert shown == "arm" and fake_live.plugin_choice_display(value) == "arm"
    value, shown = rig.ok("device/set/parameter_item", "@master", "ARC Capture", "ARC Remote Command", "send")
    assert shown == "send" and value == pytest.approx(0.5, abs=0.13)


def test_set_item_refuses_an_entry_that_never_displays(rig):
    message = rig.error("device/set/parameter_item", "@master", "ARC Capture", "ARC Remote Command", "record")
    assert "never displays 'record'" in message and "arm" in message
    assert _param(rig, "@master", "ARC Capture", "ARC Remote Command").writes == []


def test_track_mute_by_name(rig):
    assert rig.ok("track/set", "Lead", "mute", True) == ("Lead", "mute", True)
    assert rig.track("Lead").mute is True
    assert "only mute, solo" in rig.error("track/set", "Lead", "arm", True)
    assert "master track has no mute" in rig.error("track/set", "@master", "mute", True)
