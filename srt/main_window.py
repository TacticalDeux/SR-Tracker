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

from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMessageBox, QPushButton, QStatusBar,
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
        self._kills._num.setText(str(s["kills"]))
        self._sc._num.setText(str(s["soul_crystals"]))
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

        self._refresh_lock_button()
        # Populate right away rather than leaving tables blank until the
        # first tick or a manual "Re-read".
        self._refresh_sessions()
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
        self.tbl_sessions.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_sessions.verticalHeader().setVisible(False)
        self.tbl_sessions.setShowGrid(False)
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

        self.tbl_kills = QTableWidget(0, 3)
        self.tbl_kills.setHorizontalHeaderLabels(["TIME", "ENEMY ID", "MOB"])
        self.tbl_kills.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_kills.verticalHeader().setVisible(False)
        self.tbl_kills.setShowGrid(False)
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
        self.tbl_drops.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_drops.verticalHeader().setVisible(False)
        self.tbl_drops.setShowGrid(False)
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

        self.tbl_zones = QTableWidget(0, 7)
        self.tbl_zones.setHorizontalHeaderLabels(
            ["ZONE", "TIME", "KILLS", "SC", "DROPS", "XP", "KPM"]
        )
        self.tbl_zones.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_zones.verticalHeader().setVisible(False)
        self.tbl_zones.setShowGrid(False)
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
        from PySide6.QtWidgets import QComboBox
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

        # Three charts stacked: KPM, drops/m, SC/m
        charts_label = QLabel("RATES")
        charts_label.setFont(QFont("Georgia", 9))
        charts_label.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        outer.addWidget(charts_label)

        self.chart_kpm = RateBarChart()
        self.chart_drops_m = RateBarChart()
        self.chart_sc_m = RateBarChart()
        for c in (self.chart_kpm, self.chart_drops_m, self.chart_sc_m):
            c.setMinimumHeight(140)
            outer.addWidget(c, 1)

        # Per-zone breakdown in selected session
        zone_label = QLabel("PER-ZONE  IN  THIS  SESSION")
        zone_label.setFont(QFont("Georgia", 9))
        zone_label.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        outer.addWidget(zone_label)
        self.chart_zones = BarChart(bar_height=22, value_format="{:.1f}")
        self.chart_zones.setMinimumHeight(180)
        outer.addWidget(self.chart_zones, 1)

        self._tabs.addTab(tab, "Graphs")

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
                lambda: self._consumer.session_id,
                on_settings_changed=self._on_overlay_settings_changed,
            )
            self._overlay.show()
            self.btn_overlay.setText("Hide overlay")
        else:
            self._overlay.hide()
            self.btn_overlay.setText("Show overlay")

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

    def _on_overlay_settings_changed(self, settings: Settings) -> None:
        self._settings = settings
        self._refresh_lock_button()

    # ------------------------------------------------------------------
    # Refreshes
    # ------------------------------------------------------------------
    def _refresh_summary(self) -> None:
        sid = self._consumer.session_id
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
            f"Session #{sid}  ·  {s['kills']} kills  ·  {s['soul_crystals']} SC"
        )

    def _refresh_sessions(self) -> None:
        rows = self._db.sessions()
        self.tbl_sessions.setRowCount(len(rows))
        for i, r in enumerate(rows):
            # The "I" column is a roman numeral, the way a journal page would
            # mark a session entry.
            self.tbl_sessions.setItem(i, 0, _cell(f"{(i+1):02d}", align=Qt.AlignCenter))
            self.tbl_sessions.setItem(i, 1, _cell(r["started"]))
            self.tbl_sessions.setItem(i, 2, _cell(r["ended"] or "—"))
            self.tbl_sessions.setItem(i, 3, _cell(str(r["kills"]), align=Qt.AlignRight))
            self.tbl_sessions.setItem(i, 4, _cell(str(r["drops"]), align=Qt.AlignRight))
        self._show_empty(self.tbl_sessions, self._sessions_empty, len(rows) == 0)

    def _refresh_kills(self) -> None:
        sid = self._consumer.session_id
        if sid is None:
            # No active session — don't leave a previous session's rows
            # sitting there looking current.
            self.tbl_kills.setRowCount(0)
            self._show_empty(self.tbl_kills, self._kills_empty, True)
            return
        rows = self._db.recent_kills(sid, 200, names=self._names)
        self.tbl_kills.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.tbl_kills.setItem(i, 0, _cell(r["ts"]))
            self.tbl_kills.setItem(i, 1, _cell(str(r["enemy_id"]), align=Qt.AlignRight))
            self.tbl_kills.setItem(i, 2, _cell(r["name"]))
        self._show_empty(self.tbl_kills, self._kills_empty, len(rows) == 0)

    def _refresh_drops(self) -> None:
        sid = self._consumer.session_id
        if sid is None:
            self.tbl_drops.setRowCount(0)
            self._show_empty(self.tbl_drops, self._drops_empty, True)
            return
        rows = self._db.recent_drops(sid, 200, names=self._names)
        self.tbl_drops.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.tbl_drops.setItem(i, 0, _cell(r["ts"]))
            self.tbl_drops.setItem(i, 1, _cell(f"#{r['drop_id']}", align=Qt.AlignRight))
            item_id = r["item_id"]
            if item_id == 0:
                label = "Soul Crystal"
            elif item_name := r.get("item_name"):
                # Lookup found a real name (e.g. "Red Crystal"). Prefer it.
                label = item_name
            elif item_id is None:
                label = "—"
            else:
                label = f"Item #{item_id}"
            self.tbl_drops.setItem(i, 2, _cell(label))
            self.tbl_drops.setItem(i, 3, _cell(str(r["amount"] or ""), align=Qt.AlignRight))
            self.tbl_drops.setItem(i, 4, _cell(str(r["belongs_to"] or "")))
        self._show_empty(self.tbl_drops, self._drops_empty, len(rows) == 0)

    @staticmethod
    def _show_empty(table: QTableWidget, note: QLabel, empty: bool) -> None:
        """Swap between the table and its empty-state note, the same
        way the summary tab swaps its whole right column."""
        note.setVisible(empty)
        table.setVisible(not empty)

    def _refresh_zones(self) -> None:
        """Per-zone stats for the active session, or the latest session if none."""
        sid = self._consumer.session_id
        if sid is None:
            past = self._db.past_sessions(1)
            if past:
                sid = past[0]["id"]
        if sid is None:
            self.tbl_zones.setRowCount(0)
            self._show_empty(self.tbl_zones, self._zones_empty, True)
            return
        rows = self._db.zone_stats(sid)
        self.tbl_zones.setRowCount(len(rows))
        for i, z in enumerate(rows):
            # Time spent in zone
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(z["entered_at"])
                t1 = datetime.fromisoformat(z["left_at"]) if z["left_at"] else None
            except Exception:
                t0 = t1 = None
            if t0 and t1:
                secs = (t1 - t0).total_seconds()
                if secs < 60:
                    time_str = f"{int(secs)}s"
                elif secs < 3600:
                    time_str = f"{int(secs/60)}m{int(secs%60)}s"
                else:
                    time_str = f"{int(secs/3600)}h{int((secs%3600)/60)}m"
            else:
                time_str = "—"
            # KPM
            kpm = (z["kills"] / (secs/60)) if (t0 and t1 and secs > 0) else 0.0
            self.tbl_zones.setItem(i, 0, _cell(z["display_name"]))
            self.tbl_zones.setItem(i, 1, _cell(time_str, align=Qt.AlignRight))
            self.tbl_zones.setItem(i, 2, _cell(str(z["kills"]), align=Qt.AlignRight))
            self.tbl_zones.setItem(i, 3, _cell(str(z["soul_crystals"]), align=Qt.AlignRight))
            self.tbl_zones.setItem(i, 4, _cell(str(z["drops"]), align=Qt.AlignRight))
            self.tbl_zones.setItem(i, 5, _cell(f"{z['xp']:,}", align=Qt.AlignRight))
            self.tbl_zones.setItem(i, 6, _cell(f"{kpm:.2f}", align=Qt.AlignRight))
        self._show_empty(self.tbl_zones, self._zones_empty, len(rows) == 0)

    def _refresh_sessions_combo(self) -> None:
        """Populate the Graphs tab's session selector dropdown."""
        cur_text = self.cmb_sessions.currentText()
        self.cmb_sessions.blockSignals(True)
        self.cmb_sessions.clear()
        for s in self._db.past_sessions(50):
            label = f"#{s['id']}  {s['started']}"
            if s.get("current_zone"):
                label += f"  -  {s['current_zone']}"
            self.cmb_sessions.addItem(label, s["id"])
        # Restore previous selection if still there
        idx = self.cmb_sessions.findText(cur_text)
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
            sc_bars.append(Bar(label=label, value=sc_m, sublabel=f"{z['soul_crystals']} SC"))
        self.chart_kpm.set_bars(kpm_bars)
        self.chart_drops_m.set_bars(drops_bars)
        self.chart_sc_m.set_bars(sc_bars)
        # Per-zone totals bar chart
        totals_bars = [Bar(label=z["display_name"], value=z["kills"], sublabel=f"{z['soul_crystals']} SC")
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
        elif idx == 6 and hasattr(self, "_debug"):
            pass  # debug tab is its own thing

    def _on_event_seen(self, raw: str) -> None:
        """Slot for the consumer's `event_seen` signal. Routes the raw
        event to the debug console (if present) and, in any case, kicks
        a refresh of the visible table so a kill or drop appears the
        moment it's recorded, not on the next 1-second tick."""
        if hasattr(self, "_debug") and self._debug is not None:
            self._debug.append_event(raw)
        # Refresh whichever table is on screen so the user sees the
        # change immediately, without waiting for the timer.
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
        self._lbl_counters.setText(
            f"events: {seen}  parsed: {parsed}  dropped: {dropped}"
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