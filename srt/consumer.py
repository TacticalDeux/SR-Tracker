"""Background thread that drains the DLL's event buffer and persists
each event into the database. Runs in a daemon thread so closing the app
doesn't require explicit shutdown — the thread is killed when the process
exits. `stop()` does a graceful drain + close.

The injected DLL writes framed events into a named event buffer
that this process maps in via the
`TrackerDLL` ctypes wrapper. The consumer reads records directly from
the section; there is no log file.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from datetime import datetime
from statistics import median

from PySide6.QtCore import QObject, Signal

from .db import Database
from .dll import TrackerDLL


# Without any session traffic for this long, a running session reads
# as not connected (see is_connected).
_STALE_AFTER_S = 60.0


# Event types that mean the game is actually doing something, as opposed
# to bridge chatter (net_seen, net_connect, net_close, session_setup,
# key_rotation, shm_open, hook_install, dll_heartbeat) or ambient noise
# (character_spawn fires for any player walking by). The gameplay-silence watchdog watches
# these; heartbeats deliberately do NOT reset it.
# How long a portal sighting stays eligible for same-zone arrival
# correlation (seconds, monotonic clock).
_SIGHT_WINDOW_S = 10.0


# Notable-spawn outlier tuning. Per monster we remember the most recent
# spawn strengths (window), need a minimum number of sightings before
# judging, and flag a spawn whose strength reads at least this multiple
# of the running middle. Oversized spawns arrive only after minutes of
# grinding, so by then the baseline is dozens deep and reliable.
_HP_WINDOW = 50
_HP_MIN_SAMPLES = 3
_HP_OUTLIER_MULT = 3

# Notice dedupe: no matter how many arms agree on one arrival, it earns
# a single row per window (grinding gaps run to minutes, so one
# arrival never legitimately repeats inside it).
_NOTICE_DEDUPE_S = 60.0
# Burst guard: any notice at all suppresses a further row inside this
# short window, so a twin whose text cannot be matched to its sibling
# still cannot double-row.
_NOTICE_BURST_S = 10.0
# Retro-mark reach: how far back an id-less announcement may look for
# the spawn it names, and how many recent spawns are remembered.
_RETRO_MARK_S = 10.0
_RECENT_SPAWNS = 256
# Cap on arrivals held for retroactive judging (same-visit only;
# a new visit drops them with the baseline).
_PENDING_JUDGE = 256


def _normalize_notice_token(name: str) -> str:
    # Keying helper so twin reports of one arrival share a dedupe
    # key: strips the fixed surrounding template words from a full
    # sentence down to its subject token, and lowercases a plain
    # label to the same form. Falls back to the full text when no
    # subject survives.
    text = (name or "").strip()
    if not text:
        return ""
    low = text.lower()
    if "mighty" not in low:
        return low
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", low)
    stop = {
        "a", "an", "the", "mighty", "has", "have", "had", "is", "are",
        "was", "were", "be", "been", "appeared", "appears",
        "appearing", "spawned", "spawn", "arrived", "arrives",
        "arrival", "emerged", "here", "now", "wild",
    }
    kept = [t for t in cleaned.split() if t and t not in stop]
    if kept:
        return " ".join(kept)
    return low


_GAMEPLAY_TYPES = frozenset({
    "local_account",
    "enemy_spawn",
    "spawn_notification",
    "center_message",
    "damage_dealt",
    "enemy_death",
    "drop_creation",
    "pickup",
    "pickup_denied",
    "player_death",
    "exp_update",
    "level_up",
    "mirage_exit",
    "portal_sight",
    "zone_change",
})


class EventConsumer(QObject):
    # Emitted for every event seen, with the raw JSON string. The debug
    # console (dev mode only) hooks this to stream events into the UI.
    event_seen = Signal(str)
    # Emitted for consumer lifecycle messages (start, stop, errors).
    status = Signal(str)

    def __init__(self, dll: TrackerDLL, db: Database, parent: QObject | None = None,
                 names=None):
        super().__init__(parent)
        self._dll = dll
        self._db = db
        # Display-name tables for resolving monster names on notable
        # spawns. Optional so headless uses keep working without them.
        self._names = names
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
        # Attribution state, reset per session in start(): who the
        # local player is, and a map from unique enemy
        # instance id -> mob type id built from spawn events so death
        # rows can carry a displayable monster name.
        self._local_account_id: int | None = None
        # Channel-linkage gate, reset per session in start(): the player
        # id and the session setup must both arrive before anything
        # persists. Pre-link gameplay is discarded, not queued.
        self._link_player = False
        self._link_session = False
        self._mob_of_enemy: dict[int, int] = {}
        # Recent spawn strengths per monster, oldest first. Compared
        # against when a new spawn arrives to spot outliers; scoped
        # to the visit (a new arrival resets them) and the session.
        self._hp_by_mob: dict[int, deque] = {}
        # Notable enemy instances seen this visit. Fed at spawn time by
        # the paths that record a notice, consumed on the matching death
        # so the kill row carries the flag. Reset per visit and session.
        self._notable_enemy_ids: set[int] = set()
        # Arrivals held for retroactive judging: judged too early to
        # call at the time, re-judged once their baseline firms up.
        # Same-visit only; a new arrival drops them with the baseline.
        self._pending_judge: deque = deque(maxlen=_PENDING_JUDGE)
        # Last notice wall-time (monotonic) for the short cross-key
        # burst guard, a per-key log of last-write times for the long
        # dedupe window, and a bounded log of recent spawns for id-less
        # announcements to reach back over. The clock runs across
        # visits; the log does not. Both reset per session in start().
        self._last_notice_at = 0.0
        self._notice_at_by_key: dict[tuple[str, str], float] = {}
        self._recent_spawns: deque = deque(maxlen=_RECENT_SPAWNS)
        # Recent portal sightings as (monotonic-ts, text, cost), oldest
        # first. A free non-housing sighting shortly before a same-zone
        # arrival marks the new visit as a mirage run.
        self._portal_sights: deque = deque(maxlen=16)
        # Map name of the current visit, for same-zone arrival checks.
        self._current_zone: str | None = None
        # Id of the current open zone visit (None when all closed).
        # Refreshed whenever visits open or close.
        self._current_visit_id: int | None = None
        # Monotonic timestamp of the last event handed to _handle.
        # The status bar shows "Ns since last event" from this so an
        # idle game (no packets) is distinguishable from a dead bridge.
        self._last_event_at = time.monotonic()
        # Monotonic timestamp of the last GAMEPLAY event (spawns,
        # deaths, damage, drops, xp, zones). Bridge chatter — net_seen,
        # session_setup, key_rotation, dll_heartbeat — resets _last_event_at but not this, so
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
    def current_visit_id(self) -> int | None:
        """Id of the current open zone visit, if any."""
        return self._current_visit_id

    @property
    def last_session_id(self) -> int | None:
        """Most recent session id, surviving stop(). The UI falls back
        to this when no session is actively running."""
        return self._last_session_id

    def seconds_since_event(self) -> float:
        return time.monotonic() - self._last_event_at

    def seconds_since_gameplay(self) -> float:
        return time.monotonic() - self._last_gameplay_at

    def is_connected(self) -> bool:
        """Pure query: this session is linked (see needs_channel) and
        session traffic flowed recently."""
        return (
            self._running
            and self._link_player
            and self._link_session
            and self.seconds_since_gameplay() < _STALE_AFTER_S
        )

    def needs_channel(self) -> bool:
        """Pure query: tracking is running but the session is not yet
        linked — the UI snapshot carries this for the display lane."""
        return self._running and not (
            self._link_player and self._link_session
        )

    def reset_link(self) -> None:
        """Drop linkage plus session-position state (used when the
        session's rows are wiped mid-run)."""
        self._link_player = False
        self._link_session = False
        self._portal_sights = deque(maxlen=16)
        self._current_zone = None
        self._current_visit_id = None

    def start(self) -> int:
        if self._running:
            return self._session_id or -1
        self._events_seen = 0
        self._events_parsed = 0
        self._events_dropped = 0
        self._local_account_id = None
        self._link_player = False
        self._link_session = False
        self._mob_of_enemy = {}
        self._reset_visit_state()
        self._last_notice_at = 0.0
        self._notice_at_by_key = {}
        self._portal_sights = deque(maxlen=16)
        self._current_zone = None
        self._current_visit_id = None
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
            f"session #{self._session_id} started — polling DLL event buffer..."
        )
        return self._session_id

    def stop(self) -> None:
        self._running = False
        self._link_player = False
        self._link_session = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._session_id is not None:
            self._db.end_session(self._session_id)
            self.status.emit(f"session #{self._session_id} ended")
            self._session_id = None

    # --- internals ---
    def _loop(self) -> None:
        # Batch-1: drain everything available per wakeup inside one DB
        # batch (single commit), then adaptive sleep — 1ms when busy,
        # backing off to ~20ms when idle. Same events, fewer commits
        # and wakeups.
        self.status.emit("consumer thread running")
        _idle = 0
        while self._running:
            try:
                _did_work = False
                self._db.begin_batch()
                try:
                    # Cap per-wakeup so a burst never starves stop().
                    for _ in range(500):
                        if not self._running:
                            break
                        if not self._dll.has_event():
                            break
                        raw = self._dll.next_event()
                        if raw is not None:
                            self._handle(raw)
                            _did_work = True
                        else:
                            # has_event was true but no record came out:
                            # the reader resynced (fell behind / corrupt /
                            # torn). Surface it — previously silent, which
                            # looked exactly like "tracking just stopped".
                            pop = getattr(self._dll, "pop_resync", None)
                            reason = pop() if callable(pop) else None
                            if reason:
                                self._events_dropped += 1
                                self.status.emit(f"buffer resync: {reason}")
                            _did_work = True
                except Exception:
                    # Drop the half-filled batch before it can commit;
                    # the outer handler reports and keeps polling.
                    try:
                        self._db.abort_batch()
                    except Exception:
                        pass
                    raise
                finally:
                    self._db.end_batch()
                if _did_work:
                    _idle = 0
                    time.sleep(0.001)
                else:
                    _idle = min(_idle + 1, 20)
                    # 1ms -> ~20ms backoff when idle.
                    time.sleep(0.001 if _idle < 3 else 0.02)
            except Exception as e:
                # Never let a single bad frame kill the consumer, but make
                # it visible in the debug console instead of failing silent.
                # Roll back any open batch before closing it so a failed
                # drain never commits a partial batch.
                try:
                    self._db.abort_batch()
                except Exception:
                    pass
                try:
                    self._db.end_batch()
                except Exception:
                    pass
                self.status.emit(f"consumer error: {e!r}")
                time.sleep(0.1)

    def _display_monster(self, mob_id: int) -> str:
        # Human-readable monster name for notifications, falling back
        # to the numeric id when the tables are missing or silent.
        if self._names is not None:
            try:
                name = self._names.monster(mob_id)
            except Exception:
                name = None
            if name:
                return name
        return f"Mob#{mob_id}"

    def _record_notice(self, sid: int, name: str, rarity: str, ts: str,
                       enemy_id: int | None = None) -> None:
        # Single choke point for notable notices. Twins from the two
        # arms share a per-key window on the stripped subject token,
        # so one arrival never yields two rows either direction; a
        # short any-key burst guard covers twins whose sentence form
        # cannot be matched. Distinct arrivals outside both windows
        # earn their own rows. A known instance id always attaches,
        # so a dup-skipped arm still marks the later kill.
        now = time.monotonic()
        key = (_normalize_notice_token(name), rarity)
        known = self._notice_at_by_key.get(key, 0.0)
        for k, t in list(self._notice_at_by_key.items()):
            if now - t >= _NOTICE_DEDUPE_S:
                del self._notice_at_by_key[k]
        if (now - known >= _NOTICE_DEDUPE_S
                and now - self._last_notice_at >= _NOTICE_BURST_S):
            self._db.insert_spawn_notification(sid, name, rarity, ts)
            self._notice_at_by_key[key] = now
            self._last_notice_at = now
        if enemy_id:
            self._notable_enemy_ids.add(enemy_id)

    def _reset_visit_state(self) -> None:
        # A new visit is a new scaling context: strength baselines,
        # notable ids, the announcement reach-back log, unjudged
        # arrivals, and spawn-to-type links never carry over. The
        # notice clocks keep running across visits; harmless when
        # everything is already empty.
        self._hp_by_mob = {}
        self._mob_of_enemy = {}
        self._notable_enemy_ids = set()
        self._recent_spawns = deque(maxlen=_RECENT_SPAWNS)
        self._pending_judge = deque(maxlen=_PENDING_JUDGE)

    def _judge_pending(self, sid: int, mob_id: int) -> None:
        # A baseline just firmed up: re-judge this visit's arrivals
        # that came too early to call at the time, under the now-firm
        # middle. Trippers record through the choke with their
        # original arrival time so graphs bin them where they
        # happened; everything evaluated here leaves pending, so
        # full-baseline arrivals are never revisited and each early
        # arrival is judged exactly once.
        hist = self._hp_by_mob.get(mob_id)
        if not hist or len(hist) < _HP_MIN_SAMPLES:
            return
        try:
            mid = median(hist)
        except Exception:
            mid = 0
        due = [e for e in self._pending_judge if e[0] == mob_id]
        if not due:
            return
        if mid <= 0:
            return
        if not (self._link_player and self._link_session):
            return
        self._pending_judge = deque(
            (e for e in self._pending_judge if e[0] != mob_id),
            maxlen=_PENDING_JUDGE)
        for _, eid, ehp, ets in due:
            if ehp >= _HP_OUTLIER_MULT * mid:
                self._record_notice(
                    sid, self._display_monster(mob_id),
                    "mighty", ets, eid)

    def _retro_mark(self) -> None:
        # An announcement names an arrival without carrying its id.
        # Reach back over recent spawns and attach any that read as
        # outliers under the same rule the spawn-time check uses, so
        # id-less arrivals still mark their kill. Bounded and rare
        # (only on a matching announcement), so the scan stays cheap.
        now = time.monotonic()
        for tse, eid, mid, ehp in list(self._recent_spawns):
            if now - tse > _RETRO_MARK_S:
                continue
            hist = self._hp_by_mob.get(mid)
            if not hist or len(hist) < _HP_MIN_SAMPLES:
                continue
            try:
                mid_hp = median(hist)
            except Exception:
                continue
            if mid_hp > 0 and ehp >= _HP_OUTLIER_MULT * mid_hp:
                self._notable_enemy_ids.add(eid)

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

        if etype == "zone_change":
            # Every arrival opens a new scaling context, linked or
            # not; persistence below stays gated as usual.
            self._reset_visit_state()

        if etype == "enemy_spawn":
            # Remember which mob type each unique enemy instance is, so
            # the later death row can carry a displayable monster name.
            try:
                mob_id = int(data.get("mob_id", 0))
            except (TypeError, ValueError):
                mob_id = 0
            try:
                self._mob_of_enemy[int(data.get("enemy_id", 0))] = mob_id
            except (TypeError, ValueError):
                pass
            try:
                spawn_enemy_id = int(data.get("enemy_id", 0))
            except (TypeError, ValueError):
                spawn_enemy_id = 0
            try:
                hp = int(data.get("hp", 0))
            except (TypeError, ValueError):
                hp = 0
            # A boss-flagged spawn is a notable spawn: record it through
            # the spawn-notification path so the UI can surface it.
            # Gated on linkage like other persisted gameplay, and a
            # missing flag simply reads as a normal spawn.
            try:
                boss_flag = int(data.get("is_boss", 0))
            except (TypeError, ValueError):
                boss_flag = 0
            if boss_flag and self._link_player and self._link_session:
                self._record_notice(
                    sid, self._display_monster(mob_id), "boss", ts,
                    spawn_enemy_id or None)
            elif hp > 0 and self._link_player and self._link_session:
                # Outlier check against the running baseline for this
                # monster, judged before folding the new reading in so
                # a spike never vouches for itself. The baseline keeps
                # warming regardless of linkage; only recording is gated.
                hist = self._hp_by_mob.get(mob_id)
                if hist is None:
                    hist = deque(maxlen=_HP_WINDOW)
                    self._hp_by_mob[mob_id] = hist
                if len(hist) >= _HP_MIN_SAMPLES:
                    try:
                        mid = median(hist)
                    except Exception:
                        mid = 0
                    if mid > 0 and hp >= _HP_OUTLIER_MULT * mid:
                        self._record_notice(
                            sid, self._display_monster(mob_id),
                            "mighty", ts, spawn_enemy_id or None)
                elif spawn_enemy_id:
                    # Too early to call against: hold for the baseline
                    # to firm up, then judge retroactively with the
                    # arrival time.
                    self._pending_judge.append(
                        (mob_id, spawn_enemy_id, hp, ts))
            if hp > 0:
                hist = self._hp_by_mob.get(mob_id)
                if hist is None:
                    hist = deque(maxlen=_HP_WINDOW)
                    self._hp_by_mob[mob_id] = hist
                hist.append(hp)
                if len(hist) == _HP_MIN_SAMPLES:
                    # The baseline just firmed up: judge anything
                    # that arrived too early to be judged at the time.
                    self._judge_pending(sid, mob_id)
            if hp > 0 and spawn_enemy_id:
                # Remembered for id-less announcements, which reach
                # back over this log to find the spawn they name.
                self._recent_spawns.append(
                    (time.monotonic(), spawn_enemy_id, mob_id, hp))
        elif etype == "local_account":
            # Identity for this client. Used to tell local events
            # apart from the rest.
            try:
                acct = int(data.get("account_id", 0))
            except (TypeError, ValueError):
                acct = 0
            if acct:
                self._local_account_id = acct
                self._link_player = True
                self._db.set_session_account(sid, acct)
                self._db.backfill_kill_attribution(sid, acct)
        elif etype == "session_setup":
            # Second half of the channel link. Gameplay persistence
            # below stays closed until both halves arrive.
            self._link_session = True
        elif not (self._link_player and self._link_session):
            # Pre-link: discard gameplay persistence (counters and
            # liveness above keep flowing). Never queued.
            return
        elif etype == "damage_dealt":
            # Sanity guard for out-of-range values — skipped so they
            # can never grant kill credit.
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
            # Kill credit uses prior damage history for this enemy.
            # A prior notable sighting marks the kill row; the id is
            # then dropped so the set stays small and repeats read
            # as normal.
            enemy_id = None
            try:
                enemy_id = int(data.get("enemy_id", 0))
            except (TypeError, ValueError):
                return
            mob_id = self._mob_of_enemy.get(enemy_id)
            is_mighty = enemy_id in self._notable_enemy_ids
            if is_mighty:
                self._notable_enemy_ids.discard(enemy_id)
            mine = (
                self._local_account_id is not None
                and self._db.damaged_by(sid, enemy_id, self._local_account_id)
            )
            self._db.insert_kill(sid, enemy_id, mob_id, ts, is_mine=mine,
                                  is_mighty=is_mighty)
            # The id served its only read above; drop it so the map
            # stays small no matter how long the session runs.
            self._mob_of_enemy.pop(enemy_id, None)
        elif etype == "drop_creation":
            # Drop ownership is resolved at query time.
            try:
                drop_id = int(data.get("drop_id", 0))
                item_id = (int(data.get("item_id"))
                           if "item_id" in data else None)
                amount = (int(data.get("amount", 1))
                          if "amount" in data else None)
                belongs_to = (int(data.get("belongs_to", 0))
                              if "belongs_to" in data else None)
            except (TypeError, ValueError):
                return
            self._db.insert_drop(
                sid,
                drop_id,
                item_id,
                None,
                amount,
                belongs_to,
                ts,
            )
        elif etype == "pickup":
            # Someone picked a drop up. The Drops tab shows who took
            # it; ownership is derived at query time.
            try:
                picker = int(data.get("picker", 0)) or None
                drop_id = int(data.get("drop_id", 0))
            except (TypeError, ValueError):
                return
            if drop_id:
                self._db.mark_drop_pickup(sid, drop_id, picker, ts)
        elif etype == "drop_destroyed":
            # A drop vanished without being picked up.
            try:
                drop_id = int(data.get("drop_id", 0))
            except (TypeError, ValueError):
                return
            if drop_id:
                self._db.mark_drop_destroyed(sid, drop_id)
        elif etype == "player_death":
            # Local player death. XP loss is tracked on the death
            # row itself.
            try:
                exp_lost = int(data.get("exp_lost", 0))
                money_lost = int(data.get("money_lost", 0))
                items_lost = int(data.get("items_lost", 0))
            except (TypeError, ValueError):
                return
            self._db.insert_death(sid, exp_lost, money_lost, items_lost, ts)
        elif etype == "exp_update":
            # XP gain event. Negative deltas arrive wrapped and are
            # dropped silently; death echoes are dropped — deaths are
            # recorded from the death event only, so this never
            # double-counts or pollutes the gain history.
            try:
                raw_gain = int(data.get("exp_gained", 0))
                level = int(data.get("level", 0)) if "level" in data else None
                # Running totals ride along when the feed carries them;
                # older feeds simply omit them and rows store blanks.
                required_xp = (int(data["required_xp"])
                               if "required_xp" in data else None)
                total_xp = (int(data["total_xp"])
                            if "total_xp" in data else None)
            except (TypeError, ValueError):
                return
            if raw_gain >= 2**63:
                raw_gain -= 2**64
            if raw_gain < 0:
                return
            # Sanity guard for out-of-range values.
            if raw_gain > 50_000_000:
                return
            # Level is validated to the legit range; out-of-range
            # values are stored without a level instead.
            if level is not None and not 1 <= level <= 100:
                level = None
            if (level is not None and self._local_account_id is not None
                    and level == self._local_account_id):
                level = None
            self._db.insert_xp(sid, raw_gain, level, False, ts,
                               required_xp, total_xp)
        elif etype == "level_up":
            try:
                level_up = int(data.get("level", 0))
            except (TypeError, ValueError):
                return
            if not 1 <= level_up <= 100:
                return
            if (self._local_account_id is not None
                    and level_up == self._local_account_id):
                return
            self._db.insert_xp(sid, 0, level_up, True, ts)
        elif etype == "mirage_exit":
            # A mirage run ended. This arrives just before the warp-out
            # zone change closes the visit, so flag-then-close ordering
            # holds: the open visit is still open here.
            self._db.mark_current_visit_mirage(sid, ts)
        elif etype == "portal_sight":
            # A portal listing with its label and pass cost. Remembered
            # briefly for same-zone arrival correlation below.
            try:
                if "text" not in data or "cost" not in data:
                    return
                text = str(data.get("text", ""))
                cost = int(data.get("cost", 0))
            except (TypeError, ValueError):
                return
            self._portal_sights.append((time.monotonic(), text, cost))
        elif etype == "zone_change":
            new_map = str(data.get("map_name", ""))
            same_zone = (
                self._current_zone is not None and new_map == self._current_zone
            )
            self._db.insert_zone_visit(
                sid,
                new_map,
                str(data.get("display_name", "")),
                ts,
            )
            if same_zone:
                # Same-map arrival shortly after a free non-housing
                # listing means mirage entry. Insert first so the flag
                # lands on the NEW visit, then consume the sighting.
                now = time.monotonic()
                self._portal_sights = deque(
                    ((t, x, c) for (t, x, c) in self._portal_sights
                     if now - t <= _SIGHT_WINDOW_S),
                    maxlen=16)
                for sight in list(self._portal_sights):
                    if sight[2] == 0 and sight[1] != "Housing":
                        self._db.mark_current_visit_mirage(sid, ts)
                        self._portal_sights.remove(sight)
                        break
            self._current_zone = new_map
            self._current_visit_id = self._db.open_visit_id(sid)
        elif etype == "spawn_notification":
            self._record_notice(
                sid, str(data.get("name", "")), "boss", ts)
        elif etype == "center_message":
            # A mid-screen server announcement. When it names an
            # oversized arrival, keep the announcement text itself —
            # it already carries the monster name. Anything else is
            # ignored here (the raw feed still reaches the debug log).
            try:
                text = str(data.get("text", ""))
            except (TypeError, ValueError):
                return
            if text and "mighty" in text.lower():
                self._record_notice(sid, text, "mighty", ts)
                self._retro_mark()
        elif etype in (
            # Bridge/session flow signals: nothing to persist.
            "key_rotation",
            "net_seen",
            "net_connect",
            "net_close",
            "shm_open",
            "hook_install",
            "hook_patched",
            "hook_eat",
            "dll_heartbeat",
            "dll_warning",
        ):
            return
