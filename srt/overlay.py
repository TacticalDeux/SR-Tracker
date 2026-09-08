"""The always-on-top HUD overlay.

Visual identity (see `srt.theme`): a dark card pinned to the side of the
screen. Ash-gray tracked labels in Segoe UI, monospace numerals in
Consolas — every row sizes to its full text (numbers, zone names)
instead of clipping, and the window shrink-wraps the widest row so
all rows stay visually equal.

Behavior:
  - Frameless, always-on-top, no taskbar (Tool flag).
  - A child QFrame ("OverlayCard") holds the visible card; the drawer
    is transparent so there is never a box inside a box.
  - The whole card is draggable when unlocked.
  - A settings drawer unfurls from the bottom of the card.
  - Background sliders change the card's background only — text
    always renders at full opacity; Opacity sliders fade the whole
    window, background and text together. A text-color picker paints
    the stats text. When locked, the text dims to ash-gray and the
    lock pill hides.
  - When locked the whole window becomes click-through; the only way
    to unlock from outside is via the main window's Lock/Unlock button.
  - Size is layout-driven: the window shrink-wraps its content and
    re-fits whenever values, visibility, or orientation change.
"""
from __future__ import annotations
from typing import Callable
from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from . import theme
from .db import Database
from .settings import (
    FIELD_SCOPES,
    OVERLAY_FIELD_KEYS,
    Settings,
    SettingsStore,
)


#: Per-field reset scopes. "visit" resets on zone change, "session"
#: persists through the session.
VISIT_SCOPE = "visit"
SESSION_SCOPE = "session"


def field_scope(settings, key: str) -> str:
    """Effective reset scope for one overlay field.

    Unknown/new fields and junk values default to "session" (today's
    behavior). "level" is account-scoped — it has no visit meaning —
    so it always reads session-wide regardless of its stored entry."""
    if key == "level":
        return SESSION_SCOPE
    raw = None
    try:
        raw = (getattr(settings, "overlay_field_scope", None) or {}).get(key)
    except AttributeError:
        raw = None
    return raw if raw in FIELD_SCOPES else SESSION_SCOPE


def all_field_scopes(settings) -> dict[str, str]:
    """Effective scope for every known overlay field."""
    return {k: field_scope(settings, k) for k in OVERLAY_FIELD_KEYS}



POLL_MS = 500

# Field config: (key, display label, monospace number size)
_FIELDS = (
    ("kills",   "KILLS",         22),
    ("sc",      "SOUL CRYSTALS", 22),
    ("xp",      "EXPERIENCE",    16),
    ("level",   "LEVEL",         22),
    ("zone",    "ZONE",          14),
    ("deaths",  "DEATHS",        22),
    ("xp_lost", "XP LOST",       16),
    ("xp_hr",   "XP/HR",         16),
    ("dps",     "DPS",           16),
)

#: Toggleable fields for settings UIs (the main window's Overlay tab
#: builds its field list from this, so the two never drift apart).
OVERLAY_FIELDS = (
    ("kills", "Kills"),
    ("sc", "Soul crystals"),
    ("xp", "Experience"),
    ("level", "Level"),
    ("zone", "Zone"),
    ("deaths", "Deaths"),
    ("xp_lost", "XP lost"),
    ("xp_hr", "XP/hr"),
    ("dps", "DPS"),
)

_FIELD_MAP = {key: (label, size) for key, label, size in _FIELDS}
_OVERLAY_LABELS = dict(OVERLAY_FIELDS)


def ordered_fields(settings) -> list[tuple[str, str, int]]:
    """Field defs in the user's display order.

    Unknown keys are dropped and known keys missing from the saved
    order append at the end, so older settings files and future
    fields both degrade gracefully."""
    seen: set[str] = set()
    out: list[tuple[str, str, int]] = []
    for key in getattr(settings, "overlay_field_order", None) or ():
        if key in _FIELD_MAP and key not in seen:
            seen.add(key)
            label, size = _FIELD_MAP[key]
            out.append((key, label, size))
    for key, label, size in _FIELDS:
        if key not in seen:
            out.append((key, label, size))
    return out


def _valid_color(value: str, fallback: str) -> str:
    """The picked color, or the fallback when the stored value is junk."""
    return value if QColor(value).isValid() else fallback

_ORIENTATIONS = ("vertical", "horizontal")


def _contrast_text(hex_color: str) -> str:
    """Dark or light foreground that reads on the given background."""
    c = QColor(hex_color)
    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return "#1a1424" if lum > 128 else "#e8dfc8"


def _compact_number(value: int) -> str:
    """Compact large counts with K/M/B/T suffixes (1.2K, 3.4M, 5.6B, 7.8T).

    Values under 1000 render exactly; larger ones keep one decimal and
    strip a trailing .0 so 1000 -> "1K", 1500 -> "1.5K". The sign is
    preserved. T is the largest suffix — anything bigger stays in T."""
    sign = "-" if value < 0 else ""
    n = abs(value)
    if n < 1000:
        return f"{sign}{n}"
    for threshold, suffix in ((10 ** 12, "T"), (10 ** 9, "B"),
                              (10 ** 6, "M"), (10 ** 3, "K")):
        if n >= threshold:
            v = n / threshold
            text = f"{v:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"
    return f"{sign}{n}"  # unreachable, kept for safety


def _exact_number(value: int) -> str:
    """Full exact count with commas for tooltips (1,234,567)."""
    return f"{value:,}"


def _compact_rate(value: float) -> str:
    """Compact a per-hour/DPS rate, keeping one decimal (21.6K, 15.0).

    Counts drop a trailing .0 (1K); rates keep it (15.0 DPS) so a
    rate never reads as an exact count. T is the largest suffix."""
    sign = "-" if value < 0 else ""
    n = abs(float(value))
    if n < 1000:
        return f"{sign}{n:.1f}"
    for threshold, suffix in ((10 ** 12, "T"), (10 ** 9, "B"),
                              (10 ** 6, "M"), (10 ** 3, "K")):
        if n >= threshold:
            return f"{sign}{n / threshold:.1f}{suffix}"
    return f"{sign}{n:.1f}"  # unreachable, kept for safety


# ---------------------------------------------------------------------------
# Field labels: tracked uppercase names that size to their full text.
# sizeHint/minimumSizeHint report the full-text width (measured with
# QFontMetrics at the active font) so the window shrink-wraps the
# widest row and every row stretches to that same width — visually
# equal and balanced. Eliding survives only as a last-resort fallback
# when the window is squeezed (e.g. clamped to a screen edge).
# ---------------------------------------------------------------------------
class _FitLabel(QLabel):
    def __init__(self, text: str, align=Qt.AlignVCenter | Qt.AlignLeft,
                 parent=None):
        super().__init__(parent)
        self._full = text
        self._align = align
        self.setAlignment(align)
        self.setObjectName("OverlayLabel")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(40)
        self._elide()

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        avail = max(self.width(), 20)
        super().setText(QFontMetrics(self.font()).elidedText(
            self._full, Qt.ElideRight, avail))

    def _full_width(self) -> int:
        return QFontMetrics(self.font()).horizontalAdvance(self._full) + 8

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(max(self._full_width(), hint.width()), hint.height())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(max(self._full_width(), hint.width(), 40),
                     hint.height())


# ---------------------------------------------------------------------------
# Value labels: right-aligned monospace numerals that size to their
# full text instead of clipping. The minimum width is measured with
# QFontMetrics at the active scaled size, so long values (zone names
# like "Snowy Mountain", big counts) widen the window rather than
# truncating to "Snowy Mo...". The window shrink-wraps the widest row
# so all rows stay visually equal. Font-shrinkage survives only as a
# last-resort fallback when the window is squeezed.
# ---------------------------------------------------------------------------
class _FitNumber(QLabel):
    def __init__(self, number_size: int, align=Qt.AlignRight | Qt.AlignVCenter,
                 parent=None):
        super().__init__(parent)
        self._base_size = number_size
        self._floor = 0
        self._dim = False
        self._normal = theme.PARCH_BG
        self._locked_color = theme.ASH
        self._align = align
        self.setObjectName("OverlayNumber")
        self.setAlignment(align)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_floor(self, px: int) -> None:
        """Stable floor width (scaled) so short values don't jitter the
        window; content wider than the floor still widens it."""
        self._floor = max(0, int(px))
        self._update_minimum()

    def _base_font(self) -> QFont:
        f = QFont("Consolas", self._base_size)
        f.setBold(True)
        f.setStyleHint(QFont.Monospace)
        return f

    def _content_width(self) -> int:
        return QFontMetrics(self._base_font()).horizontalAdvance(
            self.text()) + 12

    def _update_minimum(self) -> None:
        self.setMinimumWidth(max(self._floor, self._content_width(), 20))

    def set_value(self, text: str) -> None:
        if text == self.text():
            return
        self.setText(text)
        self._update_minimum()
        self._refit()

    def set_colors(self, normal: str, locked: str) -> None:
        """The picked stats color and the locked-state color. Stored so
        a later lock flip re-applies them instead of falling back to a
        hardcoded pair (which used to clobber the picked color)."""
        if (normal, locked) == (self._normal, self._locked_color):
            return
        self._normal = normal
        self._locked_color = locked
        self._repaint()

    def set_dim(self, dim: bool) -> None:
        if dim == self._dim:
            return
        self._dim = dim
        self._repaint()

    def _repaint(self) -> None:
        # Inline color only; font, padding and the rest still come from
        # the OverlayNumber QSS rule.
        ink = self._locked_color if self._dim else self._normal
        self.setStyleSheet(f"color: {ink};")

    def base_size(self) -> int:
        return self._base_size

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._refit()

    def _refit(self) -> None:
        avail = max(self.width() - 2, 20)
        size = self._base_size
        while size > 6:
            f = QFont("Consolas", size)
            f.setBold(True)
            f.setStyleHint(QFont.Monospace)
            if QFontMetrics(f).horizontalAdvance(self.text()) <= avail:
                break
            size -= 1
        self.setFont(f)
        self.setMinimumHeight(QFontMetrics(f).height() + 8)

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
        # Translucent (not just NoSystemBackground) so the rgba card
        # fill composites against the game behind the window. Without
        # this the backing store stays opaque: lowering the slider
        # only darkened the card toward black instead of fading it
        # out, and the text could never float backgroundless.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._apply_text_color()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("OverlayCard")
        outer.addWidget(self._card)

        layout = QVBoxLayout(self._card)
        self._card_layout = layout
        margin_h, margin_v = self._scaled_margins(self._overlay_scale())
        layout.setContentsMargins(margin_h, margin_v, margin_h, margin_v)
        layout.setSpacing(0)

        # --- header: handle (left) + lock pill (right) ---
        header = QHBoxLayout()
        header.setSpacing(8)
        self._handle = _FitLabel("SOUL'S REMNANT",
                                 Qt.AlignVCenter | Qt.AlignLeft)
        self._handle.setObjectName("OverlayHandle")
        self._handle.setCursor(Qt.SizeAllCursor)
        header.addWidget(self._handle, 1)
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

        # --- metric rows (rebuildable: an orientation switch throws the
        # whole box away and re-creates it, which is simpler and safer
        # than swapping layouts on live widgets) ---
        self._rows: dict[str, tuple[QLabel, _FitNumber]] = {}
        # Each entry: (rule widget, key of the row above it, key of the
        # row below it) — lets us keep a divider hidden when both the
        # rows it separates are hidden, instead of leaving it floating.
        self._hairlines: list[tuple[QFrame, str, str]] = []
        self._fields_host = QVBoxLayout()
        self._fields_host.setContentsMargins(0, 0, 0, 0)
        self._fields_host.setSpacing(0)
        layout.addLayout(self._fields_host)
        # Last formatted values, so a rebuild can re-paint the rows
        # without waiting for the next poll. Tips carry the exact
        # full counts behind compact K/M/B/T text.
        self._last_values: dict[str, str] = {}
        self._last_tips: dict[str, str] = {}
        self._build_fields_box()

        # --- settings drawer (collapsed by default). Transparent on
        # purpose: the drawer lives inside the card, so giving it its
        # own bordered background produced a visible box inside a box.
        self._drawer = QFrame()
        self._drawer.setObjectName("OverlayDrawer")
        self._drawer.setMaximumHeight(0)
        self._drawer.setVisible(False)
        d_layout = QVBoxLayout(self._drawer)
        d_layout.setContentsMargins(0, 14, 0, 0)
        d_layout.setSpacing(10)

        op_row = QHBoxLayout()
        op_lbl = QLabel("BACKGROUND")
        op_lbl.setObjectName("OverlayLabel")
        op_row.addWidget(op_lbl)
        self._opacity_slider = QSlider(Qt.Horizontal)
        # 0 == backgroundless: the card and its border both vanish,
        # leaving the text floating over the game.
        self._opacity_slider.setRange(0, 100)
        self._opacity_slider.setSingleStep(5)
        self._opacity_slider.setPageStep(10)
        self._opacity_slider.setValue(int(self._settings.overlay_opacity * 100))
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        op_row.addWidget(self._opacity_slider, 1)
        d_layout.addLayout(op_row)

        # Locking dims the card to overlay_locked_opacity, which the main
        # slider never touched — so locking looked "hardcoded". A second
        # slider owns the locked value; set both while unlocked.
        locked_row = QHBoxLayout()
        locked_lbl = QLabel("LOCKED BACKGROUND")
        locked_lbl.setObjectName("OverlayLabel")
        locked_row.addWidget(locked_lbl)
        self._locked_opacity_slider = QSlider(Qt.Horizontal)
        self._locked_opacity_slider.setRange(0, 100)
        self._locked_opacity_slider.setSingleStep(5)
        self._locked_opacity_slider.setPageStep(10)
        self._locked_opacity_slider.setValue(
            int(self._settings.overlay_locked_opacity * 100))
        self._locked_opacity_slider.valueChanged.connect(
            self._on_locked_opacity_changed)
        locked_row.addWidget(self._locked_opacity_slider, 1)
        d_layout.addLayout(locked_row)

        # Whole-window opacity (background AND text together) for both
        # lock states — the counterpart to the background-only sliders
        # above. 0 fades the overlay out entirely.
        win_row = QHBoxLayout()
        win_lbl = QLabel("OPACITY")
        win_lbl.setObjectName("OverlayLabel")
        win_lbl.setToolTip("Fades background and text together")
        win_row.addWidget(win_lbl)
        self._win_opacity_slider = QSlider(Qt.Horizontal)
        self._win_opacity_slider.setRange(0, 100)
        self._win_opacity_slider.setSingleStep(5)
        self._win_opacity_slider.setPageStep(10)
        self._win_opacity_slider.setValue(
            int(self._settings.overlay_window_opacity * 100))
        self._win_opacity_slider.valueChanged.connect(
            self._on_window_opacity_changed)
        win_row.addWidget(self._win_opacity_slider, 1)
        d_layout.addLayout(win_row)

        locked_win_row = QHBoxLayout()
        locked_win_lbl = QLabel("LOCKED OPACITY")
        locked_win_lbl.setObjectName("OverlayLabel")
        locked_win_lbl.setToolTip("Fades background and text together")
        locked_win_row.addWidget(locked_win_lbl)
        self._locked_win_opacity_slider = QSlider(Qt.Horizontal)
        self._locked_win_opacity_slider.setRange(0, 100)
        self._locked_win_opacity_slider.setSingleStep(5)
        self._locked_win_opacity_slider.setPageStep(10)
        self._locked_win_opacity_slider.setValue(
            int(self._settings.overlay_locked_window_opacity * 100))
        self._locked_win_opacity_slider.valueChanged.connect(
            self._on_locked_window_opacity_changed)
        locked_win_row.addWidget(self._locked_win_opacity_slider, 1)
        d_layout.addLayout(locked_win_row)

        # Content scale: numbers, labels and the header handle grow or
        # shrink together (70% .. 150% of the designed size).
        scale_row = QHBoxLayout()
        scale_lbl = QLabel("SCALE")
        scale_lbl.setObjectName("OverlayLabel")
        scale_lbl.setToolTip("Scales the overlay text")
        scale_row.addWidget(scale_lbl)
        self._scale_slider = QSlider(Qt.Horizontal)
        self._scale_slider.setRange(70, 150)
        self._scale_slider.setSingleStep(5)
        self._scale_slider.setPageStep(10)
        self._scale_slider.setValue(
            int(round(self._overlay_scale() * 100)))
        self._scale_slider.valueChanged.connect(self._on_scale_changed)
        scale_row.addWidget(self._scale_slider, 1)
        d_layout.addLayout(scale_row)

        # Stats text color: a swatch button showing the current hex.
        color_row = QHBoxLayout()
        color_lbl = QLabel("TEXT COLOR")
        color_lbl.setObjectName("OverlayLabel")
        color_row.addWidget(color_lbl)
        self._color_btn = QPushButton()
        self._color_btn.setCursor(Qt.PointingHandCursor)
        self._color_btn.setToolTip("Pick the stats text color")
        self._color_btn.clicked.connect(self._on_pick_text_color)
        color_row.addWidget(self._color_btn, 1)
        d_layout.addLayout(color_row)
        self._sync_color_button()

        # Locked-state numbers get their own color: a swatch button
        # showing the current hex.
        locked_color_row = QHBoxLayout()
        locked_color_lbl = QLabel("LOCKED TEXT")
        locked_color_lbl.setObjectName("OverlayLabel")
        locked_color_lbl.setToolTip("Numbers while the overlay is locked")
        locked_color_row.addWidget(locked_color_lbl)
        self._locked_color_btn = QPushButton()
        self._locked_color_btn.setCursor(Qt.PointingHandCursor)
        self._locked_color_btn.setToolTip("Pick the locked stats text color")
        self._locked_color_btn.clicked.connect(
            self._on_pick_locked_text_color)
        locked_color_row.addWidget(self._locked_color_btn, 1)
        d_layout.addLayout(locked_color_row)
        self._sync_locked_color_button()

        self._field_checks: dict[str, QCheckBox] = {}
        for key, _label_text, _ in ordered_fields(self._settings):
            cb = QCheckBox(_OVERLAY_LABELS.get(key, key.title()))
            cb.setObjectName("OverlayField")
            cb.setChecked(bool(getattr(self._settings, f"overlay_show_{key}",
                                       True)))
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

        # Geometry is layout-driven: the window shrink-wraps its content
        # (see _build_fields_box / _refit_window) instead of assuming a
        # fixed size that real values then overflow. The floor scales
        # with the content scale so a scaled-down overlay can actually
        # get smaller instead of clipping inside a full-size minimum.
        self.setMinimumSize(*self._scaled_minimum(self._overlay_scale()))
        self._last_hint = None
        self.move(self._settings.overlay_pos_x, self._settings.overlay_pos_y)
        self._apply_bg()
        self._apply_window_opacity()
        self._apply_field_visibility()
        self._clamp_to_screen()
        if self._settings.overlay_locked:
            self._apply_lock_state(True)

    def _is_horizontal(self) -> bool:
        return self._settings.overlay_orientation == "horizontal"

    def _build_fields_box(self) -> None:
        """(Re)create the metric rows for the current orientation.

        Vertical mode stacks label-left/number-right rows with rules
        between them. Horizontal mode lays the fields out as columns —
        label on top, crystal-number below — with plain spacing instead
        of rules. The old box is detached and scheduled for deletion;
        values, dim state and visibility are re-applied afterwards."""
        while self._fields_host.count():
            item = self._fields_host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._rows = {}
        self._hairlines = []
        horizontal = self._is_horizontal()

        box = QWidget()
        if horizontal:
            box_lay = QHBoxLayout(box)
            box_lay.setSpacing(20)
        else:
            box_lay = QVBoxLayout(box)
            box_lay.setSpacing(0)
        box_lay.setContentsMargins(0, 0, 0, 0)

        scale = self._overlay_scale()
        ordered = ordered_fields(self._settings)
        for i, (key, label_text, num_size) in enumerate(ordered):
            row = QWidget()
            if horizontal:
                lbl = _FitLabel(label_text, Qt.AlignCenter)
                num = _FitNumber(max(6, int(round(num_size * scale))),
                                 Qt.AlignCenter)
                rl = QVBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(2)
                rl.addWidget(lbl)
                # Narrow floors so a scaled-down strip shrink-wraps
                # instead of holding the window wide; content wider
                # than the floor (zone names, big counts measured at
                # the scaled size) still widens the window via
                # _FitNumber._update_minimum, keeping all columns equal.
                num.set_floor(max(50, int(round(90 * scale))))
                rl.addWidget(num)
            else:
                lbl = _FitLabel(label_text,
                                Qt.AlignVCenter | Qt.AlignLeft)
                num = _FitNumber(max(6, int(round(num_size * scale))))
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(10)
                rl.addWidget(lbl, 1)
                num.set_floor(max(60, int(round(120 * scale))))
                rl.addWidget(num, 0)
            box_lay.addWidget(row)
            self._rows[key] = (lbl, num)
            num.set_colors(self._text_color, self._locked_text_color)
            if not horizontal and i < len(ordered) - 1:
                box_lay.addSpacing(8)
                rule = _hairline()
                box_lay.addWidget(rule)
                box_lay.addSpacing(8)
                self._hairlines.append((rule, key, ordered[i + 1][0]))

        self._fields_host.addWidget(box)
        for key, value in self._last_values.items():
            if key in self._rows:
                self._rows[key][1].set_value(value)
                if key in self._last_tips:
                    self._rows[key][1].setToolTip(self._last_tips[key])
        for _, num in self._rows.values():
            num.set_dim(self._settings.overlay_locked)
        self._apply_field_visibility()
        self._refit_window_soon()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_overlay_locked(self) -> bool:
        return self._settings.overlay_locked

    def reload_settings(self) -> None:
        """Re-read the settings store and apply everything live. Called
        by the main window's Overlay tab, so settings change mid-session
        without touching the overlay's own drawer."""
        fresh = self._settings_store.load()
        if fresh.overlay_orientation not in _ORIENTATIONS:
            fresh.overlay_orientation = "vertical"
        orientation_changed = (
            fresh.overlay_orientation != self._settings.overlay_orientation
        )
        old_order = list(
            getattr(self._settings, "overlay_field_order", None) or ())
        old_scale = self._overlay_scale()
        old_scopes = dict(
            getattr(self._settings, "overlay_field_scope", None) or {})
        self._settings = fresh
        # Colors first: a rebuild below paints new rows from the cache.
        self._apply_text_color()
        if abs(self._overlay_scale() - old_scale) > 1e-9:
            # Scale moves padding + labels + numbers; the rebuild inside
            # covers orientation/order changes too.
            self._apply_scale()
        elif orientation_changed or (
                list(fresh.overlay_field_order or ()) != old_order):
            self._build_fields_box()
        self._sync_controls()
        self._apply_lock_state(self._settings.overlay_locked)
        self._apply_bg()
        self._apply_window_opacity()
        self._apply_field_visibility()
        if dict(getattr(fresh, "overlay_field_scope", None) or {}) \
                != old_scopes:
            # Same rows, different sources (visit vs session per field)
            # — repaint now instead of waiting for the next poll.
            self._refresh()

    def _on_pick_locked_text_color(self) -> None:
        picked = QColorDialog.getColor(QColor(self._locked_text_color),
                                       self, "Overlay locked text color")
        if not picked.isValid():
            return
        self._settings.overlay_locked_text_color = picked.name()
        self._apply_text_color()
        self._sync_locked_color_button()
        self._save_settings()

    def _sync_controls(self) -> None:
        """Push the current settings into the drawer widgets (sliders,
        checkboxes) so they agree with changes made elsewhere."""
        self._opacity_slider.blockSignals(True)
        self._opacity_slider.setValue(int(self._settings.overlay_opacity * 100))
        self._opacity_slider.blockSignals(False)
        self._locked_opacity_slider.blockSignals(True)
        self._locked_opacity_slider.setValue(
            int(self._settings.overlay_locked_opacity * 100))
        self._locked_opacity_slider.blockSignals(False)
        self._win_opacity_slider.blockSignals(True)
        self._win_opacity_slider.setValue(
            int(self._settings.overlay_window_opacity * 100))
        self._win_opacity_slider.blockSignals(False)
        self._locked_win_opacity_slider.blockSignals(True)
        self._locked_win_opacity_slider.setValue(
            int(self._settings.overlay_locked_window_opacity * 100))
        self._locked_win_opacity_slider.blockSignals(False)
        self._scale_slider.blockSignals(True)
        self._scale_slider.setValue(
            int(round(self._overlay_scale() * 100)))
        self._scale_slider.blockSignals(False)
        self._sync_color_button()
        self._sync_locked_color_button()
        for key, cb in self._field_checks.items():
            cb.blockSignals(True)
            cb.setChecked(bool(getattr(self._settings, f"overlay_show_{key}",
                                       True)))
            cb.blockSignals(False)

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
        self._refit_window_soon()

    def _refit_window_soon(self) -> None:
        """Defer a forced refit to the next event-loop turn.

        Structural changes (orientation rebuild, field toggles) post
        layout requests that only settle once control returns to the
        loop — measuring synchronously reads partially-updated hints
        (a horizontal strip measured as header+footer size and stuck
        there). The zero-delay timer fires after the pending layout
        pass, so the measurement sees the settled content. The
        receiver-arg form auto-cancels if the window is destroyed."""
        QTimer.singleShot(0, self, lambda: self._refit_window(force=True))

    def _refit_window(self, force: bool = False) -> None:
        """Shrink-wrap the window around its current content.

        Called after rebuilds, visibility changes, and lock flips. New
        values arriving on the poll timer call it too: rows size to
        their content, so text that grows (counts, zones, visits)
        asks for more window instead of clipping."""
        # Layouts recalculate lazily, and QWidget.sizeHint caches: right
        # after a rebuild (orientation flip, field toggle) the cached
        # hint still describes the OLD content. Measuring that stale
        # hint once shrank a horizontal strip to header+footer size and
        # locked it in. So invalidate + activate the top layout and read
        # the layout's totals (recomputed fresh) instead of the cached
        # widget hints.
        top = self.layout()
        if top is not None:
            top.invalidate()
            top.activate()
            hint = top.totalSizeHint()
            # A horizontal strip's columns carry minimum widths the
            # plain size hint ignores — take the larger per dimension so
            # the window always covers its content minimums.
            want = hint.expandedTo(top.totalMinimumSize())
        else:
            hint = self._card.sizeHint()
            want = hint
        if force or hint != self._last_hint:
            self._last_hint = hint
            self._card.adjustSize()
            if want != self.size():
                self.resize(want)
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
        self._apply_bg()
        self._apply_window_opacity()
        for _, num in self._rows.values():
            num.set_dim(locked)
        self._refit_window_soon()

    def _set_pass_through(self, pass_through: bool) -> None:
        # WA_TransparentForMouseEvents alone doesn't give real
        # click-through for a top-level window on Windows — Qt still
        # takes the click. WindowTransparentForInput is the flag that
        # makes the OS skip the window for hit-testing, so the game
        # underneath gets the click. Toggling a window flag on a
        # visible window hides it first; re-showing is what makes the
        # new flag take effect. The old code called show() straight
        # after the flag change, which Qt treated as a no-op on the
        # just-hidden window — so locking from the tab hid the overlay
        # and it never came back.
        was_visible = self.isVisible()
        if was_visible:
            self.hide()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, pass_through)
        for w in self.findChildren(QWidget):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, pass_through)
        self.setWindowFlag(Qt.WindowTransparentForInput, pass_through)
        if was_visible:
            self.show()

    # ------------------------------------------------------------------
    # Opacity / fields / drawer
    # ------------------------------------------------------------------
    def _on_opacity_changed(self, value: int) -> None:
        self._settings.overlay_opacity = value / 100.0
        self._apply_bg()
        self._save_settings()

    def _on_locked_opacity_changed(self, value: int) -> None:
        self._settings.overlay_locked_opacity = value / 100.0
        self._apply_bg()
        self._save_settings()

    def _on_window_opacity_changed(self, value: int) -> None:
        self._settings.overlay_window_opacity = value / 100.0
        self._apply_window_opacity()
        self._save_settings()

    def _on_locked_window_opacity_changed(self, value: int) -> None:
        self._settings.overlay_locked_window_opacity = value / 100.0
        self._apply_window_opacity()
        self._save_settings()

    def _on_pick_text_color(self) -> None:
        picked = QColorDialog.getColor(QColor(self._text_color), self,
                                       "Overlay stats text color")
        if not picked.isValid():
            return
        self._settings.overlay_text_color = picked.name()
        self._apply_text_color()
        self._sync_color_button()
        self._save_settings()

    def _on_field_toggle(self, key: str, checked: bool) -> None:
        setattr(self._settings, f"overlay_show_{key}", checked)
        self._apply_field_visibility()
        self._save_settings()

    def _apply_bg(self) -> None:
        """Paint the card background at the configured translucency.

        Opacity touches the background fill only — the text is painted
        separately at full opacity, so this slider is exactly the
        "background but not text" control: at 0 the fill and the
        border both vanish and the overlay is backgroundless. (The
        old setWindowOpacity faded the whole window, text included,
        which is why locked text looked washed out.)"""
        alpha = (
            self._settings.overlay_locked_opacity
            if self._settings.overlay_locked
            else self._settings.overlay_opacity
        )
        alpha = min(1.0, max(0.0, alpha))
        self._bg_alpha = alpha
        base = QColor(theme.INK_1)
        edge = QColor(theme.INK_BORDER_2)
        self._card.setStyleSheet(
            "#OverlayCard {"
            f" background: rgba({base.red()},{base.green()},{base.blue()},{alpha:.3f});"
            " border: 1px solid"
            f" rgba({edge.red()},{edge.green()},{edge.blue()},{alpha:.3f});"
            "}"
        )

    def _card_bg_alpha(self) -> float:
        """Background alpha currently applied to the card (0..1)."""
        return getattr(self, "_bg_alpha", 1.0)

    def _apply_window_opacity(self) -> None:
        """Fade the whole window — background AND text together.

        This is the "Opacity" slider to the "Background" slider's
        scalpel: setWindowOpacity multiplies everything the window
        paints, so the text dims with the card. It composes with (not
        replaces) the card alpha from _apply_bg."""
        alpha = (
            self._settings.overlay_locked_window_opacity
            if self._settings.overlay_locked
            else self._settings.overlay_window_opacity
        )
        self.setWindowOpacity(min(1.0, max(0.0, alpha)))

    def _apply_text_color(self) -> None:
        """Paint the stats text in the picked colors.

        Labels always use the stats color; numbers use it unlocked and
        the locked color while locked. The label rule is appended after
        the base sheet in the same setStyleSheet call so the
        equal-specificity ID rule wins by order; numbers carry their
        colors inline (see _FitNumber) so lock flips can't clobber them.
        Cached on self so field-box rebuilds can paint new rows."""
        normal = _valid_color(self._settings.overlay_text_color,
                              theme.PARCH_BG)
        locked = _valid_color(self._settings.overlay_locked_text_color,
                              theme.ASH)
        self._text_color = normal
        self._locked_text_color = locked
        self.setStyleSheet(
            theme.OVERLAY_QSS
            + f"\n#OverlayLabel {{ color: {normal}; }}\n"
            + self._scale_rules()
        )
        # getattr: the constructor applies colors before the first
        # field-box build creates any rows.
        for _, num in getattr(self, "_rows", {}).values():
            num.set_colors(normal, locked)

    def _overlay_scale(self) -> float:
        """Content scale, clamped to the slider's range."""
        try:
            return min(1.5, max(0.7, float(self._settings.overlay_scale)))
        except (TypeError, ValueError):
            return 1.0

    def _scale_rules(self) -> str:
        """Inline ID rules carrying the content scale.

        Labels and the header handle size themselves in QSS, so scaling
        needs rules that win by order at equal specificity. Numbers skip
        this — _FitNumber takes its size as a constructor arg at rebuild."""
        label_px = max(6, int(round(9 * self._overlay_scale())))
        return (f"\n#OverlayLabel {{ font-size: {label_px}px; }}\n"
                f"#OverlayHandle {{ font-size: {label_px}px; }}\n")

    @staticmethod
    def _scaled_margins(scale: float) -> tuple[int, int]:
        """Card padding (horizontal, vertical) at the given scale."""
        return int(round(18 * scale)), int(round(14 * scale))

    @staticmethod
    def _scaled_minimum(scale: float) -> tuple[int, int]:
        """Window minimum size at the given scale."""
        return (max(120, int(round(180 * scale))),
                max(60, int(round(80 * scale))))

    def _apply_scale(self) -> None:
        """Apply the content scale: card padding now, text via rebuild.

        Slider drags fire continuously and a field rebuild is cheap, so
        the overlay rescales live instead of waiting for release."""
        margin_h, margin_v = self._scaled_margins(self._overlay_scale())
        self._card_layout.setContentsMargins(margin_h, margin_v,
                                             margin_h, margin_v)
        self.setMinimumSize(*self._scaled_minimum(self._overlay_scale()))
        self._apply_text_color()  # re-emits the sheet incl. label sizes
        self._build_fields_box()

    def _on_scale_changed(self, value: int) -> None:
        self._settings.overlay_scale = value / 100.0
        self._apply_scale()
        self._save_settings()

    @staticmethod
    def _paint_swatch(btn, hex_color: str) -> None:
        """Show a color as hex on a swatch button."""
        if not QColor(hex_color).isValid():
            hex_color = theme.PARCH_BG
        name = QColor(hex_color).name()
        btn.setText(name)
        # A leaf button, not a container: a bare background here
        # affects only this button.
        btn.setStyleSheet(
            f"background: {name}; color: {_contrast_text(name)};")

    def _sync_color_button(self) -> None:
        """Show the current stats-text color as hex on a swatch."""
        self._paint_swatch(self._color_btn, getattr(
            self, "_text_color", self._settings.overlay_text_color))

    def _sync_locked_color_button(self) -> None:
        """Show the current locked-text color as hex on a swatch."""
        self._paint_swatch(self._locked_color_btn, getattr(
            self, "_locked_text_color",
            self._settings.overlay_locked_text_color))

    def _apply_field_visibility(self) -> None:
        for key, (lbl, num) in self._rows.items():
            visible = bool(getattr(self._settings, f"overlay_show_{key}",
                                   True))
            lbl.setVisible(visible)
            num.setVisible(visible)
        # A divider only earns its keep if at least one of the rows it
        # separates is still showing.
        for rule, before_key, after_key in self._hairlines:
            before_visible = bool(getattr(
                self._settings, f"overlay_show_{before_key}", True))
            after_visible = bool(getattr(
                self._settings, f"overlay_show_{after_key}", True))
            rule.setVisible(before_visible or after_visible)
        self._refit_window_soon()

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
        self._refit_window_soon()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        sid = self._get_session_id()
        if sid is None:
            for _, num in self._rows.values():
                num.set_value("—")
                num.setToolTip("")
            return
        try:
            s = self._db.summary(sid)
        except Exception:
            return
        per_field = {key: field_scope(self._settings, key)
                     for key in self._rows}
        visit = s.get("visit")
        visit_vmap: dict = {}
        visit_exact: dict = {}
        if visit is not None:
            # The current visit's maps are always built while a visit is
            # open (pure formatting, no query), so even hidden rows stay
            # correct and show the right source the moment they're
            # unhidden. Level is account-scoped (no visit meaning), so
            # it stays session-wide; the zone row names the visit itself.
            sc_total = visit["sc_picked"] + visit["sc_unpicked"]
            zone_text = (visit.get("display_name")
                         or visit.get("map_name")
                         or s.get("current_zone") or "—")
            visit_vmap = {
                "kills": _mine_total(visit["my_kills"], visit["kills"]),
                "sc": _mine_total(visit["sc_picked"], sc_total),
                "xp": visit["xp"],
                "level": s["level"],
                "zone": zone_text,
                "deaths": visit["deaths"],
                "xp_lost": visit["xp_lost"],
                "xp_hr": visit["xp_hr"],
                "dps": visit["dps_mine"],
            }
            visit_exact = {
                "kills": _exact_mine_total(visit["my_kills"],
                                           visit["kills"]),
                "sc": _exact_mine_total(visit["sc_picked"], sc_total),
                "xp": _exact_number(visit["xp"])
                if isinstance(visit["xp"], int) else str(visit["xp"]),
                "level": str(s["level"]),
                "zone": zone_text + (" — mirage run" if visit.get(
                    "is_mirage") else ""),
                "deaths": _exact_number(visit["deaths"])
                if isinstance(visit["deaths"], int)
                else str(visit["deaths"]),
                "xp_lost": _exact_number(visit["xp_lost"])
                if isinstance(visit["xp_lost"], int)
                else str(visit["xp_lost"]),
                "xp_hr": f"{visit['xp_hr']:,.1f} XP/hr (this visit)",
                "dps": (f"{visit['dps_mine']:,.1f} yours / "
                        f"{visit['dps']:,.1f} total DPS (this visit)"),
            }
        # Session totals — today's behavior. The two rate rows read
        # session-wide rates; the extra query runs only when a visible
        # row actually resolves to the session side.
        rates = None
        if any(key in ("xp_hr", "dps") and bool(getattr(
                self._settings, f"overlay_show_{key}", True))
                and (visit is None or per_field.get(key) != VISIT_SCOPE)
                for key in self._rows):
            try:
                rates = self._db.session_rates(sid)
            except Exception:
                rates = None
        rates = rates or {"xp_hr": 0.0, "dps": 0.0, "dps_mine": 0.0}
        zone_text = s.get("current_zone") or "—"
        sess_vmap = {
            "kills": _mine_total(s["my_kills"], s["kills"]),
            "sc": _mine_total(s["sc_picked"],
                               s["sc_picked"] + s["sc_unpicked"]),
            "xp": s["xp"],
            "level": s["level"],
            "zone": zone_text,
            "deaths": s["deaths"],
            "xp_lost": s["xp_lost"],
            "xp_hr": rates["xp_hr"],
            "dps": rates["dps_mine"],
        }
        # Exact full values behind the compact display text — the main
        # window already shows full counts, and the overlay tooltip
        # carries them here so nothing is lost to K/M/B/T.
        sess_exact = {
            "kills": _exact_mine_total(s["my_kills"], s["kills"]),
            "sc": _exact_mine_total(s["sc_picked"],
                                    s["sc_picked"] + s["sc_unpicked"]),
            "xp": _exact_number(s["xp"]) if isinstance(s["xp"], int)
            else str(s["xp"]),
            "level": str(s["level"]),
            "zone": zone_text,
            "deaths": _exact_number(s["deaths"])
            if isinstance(s["deaths"], int) else str(s["deaths"]),
            "xp_lost": _exact_number(s["xp_lost"])
            if isinstance(s["xp_lost"], int) else str(s["xp_lost"]),
            "xp_hr": f"{rates['xp_hr']:,.1f} XP/hr (session)",
            "dps": (f"{rates['dps_mine']:,.1f} yours / "
                    f"{rates['dps']:,.1f} total DPS (session)"),
        }
        for key, (_lbl, num) in self._rows.items():
            # Each row reads its own scope: visit-scoped rows read the
            # current visit, everything else reads session totals (and
            # everything falls back to session when no visit is open).
            # Full text always: each _FitNumber sizes its minimum width
            # to the content (measured at the scaled size), so the
            # window widens for "Snowy Mountain" instead of eliding to
            # "Snowy Mo...", and all rows stretch to that widest row.
            if visit is not None and visit_vmap \
                    and per_field.get(key) == VISIT_SCOPE:
                raw, tip = visit_vmap[key], visit_exact.get(key)
            else:
                raw, tip = sess_vmap[key], sess_exact.get(key)
            text = self._format(key, raw)
            self._last_values[key] = text
            tip = tip if tip is not None else text
            self._last_tips[key] = tip
            num.set_value(text)
            num.setToolTip(tip)
        # Values can widen the content (new zone, bigger counts), so
        # re-shrink-wrap; a no-op when the hint didn't change.
        self._refit_window()

    @staticmethod
    def _format(key: str, value) -> str:
        # Large counts compact to K/M/B/T (endgame XP hits billions/
        # trillions); rates compact the same way with their units
        # (21.6K/hr, 15.0 DPS). Level and zone always render exactly.
        # kills/sc arrive pre-compacted from _mine_total, so plain
        # strings pass through untouched.
        if key in ("xp", "xp_lost", "kills", "sc", "deaths") \
                and isinstance(value, int):
            return _compact_number(value)
        if key == "xp_hr" and isinstance(value, (int, float)):
            return f"{_compact_rate(value)}/hr"
        if key == "dps" and isinstance(value, (int, float)):
            return f"{_compact_rate(value)} DPS"
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


def _mine_total(mine: int, total: int) -> str:
    """Compact 'yours/total' when they differ, else the plain total.
    Kept local (rather than importing from main_window) to avoid a
    circular import — main_window already imports this module. Exact
    full counts stay available via _exact_mine_total for tooltips."""
    if mine != total:
        return f"{_compact_number(mine)}/{_compact_number(total)}"
    return _compact_number(total)


def _exact_mine_total(mine: int, total: int) -> str:
    """Full exact 'yours/total' with commas for tooltips."""
    if mine != total:
        return f"{mine:,}/{total:,}"
    return f"{total:,}"


def _hairline() -> QFrame:
    """A 1-pixel parchment rule — the design's only divider."""
    f = QFrame()
    f.setObjectName("OverlayRule")
    f.setFrameShape(QFrame.NoFrame)
    f.setFixedHeight(1)
    return f