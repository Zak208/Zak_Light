"""
System-tray menu and icon. The menu is built from the engine state so it always shows the truth (checked
mode, existing profiles); the icon takes the color the strip is showing, so a glance at the tray tells
whether the lights are on and what they are doing.
"""

import logging
import os
import threading
import time
from typing import Callable, Optional, Tuple

import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem as item

from core.paths import resource_dir
from core.profiles import ProfileError

log = logging.getLogger("zak_light")

BRAND = (168, 85, 247)
MODE_LABELS = (("screen", "Pantalla"), ("rhythm", "Ritmo y audio"), ("effect", "Efectos"), ("static", "Color fijo"))
BRIGHTNESS_STEPS = (10, 25, 50, 75, 100)


_WORD = None


def _lettering() -> Optional[Image.Image]:
    """The ZAK lettering (white on transparent) shipped with the window, or None if the file is missing."""
    global _WORD
    if _WORD is None:
        try:
            _WORD = Image.open(os.path.join(resource_dir(), "web", "static", "logo-word.png")).convert("RGBA")
        except Exception:
            log.warning("Logo file not found: the tray shows the plain LED icon")
            _WORD = False
    return _WORD or None


def icon_image(rgb: Optional[Tuple[int, int, int]] = None, off: bool = False) -> Image.Image:
    """The ZAK logo in the color the strip shows (grey when it is off); a plain LED if the logo is missing."""
    r, g, b = (80, 80, 92) if off else (rgb or BRAND)
    word = _lettering()
    if word is not None:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle((0, 0, 63, 63), radius=14, fill=(13, 14, 22, 255))
        w = 54
        h = max(1, round(word.height * w / word.width))
        letters = word.resize((w, h), Image.LANCZOS)
        ink = Image.new("RGBA", letters.size, (r, g, b, 255))
        ink.putalpha(letters.getchannel("A"))
        img.alpha_composite(ink, ((64 - w) // 2, (64 - h) // 2))
        return img
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(r, g, b, 120))
    d.ellipse((13, 13, 51, 51), fill=(r, g, b, 255))
    d.ellipse((23, 23, 41, 41), fill=(255, 255, 255, 235 if not off else 120))
    return img


def strip_color(state) -> Tuple[Optional[Tuple[int, int, int]], bool]:
    """(average color of the lit LEDs or None, strip_is_off)."""
    colors = state.preview_colors()
    if not colors:
        return None, False
    total = sum(max(c) for c in colors)
    if total < 40:
        return None, state.dark or state.mode == "off"
    weight = [max(c) or 0 for c in colors]
    avg = tuple(int(sum(c[k] * w for c, w in zip(colors, weight)) / max(1, sum(weight))) for k in range(3))
    peak = max(avg) or 1
    lift = min(255 / peak, 2.0)
    return tuple(min(255, int(v * lift)) for v in avg), False


def build_menu(state, launch_ui: Callable, quit_app: Callable, version: str) -> pystray.Menu:
    def mode_item(mode, label):
        return item(label, lambda icon=None, it=None: state.switch_mode(mode),
                    checked=lambda it: state.mode == mode, radio=True)

    def set_brightness(pct):
        def action(icon=None, it=None):
            state.config.brightness = pct / 100.0
            state.apply_config()
            state.schedule_save()
        return action

    def profile_action(name):
        def action(icon=None, it=None):
            try:
                state.apply_profile(name)
            except ProfileError:
                log.warning("Tray: profile %r no longer exists", name)
        return action

    def profile_items():
        names = state.profiles.names()
        if not names:
            yield item("(sin perfiles guardados)", None, enabled=False)
        for name in names:
            yield item(name, profile_action(name))

    return pystray.Menu(
        item(f"Zak_light {version}", None, enabled=False),
        item("Abrir ventana", launch_ui, default=True),
        pystray.Menu.SEPARATOR,
        item("Encendido", lambda icon=None, it=None: state.toggle_power(), checked=lambda it: state.mode != "off"),
        *[mode_item(mode, label) for mode, label in MODE_LABELS],
        pystray.Menu.SEPARATOR,
        item("Brillo", pystray.Menu(*[
            item(f"{pct} %", set_brightness(pct),
                 checked=lambda it, pct=pct: round(state.config.brightness * 100) == pct, radio=True)
            for pct in BRIGHTNESS_STEPS])),
        item("Perfiles", pystray.Menu(profile_items)),
        pystray.Menu.SEPARATOR,
        item("Salir", quit_app),
    )


def start_icon_updater(icon: pystray.Icon, state, interval: float = 2.0) -> threading.Thread:
    """Keeps the tray icon in the color of the strip; redraws only when the color really changed."""
    def run():
        last = None
        while state.is_running:
            time.sleep(interval)
            try:
                rgb, off = strip_color(state)
                key = (off, None if rgb is None else tuple(v // 24 for v in rgb))
                if key != last:
                    last = key
                    icon.icon = icon_image(rgb, off)
            except Exception:
                log.exception("Could not update the tray icon")
    t = threading.Thread(target=run, name="tray-icon", daemon=True)
    t.start()
    return t
