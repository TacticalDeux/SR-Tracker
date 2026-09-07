"""Name lookup tables for monsters and items.

Loaded once at startup from `data/monsters.json` and `data/items.json`,
both of which are produced by `tools/private/REMOVED.py` from the
recovered game pck. The DLL emits numeric IDs on the wire; the consumer
uses these tables to display the human-readable name in the Kills and
Drops tabs.

The lookup is fast (a dict per table, ~1350 entries combined) so it's
called inline on every event without measurable cost.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class NameTables:
    monsters: dict[int, str]
    items: dict[int, str]

    def monster(self, ident: int) -> str | None:
        return self.monsters.get(ident)

    def item(self, ident: int) -> str | None:
        return self.items.get(ident)


def _load_one(path: Path) -> dict[int, str]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[int, str] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        ident = row.get("id")
        name = row.get("name")
        if isinstance(ident, int) and isinstance(name, str) and name:
            out[ident] = name
    return out


def load() -> NameTables:
    """Load the name tables. Falls back to empty dicts if files are missing."""
    monsters = _load_one(paths.user_data_dir() / "monsters.json")
    items    = _load_one(paths.user_data_dir() / "items.json")
    # In dev mode, prefer the in-tree `data/` next to the source.
    if not monsters and not getattr(__import__("sys"), "frozen", False):
        monsters = _load_one(paths.dev_db_path().parent / "monsters.json")
    if not items and not getattr(__import__("sys"), "frozen", False):
        items = _load_one(paths.dev_db_path().parent / "items.json")
    return NameTables(monsters=monsters, items=items)
