"""
Request models of the local API: every value a client sends is validated here, before it reaches the engine.
Kept apart from the routes (api.py) so the validation rules can be read, and tested, on their own.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.animations import EFFECT_KEYS
from core.config import (
    EFFECT_NAMES, MAX_LEDS, MODE_NAMES, PALETTE_NAMES, PERFORMANCE_MODES, PRESET_NAMES, WIRE_MAP_NAMES,
)
from core.profiles import NAME_PATTERN

Mode = Literal["screen", "rhythm", "static", "effect", "off", "calibrate"]
WireMap = Literal["RGB", "RBG", "GRB", "GBR", "BRG", "BGR"]
Preset = Literal["game", "movie", "vivid", "soft"]
Effect = Literal[EFFECT_KEYS]  # every effect in core/animations.py
ScreenStyle = Literal["edge", "average", "halves"]
ScheduleAction = Literal["on", "off", "mode", "profile", "brightness", "sunrise"]
Palette = Literal["cyberpunk", "fire", "neon_blue", "matrix", "vaporwave", "sunset"]
PerformanceMode = Literal["eco", "balanced", "fluid", "custom"]
MonitorColorProfileAction = Literal["save", "load", "delete"]
MeasurementAction = Literal["start", "stop"]
MeasurementScenario = Literal["desktop", "video", "game"]
CalibrateAction = Literal[
    "test_red", "test_green", "test_blue", "test_white", "test_edges", "test_brightness_cycle", "stop",
    "test_channel", "test_led",   # setup wizard: raw color channel / a single LED
]
AutoLayout = Literal["3_sides", "4_sides", "sides", "center", "reverse"]

# Names must stay in sync with the engine's configuration module
assert set(MODE_NAMES) == set(Mode.__args__) and set(EFFECT_NAMES) == set(Effect.__args__)
assert set(PALETTE_NAMES) == set(Palette.__args__) and set(PRESET_NAMES) == set(Preset.__args__)
assert set(WIRE_MAP_NAMES) == set(WireMap.__args__)
assert set(PERFORMANCE_MODES) == set(PerformanceMode.__args__)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ModeRequest(ApiModel):
    mode: Mode

    @field_validator("mode", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.lower() if isinstance(v, str) else v


class SettingsRequest(ApiModel):
    wire_map: Optional[WireMap] = None
    brightness: Optional[float] = Field(None, ge=0.0, le=1.0)
    gamma: Optional[float] = Field(None, ge=1.0, le=3.0)
    saturation: Optional[float] = Field(None, ge=0.0, le=3.0)
    smoothing: Optional[float] = Field(None, ge=0.01, le=1.0)
    min_luminance_floor: Optional[int] = Field(None, ge=0, le=255)
    black_bar_detection: Optional[bool] = None
    hdr_compensation: Optional[float] = Field(None, ge=0.0, le=1.0)
    target_fps: Optional[int] = Field(None, ge=15, le=60)
    performance_mode: Optional[PerformanceMode] = None
    monitor_index: Optional[int] = Field(None, ge=0, le=7)
    sample_box_size: Optional[float] = Field(None, ge=0.01, le=0.25)
    rhythm_pattern: Optional[int] = Field(None, ge=0, le=9)
    rhythm_palette: Optional[Palette] = None
    audio_sensitivity: Optional[float] = Field(None, ge=0.2, le=5.0)
    audio_device_id: Optional[str] = Field(None, max_length=512)
    white_balance: Optional[List[float]] = Field(None, min_length=3, max_length=3)
    max_power: Optional[float] = Field(None, ge=0.05, le=1.0)
    dark_light: Optional[float] = Field(None, ge=0.0, le=0.3)
    dithering: Optional[bool] = None
    adaptive_smoothing: Optional[bool] = None
    transitions: Optional[bool] = None
    screen_style: Optional[ScreenStyle] = None
    edges_enabled: Optional[List[bool]] = Field(None, min_length=4, max_length=4)
    audio_boost: Optional[float] = Field(None, ge=0.0, le=1.0)
    idle_off_minutes: Optional[int] = Field(None, ge=0, le=600)
    hotkeys_enabled: Optional[bool] = None
    notifications: Optional[bool] = None
    onboarded: Optional[bool] = None

    @field_validator("white_balance")
    @classmethod
    def _gains(cls, v):
        if v is not None and not all(0.3 <= g <= 1.0 for g in v):
            raise ValueError("each gain must be between 0.3 and 1.0")
        return v

    @field_validator("wire_map", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.upper() if isinstance(v, str) else v

    @field_validator("audio_device_id")
    @classmethod
    def _audio_device_id(cls, v):
        if v is not None and any(ord(ch) < 32 for ch in v):
            raise ValueError("audio device ID cannot contain control characters")
        return v.strip() if v is not None else v


class LedPointsRequest(ApiModel):
    points: Optional[List[List[float]]] = Field(None, min_length=1, max_length=MAX_LEDS)
    box_size: Optional[float] = Field(None, ge=0.01, le=0.25)

    @field_validator("points")
    @classmethod
    def _points(cls, pts):
        if pts is None:
            return pts
        out = []
        for pt in pts:
            if len(pt) != 2 or not all(0.0 <= c <= 1.0 for c in pt):  # NaN fails these comparisons too
                raise ValueError("each point must be [x, y] with both values in 0..1")
            out.append([round(pt[0], 4), round(pt[1], 4)])
        return out


class ZonesRequest(ApiModel):
    """Simple per-edge controls. Applying them re-creates the automatic points (custom ones are replaced)."""
    leds_left: int = Field(ge=0, le=MAX_LEDS)
    leds_top: int = Field(ge=0, le=MAX_LEDS)
    leds_right: int = Field(ge=0, le=MAX_LEDS)
    leds_bottom: int = Field(ge=0, le=MAX_LEDS)
    margin: float = Field(0.035, ge=0.0, le=0.2)
    box_size: float = Field(0.06, ge=0.01, le=0.25)


class AutoLayoutRequest(ApiModel):
    mode: AutoLayout = "3_sides"


class EdgeLedsRequest(ApiModel):
    leds_left: Optional[int] = Field(None, ge=0, le=MAX_LEDS)
    leds_top: Optional[int] = Field(None, ge=0, le=MAX_LEDS)
    leds_right: Optional[int] = Field(None, ge=0, le=MAX_LEDS)
    leds_bottom: Optional[int] = Field(None, ge=0, le=MAX_LEDS)


class PresetRequest(ApiModel):
    preset: Preset = "movie"

    @field_validator("preset", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.lower() if isinstance(v, str) else v


class ColorRequest(ApiModel):
    r: int = Field(ge=0, le=255)
    g: int = Field(ge=0, le=255)
    b: int = Field(ge=0, le=255)


class EffectRequest(ApiModel):
    effect: Effect
    speed: float = Field(1.0, ge=0.2, le=3.0)
    colors: Optional[List[List[int]]] = Field(None, min_length=3, max_length=3)
    reset_colors: Optional[bool] = None       # go back to the colours the effect starts with
    reverse: Optional[bool] = None
    mirror: Optional[bool] = None

    @field_validator("colors")
    @classmethod
    def _palette(cls, v):
        if v is not None and not all(len(c) == 3 and all(0 <= x <= 255 for x in c) for c in v):
            raise ValueError("colors must be three [r, g, b] with values 0-255")
        return v


class RuleModel(ApiModel):
    exe: str = Field(min_length=1, max_length=60, pattern=r"^[\w .\-()]+$")
    profile: str = Field("", max_length=40)
    mode: Literal["", "screen", "rhythm", "effect", "static", "off"] = ""


class ScheduleModel(ApiModel):
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: List[int] = Field(min_length=1, max_length=7)
    action: ScheduleAction
    value: str = Field("", max_length=40)

    @field_validator("days")
    @classmethod
    def _days(cls, v):
        if not all(0 <= d <= 6 for d in v):
            raise ValueError("days are 0 (Monday) to 6 (Sunday)")
        return sorted(set(v))


class AutomationRequest(ApiModel):
    app_rules_enabled: Optional[bool] = None
    app_rules: Optional[List[RuleModel]] = Field(None, max_length=30)
    schedules: Optional[List[ScheduleModel]] = Field(None, max_length=30)


class ImportRequest(ApiModel):
    config: Optional[dict] = None
    profiles: Optional[dict] = None


class PaletteRequest(ApiModel):
    palette: Palette


class CalibrateRequest(ApiModel):
    action: CalibrateAction
    index: int = Field(0, ge=0, le=MAX_LEDS)   # LED number (test_led) or channel 0-2 (test_channel)


class ProfileRequest(ApiModel):
    name: str = Field(min_length=1, max_length=40, pattern=NAME_PATTERN)


class StartupRequest(ApiModel):
    autostart: Optional[bool] = None
    low_power_ui: Optional[bool] = None
    ui_autoclose_minutes: Optional[int] = Field(None, ge=0, le=600)


class MonitorColorProfileRequest(ApiModel):
    action: MonitorColorProfileAction


class PerformanceMeasurementRequest(ApiModel):
    action: MeasurementAction
    scenario: MeasurementScenario = "desktop"
