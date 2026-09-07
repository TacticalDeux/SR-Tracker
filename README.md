# SR Tracker

Desktop tracker for a REMOVED game session. Injects a hook DLL into
the running game, decodes gameplay events, and shows kills / drops / XP /
level in a native UI plus an always-on-top overlay.

The hook DLL and its REMOVED decoder live in `dll/sr_tracker.dll`
(a closed-source binary). Everything else here is the Python/Qt
application that drives it.

## Project layout

```
srt/                  Application package
  app.py              Qt entry point + tray menu
  paths.py            File locations (dev vs frozen bundle)
  theme.py            Dark theme palette + global stylesheet
  settings.py         User settings (JSON in %APPDATA%/SRTracker/)
  db.py               SQLite persistence
  dll.py              ctypes wrapper around sr_tracker.dll
  consumer.py         Background thread that drains the DLL REMOVED
  overlay.py          Frameless / always-on-top HUD window
  main_window.py      Main tracker window (sessions, kills, drops tabs)

tools/
  build.py            Build a standalone exe with PyInstaller
  private/           Private submodule (inject + DLL-probing helpers,
                     not publicly visible — see SR-Tracker-tools)

dll/                  Private submodule: hook DLL source + binary
                      (not publicly visible — see SR-Tracker-dll)
sr_tracker.py         Thin launcher that calls srt.app.main()
REMOVED.py         End-to-end smoke test against the srt package
```

## Running from source

```sh
python sr_tracker.py
```

The first run creates `data/srtracker.db` next to the source. Settings
live in `data/settings.json` while developing. To inject the DLL into
the game:

```sh
python -m REMOVED --launch   # launch the game + inject
python -m REMOVED 12345      # inject into a specific PID
```

## Building a standalone exe

```sh
python -m tools.build --clean
```

Output: `dist/SR Tracker/SR Tracker.exe` (2.1 MB launcher) +
`dist/SR Tracker/_internal/` (Python runtime + the `srt/` package +
the bundled `dll/sr_tracker.dll`).

When frozen, the database and settings file live under
`%LOCALAPPDATA%\SRTracker\` so they survive exe upgrades.

## User data

| What | Dev | Frozen |
| --- | --- | --- |
| Database | `data/srtracker.db` | `%LOCALAPPDATA%\SRTracker\srtracker.db` |
| Settings | `data/settings.json` | `%LOCALAPPDATA%\SRTracker\settings.json` |
| DLL events log | `%TEMP%\REMOVED` (DLL-owned) | same |

## UI overview

- **Main window** (90% of available screen): Summary, Sessions, Kills,
  Drops tabs. Top bar has Start/Stop Tracking, Open Overlay,
  **Lock/Unlock Overlay**, and Reset Session.
- **Overlay**: frameless, always-on-top HUD. Drop-down to pick which
  fields show and an opacity slider. The lock button hides when
  locked, the card dims, and clicks pass through to the game. Unlock
  from the main window.

## Settings (`settings.json`)

```json
{
  "overlay_opacity": 0.88,
  "overlay_locked_opacity": 0.55,
  "overlay_show_kills": true,
  "overlay_show_sc": true,
  "overlay_show_xp": true,
  "overlay_show_level": true,
  "overlay_pos_x": 60,
  "overlay_pos_y": 60,
  "overlay_locked": false
}
```

Unknown keys are ignored on load, so older files upgrade cleanly.
