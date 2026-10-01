"""
Software Dynamic Light Effects Generator for DX Light.
High framerate, silky-smooth mathematical animations (Rainbow, Breathing, Fire, Color cycle).

All effects are vectorised (no per-LED Python loops) and driven by an accumulated phase, so
changing the speed slider never makes an animation jump.
"""

import time
import math
import numpy as np
from typing import Tuple

from .animations import REGISTRY
from .zones import perimeter_positions


def hsv_to_rgb(h: float, s: float, v: float) -> Tuple[int, int, int]:
    """h: 0.0-1.0, s: 0.0-1.0, v: 0.0-1.0. Returns (r, g, b) in 0-255."""
    i = int(h * 6.0)
    f = (h * 6.0) - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i % 6
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


DEFAULT_COLORS = [[255, 60, 140], [60, 120, 255], [255, 200, 60]]


class EffectEngine:
    def __init__(self, led_count: int = 120, seed=None):
        self.led_count = max(1, led_count)
        self._last = time.perf_counter()
        self._phase = 0.0  # accumulated "effect seconds" (real seconds x speed)
        self.now = self._last
        self._dt = 0.0
        self.rng = np.random.default_rng(seed)
        self.state: dict = {}          # per-effect memory (twinkles, fireworks...), cleared when the effect changes
        self._current = None
        self._points = None            # LED positions on the screen, to place animations around the monitor
        self._pos = None
        self.sunrise_seconds = 300.0   # how long the "Amanecer" effect takes (set by the alarm schedule)
        self.sunrise_start = 0.0

    def _advance(self, speed: float) -> float:
        now = time.perf_counter()
        dt = min(0.25, max(0.0, now - self._last))  # a long pause must not fast-forward the animation
        self._last = now
        self.now, self._dt = now, dt
        self._phase += dt * max(0.0, speed)
        return self._phase

    # ---- layout-aware rendering of the whole catalogue ----------------------------------------

    def set_layout(self, points) -> None:
        """LED positions on the screen (N x 2, 0-1). Animations use them to travel around the monitor."""
        self._points = np.asarray(points, dtype=np.float32) if points is not None and len(points) else None
        self._pos = None

    def positions(self, n: int) -> np.ndarray:
        if self._pos is None or len(self._pos) != n:
            self._pos = perimeter_positions(self._points) if self._points is not None and len(self._points) == n                 else (np.arange(n, dtype=np.float32) / max(1, n))
        return self._pos

    def restart(self, effect: str | None = None) -> None:
        """Forget the running animation's memory (and restart 'Amanecer' from its beginning)."""
        self._current = effect
        self.state = {}
        self.sunrise_start = time.perf_counter()

    def render(self, effect: str, speed: float = 1.0, colors=None, reverse: bool = False, mirror: bool = False) -> np.ndarray:
        """One frame of any effect in the catalogue, as (led_count, 3) float32 in 0-255."""
        n = self.led_count
        if effect != self._current:
            self.restart(effect)
        palette = np.array(colors if colors is not None and len(colors) == 3 else DEFAULT_COLORS, dtype=np.float32)

        if effect == "rainbow":
            frame = self.rainbow_wave(speed)
        elif effect == "fire":
            frame = self.fire_flicker(speed)
        elif effect == "cycle":
            frame = self.color_cycle(speed)
        else:
            fx = REGISTRY.get(effect)
            if fx is None or fx.render is None:
                fx = REGISTRY["aurora"]
            t = self._advance(speed)
            frame = fx.render(self, t, self._dt, self.positions(n), palette)

        frame = np.asarray(frame, dtype=np.float32)
        if reverse:
            frame = frame[::-1]
        if mirror and n > 1:
            fold = 1.0 - np.abs(2.0 * self.positions(n) - 1.0)           # 0 at both ends, 1 at the middle
            frame = frame[np.clip(np.rint(fold * (n - 1)).astype(np.int64), 0, n - 1)]
        return np.ascontiguousarray(np.clip(frame, 0.0, 255.0))

    def rainbow_wave(self, speed: float = 1.0, spatial_freq: float = 1.0) -> np.ndarray:
        """Flowing rainbow spectrum across the strip."""
        t = self._advance(speed) * 0.5
        n = self.led_count
        h = (t + (np.arange(n) / float(n)) * spatial_freq) % 1.0
        x = h * 6.0
        sector = x.astype(np.int64) % 6
        f = x - np.floor(x)
        q = 1.0 - f
        zero, one = np.zeros(n), np.ones(n)
        # full saturation and value: each sector ramps one channel while another stays at 1
        r = np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4], [one, q, zero, zero, f], one)
        g = np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4], [f, one, one, q, zero], zero)
        b = np.select([sector == 0, sector == 1, sector == 2, sector == 3, sector == 4], [zero, zero, f, one, one], q)
        return (np.rint(np.stack([r, g, b], axis=1) * 255.0)).astype(np.float32)

    def breathing(self, base_rgb: Tuple[int, int, int] = (0, 150, 255), speed: float = 1.0) -> np.ndarray:
        """Sinusoidal gentle pulsating breathing effect."""
        t = self._advance(speed) * 2.0
        # Sine wave oscillating between 0.1 and 1.0, then an exponential curve for natural eye perception
        intensity = math.pow(0.55 + 0.45 * math.sin(t), 2.0)
        c = np.array(base_rgb, dtype=np.float32) * intensity
        return np.tile(c, (self.led_count, 1))

    def fire_flicker(self, speed: float = 1.0) -> np.ndarray:
        """Organic ambient fireplace glow with warmth and gentle flicker."""
        t = self._advance(speed) * 3.0
        i = np.arange(self.led_count)
        noise = (np.sin(t + i * 0.35) + np.sin(t * 1.7 + i * 0.15)) * 0.25 + 0.5
        return np.stack([np.floor(220 + 35 * noise), np.floor(50 + 70 * noise), np.floor(10 * noise)],
                        axis=1).astype(np.float32)

    def color_cycle(self, speed: float = 0.5) -> np.ndarray:
        """Uniform ambient whole-room color shifting."""
        t = (self._advance(speed) * 0.15) % 1.0
        r, g, b = hsv_to_rgb(t, 0.9, 1.0)
        c = np.array([r, g, b], dtype=np.float32)
        return np.tile(c, (self.led_count, 1))
