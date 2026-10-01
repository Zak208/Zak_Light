"""
Zak_light local API (FastAPI) used by the window UI.

create_app(state) builds the app around an EngineState, so tests can drive it with a simulated
driver. Every input is validated (types, ranges, allowed names) and every request must come from
this machine's own pages (Host/Origin checks) with a JSON body: a web page opened in the browser
must not be able to drive the lights or rewrite the settings.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import conflicts as conflicts_mod
from core import system as winsys
from core import display as display_info
from core import windows_info
from core.animations import catalogue
from core.color import ColorPipeline
from core.config import (
    AppConfig, MAX_LEDS, generate_default_led_points, reset_tuning_defaults,
)
from core.paths import data_dir
from core.profiles import ProfileError
from engine import EngineState
from version import VERSION

log = logging.getLogger("zak_light")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_BODY_BYTES = 1_000_000  # nothing legitimate is bigger (a full backup is a few tens of KB)

from api_models import (
    ModeRequest,
    SettingsRequest,
    LedPointsRequest,
    ZonesRequest,
    AutoLayoutRequest,
    EdgeLedsRequest,
    PresetRequest,
    ColorRequest,
    EffectRequest,
    AutomationRequest,
    ImportRequest,
    PaletteRequest,
    CalibrateRequest,
    ProfileRequest,
    StartupRequest,
    MonitorColorProfileRequest,
    PerformanceMeasurementRequest,
)

PRESET_VALUES = {
    "game": dict(smoothing=0.85, saturation=1.30, black_bar_detection=False),
    "movie": dict(smoothing=0.30, saturation=1.30, black_bar_detection=True),
    "vivid": dict(smoothing=0.35, saturation=1.80, black_bar_detection=True),
    "soft": dict(smoothing=0.15, saturation=1.05, black_bar_detection=True),
}

# This is deliberately independent from the image presets. A user may prefer the colour
# treatment of "movie" while choosing a lower update rate for a laptop, for example.
PERFORMANCE_FPS = {"eco": 20, "balanced": 40, "fluid": 60}


def zones_customized(cfg) -> bool:
    """True if the LED points differ from what the automatic layout would produce (the user dragged some)."""
    auto = generate_default_led_points(cfg.leds_left, cfg.leds_top, cfg.leds_right, cfg.leds_bottom,
                                       cfg.clockwise, cfg.edge_margin)
    return cfg.led_points != auto


def _hostname(host_port: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '[::1]', 'evil.com' -> 'evil.com'."""
    host_port = host_port.strip().lower()
    if host_port.startswith("["):
        return host_port.split("]")[0] + "]"
    return host_port.rsplit(":", 1)[0] if ":" in host_port else host_port


def create_app(state: EngineState, web_dir: str, on_quit=None) -> FastAPI:
    # Looking at every running process is cheap (~6 ms) but the status is polled: cache it briefly
    conflict_cache = {"at": 0.0, "value": []}

    def current_conflicts(force: bool = False):
        now = time.monotonic()
        if force or now - conflict_cache["at"] > 4.0:
            conflict_cache["value"] = conflicts_mod.active_conflicts()
            conflict_cache["at"] = now
        return conflict_cache["value"]

    hdr_cache = {"at": -1e9, "value": None}

    def current_hdr():
        now = time.monotonic()
        if now - hdr_cache["at"] > 60.0:
            hdr_cache["value"], hdr_cache["at"] = display_info.hdr_enabled(), now
        return hdr_cache["value"]

    def audio_status():
        getter = getattr(state.audio_engine, "audio_status", None)
        if callable(getter):
            return getter()
        return {"available": False, "active": False, "selected": False, "active_device": "", "using_fallback": False, "last_error": ""}

    def diagnostics_payload():
        """A shareable support report: operational state only, never paths, pictures or app names."""
        cfg = state.config
        audio = audio_status()
        return {
            "format": "zak_light_diagnostics",
            "format_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "app_version": VERSION,
            "runtime": {
                "connected": state.driver.is_connected,
                "mode": state.mode,
                "suspended": state.suspended,
                "away": state.away,
                "capture_backend": state.screen_sampler.backend,
                "hdr_reported": current_hdr(),
            },
            "configuration": {
                "led_count": cfg.led_count,
                "monitor_index": cfg.monitor_index,
                "performance_mode": cfg.performance_mode,
                "target_fps": cfg.target_fps,
                "sample_box_size": cfg.sample_box_size,
                "black_bar_detection": cfg.black_bar_detection,
                "screen_style": cfg.screen_style,
                "edges_enabled": cfg.edges_enabled,
                "audio_boost": cfg.audio_boost,
                "audio_device_selected": bool(cfg.audio_device_id),
                "hdr_compensation": cfg.hdr_compensation,
                "monitor_color_profile_saved": cfg.has_monitor_color_profile(),
            },
            "metrics": state.metrics(),
            "engine": state.health(),
            "audio": {
                "available": bool(audio.get("available")),
                "active": bool(audio.get("active")),
                "selected": bool(audio.get("selected")),
                "using_fallback": bool(audio.get("using_fallback")),
                "has_error": bool(audio.get("last_error")),
            },
            "performance_measurement": state.performance_measurement_status(),
        }

    app = FastAPI(title="Zak_light API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        """FastAPI's default handler echoes the rejected input, and `Infinity`/`NaN` cannot be put
        in a JSON reply: a hostile value would turn the 422 into a 500. Report field + reason only."""
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": "invalid request", "details": details})

    # ---- request guard -----------------------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS:
            # DNS rebinding: a foreign name that resolves to 127.0.0.1
            return JSONResponse(status_code=403, content={"error": "forbidden host"})

        origin = request.headers.get("origin")
        if origin is not None and _hostname(origin.split("://", 1)[-1]) not in LOCAL_HOSTS:
            return JSONResponse(status_code=403, content={"error": "forbidden origin"})

        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/"):
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(status_code=403, content={"error": "cross-site request"})
            try:
                if int(request.headers.get("content-length", "0") or 0) > MAX_BODY_BYTES:
                    return JSONResponse(status_code=413, content={"error": "request too large"})
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "bad content-length"})
            has_body = request.headers.get("content-length", "0") not in ("", "0") \
                or "transfer-encoding" in request.headers
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if has_body and ctype != "application/json":
                return JSONResponse(status_code=415, content={"error": "JSON body required"})
        return await call_next(request)

    static_dir = os.path.join(web_dir, "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(web_dir, "index.html"))

    # ---- read-only ---------------------------------------------------------------------------

    @app.get("/api/status")
    async def status():
        cfg = state.config
        return {
            "version": VERSION,
            "connected": state.driver.is_connected,
            "suspended": state.suspended,
            "mode": state.mode,
            "active_effect": state.active_effect,
            "active_palette": state.active_palette,
            "audio_level": state.audio_engine.get_current_volume(),
            "capture_backend": state.screen_sampler.backend,
            "metrics": state.metrics(),
            "engine": state.health(),
            "conflicts": current_conflicts(),
            "config": {
                "wire_map": cfg.wire_map,
                "brightness": cfg.brightness,
                "gamma": cfg.gamma,
                "saturation": cfg.saturation,
                "smoothing": cfg.smoothing,
                "min_luminance_floor": cfg.min_luminance_floor,
                "black_bar_detection": cfg.black_bar_detection,
                "hdr_compensation": cfg.hdr_compensation,
                "target_fps": cfg.target_fps,
                "performance_mode": cfg.performance_mode,
                "led_count": cfg.led_count,
                "leds_left": cfg.leds_left,
                "leds_top": cfg.leds_top,
                "leds_right": cfg.leds_right,
                "leds_bottom": cfg.leds_bottom,
                "edge_mode": cfg.edge_mode,
                "clockwise": cfg.clockwise,
                "monitor_index": cfg.monitor_index,
                "monitor_color_profile_saved": cfg.has_monitor_color_profile(),
                "sample_box_size": cfg.sample_box_size,
                "edge_margin": cfg.edge_margin,
                "zones_customized": zones_customized(cfg),
                "sync_preset": cfg.sync_preset,
                "rhythm_pattern": cfg.rhythm_pattern,
                "rhythm_palette": cfg.rhythm_palette,
                "audio_sensitivity": cfg.audio_sensitivity,
                "audio_device_id": cfg.audio_device_id,
                "effect_speed": state.effect_speed,
                "static_color": state.static_color_rgb,
                "white_balance": cfg.white_balance,
                "max_power": cfg.max_power,
                "dark_light": cfg.dark_light,
                "dithering": cfg.dithering,
                "adaptive_smoothing": cfg.adaptive_smoothing,
                "transitions": cfg.transitions,
                "screen_style": cfg.screen_style,
                "edges_enabled": cfg.edges_enabled,
                "audio_boost": cfg.audio_boost,
                "effect_colors": cfg.palette_for(state.active_effect),     # colours of the effect in use
                "effect_palettes": cfg.effect_palettes,
                "effect_reverse": cfg.effect_reverse,
                "effect_mirror": cfg.effect_mirror,
                "app_rules_enabled": cfg.app_rules_enabled,
                "app_rules": cfg.app_rules,
                "schedules": cfg.schedules,
                "idle_off_minutes": cfg.idle_off_minutes,
                "ui_autoclose_minutes": cfg.ui_autoclose_minutes,
                "hotkeys_enabled": cfg.hotkeys_enabled,
                "notifications": cfg.notifications,
                "onboarded": cfg.onboarded,
            },
            "away": state.away,
            "capture_stalled": state.capture_stalled,
            "hdr": current_hdr(),
            "performance_measurement": state.performance_measurement_status(),
            "active_profile": state.profiles.matching(state.config),
            "active_rule": state.automation.active_rule["exe"] if state.automation and state.automation.active_rule else None,
        }

    @app.get("/api/led_points")
    async def get_led_points():
        """Served apart from /api/status: the list is large and only needed when editing."""
        return {"led_points": state.config.led_points}

    @app.get("/api/leds")
    async def leds(since: Optional[int] = None):
        """What the strip shows right now (for the live preview). Polled ~10 Hz while the window is visible."""
        revision = state.preview_revision
        if since == revision:
            return {"mode": state.mode, "known": state.preview_known(), "revision": revision, "unchanged": True}
        colors = state.preview_colors()
        return {"mode": state.mode, "known": colors is not None, "colors": colors or [], "revision": revision,
                "unchanged": False}

    @app.get("/api/spectrum")
    async def spectrum():
        """Lightweight endpoint the UI polls (~12 Hz) while the rhythm tab is visible."""
        return state.audio_engine.spectrum()

    @app.get("/api/audio_devices")
    async def audio_devices():
        """Output endpoint list, intentionally requested only when opening/refreshing Settings."""
        getter = getattr(state.audio_engine, "audio_devices", None)
        devices = getter() if callable(getter) else [{"id": "", "name": "Salida predeterminada de Windows", "default": True}]
        return {"devices": devices, "selected": state.config.audio_device_id, "status": audio_status()}

    @app.get("/api/startup")
    async def get_startup():
        return {"autostart": winsys.is_autostart_enabled(), "low_power_ui": state.config.low_power_ui,
                "ui_autoclose_minutes": state.config.ui_autoclose_minutes}

    @app.get("/api/diagnostics")
    async def diagnostics():
        return diagnostics_payload()

    @app.get("/api/diagnostics/export")
    async def export_diagnostics():
        return diagnostics_payload()

    @app.get("/api/performance_measurement")
    async def performance_measurement():
        return state.performance_measurement_status()

    @app.get("/api/performance_measurement/export")
    async def export_performance_measurement():
        report = state.performance_recorder.last_report()
        if report is None:
            return JSONResponse(status_code=404, content={"error": "no completed performance measurement"})
        return report


    # ---- monitors, profiles, folders -------------------------------------------------------------

    @app.get("/api/monitors")
    async def monitors():
        found = state.screen_sampler.list_monitors()
        items = [{
            "index": m["index"],
            "label": f"Monitor {m['index'] + 1} ({m['width']}x{m['height']}{', principal' if m['primary'] else ''})",
        } for m in found]
        return {"monitors": items, "selected": state.config.monitor_index}

    @app.post("/api/monitor_color_profile")
    async def monitor_color_profile(req: MonitorColorProfileRequest):
        """Store or restore visual tuning for the monitor currently selected by the user."""
        cfg = state.config
        if req.action == "save":
            cfg.save_monitor_color_profile()
        elif req.action == "load":
            if not cfg.restore_monitor_color_profile():
                return JSONResponse(status_code=404, content={"error": "no saved color profile for this monitor"})
        else:
            cfg.delete_monitor_color_profile()
        state.apply_config()
        if state.mode == "static":
            state.send_static_color()
        state.schedule_save()
        return {"status": "ok", "monitor_index": cfg.monitor_index, "saved": cfg.has_monitor_color_profile()}

    @app.get("/api/profiles")
    async def list_profiles():
        return {"profiles": state.profiles.names(), "active": state.profiles.matching(state.config)}

    @app.post("/api/profiles")
    async def save_profile(req: ProfileRequest):
        try:
            state.profiles.save(req.name, state.config)
        except ProfileError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return {"status": "ok", "profiles": state.profiles.names()}

    @app.post("/api/profiles/load")
    async def load_profile(req: ProfileRequest):
        try:
            state.apply_profile(req.name)
        except ProfileError as e:
            return JSONResponse(status_code=404, content={"error": str(e)})
        return {"status": "ok"}

    @app.post("/api/profiles/delete")
    async def delete_profile(req: ProfileRequest):
        if not state.profiles.delete(req.name):
            return JSONResponse(status_code=404, content={"error": "no such profile"})
        return {"status": "ok", "profiles": state.profiles.names()}

    @app.post("/api/conflicts/close")
    async def close_conflicts():
        """Closes the competing program (DX Light). Only ever the fixed list in core/conflicts.py."""
        closed = conflicts_mod.close_conflicts()
        return {"status": "ok", "closed": closed, "conflicts": current_conflicts(force=True)}

    @app.post("/api/conflicts/disable_autostart")
    async def disable_conflict_autostart():
        disabled = conflicts_mod.disable_conflict_autostart()
        return {"status": "ok", "disabled": disabled, "conflicts": current_conflicts(force=True)}

    # ---- effects catalogue, automation, apps, backup ---------------------------------------------------

    @app.get("/api/effects")
    async def effects():
        cfg = state.config
        items = [{**e, "defaults": e["colors"], "colors": cfg.palette_for(e["key"])} for e in catalogue()]
        return {"effects": items, "active": state.active_effect, "speed": state.effect_speed,
                "reverse": cfg.effect_reverse, "mirror": cfg.effect_mirror}

    @app.get("/api/automation")
    async def get_automation():
        cfg = state.config
        return {"app_rules_enabled": cfg.app_rules_enabled, "app_rules": cfg.app_rules, "schedules": cfg.schedules,
                "idle_off_minutes": cfg.idle_off_minutes, "hotkeys_enabled": cfg.hotkeys_enabled,
                "hotkeys": getattr(state, "hotkey_info", []),
                "active_rule": state.automation.active_rule["exe"] if state.automation and state.automation.active_rule else None}

    @app.post("/api/automation")
    async def set_automation(req: AutomationRequest):
        cfg = state.config
        if req.app_rules_enabled is not None:
            cfg.app_rules_enabled = req.app_rules_enabled
        if req.app_rules is not None:
            cfg.app_rules = [r.model_dump() for r in req.app_rules]
        if req.schedules is not None:
            cfg.schedules = [s.model_dump() for s in req.schedules]
        cfg.sanitize()  # one more pass: the stored form is the same one a file on disk would be held to
        state.schedule_save()
        return {"status": "ok", "app_rules": cfg.app_rules, "schedules": cfg.schedules}

    @app.get("/api/windows")
    async def windows():
        """Applications with a visible window, to pick one when creating a rule."""
        return {"apps": windows_info.open_apps()}

    @app.get("/api/export")
    async def export_settings():
        return {"format": "zak_light_backup", "version": VERSION, "config": asdict(state.config),
                "profiles": state.profiles.export_all()}

    @app.post("/api/import")
    async def import_settings(req: ImportRequest):
        """Restores a backup made with /api/export. Every value is sanitised exactly like a settings file."""
        imported = {"config": False, "profiles": 0}
        if req.profiles is not None:
            try:
                imported["profiles"] = state.profiles.import_all(req.profiles)
            except ProfileError as e:
                return JSONResponse(status_code=400, content={"error": str(e)})
        if req.config is not None:
            known = {k: v for k, v in req.config.items() if k in AppConfig.__dataclass_fields__}
            clean = AppConfig(**{**asdict(state.config), **known})
            for key in AppConfig.__dataclass_fields__:
                setattr(state.config, key, getattr(clean, key))
            state.config.restore_monitor_color_profile()
            state.active_effect, state.effect_speed = state.config.active_effect, state.config.effect_speed
            state.static_color_rgb = list(state.config.static_color)
            state.apply_config()
            if state.config.current_mode != "calibrate":
                state.set_mode(state.config.current_mode)
            imported["config"] = True
        state.schedule_save()
        return {"status": "ok", **imported}

    @app.post("/api/quit")
    async def quit_engine():
        """Clean shutdown (LEDs off). Used by the installer and `--quit`; only when the host app wired it."""
        if on_quit is None:
            return JSONResponse(status_code=404, content={"error": "not available"})
        threading.Thread(target=on_quit, name="quit", daemon=True).start()  # reply first, then stop
        return {"status": "ok"}

    @app.post("/api/open_data_dir")
    async def open_data_dir():
        """Opens the folder with the settings and the log file in Explorer (no path is taken from the request)."""
        os.startfile(data_dir())
        return {"status": "ok"}

    # ---- changes -----------------------------------------------------------------------------

    @app.post("/api/startup")
    async def set_startup(req: StartupRequest):
        if req.autostart is not None:
            if not winsys.set_autostart(req.autostart):
                return JSONResponse(status_code=500, content={"error": "No se pudo escribir el registro"})
            state.config.run_on_startup = req.autostart
        if req.low_power_ui is not None:
            state.config.low_power_ui = req.low_power_ui  # applies the next time the window opens
        if req.ui_autoclose_minutes is not None:
            state.config.ui_autoclose_minutes = req.ui_autoclose_minutes  # read when the window opens
        state.schedule_save()
        return {"status": "ok", "autostart": winsys.is_autostart_enabled(), "low_power_ui": state.config.low_power_ui,
                "ui_autoclose_minutes": state.config.ui_autoclose_minutes}

    @app.post("/api/mode")
    async def set_mode(req: ModeRequest):
        state.switch_mode(req.mode)  # calibration is a temporary state: never restored at startup
        return {"status": "ok", "mode": state.mode}

    @app.post("/api/performance_measurement")
    async def control_performance_measurement(req: PerformanceMeasurementRequest):
        if req.action == "start":
            if not state.start_performance_measurement(req.scenario):
                return JSONResponse(status_code=409, content={"error": "performance measurement already running"})
            return {"status": "ok", "measurement": state.performance_measurement_status()}
        was_active = state.performance_measurement_status()["active"]
        report = state.stop_performance_measurement()
        return {"status": "ok", "stopped": was_active, "measurement": state.performance_measurement_status(), "report": report}

    @app.post("/api/settings")
    async def update_settings(req: SettingsRequest):
        cfg = state.config
        previous_monitor = cfg.monitor_index
        values = req.model_dump(exclude_none=True)
        mode = values.pop("performance_mode", None)
        target_fps = values.pop("target_fps", None)
        if mode is not None:
            cfg.performance_mode = mode
            if mode != "custom":
                cfg.target_fps = PERFORMANCE_FPS[mode]
        if target_fps is not None:
            cfg.target_fps = target_fps
            cfg.performance_mode = "custom"
        for key, value in values.items():
            setattr(cfg, key, value)
        if req.monitor_index is not None and cfg.monitor_index != previous_monitor:
            cfg.restore_monitor_color_profile()
        state.apply_config()
        if req.hotkeys_enabled is not None and getattr(state, "hotkeys_control", None):
            state.hotkeys_control(req.hotkeys_enabled)   # start/stop the global shortcuts right away
        if state.mode == "static":
            state.send_static_color()
        state.schedule_save()
        return {"status": "ok"}

    @app.post("/api/led_points")
    async def update_led_points(req: LedPointsRequest):
        """Updates interactive draggable LED positions and sample box size."""
        cfg = state.config
        if req.points is not None:
            cfg.led_points = req.points
            cfg.led_count = len(req.points)
            state.audio_engine.led_count = cfg.led_count
            state.effect_engine.led_count = cfg.led_count
        if req.box_size is not None:
            cfg.sample_box_size = req.box_size
        state.apply_config()
        state.schedule_save()
        return {"status": "ok", "led_count": cfg.led_count}

    @app.post("/api/auto_layout")
    async def set_auto_layout(req: AutoLayoutRequest):
        """Automatically distributes the LED points evenly along screen edges or custom shapes."""
        mode = req.mode
        cfg = state.config
        n = cfg.led_count
        margin = cfg.edge_margin

        points = []
        if mode == "3_sides":
            # Keep the user's own 3-edge distribution when it matches the total count
            if (cfg.leds_left + cfg.leds_top + cfg.leds_right == n
                    and cfg.leds_left > 0 and cfg.leds_top > 0 and cfg.leds_right > 0):
                left_count, top_count, right_count = cfg.leds_left, cfg.leds_top, cfg.leds_right
            else:
                left_count = int(round(n * 0.25))
                right_count = int(round(n * 0.25))
                top_count = n - left_count - right_count
                cfg.leds_left, cfg.leds_top, cfg.leds_right = left_count, top_count, right_count
            cfg.leds_bottom = 0
            cfg.edge_mode = "3_sides"
            points = generate_default_led_points(left_count, top_count, right_count, 0, cfg.clockwise, cfg.edge_margin)

        elif mode == "4_sides":
            if (cfg.leds_left + cfg.leds_top + cfg.leds_right + cfg.leds_bottom == n
                    and cfg.leds_bottom > 0 and cfg.leds_left > 0 and cfg.leds_top > 0 and cfg.leds_right > 0):
                left_count, top_count = cfg.leds_left, cfg.leds_top
                right_count, bottom_count = cfg.leds_right, cfg.leds_bottom
            else:
                left_count = int(round(n * 0.20))
                right_count = int(round(n * 0.20))
                rem = n - left_count - right_count
                top_count = rem // 2
                bottom_count = rem - top_count
                cfg.leds_left, cfg.leds_top = left_count, top_count
                cfg.leds_right, cfg.leds_bottom = right_count, bottom_count
            cfg.edge_mode = "4_sides"
            points = generate_default_led_points(left_count, top_count, right_count, bottom_count, cfg.clockwise, cfg.edge_margin)

        elif mode == "sides":  # left & right dual bars
            half = n // 2
            cfg.leds_left, cfg.leds_top, cfg.leds_right, cfg.leds_bottom = half, 0, n - half, 0
            for i in range(half):
                points.append([margin, round(1.0 - (i + 0.5) / half, 4)])
            for i in range(n - half):
                points.append([1.0 - margin, round((i + 0.5) / (n - half), 4)])

        elif mode == "center":  # clustered in the center box
            for i in range(n):
                points.append([round(0.25 + 0.5 * (i + 0.5) / n, 4), 0.5])

        elif mode == "reverse":  # invert the order of the points
            cfg.clockwise = not cfg.clockwise
            points = list(reversed(cfg.led_points))

        if points:
            cfg.led_points = points
        state.apply_config()
        state.schedule_save()
        return {
            "status": "ok",
            "led_points": cfg.led_points,
            "leds_left": cfg.leds_left,
            "leds_top": cfg.leds_top,
            "leds_right": cfg.leds_right,
            "leds_bottom": cfg.leds_bottom,
        }

    @app.post("/api/zones")
    async def set_zones(req: ZonesRequest):
        """Per-edge LED counts, distance from the edge and zone size in one step."""
        total = req.leds_left + req.leds_top + req.leds_right + req.leds_bottom
        if total < 1 or total > MAX_LEDS:
            return JSONResponse(status_code=400, content={"error": f"El total de LEDs debe estar entre 1 y {MAX_LEDS}"})
        cfg = state.config
        cfg.leds_left, cfg.leds_top, cfg.leds_right, cfg.leds_bottom = req.leds_left, req.leds_top, req.leds_right, req.leds_bottom
        cfg.led_count = total
        cfg.edge_mode = "4_sides" if req.leds_bottom > 0 else "3_sides"
        cfg.edge_margin = req.margin
        cfg.sample_box_size = req.box_size
        cfg.led_points = generate_default_led_points(req.leds_left, req.leds_top, req.leds_right, req.leds_bottom,
                                                     cfg.clockwise, cfg.edge_margin)
        state.apply_config()
        state.schedule_save()
        return {"status": "ok", "led_count": total, "led_points": cfg.led_points, "zones_customized": False}

    @app.post("/api/set_edge_leds")
    async def set_edge_leds(req: EdgeLedsRequest):
        """Sets exact LED counts per edge and regenerates LED screen points for visual preview."""
        cfg = state.config
        l = cfg.leds_left if req.leds_left is None else req.leds_left
        t = cfg.leds_top if req.leds_top is None else req.leds_top
        r = cfg.leds_right if req.leds_right is None else req.leds_right
        b = cfg.leds_bottom if req.leds_bottom is None else req.leds_bottom

        total = l + t + r + b
        if total < 1 or total > MAX_LEDS:
            return JSONResponse(status_code=400, content={"error": f"El total de LEDs debe estar entre 1 y {MAX_LEDS}"})

        cfg.leds_left, cfg.leds_top, cfg.leds_right, cfg.leds_bottom = l, t, r, b
        cfg.led_count = total
        cfg.edge_mode = "4_sides" if b > 0 else "3_sides"
        cfg.led_points = generate_default_led_points(l, t, r, b, cfg.clockwise, cfg.edge_margin)
        state.apply_config()
        state.schedule_save()
        return {"status": "ok", "led_count": total, "led_points": cfg.led_points}

    @app.post("/api/preset")
    async def apply_preset(req: PresetRequest):
        """Applies the image presets (Modo Juego, Modo Película, Vívido, Blando)."""
        cfg = state.config
        cfg.sync_preset = req.preset
        for key, value in PRESET_VALUES[req.preset].items():
            setattr(cfg, key, value)
        state.apply_config()
        state.schedule_save()
        return {"status": "ok", "preset": req.preset}

    @app.post("/api/reset_defaults")
    async def reset_defaults():
        """Restores tuning and clears the current monitor's color calibration, preserving physical layout."""
        cfg = reset_tuning_defaults(state.config)
        state.active_effect = cfg.active_effect
        state.effect_speed = cfg.effect_speed
        state.static_color_rgb = list(cfg.static_color)
        state.apply_config()
        state.schedule_save()
        return {"status": "ok"}

    @app.post("/api/static_color")
    async def set_static_color(req: ColorRequest):
        state.static_color_rgb = [req.r, req.g, req.b]
        state.config.static_color = list(state.static_color_rgb)
        state.set_mode("static")
        state.config.current_mode = "static"
        state.send_static_color()
        state.schedule_save()
        return {"status": "ok"}

    @app.post("/api/effect")
    async def set_effect(req: EffectRequest):
        cfg = state.config
        state.active_effect = cfg.active_effect = req.effect
        state.effect_speed = cfg.effect_speed = req.speed
        if req.reset_colors:
            cfg.effect_palettes.pop(req.effect, None)
        elif req.colors is not None:
            cfg.effect_palettes[req.effect] = [list(c) for c in req.colors]    # each effect remembers its own colours
        if req.reverse is not None:
            cfg.effect_reverse = req.reverse
        if req.mirror is not None:
            cfg.effect_mirror = req.mirror
        state.switch_mode("effect")
        return {"status": "ok"}

    @app.post("/api/palette")
    async def set_palette(req: PaletteRequest):
        state.active_palette = req.palette
        state.config.rhythm_palette = req.palette
        state.audio_engine.set_palette(req.palette)
        state.schedule_save()
        return {"status": "ok"}

    @app.post("/api/calibrate_action")
    async def calibrate_action(req: CalibrateRequest):
        cfg = state.config
        action = req.action
        if action == "stop":
            state.set_mode(cfg.current_mode if cfg.current_mode != "calibrate" else "screen")
            return {"status": "ok"}

        state.set_mode("calibrate")  # also cancels any brightness test still running
        token = state._calibration_token

        if action in ("test_red", "test_green", "test_blue", "test_white"):
            rgb = {"test_red": (255, 0, 0), "test_green": (0, 255, 0),
                   "test_blue": (0, 0, 255), "test_white": (255, 255, 255)}[action]
            state.send_solid(*rgb)
        elif action == "test_channel":
            # one raw color channel at full power, with no wiring order applied: the wizard asks what color shows
            raw = [0, 0, 0]
            raw[min(2, req.index)] = 255
            state.driver.send_sync_screen([(1, raw[0], raw[1], raw[2], cfg.led_count)])
        elif action == "test_led":
            # exactly one LED white, all others dark: shows where the strip starts and which way it runs
            n, i = cfg.led_count, min(req.index, cfg.led_count - 1)
            white = state.color_pipeline.map_single_color(255, 255, 255)
            segs = ([(1, 0, 0, 0, i)] if i > 0 else []) + [(i + 1, white[0], white[1], white[2], i + 1)] \
                + ([(i + 2, 0, 0, 0, n)] if i + 1 < n else [])
            state.driver.send_sync_screen(segs)
        elif action == "test_edges":
            colors = [(255, 0, 0, cfg.leds_left), (0, 255, 0, cfg.leds_top),
                      (0, 0, 255, cfg.leds_right), (255, 255, 0, cfg.leds_bottom)]
            idx, segs = 1, []
            for r, g, b, count in colors:
                if count > 0:
                    c = state.color_pipeline.map_single_color(r, g, b)
                    segs.append((idx, int(c[0]), int(c[1]), int(c[2]), idx + count - 1))
                    idx += count
            state.driver.send_sync_screen(segs)
        elif action == "test_brightness_cycle":
            def run_cycle():
                for pct in (10, 25, 50, 75, 100):
                    if state._calibration_token != token:  # another action/mode took over
                        return
                    p = ColorPipeline(wire_map=cfg.wire_map, brightness=pct / 100.0, gamma=cfg.gamma)
                    c = p.map_single_color(255, 210, 160)
                    state.driver.send_sync_screen([(1, int(c[0]), int(c[1]), int(c[2]), cfg.led_count)])
                    time.sleep(1.2)

            threading.Thread(target=run_cycle, daemon=True, name="brightness-test").start()
        return {"status": "ok"}

    return app
