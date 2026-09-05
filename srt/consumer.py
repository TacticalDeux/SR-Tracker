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
        # Per-run counters. Surfaced in the status bar so the user can
        # see at a glance whether events are flowing, and how many got
        # dropped as malformed.
        self._events_seen = 0
        self._events_parsed = 0
        self._events_dropped = 0

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

    def start(self) -> int:
        if self._running:
            return self._session_id or -1
        self._events_seen = 0
        self._events_parsed = 0
        self._events_dropped = 0
        self._session_id = self._db.start_session()
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
        ts = datetime.now().isoformat(timespec="milliseconds")

        if etype == "enemy_death":
            self._db.insert_kill(sid, int(data.get("enemy_id", 0)), None, ts)
        elif etype == "drop_creation":
            self._db.insert_drop(
                sid,
                int(data.get("drop_id", 0)),
                int(data.get("item_id", 0)) if "item_id" in data else None,
                int(data.get("belongs_to", 0)) if "belongs_to" in data else None,
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
