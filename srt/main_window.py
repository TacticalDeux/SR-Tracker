"""Main tracker window.

Visual identity (see `srt.theme`): the user's command tent. Quiet ink
backdrop, command chips instead of pill-shaped buttons, sessions shown
as a vertical list of dated entries (left margin holds the date stamp),
the data tables carry field-log rules and tracked-uppercase headers.

The summary tab features the orb large in the center — the same signature
element as the overlay, scaled up. That's the one piece of imagery in
the whole main window; everything else is type and rules.
"""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider, QStatusBar,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import paths as _paths
from . import theme
from tools import inject as _inject_tool
from .consumer import EventConsumer
from .crystal import crystal_pixmap
from .debug_console import DebugConsole, is_dev_mode
from . import names as _names
from .overlay import OverlayWindow
from .settings import Settings, SettingsStore
from .charts import BarChart, Bar, RateBarChart

# ---------------------------------------------------------------------------
# The signature element, scaled up for the main window's summary tab.
# The pixel-art crystal is rendered at 96px (the source is 18x22, scaled
# 4.4x with NearestNeighbor so the pixels stay crisp at this size).
# ---------------------------------------------------------------------------
class _HeroCrystal(QLabel):
    SIZE = 96

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dim = False
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAlignment(Qt.AlignCenter)
        self._refresh()

    def set_dim(self, dim: bool) -> None:
        self._dim = dim
        self._refresh()

    def _refresh(self) -> None:
        # Render at higher resolution so the pixels read clearly on a
        # 96px tile. crystal_pixmap uses NearestNeighbor internally.
        self.setPixmap(crystal_pixmap(self.SIZE, dim=self._dim))


# ---------------------------------------------------------------------------
# Summary "instrument" — a single, focused page that reads at a glance.
# ---------------------------------------------------------------------------
class _SummaryPanel(QWidget):
    """Big orb centered, with the four metrics as a vertical list on the right.

    Empty state is also designed: when there's no session, a small ash
    instruction is set in Georgia italic, so the user is told what to do
    in the right voice, not just "click here".
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(40)
        # Left: the crystal, with a small caption beneath. The column
        # centers vertically so the crystal sits at eye-level with the
        # metric list.
        left = QVBoxLayout()
        left.setSpacing(8)
        self._crystal = _HeroCrystal()
        cap = QLabel("SOUL CRYSTAL")
        cap.setObjectName("FieldLabel")
        cap.setAlignment(Qt.AlignCenter)
        left.addStretch(1)
        left.addWidget(self._crystal, alignment=Qt.AlignCenter)
        left.addWidget(cap, alignment=Qt.AlignCenter)
        left.addStretch(1)
        self._left_widget = QWidget()
        self._left_widget.setLayout(left)
        self._left_widget.setMinimumWidth(self._crystal.SIZE + 40)
        layout.addWidget(self._left_widget, 0)
        # so we can show/hide the whole column for the empty state.
        right = QVBoxLayout()
        right.setSpacing(20)

        self._kills   = self._make_metric("KILLS")
        self._sc      = self._make_metric("SOUL CRYSTALS")
        self._xp      = self._make_metric("EXPERIENCE")
        self._level   = self._make_metric("LEVEL")

        for w in (self._kills, self._sc, self._xp, self._level):
            right.addWidget(w)
        right.addStretch(1)

        self._right_widget = QWidget()
        self._right_widget.setLayout(right)
        layout.addWidget(self._right_widget, 1)
        self._right_widget.hide()

        # Empty state replaces the right column when there's no session
        self._empty_label = QLabel(
            "No session active.\nClick Start to begin tracking."
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setFont(QFont("Georgia", 12))
        self._empty_label.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; font-style: italic;"
        )
        self._empty_holder = QWidget()
        wrap = QVBoxLayout(self._empty_holder)
        wrap.addStretch(1)
        wrap.addWidget(self._empty_label)
        wrap.addStretch(1)
        layout.addWidget(self._empty_holder, 1)
    def _make_metric(self, label_text: str) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        lbl = QLabel(label_text)
        lbl.setObjectName("FieldLabel")
        lbl.setFont(QFont("Georgia", 10))
        lbl.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; font-weight: bold; letter-spacing: 3px;"
        )
        lbl.setMinimumWidth(180)
        row.addWidget(lbl, 0)
        num = QLabel("—")
        num.setObjectName("FieldValue")
        num.setFont(QFont("Consolas", 28))
        num.setStyleSheet(f"color: {theme.PARCH_BG}; font-weight: bold;")
        num.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(num, 1)
        w._num = num
        w._lbl = lbl
        return w

    def set_summary(self, s: dict | None) -> None:
        if s is None:
            self._crystal.set_dim(True)
            self._right_widget.hide()
            self._empty_holder.show()
            return
        self._crystal.set_dim(False)
        self._empty_holder.hide()
        self._right_widget.show()
        self._kills._num.setText(_mine_total(s["my_kills"], s["kills"]))
        self._sc._num.setText(
            _mine_total(s["my_soul_crystals"], s["soul_crystals"]))
        self._xp._num.setText(f"{s['xp']:,}")
        self._level._num.setText(str(s["level"]))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(
        self,
        dll: TrackerDLL,
        db: Database,
        settings_store: SettingsStore,
    ):
        super().__init__()
        self._dll = dll
        self._db = db
        # Load the monster/item name tables once. The kills/drops tabs
        # call self._db.recent_kills/recent_drops with `names=self._names`,
        # so this has to be set up here.
        self._names = _names.load()
        self._settings_store = settings_store
        self._settings = settings_store.load()
        self._consumer = EventConsumer(dll, db, self)
        # Stream every event the consumer sees into the debug console
        # (dev mode only). This is the fastest way to verify whether the
        # DLL is producing events at all.
        self._consumer.event_seen.connect(self._on_event_seen)
        self._consumer.status.connect(self._on_consumer_status)
        self._overlay: OverlayWindow | None = None
        self.setWindowTitle("SR Tracker — Soul's Remnant")
        self._fit_to_screen()

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- header band (replaces the old "control bar") ---
        outer.addWidget(self._build_header_band())

        # --- tabs ---
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        outer.addWidget(self._tabs, 1)
        self._build_summary_tab()
        self._build_sessions_tab()
        self._build_kills_tab()
        self._build_drops_tab()
        self._build_zones_tab()
        self._build_graphs_tab()
        self._build_overlay_tab()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        # Dev mode only: a streaming event log so you can see exactly
        # what the DLL is producing. Not built into frozen exes.
        if is_dev_mode():
            self._build_debug_tab()
        # --- status bar (footer hairline) ---
        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.showMessage("Ready.")
        self._set_status("Ready.")
        # Right-aligned counter widget — visible while tracking, so the
        # user can see at a glance whether events are flowing.
        self._lbl_counters = QLabel("")
        self._lbl_counters.setFont(QFont("Consolas", 9))
        self._lbl_counters.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; background: transparent;"
        )
        sb.addPermanentWidget(self._lbl_counters)
        # --- timers ---
        self._summary_timer = QTimer(self)
        self._summary_timer.setInterval(1000)
        self._summary_timer.timeout.connect(self._on_tick)
        self._summary_timer.start()
        # Flood guard for _on_event_seen: table rebuilds are the most
        # expensive thing the GUI thread does, and damage/spawn bursts
        # can deliver dozens of events per second. Refresh the visible
        # table at most twice per second on the event path — the 1s
        # tick above catches up with anything skipped.
        self._last_tab_refresh = 0.0
        # Silence watchdog state for _on_tick: warn once per idle stretch
        # when tracking runs but no events arrive, with enough detail
        # (REMOVED head, game process state) to tell a quiet game
        # apart from a dead bridge. Reset whenever events flow again.
        self._silence_warned = False

        self._refresh_lock_button()
        # Populate right away rather than leaving tables blank until the
        # first tick or a manual "Re-read". The graphs combo fills here
        # too — otherwise the Graphs tab starts with no session selected
        # and every chart reads "No data".
        self._refresh_sessions()
        self._refresh_sessions_combo()
        self._refresh_overlay_tab()
        self._on_tick()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _fit_to_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail is not None and avail.width() > 0 and avail.height() > 0:
            w = int(avail.width() * 0.9)
            h = int(avail.height() * 0.9)
            self.resize(w, h)
            x = avail.x() + (avail.width() - w) // 2
            y = avail.y() + (avail.height() - h) // 2
            self.move(x, y)
        else:
            self.resize(1200, 760)
        self.setMinimumSize(QSize(900, 600))

    def _build_header_band(self) -> QWidget:
        band = QFrame()
        band.setObjectName("HeaderBand")
        band.setStyleSheet(
            f"#HeaderBand {{ background: {theme.INK_0};"
            f" border-bottom: 1px solid {theme.INK_BORDER}; }}"
        )
        band.setFixedHeight(64)
        lay = QHBoxLayout(band)
        lay.setContentsMargins(24, 0, 24, 0)
        lay.setSpacing(8)

        # Brand mark on the left
        brand = QLabel("SOULS  REMAINING")
        brand.setFont(QFont("Georgia", 11, QFont.Bold))
        brand.setStyleSheet(
            f"color: {theme.CRYSTAL}; letter-spacing: 4px;"
        )
        lay.addWidget(brand, 0)
        lay.addStretch(1)

        # The four command chips
        self.btn_toggle = QPushButton("Start")
        self.btn_toggle.setProperty("role", "primary")
        self.btn_toggle.clicked.connect(self._toggle_tracking)
        lay.addWidget(self.btn_toggle)

        self.btn_overlay = QPushButton("Show overlay")
        self.btn_overlay.clicked.connect(self._toggle_overlay)
        lay.addWidget(self.btn_overlay)

        self.btn_lock = QPushButton()
        self.btn_lock.setCheckable(True)
        self.btn_lock.clicked.connect(self._on_lock_button_clicked)
        lay.addWidget(self.btn_lock)

        self.btn_reset = QPushButton("Reset session")
        self.btn_reset.setProperty("role", "destructive")
        self.btn_reset.clicked.connect(self._reset_session)
        lay.addWidget(self.btn_reset)

        return band

    def _build_summary_tab(self) -> None:
        tab = QWidget()
        tab.setStyleSheet(f"background: {theme.INK_0};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        self._summary = _SummaryPanel()
        layout.addWidget(self._summary)
        self._tabs.addTab(tab, "Summary")

    def _build_sessions_tab(self) -> None:
        tab = QWidget()
        tab.setStyleSheet(f"background: {theme.INK_0};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("PAST  SESSIONS")
        title.setFont(QFont("Georgia", 11))
        title.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        header_row.addWidget(title)
        header_row.addStretch(1)
        btn_wipe = QPushButton("Wipe database")
        btn_wipe.setProperty("role", "destructive")
        btn_wipe.clicked.connect(self._wipe_database)
        header_row.addWidget(btn_wipe)
        btn = QPushButton("Re-read")
        btn.clicked.connect(self._refresh_sessions)
        header_row.addWidget(btn)
        layout.addLayout(header_row)

        # Hairline rule under the section title
        rule = QFrame()
        rule.setFrameShape(QFrame.NoFrame)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {theme.RUNE_FAINT};")
        layout.addWidget(rule)

        self.tbl_sessions = QTableWidget(0, 5)
        self.tbl_sessions.setHorizontalHeaderLabels(
            ["I", "BEGUN", "ENDED", "KILLS", "DROPS"]
        )
        # KILLS / DROPS read "yours/total" (e.g. 8/12). Double-click a
        # row to open that session's detail view.
        self.tbl_sessions.horizontalHeaderItem(3).setToolTip("Your kills / all kills")
        self.tbl_sessions.horizontalHeaderItem(4).setToolTip("Your drops / all drops")
        self.tbl_sessions.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        _align_headers(self.tbl_sessions,
                       [Qt.AlignCenter, Qt.AlignLeft, Qt.AlignLeft,
                        Qt.AlignRight, Qt.AlignRight])
        self.tbl_sessions.verticalHeader().setVisible(False)
        self.tbl_sessions.setShowGrid(False)
        self.tbl_sessions.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_sessions.setSelectionBehavior(QTableWidget.SelectRows)
        self.tbl_sessions.itemDoubleClicked.connect(self._open_session_detail)
        layout.addWidget(self.tbl_sessions)
        self._sessions_empty = _empty_note("No sessions recorded yet.")
        layout.addWidget(self._sessions_empty)
        self._tabs.addTab(tab, "Sessions")

    def _build_kills_tab(self) -> None:
        tab = QWidget()
        tab.setStyleSheet(f"background: {theme.INK_0};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("RECORDED  KILLS")
        title.setFont(QFont("Georgia", 11))
        title.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        header_row.addWidget(title)
        header_row.addStretch(1)
        btn = QPushButton("Re-read")
        btn.clicked.connect(self._refresh_kills)
        header_row.addWidget(btn)
        layout.addLayout(header_row)

        rule = QFrame()
        rule.setFrameShape(QFrame.NoFrame)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {theme.RUNE_FAINT};")
        layout.addWidget(rule)

        self.tbl_kills = QTableWidget(0, 4)
        self.tbl_kills.setHorizontalHeaderLabels(["TIME", "ENEMY ID", "MOB", "MINE"])
        self.tbl_kills.horizontalHeaderItem(3).setToolTip(
            "Whether your account damaged this enemy before it died")
        self.tbl_kills.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        _align_headers(self.tbl_kills,
                       [Qt.AlignLeft, Qt.AlignRight, Qt.AlignLeft,
                        Qt.AlignCenter])
        self.tbl_kills.verticalHeader().setVisible(False)
        self.tbl_kills.setShowGrid(False)
        self.tbl_kills.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_kills.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.tbl_kills)
        self._kills_empty = _empty_note("No kills recorded yet.")
        layout.addWidget(self._kills_empty)
        self._tabs.addTab(tab, "Kills")

    def _build_drops_tab(self) -> None:
        tab = QWidget()
        tab.setStyleSheet(f"background: {theme.INK_0};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("DROPS  &  LOOT")
        title.setFont(QFont("Georgia", 11))
        title.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        header_row.addWidget(title)
        header_row.addStretch(1)
        self._drops_filter = QLineEdit()
        self._drops_filter.setPlaceholderText("Filter by item name…")
        self._drops_filter.setClearButtonEnabled(True)
        self._drops_filter.setMinimumWidth(220)
        self._drops_filter.textChanged.connect(lambda _t: self._refresh_drops())
        header_row.addWidget(self._drops_filter)
        btn = QPushButton("Re-read")
        btn.clicked.connect(self._refresh_drops)
        header_row.addWidget(btn)
        layout.addLayout(header_row)

        rule = QFrame()
        rule.setFrameShape(QFrame.NoFrame)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {theme.RUNE_FAINT};")
        layout.addWidget(rule)

        self.tbl_drops = QTableWidget(0, 5)
        self.tbl_drops.setHorizontalHeaderLabels(
            ["TIME", "DROP", "ITEM", "QTY", "OWNER"]
        )
        self.tbl_drops.horizontalHeaderItem(4).setToolTip(
            "Drop owner: You, Unclaimed, or the owning account id")
        self.tbl_drops.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        _align_headers(self.tbl_drops,
                       [Qt.AlignLeft, Qt.AlignRight, Qt.AlignLeft,
                        Qt.AlignRight, Qt.AlignLeft])
        self.tbl_drops.verticalHeader().setVisible(False)
        self.tbl_drops.setShowGrid(False)
        self.tbl_drops.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_drops.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.tbl_drops)
        self._drops_empty = _empty_note("No drops recorded yet.")
        layout.addWidget(self._drops_empty)
        self._tabs.addTab(tab, "Drops")

    def _build_zones_tab(self) -> None:
        """Per-zone aggregated stats for the active session (or latest if no session)."""
        tab = QWidget()
        tab.setStyleSheet(f"background: {theme.INK_0};")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("ZONE  BREAKDOWN")
        title.setFont(QFont("Georgia", 11))
        title.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        header_row.addWidget(title)
        header_row.addStretch(1)
        btn = QPushButton("Re-read")
        btn.clicked.connect(self._refresh_zones)
        header_row.addWidget(btn)
        layout.addLayout(header_row)

        rule = QFrame()
        rule.setFrameShape(QFrame.NoFrame)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {theme.RUNE_FAINT};")
        layout.addWidget(rule)

        self.tbl_zones = QTableWidget(0, 12)
        self.tbl_zones.setHorizontalHeaderLabels(
            ["ZONE", "ENTERED", "LEFT", "TIME", "KILLS", "SC", "SC/HR",
             "DROPS", "D/HR", "XP", "XP/HR", "KPM"]
        )
        self.tbl_zones.horizontalHeaderItem(4).setToolTip("Your kills / all kills")
        self.tbl_zones.horizontalHeaderItem(7).setToolTip("Your drops / all drops")
        self.tbl_zones.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        _align_headers(self.tbl_zones,
                       [Qt.AlignLeft, Qt.AlignLeft, Qt.AlignLeft,
                        Qt.AlignRight, Qt.AlignRight, Qt.AlignRight,
                        Qt.AlignRight, Qt.AlignRight, Qt.AlignRight,
                        Qt.AlignRight, Qt.AlignRight, Qt.AlignRight])
        self.tbl_zones.verticalHeader().setVisible(False)
        self.tbl_zones.setShowGrid(False)
        self.tbl_zones.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_zones.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.tbl_zones)
        self._zones_empty = _empty_note("No zone visits recorded yet.")
        layout.addWidget(self._zones_empty)
        self._tabs.addTab(tab, "Zones")

    def _build_graphs_tab(self) -> None:
        """Per-session rate charts: KPM, drops/m, SC/m."""
        tab = QWidget()
        tab.setStyleSheet(f"background: {theme.INK_0};")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(20)

        # Session selector
        selector_row = QHBoxLayout()
        selector_title = QLabel("SESSION")
        selector_title.setFont(QFont("Georgia", 9))
        selector_title.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        selector_row.addWidget(selector_title)
        self.cmb_sessions = QComboBox()
        self.cmb_sessions.currentIndexChanged.connect(self._refresh_graphs)
        self.cmb_sessions.setMinimumWidth(280)
        selector_row.addWidget(self.cmb_sessions, 1)
        btn = QPushButton("Re-read")
        btn.clicked.connect(self._refresh_sessions_combo)
        selector_row.addWidget(btn)
        outer.addLayout(selector_row)

        rule = QFrame()
        rule.setFrameShape(QFrame.NoFrame)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {theme.RUNE_FAINT};")
        outer.addWidget(rule)

        # Charts live in a scroll area: the per-zone lists grow with the
        # session, and a fixed tab would squash them to nothing.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        charts_host = QWidget()
        charts_host.setStyleSheet(f"background: {theme.INK_0};")
        charts_lay = QVBoxLayout(charts_host)
        charts_lay.setContentsMargins(0, 0, 12, 0)
        charts_lay.setSpacing(20)

        # Three charts stacked: KPM, drops/m, SC/m
        charts_label = QLabel("RATES")
        charts_label.setFont(QFont("Georgia", 9))
        charts_label.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        charts_lay.addWidget(charts_label)

        self.chart_kpm = RateBarChart()
        self.chart_drops_m = RateBarChart()
        self.chart_sc_m = RateBarChart()
        for c in (self.chart_kpm, self.chart_drops_m, self.chart_sc_m):
            c.setMinimumHeight(140)
            charts_lay.addWidget(c)

        # Per-zone breakdown in selected session
        zone_label = QLabel("PER-ZONE  IN  THIS  SESSION")
        zone_label.setFont(QFont("Georgia", 9))
        zone_label.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        charts_lay.addWidget(zone_label)
        self.chart_zones = BarChart(bar_height=22, value_format="{:.1f}")
        self.chart_zones.setMinimumHeight(180)
        charts_lay.addWidget(self.chart_zones)
        charts_lay.addStretch(1)

        scroll.setWidget(charts_host)
        outer.addWidget(scroll, 1)

        self._tabs.addTab(tab, "Graphs")

    def _build_overlay_tab(self) -> None:
        """Overlay settings, live in the main window so they can change
        at any time — mid-session, overlay hidden, even locked. Every
        control pushes through the settings store and reloads the
        overlay in place; the overlay's own drawer stays as a second,
        always-synced way to change the same values."""
        from .overlay import OVERLAY_FIELDS
        tab = QWidget()
        tab.setStyleSheet(f"background: {theme.INK_0};")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        title = QLabel("OVERLAY")
        title.setFont(QFont("Georgia", 11))
        title.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        outer.addWidget(title)

        rule = QFrame()
        rule.setFrameShape(QFrame.NoFrame)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {theme.RUNE_FAINT};")
        outer.addWidget(rule)

        form = QFormLayout()
        form.setSpacing(12)

        vis_row = QHBoxLayout()
        self._ov_show_btn = QPushButton("Show overlay")
        self._ov_show_btn.clicked.connect(self._on_ov_show_hide)
        vis_row.addWidget(self._ov_show_btn)
        self._ov_lock_btn = QPushButton("Lock")
        self._ov_lock_btn.setCheckable(True)
        self._ov_lock_btn.clicked.connect(self._on_ov_lock_toggled)
        vis_row.addWidget(self._ov_lock_btn)
        vis_row.addStretch(1)
        form.addRow("Visibility:", vis_row)

        self._ov_orient = QComboBox()
        self._ov_orient.addItem("Vertical (stacked)", "vertical")
        self._ov_orient.addItem("Horizontal (strip)", "horizontal")
        self._ov_orient.currentIndexChanged.connect(
            lambda _i: self._push_overlay_settings())
        form.addRow("Layout:", self._ov_orient)

        op_row = QHBoxLayout()
        self._ov_opacity = QSlider(Qt.Horizontal)
        self._ov_opacity.setRange(20, 100)
        self._ov_opacity.valueChanged.connect(
            lambda _v: self._push_overlay_settings())
        op_row.addWidget(self._ov_opacity, 1)
        self._ov_opacity_val = QLabel("")
        self._ov_opacity_val.setFont(QFont("Consolas", 10))
        self._ov_opacity_val.setMinimumWidth(48)
        op_row.addWidget(self._ov_opacity_val)
        form.addRow("Opacity:", op_row)

        lop_row = QHBoxLayout()
        self._ov_locked_opacity = QSlider(Qt.Horizontal)
        self._ov_locked_opacity.setRange(20, 100)
        self._ov_locked_opacity.valueChanged.connect(
            lambda _v: self._push_overlay_settings())
        lop_row.addWidget(self._ov_locked_opacity, 1)
        self._ov_locked_opacity_val = QLabel("")
        self._ov_locked_opacity_val.setFont(QFont("Consolas", 10))
        self._ov_locked_opacity_val.setMinimumWidth(48)
        lop_row.addWidget(self._ov_locked_opacity_val)
        form.addRow("Locked opacity:", lop_row)
        outer.addLayout(form)

        fields_label = QLabel("FIELDS")
        fields_label.setFont(QFont("Georgia", 9))
        fields_label.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        outer.addWidget(fields_label)
        fields_row = QHBoxLayout()
        self._ov_checks: dict[str, QCheckBox] = {}
        for key, label in OVERLAY_FIELDS:
            cb = QCheckBox(label)
            cb.toggled.connect(lambda _c: self._push_overlay_settings())
            fields_row.addWidget(cb)
            self._ov_checks[key] = cb
        fields_row.addStretch(1)
        outer.addLayout(fields_row)

        outer.addStretch(1)
        note = QLabel("Changes apply to the overlay immediately, even mid-session.")
        note.setFont(QFont("Georgia", 10))
        note.setStyleSheet(f"color: {theme.ASH_BRIGHT}; font-style: italic;")
        outer.addWidget(note)

        self._overlay_tab_idx = self._tabs.addTab(tab, "Overlay")

    def _build_debug_tab(self) -> None:
        self._debug = DebugConsole()
        self._tabs.addTab(self._debug, "Debug")

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------
    def _toggle_tracking(self) -> None:
        if self._consumer.running:
            self._consumer.stop()
            self.btn_toggle.setText("Start")
            self._set_status("Tracking stopped.")
            self._on_consumer_status("tracking stopped by user")
            self._refresh_sessions()
        else:
            # The hooks must live in the GAME process — the tracker's own
            # copy of the DLL never sees game traffic. Make sure the DLL
            # is injected before starting a session.
            self._on_consumer_status("[1/6] looking for game process...")
            game = _inject_tool.DEFAULT_PROCESS
            pid = _inject_tool.find_pid(game)
            if pid == 0:
                msg = f"game '{game}' not running — start the game first"
                self._on_consumer_status(msg)
                QMessageBox.critical(
                    self, "Game not running", msg + "."
                )
                return
            self._on_consumer_status(f"[2/6] found game PID {pid}")
            if _inject_tool.is_module_loaded(pid, "sr_tracker.dll"):
                self._on_consumer_status(f"game PID {pid} already has sr_tracker.dll")
            else:
                self._on_consumer_status(
                    f"[3/6] injecting sr_tracker.dll into game PID {pid}..."
                )
                try:
                    _inject_tool.inject(pid, _paths.dll_path())
                except SystemExit as e:
                    msg = f"injection into PID {pid} failed: {e.code}"
                    self._on_consumer_status(msg + " — try running the tracker as administrator")
                    QMessageBox.critical(self, "Injection failed", msg + ".")
                    return
                except Exception as e:  # noqa: BLE001 — surface whatever ctypes raises
                    msg = f"injection into PID {pid} failed: {e!r}"
                    self._on_consumer_status(msg)
                    QMessageBox.critical(self, "Injection failed", msg)
                    return
                self._on_consumer_status("[4/6] injection done — hooks should be live")
            self._on_consumer_status(f"[5/6] dll.active()={self._dll.active()}, calling install()...")
            if not self._dll.active() and not self._dll.install():
                self._on_consumer_status("hook install FAILED — is the game running?")
                QMessageBox.critical(
                    self,
                    "Could not install hooks",
                    "The DLL could not install its hooks. Is the game running "
                    "and on the same account?",
                )
                return
            self._on_consumer_status(
                f"[6/6] hooks active={self._dll.active()} — starting consumer..."
            )
            sid = self._consumer.start()
            self.btn_toggle.setText("Stop")
            self._set_status(f"Session #{sid} started.")

    def _toggle_overlay(self) -> None:
        if self._overlay is None or not self._overlay.isVisible():
            self._overlay = OverlayWindow(
                self._db,
                self._settings_store,
                lambda: self._display_session_id(),
                on_settings_changed=self._on_overlay_settings_changed,
            )
            self._overlay.show()
            self.btn_overlay.setText("Hide overlay")
        else:
            self._overlay.hide()
            self.btn_overlay.setText("Show overlay")
        self._refresh_overlay_tab()

    def _reset_session(self) -> None:
        sid = self._consumer.session_id
        if sid is None:
            QMessageBox.information(self, "No active session", "Click Start to begin tracking.")
            return
        ans = QMessageBox.question(
            self,
            "Reset session?",
            "All kills, drops, XP, and zone visits for the current session "
            "will be deleted. The session continues under the same id.",
        )
        if ans != QMessageBox.Yes:
            return
        try:
            self._db.reset_session(sid)
        except Exception as e:
            QMessageBox.critical(self, "Reset failed", str(e))
            return
        self.tbl_kills.setRowCount(0)
        self._show_empty(self.tbl_kills, self._kills_empty, True)
        self.tbl_drops.setRowCount(0)
        self._show_empty(self.tbl_drops, self._drops_empty, True)
        self._set_status("Session reset.")

    # ------------------------------------------------------------------
    # Lock / unlock
    # ------------------------------------------------------------------
    def _on_lock_button_clicked(self) -> None:
        desired = self.btn_lock.isChecked()
        if self._overlay is None or not self._overlay.isVisible():
            self._toggle_overlay()
        if self._overlay is not None:
            self._overlay.set_locked(desired)
        self._refresh_lock_button()

    def _refresh_lock_button(self) -> None:
        locked = self._settings.overlay_locked
        self.btn_lock.blockSignals(True)
        self.btn_lock.setChecked(locked)
        self.btn_lock.setText("Unlock" if locked else "Lock")
        self.btn_lock.blockSignals(False)
        if hasattr(self, "_ov_lock_btn"):
            self._ov_lock_btn.blockSignals(True)
            self._ov_lock_btn.setChecked(locked)
            self._ov_lock_btn.setText("Unlock" if locked else "Lock")
            self._ov_lock_btn.blockSignals(False)

    def _on_overlay_settings_changed(self, settings: Settings) -> None:
        self._settings = settings
        self._refresh_lock_button()
        self._refresh_overlay_tab()

    # ------------------------------------------------------------------
    # Overlay tab (live settings)
    # ------------------------------------------------------------------
    def _on_tab_changed(self, idx: int) -> None:
        # Refresh the Overlay tab's controls when switched to, so they
        # never show values the overlay's own drawer changed earlier.
        # (The per-second tick deliberately leaves this tab alone — a
        # refresh mid-drag would fight the slider being dragged.)
        if hasattr(self, "_overlay_tab_idx") and idx == self._overlay_tab_idx:
            self._refresh_overlay_tab()

    def _refresh_overlay_tab(self) -> None:
        if not hasattr(self, "_ov_show_btn"):
            return
        s = self._settings_store.load()
        self._ov_show_btn.setText(
            "Hide overlay"
            if (self._overlay is not None and self._overlay.isVisible())
            else "Show overlay")
        self._ov_lock_btn.blockSignals(True)
        self._ov_lock_btn.setChecked(s.overlay_locked)
        self._ov_lock_btn.setText("Unlock" if s.overlay_locked else "Lock")
        self._ov_lock_btn.blockSignals(False)
        self._ov_orient.blockSignals(True)
        oi = self._ov_orient.findData(s.overlay_orientation)
        self._ov_orient.setCurrentIndex(oi if oi >= 0 else 0)
        self._ov_orient.blockSignals(False)
        self._ov_opacity.blockSignals(True)
        self._ov_opacity.setValue(int(s.overlay_opacity * 100))
        self._ov_opacity.blockSignals(False)
        self._ov_opacity_val.setText(f"{int(s.overlay_opacity * 100)}%")
        self._ov_locked_opacity.blockSignals(True)
        self._ov_locked_opacity.setValue(int(s.overlay_locked_opacity * 100))
        self._ov_locked_opacity.blockSignals(False)
        self._ov_locked_opacity_val.setText(
            f"{int(s.overlay_locked_opacity * 100)}%")
        for key, cb in self._ov_checks.items():
            cb.blockSignals(True)
            cb.setChecked(getattr(s, f"overlay_show_{key}", True))
            cb.blockSignals(False)

    def _push_overlay_settings(self) -> None:
        """Read the Overlay tab's controls into the settings store and
        reload a visible overlay in place."""
        s = self._settings_store.load()
        s.overlay_orientation = (
            self._ov_orient.currentData() or "vertical")
        s.overlay_opacity = self._ov_opacity.value() / 100.0
        s.overlay_locked_opacity = self._ov_locked_opacity.value() / 100.0
        for key, cb in self._ov_checks.items():
            setattr(s, f"overlay_show_{key}", cb.isChecked())
        self._settings_store.save(s)
        self._settings = s
        self._ov_opacity_val.setText(f"{self._ov_opacity.value()}%")
        self._ov_locked_opacity_val.setText(
            f"{self._ov_locked_opacity.value()}%")
        if self._overlay is not None and self._overlay.isVisible():
            self._overlay.reload_settings()
        self._refresh_lock_button()

    def _on_ov_show_hide(self) -> None:
        self._toggle_overlay()
        self._refresh_overlay_tab()

    def _on_ov_lock_toggled(self) -> None:
        desired = self._ov_lock_btn.isChecked()
        if self._overlay is None or not self._overlay.isVisible():
            self._toggle_overlay()
        if self._overlay is not None:
            self._overlay.set_locked(desired)
        self._refresh_overlay_tab()

    # ------------------------------------------------------------------
    # Refreshes
    # ------------------------------------------------------------------
    def _display_session_id(self) -> int | None:
        """Session whose stats the UI shows: the active one while
        tracking, else the last session this process ran, else the
        newest session in the DB (fresh start). Stopping tracking no
        longer blanks summary/kills/drops — they keep showing the
        last session until a new one starts."""
        sid = self._consumer.session_id
        if sid is not None:
            return sid
        sid = self._consumer.last_session_id
        if sid is not None:
            return sid
        try:
            past = self._db.past_sessions(1)
        except Exception:
            return None
        return past[0]["id"] if past else None

    def _refresh_summary(self) -> None:
        sid = self._display_session_id()
        if sid is None:
            self._summary.set_summary(None)
            return
        try:
            s = self._db.summary(sid)
        except Exception as e:
            self._set_status(f"(error: {e})")
            return
        self._summary.set_summary(s)
        self._set_status(
            f"Session #{sid}  ·  {_mine_total(s['my_kills'], s['kills'])} kills"
            f"  ·  {_mine_total(s['my_soul_crystals'], s['soul_crystals'])} SC"
        )

    def _refresh_sessions(self) -> None:
        rows = self._db.sessions()
        self.tbl_sessions.setRowCount(len(rows))
        for i, r in enumerate(rows):
            # The "I" column is a roman numeral, the way a journal page would
            # mark a session entry. The real session id rides along in
            # UserRole so double-click can open the detail view.
            item0 = _cell(f"{(i+1):02d}", align=Qt.AlignCenter)
            item0.setData(Qt.UserRole, r["id"])
            self.tbl_sessions.setItem(i, 0, item0)
            self.tbl_sessions.setItem(i, 1, _cell(r["started"]))
            self.tbl_sessions.setItem(i, 2, _cell(r["ended"] or "—"))
            self.tbl_sessions.setItem(
                i, 3, _cell(_mine_total(r["my_kills"], r["kills"]),
                            align=Qt.AlignRight))
            self.tbl_sessions.setItem(
                i, 4, _cell(_mine_total(r["my_drops"], r["drops"]),
                            align=Qt.AlignRight))
        self._show_empty(self.tbl_sessions, self._sessions_empty, len(rows) == 0)

    def _refresh_kills(self) -> None:
        sid = self._display_session_id()
        if sid is None:
            self.tbl_kills.setRowCount(0)
            self._show_empty(self.tbl_kills, self._kills_empty, True)
            return
        rows = self._db.recent_kills(sid, 200, names=self._names)
        self.tbl_kills.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.tbl_kills.setItem(i, 0, _cell(r["ts"]))
            self.tbl_kills.setItem(i, 1, _cell(str(r["enemy_id"]), align=Qt.AlignRight))
            self.tbl_kills.setItem(i, 2, _cell(r["name"]))
            self.tbl_kills.setItem(
                i, 3, _cell("✓" if r["is_mine"] else "—", align=Qt.AlignCenter))
        self._show_empty(self.tbl_kills, self._kills_empty, len(rows) == 0)

    def _refresh_drops(self) -> None:
        sid = self._display_session_id()
        if sid is None:
            self.tbl_drops.setRowCount(0)
            self._show_empty(self.tbl_drops, self._drops_empty, True)
            return
        acct = self._db.session_account(sid)
        rows = self._db.recent_drops(sid, 200, names=self._names,
                                     local_account=acct)
        filt = self._drops_filter.text().strip().lower()
        if filt:
            rows = [r for r in rows if filt in _drop_label(r).lower()
                    or filt in str(r["item_id"] or "")]
        self.tbl_drops.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.tbl_drops.setItem(i, 0, _cell(r["ts"]))
            self.tbl_drops.setItem(i, 1, _cell(f"#{r['drop_id']}", align=Qt.AlignRight))
            self.tbl_drops.setItem(i, 2, _cell(_drop_label(r)))
            self.tbl_drops.setItem(i, 3, _cell(str(r["amount"] or ""), align=Qt.AlignRight))
            self.tbl_drops.setItem(i, 4, _cell(_owner_label(r["belongs_to"], acct)))
        self._show_empty(self.tbl_drops, self._drops_empty, len(rows) == 0)

    def _open_session_detail(self, item: QTableWidgetItem) -> None:
        """Double-click on a Sessions row opens a read-only detail view
        for that session (totals, zones, kills, drops)."""
        sid = item.data(Qt.UserRole) if item is not None else None
        if sid is None:
            return
        dlg = _SessionDetailDialog(self._db, self._names, int(sid), self)
        dlg.exec()

    def _wipe_database(self) -> None:
        if self._consumer.session_id is not None:
            QMessageBox.information(
                self,
                "Stop tracking first",
                "Stop the current session (press Stop) before wiping "
                "the database.",
            )
            return
        ans = QMessageBox.question(
            self,
            "Wipe database?",
            "Every recorded session — kills, drops, XP, zones, damage — "
            "will be permanently deleted. This cannot be undone.",
        )
        if ans != QMessageBox.Yes:
            return
        ans2 = QMessageBox.question(
            self,
            "Really wipe everything?",
            "Last chance: delete ALL sessions and start fresh?",
        )
        if ans2 != QMessageBox.Yes:
            return
        try:
            self._db.wipe()
        except Exception as e:
            QMessageBox.critical(self, "Wipe failed", str(e))
            return
        self._refresh_sessions()
        self._refresh_summary()
        self._refresh_current_tab()
        self._set_status("Database wiped.")

    @staticmethod
    def _show_empty(table: QTableWidget, note: QLabel, empty: bool) -> None:
        """Swap between the table and its empty-state note, the same
        way the summary tab swaps its whole right column."""
        note.setVisible(empty)
        table.setVisible(not empty)

    def _refresh_zones(self) -> None:
        """Per-zone stats for the active session, or the latest session if none."""
        sid = self._display_session_id()
        if sid is None:
            self.tbl_zones.setRowCount(0)
            self._show_empty(self.tbl_zones, self._zones_empty, True)
            return
        rows = self._db.zone_stats(sid)
        # An still-open visit in the ACTIVE session is ongoing right now:
        # measure it against the current time so the row (and its rates)
        # stay live instead of showing dashes until the next zone change.
        now_iso = None
        if sid == self._consumer.session_id:
            now_iso = datetime.now().isoformat(timespec="milliseconds")
        self.tbl_zones.setRowCount(len(rows))
        for i, z in enumerate(rows):
            left = z["left_at"] or now_iso
            secs = _zone_seconds(z["entered_at"], left)
            time_str = _fmt_duration(secs)
            hrs = (secs / 3600.0) if secs else 0.0
            kpm = (z["kills"] / (secs / 60)) if secs else 0.0
            self.tbl_zones.setItem(i, 0, _cell(z["display_name"]))
            self.tbl_zones.setItem(i, 1, _cell(_fmt_clock(z["entered_at"])))
            self.tbl_zones.setItem(
                i, 2, _cell(_fmt_clock(left) if z["left_at"] else "now"))
            self.tbl_zones.setItem(i, 3, _cell(time_str, align=Qt.AlignRight))
            self.tbl_zones.setItem(
                i, 4, _cell(_mine_total(z["my_kills"], z["kills"]),
                            align=Qt.AlignRight))
            self.tbl_zones.setItem(
                i, 5, _cell(_mine_total(z["my_soul_crystals"],
                                        z["soul_crystals"]),
                            align=Qt.AlignRight))
            self.tbl_zones.setItem(
                i, 6, _cell(_fmt_rate(z["soul_crystals"], hrs),
                            align=Qt.AlignRight))
            self.tbl_zones.setItem(
                i, 7, _cell(_mine_total(z["my_drops"], z["drops"]),
                            align=Qt.AlignRight))
            self.tbl_zones.setItem(
                i, 8, _cell(_fmt_rate(z["drops"], hrs),
                            align=Qt.AlignRight))
            self.tbl_zones.setItem(i, 9, _cell(f"{z['xp']:,}", align=Qt.AlignRight))
            self.tbl_zones.setItem(
                i, 10, _cell(_fmt_rate(z["xp"], hrs), align=Qt.AlignRight))
            self.tbl_zones.setItem(i, 11, _cell(f"{kpm:.2f}", align=Qt.AlignRight))
        self._show_empty(self.tbl_zones, self._zones_empty, len(rows) == 0)

    def _refresh_sessions_combo(self) -> None:
        """Populate the Graphs tab's session selector dropdown.

        The combo used to start out empty (it was only filled by the
        Re-read button), so the Graphs tab showed "No data" with no
        session to choose until you knew to click Re-read. Now it
        fills on startup and prefers the active session, then the
        previous text, then the newest session."""
        cur_text = self.cmb_sessions.currentText()
        cur_data = self.cmb_sessions.currentData()
        self.cmb_sessions.blockSignals(True)
        self.cmb_sessions.clear()
        for s in self._db.past_sessions(50):
            label = f"#{s['id']}  {s['started']}"
            if s.get("current_zone"):
                label += f"  -  {s['current_zone']}"
            self.cmb_sessions.addItem(label, s["id"])
        active = self._consumer.session_id
        idx = -1
        if active is not None:
            idx = self.cmb_sessions.findData(active)
        if idx < 0 and cur_data is not None:
            idx = self.cmb_sessions.findData(cur_data)
        if idx < 0 and cur_text:
            idx = self.cmb_sessions.findText(cur_text)
        if idx < 0 and self.cmb_sessions.count() > 0:
            idx = 0
        if idx >= 0:
            self.cmb_sessions.setCurrentIndex(idx)
        self.cmb_sessions.blockSignals(False)
        self._refresh_graphs()

    def _refresh_graphs(self) -> None:
        """Per-zone rates for the selected session."""
        sid = self.cmb_sessions.currentData()
        if sid is None:
            self.chart_kpm.clear()
            self.chart_drops_m.clear()
            self.chart_sc_m.clear()
            self.chart_zones.clear()
            return
        zones = self._db.zone_stats(sid)
        kpm_bars = []
        drops_bars = []
        sc_bars = []
        for z in zones:
            # Compute time spent
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(z["entered_at"])
                t1 = datetime.fromisoformat(z["left_at"]) if z["left_at"] else None
            except Exception:
                t0 = t1 = None
            if t0 and t1:
                secs = (t1 - t0).total_seconds()
                minutes = secs / 60
            else:
                minutes = 0
            label = z["display_name"]
            if minutes > 0:
                kpm = z["kills"] / minutes
                drops_m = z["drops"] / minutes
                sc_m = z["soul_crystals"] / minutes
            else:
                kpm = drops_m = sc_m = 0
            kpm_bars.append(Bar(label=label, value=kpm, sublabel=f"{z['kills']} kills"))
            drops_bars.append(Bar(label=label, value=drops_m, sublabel=f"{z['drops']} drops"))
            sc_bars.append(Bar(label=label, value=sc_m, sublabel=f"{_mine_total(z['my_soul_crystals'], z['soul_crystals'])} SC"))
        self.chart_kpm.set_bars(kpm_bars)
        self.chart_drops_m.set_bars(drops_bars)
        self.chart_sc_m.set_bars(sc_bars)
        # Per-zone totals bar chart
        totals_bars = [Bar(label=z["display_name"], value=z["kills"], sublabel=f"{_mine_total(z['my_soul_crystals'], z['soul_crystals'])} SC")
                       for z in zones]
        self.chart_zones.set_bars(totals_bars)

    def _refresh_current_tab(self) -> None:
        """Refresh whichever of Sessions/Kills/Drops/Zones/Graphs is on screen right
        now, so switching tabs — or just leaving one open — doesn't show
        data that's a click behind reality."""
        idx = self._tabs.currentIndex()
        if idx == 1:
            self._refresh_sessions()
        elif idx == 2:
            self._refresh_kills()
        elif idx == 3:
            self._refresh_drops()
        elif idx == 4:
            self._refresh_zones()
        elif idx == 5:
            self._refresh_graphs()
        elif idx == 6:
            pass  # overlay tab refreshes on switch/change, never on tick
        elif idx == 7 and hasattr(self, "_debug"):
            pass  # debug tab is its own thing

    def _on_event_seen(self, raw: str) -> None:
        """Slot for the consumer's `event_seen` signal. Routes the raw
        event to the debug console (if present) and, in any case, kicks
        a refresh of the visible table so a kill or drop appears the
        moment it's recorded, not on the next 1-second tick."""
        if hasattr(self, "_debug") and self._debug is not None:
            self._debug.append_event(raw)
        # Refresh whichever table is on screen, throttled: a combat
        # burst (damage + deaths + drops per kill) used to rebuild the
        # visible 200-row table on every single event, saturating the
        # GUI thread until the app looked frozen and the Debug console
        # looked stalled. The 1s tick fills in whatever we skip here.
        now = time.monotonic()
        if now - self._last_tab_refresh >= 0.5:
            self._last_tab_refresh = now
            self._refresh_current_tab()
        self._update_counter_label()
    def _on_consumer_status(self, msg: str) -> None:
        if hasattr(self, "_debug") and self._debug is not None:
            self._debug.append_consumer_status(msg)
        else:
            # Frozen exe has no debug tab — still surface it in the status bar.
            self._set_status(msg)

    def _on_tick(self) -> None:
        self._refresh_summary()
        self._refresh_current_tab()
        self._watch_for_silence()

    # How long tracking may go without a GAMEPLAY event before we say so
    # out loud. This deliberately watches gameplay (kills, drops, xp,
    # zones), not bridge traffic: the DLL heartbeat every ~30s proves
    # the bridge is alive, so a bridge-idle watchdog would never fire.
    # Fires once per idle stretch so the Debug console records when the
    # gameplay silence started and what the bridge looked like then.
    _SILENCE_AFTER_S = 90.0

    def _watch_for_silence(self) -> None:
        c = self._consumer
        if c is None or not c.running:
            self._silence_warned = False
            return
        try:
            idle = c.seconds_since_gameplay()
            bridge_idle = c.seconds_since_event()
        except Exception:
            return
        if idle < self._SILENCE_AFTER_S:
            self._silence_warned = False
            return
        if self._silence_warned:
            return
        self._silence_warned = True
        # Bridge alive (heartbeats flowing) but no gameplay = the hooks
        # are blind to game traffic — the stall signature. Full silence
        # instead means the producer is gone; check the game process.
        if bridge_idle < self._SILENCE_AFTER_S:
            self._on_consumer_status(
                f"no game events for {int(idle)}s, but the bridge is alive"
                f" (last bridge traffic {int(bridge_idle)}s ago) — the DLL"
                " is in the game but its hooks see no game traffic"
            )
            return
        # Producer head: frozen while the game is quiet OR the hooks are
        # blind; advancing with "no events" would mean the consumer is
        # stuck draining (a resync line would have said so already).
        shm = ""
        try:
            dbg = c._dll.debug_state()  # type: ignore[attr-defined]
            shm = (f" shm head={dbg.get('head')} tail={dbg.get('tail')}"
                   f" gen={dbg.get('generation')}")
        except Exception:
            pass
        # Game process: relaunches and channel-hop reconnects are the
        # prime suspects for a bridge that dies mid-session with zero
        # diagnostics, so check whether the game (and our DLL in it) is
        # even there anymore.
        game_state = ""
        try:
            game = _inject_tool.DEFAULT_PROCESS
            pid = _inject_tool.find_pid(game)
            if not pid:
                game_state = (f" {game} is not running — if it closed or"
                              " relaunched, Stop and Start tracking again")
            elif not _inject_tool.is_module_loaded(pid, "sr_tracker.dll"):
                game_state = (f" game PID={pid} alive but sr_tracker.dll is"
                              " not loaded in it — Stop, then Start to reinject")
            else:
                game_state = (f" game PID={pid} alive, DLL loaded — hooks see"
                              " no traffic (channel reconnect the DLL missed?)")
        except Exception:
            pass
        self._on_consumer_status(
            f"no game events for {int(idle)}s while tracking;{shm}.{game_state}"
        )

    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def _update_counter_label(self) -> None:
        c = self._consumer
        if c is None or not hasattr(self, "_lbl_counters"):
            return
        seen = c.events_seen
        parsed = c.events_parsed
        dropped = c.events_dropped
        if seen == 0 and parsed == 0 and dropped == 0:
            self._lbl_counters.setText("")
            return
        # Highlight dropped events in red so a problem is obvious.
        if dropped > 0:
            self._lbl_counters.setStyleSheet(
                f"color: {theme.CRIMSON}; background: transparent; font-weight: bold;"
            )
        else:
            self._lbl_counters.setStyleSheet(
                f"color: {theme.CRYSTAL_LIGHT}; background: transparent;"
            )
        # Idle readout: seconds since the consumer last handled an event.
        # Lets the user tell "game is quiet, bridge is fine" apart from
        # "bridge is dead" without guessing.
        idle = ""
        try:
            idle_s = int(c.seconds_since_event())
            idle = f"  idle: {idle_s}s"
        except Exception:
            pass
        self._lbl_counters.setText(
            f"events: {seen}  parsed: {parsed}  dropped: {dropped}{idle}"
        )
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, e) -> None:
        # The app is set to NOT quit on last-window-closed so the tray
        # icon can keep the process alive. That means clicking the X on
        # this window would otherwise just hide it and leave a zombie
        # process — instead, the user closing the main window is the
        # "quit" gesture. The tray's "Quit" entry still works because it
        # calls `app.quit()` directly.
        try:
            if self._overlay is not None:
                self._overlay.close()
            if self._consumer.running:
                self._consumer.stop()
            if self._dll.active():
                self._dll.uninstall()
        finally:
            super().closeEvent(e)
            QApplication.instance().quit()


def _cell(text: str, *, align: Qt.AlignmentFlag = Qt.AlignLeft) -> QTableWidgetItem:
    """Helper: a table cell styled like a field-log entry (data voice)."""
    item = QTableWidgetItem(text)
    item.setTextAlignment(align | Qt.AlignVCenter)
    f = QFont("Consolas", 10)
    item.setFont(f)
    return item


def _align_headers(table: QTableWidget, aligns: list[Qt.AlignmentFlag]) -> None:
    """Match each column header's alignment to its cells'. Headers
    default to left-aligned while numeric cells are right-aligned, so
    without this the titles sit off from the text under them."""
    for i, a in enumerate(aligns):
        item = table.horizontalHeaderItem(i)
        if item is not None:
            item.setTextAlignment(a | Qt.AlignVCenter)


def _empty_note(text: str) -> QLabel:
    """An empty-state message in the same italic Georgia voice the
    summary tab already uses for 'No session active.' — so an empty
    table reads as designed, not as a blank list nobody filled in."""
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setFont(QFont("Georgia", 11))
    lbl.setStyleSheet(f"color: {theme.ASH_BRIGHT}; font-style: italic;")
    lbl.setVisible(False)
    return lbl


# Wire sentinel for "drop has no owner yet". The DLL emits it as an
# unsigned 32-bit value, so it arrives in Python as 4294967295.
_UNCLAIMED = 0xFFFFFFFF


def _mine_total(mine: int, total: int) -> str:
    """'yours/total' when they differ (e.g. 8/12), else the plain total.
    Used everywhere a count mixes party-wide events with the local
    player's share."""
    if mine != total:
        return f"{mine}/{total}"
    return str(total)


def _drop_label(r: dict) -> str:
    """Display name for a drop row: real name when the lookup knows it
    (e.g. "Red Crystal"), else Soul Crystal / Item #id."""
    item_id = r["item_id"]
    if item_id == 0:
        return "Soul Crystal"
    if r.get("item_name"):
        return r["item_name"]
    if item_id is None:
        return "—"
    return f"Item #{item_id}"


def _owner_label(belongs_to: int | None, local_account: int | None) -> str:
    """Human-readable drop owner: You, Unclaimed, #id, or —."""
    if belongs_to is None:
        return "—"
    if local_account is not None and belongs_to == local_account:
        return "You"
    if belongs_to == _UNCLAIMED:
        return "Unclaimed"
    return f"#{belongs_to}"


def _zone_seconds(entered_at: str | None, left_at: str | None) -> float | None:
    """Seconds between two ISO timestamps, or None when the visit is
    still open or either side is unparseable."""
    try:
        t0 = datetime.fromisoformat(entered_at) if entered_at else None
        t1 = datetime.fromisoformat(left_at) if left_at else None
    except Exception:
        return None
    if not t0 or not t1:
        return None
    secs = (t1 - t0).total_seconds()
    return secs if secs >= 0 else None


def _fmt_duration(secs: float | None) -> str:
    if secs is None:
        return "—"
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m{int(secs % 60)}s"
    return f"{int(secs / 3600)}h{int((secs % 3600) / 60)}m"


def _fmt_clock(ts: str | None) -> str:
    """ISO timestamp → HH:MM:SS for the zone ENTERED/LEFT columns."""
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except Exception:
        return "—"


def _fmt_rate(value: int | float, hours: float) -> str:
    """value-per-hour, compact ("1.2k/h", "45/h", "3.1/h")."""
    if not hours or hours <= 0:
        return "—"
    r = value / hours
    if r >= 1000:
        return f"{r / 1000:.1f}k/h"
    if r >= 100:
        return f"{r:.0f}/h"
    return f"{r:.1f}/h"


def _session_hours(started: str | None, ended: str | None) -> float:
    """Session length in hours; an ongoing session measures to now."""
    try:
        t0 = datetime.fromisoformat(started) if started else None
    except Exception:
        t0 = None
    try:
        t1 = datetime.fromisoformat(ended) if ended else None
    except Exception:
        t1 = None
    if t0 is None:
        return 0.0
    secs = ((t1 or datetime.now()) - t0).total_seconds()
    return secs / 3600.0 if secs > 0 else 0.0


def _ro_table(ncols: int, headers: list[str],
              aligns: list[Qt.AlignmentFlag] | None = None) -> QTableWidget:
    """A read-only log table matching the main window's field-log voice."""
    t = QTableWidget(0, ncols)
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    if aligns:
        _align_headers(t, aligns)
    t.verticalHeader().setVisible(False)
    t.setShowGrid(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    return t


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont("Georgia", 10))
    lbl.setStyleSheet(
        f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
    )
    return lbl


class _SessionDetailDialog(QDialog):
    """Click-through detail for one session: totals, per-zone breakdown,
    and the session's kills/drops with ownership marked. Read-only."""

    def __init__(self, db, names, session_id: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Session #{session_id}")
        self.setMinimumSize(760, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        s = db.summary(session_id)
        acct = db.session_account(session_id)
        meta = next(
            (r for r in db.past_sessions(10000) if r["id"] == session_id),
            None,
        )
        started = meta["started"] if meta else "—"
        ended = meta["ended"] if meta and meta["ended"] else "ongoing"

        layout.addWidget(_section_label(f"SESSION  #{session_id}"))
        form = QFormLayout()
        form.addRow("Period:", QLabel(f"{started}  →  {ended}"))
        form.addRow("Account:", QLabel(str(acct) if acct is not None else "unknown"))
        form.addRow("Kills:",
                    QLabel(f"{s['my_kills']} yours / {s['kills']} total"))
        form.addRow("Drops:",
                    QLabel(f"{s['my_drops']} yours / {s['drops']} total"))
        form.addRow("Soul crystals:",
                    QLabel(f"{s['my_soul_crystals']} yours / "
                           f"{s['soul_crystals']} total"))
        form.addRow("Experience:",
                    QLabel(f"{s['xp']:,}  (level {s['level']})"))
        hrs = _session_hours(meta["started"] if meta else None,
                             meta["ended"] if meta else None)
        mins = hrs * 60.0
        kpm = f"{(s['kills'] / mins):.2f}" if mins > 0 else "—"
        form.addRow("Rates:",
                    QLabel(f"{kpm} KPM  ·  {_fmt_rate(s['soul_crystals'], hrs)} SC  ·  "
                           f"{_fmt_rate(s['xp'], hrs)} XP  ·  "
                           f"{_fmt_rate(s['drops'], hrs)} drops"))
        layout.addLayout(form)

        layout.addWidget(_section_label("ZONES"))
        zones = _ro_table(7, ["ZONE", "ENTERED", "LEFT", "TIME", "KILLS",
                              "DROPS", "XP"],
                          [Qt.AlignLeft, Qt.AlignLeft, Qt.AlignLeft,
                           Qt.AlignRight, Qt.AlignRight, Qt.AlignRight,
                           Qt.AlignRight])
        for z in db.zone_stats(session_id):
            i = zones.rowCount()
            zones.insertRow(i)
            zones.setItem(i, 0, _cell(z["display_name"]))
            zones.setItem(i, 1, _cell(_fmt_clock(z["entered_at"])))
            zones.setItem(i, 2, _cell(_fmt_clock(z["left_at"])))
            zones.setItem(i, 3, _cell(
                _fmt_duration(_zone_seconds(z["entered_at"], z["left_at"])),
                align=Qt.AlignRight))
            zones.setItem(i, 4, _cell(
                _mine_total(z["my_kills"], z["kills"]), align=Qt.AlignRight))
            zones.setItem(i, 5, _cell(
                _mine_total(z["my_drops"], z["drops"]), align=Qt.AlignRight))
            zones.setItem(i, 6, _cell(f"{z['xp']:,}", align=Qt.AlignRight))
        zones.setMinimumHeight(140)
        layout.addWidget(zones)

        layout.addWidget(_section_label("KILLS"))
        kills = _ro_table(3, ["TIME", "MOB", "MINE"],
                          [Qt.AlignLeft, Qt.AlignLeft, Qt.AlignCenter])
        for r in db.recent_kills(session_id, 200, names=names):
            i = kills.rowCount()
            kills.insertRow(i)
            kills.setItem(i, 0, _cell(r["ts"]))
            kills.setItem(i, 1, _cell(r["name"]))
            kills.setItem(i, 2, _cell("✓" if r["is_mine"] else "—",
                                       align=Qt.AlignCenter))
        kills.setMinimumHeight(140)
        layout.addWidget(kills)

        layout.addWidget(_section_label("DROPS"))
        filt_row = QHBoxLayout()
        filt_row.addStretch(1)
        self._drops_filter = QLineEdit()
        self._drops_filter.setPlaceholderText("Filter by item name…")
        self._drops_filter.setClearButtonEnabled(True)
        self._drops_filter.setMinimumWidth(220)
        self._drops_filter.textChanged.connect(
            lambda _t: self._populate_drops())
        filt_row.addWidget(self._drops_filter)
        layout.addLayout(filt_row)
        self._drops_table = _ro_table(
            4, ["TIME", "ITEM", "QTY", "OWNER"],
            [Qt.AlignLeft, Qt.AlignLeft, Qt.AlignRight, Qt.AlignLeft])
        self._drops_table.setMinimumHeight(140)
        layout.addWidget(self._drops_table)
        self._drops_rows = db.recent_drops(session_id, 200, names=names,
                                           local_account=acct)
        self._drops_acct = acct
        self._populate_drops()

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate_drops(self) -> None:
        """Fill the dialog's drops table, honoring the name filter."""
        filt = self._drops_filter.text().strip().lower()
        rows = self._drops_rows
        if filt:
            rows = [r for r in rows if filt in _drop_label(r).lower()
                    or filt in str(r["item_id"] or "")]
        t = self._drops_table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            t.setItem(i, 0, _cell(r["ts"]))
            t.setItem(i, 1, _cell(_drop_label(r)))
            t.setItem(i, 2, _cell(str(r["amount"] or ""),
                                  align=Qt.AlignRight))
            t.setItem(i, 3, _cell(
                _owner_label(r["belongs_to"], self._drops_acct)))