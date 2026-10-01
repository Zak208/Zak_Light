"""
Screen capture in a separate, restartable helper process.

DXGI Desktop Duplication can block forever when Windows takes the desktop away (session lock, exclusive
full-screen game, GPU reset, resolution change). A call stuck inside the driver cannot be cancelled from
Python, so a capture thread would be lost for good. Here the capture lives in a child process instead:
when a call does not answer in time the child is killed and a fresh one is started, which also gives back
every GPU surface it held. The engine only ever sees a normal sampler that occasionally returns None.

The child is this same program started with `--capture-helper` (see app.py). It imports only the capture
code, talks over its stdin/stdout pipes and exits by itself when the pipe closes (the engine died).
"""

import logging
import os
import pickle
import queue
import struct
import subprocess
import sys
import threading
import time
from typing import List, Optional

import numpy as np

log = logging.getLogger("zak_light")

SAMPLE_TIMEOUT = 3.0        # a capture call that takes longer than this counts as stuck
START_TIMEOUT = 20.0        # the first answer includes opening DXGI on every adapter
MAX_BACKOFF = 20.0          # longest wait before starting a replacement helper
GIVE_UP_AFTER = 3           # helpers that die before answering even once -> capture in-process instead
MONITORS_TTL = 5.0
IDLE_RELEASE = 60.0         # seconds without capture before the helper (and its ~85 MB) is given back
CREATE_NO_WINDOW = 0x08000000


# ---- framing: 4-byte length + pickle (both ends are this program) -------------------------------

def _send(stream, obj):
    data = pickle.dumps(obj, protocol=4)
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


def _recv(stream):
    head = stream.read(4)
    if len(head) < 4:
        return None
    (size,) = struct.unpack("<I", head)
    data = stream.read(size)
    if len(data) < size:
        return None
    return pickle.loads(data)


# ---- helper side ------------------------------------------------------------------------------

def run_helper() -> int:
    """Entry point of `--capture-helper`: serves capture requests until the pipe closes."""
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr            # stray prints must never corrupt the binary protocol
    sampler = None
    while True:
        try:
            msg = _recv(inp)
        except Exception:
            return 1
        if msg is None or msg[0] == "close":
            break
        op, args = msg
        try:
            if op == "ping":
                reply = ("ok", None)
            else:
                if sampler is None:
                    from core.screen_capture import ScreenSampler
                    sampler = ScreenSampler(monitor_idx=args.get("monitor", 0))
                if op == "sample":
                    if args["monitor"] != sampler.monitor_idx:
                        sampler.switch_monitor(args["monitor"])
                    sampler.detect_black_bars = args["bars"]
                    arr = sampler.capture_and_sample(args["points"], args["box"], args["bars"])
                    reply = ("ok", (arr, sampler.frame_id, sampler.backend, sampler.device_idx))
                elif op == "monitors":
                    reply = ("ok", (sampler.list_monitors(), sampler.device_idx))
                else:
                    reply = ("err", f"unknown op {op}")
        except Exception as e:
            reply = ("err", f"{type(e).__name__}: {e}")
        try:
            _send(out, reply)
        except Exception:
            return 1
    if sampler is not None:
        try:
            sampler.close()
        except Exception:
            pass
    return 0


# ---- engine side ------------------------------------------------------------------------------

class _Helper:
    """One child process and the thread that collects its answers."""

    def __init__(self, command: List[str]):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.replies: "queue.Queue" = queue.Queue()
        self.answered = False
        threading.Thread(target=self._read, name="capture-helper-io", daemon=True).start()

    def _read(self):
        try:
            while True:
                msg = _recv(self.proc.stdout)
                if msg is None:
                    break
                self.replies.put(msg)
        except Exception:
            pass
        self.replies.put(None)          # EOF: the helper is gone

    def request(self, op: str, args, timeout: float):
        _send(self.proc.stdin, (op, args))
        try:
            msg = self.replies.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"capture helper did not answer within {timeout:.0f} s") from None
        if msg is None:
            raise ConnectionError("capture helper exited")
        self.answered = True
        return msg

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            pass


class RemoteSampler:
    """Drop-in replacement for ScreenSampler whose capture runs in a helper process."""

    def __init__(self, monitor_idx: int = 0, detect_black_bars: bool = True, command: Optional[List[str]] = None):
        if command is None:
            from core import system as winsys
            command = winsys.self_command("--capture-helper")
        self._command = command
        self.monitor_idx = monitor_idx
        self.detect_black_bars = detect_black_bars
        self.frame_id = 0
        self.device_idx = 0
        self._backend = "dxcam"
        self._helper: Optional[_Helper] = None
        self._lock = threading.Lock()
        self._local = None               # in-process sampler, only if helpers cannot run at all
        self._monitors: List[dict] = []
        self._monitors_at = -1e9
        self._failures = 0               # consecutive failed helpers
        self._dead_starts = 0            # helpers that never answered
        self._retry_at = 0.0
        self.restarts = 0
        self.last_restart_reason = ""
        self._last_used = time.monotonic()
        self._idle_timer: Optional[threading.Timer] = None
        self._planned = False            # the helper is being restarted on purpose: not a failure
        self.planned_restarts = 0

    # ---- process management -------------------------------------------------------------------

    def _fail(self, reason: str):
        helper, self._helper = self._helper, None
        if helper is not None:
            if not helper.answered:
                self._dead_starts += 1
            helper.kill()
        self._failures += 1
        self.restarts += 1
        self.last_restart_reason = reason
        backoff = min(MAX_BACKOFF, 2.0 ** (self._failures - 1))
        self._retry_at = time.monotonic() + backoff
        log.warning("Capture helper restarted (%s); next attempt in %.0f s", reason, backoff)

    def _ensure(self) -> Optional[_Helper]:
        if self._helper is not None and self._helper.proc.poll() is None:
            return self._helper
        if self._helper is not None:
            if self._planned:
                self._planned, self._helper = False, None
            else:
                self._fail("helper process exited")
        if time.monotonic() < self._retry_at:
            return None
        try:
            self._helper = _Helper(self._command)
        except Exception as e:
            self._fail(f"could not start: {type(e).__name__}")
            return None
        return self._helper

    def _call(self, op: str, args, timeout: Optional[float] = None):
        helper = self._ensure()
        if helper is None:
            return None
        limit = timeout if timeout is not None else (SAMPLE_TIMEOUT if helper.answered else START_TIMEOUT)
        try:
            status, payload = helper.request(op, args, limit)
        except (TimeoutError, ConnectionError, OSError, pickle.UnpicklingError) as e:
            if self._planned:                      # we killed it ourselves: start a fresh one right away
                self._planned, self._helper = False, None
                helper.kill()
                return None
            self._fail(f"{type(e).__name__}")
            return None
        self._failures = 0 if status == "ok" else self._failures
        self._dead_starts = 0
        if status != "ok":
            raise RuntimeError(payload)
        return payload

    # ---- the ScreenSampler interface ----------------------------------------------------------

    def _use_local(self):
        if self._local is None:
            from core.screen_capture import ScreenSampler
            log.error("Capture helper cannot start: capturing inside the main process instead")
            self._local = ScreenSampler(monitor_idx=self.monitor_idx, detect_black_bars=self.detect_black_bars)
        return self._local

    @property
    def backend(self) -> str:
        return self._local.backend if self._local else self._backend

    def capture_and_sample(self, points, box_size: float, detect_black_bars: bool) -> Optional[np.ndarray]:
        if len(points) == 0:
            return np.zeros((0, 3), dtype=np.float32)
        self._last_used = time.monotonic()
        with self._lock:
            if self._local is not None or self._dead_starts >= GIVE_UP_AFTER:
                local = self._use_local()
                if local.monitor_idx != self.monitor_idx:
                    local.switch_monitor(self.monitor_idx)
                arr = local.capture_and_sample(points, box_size, detect_black_bars)
                self.frame_id = local.frame_id
                return arr
            payload = self._call("sample", {
                "points": np.asarray(points, dtype=np.float32), "box": float(box_size),
                "bars": bool(detect_black_bars), "monitor": self.monitor_idx,
            })
        if payload is None:
            return None
        arr, self.frame_id, self._backend, self.device_idx = payload
        return arr

    def switch_monitor(self, monitor_idx: int):
        self.monitor_idx = monitor_idx   # the helper switches on its next request

    def list_monitors(self) -> List[dict]:
        if self._local is not None:
            return self._local.list_monitors()
        if time.monotonic() - self._monitors_at < MONITORS_TTL:
            return self._monitors
        if not self._lock.acquire(timeout=0.3):      # a capture call is running: serve what we know
            return self._monitors
        try:
            payload = self._call("monitors", {"monitor": self.monitor_idx}, timeout=SAMPLE_TIMEOUT)
        except RuntimeError:
            payload = None
        finally:
            self._lock.release()
        if payload is not None:
            self._monitors, self.device_idx = payload
            self._monitors_at = time.monotonic()
        return self._monitors

    def restart(self, reason: str = ""):
        """
        Replaces the helper right now and without a back-off (a display change, for example, leaves the old
        DXGI duplication useless). Safe to call from any thread, also while a capture call is in flight.
        """
        self._monitors_at = -1e9
        helper = self._helper
        if helper is None or self._local is not None:
            return
        self._planned = True
        self.planned_restarts += 1
        log.info("Capture helper restarted on purpose (%s)", reason or "requested")
        try:
            helper.proc.kill()
        except Exception:
            pass

    def hibernate_later(self, delay: Optional[float] = None):
        """Ends the helper if no capture is requested for `delay` seconds (the next capture starts a new one)."""
        delay = IDLE_RELEASE if delay is None else delay
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(delay, self._release_if_idle, args=(delay,))
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _release_if_idle(self, delay: float):
        if time.monotonic() - self._last_used < delay - 0.5 or not self._lock.acquire(timeout=1.0):
            return
        try:
            helper, self._helper = self._helper, None
            if helper is not None:
                log.info("Capture helper released after %.0f s without capturing", delay)
                try:
                    _send(helper.proc.stdin, ("close", None))
                    helper.proc.wait(timeout=1.5)
                except Exception:
                    pass
                helper.kill()
        finally:
            self._lock.release()

    def helper_status(self) -> dict:
        starting = self._local is None and self._failures == 0 and (self._helper is None or not self._helper.answered)
        return {"isolated": self._local is None, "starting": starting, "helper_restarts": self.restarts, "planned_restarts": self.planned_restarts,
                "helper_last_restart": self.last_restart_reason or None,
                "helper_pid": self._helper.proc.pid if self._helper else None}

    def close(self):
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        helper, self._helper = self._helper, None
        if helper is not None:
            try:
                _send(helper.proc.stdin, ("close", None))
                helper.proc.wait(timeout=1.5)
            except Exception:
                pass
            helper.kill()
        if self._local is not None:
            self._local.close()
