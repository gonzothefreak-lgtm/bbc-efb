"""Small QPainter charts for the Logbook (no chart library dependency).

Every chart:

* draws only the data it is given and shows a plain "no data" message otherwise,
* exposes a text summary (``summary_text()``) and sets its accessible description,
  so screen readers and tests can check what is drawn,
* paints only when visible (Qt never paints hidden widgets), keeping Combined views
  and hidden pages free of off-screen work.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

PALETTE = ("#3c9bdd", "#4cc36a", "#f0a03c", "#9b6dde", "#3cc7c0", "#e25d6a", "#d8c94c", "#7d8fa3")
OTHER_COLOUR = "#7d8fa3"
TEXT = QColor("#e7eef7")
MUTED = QColor("#a4b7cb")
GRID = QColor("#24374a")
TRACK = QColor("#22364a")


@dataclass(frozen=True, slots=True)
class Segment:
    """One labelled value. ``text`` is the value as the pilot should read it."""

    label: str
    value: float
    text: str = ""
    colour: str = ""


def colour_for(index: int) -> QColor:
    return QColor(PALETTE[index % len(PALETTE)])


def group_small(segments: Sequence[Segment], limit: int, other_label: str = "Other") -> list[Segment]:
    """Keep the ``limit - 1`` largest segments and fold the rest into one "Other" segment."""
    items = [item for item in segments if item.value > 0]
    if len(items) <= limit:
        return list(items)
    items.sort(key=lambda item: item.value, reverse=True)
    head, tail = items[: limit - 1], items[limit - 1:]
    total = sum(item.value for item in tail)
    return head + [Segment(f"{other_label} ({len(tail)})", total, "", OTHER_COLOUR)]


class _Chart(QWidget):
    """Shared behaviour: empty message, title-less body, accessible summary."""

    MIN = QSize(180, 120)

    def __init__(self, accessible_name: str = "", empty_text: str = "No records to show yet.", parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(self.MIN)
        self.empty_text = empty_text
        if accessible_name:
            self.setAccessibleName(accessible_name)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def summary_text(self) -> str:  # pragma: no cover - overridden
        return ""

    def _update_summary(self):
        self.setAccessibleDescription(self.summary_text())
        self.setToolTip(self.summary_text())
        self.update()

    def _font(self, scale: float = 1.0, bold: bool = False) -> QFont:
        font = QFont(self.font())
        size = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        font.setPointSizeF(max(6.0, size * scale))
        font.setBold(bold)
        return font

    def _paint_empty(self, painter: QPainter):
        painter.setPen(MUTED)
        painter.setFont(self._font(0.95))
        painter.drawText(QRectF(self.rect()).adjusted(8, 8, -8, -8),
                         Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.empty_text)

    def render_check(self) -> None:
        """Paint once into an off-screen image (used by tests to exercise paint code)."""
        from PySide6.QtGui import QImage
        size = self.size() if self.width() > 10 else self.sizeHint()
        image = QImage(size, QImage.Format.Format_ARGB32)
        image.fill(0)
        self.resize(size)
        self.render(image)


class DonutChart(_Chart):
    """Proportions with a centre caption and a legend (label, share, value text)."""

    def __init__(self, accessible_name="", empty_text="No records to show yet.", parent=None, max_segments=6):
        super().__init__(accessible_name, empty_text, parent)
        self.segments: list[Segment] = []
        self.centre_title = ""
        self.centre_caption = ""
        self.max_segments = max_segments
        self.footnote = ""

    def sizeHint(self):
        return QSize(340, 190)

    def set_data(self, segments: Sequence[Segment], centre_title="", centre_caption="", footnote=""):
        grouped = group_small(segments, self.max_segments)
        self.segments = [
            Segment(item.label, item.value, item.text, item.colour or PALETTE[index % len(PALETTE)])
            for index, item in enumerate(grouped)
        ]
        self.centre_title, self.centre_caption, self.footnote = centre_title, centre_caption, footnote
        self._update_summary()

    @property
    def total(self) -> float:
        return sum(item.value for item in self.segments)

    def shares(self) -> list[tuple[str, float]]:
        total = self.total
        return [(item.label, item.value / total if total else 0.0) for item in self.segments]

    def summary_text(self) -> str:
        if not self.segments:
            return self.empty_text
        parts = [f"{label}: {share:.0%}" for label, share in self.shares()]
        text = "; ".join(parts)
        if self.footnote:
            text += f". {self.footnote}"
        return text

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.segments or self.total <= 0:
            self._paint_empty(painter)
            return
        rect = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        foot_h = 0.0
        if self.footnote:
            foot_h = QFontMetricsF(self._font(0.8)).height() + 4
            rect.setBottom(rect.bottom() - foot_h)
        diameter = max(40.0, min(rect.height(), rect.width() * 0.45))
        ring = QRectF(rect.left(), rect.top() + (rect.height() - diameter) / 2, diameter, diameter)
        thickness = max(8.0, diameter * 0.17)
        inner = ring.adjusted(thickness / 2, thickness / 2, -thickness / 2, -thickness / 2)
        start = 90.0 * 16
        total = self.total
        for item in self.segments:
            span = -360.0 * 16 * item.value / total
            pen = QPen(QColor(item.colour), thickness)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.drawArc(inner, int(start), int(math.floor(span)))
            start += span
        painter.setPen(TEXT)
        if self.centre_title:
            painter.setFont(self._font(1.2, True))
            title_rect = QRectF(inner.left(), inner.center().y() - inner.height() * 0.34, inner.width(), inner.height() * 0.34)
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, self.centre_title)
        if self.centre_caption:
            painter.setPen(MUTED)
            painter.setFont(self._font(0.8))
            caption_rect = QRectF(inner.left(), inner.center().y() + 3, inner.width(), inner.height() * 0.3)
            painter.drawText(caption_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self.centre_caption)
        # Legend
        legend = QRectF(ring.right() + 14, rect.top(), rect.right() - ring.right() - 14, rect.height())
        font = self._font(0.9)
        metrics = QFontMetricsF(font)
        line = metrics.height() + 6
        top = legend.top() + max(0.0, (legend.height() - line * len(self.segments)) / 2)
        painter.setFont(font)
        for index, (item, (_label, share)) in enumerate(zip(self.segments, self.shares())):
            y = top + index * line
            painter.fillRect(QRectF(legend.left(), y + (line - 10) / 2, 10, 10), QColor(item.colour))
            painter.setPen(TEXT)
            share_text = f"{share:.0%}" if not item.text else f"{item.text}  {share:.0%}"
            share_w = metrics.horizontalAdvance(share_text) + 4
            label_w = max(10.0, legend.width() - 16 - share_w)
            painter.drawText(QRectF(legend.left() + 16, y, label_w, line), Qt.AlignmentFlag.AlignVCenter,
                             metrics.elidedText(item.label, Qt.TextElideMode.ElideRight, label_w))
            painter.setPen(MUTED)
            painter.drawText(QRectF(legend.right() - share_w, y, share_w, line),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, share_text)
        if self.footnote:
            painter.setPen(MUTED)
            painter.setFont(self._font(0.8))
            painter.drawText(QRectF(rect.left(), rect.bottom() + 4, rect.width(), foot_h),
                             Qt.AlignmentFlag.AlignLeft, self.footnote)


class BarChart(_Chart):
    """Vertical bars with category labels underneath (distributions, monthly trends)."""

    def __init__(self, accessible_name="", empty_text="No records to show yet.", parent=None, colour=PALETTE[1]):
        super().__init__(accessible_name, empty_text, parent)
        self.bars: list[Segment] = []
        self.colour = colour
        self.axis_title = ""
        self.footnote = ""

    def sizeHint(self):
        return QSize(360, 200)

    def set_data(self, bars: Sequence[Segment], axis_title="", footnote=""):
        self.bars = list(bars)
        self.axis_title, self.footnote = axis_title, footnote
        self._update_summary()

    def summary_text(self) -> str:
        if not self.bars or not any(item.value > 0 for item in self.bars):
            return self.empty_text
        parts = [f"{item.label}: {item.text or format(item.value, 'g')}" for item in self.bars]
        text = "; ".join(parts)
        if self.footnote:
            text += f". {self.footnote}"
        return text

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.bars or not any(item.value > 0 for item in self.bars):
            self._paint_empty(painter)
            return
        font = self._font(0.8)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        rect = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        bottom_text = metrics.height() * (2 if self.axis_title else 1) + 6
        if self.footnote:
            bottom_text += metrics.height() + 2
        plot = QRectF(rect.left(), rect.top() + metrics.height() + 2, rect.width(), rect.height() - bottom_text - metrics.height() - 2)
        peak = max(item.value for item in self.bars) or 1.0
        painter.setPen(QPen(GRID, 1))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        slot = plot.width() / len(self.bars)
        bar_w = max(3.0, slot * 0.62)
        for index, item in enumerate(self.bars):
            x = plot.left() + index * slot + (slot - bar_w) / 2
            h = plot.height() * (item.value / peak) if item.value > 0 else 0.0
            colour = QColor(item.colour or self.colour)
            if h > 0:
                path = QPainterPath()
                path.addRoundedRect(QRectF(x, plot.bottom() - h, bar_w, h), 2, 2)
                painter.fillPath(path, colour)
                painter.setPen(MUTED)
                value_text = item.text or format(item.value, "g")
                if metrics.horizontalAdvance(value_text) <= slot:
                    painter.drawText(QRectF(x - slot, plot.bottom() - h - metrics.height() - 1, bar_w + 2 * slot, metrics.height()),
                                     Qt.AlignmentFlag.AlignHCenter, value_text)
            painter.setPen(TEXT)
            label = metrics.elidedText(item.label, Qt.TextElideMode.ElideRight, slot)
            painter.drawText(QRectF(plot.left() + index * slot, plot.bottom() + 3, slot, metrics.height()),
                             Qt.AlignmentFlag.AlignHCenter, label)
        y = plot.bottom() + 3 + metrics.height()
        if self.axis_title:
            painter.setPen(MUTED)
            painter.drawText(QRectF(rect.left(), y + 2, rect.width(), metrics.height()), Qt.AlignmentFlag.AlignHCenter, self.axis_title)
            y += metrics.height() + 2
        if self.footnote:
            painter.setPen(MUTED)
            painter.drawText(QRectF(rect.left(), y + 2, rect.width(), metrics.height()), Qt.AlignmentFlag.AlignLeft,
                             metrics.elidedText(self.footnote, Qt.TextElideMode.ElideRight, rect.width()))


class HorizontalBarChart(_Chart):
    """Ranked horizontal bars (aircraft types, airports, operations)."""

    def __init__(self, accessible_name="", empty_text="No records to show yet.", parent=None, max_rows=8):
        super().__init__(accessible_name, empty_text, parent)
        self.rows: list[Segment] = []
        self.max_rows = max_rows
        self.hidden_rows = 0
        self.footnote = ""

    def sizeHint(self):
        return QSize(340, 200)

    def set_data(self, rows: Sequence[Segment], footnote=""):
        ranked = sorted((item for item in rows if item.value > 0), key=lambda item: item.value, reverse=True)
        self.rows = ranked[: self.max_rows]
        self.hidden_rows = max(0, len(ranked) - self.max_rows)
        self.footnote = footnote
        if self.hidden_rows and not footnote:
            self.footnote = f"{self.hidden_rows} more not shown."
        self._update_summary()

    def summary_text(self) -> str:
        if not self.rows:
            return self.empty_text
        text = "; ".join(f"{item.label}: {item.text or format(item.value, 'g')}" for item in self.rows)
        return f"{text}. {self.footnote}" if self.footnote else text

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.rows:
            self._paint_empty(painter)
            return
        font = self._font(0.9)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        rect = QRectF(self.rect()).adjusted(6, 6, -6, -6)
        if self.footnote:
            rect.setBottom(rect.bottom() - metrics.height() - 2)
        line = min(metrics.height() + 10, rect.height() / max(1, len(self.rows)))
        label_w = min(rect.width() * 0.38, max(metrics.horizontalAdvance(item.label) for item in self.rows) + 8)
        value_w = max(metrics.horizontalAdvance(item.text or format(item.value, "g")) for item in self.rows) + 8
        track_w = max(10.0, rect.width() - label_w - value_w - 8)
        peak = max(item.value for item in self.rows) or 1.0
        for index, item in enumerate(self.rows):
            y = rect.top() + index * line
            painter.setPen(TEXT)
            painter.drawText(QRectF(rect.left(), y, label_w, line), Qt.AlignmentFlag.AlignVCenter,
                             metrics.elidedText(item.label, Qt.TextElideMode.ElideRight, label_w - 4))
            bar_h = max(4.0, line * 0.45)
            track = QRectF(rect.left() + label_w, y + (line - bar_h) / 2, track_w, bar_h)
            path = QPainterPath()
            path.addRoundedRect(track, bar_h / 2, bar_h / 2)
            painter.fillPath(path, TRACK)
            fill = QRectF(track.left(), track.top(), max(bar_h, track_w * item.value / peak), bar_h)
            path = QPainterPath()
            path.addRoundedRect(fill, bar_h / 2, bar_h / 2)
            painter.fillPath(path, QColor(item.colour or PALETTE[0]))
            painter.setPen(MUTED)
            painter.drawText(QRectF(rect.right() - value_w, y, value_w, line),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, item.text or format(item.value, "g"))
        if self.footnote:
            painter.setPen(MUTED)
            painter.drawText(QRectF(rect.left(), rect.bottom() + 2, rect.width(), metrics.height()), Qt.AlignmentFlag.AlignLeft,
                             metrics.elidedText(self.footnote, Qt.TextElideMode.ElideRight, rect.width()))


@dataclass(frozen=True, slots=True)
class Marker:
    """An event marker on a time axis. ``seconds`` is from the start of the axis."""

    seconds: float
    label: str
    colour: str = PALETTE[0]


class ProfileChart(_Chart):
    """Side-on altitude profile from Recorder track points, with event markers."""

    def __init__(self, accessible_name="Altitude profile", empty_text="No altitude track points were recorded for this flight.", parent=None):
        super().__init__(accessible_name, empty_text, parent)
        self.points: list[tuple[float, float]] = []
        self.markers: list[Marker] = []
        self.gaps: list[tuple[float, float]] = []

    def sizeHint(self):
        return QSize(520, 180)

    def set_data(self, points: Sequence[tuple[float, float]], markers: Sequence[Marker] = (), gaps: Sequence[tuple[float, float]] = ()):
        self.points = sorted((float(s), float(a)) for s, a in points)
        self.markers = list(markers)
        self.gaps = [(float(a), float(b)) for a, b in gaps]
        self._update_summary()

    def summary_text(self) -> str:
        if len(self.points) < 2:
            return self.empty_text
        peak = max(alt for _s, alt in self.points)
        span = self.points[-1][0] - self.points[0][0]
        text = f"{len(self.points)} track points over {span / 60:.0f} minutes, highest {peak:,.0f} ft"
        if self.gaps:
            text += f"; {len(self.gaps)} data gap(s) shaded"
        return text

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if len(self.points) < 2:
            self._paint_empty(painter)
            return
        font = self._font(0.8)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        rect = QRectF(self.rect()).adjusted(8, metrics.height() + 6, -8, -metrics.height() - 6)
        t0, t1 = self.points[0][0], self.points[-1][0]
        span = max(1.0, t1 - t0)
        low = min(0.0, min(a for _s, a in self.points))
        high = max(a for _s, a in self.points)
        high = high if high > low else low + 1.0

        def to_point(seconds, altitude):
            return QPointF(rect.left() + rect.width() * (seconds - t0) / span,
                           rect.bottom() - rect.height() * (altitude - low) / (high - low))

        for start, end in self.gaps:
            left = rect.left() + rect.width() * (max(start, t0) - t0) / span
            right = rect.left() + rect.width() * (min(end, t1) - t0) / span
            if right > left:
                painter.fillRect(QRectF(left, rect.top(), right - left, rect.height()), QColor(226, 93, 106, 50))
        painter.setPen(QPen(GRID, 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        path = QPainterPath(to_point(*self.points[0]))
        for seconds, altitude in self.points[1:]:
            path.lineTo(to_point(seconds, altitude))
        fill = QPainterPath(path)
        fill.lineTo(QPointF(rect.right(), rect.bottom()))
        fill.lineTo(QPointF(rect.left(), rect.bottom()))
        fill.closeSubpath()
        painter.fillPath(fill, QColor(60, 155, 221, 45))
        painter.setPen(QPen(QColor(PALETTE[0]), 2))
        painter.drawPath(path)
        painter.setPen(MUTED)
        painter.drawText(QRectF(rect.left(), rect.top() - metrics.height() - 4, rect.width(), metrics.height()),
                         Qt.AlignmentFlag.AlignLeft, f"{high:,.0f} ft")
        painter.drawText(QRectF(rect.left(), rect.bottom() + 3, rect.width(), metrics.height()),
                         Qt.AlignmentFlag.AlignRight, f"{span / 60:.0f} min")
        for marker in self.markers:
            if not t0 <= marker.seconds <= t1:
                continue
            x = rect.left() + rect.width() * (marker.seconds - t0) / span
            painter.setPen(QPen(QColor(marker.colour), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.setPen(QColor(marker.colour))
            painter.drawText(QRectF(x + 3, rect.top(), 120, metrics.height()), Qt.AlignmentFlag.AlignLeft, marker.label)


@dataclass(frozen=True, slots=True)
class Milestone:
    """A labelled point on the flight event strip; ``time_text`` is already formatted."""

    label: str
    time_text: str
    known: bool = True


class TimelineStrip(_Chart):
    """OFF - TO - TD - ON style strip. Unknown milestones are drawn hollow and grey."""

    MIN = QSize(220, 64)

    def __init__(self, accessible_name="Flight event timeline", empty_text="No flight events were recorded.", parent=None):
        super().__init__(accessible_name, empty_text, parent)
        self.milestones: list[Milestone] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        metrics = QFontMetricsF(self._font(0.85))
        return QSize(420, int(metrics.height() * 2 + 34))

    def minimumSizeHint(self):
        return QSize(220, self.sizeHint().height())

    def set_data(self, milestones: Sequence[Milestone]):
        self.milestones = list(milestones)
        self.setFixedHeight(self.sizeHint().height())
        self._update_summary()

    def summary_text(self) -> str:
        if not any(item.known for item in self.milestones):
            return self.empty_text
        return "; ".join(f"{item.label} {item.time_text}" for item in self.milestones)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.milestones or not any(item.known for item in self.milestones):
            self._paint_empty(painter)
            return
        font = self._font(0.85)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        rect = QRectF(self.rect()).adjusted(10, 4, -10, -4)
        y = rect.top() + 10
        count = len(self.milestones)
        slot = rect.width() / count
        xs = [rect.left() + slot * (index + 0.5) for index in range(count)]
        painter.setPen(QPen(QColor(PALETTE[1]), 2))
        painter.drawLine(QPointF(xs[0], y), QPointF(xs[-1], y))
        for x, item in zip(xs, self.milestones):
            if item.known:
                painter.setPen(QPen(QColor(PALETTE[1]), 3))
                painter.setBrush(QColor("#0d151f"))
            else:
                painter.setPen(QPen(MUTED, 2, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(x, y), 6, 6)
            painter.setPen(TEXT if item.known else MUTED)
            painter.drawText(QRectF(x - slot / 2, y + 10, slot, metrics.height()), Qt.AlignmentFlag.AlignHCenter,
                             metrics.elidedText(item.label, Qt.TextElideMode.ElideRight, slot))
            painter.setPen(MUTED)
            painter.drawText(QRectF(x - slot / 2, y + 10 + metrics.height(), slot, metrics.height()), Qt.AlignmentFlag.AlignHCenter,
                             metrics.elidedText(item.time_text, Qt.TextElideMode.ElideRight, slot))
