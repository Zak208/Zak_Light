"""
Automation on top of the engine: per-application rules, schedules (with a sunrise alarm) and idle detection.

The engine loop calls tick() every iteration; the work is throttled to about once a second. All inputs
(foreground window, clock, idle time, audio activity) are injectable so the logic is tested without a desktop.
"""

import datetime as dt
import logging
import time
from typing import Callable, Optional

from .profiles import PROFILE_FIELDS, ProfileError
from . import windows_info

log = logging.getLogger("zak_light")

TICK_SECONDS = 1.0
RULE_CONFIRM_TICKS = 2      # a rule must hold for this many ticks: alt-tabbing through a game must not flap the lights
SCHEDULE_LATE_SECONDS = 150  # a schedule that was missed by less than this (PC busy, just woke up) still fires
MODES = ("screen", "rhythm", "effect", "static")
AUDIO_PEAK_ACTIVE = 0.01


class Automation:
    def __init__(
        self,
        state,
        now: Callable[[], float] = time.time,
        foreground: Callable[[], Optional[str]] = windows_info.foreground_exe,
        idle: Callable[[], float] = windows_info.idle_seconds,
        audio_active: Optional[Callable[[], bool]] = None,
    ):
        self.state = state
        self._now = now
        self._foreground = foreground
        self._idle = idle
        self._audio_active = audio_active if audio_active is not None else self._default_audio_active
        self._last_tick = 0.0
        # application rules
        self.active_rule: Optional[dict] = None
        self._candidate = None
        self._candidate_ticks = 0
        self._baseline: Optional[dict] = None
        # schedules
        self._fired: set = set()
        self.last_mode_on = "screen"

    # ---- public ------------------------------------------------------------------------------

    def tick(self, force: bool = False):
        now = self._now()
        if not force and now - self._last_tick < TICK_SECONDS:
            return
        self._last_tick = now
        if self.state.mode in MODES:
            self.last_mode_on = self.state.mode      # what 'turn on' should bring back
        # Each step is protected on its own: a bug in one must not stop the others from running
        for step in (self._apply_rules, lambda: self._run_schedules(now), self._check_idle):
            try:
                step()
            except Exception:
                log.exception("Automation step failed")

    # ---- application rules -------------------------------------------------------------------

    def _apply_rules(self):
        cfg = self.state.config
        if not cfg.app_rules_enabled or not cfg.app_rules:
            if self.active_rule is not None:
                self._leave_rule()
            return
        exe = self._foreground()
        rule = next((r for r in cfg.app_rules if r["exe"] == exe), None) if exe else None
        current_key = rule["exe"] if rule else None
        if current_key == (self.active_rule["exe"] if self.active_rule else None):
            self._candidate, self._candidate_ticks = None, 0
            return
        if current_key != self._candidate:
            self._candidate, self._candidate_ticks = current_key, 1
            return
        self._candidate_ticks += 1
        if self._candidate_ticks < RULE_CONFIRM_TICKS:
            return
        self._candidate, self._candidate_ticks = None, 0
        if rule is None:
            self._leave_rule()
        else:
            self._enter_rule(rule)

    def _snapshot(self) -> dict:
        cfg = self.state.config
        return {"fields": {k: getattr(cfg, k) for k in PROFILE_FIELDS}, "mode": self.state.mode,
                "effect": self.state.active_effect, "speed": self.state.effect_speed,
                "static": list(self.state.static_color_rgb)}

    def _enter_rule(self, rule: dict):
        if self._baseline is None:
            self._baseline = self._snapshot()        # what to come back to when the application closes
        if rule["profile"]:
            try:
                self.state.profiles.apply(rule["profile"], self.state.config)
            except ProfileError:
                log.warning("App rule for %s: profile %r does not exist", rule["exe"], rule["profile"])
        cfg = self.state.config
        self.state.active_effect, self.state.effect_speed = cfg.active_effect, cfg.effect_speed
        self.state.static_color_rgb = list(cfg.static_color)
        self.state.apply_config()
        if rule["mode"]:
            self.state.set_mode(rule["mode"])
        self.active_rule = rule
        log.info("App rule: %s -> profile=%r mode=%r", rule["exe"], rule["profile"], rule["mode"])
        self._notify(f"{rule['exe']}: perfil “{rule['profile'] or 'actual'}”")

    def _leave_rule(self):
        base = self._baseline
        self.active_rule, self._baseline = None, None
        if base is None:
            return
        cfg = self.state.config
        for key, value in base["fields"].items():
            setattr(cfg, key, value)
        self.state.active_effect, self.state.effect_speed = base["effect"], base["speed"]
        self.state.static_color_rgb = base["static"]
        self.state.apply_config()
        self.state.set_mode(base["mode"])
        log.info("App rule ended: settings restored")

    # ---- schedules ---------------------------------------------------------------------------

    def _run_schedules(self, now: float):
        cfg = self.state.config
        if not cfg.schedules:
            return
        moment = dt.datetime.fromtimestamp(now)
        self._fired = {k for k in self._fired if k[1] == moment.date()}       # forget yesterday
        for index, item in enumerate(cfg.schedules):
            if moment.weekday() not in item["days"]:
                continue
            hour, minute = map(int, item["time"].split(":"))
            due = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
            late = (moment - due).total_seconds()
            key = (index, moment.date(), item["time"])
            if 0 <= late <= SCHEDULE_LATE_SECONDS and key not in self._fired:
                self._fired.add(key)
                self._do(item["action"], item.get("value", ""))

    def _do(self, action: str, value: str):
        state = self.state
        log.info("Schedule: %s %s", action, value)
        if action == "off":
            state.set_away(False)
            state.set_mode("off")
        elif action == "on":
            state.set_away(False)
            state.set_mode(self.last_mode_on if state.mode == "off" else state.mode)
        elif action == "mode" and value in MODES:
            state.set_away(False)
            state.set_mode(value)
        elif action == "profile":
            try:
                state.profiles.apply(value, state.config)
                state.active_effect, state.effect_speed = state.config.active_effect, state.config.effect_speed
                state.static_color_rgb = list(state.config.static_color)
                state.apply_config()
            except ProfileError:
                log.warning("Schedule: profile %r does not exist", value)
        elif action == "brightness":
            try:
                state.config.brightness = max(0.05, min(1.0, float(value) / 100.0))
                state.apply_config()
            except ValueError:
                pass
        elif action == "sunrise":
            try:
                minutes = max(1.0, min(120.0, float(value or 15)))
            except ValueError:
                minutes = 15.0
            state.effect_engine.sunrise_seconds = minutes * 60.0
            state.active_effect = "sunrise"
            state.effect_engine.restart("sunrise")
            state.set_away(False)
            state.set_mode("effect")
        state.schedule_save()

    # ---- idle --------------------------------------------------------------------------------

    def _default_audio_active(self) -> bool:
        try:
            return self.state.audio_engine._peak_meter().get_peak() > AUDIO_PEAK_ACTIVE
        except Exception:
            return False

    def _check_idle(self):
        minutes = self.state.config.idle_off_minutes
        if minutes <= 0 or self.state.mode == "off":
            if self.state.away:
                self.state.set_away(False)
            return
        idle = self._idle()
        if idle < minutes * 60:
            if self.state.away:
                self.state.set_away(False)
            return
        # Long without input: the lights go off... unless something is still playing (a film, music)
        if self._audio_active():
            if self.state.away:
                self.state.set_away(False)
            return
        if not self.state.away:
            self.state.set_away(True)

    # ---- notifications -----------------------------------------------------------------------

    def _notify(self, text: str):
        notify = getattr(self.state, "notify", None)
        if notify and self.state.config.notifications:
            try:
                notify("Zak_light", text)
            except Exception:
                log.exception("Notification failed")
