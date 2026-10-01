import json

import pytest

from core.config import AppConfig
from core.profiles import ProfileError, ProfileStore, PROFILE_FIELDS


@pytest.fixture
def store(tmp_path):
    return ProfileStore(str(tmp_path / "profiles.json"))


def test_save_load_and_delete_round_trip(store):
    cfg = AppConfig(brightness=0.3, rhythm_palette="fire", effect_speed=2.0, static_color=[1, 2, 3])
    store.save("Noche", cfg)
    assert store.names() == ["Noche"]

    other = AppConfig()
    store.apply("Noche", other)
    assert (other.brightness, other.rhythm_palette, other.effect_speed, other.static_color) == (0.3, "fire", 2.0, [1, 2, 3])

    assert store.delete("Noche") and store.names() == [] and not store.delete("Noche")


def test_a_profile_never_touches_the_hardware_calibration(store):
    store.save("p", AppConfig(brightness=0.2, wire_map="GRB", leds_left=3, monitor_index=1))
    cfg = AppConfig(wire_map="BGR", leds_left=17, monitor_index=0)
    led_points = list(cfg.led_points)
    store.apply("p", cfg)
    assert cfg.brightness == 0.2
    assert (cfg.wire_map, cfg.leds_left, cfg.monitor_index, cfg.led_points) == ("BGR", 17, 0, led_points)
    assert "wire_map" not in PROFILE_FIELDS and "led_points" not in PROFILE_FIELDS


def test_edited_profile_files_are_sanitised_on_load(store, tmp_path):
    (tmp_path / "profiles.json").write_text(json.dumps({"x": {"brightness": 99, "gamma": "no", "target_fps": -5,
                                                               "rhythm_palette": "evil", "static_color": [999]}}), encoding="utf-8")
    cfg = AppConfig()
    store.apply("x", cfg)
    assert cfg.brightness == 1.0 and cfg.gamma == 2.2 and cfg.target_fps == 15
    assert cfg.rhythm_palette == "cyberpunk" and cfg.static_color == [255, 180, 100]


@pytest.mark.parametrize("name", ["", " x", "../etc", "a" * 41, "a/b", "a\\b", "x\n", "<script>"])
def test_bad_names_are_rejected(store, name):
    with pytest.raises(ProfileError):
        store.save(name, AppConfig())


def test_profile_count_is_capped(store):
    for i in range(30):
        store.save(f"p{i}", AppConfig())
    with pytest.raises(ProfileError):
        store.save("one-too-many", AppConfig())
    store.save("p3", AppConfig(brightness=0.1))   # overwriting an existing one is always allowed


def test_damaged_profiles_file_does_not_crash(store, tmp_path):
    (tmp_path / "profiles.json").write_text("{nope", encoding="utf-8")
    assert store.names() == []
    store.save("fresh", AppConfig())
    assert store.names() == ["fresh"]


# ---- through the API ---------------------------------------------------------------------------

def test_profiles_api_flow(client, state):
    state.config.brightness = 0.4
    assert client.post("/api/profiles", json={"name": "Cine"}).json()["profiles"] == ["Cine"]
    state.config.brightness = 0.9
    assert client.post("/api/profiles/load", json={"name": "Cine"}).status_code == 200
    assert state.config.brightness == 0.4 and state.color_pipeline.brightness == 0.4   # applied to the live pipeline
    assert client.get("/api/profiles").json() == {"profiles": ["Cine"], "active": "Cine"}
    assert client.post("/api/profiles/delete", json={"name": "Cine"}).status_code == 200
    assert client.post("/api/profiles/load", json={"name": "Cine"}).status_code == 404


@pytest.mark.parametrize("name", ["../x", "a/b", "", "x" * 41])
def test_profiles_api_rejects_bad_names(client, name):
    assert client.post("/api/profiles", json={"name": name}).status_code == 422


def test_monitors_endpoint(client, state):
    data = client.get("/api/monitors").json()
    assert [m["index"] for m in data["monitors"]] == [0, 1]
    assert "principal" in data["monitors"][0]["label"] and data["selected"] == 0


def test_open_data_dir_takes_no_path_from_the_request(client, monkeypatch):
    opened = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(path), raising=False)
    assert client.post("/api/open_data_dir", json={"path": "C:\\Windows"}).status_code == 200
    assert len(opened) == 1 and "Windows" not in opened[0]


# ---- which profile is active ----------------------------------------------------------------------------------------

def test_the_active_profile_is_the_one_whose_values_are_in_use(state):
    state.config.brightness = 0.4
    state.profiles.save("Noche", state.config)
    state.config.brightness = 0.9
    state.profiles.save("Dia", state.config)
    assert state.profiles.matching(state.config) == "Dia"
    state.config.brightness = 0.4
    assert state.profiles.matching(state.config) == "Noche"
    state.config.brightness = 0.55                       # moved a slider: no longer any saved profile
    assert state.profiles.matching(state.config) is None


def test_active_profile_is_reported_by_the_api(client, state):
    client.post("/api/profiles", json={"name": "Cine"})
    assert client.get("/api/profiles").json()["active"] == "Cine"
    assert client.get("/api/status").json()["active_profile"] == "Cine"
    client.post("/api/settings", json={"saturation": 2.2})
    assert client.get("/api/status").json()["active_profile"] is None
