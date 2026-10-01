"""
Named profiles: a snapshot of the look/behaviour settings (brightness, colors, rhythm, effects...)
that can be saved and recalled. Hardware calibration (wiring, LED positions, monitor) is NOT part
of a profile, so loading one can never break the strip layout.
"""

import json
import logging
import os
import re
import threading
from dataclasses import asdict
from typing import Dict, List

from .config import AppConfig

log = logging.getLogger("zak_light")

PROFILE_FIELDS = (
    "brightness", "gamma", "saturation", "smoothing", "min_luminance_floor", "black_bar_detection",
    "target_fps", "performance_mode", "sample_box_size", "sync_preset", "rhythm_pattern", "rhythm_palette",
    "audio_sensitivity", "active_effect", "effect_speed", "static_color",
    "dark_light", "adaptive_smoothing", "screen_style", "edges_enabled", "audio_boost",
    "effect_palettes", "effect_reverse", "effect_mirror",
)
MAX_PROFILES = 30
NAME_PATTERN = r"^[\w][\w .\-]{0,39}$"   # letters (accents included), digits, space, dot, dash, underscore
_NAME_RE = re.compile(NAME_PATTERN)


class ProfileError(ValueError):
    pass


class ProfileStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    # ---- storage ---------------------------------------------------------------------------

    def _read(self) -> Dict[str, dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            log.exception("profiles.json is unreadable; starting with no profiles")
            return {}

    def _write(self, profiles: Dict[str, dict]):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---- api -------------------------------------------------------------------------------

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._read(), key=str.lower)

    def save(self, name: str, cfg: AppConfig):
        if not _NAME_RE.fullmatch(name):
            raise ProfileError("invalid profile name")
        with self._lock:
            profiles = self._read()
            if name not in profiles and len(profiles) >= MAX_PROFILES:
                raise ProfileError(f"at most {MAX_PROFILES} profiles")
            snapshot = asdict(cfg)
            profiles[name] = {k: snapshot[k] for k in PROFILE_FIELDS}
            self._write(profiles)

    def apply(self, name: str, cfg: AppConfig):
        """Copies a saved profile into cfg. Values go through the normal sanitising first."""
        with self._lock:
            profile = self._read().get(name)
        if profile is None:
            raise ProfileError("no such profile")
        merged = asdict(cfg)
        merged.update({k: v for k, v in profile.items() if k in PROFILE_FIELDS})
        # Saved profiles from before the selector only have target_fps; preserve their effective
        # rate and infer the matching label instead of displaying a contradictory profile mode.
        if "performance_mode" not in profile and "target_fps" in profile:
            merged["performance_mode"] = {20: "eco", 40: "balanced", 60: "fluid"}.get(
                profile["target_fps"], "custom"
            )
        clean = AppConfig(**merged)  # clamps/validates every value, even if the file was edited by hand
        for key in PROFILE_FIELDS:
            setattr(cfg, key, getattr(clean, key))

    def matching(self, cfg: AppConfig):
        """Name of the saved profile whose values are the ones currently in use, or None."""
        with self._lock:
            profiles = self._read()
        now = asdict(cfg)
        for name, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            merged = asdict(AppConfig())
            merged.update({k: v for k, v in profile.items() if k in PROFILE_FIELDS})
            try:
                clean = asdict(AppConfig(**merged))
            except Exception:
                continue
            if "performance_mode" not in profile and "target_fps" in profile:
                clean["performance_mode"] = {20: "eco", 40: "balanced", 60: "fluid"}.get(profile["target_fps"], "custom")
            if all(clean[k] == now[k] for k in PROFILE_FIELDS):
                return name
        return None

    def export_all(self) -> Dict[str, dict]:
        with self._lock:
            return self._read()

    def import_all(self, profiles) -> int:
        """
        Adds/replaces profiles from an exported backup. Names are validated, every profile is passed through
        the normal sanitising, unknown fields are dropped and the total is capped. Returns how many were kept.
        """
        if not isinstance(profiles, dict):
            raise ProfileError("profiles must be an object")
        clean: Dict[str, dict] = {}
        for name, values in profiles.items():
            if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or not isinstance(values, dict):
                continue
            base = asdict(AppConfig())
            base.update({k: v for k, v in values.items() if k in PROFILE_FIELDS})
            sane = asdict(AppConfig(**base))
            clean[name] = {k: sane[k] for k in PROFILE_FIELDS}
        with self._lock:
            merged = self._read()
            merged.update(clean)
            if len(merged) > MAX_PROFILES:
                raise ProfileError(f"at most {MAX_PROFILES} profiles")
            self._write(merged)
        return len(clean)

    def delete(self, name: str) -> bool:
        with self._lock:
            profiles = self._read()
            if name not in profiles:
                return False
            del profiles[name]
            self._write(profiles)
            return True
