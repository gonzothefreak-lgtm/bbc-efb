"""Flight Details: a large, resizable overlay inside the EFB window (Q3, Q11).

* Not a separate window: the overlay is a child widget covering its host (the Logbook
  workspace or a page) with a dimmed backdrop. The pages underneath are untouched,
  so search, filters, selection, view mode and scroll position survive open and close.
* A persistent header (side-on aircraft profile, route, aircraft, registration, date,
  flight time, touchdown rate and record status) stays visible above the tabs:
  Overview, Black Box (Recorder flights), Operations, Notes, Record History.
* The panel can be resized with the corner grip or the keyboard (Ctrl+Shift+arrows),
  maximised, and closed with Esc. Focus returns to where it was when it opened.
* Everything unknown says so. Nothing is estimated.
* Fields the Recorder could not be sure of are listed on the Overview with "Confirm as
  correct" (records a pilot confirmation, the value is unchanged) and "Correct…" (opens
  the editor). A suspected diversion is answered there too. ``confirm_requested`` carries
  the request to the Logbook, which writes it with ``FlightStore.confirm``.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from .aircraft_art import AircraftArtLibrary, AircraftArtWidget, shared_library
from .charts import PALETTE, Marker, Milestone, ProfileChart, TimelineStrip
from .model import (
    FIELD_LABELS, ORIGIN_LABELS, TIME_BASIS_LABELS, FlightView, JournalContents,
    evidence_timeline, event_offset_s, format_date, format_duration, format_rate, format_time,
    format_value, load_journal, resolve_journal,
)

TABS = ("overview", "black_box", "operations", "notes", "history")
TAB_TITLES = {
    "overview": "Overview", "black_box": "Black Box", "operations": "Operations",
    "notes": "Notes", "history": "Record History",
}
MIN_PANEL = QSize(560, 420)
MARGIN = 16
UNKNOWN_STYLE = "color: #8ea3b8; font-style: italic;"
CORRECTED_BY = {"pilot": "pilot", "recorder": "Recorder", "import": "import"}

JournalLoader = Callable[[FlightView], JournalContents]


JOURNAL_CACHE_SIZE = 4


def journal_loader_for(data_root: str | os.PathLike[str]) -> JournalLoader:
    """A loader that reads Recorder journals from the pilot's data folder.

    Parsed journals are cached by (journal path, modification time, size), so rebuilding
    the overlay (for example after accepting a gap) does not parse the same file again.
    A changed file is read afresh. Safe to call from a worker thread.
    """
    cache: OrderedDict[tuple, JournalContents] = OrderedDict()
    lock = threading.Lock()

    def load(view: FlightView) -> JournalContents:
        if not view.journal:
            return JournalContents(problem="This record has no journal file.")
        path = resolve_journal(data_root, view.journal)
        key = None
        if path is not None:
            try:
                stat = path.stat()
                key = (str(path), stat.st_mtime_ns, stat.st_size)
            except OSError:
                key = None
        if key is not None:
            with lock:
                if key in cache:
                    cache.move_to_end(key)
                    return cache[key]
        contents = load_journal(data_root, view.journal)
        if key is not None and not contents.problem:
            with lock:
                cache[key] = contents
                while len(cache) > JOURNAL_CACHE_SIZE:
                    cache.popitem(last=False)
        return contents

    load.cache = cache  # type: ignore[attr-defined]  # for tests and diagnostics
    return load


def _label(text: str, name: str = "", wrap: bool = False, selectable: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    return widget


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    layout.addWidget(_label(title, "cardTitle"))
    return frame, layout


def _row(name: str, value: str | None, origin: str = "", unknown: str = "Not available") -> tuple[str, str | None, str, str]:
    """One form row. ``origin`` is shown beside a known value; ``unknown`` replaces a missing one."""
    return (name, value, origin, unknown)


def _form(rows: list[tuple[str, str | None, str, str]]) -> QWidget:
    """Rows from ``_row``. Unknown values are shown muted and italic with their own wording."""
    widget = QWidget()
    grid = QGridLayout(widget)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(14)
    grid.setVerticalSpacing(5)
    for row, (name, value, origin, unknown) in enumerate(rows):
        grid.addWidget(_label(name, "muted"), row, 0, Qt.AlignmentFlag.AlignTop)
        known = value not in (None, "")
        shown = _label(value if known else unknown, wrap=True, selectable=True)
        if not known:
            shown.setStyleSheet(UNKNOWN_STYLE)
        shown.setAccessibleName(f"{name}: {shown.text()}" + (f" ({origin})" if known and origin else ""))
        grid.addWidget(shown, row, 1)
        if known and origin:
            grid.addWidget(_label(origin, "muted"), row, 2, Qt.AlignmentFlag.AlignTop)
    grid.setColumnStretch(1, 1)
    return widget


def _table(headers: list[str], rows: list[list[str]], name: str) -> QTableWidget:
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setAccessibleName(name)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.verticalHeader().hide()
    table.setWordWrap(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setTabKeyNavigation(False)
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            item = QTableWidgetItem(value)
            if value in ("—", "Not recorded", "Not measured", "Unknown", "Not available"):
                item.setForeground(QColor("#8ea3b8"))
            table.setItem(r, c, item)
    table.setMinimumHeight(min(320, 40 + 30 * max(1, len(rows))))
    return table


def _origin_note(view: FlightView, name: str) -> str:
    prov = view.provenance.get(name)
    if prov is None:
        return ""
    note = ORIGIN_LABELS.get(prov.origin, prov.origin)
    if prov.corrected:
        # Say who made the latest correction: a Recorder or import revision is not the pilot.
        who = CORRECTED_BY.get(prov.corrections[-1].by, prov.corrections[-1].by or "unknown")
        count = len(prov.corrections)
        note += f", corrected by {who}" + (f" ({count}×)" if count > 1 else "")
    if any(check.name == name for check in view.field_checks):
        note += ", needs confirmation"
    elif prov.confirmed:
        note += ", confirmed by pilot"
    return note


def _measured_note(origin: str) -> str:
    return {
        "recorder": "Measured by the Recorder", "imported": "Imported", "pilot": "Pilot entry",
    }.get(origin, origin)


def _scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    return area


class ProfileHeader(QFrame):
    """Persistent side-on aircraft profile and key facts above the tabs."""

    def __init__(self, library: AircraftArtLibrary, parent=None):
        super().__init__(parent)
        self.setObjectName("detailsHeader")
        self.art = AircraftArtWidget(library)
        self.art.setFixedSize(260, 104)
        self.art_caption = _label("", "muted", wrap=True)
        self.art_caption.setMaximumWidth(260)
        self.route = _label("", "heading", selectable=True)
        self.aircraft = _label("", selectable=True)
        self.facts = _label("", "muted", wrap=True, selectable=True)
        self.status = _label("")
        self.source = _label("", "muted")
        art_column = QVBoxLayout()
        art_column.setSpacing(2)
        art_column.addWidget(self.art)
        art_column.addWidget(self.art_caption)
        text = QVBoxLayout()
        text.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(self.route, 1)
        top.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        text.addLayout(top)
        text.addWidget(self.aircraft)
        text.addWidget(self.facts)
        text.addWidget(self.source)
        text.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(16)
        layout.addLayout(art_column)
        layout.addLayout(text, 1)

    def show_flight(self, view: FlightView) -> None:
        self.art.set_type(view.aircraft)
        self.art_caption.setText(self.art.caption)
        route = view.route
        if view.diverted:
            route += "  (diverted)"
        self.route.setText(route)
        parts = [view.aircraft or "Aircraft type unknown"]
        if view.registration:
            parts.append(view.registration)
        if view.callsign:
            parts.append(view.callsign)
        self.aircraft.setText("  |  ".join(parts))
        self.facts.setText("  |  ".join((
            format_date(view),
            f"Flight time {format_duration(view.flight_time_s, 'unknown')}",
            f"Touchdown rate {format_rate(view.touchdown_rate_fpm, 'not measured')}",
        )))
        colour = "#ffd693" if view.needs_attention else "#8fe0a6" if view.status in ("recorded", "reviewed") else "#cfe2f3"
        self.status.setText(view.status_label)
        self.status.setStyleSheet(f"color: {colour}; font-weight: 650; padding: 3px 10px;"
                                  "border: 1px solid #344b61; border-radius: 10px;")
        source = f"Source: {view.source_label}"
        if view.simulated:
            source += ". Simulated data, not native simulator telemetry."
        self.source.setText(source)
        self.setAccessibleName(f"Flight {route}, {self.aircraft.text()}, {self.facts.text()}, {view.status_label}")


class ResizeGrip(QWidget):
    """Bottom-right grip that resizes the details panel inside its host."""

    def __init__(self, overlay: "FlightDetailsOverlay"):
        super().__init__(overlay.panel)
        self.overlay = overlay
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("Drag to resize Flight Details (Ctrl+Shift+arrow keys also work)")
        self.setAccessibleName("Resize Flight Details")
        self._start: QPoint | None = None
        self._size = QSize()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.globalPosition().toPoint()
            self._size = self.overlay.panel.size()
            event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._start is not None:
            delta = event.globalPosition().toPoint() - self._start
            self.overlay.resize_panel(self._size.width() + delta.x(), self._size.height() + delta.y())
            event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._start = None
        event.accept()

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setPen(QColor("#6f8aa5"))
        for offset in (4, 8, 12):
            painter.drawLine(self.width() - 2, offset, offset, self.height() - 2)


class FlightDetailsOverlay(QWidget):
    """In-EFB Flight Details overlay. Attach once to a host widget and reuse it."""

    closed = Signal(str)
    edit_requested = Signal(str)
    #: (flight id, tuple of field names, diversion answer dict or None)
    confirm_requested = Signal(str, object, object)
    tab_changed = Signal(str)
    journal_ready = Signal(str)
    _journal_loaded = Signal(int, object, object)

    def __init__(self, host: QWidget, *, library: AircraftArtLibrary | None = None,
                 journal_loader: JournalLoader | None = None, can_edit: bool = True,
                 load_in_background: bool = False):
        super().__init__(host)
        self.host = host
        self.library = library or shared_library()
        self.journal_loader = journal_loader
        #: True: Black Box journals are parsed on a worker thread (the app); False: inline.
        self.load_in_background = load_in_background
        self._generation = 0
        self._waiting_area: QScrollArea | None = None
        self._journal_loaded.connect(self._journal_arrived, Qt.ConnectionType.QueuedConnection)
        self._can_edit = can_edit
        self._edit_reason = ""
        self._confirm_buttons: list[QPushButton] = []
        self.view: FlightView | None = None
        self._journal: JournalContents | None = None
        self._return_focus: QWidget | None = None
        self._maximised = False
        self._user_size: QSize | None = None
        self.setObjectName("detailsOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        self.panel = QFrame(self)
        self.panel.setObjectName("detailsPanel")
        self.panel.setStyleSheet(
            "QFrame#detailsPanel { background: #0f1a26; border: 1px solid #3c9bdd; border-radius: 10px; }"
            "QFrame#detailsHeader { background: #122030; border: 1px solid #283b50; border-radius: 9px; }")
        self.panel.setAccessibleName("Flight Details")
        title = _label("Flight Details", "cardTitle")
        self.maximise_button = QPushButton("Maximise")
        self.maximise_button.setAccessibleName("Maximise Flight Details")
        self.maximise_button.clicked.connect(self.toggle_maximised)
        self.close_x = QPushButton("Close")
        self.close_x.setAccessibleName("Close Flight Details")
        self.close_x.clicked.connect(self.close_overlay)
        title_row = QHBoxLayout()
        title_row.addWidget(title, 1)
        title_row.addWidget(self.maximise_button)
        title_row.addWidget(self.close_x)

        self.header = ProfileHeader(self.library)
        self.read_only_note = _label("", "warning", wrap=True)
        self.read_only_note.hide()
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.edit_button = QPushButton("Edit flight")
        self.edit_button.setObjectName("primary")
        self.edit_button.clicked.connect(self._edit)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close_overlay)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.edit_button)

        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(title_row)
        layout.addWidget(self.header)
        layout.addWidget(self.read_only_note)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(buttons)
        self.grip = ResizeGrip(self)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close_overlay,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        for key, dx, dy in ((Qt.Key.Key_Right, 40, 0), (Qt.Key.Key_Left, -40, 0),
                            (Qt.Key.Key_Down, 0, 40), (Qt.Key.Key_Up, 0, -40)):
            QShortcut(QKeySequence(Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier | key), self,
                      lambda dx=dx, dy=dy: self.resize_panel(self.panel.width() + dx, self.panel.height() + dy),
                      context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        host.installEventFilter(self)
        self.hide()

    # Geometry -------------------------------------------------------------------------

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.host and event.type() == QEvent.Type.Resize and self.isVisible():
            self._layout_panel()
        return False

    def _available(self) -> QRect:
        return self.rect().adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)

    def _layout_panel(self):
        self.setGeometry(self.host.rect())
        area = self._available()
        if self._maximised:
            self.panel.setGeometry(area)
        else:
            wanted = self._user_size or QSize(int(area.width() * 0.82), int(area.height() * 0.88))
            width = max(min(MIN_PANEL.width(), area.width()), min(wanted.width(), area.width()))
            height = max(min(MIN_PANEL.height(), area.height()), min(wanted.height(), area.height()))
            x = area.left() + (area.width() - width) // 2
            y = area.top() + (area.height() - height) // 2
            self.panel.setGeometry(x, y, width, height)
        self.grip.move(self.panel.width() - self.grip.width() - 3, self.panel.height() - self.grip.height() - 3)
        self.grip.setVisible(not self._maximised)
        self.grip.raise_()

    def resize_panel(self, width: int, height: int) -> None:
        """Resize within the host, never below the minimum readable size (Q12)."""
        area = self._available()
        width = max(min(MIN_PANEL.width(), area.width()), min(int(width), area.width()))
        height = max(min(MIN_PANEL.height(), area.height()), min(int(height), area.height()))
        self._user_size = QSize(width, height)
        self._maximised = False
        self.maximise_button.setText("Maximise")
        self._layout_panel()

    def toggle_maximised(self) -> None:
        self._maximised = not self._maximised
        self.maximise_button.setText("Restore" if self._maximised else "Maximise")
        self.maximise_button.setAccessibleName(("Restore" if self._maximised else "Maximise") + " Flight Details")
        self._layout_panel()

    @property
    def maximised(self) -> bool:
        return self._maximised

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(4, 9, 15, 165))

    def mousePressEvent(self, event):  # noqa: N802
        event.accept()  # clicks on the backdrop never reach the page underneath

    # Opening and closing --------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.isVisible() and self.view is not None

    @property
    def current_tab(self) -> str:
        widget = self.tabs.currentWidget()
        return widget.property("tab_key") if widget is not None else ""

    def open_flight(self, view: FlightView, tab: str = "overview") -> None:
        if not self.isVisible():
            focus = QApplication.focusWidget()
            self._return_focus = focus if focus is not None and not self.isAncestorOf(focus) else None
        self.view = view
        self._journal = None
        self._generation += 1
        self._build(view)
        self.select_tab(tab)
        self._layout_panel()
        self.show()
        self.raise_()
        self.tabs.setFocus(Qt.FocusReason.OtherFocusReason)

    def refresh_from(self, lookup: Callable[[str], FlightView | None]) -> None:
        """Re-show the open flight after the Logbook reloaded (or say it is gone)."""
        if self.view is None:
            return
        updated = lookup(self.view.id)
        if updated is None:
            self.read_only_note.setText("This flight is no longer in the Logbook folder. What is shown is the last copy read.")
            self.read_only_note.show()
            self.edit_button.setEnabled(False)
            self.edit_button.setToolTip("This flight is no longer in the Logbook folder.")
            return
        # The flight is (back) in the folder: drop a stale 'no longer in the folder' note and
        # restore Edit even when the record itself is unchanged.
        if updated.read_only:
            self.read_only_note.setText(updated.read_only_reason)
        self.read_only_note.setVisible(updated.read_only)
        if updated == self.view:
            self._sync_edit()
        else:
            tab = self.current_tab
            self.view = updated
            self._journal = None
            self._generation += 1
            self._build(updated)
            self.select_tab(tab)

    def close_overlay(self) -> None:
        if not self.isVisible():
            return
        flight_id = self.view.id if self.view else ""
        self.hide()
        target = self._return_focus
        self._return_focus = None
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)
        self.closed.emit(flight_id)

    def select_tab(self, key: str) -> None:
        for index in range(self.tabs.count()):
            if self.tabs.widget(index).property("tab_key") == key:
                self.tabs.setCurrentIndex(index)
                return
        self.tabs.setCurrentIndex(0)

    def tab_keys(self) -> list[str]:
        return [self.tabs.widget(index).property("tab_key") for index in range(self.tabs.count())]

    def set_can_edit(self, can_edit: bool, reason: str = "") -> None:
        self._can_edit = can_edit
        self._edit_reason = reason
        if self.view is not None:
            self._sync_edit()

    def _sync_edit(self):
        view = self.view
        allowed = self._can_edit and view is not None and not view.read_only
        self.edit_button.setEnabled(allowed)
        tip = "" if allowed else (view.read_only_reason if view and view.read_only else self._edit_reason)
        self.edit_button.setToolTip(tip)
        for button in self._confirm_buttons:
            try:
                button.setEnabled(allowed)
                button.setToolTip(button.property("help_text") if allowed else tip)
            except RuntimeError:  # deleted with an old Overview tab
                pass

    def confirm_fields(self, names, diversion=None) -> None:
        """Ask the Logbook to record a pilot confirmation of ``names`` for the open flight."""
        if self.view is not None and self.edit_button.isEnabled():
            self.confirm_requested.emit(self.view.id, tuple(names), dict(diversion) if diversion else None)

    def _edit(self):
        if self.view is not None and self.edit_button.isEnabled():
            self.edit_requested.emit(self.view.id)

    def _tab_changed(self, index):
        widget = self.tabs.widget(index)
        if widget is None:
            return
        key = widget.property("tab_key")
        if key == "black_box" and widget.property("pending"):
            self._fill_black_box(widget)
        self.tab_changed.emit(key)

    # Content --------------------------------------------------------------------------

    def _build(self, view: FlightView) -> None:
        self.header.show_flight(view)
        if view.read_only:
            self.read_only_note.setText(view.read_only_reason)
        self.read_only_note.setVisible(view.read_only)
        self._sync_edit()
        self._confirm_buttons = []
        blocker = self.tabs.blockSignals(True)
        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            widget.deleteLater()
        for key in TABS:
            if key == "black_box" and not (view.source == "recorder" or view.has_evidence):
                continue  # Q4/Q11: Black Box only where a recording exists
            page = getattr(self, f"_tab_{key}")(view)
            page.setProperty("tab_key", key)
            self.tabs.addTab(page, TAB_TITLES[key])
        self.tabs.blockSignals(blocker)
        self._sync_edit()

    def _confirm_button(self, text: str, help_text: str, action: Callable[[], None], name: str) -> QPushButton:
        button = QPushButton(text)
        button.setAccessibleName(name)
        button.setProperty("help_text", help_text)
        button.setToolTip(help_text)
        button.clicked.connect(action)
        self._confirm_buttons.append(button)
        return button

    def _check_row(self, view: FlightView, check) -> QWidget:
        """One field needing confirmation: its text and the pilot's choices."""
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
        layout.addWidget(_label(check.text, "warning", wrap=True, selectable=True))
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        label = FIELD_LABELS.get(check.name, check.name)
        planned = view.planned_destination if check.name == "arrival" else ""
        if planned:
            arrival = view.arrival or "the recorded airport"
            buttons.addWidget(self._confirm_button(
                "Confirm diversion",
                f"Records that this flight diverted to {arrival} instead of {planned}. The To value is not changed.",
                lambda: self.confirm_fields(("arrival",), {
                    "confirmed": True, "planned_arrival": planned, "arrival": view.arrival or None,
                    "reason": "Confirmed by the pilot in Flight Details."}),
                f"Confirm diversion to {arrival}"))
            buttons.addWidget(self._confirm_button(
                "Not a diversion",
                f"Records that {arrival} is the correct arrival and the flight was not a diversion from {planned}.",
                lambda: self.confirm_fields(("arrival",), {
                    "confirmed": False, "planned_arrival": planned, "arrival": view.arrival or None,
                    "reason": "The pilot confirmed the arrival; not a diversion."}),
                f"Confirm {arrival} as the arrival, not a diversion"))
        elif check.name in view.provenance and (view.provenance[check.name].value not in ("", None)):
            buttons.addWidget(self._confirm_button(
                "Confirm as correct",
                f"Records that you checked {label} and it is correct. The value is not changed; the "
                "confirmation is kept in Record History.",
                lambda _checked=False, name=check.name: self.confirm_fields((name,)),
                f"Confirm {label} as correct"))
        buttons.addWidget(self._confirm_button(
            "Correct…", f"Open the editor to correct or fill in {label}.", self._edit, f"Correct {label}"))
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return row

    def _tab_overview(self, view: FlightView) -> QWidget:
        body = QWidget()
        layout = QGridLayout(body)
        layout.setSpacing(12)
        summary, s_layout = _card("Flight summary")
        rows = [
            _row("Aircraft", view.aircraft, _origin_note(view, "aircraft"), "Unknown"),
            _row("Registration", view.registration, _origin_note(view, "registration"), "Not recorded"),
            _row("Callsign", view.callsign, _origin_note(view, "callsign"), "Not recorded"),
            _row("Operation", view.operation_label if view.operation else None, "", "Not set"),
            _row("Date", format_date(view) if view.flight_date else view.flight_date_text or None,
                 _origin_note(view, "flight_date"), "Unknown"),
            _row("From", view.departure, _origin_note(view, "departure"), "Unknown"),
            _row("To", view.arrival, _origin_note(view, "arrival"), "Unknown"),
        ]
        if view.diverted:
            planned = view.diversion.get("planned_arrival")
            reason = view.diversion.get("reason")
            rows.append(_row("Planned destination", planned if isinstance(planned, str) else None, "", "Not recorded"))
            rows.append(_row("Diversion reason", reason if isinstance(reason, str) else None, "", "Not recorded"))
        rows += [
            _row("Flight time", format_duration(view.flight_time_s, "") or None,
                 _origin_note(view, "minutes") if view.minutes is not None else "Recorder block time", "Unknown"),
            _row("Distance", f"{view.distance_nm:,.0f} nm" if view.distance_nm is not None else None,
                 _measured_note(view.distance_origin), "Not recorded"),
            _row("Landings", str(view.landings) if view.landings is not None else None,
                 _measured_note(view.landings_origin), "Not recorded"),
            _row("Touchdown rate", format_rate(view.touchdown_rate_fpm, "") or None,
                 _measured_note(view.touchdown_rate_origin), "Not measured"),
            _row("Source", view.source_label),
            _row("Record status", view.status_label),
        ]
        s_layout.addWidget(_form(rows))
        layout.addWidget(summary, 0, 0, 2, 1)

        times, t_layout = _card(f"Flight event times ({TIME_BASIS_LABELS.get(view.time_basis, view.time_basis)})"
                                if view.has_evidence else "Flight event times")
        if view.has_evidence:
            t_layout.addWidget(_form([
                _row("Off-block", format_time(view.off_block, "") or None, "", "Not recorded"),
                _row("Takeoff", format_time(view.takeoff, "") or None, "", "Not recorded"),
                _row("Touchdown", format_time(view.landing, "") or None, "", "Not recorded"),
                _row("On-block", format_time(view.on_block, "") or None, "", "Not recorded"),
                _row("Airborne time", format_duration(view.airborne_s, "") or None, "", "Not measured"),
                _row("Block time", format_duration(view.block_s, "") or None, "", "Not measured"),
            ]))
            strip = TimelineStrip()
            strip.set_data([
                Milestone("OFF", format_time(view.off_block, "—"), view.off_block is not None),
                Milestone("TO", format_time(view.takeoff, "—"), view.takeoff is not None),
                Milestone("AIRBORNE", format_duration(view.airborne_s, "—"), view.airborne_s is not None),
                Milestone("TD", format_time(view.landing, "—"), view.landing is not None),
                Milestone("ON", format_time(view.on_block, "—"), view.on_block is not None),
            ])
            t_layout.addWidget(strip)
        else:
            message = ("Event times are recorded only by Flight Logging. This is a manual entry."
                       if view.source == "manual" else
                       "The imported record does not include flight event times.")
            t_layout.addWidget(_label(message, "muted", wrap=True))
        layout.addWidget(times, 0, 1)

        if view.problems or view.summary or view.field_checks:
            notes, n_layout = _card("Record notes")
            if view.field_checks:
                n_layout.addWidget(_label(
                    "Needs confirmation: the Recorder could not be sure of these fields. Check each one, "
                    "then confirm it as correct or correct it.", "warning", wrap=True))
                for check in view.field_checks:
                    n_layout.addWidget(self._check_row(view, check))
            if view.summary:
                n_layout.addWidget(_label(view.summary, wrap=True, selectable=True))
            for problem in view.problems:
                n_layout.addWidget(_label(problem, "warning", wrap=True))
            layout.addWidget(notes, 1, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(2, 1)
        return _scroll(body)

    def _recording_card(self, view: FlightView) -> QFrame:
        info, i_layout = _card("Recording")
        if view.simulated:
            i_layout.addWidget(_label("Simulated data. This recording came from the simulated provider, "
                                      "not from Microsoft Flight Simulator.", "warning", wrap=True))
        if not view.has_evidence:
            i_layout.addWidget(_label("The Recorder evidence block is missing from this record, so no "
                                      "measured times, gaps or touchdowns can be shown.", "warning", wrap=True))
        i_layout.addWidget(_form([
            _row("Summary", view.summary, "", "No summary was saved"),
            _row("Time basis", TIME_BASIS_LABELS.get(view.time_basis, view.time_basis) if view.time_basis else None,
                 "", "Not recorded"),
            _row("Recorder rules", view.rules_version, "", "Not recorded"),
            _row("Journal", view.journal, "", "No journal file"),
            _row("Recorder state", view.recorder_state.capitalize() if view.recorder_state else None, "", "Not recorded"),
        ]))
        return info

    def _tab_black_box(self, view: FlightView) -> QWidget:
        """Placeholder; the journal is read only when the pilot opens this tab."""
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(12)
        layout.addWidget(self._recording_card(view))
        layout.addWidget(_label("Loading the recording\u2026", "muted"))
        layout.addStretch(1)
        area = _scroll(body)
        area.setProperty("pending", True)
        return area

    def journal(self) -> JournalContents | None:
        return self._journal

    @staticmethod
    def _read_journal(loader: JournalLoader, view: FlightView) -> JournalContents:
        try:
            return loader(view)
        except Exception as error:  # a viewer must never take the EFB down
            return JournalContents(problem=f"The journal could not be read ({error}). The saved summary is shown.")

    def _fill_black_box(self, area: QScrollArea) -> None:
        area.setProperty("pending", False)
        view = self.view
        if view is None:
            return
        loader = self.journal_loader
        if loader is not None and view.journal:
            if self.load_in_background:
                # Parse off the UI thread; the "Loading the recording…" placeholder stays
                # until the result arrives through a queued signal.
                generation = self._generation
                self._waiting_area = area

                def work():
                    contents = self._read_journal(loader, view)
                    try:
                        self._journal_loaded.emit(generation, view, contents)
                    except RuntimeError:  # the overlay was deleted meanwhile
                        pass

                threading.Thread(target=work, name="bbc-journal-loader", daemon=True).start()
                return
            contents = self._read_journal(loader, view)
        elif not view.journal:
            contents = JournalContents(problem="This record has no journal file. The saved summary is shown.")
        else:
            contents = JournalContents(problem="The journal is not opened in this build; the saved summary is shown.")
        self._render_black_box(area, view, contents)

    def _journal_arrived(self, generation: int, view: FlightView, contents: JournalContents) -> None:
        area = self._waiting_area
        if generation != self._generation or view is not self.view or area is None:
            return  # another flight or rebuild since; its own load is under way
        if not any(self.tabs.widget(i) is area for i in range(self.tabs.count())):
            return
        self._waiting_area = None
        self._render_black_box(area, view, contents)

    def _render_black_box(self, area: QScrollArea, view: FlightView, contents: JournalContents) -> None:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(12)
        layout.addWidget(self._recording_card(view))
        self._journal = contents
        events = list(contents.events) or list(evidence_timeline(view))
        if contents.problem:
            layout.addWidget(_label(contents.problem, "muted", wrap=True))
        if contents.torn_lines:
            layout.addWidget(_label(f"{contents.torn_lines} journal line(s) could not be read and were skipped "
                                    "(for example an unfinished last line after an interruption).", "warning", wrap=True))
        profile_card, p_layout = _card("Flight profile")
        chart = ProfileChart()
        markers, gaps = [], []
        colours = {"takeoff": PALETTE[1], "touchdown": PALETTE[2], "bounce": PALETTE[5], "block_off": PALETTE[4],
                   "block_on": PALETTE[4], "paused": PALETTE[7], "gap": PALETTE[5]}
        gap_start = None
        for event in contents.events:
            offset = event_offset_s(event, contents)
            if offset is None:
                continue
            if event.kind in colours and event.kind != "gap":
                markers.append(Marker(offset, event.label, colours[event.kind]))
            if event.kind == "gap":
                gap_start = offset
            elif gap_start is not None and event.kind in ("resumed", "flight_ended", "phase_changed"):
                gaps.append((gap_start, offset))
                gap_start = None
        chart.set_data([(point.seconds, point.altitude_ft) for point in contents.profile], markers, gaps)
        p_layout.addWidget(chart)
        layout.addWidget(profile_card)

        events_card, e_layout = _card(f"Event timeline ({TIME_BASIS_LABELS.get(view.time_basis, 'time basis unknown')})")
        if events:
            e_layout.addWidget(_table(
                ["Time", "Event", "Phase", "Detail"],
                [[format_time(event.time, "—"), event.label, event.phase.replace("_", " ") or "—",
                  event.detail or "—"] for event in events],
                "Black Box event timeline"))
        else:
            e_layout.addWidget(_label("No flight events were recorded.", "muted"))
        layout.addWidget(events_card)

        gaps_card, g_layout = _card("Data gaps")
        if view.gaps:
            g_layout.addWidget(_label("Accepting a gap records that you reviewed it. The gap and the missing "
                                      "data stay in the record.", "muted", wrap=True))
            rows = []
            for gap in view.gaps:
                duration = gap.duration_s
                review = (f"Accepted {format_time(gap.acknowledged_at, '')} {gap.acknowledged_at[:10]}".strip()
                          + (f": {gap.note}" if gap.note else "")) if gap.acknowledged else "Needs review"
                rows.append([str(gap.index + 1), gap.reason, format_time(gap.start, "—"), format_time(gap.end, "—"),
                             format_duration(duration, "—"), review])
            g_layout.addWidget(_table(["#", "Reason", "From", "To", "Length", "Review"], rows, "Recorder data gaps"))
        else:
            g_layout.addWidget(_label("No data gaps were recorded.", "muted"))
        layout.addWidget(gaps_card)

        touch_card, td_layout = _card("Touchdowns")
        touchdowns = list(contents.touchdowns) or list(view.touchdowns)
        if touchdowns:
            td_layout.addWidget(_table(
                ["Time", "Type", "Rate"],
                [[format_time(item.time, "—"), "Bounce" if item.bounce else "Touchdown",
                  format_rate(item.rate_fpm)] for item in touchdowns],
                "Touchdowns"))
        elif view.landing or view.touchdown_rate_fpm is not None:
            td_layout.addWidget(_form([
                _row("Touchdown", format_time(view.landing, "") or None, "", "Not recorded"),
                _row("Rate", format_rate(view.touchdown_rate_fpm, "") or None,
                     _measured_note(view.touchdown_rate_origin), "Not measured"),
            ]))
        else:
            td_layout.addWidget(_label("No touchdown was recorded.", "muted"))
        layout.addWidget(touch_card)
        layout.addStretch(1)
        area.setWidget(body)  # replacing the widget lets the scroll area size the new content
        self.journal_ready.emit(view.id)

    def _tab_operations(self, view: FlightView) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        card, c_layout = _card("Crew, passengers, baggage and freight")
        payload = view.payload
        c_layout.addWidget(_form([
            _row("Crew", format_value(view.crew, unknown="") or None, _origin_note(view, "crew"), "Not entered"),
            _row("Passengers", format_value(view.passengers, unknown="") or None,
                 _origin_note(view, "passengers"), "Not entered"),
            _row("Baggage", format_value(view.baggage_kg, "kg", unknown="") or None,
                 _origin_note(view, "baggage_kg"), "Not entered"),
            _row("Freight", format_value(view.freight_kg, "kg", unknown="") or None,
                 _origin_note(view, "freight_kg"), "Not entered"),
            _row("Freight description", view.freight_description, "", "Not entered"),
            _row("Total payload", None if payload.label == "Unknown" else payload.label, "", "Unknown"),
        ]))
        c_layout.addWidget(_label("Total payload adds baggage and freight only. No standard passenger or crew "
                                  "mass is assumed, and a partial sum is labelled incomplete.", "muted", wrap=True))
        layout.addWidget(card)
        layout.addStretch(1)
        return _scroll(body)

    def _tab_notes(self, view: FlightView) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        notes = QPlainTextEdit()
        notes.setReadOnly(True)
        notes.setAccessibleName("Flight notes")
        notes.setPlainText(view.notes or "")
        notes.setPlaceholderText("No notes for this flight. Use Edit flight to add some.")
        layout.addWidget(notes, 1)
        origin = _origin_note(view, "notes")
        if view.notes and origin:
            layout.addWidget(_label(origin, "muted"))
        return body

    def _tab_history(self, view: FlightView) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(12)
        origin_card, o_layout = _card("Where this record came from")
        rows = [
            _row("Created", f"{view.created_at[:19].replace('T', ' ')} UTC" if view.created_at else None, "", "Unknown"),
            _row("Created by", view.revisions[0].by_label if view.revisions else None, "", "Unknown"),
            _row("Source", view.source_label),
            _row("Record format", str(view.schema)),
        ]
        if view.importer or view.import_reference:
            rows.append(_row("Imported with", view.importer, "", "Not recorded"))
            rows.append(_row("Import reference", view.import_reference, "", "Not recorded"))
        if view.migrated_from_schema is not None:
            rows.append(_row("Converted from", f"record format {view.migrated_from_schema} (original copy kept)"))
        o_layout.addWidget(_form(rows))
        for problem in view.problems:
            o_layout.addWidget(_label(problem, "warning", wrap=True))
        layout.addWidget(origin_card)

        prov_card, p_layout = _card("Original values and corrections")
        prov_rows = []
        for name, prov in view.provenance.items():
            if name == "notes":
                original = "(text)" if prov.original else "—"
                current = "(text)" if prov.value else "—"
            else:
                original = format_value(prov.original, unknown="—")
                current = format_value(prov.value, unknown="—")
            prov_rows.append([FIELD_LABELS.get(name, name), prov.origin_label, original, current,
                              f"{len(prov.corrections)}" if prov.corrected else "None",
                              "Yes" if prov.confirmed else ("Needs confirmation" if any(
                                  check.name == name for check in view.field_checks) else "—")])
        p_layout.addWidget(_table(["Field", "Origin", "Original", "Current", "Corrections", "Pilot confirmed"],
                                  prov_rows, "Field provenance"))
        layout.addWidget(prov_card)

        rev_card, r_layout = _card("Saved versions")
        rev_rows = []
        for revision in view.revisions:
            changes = "; ".join(
                f"{FIELD_LABELS.get(c.name, c.name)}: {format_value(c.previous, unknown='blank')} → "
                f"{format_value(c.value, unknown='blank')}" if c.name != "notes" else "Notes changed"
                for c in revision.changes)
            if revision.confirmed:
                confirmed = "Confirmed as correct: " + ", ".join(FIELD_LABELS.get(n, n) for n in revision.confirmed)
                changes = f"{changes}; {confirmed}" if changes else confirmed
            changes = changes or ("Original values" if revision.index == 0 else "No field changes")
            rev_rows.append([str(revision.index + 1), revision.saved_at[:19].replace("T", " ") or "—",
                             revision.by_label, revision.reason or "—", changes])
        r_layout.addWidget(_table(["#", "Saved (UTC)", "By", "Reason", "Changes"], rev_rows, "Saved versions"))
        layout.addWidget(rev_card)

        if view.gaps:
            ack_card, a_layout = _card("Gap reviews")
            ack_rows = [[str(gap.index + 1), gap.reason,
                         (gap.acknowledged_at[:19].replace("T", " ") if gap.acknowledged else "Not reviewed"),
                         gap.acknowledged_by.capitalize() if gap.acknowledged else "—", gap.note or "—"]
                        for gap in view.gaps]
            a_layout.addWidget(_table(["Gap", "Reason", "Accepted (UTC)", "By", "Note"], ack_rows, "Gap reviews"))
            layout.addWidget(ack_card)
        layout.addStretch(1)
        return _scroll(body)


__all__ = ["FlightDetailsOverlay", "ProfileHeader", "TABS", "journal_loader_for"]
