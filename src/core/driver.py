"""
DX Light USB HID Hardware Driver.
Direct communication with QinHeng / ROBOBLOQ USB HID controller (VID 0x1A86, PID 0xFE07).
"""

import logging
import time
import threading
from typing import List, Tuple, Optional

import numpy as np

try:
    import hid
except ImportError:  # the package is only needed on a machine with the controller
    hid = None

from .protocol import (
    merge_runs,
    build_sync_screen_packet,
    build_brightness_packet,
    build_turn_off_packet
)

log = logging.getLogger("zak_light")

TARGET_VID = 0x1A86  # 6790
TARGET_PID = 0xFE07  # 65031
TARGET_INTERFACE = 0
KEEPALIVE_SECONDS = 1.0  # unchanged frames are re-sent at least this often
DEADBAND = 1             # channel changes this small are not worth a USB write...
DEADBAND_MIN_LEVEL = 16  # ...unless the colors are so dark that one step is visible
HARDWARE_BRIGHTNESS = 255  # brightness is applied in software only; the register stays at maximum


class DxLightDriver:
    def __init__(self, hid_module=None):
        self._hid = hid_module if hid_module is not None else hid  # injectable for tests
        self.device = None
        self.path: Optional[bytes] = None
        self._seq_id: int = 1
        # Re-entrant: _write_chunks() (lock held) calls connect()/disconnect(), which lock again
        self._lock = threading.RLock()
        self.is_connected: bool = False
        self._needs_init = False
        self._last_segments = None
        self._last_array = None
        self.last_frame = None       # last (N, 3) frame shown on the strip: the start of a cross-fade
        self._last_send = 0.0
        self._last_error = None
        self.writes_ok = 0           # successful USB writes (diagnostics)
        self.writes_failed = 0
        self.last_write = 0.0        # time.monotonic() of the last successful write

    def invalidate(self):
        """Forget the last streamed frame so the next one is always sent."""
        self._last_segments = None
        self._last_array = None

    def consume_needs_init(self) -> bool:
        """True once after every (re)connection: the caller must re-apply the current mode."""
        flag, self._needs_init = self._needs_init, False
        return flag

    def _next_seq_id(self) -> int:
        self._seq_id += 1
        if self._seq_id >= 255:
            self._seq_id = 1
        return self._seq_id

    def _log_once(self, message: str):
        """Logs a message the first time it happens (and again after the state changes)."""
        if message != self._last_error:
            self._last_error = message
            log.warning(message)

    @staticmethod
    def _pick_interface(devices):
        """Interface 0 of the controller; if hidapi reports -1 (non-composite device), the only entry."""
        for d in devices:
            if d.get("interface_number") == TARGET_INTERFACE:
                return d
        for d in devices:
            if d.get("interface_number") == -1:
                return d
        return None

    def connect(self) -> bool:
        """Finds and opens the DX Light USB HID device."""
        if self._hid is None:
            return False
        with self._lock:
            try:
                target = self._pick_interface(self._hid.enumerate(TARGET_VID, TARGET_PID))
                if not target:
                    return False

                self.path = target["path"]
                device = self._hid.device()
                device.open_path(self.path)
                self.device = device
                self.is_connected = True
                self._needs_init = True
                self.invalidate()
                # Fresh connection: make sure the hardware brightness register is at maximum
                device.write(build_brightness_packet(HARDWARE_BRIGHTNESS, self._next_seq_id())[0])
                log.info("USB device connected")
                self._last_error = None
                return True
            except Exception as e:
                self._log_once(f"USB connect failed: {e}")
                self.device = None
                self.is_connected = False
                return False

    def disconnect(self):
        """Closes the device connection."""
        with self._lock:
            if self.device:
                try:
                    self.device.close()
                except Exception:
                    pass
                self.device = None
            self.is_connected = False

    def _write_chunks(self, chunks: List[bytearray]) -> bool:
        if not self.is_connected or not self.device:
            if not self.connect():
                return False

        try:
            for chunk in chunks:
                # hidapi reports a failed write by returning -1, it does not raise
                if self.device.write(chunk) < 0:
                    raise IOError("hid write returned -1")
            self.writes_ok += 1
            self.last_write = time.monotonic()
            return True
        except Exception as e:
            # Unplugged, or the device re-enumerated after suspend/resume: drop it so the next
            # call reconnects instead of writing to a dead handle forever
            self._log_once(f"USB write failed, will reconnect: {e}")
            self.writes_failed += 1
            self.disconnect()
            return False

    def send_sync_screen(self, segments: List[Tuple[int, int, int, int, int]], dedupe: bool = False) -> bool:
        """
        Sends high-speed screen synchronization frame.
        segments: list of (start_led, r, g, b, end_led) tuples.
        dedupe: skip the USB write when the frame is identical to the previous one
                (a keep-alive is still sent every KEEPALIVE_SECONDS).
        """
        if not segments:
            return True
        with self._lock:
            now = time.monotonic()
            if dedupe and segments == self._last_segments and now - self._last_send < KEEPALIVE_SECONDS:
                return True
            chunks = build_sync_screen_packet(segments, self._next_seq_id())
            self._last_array = None  # send_colors() records its own frame after this returns
            ok = self._write_chunks(chunks)
            self._last_segments = list(segments) if ok else None
            self._last_send = now
            return ok

    def send_colors(self, colors: np.ndarray) -> bool:
        """
        Streams a frame given as an (N, 3) uint8 array, already in the strip's channel order.
        Skips the write when nothing visible changed (exact bytes, or within DEADBAND) and
        merges equal neighbours into ranges to keep the USB packets short.
        """
        if colors.shape[0] == 0:
            return True
        with self._lock:
            now = time.monotonic()
            last = self._last_array
            if last is not None and last.shape == colors.shape and now - self._last_send < KEEPALIVE_SECONDS:
                if colors.tobytes() == last.tobytes():
                    return True
                if max(int(colors.max()), int(last.max())) >= DEADBAND_MIN_LEVEL:
                    if int(np.abs(colors.astype(np.int16) - last).max()) <= DEADBAND:
                        return True
            ok = self.send_sync_screen(merge_runs(colors.tolist()))
            self._last_array = colors.copy() if ok else None
            if ok:
                self.last_frame = self._last_array
            return ok

    def set_brightness(self, level: int) -> bool:
        """Sets hardware brightness level (0 - 255)."""
        with self._lock:
            self.invalidate()
            chunks = build_brightness_packet(level, self._next_seq_id())
            return self._write_chunks(chunks)

    def turn_off(self) -> bool:
        """Turns off the LEDs."""
        with self._lock:
            self.last_frame = None
            self.invalidate()
            chunks = build_turn_off_packet(self._next_seq_id())
            return self._write_chunks(chunks)
