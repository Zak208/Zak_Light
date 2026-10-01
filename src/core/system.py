"""
Windows integration: start with Windows and single instance.
All registry writes are per-user (HKCU), so no administrator rights are needed.
"""

import ctypes
import os
import sys
import winreg

from .paths import APP_NAME, FROZEN

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

_ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def launch_command() -> str:
    """Command line Windows runs at logon. Starts hidden in the tray."""
    if FROZEN:
        return f'"{sys.executable}" --minimized'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    interpreter = pythonw if os.path.exists(pythonw) else sys.executable
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    return f'"{interpreter}" "{script}" --minimized'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def trim_working_set():
    """Returns the pages this process no longer needs to Windows (import-time code, etc.)."""
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi")
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        psapi.EmptyWorkingSet(kernel32.GetCurrentProcess())
    except Exception:
        pass


def set_window_icon(title: str, ico_path: str) -> bool:
    """Gives the window with this title its icon (title bar and taskbar). False if the window or file is missing."""
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
        hwnd = user32.FindWindowW(None, title)
        if not hwnd or not os.path.isfile(ico_path):
            return False
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
        for which, size in ((0, 16), (1, 32)):                      # ICON_SMALL (title bar), ICON_BIG (Alt+Tab, taskbar)
            icon = user32.LoadImageW(None, ico_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if icon:
                user32.SendMessageW(hwnd, WM_SETICON, which, icon)
        return True
    except Exception:
        return False


def set_dark_title_bar(title: str) -> bool:
    """Makes the title bar of the window with this title dark (Windows 10 1809+ / 11). False if not found."""
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        value = ctypes.c_int(1)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20 on current builds, 19 on early ones)
            if dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                return True
        return False
    except Exception:
        return False


def message_box(title: str, text: str):
    """Modal error/info box for situations where the program has no window and no console."""
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 | 0x0)  # MB_ICONERROR | MB_OK


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_ram_mb() -> float:
    """Resident memory (working set) of this process in MB, as Task Manager shows it."""
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_ulong]
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return 0.0
    return counters.WorkingSetSize / (1024 * 1024)


def other_process_stats(pid: int):
    """(CPU seconds used so far, resident MB) of another process of ours, or None if it cannot be read."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    handle = kernel32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, *map(ctypes.byref, (created, exited, kernel, user))):
            return None
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCounters), ctypes.c_ulong]
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        ram = counters.WorkingSetSize / (1024 * 1024) if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb) else 0.0
        return (kernel.value + user.value) / 1e7, ram           # FILETIME units are 100 ns
    finally:
        kernel32.CloseHandle(handle)


_mutex_handles = {}


def acquire_single_instance(name: str = "single_instance") -> bool:
    """False if another process already holds the named mutex (e.g. another running engine)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handles[name] = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}_{name}")
    return kernel32.GetLastError() != _ERROR_ALREADY_EXISTS


def self_command(*args: str) -> list:
    """Command line that re-launches this program (frozen exe, or python + app.py) with args."""
    if FROZEN:
        return [sys.executable, *args]
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    return [sys.executable, script, *args]


def is_foreground(title: str) -> bool:
    """True if the window with this exact title is the one the user is working in."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    buf = ctypes.create_unicode_buffer(len(title) + 2)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value == title


def focus_window(title: str) -> bool:
    """Brings an existing top-level window to the front. False if there is none."""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True
