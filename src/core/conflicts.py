"""
Other programs that drive the same LED controller.

Only one program can usefully talk to the strip: if the original *DX Light* app is running it keeps
streaming its own screen-sync frames to the same device, so whatever Zak_light shows is overwritten a
moment later (the "it always goes back to screen sync" symptom). This module detects that, and offers
two explicit, user-triggered remedies: close it, or stop it from starting with Windows.
Nothing here runs on its own.
"""

import ctypes
import logging
import os
import subprocess
import winreg
from ctypes import wintypes
from typing import Dict, List

log = logging.getLogger("zak_light")

# Programs known to use the same controller. Fixed list: no name ever comes from a request.
KNOWN_CONFLICTS = [
    {"name": "DX Light", "process": "DX Light.exe", "run_value": "electron.app.DX Light"},
]
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

_TH32CS_SNAPPROCESS = 0x2


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def running_process_names() -> Dict[str, int]:
    """{lower-case exe name: number of processes} for everything running now."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    counts: Dict[str, int] = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return counts
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile.lower()
            counts[name] = counts.get(name, 0) + 1
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return counts


def _autostart_enabled(run_value: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, run_value)
            return True
    except OSError:
        return False


def active_conflicts() -> List[dict]:
    """Known competing programs that are running right now."""
    running = running_process_names()
    found = []
    for app in KNOWN_CONFLICTS:
        count = running.get(app["process"].lower(), 0)
        if count:
            found.append({"name": app["name"], "processes": count, "autostart": _autostart_enabled(app["run_value"])})
    return found


def close_conflicts() -> List[str]:
    """Ends every known competing program (and its child processes). Returns the names it closed."""
    closed = []
    running = running_process_names()
    for app in KNOWN_CONFLICTS:
        if running.get(app["process"].lower(), 0):
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
            subprocess.run([taskkill, "/F", "/T", "/IM", app["process"]], capture_output=True,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
            closed.append(app["name"])
            log.info("Closed %s at the user's request (it was competing for the LED controller)", app["name"])
    return closed


def disable_conflict_autostart() -> List[str]:
    """Stops known competing programs from starting with Windows (removes only their own Run entry)."""
    disabled = []
    for app in KNOWN_CONFLICTS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, app["run_value"])
            disabled.append(app["name"])
            log.info("Removed %s from Windows startup at the user's request", app["name"])
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("Could not remove %s from startup", app["name"])
    return disabled
