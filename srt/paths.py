"""Path resolution that works both in dev and as a frozen PyInstaller bundle."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Source-tree layout (running `python -m srt.app` or `python sr_tracker.py`):
#
#   <project>/srt/*.py
#   <project>/dll/sr_tracker.dll
#   <project>/data/srtracker.db          (dev only)
#
# Frozen layout (PyInstaller --add-binary "dll/sr_tracker.dll;dll"):
#
#   <exe>
#   _internal/dll/sr_tracker.dll
#   _internal/srt/*.py
#
# In dev we keep the DB next to the source. When frozen, the DB lives under
# %LOCALAPPDATA%/SRTracker/ so it survives exe upgrades.

APP_DIR = Path(__file__).resolve().parent.parent


def _bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", APP_DIR))


def assets_dir() -> Path:
    """Location of the bundled image assets (the soul crystal icon)."""
    return _bundle_dir() / "assets"


def data_files_dir() -> Path:
    """Location of the bundled data files (monster/item name tables)."""
    return _bundle_dir() / "data"


def dll_path() -> Path:
    return _bundle_dir() / "dll" / "sr_tracker.dll"


def dev_db_path() -> Path:
    return APP_DIR / "data" / "srtracker.db"


def user_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "SRTracker"
    return APP_DIR / "data"


def user_db_path() -> Path:
    return user_data_dir() / "srtracker.db"


def user_settings_path() -> Path:
    return user_data_dir() / "settings.json"
