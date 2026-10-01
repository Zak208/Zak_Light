"""
Zak_light - engine process (system tray) of the ambient lighting app.

The engine (capture, audio analysis, USB output, local API) runs here without any window.
The window is a separate process (`--ui`, see ui_window.py) that exists only while it is open.

    Zak_light.exe               start the engine and open the window (or just open the window
                                if the engine is already running)
    Zak_light.exe --minimized   start the engine only (this is what "start with Windows" runs)
    Zak_light.exe --ui          window process (started by the engine / by a second launch)
    Zak_light.exe --set-mode X  control a running engine from the command line (see cli.py)
    Zak_light.exe --quit        close a running engine cleanly (LEDs off); the installer uses it
"""

import os
import sys

if "--ui" in sys.argv:  # window process: must not import the engine stack (numpy, opencv, ...)
    from ui_window import run_ui
    run_ui()
    sys.exit(0)

if "--capture-helper" in sys.argv:  # screen-capture child process (core/capture_proxy.py): capture stack only
    from core.capture_proxy import run_helper
    sys.exit(run_helper())

if any(flag in sys.argv for flag in ("--set-mode", "--brightness", "--status", "--quit", "--toggle", "--profile", "--effect")):
    from cli import run_cli
    sys.exit(run_cli(sys.argv[1:]))

# Our matrix work is tiny: one BLAS thread avoids ~500 MB of committed thread buffers on many-core CPUs.
# Must be set before numpy is imported.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import asyncio
import logging
import socket
import subprocess
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from typing import Optional

import pystray
import uvicorn

from api import create_app
from core import conflicts as conflicts_mod
from core import display as display_info
from core import system as winsys
from core.automation import Automation
from core.hotkeys import DEFAULT_BINDINGS, HotkeyManager
from core.paths import data_dir, resource_dir
from core.power import PowerMonitor
from engine import EngineState, run_engine
import tray as tray_ui
from version import VERSION

# Console-less or redirected runs use cp1252, which cannot encode the emoji in our log lines
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("zak_light")

PORT = 8765
UI_URL = f"http://127.0.0.1:{PORT}"

state: Optional[EngineState] = None
ui_process: Optional[subprocess.Popen] = None
tray_icon: Optional[pystray.Icon] = None
hotkey_manager: Optional[HotkeyManager] = None


class _ClosedConnectionNoise(logging.Filter):
    """The window (WebView2) drops connections abruptly; asyncio then logs a harmless ConnectionResetError."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, (ConnectionResetError, ConnectionAbortedError)) and "connection_lost" in record.getMessage())


def setup_logging():
    handler = RotatingFileHandler(os.path.join(data_dir(), "zak_light.log"), maxBytes=512_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("asyncio").addFilter(_ClosedConnectionNoise())

    # Anything that would otherwise die silently (no console in the installed build) ends up in the log
    sys.excepthook = lambda t, v, tb: log.critical("Uncaught exception", exc_info=(t, v, tb))
    threading.excepthook = lambda a: log.critical(
        "Uncaught exception in thread %s", getattr(a.thread, "name", "?"), exc_info=(a.exc_type, a.exc_value, a.exc_traceback)
    )


class QuietServer(uvicorn.Server):
    """uvicorn wakes up 10 times a second forever just to refresh its Date header; we do not need that."""

    async def main_loop(self):
        while not self.should_exit:
            await asyncio.sleep(1.0)


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def wait_for_server(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(UI_URL + "/api/startup", timeout=0.5):
                return True
        except Exception:
            time.sleep(0.1)
    return False


# ---- tray ------------------------------------------------------------------------------------

def launch_ui(icon=None, item=None):
    """Opens the window as a separate process (WebView2 lives only while it is open)."""
    global ui_process
    if ui_process is not None and ui_process.poll() is None:
        winsys.focus_window("Zak_light")
        return
    ui_process = subprocess.Popen(winsys.self_command("--ui"))


def notify(title: str, text: str):
    """Tray balloon; respects the user's notifications setting and never raises."""
    try:
        if state is not None and state.config.notifications and tray_icon is not None:
            tray_icon.notify(text, title)
    except Exception:
        log.exception("Could not show a notification")


def set_hotkeys(enabled: bool):
    """Starts or stops the global shortcuts (called at startup and when the setting changes)."""
    global hotkey_manager
    if enabled and hotkey_manager is None:
        actions = {
            "power": state.toggle_power,
            "next_mode": lambda: state.cycle_mode(1),
            "prev_mode": lambda: state.cycle_mode(-1),
            "brightness_up": lambda: state.nudge_brightness(+0.1),
            "brightness_down": lambda: state.nudge_brightness(-0.1),
        }
        mgr = HotkeyManager(actions)
        mgr.start()
        hotkey_manager = mgr
        state.hotkey_info = [{"action": a, "text": desc, "active": a in mgr.registered.values()}
                             for a, (_m, _v, desc) in DEFAULT_BINDINGS.items()]
    elif not enabled and hotkey_manager is not None:
        hotkey_manager.stop()
        hotkey_manager = None
        state.hotkey_info = [{**h, "active": False} for h in getattr(state, "hotkey_info", [])]


def health_watch():
    """Tells the user, once per event, about things they would otherwise only discover by a dark strip."""
    lost_since = None
    lost_told = False
    conflict_told = False
    stall_told = False
    time.sleep(4.0)
    while state.is_running:
        try:
            if not state.driver.is_connected:
                lost_since = lost_since or time.monotonic()
                if not lost_told and time.monotonic() - lost_since > 10.0:
                    lost_told = True
                    notify("Zak_light", "Se perdió la conexión USB con la tira de LEDs. Reintentando…")
            else:
                if lost_told:
                    notify("Zak_light", "Tira de LEDs reconectada.")
                lost_since, lost_told = None, False

            found = conflicts_mod.active_conflicts()
            if found and not conflict_told:
                conflict_told = True
                names = ", ".join(c.get("name", "") for c in found if c.get("name")) or "otro programa"
                notify("Zak_light", f"{names} está usando la tira: los colores pueden no cambiar. Ciérralo desde la app.")
            elif not found:
                conflict_told = False

            if state.capture_stalled and not stall_told:
                stall_told = True
                notify("Zak_light", "La captura de pantalla no responde; se sigue intentando recuperar.")
            elif not state.capture_stalled:
                stall_told = False
        except Exception:
            log.exception("Health watch failed")
        time.sleep(3.0)


def quit_app(icon=None, item=None):
    state.is_running = False
    set_hotkeys(False)
    state.wake.set()
    threading.Thread(target=getattr(state.screen_sampler, "close", lambda: None), daemon=True).start()
    state.flush_save()
    try:
        state.driver.turn_off()  # directly: the engine thread may be busy inside a capture call
    except Exception:
        log.exception("Could not turn the LEDs off on exit")
    if ui_process is not None and ui_process.poll() is None:
        ui_process.terminate()
    if tray_icon:
        tray_icon.stop()  # makes tray_icon.run() in main() return


# ---- main ------------------------------------------------------------------------------------

def main():
    global state, tray_icon
    want_window = "--minimized" not in sys.argv
    setup_logging()

    if not winsys.acquire_single_instance("engine"):
        # The engine is already running: launching the program again just opens the window
        if want_window:
            subprocess.Popen(winsys.self_command("--ui"))
        return

    if port_in_use():
        log.error("Port %d is already in use by another program", PORT)
        winsys.message_box("Zak_light", f"El puerto {PORT} esta ocupado por otro programa.\n"
                           "Cierra ese programa e inicia Zak_light de nuevo.")
        return

    log.info("Zak_light %s starting", VERSION)
    state = EngineState()  # only after the single-instance and port checks

    # Keep the registry in sync with the saved preference (also repairs a moved install folder)
    if state.config.run_on_startup:
        winsys.set_autostart(True)

    state.automation = Automation(state)
    state.notify = notify
    state.hotkeys_control = set_hotkeys
    if display_info.hdr_enabled():
        log.warning("HDR is on: screen colors may look washed out; turn HDR off or use the average style")

    # 1. Rendering engine thread
    engine_thread = threading.Thread(target=run_engine, args=(state,), name="engine", daemon=True)
    engine_thread.start()

    # 2. Dark LEDs while the session is locked / the display is off / the PC sleeps
    display_timer = {"t": None}

    def on_display_change():
        """Windows sends several messages per change; restart the capture once, when it has settled."""
        if display_timer["t"] is not None:
            display_timer["t"].cancel()
        display_timer["t"] = threading.Timer(1.0, state.display_changed)
        display_timer["t"].daemon = True
        display_timer["t"].start()

    power = PowerMonitor(state.set_suspended, on_display_change)
    power.start()

    # 3. Local API server
    # log_config=None: a windowed (no console) build has no stdout for uvicorn's formatter
    config = uvicorn.Config(create_app(state, os.path.join(resource_dir(), "web"), on_quit=quit_app), host="127.0.0.1", port=PORT,
                            log_config=None, ws="none", lifespan="off", access_log=False)
    server = QuietServer(config)
    threading.Thread(target=server.run, name="api-server", daemon=True).start()
    if not wait_for_server():
        log.error("The local API did not start")

    # 4. System tray icon (runs on this, the main thread)
    tray_menu = tray_ui.build_menu(state, launch_ui, quit_app, VERSION)
    tray_icon = pystray.Icon("Zak_light", tray_ui.icon_image(), "Zak_light", tray_menu)
    tray_ui.start_icon_updater(tray_icon, state)
    set_hotkeys(state.config.hotkeys_enabled)
    threading.Thread(target=health_watch, name="health-watch", daemon=True).start()

    print("Motor y bandeja de Zak_light iniciados.")
    if want_window:
        launch_ui()

    # Startup imports are no longer needed: give those pages back to Windows
    threading.Timer(6.0, winsys.trim_working_set).start()

    tray_icon.run()  # blocks until quit_app()

    # Let the engine loop turn the LEDs off before the process exits
    engine_thread.join(timeout=3)
    power.stop()
    server.should_exit = True
    log.info("Zak_light stopped")


if __name__ == "__main__":
    main()
