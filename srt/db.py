"""SQLite persistence for session events.

The schema mirrors the original Go storage layer. All writes go through a
single shared connection guarded by a re-entrant lock — sqlite3 connections
are not safe to share across threads without serialization.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started TEXT NOT NULL,
        ended TEXT,
        current_zone TEXT,
        local_account_id INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS kills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        enemy_id INTEGER NOT NULL,
        mob_id INTEGER,
        is_mine INTEGER NOT NULL DEFAULT 0,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )""",
    """CREATE TABLE IF NOT EXISTS drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        drop_id INTEGER NOT NULL,
        item_id INTEGER,
        mob_id INTEGER,
        amount INTEGER,
        color_r INTEGER,
        color_g INTEGER,
        color_b INTEGER,
        is_shiny INTEGER,
        belongs_to INTEGER,
        picked_up_by INTEGER,
        picked_up_at TEXT,
        destroyed INTEGER NOT NULL DEFAULT 0,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )""",
    """CREATE TABLE IF NOT EXISTS deaths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        exp_lost INTEGER NOT NULL DEFAULT 0,
        money_lost INTEGER NOT NULL DEFAULT 0,
        items_lost INTEGER NOT NULL DEFAULT 0,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )""",
    """CREATE TABLE IF NOT EXISTS xp_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        xp_gained INTEGER NOT NULL,
        bonus_party INTEGER,
        level INTEGER,
        is_level_up INTEGER,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )""",
    """CREATE TABLE IF NOT EXISTS damage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        enemy_id INTEGER NOT NULL,
        attacker INTEGER NOT NULL,
        damage INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )""",
    """CREATE TABLE IF NOT EXISTS spawn_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        rarity TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        event_type TEXT NOT NULL,
        data TEXT NOT NULL,
        timestamp TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS zone_visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        map_name TEXT NOT NULL,
        display_name TEXT,
        entered_at TEXT NOT NULL,
        left_at TEXT,
        is_mirage INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )""",
    """CREATE VIEW IF NOT EXISTS zone_stats AS
        SELECT
            zv.id AS zone_visits_id,
            zv.session_id,
            zv.map_name,
            COALESCE(zv.display_name, zv.map_name) AS display_name,
            zv.entered_at,
            zv.left_at,
            (SELECT COUNT(*) FROM kills k WHERE k.session_id = zv.session_id AND k.timestamp >= zv.entered_at
                AND (zv.left_at IS NULL OR k.timestamp < zv.left_at)) AS kills,
            (SELECT COALESCE(SUM(COALESCE(d.amount, 1)), 0) FROM drops d WHERE d.session_id = zv.session_id AND d.item_id = 0
                AND d.timestamp >= zv.entered_at
                AND (zv.left_at IS NULL OR d.timestamp < zv.left_at)) AS soul_crystals,
            (SELECT COUNT(*) FROM drops d WHERE d.session_id = zv.session_id
                AND d.timestamp >= zv.entered_at
                AND (zv.left_at IS NULL OR d.timestamp < zv.left_at)) AS drops,
            (SELECT COALESCE(SUM(xp_gained), 0) FROM xp_events x WHERE x.session_id = zv.session_id
                AND x.timestamp >= zv.entered_at
                AND (zv.left_at IS NULL OR x.timestamp < zv.left_at)) AS xp,
            (SELECT COUNT(*) FROM kills k WHERE k.session_id = zv.session_id AND k.is_mine = 1
                AND k.timestamp >= zv.entered_at
                AND (zv.left_at IS NULL OR k.timestamp < zv.left_at)) AS my_kills,
            (SELECT COUNT(*) FROM drops d WHERE d.session_id = zv.session_id
                AND d.belongs_to = (SELECT local_account_id FROM sessions s WHERE s.id = zv.session_id)
                AND d.timestamp >= zv.entered_at
                AND (zv.left_at IS NULL OR d.timestamp < zv.left_at)) AS my_drops,
            (SELECT COALESCE(SUM(COALESCE(d.amount, 1)), 0) FROM drops d WHERE d.session_id = zv.session_id
                AND d.item_id = 0
                AND d.belongs_to = (SELECT local_account_id FROM sessions s WHERE s.id = zv.session_id)
                AND d.timestamp >= zv.entered_at
                AND (zv.left_at IS NULL OR d.timestamp < zv.left_at)) AS my_soul_crystals
        FROM zone_visits zv
    """,
    """CREATE INDEX IF NOT EXISTS idx_kills_session_ts ON kills(session_id, timestamp)""",
    """CREATE INDEX IF NOT EXISTS idx_drops_session_ts ON drops(session_id, timestamp)""",
    """CREATE INDEX IF NOT EXISTS idx_xp_session_ts ON xp_events(session_id, timestamp)""",
    """CREATE INDEX IF NOT EXISTS idx_zones_session ON zone_visits(session_id)""",
    """CREATE INDEX IF NOT EXISTS idx_damage_session_enemy ON damage(session_id, enemy_id)""",
    """CREATE INDEX IF NOT EXISTS idx_deaths_session_ts ON deaths(session_id, timestamp)""",
    """CREATE INDEX IF NOT EXISTS idx_drops_session_drop ON drops(session_id, drop_id)""",
]

# Tables wiped by reset_session (everything per-session, but not the session row).
_RESET_TABLES = ("kills", "drops", "xp_events", "spawn_notifications", "zone_visits", "events", "damage", "deaths")


def _span_seconds(started: str | None, ended: str | None) -> float:
    """Seconds from started to ended-or-now; 0.0 when unusable."""
    try:
        t0 = datetime.fromisoformat(started) if started else None
    except Exception:
        t0 = None
    if t0 is None:
        return 0.0
    try:
        t1 = datetime.fromisoformat(ended) if ended else None
    except Exception:
        t1 = None
    secs = ((t1 or datetime.now()) - t0).total_seconds()
    return secs if secs > 0 else 0.0

# SQLite INTEGER is a signed 64-bit int. Python ints are unbounded, so
# clamp every integer at the DB boundary so no INSERT can ever overflow.
_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1


def _i64(value: int | None) -> int | None:
    """Coerce an int into SQLite INTEGER range (None passes through)."""
    if value is None:
        return None
    value = int(value)
    if value < _I64_MIN:
        return _I64_MIN
    if value > _I64_MAX:
        return _I64_MAX
    return value


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            for s in SCHEMA:
                cur.execute(s)
            # Additive migrations for DBs created before the columns
            # were added to the CREATE TABLE. CREATE TABLE IF NOT EXISTS
            # is a no-op when the table already exists, so we have to
            # ALTER. entered_at is added with a default of '' so old
            # rows (no timestamp) don't fail the view's NULL-unsafe
            # comparisons like k.timestamp >= zv.entered_at.
            self._ensure_column("zone_visits", "entered_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("zone_visits", "left_at", "TEXT")
            self._ensure_column("sessions", "local_account_id", "INTEGER")
            self._ensure_column("kills", "is_mine", "INTEGER NOT NULL DEFAULT 0")
            # Notable-kill flag for kills whose spawn stood out; old rows
            # default to normal.
            self._ensure_column("kills", "is_mighty", "INTEGER NOT NULL DEFAULT 0")
            # Every other column the read/write paths reference, for DBs
            # created before that column existed (same "no such column"
            # crash as entered_at/local_account_id before them — e.g.
            # sessions.current_zone broke past_sessions and the session
            # detail dialog). All nullable so ALTER never fails on old rows.
            self._ensure_column("sessions", "current_zone", "TEXT")
            self._ensure_column("kills", "mob_id", "INTEGER")
            self._ensure_column("drops", "item_id", "INTEGER")
            self._ensure_column("drops", "mob_id", "INTEGER")
            self._ensure_column("drops", "amount", "INTEGER")
            self._ensure_column("drops", "belongs_to", "INTEGER")
            # Drop lifecycle: who took the drop and when, or whether it
            # is gone.
            self._ensure_column("drops", "picked_up_by", "INTEGER")
            self._ensure_column("drops", "picked_up_at", "TEXT")
            self._ensure_column("drops", "destroyed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("xp_events", "level", "INTEGER")
            self._ensure_column("xp_events", "is_level_up", "INTEGER")
            self._ensure_column("xp_events", "bonus_party", "INTEGER")
            self._ensure_column("zone_visits", "display_name", "TEXT")
            self._ensure_column("zone_visits", "is_mirage", "INTEGER NOT NULL DEFAULT 0")
            # Old builds created zone_visits with a NOT NULL `timestamp`
            # column; current code writes entered_at/left_at instead, so
            # every zone insert fails on the legacy constraint until the
            # column is gone. Backfill entered_at from it first.
            self._drop_legacy_zone_timestamp()
            # The view was created with CREATE VIEW IF NOT EXISTS, so
            # if the DB predates the entered_at/left_at columns being
            # referenced inside the view, the view's stored definition
            # is the old one. Drop and recreate to pick up the new
            # columns.
            cur.execute("DROP VIEW IF EXISTS zone_stats")
            for s in SCHEMA:
                cur.execute(s)
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cur = self._conn.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in cur.fetchall()}
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _drop_legacy_zone_timestamp(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(zone_visits)")
        cols = {row[1] for row in cur.fetchall()}
        if "timestamp" not in cols:
            return
        self._conn.execute(
            "UPDATE zone_visits SET entered_at = timestamp "
            "WHERE (entered_at IS NULL OR entered_at = '') "
            "AND timestamp IS NOT NULL"
        )
        try:
            self._conn.execute("ALTER TABLE zone_visits DROP COLUMN timestamp")
        except sqlite3.OperationalError:
            # Very old sqlite without DROP COLUMN: rebuild the table.
            self._conn.execute(
                """CREATE TABLE zone_visits_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    map_name TEXT NOT NULL,
                    display_name TEXT,
                    entered_at TEXT NOT NULL,
                    left_at TEXT,
                    is_mirage INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                )"""
            )
            self._conn.execute(
                "INSERT INTO zone_visits_new "
                "(id, session_id, map_name, display_name, entered_at, left_at, is_mirage) "
                "SELECT id, session_id, map_name, display_name, entered_at, left_at, "
                "COALESCE(is_mirage, 0) "
                "FROM zone_visits"
            )
            self._conn.execute("DROP TABLE zone_visits")
            self._conn.execute("ALTER TABLE zone_visits_new RENAME TO zone_visits")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- session lifecycle ---
    def start_session(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (started) VALUES (?)",
                (datetime.now().isoformat(timespec="seconds"),),
            )
            self._conn.commit()
            return cur.lastrowid

    def end_session(self, session_id: int) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended = ? WHERE id = ?",
                (ts, session_id),
            )
            # Close any still-open zone visit so per-zone aggregates
            # have a precise end bound.
            self._conn.execute(
                "UPDATE zone_visits SET left_at = ? "
                "WHERE session_id = ? AND left_at IS NULL",
                (ts, session_id),
            )
            self._conn.commit()

    def reset_session(self, session_id: int) -> None:
        """Wipe all event rows for a session, keep the session itself."""
        with self._lock:
            for table in _RESET_TABLES:
                # Handle zone_visits separately - it has a different schema
                if table == 'zone_visits':
                    continue  # zone_visits rows are kept for history; reset doesn't wipe
                self._conn.execute(
                    f"DELETE FROM {table} WHERE session_id = ?",
                    (session_id,),
                )
            self._conn.commit()

    def insert_kill(self, session_id: int, enemy_id: int, mob_id: int | None,
                    ts: str, is_mine: bool = False,
                    is_mighty: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kills (session_id, enemy_id, mob_id, is_mine, is_mighty, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, enemy_id, mob_id, int(is_mine), int(is_mighty), ts),
            )
            self._conn.commit()

    def insert_damage(self, session_id: int, enemy_id: int, attacker: int,
                       damage: int, ts: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO damage (session_id, enemy_id, attacker, damage, timestamp) "
                "VALUES (?,?,?,?,?)",
                (session_id, _i64(enemy_id), _i64(attacker), _i64(damage), ts),
            )
            self._conn.commit()

    def damaged_by(self, session_id: int, enemy_id: int, account_id: int) -> bool:
        """True if the given attacker damaged the given enemy this session."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM damage WHERE session_id = ? AND enemy_id = ? "
                "AND attacker = ? LIMIT 1",
                (session_id, enemy_id, account_id),
            )
            return cur.fetchone() is not None

    def set_session_account(self, session_id: int, account_id: int) -> None:
        """Remember who the local player is for this session.
        First value wins."""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET local_account_id = COALESCE(local_account_id, ?) "
                "WHERE id = ?",
                (account_id, session_id),
            )
            self._conn.commit()

    def session_account(self, session_id: int) -> int | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT local_account_id FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def backfill_kill_attribution(self, session_id: int, account_id: int) -> None:
        """Fix up kill attribution for kills recorded before identity arrived."""
        with self._lock:
            self._conn.execute(
                "UPDATE kills SET is_mine = 1 WHERE session_id = ? AND is_mine = 0 "
                "AND EXISTS (SELECT 1 FROM damage d WHERE d.session_id = kills.session_id "
                "AND d.enemy_id = kills.enemy_id AND d.attacker = ?)",
                (session_id, account_id),
            )
            self._conn.commit()

    def wipe(self) -> None:
        """Delete every row in every table, sessions included. Schema stays."""
        with self._lock:
            for table in ("damage", "kills", "drops", "xp_events",
                          "spawn_notifications", "zone_visits", "events",
                          "deaths", "sessions"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('sessions','drops','xp_events','spawn_notifications',"
                "'zone_visits','events','damage','deaths')"
            )
            self._conn.commit()
            self._conn.execute("VACUUM")

    def insert_drop(self, session_id: int, drop_id: int, item_id: int | None,
                     mob_id: int | None, amount: int | None,
                     belongs_to: int | None, ts: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO drops (session_id, drop_id, item_id, mob_id, amount, belongs_to, timestamp)
                   VALUES (?,?,?,?,?,?,?)""",
                (session_id, _i64(drop_id), _i64(item_id), _i64(mob_id),
                 _i64(amount), _i64(belongs_to), ts),
            )
            self._conn.commit()

    def insert_death(self, session_id: int, exp_lost: int, money_lost: int,
                     items_lost: int, ts: str) -> None:
        """Record a local-player death."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO deaths (session_id, exp_lost, money_lost, items_lost, timestamp)"
                " VALUES (?,?,?,?,?)",
                (session_id, _i64(exp_lost), _i64(money_lost), _i64(items_lost), ts),
            )
            self._conn.commit()

    def mark_drop_pickup(self, session_id: int, drop_id: int,
                         picker: int | None, ts: str) -> None:
        """Attribute a drop to whoever picked it up."""
        with self._lock:
            self._conn.execute(
                """UPDATE drops SET picked_up_by = ?, picked_up_at = ?
                   WHERE id = (SELECT id FROM drops
                               WHERE session_id = ? AND drop_id = ?
                                 AND picked_up_by IS NULL
                               ORDER BY id DESC LIMIT 1)""",
                (picker, ts, session_id, drop_id),
            )
            self._conn.commit()

    def mark_drop_destroyed(self, session_id: int, drop_id: int) -> None:
        """Flag a drop as gone without being picked up."""
        with self._lock:
            self._conn.execute(
                """UPDATE drops SET destroyed = 1
                   WHERE id = (SELECT id FROM drops
                               WHERE session_id = ? AND drop_id = ?
                                 AND picked_up_by IS NULL AND destroyed = 0
                               ORDER BY id DESC LIMIT 1)""",
                (session_id, drop_id),
            )
            self._conn.commit()

    def deaths_for_session(self, session_id: int) -> list[dict]:
        """Every recorded death in a session, oldest first."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT exp_lost, money_lost, items_lost, timestamp
                   FROM deaths WHERE session_id = ? ORDER BY id""",
                (session_id,),
            )
            return [
                {"exp_lost": r[0] or 0, "money_lost": r[1] or 0,
                 "items_lost": r[2] or 0, "ts": r[3]}
                for r in cur.fetchall()
            ]

    def insert_xp(self, session_id: int, xp: int, level: int | None,
                   is_level_up: bool, ts: str) -> None:
        # Level is validated to the legit range; out-of-range values
        # are stored without a level instead. XP value is kept as-is.
        if level is not None:
            try:
                level = int(level)
            except (TypeError, ValueError):
                level = None
            if level is not None and not 1 <= level <= 100:
                level = None
        with self._lock:
            self._conn.execute(
                "INSERT INTO xp_events (session_id, xp_gained, level, is_level_up, timestamp) VALUES (?,?,?,?,?)",
                (session_id, _i64(xp), _i64(level), int(is_level_up), ts),
            )
            self._conn.commit()

    def insert_spawn_notification(self, session_id: int, name: str, rarity: str, ts: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO spawn_notifications (session_id, name, rarity, timestamp) VALUES (?,?,?,?)",
                (session_id, name, rarity, ts),
            )
            self._conn.commit()

    def recent_spawn_notifications(self, session_id: int, limit: int = 50) -> list[dict]:
        """Most recent spawn notifications for a session, newest first."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT name, rarity, timestamp FROM spawn_notifications
                   WHERE session_id = ? ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            )
            return [
                {"name": r[0], "rarity": r[1], "ts": r[2]}
                for r in cur.fetchall()
            ]

    def insert_zone_visit(self, session_id: int, map_name: str, display_name: str, ts: str) -> None:
        # Close the previous zone visit (if any) for this session by stamping
        # left_at on the most recent row with no left_at yet.
        with self._lock:
            self._conn.execute(
                "UPDATE zone_visits SET left_at = ? "
                "WHERE session_id = ? AND left_at IS NULL AND map_name != ?",
                (ts, session_id, map_name),
            )
            self._conn.execute(
                "INSERT INTO zone_visits (session_id, map_name, display_name, entered_at) VALUES (?,?,?,?)",
                (session_id, map_name, display_name, ts),
            )
            self._conn.execute(
                "UPDATE sessions SET current_zone = ? WHERE id = ?",
                (map_name, session_id),
            )
            self._conn.commit()

    def mark_current_visit_mirage(self, session_id: int, ts: str) -> None:
        """Flag the currently open zone visit as a mirage run. Targets
        the latest still-open visit; a no-op when none is open."""
        with self._lock:
            self._conn.execute(
                """UPDATE zone_visits SET is_mirage = 1
                   WHERE id = (SELECT id FROM zone_visits
                               WHERE session_id = ? AND left_at IS NULL
                               ORDER BY id DESC LIMIT 1)""",
                (session_id,),
            )
            self._conn.commit()

    def close_open_zones(self, session_id: int, ts: str) -> None:
        """Stamp left_at on any still-open zone visits (called on session end)."""
        with self._lock:
            self._conn.execute(
                "UPDATE zone_visits SET left_at = ? "
                "WHERE session_id = ? AND left_at IS NULL",
                (ts, session_id),
            )
            self._conn.commit()

    def open_visit_id(self, session_id: int) -> int | None:
        """Id of the latest still-open visit, or None when all closed."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM zone_visits WHERE session_id = ? "
                "AND left_at IS NULL ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def visit_stats(self, session_id: int, visit_id: int) -> dict | None:
        """Aggregates scoped to one zone visit's time window.

        Returns kills, my_kills, drops, my_drops, sc_picked, sc_unpicked,
        xp, deaths, xp_lost, damage_mine, damage_total plus elapsed_s
        (entered_at to left_at, or to now while open), dps, dps_mine
        and xp_hr. None for an unknown visit."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT map_name, COALESCE(display_name, map_name), "
                "entered_at, left_at, is_mirage FROM zone_visits "
                "WHERE id = ? AND session_id = ?",
                (visit_id, session_id),
            )
            vrow = cur.fetchone()
            if vrow is None:
                return None
            entered_at, left_at = vrow[2], vrow[3]
            acct = self.session_account(session_id)
            mine_acct = acct if acct is not None else -1

            def one(sql: str, params: tuple) -> int:
                c = self._conn.execute(sql, params)
                return int(c.fetchone()[0] or 0)

            w = "timestamp >= ? AND (? IS NULL OR timestamp < ?)"
            win = (entered_at, left_at, left_at)
            kills = one(f"SELECT COUNT(*) FROM kills WHERE session_id = ? AND {w}",
                        (session_id,) + win)
            my_kills = one(
                "SELECT COUNT(*) FROM kills WHERE session_id = ? "
                f"AND is_mine = 1 AND {w}", (session_id,) + win)
            drops = one(f"SELECT COUNT(*) FROM drops WHERE session_id = ? AND {w}",
                        (session_id,) + win)
            my_drops = one(
                "SELECT COUNT(*) FROM drops WHERE session_id = ? "
                f"AND belongs_to = ? AND {w}", (session_id, mine_acct) + win)
            sc_picked = one(
                "SELECT COALESCE(SUM(COALESCE(amount, 1)), 0) FROM drops "
                f"WHERE session_id = ? AND item_id = 0 AND picked_up_by = ? AND {w}",
                (session_id, mine_acct) + win)
            sc_unpicked = one(
                "SELECT COALESCE(SUM(COALESCE(amount, 1)), 0) FROM drops "
                f"WHERE session_id = ? AND item_id = 0 AND belongs_to = ? "
                f"AND (picked_up_by IS NULL OR picked_up_by != ?) AND {w}",
                (session_id, mine_acct, mine_acct) + win)
            xp = one(
                "SELECT COALESCE(SUM(xp_gained), 0) FROM xp_events "
                f"WHERE session_id = ? AND {w}", (session_id,) + win)
            deaths = one(f"SELECT COUNT(*) FROM deaths WHERE session_id = ? AND {w}",
                         (session_id,) + win)
            xp_lost = one(
                "SELECT COALESCE(SUM(exp_lost), 0) FROM deaths "
                f"WHERE session_id = ? AND {w}", (session_id,) + win)
            damage_total = one(
                "SELECT COALESCE(SUM(damage), 0) FROM damage "
                f"WHERE session_id = ? AND {w}", (session_id,) + win)
            damage_mine = one(
                "SELECT COALESCE(SUM(damage), 0) FROM damage "
                f"WHERE session_id = ? AND attacker = ? AND {w}",
                (session_id, mine_acct) + win)
            elapsed_s = _span_seconds(entered_at, left_at)
            hrs = elapsed_s / 3600.0
            return {
                "visit_id": visit_id,
                "map_name": vrow[0], "display_name": vrow[1],
                "entered_at": entered_at, "left_at": left_at,
                "is_mirage": bool(vrow[4]),
                "kills": kills, "my_kills": my_kills,
                "drops": drops, "my_drops": my_drops,
                "sc_picked": sc_picked, "sc_unpicked": sc_unpicked,
                "xp": xp, "deaths": deaths, "xp_lost": xp_lost,
                "damage_mine": damage_mine, "damage_total": damage_total,
                "elapsed_s": elapsed_s,
                "dps": (damage_total / elapsed_s) if elapsed_s > 0 else 0.0,
                "dps_mine": (damage_mine / elapsed_s) if elapsed_s > 0 else 0.0,
                "xp_hr": (xp / hrs) if hrs > 0 else 0.0,
            }

    def session_rates(self, session_id: int) -> dict:
        """Session-wide damage/xp rates over the session span
        (started to ended, or to now while ongoing)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT started, ended, local_account_id FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            started = row[0] if row else None
            ended = row[1] if row and len(row) > 1 else None
            acct = row[2] if row and len(row) > 2 else None
            mine_acct = acct if acct is not None else -1
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(damage), 0) FROM damage WHERE session_id = ?",
                (session_id,),
            )
            damage_total = int(cur.fetchone()[0] or 0)
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(damage), 0) FROM damage "
                "WHERE session_id = ? AND attacker = ?",
                (session_id, mine_acct),
            )
            damage_mine = int(cur.fetchone()[0] or 0)
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(xp_gained), 0) FROM xp_events WHERE session_id = ?",
                (session_id,),
            )
            xp = int(cur.fetchone()[0] or 0)
            elapsed_s = _span_seconds(started, ended)
            hrs = elapsed_s / 3600.0
            return {
                "damage_mine": damage_mine, "damage_total": damage_total,
                "dps": (damage_total / elapsed_s) if elapsed_s > 0 else 0.0,
                "dps_mine": (damage_mine / elapsed_s) if elapsed_s > 0 else 0.0,
                "xp": xp, "xp_hr": (xp / hrs) if hrs > 0 else 0.0,
                "elapsed_s": elapsed_s,
            }

    # --- read paths ---
    def summary(self, session_id: int) -> dict:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM kills WHERE session_id = ?", (session_id,))
            kills = cur.fetchone()[0]
            # Soul crystals are a quantity (SUM of amount), not a count of
            # drop events.
            cur.execute("SELECT COALESCE(SUM(COALESCE(amount, 1)), 0) FROM drops "
                        "WHERE session_id = ? AND item_id = 0", (session_id,))
            sc = cur.fetchone()[0]
            cur.execute(
                "SELECT COALESCE(SUM(COALESCE(amount, 1)), 0) FROM drops "
                "WHERE session_id = ? AND item_id = 0 AND belongs_to = "
                "(SELECT local_account_id FROM sessions WHERE id = ?)",
                (session_id, session_id),
            )
            my_sc = cur.fetchone()[0]
            cur.execute("SELECT local_account_id FROM sessions WHERE id = ?",
                        (session_id,))
            acct_row = cur.fetchone()
            local_acct = acct_row[0] if acct_row else None
            sc_pick = self.sc_totals(session_id, local_acct)
            cur.execute("SELECT COALESCE(SUM(xp_gained), 0) FROM xp_events WHERE session_id = ?", (session_id,))
            xp = cur.fetchone()[0] or 0
            cur.execute("SELECT COALESCE(MAX(CASE WHEN level BETWEEN 1 AND 100 THEN level ELSE 0 END), 0) FROM xp_events WHERE session_id = ?", (session_id,))
            level = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM drops WHERE session_id = ?", (session_id,))
            drops = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM kills WHERE session_id = ? AND is_mine = 1",
                        (session_id,))
            my_kills = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM kills WHERE session_id = ? AND is_mighty = 1",
                        (session_id,))
            mighty_kills = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM drops WHERE session_id = ? AND belongs_to = "
                "(SELECT local_account_id FROM sessions WHERE id = ?)",
                (session_id, session_id),
            )
            my_drops = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM drops WHERE session_id = ? AND picked_up_by = "
                "(SELECT local_account_id FROM sessions WHERE id = ?)",
                (session_id, session_id),
            )
            my_pickups = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(exp_lost), 0), "
                "COALESCE(SUM(money_lost), 0), COALESCE(SUM(items_lost), 0) "
                "FROM deaths WHERE session_id = ?",
                (session_id,),
            )
            drow = cur.fetchone()
            deaths, xp_lost, money_lost, items_lost = (
                drow[0], int(drow[1]), int(drow[2]), int(drow[3]))
            cur.execute("SELECT current_zone FROM sessions WHERE id = ?",
                        (session_id,))
            row = cur.fetchone()
            current_zone = row[0] if row else None
            cur.execute(
                "SELECT display_name FROM zone_visits WHERE session_id = ? "
                "ORDER BY entered_at DESC LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
            # Prefer the human-readable zone name; fall back to the raw
            # map id stored on the session, else "no zone seen yet".
            zone_display = (row[0] if row and row[0] else None) or current_zone
            cur.execute(
                "SELECT COALESCE(SUM(damage), 0) FROM damage WHERE session_id = ?",
                (session_id,),
            )
            damage_total = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COALESCE(SUM(damage), 0) FROM damage "
                "WHERE session_id = ? AND attacker = ?",
                (session_id, local_acct if local_acct is not None else -1),
            )
            damage_mine = int(cur.fetchone()[0] or 0)
            open_id = self.open_visit_id(session_id)
            visit = self.visit_stats(session_id, open_id) if open_id else None
            return {
                "session_id": session_id,
                "kills": kills,
                "my_kills": my_kills,
                "mighty_kills": mighty_kills,
                "soul_crystals": sc,
                "my_soul_crystals": my_sc,
                "sc_picked": sc_pick["picked"],
                "sc_unpicked": sc_pick["unpicked"],
                "xp": int(xp),
                "level": int(level),
                "drops": drops,
                "my_drops": my_drops,
                "my_pickups": my_pickups,
                "deaths": deaths,
                "xp_lost": xp_lost,
                "money_lost": money_lost,
                "items_lost": items_lost,
                "damage_mine": damage_mine,
                "damage_total": damage_total,
                "visit": visit,
                "current_zone": zone_display,
            }

    def sc_totals(self, session_id: int,
                    local_account_id: int | None) -> dict:
        """Soul-crystal running totals by amount, local only."""
        if local_account_id is None:
            return {"picked": 0, "unpicked": 0}
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(COALESCE(amount, 1)), 0) FROM drops "
                "WHERE session_id = ? AND item_id = 0 AND picked_up_by = ?",
                (session_id, local_account_id),
            )
            picked = cur.fetchone()[0] or 0
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(COALESCE(amount, 1)), 0) FROM drops "
                "WHERE session_id = ? AND item_id = 0 AND belongs_to = ? "
                "AND (picked_up_by IS NULL OR picked_up_by != ?)",
                (session_id, local_account_id, local_account_id),
            )
            unpicked = cur.fetchone()[0] or 0
            return {"picked": int(picked), "unpicked": int(unpicked)}

    def zone_stats(self, session_id: int) -> list[dict]:
        """Per-zone aggregated stats for a session (kills/sc/drops/xp + time spent)."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT zv.map_name,
                          COALESCE(MAX(zv.display_name), zv.map_name) AS display_name,
                          zv.entered_at,
                          zv.left_at,
                          zv.is_mirage,
                          zv.id,
                          zs.kills,
                          zs.soul_crystals,
                          zs.drops,
                          zs.xp,
                          zs.my_kills,
                          zs.my_drops,
                          zs.my_soul_crystals
                   FROM zone_visits zv
                   LEFT JOIN zone_stats zs ON zs.zone_visits_id = zv.id
                   WHERE zv.session_id = ?
                   GROUP BY zv.id
                   ORDER BY zv.entered_at""",
                (session_id,),
            )
            return [{
                "map_name": r[0],
                "display_name": r[1],
                "entered_at": r[2],
                "left_at": r[3],
                "is_mirage": bool(r[4]),
                "id": r[5],
                "kills": r[6] or 0,
                "soul_crystals": r[7] or 0,
                "drops": r[8] or 0,
                "xp": r[9] or 0,
                "my_kills": r[10] or 0,
                "my_drops": r[11] or 0,
                "my_soul_crystals": r[12] or 0,
            } for r in cur.fetchall()]

    def past_sessions(self, limit: int = 50) -> list[dict]:
        """All sessions, newest first, with summary stats."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT s.id, s.started, s.ended, s.current_zone,
                          (SELECT COUNT(*) FROM kills WHERE session_id = s.id) AS kills,
                          (SELECT COUNT(*) FROM kills WHERE session_id = s.id AND is_mine = 1) AS my_kills,
                          (SELECT COALESCE(SUM(COALESCE(amount, 1)), 0) FROM drops WHERE session_id = s.id AND item_id = 0) AS sc,
                          (SELECT COALESCE(SUM(COALESCE(amount, 1)), 0) FROM drops WHERE session_id = s.id
                           AND item_id = 0 AND belongs_to = s.local_account_id) AS my_sc,
                          (SELECT COALESCE(SUM(xp_gained), 0) FROM xp_events WHERE session_id = s.id) AS xp,
                           (SELECT COALESCE(MAX(CASE WHEN level BETWEEN 1 AND 100 THEN level ELSE 0 END), 0) FROM xp_events WHERE session_id = s.id) AS lvl,
                          (SELECT COUNT(*) FROM drops WHERE session_id = s.id) AS drops,
                          (SELECT COUNT(*) FROM drops WHERE session_id = s.id
                           AND belongs_to = s.local_account_id) AS my_drops
                   FROM sessions s
                   ORDER BY s.id DESC
                   LIMIT ?""",
                (limit,),
            )
            return [{
                "id": r[0], "started": r[1], "ended": r[2], "current_zone": r[3],
                "kills": r[4], "my_kills": r[5] or 0,
                "soul_crystals": r[6] or 0, "my_soul_crystals": r[7] or 0,
                "xp": r[8] or 0,
                "level": r[9] or 0, "drops": r[10], "my_drops": r[11] or 0,
            } for r in cur.fetchall()]

    def session_zone_timeline(self, session_id: int) -> list[dict]:
        """Zone visits for a session, with start/end timestamps."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT id, map_name, COALESCE(display_name, map_name),
                          entered_at, left_at, is_mirage
                   FROM zone_visits WHERE session_id = ?
                   ORDER BY entered_at""",
                (session_id,),
            )
            return [{
                "id": r[0], "map_name": r[1], "display_name": r[2],
                "entered_at": r[3], "left_at": r[4],
                "is_mirage": bool(r[5]),
            } for r in cur.fetchall()]

    def cumulative_events(self, session_id: int, kind: str) -> list[tuple[str, float]]:
        """Raw (timestamp, delta) event stream for candle charts.

        kind: 'kills' (delta 1 per kill), 'xp' (xp_gained; level-up
        marker rows carry no gain and are skipped), 'drops' (1 per
        drop), 'sc' (amount of item_id 0 drops). Ordered oldest-first.
        Timestamps are the stored ISO strings; callers parse them."""
        with self._lock:
            if kind == "kills":
                cur = self._conn.execute(
                    "SELECT timestamp FROM kills WHERE session_id = ? "
                    "ORDER BY timestamp",
                    (session_id,),
                )
                return [(r[0], 1.0) for r in cur.fetchall() if r[0]]
            if kind == "xp":
                cur = self._conn.execute(
                    "SELECT timestamp, xp_gained FROM xp_events "
                    "WHERE session_id = ? AND COALESCE(xp_gained, 0) > 0 "
                    "ORDER BY timestamp",
                    (session_id,),
                )
                return [(r[0], float(r[1])) for r in cur.fetchall() if r[0]]
            if kind == "drops":
                cur = self._conn.execute(
                    "SELECT timestamp FROM drops WHERE session_id = ? "
                    "ORDER BY timestamp",
                    (session_id,),
                )
                return [(r[0], 1.0) for r in cur.fetchall() if r[0]]
            if kind == "sc":
                cur = self._conn.execute(
                    "SELECT timestamp, COALESCE(amount, 1) FROM drops "
                    "WHERE session_id = ? AND item_id = 0 ORDER BY timestamp",
                    (session_id,),
                )
                return [(r[0], float(r[1])) for r in cur.fetchall() if r[0]]
            if kind == "damage":
                cur = self._conn.execute(
                    "SELECT timestamp, damage FROM damage "
                    "WHERE session_id = ? ORDER BY timestamp",
                    (session_id,),
                )
                return [(r[0], float(r[1] or 0)) for r in cur.fetchall() if r[0]]
            raise ValueError(f"unknown candle kind: {kind!r}")

    def recent_kills(self, session_id: int, limit: int = 200, names=None) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT rowid AS id, enemy_id, mob_id, is_mine, is_mighty, timestamp
                   FROM kills WHERE session_id = ? ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            )
            return [
                {"id": r[0], "enemy_id": r[1], "mob_id": r[2], "ts": r[5],
                 "is_mine": bool(r[3]),
                 "is_mighty": bool(r[4]),
                 "name": (names.monster(r[2]) if (names and r[2]) else None)
                         or f"Mob#{r[2] or '?'}"}
                for r in cur.fetchall()
            ]

    def cumulative_mighty(self, session_id: int) -> list[tuple[str, int]]:
        """Time-ordered (timestamp, 1) stream of notable spawns.

        Mirrors the cumulative event streams: one entry per matching
        notice, oldest first. Timestamps are the stored ISO strings;
        callers parse them."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT timestamp FROM spawn_notifications "
                "WHERE session_id = ? AND rarity = 'mighty' "
                "ORDER BY timestamp",
                (session_id,),
            )
            return [(r[0], 1) for r in cur.fetchall() if r[0]]

    def recent_drops(self, session_id: int, limit: int = 200, names=None,
                     local_account: int | None = None) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT id, drop_id, item_id, mob_id, amount, belongs_to,
                          picked_up_by, picked_up_at, destroyed, timestamp
                   FROM drops WHERE session_id = ? ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            )
            return [{
                "id": r[0], "drop_id": r[1], "item_id": r[2], "mob_id": r[3],
                "amount": r[4], "belongs_to": r[5], "picked_up_by": r[6],
                "picked_up_at": r[7], "destroyed": bool(r[8]), "ts": r[9],
                "item_name": (names.item(r[2]) if (names and r[2] is not None) else None),
                "mine": (local_account is not None and r[5] == local_account),
                "picked_by_me": (local_account is not None and r[6] == local_account),
            } for r in cur.fetchall()]

    def sessions(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT s.id, s.started, s.ended,
                          (SELECT COUNT(*) FROM kills WHERE session_id = s.id) AS kills,
                          (SELECT COUNT(*) FROM kills WHERE session_id = s.id AND is_mine = 1) AS my_kills,
                          (SELECT COUNT(*) FROM drops WHERE session_id = s.id) AS drops,
                          (SELECT COUNT(*) FROM drops WHERE session_id = s.id
                           AND belongs_to = s.local_account_id) AS my_drops,
                          (SELECT COALESCE(SUM(damage), 0) FROM damage
                           WHERE session_id = s.id AND attacker = s.local_account_id) AS dmg_mine,
                          (SELECT COALESCE(SUM(damage), 0) FROM damage
                           WHERE session_id = s.id) AS dmg_total
                   FROM sessions s ORDER BY s.id DESC LIMIT 50"""
            )
            return [
                {"id": r[0], "started": r[1], "ended": r[2],
                 "kills": r[3], "my_kills": r[4] or 0,
                 "drops": r[5], "my_drops": r[6] or 0,
                 "damage_mine": int(r[7] or 0),
                 "damage_total": int(r[8] or 0)}
                for r in cur.fetchall()
            ]
