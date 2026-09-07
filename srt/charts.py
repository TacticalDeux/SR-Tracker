"""Lightweight chart widgets for the SR Tracker main window.

Avoids the QtCharts dependency by drawing directly on a QWidget using
QPainter. Each chart is a self-contained panel.

Chart types:
  - BarChart:     horizontal bars (legacy fixed-metric use)
  - RateBarChart: vertical bars with a rate-per-minute label (legacy)
  - SeriesChart:  one configurable chart behind the Graphs tab's metric
                  + chart-type selectors. Renders the same labeled
                  points as "bars" (per-bin values), "line" (shape over
                  time, zoomable), or "share" (per-zone donut).
  - SpiderChart:  radar web comparing zones across normalized stats
                  (kills, drops, crystals, XP, minutes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen,
)
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
            p.end()
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


@dataclass
class Point:
    label: str
    value: float
    sublabel: str = ""  # small text under the value (e.g. "12 kills")
    color: QColor | None = None  # overrides the default
    fmt: str = ""  # per-point value format; falls back to the chart's


class _ZoomState:
    """Fractional view window for the zoomable charts.

    The full data range is [0, 1]; zooming shrinks the window around the
    cursor, panning slides it. Charts render only the indices inside the
    window and rescale their value axis to the visible slice, so zooming
    into a flat stretch reveals its detail instead of stretching it.
    """

    MIN_POINTS = 2  # never zoom past a two-point window

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.lo, self.hi = 0.0, 1.0

    @property
    def is_full(self) -> bool:
        return self.lo <= 0.0 and self.hi >= 1.0

    def zoom(self, cursor: float, factor: float, n: int) -> bool:
        """Shrink (factor < 1) or grow the window around cursor (0..1).

        Returns True if the window changed."""
        if n < 3:
            return False
        cursor = min(max(cursor, 0.0), 1.0)
        span = self.hi - self.lo
        if span <= 0:
            span = 1.0
        min_span = (self.MIN_POINTS - 1) / (n - 1)
        new_span = min(1.0, max(min_span, span * factor))
        if new_span >= 1.0:
            changed = not self.is_full
            self.reset()
            return changed
        lo = cursor - (cursor - self.lo) / span * new_span
        lo = min(max(lo, 0.0), 1.0 - new_span)
        changed = (lo, lo + new_span) != (self.lo, self.hi)
        self.lo, self.hi = lo, lo + new_span
        return changed

    def pan(self, delta: float) -> bool:
        """Slide the window by delta (fraction of the full range)."""
        span = self.hi - self.lo
        if span >= 1.0:
            return False
        lo = min(max(self.lo + delta, 0.0), 1.0 - span)
        if lo == self.lo:
            return False
        self.lo, self.hi = lo, lo + span
        return True

    def indices(self, n: int) -> tuple[int, int]:
        """Inclusive (i0, i1) data indices inside the window."""
        if n <= 0:
            return (0, -1)
        if self.is_full:
            return (0, n - 1)
        i0 = min(max(int(round(self.lo * (n - 1))), 0), n - 1)
        i1 = min(max(int(round(self.hi * (n - 1))), 0), n - 1)
        if i1 <= i0:
            i1 = min(i0 + 1, n - 1)
        return (i0, i1)


class SeriesChart(QWidget):
    """One chart, three renderings. The Graphs tab feeds it per-bin
    time-series values for the selected metric and lets the user switch
    between bars, line, and share — same data, three ways to read it.

    Bars read exact values off the right column; line reads the shape
    of a session (where it spiked, where it fell off); share reads the
    per-zone split as a donut with a legend.
    """

    MODES = ("bars", "line", "share")

    # Bar geometry doubles as the zoom/pan mapping for bars mode.
    _BAR_H = 26
    _BAR_MARGIN = 12
    # Plot margins (left, right, top, bottom) for line/area mode.
    _PLOT_MARGINS = (44, 16, 24, 30)

    def __init__(self, parent: QWidget | None = None, *,
                 mode: str = "bars", value_format: str = "{:.0f}"):
        super().__init__(parent)
        self._points: list[Point] = []
        self._mode = mode if mode in self.MODES else "bars"
        self._value_format = value_format
        self._zoom = _ZoomState()
        self._pan_start = None
        # In-graph hover state: None, ("point", fpos) with a fractional
        # line position (the readout interpolates between bins), or
        # ("slice", index) for share. Bars have no hover — the values
        # already read straight off the rows. Painted directly on the
        # graph (instant, themed, clamped) — no Qt popout tooltip.
        self._hover: tuple[str, float] | None = None
        self._hover_pos: QPointF | None = None
        self.setMinimumHeight(120)
        self.setMouseTracking(True)

    def set_points(self, points: list[Point]) -> None:
        points = list(points)
        if len(points) != len(self._points):
            # New shape (session/metric switch) — drop the zoom and the
            # hover. Plain value refreshes keep both, so the live tick
            # neither yanks the view nor flashes a stale readout while
            # the cursor sits still (the old code cleared the hover on
            # every tick, which read as the wrong/previous slice).
            self._zoom.reset()
            self._hover = None
            self._hover_pos = None
        self._points = points
        self.update()

    def reset_zoom(self) -> None:
        if not self._zoom.is_full:
            self._zoom.reset()
            self.update()

    def _plot_rect(self) -> QRectF:
        ml, mr, mt, mb = self._PLOT_MARGINS
        return QRectF(ml, mt,
                      self.width() - ml - mr,
                      self.height() - mt - mb)

    # -- zoom / pan input (line mode only; bars and share ignore the
    # wheel so the surrounding scroll area keeps scrolling) --
    def _cursor_frac(self, pos) -> float | None:
        n = len(self._points)
        if n < 3 or self._mode != "line":
            return None
        plot = self._plot_rect()
        if plot.width() <= 0:
            return None
        return min(max((pos.x() - plot.left()) / plot.width(), 0.0), 1.0)

    def wheelEvent(self, e) -> None:
        steps = e.angleDelta().y() / 120.0
        frac = self._cursor_frac(e.position()) if steps else None
        if frac is None:
            e.ignore()
            return
        if self._zoom.zoom(frac, 0.8 ** steps, len(self._points)):
            e.accept()
            self.update()
        else:
            e.ignore()

    def mousePressEvent(self, e) -> None:
        if (e.button() == Qt.LeftButton and self._mode == "line"
                and not self._zoom.is_full):
            self._pan_start = e.position()
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._pan_start is None:
            self._update_hover(e.position())
            super().mouseMoveEvent(e)
            return
        plot = self._plot_rect()
        if plot.width() <= 0:
            return
        delta = -(e.position().x() - self._pan_start.x()) / plot.width()
        if self._zoom.pan(delta):
            self._pan_start = e.position()
            self.update()
        e.accept()

    def leaveEvent(self, _e) -> None:
        if self._hover is not None or self._hover_pos is not None:
            self._hover = None
            self._hover_pos = None
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self._pan_start is not None:
            self._pan_start = None
            e.accept()
        else:
            super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and not self._zoom.is_full:
            self.reset_zoom()
            e.accept()
        else:
            super().mouseDoubleClickEvent(e)

    # -- hover: in-graph annotation state (value at the cursor, per
    # mode). Instant (no tooltip delay), painted with the main-window
    # palette, clamped inside the widget. _update_hover tracks the
    # hovered datum; the paint pass draws the highlight + the box.
    def _update_hover(self, pos) -> None:
        new: tuple[str, float] | None
        if self._mode == "share":
            idx = self._share_index(pos)
            new = ("slice", idx) if idx is not None else None
        elif self._mode == "line":
            fpos = self._series_position(pos)
            new = ("point", fpos) if fpos is not None else None
        else:  # bars: values read straight off the rows — no hover.
            new = None
        anchor = QPointF(pos.x(), pos.y())
        if new != self._hover or (
                new is not None and anchor != self._hover_pos):
            self._hover = new
            self._hover_pos = anchor if new is not None else None
            self.update()

    def _point_lines(self, pt: Point) -> list[str]:
        lines = [pt.label, (pt.fmt or self._value_format).format(pt.value)]
        if pt.sublabel:
            lines.append(pt.sublabel)
        return lines

    def _hover_lines(self) -> list[str]:
        """Text lines for the current hover, or [] when nothing hovers."""
        if self._hover is None:
            return []
        kind, i = self._hover
        if self._mode == "share" and kind == "slice":
            _cx, _cy, _o, _in, total, slices = self._share_geometry()
            if not 0 <= i < len(slices) or total <= 0:
                return []
            pt, _c, _s, _sp = slices[int(i)]
            frac = max(pt.value, 0.0) / total
            return [pt.label,
                    f"{(pt.fmt or self._value_format).format(pt.value)}"
                    f" ({frac * 100.0:.1f}%)"]
        if self._mode == "line" and kind == "point":
            at = self._series_hover_at(i)
            if at is None:
                return []
            _pixel, value, base, frac = at
            i0, _i1 = self._zoom.indices(len(self._points))
            a = self._points[i0 + base]
            if frac < 1e-9:
                return self._point_lines(a)
            b = self._points[min(i0 + base + 1, len(self._points) - 1)]
            return [f"{a.label} – {b.label}",
                    f"≈ {(a.fmt or self._value_format).format(value)}"]
        return []

    def _series_position(self, pos) -> float | None:
        """Fractional cursor position into the zoomed line slice.

        0.0 is the first visible bin, N-1 the last. Values between
        bins are what let the readout interpolate instead of only
        ever snapping to the plotted points."""
        n = len(self._points)
        if not n or self._mode != "line":
            return None
        i0, i1 = self._zoom.indices(n)
        plot = self._plot_rect()
        if plot.width() <= 0:
            return None
        frac = (pos.x() - plot.left()) / plot.width()
        if not 0.0 <= frac <= 1.0:
            return None
        return frac * (i1 - i0)

    def _series_hover_at(self, fpos: float):
        """Pixel + interpolated value at a fractional slice position.

        Returns (pixel, value, base, frac): base/frac split fpos into
        the segment [base, base+1]; exactly-on-a-point yields frac 0.
        The pixel lerps between the painted coords, so the marker
        always sits on the curve and the box always reads what the
        marker sits on."""
        pts, coords, _plot = self._series_coords()
        m = len(pts)
        if not m or not coords:
            return None
        fpos = min(max(fpos, 0.0), float(m - 1))
        base = min(int(fpos), m - 1)
        nxt = min(base + 1, m - 1)
        frac = fpos - base
        vmax = max((pt.value for pt in pts), default=1.0)
        if vmax <= 0:
            vmax = 1.0
        value = pts[base].value + frac * (pts[nxt].value - pts[base].value)
        pixel = coords[base] * (1.0 - frac) + coords[nxt] * frac
        return pixel, value, base, frac

    def _series_index(self, pos) -> int | None:
        """Nearest-bin snap for the back-compat _series_tip shim."""
        fpos = self._series_position(pos)
        if fpos is None:
            return None
        n = len(self._points)
        i0, i1 = self._zoom.indices(n)
        return min(max(int(round(fpos)), 0), i1 - i0)

    def _share_index(self, pos) -> int | None:
        cx, cy, outer, inner, total, slices = self._share_geometry()
        if total <= 0:
            return None
        dist = math.hypot(pos.x() - cx, pos.y() - cy)
        if dist < inner or dist > outer:
            return None
        angle = (math.degrees(math.atan2(-(pos.y() - cy),
                                         pos.x() - cx)) - 90.0) % 360.0
        acc = 0.0
        for idx, (pt, _color, _start, _span) in enumerate(slices):
            frac = max(pt.value, 0.0) / total
            if frac <= 0:
                continue
            acc += frac
            if angle <= acc * 360.0:
                return idx
        return None

    # Back-compat shims for the old popout-tooltip helpers (tests and
    # any external callers). They now resolve through the index
    # hit-tests above.
    def _point_tip(self, pt: Point) -> str:
        return "\n".join(self._point_lines(pt))

    def _series_tip(self, pos) -> str:
        i = self._series_index(pos)
        if i is None:
            return ""
        i0, _i1 = self._zoom.indices(len(self._points))
        return self._point_tip(self._points[i0 + i])

    def _share_tip(self, pos) -> str:
        idx = self._share_index(pos)
        if idx is None:
            return ""
        _cx, _cy, _o, _in, total, slices = self._share_geometry()
        pt, _c, _s, _sp = slices[idx]
        frac = max(pt.value, 0.0) / total if total > 0 else 0.0
        return (f"{pt.label}\n"
                f"{(pt.fmt or self._value_format).format(pt.value)}"
                f" ({frac * 100.0:.1f}%)")

    def set_mode(self, mode: str) -> None:
        if mode in self.MODES and mode != self._mode:
            self._mode = mode
            # The zoom window is meaningless across shapes — a sliced
            # bars list is not the same view as a line window — so a
            # mode switch always returns to the full range.
            self._zoom.reset()
            self._pan_start = None
            self._hover = None
            self._hover_pos = None
            self.update()

    def set_value_format(self, fmt: str) -> None:
        self._value_format = fmt
        self.update()

    def clear(self) -> None:
        self._points.clear()
        self._zoom.reset()
        self._pan_start = None
        self._hover = None
        self._hover_pos = None
        self.update()

    # -- painting --
    def paintEvent(self, _e) -> None:
        # try/finally: an exception mid-paint must still end the
        # painter, or Qt logs "QBackingStore::endPaint() called with
        # active painter" and the widget goes blank.
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.fillRect(self.rect(), QColor(theme.INK_0))
            if not self._points:
                self._paint_empty(p)
                return
            if self._mode == "bars":
                self._paint_bars(p)
            elif self._mode == "share":
                self._paint_share(p)
            else:
                self._paint_series(p)
            self._paint_hover(p)
        finally:
            p.end()

    def _series_coords(self) -> tuple[list[Point], list[QPointF], QRectF]:
        """Zoomed line-mode slice plus its pixel coords and plot rect.

        Shared by the line painter and the hover highlight so the dot
        and the box can never disagree about where a point is."""
        n = len(self._points)
        i0, i1 = self._zoom.indices(n)
        pts = self._points[i0:i1 + 1]
        plot = self._plot_rect()
        coords: list[QPointF] = []
        if plot.width() >= 50 and plot.height() >= 40 and pts:
            m = len(pts)
            vmax = max((pt.value for pt in pts), default=1.0)
            if vmax <= 0:
                vmax = 1.0
            for i, pt in enumerate(pts):
                x = plot.left() + (i / (m - 1) if m > 1 else 0.5) * plot.width()
                y = plot.bottom() - (pt.value / vmax) * plot.height()
                coords.append(QPointF(x, y))
        return pts, coords, plot

    def _paint_hover(self, p: QPainter) -> None:
        """Highlight the hovered datum + a themed box clamped inside."""
        lines = self._hover_lines()
        if not lines or self._hover is None or self._hover_pos is None:
            return
        kind, i = self._hover
        anchor: QPointF
        if kind == "point":
            at = self._series_hover_at(i)
            if at is None:
                return
            c, _v, _b, _f = at
            _pts, _coords, plot = self._series_coords()
            # Guide line + a filled marker so the read point is
            # obvious. The marker rides the interpolated position, so
            # between bins it sits on the curve, not on thin air.
            p.setPen(QPen(QColor(theme.RUNE), 1, Qt.DashLine))
            p.drawLine(QPointF(c.x(), plot.top()),
                       QPointF(c.x(), plot.bottom()))
            p.setPen(QPen(QColor(theme.PARCH_BG), 1))
            p.setBrush(QColor(theme.RUNE))
            p.drawEllipse(c, 6.0, 6.0)
            p.setBrush(Qt.NoBrush)
            anchor = c
        else:  # "slice"
            _cx, _cy, outer, _inner, _total, slices = self._share_geometry()
            if not 0 <= i < len(slices):
                return
            _pt, _color, start, span = slices[int(i)]
            rect = QRectF(_cx - outer, _cy - outer, outer * 2, outer * 2)
            # Outline only: repainting the wedge would cover the hole
            # and its session total. A parchment edge marks the slice
            # without touching any other paint.
            p.setPen(QPen(QColor(theme.PARCH_BG), 3))
            p.setBrush(Qt.NoBrush)
            p.drawPie(rect, int(round(start * 16)), int(round(span * 16)))
            anchor = self._hover_pos
        self._paint_tip(p, anchor, lines)

    def _tip_rect(self, anchor: QPointF, lines: list[str]) -> QRectF:
        """Box rect for the hover annotation, clamped inside the widget.

        Prefers top-right of the anchor; flips to whichever side fits
        so the box never draws outside the chart."""
        title_font = QFont("Segoe UI", 9, QFont.Bold)
        body_font = QFont("Consolas", 9, QFont.Bold)
        fm_t = QFontMetrics(title_font)
        fm_b = QFontMetrics(body_font)
        w = max([fm_t.horizontalAdvance(lines[0])] +
                [fm_b.horizontalAdvance(ln) for ln in lines[1:]] + [0])
        h = fm_t.height() + sum(fm_b.height() for _ in lines[1:])
        pad_h, pad_v, gap = 10, 8, 14
        bw, bh = w + pad_h * 2, h + pad_v * 2
        x = anchor.x() + gap
        y = anchor.y() - bh - gap
        if x + bw > self.width() - 6:
            x = anchor.x() - bw - gap
        if x < 6:
            x = min(max(x, 6.0), max(6.0, self.width() - bw - 6.0))
        if y < 6:
            y = 6.0
        if y + bh > self.height() - 6:
            y = max(6.0, self.height() - bh - 6.0)
        # Degenerate (widget narrower than the box): pin to the corner.
        if bw > self.width() - 12:
            x, bw = 6.0, self.width() - 12.0
        return QRectF(x, y, bw, bh)

    def _paint_tip(self, p: QPainter, anchor: QPointF,
                   lines: list[str]) -> None:
        box = self._tip_rect(anchor, lines)
        if box.width() <= 0 or box.height() <= 0:
            return
        p.setPen(QPen(QColor(theme.INK_BORDER_2), 1))
        p.setBrush(QColor(theme.INK_1))
        p.drawRoundedRect(box, 4.0, 4.0)
        # Warm accent edge ties the box to the main-window theme.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.RUNE))
        p.drawRect(QRectF(box.left(), box.top() + 4, 3, box.height() - 8))
        title_font = QFont("Segoe UI", 9, QFont.Bold)
        body_font = QFont("Consolas", 9, QFont.Bold)
        pad_h, pad_v = 10, 8
        p.setPen(QColor(theme.PARCH_BG))
        p.setFont(title_font)
        fm_t = QFontMetrics(title_font)
        y = box.top() + pad_v
        p.drawText(QRectF(box.left() + pad_h, y,
                          box.width() - pad_h * 2, fm_t.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, lines[0])
        y += fm_t.height()
        p.setFont(body_font)
        fm_b = QFontMetrics(body_font)
        for ln in lines[1:]:
            p.drawText(QRectF(box.left() + pad_h, y,
                              box.width() - pad_h * 2, fm_b.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, ln)
            y += fm_b.height()

    def _paint_empty(self, p: QPainter) -> None:
        p.setPen(QColor(theme.ASH))
        p.setFont(QFont("Segoe UI", 10))
        p.drawText(self.rect(), Qt.AlignCenter, "No data")

    def _paint_bars(self, p: QPainter) -> None:
        # Bars always show the full range — no zoom, no pan. The chart
        # lives in a scroll area, so long lists scroll instead of slice.
        pts = self._points
        max_value = max((pt.value for pt in pts), default=1.0)
        if max_value <= 0:
            max_value = 1.0
        margin = self._BAR_MARGIN
        label_w = 180
        val_w = 110
        bar_h = self._BAR_H
        bar_x = margin + label_w + 10
        bar_w = self.width() - bar_x - val_w - margin
        if bar_w < 50:
            bar_w = 50
        label_font = QFont("Segoe UI", 10, QFont.Bold)
        value_font = QFont("Consolas", 10, QFont.Bold)
        sub_font = QFont("Segoe UI", 8)
        lm = QFontMetrics(label_font)
        for i, pt in enumerate(pts):
            y = margin + i * bar_h
            p.setPen(QColor(theme.PARCH_BG))
            p.setFont(label_font)
            p.drawText(QRectF(margin, y, label_w, bar_h - 4),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       lm.elidedText(pt.label, Qt.ElideRight, label_w))
            color = pt.color or QColor(theme.CRYSTAL)
            bar_len = int((pt.value / max_value) * bar_w)
            p.fillRect(QRectF(bar_x, y + 6, bar_len, bar_h - 16), color)
            p.setPen(QColor(theme.PARCH_BG))
            p.setFont(value_font)
            p.drawText(QRectF(self.width() - val_w - margin, y, val_w,
                              bar_h - 4),
                       Qt.AlignVCenter | Qt.AlignRight,
                       (pt.fmt or self._value_format).format(pt.value))
            if pt.sublabel:
                p.setPen(QColor(theme.ASH_BRIGHT))
                p.setFont(sub_font)
                # Sublabel tucks under the value when the bar is short;
                # otherwise it would collide with the bar itself.
                p.drawText(QRectF(bar_x, y + bar_h - 12, bar_w, 12),
                           Qt.AlignVCenter | Qt.AlignLeft, pt.sublabel)

    def _share_geometry(self):
        """Donut center/radii plus one slice per point, clockwise from top.

        Returns (cx, cy, outer, inner, total, slices) where each slice is
        (point, color, start_deg, span_deg). The legend layout lives in
        _paint_share; geometry here is shared with the hover hit-test so
        the two can never disagree about where a slice is.
        """
        pts = self._points
        total = sum(max(pt.value, 0.0) for pt in pts)
        margin = 12
        legend_w = min(250, int(self.width() * 0.42))
        donut_w = max(50, self.width() - legend_w - margin * 3)
        area_h = max(50, self.height() - 24)
        outer = max(10.0, min(donut_w, area_h) / 2.0 - 4.0)
        cx = margin + donut_w / 2.0
        cy = 12 + area_h / 2.0
        inner = outer * 0.55
        slices = []
        if total > 0:
            acc = 0.0
            for i, pt in enumerate(pts):
                frac = max(pt.value, 0.0) / total
                # Rotation palette shared with SpiderChart below.
                color = pt.color or QColor(
                    SPIDER_COLORS[i % len(SPIDER_COLORS)])
                slices.append((pt, color, 90.0 - acc * 360.0,
                               -frac * 360.0))
                acc += frac
        return cx, cy, outer, inner, total, slices

    def _paint_share(self, p: QPainter) -> None:
        cx, cy, outer, inner, total, slices = self._share_geometry()
        if total <= 0:
            self._paint_empty(p)
            return
        rect = QRectF(cx - outer, cy - outer, outer * 2, outer * 2)
        for _pt, color, start, span in slices:
            if not span:
                continue
            p.setPen(QPen(color, 1))
            p.setBrush(color)
            p.drawPie(rect, int(round(start * 16)), int(round(span * 16)))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.INK_0))
        p.drawEllipse(QPointF(cx, cy), inner, inner)
        # Session total sits in the hole; the legend carries the split.
        p.setPen(QColor(theme.PARCH_BG))
        p.setFont(QFont("Consolas", 13, QFont.Bold))
        hole = QRectF(cx - inner, cy - inner, inner * 2, inner * 2)
        p.drawText(hole, Qt.AlignCenter,
                   QFontMetrics(p.font()).elidedText(
                       self._value_format.format(total), Qt.ElideRight,
                       int(inner * 1.5)))
        # Legend down the right side. Overflow rows drop off instead of
        # squeezing the donut — hovering still identifies every slice.
        legend_w = min(250, int(self.width() * 0.42))
        lx = self.width() - legend_w - 12
        y = 16.0
        leg_font = QFont("Segoe UI", 9)
        p.setFont(leg_font)
        leg_fm = QFontMetrics(leg_font)
        for pt, color, _start, _span in slices:
            if y > self.height() - 12:
                break
            p.fillRect(QRectF(lx, y + 3, 12, 12), color)
            pct = (max(pt.value, 0.0) / total) * 100.0
            text = (f"{pt.label} — "
                    f"{(pt.fmt or self._value_format).format(pt.value)}"
                    f" ({pct:.1f}%)")
            p.setPen(QColor(theme.PARCH_BG))
            p.drawText(QRectF(lx + 18, y, legend_w - 18, 20),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       leg_fm.elidedText(text, Qt.ElideRight,
                                         int(legend_w - 18)))
            y += 22

    def _paint_series(self, p: QPainter) -> None:
        pts, coords, plot = self._series_coords()
        n = len(pts)
        if not n or not coords:
            return
        max_value = max((pt.value for pt in pts), default=1.0)
        if max_value <= 0:
            max_value = 1.0
        # Faint gridlines at 0 / half / max so the shape has a scale.
        p.setPen(QPen(QColor(theme.INK_BORDER_2), 1))
        p.setFont(QFont("Consolas", 8))
        for frac, tag in ((1.0, self._value_format.format(max_value)),
                          (0.5, self._value_format.format(max_value / 2)),
                          (0.0, self._value_format.format(0))):
            gy = plot.bottom() - frac * plot.height()
            p.drawLine(QPointF(plot.left(), gy), QPointF(plot.right(), gy))
            p.setPen(QColor(theme.ASH_BRIGHT))
            p.drawText(QRectF(0, gy - 10, self._PLOT_MARGINS[0] - 6, 20),
                       Qt.AlignVCenter | Qt.AlignRight, tag)
            p.setPen(QPen(QColor(theme.INK_BORDER_2), 1))

        pen = QPen(QColor(theme.CRYSTAL_LIGHT), 2)
        p.setPen(pen)
        for a, b in zip(coords, coords[1:]):
            p.drawLine(a, b)
        dot_pen = QPen(QColor(theme.CRYSTAL_HIGH), 1)
        p.setPen(dot_pen)
        for c in coords:
            p.setBrush(QColor(theme.CRYSTAL))
            p.drawEllipse(c, 3.5, 3.5)
        p.setBrush(Qt.NoBrush)
        # Labels: every point when they fit, otherwise every kth.
        p.setPen(QColor(theme.PARCH_BG))
        lab_font = QFont("Segoe UI", 9)
        p.setFont(lab_font)
        lm = QFontMetrics(lab_font)
        slot = plot.width() / n if n else plot.width()
        step = max(1, int(90 / slot) + 1) if slot < 90 else 1
        for i, pt in enumerate(pts):
            if i % step:
                continue
            c = coords[i]
            p.drawText(QRectF(c.x() - slot / 2, plot.bottom() + 4, slot, 20),
                       Qt.AlignHCenter | Qt.AlignTop,
                       lm.elidedText(pt.label, Qt.ElideRight, int(slot)))
            p.setPen(QColor(theme.CRYSTAL_LIGHT))
            p.setFont(QFont("Consolas", 8, QFont.Bold))
            p.drawText(QRectF(c.x() - slot / 2, c.y() - 20, slot, 16),
                       Qt.AlignHCenter | Qt.AlignBottom,
                       (pt.fmt or self._value_format).format(pt.value))
            p.setPen(QColor(theme.PARCH_BG))
            p.setFont(lab_font)


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
            p.end()
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


@dataclass
class SpiderSeries:
    """One polygon on the radar web (raw values, one per axis)."""
    label: str
    values: list[float]
    color: QColor | None = None
    dimmed: bool = False  # de-emphasized (e.g. not the picked zone)


#: Rotation palette for zone polygons (caller caps the series count).
SPIDER_COLORS = (
    theme.CRYSTAL, theme.RUNE, theme.CRYSTAL_LIGHT, theme.CRIMSON,
    theme.PARCH_RULE, theme.ASH_BRIGHT, theme.CRYSTAL_HIGH, theme.PARCH_BG,
)


class SpiderChart(QWidget):
    """Radar web comparing several series across normalized stats.

    Each axis is scaled 0..1 by the max across the series, so zones
    (or sessions) of very different sizes still read as shapes: a
    bigger polygon farmed more, a skewed one leaned on one stat. The
    axis labels carry the per-axis max so the scale isn't a mystery.
    """

    def __init__(self, parent: QWidget | None = None,
                 *, value_format: str = "{:.0f}"):
        super().__init__(parent)
        self._axes: list[str] = []
        self._series: list[SpiderSeries] = []
        self._value_format = value_format
        self.setMinimumHeight(300)

    def set_axes(self, axes: list[str]) -> None:
        self._axes = list(axes)
        self.update()

    def set_series(self, series: list[SpiderSeries]) -> None:
        self._series = list(series)
        self.update()

    def set_value_format(self, fmt: str) -> None:
        self._value_format = fmt
        self.update()

    def clear(self) -> None:
        self._series.clear()
        self.update()

    def paintEvent(self, _e) -> None:
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.INK_0))
        if not self._axes or not self._series:
            p.setPen(QColor(theme.ASH))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(self.rect(), Qt.AlignCenter, "No data")
            p.end()
            return

        n = len(self._axes)
        # Legend reserves the bottom; the web takes the rest.
        legend_h = len(self._series) * 20 + 12
        side = min(self.width() - 24,
                   self.height() - legend_h - 70)
        if side < 120:
            p.end()
            return
        cx = self.width() / 2
        cy = 34 + (self.height() - legend_h - 34) / 2
        radius = side / 2
        maxima = []
        for a in range(n):
            m = max((s.values[a] if a < len(s.values) else 0.0)
                    for s in self._series)
            maxima.append(m if m > 0 else 1.0)

        def vertex(a: int, frac: float) -> QPointF:
            # Start at the top, sweep clockwise.
            ang = -math.pi / 2 + 2 * math.pi * a / n
            return QPointF(cx + math.cos(ang) * radius * frac,
                           cy + math.sin(ang) * radius * frac)

        # Web rings + spokes.
        web_pen = QPen(QColor(theme.INK_BORDER_2), 1)
        p.setPen(web_pen)
        for ring in (0.25, 0.5, 0.75, 1.0):
            path = QPainterPath(vertex(0, ring))
            for a in range(1, n):
                path.lineTo(vertex(a, ring))
            path.closeSubpath()
            p.drawPath(path)
        for a in range(n):
            p.drawLine(QPointF(cx, cy), vertex(a, 1.0))

        # Axis labels with their scale max.
        p.setFont(QFont("Segoe UI", 8))
        for a, name in enumerate(self._axes):
            v = vertex(a, 1.0)
            tag = f"{name} · {self._value_format.format(maxima[a])}"
            box = QRectF(v.x() - 80, v.y() - 24, 160, 28)
            # Push the text box outward from the web so it never sits
            # on a polygon edge.
            dx, dy = v.x() - cx, v.y() - cy
            dist = math.hypot(dx, dy) or 1.0
            box.translate(dx / dist * 10, dy / dist * 10)
            p.setPen(QColor(theme.ASH_BRIGHT))
            p.drawText(box, Qt.AlignCenter, tag)

        # Polygons, dimmed ones first so the highlight sits on top.
        for s in sorted(self._series, key=lambda s: (not s.dimmed, s.label)):
            color = s.color or QColor(theme.CRYSTAL)
            if s.dimmed:
                color = QColor(color)
                color.setAlpha(70)
            pts = [vertex(a, min((s.values[a] if a < len(s.values) else 0.0)
                                 / maxima[a], 1.0))
                   for a in range(n)]
            path = QPainterPath(pts[0])
            for pt in pts[1:]:
                path.lineTo(pt)
            path.closeSubpath()
            wash = QColor(color)
            wash.setAlpha(60 if not s.dimmed else 25)
            p.fillPath(path, wash)
            edge = QColor(color)
            edge.setAlpha(255 if not s.dimmed else 110)
            p.setPen(QPen(edge, 2 if not s.dimmed else 1))
            p.drawPath(path)
            dot = QColor(color)
            dot.setAlpha(255 if not s.dimmed else 110)
            p.setBrush(dot)
            for pt in pts:
                p.drawEllipse(pt, 3.0, 3.0)
            p.setBrush(Qt.NoBrush)

        # Legend under the web.
        p.setFont(QFont("Segoe UI", 9))
        lm = QFontMetrics(p.font())
        ly = cy + radius + 26
        for s in self._series:
            color = s.color or QColor(theme.CRYSTAL)
            chip = QColor(color)
            if s.dimmed:
                chip.setAlpha(110)
            p.fillRect(QRectF(cx - radius, ly, 10, 10), chip)
            p.setPen(QColor(theme.PARCH_BG if not s.dimmed else theme.ASH_BRIGHT))
            p.drawText(QRectF(cx - radius + 16, ly - 4, radius * 2 - 16, 18),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       lm.elidedText(s.label, Qt.ElideRight,
                                     int(radius * 2 - 16)))
            ly += 20
        p.end()
