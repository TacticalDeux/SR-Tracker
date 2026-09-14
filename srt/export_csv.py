"""CSV export for sessions.

Pure stdlib helpers behind the Sessions-tab and session-detail Export
buttons. Single-table CSVs with an `utf-8-sig` encoding so Excel opens
them directly. A per-session export writes one file per table next to
the picked path (`<stem>_summary.csv`, `<stem>_zones.csv`, ...), since
a session's tables don't share columns.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

# Row caps: exports bypass the UI's 200-row window, but a session can
# still grow unboundedly — cap high enough to never bite in practice.
_DETAIL_LIMIT = 100_000

_UNCLAIMED = 0xFFFFFFFF


def _write(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _zone_seconds(entered: str | None, left: str | None) -> int | None:
    try:
        t0 = datetime.fromisoformat(entered) if entered else None
        t1 = datetime.fromisoformat(left) if left else None
    except Exception:
        return None
    if not t0 or not t1:
        return None
    secs = int((t1 - t0).total_seconds())
    return secs if secs >= 0 else None


def _owner(belongs_to, acct) -> str:
    if belongs_to is None:
        return ""
    if acct is not None and belongs_to == acct:
        return "You"
    if belongs_to == _UNCLAIMED:
        return "Unclaimed"
    return f"#{belongs_to}"


def _status(r: dict, acct) -> str:
    picker = r.get("picked_up_by")
    if picker is not None:
        if acct is not None and picker == acct:
            return "Picked up (you)"
        return f"Picked up (#{picker})"
    if r.get("destroyed"):
        return "Gone"
    return "On ground"


def export_all_sessions(db, path: str | Path) -> Path:
    """Every session's summary row, newest first. Returns the path."""
    path = Path(path)
    rows = db.past_sessions(10_000)
    _write(path,
           ["id", "started", "ended", "zone", "kills", "my_kills",
            "soul_crystals", "my_soul_crystals", "xp", "level",
            "drops", "my_drops"],
           [[r["id"], r["started"], r["ended"], r["current_zone"],
             r["kills"], r["my_kills"], r["soul_crystals"],
             r["my_soul_crystals"], r["xp"], r["level"],
             r["drops"], r["my_drops"]] for r in rows])
    return path


# One file per table, in this order for a full-session export.
TABLES = ("summary", "zones", "kills", "drops", "mighties")


def export_session_table(db, names, session_id: int, table: str,
                         base: str | Path) -> Path:
    """One table to `<stem>_<table>.csv` next to `base`. Returns the path."""
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")
    base = Path(base)
    stem = base.stem or f"session-{session_id}"
    dest = base.parent

    s = db.summary(session_id)
    acct = db.session_account(session_id)
    meta = db.session_meta(session_id) or {}
    if table == "summary":
        header = ["metric", "value"]
        rows = [
            ["session_id", session_id],
            ["started", meta.get("started", "")],
            ["ended", meta.get("ended", "") or "ongoing"],
            ["account", acct if acct is not None else "unknown"],
            ["kills_mine", s["my_kills"]],
            ["kills_total", s["kills"]],
            ["mighties", s.get("mighty_kills", 0)],
            ["drops_mine", s["my_drops"]],
            ["drops_total", s["drops"]],
            ["sc_picked", s["sc_picked"]],
            ["sc_total", s["sc_picked"] + s["sc_unpicked"]],
            ["xp", s["xp"]],
            ["level", s["level"]],
            ["deaths", s.get("deaths", 0)],
            ["xp_lost", s.get("xp_lost", 0)],
            ["damage_mine", s["damage_mine"]],
            ["damage_total", s["damage_total"]],
        ]
    elif table == "zones":
        zones = db.zone_stats(session_id)
        header = ["zone", "map", "entered", "left", "mirage", "time_s",
                  "kills", "my_kills", "soul_crystals", "drops", "xp"]
        rows = [[z["display_name"], z["map_name"], z["entered_at"],
                 z["left_at"] or "", "yes" if z.get("is_mirage") else "no",
                 _zone_seconds(z["entered_at"], z["left_at"]) or "",
                 z["kills"], z["my_kills"], z["soul_crystals"],
                 z["drops"], z["xp"]] for z in zones]
    elif table == "kills":
        kills = db.recent_kills(session_id, _DETAIL_LIMIT, names=names)
        header = ["time", "enemy_id", "mob_id", "name", "mine", "mighty"]
        rows = [[r["ts"], r["enemy_id"], r["mob_id"] or "", r["name"],
                 "yes" if r["is_mine"] else "no",
                 "yes" if r["is_mighty"] else "no"] for r in kills]
    elif table == "drops":
        drops = db.recent_drops(session_id, _DETAIL_LIMIT, names=names,
                                local_account=acct)
        header = ["time", "drop_id", "item_id", "item", "qty",
                  "owner", "status"]
        rows = [[r["ts"], r["drop_id"],
                 r["item_id"] if r["item_id"] is not None else "",
                 ("Soul Crystal" if r["item_id"] == 0
                  else r.get("item_name") or
                  (f"Item #{r['item_id']}"
                   if r["item_id"] is not None else "")),
                 r["amount"] or "", _owner(r["belongs_to"], acct),
                 _status(r, acct)] for r in drops]
    else:
        try:
            notes = db.recent_spawn_notifications(session_id, _DETAIL_LIMIT)
        except Exception:
            notes = []
        mighties = [n for n in notes
                    if str(n.get("rarity", "")) == "mighty"]
        header = ["time", "notice"]
        rows = [[n.get("ts", ""), str(n.get("name", "?"))]
                for n in mighties]

    p = dest / f"{stem}_{table}.csv"
    _write(p, header, rows)
    return p


def export_session(db, names, session_id: int,
                   base: str | Path) -> list[Path]:
    """One file per table next to `base` (stem suffixed). Returns paths."""
    return [export_session_table(db, names, session_id, t, base)
            for t in TABLES]
