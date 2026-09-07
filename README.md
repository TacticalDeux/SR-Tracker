# SR Tracker

Desktop tracker for Soul's Remnant (REMOVED game session). Injects a hook DLL into
the running game, decodes gameplay events from REMOVED, and shows kills / drops /
soul crystals / XP / level / zone / deaths in a native Qt UI plus an always-on-top overlay.

The hook DLL lives in `dll/sr_tracker.dll` (C++ source in `dll/REMOVED` alongside
the binary). Everything else here is the Python/Qt application that drives it.

## Project layout

```
srt/                  Application package
  app.py              Qt entry point + tray menu (Toggle Overlay / Show Main / Quit)
  main_window.py      Main tracker window (Summary, Sessions, Kills, Drops, Zones, Graphs, Overlay + Debug)
  overlay.py          Frameless / always-on-top HUD window
  consumer.py         Background thread that drains the DLL REMOVED into SQLite
  dll.py              ctypes wrapper around sr_tracker.dll (Global\REMOVED)
  db.py               SQLite persistence (sessions, kills, drops, deaths, xp_events, zone_visits, ...)
  settings.py         User settings (Settings dataclass + thread-safe SettingsStore)
  paths.py            File locations (dev vs frozen bundle)
  theme.py            Ink/Parch/Crystal palette + global QSS (+ overlay QSS)
  charts.py           SeriesChart / SpiderChart behind the Graphs tab
  crystal.py          Soul-crystal pixmap rendering (Summary orb)
  names.py            Monster/item name lookups
  debug_console.py    Dev-mode event streaming console (Debug tab)

tools/
  build.py            Build a standalone exe with PyInstaller (--clean / --console / --no-uac-admin)
  REMOVED       REMOVED
  REMOVED.py    Name-extraction helper
  REMOVED.py        Hang diagnostics
  REMOVED.py    DLL export checker
  private/            Private submodule

dll/                  Private hook DLL (submodule pointer; binary bundled into releases)
  sr_tracker.dll      Hook DLL binary (bundled into the exe)
  REMOVED      Hook DLL source
  REMOVED.txt     Decoder helper data

assets/
  soul_crystal.png    Crystal icon bundled into the exe

sr_tracker.py         Thin launcher that calls srt.app.main()
SR Tracker.spec       Legacy PyInstaller spec (superseded by tools/build.py)
```

No `requirements*.txt` — you need Python 3 + PySide6 + PyInstaller.

## Running from source

```sh
python sr_tracker.py
# equivalent: python -m srt.app
```

The first run creates `data/srtracker.db` next to the source. Settings
live in `data/settings.json` while developing.

To inject the DLL into the game (default target `REMOVED`):

```sh
python -m REMOVED               # auto-find the game
python -m REMOVED 12345         # inject into a specific PID
python -m REMOVED --launch      # launch the game then inject
python -m REMOVED --name proc.exe  # match a different process name
```

In the UI, **Start Tracking** does this automatically: finds the game PID →
checks if the DLL is loaded → injects via `REMOVED` → waits for
install → starts the `EventConsumer`, which drains the `Global\REMOVED`
REMOVED into SQLite. **Stop** ends the session; **Reset session** clears
kills/drops/XP/etc. for the current session id (keeps the id).

Deaths are recorded from the game's death screen with the exact toll
(XP / money / items lost); pickups resolve per drop (yours vs others).

## Building a standalone exe

```sh
python -m tools.build --clean
python -m tools.build --console    # debug build with a console window
python -m tools.build --no-uac-admin  # skip the REMOVED manifest
```

Output: `dist/SR Tracker/SR Tracker.exe` (small bootloader) +
`dist/SR Tracker/_internal/` (Python runtime + the `srt/` package +
bundled `dll/sr_tracker.dll` + `assets/soul_crystal.png`).

The exe embeds a `REMOVED` manifest by default (REMOVED on every
launch) so `REMOVED` / `REMOVED` injection works without a
separate "run as admin" step.

When frozen, the database and settings file live under
`%LOCALAPPDATA%\SRTracker\` so they survive exe upgrades.

## User data

| What | Dev | Frozen |
| --- | --- | --- |
| Database | `data/srtracker.db` | `%LOCALAPPDATA%\SRTracker\srtracker.db` |
| Settings | `data/settings.json` | `%LOCALAPPDATA%\SRTracker\settings.json` |
| DLL events log | `%TEMP%\REMOVED` (DLL-owned) | same |

SQLite tables: `sessions`, `kills`, `drops`, `deaths`, `xp_events`, `damage`,
`spawn_notifications`, `events`, `zone_visits` (+ `zone_stats` view), with
migration logic for legacy DBs.

## UI overview

- **Main window**: 7 tabs + Debug.
  - **Summary**: soul-crystal orb + at-a-glance metrics. Every count states
    what it means: kills/drops read "yours / session total", soul crystals
    read "picked up / total" (your crystals only), XP is the session total.
  - **Sessions**: session list (double-click a row to open details) with the
    same yours/total labeling; detail dialog adds a per-zone breakdown.
  - **Kills**: time / enemy / mob / mine.
  - **Drops**: time / drop / item / qty / owner / status (On Ground, picked
    up by you/other player, destroyed).
  - **Zones**: per-zone breakdown.
  - **Graphs**: metric/chart/zone selectors + session selector + Details / Refresh.
  - **Overlay**: all overlay settings (fields, order, opacity, scale, colors, orientation).
   - **Debug**: dev event stream (`debug_console.py`).
  - Header band: **Start/Stop Tracking** (primary), **Show overlay**,
    **Lock/Unlock overlay** toggle, **Reset session** (destructive, confirms first).
  - Status bar with event counters. Tray icon: Toggle Overlay / Show Main Window / Quit
    (Quit routes through `closeEvent`: stops consumer, uninstalls hooks, releases DB).
- **Overlay**: frameless, always-on-top HUD. Rows size to their full text —
  long zone names and big counts widen the window instead of clipping.
  - 7 toggleable fields: kills, sc (soul crystals, picked up / total),
    xp, level, zone, deaths, xp_lost — drag-to-reorder (top to bottom).
  - Large counts render compact (1.5K, 3.4M, 5.6B); exact values on hover
    and in the main window.
  - Two opacity paths: card-background alpha + whole-window opacity, each with a
    separate locked value. Scale slider 70%–150%. Text + locked-text color pickers.
    Vertical (stacked rows) or horizontal (strip) orientation.
  - Draggable when unlocked; when locked the handle hides, the card dims, and
    clicks pass through to the game. Position saved as `overlay_pos_x/y` and
    clamped to the screen on startup. Unlock from the main window header or Overlay tab.

## Settings (`settings.json`)

```json
{
  "overlay_opacity": 0.88,
  "overlay_locked_opacity": 0.55,
  "overlay_window_opacity": 1.0,
  "overlay_locked_window_opacity": 1.0,
  "overlay_text_color": "#e8dfc8",
  "overlay_locked_text_color": "#3a3a48",
  "overlay_scale": 1.0,
  "overlay_show_kills": true,
  "overlay_show_sc": true,
  "overlay_show_xp": true,
  "overlay_show_level": true,
  "overlay_show_zone": true,
  "overlay_show_deaths": true,
  "overlay_show_xp_lost": true,
  "overlay_field_order": ["kills", "sc", "xp", "level", "zone", "deaths", "xp_lost"],
  "overlay_pos_x": 60,
  "overlay_pos_y": 60,
  "overlay_locked": false,
  "overlay_orientation": "vertical"
}
```

Unknown keys are ignored on load and missing keys fall back to defaults, so older
files upgrade cleanly. Corrupt files fall back to defaults; saves are atomic
(write-temp-then-replace) and never crash the UI.
