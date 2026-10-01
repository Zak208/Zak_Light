import numpy as np

from core import loopback
from core.audio_visualizer import AUDIO_PALETTES, AudioReactiveEngine, N_PATTERNS, render_pattern

SR = 48000
PAL = AUDIO_PALETTES["cyberpunk"]


def test_there_are_ten_patterns_and_all_render_valid_frames():
    assert N_PATTERNS == 10
    extras = {"left": 0.7, "right": 0.3, "bars": [0.1, 0.4, 0.9, 0.5, 0.2, 0.1, 0.0], "beat": 0.6, "beat_count": 4}
    for pat in range(N_PATTERNS):
        for n in (1, 10, 63, 254):
            frame = render_pattern(pat, n, 0.6, 1.0, PAL, extras)
            assert frame.shape == (n, 3) and frame.dtype == np.float32
            assert np.isfinite(frame).all() and frame.min() >= 0 and frame.max() <= 255


def test_vu_stereo_lights_the_louder_side():
    n = 64
    left_loud = render_pattern(7, n, 0.5, 0.0, PAL, {"left": 1.0, "right": 0.0})
    assert left_loud[:n // 2].sum() > 2 * left_loud[n // 2:].sum()
    right_loud = render_pattern(7, n, 0.5, 0.0, PAL, {"left": 0.0, "right": 1.0})
    assert right_loud[n // 2:].sum() > 2 * right_loud[:n // 2].sum()


def test_vu_bars_grow_from_the_bottom_with_the_level():
    n = 64
    quiet = render_pattern(7, n, 0.5, 0.0, PAL, {"left": 0.3, "right": 0.3})
    loud = render_pattern(7, n, 0.5, 0.0, PAL, {"left": 0.9, "right": 0.9})
    assert loud.sum() > quiet.sum()


def test_perimeter_spectrum_puts_low_notes_first_and_high_notes_last():
    bass_only = render_pattern(8, 70, 0.5, 0.0, PAL, {"bars": [1, 0, 0, 0, 0, 0, 0]})
    assert bass_only[:10].sum() > 5 * bass_only[-10:].sum()
    treble_only = render_pattern(8, 70, 0.5, 0.0, PAL, {"bars": [0, 0, 0, 0, 0, 0, 1]})
    assert treble_only[-10:].sum() > 5 * treble_only[:10].sum()


def test_color_per_beat_changes_color_and_flashes():
    quiet_a = render_pattern(9, 30, 0.3, 0.0, PAL, {"beat_count": 0, "beat": 0.0})
    quiet_b = render_pattern(9, 30, 0.3, 0.0, PAL, {"beat_count": 1, "beat": 0.0})
    assert not np.allclose(quiet_a, quiet_b)                                  # a beat moved on to the next color
    flash = render_pattern(9, 30, 0.3, 0.0, PAL, {"beat_count": 0, "beat": 1.0})
    assert flash.sum() > 1.5 * quiet_a.sum()


def test_stereo_analysis_tells_left_from_right():
    cap = loopback.LoopbackCapture()
    cap.sample_rate = SR
    an = loopback.SpectrumAnalyzer(cap)
    tone = (0.4 * np.sin(2 * np.pi * 440 * np.arange(SR // 2) / SR)).astype(np.float32)
    silent = np.zeros_like(tone)
    for i in range(0, tone.size - 1200, 1200):
        cap._push(tone[i:i + 1200], tone[i:i + 1200], silent[i:i + 1200])       # only the left channel plays
        f = an.update(0.025)
    assert f.left > 0.5 and f.right < 0.05


def test_beats_are_counted_once_each():
    engine = AudioReactiveEngine()
    engine.capture.active = True
    beats = [0.0, 1.0, 0.8, 0.5, 0.0, 1.0, 0.7, 0.0]
    seq = iter(beats)
    engine.analyzer.update = lambda dt: loopback.Features(0.5, 0.5, 0.2, 0.1, next(seq), 0.4, 0.6)
    for _ in beats:
        engine.update_volume(0.025)
    assert engine.beat_count == 2
    assert engine.right > engine.left or np.isclose(engine.right, engine.left, atol=0.3)
    engine.suspend()
    assert engine.beat_count == 0 and engine.left == 0.0
