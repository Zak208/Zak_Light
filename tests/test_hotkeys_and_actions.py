import ctypes
import time


from core import hotkeys
from core.hotkeys import HotkeyManager, MOD_ALT, MOD_CONTROL, MOD_SHIFT, WM_HOTKEY

# Obscure combinations (Ctrl+Alt+Shift+F13..F17) so the tests never grab a shortcut somebody really uses
VK_F13 = 0x7C
TEST_BINDINGS = {name: (MOD_CONTROL | MOD_ALT | MOD_SHIFT, VK_F13 + i, name) for i, name in
                 enumerate(["power", "next_mode", "prev_mode", "brightness_up", "brightness_down"])}


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_registered_hotkeys_call_their_action_and_are_released_on_stop():
    called = []
    mgr = HotkeyManager({"power": lambda: called.append("power"), "next_mode": lambda: called.append("next")}, TEST_BINDINGS)
    assert mgr.start()
    try:
        assert len(mgr.registered) == 2 and not mgr.failed                # only the actions we provided are bound
        ids = {v: k for k, v in mgr.registered.items()}
        ctypes.windll.user32.PostThreadMessageW(mgr._thread_id, WM_HOTKEY, ids["power"], 0)
        ctypes.windll.user32.PostThreadMessageW(mgr._thread_id, WM_HOTKEY, ids["next_mode"], 0)
        assert wait_for(lambda: called == ["power", "next"])
    finally:
        mgr.stop()
    # released: a second manager can take the same combinations
    again = HotkeyManager({"power": lambda: None}, TEST_BINDINGS)
    assert again.start() and again.registered and not again.failed
    again.stop()


def test_a_combination_already_in_use_is_reported_not_raised():
    first = HotkeyManager({"power": lambda: None}, TEST_BINDINGS)
    second = HotkeyManager({"power": lambda: None}, TEST_BINDINGS)
    assert first.start()
    try:
        assert second.start()
        assert second.failed == ["power"] and not second.registered
    finally:
        first.stop()
        second.stop()


def test_a_failing_action_does_not_kill_the_hotkey_thread():
    ok = []

    def boom():
        raise RuntimeError("x")

    mgr = HotkeyManager({"power": boom, "next_mode": lambda: ok.append(1)}, TEST_BINDINGS)
    assert mgr.start()
    try:
        ids = {v: k for k, v in mgr.registered.items()}
        ctypes.windll.user32.PostThreadMessageW(mgr._thread_id, WM_HOTKEY, ids["power"], 0)
        ctypes.windll.user32.PostThreadMessageW(mgr._thread_id, WM_HOTKEY, ids["next_mode"], 0)
        assert wait_for(lambda: ok == [1])
    finally:
        mgr.stop()


def test_default_bindings_are_unique_and_described():
    combos = [(m, vk) for m, vk, _ in hotkeys.DEFAULT_BINDINGS.values()]
    assert len(combos) == len(set(combos))
    assert all(desc for *_, desc in hotkeys.DEFAULT_BINDINGS.values())


# ---- actions shared by the tray, the hotkeys and the API --------------------------------------------------------

def test_toggle_power_remembers_the_mode(state):
    state.switch_mode("effect")
    state.toggle_power()
    assert state.mode == "off" and state.config.current_mode == "off"
    state.toggle_power()
    assert state.mode == "effect"


def test_cycle_mode_walks_through_the_four_modes_and_wraps(state):
    state.switch_mode("screen")
    seen = []
    for _ in range(5):
        state.cycle_mode(1)
        seen.append(state.mode)
    assert seen == ["rhythm", "effect", "static", "screen", "rhythm"]
    state.cycle_mode(-1)
    assert state.mode == "screen"


def test_cycle_mode_from_off_turns_on_in_the_last_mode(state):
    state.switch_mode("rhythm")
    state.switch_mode("off")
    state.cycle_mode(1)
    assert state.mode == "rhythm"


def test_calibration_is_never_remembered_as_the_start_mode(state):
    state.switch_mode("static")
    state.switch_mode("calibrate")
    assert state.config.current_mode == "static"


def test_brightness_nudges_are_clamped_and_reach_the_pipeline(state):
    state.config.brightness = 0.95
    state.nudge_brightness(+0.1)
    assert state.config.brightness == 1.0 and state.color_pipeline.brightness == 1.0
    for _ in range(20):
        state.nudge_brightness(-0.1)
    assert state.config.brightness == 0.05
