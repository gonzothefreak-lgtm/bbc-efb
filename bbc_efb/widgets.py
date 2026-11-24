"""Shared Qt Widgets used by the EFB sections."""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHeaderView, QLabel, QPushButton, QScrollArea,
    QTableWidget, QVBoxLayout, QWidget,
)


def label(text, style="", wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(style)
    widget.setWordWrap(wrap)
    return widget


def button(text, slot=None, style=""):
    widget = QPushButton(text)
    widget.setObjectName(style)
    if slot is not None:
        widget.clicked.connect(slot)
    return widget


def card(title=None, description=None):
    widget = QFrame()
    widget.setObjectName("card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(9)
    if title:
        layout.addWidget(label(title, "cardTitle"))
    if description:
        layout.addWidget(label(description, "muted", wrap=True))
    return widget, layout


def scroll(widget):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area


def page():
    widget = QWidget()
    widget.setObjectName("page")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    return widget, layout


class _RowActivation(QObject):
    """Call ``callback(row)`` on double-click and on Enter/Return, once per activation."""

    KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)

    def __init__(self, widget, callback):
        super().__init__(widget)
        self._widget = widget
        self._callback = callback
        widget.installEventFilter(self)
        widget.doubleClicked.connect(lambda index: self._activate(index.row()))

    def _activate(self, row):
        if 0 <= row < self._widget.rowCount():
            self._callback(row)

    def eventFilter(self, watched, event):
        if (watched is self._widget and event.type() == QEvent.Type.KeyPress
                and event.key() in self.KEYS and not event.isAutoRepeat()):
            if self._widget.selectedItems():
                self._activate(self._widget.currentRow())
            return True
        return False


def table(headers, *, accessible_name="", on_activate=None):
    """A read-only, single-row-selection table.

    ``on_activate(row)`` runs on double-click or Enter/Return, so the table can be
    used from the keyboard. Keep the returned table's rows mapped to your own list.
    """
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setAlternatingRowColors(True)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    widget.setTabKeyNavigation(False)
    if accessible_name:
        widget.setAccessibleName(accessible_name)
    if on_activate is not None:
        widget.row_activation = _RowActivation(widget, on_activate)
    return widget
