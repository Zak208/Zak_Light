"""
Window process of Zak_light (started with `--ui`).

The lighting engine lives in its own headless process (tray icon, no WebView2). This process only
shows the web UI of that engine, so the ~300 MB of WebView2 processes exist only while the window
is open: closing the window ends this process and frees all of it. Kept import-light on purpose
(no numpy / opencv / fastapi here).
"""

import json
import os
import subprocess
import threading
import time
import urllib.request

from core import system as winsys
from core.config import load_config
from core.paths import resource_dir

UI_URL = "http://127.0.0.1:8765"
WINDOW_TITLE = "Zak_light"
ENGINE_WAIT_SECONDS = 20
ENGINE_GONE_AFTER = 3  # consecutive failed health checks before the window closes itself


def fetch_startup(timeout: float = 0.6):
    """The engine's startup/window settings, or None if it does not answer."""
    try:
        with urllib.request.urlopen(UI_URL + "/api/startup", timeout=timeout) as resp:
            return json.load(resp)
    except Exception:
        return None


def engine_running(timeout: float = 0.6) -> bool:
    return fetch_startup(timeout) is not None


def ensure_engine() -> bool:
    """Starts the engine in the background if it is not running yet, then waits for its server."""
    if engine_running():
        return True
    subprocess.Popen(winsys.self_command("--minimized"))
    deadline = time.monotonic() + ENGINE_WAIT_SECONDS
    while time.monotonic() < deadline:
        if engine_running():
            return True
        time.sleep(0.3)
    return False


def run_ui():
    if not winsys.acquire_single_instance("ui"):
        winsys.focus_window(WINDOW_TITLE)  # a window is already open: just bring it forward
        return

    cfg = load_config()
    autoclose = cfg.ui_autoclose_minutes * 60.0     # refreshed from the engine while the window is open
    if cfg.low_power_ui:
        # Simple UI: software rendering keeps WebView2 off the GPU. No Windows per-app GPU
        # preference is written (the WebView2 runtime is shared with other apps).
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu")

    if not ensure_engine():
        return

    import webview  # deferred: only this process needs it

    window = webview.create_window(
        title=WINDOW_TITLE,
        url=UI_URL,
        width=460,
        height=780,
        resizable=True,
        min_size=(420, 560),
        background_color="#0a0b12",
        text_select=False,
    )

    def watch_engine():
        """If the engine quits (tray > Salir) while the window is open, close the window too."""
        nonlocal autoclose
        failures = 0
        unused_since = time.monotonic()
        while True:
            time.sleep(3)
            startup = fetch_startup(1.5)
            failures = 0 if startup is not None else failures + 1
            if startup is not None:        # a change made in Settings applies right away, not only on reopening
                autoclose = float(startup.get("ui_autoclose_minutes", 0)) * 60.0
            # A window nobody looks at still costs ~450 MB of WebView2: close it after the chosen time
            if winsys.is_foreground(WINDOW_TITLE):
                unused_since = time.monotonic()
            unused = autoclose and time.monotonic() - unused_since > autoclose
            if failures >= ENGINE_GONE_AFTER or unused:
                try:
                    window.destroy()
                except Exception:
                    pass
                return

    threading.Thread(target=watch_engine, daemon=True).start()

    def on_started():  # runs once the GUI loop is up
        # Whatever start flags this process inherited (a shortcut set to "minimized", a parent that hides
        # its children...), the window the user asked for must appear restored and in front.
        try:
            window.restore()
        except Exception:
            pass
        icon = os.path.join(resource_dir(), "web", "static", "favicon.ico")
        for _ in range(20):  # the native window may need a moment before it can be styled
            winsys.set_window_icon(WINDOW_TITLE, icon)
            if winsys.set_dark_title_bar(WINDOW_TITLE):
                break
            time.sleep(0.25)
        winsys.focus_window(WINDOW_TITLE)

    webview.start(on_started)
