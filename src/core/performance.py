"""
On-demand performance measurement: one aggregate sample per second, only while the user records.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from engine import EngineState

log = logging.getLogger("zak_light")


class PerformanceRecorder:
    """In-memory, low-frequency measurement for a real hardware test.

    It collects aggregate engine data once per second only while the user explicitly records a
    scenario. Samples are never persisted automatically and contain no screen pixels, file paths
    or application names; the UI exports the completed report when the user chooses to do so.
    """

    INTERVAL_S = 1.0
    MAX_SAMPLES = 7_200  # two hours is ample, bounded even if a user forgets to stop it

    def __init__(self):
        self._lock = threading.Lock()
        self._active = False
        self._generation = 0
        self._scenario = "desktop"
        self._started_at = 0.0
        self._started_utc = ""
        self._last_sample_at = 0.0
        self._samples = []
        self._samples_truncated = False
        self._baseline = {}
        self._last_report = None

    @staticmethod
    def _stats(samples, key: str) -> dict:
        values = [float(sample[key]) for sample in samples if sample.get(key) is not None]
        if not values:
            return {"min": None, "mean": None, "max": None}
        return {
            "min": round(min(values), 2),
            "mean": round(sum(values) / len(values), 2),
            "max": round(max(values), 2),
        }

    def start(self, state: "EngineState", scenario: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._active:
                return False
            health = state.health()
            self._active = True
            self._generation += 1
            self._scenario = scenario
            self._started_at = now
            self._started_utc = datetime.now(timezone.utc).isoformat()
            self._last_sample_at = 0.0
            self._samples = []
            self._samples_truncated = False
            self._baseline = {
                "usb_writes": health["usb_writes"],
                "usb_failures": health["usb_failures"],
                "capture_errors": health["capture"]["errors"],
            }
            self._last_report = None
        self.record(state, force=True)
        return True

    def record(self, state: "EngineState", force: bool = False):
        now = time.monotonic()
        with self._lock:
            if not self._active or (not force and now - self._last_sample_at < self.INTERVAL_S):
                return
            generation = self._generation
            started_at = self._started_at

        metrics, health = state.metrics(), state.health()
        capture = health["capture"]
        sample = {
            "t_s": round(now - started_at, 1),
            "cpu_pct": metrics["cpu_pct"],
            "ram_mb": metrics["ram_mb"],
            "frame_ms": metrics["frame_ms"],
            "capture_fps": capture["fps"],
            "latency_ms": health["latency_ms"],
            "usb_writes": health["usb_writes"],
            "usb_failures": health["usb_failures"],
            "capture_errors": capture["errors"],
            "capture_stalled": health["capture_stalled"],
        }
        with self._lock:
            if not self._active or generation != self._generation:
                return
            if len(self._samples) >= self.MAX_SAMPLES:
                self._samples.pop(0)
                self._samples_truncated = True
            self._samples.append(sample)
            self._last_sample_at = now

    def stop(self, state: "EngineState", reason: str = "user") -> dict | None:
        self.record(state, force=True)
        now = time.monotonic()
        health = state.health()
        with self._lock:
            if not self._active:
                return self._last_report
            samples = list(self._samples)
            report = {
                "format": "zak_light_performance_measurement",
                "format_version": 1,
                "scenario": self._scenario,
                "started_utc": self._started_utc,
                "duration_s": round(now - self._started_at, 1),
                "stop_reason": reason,
                "samples_truncated": self._samples_truncated,
                "configuration": {
                    "performance_mode": state.config.performance_mode,
                    "target_fps": state.config.target_fps,
                    "mode": state.mode,
                    "led_count": state.config.led_count,
                    "monitor_index": state.config.monitor_index,
                    "capture_backend": state.screen_sampler.backend,
                },
                "summary": {
                    "cpu_pct": self._stats(samples, "cpu_pct"),
                    "ram_mb": self._stats(samples, "ram_mb"),
                    "frame_ms": self._stats(samples, "frame_ms"),
                    "capture_fps": self._stats(samples, "capture_fps"),
                    "latency_ms": self._stats(samples, "latency_ms"),
                    "usb_writes": health["usb_writes"] - self._baseline["usb_writes"],
                    "usb_failures": health["usb_failures"] - self._baseline["usb_failures"],
                    "capture_errors": health["capture"]["errors"] - self._baseline["capture_errors"],
                    "capture_stalled_samples": sum(bool(s["capture_stalled"]) for s in samples),
                },
                "samples": samples,
            }
            self._active = False
            self._last_report = report
            return report

    def status(self) -> dict:
        with self._lock:
            duration = time.monotonic() - self._started_at if self._active else 0.0
            return {
                "active": self._active,
                "scenario": self._scenario if self._active else None,
                "duration_s": round(duration, 1),
                "samples": len(self._samples) if self._active else 0,
                "report_ready": self._last_report is not None,
            }

    def last_report(self) -> dict | None:
        with self._lock:
            return self._last_report
