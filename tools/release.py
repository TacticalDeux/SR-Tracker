"""One-command release flow: bump version, build, pack, publish.

Wraps the manual loop (edit version, commit, build, vpk pack,
vpk upload) so a release is a single invocation:

    python -m tools.release 1.1.3          # full flow, publishes live
    python -m tools.release 1.1.3 --draft  # upload as draft instead
    python -m tools.release 1.1.3 --skip-upload  # stop after pack
    python -m tools.release 1.1.3 --dry-run      # print only, change nothing
    python -m tools.release 1.1.3 --notes-file notes.md  # custom body
    python -m tools.release 1.1.3 --no-notes     # pack with no release body

Release notes are auto-generated from commit subjects since the
previous vX.Y.Z tag and fed to `vpk pack --releaseNotes`, which is
what becomes the GitHub release body on upload (upload itself takes
no notes flag, so without this the release page shows almost
nothing).

The GitHub token is NEVER passed on the command line: set VPK_TOKEN
in the environment (or type it when prompted) and it is forwarded to
vpk through the environment only.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

from .build import build as _build
from .build import pack as _pack
from .build import PROJECT_ROOT

REPO_URL = "https://github.com/TacticalDeux/SR-Tracker"
CHANNEL = "win"
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
INIT_FILE = PROJECT_ROOT / "srt" / "__init__.py"


def _run(cmd: list[str], env_extra: dict[str, str] | None = None,
         dry_run: bool = False) -> None:
    # Token-safe printer: vpk takes the token from the environment,
    # so the argv itself is always safe to echo.
    print(">>>", " ".join(cmd))
    if dry_run:
        return
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    res = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    if res.returncode != 0:
        sys.exit(res.returncode)


def _git(*args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(PROJECT_ROOT),
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"ERROR: git {' '.join(args)} failed: {res.stderr.strip()}")
        sys.exit(res.returncode)
    return res.stdout.strip()


def _current_version() -> str:
    from srt import __version__ as _ver
    return _ver


# Submodule placeholders (.gitignore documents them as uninitialized):
# their local checkouts always differ from the recorded gitlinks, so
# they would block every release if counted as dirty.
_ALWAYS_DIRTY_OK = ("dll", "tools/private")


def _check_clean(allow_dirty: bool) -> None:
    # Untracked files (Releases/, scratch) never block a release.
    lines = _git("status", "--porcelain").splitlines()
    dirty = []
    for ln in lines:
        if ln.startswith("??"):
            continue
        # Porcelain XY columns shift (staged vs unstaged, and gitlink
        # entries vary between runs), so never slice positionally:
        # the path is the last whitespace-separated token.
        path = ln.split()[-1].strip().strip('"')
        if path in _ALWAYS_DIRTY_OK or path.startswith("tools/private/"):
            continue
        dirty.append(ln)
    if dirty and not allow_dirty:
        print("ERROR: working tree has uncommitted changes:")
        for ln in dirty:
            print(f"  {ln}")
        print("Commit them or re-run with --allow-dirty.")
        sys.exit(1)


_VERSION_LINE_RE = re.compile(r'^(__version__\s*=\s*")[^"]*(")\s*$', re.M)


def _bump_version(new: str, dry_run: bool) -> None:
    # Touch exactly one line: the __version__ assignment. Anything
    # else means the file layout changed under us — refuse to guess.
    text = INIT_FILE.read_text(encoding="utf-8")
    updated, n = _VERSION_LINE_RE.subn(rf"\g<1>{new}\g<2>", text)
    if n != 1:
        print(f"ERROR: expected one __version__ line in {INIT_FILE}, "
              f"found {n}")
        sys.exit(1)
    if dry_run:
        print(f"would set __version__ = {new!r} in {INIT_FILE}")
        return
    INIT_FILE.write_text(updated, encoding="utf-8")


def _commit_bump(new: str, dry_run: bool) -> None:
    if dry_run:
        print(f'would commit: build: bump version to {new}')
        return
    _run(["git", "add", "srt/__init__.py"])
    _run(["git", "commit", "-m", f"build: bump version to {new}"])


def _token() -> str:
    tok = os.environ.get("VPK_TOKEN", "").strip()
    if tok:
        return tok
    try:
        tok = getpass.getpass("GitHub token (VPK_TOKEN, input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)
    if not tok:
        print("ERROR: a token is required for download/upload.")
        sys.exit(1)
    return tok


def _newer(new: str, cur: str) -> bool:
    return tuple(int(p) for p in new.split(".")) > tuple(
        int(p) for p in cur.split("."))


_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _previous_tag(new: str) -> str | None:
    """Highest vX.Y.Z tag strictly below `new`, or None on first release."""
    want = tuple(int(p) for p in new.split("."))
    best: tuple[tuple[int, int, int], str] | None = None
    for line in _git("tag", "--list", "v*").splitlines():
        m = _TAG_RE.match(line.strip())
        if not m:
            continue
        ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if ver < want and (best is None or ver > best[0]):
            best = (ver, line.strip())
    return best[1] if best else None


def _commit_subjects(base: str | None) -> list[str]:
    """Subject lines since `base` (chronological), minus version bumps."""
    rev = f"{base}..HEAD" if base else "HEAD"
    lines = _git("log", rev, "--format=%s", "--no-merges").splitlines()
    # The bump commit is release plumbing, not a user-facing change.
    subs = [s for s in (ln.strip() for ln in lines)
            if s and not s.startswith("build: bump version")]
    subs.reverse()
    return subs


def _render_notes(new: str, prev: str | None,
                  subjects: list[str]) -> str:
    """Group conventional-prefix subjects into a release-notes page."""
    from datetime import date
    feats = [s for s in subjects if s.startswith("feat")]
    fixes = [s for s in subjects if s.startswith("fix")]
    rest = [s for s in subjects if s not in feats and s not in fixes]
    lines = [f"# SR Tracker v{new}", "",
             f"Released {date.today().isoformat()}.", ""]
    if prev:
        lines += [f"Full diff: {REPO_URL}/compare/{prev}...v{new}", ""]
    if feats:
        lines += ["## What's new", *[f"- {s}" for s in feats], ""]
    if fixes:
        lines += ["## Fixes", *[f"- {s}" for s in fixes], ""]
    if rest:
        lines += ["## Other changes", *[f"- {s}" for s in rest], ""]
    if not subjects:
        lines += ["- Maintenance release.", ""]
    return "\n".join(lines).rstrip() + "\n"


def _notes_file(new: str, dry_run: bool) -> Path:
    """Generate notes into Releases/notes-vX.Y.Z.md and return the path."""
    prev = _previous_tag(new)
    subjects = _commit_subjects(prev)
    text = _render_notes(new, prev, subjects)
    path = PROJECT_ROOT / "Releases" / f"notes-v{new}.md"
    if dry_run:
        print(f"would write release notes to {path}:")
        print(text)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"release notes: {len(subjects)} commit(s) since "
          f"{prev or 'the beginning'} -> {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut a new SR Tracker release.")
    parser.add_argument("version", help="New version (e.g. 1.1.3)")
    parser.add_argument("--draft", action="store_true",
                        help="Upload as draft (not visible to clients)")
    parser.add_argument("--skip-upload", action="store_true",
                        help="Stop after pack; do not touch GitHub")
    parser.add_argument("--no-commit", action="store_true",
                        help="Bump the file but do not commit it")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Release with uncommitted changes present")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print every step without changing anything")
    parser.add_argument("--notes-file", default=None,
                        help="Use this markdown file as the release body "
                             "instead of generating one")
    parser.add_argument("--no-notes", action="store_true",
                        help="Pack without release notes")
    args = parser.parse_args(argv)

    m = VERSION_RE.match(args.version.strip())
    if not m:
        print("ERROR: version must look like 1.2.3")
        return 1
    new = m.group(1)
    cur = _current_version()
    if not _newer(new, cur):
        print(f"ERROR: {new} is not newer than current {cur}")
        return 1

    token = "" if (args.skip_upload or args.dry_run) else _token()
    env = {"VPK_TOKEN": token} if token else None

    _check_clean(args.allow_dirty)
    _bump_version(new, args.dry_run)
    if not args.no_commit:
        _commit_bump(new, args.dry_run)

    # Pull prior release assets so pack emits deltas and the feed
    # stays cumulative across versions.
    _run(["vpk", "download", "github", "--repoUrl", REPO_URL,
          "-o", str(PROJECT_ROOT / "Releases")], env_extra=env,
         dry_run=args.dry_run)
    if args.no_notes:
        notes: str | Path | None = None
    elif args.notes_file:
        notes = args.notes_file
    else:
        notes = _notes_file(new, args.dry_run)
    if args.dry_run:
        print("would run: PyInstaller build --clean")
        print(f"would run: vpk pack into Releases/ (notes={notes})")
    else:
        if _build(clean=True, console=False) != 0:
            return 1
        if _pack(notes, new) != 0:
            return 1
    if args.skip_upload:
        print("stopping before upload (--skip-upload).")
        return 0

    upload = ["vpk", "upload", "github", "-o", str(PROJECT_ROOT / "Releases"),
              "-c", CHANNEL, "--repoUrl", REPO_URL,
              "--tag", f"v{new}"]
    if not args.draft:
        upload.append("--publish")
    if not args.dry_run:
        answer = input(f"Upload {new} to {REPO_URL} "
                       f"({'DRAFT' if args.draft else 'LIVE'})? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted before upload.")
            return 1
    _run(upload, env_extra=env, dry_run=args.dry_run)
    print(f"\nrelease {new} -> {REPO_URL}/releases/tag/v{new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
