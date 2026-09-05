"""The always-on-top HUD overlay.

Visual identity (see `srt.theme`): a parchment strip pinned to the side
of the screen. Parchment background, ash-gray tracked labels in Georgia,
monospace numerals in Consolas. The soul-crystal count sits next to a
hand-traced pixel-art crystal — the design's signature element.

Behavior:
  - Frameless, always-on-top, no taskbar (Tool flag).
  - A child QFrame ("OverlayCard") holds the visible card.
  - The whole card is draggable when unlocked.
  - A settings drawer unfurls from the bottom of the card.
  - When locked, the card dims, the crystal goes ash-gray, the lock
    pill hides, and the whole window becomes click-through.
  - The only way to unlock from outside is via the main window's
    Lock/Unlock button.
"""
from __future__ import annotations
from typing import Callable
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QSlider, QVBoxLayout, QWidget,
)

from . import theme
from .crystal import crystal_pixmap
from .db import Database
from .settings import Settings, SettingsStore



POLL_MS = 500

# Field config: (key, display label, monospace number size)
_FIELDS = (
    ("kills", "KILLS",         22),
    ("sc",    "SOUL CRYSTALS", 22),
    ("xp",    "EXPERIENCE",    16),
    ("level", "LEVEL",         22),
    ("zone",  "ZONE",          14),
)


# ---------------------------------------------------------------------------
# The signature element: a single field row. A pixel-art soul crystal
# sits on the left, a monospace number sits to its right. The two are
# painted into one canvas so the row reads as a single instrument — the
# count and the crystal belong to each other, the way "KILLS 12" and
# its counter-graphic should.
# ---------------------------------------------------------------------------
class _CrystalNumber(QWidget):
    def __init__(self, number_size: int, parent=None):
        super().__init__(parent)
        self._number = "0"
        self._dim = False
        self._number_size = number_size
        # Pre-render the crystal pixmaps at the size we use them. The
        # crystal is 18x22 in the source, scaled with NearestNeighbor
        # so the pixels stay crisp at every display size.
        self._crystal = crystal_pixmap(32, dim=False)
        self._crystal_dim = crystal_pixmap(32, dim=True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(40)

    def set_value(self, text: str) -> None:
        self._number = text
        self.update()

    def set_dim(self, dim: bool) -> None:
        self._dim = dim
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)

        crystal_size = self._crystal.width()
        y = (self.height() - crystal_size) // 2
        pm = self._crystal_dim if self._dim else self._crystal
        p.drawPixmap(0, y, pm)

        # The crystal wants nearest-neighbor crispness, but the numerals
        # shouldn't inherit that — restore antialiasing so the monospace
        # digits render smoothly instead of jagged.
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        # Numbers and crystal sit on the dark card. Unlocked = full
        # brightness parchment-white, locked = ash so the row reads
        # "muted" the same way the crystal does.
        ink = QColor(theme.ASH) if self._dim else QColor(theme.PARCH_BG)
        p.setPen(ink)
        f = QFont("Consolas", self._number_size)
        f.setBold(True)
        f.setStyleHint(QFont.Monospace)
        p.setFont(f)
        rect = QRect(
            crystal_size + 12, 0,
            self.width() - crystal_size - 12, self.height(),
        )
        p.drawText(rect, Qt.AlignVCenter | Qt.AlignRight, self._number)
        p.end()

# ---------------------------------------------------------------------------
# The overlay window.
# ---------------------------------------------------------------------------
class OverlayWindow(QWidget):
    def __init__(
        self,
        db: Database,
        settings_store: SettingsStore,
        get_session_id: Callable[[], int | None],
        on_settings_changed: Callable[[Settings], None],
    ):
        super().__init__()
        self._db = db
        self._settings_store = settings_store
        self._settings = settings_store.load()
        self._get_session_id = get_session_id
        self._on_settings_changed = on_settings_changed

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setStyleSheet(theme.OVERLAY_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("OverlayCard")
        outer.addWidget(self._card)

        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(0)

        # --- header: handle (left) + lock pill (right) ---
        header = QHBoxLayout()
        header.setSpacing(8)
        self._handle = QLabel("SOULS  REMAINING")
        self._handle.setObjectName("OverlayHandle")
        self._handle.setCursor(Qt.SizeAllCursor)
        header.addWidget(self._handle)
        header.addStretch(1)
        self._lock_btn = QPushButton("LOCK")
        self._lock_btn.setObjectName("OverlayPill")
        self._lock_btn.setCursor(Qt.PointingHandCursor)
        self._lock_btn.setFixedHeight(22)
        self._lock_btn.clicked.connect(self._toggle_lock)
        header.addWidget(self._lock_btn)
        layout.addLayout(header)

        layout.addWidget(_hairline())
        layout.addSpacing(10)

        # --- metric rows ---
        self._rows: dict[str, tuple[QLabel, _CrystalNumber]] = {}
        # Each entry: (rule widget, key of the row above it, key of the
        # row below it) — lets us keep a divider hidden when both the
        # rows it separates are hidden, instead of leaving it floating.
        self._hairlines: list[tuple[QFrame, str, str]] = []
        for i, (key, label_text, num_size) in enumerate(_FIELDS):
            row_wrap = QVBoxLayout()
            row_wrap.setSpacing(0)
            line = QHBoxLayout()
            line.setSpacing(10)
            lbl = QLabel(label_text)
            lbl.setObjectName("OverlayLabel")
            lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            line.addWidget(lbl, 1)
            num = _CrystalNumber(num_size)
            num.setMinimumWidth(150)
            line.addWidget(num, 0)
            row_wrap.addLayout(line)
            layout.addLayout(row_wrap)
            self._rows[key] = (lbl, num)

            if i < len(_FIELDS) - 1:
                layout.addSpacing(8)
                rule = _hairline()
                layout.addWidget(rule)
                layout.addSpacing(8)
                self._hairlines.append((rule, key, _FIELDS[i + 1][0]))

        # --- settings drawer (collapsed by default) ---
        self._drawer = QFrame()
        self._drawer.setObjectName("OverlayCard")
        self._drawer.setMaximumHeight(0)
        self._drawer.setVisible(False)
        d_layout = QVBoxLayout(self._drawer)
        d_layout.setContentsMargins(0, 14, 0, 0)
        d_layout.setSpacing(10)

        op_row = QHBoxLayout()
        op_lbl = QLabel("OPACITY")
        op_lbl.setObjectName("OverlayLabel")
        op_row.addWidget(op_lbl)
        self._opacity_slider = QSlider(Qt.Horizontal)
        self._opacity_slider.setRange(20, 100)
        self._opacity_slider.setSingleStep(5)
        self._opacity_slider.setPageStep(10)
        self._opacity_slider.setValue(int(self._settings.overlay_opacity * 100))
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        op_row.addWidget(self._opacity_slider, 1)
        d_layout.addLayout(op_row)

        self._field_checks: dict[str, QCheckBox] = {}
        for key, label_text, _ in _FIELDS:
            cb = QCheckBox(label_text.title())
            cb.setObjectName("OverlayField")
            cb.setChecked(getattr(self._settings, f"overlay_show_{key}"))
            cb.toggled.connect(lambda checked, k=key: self._on_field_toggle(k, checked))
            d_layout.addWidget(cb)
            self._field_checks[key] = cb

        layout.addWidget(self._drawer)
        self._drawer_visible = False

        # --- footer: open drawer ---
        layout.addSpacing(10)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self._drawer_toggle = QPushButton("SETTINGS")
        self._drawer_toggle.setObjectName("OverlayPill")
        self._drawer_toggle.setCursor(Qt.PointingHandCursor)
        self._drawer_toggle.setFixedHeight(20)
        self._drawer_toggle.clicked.connect(self._toggle_drawer)
        footer.addWidget(self._drawer_toggle)
        layout.addLayout(footer)

        # --- refresh timer ---
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        # Drag state
        self._drag_pos: QPoint | None = None
        self._drag_active = False

        # Initial geometry
        self.resize(280, 360)
        self.move(self._settings.overlay_pos_x, self._settings.overlay_pos_y)
        self._apply_opacity()
        self._apply_field_visibility()
        self._clamp_to_screen()
        if self._settings.overlay_locked:
            self._apply_lock_state(True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_overlay_locked(self) -> bool:
        return self._settings.overlay_locked

    def set_locked(self, locked: bool) -> None:
        if locked == self._settings.overlay_locked:
            return
        self._apply_lock_state(locked)
        self._save_settings()

    def toggle_lock(self) -> None:
        self._apply_lock_state(not self._settings.overlay_locked)
        self._save_settings()

    def overlay_position(self) -> tuple[int, int]:
        return self.x(), self.y()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._clamp_to_screen()

    def _clamp_to_screen(self) -> None:
        """Pull a saved position back on-screen if a monitor was
        unplugged or the resolution changed since it was saved — a
        locked overlay you can't see is an overlay you can't unlock."""
        screen = QGuiApplication.screenAt(self.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        x = min(max(self.x(), avail.x()), avail.x() + avail.width() - self.width())
        y = min(max(self.y(), avail.y()), avail.y() + avail.height() - self.height())
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    # ------------------------------------------------------------------
    # Lock state
    # ------------------------------------------------------------------
    def _toggle_lock(self) -> None:
        self._apply_lock_state(not self._settings.overlay_locked)
        self._save_settings()

    def _apply_lock_state(self, locked: bool) -> None:
        self._settings.overlay_locked = locked
        if locked:
            self._lock_btn.hide()
            self._drawer_toggle.hide()
            self._handle.setCursor(Qt.ArrowCursor)
            self._set_drawer_visible(False)
            self._set_pass_through(True)
        else:
            self._lock_btn.show()
            self._drawer_toggle.show()
            self._handle.setCursor(Qt.SizeAllCursor)
            self._set_pass_through(False)
        self._apply_opacity()
        for _, num in self._rows.values():
            num.set_dim(locked)

    def _set_pass_through(self, pass_through: bool) -> None:
        # WA_TransparentForMouseEvents has to be set on the top-level
        # window itself for clicks to actually fall through to whatever
        # is behind it on the desktop — setting it only on children (as
        # before) just forwards events to this window, not past it.
        # The lock pill is already hidden whenever we're locked, so it
        # doesn't need a special case here.
        self.setAttribute(Qt.WA_TransparentForMouseEvents, pass_through)
        for w in self.findChildren(QWidget):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, pass_through)

    # ------------------------------------------------------------------
    # Opacity / fields / drawer
    # ------------------------------------------------------------------
    def _on_opacity_changed(self, value: int) -> None:
        self._settings.overlay_opacity = value / 100.0
        self._apply_opacity()
        self._save_settings()

    def _on_field_toggle(self, key: str, checked: bool) -> None:
        setattr(self._settings, f"overlay_show_{key}", checked)
        self._apply_field_visibility()
        self._save_settings()

    def _apply_opacity(self) -> None:
        opacity = (
            self._settings.overlay_locked_opacity
            if self._settings.overlay_locked
            else self._settings.overlay_opacity
        )
        self.setWindowOpacity(opacity)

    def _apply_field_visibility(self) -> None:
        for key, (lbl, num) in self._rows.items():
            visible = getattr(self._settings, f"overlay_show_{key}")
            lbl.setVisible(visible)
            num.setVisible(visible)
        # A divider only earns its keep if at least one of the rows it
        # separates is still showing.
        for rule, before_key, after_key in self._hairlines:
            before_visible = getattr(self._settings, f"overlay_show_{before_key}")
            after_visible = getattr(self._settings, f"overlay_show_{after_key}")
            rule.setVisible(before_visible or after_visible)
        self._card.adjustSize()
        self.adjustSize()

    def _toggle_drawer(self) -> None:
        self._set_drawer_visible(not self._drawer_visible)

    def _set_drawer_visible(self, visible: bool) -> None:
        self._drawer_visible = visible
        if visible:
            self._drawer.setVisible(True)
            self._drawer.setMaximumHeight(16777215)
        else:
            self._drawer.setMaximumHeight(0)
            self._drawer.setVisible(False)
        self._card.adjustSize()
        self.adjustSize()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        sid = self._get_session_id()
        for key, (_lbl, num) in self._rows.items():
            if sid is None:
                num.set_value("—")
                continue
            try:
                s = self._db.summary(sid)
            except Exception:
                return
            vmap = {
                "kills": s["kills"],
                "sc": s["soul_crystals"],
                "xp": s["xp"],
                "level": s["level"],
                "zone": s.get("current_zone", "—"),
            }
            num.set_value(self._format(key, vmap[key]))

    @staticmethod
    def _format(key: str, value: int) -> str:
        if key == "xp":
            return f"{value:,}"
        return str(value)

    # ------------------------------------------------------------------
    # Drag support
    # ------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and not self._settings.overlay_locked:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._drag_active = True
            e.accept()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_active and self._drag_pos is not None and not self._settings.overlay_locked:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e) -> None:
        if self._drag_active:
            self._settings.overlay_pos_x, self._settings.overlay_pos_y = self.x(), self.y()
            self._save_settings()
        self._drag_active = False
        self._drag_pos = None
        e.accept()

    def _save_settings(self) -> None:
        self._settings_store.save(self._settings)
        if self._on_settings_changed is not None:
            self._on_settings_changed(self._settings)

    def closeEvent(self, e) -> None:
        e.ignore()
        self.hide()


def _hairline() -> QFrame:
    """A 1-pixel parchment rule — the design's only divider."""
    f = QFrame()
    f.setObjectName("OverlayRule")
    f.setFrameShape(QFrame.NoFrame)
    f.setFixedHeight(1)
    return f