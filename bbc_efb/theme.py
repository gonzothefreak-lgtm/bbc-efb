"""Dark EFB theme: a QPalette for every widget and popup, plus a stylesheet for accents.

Call ``apply_theme(app)`` once after creating the QApplication. Font sizes are in
points relative to the application's own font, so Windows display scaling and the
"Text size" accessibility setting carry through. ``STYLE`` is the stylesheet at the
default 10 pt base, kept for callers that only set a stylesheet.
"""

from PySide6.QtGui import QColor, QPalette

TEXT = "#e7eef7"
TEXT_DISABLED = "#7d8fa3"
WINDOW = "#0d151f"
BASE = "#0c1723"
BASE_DISABLED = "#111b26"
ALTERNATE = "#162333"
PANEL = "#142130"
BUTTON = "#192a3c"
HIGHLIGHT = "#175b8f"
LINK = "#80c6ff"
BORDER = "#344b61"
BORDER_DISABLED = "#223247"


def style(base_pt=10.0):
    """Return the stylesheet with sizes scaled from ``base_pt`` (the app font size in points)."""
    def pt(scale):
        return f"{max(6.0, base_pt * scale):.1f}pt"

    return f"""
* {{ font-family: "Segoe UI"; font-size: {pt(1.0)}; color: {TEXT}; }}
*:disabled {{ color: {TEXT_DISABLED}; }}
QMainWindow, QWidget#surface, QWidget#page {{ background: {WINDOW}; }}
QFrame#top {{ background: #101c29; border-bottom: 1px solid #24374a; }}
QFrame#footer {{ background: #0a121b; border-top: 1px solid #2a4055; }}
QFrame#sidebar {{ background: #101b28; border: 1px solid #243548; border-radius: 9px; }}
QFrame#card {{ background: {PANEL}; border: 1px solid #283b50; border-radius: 9px; }}
QLabel#brand {{ font-size: {pt(1.45)}; font-weight: 700; color: #f1f7ff; }}
QLabel#brandMark {{ background: #10558a; color: #e9f6ff; border-radius: 6px;
                    font-size: {pt(0.85)}; font-weight: 700; }}
QLabel#heading {{ font-size: {pt(2.05)}; font-weight: 650; color: #f1f7ff; }}
QLabel#cardTitle {{ font-size: {pt(1.15)}; font-weight: 650; }}
QLabel#metric {{ font-size: {pt(1.9)}; font-weight: 650; color: {LINK}; }}
QLabel#muted {{ color: #a4b7cb; }}
QLabel#warning {{ color: #ffd693; }}
QPushButton {{ background: {BUTTON}; border: 1px solid {BORDER}; border-radius: 6px;
              padding: 7px 11px; font-weight: 600; }}
QPushButton:hover {{ background: #253d53; }}
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus,
QDateEdit:focus, QSpinBox:focus, QTableWidget:focus {{ border: 1px solid #86caff; }}
QPushButton:disabled {{ color: {TEXT_DISABLED}; background: #14202b; border-color: {BORDER_DISABLED}; }}
QPushButton#primary, QPushButton:checked {{ background: {HIGHLIGHT}; border-color: #3285bf; }}
QPushButton#primary:hover, QPushButton:checked:hover {{ background: #206da6; }}
QPushButton#primary:disabled {{ color: {TEXT_DISABLED}; background: #14202b; border-color: {BORDER_DISABLED}; }}
QPushButton#window, QPushButton#close {{ background: transparent; border-color: transparent;
                                       font-size: {pt(1.15)}; padding: 3px; }}
QPushButton#window:hover {{ background: #293d50; }}
QPushButton#close:hover {{ background: #993542; }}
QPushButton#status {{ background: transparent; border-color: transparent; color: #b6c8da;
                      padding: 4px 8px; }}
QPushButton#status:hover {{ background: #1d2b38; }}
QLineEdit, QDateEdit, QSpinBox, QPlainTextEdit, QComboBox, QListWidget {{
    background: {BASE}; border: 1px solid {BORDER}; border-radius: 5px; padding: 6px;
    selection-background-color: {HIGHLIGHT}; }}
QLineEdit:disabled, QDateEdit:disabled, QSpinBox:disabled, QPlainTextEdit:disabled,
QComboBox:disabled, QListWidget:disabled {{
    color: {TEXT_DISABLED}; background: {BASE_DISABLED}; border-color: {BORDER_DISABLED}; }}
QLineEdit:read-only, QPlainTextEdit[readOnly="true"] {{ background: {BASE_DISABLED}; }}
QTableWidget {{ background: #101a26; alternate-background-color: {ALTERNATE};
               border: 1px solid #2a4055; border-radius: 5px; gridline-color: #24374a;
               selection-background-color: #1a5682; }}
QTableWidget::item {{ padding: 4px; }}
QTableWidget:disabled {{ color: {TEXT_DISABLED}; }}
QHeaderView::section {{ background: #1d3043; color: #deebf6; border: none;
                       padding: 7px; font-weight: 650; }}
QScrollArea {{ border: none; background: transparent; }}
QProgressBar {{ min-height: 8px; max-height: 8px; border: none;
               border-radius: 4px; background: #22364a; }}
QProgressBar::chunk {{ background: #3c9bdd; border-radius: 4px; }}
QMessageBox, QFileDialog, QCalendarWidget, QMenu {{ background: {PANEL}; }}
QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: #1d3043; }}
QCalendarWidget QToolButton {{ background: transparent; color: {TEXT}; border: none; padding: 4px 8px; }}
QCalendarWidget QToolButton:hover {{ background: #253d53; }}
QCalendarWidget QMenu {{ background: {PANEL}; color: {TEXT}; }}
QCalendarWidget QSpinBox {{ background: {BASE}; color: {TEXT}; }}
QCalendarWidget QAbstractItemView, QComboBox QAbstractItemView, QListView, QTreeView {{
    background: {BASE}; color: {TEXT}; alternate-background-color: {ALTERNATE};
    selection-background-color: {HIGHLIGHT}; selection-color: #ffffff; outline: 0; }}
QCalendarWidget QAbstractItemView:disabled {{ color: {TEXT_DISABLED}; }}
QMenu::item:selected {{ background: {HIGHLIGHT}; }}
QMenu::item:disabled {{ color: {TEXT_DISABLED}; }}
QToolTip {{ background: #15293c; color: #edf6ff; border: 1px solid #48718f; }}
"""


STYLE = style()


def palette():
    """A dark palette, including the Disabled group, for widgets the stylesheet does not reach."""
    result = QPalette()
    roles = {
        QPalette.ColorRole.Window: WINDOW, QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: BASE, QPalette.ColorRole.AlternateBase: ALTERNATE,
        QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.Button: BUTTON,
        QPalette.ColorRole.ButtonText: TEXT, QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Highlight: HIGHLIGHT, QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: "#15293c", QPalette.ColorRole.ToolTipText: "#edf6ff",
        QPalette.ColorRole.PlaceholderText: "#8196ab", QPalette.ColorRole.Link: LINK,
    }
    for role, colour in roles.items():
        result.setColor(role, QColor(colour))
    disabled = QPalette.ColorGroup.Disabled
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.HighlightedText):
        result.setColor(disabled, role, QColor(TEXT_DISABLED))
    result.setColor(disabled, QPalette.ColorRole.Base, QColor(BASE_DISABLED))
    result.setColor(disabled, QPalette.ColorRole.Button, QColor("#14202b"))
    result.setColor(disabled, QPalette.ColorRole.Highlight, QColor("#2a3f52"))
    return result


def apply_theme(app):
    """Apply the palette and a stylesheet sized from the app's current font."""
    app.setPalette(palette())
    size = app.font().pointSizeF()
    app.setStyleSheet(style(size if size > 0 else 10.0))
