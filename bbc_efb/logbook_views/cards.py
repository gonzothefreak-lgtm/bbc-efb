"""Flight cards: a virtualised card grid over the same filtered model as the table.

``FlightCardView`` is a ``QListView`` in icon mode with uniform item sizes, so only the
cards on screen are painted; 2,000 flights scroll as smoothly as 20. The delegate draws
each card (date, operation chip, side-on aircraft art, callsign and route, flight time,
touchdown rate, record status) from the row's ``FlightView``.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate

from .aircraft_art import AircraftArtLibrary, shared_library
from .model import ROLE_FLIGHT, FlightView, format_date, format_duration, format_rate

CARD_BG = QColor("#142130")
CARD_BG_HOVER = QColor("#182a3c")
CARD_BG_SELECTED = QColor("#17324a")
CARD_BORDER = QColor("#283b50")
CARD_BORDER_SELECTED = QColor("#86caff")
TEXT = QColor("#e7eef7")
MUTED = QColor("#a4b7cb")
WARNING = QColor("#ffd693")
CHIP_BG = QColor("#1d3a55")
SIM_BG = QColor("#4a3b17")


def card_summary(view: FlightView) -> str:
    """One plain sentence describing a card (tooltip and accessibility)."""
    parts = [format_date(view), view.callsign or "no callsign", view.route, view.aircraft or "aircraft unknown",
             f"flight time {format_duration(view.flight_time_s, 'n/a')}",
             f"touchdown rate {format_rate(view.touchdown_rate_fpm, 'not measured')}", view.review_label]
    if view.simulated:
        parts.append("simulated data")
    return ", ".join(parts)


class FlightCardDelegate(QStyledItemDelegate):
    """Paints one flight card. Size scales with the view's font (Windows text scaling)."""

    def __init__(self, parent=None, library: AircraftArtLibrary | None = None):
        super().__init__(parent)
        self.library = library or shared_library()

    def _metrics(self, option) -> QFontMetricsF:
        return QFontMetricsF(option.font)

    def card_size(self, font: QFont) -> QSize:
        metrics = QFontMetricsF(font)
        line = metrics.height()
        width = max(250, int(metrics.averageCharWidth() * 40))
        art = int(width * 0.34)
        height = int(10 + line + 6 + art + 6 + line * 1.25 + 4 + line + 12)
        return QSize(width, height)

    def sizeHint(self, option, index):  # noqa: N802 (Qt API)
        return self.card_size(option.font)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        view = index.data(ROLE_FLIGHT)
        if not isinstance(view, FlightView):
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focus = bool(option.state & QStyle.StateFlag.State_HasFocus)
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        painter.fillPath(path, CARD_BG_SELECTED if selected else CARD_BG_HOVER if hover else CARD_BG)
        painter.setPen(QPen(CARD_BORDER_SELECTED if (selected or focus) else CARD_BORDER, 2 if focus else 1))
        painter.drawPath(path)

        font = QFont(option.font)
        metrics = QFontMetricsF(font)
        line = metrics.height()
        inner = rect.adjusted(12, 8, -12, -8)
        # Top row: date and chips
        painter.setFont(font)
        painter.setPen(MUTED)
        painter.drawText(QRectF(inner.left(), inner.top(), inner.width() * 0.55, line),
                         Qt.AlignmentFlag.AlignVCenter, format_date(view))
        chip_right = inner.right()
        for text, colour in ((view.operation_label if view.operation else "", CHIP_BG),
                             ("Simulated" if view.simulated else "", SIM_BG)):
            if not text:
                continue
            small = QFont(font)
            small.setPointSizeF(max(6.0, font.pointSizeF() * 0.85))
            small_metrics = QFontMetricsF(small)
            width = small_metrics.horizontalAdvance(text) + 14
            chip = QRectF(chip_right - width, inner.top(), width, line)
            chip_path = QPainterPath()
            chip_path.addRoundedRect(chip, line / 2, line / 2)
            painter.fillPath(chip_path, colour)
            painter.setFont(small)
            painter.setPen(TEXT)
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
            painter.setFont(font)
            chip_right -= width + 6
        # Art
        art_h = rect.width() * 0.34
        art_rect = QRectF(inner.left(), inner.top() + line + 4, inner.width(), art_h)
        pixmap = self.library.pixmap(view.aircraft, art_rect.size().toSize(), painter.device().devicePixelRatioF()
                                     if painter.device() is not None else 1.0)
        painter.drawPixmap(art_rect.topLeft(), pixmap)
        # Callsign and route
        y = art_rect.bottom() + 4
        bold = QFont(font)
        bold.setBold(True)
        bold.setPointSizeF(font.pointSizeF() * 1.12)
        bold_metrics = QFontMetricsF(bold)
        painter.setFont(bold)
        painter.setPen(TEXT)
        title = view.callsign or view.aircraft or "Flight"
        title_w = min(inner.width() * 0.5, bold_metrics.horizontalAdvance(title) + 2)
        painter.drawText(QRectF(inner.left(), y, title_w, line * 1.25), Qt.AlignmentFlag.AlignVCenter,
                         bold_metrics.elidedText(title, Qt.TextElideMode.ElideRight, title_w))
        painter.setFont(font)
        route_w = inner.width() - title_w - 10
        painter.drawText(QRectF(inner.left() + title_w + 10, y, route_w, line * 1.25), Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(view.route, Qt.TextElideMode.ElideRight, route_w))
        # Bottom row: time, rate, status
        y += line * 1.25 + 4
        gap = 8.0
        third = (inner.width() - 2 * gap) / 3
        painter.setPen(TEXT if view.flight_time_s is not None else MUTED)
        painter.drawText(QRectF(inner.left(), y, third, line), Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(f"Time {format_duration(view.flight_time_s, 'n/a')}",
                                            Qt.TextElideMode.ElideRight, third))
        painter.setPen(TEXT if view.touchdown_rate_fpm is not None else MUTED)
        painter.drawText(QRectF(inner.left() + third + gap, y, third, line), Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(format_rate(view.touchdown_rate_fpm, "Rate n/a"), Qt.TextElideMode.ElideRight, third))
        painter.setPen(WARNING if view.needs_attention or view.needs_confirmation else MUTED)
        painter.drawText(QRectF(inner.left() + 2 * (third + gap), y, third, line),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                         metrics.elidedText(view.review_label, Qt.TextElideMode.ElideRight, third))
        painter.restore()

    def helpEvent(self, event, view, option, index):  # noqa: N802 (Qt API)
        flight = index.data(ROLE_FLIGHT)
        if isinstance(flight, FlightView) and event is not None:
            from PySide6.QtWidgets import QToolTip
            QToolTip.showText(event.globalPos(), card_summary(flight), view)
            return True
        return super().helpEvent(event, view, option, index)


class FlightCardView(QListView):
    """Card grid sharing the History proxy model (and selection model in Combined view)."""

    def __init__(self, parent=None, library: AircraftArtLibrary | None = None):
        super().__init__(parent)
        self.card_delegate = FlightCardDelegate(self, library)
        self.setItemDelegate(self.card_delegate)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setMouseTracking(True)
        self.setAccessibleName("Flight cards")
        self.setStyleSheet("QListView { background: transparent; border: none; }")
        self._update_grid()

    def _update_grid(self):
        size = self.card_delegate.card_size(self.font())
        self.setGridSize(QSize(size.width() + 8, size.height() + 8))

    def changeEvent(self, event):  # noqa: N802 (Qt API)
        super().changeEvent(event)
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.FontChange:
            self._update_grid()
