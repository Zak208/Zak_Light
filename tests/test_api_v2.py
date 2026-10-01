import json

import pytest

from core.animations import EFFECT_KEYS


# ---- light-quality settings ----------------------------------------------------------------------------------

def test_new_settings_are_validated_and_reach_the_pipeline(client, state):
    body = {"white_balance": [1.0, 0.8, 0.6], "max_power": 0.5, "dark_light": 0.05, "dithering": False,
            "adaptive_smoothing": False, "transitions": False, "screen_style": "average",
            "edges_enabled": [True, False, True, False], "audio_boost": 0.4, "idle_off_minutes": 15}
    assert client.post("/api/settings", json=body).status_code == 200
    cfg, p = state.config, state.color_pipeline
    assert cfg.screen_style == "average" and cfg.edges_enabled == [True, False, True, False] and cfg.idle_off_minutes == 15
    assert p.white_balance.tolist() == pytest.approx([1.0, 0.8, 0.6]) and p.max_power == 0.5 and p.dark_light == 0.05
    assert p.dithering is False and p.adaptive_smoothing is False


@pytest.mark.parametrize("bad", [
    {"white_balance": [1, 1]}, {"white_balance": [2, 1, 1]}, {"white_balance": [0.1, 1, 1]}, {"max_power": 0},
    {"max_power": 2}, {"dark_light": 0.9}, {"screen_style": "wild"}, {"edges_enabled": [True]},
    {"audio_boost": 1.5}, {"idle_off_minutes": -1}, {"idle_off_minutes": 100000}, {"rhythm_pattern": 10},
])
def test_bad_new_settings_are_rejected_without_side_effects(client, state, bad):
    before = state.config.max_power, state.config.screen_style
    assert client.post("/api/settings", json=bad).status_code == 422
    assert (state.config.max_power, state.config.screen_style) == before


def test_all_ten_rhythm_patterns_are_accepted(client, state):
    for i in range(10):
        assert client.post("/api/settings", json={"rhythm_pattern": i}).status_code == 200
    assert state.config.rhythm_pattern == 9


def test_status_carries_the_new_fields(client, monkeypatch):
    monkeypatch.setattr("core.display.hdr_enabled", lambda: True)
    data = client.get("/api/status").json()
    assert data["hdr"] is True and data["capture_stalled"] is False and data["away"] is False
    assert {"white_balance", "max_power", "screen_style", "app_rules", "schedules", "effect_palettes", "effect_colors",
            "hdr_compensation", "monitor_color_profile_saved"} <= set(data["config"])
    assert {"latency_ms", "fps_scale", "capture_stalled", "uptime_s", "usb_writes_per_min"} <= set(data["engine"])


def test_performance_profiles_set_fps_without_changing_image_tuning(client, state):
    state.config.gamma = 2.6
    assert client.post("/api/settings", json={"performance_mode": "eco"}).status_code == 200
    assert state.config.performance_mode == "eco" and state.config.target_fps == 20
    assert state.config.gamma == 2.6

    assert client.post("/api/settings", json={"performance_mode": "fluid"}).status_code == 200
    assert state.config.performance_mode == "fluid" and state.config.target_fps == 60

    assert client.post("/api/settings", json={"target_fps": 35}).status_code == 200
    assert state.config.performance_mode == "custom" and state.config.target_fps == 35


def test_led_preview_omits_an_unchanged_frame(client, state):
    first = client.get("/api/leds").json()
    assert first["unchanged"] is False and "colors" in first

    same = client.get(f"/api/leds?since={first['revision']}").json()
    assert same["unchanged"] is True and "colors" not in same

    state.set_mode("off")
    changed = client.get(f"/api/leds?since={first['revision']}").json()
    assert changed["unchanged"] is False and changed["revision"] > first["revision"]


def test_audio_device_selection_and_safe_diagnostics_export(client, state):
    state.audio_engine.audio_devices = lambda: [
        {"id": "", "name": "Salida predeterminada de Windows", "default": True},
        {"id": "endpoint-1", "name": "Auriculares", "default": False},
    ]
    state.audio_engine.audio_status = lambda: {
        "available": True, "active": False, "selected": False, "active_device": "", "last_error": ""
    }
    listed = client.get("/api/audio_devices").json()
    assert listed["selected"] == "" and listed["devices"][1]["id"] == "endpoint-1"

    assert client.post("/api/settings", json={"audio_device_id": "endpoint-1"}).status_code == 200
    assert state.config.audio_device_id == "endpoint-1"
    assert client.post("/api/settings", json={"audio_device_id": "bad\nendpoint"}).status_code == 422

    report = client.get("/api/diagnostics/export").json()
    assert report["format"] == "zak_light_diagnostics" and report["configuration"]["audio_device_selected"] is True
    assert "data_dir" not in report and "log_file" not in report and "led_points" not in report["configuration"]


def test_monitor_color_profile_and_hdr_compensation_apply_to_the_live_pipeline(client, state):
    assert client.post("/api/settings", json={"gamma": 2.6, "hdr_compensation": 0.45}).status_code == 200
    assert client.post("/api/monitor_color_profile", json={"action": "save"}).json()["saved"] is True
    assert client.post("/api/settings", json={"gamma": 1.4, "hdr_compensation": 0.0}).status_code == 200
    assert client.post("/api/monitor_color_profile", json={"action": "load"}).status_code == 200
    assert state.config.gamma == 2.6 and state.config.hdr_compensation == 0.45
    assert state.color_pipeline.gamma == 2.6 and state.color_pipeline.hdr_compensation == 0.45

    assert client.post("/api/settings", json={"monitor_index": 1, "gamma": 1.2}).status_code == 200
    assert client.post("/api/monitor_color_profile", json={"action": "save"}).status_code == 200
    assert client.post("/api/settings", json={"monitor_index": 0}).status_code == 200
    assert state.config.gamma == 2.6  # automatic restore for the previously calibrated monitor


def test_performance_measurement_api_exports_an_anonymous_report(client, state):
    started = client.post("/api/performance_measurement", json={"action": "start", "scenario": "desktop"})
    assert started.status_code == 200 and started.json()["measurement"]["active"] is True
    state.performance_recorder.record(state, force=True)
    stopped = client.post("/api/performance_measurement", json={"action": "stop"})
    report = stopped.json()["report"]
    assert report["format"] == "zak_light_performance_measurement" and report["samples"]
    assert "colors" not in report and "led_points" not in report
    assert client.get("/api/performance_measurement/export").json()["scenario"] == "desktop"
    assert client.post("/api/performance_measurement", json={"action": "start", "scenario": "unknown"}).status_code == 422


# ---- effects ----------------------------------------------------------------------------------------------------

def test_the_effect_catalogue_is_served_and_complete(client):
    data = client.get("/api/effects").json()
    assert [e["key"] for e in data["effects"]] == list(EFFECT_KEYS)
    assert {e["group"] for e in data["effects"]} == {"ambient", "show"}
    assert all(len(e["colors"]) == 3 and len(e["defaults"]) == 3 and len(e["roles"]) == 3 for e in data["effects"])


@pytest.mark.parametrize("effect", EFFECT_KEYS)
def test_every_effect_can_be_selected(client, state, effect):
    assert client.post("/api/effect", json={"effect": effect, "speed": 1.5}).status_code == 200
    assert state.active_effect == effect and state.mode == "effect"


def test_effect_colors_direction_and_mirror_are_stored(client, state):
    body = {"effect": "comet", "speed": 1.0, "colors": [[255, 0, 0], [0, 255, 0], [0, 0, 255]], "reverse": True, "mirror": True}
    assert client.post("/api/effect", json=body).status_code == 200
    assert state.config.palette_for("comet") == [[255, 0, 0], [0, 255, 0], [0, 0, 255]]
    assert state.config.effect_reverse is True and state.config.effect_mirror is True
    # omitting them keeps what was chosen, and another effect keeps its own colours
    client.post("/api/effect", json={"effect": "aurora"})
    assert state.config.effect_reverse is True
    assert state.config.palette_for("aurora") == [[16, 185, 129], [59, 130, 246], [168, 85, 247]]
    assert state.config.palette_for("comet") == [[255, 0, 0], [0, 255, 0], [0, 0, 255]]


def test_each_effect_starts_with_its_own_colours_and_can_go_back_to_them(client, state):
    assert state.config.palette_for("police")[:2] == [[255, 0, 0], [0, 50, 255]]       # a police light is red and blue
    assert state.config.palette_for("scanner")[0] == [255, 0, 0]
    client.post("/api/effect", json={"effect": "police", "colors": [[0, 255, 0], [0, 255, 255], [9, 9, 9]]})
    data = client.get("/api/effects").json()
    police = next(e for e in data["effects"] if e["key"] == "police")
    assert police["colors"][0] == [0, 255, 0] and police["defaults"][0] == [255, 0, 0] and len(police["roles"]) == 3
    client.post("/api/effect", json={"effect": "police", "reset_colors": True})
    assert state.config.palette_for("police")[:2] == [[255, 0, 0], [0, 50, 255]] and "police" not in state.config.effect_palettes
    assert client.get("/api/status").json()["config"]["effect_colors"][0] == [255, 0, 0]


@pytest.mark.parametrize("colors", [[[1, 2, 3]], [[1, 2], [3, 4], [5, 6]], [[300, 0, 0], [0, 0, 0], [0, 0, 0]], [[-1, 0, 0]] * 3])
def test_bad_effect_palettes_are_rejected(client, state, colors):
    before = dict(state.config.effect_palettes)
    assert client.post("/api/effect", json={"effect": "aurora", "colors": colors}).status_code == 422
    assert state.config.effect_palettes == before


# ---- automation ---------------------------------------------------------------------------------------------------

def test_rules_and_schedules_are_stored(client, state):
    body = {"app_rules_enabled": True,
            "app_rules": [{"exe": "Game.exe", "profile": "Juego", "mode": "screen"}],
            "schedules": [{"time": "22:30", "days": [4, 5, 5, 6], "action": "off", "value": ""}]}
    r = client.post("/api/automation", json=body)
    assert r.status_code == 200
    assert state.config.app_rules == [{"exe": "game.exe", "profile": "Juego", "mode": "screen"}]     # lower-cased
    assert state.config.schedules[0]["days"] == [4, 5, 6]                                              # sorted, deduplicated
    data = client.get("/api/automation").json()
    assert data["app_rules_enabled"] is True and len(data["schedules"]) == 1


@pytest.mark.parametrize("bad", [
    {"app_rules": [{"exe": "../evil.exe"}]}, {"app_rules": [{"exe": ""}]}, {"app_rules": [{"exe": "a.exe", "mode": "hack"}]},
    {"app_rules": [{"exe": "a.exe"}] * 31},
    {"schedules": [{"time": "25:00", "days": [1], "action": "off"}]}, {"schedules": [{"time": "7:5", "days": [1], "action": "off"}]},
    {"schedules": [{"time": "10:00", "days": [], "action": "off"}]}, {"schedules": [{"time": "10:00", "days": [9], "action": "off"}]},
    {"schedules": [{"time": "10:00", "days": [1], "action": "format_disk"}]},
])
def test_bad_automation_is_rejected_and_nothing_changes(client, state, bad):
    assert client.post("/api/automation", json=bad).status_code == 422
    assert state.config.app_rules == [] and state.config.schedules == []


def test_open_apps_are_listed(client, monkeypatch):
    monkeypatch.setattr("core.windows_info.open_apps", lambda: [{"exe": "game.exe", "title": "A Game"}])
    assert client.get("/api/windows").json() == {"apps": [{"exe": "game.exe", "title": "A Game"}]}


# ---- setup wizard ---------------------------------------------------------------------------------------------------

def test_channel_test_sends_one_raw_channel(client, state):
    for idx, expected in ((0, (255, 0, 0)), (1, (0, 255, 0)), (2, (0, 0, 255))):
        client.post("/api/calibrate_action", json={"action": "test_channel", "index": idx})
        seg = state.driver.sent[-1][0]
        assert seg[1:4] == expected and seg[0] == 1 and seg[4] == state.config.led_count   # no wiring applied


def test_single_led_test_lights_exactly_one_led(client, state):
    n = state.config.led_count
    for index in (0, 5, n - 1):
        client.post("/api/calibrate_action", json={"action": "test_led", "index": index})
        segs = state.driver.sent[-1]
        lit = [s for s in segs if max(s[1:4]) > 0]
        assert len(lit) == 1 and lit[0][0] == lit[0][4] == index + 1
        covered = sum(s[4] - s[0] + 1 for s in segs)
        assert covered == n                                              # every LED is told what to do


def test_led_test_index_is_bounded(client):
    assert client.post("/api/calibrate_action", json={"action": "test_led", "index": 9999}).status_code == 422
    assert client.post("/api/calibrate_action", json={"action": "test_channel", "index": -1}).status_code == 422


# ---- backup -------------------------------------------------------------------------------------------------------

def test_export_then_import_restores_everything(client, state):
    state.config.brightness = 0.33
    state.config.schedules = [{"time": "07:00", "days": [1], "action": "off", "value": ""}]
    client.post("/api/profiles", json={"name": "Cine"})
    backup = client.get("/api/export").json()
    assert backup["format"] == "zak_light_backup" and "Cine" in backup["profiles"]

    state.config.brightness = 0.9
    state.config.schedules = []
    client.post("/api/profiles/delete", json={"name": "Cine"})
    r = client.post("/api/import", json={"config": backup["config"], "profiles": backup["profiles"]})
    assert r.status_code == 200 and r.json()["config"] is True and r.json()["profiles"] == 1
    assert state.config.brightness == 0.33 and len(state.config.schedules) == 1
    assert state.color_pipeline.brightness == pytest.approx(0.33)             # applied to the running engine
    assert client.get("/api/profiles").json()["profiles"] == ["Cine"]


def test_a_hostile_backup_is_sanitised_not_trusted(client, state):
    hostile = {"config": {"brightness": 99, "gamma": "x", "led_count": 10 ** 9, "wire_map": "evil", "current_mode": "calibrate",
                          "app_rules": [{"exe": "../x"}], "schedules": "nope", "unknown_field": 1, "edges_enabled": [1]},
               "profiles": {"../escape": {"brightness": 0.1}, "Bueno": {"brightness": 7, "rhythm_palette": "bad"}, "x" * 50: {}}}
    assert client.post("/api/import", json=hostile).status_code == 200
    cfg = state.config
    assert cfg.brightness == 1.0 and cfg.led_count == 254 and cfg.wire_map == "RGB" and cfg.current_mode == "screen"
    assert cfg.app_rules == [] and cfg.schedules == [] and not hasattr(cfg, "unknown_field")
    assert state.profiles.names() == ["Bueno"]                                # bad names dropped, values clamped
    assert state.profiles.export_all()["Bueno"]["brightness"] == 1.0


def test_importing_too_many_profiles_is_refused(client, state):
    many = {f"p{i}": {} for i in range(40)}
    assert client.post("/api/import", json={"profiles": many}).status_code == 400
    assert state.profiles.names() == []


def test_huge_requests_are_refused_before_being_parsed(client):
    body = json.dumps({"profiles": {"a": {"x": "y" * 1_100_000}}})
    r = client.post("/api/import", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_import_needs_a_local_json_request(client):
    assert client.post("/api/import", json={}, headers={"Origin": "http://evil.com"}).status_code == 403
    assert client.post("/api/import", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415


# ---- window auto-close ---------------------------------------------------------------------------------------------

def test_window_autoclose_is_stored_and_bounded(client, state):
    assert client.post("/api/startup", json={"ui_autoclose_minutes": 20}).status_code == 200
    assert state.config.ui_autoclose_minutes == 20 and client.get("/api/startup").json()["ui_autoclose_minutes"] == 20
    assert client.post("/api/startup", json={"ui_autoclose_minutes": -1}).status_code == 422
    assert client.post("/api/startup", json={"ui_autoclose_minutes": 99999}).status_code == 422
