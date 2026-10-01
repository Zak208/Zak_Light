"""
Display information: is HDR ("advanced color") switched on for any active monitor?

Screen capture through Desktop Duplication returns SDR pixels; with HDR on, Windows tone-maps the desktop for
the capture, which can make colors look washed out. Knowing it lets the UI say so instead of leaving the user
guessing. Soft-fail: any problem returns None ("unknown").
"""

import ctypes
from ctypes import wintypes
from typing import Optional

QDC_ONLY_ACTIVE_PATHS = 0x2
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _Rational(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class _PathSource(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", wintypes.UINT), ("modeInfoIdx", wintypes.UINT), ("statusFlags", wintypes.UINT)]


class _PathTarget(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", wintypes.UINT), ("modeInfoIdx", wintypes.UINT),
                ("outputTechnology", wintypes.UINT), ("rotation", wintypes.UINT), ("scaling", wintypes.UINT),
                ("refreshRate", _Rational), ("scanLineOrdering", wintypes.UINT), ("targetAvailable", wintypes.BOOL),
                ("statusFlags", wintypes.UINT)]


class _PathInfo(ctypes.Structure):
    _fields_ = [("sourceInfo", _PathSource), ("targetInfo", _PathTarget), ("flags", wintypes.UINT)]


class _ModeInfo(ctypes.Structure):
    _fields_ = [("infoType", wintypes.UINT), ("id", wintypes.UINT), ("adapterId", LUID), ("union", ctypes.c_ubyte * 48)]


class _DeviceInfoHeader(ctypes.Structure):
    _fields_ = [("type", wintypes.UINT), ("size", wintypes.UINT), ("adapterId", LUID), ("id", wintypes.UINT)]


class _AdvancedColorInfo(ctypes.Structure):
    _fields_ = [("header", _DeviceInfoHeader), ("value", wintypes.UINT), ("colorEncoding", wintypes.UINT),
                ("bitsPerColorChannel", wintypes.UINT)]


def hdr_enabled() -> Optional[bool]:
    """True if HDR is on for at least one active monitor, False if it is off everywhere, None if unknown."""
    try:
        user32 = ctypes.windll.user32
        n_paths, n_modes = wintypes.UINT(0), wintypes.UINT(0)
        if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)) != 0:
            return None
        paths = (_PathInfo * n_paths.value)()
        modes = (_ModeInfo * n_modes.value)()
        if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                     ctypes.byref(n_modes), modes, None) != 0:
            return None
        seen_any = False
        for i in range(n_paths.value):
            info = _AdvancedColorInfo()
            info.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO
            info.header.size = ctypes.sizeof(info)
            info.header.adapterId = paths[i].targetInfo.adapterId
            info.header.id = paths[i].targetInfo.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(info)) == 0:
                seen_any = True
                if info.value & 0x2:          # advancedColorEnabled
                    return True
        return False if seen_any else None
    except Exception:
        return None
