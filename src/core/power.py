"""
Session / display / power notifications.

A hidden message-only window receives Windows' notifications for: session lock/unlock,
display on/off and system suspend/resume. The LEDs should be dark while nobody is looking at the
screen (locked PC, monitor asleep, computer suspended) and come back afterwards.
"""

import ctypes
import logging
import threading
import uuid
from ctypes import wintypes
from typing import Callable, Optional

log = logging.getLogger("zak_light")

WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
WM_CLOSE = 0x0010
WM_DISPLAYCHANGE = 0x007E   # resolution, monitor layout or monitor attached/removed
WM_DESTROY = 0x0002

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013

WTS_CONSOLE_CONNECT = 0x1
WTS_CONSOLE_DISCONNECT = 0x2
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8

GUID_CONSOLE_DISPLAY_STATE = "{6FE69556-704A-47A0-8F24-C28D936FDA47}"  # 0 off, 1 on, 2 dimmed


class PowerState:
    """Combines the separate notifications into one 'should the LEDs be dark?' answer."""

    def __init__(self):
        self.locked = False
        self.display_off = False
        self.asleep = False

    @property
    def inactive(self) -> bool:
        return self.locked or self.display_off or self.asleep

    def on_session_change(self, code: int):
        if code in (WTS_SESSION_LOCK, WTS_CONSOLE_DISCONNECT):
            self.locked = True
        elif code in (WTS_SESSION_UNLOCK, WTS_CONSOLE_CONNECT):
            self.locked = False

    def on_power_event(self, code: int, display_state: Optional[int] = None):
        if code == PBT_APMSUSPEND:
            self.asleep = True
        elif code in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
            self.asleep = False
        elif code == PBT_POWERSETTINGCHANGE and display_state is not None:
            self.display_off = display_state == 0  # 2 (dimmed) still shows a picture


class _PowerBroadcastSetting(ctypes.Structure):
    _fields_ = [("PowerSetting", ctypes.c_ubyte * 16), ("DataLength", wintypes.DWORD), ("Data", ctypes.c_ubyte * 1)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class PowerMonitor:
    """Runs the message window on its own thread and reports changes through on_change(inactive)."""

    def __init__(self, on_change: Callable[[bool], None], on_display_change: Optional[Callable[[], None]] = None):
        self.state = PowerState()
        self._on_change = on_change
        self._on_display_change = on_display_change
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self.hwnd = None
        self._proc = WNDPROC(self._wnd_proc)  # keep a reference: ctypes must not free the callback

    # ---- window procedure --------------------------------------------------------------------

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        before = self.state.inactive
        try:
            if msg == WM_WTSSESSION_CHANGE:
                self.state.on_session_change(int(wparam))
            elif msg == WM_POWERBROADCAST:
                display_state = None
                if wparam == PBT_POWERSETTINGCHANGE and lparam:
                    setting = ctypes.cast(lparam, ctypes.POINTER(_PowerBroadcastSetting)).contents
                    if bytes(setting.PowerSetting) == uuid.UUID(GUID_CONSOLE_DISPLAY_STATE).bytes_le \
                            and setting.DataLength >= 4:
                        display_state = ctypes.c_uint32.from_address(ctypes.addressof(setting.Data)).value
                self.state.on_power_event(int(wparam), display_state)
            elif msg == WM_DISPLAYCHANGE:
                if self._on_display_change:
                    self._on_display_change()
            elif msg == WM_DESTROY:
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("Power notification handler failed")
        if self.state.inactive != before:
            try:
                self._on_change(self.state.inactive)
            except Exception:
                log.exception("Power change callback failed")
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- thread ------------------------------------------------------------------------------

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="power-events", daemon=True)
        self._thread.start()
        return self._ready.wait(3.0) and self.hwnd is not None

    def stop(self):
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self):
        user32, kernel32, wtsapi = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.wtsapi32
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
        user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
        wtsapi.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        wtsapi.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]

        class_name = f"ZakLightPower{uuid.uuid4().hex[:8]}"
        wc = _WNDCLASS()
        wc.lpfnWndProc = self._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            log.error("Power monitor: RegisterClass failed")
            self._ready.set()
            return
        # A hidden top-level window (never shown), not a message-only one: Windows broadcasts
        # WM_DISPLAYCHANGE to top-level windows only.
        hwnd = user32.CreateWindowExW(0, class_name, "ZakLightPower", 0, 0, 0, 0, 0,
                                      None, None, wc.hInstance, None)
        if not hwnd:
            log.error("Power monitor: CreateWindow failed")
            self._ready.set()
            return
        self.hwnd = hwnd

        guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(GUID_CONSOLE_DISPLAY_STATE).bytes_le)
        power_handle = user32.RegisterPowerSettingNotification(hwnd, ctypes.byref(guid), 0)  # DEVICE_NOTIFY_WINDOW_HANDLE
        wtsapi.WTSRegisterSessionNotification(hwnd, 0)  # NOTIFY_FOR_THIS_SESSION
        log.info("Power monitor started (session lock, display, suspend)")
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        try:
            wtsapi.WTSUnRegisterSessionNotification(hwnd)
            if power_handle:
                user32.UnregisterPowerSettingNotification(power_handle)
        finally:
            self.hwnd = None
