"""User settings, persisted as JSON in the user data dir.

Schema is intentionally tiny — the tracker has very few preferences. New keys
must be added with a sensible default in `Settings.defaults()`; readers
should treat any missing key as "use the default" so a partial or older
settings file upgrades cleanly.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from . import paths


@dataclass
class Settings:
    # Overlay window
    overlay_opacity: float = 0.88        # 0.0 (invisible) .. 1.0
    overlay_locked_opacity: float = 0.55 # opacity while locked
    overlay_show_kills: bool = True
    overlay_show_sc: bool = True
    overlay_show_xp: bool = True
    overlay_show_level: bool = True
    overlay_show_zone: bool = True
    overlay_pos_x: int = 60
    overlay_pos_y: int = 60
    overlay_locked: bool = False
    overlay_orientation: str = "vertical"  # "vertical" (stacked rows) or "horizontal" (strip)

    @classmethod
    def defaults(cls) -> "Settings":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        # Drop unknown keys (forward compat) and fill missing ones with defaults.
        valid = {f.name for f in fields(cls)}
        clean = {k: v for k, v in d.items() if k in valid}
        merged = asdict(cls.defaults())
        merged.update(clean)
        return cls(**merged)


class SettingsStore:
    """Thread-safe load/save of the settings file."""

    def __init__(self, path: Path | None = None):
        self._path = path or paths.user_settings_path()
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Settings:
        with self._lock:
            if not self._path.exists():
                return Settings.defaults()
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return Settings.defaults()
                return Settings.from_dict(data)
            except (json.JSONDecodeError, OSError):
                # Corrupted file → fall back to defaults; don't crash.
                return Settings.defaults()

    def save(self, settings: Settings) -> None:
        with self._lock:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            try:
                with tmp.open("w", encoding="utf-8") as f:
                    json.dump(settings.to_dict(), f, indent=2)
                tmp.replace(self._path)
            except OSError:
                # Best-effort save; never let settings I/O crash the UI.
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
