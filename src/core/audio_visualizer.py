"""
Real-time Ultra-Smooth Audio Reactive Visualizer Engine for Zak_light.
Features:
1. Windows CoreAudio Master Output Peak Meter (pycaw) - 0 latency, 0 CPU overhead.
2. Asymmetric Attack & Exponential Decay Envelope Follower (eliminates all strobing/flickering).
3. 7 Dynamic Smooth Software Visualizer Patterns (Center Pulse, Side Flow, Beat Breath, Fire Beat, Spectrum Wave, Liquid Wave, Ambient Glow).
4. Direct, vibrant, 100% faithful palette color reproduction (Cyberpunk, Fire, Neon Blue, Matrix, Vaporwave, Sunset).
"""

import time
import math
import numpy as np
from typing import List, Tuple, Dict

from .loopback import HAS_LOOPBACK, LoopbackCapture, SpectrumAnalyzer, list_output_devices

try:
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    from comtypes import CLSCTX_ALL
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False

# High-fidelity, punchy color palettes for Audio Reactive modes: (R, G, B)
AUDIO_PALETTES: Dict[str, List[Tuple[int, int, int]]] = {
    "cyberpunk": [(0, 220, 255), (255, 0, 140), (160, 20, 255)],     # Electric Cyan, Hot Magenta, Violet
    "fire": [(255, 40, 0), (255, 120, 0), (255, 220, 20)],           # Crimson Red, Fiery Orange, Amber Yellow
    "neon_blue": [(0, 60, 240), (0, 190, 255), (220, 245, 255)],    # Deep Royal Blue, Vivid Cyan, Ice White
    "matrix": [(0, 60, 15), (0, 255, 60), (180, 255, 200)],         # Forest Green, Matrix Green, Mint White
    "vaporwave": [(255, 60, 160), (160, 60, 255), (0, 240, 220)],   # Retro Pink, Purple, Bright Turquoise
    "sunset": [(255, 70, 10), (240, 0, 100), (130, 0, 200)],        # Sunset Orange, Magenta Rose, Deep Violet
}


class WindowsAudioMeter:
    """Reads master output peak volume directly from Windows CoreAudio."""
    def __init__(self):
        self.meter = None
        self._init_meter()

    def _init_meter(self):
        if not HAS_PYCAW:
            return
        try:
            speakers = AudioUtilities.GetSpeakers()
            if speakers and hasattr(speakers, '_dev') and speakers._dev:
                interface = speakers._dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
                self.meter = interface.QueryInterface(IAudioMeterInformation)
        except Exception:
            self.meter = None

    def get_peak(self) -> float:
        if not self.meter:
            self._init_meter()
        if self.meter:
            try:
                return float(self.meter.GetPeakValue())
            except Exception:
                self.meter = None
        return 0.0


N_PATTERNS = 10


def render_pattern(pat: int, n: int, vol: float, phase: float, palette, extras=None) -> np.ndarray:
    """
    The seven rhythm patterns as (n, 3) float32 colors in 0..255. Fully vectorised: the original
    per-LED loops are kept in tests/reference_patterns.py and compared against this in the tests.
    """
    c_base = np.array(palette[0], dtype=np.float64)
    c_mid = np.array(palette[1], dtype=np.float64)
    c_peak = np.array(palette[2], dtype=np.float64)

    # Ambient baseline brightness so the palette colors are ALWAYS clearly visible
    ambient_scale = 0.22 + 0.15 * vol
    # Punch scale when the beat hits
    punch_scale = 0.40 + 0.60 * vol

    i = np.arange(n, dtype=np.float64)
    half = n // 2

    def lerp(t, a, b):
        """(n,) blend factor, colors a and b -> (n, 3): t*a + (1-t)*b"""
        t = t[:, None]
        return t * a + (1.0 - t) * b

    pat = pat % N_PATTERNS
    extras = extras or {}
    if pat == 0:
        # Pattern 0 (Pickup 1): Expansión Central (Center Outward Pulse)
        extent = max(2.0, float(half) * (0.2 + 0.9 * vol))
        dist = np.abs(i - half)
        ratio = np.power(np.clip(1.0 - dist / extent, 0.0, 1.0), 0.7)
        colors = np.where((dist <= extent)[:, None], lerp(ratio, c_peak, c_mid) * punch_scale, c_base * ambient_scale)

    elif pat == 1:
        # Pattern 1 (Pickup 2): Flujo Lateral Estéreo (Left to Right Wave)
        extent = max(3.0, float(n) * (0.2 + 0.85 * vol))
        ratio = np.clip(1.0 - i / extent, 0.0, 1.0)
        colors = np.where((i <= extent)[:, None], lerp(ratio, c_peak, c_mid) * punch_scale, c_base * ambient_scale)

    elif pat == 2:
        # Pattern 2 (Pickup 3): Latido de Ritmo (Beat Breath)
        pulse = 0.25 + 0.75 * math.pow(vol, 0.85)
        t_blend = 0.5 + 0.5 * math.sin(phase)
        colors = np.tile((t_blend * c_mid + (1.0 - t_blend) * c_peak) * pulse, (n, 1))

    elif pat == 3:
        # Pattern 3 (Pickup 4): Fuego Rítmico Danzante
        heat = np.sin(i * 0.14 + phase * 2.2) * 0.3 + 0.7
        heat_val = np.clip(heat * (0.35 + 0.65 * vol), 0.0, 1.0)
        hot = lerp((heat_val - 0.6) / 0.4, c_peak, c_mid)
        cool = lerp(heat_val / 0.6, c_mid, c_base)
        colors = np.where((heat_val > 0.6)[:, None], hot, cool) * punch_scale

    elif pat == 4:
        # Pattern 4 (Pickup 5): Espectro de Paleta (Palette Spectrum Wave)
        pos = (i / n + phase * 0.2) % 1.0
        first = lerp(1.0 - pos / 0.33, c_base, c_mid)                 # (1-t)*base + t*mid
        second = lerp(1.0 - (pos - 0.33) / 0.33, c_mid, c_peak)
        third = lerp(1.0 - (pos - 0.66) / 0.34, c_peak, c_base)
        blend = np.where((pos < 0.33)[:, None], first, np.where((pos < 0.66)[:, None], second, third))
        colors = blend * (0.35 + 0.65 * vol)

    elif pat == 5:
        # Pattern 5 (Pickup 6): Onda Líquida
        wave = np.sin(i / n * 4.0 * math.pi + phase) * 0.5 + 0.5
        wave_pow = np.power(np.clip(wave, 0.0, 1.0), 1.4)
        colors = lerp(wave_pow, c_peak, c_base) * (0.28 + 0.72 * vol)

    elif pat == 6:
        # Pattern 6 (Pickup 7): Resplandor Ambiental
        ambient = 0.30 + 0.70 * vol
        subtle = np.sin(i / n * 2.0 * math.pi + phase * 0.5) * 0.2 + 0.8
        colors = c_mid * ambient * subtle[:, None]

    elif pat == 7:
        # Pattern 7: VU estéreo. The left channel fills the left side upwards, the right channel the right side.
        left, right = float(extras.get("left", vol)), float(extras.get("right", vol))
        u = i / n
        first_half = u < 0.5
        height = np.where(first_half, u / 0.5, (1.0 - u) / 0.5)           # 0 at the bottom ends, 1 at the top middle
        level = np.where(first_half, left, right)
        lit = np.clip((level - height) / 0.08 + 0.5, 0.0, 1.0)
        ramp = np.where(first_half, 0, 0) + height
        color = np.where((ramp < 0.5)[:, None], lerp(1.0 - ramp / 0.5, c_base, c_mid), lerp(1.0 - (ramp - 0.5) / 0.5, c_mid, c_peak))
        colors = color * lit[:, None] * punch_scale + c_base * ambient_scale * 0.5 * (1.0 - lit)[:, None]

    elif pat == 8:
        # Pattern 8: Espectro perimetral. Low notes at the start of the strip, high notes at the end.
        bars = np.asarray(extras.get("bars", [vol] * 7), dtype=np.float64)
        centres = (np.arange(len(bars)) + 0.5) / len(bars)
        level = np.clip(np.interp(i / n, centres, bars), 0.0, 1.0)
        color = np.where((level < 0.5)[:, None], lerp(1.0 - level / 0.5, c_base, c_mid), lerp(1.0 - (level - 0.5) / 0.5, c_mid, c_peak))
        colors = color * (0.10 + 0.90 * level)[:, None]

    else:
        # Pattern 9: Color por golpe. Every beat moves on to the next palette color and flashes the strip.
        count = int(extras.get("beat_count", 0))
        flash = float(extras.get("beat", 0.0))
        here, there = np.asarray(palette[count % 3], dtype=np.float64), np.asarray(palette[(count + 1) % 3], dtype=np.float64)
        blend = (i / n)[:, None]
        base_color = here * (1.0 - 0.35 * blend) + there * 0.35 * blend
        level = 0.22 + 0.30 * vol + 0.48 * flash
        colors = base_color * level + 255.0 * 0.18 * flash

    return np.clip(colors, 0.0, 255.0).astype(np.float32)


class AudioReactiveEngine:
    def __init__(
        self,
        led_count: int = 63,
        palette_name: str = "cyberpunk",
        sensitivity: float = 1.5,
        rhythm_type: str = "software",
        audio_device_id: str = "",
    ):
        self.led_count = max(10, led_count)
        self.palette_name = palette_name if palette_name in AUDIO_PALETTES else "cyberpunk"
        self.palette = AUDIO_PALETTES[self.palette_name]
        self.sensitivity = max(0.2, min(5.0, sensitivity))
        self.rhythm_type = "software"
        self.pattern_idx = 0  # 0 to 6 (Pickup 1 to 7)

        self._meter = None
        self._last_beat = 0.0   # WindowsAudioMeter, created on first use: only the no-loopback fallback needs it
        self.capture = LoopbackCapture(audio_device_id) if HAS_LOOPBACK else None
        self.analyzer = SpectrumAnalyzer(self.capture) if self.capture else None
        self.prev_volume = 0.0
        self.bass = 0.0    # smoothed bass energy 0..1
        self.left = 0.0    # smoothed loudness of the left / right channel 0..1
        self.right = 0.0
        self.beat = 0.0    # 1.0 on a detected beat, decaying
        self.beat_count = 0  # beats detected since the mode started
        self.phase = 0.0
        self.last_time = time.perf_counter()

    def set_palette(self, name: str):
        if name in AUDIO_PALETTES:
            self.palette_name = name
            self.palette = AUDIO_PALETTES[name]

    def set_audio_device(self, device_id: str):
        """Apply a user-selected endpoint; the capture reopens only if rhythm is active."""
        if self.capture:
            self.capture.set_device(device_id)

    def audio_devices(self) -> list[dict]:
        """Enumeration is called by the settings UI only, never from a frame/status poll."""
        return list_output_devices()

    def audio_status(self) -> dict:
        if self.capture:
            return self.capture.status()
        return {"available": False, "active": False, "selected": False, "active_device": "", "using_fallback": False, "last_error": ""}

    # Envelope time constants (seconds). Equivalent to the old per-frame 0.28 / 0.045 at 40 FPS,
    # but independent of frame rate.
    ATTACK_TAU = 0.076   # catches bass kicks and snare hits without lag
    DECAY_TAU = 0.54     # fades over ~half a second, preventing strobe/flicker

    def pulse(self, dt: float) -> float:
        """Bass/beat energy 0..1 for effects layered on other modes (e.g. the screen). Advances the filters."""
        self.update_volume(dt)
        return min(1.0, 0.6 * self.bass + 0.5 * self.beat)

    def spectrum(self) -> dict:
        """Snapshot for the UI equalizer (read-only: does not advance any filter)."""
        active = bool(self.capture and self.capture.active and self.analyzer)
        return {
            "active": active,
            "bars": [round(b, 3) for b in self.analyzer.bars] if active else [0.0] * 7,
            "beat": round(self.beat, 3),
            "level": round(self.get_current_volume(), 3),
            "audio": self.audio_status(),
        }

    def _peak_meter(self) -> "WindowsAudioMeter":
        if self._meter is None:
            self._meter = WindowsAudioMeter()
        return self._meter

    def activate(self):
        """Starts system-audio capture (call when the rhythm mode becomes active)."""
        if self.capture:
            self.capture.start()

    def suspend(self):
        """Stops audio capture to save CPU while another mode is active."""
        if self.capture:
            self.capture.stop()
        self.prev_volume = self.bass = self.beat = self.left = self.right = 0.0
        self.beat_count = 0

    @staticmethod
    def _follow(current: float, target: float, dt: float, attack: float, decay: float) -> float:
        tau = attack if target > current else decay
        return current + (1.0 - math.exp(-dt / tau)) * (target - current)

    def update_volume(self, dt: float) -> float:
        """Advances the envelopes by dt seconds. Call once per frame."""
        gain = self.sensitivity / 1.5
        if self.capture and self.capture.active and self.analyzer:
            f = self.analyzer.update(dt)
            scaled = min(1.0, f.level * gain)
            bass_target = min(1.0, f.bass * gain)
            left_target, right_target = min(1.0, f.left * gain), min(1.0, f.right * gain)
            if f.beat > 0.95 >= self._last_beat:
                self.beat_count += 1
            self._last_beat = f.beat
            self.beat = f.beat
        else:  # no loopback: fall back to the master peak meter
            raw_peak = self._peak_meter().get_peak()
            scaled = min(1.0, math.sqrt(raw_peak) * self.sensitivity * 1.5)
            bass_target = left_target = right_target = scaled
            self.beat = 0.0

        self.prev_volume = self._follow(self.prev_volume, scaled, dt, self.ATTACK_TAU, self.DECAY_TAU)
        self.bass = self._follow(self.bass, bass_target, dt, self.ATTACK_TAU, self.DECAY_TAU)
        self.left = self._follow(self.left, left_target, dt, self.ATTACK_TAU, self.DECAY_TAU)
        self.right = self._follow(self.right, right_target, dt, self.ATTACK_TAU, self.DECAY_TAU)
        return self.get_current_volume()

    def get_current_volume(self) -> float:
        """Last smoothed volume [0.0 - 1.0]. Read-only: safe to call from the UI/status endpoint."""
        return max(0.0, min(1.0, self.prev_volume))

    def get_frame_colors(self) -> np.ndarray:
        """
        Calculates silky-smooth dynamic responsive LED colors based on current audio energy
        and faithfully renders the active color palette.
        Returns shape (led_count, 3) in range 0-255.
        """
        now = time.perf_counter()
        dt = max(0.001, min(0.1, now - self.last_time))
        self.last_time = now

        vol = self.update_volume(dt)
        # Drive the patterns mostly by the bass and kick it on every detected beat
        vol = min(1.0, 0.55 * vol + 0.6 * self.bass + 0.4 * self.beat)
        self.phase = (self.phase + dt * (1.2 + 2.5 * vol)) % (2.0 * math.pi)

        extras = {"left": self.left, "right": self.right, "beat": self.beat, "beat_count": self.beat_count,
                  "bars": self.analyzer.bars if self.analyzer else [vol] * 7}
        return render_pattern(self.pattern_idx, self.led_count, vol, self.phase, self.palette, extras)
