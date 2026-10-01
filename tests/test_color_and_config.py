import json
import math

import numpy as np

import core.config as cfgmod
from core.color import ColorPipeline
from core.config import AppConfig, load_config, save_config


# ---- color pipeline ----------------------------------------------------------------------------

def test_single_color_is_clamped_not_wrapped():
    p = ColorPipeline(wire_map="RGB", brightness=1.0)
    assert p.map_single_color(300, -5, 10) == (255, 0, 0)   # was IndexError / wrapped to 248


def test_wire_map_reorders_channels():
    arr = ColorPipeline(wire_map="BGR", gamma=1.0, saturation=1.0, brightness=1.0, smoothing=1.0, min_luminance_floor=0)
    out = arr.process_colors_array(np.array([[10, 20, 30]], dtype=np.float32))
    assert out.tolist() == [[30, 20, 10]]


def test_gamma_zero_no_longer_crashes():
    assert ColorPipeline(gamma=0.0).gamma == 1.0


def test_brightness_scales_and_rounds():
    p = ColorPipeline(wire_map="RGB", gamma=1.0, saturation=1.0, brightness=0.5, smoothing=1.0, min_luminance_floor=0)
    assert p.process_colors_array(np.array([[255, 100, 3]], dtype=np.float32)).tolist() == [[128, 50, 2]]


def test_soft_floor_keeps_hue_of_dim_colors():
    p = ColorPipeline(wire_map="RGB", gamma=1.0, saturation=1.0, brightness=1.0, smoothing=1.0, min_luminance_floor=4)
    out = p.process_colors_array(np.array([[2, 1, 0], [0, 0, 0]], dtype=np.float32))
    assert out.tolist() == [[4, 2, 0], [0, 0, 0]]            # lifted proportionally; true black stays black


def test_min_floor_can_be_changed_at_runtime():
    p = ColorPipeline(wire_map="RGB", gamma=1.0, saturation=1.0, brightness=1.0, smoothing=1.0, min_luminance_floor=0)
    p.update_settings(min_floor=4)
    assert p.process_colors_array(np.array([[2, 0, 0]], dtype=np.float32)).tolist() == [[4, 0, 0]]


def test_hdr_compensation_is_opt_in_and_preserves_white():
    raw = np.array([[64, 64, 64], [255, 255, 255]], dtype=np.float32)
    plain = ColorPipeline(gamma=1.0, saturation=1.0, brightness=1.0, smoothing=1.0, min_luminance_floor=0)
    compensated = ColorPipeline(gamma=1.0, saturation=1.0, brightness=1.0, smoothing=1.0,
                                min_luminance_floor=0, hdr_compensation=1.0)
    assert plain.process_colors_array(raw).tolist() == [[64, 64, 64], [255, 255, 255]]
    out = compensated.process_colors_array(raw)
    assert out[0, 0] > 64 and out[1].tolist() == [255, 255, 255]


def test_smoothing_is_frame_rate_independent():
    def settle(dt, steps):
        p = ColorPipeline(smoothing=0.3)
        p.process_colors_array(np.zeros((1, 3), dtype=np.float32))
        for _ in range(steps):
            p.process_colors_array(np.full((1, 3), 255, dtype=np.float32), dt=dt)
        return p._prev_colors[0, 0]

    # the same 100 ms of real time at 40 FPS and at 80 FPS
    assert math.isclose(settle(1 / 40, 4), settle(1 / 80, 8), rel_tol=1e-3)


def test_settled_flag_reports_convergence():
    p = ColorPipeline(smoothing=0.5)
    target = np.full((2, 3), 200, dtype=np.float32)
    p.process_colors_array(np.zeros((2, 3), dtype=np.float32))
    p.process_colors_array(target)
    assert p.settled is False
    for _ in range(40):
        p.process_colors_array(target)
    assert p.settled is True


# ---- configuration -----------------------------------------------------------------------------

def test_hostile_values_are_sanitised():
    cfg = AppConfig(gamma=0, led_count=10**6, brightness="x", wire_map="zzz", led_points=[[2, -1], [0.5]],
                    static_color=[999, -3], current_mode="weird", target_fps=500, monitor_index=99)
    assert cfg.gamma == 1.0 and cfg.led_count == 254 and cfg.brightness == 0.85
    assert cfg.wire_map == "RGB" and cfg.current_mode == "screen" and cfg.target_fps == 60
    assert cfg.static_color == [255, 180, 100] and cfg.monitor_index == 7
    assert len(cfg.led_points) == 254 and all(0 <= c <= 1 for p in cfg.led_points for c in p)


def test_nan_is_rejected(config):
    cfg = AppConfig(brightness=float("nan"), saturation=float("inf"))
    assert cfg.brightness == 0.85 and cfg.saturation == 1.3    # non-finite numbers fall back to the defaults


def test_save_is_atomic_and_round_trips(config, tmp_path):
    cfg = AppConfig(brightness=0.4)
    save_config(cfg)
    assert not (tmp_path / "config.json.tmp").exists()
    assert load_config().brightness == 0.4


def test_corrupt_file_falls_back_to_the_last_good_copy(config, tmp_path):
    save_config(AppConfig(brightness=0.4))
    load_config()                                            # remembers a good copy (.bak)
    (tmp_path / "config.json").write_text("{ this is not json", encoding="utf-8")
    assert load_config().brightness == 0.4                   # restored from .bak, not reset to defaults
    assert (tmp_path / "config.json.corrupt").exists()       # the damaged file is kept for inspection
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["brightness"] == 0.4


def test_corrupt_file_without_backup_uses_defaults(config, tmp_path):
    (tmp_path / "config.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_config().brightness == AppConfig().brightness


def test_unknown_fields_in_the_file_are_ignored(config, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"brightness": 0.3, "from_the_future": 1}), encoding="utf-8")
    assert load_config().brightness == 0.3


def test_reset_keeps_hardware_calibration():
    cfg = AppConfig(wire_map="GRB", leds_left=5, leds_top=6, leds_right=7, led_count=18, brightness=0.2,
                    white_balance=[0.5, 0.6, 0.7], hdr_compensation=0.8)
    cfg.save_monitor_color_profile()
    cfgmod.reset_tuning_defaults(cfg)
    assert cfg.brightness == 0.85 and cfg.wire_map == "GRB" and cfg.leds_top == 6
    assert cfg.white_balance == [1.0, 1.0, 1.0] and cfg.hdr_compensation == 0.0
    assert not cfg.has_monitor_color_profile()


def test_performance_mode_is_sanitised():
    assert AppConfig(performance_mode="eco").performance_mode == "eco"
    assert AppConfig(performance_mode="not-a-mode").performance_mode == "custom"


def test_audio_device_id_is_an_opaque_but_bounded_value():
    assert AppConfig(audio_device_id="endpoint-1").audio_device_id == "endpoint-1"
    assert AppConfig(audio_device_id="endpoint\n-1").audio_device_id == ""
    assert AppConfig(audio_device_id="x" * 600).audio_device_id == "x" * 512


def test_monitor_color_profile_is_sanitised_and_restored_on_load():
    cfg = AppConfig(monitor_index=1, brightness=0.1, monitor_color_profiles={"1": {
        "brightness": 0.6, "gamma": 2.5, "saturation": 1.7, "smoothing": 0.4, "min_luminance_floor": 3,
        "white_balance": [1.0, 0.8, 0.7], "dark_light": 0.1, "hdr_compensation": 0.5,
    }})
    assert cfg.restore_monitor_color_profile()
    assert cfg.brightness == 0.6 and cfg.gamma == 2.5 and cfg.white_balance == [1.0, 0.8, 0.7]
    assert cfg.hdr_compensation == 0.5 and cfg.has_monitor_color_profile()
