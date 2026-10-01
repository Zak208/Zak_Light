import json
import time

import numpy as np
import pytest

from core.animations import AMBIENT, EFFECT_KEYS, REGISTRY, SHOW, catalogue
from core.effects import EffectEngine

FPS = 40


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, seconds=1.0 / FPS):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr("core.effects.time.perf_counter", c)
    return c


def frames(clock, effect, n=63, seconds=3.0, speed=1.0, colors=None, engine=None, **kw):
    eng = engine or EffectEngine(n, seed=7)
    eng._last = clock.t
    out = []
    for _ in range(int(seconds * FPS)):
        clock.tick()
        out.append(eng.render(effect, speed, colors, **kw))
    return np.array(out), eng


def test_catalogue_is_complete_and_has_both_groups():
    assert len(EFFECT_KEYS) == len(set(EFFECT_KEYS)) >= 16
    assert len(AMBIENT) >= 8 and len(SHOW) >= 6 and set(AMBIENT) | set(SHOW) == set(EFFECT_KEYS)
    cat = catalogue()
    json.dumps(cat)                                                   # the UI receives it as JSON
    assert all({"key", "label", "group", "description", "swatch", "uses_colors"} <= set(c) for c in cat)


@pytest.mark.parametrize("effect", EFFECT_KEYS)
@pytest.mark.parametrize("n", [1, 10, 63, 254])
def test_every_effect_renders_valid_frames(clock, effect, n):
    clip, _ = frames(clock, effect, n=n, seconds=2.0)
    assert clip.shape == (80, n, 3) and clip.dtype == np.float32
    assert np.isfinite(clip).all() and clip.min() >= 0 and clip.max() <= 255


@pytest.mark.parametrize("effect", EFFECT_KEYS)
def test_every_effect_actually_moves(clock, effect):
    eng = EffectEngine(63, seed=7)
    eng.sunrise_seconds = 8.0
    clip, _ = frames(clock, effect, seconds=9.0, engine=eng)
    assert np.abs(np.diff(clip, axis=0)).sum() > 0, f"{effect} is static"


@pytest.mark.parametrize("effect", [k for k in EFFECT_KEYS if REGISTRY[k].uses_colors])
def test_color_effects_follow_the_chosen_palette(clock, effect):
    red, blue = [[255, 0, 0]] * 3, [[0, 0, 255]] * 3
    a, _ = frames(clock, effect, colors=red, seconds=4.0)
    b, _ = frames(clock, effect, colors=blue, seconds=4.0)
    assert a[..., 0].sum() > a[..., 2].sum() and b[..., 2].sum() > b[..., 0].sum()


@pytest.mark.parametrize("effect", [k for k in EFFECT_KEYS if not REGISTRY[k].uses_colors])
def test_fixed_effects_ignore_the_palette(clock, effect):
    a, _ = frames(clock, effect, colors=[[255, 0, 0]] * 3, seconds=2.0)
    b, _ = frames(clock, effect, colors=[[0, 0, 255]] * 3, seconds=2.0)
    assert np.allclose(a, b)


def test_direction_and_mirror(clock):
    normal, _ = frames(clock, "chase", seconds=1.0)
    clock.t = 1000.0
    reverse, _ = frames(clock, "chase", seconds=1.0, reverse=True)
    assert np.allclose(reverse, normal[:, ::-1])
    clock.t = 1000.0
    mirrored, _ = frames(clock, "aurora", n=64, seconds=1.0, mirror=True)
    for i in range(1, 64):
        assert np.allclose(mirrored[:, i], mirrored[:, 64 - i])      # left and right halves match


def test_changing_effect_clears_the_old_one(clock):
    clip, eng = frames(clock, "fireworks", seconds=3.0)
    assert eng.state.get("bursts")
    eng.render("stars")
    assert "bursts" not in eng.state


def test_speed_zero_freezes_and_speed_scales(clock):
    still, _ = frames(clock, "plasma", seconds=1.0, speed=0.0)
    assert np.allclose(still[0], still[-1])
    slow, _ = frames(clock, "plasma", seconds=2.0, speed=1.0)
    clock.t = 1000.0
    fast, _ = frames(clock, "plasma", seconds=1.0, speed=2.0)
    assert np.allclose(slow[-1], fast[-1], atol=1e-3)                 # 2 s at x1 == 1 s at x2


def test_sunrise_goes_from_dark_red_to_warm_white(clock):
    eng = EffectEngine(32, seed=1)
    eng.sunrise_seconds = 10.0
    clip, _ = frames(clock, "sunrise", n=32, seconds=12.0, engine=eng)
    start, middle, end = clip[2].mean(axis=0), clip[200].mean(axis=0), clip[-1].mean(axis=0)
    assert start.max() < 60 and start[0] > start[2]                     # deep red, dim
    assert start.sum() < middle.sum() < end.sum()                       # only ever brighter
    assert end.min() > 140 and end[0] >= end[2]                         # ends as warm white and stays there
    assert np.allclose(clip[-1], clip[-40], atol=1.0)


@pytest.mark.parametrize("effect", SHOW + ("stars", "candle", "aurora"))
def test_no_effect_flashes_faster_than_three_times_a_second(clock, effect):
    """Photosensitivity guideline: no more than 3 light flashes per second, for any effect."""
    eng = EffectEngine(63, seed=3)
    clip, _ = frames(clock, effect, seconds=20.0, engine=eng)
    light = clip.mean(axis=(1, 2))
    span = light.max() - light.min()
    if span < 8:                                                        # too gentle to be a flash at all
        return
    mid = light.min() + 0.5 * span
    rises = int(np.sum((light[1:] > mid) & (light[:-1] <= mid)))
    assert rises / 20.0 <= 3.0, f"{effect}: {rises / 20.0:.1f} flashes/s"


@pytest.mark.parametrize("effect", EFFECT_KEYS)
def test_effects_are_cheap(effect):
    eng = EffectEngine(254, seed=1)
    eng.render(effect)
    t0 = time.perf_counter()
    for _ in range(200):
        eng.render(effect)
    per_frame_ms = (time.perf_counter() - t0) / 200 * 1000
    assert per_frame_ms < 2.0, f"{effect}: {per_frame_ms:.2f} ms/frame for 254 LEDs"


def test_unknown_effect_falls_back_instead_of_crashing(clock):
    clip, _ = frames(clock, "does-not-exist", seconds=0.5)
    assert np.isfinite(clip).all()


# ---- how the show effects look on the strip ----------------------------------------------------------------------------

def test_scanner_has_a_bright_beam_a_trail_and_no_stray_colour(clock):
    clip, _ = frames(clock, "scanner", colors=[[255, 0, 0], [140, 0, 0], [0, 255, 0]], seconds=6.0)
    assert clip.max() >= 250
    assert clip[..., 1].max() < 1.0                                   # the third (green) colour is never used: no green glow
    lit = (clip.sum(axis=2) > 40).sum(axis=1)
    assert 2 <= lit.mean() <= 25                                      # a beam, not the whole strip and not a single dot


def test_police_uses_only_its_two_colours_and_fades_each_flash(clock):
    clip, _ = frames(clock, "police", colors=[[255, 0, 0], [0, 0, 255], [0, 255, 0]], seconds=4.0)
    assert clip[..., 1].max() == 0                                    # never green
    left, right = clip[:, :30], clip[:, 33:]
    assert left[..., 0].max() > 240 and left[..., 2].max() == 0      # one side only red...
    assert right[..., 2].max() > 240 and right[..., 0].max() == 0    # ...the other only blue
    reds = sorted(set(np.round(left[..., 0].ravel()).astype(int)))
    assert len(reds) > 4                                              # a fade, not just on/off


def test_chase_gaps_are_really_dark(clock):
    clip, _ = frames(clock, "chase", colors=[[255, 255, 255]] * 3, seconds=3.0)
    frame = clip[30]
    assert frame.max() > 240 and np.sort(frame[:, 0])[len(frame) // 4] < 6     # a quarter of the LEDs are (almost) off


def test_light_red_is_not_washed_out_on_the_leds():
    """Effects go through the LED gamma curve, so a light red keeps its hue instead of turning pink."""
    from core.color import ColorPipeline
    pipe = ColorPipeline(wire_map="RGB", gamma=2.2, brightness=1.0, smoothing=1.0, min_luminance_floor=0)
    raw = np.array([[255, 110, 110]], dtype=np.float32)
    linear = pipe.process_colors_array(raw, apply_gamma=False)[0]
    pipe.reset_smoothing()
    curved = pipe.process_colors_array(raw, apply_gamma=False, led_gamma=True)[0]
    assert linear[1] > 100 and curved[1] < 45 and curved[0] == 255
