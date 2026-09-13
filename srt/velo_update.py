"""Update channel backed by Velopack (portable distribution).

Same worker protocol as the retired hand-rolled updater so the UI is
unchanged: `check_in_background` reports ("available" | "current" |
"dev" | "offline" | "rate-limited" | "unparseable", info) off the UI
thread, downloads emit ("__progress__", (got, total)), failures emit
("__failed__", detail), and a finished download emits
("__downloaded__", pending) for apply-and-restart. Stdlib plus the
`velopack` pip package only.

Unpacked runs (source checkouts, dev `dist\\` test builds without a
Velopack manifest) report "dev" so the UI shows the pull-source
message instead of touching the network.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

REPO_URL = "https://github.com/TacticalDeux/SR-Tracker"

_lock = threading.Lock()


@dataclass
class PendingUpdate:
    """A downloaded-ready Velopack update, shaped for the UI dialog."""
    tag: str
    notes: str
    info: Any


def acquire_lock() -> bool:
    """Single-flight guard for downloads/applies. Caller releases."""
    return _lock.acquire(blocking=False)


def release_lock() -> None:
    try:
        _lock.release()
    except RuntimeError:
        pass


def _manager() -> Any:
    """Build an UpdateManager, or raise when this is not a packed app."""
    import velopack
    feed = os.environ.get("SR_TRACKER_UPDATE_FEED", "").strip()
    # Local end-to-end tests point at a static feed dir via env;
    # default stays on the GitHub releases feed.
    source = velopack.HttpSource(feed) if feed else velopack.GithubSource(REPO_URL)
    mgr = velopack.UpdateManager(source)
    # Touch the locator-backed state so unpacked runs (no manifest,
    # no packages dir) fail here instead of mid-check.
    mgr.get_is_portable()
    mgr.get_current_version()
    return mgr


def check_in_background(current: str, callback: Callable,
                        force: bool = False) -> None:
    """Run the Velopack check off the UI thread; callback(status, info)."""
    def _work() -> None:
        try:
            mgr = _manager()
        except Exception:
            try:
                callback("dev", None)
            except Exception:
                pass
            return
        try:
            found = mgr.check_for_updates()
        except Exception:
            status, info = "offline", None
        else:
            if found is None:
                status, info = "current", None
            else:
                try:
                    asset = found.TargetFullRelease
                    tag = str(asset.Version or "")
                    notes = str(asset.NotesMarkdown or "")
                except Exception:
                    tag, notes = "?", ""
                status = "available"
                info = PendingUpdate(tag=tag, notes=notes, info=found)
        try:
            callback(status, info)
        except Exception:
            pass
    t = threading.Thread(target=_work, daemon=True,
                         name="VeloUpdateCheck")
    t.start()


def download_update(pending: PendingUpdate,
                    progress: Callable | None = None) -> PendingUpdate:
    """Download the pending update (blocking). Returns it for apply."""
    mgr = _manager()
    raw = pending.info

    def _on_progress(*args: Any) -> None:
        pct = 0
        if args:
            try:
                pct = int(args[0])
            except (TypeError, ValueError):
                pct = 0
        if callable(progress):
            try:
                progress(pct, 100)
            except Exception:
                pass

    try:
        mgr.download_updates(raw, _on_progress)
    except TypeError:
        # Older binding without the progress slot; retry bare.
        mgr.download_updates(raw)
    return pending


def apply_and_restart(pending: PendingUpdate) -> None:
    """Apply the downloaded update and restart into it (blocking)."""
    mgr = _manager()
    mgr.apply_updates_and_restart(pending.info)
