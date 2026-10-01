"""
Strip capture: read only the screen regions the LEDs actually look at.

Copying a full 1440p frame from the GPU costs ~28 % of a core at 40 FPS; the LEDs only sample
a thin band along the edges, so we copy just those bands (a few % of the pixels) from ONE
acquired desktop frame. dxcam's public API can only return one region per acquired frame, so
StripGrabber drives its (private) camera internals: dxcam is pinned in requirements.txt and any
incompatibility raises at construction so the caller falls back to full-frame grabs.

Planning (which rectangles to read) is pure numpy so it can be tested without a GPU.
"""

import ctypes
from typing import Dict, List, Optional, Tuple

import numpy as np

Region = Tuple[int, int, int, int]  # left, top, right, bottom in screen pixels

LETTERBOX_PROBE_X = (0.30, 0.70)  # columns read in full height to find black bars
LETTERBOX_PROBE_WIDTH = 8                # pixels per probe column
OFFSET_STEP = 0.01                       # letterbox offsets are quantised to 1 % of the height
PLAN_PAD = 12                            # extra pixels around each box (thumbnail rounding)
SIDE_BIAS = 0.012                        # preference for assigning a LED to a side bar (normalized)
MAX_COVERAGE = 0.60                     # above this share of the screen, a full frame is cheaper


def letterbox_offsets_from_rows(row_means: np.ndarray) -> Tuple[int, int]:
    """
    Black-bar rows at the top/bottom given the mean brightness of every screen row.
    Same thresholds as the full-frame detector, applied to full-resolution rows.
    """
    h = row_means.size
    bd = max(4, int(h * 0.06))
    cap = int(h * 0.22)
    top = bot = 0

    if row_means[:bd].mean() < 14.0:
        active = np.flatnonzero(row_means[:int(h * 0.25)] > 16.0)
        if active.size:
            top = min(int(active[0]), cap)

    if row_means[h - bd:].mean() < 14.0:
        start = int(h * 0.75)
        active = np.flatnonzero(row_means[start:] > 16.0)
        if active.size:
            bot = min(h - start - 1 - int(active[-1]), cap)

    return top, bot


def quantize_offsets(top_px: int, bot_px: int, height: int) -> Tuple[int, int]:
    """Rounds offsets to OFFSET_STEP so tiny fluctuations do not trigger a new plan."""
    step = max(1, int(round(height * OFFSET_STEP)))
    return (top_px // step) * step, (bot_px // step) * step


def _union(a: Region, b: Region) -> Region:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _area(r: Region) -> int:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def merge_regions(regions: List[Region]) -> List[Region]:
    """Merges regions whose union costs barely more than the two apart."""
    regions = list(regions)
    changed = True
    while changed:
        changed = False
        for i in range(len(regions)):
            for j in range(i + 1, len(regions)):
                u = _union(regions[i], regions[j])
                # Merge only when reading the union costs barely more than reading both apart;
                # overlapping L-shaped bands must NOT merge (their bounding box is the whole screen).
                if _area(u) <= 1.15 * (_area(regions[i]) + _area(regions[j])):
                    regions[i] = u
                    del regions[j]
                    changed = True
                    break
            if changed:
                break
    return regions


def plan_regions(
    points: np.ndarray,
    box_size: float,
    width: int,
    height: int,
    top_off: int = 0,
    bot_off: int = 0,
) -> Optional[List[Region]]:
    """
    Screen rectangles that contain every LED sampling box, or None if reading them costs
    about as much as a full frame. points: (N, 2) normalized [x, y]; offsets in pixels.

    LEDs are grouped by nearest screen edge and each group gets one bounding rectangle.
    """
    if len(points) == 0:
        return None

    eff_h = height - top_off - bot_off
    half_x = width * box_size * 0.5 + PLAN_PAD
    half_y = eff_h * box_size * 0.5 + PLAN_PAD + height * OFFSET_STEP  # slack: offsets may drift one step

    px = points[:, 0] * width
    py = top_off + points[:, 1] * eff_h

    # Nearest edge, with a small bias towards the side bars: the lowest LED of a side bar sits
    # about as close to the bottom edge as to its own side, and plain "nearest edge" would carve
    # a needless bottom band out of it.
    x, y = points[:, 0], points[:, 1]
    is_side = np.minimum(x, 1.0 - x) <= np.minimum(y, 1.0 - y) + SIDE_BIAS
    group = np.where(is_side, np.where(x < 0.5, 0, 1), np.where(y < 0.5, 2, 3))

    regions: List[Region] = []
    for g in range(4):
        sel = group == g
        if not sel.any():
            continue
        left = int(max(0, np.floor(px[sel].min() - half_x)))
        right = int(min(width, np.ceil(px[sel].max() + half_x)))
        top = int(max(0, np.floor(py[sel].min() - half_y)))
        bottom = int(min(height, np.ceil(py[sel].max() + half_y)))
        if right > left and bottom > top:
            regions.append((left, top, right, bottom))

    regions = merge_regions(regions)
    if sum(_area(r) for r in regions) > MAX_COVERAGE * width * height:
        return None
    return regions


def probe_regions(width: int, height: int) -> List[Region]:
    """Thin full-height columns used to detect letterbox bars (~1 % of the screen in total)."""
    out = []
    for fx in LETTERBOX_PROBE_X:
        left = int(width * fx)
        out.append((left, 0, min(width, left + LETTERBOX_PROBE_WIDTH), height))
    return out


class StripGrabber:
    """Reads several rectangles of the same acquired desktop frame through a dxcam camera."""

    _REQUIRED = (
        "_acquire_new_frame", "_multithread_guard", "_update_source_region", "_source_region",
        "_device", "_output", "_duplicator", "_processor",
        "_release_frame_if_early_release", "_release_frame_if_late_release",
    )

    def __init__(self, cam):
        missing = [name for name in self._REQUIRED if not hasattr(cam, name)]
        if missing:
            raise RuntimeError(f"dxcam internals changed, missing: {missing}")
        if getattr(cam, "rotation_angle", 0) != 0:
            raise RuntimeError("rotated outputs are not supported by strip capture")
        from dxcam.core.stagesurf import StageSurface  # deferred: only needed on Windows with dxcam
        self._StageSurface = StageSurface
        self.cam = cam
        self._device = cam._device
        self._stages: Dict[Region, object] = {}

    def _stage(self, region: Region):
        st = self._stages.get(region)
        if st is None:
            w, h = region[2] - region[0], region[3] - region[1]
            st = self._StageSurface(output=self.cam._output, device=self.cam._device)
            st.release()  # the constructor allocates a full-screen texture; we want region-sized
            st.rebuild(self.cam._output, self.cam._device, dim=(w, h))
            self._stages[region] = st
        return st

    def _sync(self, regions: List[Region]):
        if self.cam._device is not self._device:  # dxcam rebuilt its device after an output change
            self.release()
            self._device = self.cam._device
        for stale in [r for r in self._stages if r not in regions]:
            self._stages.pop(stale).release()

    def grab(self, regions: List[Region]) -> Optional[List[np.ndarray]]:
        """
        One RGB array per region from a newly presented frame, or None when the screen has not
        changed since the last call (the caller keeps the previous strips).
        """
        cam = self.cam
        if not cam._acquire_new_frame(wait_for_frame=False):
            return None
        try:
            with cam._multithread_guard():
                self._sync(regions)
                stages = [self._stage(r) for r in regions]
                for region, st in zip(regions, stages):
                    cam._update_source_region(region)
                    cam._device.im_context.CopySubresourceRegion(
                        st.texture, 0, 0, 0, 0, cam._duplicator.texture, 0, ctypes.byref(cam._source_region)
                    )
                cam._release_frame_if_early_release()

                frames = []
                for region, st in zip(regions, stages):
                    w, h = region[2] - region[0], region[3] - region[1]
                    rect = st.map()
                    try:
                        # process() may return a reusable buffer, so take a copy
                        frames.append(cam._processor.process(rect, w, h, (0, 0, w, h), 0).copy())
                    finally:
                        st.unmap()
                return frames
        finally:
            cam._release_frame_if_late_release()

    def release(self):
        for st in self._stages.values():
            try:
                st.release()
            except Exception:
                pass
        self._stages.clear()
