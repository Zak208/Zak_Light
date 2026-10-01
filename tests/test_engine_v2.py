"""Engine features added in v2: stall-proof capture, fades, screen styles, bass pulse, dark states."""
import threading
import time

import numpy as np
import pytest

from engine import Crossfade
from core.zones import apply_screen_style, edge_groups
from test_engine import frames_of, run_in_thread, stop, wait_until


# ---- the capture can no longer freeze the lights ---------------------------------------------------

def test_a_blocked_capture_never_freezes_the_engine(state):
    """dxcam retries forever inside a capture call when Windows takes the desktop away."""
    release = threading.Event()
    calls = []

    def hanging_capture(points, box_size, detect_black_bars):
        calls.append(1)
        if len(calls) > 3:
            release.wait(30)                      # desktop access lost: this call never returns
        return np.full((len(points), 3), 90.0, dtype=np.float32)

    state.screen_sampler.capture_and_sample = hanging_capture
    state.config.target_fps = 60
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 2)
    assert wait_until(lambda: state.capture_stalled, timeout=5)        # reported, not hidden

    started = time.monotonic()
    state.static_color_rgb = [0, 255, 0]
    state.config.brightness = 1.0
    state.apply_config()
    state.set_mode("static")                                           # the user asks for another mode
    assert wait_until(lambda: state.driver.last_frame is not None and state.driver.last_frame[0][1] > 100, timeout=3)
    assert time.monotonic() - started < 2.5                            # answered promptly despite the hang
    release.set()
    stop(state, t)


def test_capture_recovers_by_itself(state):
    gate = threading.Event()

    def capture(points, box_size, detect_black_bars):
        if not gate.is_set():
            gate.wait(30)
        return np.full((len(points), 3), 50.0, dtype=np.float32)

    state.screen_sampler.capture_and_sample = capture
    t = run_in_thread(state)
    assert wait_until(lambda: state.capture_stalled, timeout=5)
    gate.set()
    assert wait_until(lambda: not state.capture_stalled and len(state.driver.sent) > 0, timeout=5)
    stop(state, t)


def test_blocked_capture_is_not_restarted_concurrently(state):
    """Mode changes must not create competing dxcam calls while one capture is still blocked."""
    release = threading.Event()
    calls = []

    def capture(points, box_size, detect_black_bars):
        calls.append(1)
        if len(calls) == 1:
            release.wait(30)
        return np.full((len(points), 3), 50.0, dtype=np.float32)

    state.screen_sampler.capture_and_sample = capture
    t = run_in_thread(state)
    assert wait_until(lambda: state.capture_stalled, timeout=5)
    state.set_mode("static")
    assert wait_until(lambda: not state.worker.running)  # old capture is still alive, but was cancelled
    state.set_mode("screen")
    time.sleep(0.3)
    assert len(calls) == 1

    release.set()
    assert wait_until(lambda: len(calls) >= 2, timeout=5)
    stop(state, t)


def test_leaving_the_screen_mode_stops_the_capture_thread(state):
    t = run_in_thread(state)
    assert wait_until(lambda: state.worker.running)
    state.set_mode("off")
    assert wait_until(lambda: not state.worker.running)
    stop(state, t)


# ---- fades -------------------------------------------------------------------------------------------

def test_crossfade_blends_from_the_previous_frame():
    x = Crossfade()
    x.start(np.zeros((4, 3)), 0.2)
    assert x.apply(np.full((4, 3), 200, np.uint8)).max() < 200          # partway
    time.sleep(0.25)
    assert x.apply(np.full((4, 3), 200, np.uint8)).min() == 200         # finished: the new frame as is
    assert not x.active


def test_crossfade_ignores_a_source_of_a_different_size():
    x = Crossfade()
    x.start(np.zeros((9, 3)), 1.0)
    assert x.apply(np.full((4, 3), 100, np.uint8)).shape == (4, 3)


def test_switching_off_fades_instead_of_cutting(state):
    state.config.transitions = True
    state.static_color_rgb = [255, 255, 255]
    state.config.brightness = 1.0
    state.apply_config()
    state.set_mode("static")
    t = run_in_thread(state)
    assert wait_until(lambda: state.driver.last_frame is not None and state.driver.last_frame.max() > 200, timeout=3)
    state.set_mode("off")
    assert wait_until(lambda: state.driver.off_count >= 1, timeout=3)
    levels = [f.max() for f in frames_of(state)]
    assert any(0 < v < 200 for v in levels)                            # intermediate brightness was shown
    stop(state, t)


def test_transitions_can_be_turned_off(state):
    state.config.transitions = False
    state.config.brightness = 1.0
    state.apply_config()
    state.set_mode("static")
    state.static_color_rgb = [255, 255, 255]
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 1)
    time.sleep(0.6)
    stop(state, t)
    assert state.driver.sent and frames_of(state) == []                # painted at once: no fade frames at all


# ---- dark states --------------------------------------------------------------------------------------

def test_idle_away_keeps_the_strip_dark_and_restores(state):
    state.config.target_fps = 60
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 2)
    state.set_away(True)
    assert wait_until(lambda: state.driver.off_count >= 1, timeout=3)
    assert wait_until(lambda: not state.worker.running)
    assert set(map(tuple, state.preview_colors())) == {(0, 0, 0)}
    time.sleep(0.6)                                                    # the fade to dark is over
    frozen = len(state.driver.sent)
    time.sleep(0.4)
    assert len(state.driver.sent) == frozen
    state.set_away(False)
    assert wait_until(lambda: len(state.driver.sent) > frozen, timeout=3)
    stop(state, t)


# ---- screen styles and edge mask (pure) -------------------------------------------------------------------

def layout():
    # 2 LEDs per edge: left (x small), top, right, bottom
    return np.array([[0.03, 0.3], [0.03, 0.7], [0.3, 0.03], [0.7, 0.03], [0.97, 0.3], [0.97, 0.7],
                     [0.3, 0.97], [0.7, 0.97]], dtype=np.float32)


def test_edge_groups_assign_left_top_right_bottom():
    assert edge_groups(layout()).tolist() == [0, 0, 1, 1, 2, 2, 3, 3]


def test_average_style_gives_every_led_the_same_color():
    raw = np.arange(24, dtype=np.float32).reshape(8, 3)
    out = apply_screen_style(raw, layout(), "average", [True] * 4)
    assert np.allclose(out, raw.mean(axis=0)) and out.shape == raw.shape


def test_halves_style_separates_left_and_right():
    raw = np.zeros((8, 3), np.float32)
    raw[[0, 1, 2, 6]] = 100                                             # the LEDs with x < 0.5
    out = apply_screen_style(raw, layout(), "halves", [True] * 4)
    left, right = layout()[:, 0] < 0.5, layout()[:, 0] >= 0.5
    assert len({tuple(r) for r in out[left]}) == 1 and len({tuple(r) for r in out[right]}) == 1
    assert out[left][0][0] > out[right][0][0]


def test_disabled_edges_stay_dark_and_the_input_is_not_modified():
    raw = np.full((8, 3), 200, np.float32)
    before = raw.copy()
    out = apply_screen_style(raw, layout(), "edge", [True, False, True, False])   # no top, no bottom
    assert out[[2, 3, 6, 7]].max() == 0 and out[[0, 1, 4, 5]].min() == 200
    assert np.array_equal(raw, before)


def test_edge_style_without_masks_is_a_no_op():
    raw = np.random.default_rng(0).random((8, 3)).astype(np.float32)
    assert apply_screen_style(raw, layout(), "edge", [True] * 4) is raw


# ---- bass pulse layered on the screen ------------------------------------------------------------------------

def test_audio_boost_brightens_the_picture_and_starts_the_audio(state):
    state.config.audio_boost = 1.0
    state.config.target_fps = 60
    state.config.brightness = 0.5
    state.color_pipeline.update_settings(smoothing=1.0, brightness=0.5)
    started = []
    state.audio_engine.activate = lambda: started.append(1)
    t = run_in_thread(state)
    assert wait_until(lambda: started and len(state.driver.sent) >= 3)
    time.sleep(0.7)                                                    # past the 0.4 s fade-in
    boosted = max(f.max() for f in frames_of(state)[-3:])
    stop(state, t)

    plain = type(state)(config=type(state.config)(), driver=type(state.driver)(), screen_sampler=type(state.screen_sampler)(),
                        audio_engine=type(state.audio_engine)(), profiles=state.profiles)
    plain.config.audio_boost = 0.0
    plain.config.target_fps = 60
    plain.color_pipeline.update_settings(smoothing=1.0, brightness=0.5)
    t2 = run_in_thread(plain)
    assert wait_until(lambda: len(plain.driver.sent) >= 3)
    time.sleep(0.7)
    stop(plain, t2)
    assert boosted > max(f.max() for f in frames_of(plain)[-3:])        # same picture, louder bass => brighter


def test_no_audio_is_started_for_a_plain_screen_mode(state):
    started = []
    state.audio_engine.activate = lambda: started.append(1)
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 2)
    stop(state, t)
    assert started == []


def test_performance_measurement_is_bounded_and_contains_no_screen_data(state):
    assert state.start_performance_measurement("desktop")
    state.performance_recorder.record(state, force=True)
    report = state.stop_performance_measurement()
    assert report["format"] == "zak_light_performance_measurement"
    assert report["scenario"] == "desktop" and report["samples"]
    assert {"cpu_pct", "ram_mb", "capture_fps", "usb_writes"} <= set(report["summary"])
    assert "led_points" not in report and "colors" not in report


# ---- metrics include the capture helper ----------------------------------------------------------------------------

def test_other_process_stats_reads_a_live_process_and_rejects_a_dead_pid():
    import os

    from core import system as winsys
    cpu, ram = winsys.other_process_stats(os.getpid())
    assert cpu > 0 and ram > 5
    assert winsys.other_process_stats(2 ** 30) is None


def test_metrics_add_the_helper_process_to_the_totals(state, monkeypatch):
    state.screen_sampler.helper_status = lambda: {"helper_pid": 4242}
    readings = iter([(10.0, 80.0), (11.0, 81.0)])
    monkeypatch.setattr("core.system.other_process_stats", lambda pid: next(readings))
    state.metrics()                                   # first call only records the starting point
    time.sleep(0.2)
    m = state.metrics()
    assert m["capture_ram_mb"] == 81.0 and m["ram_mb"] == pytest.approx(m["engine_ram_mb"] + 81.0, abs=0.2)
    assert m["capture_cpu_pct"] > 0 and m["cpu_pct"] >= m["capture_cpu_pct"]
