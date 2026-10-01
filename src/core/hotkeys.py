"""
Global keyboard shortcuts (work inside games and full-screen apps, unlike the ones in the window).

A dedicated thread registers the combinations with Windows (RegisterHotKey) and runs a message loop. If a
combination is already taken by another program it is simply skipped and reported, never an error.
"""

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Dict, Optional, Tuple

log = logging.getLogger("zak_light")

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28

# action -> (modifiers, virtual key, human description)
DEFAULT_BINDINGS: Dict[str, Tuple[int, int, str]] = {
    "power": (MOD_CONTROL | MOD_ALT, ord("L"), "Ctrl+Alt+L: encender / apagar"),
    "next_mode": (MOD_CONTROL | MOD_ALT, VK_RIGHT, "Ctrl+Alt+→: modo siguiente"),
    "prev_mode": (MOD_CONTROL | MOD_ALT, VK_LEFT, "Ctrl+Alt+←: modo anterior"),
    "brightness_up": (MOD_CONTROL | MOD_ALT, VK_UP, "Ctrl+Alt+↑: más brillo"),
    "brightness_down": (MOD_CONTROL | MOD_ALT, VK_DOWN, "Ctrl+Alt+↓: menos brillo"),
}


class HotkeyManager:
    def __init__(self, actions: Dict[str, Callable[[], None]], bindings: Optional[Dict[str, Tuple[int, int, str]]] = None):
        self.actions = actions
        self.bindings = bindings if bindings is not None else DEFAULT_BINDINGS
        self.registered: Dict[int, str] = {}     # hotkey id -> action name
        self.failed: list = []                   # actions whose combination was already taken
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        return self._ready.wait(3.0)

    def stop(self):
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = 0

    def trigger(self, hotkey_id: int):
        """What happens when Windows reports a hotkey (also used by the tests)."""
        name = self.registered.get(hotkey_id)
        action = self.actions.get(name) if name else None
        if action:
            try:
                action()
            except Exception:
                log.exception("Hotkey action %s failed", name)

    def _run(self):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        for n, (action, (mods, vk, _desc)) in enumerate(self.bindings.items(), start=1):
            if action not in self.actions:
                continue
            if user32.RegisterHotKey(None, n, mods | MOD_NOREPEAT, vk):
                self.registered[n] = action
            else:
                self.failed.append(action)
                log.warning("Hotkey for %r is already used by another program", action)
        log.info("Global hotkeys: %d registered, %d unavailable", len(self.registered), len(self.failed))
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.trigger(int(msg.wParam))
        for n in list(self.registered):
            user32.UnregisterHotKey(None, n)
        self.registered.clear()
