"""User settings, persisted as JSON in the user data dir.

Schema is intentionally tiny — the tracker has very few preferences. New keys
must be added with a sensible default in `Settings.defaults()`; readers
should treat any missing key as "use the default" so a partial or older
settings file upgrades cleanly.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths


#: Overlay fields that can choose a reset scope. Kept in sync with
#: overlay.OVERLAY_FIELDS (this module stays Qt-free, so the key list
#: lives here and overlay.py mirrors it).
OVERLAY_FIELD_KEYS = ("kills", "mighties", "sc", "xp", "level", "zone",
                      "deaths", "xp_lost", "xp_hr", "xp_pct", "xp_pct_hr",
                      "dps")

#: Valid per-field scopes: "visit" resets on zone change, "session"
#: persists through the session.
FIELD_SCOPES = ("visit", "session")

#: Window placement presets for the overlay (corner of the available
#: geometry the offsets are measured inward from).
OVERLAY_ANCHORS = ("top-left", "top-right", "bottom-left", "bottom-right")


@dataclass
class Settings:
    # Overlay window. overlay_opacity drives the card BACKGROUND fill
    # only (text stays full-bright); overlay_window_opacity fades the
    # whole window, background and text together (see
    # OverlayWindow._apply_bg / _apply_window_opacity).
    # overlay_text_color paints the stats text (labels + numbers);
    # it matches theme.PARCH_BG by default (kept a literal so this
    # module stays Qt-free).
    overlay_opacity: float = 0.88        # card bg alpha 0.0 .. 1.0
    overlay_locked_opacity: float = 0.55 # card bg alpha while locked
    overlay_window_opacity: float = 1.0  # whole window 0.0 .. 1.0
    overlay_locked_window_opacity: float = 1.0  # whole window while locked
    overlay_text_color: str = "#e8dfc8"  # stats text (labels + numbers)
    # overlay_locked_text_color paints the numbers while locked (labels
    # stay at overlay_text_color); matches the old hardcoded dim gray.
    overlay_locked_text_color: str = "#3a3a48"
    # Content scale for the overlay (card padding, minimums, and the
    # base every text class multiplies at render time).
    # 1.0 == designed size; the scale slider writes 0.7 .. 1.5.
    overlay_scale: float = 1.0
    # Per-class text scales, each moving only its own target at render
    # time (1.0 == designed size; each tab slider writes 0.5 .. 2.0).
    overlay_numbers_scale: float = 1.0
    overlay_title_scale: float = 1.0
    overlay_labels_scale: float = 1.0
    # Window placement preset plus inward pixel offsets from it.
    overlay_anchor: str = "top-left"  # one of the OVERLAY_ANCHORS below
    overlay_x: int = 0
    overlay_y: int = 0
    overlay_show_kills: bool = True
    overlay_show_mighties: bool = True
    overlay_show_sc: bool = True
    overlay_show_xp: bool = True
    overlay_show_level: bool = True
    overlay_show_zone: bool = True
    overlay_show_deaths: bool = True
    overlay_show_xp_lost: bool = True
    overlay_show_xp_hr: bool = True
    overlay_show_xp_pct: bool = True
    overlay_show_xp_pct_hr: bool = True
    overlay_show_dps: bool = True
    # Per-field reset scope: each movable overlay field independently
    # chooses "visit" (reset on zone change) or "session" (persist
    # through the session). Missing keys upgrade to "session" (today's
    # behavior); junk values sanitize to "session". "level" is
    # account-scoped and always reads session-wide regardless of its
    # entry (see overlay.field_scope).
    overlay_field_scope: dict[str, str] = field(
        default_factory=lambda: {k: "session" for k in OVERLAY_FIELD_KEYS})
    # Display order of the overlay fields, top to bottom. Unknown keys
    # are ignored and missing known keys append at the end, so older
    # files and future fields both degrade gracefully.
    overlay_field_order: list[str] = field(
        default_factory=lambda: ["kills", "mighties", "sc", "xp", "level",
                                   "zone", "deaths", "xp_lost", "xp_hr",
                                   "xp_pct", "xp_pct_hr", "dps"])
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
        d = dict(d)
        # Legacy migration: the old global `overlay_per_zone` bool becomes
        # a per-field scope map (True -> every field "visit", False -> every
        # field "session" which equals the default), then the old key is
        # dropped. An explicit `overlay_field_scope` map always wins over
        # the legacy flag when both are present.
        legacy = d.pop("overlay_per_zone", None)
        raw_scopes = d.get("overlay_field_scope")
        if isinstance(raw_scopes, dict):
            scopes = {k: "session" for k in OVERLAY_FIELD_KEYS}
            for k, v in raw_scopes.items():
                if isinstance(k, str) and k:
                    scopes[k] = v if v in FIELD_SCOPES else "session"
            d["overlay_field_scope"] = scopes
        elif legacy is True:
            d["overlay_field_scope"] = {
                k: "visit" for k in OVERLAY_FIELD_KEYS}
        elif legacy is False:
            d["overlay_field_scope"] = {
                k: "session" for k in OVERLAY_FIELD_KEYS}
        valid = {f.name for f in fields(cls)}
        clean = {k: v for k, v in d.items() if k in valid}
        merged = asdict(cls.defaults())
        merged.update(clean)
        # Retired single text scale: fold a stored non-default value
        # into the numbers scale so older files keep their sizing.
        legacy_text = d.pop("overlay_text_scale", None)
        if "overlay_numbers_scale" not in d:
            try:
                legacy_f = float(legacy_text)
            except (TypeError, ValueError):
                legacy_f = 1.0
            if abs(legacy_f - 1.0) > 1e-9:
                merged["overlay_numbers_scale"] = legacy_f
        # Per-class text scales: clamp so junk files degrade cleanly.
        for _k in ("overlay_numbers_scale", "overlay_title_scale",
                   "overlay_labels_scale"):
            try:
                merged[_k] = min(
                    2.0, max(0.5, float(merged.get(_k, 1.0))))
            except (TypeError, ValueError):
                merged[_k] = 1.0
        if merged.get("overlay_anchor") not in OVERLAY_ANCHORS:
            merged["overlay_anchor"] = "top-left"
        for _k in ("overlay_x", "overlay_y"):
            try:
                merged[_k] = int(merged.get(_k, 0))
            except (TypeError, ValueError):
                merged[_k] = 0
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
