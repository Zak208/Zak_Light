"""
Screen capture on its own thread: a capture call that never returns must not freeze the engine loop.
"""

import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from engine import EngineState

log = logging.getLogger("zak_light")


class ScreenWorker:
    """
    Runs the screen capture on its own thread and publishes the latest result.

    If a capture call never returns (Windows took the desktop away), this thread is stuck but the engine is
    not: it keeps answering mode changes and reports the stall. When the call finally returns, or a new worker
    is started, results from a stale generation are ignored.
    """

    def __init__(self, state: "EngineState"):
        self.state = state
        self._lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._stop: Optional[threading.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._generation = 0
        self._raw = None
        self._frame_id = 0
        self._stamp = 0.0
        self.started_at = 0.0
        # A DXGI call can get stuck while Windows changes desktop ownership. Never start a
        # second call on the same ScreenSampler: dxcam is not safe to drive from two threads.
        self._capture_inflight = False
        self._capture_started_at = 0.0
        self._restart_requested = False
        self.capture_calls = 0
        self.capture_errors = 0
        self.last_capture_ms = 0.0
        self.last_error_type = ""
        self.last_error_at = 0.0
        self._result_stamps = deque(maxlen=120)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._stop and not self._stop.is_set())

    def start(self):
        with self._control_lock:
            if self.running:
                return
            if self._capture_inflight:
                self._restart_requested = True
                return
            self._generation += 1
            self._stop = threading.Event()
            self._restart_requested = False
            stop, generation = self._stop, self._generation
        with self._lock:
            self._raw, self._stamp = None, 0.0
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, args=(stop, generation), name="capture", daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop:
            self._stop.set()  # an orphan blocked in a capture call exits on its own if it ever returns
        # Not capturing any more (another mode): a separate capture process can be given back after a while
        hibernate = getattr(self.state.screen_sampler, "hibernate_later", None)
        if hibernate:
            hibernate()

    def snapshot(self):
        with self._lock:
            return self._raw, self._frame_id, self._stamp

    def health(self) -> dict:
        """Capture telemetry and whether an old blocked call prevents a safe restart."""
        now = time.monotonic()
        with self._control_lock:
            inflight = self._capture_inflight
            age = round(now - self._capture_started_at, 1) if inflight else 0.0
            pending = self._restart_requested
            calls, errors, last_ms = self.capture_calls, self.capture_errors, self.last_capture_ms
            error_type, error_at = self.last_error_type, self.last_error_at
        with self._lock:
            fps = sum(stamp >= now - 1.0 for stamp in self._result_stamps)
        extra = getattr(self.state.screen_sampler, "helper_status", None)
        return {
            **(extra() if extra else {"isolated": False}),
            "inflight": inflight,
            "age_s": age,
            "restart_pending": pending,
            "fps": fps,
            "calls": calls,
            "errors": errors,
            "last_ms": round(last_ms, 1),
            "last_error_type": error_type or None,
            "last_error_age_s": round(now - error_at, 1) if error_at else None,
        }

    def _run(self, stop: threading.Event, generation: int):
        state, last_error = self.state, None
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                cfg = state.config
                with self._control_lock:
                    if stop.is_set() or self._capture_inflight:
                        return
                    self._capture_inflight = True
                    self._capture_started_at = t0
                    self.capture_calls += 1
                try:
                    raw = state.screen_sampler.capture_and_sample(
                        points=state.screen_points, box_size=cfg.sample_box_size,
                        detect_black_bars=cfg.black_bar_detection
                    )
                finally:
                    with self._control_lock:
                        self._capture_inflight = False
                        self._capture_started_at = 0.0
                        self.last_capture_ms = (time.monotonic() - t0) * 1000.0
                last_error = None
                with self._control_lock:
                    self.last_error_type = ""
                    self.last_error_at = 0.0
                if raw is not None and not stop.is_set():
                    with self._lock:
                        stamp = time.monotonic()
                        if generation == self._generation:
                            self._raw, self._frame_id, self._stamp = raw, state.screen_sampler.frame_id, stamp
                            self._result_stamps.append(stamp)
            except Exception as e:
                with self._control_lock:
                    self.capture_errors += 1
                    self.last_error_type = type(e).__name__
                    self.last_error_at = time.monotonic()
                if str(e) != last_error:
                    last_error = str(e)
                    log.exception("Screen capture error")
                stop.wait(0.5)
                continue
            fps = max(15.0, min(60.0, state.config.target_fps * state.fps_scale))
            stop.wait(max(0.001, 1.0 / fps - (time.monotonic() - t0)))
