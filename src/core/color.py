"""
Colorimetry, Calibration and Temporal Smoothing Pipeline.

Order of operations for a frame of raw LED colors (N, 3):
  1. temporal smoothing (frame-rate compensated; optionally faster on scene cuts)
  2. saturation boost (screen capture only)
  3. global brightness, then per-channel white balance
  4. "dark screen" background light (only ever raises a LED, never lowers it)
  5. LED gamma curve (computed in floating point, so nothing is lost before step 8)
  6. soft floor: very dim colors are lifted proportionally so hue survives
  7. power limit: caps the total light so a white screen cannot overload the USB supply
  8. quantisation to 8 bit, optionally with temporal dithering (hides banding in dark gradients)
  9. channel permutation to the wiring order of the strip
"""

import threading
from typing import Tuple, List
import numpy as np

# Channel mapping order indices (r_idx, g_idx, b_idx)
WIRE_MAPS = {
    "RGB": (0, 1, 2),
    "RBG": (0, 2, 1),
    "GRB": (1, 0, 2),
    "GBR": (1, 2, 0),
    "BRG": (2, 0, 1),
    "BGR": (2, 1, 0),
}

BIAS_COLOR = (255, 176, 110)  # warm white used for the dark-screen background light


class ColorPipeline:
    # Smoothing factors are tuned for this frame time; other frame rates are compensated so a
    # given "smoothing" setting looks the same at 20, 40 or 60 FPS.
    REFERENCE_DT = 1.0 / 40.0
    SCENE_CUT_START = 18.0   # mean per-LED change (0-255) where smoothing starts to give way
    SCENE_CUT_FULL = 68.0    # ...and where it has fully given way

    def __init__(
        self,
        wire_map: str = "BGR",
        gamma: float = 2.2,
        brightness: float = 1.0,  # 0.0 to 1.0
        saturation: float = 1.25, # 1.0 = neutral, >1.0 = vibrant
        min_luminance_floor: int = 2, # Soft floor, won't clip to 0 abruptly
        smoothing: float = 0.35,   # LERP alpha factor: 0.0 (freeze) to 1.0 (instant)
        white_balance=(1.0, 1.0, 1.0),
        max_power: float = 1.0,
        dark_light: float = 0.0,
        dithering: bool = False,
        adaptive_smoothing: bool = False,
        hdr_compensation: float = 0.0,
    ):
        self.wire_map = wire_map.upper() if wire_map.upper() in WIRE_MAPS else "BGR"
        self.gamma = max(1.0, min(3.0, float(gamma)))  # gamma <= 0 would crash
        self.brightness = max(0.0, min(1.0, brightness))
        self.saturation = max(0.0, min(3.0, saturation))
        self.min_floor = max(0, min(255, min_luminance_floor))
        self.smoothing = max(0.01, min(1.0, smoothing))
        self.white_balance = self._clean_gains(white_balance)
        self.max_power = max(0.05, min(1.0, float(max_power)))
        self.dark_light = max(0.0, min(0.3, float(dark_light)))
        self.dithering = bool(dithering)
        self.adaptive_smoothing = bool(adaptive_smoothing)
        self.hdr_compensation = max(0.0, min(1.0, float(hdr_compensation)))

        # Previous frame colors for temporal smoothing: shape (N, 3)
        self._prev_colors: np.ndarray | None = None
        self._residual: np.ndarray | None = None  # dithering error carried to the next frame
        self.preview: np.ndarray | None = None  # (N, 3) uint8 RGB as a person would see it (for the UI)
        self.settled = False  # True once the smoothed output has reached the target colors
        self.activity = 0.0   # mean per-LED change of the last frame (drives the adaptive frame rate)
        # Settings arrive from HTTP threads while the engine thread renders
        self._lock = threading.RLock()

    @staticmethod
    def _clean_gains(gains) -> np.ndarray:
        try:
            g = np.array([float(x) for x in gains], dtype=np.float32)
            if g.shape != (3,) or not np.all(np.isfinite(g)):
                raise ValueError
        except (TypeError, ValueError):
            g = np.ones(3, dtype=np.float32)
        return np.clip(g, 0.3, 1.0)

    def update_settings(
        self,
        wire_map: str | None = None,
        gamma: float | None = None,
        brightness: float | None = None,
        saturation: float | None = None,
        smoothing: float | None = None,
        min_floor: int | None = None,
        white_balance=None,
        max_power: float | None = None,
        dark_light: float | None = None,
        dithering: bool | None = None,
        adaptive_smoothing: bool | None = None,
        hdr_compensation: float | None = None,
    ):
        with self._lock:
            if wire_map and wire_map.upper() in WIRE_MAPS:
                self.wire_map = wire_map.upper()
            if gamma is not None:
                self.gamma = max(1.0, min(3.0, float(gamma)))
            if brightness is not None:
                self.brightness = max(0.0, min(1.0, brightness))
            if saturation is not None:
                self.saturation = max(0.0, min(3.0, saturation))
            if smoothing is not None:
                self.smoothing = max(0.01, min(1.0, smoothing))
            if min_floor is not None:
                self.min_floor = max(0, min(255, int(min_floor)))
            if white_balance is not None:
                self.white_balance = self._clean_gains(white_balance)
            if max_power is not None:
                self.max_power = max(0.05, min(1.0, float(max_power)))
            if dark_light is not None:
                self.dark_light = max(0.0, min(0.3, float(dark_light)))
            if dithering is not None:
                self.dithering = bool(dithering)
                self._residual = None
            if adaptive_smoothing is not None:
                self.adaptive_smoothing = bool(adaptive_smoothing)
            if hdr_compensation is not None:
                self.hdr_compensation = max(0.0, min(1.0, float(hdr_compensation)))

    def apply_saturation(self, rgb_array: np.ndarray) -> np.ndarray:
        """
        Boosts saturation using fast vectorized numpy operations.
        rgb_array shape: (N, 3) float32 [0..255]
        """
        if abs(self.saturation - 1.0) < 0.01:
            return rgb_array

        # Luminance per Rec. 709
        lum = 0.2126 * rgb_array[:, 0] + 0.7152 * rgb_array[:, 1] + 0.0722 * rgb_array[:, 2]
        lum = lum[:, np.newaxis]
        # Linear interpolation between grayscale luminance and color
        saturated = lum + self.saturation * (rgb_array - lum)
        return np.clip(saturated, 0.0, 255.0)

    def apply_hdr_compensation(self, rgb_array: np.ndarray) -> np.ndarray:
        """Gently restore contrast from Windows' SDR tone mapping when the user opts in.

        Desktop Duplication exposes SDR pixels even while the monitor has HDR enabled, but the
        amount of tone mapping varies by GPU and display. This deliberately is a manual 0..1
        control rather than an automatic conversion; zero is an exact no-op.
        """
        if self.hdr_compensation <= 0.0:
            return rgb_array
        strength = self.hdr_compensation
        normalized = np.clip(rgb_array / 255.0, 0.0, 1.0)
        # Lift subdued mid-tones and restore a little chroma, without pushing white above white.
        raised = np.power(normalized, 1.0 - 0.22 * strength)
        lum = (0.2126 * raised[:, 0] + 0.7152 * raised[:, 1] + 0.0722 * raised[:, 2])[:, np.newaxis]
        compensated = lum + (raised - lum) * (1.0 + 0.12 * strength)
        return np.clip(compensated * 255.0, 0.0, 255.0)

    def process_colors(
        self, raw_rgb: np.ndarray, apply_gamma: bool = True, dt: float | None = None
    ) -> List[Tuple[int, int, int]]:
        """List-of-tuples form of process_colors_array (kept for callers that want tuples)."""
        return [tuple(row) for row in self.process_colors_array(raw_rgb, apply_gamma, dt).tolist()]

    def process_colors_array(
        self, raw_rgb: np.ndarray, apply_gamma: bool = True, dt: float | None = None, led_gamma: bool = False
    ) -> np.ndarray:
        """
        Returns a (N, 3) uint8 array in the strip's channel order, ready to be sent.
        apply_gamma=True is the screen path (saturation, HDR, dark light and LED gamma). led_gamma=True applies only
        the LED gamma curve, for generated colours (effects) that are already meant to look as they are.
        """
        with self._lock:
            return self._process_colors(raw_rgb, apply_gamma, dt, led_gamma)

    def _smooth(self, rgb: np.ndarray, dt: float | None) -> np.ndarray:
        if self._prev_colors is None or self._prev_colors.shape != rgb.shape:
            self._prev_colors = rgb.copy()
            self.settled = True
            self.activity = 0.0
            return self._prev_colors.copy()

        delta = rgb - self._prev_colors
        self.activity = float(np.abs(delta).mean())
        alpha = self.smoothing
        if self.adaptive_smoothing:
            # A scene cut should show at once; slow drift should stay calm. The smoothing "gives way"
            # in proportion to how much the picture just changed.
            cut = float(np.clip((self.activity - self.SCENE_CUT_START) / (self.SCENE_CUT_FULL - self.SCENE_CUT_START), 0.0, 1.0))
            alpha = alpha + (1.0 - alpha) * cut * cut
        if dt is not None:
            ratio = max(0.1, min(10.0, dt / self.REFERENCE_DT))
            alpha = 1.0 - (1.0 - min(alpha, 0.999)) ** ratio
        self._prev_colors += alpha * delta
        self.settled = float(np.abs(delta).max(initial=0.0)) * (1.0 - alpha) < 0.5
        return self._prev_colors.copy()

    def _process_colors(self, raw_rgb: np.ndarray, apply_gamma: bool, dt: float | None, led_gamma: bool = False) -> np.ndarray:
        rgb = raw_rgb.astype(np.float32)

        # 1. temporal smoothing
        smoothed = self._smooth(rgb, dt)

        # 2. saturation (only for screen capture, keep palettes natural)
        saturated = self.apply_saturation(smoothed) if apply_gamma else smoothed

        # 3. optional HDR tone-map compensation, then brightness and white balance
        corrected = self.apply_hdr_compensation(saturated) if apply_gamma else saturated
        scaled = corrected * self.brightness * self.white_balance

        # 4. background light while the screen is dark: a floor on the LED's final light output,
        #    expressed here in the pre-gamma domain so that it comes out as the level the user chose
        if apply_gamma and self.dark_light > 0.0:
            target = (np.array(BIAS_COLOR, dtype=np.float32) / 255.0) * self.dark_light
            scaled = np.maximum(scaled, 255.0 * np.power(target, 1.0 / self.gamma))

        scaled = np.clip(scaled, 0.0, 255.0)
        self.preview = np.rint(scaled).astype(np.uint8)  # before the LED gamma: that curve compensates the LEDs, not the eye

        # 5. LED gamma, in floating point (effects and palettes are already meant for the LEDs)
        out = 255.0 * np.power(scaled / 255.0, self.gamma) if (apply_gamma or led_gamma) else scaled

        # 6. soft floor (after gamma, which would otherwise crush it to 0): lift very dim colors
        #    proportionally so the hue is kept and dark scenes keep a faint glow
        if self.min_floor > 0:
            max_val = out.max(axis=1, keepdims=True)
            dim = (max_val > 0.0) & (max_val < self.min_floor)
            if dim.any():
                out = np.where(dim, out * (self.min_floor / np.maximum(max_val, 1e-6)), out)

        # 7. power limit: share of the total light the strip could emit
        if self.max_power < 1.0 and out.size:
            share = float(out.sum()) / (out.shape[0] * 765.0)
            if share > self.max_power:
                out = out * (self.max_power / share)

        # 8. to 8 bit
        if self.dithering:
            if self._residual is None or self._residual.shape != out.shape:
                self._residual = np.zeros_like(out)
            wanted = out + self._residual
            quantised = np.clip(np.floor(wanted + 0.5), 0, 255)
            self._residual = np.clip(wanted - quantised, -1.0, 1.0)  # the error is paid in the next frames
            out = quantised
        else:
            out = np.rint(np.clip(out, 0.0, 255.0))
        out = out.astype(np.uint8)

        # 9. channel reordering
        r_idx, g_idx, b_idx = WIRE_MAPS[self.wire_map]
        return np.ascontiguousarray(out[:, (r_idx, g_idx, b_idx)])

    def reset_smoothing(self):
        """Drops the temporal history (call when switching modes or LED count)."""
        with self._lock:
            self._prev_colors = None
            self._residual = None

    def map_single_color(self, r: int, g: int, b: int) -> Tuple[int, int, int]:
        """Maps one RGB color through brightness, white balance, gamma, power limit and the wiring order."""
        r_idx, g_idx, b_idx = WIRE_MAPS[self.wire_map]
        # Inputs come from the API/config: clamp so a bad value can neither raise nor wrap around
        base = np.clip(np.array([r, g, b], dtype=np.float32), 0, 255) * self.brightness * self.white_balance
        out = 255.0 * np.power(np.clip(base, 0, 255) / 255.0, self.gamma)
        share = float(out.sum()) / 765.0
        if self.max_power < 1.0 and share > self.max_power:
            out = out * (self.max_power / share)
        rgb = [int(v) for v in np.rint(np.clip(out, 0, 255))]
        return (rgb[r_idx], rgb[g_idx], rgb[b_idx])
