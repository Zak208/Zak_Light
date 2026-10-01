"""
Zak_light engine: shared state and the rendering loop.

Kept free of any GUI/web code so it can be tested with a simulated driver (see tests/).

Threads: the engine loop (this file), a capture worker while the screen mode runs, the API server, and
small helpers (power events, hotkeys). The loop never calls anything that can block on Windows graphics:
screen capture lives in ScreenWorker, so a capture that hangs (lost desktop access: UAC prompt, lock screen,
exclusive full-screen game, GPU reset...) can at worst leave the picture stale, never freeze the lights.
"""

import logging
import os
import threading
import time
from typing import Optional

import numpy as np

from core.audio_visualizer import AudioReactiveEngine
from core.color import ColorPipeline
from core.config import AppConfig, load_config, save_config
from core.driver import DxLightDriver
from core.effects import EffectEngine
from core.paths import data_dir
from core.performance import PerformanceRecorder
from core.profiles import ProfileStore
from core.capture_proxy import RemoteSampler
from core.screen_capture import ScreenSampler
from core.screen_worker import ScreenWorker
from core.zones import apply_screen_style
from core import system as winsys

log = logging.getLogger("zak_light")

EFFECT_MAX_FPS = 30       # slow animations do not need more
STILL_AFTER_FRAMES = 40   # about 1 s of an unchanged screen before the engine "coasts"
COAST_INTERVAL = 0.1      # polling period while coasting (seconds)
STATIC_KEEPALIVE = 10.0   # static/off modes re-send every so often, so a device that reset is noticed
OFF_KEEPALIVE = 30.0
START_GRACE = 10.0         # extra time before a capture process that is still starting counts as stalled
STALL_AFTER = 1.5         # seconds without a new capture result before the capture counts as stalled
FADE_SECONDS = 0.4        # cross-fade between modes / when turning on and off
CALM_ACTIVITY = 1.2       # mean per-LED change (0-255) below which the picture counts as calm
CALM_AFTER_FRAMES = 60    # ...for this many frames before the frame rate is lowered
CALM_FPS_SCALE = 0.5
MODES_CYCLE = ("screen", "rhythm", "effect", "static")


class Crossfade:
    """Blends the first frames of a new mode with what was on the strip before (smoothstep)."""

    def __init__(self):
        self.source = None
        self.started = 0.0
        self.duration = 0.0
        self.active = False

    def start(self, source, duration: float):
        self.source = None if source is None else np.asarray(source, dtype=np.float32)
        self.started = time.monotonic()
        self.duration = duration
        self.active = duration > 0

    def apply(self, frame: np.ndarray) -> np.ndarray:
        if not self.active:
            return frame
        t = (time.monotonic() - self.started) / self.duration
        if t >= 1.0:
            self.active = False
            return frame
        src = self.source if (self.source is not None and self.source.shape == frame.shape) else 0.0
        t = t * t * (3.0 - 2.0 * t)
        return np.rint(src * (1.0 - t) + frame.astype(np.float32) * t).astype(np.uint8)


class EngineState:
    """Everything the rendering loop and the API share."""

    def __init__(self, config: Optional[AppConfig] = None, driver=None, screen_sampler=None, audio_engine=None,
                 profiles: Optional[ProfileStore] = None):
        self.config: AppConfig = config if config is not None else load_config()
        # Named profiles remain portable, while this calibration is tied to the physical monitor.
        # Restore it only for the active runtime config, never while validating a named profile.
        self.config.restore_monitor_color_profile()
        cfg = self.config
        self.driver = driver if driver is not None else DxLightDriver()
        self.color_pipeline = ColorPipeline(wire_map=cfg.wire_map, gamma=cfg.gamma, brightness=cfg.brightness,
                                            saturation=cfg.saturation, min_luminance_floor=cfg.min_luminance_floor,
                                            smoothing=cfg.smoothing, hdr_compensation=cfg.hdr_compensation)
        if screen_sampler is None:
            # Capture runs in a restartable helper process unless told otherwise (debugging, benchmarks)
            sampler_class = ScreenSampler if os.environ.get("ZAK_INPROC_CAPTURE") else RemoteSampler
            screen_sampler = sampler_class(monitor_idx=cfg.monitor_index, detect_black_bars=cfg.black_bar_detection)
        self.screen_sampler = screen_sampler
        self.audio_engine = audio_engine if audio_engine is not None else AudioReactiveEngine(
            led_count=cfg.led_count,
            palette_name=cfg.rhythm_palette,
            sensitivity=cfg.audio_sensitivity,
            rhythm_type="software",
            audio_device_id=cfg.audio_device_id,
        )
        self.audio_engine.pattern_idx = cfg.rhythm_pattern
        self.effect_engine = EffectEngine(led_count=cfg.led_count)
        self.effect_engine.set_layout(cfg.led_points)
        self.profiles = profiles if profiles is not None else ProfileStore(os.path.join(data_dir(), "profiles.json"))
        self.worker = ScreenWorker(self)
        self.performance_recorder = PerformanceRecorder()
        self._helper_cpu_prev = (0, 0.0)
        self.xfade = Crossfade()
        self.automation = None          # set by the application (app rules, schedules, idle)

        self.mode = cfg.current_mode
        self.last_on_mode = cfg.current_mode if cfg.current_mode in MODES_CYCLE else "screen"
        self.notify = None              # callable(title, text) set by the tray
        self.is_running = True
        self.suspended = False          # session locked / display off / system asleep: LEDs stay dark
        self.away = False               # no activity for a while (idle setting): LEDs stay dark too
        self.capture_stalled = False
        self.active_effect = cfg.active_effect
        self.active_palette = cfg.rhythm_palette
        self.effect_speed = cfg.effect_speed
        self.static_color_rgb = list(cfg.static_color)
        self.screen_points = np.empty((0, 2), dtype=np.float32)
        self.preview_revision = 0

        self.frame_ms = 0.0             # smoothed capture -> USB time per frame (screen mode)
        self.latency_ms = 0.0           # smoothed age of the picture at the moment it is sent
        self.fps_scale = 1.0            # 1.0 normally, lower while the picture is calm
        self._cpu_prev = (time.perf_counter(), time.process_time())
        self.started_at = time.monotonic()
        self.wake = threading.Event()   # set to interrupt the engine loop's idle waits
        self._save_lock = threading.Lock()
        self._save_timer: Optional[threading.Timer] = None
        self._calibration_token = 0     # bumping it cancels a running brightness test
        self.stage = "starting"         # what the engine loop is doing right now (diagnostics)
        self.stage_since = time.monotonic()
        self.loop_ticks = 0
        self.apply_config()

    # ---- modes -------------------------------------------------------------------------------

    @property
    def dark(self) -> bool:
        """The strip must stay dark (locked session, display off, suspend, or idle)."""
        return self.suspended or self.away

    def set_mode(self, mode: str):
        self.mode = mode
        if mode in MODES_CYCLE:
            self.last_on_mode = mode
        self._calibration_token += 1    # any running calibration sequence is now stale
        self.mark_preview_dirty()
        self.wake.set()

    def switch_mode(self, mode: str):
        """set_mode + remember it as the mode to start in (tray, hotkeys and the API all use this)."""
        self.set_mode(mode)
        if mode != "calibrate":
            self.config.current_mode = mode
        self.schedule_save()

    def toggle_power(self):
        self.switch_mode(self.last_on_mode if self.mode == "off" else "off")

    def cycle_mode(self, step: int = 1):
        """Next (or previous) of Pantalla / Ritmo / Efectos / Color; from 'off' it turns on in the last mode."""
        if self.mode == "off":
            self.switch_mode(self.last_on_mode)
            return
        index = MODES_CYCLE.index(self.mode) if self.mode in MODES_CYCLE else 0
        self.switch_mode(MODES_CYCLE[(index + step) % len(MODES_CYCLE)])

    def apply_profile(self, name: str):
        """Loads a saved profile into the running engine (raises ProfileError if it does not exist)."""
        self.profiles.apply(name, self.config)
        cfg = self.config
        self.active_effect, self.effect_speed = cfg.active_effect, cfg.effect_speed
        self.static_color_rgb = list(cfg.static_color)
        self.apply_config()
        if self.mode == "static":
            self.send_static_color()
        self.schedule_save()

    def nudge_brightness(self, delta: float):
        self.config.brightness = max(0.05, min(1.0, round(self.config.brightness + delta, 3)))
        self.apply_config()
        if self.mode == "static":
            self.send_static_color()
        self.schedule_save()

    def mark(self, stage: str):
        self.stage = stage
        self.stage_since = time.monotonic()

    def health(self) -> dict:
        """How the engine loop is doing: a capture call that never returns shows up as capture_stalled."""
        now = time.monotonic()
        return {
            "uptime_s": round(now - self.started_at, 1),
            "stage": self.stage,
            "stage_age_s": round(now - self.stage_since, 1),
            "loop_ticks": self.loop_ticks,
            "usb_writes": getattr(self.driver, "writes_ok", 0),
            "usb_write_age_s": round(now - self.driver.last_write, 1) if getattr(self.driver, "last_write", 0) else None,
            "usb_failures": getattr(self.driver, "writes_failed", 0),
            "usb_writes_per_min": round(
                getattr(self.driver, "writes_ok", 0) * 60.0 / max(now - self.started_at, 1.0), 1
            ),
            "capture_stalled": self.capture_stalled,
            "latency_ms": round(self.latency_ms, 1),
            "fps_scale": self.fps_scale,
            "capture": self.worker.health(),
        }

    def start_performance_measurement(self, scenario: str) -> bool:
        return self.performance_recorder.start(self, scenario)

    def stop_performance_measurement(self, reason: str = "user") -> dict | None:
        return self.performance_recorder.stop(self, reason)

    def performance_measurement_status(self) -> dict:
        return self.performance_recorder.status()

    def mark_preview_dirty(self):
        """Bump the revision used by the local UI to avoid serialising unchanged LED frames."""
        self.preview_revision += 1

    def preview_known(self) -> bool:
        """Whether the current mode has a meaningful LED frame for the live preview."""
        return self.mode != "calibrate"

    def preview_colors(self):
        """
        The colors the strip is showing right now as [[r, g, b], ...], or None when they are not known
        (calibration tests). Dark while off or while the strip is kept dark.
        """
        n = len(self.config.led_points) or self.config.led_count
        if self.dark or self.mode == "off":
            return [[0, 0, 0]] * n
        if self.mode == "static":
            scale = self.config.brightness
            return [[int(round(c * scale)) for c in self.static_color_rgb]] * n
        if self.mode == "calibrate":
            return None
        frame = self.color_pipeline.preview
        return frame.tolist() if frame is not None else [[0, 0, 0]] * n

    def set_suspended(self, suspended: bool):
        if suspended != self.suspended:
            log.info("Session/display %s: LEDs %s", "inactive" if suspended else "active",
                     "off" if suspended else "restored")
        self.suspended = suspended
        self.mark_preview_dirty()
        self.wake.set()

    def set_away(self, away: bool):
        if away != self.away:
            log.info("No activity: LEDs %s", "off" if away else "restored")
        self.away = away
        self.mark_preview_dirty()
        self.wake.set()

    # ---- settings ----------------------------------------------------------------------------

    def apply_config(self):
        """Pushes the values in self.config into the running components."""
        cfg = self.config
        # Settings change rarely, whereas capture runs up to 60 times per second. Keep a
        # contiguous typed copy of the user-facing point list until configuration changes.
        self.screen_points = np.ascontiguousarray(np.asarray(cfg.led_points, dtype=np.float32))
        self.color_pipeline.update_settings(
            wire_map=cfg.wire_map,
            gamma=cfg.gamma,
            brightness=cfg.brightness,
            saturation=cfg.saturation,
            smoothing=cfg.smoothing,
            min_floor=cfg.min_luminance_floor,
            white_balance=cfg.white_balance,
            max_power=cfg.max_power,
            dark_light=cfg.dark_light,
            dithering=cfg.dithering,
            adaptive_smoothing=cfg.adaptive_smoothing,
            hdr_compensation=cfg.hdr_compensation,
        )
        self.screen_sampler.detect_black_bars = cfg.black_bar_detection
        if cfg.monitor_index != self.screen_sampler.monitor_idx:  # only when it really changed
            self.screen_sampler.switch_monitor(cfg.monitor_index)

        self.audio_engine.led_count = cfg.led_count
        self.audio_engine.sensitivity = cfg.audio_sensitivity
        self.audio_engine.pattern_idx = cfg.rhythm_pattern
        self.audio_engine.set_palette(cfg.rhythm_palette)
        if hasattr(self.audio_engine, "set_audio_device"):
            self.audio_engine.set_audio_device(cfg.audio_device_id)
        self.active_palette = cfg.rhythm_palette
        self.effect_engine.led_count = cfg.led_count
        self.effect_engine.set_layout(cfg.led_points)
        self.mark_preview_dirty()
        self.wake.set()  # idle modes (static/off) re-render right away

    def display_changed(self):
        """The monitor setup changed (resolution, monitor plugged/unplugged): capture must start from scratch."""
        restart = getattr(self.screen_sampler, "restart", None)
        if restart:
            restart("display change")
        self.wake.set()

    def metrics(self) -> dict:
        """Real resource usage of this process (CPU as % of the whole machine, like Task Manager)."""
        now, cpu = time.perf_counter(), time.process_time()
        prev_now, prev_cpu = self._cpu_prev
        self._cpu_prev = (now, cpu)
        elapsed = max(now - prev_now, 1e-6)
        cores = os.cpu_count() or 1
        engine_pct = (cpu - prev_cpu) / elapsed / cores * 100.0
        engine_ram = winsys.process_ram_mb()

        # The capture helper does the heavy work (DXGI, strips); counting only this process would hide it
        helper_pid = getattr(self.screen_sampler, "helper_status", lambda: {})().get("helper_pid")
        helper_pct, helper_ram = 0.0, 0.0
        stats = winsys.other_process_stats(helper_pid) if helper_pid else None
        if stats:
            prev_pid, prev_hcpu = self._helper_cpu_prev
            if prev_pid == helper_pid:
                helper_pct = max(0.0, stats[0] - prev_hcpu) / elapsed / cores * 100.0
            self._helper_cpu_prev = (helper_pid, stats[0])
            helper_ram = stats[1]
        return {
            "ram_mb": round(engine_ram + helper_ram, 1),
            "cpu_pct": round(engine_pct + helper_pct, 1),
            "engine_ram_mb": round(engine_ram, 1),
            "capture_ram_mb": round(helper_ram, 1),
            "capture_cpu_pct": round(helper_pct, 1),
            "frame_ms": round(self.frame_ms, 1),
        }

    def schedule_save(self):
        """Debounced config write: a slider drag fires dozens of updates, so save once it settles."""
        with self._save_lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.5, self.flush_save)
            self._save_timer.daemon = True
            self._save_timer.start()

    def flush_save(self):
        with self._save_lock:
            if self._save_timer:
                self._save_timer.cancel()
                self._save_timer = None
        save_config(self.config)  # serialised and atomic inside save_config

    # ---- LED helpers used by the API and the loop --------------------------------------------

    def solid_frame(self, n: Optional[int] = None) -> np.ndarray:
        """The static color as an (N, 3) frame in the strip's channel order."""
        r, g, b = self.static_color_rgb
        mapped = self.color_pipeline.map_single_color(r, g, b)
        return np.tile(np.array(mapped, dtype=np.uint8), (n or self.config.led_count, 1))

    def send_solid(self, r: int, g: int, b: int):
        """Paints every LED one color (brightness/gamma/wiring applied)."""
        mapped = self.color_pipeline.map_single_color(r, g, b)
        ok = self.driver.send_sync_screen([(1, mapped[0], mapped[1], mapped[2], self.config.led_count)])
        self.driver.last_frame = np.tile(np.array(mapped, dtype=np.uint8), (self.config.led_count, 1))
        self.mark_preview_dirty()
        return ok

    def send_static_color(self):
        r, g, b = self.static_color_rgb
        return self.send_solid(r, g, b)

    def idle_wait(self, seconds: float):
        """Sleeps until the timeout or until something (mode change, quit) wakes the loop."""
        self.wake.wait(seconds)
        self.wake.clear()

    # ---- fades ---------------------------------------------------------------------------------

    def fade_to(self, target: np.ndarray, duration: float = FADE_SECONDS, steps: int = 8):
        """Smoothly moves the strip from what it shows now to `target` (an (N, 3) frame)."""
        start = getattr(self.driver, "last_frame", None)
        if start is None or start.shape != target.shape:
            start = np.zeros_like(target)
        start = start.astype(np.float32)
        for i in range(1, steps + 1):
            if not self.is_running:
                return
            t = i / steps
            t = t * t * (3.0 - 2.0 * t)
            self.driver.send_colors(np.rint(start * (1.0 - t) + target.astype(np.float32) * t).astype(np.uint8))
            self.mark_preview_dirty()
            time.sleep(duration / steps)

    def fade_to_dark(self, duration: float = FADE_SECONDS):
        last = getattr(self.driver, "last_frame", None)
        if last is not None and self.config.transitions:
            self.fade_to(np.zeros_like(last), duration)
        self.driver.turn_off()
        self.mark_preview_dirty()


def _init_com():
    """The audio fallback (pycaw peak meter) needs COM on the thread that calls it."""
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def run_engine(state: EngineState):
    """Main rendering loop executing the active mode. Runs until state.is_running is cleared."""
    _init_com()
    last_mode = None
    last_error = None
    last_t = time.perf_counter()
    last_keepalive = 0.0
    still = 0               # consecutive screen iterations without a new frame
    calm = 0                # consecutive screen iterations with an (almost) unchanged picture
    last_frame_id = -1
    last_array = None
    was_dark = False
    audio_on = False

    while state.is_running:
        t0 = time.perf_counter()
        dt = min(0.25, max(0.001, t0 - last_t))
        last_t = t0
        state.loop_ticks += 1
        try:
            state.performance_recorder.record(state)
            state.mark("mode")
            mode = state.mode
            cfg = state.config
            fps = max(15.0, min(60.0, cfg.target_fps * state.fps_scale))
            if mode == "effect":
                fps = min(fps, EFFECT_MAX_FPS)

            if state.automation is not None:
                state.automation.tick()

            # Locked session / display off / suspended / idle: keep the LEDs dark until it is over
            if state.dark:
                if not was_dark:
                    was_dark = True
                    state.worker.stop()
                    state.audio_engine.suspend()
                    audio_on = False
                    state.fade_to_dark()
                state.idle_wait(1.0)
                continue
            if was_dark:
                was_dark = False
                last_mode = None  # re-apply the current mode

            if not state.driver.is_connected:
                if not state.driver.connect():
                    state.idle_wait(1.0)
                    continue
            if state.driver.consume_needs_init():
                last_mode = None  # (re)connected: bring the strip back to the current mode

            if mode != last_mode:
                previous = state.driver.last_frame
                last_mode = mode
                state.color_pipeline.reset_smoothing()
                state.driver.invalidate()
                still, calm, last_frame_id, last_array = 0, 0, -1, None
                state.fps_scale = 1.0
                last_keepalive = time.monotonic()
                if mode == "screen":
                    state.worker.start()
                else:
                    state.worker.stop()
                    state.capture_stalled = False
                if mode == "effect":
                    state.effect_engine.restart(state.active_effect)
                fade = FADE_SECONDS if cfg.transitions else 0.0
                if mode == "off":
                    state.fade_to_dark(fade)
                elif mode == "static":
                    if fade:
                        state.fade_to(state.solid_frame(len(previous) if previous is not None else None), fade)
                    state.send_static_color()
                else:
                    state.xfade.start(previous, fade)

            want_audio = mode == "rhythm" or (mode == "screen" and cfg.audio_boost > 0)
            if want_audio != audio_on:
                audio_on = want_audio
                (state.audio_engine.activate if audio_on else state.audio_engine.suspend)()

            # Nothing to render in these modes: sleep until something changes instead of polling
            if mode in ("off", "calibrate", "static"):
                state.mark("idle")
                state.idle_wait(2.0)
                now = time.monotonic()
                if mode != "calibrate" and now - last_keepalive >= (OFF_KEEPALIVE if mode == "off" else STATIC_KEEPALIVE):
                    last_keepalive = now
                    if mode == "static":
                        state.send_static_color()
                    else:
                        state.driver.turn_off()
                continue

            def emit(colors, mode=mode):
                if state.mode == mode:
                    state.driver.send_colors(state.xfade.apply(colors))
                    state.mark_preview_dirty()

            if mode == "screen":
                # A capture that is already blocked is not replaced until it returns: two
                # simultaneous calls into dxcam can corrupt its internal staging surfaces.
                if not state.worker.running:
                    state.worker.start()
                state.mark("capture")
                raw, frame_id, stamp = state.worker.snapshot()
                now = time.monotonic()
                # A capture process that is still starting (opening DXGI takes a couple of seconds) is not a stall
                warming = getattr(state.screen_sampler, "helper_status", lambda: {})().get("starting", False)
                stalled = (now - max(stamp, state.worker.started_at)) > (STALL_AFTER + (START_GRACE if warming else 0.0))
                if stalled != state.capture_stalled:
                    state.capture_stalled = stalled
                    log.warning("Screen capture %s", "stalled (desktop access lost?): keeping the last colors" if stalled else "recovered")
                state.mark("process")
                if raw is None or stalled:
                    if stalled and last_array is not None and now - last_keepalive >= 1.0:
                        last_keepalive = now
                        state.driver.send_colors(last_array)
                    state.idle_wait(0.2 if stalled else 0.03)
                    continue

                if frame_id == last_frame_id and state.color_pipeline.settled and last_array is not None:
                    still += 1
                else:
                    still, last_frame_id = 0, frame_id

                if still >= STILL_AFTER_FRAMES:
                    # The picture has not changed for a while and the smoothing has converged:
                    # coast at a low rate and only re-send the last frame now and then
                    if now - last_keepalive >= 1.0:
                        last_keepalive = now
                        state.driver.send_colors(last_array)
                    state.idle_wait(COAST_INTERVAL)
                    continue

                styled = apply_screen_style(raw, state.screen_points, cfg.screen_style, cfg.edges_enabled)
                if cfg.audio_boost > 0:  # bass pulse on top of the picture
                    styled = np.minimum(styled * (1.0 + cfg.audio_boost * 0.9 * state.audio_engine.pulse(dt)), 255.0)
                last_array = state.color_pipeline.process_colors_array(styled, dt=dt)
                emit(last_array)
                state.frame_ms += 0.1 * ((time.perf_counter() - t0) * 1000.0 - state.frame_ms)
                state.latency_ms += 0.1 * ((time.monotonic() - stamp) * 1000.0 - state.latency_ms)

                # A calm picture does not need 40 updates a second: halve the rate, restore it at once on change
                calm = calm + 1 if state.color_pipeline.activity < CALM_ACTIVITY else 0
                state.fps_scale = CALM_FPS_SCALE if (calm >= CALM_AFTER_FRAMES and cfg.audio_boost <= 0) else 1.0

            elif mode == "rhythm":
                # apply_gamma=False preserves the vibrant, true colors of the active palette
                colors = state.audio_engine.get_frame_colors()
                emit(state.color_pipeline.process_colors_array(colors, apply_gamma=False, dt=dt))

            elif mode == "effect":
                raw = state.effect_engine.render(
                    state.active_effect, state.effect_speed, cfg.palette_for(state.active_effect), cfg.effect_reverse, cfg.effect_mirror
                )
                # led_gamma: dim parts must look as dim on the LEDs as in the preview, and mixed colours (a light
                # red) must not wash out towards pink/white, which they do on LEDs driven linearly
                emit(state.color_pipeline.process_colors_array(raw, apply_gamma=False, dt=dt, led_gamma=True))

            last_error = None

        except Exception as e:
            if str(e) != last_error:  # log each distinct error once, not every frame
                last_error = str(e)
                log.exception("Error in engine loop (mode=%s)", state.mode)
            time.sleep(0.2)
            continue

        delay = 1.0 / fps - (time.perf_counter() - t0)
        if delay > 0:
            time.sleep(delay)

    # Cleanup
    try:
        state.stop_performance_measurement("engine_stopped")
        state.worker.stop()
        state.audio_engine.suspend()
        state.driver.turn_off()
        state.driver.disconnect()
    except Exception:
        log.exception("Error while shutting the engine down")
