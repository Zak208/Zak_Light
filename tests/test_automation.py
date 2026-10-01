import datetime as dt

import pytest

from core.automation import Automation, RULE_CONFIRM_TICKS
from core.config import AppConfig


def ts(day, hh, mm, ss=0):
    """Epoch seconds for 2026-10-<day> hh:mm:ss (2026-10-05 is a Monday)."""
    return dt.datetime(2026, 10, day, hh, mm, ss).timestamp()


class Env:
    """Controllable world for the automation: clock, foreground app, idle time, audio."""
    def __init__(self):
        self.now = ts(5, 12, 0)
        self.fg = None
        self.idle = 0.0
        self.audio = False


@pytest.fixture
def env():
    return Env()


@pytest.fixture
def auto(state, env):
    return Automation(state, now=lambda: env.now, foreground=lambda: env.fg, idle=lambda: env.idle,
                      audio_active=lambda: env.audio)


def step(auto, env, seconds=1.0, times=1):
    for _ in range(times):
        env.now += seconds
        auto.tick()


# ---- schedules ---------------------------------------------------------------------------------

def test_schedule_fires_once_at_its_time(state, auto, env):
    state.config.schedules = [{"time": "22:30", "days": [0, 1, 2, 3, 4, 5, 6], "action": "off", "value": ""}]
    env.now = ts(5, 22, 29, 50)
    step(auto, env, 1, 5)
    assert state.mode != "off"                                     # not yet
    step(auto, env, 1, 15)
    assert state.mode == "off"
    state.set_mode("screen")                                        # the user turns it back on...
    step(auto, env, 1, 30)
    assert state.mode == "screen"                                   # ...and it does not fire a second time that day


def test_schedule_fires_again_the_next_day(state, auto, env):
    state.config.schedules = [{"time": "07:00", "days": [0, 1, 2, 3, 4, 5, 6], "action": "mode", "value": "effect"}]
    env.now = ts(5, 7, 0, 5)
    auto.tick(force=True)
    assert state.mode == "effect"
    state.set_mode("screen")
    env.now = ts(6, 7, 0, 5)
    auto.tick(force=True)
    assert state.mode == "effect"


def test_schedule_respects_weekdays(state, auto, env):
    state.config.schedules = [{"time": "09:00", "days": [5, 6], "action": "off", "value": ""}]   # weekend only
    env.now = ts(5, 9, 0, 5)                                        # a Monday
    auto.tick(force=True)
    assert state.mode != "off"
    env.now = ts(10, 9, 0, 5)                                       # a Saturday
    auto.tick(force=True)
    assert state.mode == "off"


def test_a_slightly_late_schedule_still_fires_but_a_very_late_one_does_not(state, auto, env):
    state.config.schedules = [{"time": "20:00", "days": [0], "action": "off", "value": ""}]
    env.now = ts(5, 20, 1, 30)                                      # PC busy / just woke up: 90 s late
    auto.tick(force=True)
    assert state.mode == "off"
    state.set_mode("screen")
    env.now = ts(12, 20, 30)                                        # half an hour late: missed
    auto.tick(force=True)
    assert state.mode == "screen"


def test_turn_on_returns_to_the_last_mode_used(state, auto, env):
    state.config.schedules = [{"time": "08:00", "days": [0], "action": "on", "value": ""}]
    state.set_mode("effect")
    auto.tick(force=True)
    state.set_mode("off")
    env.now = ts(5, 8, 0, 3)
    auto.tick(force=True)
    assert state.mode == "effect"


def test_sunrise_alarm_starts_the_effect_with_its_duration(state, auto, env):
    state.config.schedules = [{"time": "06:45", "days": [0], "action": "sunrise", "value": "20"}]
    state.set_mode("off")
    env.now = ts(5, 6, 45, 2)
    auto.tick(force=True)
    assert state.mode == "effect" and state.active_effect == "sunrise"
    assert state.effect_engine.sunrise_seconds == 20 * 60


def test_brightness_and_profile_actions(state, auto, env):
    state.config.brightness = 0.9
    state.config.schedules = [{"time": "23:00", "days": [0], "action": "brightness", "value": "30"},
                              {"time": "23:05", "days": [0], "action": "profile", "value": "Noche"}]
    state.config.dithering = False
    from core.config import AppConfig as C
    night = C(brightness=0.2, rhythm_palette="sunset")
    state.profiles.save("Noche", night)
    env.now = ts(5, 23, 0, 1)
    auto.tick(force=True)
    assert state.config.brightness == pytest.approx(0.3) and state.color_pipeline.brightness == pytest.approx(0.3)
    env.now = ts(5, 23, 5, 1)
    auto.tick(force=True)
    assert state.config.brightness == pytest.approx(0.2) and state.config.rhythm_palette == "sunset"


def test_unknown_profile_in_a_schedule_does_not_crash(state, auto, env):
    state.config.schedules = [{"time": "10:00", "days": [0], "action": "profile", "value": "Nope"}]
    env.now = ts(5, 10, 0, 1)
    auto.tick(force=True)                                           # logged, nothing else happens
    assert state.mode == "screen"


# ---- application rules ---------------------------------------------------------------------------

def setup_rules(state):
    state.profiles.save("Juego", AppConfig(brightness=1.0, smoothing=0.8, sync_preset="game"))
    state.config.app_rules_enabled = True
    state.config.app_rules = [{"exe": "game.exe", "profile": "Juego", "mode": "screen"},
                              {"exe": "player.exe", "profile": "", "mode": "effect"}]


def test_rule_applies_its_profile_and_mode_after_a_short_confirmation(state, auto, env):
    setup_rules(state)
    state.config.brightness, state.config.smoothing = 0.4, 0.3
    state.set_mode("static")
    env.fg = "game.exe"
    step(auto, env, 1.0, RULE_CONFIRM_TICKS - 1)
    assert state.mode == "static"                                   # one tick is not enough: alt-tab must not flap
    step(auto, env, 1.0, 2)
    assert state.mode == "screen" and state.config.smoothing == 0.8 and state.config.brightness == 1.0
    assert state.color_pipeline.smoothing == 0.8                    # and it reached the running pipeline


def test_leaving_the_application_restores_everything(state, auto, env):
    setup_rules(state)
    state.config.brightness, state.config.smoothing = 0.4, 0.3
    state.set_mode("static")
    env.fg = "game.exe"
    step(auto, env, 1.0, 4)
    assert state.mode == "screen"
    env.fg = "notepad.exe"
    step(auto, env, 1.0, 4)
    assert state.mode == "static" and state.config.brightness == 0.4 and state.config.smoothing == 0.3


def test_alt_tab_flicker_does_not_trigger_rules(state, auto, env):
    setup_rules(state)
    state.set_mode("static")
    for fg in ["game.exe", "chrome.exe", "game.exe", "chrome.exe", "game.exe", "chrome.exe"]:
        env.fg = fg
        step(auto, env, 1.0, 1)
    assert state.mode == "static"


def test_switching_between_two_ruled_apps_keeps_the_original_baseline(state, auto, env):
    setup_rules(state)
    state.config.brightness = 0.4
    state.set_mode("static")
    env.fg = "game.exe"
    step(auto, env, 1.0, 4)
    env.fg = "player.exe"
    step(auto, env, 1.0, 4)
    assert state.mode == "effect"
    env.fg = None
    step(auto, env, 1.0, 4)
    assert state.mode == "static" and state.config.brightness == 0.4   # not the game's profile


def test_rules_do_nothing_when_disabled_and_never_crash_on_a_missing_profile(state, auto, env):
    setup_rules(state)
    state.config.app_rules_enabled = False
    env.fg = "game.exe"
    step(auto, env, 1.0, 5)
    assert state.mode == "screen" and state.config.smoothing != 0.8     # nothing was applied
    state.config.app_rules_enabled = True
    state.config.app_rules = [{"exe": "game.exe", "profile": "Borrado", "mode": "effect"}]
    step(auto, env, 1.0, 5)
    assert state.mode == "effect"                                   # mode still applied; the missing profile is skipped


# ---- idle --------------------------------------------------------------------------------------------------

def test_idle_turns_the_strip_dark_and_activity_brings_it_back(state, auto, env):
    state.config.idle_off_minutes = 10
    env.idle = 9 * 60
    step(auto, env, 1.0, 2)
    assert not state.away
    env.idle = 10 * 60 + 5
    step(auto, env, 1.0, 2)
    assert state.away
    env.idle = 2
    step(auto, env, 1.0, 2)
    assert not state.away


def test_sound_keeps_the_lights_on_even_when_idle(state, auto, env):
    state.config.idle_off_minutes = 5
    env.idle, env.audio = 3600, True                                # watching a film: no keyboard, but sound
    step(auto, env, 1.0, 3)
    assert not state.away
    env.audio = False
    step(auto, env, 1.0, 3)
    assert state.away


def test_idle_is_off_by_default_and_ignored_while_the_strip_is_off(state, auto, env):
    env.idle = 99999
    step(auto, env, 1.0, 3)
    assert not state.away                                           # setting is 0 = never
    state.config.idle_off_minutes = 1
    state.set_mode("off")
    step(auto, env, 1.0, 3)
    assert not state.away


def test_tick_is_throttled(state, auto, env):
    calls = []
    auto._check_idle = lambda: calls.append(1)
    auto._last_tick = env.now
    for _ in range(50):
        auto.tick()                                                 # the clock does not move: nothing happens
    assert calls == []
    env.now += 1.5
    auto.tick()
    assert calls == [1]


def test_a_failing_step_does_not_stop_the_automation(state, auto, env):
    def boom():
        raise RuntimeError("x")
    auto._apply_rules = boom
    state.config.idle_off_minutes = 1
    env.idle = 9999
    step(auto, env, 1.0, 2)
    assert state.away                                               # idle still worked after the rules step failed
