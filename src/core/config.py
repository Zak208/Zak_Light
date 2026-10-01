"""
Configuration Manager for Zak_light.
Loads and saves user settings to config.json.
Provides hardware preservation on reset to default tuning and interactive LED coordinates.
"""

import os
import re
import json
import math
import shutil
import logging
import threading
from dataclasses import dataclass, asdict, field
from typing import Dict, List

from .animations import EFFECT_KEYS, default_palette
from .paths import data_dir, resource_dir

log = logging.getLogger("zak_light")
_io_lock = threading.Lock()

MAX_LEDS = 254  # the HID protocol addresses LEDs with one byte (255 is reserved)
WIRE_MAP_NAMES = ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR")
PRESET_NAMES = ("game", "movie", "vivid", "soft")
PERFORMANCE_MODES = ("eco", "balanced", "fluid", "custom")
MODE_NAMES = ("screen", "rhythm", "static", "effect", "off", "calibrate")
EFFECT_NAMES = EFFECT_KEYS   # the animation catalogue is the single source of effect names
PALETTE_NAMES = ("cyberpunk", "fire", "neon_blue", "matrix", "vaporwave", "sunset")
SCREEN_STYLES = ("edge", "average", "halves")
SCHEDULE_ACTIONS = ("on", "off", "mode", "profile", "brightness", "sunrise")
MAX_RULES = 30
MONITOR_COLOR_FIELDS = (
    "brightness", "gamma", "saturation", "smoothing", "min_luminance_floor", "white_balance", "dark_light",
    "hdr_compensation",
)


def _clamp(value, lo, hi, default, kind=float):
    """Coerces value to kind and limits it to [lo, hi]; falls back to default if it is not a number."""
    try:
        number = kind(value)
        if isinstance(number, float) and not math.isfinite(number):
            raise ValueError
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))

CONFIG_FILE_PATH = os.path.join(data_dir(), "config.json")


def generate_default_led_points(
    leds_left: int = 17,
    leds_top: int = 29,
    leds_right: int = 17,
    leds_bottom: int = 0,
    clockwise: bool = True,
    margin: float = 0.035
) -> List[List[float]]:
    """
    Generates normalized (0.0 to 1.0) [x, y] coordinates for each LED along the monitor edges.
    """
    points = []  # `margin`: how far inside the screen edge the sampling points sit (fraction of the screen)

    # 1. Left Edge (Bottom to Top if clockwise)
    if leds_left > 0:
        for i in range(leds_left):
            fraction = (i + 0.5) / leds_left
            y = (1.0 - fraction) if clockwise else fraction
            points.append([margin, round(y, 4)])

    # 2. Top Edge (Left to Right)
    if leds_top > 0:
        for i in range(leds_top):
            fraction = (i + 0.5) / leds_top
            x = fraction if clockwise else (1.0 - fraction)
            points.append([round(x, 4), margin])

    # 3. Right Edge (Top to Bottom)
    if leds_right > 0:
        for i in range(leds_right):
            fraction = (i + 0.5) / leds_right
            y = fraction if clockwise else (1.0 - fraction)
            points.append([1.0 - margin, round(y, 4)])

    # 4. Bottom Edge (Right to Left if 4 sides)
    if leds_bottom > 0:
        for i in range(leds_bottom):
            fraction = (i + 0.5) / leds_bottom
            x = (1.0 - fraction) if clockwise else fraction
            points.append([round(x, 4), 1.0 - margin])

    return points


_EXE_RE = re.compile(r"[\w .\-()]{1,60}")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _clean_rules(rules) -> list:
    """Application rules: {"exe", "profile", "mode"}. Anything malformed is dropped, never repaired by guessing."""
    out = []
    for r in rules if isinstance(rules, (list, tuple)) else []:
        if not isinstance(r, dict) or len(out) >= MAX_RULES:
            continue
        exe = str(r.get("exe", "")).strip().lower()
        profile = str(r.get("profile", ""))[:40]
        mode = r.get("mode", "")
        if not _EXE_RE.fullmatch(exe) or (mode and mode not in ("screen", "rhythm", "effect", "static", "off")):
            continue
        out.append({"exe": exe, "profile": profile, "mode": mode or ""})
    return out


def _clean_schedules(items) -> list:
    """Schedules: {"time": "HH:MM", "days": [0-6], "action", "value"}."""
    out = []
    for s in items if isinstance(items, (list, tuple)) else []:
        if not isinstance(s, dict) or len(out) >= MAX_RULES:
            continue
        time_ = str(s.get("time", ""))
        days = sorted({int(d) for d in s.get("days", []) if isinstance(d, (int, float)) and 0 <= int(d) <= 6}) if isinstance(s.get("days", []), (list, tuple)) else []
        action = s.get("action")
        if not _TIME_RE.fullmatch(time_) or action not in SCHEDULE_ACTIONS or not days:
            continue
        out.append({"time": time_, "days": days, "action": action, "value": str(s.get("value", ""))[:40]})
    return out


def _clean_palettes(palettes) -> Dict[str, List[List[int]]]:
    """Per-effect colour choices: only known effects, exactly three [r, g, b] each (anything else is dropped)."""
    if not isinstance(palettes, dict):
        return {}
    clean: Dict[str, List[List[int]]] = {}
    for key, colors in palettes.items():
        if key in EFFECT_KEYS and isinstance(colors, (list, tuple)) and len(colors) == 3 and all(
                isinstance(c, (list, tuple)) and len(c) == 3 for c in colors):
            clean[key] = [[_clamp(v, 0, 255, 0, int) for v in c] for c in colors]
    return clean


def _clean_monitor_color_profiles(profiles) -> Dict[str, dict]:
    """Per-monitor visual calibration. Keys are local monitor indexes, never display names."""
    clean: Dict[str, dict] = {}
    if not isinstance(profiles, dict):
        return clean
    for raw_index, values in profiles.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if not 0 <= index <= 7 or not isinstance(values, dict):
            continue
        wb = values.get("white_balance")
        clean[str(index)] = {
            "brightness": _clamp(values.get("brightness"), 0.0, 1.0, 0.85),
            "gamma": _clamp(values.get("gamma"), 1.0, 3.0, 2.2),
            "saturation": _clamp(values.get("saturation"), 0.0, 3.0, 1.3),
            "smoothing": _clamp(values.get("smoothing"), 0.01, 1.0, 0.3),
            "min_luminance_floor": _clamp(values.get("min_luminance_floor"), 0, 255, 2, int),
            "white_balance": [_clamp(g, 0.3, 1.0, 1.0) for g in wb] if (
                isinstance(wb, (list, tuple)) and len(wb) == 3) else [1.0, 1.0, 1.0],
            "dark_light": _clamp(values.get("dark_light"), 0.0, 0.3, 0.0),
            "hdr_compensation": _clamp(values.get("hdr_compensation"), 0.0, 1.0, 0.0),
        }
    return clean


@dataclass
class AppConfig:
    # 1. Hardware Layout & Wiring (Preserved during tuning reset)
    wire_map: str = "RGB"               # "RGB", "GRB", "BGR", "RBG", "BRG", "GBR"
    led_count: int = 63                 # Total LEDs on the strip
    edge_mode: str = "3_sides"          # "3_sides" (L, T, R) or "4_sides" (L, T, R, B)
    leds_left: int = 17
    leds_top: int = 29
    leds_right: int = 17
    leds_bottom: int = 0
    clockwise: bool = True              # True = L -> T -> R, False = R -> T -> L
    monitor_index: int = 0              # 0 = primary monitor
    monitor_color_profiles: Dict[str, dict] = field(default_factory=dict)  # visual calibration by monitor index

    # 2. Interactive LED Screen Coordinates (Each LED has its own draggable sample point [x, y])
    led_points: List[List[float]] = field(default_factory=list)
    sample_box_size: float = 0.06       # Percentage of screen area sampled per LED (6%)
    edge_margin: float = 0.035          # Distance of the automatic points from the screen edge (3.5%)

    # 3. Image Processing & Colorimetry Tuning
    brightness: float = 0.85            # 0.05 to 1.0 (85% default)
    gamma: float = 2.2                  # 1.0 to 3.0 (2.2 default)
    saturation: float = 1.30            # 0.5 to 2.5 (1.30 default)
    smoothing: float = 0.30             # 0.05 to 0.9 (0.30 default)
    min_luminance_floor: int = 2        # Soft ambient floor (no harsh black cutoffs)
    black_bar_detection: bool = True    # Auto-detect letterbox bars
    hdr_compensation: float = 0.0       # manual SDR tone-map compensation, only for screen capture

    # 4. Presets & Performance
    sync_preset: str = "movie"          # "game", "movie", "vivid", "soft"
    target_fps: int = 40                # 20, 30, 40, or 60 FPS
    performance_mode: str = "balanced" # Eco (20 FPS), balanced (40 FPS), fluid (60 FPS), or custom

    # 5. Audio Reactive Tuning (100% Software)
    rhythm_pattern: int = 0             # 0 to 6 (Pickup 1 to 7)
    rhythm_palette: str = "cyberpunk"   # "cyberpunk", "fire", "neon_blue", "matrix", "vaporwave", "sunset", "aurora"
    audio_sensitivity: float = 1.5      # 0.5 to 3.0
    audio_device_id: str = ""          # empty = follow the default Windows output device

    # 6. Dynamic Effects & Static Color
    active_effect: str = "rainbow"      # "rainbow", "breathing", "fire", "cycle"
    effect_speed: float = 1.0           # 0.2 to 3.0
    static_color: List[int] = field(default_factory=lambda: [255, 180, 100])

    # 6b. Light quality
    white_balance: List[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])  # per-channel gain 0.3-1
    max_power: float = 1.0              # cap on the total light (share of the maximum), protects the USB supply
    dark_light: float = 0.0             # warm background light while the screen is dark (0-30 %)
    dithering: bool = True              # temporal dithering: no visible steps in dark gradients
    adaptive_smoothing: bool = True     # smoothing gives way on scene cuts
    transitions: bool = True            # cross-fade between modes and when turning on/off

    # 6c. Screen mode options
    screen_style: str = "edge"          # "edge" (each LED its own zone), "average" (whole screen), "halves" (left/right)
    edges_enabled: List[bool] = field(default_factory=lambda: [True, True, True, True])  # left, top, right, bottom
    audio_boost: float = 0.0            # bass pulse on top of the picture (0-1)

    # 6d. Effects options
    effect_palettes: Dict[str, List[List[int]]] = field(default_factory=dict)   # colours the user changed, per effect
    effect_reverse: bool = False
    effect_mirror: bool = False

    # 6e. Automation
    app_rules_enabled: bool = False
    app_rules: List[dict] = field(default_factory=list)       # [{"exe": "game.exe", "profile": "Juego", "mode": "screen"}]
    schedules: List[dict] = field(default_factory=list)       # [{"time": "22:30", "days": [0..6], "action": "off", "value": ""}]
    idle_off_minutes: int = 0           # turn the LEDs off after this long without activity (0 = never)
    hotkeys_enabled: bool = True
    notifications: bool = True
    onboarded: bool = False             # set once the setup wizard has been completed (or skipped)

    # 7. Global State
    current_mode: str = "screen"        # "screen", "rhythm", "effect", "static", "off", "calibrate"
    run_on_startup: bool = False
    ui_autoclose_minutes: int = 0       # close the window after this long unused, in the background (0 = never)
    low_power_ui: bool = True           # UI without GPU acceleration + integrated GPU preference

    def palette_for(self, effect: str) -> List[List[int]]:
        """The three colours to use for an effect: the user's own choice, or the one it starts with."""
        return [list(c) for c in self.effect_palettes.get(effect) or default_palette(effect)]

    def sanitize(self):
        """Forces every field into a valid range/type: config.json is user-editable and may be damaged."""
        self.wire_map = self.wire_map.upper() if isinstance(self.wire_map, str) and self.wire_map.upper() in WIRE_MAP_NAMES else "RGB"
        self.edge_mode = self.edge_mode if self.edge_mode in ("3_sides", "4_sides") else "3_sides"
        self.led_count = _clamp(self.led_count, 1, MAX_LEDS, 63, int)
        for name in ("leds_left", "leds_top", "leds_right", "leds_bottom"):
            setattr(self, name, _clamp(getattr(self, name), 0, MAX_LEDS, 0, int))
        self.clockwise = bool(self.clockwise)
        self.monitor_index = _clamp(self.monitor_index, 0, 7, 0, int)
        self.sample_box_size = _clamp(self.sample_box_size, 0.01, 0.25, 0.06)
        self.edge_margin = _clamp(self.edge_margin, 0.0, 0.2, 0.035)
        self.brightness = _clamp(self.brightness, 0.0, 1.0, 0.85)
        self.gamma = _clamp(self.gamma, 1.0, 3.0, 2.2)
        self.saturation = _clamp(self.saturation, 0.0, 3.0, 1.3)
        self.smoothing = _clamp(self.smoothing, 0.01, 1.0, 0.3)
        self.min_luminance_floor = _clamp(self.min_luminance_floor, 0, 255, 2, int)
        self.black_bar_detection = bool(self.black_bar_detection)
        self.hdr_compensation = _clamp(self.hdr_compensation, 0.0, 1.0, 0.0)
        self.sync_preset = self.sync_preset if self.sync_preset in PRESET_NAMES else "movie"
        self.target_fps = _clamp(self.target_fps, 15, 60, 40, int)
        self.performance_mode = self.performance_mode if self.performance_mode in PERFORMANCE_MODES else "custom"
        self.rhythm_pattern = _clamp(self.rhythm_pattern, 0, 9, 0, int)
        self.rhythm_palette = self.rhythm_palette if self.rhythm_palette in PALETTE_NAMES else "cyberpunk"
        self.audio_sensitivity = _clamp(self.audio_sensitivity, 0.2, 5.0, 1.5)
        self.audio_device_id = self._clean_audio_device_id(self.audio_device_id)
        self.monitor_color_profiles = _clean_monitor_color_profiles(self.monitor_color_profiles)
        self.active_effect = self.active_effect if self.active_effect in EFFECT_NAMES else "rainbow"
        self.effect_speed = _clamp(self.effect_speed, 0.2, 3.0, 1.0)
        # calibration is a temporary test state: starting in it would leave the strip idle and dark
        self.current_mode = self.current_mode if self.current_mode in MODE_NAMES and self.current_mode != "calibrate" else "screen"
        self.run_on_startup = bool(self.run_on_startup)
        self.low_power_ui = bool(self.low_power_ui)

        self.white_balance = [_clamp(g, 0.3, 1.0, 1.0) for g in self.white_balance] if (
            isinstance(self.white_balance, (list, tuple)) and len(self.white_balance) == 3) else [1.0, 1.0, 1.0]
        self.max_power = _clamp(self.max_power, 0.05, 1.0, 1.0)
        self.dark_light = _clamp(self.dark_light, 0.0, 0.3, 0.0)
        self.dithering = bool(self.dithering)
        self.adaptive_smoothing = bool(self.adaptive_smoothing)
        self.transitions = bool(self.transitions)
        self.screen_style = self.screen_style if self.screen_style in SCREEN_STYLES else "edge"
        self.edges_enabled = [bool(v) for v in self.edges_enabled] if (
            isinstance(self.edges_enabled, (list, tuple)) and len(self.edges_enabled) == 4) else [True] * 4
        self.audio_boost = _clamp(self.audio_boost, 0.0, 1.0, 0.0)
        self.effect_palettes = _clean_palettes(self.effect_palettes)
        self.effect_reverse = bool(self.effect_reverse)
        self.effect_mirror = bool(self.effect_mirror)
        self.app_rules_enabled = bool(self.app_rules_enabled)
        self.app_rules = _clean_rules(self.app_rules)
        self.schedules = _clean_schedules(self.schedules)
        self.idle_off_minutes = _clamp(self.idle_off_minutes, 0, 600, 0, int)
        self.ui_autoclose_minutes = _clamp(self.ui_autoclose_minutes, 0, 600, 0, int)
        self.hotkeys_enabled = bool(self.hotkeys_enabled)
        self.notifications = bool(self.notifications)
        self.onboarded = bool(self.onboarded)

        color = self.static_color if isinstance(self.static_color, (list, tuple)) and len(self.static_color) == 3 else [255, 180, 100]
        self.static_color = [_clamp(c, 0, 255, 0, int) for c in color]

        # led_points: N finite [x, y] pairs inside the screen
        valid = isinstance(self.led_points, list) and all(
            isinstance(pt, (list, tuple)) and len(pt) == 2 for pt in self.led_points
        )
        if valid:
            self.led_points = [[_clamp(pt[0], 0.0, 1.0, 0.5), _clamp(pt[1], 0.0, 1.0, 0.5)] for pt in self.led_points]
        else:
            self.led_points = []

    @staticmethod
    def _clean_audio_device_id(value) -> str:
        """Endpoint IDs are opaque Windows strings; only reject control characters and huge input."""
        if not isinstance(value, str):
            return ""
        if any(ord(ch) < 32 for ch in value):
            return ""
        return value.strip()[:512]

    def has_monitor_color_profile(self, monitor_index: int | None = None) -> bool:
        index = self.monitor_index if monitor_index is None else _clamp(monitor_index, 0, 7, 0, int)
        return str(index) in self.monitor_color_profiles

    def save_monitor_color_profile(self, monitor_index: int | None = None):
        index = self.monitor_index if monitor_index is None else _clamp(monitor_index, 0, 7, 0, int)
        self.monitor_color_profiles[str(index)] = {
            "brightness": self.brightness,
            "gamma": self.gamma,
            "saturation": self.saturation,
            "smoothing": self.smoothing,
            "min_luminance_floor": self.min_luminance_floor,
            "white_balance": list(self.white_balance),
            "dark_light": self.dark_light,
            "hdr_compensation": self.hdr_compensation,
        }

    def restore_monitor_color_profile(self, monitor_index: int | None = None) -> bool:
        index = self.monitor_index if monitor_index is None else _clamp(monitor_index, 0, 7, 0, int)
        profile = self.monitor_color_profiles.get(str(index))
        if not profile:
            return False
        for key in MONITOR_COLOR_FIELDS:
            setattr(self, key, list(profile[key]) if key == "white_balance" else profile[key])
        return True

    def delete_monitor_color_profile(self, monitor_index: int | None = None) -> bool:
        index = self.monitor_index if monitor_index is None else _clamp(monitor_index, 0, 7, 0, int)
        return self.monitor_color_profiles.pop(str(index), None) is not None

    def __post_init__(self):
        self.sanitize()
        # Ensure led_points is populated and matches led_count
        if not self.led_points or len(self.led_points) != self.led_count:
            self.led_points = generate_default_led_points(
                self.leds_left, self.leds_top, self.leds_right, self.leds_bottom, self.clockwise, self.edge_margin
            )
            # If led_count differs from generated points, adjust length
            if len(self.led_points) < self.led_count:
                pad = self.led_count - len(self.led_points)
                self.led_points.extend([[0.5, 0.05]] * pad)
            elif len(self.led_points) > self.led_count:
                self.led_points = self.led_points[:self.led_count]


def reset_tuning_defaults(config: AppConfig) -> AppConfig:
    """
    Resets all color, sync, rhythm, and slider settings to factory defaults
    while strictly PRESERVING user hardware calibration and custom LED points:
    - wire_map
    - led_count
    - leds_left, leds_top, leds_right, leds_bottom
    - edge_mode
    - clockwise
    - monitor_index
    - led_points
    """
    config.brightness = 0.85
    config.gamma = 2.2
    config.saturation = 1.30
    config.smoothing = 0.30
    config.min_luminance_floor = 2
    config.black_bar_detection = True
    config.hdr_compensation = 0.0
    config.sync_preset = "movie"
    config.target_fps = 40
    config.performance_mode = "balanced"
    config.rhythm_pattern = 0
    config.rhythm_palette = "cyberpunk"
    config.audio_sensitivity = 1.5
    config.audio_device_id = ""
    config.delete_monitor_color_profile()
    config.active_effect = "rainbow"
    config.effect_speed = 1.0
    config.static_color = [255, 180, 100]
    config.white_balance = [1.0, 1.0, 1.0]
    config.max_power = 1.0
    config.dark_light = 0.0
    config.dithering = True
    config.adaptive_smoothing = True
    config.transitions = True
    config.screen_style = "edge"
    config.edges_enabled = [True, True, True, True]
    config.audio_boost = 0.0
    config.effect_palettes = {}
    config.effect_reverse = False
    config.effect_mirror = False
    return config


def load_config() -> AppConfig:
    """Loads configuration from config.json, or creates default."""
    if not os.path.exists(CONFIG_FILE_PATH):
        # First run of an installed build: seed from the calibration shipped in the package.
        bundled = os.path.join(resource_dir(), "config.json")
        if bundled != CONFIG_FILE_PATH and os.path.exists(bundled):
            shutil.copyfile(bundled, CONFIG_FILE_PATH)
            return load_config()
        cfg = AppConfig()
        save_config(cfg)
        return cfg

    backup = CONFIG_FILE_PATH + ".bak"
    try:
        cfg = _read_config_file(CONFIG_FILE_PATH)
    except Exception as e:
        log.error("config.json is unreadable (%s)", e)
        # Keep the damaged file for inspection and fall back to the last good copy, if any
        try:
            shutil.copyfile(CONFIG_FILE_PATH, CONFIG_FILE_PATH + ".corrupt")
        except OSError:
            pass
        try:
            cfg = _read_config_file(backup)
        except Exception:
            log.warning("No valid backup either: starting with default settings")
            return AppConfig()
        log.warning("Restored settings from %s", backup)
        save_config(cfg)
        return cfg

    # Remember the last configuration that loaded fine
    try:
        shutil.copyfile(CONFIG_FILE_PATH, backup)
    except OSError:
        pass
    return cfg


def _read_config_file(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("config root is not an object")
    valid_data = {k: v for k, v in data.items() if k in AppConfig.__dataclass_fields__}
    # Configurations created before the performance selector keep their effective frame rate.
    if "performance_mode" not in valid_data:
        valid_data["performance_mode"] = {20: "eco", 40: "balanced", 60: "fluid"}.get(
            valid_data.get("target_fps"), "custom"
        )
    valid_data.setdefault("onboarded", True)  # a settings file from an earlier version is already calibrated
    cfg = AppConfig(**valid_data)
    cfg.restore_monitor_color_profile()  # only when loading the active configuration, not profile validation
    return cfg


def save_config(config: AppConfig):
    """Saves configuration atomically: a crash mid-write can never leave a truncated file."""
    with _io_lock:
        tmp = CONFIG_FILE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(config), f, indent=4)
            os.replace(tmp, CONFIG_FILE_PATH)
        except Exception:
            log.exception("Could not save %s", CONFIG_FILE_PATH)
