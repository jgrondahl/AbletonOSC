"""Inserting and deleting devices, clip automation envelopes, and the browser."""


def test_insert_a_native_device(rig):
    reply = rig.ok("track/insert_device", "Bass", "Saturator")
    assert reply == (2, "EQ Eight", "Compressor", "Saturator")
    assert rig.ok("track/insert_device", "Bass", "Utility", 0)[:2] == (0, "Utility")


def test_insert_refuses_an_index_off_the_end(rig):
    assert "outside 0..2" in rig.error("track/insert_device", "Bass", "Saturator", 5)


def test_insert_is_refused_before_live_12_3(legacy_rig):
    assert "needs Live 12.3 or later" in legacy_rig.error("track/insert_device", "Bass", "Saturator")


def test_delete_a_device_by_name(rig):
    assert rig.ok("track/delete_device", "Bass", "Compressor") == ("EQ Eight",)
    assert "directly on the track" in rig.error("track/delete_device", "Kick", "Drum Rack>Kick>Simpler")


def test_an_envelope_is_created_on_first_step_and_read_back(rig):
    assert rig.ok("clip/envelope/get", "Kick", 1, "EQ Eight", "1 Gain A", 0.0) == ("none",)
    assert rig.ok("clip/envelope/insert_step", "Kick", 1, "EQ Eight", "1 Gain A", 0.0, 16.0, -0.5) == (0.0, 16.0, -0.5)
    assert rig.ok("clip/envelope/get", "Kick", 1, "EQ Eight", "1 Gain A", 8.0) == ("envelope", -0.5)
    assert rig.ok("clip/envelope/get", "Kick", 1, "EQ Eight", "1 Gain A", 20.0) == ("envelope", 0.0)
    assert rig.ok("clip/envelope/clear", "Kick", 1, "EQ Eight", "1 Gain A") == ("1 Gain A",)
    assert rig.ok("clip/envelope/get", "Kick", 1, "EQ Eight", "1 Gain A", 8.0) == ("none",)


def test_an_envelope_step_outside_the_parameter_range_is_refused(rig):
    assert "outside '1 Gain A''s range" in rig.error("clip/envelope/insert_step", "Kick", 1, "EQ Eight", "1 Gain A",
                                                     0.0, 16.0, 3.0)
    assert rig.ok("clip/envelope/get", "Kick", 1, "EQ Eight", "1 Gain A", 0.0) == ("none",)


def test_an_envelope_on_the_mixer(rig):
    rig.ok("clip/envelope/insert_step", "Lead", 0, "@mixer", "Track Volume", 0.0, 8.0, 0.5)
    assert rig.ok("clip/envelope/get", "Lead", 0, "@mixer", "Track Volume", 4.0) == ("envelope", 0.5)


def test_browser_list_walks_by_name(rig):
    assert rig.ok("browser/list", "instruments") == ("Operator",)
    assert rig.ok("browser/list", "instruments", "Operator", "Bass") == ("Sub Bass.adg",)
    assert "category must be one of" in rig.error("browser/list", "synths")
    assert "no browser item named 'Wavetable'" in rig.error("browser/list", "instruments", "Wavetable")


def test_browser_load_puts_the_item_on_the_named_track(rig):
    devices = rig.ok("browser/load", "Bass", "instruments", "Operator", "Bass", "Sub Bass.adg")
    assert devices[-1] == "Sub Bass"
    assert rig.app.browser.loaded == [("Bass", "Sub Bass.adg")]
    assert rig.song.view.selected_track is rig.track("Bass")


def test_browser_load_refuses_a_folder(rig):
    assert "is a folder" in rig.error("browser/load", "Bass", "instruments", "Operator")
    assert rig.app.browser.loaded == []
