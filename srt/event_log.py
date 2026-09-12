"""Per-session debug event log file, plus the Report Bug helpers.

The file mirror (EventLog) streams the session's event feed to disk in
every build, rolled into bounded parts. The report helpers below are
always on too: a bounded in-memory ring of recent lines, a plain-text
report builder that prefers the session file, and a small upload
routine used by the Report Bug dialog.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject

from . import paths as _paths
from .debug_console import display_event as _base_display_event


# Event types the log sink knows how to show. Anything else is
# reduced to a generic marker below, so unfamiliar traffic never
# reaches disk or the report buffer in raw form.
_KNOWN_EVENT_TYPES = frozenset({
    "local_account",
    "enemy_spawn",
    "spawn_notification",
    "center_message",
    "damage_dealt",
    "enemy_death",
    "drop_creation",
    "drop_destroyed",
    "pickup",
    "pickup_denied",
    "player_death",
    "exp_update",
    "level_up",
    "mirage_exit",
    "portal_sight",
    "zone_change",
    "character_spawn",
    "session_setup",
    "heartbeat",
    "dll_heartbeat",
    "dll_warning",
    "key_rotation",
    "net_seen",
    "net_connect",
    "net_close",
    "shm_open",
    "hook_install",
    "hook_patched",
    "hook_eat",
    "packet_parsed",
})

_OP_TYPE_RE = re.compile(r"^op\s*(\d+)$", re.IGNORECASE)


def display_event(raw: str) -> str:
    """Display form of a raw event JSON string, deny by default.

    Known types keep the shared sanitizer's exact output; unknown
    types reduce to a generic marker so their values never pass
    through. Non-JSON input is returned unchanged.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if not isinstance(data, dict):
        return json.dumps({"type": "packet_parsed", "status": "parsed"})
    etype = data.get("type", "unknown")
    if isinstance(etype, str) and _OP_TYPE_RE.match(etype.strip()):
        return _base_display_event(raw)
    if etype in _KNOWN_EVENT_TYPES:
        return _base_display_event(raw)
    return json.dumps({"type": "packet_parsed", "status": "parsed"})


_LOG_PREFIX = "debug-"
_LOG_SUFFIX = ".log"
_PART_TAG = ".part"

# A session file rolls at this size; disk per session stays bounded.
_PART_MAX_BYTES = 8_000_000

# Newest rolled parts kept per session.
_KEEP_PARTS = 5

# How many recent sessions to keep; older ones are pruned on open.
# A session counts whole (all its parts), so rotation never evades it.
_KEEP_NEWEST = 10


def log_dir() -> Path:
    """Directory session logs live in (same data dir as the DB)."""
    d = _paths.user_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def session_log_stem(session_id: int) -> Path:
    """Base path for a tracking session's rolled parts."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return log_dir() / f"{_LOG_PREFIX}{session_id}-{stamp}"


def session_log_path(session_id: int) -> Path:
    """First part path for a tracking session."""
    stem = session_log_stem(session_id)
    return stem.parent / f"{stem.name}{_PART_TAG}1{_LOG_SUFFIX}"


def _split_part(name: str) -> tuple:
    """(session key, part number) for a log file name.

    Legacy whole files read as part 0 of their session. Anything
    foreign returns (None, 0).
    """
    if not (name.startswith(_LOG_PREFIX) and name.endswith(_LOG_SUFFIX)):
        return None, 0
    core = name[len(_LOG_PREFIX):-len(_LOG_SUFFIX)]
    head, sep, tail = core.rpartition(_PART_TAG)
    if sep and tail.isdigit():
        return head, int(tail)
    return core, 0


def session_parts(stem: Path) -> list:
    """Existing rolled parts for a session stem, oldest first."""
    try:
        files = list(stem.parent.glob(f"{_LOG_PREFIX}*{_LOG_SUFFIX}"))
    except OSError:
        return []
    key = stem.name[len(_LOG_PREFIX):]
    parts = [p for p in files if _split_part(p.name)[0] == key]
    return sorted(parts, key=lambda p: _split_part(p.name)[1])


def prune_session_parts(stem: Path, keep: int = _KEEP_PARTS) -> None:
    """Drop a session's oldest rolled parts beyond `keep`. Never raises."""
    try:
        parts = session_parts(stem)
    except OSError:
        return
    for stale in parts[:max(0, len(parts) - keep)]:
        try:
            stale.unlink()
        except OSError:
            pass


def prune_old_logs(keep: int = _KEEP_NEWEST) -> None:
    """Delete sessions beyond the newest `keep`, whole (all parts).

    Sessions group by stem so rotation never hides old traffic from
    the bound. Never raises.
    """
    try:
        files = list(log_dir().glob(f"{_LOG_PREFIX}*{_LOG_SUFFIX}"))
    except OSError:
        return
    groups: dict = {}
    for path in files:
        key, _ = _split_part(path.name)
        if key is None:
            continue
        groups.setdefault(key, []).append(path)

    def newest(paths: list) -> float:
        try:
            return max(p.stat().st_mtime for p in paths)
        except OSError:
            return 0.0

    ranked = sorted(groups.values(), key=newest, reverse=True)
    for stale_group in ranked[keep:]:
        for stale in stale_group:
            try:
                stale.unlink()
            except OSError:
                pass


def latest_session_stem() -> Path | None:
    """Stem of the newest session with files on disk, or None.

    Newest by file modification time, so a just-finished session wins
    over older ones. Never raises.
    """
    try:
        anchor = log_dir()
    except OSError:
        return None
    try:
        files = list(anchor.glob(f"{_LOG_PREFIX}*{_LOG_SUFFIX}"))
    except OSError:
        return None
    best_key = None
    best_mtime = -1.0
    newest: dict = {}
    for path in files:
        key, _ = _split_part(path.name)
        if key is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if key not in newest or mtime > newest[key]:
            newest[key] = mtime
    if not newest:
        return None
    # Key breaks mtime ties deterministically; within one session the
    # key's stamp orders chronologically.
    best_key = max(newest, key=lambda k: (newest[k], k))
    return anchor / f"{_LOG_PREFIX}{best_key}"


def read_session_tail(parts: list, max_bytes: int) -> list:
    """Newest-first tail across rolled parts, bounded by `max_bytes`.

    Walks from the newest part backwards, taking each part's tail
    until the budget is spent. At least one line per readable part.
    """
    out: list = []
    budget = max_bytes
    ordered = sorted(parts, key=lambda p: _split_part(p.name)[1],
                     reverse=True)
    for part in ordered:
        try:
            text = part.read_text(encoding="utf-8",
                                  errors="replace").splitlines()
        except OSError:
            continue
        take: list = []
        size = 0
        for line in reversed(text):
            cost = len(line.encode("utf-8")) + 1
            if take and size + cost > budget:
                break
            take.append(line)
            size += cost
        out[0:0] = take[::-1]
        budget -= size
        if budget <= 0:
            break
    return out


def assemble_log_lines(*, log: Any, fallback_lines: list,
                       max_bytes: int) -> tuple:
    """(lines, total, source) for a report: session file preferred.

    Falls back to the memory ring only when no session file exists
    (e.g. reporting without a tracking session). Never raises.
    """
    if log is not None:
        try:
            parts = session_parts(log.stem)
        except OSError:
            parts = []
        if parts:
            try:
                return (read_session_tail(parts, max_bytes),
                        log.total_lines, "session file")
            except OSError:
                pass
    lines = list(fallback_lines)
    return lines, len(lines), "recent activity"


def read_full_session_text(log: Any) -> tuple:
    """(joined text, part count) for the whole session file.

    Parts are read oldest first so the result runs in true order.
    Never raises; missing/unreadable files read as empty.
    """
    if log is None:
        return "", 0
    try:
        stem = log.stem
    except AttributeError:
        return "", 0
    try:
        parts = session_parts(stem)
    except OSError:
        return "", 0
    if not parts:
        return "", 0
    ordered = sorted(parts, key=lambda p: _split_part(p.name)[1])
    blobs: list = []
    for part in ordered:
        try:
            blobs.append(part.read_text(encoding="utf-8",
                                        errors="replace"))
        except OSError:
            continue
    text = "".join(blobs)
    if text.endswith("\n"):
        text = text[:-1]
    return text, len(ordered)


def full_session_size(log: Any) -> tuple:
    """(bytes on disk, part count) for the session file. Never raises."""
    if log is None:
        return 0, 0
    try:
        parts = session_parts(log.stem)
    except (AttributeError, OSError):
        return 0, 0
    total = 0
    for part in parts:
        try:
            total += part.stat().st_size
        except OSError:
            continue
    return total, len(parts)


def format_report_size(num_bytes: Any) -> str:
    """Short human size ("3.2 MB", "850 KB") for dialog labels."""
    try:
        n = int(num_bytes)
    except (TypeError, ValueError):
        return "unknown size"
    if n < 0:
        n = 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1000:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n} bytes"


def build_full_report_text(*, comment: str, log_text: str,
                           app_version: str, session_id: Any,
                           events_seen: int, total_lines: Any = None,
                           source: str | None = None,
                           part_count: Any = None) -> str:
    """Plain-text report with the whole joined log (for Save)."""
    lines = str(log_text).splitlines() if log_text else []
    if total_lines is None:
        total_lines = len(lines)
    try:
        total = int(total_lines)
    except (TypeError, ValueError):
        total = len(lines)
    tag = source or ""
    if part_count:
        try:
            tag = f"{tag}, {int(part_count)} parts".lstrip(", ")
        except (TypeError, ValueError):
            pass
    head = [
        "SR Tracker bug report",
        f"app_version: {app_version}",
        f"session_id: {session_id if session_id is not None else 'n/a'}",
        f"events_seen: {events_seen}",
        f"log_lines: {len(lines)} of {total}"
        + (f" [{tag}]" if tag else ""),
        f"comment: {str(comment).strip() or '(none)'}",
        "--- log ---",
    ]
    if not lines:
        return "\n".join(head + ["(no log lines)"])
    return "\n".join(head + lines)


def _ts() -> str:
    return time.strftime("%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"


class EventLog(QObject):
    """File sink fed by the consumer's `event_seen` / `status` signals.

    Active in every build. Writes are line-buffered with no per-line
    fsync; the session file rolls at ~8MB keeping the newest few parts,
    so disk stays bounded no matter how long tracking runs.
    """

    def __init__(self, path: Path, parent: QObject | None = None,
                 stem: Path | None = None, part: int = 1):
        super().__init__(parent)
        self.path = path
        self.stem = stem if stem is not None else path
        self._part = part
        # Line-buffered: each line flushes without a per-line fsync.
        self._fh = open(path, "a", encoding="utf-8", buffering=1)
        try:
            self._bytes = path.stat().st_size
        except OSError:
            self._bytes = 0
        # Every line written this session, for the report header's
        # total-vs-sent counts. No disk read needed.
        self.total_lines = 0
        self._roll_ok = True
        self._consumer: Any | None = None

    @classmethod
    def open_session(
        cls, session_id: int, parent: QObject | None = None,
        keep: int = _KEEP_NEWEST,
    ) -> "EventLog":
        """Open the log for a tracking session, pruning older ones."""
        stem = session_log_stem(session_id)
        log = cls(stem.parent / f"{stem.name}{_PART_TAG}1{_LOG_SUFFIX}",
                  parent, stem=stem, part=1)
        prune_old_logs(keep)
        return log

    # ------------------------------------------------------------------
    # Slots (connected to the consumer in subscribe()).
    # ------------------------------------------------------------------
    def append_event(self, raw: str) -> None:
        # Sanitized form only — display_event() strips key material and
        # peer addresses; the raw string never reaches disk.
        self._write(f"{_ts()}  {display_event(raw)}")
        self.total_lines += 1

    def append_status(self, msg: str) -> None:
        self._write(f"{_ts()}  [consumer] {msg}")
        self.total_lines += 1

    def subscribe(self, consumer: Any) -> "EventLog":
        """Connect to the consumer's signals. Returns self."""
        self._consumer = consumer
        consumer.event_seen.connect(self.append_event)
        consumer.status.connect(self.append_status)
        return self

    def unsubscribe(self) -> None:
        """Detach from the consumer's signals, if attached."""
        if self._consumer is None:
            return
        for signal, slot in (
            (self._consumer.event_seen, self.append_event),
            (self._consumer.status, self.append_status),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._consumer = None

    def close(self) -> None:
        """Detach from signals and close the file handle."""
        self.unsubscribe()
        try:
            self._fh.close()
        except OSError:
            pass

    # ------------------------------------------------------------------
    def _write(self, line: str) -> None:
        data = (line + "\n").encode("utf-8")
        if (self._roll_ok and self._bytes > 0
                and self._bytes + len(data) > _PART_MAX_BYTES):
            self._roll()
        try:
            self._fh.write(line + "\n")
        except OSError:
            return
        self._bytes += len(data)

    def _roll(self) -> None:
        """Open the next rolled part; prune this session to the newest."""
        nxt = (self.stem.parent /
               f"{self.stem.name}{_PART_TAG}{self._part + 1}{_LOG_SUFFIX}")
        try:
            fh = open(nxt, "a", encoding="utf-8", buffering=1)
        except OSError:
            # Keep writing to the current part rather than dropping the
            # stream; don't retry every line.
            self._roll_ok = False
            return
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = fh
        self.path = nxt
        self._part += 1
        self._bytes = 0
        prune_session_parts(self.stem)


# ---------------------------------------------------------------------------
# Report Bug helpers (always on, dev and frozen alike).
# ---------------------------------------------------------------------------
# The ring holds recent lines only; at typical line lengths this stays
# well under a megabyte. It never touches disk by itself.
_REPORT_BUFFER_MAXLEN = 3000

# Outgoing reports keep the newest slice of the session log, capped
# by both line count and byte size. The byte cap binds: at typical
# line lengths ~1MB holds several thousand lines, comfortably under
# the store's comfort for a single bound value, with headroom left on
# the proxy side.
_REPORT_MAX_LINES = 8000
_REPORT_MAX_BYTES = 1_000_000

# Whole-file uploads ride one multipart call: the inline excerpt stays
# under the usual bound while the file part carries the joined session
# text. The file ceiling covers every part a session may hold.
_FULL_FILE_MAX_BYTES = 40_000_000

# Lines shown in the dialog's read-only tail preview.
_PREVIEW_LINES = 50

# At most one upload per cooldown window; the dialog offers Copy/Save
# while the guard is hot.
_SEND_COOLDOWN_S = 180.0

# Network wait per upload attempt; whole-file posts carry megabytes,
# so they get a generous window.
_UPLOAD_TIMEOUT_S = 15
_FULL_UPLOAD_TIMEOUT_S = 120

# Last successful send (monotonic clock), for the cooldown guard.
_last_send_ts = 0.0


class ReportBuffer(QObject):
    """Always-on in-memory ring of recent sanitized lines.

    Fed by the same consumer signals as the file sink, plus short app
    lifecycle entries from the main window. Each line carries a source
    tag ([event], [consumer], [app]). Bounded memory, zero disk writes.
    """

    def __init__(self, maxlen: int = _REPORT_BUFFER_MAXLEN,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._lines: deque = deque(maxlen=maxlen)
        self._consumer: Any | None = None

    # ------------------------------------------------------------------
    # Slots (connected to the consumer in subscribe()).
    # ------------------------------------------------------------------
    def append_event(self, raw: str) -> None:
        # Same sanitizer as the on-screen console and the file sink —
        # key material never enters the buffer.
        self._lines.append(f"{_ts()}  [event] {display_event(raw)}")

    def append_status(self, msg: str) -> None:
        self._lines.append(f"{_ts()}  [consumer] {msg}")

    def append_app(self, msg: str) -> None:
        """Lifecycle entries from the main window (start/stop, errors)."""
        self._lines.append(f"{_ts()}  [app] {msg}")

    def subscribe(self, consumer: Any) -> "ReportBuffer":
        """Connect to the consumer's signals. Returns self."""
        self._consumer = consumer
        consumer.event_seen.connect(self.append_event)
        consumer.status.connect(self.append_status)
        return self

    def unsubscribe(self) -> None:
        """Detach from the consumer's signals, if attached."""
        if self._consumer is None:
            return
        for signal, slot in (
            (self._consumer.event_seen, self.append_event),
            (self._consumer.status, self.append_status),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._consumer = None

    # ------------------------------------------------------------------
    def lines(self) -> list:
        """A snapshot of the buffered lines, oldest first."""
        return list(self._lines)

    def __len__(self) -> int:
        return len(self._lines)


def truncate_report_lines(lines: list) -> tuple:
    """Newest slice of `lines` within the report caps.

    Returns (kept_lines, dropped_count). The tail is kept: the most
    recent lines matter most for a bug report.
    """
    kept = lines[-_REPORT_MAX_LINES:]
    dropped = max(0, len(lines) - len(kept))
    # Byte cap, measured on the wire form. Drop from the front.
    size = sum(len(l) + 1 for l in kept)
    while kept and size > _REPORT_MAX_BYTES:
        size -= len(kept.pop(0)) + 1
        dropped += 1
    return kept, dropped


def build_report_text(*, comment: str, include_log: bool, lines: list,
                      app_version: str, session_id: Any,
                      events_seen: int, total_lines: Any = None,
                      source: str | None = None) -> str:
    """Plain-text report with a header block, for Copy/Save/preview.

    `total_lines` is the full log size behind the slice (the session
    file count, or the ring size on fallback); truncation stays
    visible instead of silent.
    """
    kept, _ = truncate_report_lines(lines) if include_log else ([], 0)
    if total_lines is None:
        total_lines = len(lines) if include_log else 0
    try:
        total = int(total_lines)
    except (TypeError, ValueError):
        total = len(kept)
    omitted = max(0, total - len(kept))
    head = [
        "SR Tracker bug report",
        f"app_version: {app_version}",
        f"session_id: {session_id if session_id is not None else 'n/a'}",
        f"events_seen: {events_seen}",
        f"log_lines: {len(kept)} of {total}"
        + (f" [{source}]" if source else "")
        + (f" ({omitted} older lines omitted)" if omitted else ""),
        f"comment: {comment.strip() or '(none)'}",
        "--- log ---",
    ]
    if not include_log:
        return "\n".join(head + ["(log not included — comment only)"])
    return "\n".join(head + kept)


def build_report_payload(*, comment: str, include_log: bool, lines: list,
                         app_version: str, session_id: Any,
                         events_seen: int) -> dict:
    """Upload payload: header fields plus the capped log slice."""
    kept, _ = truncate_report_lines(lines) if include_log else ([], 0)
    try:
        sid = int(session_id) if session_id is not None else 0
    except (TypeError, ValueError):
        sid = 0
    return {
        "app_version": str(app_version),
        "session_id": sid,
        "event_count": int(events_seen),
        "comment": comment.strip(),
        "log": "\n".join(kept),
    }


def load_report_endpoint() -> str:
    """Public report proxy address from the dev-local config module.

    Missing module or an empty value both read as unconfigured — the
    dialog then offers Copy/Save only. No secret is involved: the proxy
    holds the store credentials, so this file carries a public address
    at most. Never raises.
    """
    try:
        from . import report_config as _cfg
    except ImportError:
        return ""
    try:
        return (getattr(_cfg, "REPORT_URL", "") or "").strip()
    except Exception:
        return ""


def send_cooldown_remaining(now: Any = None) -> float:
    """Seconds until another send is allowed (0 when clear)."""
    if now is None:
        now = time.monotonic()
    return max(0.0, _SEND_COOLDOWN_S - (now - _last_send_ts))


def mark_sent(now: Any = None) -> None:
    """Record a successful send for the cooldown guard."""
    global _last_send_ts
    _last_send_ts = time.monotonic() if now is None else now


# Incoming caps mirrored from the proxy: the same report must pass
# there, so reject it here before spending a network round trip.
_PROXY_MAX_COMMENT = 5000
_PROXY_MAX_LOG = 1_000_000
_PROXY_MAX_VERSION = 64


def validate_report_payload(payload: Any) -> str | None:
    """Generic rejection reason, or None when the payload may be sent."""
    if not isinstance(payload, dict):
        return "The report was rejected."
    comment = payload.get("comment", "")
    log = payload.get("log", "")
    session_id = payload.get("session_id", 0)
    app_version = payload.get("app_version", "")
    event_count = payload.get("event_count", 0)
    if (not isinstance(comment, str) or len(comment) > _PROXY_MAX_COMMENT
            or not isinstance(log, str) or len(log) > _PROXY_MAX_LOG
            or not isinstance(session_id, int) or session_id < 0
            or not isinstance(app_version, str)
            or not 0 < len(app_version) <= _PROXY_MAX_VERSION
            or not isinstance(event_count, int) or event_count < 0):
        return "The report was rejected."
    return None


def validate_full_upload(metadata: Any, file_bytes: Any) -> str | None:
    """Generic rejection reason for a whole-file upload, or None."""
    rejected = validate_report_payload(metadata)
    if rejected is not None:
        return rejected
    if not isinstance(file_bytes, (bytes, bytearray)):
        return "The report was rejected."
    if not 0 < len(file_bytes) <= _FULL_FILE_MAX_BYTES:
        return "The report was rejected."
    return None


def encode_multipart_report(metadata: dict, file_bytes: bytes,
                            filename: str = "session.log") -> tuple:
    """(body, content_type) for one whole-file POST. Stdlib only.

    Two parts: a `metadata` JSON part carrying the usual flat fields
    (with the capped excerpt as `log`) and a `file` part with the
    joined session text. Never raises on sane inputs.
    """
    boundary = uuid.uuid4().hex
    try:
        meta_json = json.dumps({
            "app_version": metadata.get("app_version", ""),
            "session_id": metadata.get("session_id", 0),
            "event_count": metadata.get("event_count", 0),
            "comment": metadata.get("comment", ""),
            "log": metadata.get("log", ""),
        }).encode("utf-8")
    except (TypeError, ValueError):
        meta_json = b"{}"
    safe_name = str(filename or "session.log").replace('"', "_")
    head_meta = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="metadata"\r\n'
        "Content-Type: application/json\r\n\r\n"
    ).encode("utf-8")
    head_file = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file";'
        f' filename="{safe_name}"\r\n'
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = (head_meta + meta_json + b"\r\n"
            + head_file + bytes(file_bytes) + tail)
    return body, f"multipart/form-data; boundary={boundary}"


def _read_reply(resp: Any) -> tuple:
    """(ok, message) from a proxy reply. Generic text only."""
    try:
        data = json.loads(resp.read(65536).decode("utf-8", "replace"))
    except ValueError:
        return False, "Upload got an unreadable reply."
    if not isinstance(data, dict):
        return False, "Upload got an unreadable reply."
    if data.get("ok") is True:
        return True, "Report sent. Thank you."
    err = data.get("error")
    if isinstance(err, str) and err.strip():
        return False, err.strip()
    return False, "Upload was rejected."


def send_report(payload: dict, url: str,
                timeout: int = _UPLOAD_TIMEOUT_S) -> tuple:
    """POST the report to the public proxy. Returns (ok, message).

    Stdlib only, so frozen builds need no new dependency. The same flat
    payload as before, with no Authorization header — the app ships no
    secret. Messages stay generic; proxy detail never surfaces in the UI.
    """
    if not url:
        return False, "Upload is not configured on this machine."
    rejected = validate_report_payload(payload)
    if rejected is not None:
        return False, rejected
    try:
        body = json.dumps({
            "app_version": payload.get("app_version", ""),
            "session_id": payload.get("session_id", 0),
            "event_count": payload.get("event_count", 0),
            "comment": payload.get("comment", ""),
            "log": payload.get("log", ""),
        }).encode("utf-8")
    except (TypeError, ValueError):
        return False, "Could not encode the report."
    # Edge bot rules refuse the stock urllib agent outright, so send a
    # browser-style one (proven to pass where the default is refused).
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                " SR-Tracker-Report/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read(65536)
    except urllib.error.HTTPError as e:
        return False, f"Upload failed (status {e.code})."
    except Exception as e:
        return False, f"Upload failed ({type(e).__name__})."
    if status // 100 != 2:
        return False, f"Upload failed (status {status})."
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return False, "Upload got an unreadable reply."
    if not isinstance(data, dict):
        return False, "Upload got an unreadable reply."
    if data.get("ok") is True:
        return True, "Report sent. Thank you."
    # The proxy only ever emits generic text, so its message is safe
    # to show; fall back when it says nothing usable.
    err = data.get("error")
    if isinstance(err, str) and err.strip():
        return False, err.strip()
    return False, "Upload was rejected."


def send_full_report(metadata: dict, file_bytes: bytes, url: str,
                     timeout: int = _FULL_UPLOAD_TIMEOUT_S) -> tuple:
    """POST one whole-file multipart upload. Returns (ok, message).

    The metadata part carries the usual flat fields (with the capped
    excerpt as `log`); the file part carries the joined session text.
    Stdlib only, no Authorization header — the app ships no secret.
    Messages stay generic; proxy detail never surfaces in the UI.
    """
    if not url:
        return False, "Upload is not configured on this machine."
    rejected = validate_full_upload(metadata, file_bytes)
    if rejected is not None:
        return False, rejected
    try:
        body, content_type = encode_multipart_report(metadata, file_bytes)
    except (TypeError, ValueError):
        return False, "Could not encode the report."
    # Edge bot rules refuse the stock urllib agent outright, so send a
    # browser-style one (proven to pass where the default is refused).
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": content_type,
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                " SR-Tracker-Report/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status // 100 != 2:
                return False, f"Upload failed (status {status})."
            return _read_reply(resp)
    except urllib.error.HTTPError as e:
        return False, f"Upload failed (status {e.code})."
    except Exception as e:
        return False, f"Upload failed ({type(e).__name__})."
