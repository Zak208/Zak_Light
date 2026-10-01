"""
Resource and user-data locations.

Running from source: the code and the web files live in src/, the user's data in data/ (next to src/).
Running frozen (PyInstaller/setup.exe): resources are bundled read-only in the install
folder, so settings must go to %APPDATA%\\Zak_light (Program Files is not writable).
"""

import os
import sys

APP_NAME = "Zak_light"
FROZEN = bool(getattr(sys, "frozen", False))


def resource_dir() -> str:
    if FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the src/ folder


def data_dir() -> str:
    if FROZEN:
        path = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
        os.makedirs(path, exist_ok=True)
        return path
    path = os.path.join(os.path.dirname(resource_dir()), "data")
    os.makedirs(path, exist_ok=True)
    return path
