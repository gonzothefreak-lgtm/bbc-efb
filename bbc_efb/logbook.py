"""The Logbook section: the approved Q1-Q14 pages plus the unified Flight Editor.

The pages (Dashboard, Flight History in Table, Cards or Combined view, the in-EFB
Flight Details overlay, Black Box Recorder Management and Statistics) live in
``bbc_efb.logbook_views`` and read storage v2 records through
``logbook_views.model``. This module adds what only the Logbook section does:

* the single editor for manual entry and pilot corrections (``FlightEditor``);
* saving through the app's one ``FlightStore`` (contract C1): the Logbook never opens
  or closes a store itself. The shell passes the store with ``set_store`` and handles
  ``data_folder_requested`` (Settings > Data Management / ``DataFolderControl``);
* conflict handling that never creates a duplicate flight to get round a read-only
  or unreadable record;
* the live Flight Logging status on the Black Box page (``set_recorder_status``),
  fed by the Recorder only.
"""

from __future__ import annotations

from datetime import date
import logging
from types import SimpleNamespace

from PySide6.QtCore import SIGNAL, QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDateEdit, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .logbook_views.dashboard import LogbookWorkspace
from .logbook_views.details import journal_loader_for
from .logbook_views.history import VIEW_MODES
from .logbook_views.model import ViewError, flight_view
from .logbook_views.recorder_management import StaleListing, StoreActions
from .storage import (
    FIELD_LABELS, FIELD_NAMES, NUMBER_LIMITS, TEXT_LIMITS, FlightFields, RecordConflict,
    RecordMissing, RecordReadOnly, UnreadableRecord,
)
from .storage_settings import data_folder_notes
from .widgets import button, card, label, scroll

log = logging.getLogger(__name__)

DATE_MIN = QDate(100, 1, 1)
DATE_MAX = QDate(9999, 12, 31)
#: Date shown (as "Stored: …") when the stored flight date cannot be shown in the picker.
#: QDateEdit cannot go below 100-01-01, so the picker's lowest date carries the special text
#: only while such a record is open.
STORED_DATE = DATE_MIN
#: Largest value a QSpinBox can hold (signed 32-bit).
SPIN_MAX = 2**31 - 1
MAX_LISTED_PROBLEMS = 5
_SAME = object()

#: Old page names (round 1) mapped to the workspace pages.
PAGE_ALIASES = {
    "Dashboard": "dashboard", "Flight History": "history", "Black Box Recorder": "black_box",
    "Black Box": "black_box", "Statistics": "statistics",
}
VIEW_PREFERENCE = "logbook.history_view"
COLUMNS_PREFERENCE = "logbook.hidden_columns"


def describe_source(flight):
    """Short honest label for where a flight record came from."""
    if flight.source == "recorder":
        return "Recorder (simulated)" if flight.simulated else "Recorder"
    if flight.source == "imported":
        return "Imported"
    return "Manual"


def _field_list(names):
    return ", ".join(FIELD_LABELS.get(name, name).lower() for name in names)


class FlightEditor(QWidget):
    """The one editor for new manual flights and for corrections to any flight.

    Loading never changes a value. Each field remembers what it showed, the exact stored
    value and whether the widget could show that value exactly. ``values()`` returns the
    stored value while the field still shows what was loaded, unless the stored value
    could not be shown and the pilot has touched the field: then what the pilot sees is
    what is saved (so picking today's date replaces an unshowable stored date).
    """

    TEXT = (
        ("aircraft", "&Aircraft type"), ("departure", "&From"), ("arrival", "&To"),
        ("callsign", "&Callsign"), ("registration", "&Registration"),
    )
    NUMBERS = (
        ("minutes", "D&uration", " min"), ("crew", "Cre&w", ""), ("passengers", "Pa&ssengers", ""),
        ("baggage_kg", "&Baggage", " kg"), ("freight_kg", "Frei&ght", " kg"),
    )

    def __init__(self, save, cancel):
        super().__init__()
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        self.heading = label("", "heading")
        header.addWidget(self.heading, 1)
        header.addWidget(button("Cancel", cancel))
        self.save_button = button("Save flight", save, "primary")
        header.addWidget(self.save_button)
        outer.addLayout(header)
        self.notice = label("", "warning", wrap=True)
        self.notice.hide()
        outer.addWidget(self.notice)

        form_card, contents = card(
            "Flight record",
            "Aircraft type, From, To and flight date are required. Crew, passengers, baggage "
            "and freight are optional; leave them at Unknown when you do not know them.",
        )
        form = QFormLayout()
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("dd MMM yyyy")
        self.date.setDateRange(DATE_MIN, DATE_MAX)
        form.addRow("Flight &date", self.date)
        self.text_fields = {}
        for name, caption in self.TEXT:
            field = QLineEdit()
            field.setMaxLength(TEXT_LIMITS[name])
            self.text_fields[name] = field
            form.addRow(caption, field)
        self.number_fields = {}
        for name, caption, suffix in self.NUMBERS:
            spin = QSpinBox()
            spin.setRange(-1, NUMBER_LIMITS[name])
            spin.setSpecialValueText("Unknown")
            spin.setSuffix(suffix)
            spin.setAccessibleName(f"{FIELD_LABELS[name]}, Unknown when cleared to the lowest value")
            self.number_fields[name] = spin
            form.addRow(caption, spin)
        self.minutes = self.number_fields["minutes"]
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(94)
        form.addRow("&Notes", self.notes)
        self.reason = QLineEdit()
        self.reason.setMaxLength(500)
        self.reason.setPlaceholderText("Optional, kept in Record History")
        form.addRow("Reason for c&hange", self.reason)
        self.form = form
        contents.addLayout(form)
        outer.addWidget(form_card)
        outer.addStretch()
        self.original = None
        self._loaded = {}
        self._touched = set()
        self._loading = False
        self._notices = []
        self.save_block = ""

        self.date.dateChanged.connect(lambda *_: self._touch("flight_date"))
        for name, field in self.text_fields.items():
            field.textChanged.connect(lambda *_, key=name: self._touch(key))
        for name, spin in self.number_fields.items():
            spin.valueChanged.connect(lambda *_, key=name: self._touch(key))
        self.notes.textChanged.connect(lambda: self._touch("notes"))

    def _touch(self, name):
        if not self._loading:
            self._touched.add(name)

    # Loading

    def open(self, flight, pending=None):
        """Show ``flight`` (None: a new manual flight). ``pending`` re-applies the pilot's
        edits after a conflict. Returns the names of pending edits that could not be shown
        exactly (and so are not in the editor)."""
        if flight is None:
            self.heading.setText("New manual flight")
        else:
            self.heading.setText("Correct recorded flight" if flight.source == "recorder"
                                 else "Correct imported flight" if flight.source == "imported"
                                 else "Edit flight")
        current = flight.fields if flight else None
        notices = []
        self._loaded = {}
        self._touched = set()
        self.original = None
        self.save_block = ""
        # Saving stays off until the whole flight has loaded, so a load that fails part-way
        # can never be saved over the stored record.
        self.save_button.setEnabled(False)
        self._loading = True
        try:
            self.date.setSpecialValueText("")
            self.date.setDateRange(DATE_MIN, DATE_MAX)
            if current is None:
                self.date.setDate(QDate.currentDate())
            else:
                stored = current.flight_date
                shown = QDate.fromString(stored, Qt.DateFormat.ISODate) if isinstance(stored, str) else QDate()
                if shown.isValid() and DATE_MIN <= shown <= DATE_MAX:
                    self.date.setDate(shown)
                else:
                    # Show the stored text itself; any real date the pilot picks replaces it.
                    self.date.setSpecialValueText(f"Stored: {stored}")
                    self.date.setDate(STORED_DATE)
                    notices.append(f"The stored flight date {stored!r} cannot be shown in the date "
                                   "picker. It is kept as stored unless you pick a new date.")
                self._loaded["flight_date"] = (self.date.date(), stored, True)

            for name, field in self.text_fields.items():
                value = getattr(current, name) if current else ""
                field.setMaxLength(max(TEXT_LIMITS[name], len(value)))
                field.setText(value)
                if current is not None:
                    exact = field.text() == value
                    if not exact:
                        notices.append(f"{FIELD_LABELS[name]} cannot be shown exactly. It is kept as "
                                       "stored unless you change it.")
                    self._loaded[name] = (field.text(), value, exact)
                if len(value) > TEXT_LIMITS[name]:
                    notices.append(f"{FIELD_LABELS[name]} is longer than {TEXT_LIMITS[name]:,} characters. "
                                   "It is kept as stored; a changed value must fit the limit.")

            for name, spin in self.number_fields.items():
                value = getattr(current, name) if current else None
                limit = NUMBER_LIMITS[name]
                spin.setRange(-1, limit)
                # QSpinBox holds a signed 32-bit int: a stored value outside that range (or a
                # negative one) is shown as Unknown and kept as stored unless the pilot edits it.
                fits = type(value) is int and 0 <= value <= SPIN_MAX
                if fits and value > limit:
                    spin.setMaximum(value)  # show the stored value exactly instead of clamping it
                spin.setValue(value if fits else -1)
                if current is not None:
                    exact = (spin.value() == -1) if value is None else (value >= 0 and spin.value() == value)
                    if value is not None and not 0 <= value <= limit:
                        notices.append(
                            f"The stored {FIELD_LABELS[name].lower()} {value!r} is outside the range this "
                            "version accepts. It is kept as stored unless you change it"
                            + ("." if exact else "; it is shown as Unknown here."))
                    self._loaded[name] = (spin.value(), value, exact)

            notes = current.notes if current else ""
            self.notes.setPlainText(notes)
            if current is not None:
                self._loaded["notes"] = (self.notes.toPlainText(), notes, self.notes.toPlainText() == notes)
        finally:
            self._loading = False

        if flight is not None and getattr(flight, "evidence", None) is not None:
            try:
                checks = flight_view(flight).field_checks
            except Exception:  # the notice is a help; never block the editor on it
                checks = ()
            if checks:
                notices.append("Needs confirmation: the Recorder could not be sure of these fields. "
                               + " ".join(check.text for check in checks)
                               + " Correct or fill them in if they are wrong or empty. If they are right, "
                               "use Confirm as correct in Flight Details instead.")
        self.reason.clear()
        self.form.setRowVisible(self.reason, flight is not None)
        self.original = self.values()
        if current is not None and self.original != current:
            log.error("Editor did not reproduce stored values for flight %s", flight.id)
            notices.append("Some stored values could not be loaded exactly. Check the flight before saving.")
        not_applied = []
        if pending:
            for name, value in pending.items():
                if not self.show_value(name, value):
                    not_applied.append(name)
            applied = [name for name in pending if name not in not_applied]
            if applied:
                notices.append("This flight was changed elsewhere. The editor shows the saved flight with "
                               f"your edits applied on top ({_field_list(applied)}).")
            if not_applied:
                notices.append("These edits could not be shown in the editor and are NOT applied: "
                               + "; ".join(f"{FIELD_LABELS.get(n, n).lower()} {pending[n]!r}" for n in not_applied)
                               + ". Enter them again if you still want them.")
        self._set_notices(notices)
        self.save_button.setEnabled(True)
        self.save_button.setToolTip("")
        self.text_fields["aircraft"].setFocus()
        return not_applied

    def show_value(self, name, value):
        """Put ``value`` in the field as the pilot's edit. Returns False when it cannot be
        shown exactly (then the field is left as it was)."""
        shown_ok = False
        if name == "flight_date":
            shown = QDate.fromString(value, Qt.DateFormat.ISODate) if isinstance(value, str) else QDate()
            if shown.isValid() and DATE_MIN <= shown <= DATE_MAX:
                self.date.setDate(shown)
                shown_ok = self.date.date() == shown
        elif name in self.text_fields:
            if isinstance(value, str):
                field = self.text_fields[name]
                field.setMaxLength(max(field.maxLength(), len(value)))
                field.setText(value)
                shown_ok = field.text() == value
        elif name in self.number_fields:
            spin = self.number_fields[name]
            if value is None:
                spin.setValue(-1)
                shown_ok = True
            elif type(value) is int and 0 <= value <= SPIN_MAX:
                if value > spin.maximum():
                    spin.setMaximum(value)
                spin.setValue(value)
                shown_ok = spin.value() == value
        elif name == "notes":
            if isinstance(value, str):
                self.notes.setPlainText(value)
                shown_ok = self.notes.toPlainText() == value
        if shown_ok:
            self._touched.add(name)
        return shown_ok

    def _set_notices(self, notices):
        self._notices = list(notices)
        self._render_notice()

    def add_notice(self, text):
        self._notices.append(text)
        self._render_notice()

    def _render_notice(self):
        lines = list(self._notices)
        if self.save_block:
            lines.append(self.save_block)
        self.notice.setText("\n".join(lines))
        self.notice.setVisible(bool(lines))

    def block_save(self, reason):
        """Keep the pilot's edits on screen but refuse to save them (with the reason)."""
        self.save_block = reason
        self.save_button.setEnabled(False)
        self.save_button.setToolTip(reason)
        self._render_notice()

    def allow_save(self):
        self.save_block = ""
        self.save_button.setEnabled(True)
        self.save_button.setToolTip("")
        self._render_notice()

    def mark_as_new(self):
        self.heading.setText("New manual flight")
        self.form.setRowVisible(self.reason, False)

    def reason_text(self):
        return self.reason.text().strip() if self.form.isRowVisible(self.reason) else ""

    # Reading

    def _value(self, name, shown, convert):
        loaded = self._loaded.get(name)
        if loaded is not None:
            loaded_shown, stored, exact = loaded
            if shown == loaded_shown and (exact or name not in self._touched):
                return stored
        return convert(shown)

    def values(self):
        data = {"flight_date": self._value(
            "flight_date", self.date.date(), lambda shown: shown.toString(Qt.DateFormat.ISODate))}
        for name, field in self.text_fields.items():
            data[name] = self._value(name, field.text(), str)
        for name, spin in self.number_fields.items():
            data[name] = self._value(name, spin.value(), lambda shown: None if shown < 0 else shown)
        data["notes"] = self._value("notes", self.notes.toPlainText(), str)
        return FlightFields(**data)

    @property
    def dirty(self):
        return self.original is not None and self.values() != self.original


class Logbook(QWidget):
    """The Logbook section body: ``LogbookWorkspace`` pages plus the Flight Editor.

    Contract C1: the app owns the single ``FlightStore``. ``set_store`` shows a store (or
    None); ``data_folder_requested`` asks the shell to choose or change the folder;
    ``set_recorder_status`` feeds the live Black Box page.
    """

    data_folder_requested = Signal()
    diagnostics_requested = Signal()
    flight_logging_settings_requested = Signal()

    def __init__(self, store=None, *, settings=None, preferences=None, today=date.today,
                 journal_in_background=True):
        super().__init__()
        #: Black Box journals are parsed on a worker thread so a long recording never
        #: freezes the EFB window.
        self.journal_in_background = journal_in_background
        self.store = None
        self.settings = settings          # kept for older hosts; the shell remembers folders
        self.preferences = preferences
        self.flights = []
        self.unreadable = []
        self.unshowable = []
        self.load_error = ""
        self.folder_notes = ()
        self.selected = None
        self.editing = None
        self.recorder_status = None
        self._views = {}                  # (id, digest) -> FlightView, so unchanged records convert once
        self._listing_key = None
        self._last_open = None
        self._last_record = None
        self._session_columns = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.folder_card, folder_layout = card(
            "No data folder",
            "Choose a local folder for your flight records. The Logbook keeps one JSON file per "
            "flight there, and Flight Logging saves recorded flights in the same folder.",
        )
        self.folder_button = button("Choose data folder…", self.choose_data_folder, "primary")
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_button)
        folder_row.addStretch(1)
        folder_layout.addLayout(folder_row)
        layout.addWidget(self.folder_card)
        self.warning = label("", "warning", wrap=True)
        self.warning.hide()
        layout.addWidget(self.warning)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.workspace = LogbookWorkspace(today=today, preferences=self._view_preferences(), can_edit=False)
        self.editor = FlightEditor(self.save_flight, self.cancel_edit)
        self.pages = {"Workspace": self.workspace, "Editor": scroll(self.editor)}
        for widget in self.pages.values():
            self.stack.addWidget(widget)

        self.workspace.edit_requested.connect(self.edit_flight)
        self.workspace.confirm_requested.connect(self.confirm_fields)
        self.workspace.new_flight_requested.connect(self.new_flight)
        self.workspace.records_changed.connect(lambda: self.reload(force=True))
        self.workspace.diagnostics_requested.connect(self.diagnostics_requested)
        self.workspace.flight_logging_settings_requested.connect(self.flight_logging_settings_requested)
        self.workspace.preferences_changed.connect(self._save_view_preferences)
        self.workspace.page_changed.connect(lambda _page: self.reload())
        self._subscribe_preferences()

        self.set_store(store)
        self.workspace.set_recorder_status(None)
        self.navigate("dashboard")

    # Dialog hooks (kept as methods so tests and hosts can replace them)

    def _warn(self, title, text):
        QMessageBox.warning(self, title, text)

    def _ask(self, title, text, accept, reject, default_accept=True):
        box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.NoButton, self)
        accept_button = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
        reject_button = box.addButton(reject, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(accept_button if default_accept else reject_button)
        box.setEscapeButton(reject_button)
        box.exec()
        return box.clickedButton() is accept_button

    # Compatibility views of the pages

    @property
    def dashboard(self):
        return self.workspace.dashboard

    @property
    def history(self):
        return self.workspace.history

    @property
    def details(self):
        return self.workspace.details

    @property
    def black_box(self):
        return self.workspace.recorder

    @property
    def statistics(self):
        return self.workspace.statistics

    # Navigation

    def _editor_open(self):
        return self.stack.currentWidget() is self.pages["Editor"]

    def navigate(self, name):
        """Show a Logbook page ('dashboard', 'history', 'black_box', 'statistics' or the old titles)."""
        key = PAGE_ALIASES.get(name, name)
        if self._editor_open():
            if not self.can_leave_editor():
                return False
            self._close_editor()
        self.stack.setCurrentWidget(self.workspace)
        self.workspace.navigate(key)
        return True

    @property
    def active_section(self):
        return self.workspace.current_page

    # Store (contract C1)

    def _writable(self, store=None):
        store = self.store if store is None else store
        if store is None or getattr(store, "read_only", False):
            return False
        return bool(getattr(store, "writable", True))

    def _can_write(self):
        return self._writable()

    def _edit_block_reason(self):
        if self.store is None:
            return "Choose a data folder first."
        if getattr(self.store, "read_only", False):
            return "This data folder is open read-only (another BBC EFB window may be using it)."
        if not self._writable():
            return "This data folder has been closed. Choose it again in Settings > Data Management."
        return ""

    def can_switch_store(self):
        """Ask before a data folder change would lose unsaved edits. True: go ahead.

        When the pilot chooses to discard, the editor is closed now, so ``set_store``
        does not ask again."""
        if not self._editor_open() or not self.editor.dirty:
            return True
        if self._ask("Discard unsaved flight changes?",
                     "Changing the data folder closes the Flight Editor. Your changes have not been saved.",
                     "Discard", "Cancel", default_accept=False):
            self._close_editor()
            self.stack.setCurrentWidget(self.workspace)
            return True
        return False

    def set_store(self, store):
        """Show ``store`` (the app's single FlightStore, or None). Never opens or closes a store.

        Unsaved edits are never dropped silently: if the editor holds changes, the pilot
        chooses to keep them (they stay in the editor; saving is allowed only where it
        cannot create a duplicate) or to discard them.
        """
        if store is self.store:
            self.reload(force=True)
            return True
        keep = False
        if self._editor_open():
            if self.editor.dirty:
                root = getattr(store, "root", None)
                where = f"the data folder {root}" if root is not None else "no data folder"
                keep = self._ask(
                    "Data folder changed",
                    f"The Logbook now uses {where}. Your flight changes have not been saved.\n\n"
                    "Keep them in the editor, or discard them?",
                    "Keep my changes", "Discard", default_accept=True)
            if not keep:
                self._close_editor()
        previous_editing = self.editing
        self.store = store
        self.folder_notes = data_folder_notes(store.root) if store is not None and hasattr(store, "root") else ()
        self._views.clear()
        self._listing_key = None
        self.selected = None
        self.workspace.reset_browsing()
        writable = self._writable()
        self.workspace.set_actions(StoreActions(store) if writable else None)
        self.workspace.set_journal_loader(
            journal_loader_for(store.root) if store is not None and hasattr(store, "root") else None,
            background=self.journal_in_background)
        self.workspace.set_can_edit(writable, self._edit_block_reason())
        if keep:
            self._rebase_editor(previous_editing)
        else:
            self.stack.setCurrentWidget(self.workspace)
        self.reload(force=True)
        return True

    def _rebase_editor(self, flight):
        """Keep the pilot's edits after a folder change, saving only where that is safe."""
        root = getattr(self.store, "root", None)
        if not self._writable():
            self.editor.block_save(
                f"{self._edit_block_reason()} Your changes are still shown so you can copy them; "
                "they cannot be saved here.")
            return
        if flight is None:
            self.editing = None
            self.editor.allow_save()
            self.editor.add_notice(f"The data folder changed. Saving adds this new flight to {root}.")
            return
        try:
            fresh = self.store.get(flight.id)
        except (RecordMissing, UnreadableRecord, OSError, ValueError):
            fresh = None
        if fresh is not None and fresh.digest == flight.digest and not fresh.read_only:
            self.editing = fresh   # the same flight, unchanged, is in this folder
            self.editor.allow_save()
            return
        self.editing = flight
        self.editor.block_save(
            f"This flight is not in the data folder now open ({root}), or it differs there. Your "
            "changes are still shown so you can copy them. Saving is turned off so no duplicate "
            "flight is created; choose the original folder again to save them.")

    def refresh(self, store=_SAME):
        """Reload flights. ``refresh(store)`` is ``set_store(store)`` (older hosts call this)."""
        if store is _SAME:
            self.reload(force=True)
        else:
            self.set_store(store)

    def reload(self, force=False):
        """Read the listing and update every page. Unchanged listings cost one directory scan."""
        load_error = ""
        try:
            flights, unreadable = self.store.listing() if self.store is not None else ([], [])
        except OSError as error:
            log.exception("Listing flights failed")
            flights, unreadable = [], []
            load_error = f"The data folder could not be read: {error}"
        key = (tuple((flight.id, flight.digest) for flight in flights),
               tuple((getattr(item, "name", str(item)), getattr(item, "kind", ""), getattr(item, "reason", ""))
                     for item in unreadable), load_error)
        if not force and key == self._listing_key:
            return False
        self._listing_key = key
        self.flights, self.unreadable, self.load_error = list(flights), list(unreadable), load_error
        views, unshowable, cache = [], [], {}
        for flight in flights:
            view_key = (flight.id, flight.digest)
            view = self._views.get(view_key)
            if view is None:
                try:
                    view = flight_view(flight)
                except (ViewError, AttributeError, TypeError, KeyError, ValueError) as error:
                    unshowable.append(SimpleNamespace(
                        name=getattr(flight, "file_name", "") or flight.id, kind="unreadable",
                        reason=f"The record could not be shown ({error})."))
                    continue
            cache[view_key] = view
            views.append(view)
        self._views = cache
        self.unshowable = unshowable
        self.workspace.set_flights(views, list(unreadable) + unshowable)
        self._render_banner()
        return True

    def _warning_text(self):
        lines = []
        if self.load_error:
            lines.append(self.load_error)
        reason = self._edit_block_reason() if self.store is not None else ""
        if reason:
            lines.append(reason + " Flights cannot be added or changed.")
        lines.extend(self.folder_notes)
        problems = list(self.unreadable) + list(self.unshowable)
        if problems:
            lines.append(f"{len(problems)} file(s) in the flights folder are not shown "
                         "(listed in Black Box > Needs attention):")
            for item in problems[:MAX_LISTED_PROBLEMS]:
                lines.append(f"• {getattr(item, 'name', item)}: {getattr(item, 'reason', 'unreadable')}")
            if len(problems) > MAX_LISTED_PROBLEMS:
                lines.append(f"…and {len(problems) - MAX_LISTED_PROBLEMS} more.")
        return "\n".join(lines)

    def _render_banner(self):
        text = self._warning_text()
        self.warning.setText(text)
        self.warning.setVisible(bool(text))
        self.folder_card.setVisible(self.store is None)

    def choose_data_folder(self):
        """Ask the shell to choose or change the data folder (contract C1).

        The shell handles ``data_folder_requested`` with its DataFolderControl (Settings >
        Data Management) and then calls ``set_store``. The Logbook never opens a store."""
        try:
            connected = self.receivers(SIGNAL("data_folder_requested()")) > 0
        except (TypeError, ValueError, RuntimeError):
            connected = True
        if not connected:
            self._warn("Choose the data folder in Settings",
                       "Open Settings > Data Management to choose where your Logbook is kept.")
            return False
        self.data_folder_requested.emit()
        return True

    # Live Flight Logging status (Black Box page)

    def set_recorder_status(self, status):
        """Show the Recorder's live status on the Black Box page. Reload when a flight closes."""
        self.recorder_status = status
        live = self.workspace.set_recorder_status(status)
        last = getattr(status, "last_flight", None)
        record = getattr(last, "record_id", None) or getattr(status, "last_record_id", None)
        closed = self._last_open is True and not live.flight_open
        if closed or (record is not None and record != self._last_record):
            self.reload()
        self._last_open = live.flight_open
        self._last_record = record
        return live

    # Details

    def _fresh(self, flight_or_id):
        """Re-read a flight from disk. Warns and returns None when it is gone or unreadable."""
        flight_id = flight_or_id if isinstance(flight_or_id, str) else flight_or_id.id
        if self.store is None:
            return None if isinstance(flight_or_id, str) else flight_or_id
        try:
            return self.store.get(flight_id)
        except RecordMissing:
            self.reload()
            self._warn("Flight not found", "This flight is no longer in the data folder. It may have "
                       "been moved or deleted. Flight History has been reloaded.")
        except (UnreadableRecord, OSError, ValueError) as error:
            self.reload()
            self._warn("Flight cannot be read", f"This flight's file can no longer be read: {error}")
        return None

    def open_details(self, flight, tab="overview"):
        """Open Flight Details for ``flight`` (a Flight or an id), read fresh from disk."""
        fresh = self._fresh(flight)
        if fresh is None:
            return False
        self.reload()
        self.selected = fresh
        if self._editor_open():
            if not self.can_leave_editor():
                return False
            self._close_editor()
        self.stack.setCurrentWidget(self.workspace)
        return self.workspace.open_flight(fresh.id, tab)

    def confirm_fields(self, flight_id, names, diversion=None):
        """Record that the pilot checked ``names`` of a flight and they are correct.

        Writes a pilot revision through ``FlightStore.confirm`` (no value changes). Refused,
        with nothing written, when the flight changed since Flight Details showed it.
        """
        if not self._can_write():
            reason = self._edit_block_reason()
            if reason:
                self._warn("Flight cannot be changed", reason)
            return False
        details = self.workspace.details
        seen = details.view if details.view is not None and details.view.id == flight_id else None
        try:
            saved = StoreActions(self.store).confirm_fields(flight_id, names, seen=seen, diversion=diversion)
        except (StaleListing, RecordConflict) as error:
            self.reload(force=True)
            self._warn("Flight changed", str(error))
            return False
        except RecordMissing:
            self.reload(force=True)
            self._warn("Flight not found", "This flight is no longer in the data folder. It may have "
                       "been moved or deleted. Flight History has been reloaded.")
            return False
        except (RecordReadOnly, UnreadableRecord, OSError, ValueError) as error:
            self.reload(force=True)
            self._warn("Confirmation not saved", str(error))
            return False
        self.selected = saved
        self.reload(force=True)
        return True

    # Editing

    def new_flight(self):
        self.start_editor(None)

    def edit_flight(self, flight_id):
        self.start_editor(flight_id)

    def start_editor(self, flight):
        """Open the editor for a new flight (None), a Flight or a flight id."""
        if not self._can_write():
            reason = self._edit_block_reason()
            if reason:
                self._warn("Flight cannot be edited", reason)
            return False
        if self._editor_open() and not self.can_leave_editor():
            return False
        if flight is not None:
            flight = self._fresh(flight)
            if flight is None:
                return False
            if flight.read_only:
                self._warn("Flight is read-only", flight.read_only_reason)
                return False
        try:
            self.editor.open(flight)
        except Exception as error:  # never leave a half-loaded, saveable form on screen
            log.exception("Flight Editor could not load flight %s", getattr(flight, "id", None))
            self.editing = None
            try:
                self.editor.open(None)
            except Exception:
                log.exception("Flight Editor could not be reset")
            self.editor.original = None
            self.editor.block_save("The flight could not be loaded into the editor.")
            if self._editor_open():
                self.stack.setCurrentWidget(self.workspace)
            self._warn("Flight could not be opened",
                       f"This flight could not be loaded into the editor, so nothing was changed: {error}")
            return False
        self.editing = flight
        self.stack.setCurrentWidget(self.pages["Editor"])
        self.pages["Editor"].verticalScrollBar().setValue(0)
        return True

    def _close_editor(self):
        self.editing = None
        self.editor.open(None)
        self.editor.original = None
        if self._editor_open():
            self.stack.setCurrentWidget(self.workspace)

    def _hold_edits(self, title, reason):
        """Keep the pilot's edits on screen without a save that could duplicate the flight."""
        self.editor.block_save(f"{reason} Your changes are still in the editor so you can copy them. "
                               "Saving is turned off so no duplicate flight is created.")
        self._warn(title, f"{reason}\n\nYour changes are still in the editor so you can copy them. "
                   "Saving is turned off so no duplicate flight is created.")

    def _renamed_copies(self, flight_id):
        return [item.name for item in self.unreadable
                if flight_id.lower() in str(getattr(item, "name", "")).lower()]

    def _record_gone(self, flight):
        """After RecordMissing: True only when the file is really gone (not renamed or unreadable)."""
        self.reload(force=True)
        try:
            self.store.get(flight.id)
        except RecordMissing:
            pass
        except (UnreadableRecord, OSError, ValueError) as error:
            self._hold_edits("Flight could not be saved", f"This flight's file can no longer be read: {error}")
            return False
        else:
            return False  # it is back: the caller treats this as a conflict
        copies = self._renamed_copies(flight.id)
        if copies:
            self._hold_edits(
                "Flight could not be saved",
                f"This flight's file seems to have been renamed to {', '.join(copies)} (for example by a "
                f"sync tool). Rename it back to {flight.id}.json, then open the flight again.")
            return False
        return True

    def save_flight(self):
        if self.store is None or not self.editor.save_button.isEnabled():
            return False
        if not self._can_write():
            self._warn("Flight could not be saved", self._edit_block_reason())
            return False
        values = self.editor.values()
        try:
            saved = self.store.save(values, self.editing, reason=self.editor.reason_text())
        except RecordConflict:
            return self._handle_conflict(values)
        except RecordReadOnly as error:
            self.reload(force=True)
            self._hold_edits("Flight could not be saved", str(error) or "This flight is now read-only.")
            return False
        except UnreadableRecord as error:
            self.reload(force=True)
            self._hold_edits("Flight could not be saved", f"This flight's file can no longer be read: {error}")
            return False
        except RecordMissing as error:
            if self.editing is None:
                self._warn("Flight could not be saved", str(error))
                return False
            if not self._record_gone(self.editing):
                # Either the edits are now held (renamed or unreadable file), or the file is
                # back on disk, which is an ordinary conflict.
                return False if self.editor.save_block else self._handle_conflict(values)
            self.editing = None
            self.editor.mark_as_new()
            self._warn("Flight could not be saved",
                       "This flight is not in the current data folder. It may have been moved or deleted. "
                       "Your edits are still in the editor; saving again creates them as a new flight.")
            return False
        except (OSError, ValueError) as error:
            self._warn("Flight could not be saved", str(error))
            return False
        self.selected = saved
        self.editing = None
        self.editor.original = None
        self.reload(force=True)
        self.stack.setCurrentWidget(self.workspace)
        self.workspace.open_flight(saved.id)
        return True

    def _handle_conflict(self, values):
        old = self.editing
        original = self.editor.original
        reason = self.editor.reason.text()
        mine = {name: getattr(values, name) for name in FIELD_NAMES
                if getattr(values, name) != getattr(original, name)}
        self.reload(force=True)
        try:
            fresh = self.store.get(old.id)
        except RecordMissing:
            if self._record_gone(old):
                self.editing = None
                self.editor.mark_as_new()
                self._warn("Flight changed on disk",
                           "This flight was removed from the data folder. Your edits are still in the "
                           "editor; saving again creates them as a new flight.")
            return False
        except (UnreadableRecord, OSError, ValueError) as error:
            self._hold_edits("Flight changed on disk", f"This flight's file can no longer be read: {error}")
            return False
        if fresh.read_only:
            # Never save a copy to get round a read-only record: that would count the flight twice.
            self._hold_edits("Flight changed on disk", fresh.read_only_reason)
            return False
        theirs = [name for name in FIELD_NAMES if getattr(fresh.fields, name) != getattr(old.fields, name)]
        self.editing = fresh
        not_applied = self.editor.open(fresh, pending=mine)
        self.editor.reason.setText(reason)
        applied = {name: value for name, value in mine.items() if name not in not_applied}
        both = [name for name in applied if name in theirs and getattr(fresh.fields, name) != applied[name]]
        names = _field_list(theirs) or "no visible values"
        text = f"Another window or program saved this flight while you were editing it. It changed {names}."
        if both:
            text += f" You also changed {_field_list(both)}; applying your edits replaces the other values."
        if not_applied:
            text += (f" These edits could not be shown in the editor and are NOT applied: {_field_list(not_applied)}."
                     " Enter them again if you still want them.")
        text += " The editor now shows the saved flight"
        text += " with your other edits on top." if applied else "."
        text += " Both versions stay in Record History."
        if applied and self._ask("Flight changed on disk", text, "Apply my edits", "Keep editing"):
            return self.save_flight()
        if not applied:
            self._warn("Flight changed on disk", text)
        return False

    def can_leave_editor(self):
        if not self._editor_open() or not self.editor.dirty:
            return True
        return self._ask("Discard unsaved flight changes?", "Your changes have not been saved.",
                         "Discard", "Cancel", default_accept=False)

    def cancel_edit(self):
        if not self.can_leave_editor():
            return False
        editing = self.editing
        self._close_editor()
        self.stack.setCurrentWidget(self.workspace)
        if editing is not None and self.store is not None:
            self.workspace.open_flight(editing.id)
        return True

    # Preferences (Q8 basic preferences: History view and hidden columns)

    def _view_preferences(self):
        prefs = self.preferences
        result = {}
        if prefs is None:
            return result
        try:
            mode = prefs.get(VIEW_PREFERENCE)
            if mode in VIEW_MODES:
                result["view_mode"] = mode
        except (ValueError, KeyError, AttributeError, TypeError):
            pass
        try:
            columns = prefs.get(COLUMNS_PREFERENCE)
            if isinstance(columns, (list, tuple)) and all(isinstance(item, str) for item in columns):
                result["hidden_columns"] = list(columns)
        except (ValueError, KeyError, AttributeError, TypeError):
            pass  # this build's preferences do not know the key yet: defaults apply
        return result

    def _save_view_preferences(self, values):
        prefs = self.preferences
        if prefs is None:
            return
        try:
            prefs.set(VIEW_PREFERENCE, values.get("view_mode"))
        except (ValueError, KeyError, AttributeError, TypeError) as error:
            log.warning("History view preference not kept: %s", error)
        try:
            prefs.set(COLUMNS_PREFERENCE, list(values.get("hidden_columns", ())))
        except (ValueError, KeyError, AttributeError, TypeError):
            self._session_columns = list(values.get("hidden_columns", ()))  # kept for this session only
        save = getattr(prefs, "save", None)
        if callable(save) and getattr(prefs, "writable", True):
            try:
                save()
            except (OSError, ValueError) as error:
                log.warning("Logbook preferences not saved: %s", error)

    def _subscribe_preferences(self):
        subscribe = getattr(self.preferences, "subscribe", None)
        if not callable(subscribe):
            return
        history = self.workspace.history

        def changed(key, value):
            try:
                if key == VIEW_PREFERENCE and value in VIEW_MODES and value != history.view_mode:
                    history.set_view_mode(value, emit=False)
            except RuntimeError:
                pass  # the Logbook was closed

        unsubscribe = subscribe(changed)
        if callable(unsubscribe):
            self.destroyed.connect(lambda *_: unsubscribe())


__all__ = ["FlightEditor", "Logbook", "describe_source"]
