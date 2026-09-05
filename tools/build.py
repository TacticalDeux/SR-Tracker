"""Build a standalone SR Tracker.exe with PyInstaller.

Replaces the old `SR Tracker.spec` and `build/Taskfile.yml` workflow. The
output layout is:

    dist/SR Tracker/SR Tracker.exe     (2.1 MB bootloader)
    dist/SR Tracker/_internal/         (Python runtime + srt/ + dll/)

Usage:
    python -m tools.build              # onefile-style folder build
    python -m tools.build --clean      # wipe build/ and dist/ first
    python -m tools.build --console    # build with a console (debug)
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "SR Tracker"
ENTRY = "sr_tracker.py"  # thin launcher that imports srt.app


def _run(cmd: list[str]) -> None:
    print(">>>", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if res.returncode != 0:
        sys.exit(res.returncode)


def build(clean: bool, console: bool, uac_admin: bool = True) -> int:
    if clean:
        for d in ("build", "dist"):
            p = PROJECT_ROOT / d
            if p.exists():
                print(f"removing {p}")
                shutil.rmtree(p, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", APP_NAME,
        "--add-binary", "dll/sr_tracker.dll;dll",
        "--add-data", "assets/soul_crystal.png;assets",
    ]
    if not console:
        cmd.append("--windowed")
    if uac_admin:
        # Embed a REMOVED manifest so Windows shows a UAC
        # prompt on every launch. Needed for REMOVED/REMOVED
        # injection into the game without a separate "run as admin" step.
        cmd.append("--uac-admin")
    cmd.append(ENTRY)
    _run(cmd)

    out = PROJECT_ROOT / "dist" / APP_NAME
    if not (out / f"{APP_NAME}.exe").exists():
        print(f"ERROR: expected {out / (APP_NAME + '.exe')}")
        return 1
    print(f"\nbuild complete: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build SR Tracker with PyInstaller.")
    parser.add_argument("--clean", action="store_true", help="Wipe build/ and dist/ first")
    parser.add_argument("--console", action="store_true", help="Build with a console window for debugging")
    parser.add_argument("--no-uac-admin", action="store_true", help="Skip the REMOVED manifest (no REMOVED)")
    args = parser.parse_args(argv)
    return build(args.clean, args.console, uac_admin=not args.no_uac_admin)


if __name__ == "__main__":
    sys.exit(main())
