"""
The capture helper process: normal answers, a call that never returns (killed and replaced), a helper that dies,
and the fallback when helpers cannot run at all. A scripted stand-in for the helper is used, so no GPU is needed.
"""
import sys
import textwrap
import time

import numpy as np
import pytest

from core import capture_proxy as cp

FAKE = textwrap.dedent('''
    import sys, os, time
    sys.path.insert(0, {root!r})
    import numpy as np
    from core import capture_proxy as cp
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    mode = {mode!r}
    calls = 0
    while True:
        msg = cp._recv(inp)
        if msg is None or msg[0] == "close":
            break
        op, args = msg
        calls += 1
        if mode == "hang" and op == "sample" and calls >= 2:
            time.sleep(60)
        if mode == "die_at_2" and calls >= 2:
            os._exit(3)
        if op == "sample":
            n = len(args["points"])
            cp._send(out, ("ok", (np.full((n, 3), float(args["monitor"] + 1), np.float32), calls, "dxcam", 0)))
        elif op == "monitors":
            cp._send(out, ("ok", ([{{"index": 0, "width": 1, "height": 1, "primary": True}}], 0)))
        else:
            cp._send(out, ("ok", None))
''')

ROOT = __file__.rsplit("tests", 1)[0].rstrip("\\/") + "/src"
PTS = [[0.0, 0.5], [1.0, 0.5], [0.5, 0.0]]


def sampler_for(tmp_path, mode):
    script = tmp_path / f"fake_{mode}.py"
    script.write_text(FAKE.format(root=ROOT, mode=mode), encoding="utf-8")
    return cp.RemoteSampler(command=[sys.executable, str(script)])


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(cp, "SAMPLE_TIMEOUT", 0.6)
    monkeypatch.setattr(cp, "MAX_BACKOFF", 0.3)


def test_samples_come_back_from_the_helper(tmp_path):
    s = sampler_for(tmp_path, "ok")
    try:
        arr = s.capture_and_sample(PTS, 0.06, True)
        assert arr.shape == (3, 3) and float(arr[0, 0]) == 1.0
        assert s.frame_id == 1 and s.backend == "dxcam" and s.helper_status()["helper_restarts"] == 0
        s.switch_monitor(1)                                   # travels with the next request
        assert float(s.capture_and_sample(PTS, 0.06, True)[0, 0]) == 2.0
        assert s.list_monitors()[0]["primary"] is True
    finally:
        s.close()


def test_a_stuck_capture_call_is_killed_and_replaced(tmp_path):
    s = sampler_for(tmp_path, "hang")
    try:
        assert s.capture_and_sample(PTS, 0.06, True) is not None
        first_pid = s.helper_status()["helper_pid"]
        t0 = time.monotonic()
        assert s.capture_and_sample(PTS, 0.06, True) is None          # the 2nd call never returns
        assert time.monotonic() - t0 < 2.0                              # ...but the caller got its thread back
        assert s.helper_status()["helper_restarts"] == 1 and s.helper_status()["helper_pid"] is None
        time.sleep(0.5)                                                 # backoff
        assert s.capture_and_sample(PTS, 0.06, True) is not None        # a new helper answers again
        assert s.helper_status()["helper_pid"] != first_pid
    finally:
        s.close()


def test_a_helper_that_dies_is_replaced(tmp_path):
    s = sampler_for(tmp_path, "die_at_2")
    try:
        assert s.capture_and_sample(PTS, 0.06, True) is not None
        assert s.capture_and_sample(PTS, 0.06, True) is None
        assert s.helper_status()["helper_restarts"] == 1
        assert "Error" in s.helper_status()["helper_last_restart"] or "exited" in s.helper_status()["helper_last_restart"]
    finally:
        s.close()


def test_restarts_back_off_instead_of_spinning(tmp_path):
    s = cp.RemoteSampler(command=[sys.executable, "-c", "import sys; sys.exit(1)"])
    try:
        for _ in range(6):
            s.capture_and_sample(PTS, 0.06, True)
        assert s.helper_status()["helper_restarts"] <= 2            # most calls were skipped during the backoff
    finally:
        s.close()


def test_falls_back_to_in_process_capture_when_helpers_never_start(tmp_path, monkeypatch):
    class Local:
        monitor_idx, frame_id, backend = 0, 7, "mss"

        def switch_monitor(self, idx):
            self.monitor_idx = idx

        def capture_and_sample(self, pts, box, bars):
            return np.ones((len(pts), 3), np.float32)

        def close(self):
            pass

    monkeypatch.setattr("core.screen_capture.ScreenSampler", lambda **kw: Local())
    monkeypatch.setattr(cp, "MAX_BACKOFF", 0.01)
    s = cp.RemoteSampler(command=[sys.executable, "-c", "import sys; sys.exit(1)"])
    try:
        out = None
        for _ in range(40):
            out = s.capture_and_sample(PTS, 0.06, True)
            if out is not None:
                break
            time.sleep(0.03)
        assert out is not None and s.helper_status()["isolated"] is False and s.backend == "mss"
    finally:
        s.close()


def test_the_real_helper_loop_serves_requests_over_pipes(tmp_path, monkeypatch):
    """run_helper end to end with a stub sampler class (no GPU)."""
    import io

    class Stub:
        monitor_idx, frame_id, backend, device_idx = 0, 3, "stub", 0

        def __init__(self, monitor_idx=0):
            pass

        def capture_and_sample(self, pts, box, bars):
            return np.full((len(pts), 3), 9.0, np.float32)

        def list_monitors(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("core.screen_capture.ScreenSampler", Stub)
    req = io.BytesIO()
    cp._send(req, ("sample", {"points": np.asarray(PTS, np.float32), "box": 0.06, "bars": True, "monitor": 0}))
    cp._send(req, ("boom", {}))
    cp._send(req, ("close", None))
    req.seek(0)
    out = io.BytesIO()

    class FakeStdin:
        buffer = req

    class FakeStdout:
        buffer = out

    monkeypatch.setattr(sys, "stdin", FakeStdin())
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    assert cp.run_helper() == 0
    monkeypatch.undo()
    out.seek(0)
    ok, payload = cp._recv(out)
    assert ok == "ok" and payload[0].shape == (3, 3) and payload[2] == "stub"
    err, text = cp._recv(out)
    assert err == "err" and "unknown op" in text


def test_an_unused_helper_is_given_back_and_a_new_one_starts_on_demand(tmp_path):
    s = sampler_for(tmp_path, "ok")
    try:
        assert s.capture_and_sample(PTS, 0.06, True) is not None and s.helper_status()["helper_pid"]
        s.hibernate_later(0.8)
        time.sleep(1.6)
        assert s.helper_status()["helper_pid"] is None and s.helper_status()["helper_restarts"] == 0   # released, not a failure
        assert s.capture_and_sample(PTS, 0.06, True) is not None and s.helper_status()["helper_pid"]
    finally:
        s.close()


def test_using_the_helper_again_cancels_the_release(tmp_path):
    s = sampler_for(tmp_path, "ok")
    try:
        s.capture_and_sample(PTS, 0.06, True)
        s.hibernate_later(1.0)
        time.sleep(0.6)
        s.capture_and_sample(PTS, 0.06, True)            # the user came back to the screen mode
        time.sleep(0.9)
        assert s.helper_status()["helper_pid"] is not None
    finally:
        s.close()


def test_a_starting_helper_is_reported_as_warming_up_not_failed(tmp_path):
    s = sampler_for(tmp_path, "ok")
    try:
        assert s.helper_status()["starting"] is True            # nothing launched yet
        s.capture_and_sample(PTS, 0.06, True)
        assert s.helper_status()["starting"] is False
        s._fail("test")
        assert s.helper_status()["starting"] is False            # a failure must show as a stall, not as warming up
    finally:
        s.close()


def test_a_planned_restart_is_not_a_failure_and_needs_no_backoff(tmp_path):
    s = sampler_for(tmp_path, "ok")
    try:
        s.capture_and_sample(PTS, 0.06, True)
        old = s.helper_status()["helper_pid"]
        s.restart("display change")
        arr = s.capture_and_sample(PTS, 0.06, True)       # may need one extra call to notice the dead helper
        if arr is None:
            arr = s.capture_and_sample(PTS, 0.06, True)
        assert arr is not None
        st = s.helper_status()
        assert st["helper_restarts"] == 0 and st["planned_restarts"] == 1 and st["helper_pid"] != old
    finally:
        s.close()
