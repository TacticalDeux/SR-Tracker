"""Background thread that drains the DLL's event REMOVED and persists
each event into the database. Runs in a daemon thread so closing the app
doesn't require explicit shutdown — the thread is killed when the process
exits. `stop()` does a graceful drain + close.

The injected DLL writes framed events into a named REMOVED section
("Global\\REMOVED") that this process maps in via the
`TrackerDLL` ctypes wrapper. The consumer reads records directly from
the section; there is no log file.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from .db import Database
from .dll import TrackerDLL


# Event types that mean the game is actually doing something, as opposed
# to bridge chatter (net_seen, net_connect, net_close, handshake, shm_open,
# hook_install, dll_heartbeat) or ambient noise (character_spawn fires
# for any player walking by). The gameplay-silence watchdog watches
# these; heartbeats deliberately do NOT reset it.
_GAMEPLAY_TYPES = frozenset({
    "local_account",
    "enemy_spawn",
    "spawn_notification",
    "damage_dealt",
    "enemy_death",
    "drop_creation",
    "exp_update",
    "level_up",
    "zone_change",
})


class EventConsumer(QObject):
    # Emitted for every event seen, with the raw JSON string. The debug
    # console (dev mode only) hooks this to stream events into the UI.
    event_seen = Signal(str)
    # Emitted for consumer lifecycle messages (start, stop, errors).
    status = Signal(str)

    def __init__(self, dll: TrackerDLL, db: Database, parent: QObject | None = None):
        super().__init__(parent)
        self._dll = dll
        self._db = db
        self._running = False
        self._thread: threading.Thread | None = None
        self._session_id: int | None = None
        # Most recent session this process started, kept across stop()
        # so the UI can keep showing the last session's stats instead
        # of blanking everything the moment tracking stops.
        self._last_session_id: int | None = None
        # Per-run counters. Surfaced in the status bar so the user can
        # see at a glance whether events are flowing, and how many got
        # dropped as malformed.
        self._events_seen = 0
        self._events_parsed = 0
        self._events_dropped = 0
        # Attribution state, reset per session in start(): the account id
        # behind this client (packets), and a map from unique enemy
        # instance id -> mob type id built from spawn events so death
        # rows can carry a displayable monster name.
        self._local_account_id: int | None = None
        self._mob_of_enemy: dict[int, int] = {}
        # Monotonic timestamp of the last event handed to _handle.
        # The status bar shows "Ns since last event" from this so an
        # idle game (no packets) is distinguishable from a dead bridge.
        self._last_event_at = time.monotonic()
        # Monotonic timestamp of the last GAMEPLAY event (spawns,
        # deaths, damage, drops, xp, zones). Bridge chatter — net_seen,
        # handshake, dll_heartbeat — resets _last_event_at but not this, so
        # "DLL alive, hooks blind" (heartbeats flowing, nothing else) is
        # still reported as a stall instead of looking healthy.
        self._last_gameplay_at = time.monotonic()

    # --- public API ---
    @property
    def events_seen(self) -> int:
        return self._events_seen

    @property
    def events_parsed(self) -> int:
        return self._events_parsed

    @property
    def events_dropped(self) -> int:
        return self._events_dropped

    @property
    def running(self) -> bool:
        return self._running

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def last_session_id(self) -> int | None:
        """Most recent session id, surviving stop(). The UI falls back
        to this when no session is actively running."""
        return self._last_session_id

    def seconds_since_event(self) -> float:
        return time.monotonic() - self._last_event_at

    def seconds_since_gameplay(self) -> float:
        return time.monotonic() - self._last_gameplay_at

    def start(self) -> int:
        if self._running:
            return self._session_id or -1
        self._events_seen = 0
        self._events_parsed = 0
        self._events_dropped = 0
        self._local_account_id = None
        self._mob_of_enemy = {}
        self._last_event_at = time.monotonic()
        self._last_gameplay_at = time.monotonic()
        self._session_id = self._db.start_session()
        self._last_session_id = self._session_id
        self._running = True
        # Wait briefly for the injected DLL to finish installing. The
        # tracker usually starts after injection (see main_window.py
        # _toggle_tracking), so this returns immediately. If injection
        # is in flight or the DLL never became ready, surface a status
        # line so the Debug console shows what's happening instead of
        # looking frozen.
        if not self._dll.wait_for_install(timeout_s=3.0):
            self.status.emit(
                "DLL did not become ready within 3s — events will not flow"
            )
        self._thread = threading.Thread(target=self._loop, daemon=True, name="EventConsumer")
        self._thread.start()
        self.status.emit(
            f"session #{self._session_id} started — polling DLL REMOVED..."
        )
        return self._session_id

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._session_id is not None:
            self._db.end_session(self._session_id)
            self.status.emit(f"session #{self._session_id} ended")
            self._session_id = None

    # --- internals ---
    def _loop(self) -> None:
        # 5ms idle sleep is short enough for ~200 events/sec but cheap when
        # the buffer is empty.
        self.status.emit("consumer thread running")
        while self._running:
            try:
                if self._dll.has_event():
                    raw = self._dll.next_event()
                    if raw is not None:
                        self._handle(raw)
                    else:
                        # has_event was true but no record came out: the
                        # reader resynced (fell behind / corrupt / torn).
                        # Surface it — previously silent, which looked
                        # exactly like "tracking just stopped".
                        pop = getattr(self._dll, "pop_resync", None)
                        reason = pop() if callable(pop) else None
                        if reason:
                            self._events_dropped += 1
                            self.status.emit(f"REMOVED resync: {reason}")
                        time.sleep(0.005)
                else:
                    time.sleep(0.005)
            except Exception as e:
                # Never let a single bad frame kill the consumer, but make
                # it visible in the debug console instead of failing silent.
                self.status.emit(f"consumer error: {e!r}")
                time.sleep(0.1)

    def _handle(self, raw: str) -> None:
        # Stream the raw event out for the debug console. We do this
        # before parsing so malformed JSON is still visible in the log.
        self._events_seen += 1
        self._last_event_at = time.monotonic()
        self.event_seen.emit(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Surface the malformed payload in a more useful form than just
            # "[bad-json]". Include the first 80 bytes and a hex dump of
            # the same so the user can see whether the DLL is sending
            # text that *looks* like JSON but isn't, or just raw bytes.
            preview = raw[:80].encode("utf-8", errors="replace")
            hex_preview = preview.hex()
            self.status.emit(
                f"bad-json at pos {e.pos}: {raw[:60]!r}  hex={hex_preview}"
            )
            self._events_dropped += 1
            return
        sid = self._session_id
        if sid is None:
            return
        self._events_parsed += 1
        etype = data.get("type", "unknown")
        if etype in _GAMEPLAY_TYPES:
            self._last_gameplay_at = time.monotonic()
        ts = datetime.now().isoformat(timespec="milliseconds")

        if etype == "enemy_spawn":
            # Remember which mob type each unique enemy instance is, so
            # the later death row can carry a displayable monster name.
            try:
                self._mob_of_enemy[int(data.get("enemy_id", 0))] = int(
                    data.get("mob_id", 0))
            except (TypeError, ValueError):
                pass
        elif etype == "local_account":
            # packets: the account id behind this client. Drops carry
            # belongs_to and kills are credited via the damage table,
            # both keyed off this id.
            try:
                acct = int(data.get("account_id", 0))
            except (TypeError, ValueError):
                acct = 0
            if acct:
                self._local_account_id = acct
                self._db.set_session_account(sid, acct)
                self._db.backfill_kill_attribution(sid, acct)
        elif etype == "damage_dealt":
            # damage == 0xFFFFFFFF is the post-mortem deathblow sentinel,
            # not real damage — skip it so it can never grant kill credit.
            try:
                dmg = int(data.get("damage", 0))
                attacker = int(data.get("attacker", 0))
                target = int(data.get("enemy_id", 0))
            except (TypeError, ValueError):
                return
            if dmg == 0xFFFFFFFF or attacker == 0 or target == 0:
                return
            self._db.insert_damage(sid, target, attacker, dmg, ts)
        elif etype == "enemy_death":
            # Deaths carry no killer field: credit goes to the local
            # account only if it damaged this enemy instance beforehand.
            enemy_id = int(data.get("enemy_id", 0))
            mob_id = self._mob_of_enemy.get(enemy_id)
            mine = (
                self._local_account_id is not None
                and self._db.damaged_by(sid, enemy_id, self._local_account_id)
            )
            self._db.insert_kill(sid, enemy_id, mob_id, ts, is_mine=mine)
        elif etype == "drop_creation":
            # Drops carry no mob reference — attribution is belongs_to
            # vs the local account id (0xFFFFFFFF = unclaimed).
            self._db.insert_drop(
                sid,
                int(data.get("drop_id", 0)),
                int(data.get("item_id", 0)) if "item_id" in data else None,
                None,
                int(data.get("amount", 1)) if "amount" in data else None,
                int(data.get("belongs_to", 0)) if "belongs_to" in data else None,
                ts,
            )
        elif etype == "exp_update":
            self._db.insert_xp(
                sid,
                int(data.get("exp_gained", 0)),
                int(data.get("level", 0)) if "level" in data else None,
                False,
                ts,
            )
        elif etype == "level_up":
            self._db.insert_xp(sid, 0, int(data.get("level", 0)), True, ts)
        elif etype == "zone_change":
            self._db.insert_zone_visit(
                sid,
                str(data.get("map_name", "")),
                str(data.get("display_name", "")),
                ts,
            )
        elif etype == "spawn_notification":
            self._db.insert_spawn_notification(
                sid,
                str(data.get("name", "")),
                "boss",
                ts,
            )
