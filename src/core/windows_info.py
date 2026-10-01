"""
Small read-only queries about the Windows session, used by the automation (app rules, idle detection).
Every function fails soft: on any error it returns an "unknown" value instead of raising.
"""

import ctypes
import os
from ctypes import wintypes
from typing import List, Optional

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_OWN_NAMES = {"zak_light.exe", "python.exe", "pythonw.exe"}
_SHELL_NAMES = {"explorer.exe", "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
                "applicationframehost.exe", "textinputhost.exe", "lockapp.exe", "systemsettings.exe"}


def _exe_of_window(hwnd) -> Optional[str]:
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)
    return None


def foreground_exe() -> Optional[str]:
    """Executable name (lower case, e.g. 'valorant-win64-shipping.exe') of the window the user is using."""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        return _exe_of_window(hwnd) if hwnd else None
    except Exception:
        return None


def open_apps() -> List[dict]:
    """Applications with a visible window: [{"exe", "title"}], for the 'rules per application' picker."""
    found = {}
    try:
        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    exe = _exe_of_window(hwnd)
                    if exe and exe not in _SHELL_NAMES and exe not in _OWN_NAMES and exe not in found:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        found[exe] = buf.value
            return True

        user32.EnumWindows(enum_proc(visit), 0)
    except Exception:
        pass
    return [{"exe": e, "title": t} for e, t in sorted(found.items())]


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (0.0 if it cannot be read)."""
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0
