"""The soul crystal icon, loaded directly from the bundled asset.

The user provided the actual game icon (a 256x256 JPEG) and asked that
we just use it. So that's what this module does — loads the JPEG, keeps
its native aspect, scales it to whatever size the UI needs, and offers
a `dim=True` variant for the locked-overlay state.

The asset lives at `<repo>/assets/soul_crystal.jpg`. When the app is
frozen with PyInstaller, the file is bundled into `_internal/assets/`
and resolved via `sys._MEIPASS`.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

from . import paths


# Asset paths — dev (next to source) and frozen (in the PyInstaller bundle).
_ASSET = Path(__file__).resolve().parent.parent / "assets" / "soul_crystal.png"
_FROZEN_ASSET = paths.assets_dir() / "soul_crystal.png"


# Semantic palette tokens sampled from the source asset. These are the
# canonical "this is the soul crystal" colors and are re-exported in
# `srt.theme` so the global stylesheet can use them as the accent family.
CRYSTAL_DEEP   = "#1f3a5c"
CRYSTAL_MID    = "#5b9bcc"
CRYSTAL_LIGHT  = "#a8d6f0"
CRYSTAL_HIGH   = "#eef8ff"


# Lazily loaded — QPixmap construction needs a QGuiApplication to be
# alive, and this module may be imported at app startup before that
# exists. The first call to `crystal_pixmap()` triggers the load.
_SOURCE: QPixmap | None = None


def _source() -> QPixmap:
    global _SOURCE
    if _SOURCE is None:
        for candidate in (_FROZEN_ASSET, _ASSET):
            if candidate.exists():
                pm = QPixmap(str(candidate))
                if not pm.isNull():
                    _SOURCE = pm
                    return _SOURCE
        _SOURCE = QPixmap()  # empty fallback; never None after first call
    return _SOURCE


def crystal_pixmap(size: int, dim: bool = False) -> QPixmap:
    """Render the crystal scaled to fit a `size` x `size` square.

    `dim=True` desaturates the crystal toward ash-gray for the locked-
    overlay state — the silhouette stays recognizable as a crystal, but
    the colors are drained so the user can tell at a glance the overlay
    is muted.
    """
    if size <= 0:
        return QPixmap()
    src = _source()
    if src.isNull():
        return QPixmap()
    scaled = src.scaled(
        size, size,
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    target = QPixmap(size, size)
    target.fill(Qt.transparent)
    p = QPainter(target)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    x = (size - scaled.width()) // 2
    y = (size - scaled.height()) // 2
    p.drawPixmap(x, y, scaled)
    p.end()

    if dim:
        p = QPainter(target)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setCompositionMode(QPainter.CompositionMode_SourceAtop)
        p.fillRect(target.rect(), QColor("#3a3a48"))
        p.end()

    return target
