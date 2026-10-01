import pytest
from fastapi.testclient import TestClient

from api import create_app


# ---- who may talk to the API -------------------------------------------------------------------

def test_status_and_index_work_for_local_hosts(client):
    assert client.get("/api/status").status_code == 200
    assert client.get("/").status_code == 200


@pytest.mark.parametrize("host", ["evil.com", "evil.com:8765", "192.168.1.5:8765", "127.0.0.1.evil.com"])
def test_dns_rebinding_hosts_are_rejected(state, tmp_path, host):
    web = tmp_path / "w"
    web.mkdir()
    (web / "index.html").write_text("x", encoding="utf-8")
    c = TestClient(create_app(state, str(web)), base_url=f"http://{host}")
    assert c.get("/api/status").status_code == 403


@pytest.mark.parametrize("origin", ["http://evil.com", "https://evil.com:8765", "null"])
def test_foreign_origins_cannot_change_anything(client, state, origin):
    before = state.mode
    r = client.post("/api/mode", json={"mode": "off"}, headers={"Origin": origin})
    assert r.status_code == 403 and state.mode == before


def test_own_origin_is_accepted(client):
    r = client.post("/api/mode", json={"mode": "static"}, headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


def test_cross_site_fetch_metadata_is_rejected(client):
    r = client.post("/api/mode", json={"mode": "off"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_form_encoded_and_plain_text_bodies_are_rejected(client, state):
    before = state.mode
    assert client.post("/api/mode", content="mode=off",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/api/mode", content='{"mode":"off"}',
                       headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/mode", content='{"mode":"off"}').status_code == 415   # no type at all
    assert state.mode == before


def test_bodyless_post_still_works(client):
    assert client.post("/api/reset_defaults").status_code == 200


# ---- validation --------------------------------------------------------------------------------

def test_mode_must_be_a_known_mode(client, state):
    assert client.post("/api/mode", json={"mode": "hack"}).status_code == 422
    assert client.post("/api/mode", json={"mode": "OFF"}).status_code == 200 and state.mode == "off"


def test_calibrate_mode_is_not_persisted(client, state):
    client.post("/api/mode", json={"mode": "static"})
    client.post("/api/mode", json={"mode": "calibrate"})
    assert state.mode == "calibrate" and state.config.current_mode == "static"


@pytest.mark.parametrize("rgb", [{"r": 300, "g": 0, "b": 0}, {"r": -1, "g": 0, "b": 0}, {"r": "x", "g": 0, "b": 0}, {"r": 1}])
def test_static_color_rejects_bad_values_and_keeps_state(client, state, rgb):
    before = list(state.static_color_rgb)
    assert client.post("/api/static_color", json=rgb).status_code == 422
    assert state.static_color_rgb == before


def test_static_color_paints_the_strip(client, state):
    assert client.post("/api/static_color", json={"r": 255, "g": 0, "b": 0}).status_code == 200
    assert state.mode == "static" and state.driver.sent


def test_settings_reject_out_of_range_and_unknown(client, state):
    assert client.post("/api/settings", json={"brightness": 5}).status_code == 422
    assert client.post("/api/settings", json={"gamma": "abc"}).status_code == 422
    assert client.post("/api/settings", json={"target_fps": 500}).status_code == 422
    assert client.post("/api/settings", json={"wire_map": "XYZ"}).status_code == 422
    assert client.post("/api/settings", json={"evil_key": 1, "brightness": 0.5}).status_code == 200
    assert state.config.brightness == 0.5 and not hasattr(state.config, "evil_key")


def test_settings_reach_the_running_pipeline(client, state):
    client.post("/api/settings", json={"min_luminance_floor": 9, "wire_map": "grb", "smoothing": 0.9})
    assert state.color_pipeline.min_floor == 9            # was never propagated before
    assert state.color_pipeline.wire_map == "GRB" and state.color_pipeline.smoothing == 0.9


def test_settings_do_not_reopen_the_monitor_when_unchanged(client, state):
    state.screen_sampler.switch_monitor = lambda idx: (_ for _ in ()).throw(AssertionError("reopened"))
    assert client.post("/api/settings", json={"brightness": 0.6}).status_code == 200


def test_led_points_validation(client, state):
    good = [[0.1, 0.2], [0.3, 0.4]]
    assert client.post("/api/led_points", json={"points": good, "box_size": 0.1}).status_code == 200
    assert state.config.led_count == 2 and state.config.sample_box_size == 0.1

    bad = [
        {"points": []},
        {"points": [[0.1]]},
        {"points": [[0.1, 2.0]]},
        {"points": [[0.1, -0.1]]},
        {"points": [["a", "b"]]},
        {"points": [[0.5, 0.5]] * 255},
        {"box_size": 5},
    ]
    for payload in bad:
        assert client.post("/api/led_points", json=payload).status_code == 422, payload
    assert state.config.led_count == 2                     # nothing above changed the state


def test_edge_led_counts_are_capped(client, state):
    assert client.post("/api/set_edge_leds", json={"leds_left": 10 ** 8}).status_code == 422
    assert client.post("/api/set_edge_leds", json={"leds_left": 200, "leds_top": 100}).status_code == 400
    assert client.post("/api/set_edge_leds", json={"leds_left": 0, "leds_top": 0, "leds_right": 0, "leds_bottom": 0}).status_code == 400
    r = client.post("/api/set_edge_leds", json={"leds_left": 10, "leds_top": 20, "leds_right": 10, "leds_bottom": 0})
    assert r.status_code == 200 and r.json()["led_count"] == 40 and len(state.config.led_points) == 40


def test_effect_palette_preset_and_calibration_use_allow_lists(client, state):
    assert client.post("/api/effect", json={"effect": "nope"}).status_code == 422
    for raw in ('{"effect":"fire","speed":Infinity}', '{"effect":"fire","speed":NaN}'):
        assert client.post("/api/effect", content=raw,
                           headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post("/api/effect", json={"effect": "fire", "speed": 99}).status_code == 422
    assert client.post("/api/effect", json={"effect": "fire", "speed": 2}).status_code == 200
    assert state.active_effect == "fire" and state.effect_speed == 2

    assert client.post("/api/palette", json={"palette": "nope"}).status_code == 422
    assert client.post("/api/preset", json={"preset": "nope"}).status_code == 422
    state.config.target_fps = 20
    state.config.performance_mode = "eco"
    assert client.post("/api/preset", json={"preset": "game"}).status_code == 200
    assert state.config.target_fps == 20 and state.config.performance_mode == "eco"
    assert state.config.black_bar_detection is False

    before = state.mode
    assert client.post("/api/calibrate_action", json={"action": "nope"}).status_code == 422
    assert state.mode == before                             # an unknown action must not freeze the LEDs
    assert client.post("/api/calibrate_action", json={"action": "test_red"}).status_code == 200
    assert state.mode == "calibrate"
    assert client.post("/api/calibrate_action", json={"action": "stop"}).status_code == 200
    assert state.mode != "calibrate"


def test_auto_layout_rejects_unknown_modes(client):
    assert client.post("/api/auto_layout", json={"mode": "spiral"}).status_code == 422
    assert client.post("/api/auto_layout", json={"mode": "4_sides"}).status_code == 200


def test_status_does_not_ship_the_led_points_but_has_their_own_endpoint(client, state):
    assert "led_points" not in client.get("/api/status").json()["config"]
    assert len(client.get("/api/led_points").json()["led_points"]) == len(state.config.led_points)


def test_status_reports_connection_and_version(client, state):
    data = client.get("/api/status").json()
    assert data["connected"] is True and data["version"]
    state.driver.is_connected = False
    assert client.get("/api/status").json()["connected"] is False


def test_brightness_test_is_cancelled_by_a_new_action(client, state):
    client.post("/api/calibrate_action", json={"action": "test_brightness_cycle"})
    token = state._calibration_token
    client.post("/api/mode", json={"mode": "off"})
    assert state._calibration_token != token               # the running sequence is stale and stops itself


def test_quit_endpoint_is_only_available_when_wired_and_only_to_local_pages(state, tmp_path):
    web = tmp_path / "w2"
    web.mkdir()
    (web / "index.html").write_text("x", encoding="utf-8")

    plain = TestClient(create_app(state, str(web)), base_url="http://127.0.0.1:8765")
    assert plain.post("/api/quit", json={}).status_code == 404

    called = []
    wired = TestClient(create_app(state, str(web), on_quit=lambda: called.append(1)), base_url="http://127.0.0.1:8765")
    assert wired.post("/api/quit", json={}, headers={"Origin": "http://evil.com"}).status_code == 403
    assert wired.post("/api/quit", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert called == []                                        # a web page cannot shut the program down
    assert wired.post("/api/quit", json={}).status_code == 200
    import time
    time.sleep(0.2)
    assert called == [1]


# ---- a competing program (the original DX Light) -----------------------------------------------

def test_status_reports_a_competing_program(client, monkeypatch):
    monkeypatch.setattr("core.conflicts.active_conflicts",
                        lambda: [{"name": "DX Light", "processes": 5, "autostart": True}])
    assert client.get("/api/status").json()["conflicts"][0]["name"] == "DX Light"


def test_conflict_remedies_take_no_input_and_need_a_local_json_request(client, monkeypatch):
    closed, disabled = [], []
    monkeypatch.setattr("core.conflicts.close_conflicts", lambda: closed.append(1) or ["DX Light"])
    monkeypatch.setattr("core.conflicts.disable_conflict_autostart", lambda: disabled.append(1) or ["DX Light"])

    assert client.post("/api/conflicts/close", headers={"Origin": "http://evil.com"}).status_code == 403
    assert client.post("/api/conflicts/disable_autostart", content="x",
                       headers={"Content-Type": "text/plain"}).status_code == 415
    assert closed == [] and disabled == []

    assert client.post("/api/conflicts/close", json={"name": "explorer.exe"}).json()["closed"] == ["DX Light"]
    assert client.post("/api/conflicts/disable_autostart").json()["disabled"] == ["DX Light"]
    assert closed == [1] and disabled == [1]


def test_close_only_targets_the_fixed_list(monkeypatch):
    import core.conflicts as c
    monkeypatch.undo()
    assert [a["process"] for a in c.KNOWN_CONFLICTS] == ["DX Light.exe"]


# ---- redesign support: live preview and simple zone controls -----------------------------------

def test_leds_endpoint_follows_the_mode(client, state):
    state.static_color_rgb = [200, 100, 50]
    state.config.brightness = 0.5
    state.set_mode("static")
    data = client.get("/api/leds").json()
    assert data["known"] and data["colors"][0] == [100, 50, 25]          # color x brightness
    assert len(data["colors"]) == len(state.config.led_points)

    state.set_mode("off")
    assert set(map(tuple, client.get("/api/leds").json()["colors"])) == {(0, 0, 0)}

    state.set_mode("calibrate")
    assert client.get("/api/leds").json()["known"] is False               # tests paint their own colors

    state.set_mode("screen")
    import numpy as np
    state.color_pipeline.process_colors_array(np.full((len(state.config.led_points), 3), 120, dtype=np.float32))
    assert client.get("/api/leds").json()["known"] is True


def test_leds_are_dark_while_the_session_is_locked(client, state):
    state.set_mode("static")
    state.set_suspended(True)
    assert set(map(tuple, client.get("/api/leds").json()["colors"])) == {(0, 0, 0)}


def test_zones_regenerate_points_from_simple_controls(client, state):
    r = client.post("/api/zones", json={"leds_left": 10, "leds_top": 20, "leds_right": 10, "leds_bottom": 0,
                                        "margin": 0.1, "box_size": 0.08})
    assert r.status_code == 200 and r.json()["led_count"] == 40
    cfg = state.config
    assert len(cfg.led_points) == 40 and cfg.edge_margin == 0.1 and cfg.sample_box_size == 0.08
    assert abs(cfg.led_points[0][0] - 0.1) < 1e-9                              # left edge sits at the margin
    assert client.get("/api/status").json()["config"]["zones_customized"] is False


def test_dragging_points_marks_the_zones_as_customised(client):
    client.post("/api/zones", json={"leds_left": 2, "leds_top": 2, "leds_right": 2, "leds_bottom": 0})
    assert client.get("/api/status").json()["config"]["zones_customized"] is False
    pts = client.get("/api/led_points").json()["led_points"]
    pts[0] = [0.5, 0.5]
    client.post("/api/led_points", json={"points": pts})
    assert client.get("/api/status").json()["config"]["zones_customized"] is True
    client.post("/api/zones", json={"leds_left": 2, "leds_top": 2, "leds_right": 2, "leds_bottom": 0})
    assert client.get("/api/status").json()["config"]["zones_customized"] is False   # automatic layout restored


@pytest.mark.parametrize("body", [
    {"leds_left": 0, "leds_top": 0, "leds_right": 0, "leds_bottom": 0},
    {"leds_left": 200, "leds_top": 100, "leds_right": 0, "leds_bottom": 0},
    {"leds_left": 5, "leds_top": 5, "leds_right": 5, "leds_bottom": 0, "margin": 0.9},
    {"leds_left": 5, "leds_top": 5, "leds_right": 5, "leds_bottom": 0, "box_size": 5},
    {"leds_left": -1, "leds_top": 5, "leds_right": 5, "leds_bottom": 0},
    {"leds_left": 5},
])
def test_zones_reject_bad_values_without_changing_anything(client, state, body):
    before = list(state.config.led_points)
    assert client.post("/api/zones", json=body).status_code in (400, 422)
    assert state.config.led_points == before
