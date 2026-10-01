"""
High-Performance Screen Border & Interactive Multi-Point Sampler for Zak_light.
Features:
- DXGI (DirectX Desktop Duplication) with MSS fallback (< 1% CPU overhead).
- Interactive Custom Point Sampling: every LED has its own draggable (x, y) box on the screen.
- Automatic Letterbox (black bar) detection for movies.
- Multi-monitor support (Display 1, Display 2, etc.).
"""

import os
import re
import time
import logging
from typing import List, Tuple, Optional
import numpy as np

from .strip_capture import (
    Region,
    StripGrabber,
    letterbox_offsets_from_rows,
    plan_regions,
    probe_regions,
    quantize_offsets,
)

log = logging.getLogger("zak_light")

try:
    import dxcam
    # Suppress factory singleton warning on repeated output query
    logging.getLogger("dxcam").setLevel(logging.ERROR)
    HAS_DXCAM = True
except ImportError:
    HAS_DXCAM = False

import cv2

THUMB_WIDTH = 256  # frames are reduced to this width before sampling


def _new_mss():
    """mss (GDI capture) is imported only when dxcam cannot be used."""
    import mss
    return mss.mss()


OFFSET_CONFIRM_FRAMES = 3  # consecutive identical readings needed to accept new letterbox bars


def _covered(new_regions: List[Region], old_regions: List[Region]) -> bool:
    """True if every new region lies inside one of the old ones (its pixels are already on the canvas)."""
    return all(
        any(o[0] <= r[0] and o[1] <= r[1] and o[2] >= r[2] and o[3] >= r[3] for o in old_regions)
        for r in new_regions
    )


class ScreenSampler:
    def __init__(
        self,
        monitor_idx: int = 0,
        detect_black_bars: bool = True
    ):
        self.monitor_idx = monitor_idx
        self.detect_black_bars = detect_black_bars

        self._dxcam_cameras = {}
        self._dxcam_inst = None
        self._mss_inst = None
        self.device_idx = 0              # graphics adapter that owns the captured output

        self._init_strip_state()
        self._init_backend()

    def _init_strip_state(self):
        """State of the strip capture (see capture_and_sample); also used by tests with a fake camera."""
        self._strips_forced_off = bool(os.environ.get("ZAK_FULLFRAME"))  # escape hatch / benchmarking
        self._strips_failures = 0
        self._strips_retry_at = 0.0      # after a failure, full-frame capture is used until this time
        self._grabber: Optional[StripGrabber] = None
        self._regions: Optional[List[Region]] = []
        self._n_probes = 0
        self._offsets = (0, 0)           # letterbox bars in screen pixels, quantised
        self._pending_offsets = (0, 0)   # candidate new bars, adopted after OFFSET_CONFIRM_FRAMES
        self._pending_count = 0
        self._plan_key = None
        self._canvas: Optional[np.ndarray] = None
        self._canvas_ready = False
        self.frame_id = 0                # increments whenever a new screen frame was read
        self._failed_monitors = {}       # monitor index -> time of the last failed open

    def _get_or_create_dxcam(self, monitor_idx: int):
        if not HAS_DXCAM:
            return None
        # Reuse existing camera if valid and not released
        if monitor_idx in self._dxcam_cameras:
            cam = self._dxcam_cameras[monitor_idx]
            if cam is not None and not getattr(cam, "is_released", False):
                return cam
        # An unavailable monitor is not retried on every settings change (each try is slow)
        if time.monotonic() - self._failed_monitors.get(monitor_idx, -1e9) < 5.0:
            return None
        # Desktop Duplication only works on the adapter that owns the output, so try each
        # adapter until one accepts it (matters on PCs with an iGPU + a dedicated GPU).
        for device_idx in range(4):
            try:
                cam = dxcam.create(device_idx=device_idx, output_idx=monitor_idx)
            except Exception:
                cam = None
            if cam is not None:
                self._dxcam_cameras[monitor_idx] = cam
                self.device_idx = device_idx
                return cam
        self._failed_monitors[monitor_idx] = time.monotonic()
        return None

    def _init_backend(self):
        """Attempts to initialize DXCam (DirectX Desktop Duplication), falls back to MSS."""
        self._dxcam_inst = self._get_or_create_dxcam(self.monitor_idx)
        if not self._dxcam_inst:
            try:
                self._mss_inst = _new_mss()
            except Exception:
                self._mss_inst = None

    def list_monitors(self) -> List[dict]:
        """Outputs of the adapter that owns the capture: [{index, width, height, primary}]."""
        if not HAS_DXCAM:
            return []
        try:
            info = dxcam.output_info()
        except Exception:
            return []
        monitors = []
        for line in info.splitlines():
            m = re.match(r"Device\[(\d+)\] Output\[(\d+)\]: Res:\((\d+), (\d+)\).*Primary:(True|False)", line)
            if m and int(m.group(1)) == self.device_idx:
                monitors.append({"index": int(m.group(2)), "width": int(m.group(3)),
                                 "height": int(m.group(4)), "primary": m.group(5) == "True"})
        return monitors

    @property
    def backend(self) -> str:
        return "dxcam" if self._dxcam_inst else "mss"

    def switch_monitor(self, monitor_idx: int):
        if self.monitor_idx == monitor_idx and self._dxcam_inst is not None:
            return
        self.monitor_idx = monitor_idx
        self._dxcam_inst = self._get_or_create_dxcam(self.monitor_idx)
        if not self._dxcam_inst and not self._mss_inst:
            try:
                self._mss_inst = _new_mss()
            except Exception:
                self._mss_inst = None

    def grab_frame(self) -> Optional[np.ndarray]:
        """
        Grabs a full RGB frame as a NumPy array (H, W, 3).
        Returns None if screen is locked or capture fails.
        """
        # Try DXCam first (GPU-direct, 0 copy)
        if self._dxcam_inst:
            try:
                frame = self._dxcam_inst.grab(copy=False, new_frame_only=False)
                if frame is not None:
                    return frame
            except Exception:
                log.debug("dxcam grab failed", exc_info=True)

        # Fallback to MSS (GDI/Desktop)
        if not self._mss_inst:
            try:
                self._mss_inst = _new_mss()
            except Exception:
                return None

        try:
            mon_list = self._mss_inst.monitors
            target_mon = mon_list[min(len(mon_list) - 1, max(1, self.monitor_idx + 1))]
            raw_shot = self._mss_inst.grab(target_mon)
            arr = np.frombuffer(raw_shot.raw, dtype=np.uint8).reshape((raw_shot.height, raw_shot.width, 4))
            # BGRA -> RGB
            return arr[:, :, [2, 1, 0]]
        except Exception:
            return None

    def sample_led_points(
        self,
        frame: np.ndarray,
        points: List[List[float]],
        box_size: float = 0.06,
        detect_black_bars: bool = True
    ) -> np.ndarray:
        """
        Samples the average RGB color around each LED point, fully vectorized.
        points: list of [x, y] normalized coordinates (0.0 <= x, y <= 1.0).
        box_size: fraction of screen dimension to average around each point.

        The frame is first reduced to a small thumbnail (INTER_AREA = proper averaging) and
        every box is then read from an integral image, so cost no longer depends on
        the screen resolution or on the LED count.
        """
        total = len(points)
        if total == 0:
            return np.zeros((0, 3), dtype=np.float32)

        H, W = frame.shape[:2]
        th = max(8, int(round(THUMB_WIDTH * H / W)))
        thumb = cv2.resize(frame[:, :, :3], (THUMB_WIDTH, th), interpolation=cv2.INTER_AREA)

        top_offset, bot_offset = self._letterbox_offsets(thumb) if detect_black_bars else (0, 0)
        return self._sample_thumb(thumb, np.asarray(points, dtype=np.float32), box_size, top_offset, bot_offset)

    @staticmethod
    def _sample_thumb(
        thumb: np.ndarray, pts: np.ndarray, box_size: float, top_offset: int, bot_offset: int
    ) -> np.ndarray:
        """Average color of every LED box on a thumbnail (offsets = letterbox rows in thumb pixels)."""
        th = thumb.shape[0]
        eff_h = th - top_offset - bot_offset
        cx = (pts[:, 0] * THUMB_WIDTH).astype(np.int32)
        cy = top_offset + (pts[:, 1] * eff_h).astype(np.int32)

        half_w = max(1, int(THUMB_WIDTH * box_size * 0.5))
        half_h = max(1, int(eff_h * box_size * 0.5))
        x0 = np.clip(cx - half_w, 0, THUMB_WIDTH - 1)
        x1 = np.clip(cx + half_w, x0 + 1, THUMB_WIDTH)
        y0 = np.clip(cy - half_h, 0, th - 1)
        y1 = np.clip(cy + half_h, y0 + 1, th)

        # Integral image: sum of any box in 4 lookups
        integral = cv2.integral(thumb, sdepth=cv2.CV_32S)
        sums = integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
        area = ((x1 - x0) * (y1 - y0)).astype(np.float32)[:, None]
        return (sums.astype(np.float32) / area)

    # ---- strip capture: read only the screen bands the LEDs look at --------------------------

    def capture_and_sample(
        self, points: List[List[float]], box_size: float, detect_black_bars: bool
    ) -> Optional[np.ndarray]:
        """
        Captures and samples in one step. With dxcam this copies only the edge bands from the
        GPU (a few % of the pixels) and falls back to full-frame grabs if that is not possible.
        Returns the (N, 3) LED colors, or None while no frame is available yet.
        """
        if len(points) == 0:
            return np.zeros((0, 3), dtype=np.float32)

        now = time.monotonic()
        if self._dxcam_inst is not None and not self._strips_forced_off and now >= self._strips_retry_at:
            try:
                result = self._capture_strips(np.asarray(points, dtype=np.float32), box_size, detect_black_bars)
                self._strips_failures = 0
                return result
            except Exception:
                # Not permanent: use full-frame capture for a while, then try the strips again
                self._strips_failures += 1
                delay = min(60.0, 2.0 ** self._strips_failures)
                self._strips_retry_at = now + delay
                log.exception("Strip capture failed (%d in a row); full-frame capture for %.0f s",
                              self._strips_failures, delay)
                self._reset_strips()

        frame = self.grab_frame()
        if frame is None:
            return None
        self.frame_id += 1  # full-frame grabs cannot tell whether the picture changed: count each one
        return self.sample_led_points(frame, points, box_size, detect_black_bars)

    def _reset_strips(self):
        if self._grabber is not None:
            self._grabber.release()
        self._grabber = None
        self._plan_key = None
        self._canvas = None

    def _capture_strips(self, pts: np.ndarray, box_size: float, detect_bars: bool) -> Optional[np.ndarray]:
        cam = self._dxcam_inst
        width, height = int(cam.width), int(cam.height)

        if self._grabber is None or self._grabber.cam is not cam:
            if self._grabber is not None:
                self._grabber.release()  # monitor switched: free the old staging surfaces
            self._grabber = StripGrabber(cam)
            self._plan_key = None

        if not detect_bars:
            self._offsets = (0, 0)  # bars detection switched off: stop compressing the picture
            self._pending_count = 0

        key = (width, height, round(box_size, 4), detect_bars, pts.tobytes(), self._offsets)
        if key != self._plan_key:
            regions = plan_regions(pts, box_size, width, height, *self._offsets)
            self._plan_key = key
            if regions is None:  # bands would cover most of the screen: full frames are cheaper
                self._regions = None
                return self._capture_full_fallback(pts, box_size, detect_bars)
            probes = probe_regions(width, height) if detect_bars else []

            th = max(8, int(round(THUMB_WIDTH * height / width)))
            old_bands = (self._regions or [])[:len(self._regions or []) - self._n_probes]
            reusable = (
                self._canvas is not None and self._canvas.shape == (th, THUMB_WIDTH, 3)
                and self._canvas_ready and _covered(regions, old_bands)
            )
            if not reusable:
                # The new bands include areas never read yet: wait for a frame that contains them
                # (a static screen only delivers one when something changes). Until then the strip
                # simply keeps showing the last colors.
                if self._canvas is None or self._canvas.shape != (th, THUMB_WIDTH, 3):
                    self._canvas = np.zeros((th, THUMB_WIDTH, 3), dtype=np.uint8)
                self._canvas_ready = False
            self._regions, self._n_probes = regions + probes, len(probes)

        if self._regions is None:
            return self._capture_full_fallback(pts, box_size, detect_bars)

        strips = self._grabber.grab(self._regions)
        if strips is not None:
            self.frame_id += 1
            n_bands = len(self._regions) - self._n_probes
            for region, strip in zip(self._regions[:n_bands], strips[:n_bands]):
                self._paste(region, strip, width, height)

            if self._n_probes:
                rows = np.max([s.mean(axis=(1, 2)) for s in strips[n_bands:]], axis=0)
                seen = quantize_offsets(*letterbox_offsets_from_rows(rows), height)
                if seen == self._offsets:
                    self._pending_count = 0
                else:
                    # Dark scenes make the detection flicker around its thresholds: only adopt new
                    # bars once the same reading repeats, otherwise every flip would replan the bands
                    self._pending_count = self._pending_count + 1 if seen == self._pending_offsets else 1
                    self._pending_offsets = seen
                    if self._pending_count >= OFFSET_CONFIRM_FRAMES:
                        self._offsets = seen
                        self._pending_count = 0
                        self._canvas_ready = False
                        return None
            self._canvas_ready = True

        if not self._canvas_ready:
            return None

        th = self._canvas.shape[0]
        top_t = int(round(self._offsets[0] * th / height))
        bot_t = int(round(self._offsets[1] * th / height))
        return self._sample_thumb(self._canvas, pts, box_size, top_t, bot_t)

    def _capture_full_fallback(self, pts, box_size, detect_bars):
        frame = self.grab_frame()
        if frame is None:
            return None
        self.frame_id += 1
        return self.sample_led_points(frame, pts.tolist(), box_size, detect_bars)

    def _paste(self, region, strip: np.ndarray, width: int, height: int):
        """Downscales a strip into its place on the thumbnail canvas."""
        th = self._canvas.shape[0]
        x0 = int(np.floor(region[0] * THUMB_WIDTH / width))
        x1 = min(THUMB_WIDTH, int(np.ceil(region[2] * THUMB_WIDTH / width)))
        y0 = int(np.floor(region[1] * th / height))
        y1 = min(th, int(np.ceil(region[3] * th / height)))
        if x1 > x0 and y1 > y0:
            # Each thumbnail pixel averages ~10x10 screen pixels; averaging every 3rd one of them
            # gives the same colour for ~9x less work (the LED boxes are far larger than that).
            sub = strip[::3, ::3, :3]
            self._canvas[y0:y1, x0:x1] = cv2.resize(sub, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _letterbox_offsets(thumb: np.ndarray) -> Tuple[int, int]:
        """Rows of black bars at the top and bottom of the thumbnail (0 if none)."""
        th = thumb.shape[0]
        bd = max(1, int(th * 0.06))
        cap = int(th * 0.22)
        top = bot = 0

        if thumb[:bd].mean() < 14.0:
            row_means = thumb[:int(th * 0.25)].mean(axis=(1, 2))
            active = np.flatnonzero(row_means > 16.0)
            if active.size:
                top = min(int(active[0]), cap)

        if thumb[th - bd:].mean() < 14.0:
            start = int(th * 0.75)
            row_means = thumb[start:].mean(axis=(1, 2))
            active = np.flatnonzero(row_means > 16.0)
            if active.size:
                bot = min(row_means.size - 1 - int(active[-1]), cap)

        return top, bot

    def close(self):
        self._reset_strips()
        for cam in list(self._dxcam_cameras.values()):
            try:
                cam.release()
            except Exception:
                pass
        self._dxcam_cameras.clear()
        self._dxcam_inst = None
        if self._mss_inst:
            try:
                self._mss_inst.close()
            except Exception:
                pass
            self._mss_inst = None
