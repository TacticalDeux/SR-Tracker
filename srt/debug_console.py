"""The dev-mode debug console.

A read-only text view that streams events as the consumer sees them. Only
mounted in the main window when running from source — frozen builds
don't include it, so the dev signal stays out of the distributed exe.

The console shows the raw JSON for each event (one line per event) so
you can see exactly what the DLL is producing, plus a short running
counter for kills/drops/xp parsed in real time.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from . import theme


_MAX_LINES = 5000  # cap the log so the text widget stays responsive


class DebugConsole(QWidget):
    """A streaming log view + per-event-type counter row."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {theme.INK_0};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        # Header row
        header_row = QHBoxLayout()
        title = QLabel("DEBUG  CONSOLE")
        title.setFont(QFont("Georgia", 11))
        title.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 3px; font-weight: bold;"
        )
        header_row.addWidget(title)
        header_row.addStretch(1)

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.clicked.connect(self._on_clear)
        header_row.addWidget(self._btn_clear)
        self._btn_pause = QPushButton("Pause")
        self._btn_pause.setCheckable(True)
        self._btn_pause.toggled.connect(self._on_pause)
        header_row.addWidget(self._btn_pause)

        layout.addLayout(header_row)

        # Hairline
        rule = _hairline()
        layout.addWidget(rule)

        # Counter row
        self._counters: dict[str, QLabel] = {}
        counter_row = QHBoxLayout()
        counter_row.setSpacing(24)
        for key in ("enemy_death", "drop_creation", "exp_update",
                    "level_up", "zone_change", "spawn_notification"):
            self._counters[key] = self._make_counter(counter_row, key)
        counter_row.addStretch(1)
        layout.addLayout(counter_row)

        # Log view
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(QFont("Consolas", 9))
        self._text.setStyleSheet(
            f"QTextEdit {{ background: {theme.INK_1}; color: {theme.PARCH_BG};"
            f" border: 1px solid {theme.INK_BORDER}; }}"
        )
        layout.addWidget(self._text, 1)

        # Bookkeeping
        self._paused = False
        self._pending_lines: list[str] = []
        self._counts: Counter = Counter()

    # ------------------------------------------------------------------
    def append_event(self, raw: str) -> None:
        """Called by the consumer for every event seen."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            line = f"[bad-json] {raw[:200]}"
        else:
            etype = data.get("type", "unknown")
            self._counts[etype] += 1
            self._refresh_counters()
            line = f"{_ts()}  {raw}"
        if self._paused:
            self._pending_lines.append(line)
            return
        self._write(line)

    def append_consumer_status(self, msg: str) -> None:
        """For non-event messages (consumer start/stop, errors)."""
        line = f"{_ts()}  [consumer] {msg}"
        if self._paused:
            self._pending_lines.append(line)
            return
        self._write(line)

    # ------------------------------------------------------------------
    def _write(self, line: str) -> None:
        self._text.append(line)
        # Trim the buffer so it doesn't grow without bound.
        doc = self._text.document()
        if doc.blockCount() > _MAX_LINES:
            cursor = self._text.textCursor()
            # Fully scoped enums: Start/Down are MoveOperation,
            # KeepAnchor is MoveMode. Unscoped access (cursor.Start)
            # raises AttributeError on current PySide6.
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(QTextCursor.MoveOperation.Down,
                                QTextCursor.MoveMode.KeepAnchor,
                                doc.blockCount() - _MAX_LINES)
            cursor.removeSelectedText()
            cursor.deleteChar()

    def _refresh_counters(self) -> None:
        # The value label shows just the number; the title label above
        # it already names the event type. Showing both here duplicated
        # the name ("ENEMY DEATH / ENEMY DEATH - 0") and the long text
        # overflowed the box once events started flowing.
        for etype, lbl in self._counters.items():
            lbl.setText(str(self._counts.get(etype, 0)))

    def _make_counter(self, parent_layout: QHBoxLayout, key: str) -> QLabel:
        wrap = QVBoxLayout()
        wrap.setSpacing(2)
        title = QLabel(key.upper().replace("_", " "))
        title.setFont(QFont("Georgia", 9))
        title.setStyleSheet(
            f"color: {theme.ASH_BRIGHT}; letter-spacing: 2px; font-weight: bold;"
        )
        wrap.addWidget(title)
        value = QLabel("0")
        value.setFont(QFont("Consolas", 14, QFont.Bold))
        value.setStyleSheet(f"color: {theme.CRYSTAL_LIGHT};")
        wrap.addWidget(value)
        box = QWidget()
        box.setLayout(wrap)
        parent_layout.addWidget(box, 0)
        return value

    def _on_clear(self) -> None:
        self._text.clear()
        self._pending_lines.clear()
        self._counts.clear()
        self._refresh_counters()

    def _on_pause(self, checked: bool) -> None:
        self._paused = checked
        self._btn_pause.setText("Resume" if checked else "Pause")
        if not checked:
            # Flush anything that arrived while paused.
            pending = self._pending_lines
            self._pending_lines = []
            for line in pending:
                self._write(line)


def _hairline() -> QWidget:
    from PySide6.QtWidgets import QFrame
    f = QFrame()
    f.setFrameShape(QFrame.NoFrame)
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {theme.RUNE_FAINT};")
    return f


def _ts() -> str:
    return time.strftime("%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"


def is_dev_mode() -> bool:
    """True when running from source (not the PyInstaller bundle).

    The debug console is only mounted in dev mode so the running exe
    has no dev hooks visible to the user.
    """
    return not getattr(__import__("sys"), "frozen", False)
