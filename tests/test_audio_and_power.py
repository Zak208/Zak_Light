import ctypes
import uuid
from ctypes import wintypes

import numpy as np
import pytest

from core import loopback, power

SR = 48000


class Clock:
    """A fake monotonic clock so a whole 'song' can be analysed in a few milliseconds."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def analyzer(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(loopback.time, "monotonic", clock)
    cap = loopback.LoopbackCapture()                          # the thread is never started
    cap.sample_rate = SR
    return loopback.SpectrumAnalyzer(cap), cap, clock


def play(analyzer, signal, clock, step=0.025):
    """Feeds a signal 25 ms at a time, like the capture thread, and collects the features."""
    an, cap, _ = analyzer
    n = int(SR * step)
    feats = []
    for i in range(0, len(signal) - n, n):
        cap._push(signal[i:i + n])
        clock.t += step
        feats.append(an.update(step))
    return feats


def kick_train(seconds, bpm=120):
    x = np.zeros(int(SR * seconds), dtype=np.float32)
    period = 60.0 / bpm
    for k in np.arange(0.3, seconds - 0.2, period):
        i, n = int(k * SR), int(0.12 * SR)
        tt = np.arange(n) / SR
        x[i:i + n] += 0.5 * np.sin(2 * np.pi * 60 * tt) * np.exp(-tt / 0.04)
    return x


def hats(seconds):
    x = np.zeros(int(SR * seconds), dtype=np.float32)
    rng = np.random.default_rng(0)
    for k in np.arange(0.2, seconds - 0.1, 0.25):
        i, n = int(k * SR), int(0.03 * SR)
        x[i:i + n] += 0.12 * rng.standard_normal(n) * np.exp(-np.arange(n) / SR / 0.01)
    return x


def count_beats(feats):
    beats, prev = 0, 0.0
    for f in feats:
        if f.beat > 0.95 >= prev:
            beats += 1
        prev = f.beat
    return beats


def test_kicks_are_detected_as_beats(analyzer):
    feats = play(analyzer, kick_train(6), analyzer[2])
    assert 8 <= count_beats(feats) <= 12                      # 120 BPM over ~6 s
    assert max(f.bass for f in feats) > 0.8 and max(f.high for f in feats) < 0.1


def test_hats_alone_are_not_beats_and_are_not_bass(analyzer):
    feats = play(analyzer, hats(5), analyzer[2])
    assert count_beats(feats) == 0
    settled = feats[20:]                                      # the first ~0.5 s is the auto-gain warming up
    mean_bass = np.mean([f.bass for f in settled])
    mean_high = np.mean([f.high for f in settled])
    # short clicks splash a little energy into the bass band for one frame, but the highs clearly dominate
    assert mean_bass < 0.25 and mean_high > 2 * mean_bass


def test_silence_is_silent(analyzer):
    feats = play(analyzer, np.zeros(SR * 2, dtype=np.float32), analyzer[2])
    assert all(f.level == 0 and f.beat == 0 for f in feats)


def peak_bars(analyzer, signal):
    """Highest value each equalizer bar reached while the signal played."""
    an, cap, clock = analyzer
    n, peaks = int(SR * 0.025), np.zeros(7)
    for i in range(0, len(signal) - n, n):
        cap._push(signal[i:i + n])
        clock.t += 0.025
        an.update(0.025)
        peaks = np.maximum(peaks, an.bars)
    return peaks


def test_equalizer_bars_follow_the_spectrum(analyzer):
    kicks = peak_bars(analyzer, kick_train(3))
    assert kicks[0] > 0.5 and kicks[0] > kicks[-1] + 0.3      # 60 Hz lives in the first bar
    noise = peak_bars(analyzer, hats(3))
    assert noise[-1] > noise[0]                               # noise bursts reach the top bars


def test_quiet_and_loud_versions_of_the_same_music_react_alike(analyzer, monkeypatch):
    """Auto-gain: the Windows volume slider must not decide how the lights behave."""
    def run(gain):
        clock = Clock()
        monkeypatch.setattr(loopback.time, "monotonic", clock)
        cap = loopback.LoopbackCapture()
        cap.sample_rate = SR
        a = loopback.SpectrumAnalyzer(cap)
        return count_beats(play((a, cap, clock), kick_train(6) * gain, clock))

    assert abs(run(1.0) - run(0.1)) <= 1


def test_loopback_start_stop_never_leaves_two_threads(monkeypatch):
    cap = loopback.LoopbackCapture()
    monkeypatch.setattr(cap, "_capture_until_device_changes", lambda stop: stop.wait(5))
    monkeypatch.setattr(loopback.comtypes, "CoInitialize", lambda: None)
    monkeypatch.setattr(loopback.comtypes, "CoUninitialize", lambda: None)
    cap.start()
    first = cap._thread
    cap.start()                                               # already running: no second thread
    assert cap._thread is first
    cap.stop()
    cap.start()
    assert cap._thread is not first
    cap.stop()


# ---- power / session notifications -------------------------------------------------------------

def test_power_state_combines_causes():
    st = power.PowerState()
    st.on_session_change(power.WTS_SESSION_LOCK)
    assert st.inactive
    st.on_power_event(power.PBT_POWERSETTINGCHANGE, 0)        # display off as well
    st.on_session_change(power.WTS_SESSION_UNLOCK)
    assert st.inactive                                        # still dark: the monitor is off
    st.on_power_event(power.PBT_POWERSETTINGCHANGE, 1)
    assert not st.inactive
    st.on_power_event(power.PBT_POWERSETTINGCHANGE, 2)        # dimmed still shows a picture
    assert not st.inactive
    st.on_power_event(power.PBT_APMSUSPEND)
    assert st.inactive
    st.on_power_event(power.PBT_APMRESUMEAUTOMATIC)
    assert not st.inactive


def test_power_monitor_reacts_to_real_window_messages():
    changes = []
    mon = power.PowerMonitor(changes.append)
    assert mon.start()
    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t

    def display(value):
        s = power._PowerBroadcastSetting()
        ctypes.memmove(s.PowerSetting, uuid.UUID(power.GUID_CONSOLE_DISPLAY_STATE).bytes_le, 16)
        s.DataLength = 4
        ctypes.c_uint32.from_address(ctypes.addressof(s.Data)).value = value
        user32.SendMessageW(mon.hwnd, power.WM_POWERBROADCAST, power.PBT_POWERSETTINGCHANGE, ctypes.addressof(s))

    try:
        user32.SendMessageW(mon.hwnd, power.WM_WTSSESSION_CHANGE, power.WTS_SESSION_LOCK, 0)
        user32.SendMessageW(mon.hwnd, power.WM_WTSSESSION_CHANGE, power.WTS_SESSION_UNLOCK, 0)
        display(0)
        display(1)
        user32.SendMessageW(mon.hwnd, power.WM_POWERBROADCAST, power.PBT_APMSUSPEND, 0)
        user32.SendMessageW(mon.hwnd, power.WM_POWERBROADCAST, power.PBT_APMRESUMEAUTOMATIC, 0)
    finally:
        mon.stop()
    assert changes == [True, False, True, False, True, False]
    assert mon.hwnd is None


def test_display_change_messages_reach_the_callback():
    import ctypes
    import time

    from core.power import WM_DISPLAYCHANGE, PowerMonitor
    seen = []
    mon = PowerMonitor(lambda inactive: None, lambda: seen.append(1))
    assert mon.start()
    try:
        ctypes.windll.user32.PostMessageW(mon.hwnd, WM_DISPLAYCHANGE, 32, 0)
        for _ in range(100):
            if seen:
                break
            time.sleep(0.02)
        assert seen
    finally:
        mon.stop()
