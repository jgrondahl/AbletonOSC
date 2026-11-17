"""Name addressing: the rig never aims an edit by index, and a missing or ambiguous name is refused."""


def test_version_reports_the_api_live_and_insert_device(rig, legacy_rig):
    assert rig.ok("get/version") == (1, 12, 3, 1, True)
    assert legacy_rig.ok("get/version") == (1, 12, 2, 5, False)


def test_tracks_resolve_by_name_including_returns_and_master(rig):
    assert rig.ok("track/get", "Kick")[0] == "Kick"
    assert rig.ok("track/get", "A-Reverb")[0] == "A-Reverb"
    assert rig.ok("track/get", "@master")[0] == "Master"


def test_a_missing_track_is_refused_with_the_names_that_exist(rig):
    message = rig.error("track/get", "Hats")
    assert "no track named 'Hats'" in message and "'Kick'" in message and "'Bass'" in message


def test_the_master_is_not_reachable_by_its_display_name(rig):
    # A regular track may also be called "Master"; only "@master" means the master track.
    assert "@master" in rig.error("track/get", "Master")


def test_two_tracks_with_one_name_are_refused_not_guessed(rig):
    rig.track("Bass").name = "Kick"
    message = rig.error("track/get", "Kick")
    assert "2 tracks are named 'Kick'" in message


def test_a_non_string_target_is_refused(rig):
    assert "non-empty name" in rig.error("track/get", 3)


def test_an_ambiguous_device_is_refused_and_says_how_to_choose(rig):
    message = rig.error("device/get/parameters", "Lead", "EQ Eight")
    assert "2 devices are named 'EQ Eight'" in message and "EQ Eight#2" in message


def test_the_nth_device_of_a_name_is_reachable(rig):
    lead = rig.track("Lead")
    lead.devices[1].parameters[1].value = 0.9
    assert rig.ok("device/get/parameter", "Lead", "EQ Eight#2", "1 Frequency A")[0] == 0.9
    assert rig.ok("device/get/parameter", "Lead", "EQ Eight#1", "1 Frequency A")[0] == 0.3
    assert "not 3" in rig.error("device/get/parameters", "Lead", "EQ Eight#3")


def test_an_exact_name_containing_a_hash_wins(rig):
    rig.track("Bass").devices[0].name = "Bus#2"
    assert rig.ok("device/get/parameters", "Bass", "Bus#2")[0] == "Bus#2"


def test_a_rack_path_reaches_inside_chains(rig):
    assert rig.ok("device/get/chains", "Kick", "Drum Rack") == ("Drum Rack", "Kick", "Snare")
    assert rig.ok("device/get/parameter", "Kick", "Drum Rack>Kick>Simpler", "Transpose")[0] == 0.0
    assert "alternate device>chain>device" in rig.error("device/get/parameters", "Kick", "Drum Rack>Kick")
    assert "no chain named 'Hat'" in rig.error("device/get/parameters", "Kick", "Drum Rack>Hat>Simpler")


def test_the_mixer_is_a_device_named_at_mixer(rig):
    names = rig.ok("device/get/parameters", "Snare", "@mixer")
    assert names == ("@mixer", "Track Volume", "Track Panning")
    assert rig.ok("device/get/parameter", "@master", "@mixer", "Track Volume")[4] == "3.2 dB"


def test_a_missing_parameter_lists_the_ones_that_exist(rig):
    message = rig.error("device/get/parameter", "@master", "Utility", "Volume")
    assert "no parameter named 'Volume'" in message and "'Gain'" in message
