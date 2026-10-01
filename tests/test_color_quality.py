"""Light-quality features of the color pipeline: each one is checked against numbers, not by eye."""
import numpy as np
import pytest

from core.color import ColorPipeline


def pipe(**kw):
    base = dict(wire_map="RGB", gamma=1.0, saturation=1.0, brightness=1.0, smoothing=1.0, min_luminance_floor=0)
    base.update(kw)
    return ColorPipeline(**base)


def frame(value, n=10):
    return np.full((n, 3), value, dtype=np.float32)


# ---- dithering -----------------------------------------------------------------------------------

def test_dithering_reproduces_fractional_levels_on_average():
    p = pipe(dithering=True)
    outs = [p.process_colors_array(frame(10.3))[0, 0] for _ in range(400)]
    assert abs(np.mean(outs) - 10.3) < 0.03
    assert set(outs) <= {10, 11}                          # only the two neighbouring levels are used


def test_without_dithering_the_level_is_rounded():
    p = pipe(dithering=False)
    assert {int(p.process_colors_array(frame(10.3))[0, 0]) for _ in range(20)} == {10}


def test_dithering_hides_banding_in_a_dark_gradient_after_gamma():
    """With gamma, a slow fade between two dark values must not jump in whole steps of the 8-bit output."""
    p = pipe(dithering=True, gamma=2.2)
    means = []
    for v in np.linspace(30, 34, 5):
        outs = [p.process_colors_array(frame(v))[0, 0] for _ in range(300)]
        means.append(np.mean(outs))
    steps = np.diff(means)
    assert np.all(steps > 0) and steps.max() < 0.6          # sub-level progression, no 1-level jumps


# ---- scene cuts ---------------------------------------------------------------------------------

def test_adaptive_smoothing_follows_a_scene_cut_but_not_small_drift():
    def first_step(adaptive, jump):
        p = pipe(smoothing=0.1, adaptive_smoothing=adaptive)
        p.process_colors_array(frame(100))
        return float(p.process_colors_array(frame(100 + jump))[0, 0]) - 100

    assert first_step(False, 200) == pytest.approx(20, abs=1)          # fixed smoothing: 10 % of the jump
    assert first_step(True, 150) > 100                                  # cut: nearly all of it at once
    assert first_step(True, 8) == pytest.approx(first_step(False, 8), abs=0.6)  # drift: unchanged


def test_adaptive_smoothing_is_still_frame_rate_independent():
    def settle(dt, steps):
        p = pipe(smoothing=0.2, adaptive_smoothing=True)
        p.process_colors_array(frame(0))
        for _ in range(steps):
            p.process_colors_array(frame(10), dt=dt)
        return p._prev_colors[0, 0]
    assert settle(1 / 40, 4) == pytest.approx(settle(1 / 80, 8), rel=0.02)


# ---- white balance -------------------------------------------------------------------------------

def test_white_balance_scales_each_channel():
    p = pipe(white_balance=(1.0, 0.8, 0.5))
    assert p.process_colors_array(frame(200))[0].tolist() == [200, 160, 100]
    assert p.map_single_color(200, 200, 200) == (200, 160, 100)


def test_white_balance_is_limited_and_sanitised():
    assert pipe(white_balance=(5, -1, 0)).white_balance.tolist() == pytest.approx([1.0, 0.3, 0.3])
    assert pipe(white_balance="garbage").white_balance.tolist() == [1.0, 1.0, 1.0]


# ---- dark-screen background light ------------------------------------------------------------------

def test_dark_screen_gets_the_background_light_at_the_chosen_level():
    p = pipe(gamma=2.2, dark_light=0.05)
    out = p.process_colors_array(frame(0))[0]
    assert out.max() == pytest.approx(0.05 * 255, abs=1.5)              # 5 % of full output, after the gamma
    assert out[0] > out[1] > out[2]                                     # warm white


def test_background_light_never_dims_a_lit_led():
    bright = frame(200)
    assert np.array_equal(pipe(gamma=2.2, dark_light=0.1).process_colors_array(bright),
                          pipe(gamma=2.2, dark_light=0.0).process_colors_array(bright))


def test_background_light_is_off_by_default_and_ignores_effects():
    assert pipe(gamma=2.2).process_colors_array(frame(0)).max() == 0
    assert pipe(gamma=2.2, dark_light=0.1).process_colors_array(frame(0), apply_gamma=False).max() == 0


# ---- power limit --------------------------------------------------------------------------------

def test_power_limit_caps_total_light():
    p = pipe(max_power=0.5)
    out = p.process_colors_array(frame(255))
    assert out.sum() / (out.shape[0] * 765) == pytest.approx(0.5, abs=0.01)


def test_power_limit_does_not_touch_frames_below_it():
    x = frame(60)
    assert np.array_equal(pipe(max_power=0.5).process_colors_array(x), pipe().process_colors_array(x))


def test_power_limit_applies_to_single_colors_too():
    assert max(pipe(max_power=0.25).map_single_color(255, 255, 255)) <= 65


def test_runtime_settings_reach_the_new_options():
    p = pipe()
    p.update_settings(max_power=0.5, dark_light=0.1, dithering=True, adaptive_smoothing=True, white_balance=(1, 1, 0.5))
    assert (p.max_power, p.dark_light, p.dithering, p.adaptive_smoothing) == (0.5, 0.1, True, True)
    assert p.white_balance[2] == 0.5
    p.update_settings(max_power=9, dark_light=9)
    assert (p.max_power, p.dark_light) == (1.0, 0.3)                    # clamped, never trusted blindly
