"""Build a standalone SR Tracker.exe with PyInstaller.

Replaces the old `SR Tracker.spec` and `build/Taskfile.yml` workflow. The
output layout is:

    dist/SR Tracker/SR Tracker.exe     (2.1 MB bootloader)
    dist/SR Tracker/_internal/         (Python runtime + srt/ + dll/)

Usage:
    python -m tools.build              # onefile-style folder build
    python -m tools.build --clean      # wipe build/ and dist/ first
    python -m tools.build --console    # build with a console (debug)
    python -m tools.build --pack       # build, then pack a Velopack
                                       # portable release into Releases/
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
# Velopack identifiers: packId is NuGet-id-safe (no spaces).
PACK_ID = "TacticalDeux.SRTracker"
PACK_TITLE = "SR Tracker"


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
        "--add-data", "data/monsters.json;data",
        "--add-data", "data/items.json;data",
    ]
    if not console:
        cmd.append("--windowed")
    if uac_admin:
        # Embed an admin manifest so Windows shows a UAC
        # prompt on every launch. Needed for DLL injection
        # into the game without a separate "run as admin" step.
        cmd.append("--uac-admin")
    cmd.append(ENTRY)
    _run(cmd)

    out = PROJECT_ROOT / "dist" / APP_NAME
    if not (out / f"{APP_NAME}.exe").exists():
        print(f"ERROR: expected {out / (APP_NAME + '.exe')}")
        return 1
    print(f"\nbuild complete: {out}")
    return 0


def pack() -> int:
    """Pack the PyInstaller output dir into Releases/ (portable only).

    Portable bundle is on by default; `--noInst` skips the Setup
    installer per the portable-distribution decision. Expects
    `dist/SR Tracker/` from build() to already exist.
    """
    from srt import __version__ as _ver

    pack_dir = PROJECT_ROOT / "dist" / APP_NAME
    if not (pack_dir / f"{APP_NAME}.exe").exists():
        print(f"ERROR: pack dir missing {pack_dir} — run without --pack-only first")
        return 1
    cmd = [
        "vpk", "pack",
        "-u", PACK_ID,
        "-v", _ver,
        "--packTitle", PACK_TITLE,
        "-p", str(pack_dir),
        "-e", f"{APP_NAME}.exe",
        "--noInst",
        "-o", str(PROJECT_ROOT / "Releases"),
    ]
    _run(cmd)
    print(f"\npack complete: {PROJECT_ROOT / 'Releases'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build SR Tracker with PyInstaller.")
    parser.add_argument("--clean", action="store_true", help="Wipe build/ and dist/ first")
    parser.add_argument("--console", action="store_true", help="Build with a console window for debugging")
    parser.add_argument("--no-uac-admin", action="store_true", help="Skip the admin manifest (no UAC prompt)")
    parser.add_argument("--pack", action="store_true", help="Also run vpk pack into Releases/ (portable only)")
    parser.add_argument("--pack-only", action="store_true", help="Skip PyInstaller, only run vpk pack")
    args = parser.parse_args(argv)
    if not args.pack_only:
        rc = build(args.clean, args.console, uac_admin=not args.no_uac_admin)
        if rc != 0:
            return rc
    if args.pack or args.pack_only:
        return pack()
    return 0


if __name__ == "__main__":
    sys.exit(main())
