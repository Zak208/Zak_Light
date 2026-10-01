"""The vectorised patterns/effects must reproduce the original per-LED loops."""
import numpy as np
import pytest

from core.audio_visualizer import AUDIO_PALETTES, render_pattern
from core.effects import EffectEngine
from reference_patterns import reference_fire, reference_pattern, reference_rainbow


@pytest.mark.parametrize("pat", range(7))
@pytest.mark.parametrize("n", [1, 7, 10, 63, 120, 254])
@pytest.mark.parametrize("vol", [0.0, 0.3, 1.0])
def test_pattern_matches_reference(pat, n, vol):
    palette = AUDIO_PALETTES["cyberpunk"]
    for phase in (0.0, 1.7, 5.9):
        got = render_pattern(pat, n, vol, phase, palette)
        want = reference_pattern(pat, n, vol, phase, palette)
        assert got.shape == (n, 3) and got.dtype == np.float32
        assert np.abs(got - want).max() < 0.05, (pat, n, vol, phase)


@pytest.mark.parametrize("palette", list(AUDIO_PALETTES))
def test_every_palette_is_supported(palette):
    got = render_pattern(0, 63, 0.6, 1.0, AUDIO_PALETTES[palette])
    want = reference_pattern(0, 63, 0.6, 1.0, AUDIO_PALETTES[palette])
    assert np.abs(got - want).max() < 0.05


def test_pattern_index_wraps_like_before():
    a = render_pattern(12, 63, 0.5, 1.0, AUDIO_PALETTES["fire"])
    b = render_pattern(2, 63, 0.5, 1.0, AUDIO_PALETTES["fire"])
    assert np.array_equal(a, b)


@pytest.mark.parametrize("n", [1, 10, 63, 254])
def test_rainbow_matches_reference(n):
    eng = EffectEngine(n)
    for phase in (0.0, 0.123, 0.5, 0.999, 3.7):
        eng._phase, eng._last = phase, eng._last  # freeze the clock: advance adds ~0
        got = eng.rainbow_wave(speed=0.0)             # speed 0 => the phase stays exactly as set
        assert np.abs(got - reference_rainbow(n, phase * 0.5)).max() <= 1.0
    # the rainbow really cycles through all hues
    assert len({tuple(c) for c in EffectEngine(254).rainbow_wave()}) > 100


@pytest.mark.parametrize("n", [1, 10, 63])
def test_fire_matches_reference(n):
    eng = EffectEngine(n)
    for phase in (0.0, 1.0, 7.3):
        eng._phase = phase
        assert np.abs(eng.fire_flicker(speed=0.0) - reference_fire(n, phase * 3.0)).max() <= 1.0


def test_changing_speed_does_not_make_the_animation_jump(monkeypatch):
    """The old code computed phase = elapsed * speed, so moving the slider teleported the animation."""
    clock = {"t": 50.0}
    monkeypatch.setattr("core.effects.time.perf_counter", lambda: clock["t"])
    eng = EffectEngine(32)
    eng._last = clock["t"]
    for _ in range(40):                                        # 1 s at speed 1
        clock["t"] += 0.025
        eng.rainbow_wave(speed=1.0)
    phase_before = eng._phase
    clock["t"] += 0.025
    after = eng.rainbow_wave(speed=3.0)                        # slider moved to 3x
    # the animation simply continues: this frame advances by dt * 3 from where it was
    expected = reference_rainbow(32, (phase_before + 0.025 * 3.0) * 0.5)
    assert np.abs(after - expected).max() <= 1.0


def test_a_long_pause_does_not_fast_forward_the_animation(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("core.effects.time.perf_counter", lambda: clock["t"])
    eng = EffectEngine(8)
    eng._last = 0.0
    eng.fire_flicker()
    clock["t"] = 3600.0                                         # e.g. the PC was suspended for an hour
    eng.fire_flicker()
    assert eng._phase <= 0.3
