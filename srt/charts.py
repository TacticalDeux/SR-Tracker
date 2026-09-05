"""Lightweight chart widgets for the SR Tracker main window.

Avoids the QtCharts dependency by drawing directly on a QWidget using
QPainter. Each chart is a self-contained panel that takes a list of
(label, value) pairs and renders them as a bar chart.

Three chart types are provided:
  - BarChart:     horizontal bars
  - RateBarChart: vertical bars with a rate-per-minute label
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import theme


@dataclass
class Bar:
    label: str
    value: float
    sublabel: str = ""  # small text below the value (e.g. "12 SC / 5m")
    color: QColor | None = None  # overrides the default


class BarChart(QWidget):
    """Horizontal bar chart with labels on the left and values on the right."""

    def __init__(self, parent: QWidget | None = None, *, bar_height: int = 28,
                 value_format: str = "{:.0f}"):
        super().__init__(parent)
        self._bars: list[Bar] = []
        self._bar_height = bar_height
        self._value_format = value_format
        self.setMinimumHeight(80)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        # Auto-size: a few rows
        self.setMinimumHeight(bar_height * 3 + 30)

    def set_bars(self, bars: list[Bar]) -> None:
        self._bars = list(bars)
        # Update minimum height to fit bars
        self.setMinimumHeight(max(80, self._bar_height * len(bars) + 30))
        self.update()

    def clear(self) -> None:
        self._bars.clear()
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.INK_0))

        if not self._bars:
            p.setPen(QColor(theme.ASH))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(self.rect(), Qt.AlignCenter, "No data")
            return

        max_value = max((b.value for b in self._bars), default=1.0)
        if max_value <= 0:
            max_value = 1.0

        # Layout
        margin = 12
        label_w = 180
        sub_w = 140
        bar_x = margin + label_w + 10
        bar_w = self.width() - bar_x - sub_w - margin
        if bar_w < 50: bar_w = 50

        # Fonts
        label_font = QFont("Segoe UI", 10, QFont.Bold)
        value_font = QFont("Consolas", 10, QFont.Bold)
        sub_font = QFont("Segoe UI", 8)

        for i, bar in enumerate(self._bars):
            y = margin + i * self._bar_height
            rect = QRectF(margin, y, self.width() - 2 * margin, self._bar_height - 4)

            # Label
            p.setPen(QColor(theme.PARCH_BG))
            p.setFont(label_font)
            label_rect = QRectF(margin, y, label_w, self._bar_height - 4)
            p.drawText(label_rect, Qt.AlignVCenter | Qt.AlignLeft, bar.label)

            # Bar
            bar_color = bar.color or QColor(theme.CRYSTAL)
            bar_len = int((bar.value / max_value) * bar_w)
            p.fillRect(QRectF(bar_x, y + 6, bar_len, self._bar_height - 16), bar_color)

            # Sublabel (right of bar)
            if bar.sublabel:
                p.setPen(QColor(theme.ASH_BRIGHT))
                p.setFont(sub_font)
                sub_rect = QRectF(self.width() - sub_w - margin, y, sub_w, self._bar_height - 4)
                p.drawText(sub_rect, Qt.AlignVCenter | Qt.AlignRight, bar.sublabel)

            # Value (on the bar, white)
            p.setPen(QColor(theme.INK_0))
            p.setFont(value_font)
            value_str = self._value_format.format(bar.value)
            val_rect = QRectF(bar_x, y + 4, max(bar_len, 60), self._bar_height - 8)
            p.drawText(val_rect, Qt.AlignVCenter | Qt.AlignLeft, value_str)

        p.end()


class RateBarChart(BarChart):
    """Like BarChart but values are interpreted as rates (per minute).

    The bar is scaled to the maximum rate; the right-aligned number is
    the rate (e.g. "1.4/min")."""

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.INK_0))

        if not self._bars:
            p.setPen(QColor(theme.ASH))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(self.rect(), Qt.AlignCenter, "No data")
            return

        max_value = max((b.value for b in self._bars), default=1.0)
        if max_value <= 0:
            max_value = 1.0

        margin = 12
        label_w = 180
        sub_w = 100
        bar_x = margin + label_w + 10
        bar_w = self.width() - bar_x - sub_w - margin
        if bar_w < 50: bar_w = 50

        label_font = QFont("Segoe UI", 10, QFont.Bold)
        value_font = QFont("Consolas", 10, QFont.Bold)
        sub_font = QFont("Segoe UI", 8)

        for i, bar in enumerate(self._bars):
            y = margin + i * self._bar_height

            # Label
            p.setPen(QColor(theme.PARCH_BG))
            p.setFont(label_font)
            p.drawText(QRectF(margin, y, label_w, self._bar_height - 4), Qt.AlignVCenter | Qt.AlignLeft, bar.label)

            # Bar
            bar_color = bar.color or QColor(theme.CRYSTAL)
            bar_len = int((bar.value / max_value) * bar_w)
            p.fillRect(QRectF(bar_x, y + 6, bar_len, self._bar_height - 16), bar_color)

            # Rate label
            p.setPen(QColor(theme.PARCH_BG))
            p.setFont(value_font)
            sub_rect = QRectF(self.width() - sub_w - margin, y, sub_w, self._bar_height - 4)
            rate_str = f"{bar.value:.1f}/min" if bar.value < 100 else f"{bar.value:.0f}/min"
            p.drawText(sub_rect, Qt.AlignVCenter | Qt.AlignRight, rate_str)

        p.end()
