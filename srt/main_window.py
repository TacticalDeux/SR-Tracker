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
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox,
    QDialog, QDialogButtonBox, QFrame,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider, QStatusBar,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import paths as _paths
from . import theme
from REMOVED import inject as _inject_tool
from .consumer import EventConsumer
from .crystal import crystal_pixmap
from .debug_console import DebugConsole, is_dev_mode
from . import names as _names
from .overlay import OverlayWindow
from .settings import Settings, SettingsStore
from .charts import (
    Point, SeriesChart, SpiderChart, SpiderSeries,
    SPIDER_COLORS,
)

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
        self._deaths  = self._make_metric("DEATHS")
        self._xp_lost = self._make_metric("XP LOST")

        for w in (self._kills, self._sc, self._xp, self._level,
                  self._deaths, self._xp_lost):
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
        sc_total = s["sc_picked"] + s["sc_unpicked"]
        self._kills._num.setText(_mine_total(s["my_kills"], s["kills"]))
        self._kills._num.setToolTip(
            f"{s['my_kills']} yours / {s['kills']} session total")
        self._sc._num.setText(_mine_total(s["sc_picked"], sc_total))
        self._sc._num.setToolTip(f"{s['sc_picked']:,} picked up / {sc_total:,} total")
        self._xp._num.setText(f"{s['xp']:,}")
        self._xp._num.setToolTip(f"{s['xp']:,} session total")
        self._level._num.setText(str(s["level"]))
        self._level._num.setToolTip(f"Current level {s['level']}")
        self._deaths._num.setText(str(s.get("deaths", 0)))
        self._deaths._num.setToolTip(f"{s.get('deaths', 0)} deaths this session")
        self._xp_lost._num.setText(f"{s.get('xp_lost', 0):,}")
        self._xp_lost._num.setToolTip(
            f"{s.get('xp_lost', 0):,} XP lost to deaths")


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
        # Dropdown popups track the mouse so the hovered row always
        # highlights (see the QComboBox QAbstractItemView rules in
        # theme.py) — without tracking, the list only marks the
        # last-clicked row and hovering gives no feedback at all.
        for _cb in self.findChildren(QComboBox):
            _cb.view().setMouseTracking(True)
        # Snapshot tab pages by role. Refreshes match on widget
        # identity, never on hardcoded indices — the Debug tab only
        # exists in dev mode, so indices shift between dev and frozen.
        self._tab_summary = self._tabs.widget(0)
        self._tab_sessions = self._tabs.widget(1)
        self._tab_kills = self._tabs.widget(2)
        self._tab_drops = self._tabs.widget(3)
        self._tab_zones = self._tabs.widget(4)
        self._tab_graphs = self._tabs.widget(5)
        self._tab_overlay = self._tabs.widget(6)
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
        brand = QLabel("SOUL'S REMNANT")
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
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        self._summary = _SummaryPanel()
        layout.addWidget(self._summary)
        self._tabs.addTab(tab, "Summary")

    def _build_sessions_tab(self) -> None:
        tab = QWidget()
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
        # KILLS / DROPS read "yours/session-total" (e.g. 8/12).
        # Double-click a row to open that session's detail view.
        self.tbl_sessions.horizontalHeaderItem(3).setToolTip("Yours / session total kills")
        self.tbl_sessions.horizontalHeaderItem(4).setToolTip("Yours / session total drops")
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

        self.tbl_drops = QTableWidget(0, 6)
        self.tbl_drops.setHorizontalHeaderLabels(
            ["TIME", "DROP", "ITEM", "QTY", "OWNER", "STATUS"]
        )
        self.tbl_drops.horizontalHeaderItem(4).setToolTip(
            "Drop owner: You, Unclaimed, or the owning account id")
        self.tbl_drops.horizontalHeaderItem(5).setToolTip(
            "Pickup lifecycle: still on the ground, who picked it up, or gone")
        self.tbl_drops.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        _align_headers(self.tbl_drops,
                       [Qt.AlignLeft, Qt.AlignRight, Qt.AlignLeft,
                        Qt.AlignRight, Qt.AlignLeft, Qt.AlignLeft])
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
        self.tbl_zones.horizontalHeaderItem(4).setToolTip("Yours / session total kills")
        self.tbl_zones.horizontalHeaderItem(7).setToolTip("Yours / session total drops")
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

    # (metric key, menu label, value format). The Graphs tab charts
    # per-zone values for whichever of these is selected.
    _GRAPH_METRICS = (
        ("kills",   "Kills",           "{:.0f}"),
        ("drops",   "Drops",           "{:.0f}"),
        ("sc",      "Soul crystals",   "{:.0f}"),
        ("xp",      "Experience",      "{:.0f}"),
        ("kpm",     "Kills / min",     "{:.1f}"),
        ("drops_m", "Drops / min",     "{:.1f}"),
        ("sc_m",    "Soul cryst. / min", "{:.1f}"),
        ("xp_m",    "Experience / min", "{:.0f}"),
        ("minutes", "Minutes in zone", "{:.1f}"),
    )

    def _build_graphs_tab(self) -> None:
        """One configurable chart: session + metric + chart-type selectors
        drive a single per-zone chart, with the session totals as a strip
        above it. Replaces the old wall of fixed KPM/drops/SC charts —
        every combination the old charts showed is one selection away,
        plus XP and per-minute rates the old tab had no room for."""
        tab = QWidget()
        self._graphs_tab = tab
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        # Session selector
        selector_row = QHBoxLayout()
        selector_row.addWidget(_mini_title("SESSION"))
        self.cmb_sessions = QComboBox()
        self.cmb_sessions.currentIndexChanged.connect(self._refresh_graphs)
        self.cmb_sessions.setMinimumWidth(280)
        selector_row.addWidget(self.cmb_sessions, 1)
        btn_details = QPushButton("Details…")
        btn_details.setToolTip("Open the detail view for the selected session")
        btn_details.clicked.connect(self._open_selected_session_detail)
        selector_row.addWidget(btn_details)
        btn = QPushButton("Re-read")
        btn.clicked.connect(self._refresh_sessions_combo)
        selector_row.addWidget(btn)
        outer.addLayout(selector_row)

        # Metric + chart-type + zone selectors. Line is the default view:
        # it reads the shape of a session at a glance, which is what the
        # tab is for; bars suit exact read-offs, candles the pace over
        # time, spider the zone-vs-zone balance.
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(_mini_title("CHART"))
        self.cmb_metric = QComboBox()
        for key, label, _fmt in self._GRAPH_METRICS:
            self.cmb_metric.addItem(label, key)
        self.cmb_metric.setCurrentIndex(4)  # Kills / min
        self.cmb_metric.currentIndexChanged.connect(self._refresh_graphs)
        self.cmb_metric.setMinimumWidth(200)
        cfg_row.addWidget(self.cmb_metric)
        self.cmb_chart_type = QComboBox()
        self.cmb_chart_type.addItem("Line", "line")
        self.cmb_chart_type.addItem("Cumulative", "cumulative")
        self.cmb_chart_type.addItem("Bars", "bars")
        self.cmb_chart_type.addItem("Share", "share")
        self.cmb_chart_type.addItem("Spider", "spider")
        self.cmb_chart_type.setCurrentIndex(0)
        self.cmb_chart_type.currentIndexChanged.connect(self._refresh_graphs)
        cfg_row.addWidget(self.cmb_chart_type)
        cfg_row.addWidget(_mini_title("ZONE"))
        self.cmb_zone = QComboBox()
        self.cmb_zone.setMinimumWidth(200)
        self.cmb_zone.setToolTip(
            "All zones charts the stat over the whole session; picking "
            "a zone scopes every chart to the time spent in that "
            "visit. Repeat visits are numbered first-entered-first "
            "(#1, #2, …). Share always splits the whole session by "
            "zone, and spider always compares all zones, highlighting "
            "the picked one.")
        self.cmb_zone.currentIndexChanged.connect(self._refresh_graphs)
        cfg_row.addWidget(self.cmb_zone)
        cfg_row.addStretch(1)
        outer.addLayout(cfg_row)

        rule = QFrame()
        rule.setFrameShape(QFrame.NoFrame)
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {theme.RUNE_FAINT};")
        outer.addWidget(rule)

        # Session totals strip: what the session amounted to overall.
        self._graphs_totals = QLabel("")
        self._graphs_totals.setFont(QFont("Consolas", 10))
        self._graphs_totals.setStyleSheet(f"color: {theme.ASH_BRIGHT};")
        self._graphs_totals.setWordWrap(True)
        outer.addWidget(self._graphs_totals)

        # The chart lives in a scroll area: long per-zone lists grow
        # with the session, and a fixed tab would squash them to nothing.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        charts_host = QWidget()
        charts_lay = QVBoxLayout(charts_host)
        charts_lay.setContentsMargins(0, 0, 12, 0)
        charts_lay.setSpacing(20)

        self._graphs_section_title = _mini_title("PER-ZONE  IN  THIS  SESSION")
        charts_lay.addWidget(self._graphs_section_title)
        self.chart_main = SeriesChart(mode="line")
        self.chart_main.setMinimumHeight(300)
        charts_lay.addWidget(self.chart_main)
        self.chart_spider = SpiderChart()
        self.chart_spider.setMinimumHeight(340)
        self.chart_spider.setVisible(False)
        charts_lay.addWidget(self.chart_spider)
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
        self._ov_opacity.setRange(0, 100)
        self._ov_opacity.valueChanged.connect(
            lambda _v: self._push_overlay_settings())
        op_row.addWidget(self._ov_opacity, 1)
        self._ov_opacity_val = QLabel("")
        self._ov_opacity_val.setFont(QFont("Consolas", 10))
        self._ov_opacity_val.setMinimumWidth(48)
        op_row.addWidget(self._ov_opacity_val)
        form.addRow("Background:", op_row)

        lop_row = QHBoxLayout()
        self._ov_locked_opacity = QSlider(Qt.Horizontal)
        self._ov_locked_opacity.setRange(0, 100)
        self._ov_locked_opacity.valueChanged.connect(
            lambda _v: self._push_overlay_settings())
        lop_row.addWidget(self._ov_locked_opacity, 1)
        self._ov_locked_opacity_val = QLabel("")
        self._ov_locked_opacity_val.setFont(QFont("Consolas", 10))
        self._ov_locked_opacity_val.setMinimumWidth(48)
        lop_row.addWidget(self._ov_locked_opacity_val)
        form.addRow("Locked background:", lop_row)

        wop_row = QHBoxLayout()
        self._ov_window_opacity = QSlider(Qt.Horizontal)
        self._ov_window_opacity.setRange(0, 100)
        self._ov_window_opacity.setToolTip(
            "Fades background and text together")
        self._ov_window_opacity.valueChanged.connect(
            lambda _v: self._push_overlay_settings())
        wop_row.addWidget(self._ov_window_opacity, 1)
        self._ov_window_opacity_val = QLabel("")
        self._ov_window_opacity_val.setFont(QFont("Consolas", 10))
        self._ov_window_opacity_val.setMinimumWidth(48)
        wop_row.addWidget(self._ov_window_opacity_val)
        form.addRow("Opacity:", wop_row)

        lwop_row = QHBoxLayout()
        self._ov_locked_window_opacity = QSlider(Qt.Horizontal)
        self._ov_locked_window_opacity.setRange(0, 100)
        self._ov_locked_window_opacity.setToolTip(
            "Fades background and text together")
        self._ov_locked_window_opacity.valueChanged.connect(
            lambda _v: self._push_overlay_settings())
        lwop_row.addWidget(self._ov_locked_window_opacity, 1)
        self._ov_locked_window_opacity_val = QLabel("")
        self._ov_locked_window_opacity_val.setFont(QFont("Consolas", 10))
        self._ov_locked_window_opacity_val.setMinimumWidth(48)
        lwop_row.addWidget(self._ov_locked_window_opacity_val)
        form.addRow("Locked opacity:", lwop_row)

        scale_tab_row = QHBoxLayout()
        self._ov_scale = QSlider(Qt.Horizontal)
        self._ov_scale.setRange(70, 150)
        self._ov_scale.setToolTip("Scales the overlay text")
        self._ov_scale.valueChanged.connect(
            lambda _v: self._push_overlay_settings())
        scale_tab_row.addWidget(self._ov_scale, 1)
        self._ov_scale_val = QLabel("")
        self._ov_scale_val.setFont(QFont("Consolas", 10))
        self._ov_scale_val.setMinimumWidth(48)
        scale_tab_row.addWidget(self._ov_scale_val)
        form.addRow("Scale:", scale_tab_row)

        color_row = QHBoxLayout()
        self._ov_color_btn = QPushButton()
        self._ov_color_btn.setToolTip("Pick the overlay stats text color")
        self._ov_color_btn.setCursor(Qt.PointingHandCursor)
        self._ov_color_btn.clicked.connect(
            self._on_pick_overlay_text_color)
        color_row.addWidget(self._ov_color_btn)
        color_row.addStretch(1)
        form.addRow("Stats text color:", color_row)

        locked_color_row = QHBoxLayout()
        self._ov_locked_color_btn = QPushButton()
        self._ov_locked_color_btn.setToolTip(
            "Pick the overlay numbers' color while locked")
        self._ov_locked_color_btn.setCursor(Qt.PointingHandCursor)
        self._ov_locked_color_btn.clicked.connect(
            self._on_pick_overlay_locked_text_color)
        locked_color_row.addWidget(self._ov_locked_color_btn)
        locked_color_row.addStretch(1)
        form.addRow("Locked stats text color:", locked_color_row)
        outer.addLayout(form)

        fields_label = QLabel("FIELDS")
        fields_label.setFont(QFont("Georgia", 9))
        fields_label.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        outer.addWidget(fields_label)
        # One list owns both toggles and order: checkable rows the user
        # can drag (or nudge with the arrows) into display order.
        fields_row = QHBoxLayout()
        self._ov_fields = QListWidget()
        self._ov_fields.setDragDropMode(
            QAbstractItemView.InternalMove)
        self._ov_fields.setDefaultDropAction(Qt.MoveAction)
        self._ov_fields.setSelectionMode(
            QAbstractItemView.SingleSelection)
        self._ov_fields.setMaximumHeight(178)
        self._ov_fields.itemChanged.connect(
            lambda _i: self._push_overlay_settings())
        self._ov_fields.model().rowsMoved.connect(
            lambda *_a: self._push_overlay_settings())
        fields_row.addWidget(self._ov_fields, 1)
        move_col = QVBoxLayout()
        self._ov_field_up = QPushButton("▲")
        self._ov_field_up.setToolTip("Move the selected field up")
        self._ov_field_up.setFixedWidth(36)
        self._ov_field_up.clicked.connect(
            lambda: self._on_ov_field_move(-1))
        move_col.addWidget(self._ov_field_up)
        self._ov_field_down = QPushButton("▼")
        self._ov_field_down.setToolTip("Move the selected field down")
        self._ov_field_down.setFixedWidth(36)
        self._ov_field_down.clicked.connect(
            lambda: self._on_ov_field_move(+1))
        move_col.addWidget(self._ov_field_down)
        move_col.addStretch(1)
        fields_row.addLayout(move_col)
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
        # Reuse the window: building a fresh OverlayWindow on every show
        # leaked the old one — its poll timer kept running against the
        # DB twice a second — and reset its screen position. A closed
        # overlay is only ever hidden (see OverlayWindow.closeEvent),
        # never destroyed, so self._overlay stays usable across hides.
        if self._overlay is None:
            self._overlay = OverlayWindow(
                self._db,
                self._settings_store,
                lambda: self._display_session_id(),
                on_settings_changed=self._on_overlay_settings_changed,
            )
        if self._overlay.isVisible():
            self._overlay.hide()
            self.btn_overlay.setText("Show overlay")
        else:
            # Pick up anything changed while hidden (orientation,
            # opacity, field set) before showing.
            self._overlay.reload_settings()
            self._overlay.show()
            self.btn_overlay.setText("Hide overlay")
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
        self._set_overlay_locked(self.btn_lock.isChecked())

    def _set_overlay_locked(self, locked: bool) -> None:
        """Single funnel for every lock/unlock path (header chip, Overlay
        tab, overlay pill). The settings store is the one source of
        truth — written first, then re-read — so the two buttons can
        never disagree with each other or with the overlay. (They used
        to read different copies: the tab read the store, the header
        read in-memory state, and whichever refreshed last won.)"""
        s = self._settings_store.load()
        s.overlay_locked = locked
        self._settings_store.save(s)
        self._settings = s
        if self._overlay is None:
            self._overlay = OverlayWindow(
                self._db,
                self._settings_store,
                lambda: self._display_session_id(),
                on_settings_changed=self._on_overlay_settings_changed,
            )
        if not self._overlay.isVisible():
            self._overlay.reload_settings()
            self._overlay.show()
            self.btn_overlay.setText("Hide overlay")
        self._overlay.set_locked(locked)
        self._refresh_lock_button()
        self._refresh_overlay_tab()

    def _refresh_lock_button(self) -> None:
        locked = self._settings_store.load().overlay_locked
        self._settings.overlay_locked = locked
        buttons = [self.btn_lock]
        if hasattr(self, "_ov_lock_btn"):
            buttons.append(self._ov_lock_btn)
        for btn in buttons:
            btn.blockSignals(True)
            btn.setChecked(locked)
            btn.setText("Unlock" if locked else "Lock")
            # "Unlock" is wider than "Lock" — pin the chip to the wider
            # label so flipping never reflows the header or the form.
            need = max(btn.fontMetrics().horizontalAdvance(t)
                       for t in ("Lock", "Unlock")) + 30
            btn.setMinimumWidth(need)
            btn.blockSignals(False)

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
        if self._tabs.widget(idx) is self._tab_overlay:
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
        self._ov_window_opacity.blockSignals(True)
        self._ov_window_opacity.setValue(
            int(s.overlay_window_opacity * 100))
        self._ov_window_opacity.blockSignals(False)
        self._ov_window_opacity_val.setText(
            f"{int(s.overlay_window_opacity * 100)}%")
        self._ov_locked_window_opacity.blockSignals(True)
        self._ov_locked_window_opacity.setValue(
            int(s.overlay_locked_window_opacity * 100))
        self._ov_locked_window_opacity.blockSignals(False)
        self._ov_locked_window_opacity_val.setText(
            f"{int(s.overlay_locked_window_opacity * 100)}%")
        self._ov_scale.blockSignals(True)
        self._ov_scale.setValue(int(round(s.overlay_scale * 100)))
        self._ov_scale.blockSignals(False)
        self._ov_scale_val.setText(f"{int(round(s.overlay_scale * 100))}%")
        self._sync_ov_color_button(s.overlay_text_color)
        self._sync_ov_locked_color_button(s.overlay_locked_text_color)
        self._refresh_ov_fields(s)

    def _push_overlay_settings(self) -> None:
        """Read the Overlay tab's controls into the settings store and
        reload a visible overlay in place."""
        s = self._settings_store.load()
        s.overlay_orientation = (
            self._ov_orient.currentData() or "vertical")
        s.overlay_opacity = self._ov_opacity.value() / 100.0
        s.overlay_locked_opacity = self._ov_locked_opacity.value() / 100.0
        s.overlay_window_opacity = self._ov_window_opacity.value() / 100.0
        s.overlay_locked_window_opacity = (
            self._ov_locked_window_opacity.value() / 100.0)
        s.overlay_scale = self._ov_scale.value() / 100.0
        order = []
        for i in range(self._ov_fields.count()):
            item = self._ov_fields.item(i)
            key = item.data(Qt.UserRole)
            order.append(key)
            setattr(s, f"overlay_show_{key}",
                    item.checkState() == Qt.Checked)
        s.overlay_field_order = order
        self._settings_store.save(s)
        self._settings = s
        self._ov_opacity_val.setText(f"{self._ov_opacity.value()}%")
        self._ov_locked_opacity_val.setText(
            f"{self._ov_locked_opacity.value()}%")
        self._ov_window_opacity_val.setText(
            f"{self._ov_window_opacity.value()}%")
        self._ov_locked_window_opacity_val.setText(
            f"{self._ov_locked_window_opacity.value()}%")
        self._ov_scale_val.setText(f"{self._ov_scale.value()}%")
        if self._overlay is not None and self._overlay.isVisible():
            self._overlay.reload_settings()
        self._refresh_lock_button()

    @staticmethod
    def _paint_ov_swatch(btn: QPushButton, hex_color: str) -> None:
        """Show a color as hex on a swatch button."""
        if not QColor(hex_color).isValid():
            hex_color = theme.PARCH_BG
        name = QColor(hex_color).name()
        c = QColor(name)
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        fg = "#1a1424" if lum > 128 else "#e8dfc8"
        btn.setText(name)
        # A leaf button, not a container: a bare background here
        # affects only this button.
        btn.setStyleSheet(f"background: {name}; color: {fg};")

    def _sync_ov_color_button(self, hex_color: str) -> None:
        """Show the stats-text color as hex on a swatch button."""
        self._paint_ov_swatch(self._ov_color_btn, hex_color)

    def _sync_ov_locked_color_button(self, hex_color: str) -> None:
        """Show the locked-text color as hex on a swatch button."""
        self._paint_ov_swatch(self._ov_locked_color_btn, hex_color)

    def _on_pick_overlay_text_color(self) -> None:
        s = self._settings_store.load()
        picked = QColorDialog.getColor(QColor(s.overlay_text_color),
                                       self, "Overlay stats text color")
        if not picked.isValid():
            return
        s.overlay_text_color = picked.name()
        self._settings_store.save(s)
        self._settings = s
        self._sync_ov_color_button(s.overlay_text_color)
        if self._overlay is not None:
            self._overlay.reload_settings()

    def _on_pick_overlay_locked_text_color(self) -> None:
        s = self._settings_store.load()
        picked = QColorDialog.getColor(
            QColor(s.overlay_locked_text_color),
            self, "Overlay locked text color")
        if not picked.isValid():
            return
        s.overlay_locked_text_color = picked.name()
        self._settings_store.save(s)
        self._settings = s
        self._sync_ov_locked_color_button(s.overlay_locked_text_color)
        if self._overlay is not None:
            self._overlay.reload_settings()

    def _refresh_ov_fields(self, s) -> None:
        """Rebuild the field list when the order changed; always re-apply
        the checks. Rebuilding only on change keeps the selection (and
        avoids fighting a drag in progress) on plain check toggles."""
        from .overlay import OVERLAY_FIELDS
        labels = dict(OVERLAY_FIELDS)
        order = list(getattr(s, "overlay_field_order", None)
                     or [k for k, _ in OVERLAY_FIELDS])
        current = [self._ov_fields.item(i).data(Qt.UserRole)
                   for i in range(self._ov_fields.count())]
        if current != order:
            self._ov_fields.blockSignals(True)
            try:
                self._ov_fields.clear()
                for key in order:
                    item = QListWidgetItem(labels.get(key, key))
                    item.setData(Qt.UserRole, key)
                    item.setFlags(Qt.ItemIsEnabled
                                  | Qt.ItemIsSelectable
                                  | Qt.ItemIsUserCheckable
                                  | Qt.ItemIsDragEnabled)
                    self._ov_fields.addItem(item)
            finally:
                self._ov_fields.blockSignals(False)
        for i in range(self._ov_fields.count()):
            item = self._ov_fields.item(i)
            self._ov_fields.blockSignals(True)
            try:
                item.setCheckState(
                    Qt.Checked
                    if getattr(s, f"overlay_show_{item.data(Qt.UserRole)}",
                               True)
                    else Qt.Unchecked)
            finally:
                self._ov_fields.blockSignals(False)

    def _on_ov_field_move(self, delta: int) -> None:
        """Nudge the selected field up (-1) or down (+1) in the order."""
        row = self._ov_fields.currentRow()
        dest = row + delta
        if row < 0 or dest < 0 or dest >= self._ov_fields.count():
            return
        item = self._ov_fields.takeItem(row)
        self._ov_fields.insertItem(dest, item)
        self._ov_fields.setCurrentRow(dest)
        self._push_overlay_settings()

    def _on_ov_show_hide(self) -> None:
        self._toggle_overlay()
        self._refresh_overlay_tab()

    def _on_ov_lock_toggled(self) -> None:
        self._set_overlay_locked(self._ov_lock_btn.isChecked())

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
        sc_total = s["sc_picked"] + s["sc_unpicked"]
        self._set_status(
            f"Session #{sid}  ·  {s['my_kills']} yours / {s['kills']} session total kills"
            f"  ·  {s['sc_picked']:,} picked up / {sc_total:,} total SC"
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
            kills_item = _cell(_mine_total(r["my_kills"], r["kills"]),
                                align=Qt.AlignRight)
            kills_item.setToolTip(
                f"{r['my_kills']} yours / {r['kills']} session total")
            self.tbl_sessions.setItem(i, 3, kills_item)
            drops_item = _cell(_mine_total(r["my_drops"], r["drops"]),
                                align=Qt.AlignRight)
            drops_item.setToolTip(
                f"{r['my_drops']} yours / {r['drops']} session total")
            self.tbl_sessions.setItem(i, 4, drops_item)
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
            self.tbl_drops.setItem(i, 5, _cell(_drop_status(r, acct)))
        self._show_empty(self.tbl_drops, self._drops_empty, len(rows) == 0)

    def _open_session_detail(self, item: QTableWidgetItem) -> None:
        """Double-click ANYWHERE on a Sessions row opens that session's
        detail view. The session id rides on the row's first cell
        (UserRole), so resolve through the row — not the clicked cell,
        which only carries it in column 0."""
        if item is None:
            return
        first = self.tbl_sessions.item(item.row(), 0)
        sid = first.data(Qt.UserRole) if first is not None else None
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
            zone_item = _cell(z["display_name"]
                              + ("  ·  Mirage" if z.get("is_mirage") else ""))
            if z.get("is_mirage"):
                zone_item.setToolTip("Mirage run")
            self.tbl_zones.setItem(i, 0, zone_item)
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

    def _zone_metrics(self, sid: int) -> list[dict]:
        """Per-visit totals + minutes for a session, ready to chart.

        An still-open visit in the ACTIVE session measures against now
        so the current zone stays live; anywhere else an open visit
        contributes no time (its end is unknown). Repeat visits to the
        same zone are numbered first-entered-first ("Snowy Mountain
        #1", "Snowy Mountain #2") so the picker and the spider can
        tell them apart; a zone visited once keeps its bare name.
        Each entry also carries its visit window ("entered_at" /
        "left_at", effective ISO strings, left_at possibly None for a
        stale open visit) so charts can scope to the time spent in
        that visit."""
        now_iso = None
        if sid == self._consumer.session_id:
            now_iso = datetime.now().isoformat(timespec="milliseconds")
        out = []
        for z in self._db.zone_stats(sid):
            left = z["left_at"] or now_iso
            secs = _zone_seconds(z["entered_at"], left)
            minutes = (secs / 60.0) if secs else 0.0
            out.append({
                "display": z["display_name"],
                "map_name": z["map_name"],
                "entered_at": z["entered_at"],
                "left_at": left,
                "kills": z["kills"],
                "drops": z["drops"],
                "sc": z["soul_crystals"],
                "xp": z["xp"],
                "minutes": minutes,
                "kpm": (z["kills"] / minutes) if minutes > 0 else 0.0,
                "drops_m": (z["drops"] / minutes) if minutes > 0 else 0.0,
                "sc_m": (z["soul_crystals"] / minutes) if minutes > 0 else 0.0,
                "xp_m": (z["xp"] / minutes) if minutes > 0 else 0.0,
            })
        # zone_stats arrives ordered by entered_at, so enumeration
        # order IS first-entered-first.
        repeats = {}
        for m in out:
            repeats[m["map_name"]] = repeats.get(m["map_name"], 0) + 1
        seen: dict[str, int] = {}
        for m in out:
            if repeats[m["map_name"]] > 1:
                seen[m["map_name"]] = seen.get(m["map_name"], 0) + 1
                m["display"] = f"{m['display']} #{seen[m['map_name']]}"
        return out

    def _visit_window(self, m: dict) -> tuple[datetime, datetime] | None:
        """A zone entry's time window as datetimes, or None when the
        entry has no usable start. An open end measures against now."""
        try:
            start = datetime.fromisoformat(m["entered_at"])
        except (TypeError, ValueError):
            return None
        try:
            end = (datetime.fromisoformat(m["left_at"])
                   if m.get("left_at") else datetime.now())
        except (TypeError, ValueError):
            end = datetime.now()
        return (start, end)

    # Metric key -> cumulative event stream behind it. Rate metrics
    # chart their base quantity (Kills/min shares are kill shares);
    # minutes chart elapsed session time itself.
    _BASE_QUANTITY = {
        "kills": "kills", "kpm": "kills",
        "drops": "drops", "drops_m": "drops",
        "sc": "sc", "sc_m": "sc",
        "xp": "xp", "xp_m": "xp",
        "minutes": "minutes",
    }

    # Metric key -> per-bin value behind it. "minutes" is cumulative
    # elapsed time (per-bin minutes would be a flat line at the bin
    # width); everything else is gained-in-the-bin, with rate metrics
    # dividing by the bin width.
    _TIME_RATE = {"kpm", "drops_m", "sc_m", "xp_m"}

    def _time_series(self, sid: int, key: str,
                     window: tuple[datetime, datetime] | None) -> list[Point]:
        """The selected stat over time as per-bin points (bin count
        adapts to the span — see _bin_count).

        Unscoped, the span is the metric's own first-to-last event; a
        picked zone scopes both span and events to that visit's window
        ("over the time spent on that zone"). A single-moment span
        yields one bin; "minutes" charts cumulative elapsed time."""
        def _parse(ts: str):
            try:
                return datetime.fromisoformat(ts)
            except (TypeError, ValueError):
                return None

        kind = self._BASE_QUANTITY.get(key, "kills")
        if window is not None:
            start, end = window
            if end < start:
                return []
        else:
            start = end = None

        if kind == "minutes":
            if start is None:
                stamps = []
                for k in ("kills", "xp", "drops", "sc"):
                    stamps += [ts for ts, _d
                               in self._db.cumulative_events(sid, k)]
                moments = sorted({_parse(ts) for ts in stamps} - {None})
                if len(moments) < 2:
                    return []
                start, end = moments[0], moments[-1]
            span_s = (end - start).total_seconds()
            if span_s <= 0:
                return []
            n = _bin_count(span_s)
            return [Point(
                label=_tick_label(start + (end - start) * b / n, span_s),
                value=(start + (end - start) * (b + 1) / n
                       - start).total_seconds() / 60.0,
            ) for b in range(n)]

        events = []
        for ts, delta in self._db.cumulative_events(sid, kind):
            t = _parse(ts)
            if t is None:
                continue
            if window is not None and not (start <= t <= end):
                continue
            events.append((t, delta))
        if not events:
            return []
        if start is None:
            events.sort(key=lambda e: e[0])
            start, end = events[0][0], events[-1][0]
        span_s = (end - start).total_seconds()
        if span_s <= 0:
            # Every event landed on the same instant: one bin holding
            # the total (a rate over zero time is meaningless, so rate
            # metrics show the raw count here rather than a 60x spike).
            total = sum(d for _t, d in events)
            return [Point(label=_tick_label(start, 0.0), value=total)]
        n = _bin_count(span_s)
        bins = [0.0] * n
        for t, delta in events:
            i = min(int((t - start).total_seconds() / span_s * n), n - 1)
            bins[i] += delta
        width_min = span_s / n / 60.0
        out = []
        for b in range(n):
            bs = start + (end - start) * b / n
            v = bins[b] / width_min if key in self._TIME_RATE else bins[b]
            out.append(Point(label=_tick_label(bs, span_s), value=v))
        return out

    _SPIDER_AXES = ("Kills", "Drops", "SC", "XP", "Minutes")

    def _sync_zone_combo(self, metrics: list[dict]) -> None:
        """Rebuild the zone picker for the current session, keeping the
        selection when the same zone is still there."""
        cur_text = self.cmb_zone.currentText()
        self.cmb_zone.blockSignals(True)
        self.cmb_zone.clear()
        self.cmb_zone.addItem("All zones", None)
        for i, m in enumerate(metrics):
            self.cmb_zone.addItem(m["display"], i)
        idx = self.cmb_zone.findText(cur_text)
        self.cmb_zone.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_zone.blockSignals(False)

    @staticmethod
    def _cumulative_points(points: list[Point]) -> list[Point]:
        """Running totals over per-bin points — the session's climb.

        Each point keeps its bin label and gains a "+n" sublabel, so
        the tooltip reads both the total and what the bin added."""
        out = []
        run = 0.0
        for pt in points:
            run += pt.value
            out.append(Point(label=pt.label, value=run,
                             sublabel=f"+{pt.value:g}",
                             fmt=pt.fmt, color=pt.color))
        return out

    def _zone_share(self, sid: int, base: str) -> list[Point]:
        """Whole-session totals per zone for the share donut.

        Zero-total zones are dropped — they would paint no slice and
        only crowd the legend. An all-zero session yields [], which the
        chart renders as "No data"."""
        return [Point(label=m["display"], value=m.get(base, 0.0))
                for m in self._zone_metrics(sid)
                if m.get(base, 0.0) > 0]

    def _refresh_graphs(self) -> None:
        """The selected stat, charted in the selected style and scope.

        Line/cumulative/bars chart the stat over time (per-bin gains,
        or cumulative elapsed time for "minutes"; cumulative keeps a
        running total so the climb reads directly). A picked zone
        scopes the span and the events to that visit's window — "over
        the time spent on that zone". Share and spider are the
        exceptions: both always cover every zone (share splits the
        session total, spider compares zones on one radar web) and
        spider dims all but the picked zone (if any)."""
        sid = self.cmb_sessions.currentData()
        if sid is None:
            self.chart_main.clear()
            self.chart_spider.clear()
            self._graphs_totals.setText("")
            return
        sid = int(sid)
        key = self.cmb_metric.currentData() or "kills"
        mode = self.cmb_chart_type.currentData() or "line"
        fmt = next((f for k, _l, f in self._GRAPH_METRICS if k == key),
                   "{:.0f}")
        metrics = self._zone_metrics(int(sid))
        self._sync_zone_combo(metrics)
        zone_idx = self.cmb_zone.currentData()  # None == all zones
        window = (self._visit_window(metrics[zone_idx])
                  if zone_idx is not None else None)
        scope = (metrics[zone_idx]["display"]
                 if zone_idx is not None else "Whole session")

        if mode == "share":
            # Whole-session by construction — the totals strip below
            # sums the session instead of echoing a stale zone pick.
            zone_idx = None
            window = None
            scope = "Whole session"
        self.chart_main.setVisible(mode in ("line", "cumulative",
                                            "bars", "share"))
        self.chart_spider.setVisible(mode == "spider")
        # Share is whole-session by construction — a zone-scoped share
        # would be one 100% slice — so the picker steps aside for it.
        self.cmb_zone.setEnabled(mode != "share")

        if mode == "share":
            self._graphs_section_title.setText(
                f"{key.upper()}  SHARE  BY  ZONE")
            self.chart_main.set_mode("share")
            self.chart_main.set_value_format(fmt)
            self.chart_main.set_points(
                self._zone_share(sid,
                                 self._BASE_QUANTITY.get(key, "kills")))
        elif mode == "spider":
            self._graphs_section_title.setText("ZONES  COMPARED  (SPIDER)")
            biggest = sorted(range(len(metrics)),
                             key=lambda i: (metrics[i]["kills"],
                                            metrics[i]["xp"]),
                             reverse=True)[:8]
            self.chart_spider.set_axes(list(self._SPIDER_AXES))
            self.chart_spider.set_value_format("{:.0f}")
            self.chart_spider.set_series([
                SpiderSeries(
                    label=metrics[i]["display"],
                    values=[metrics[i]["kills"], metrics[i]["drops"],
                            metrics[i]["sc"], metrics[i]["xp"],
                            metrics[i]["minutes"]],
                    color=QColor(SPIDER_COLORS[j % len(SPIDER_COLORS)]),
                    dimmed=(zone_idx is not None and i != zone_idx),
                )
                for j, i in enumerate(biggest)
            ])
        elif mode in ("line", "cumulative", "bars"):
            if mode == "cumulative" and key != "minutes":
                self._graphs_section_title.setText(
                    f"{scope.upper()}  OVER  TIME  (RUNNING TOTAL)")
            else:
                self._graphs_section_title.setText(
                    f"{scope.upper()}  OVER  TIME")
            # "minutes" is already an elapsed-time curve, so cumulative
            # shows it as-is; everything else accumulates per-bin gains.
            self.chart_main.set_mode("line" if mode == "cumulative"
                                     else mode)
            self.chart_main.set_value_format(fmt)
            pts = self._time_series(sid, key, window)
            if mode == "cumulative" and key != "minutes":
                pts = self._cumulative_points(pts)
            self.chart_main.set_points(pts)

        if zone_idx is not None:
            m = metrics[zone_idx]
            self._graphs_totals.setText(
                f"{m['display']}: {m['kills']} kills  ·  {m['sc']} SC  ·  "
                f"{m['xp']:,} XP  ·  {m['drops']} drops  ·  "
                f"{m['minutes']:.1f} min"
            )
            return
        tot_k = sum(m["kills"] for m in metrics)
        tot_d = sum(m["drops"] for m in metrics)
        tot_s = sum(m["sc"] for m in metrics)
        tot_x = sum(m["xp"] for m in metrics)
        tot_m = sum(m["minutes"] for m in metrics)
        kpm = f"{(tot_k / tot_m):.2f}" if tot_m > 0 else "—"
        self._graphs_totals.setText(
            f"{tot_k} kills  ·  {tot_s} SC  ·  {tot_x:,} XP  ·  "
            f"{tot_d} drops  ·  {kpm} KPM  ·  {_fmt_duration(tot_m * 60)}"
        )

    def _open_selected_session_detail(self) -> None:
        """Details… button next to the Graphs session selector."""
        sid = self.cmb_sessions.currentData()
        if sid is None:
            return
        dlg = _SessionDetailDialog(self._db, self._names, int(sid), self)
        dlg.exec()

    def _refresh_current_tab(self) -> None:
        """Refresh whichever of Sessions/Kills/Drops/Zones/Graphs is on screen right
        now, so switching tabs — or just leaving one open — doesn't show
        data that's a click behind reality. Matches on widget identity:
        hardcoded indices would silently refresh the wrong tab in the
        frozen exe, where the dev-only Debug tab doesn't exist."""
        cur = self._tabs.currentWidget()
        if cur is self._tab_sessions:
            self._refresh_sessions()
        elif cur is self._tab_kills:
            self._refresh_kills()
        elif cur is self._tab_drops:
            self._refresh_drops()
        elif cur is self._tab_zones:
            self._refresh_zones()
        elif cur is self._tab_graphs:
            self._refresh_graphs()
        # Overlay tab refreshes on switch/change, never on tick; the
        # debug tab is its own thing.

    def _on_event_seen(self, raw: str) -> None:
        """Slot for the consumer's `event_seen` signal. Routes the raw
        event to the debug console (if present) and, in any case, kicks
        a refresh of the visible table so a kill or drop appears the
        moment it's recorded, not on the next 1-second tick.
        """
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


def _drop_status(r: dict, local_account: int | None) -> str:
    """Lifecycle state of a drop row: who picked it up, or whether it
    vanished unclaimed (expiry/destroy) — else still on the ground."""
    picker = r.get("picked_up_by")
    if picker is not None:
        if local_account is not None and picker == local_account:
            return "Picked up (you)"
        return f"Picked up (#{picker})"
    if r.get("destroyed"):
        return "Gone"
    return "On ground"


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


def _bin_count(span_s: float, cap: int = 24) -> int:
    """Bin count that adapts to the span: ~15s bins, at least 6
    points so a 5-minute session charts per-minute detail instead
    of one or two dots, at most `cap` so long sessions stay
    readable. Zero span collapses to a single bin."""
    if span_s <= 0:
        return 1
    return max(6, min(cap, round(span_s / 15.0)))


def _tick_label(moment: datetime, span_s: float) -> str:
    """Bin tag with seconds on short spans (where HH:MM tags would
    all read the same) and plain HH:MM otherwise."""
    return moment.strftime("%H:%M:%S" if span_s < 600 else "%H:%M")


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


def _mini_title(text: str) -> QLabel:
    """Small tracked section caption for control rows (SESSION, CHART)."""
    lbl = QLabel(text)
    lbl.setFont(QFont("Georgia", 9))
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
        # The old dialog laid every section straight on the window with
        # a minimum size smaller than the content — long sessions
        # overflowed, widgets overlapped, and Close could end up
        # off-screen. All content now lives in a scroll area; Close
        # stays pinned outside it so it can never be cut off.
        self.resize(820, 700)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
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
                    QLabel(f"{s['my_kills']} yours / {s['kills']} session total"))
        form.addRow("Drops:",
                    QLabel(f"{s['my_drops']} yours / {s['drops']} session total"))
        form.addRow("Soul crystals:",
                    QLabel(f"{s['sc_picked']:,} picked up / "
                           f"{s['sc_picked'] + s['sc_unpicked']:,} total"))
        form.addRow("Experience:",
                    QLabel(f"{s['xp']:,} session total  (level {s['level']})"))
        if s.get("deaths"):
            form.addRow("Deaths:",
                        QLabel(f"{s['deaths']} deaths  ·  lost {s['xp_lost']:,} XP"
                               + (f", {s['items_lost']} items"
                                  if s.get("items_lost") else "")))
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
        zones.horizontalHeaderItem(4).setToolTip("Yours / session total kills")
        zones.horizontalHeaderItem(5).setToolTip("Yours / session total drops")
        zones.horizontalHeaderItem(6).setToolTip("Session total experience")
        for z in db.zone_stats(session_id):
            i = zones.rowCount()
            zones.insertRow(i)
            dz_item = _cell(z["display_name"]
                            + ("  ·  Mirage" if z.get("is_mirage") else ""))
            if z.get("is_mirage"):
                dz_item.setToolTip("Mirage run")
            zones.setItem(i, 0, dz_item)
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
        # Capped: a long zone list scrolls inside the table instead of
        # stretching the dialog past the screen.
        zones.setMinimumHeight(140)
        zones.setMaximumHeight(300)
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
        kills.setMaximumHeight(300)
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
            5, ["TIME", "ITEM", "QTY", "OWNER", "STATUS"],
            [Qt.AlignLeft, Qt.AlignLeft, Qt.AlignRight, Qt.AlignLeft,
             Qt.AlignLeft])
        self._drops_table.setMinimumHeight(140)
        self._drops_table.setMaximumHeight(300)
        layout.addWidget(self._drops_table)
        self._drops_rows = db.recent_drops(session_id, 200, names=names,
                                           local_account=acct)
        self._drops_acct = acct
        self._populate_drops()

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

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
            t.setItem(i, 4, _cell(
                _drop_status(r, self._drops_acct)))