import threading
import time

import numpy as np

import engine
from engine import run_engine


def frames_of(state):
    """Frames sent as arrays (streamed modes and fades); solid colors go out as segments and are skipped."""
    return [f for f in state.driver.sent if isinstance(f, np.ndarray)]


def run_in_thread(state):
    t = threading.Thread(target=run_engine, args=(state,), daemon=True)
    t.start()
    return t


def wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def stop(state, thread):
    state.is_running = False
    state.wake.set()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_screen_mode_streams_frames_and_stops_cleanly(state):
    state.config.target_fps = 60
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 3)
    stop(state, t)
    assert state.driver.off_count >= 1                      # LEDs are turned off on shutdown


def test_static_mode_fades_in_then_idles(state):
    state.static_color_rgb = [10, 20, 30]
    state.config.brightness = 1.0
    state.set_mode("static")
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 2)          # a fade from black, not a jump
    time.sleep(0.8)                                                   # the fade (0.4 s) is over
    sent = len(state.driver.sent)
    time.sleep(0.3)
    assert len(state.driver.sent) == sent                             # no polling while nothing changes
    frames = frames_of(state)
    assert frames and frames[0].max() <= frames[-1].max()             # brightens towards the color
    stop(state, t)


def test_off_mode_turns_the_strip_off(state):
    state.set_mode("off")
    t = run_in_thread(state)
    assert wait_until(lambda: state.driver.off_count >= 1)
    stop(state, t)


def test_a_failing_iteration_does_not_kill_the_engine(state):
    calls = {"n": 0}

    def flaky(points, box_size, detect_black_bars):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("capture exploded")
        return np.full((len(points), 3), 50.0, dtype=np.float32)

    state.screen_sampler.capture_and_sample = flaky
    state.config.target_fps = 60
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 1, timeout=5)   # recovered after the errors
    assert t.is_alive()
    stop(state, t)


def test_mode_change_work_is_protected_too(state):
    """The mode switch (turn_off, static color, ...) used to run outside the try block."""
    state.driver.turn_off = lambda: (_ for _ in ()).throw(RuntimeError("usb hiccup"))
    state.set_mode("off")
    t = run_in_thread(state)
    time.sleep(0.4)
    assert t.is_alive()
    stop(state, t)


def test_suspended_session_darkens_then_restores(state):
    state.config.target_fps = 60
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 2)
    state.set_suspended(True)
    assert wait_until(lambda: state.driver.off_count >= 1)
    frozen = len(state.driver.sent)
    time.sleep(0.3)
    assert len(state.driver.sent) == frozen                 # nothing is streamed while suspended
    state.set_suspended(False)
    assert wait_until(lambda: len(state.driver.sent) > frozen)
    stop(state, t)


def test_device_reconnection_reapplies_the_mode(state):
    state.static_color_rgb = [1, 2, 3]
    state.set_mode("static")
    flags = iter([False, True])
    state.driver.consume_needs_init = lambda: next(flags, False)
    t = run_in_thread(state)
    assert wait_until(lambda: len(state.driver.sent) >= 2, timeout=5)   # initial paint + repaint after reconnect
    stop(state, t)


def test_unchanged_screen_makes_the_engine_coast(state, monkeypatch):
    monkeypatch.setattr(engine, "STILL_AFTER_FRAMES", 3)
    state.screen_sampler.capture_and_sample = lambda points, box_size, detect_black_bars: \
        np.full((len(points), 3), 60.0, dtype=np.float32)   # frame_id never advances: a static screen
    state.config.target_fps = 60
    t = run_in_thread(state)
    time.sleep(0.6)
    coasting_rate = len(state.driver.sent)
    stop(state, t)
    assert coasting_rate < 0.6 * 60 * 0.5                    # far fewer frames than the active rate


def test_apply_config_reaches_every_component(state):
    state.config.brightness = 0.3
    state.config.rhythm_palette = "fire"
    state.config.led_count = 12
    state.apply_config()
    assert state.color_pipeline.brightness == 0.3
    assert state.audio_engine.palette_name == "fire" and state.audio_engine.led_count == 12
    assert state.effect_engine.led_count == 12
