"""
Strip capture must give ~the same LED colors as a full frame. Runs without a GPU: a fake camera
serves the strips from an in-memory frame, like StripGrabber would from the desktop duplication.
"""
import numpy as np
import pytest

from core import strip_capture as sc
from core.config import generate_default_led_points
from core.screen_capture import ScreenSampler

H, W = 1440, 2560
MAX_ERROR = 4.0  # out of 255; measured 0.7-1.8


class FakeCam:
    width, height = W, H


class FakeGrabber:
    def __init__(self, frame):
        self.frame = frame
        self.cam = FakeCam()
        self.served = True  # False => behaves like a static screen (no new frame)

    def grab(self, regions):
        if not self.served:
            return None
        return [self.frame[t:b, l:r].copy() for (l, t, r, b) in regions]

    def release(self):
        pass


def make_sampler(frame):
    s = ScreenSampler.__new__(ScreenSampler)
    s._init_strip_state()
    s._dxcam_inst = FakeCam()
    s._grabber = FakeGrabber(frame)
    s._grabber.cam = s._dxcam_inst
    s._mss_inst = None
    s.grab_frame = lambda: frame          # full-frame path (fallback / wide layouts)
    return s


@pytest.fixture(scope="module")
def picture():
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:H, 0:W]
    base = np.stack([xx / W * 255, yy / H * 255, (xx + yy) / (W + H) * 255], axis=2)
    return np.clip(base + rng.normal(0, 8, base.shape), 0, 255).astype(np.uint8)


def reference(frame, points, box, bars):
    return ScreenSampler.sample_led_points(ScreenSampler.__new__(ScreenSampler), frame, points, box, bars)


def capture(sampler, points, box, bars, frames=6):
    out = None
    for _ in range(frames):  # letterbox changes need a few confirming frames
        out = sampler.capture_and_sample(points, box, bars)
    return out


@pytest.mark.parametrize("bars", [False, True])
def test_three_sides_matches_full_frame_and_reads_few_pixels(picture, bars):
    points = generate_default_led_points(17, 29, 17, 0)
    s = make_sampler(picture)
    out = capture(s, points, 0.06, bars)
    assert np.abs(out - reference(picture, points, 0.06, bars)).max() < MAX_ERROR
    bands = s._regions[:len(s._regions) - s._n_probes]
    assert sum(sc._area(r) for r in bands) < 0.35 * W * H


def test_four_sides_matches_full_frame(picture):
    points = generate_default_led_points(14, 24, 14, 24)
    out = capture(make_sampler(picture), points, 0.06, False)
    assert np.abs(out - reference(picture, points, 0.06, False)).max() < MAX_ERROR


def test_letterbox_bars_are_detected_and_respected(picture):
    movie = picture.copy()
    movie[:200] = 0
    movie[-200:] = 0
    points = generate_default_led_points(17, 29, 17, 0)
    s = make_sampler(movie)
    out = capture(s, points, 0.06, True)
    assert abs(s._offsets[0] - 200) <= 14 and abs(s._offsets[1] - 200) <= 14
    assert np.abs(out - reference(movie, points, 0.06, True)).max() < MAX_ERROR


def test_disabling_bar_detection_resets_the_offsets(picture):
    movie = picture.copy()
    movie[:200] = 0
    movie[-200:] = 0
    points = generate_default_led_points(17, 29, 17, 0)
    s = make_sampler(movie)
    capture(s, points, 0.06, True)
    assert s._offsets != (0, 0)
    capture(s, points, 0.06, False)
    assert s._offsets == (0, 0)                              # used to stay compressed until restart


def test_flickering_bar_detection_does_not_replan_every_frame(picture):
    """In dark scenes the detection flips between 'bars' and 'no bars': only a repeated reading counts."""
    movie = picture.copy()
    movie[:200] = 0
    movie[-200:] = 0
    points = generate_default_led_points(17, 29, 17, 0)
    s = make_sampler(picture)
    capture(s, points, 0.06, True)
    assert s._offsets == (0, 0)
    for frame in (movie, picture, movie, picture, movie, picture):   # alternate every frame
        s._grabber.frame = frame
        s.capture_and_sample(points, 0.06, True)
    assert s._offsets == (0, 0)                              # never confirmed, never adopted


def test_layout_edit_on_a_static_screen_keeps_showing_colors(picture):
    """A changed layout needs pixels that were never read; until a new frame arrives the old colors stay."""
    points = generate_default_led_points(17, 29, 17, 0)
    s = make_sampler(picture)
    assert capture(s, points, 0.06, False) is not None
    s._grabber.served = False                                # static screen: no new frames
    wider = generate_default_led_points(17, 29, 17, 0)
    out = s.capture_and_sample(wider, 0.12, False)           # bigger boxes => new regions
    assert out is None                                       # nothing wrong is shown...
    s._grabber.served = True
    assert s.capture_and_sample(wider, 0.12, False) is not None   # ...and it recovers with the next frame


def test_unchanged_layout_on_a_static_screen_still_returns_colors(picture):
    points = generate_default_led_points(17, 29, 17, 0)
    s = make_sampler(picture)
    capture(s, points, 0.06, False)
    s._grabber.served = False
    assert s.capture_and_sample(points, 0.06, False) is not None


def test_failures_fall_back_to_full_frames_then_retry(picture, monkeypatch):
    points = generate_default_led_points(17, 29, 17, 0)
    s = make_sampler(picture)
    s._grab_frame_calls = 0
    monkeypatch.setattr(s, "grab_frame", lambda: picture)
    s._grabber.grab = lambda regions: (_ for _ in ()).throw(RuntimeError("dxcam internals changed"))

    now = {"t": 1000.0}
    monkeypatch.setattr("core.screen_capture.time.monotonic", lambda: now["t"])
    assert s.capture_and_sample(points, 0.06, False) is not None       # failed, but full-frame took over
    assert s._strips_failures == 1 and s._strips_retry_at > now["t"]

    s._grabber = None
    now["t"] += 10                                            # after the back-off the strips are tried again
    s._plan_key = None
    s.capture_and_sample(points, 0.06, False)
    assert s._strips_failures == 0 or s._strips_failures == 2  # tried again (either recovered or failed again)


def test_points_covering_most_of_the_screen_use_full_frames(picture):
    spread = [[x, y] for x in np.linspace(0.05, 0.95, 12) for y in np.linspace(0.05, 0.95, 12)]
    s = make_sampler(picture)
    out = capture(s, spread, 0.06, False)
    assert s._regions is None and out is not None


def test_plan_regions_handles_degenerate_input():
    assert sc.plan_regions(np.zeros((0, 2), dtype=np.float32), 0.06, W, H) is None
    regions = sc.plan_regions(np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float32), 0.25, W, H)
    assert regions is not None and all(0 <= l < r <= W and 0 <= t < b <= H for l, t, r, b in regions)


def test_letterbox_rows_from_a_column(picture):
    movie = picture.copy()
    movie[:200] = 0
    movie[-200:] = 0
    assert sc.letterbox_offsets_from_rows(movie[:, 640:648].mean(axis=(1, 2))) == (200, 200)
    assert sc.letterbox_offsets_from_rows(picture[:, 640:648].mean(axis=(1, 2))) == (0, 0)
