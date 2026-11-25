"""Shared Qt Widgets used by the EFB sections."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHeaderView, QLabel, QPushButton, QScrollArea,
    QTableWidget, QVBoxLayout, QWidget,
)

from .aircraft import aircraft_profile


def aircraft_picture(name, *, height, width):
    profile = aircraft_profile(name)
    if profile is None or not profile.drawing.is_file():
        return None
    pixmap = QPixmap(str(profile.drawing))
    if pixmap.isNull():
        return None
    widget = QLabel()
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    widget.setAccessibleName(f"Side-view drawing of {profile.name}")
    widget.setPixmap(pixmap.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation))
    return widget


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


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setAlternatingRowColors(True)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return widget
