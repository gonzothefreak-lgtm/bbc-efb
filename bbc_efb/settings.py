"""Main Settings: SimConnect, Flight Logging & Privacy, Data Management and Layout.

``SettingsPage`` is a plain ``QWidget`` that takes duck-typed dependencies, so it
works before the shell, Core host and Recorder are wired together:

* ``preferences``: a ``bbc_efb.preferences.Preferences`` (or anything with
  ``get(key)``, ``set(key, value)``, ``save()`` and optionally ``path``,
  ``read_only_reason``, ``problem``, ``adopt(bytes)``).
* ``data_folder``: an object with ``current() -> Path | None`` and optionally
  ``change(path)`` (opens and remembers a new data folder; raises with a
  plain-English message on failure) and ``release()`` (closes the current
  folder, needed only to restore a backup *into* the folder in use). A plain
  callable returning the path is also accepted (read-only then).
* ``diagnostics``: a callable returning connection diagnostics: a
  ``ConnectionDiagnostics``, a mapping with the same keys, a
  ``bbc_core.contract.Connection``, or ``None`` when Core is not running yet.
* ``measure_response_times``: optional callable returning SimConnect's five
  response times in seconds (``Core.response_times()`` via the host thread). It is
  run off the UI thread because the SDK may block while it measures.

Every dialog goes through ``dialogs`` (default ``QtDialogs``) and every slow job
through ``run_task(fn, done)`` (default: a worker thread that reports back on the
UI thread), so tests can drive the page without modal windows.

``choose_data_folder(parent)`` is the first-launch helper: it explains the choice,
offers ``Documents\\BBC EFB Data`` or a folder of the pilot's choosing, creates it,
proves it is writable and warns about OneDrive, network and drive-root folders.

PROPOSALS for the owner
-----------------------

* The diagnostics view refreshes once a second only while it is on screen, and
  response times are measured only when the pilot presses the button (the SDK
  call can block and adds simulator traffic).
* Changing the data folder switches folders; it does not move flights (see
  ``data_management``). A "Show folder" button opens the folder in Explorer.
* The Flight Logging inventory shown in Privacy is the planned inventory; it is
  labelled as such until the owner approves the Recorder rules. The Recorder can
  pass its exact list through ``recorded_items``.
* Restoring offers to switch to the restored folder; restoring into the folder in
  use first releases it, and the old contents are kept beside it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import threading
from typing import Any, Callable, Mapping

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QStackedWidget,
    QVBoxLayout, QWidget,
)

from . import VERSION
from . import data_management as dm
from .preferences import (
    DENSITIES, HISTORY_VIEWS, INFLIGHT_PANELS, LAYOUT_KEYS, LOGBOOK_PANELS, PreferencesError,
    PreferencesSaveError,
)

log = logging.getLogger(__name__)

PAGES = (
    ("simconnect", "SimConnect"),
    ("flight_logging", "Flight Logging & Privacy"),
    ("data_management", "Data Management"),
    ("layout", "Layout"),
    ("modules", "Modules (Experimental)"),
)
NOT_AVAILABLE = "Not available"
RESPONSE_TIME_LABELS = (
    "Total round trip",
    "Request until sent",
    "Request until received by the simulator",
    "Request until the simulator answered",
    "Simulator answer until received here",
)
STATE_TEXT = {
    "disconnected": "Not connected",
    "connecting": "Connecting",
    "connected": "Connected",
    "error": "Connection error",
    "unknown": "Unknown (Black Box Core is not running)",
}
PLANNED_RECORDED_ITEMS = (
    "Flight date, aircraft type and the aircraft title reported by the simulator.",
    "Departure and arrival airports when the simulator data shows them; otherwise they stay unknown.",
    "Callsign and registration when the simulator or you provide them.",
    "Off-block, takeoff, landing and on-block times, with whether simulator or computer time was used.",
    "Touchdown vertical speed when it was measured. It is never estimated.",
    "A flight journal: phase changes, pauses, simulation rate changes, connection gaps and "
    "periodic position, altitude and speed samples.",
    "Every correction you make, with the original value, the time and your reason.",
)
NOT_RECORDED_ITEMS = (
    "No audio, video, screenshots or keystrokes.",
    "No text or voice chat, and no VATSIM or other account passwords.",
    "Nothing about other programs on this computer.",
)
DELETE_TEXT = ("This build has no Delete Flight command. To remove a flight, close BBC EFB and delete its file "
               "from the flights folder. Create a backup first if you may want it back.")
TRANSFER_TEXT = ("BBC EFB sends nothing over the internet. This build has no optional transfers, uploads or "
                 "online services, and it does not listen for network connections.")


# Diagnostics model ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ConnectionDiagnostics:
    """What Settings > SimConnect > Connection Diagnostics shows. ``None`` means unknown."""

    state: str = "unknown"
    detail: str = ""
    source: str = ""
    session_id: str | None = None
    since: datetime | None = None
    dll_path: str | None = None
    dll_found: bool | None = None
    dll_candidates: tuple[str, ...] = ()
    dll_live: bool = False
    """True when a new SimConnect.dll choice is used from the next connection attempt (no restart)."""
    pending_provider: str | None = None
    """The replacement the Core host will use at its next connection attempt, if any."""
    response_times_s: tuple[float, ...] | None = None
    response_measured_at: datetime | None = None
    last_errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _text(value) -> str:
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw)


def _source_text(source) -> str:
    """``bbc_core.contract.Source(name, simulated)``, an enum or plain text."""
    if source is None:
        return ""
    if hasattr(source, "simulated") and hasattr(source, "name"):
        return "simulated" if source.simulated else str(source.name)
    return _text(source)


def coerce_diagnostics(value) -> ConnectionDiagnostics:
    """Turn whatever the provider returned into ``ConnectionDiagnostics`` (unknown stays unknown)."""
    if value is None:
        return ConnectionDiagnostics()
    if isinstance(value, ConnectionDiagnostics):
        return value
    if isinstance(value, Mapping):
        get = value.get
    else:
        def get(name, default=None):
            return getattr(value, name, default)
    connection = get("connection")
    if connection is not None and get("state") is None:
        base = coerce_diagnostics(connection)
    else:
        base = ConnectionDiagnostics()
    state = get("state")
    since = get("since") or get("occurred_at")
    times = get("response_times_s")
    if times is None:
        times = get("response_times")
    errors = get("last_errors") or ()
    if isinstance(errors, str):
        errors = (errors,)
    candidates = get("dll_candidates") or ()
    dll_path = get("dll_path")
    dll_found = get("dll_found")
    if dll_found is None and dll_path:
        dll_found = Path(str(dll_path)).is_file()
    return replace(
        base,
        state=_text(state).lower() if state is not None else base.state,
        detail=str(get("detail") or base.detail or ""),
        source=_source_text(get("source")) or base.source,
        session_id=str(get("session_id")) if get("session_id") else base.session_id,
        since=since if isinstance(since, datetime) else base.since,
        dll_path=str(dll_path) if dll_path else base.dll_path,
        dll_found=dll_found if isinstance(dll_found, bool) else base.dll_found,
        dll_candidates=tuple(str(item) for item in candidates) or base.dll_candidates,
        dll_live=get("dll_live") is True or base.dll_live,
        pending_provider=str(get("pending_provider")) if get("pending_provider") else base.pending_provider,
        response_times_s=tuple(float(item) for item in times) if times else base.response_times_s,
        response_measured_at=get("response_measured_at") or base.response_measured_at,
        last_errors=tuple(_describe_error(item) for item in errors) or base.last_errors,
        notes=tuple(str(item) for item in (get("notes") or ())) or base.notes,
    )


def _describe_error(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, (tuple, list)) and len(item) == 2:
        when, text = item
        if isinstance(when, datetime):
            return f"{_when(when)}  {text}"
        return f"{when}  {text}"
    if isinstance(item, Mapping):
        when = item.get("at") or item.get("occurred_at")
        text = item.get("message") or item.get("detail") or item.get("text") or str(dict(item))
        return f"{_when(when)}  {text}" if isinstance(when, datetime) else str(text)
    return str(item)


def _when(moment: datetime | None) -> str:
    if moment is None:
        return NOT_AVAILABLE
    if moment.tzinfo is None:
        return moment.strftime("%Y-%m-%d %H:%M:%S") + " (time zone unknown)"
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_response_times(times_s) -> tuple[tuple[str, str], ...]:
    """Label SimConnect's response times (seconds) in milliseconds."""
    if not times_s:
        return ()
    rows = []
    for index, value in enumerate(times_s):
        name = RESPONSE_TIME_LABELS[index] if index < len(RESPONSE_TIME_LABELS) else f"Value {index + 1}"
        try:
            number = float(value)
        except (TypeError, ValueError):
            rows.append((name, NOT_AVAILABLE))
            continue
        rows.append((name, f"{number * 1000:.1f} ms" if number == number and number >= 0 else NOT_AVAILABLE))
    return tuple(rows)


# Small widget helpers (mirroring widgets.py object names so the theme applies) -------

def _label(text="", style="", wrap=True, selectable=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(style)
    widget.setWordWrap(wrap)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                       | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    return widget


def _button(text, slot=None, style="", accessible=""):
    widget = QPushButton(text)
    widget.setObjectName(style)
    if accessible:
        widget.setAccessibleName(accessible)
    if slot is not None:
        widget.clicked.connect(slot)
    return widget


def _card(title, description=None):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(9)
    layout.addWidget(_label(title, "cardTitle", wrap=False))
    if description:
        layout.addWidget(_label(description, "muted"))
    return frame, layout


def _bullets(items) -> str:
    return "\n".join(f"• {item}" for item in items)


# Dialogs and tasks -------------------------------------------------------------

class QtDialogs:
    """Modal dialogs used by Settings. Replace in tests with an object of the same shape."""

    def __init__(self, parent: QWidget | None):
        self.parent = parent

    def choose_folder(self, title: str, start: Path | None = None) -> Path | None:
        chosen = QFileDialog.getExistingDirectory(self.parent, title, str(start or ""))
        return Path(chosen) if chosen else None

    def choose_save_file(self, title: str, start: Path | None, name_filter: str) -> Path | None:
        chosen, _ = QFileDialog.getSaveFileName(self.parent, title, str(start or ""), name_filter,
                                                options=QFileDialog.Option.DontConfirmOverwrite)
        return Path(chosen) if chosen else None

    def choose_open_file(self, title: str, start: Path | None, name_filter: str) -> Path | None:
        chosen, _ = QFileDialog.getOpenFileName(self.parent, title, str(start or ""), name_filter)
        return Path(chosen) if chosen else None

    def confirm(self, title: str, text: str, accept: str = "OK", reject: str = "Cancel",
                default_accept: bool = False) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.NoButton, self.parent)
        yes = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton(reject, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes if default_accept else no)
        box.setEscapeButton(no)
        box.exec()
        return box.clickedButton() is yes

    def inform(self, title: str, text: str) -> None:
        QMessageBox.information(self.parent, title, text)

    def warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self.parent, title, text)

    def first_folder_prompt(self, text: str, suggested: Path | None) -> str | None:
        """Return "suggested", "choose" or None (cancelled)."""
        box = QMessageBox(QMessageBox.Icon.Information, "Choose where to keep your Logbook", text,
                          QMessageBox.StandardButton.NoButton, self.parent)
        use = box.addButton("Use suggested folder", QMessageBox.ButtonRole.AcceptRole) if suggested else None
        choose = box.addButton("Choose folder…", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(use or choose)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if use is not None and clicked is use:
            return "suggested"
        if clicked is choose:
            return "choose"
        return None


class _Relay(QObject):
    finished = Signal(object)


def _call(fn):
    try:
        return fn(), None
    except Exception as error:  # reported to the pilot, never swallowed
        return None, error


def run_inline(fn, done):
    """A ``run_task`` that runs ``fn`` at once (tests, tiny jobs)."""
    done(*_call(fn))


def documents_folder() -> Path:
    from PySide6.QtCore import QStandardPaths
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return Path(location) if location else Path.home() / "Documents"


def suggested_data_folder() -> Path:
    return documents_folder() / "BBC EFB Data"


FIRST_LAUNCH_TEXT = (
    "BBC EFB keeps your Logbook in a folder you choose, as plain JSON files you can open and back up. "
    "Nothing is hidden in AppData and nothing is sent anywhere.\n\n"
    "Choose a local folder you can write to, for example in Documents or a dedicated folder on C:. "
    "Avoid OneDrive, network drives and the top of a drive."
)


def choose_data_folder(parent: QWidget | None = None, *, dialogs=None, suggested: Path | None = None,
                       first_launch: bool = True, check=dm.check_data_folder) -> Path | None:
    """Ask the pilot for a writable data folder; create it if needed. Returns None if cancelled.

    The returned folder exists and passed a write test. The caller opens it as the
    data folder and remembers it.
    """
    dialogs = dialogs or QtDialogs(parent)
    if suggested is None and first_launch:
        suggested = suggested_data_folder()
    while True:
        if first_launch:
            choice = dialogs.first_folder_prompt(
                FIRST_LAUNCH_TEXT + (f"\n\nSuggested folder: {suggested}" if suggested else ""), suggested)
            if choice is None:
                return None
            candidate = suggested if choice == "suggested" else dialogs.choose_folder(
                "Choose a folder for your Logbook data", suggested.parent if suggested else None)
            if candidate is None:
                continue
        else:
            candidate = dialogs.choose_folder("Choose a folder for your Logbook data", suggested)
            if candidate is None:
                return None
        result = check(candidate, create=True)
        if not result.usable:
            dialogs.warn("This folder cannot be used", f"{result.path}\n\n{result.problem}\n\nChoose another folder.")
            continue
        if result.notes:
            text = f"{result.path}\n\n" + _bullets(result.notes) + "\n\nUse this folder anyway?"
            if not dialogs.confirm("Check this folder", text, accept="Use this folder", reject="Choose another"):
                if result.created:
                    try:
                        result.path.rmdir()
                    except OSError:
                        pass
                continue
        return result.path


# Settings page --------------------------------------------------------------------

class _FolderAccess:
    """Adapter over the duck-typed ``data_folder`` dependency."""

    def __init__(self, source):
        self.source = source

    def current(self) -> Path | None:
        source = self.source
        if source is None:
            return None
        if hasattr(source, "current"):
            value = source.current()
        elif callable(source):
            value = source()
        else:
            value = source
        return Path(value) if value else None

    @property
    def can_change(self) -> bool:
        return hasattr(self.source, "change")

    @property
    def can_release(self) -> bool:
        return hasattr(self.source, "release") and self.can_change

    def change(self, folder: Path):
        return self.source.change(Path(folder))

    def release(self):
        return self.source.release()


def _newer_module_settings_text() -> str:
    """Why module switches are disabled when module-state.json was written by a newer EFB."""
    try:
        from .modules.loader import NEWER_STATE_MESSAGE
    except ImportError:  # pragma: no cover - part of every build
        NEWER_STATE_MESSAGE = ("Module settings were written by a newer EFB. They are kept unchanged and cannot "
                               "be changed in this EFB.")
    return NEWER_STATE_MESSAGE


class SettingsPage(QWidget):
    """Main Settings. ``show_page(name)`` or ``open_connection_diagnostics()`` from the footer."""

    page_changed = Signal(str)
    data_folder_changed = Signal(object)
    automatic_logging_changed = Signal(bool)

    def __init__(self, preferences=None, data_folder=None, diagnostics: Callable[[], Any] | None = None, *,
                 measure_response_times: Callable[[], Any] | None = None, dialogs=None,
                 run_task: Callable | None = None, app_version: str = VERSION,
                 record_schema: int | None = None, recorded_items=None,
                 delete_text: str = DELETE_TEXT, refresh_ms: int = 1000, modules=None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._modules = modules
        self.setObjectName("page")
        self.preferences = preferences
        self.folders = _FolderAccess(data_folder)
        self.diagnostics_provider = diagnostics
        self.measure_response_times = measure_response_times
        self.dialogs = dialogs or QtDialogs(self)
        self.app_version = app_version
        self.record_schema = record_schema
        self.recorded_items = tuple(recorded_items) if recorded_items else None
        self.delete_text = delete_text
        self._relay = _Relay(self)
        self._relay.finished.connect(self._task_finished)
        self._run_task = run_task or self._thread_task
        self._busy = False
        self._measured: tuple[tuple[float, ...], datetime] | None = None
        self._measure_error: str | None = None
        self._measuring = False
        self._syncing = False
        self._dll_choice_changed = False  # set after the pilot changes the SimConnect.dll choice
        self.last_diagnostics = ConnectionDiagnostics()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        self.navigation = QListWidget()
        self.navigation.setObjectName("settingsNavigation")
        self.navigation.setAccessibleName("Settings pages")
        self.navigation.setMinimumWidth(170)
        self.navigation.setMaximumWidth(240)
        self.stack = QStackedWidget()
        self._pages: dict[str, int] = {}
        builders = {
            "simconnect": self._build_simconnect,
            "flight_logging": self._build_flight_logging,
            "data_management": self._build_data_management,
            "layout": self._build_layout,
            "modules": self._build_modules,
        }
        for key, title in PAGES:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.navigation.addItem(item)
            content = QWidget()
            content.setObjectName("page")
            layout = QVBoxLayout(content)
            layout.setContentsMargins(0, 0, 8, 0)
            layout.setSpacing(14)
            layout.addWidget(_label(title, "heading", wrap=False))
            builders[key](layout)
            layout.addStretch(1)
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(content)
            area.setAccessibleName(f"{title} settings")
            self._pages[key] = self.stack.addWidget(area)
        outer.addWidget(self.navigation)
        outer.addWidget(self.stack, 1)
        self.navigation.currentRowChanged.connect(self._row_changed)

        self._timer = QTimer(self)
        self._timer.setInterval(max(200, int(refresh_ms)))
        self._timer.timeout.connect(self.refresh_diagnostics)

        start = self._pref("settings.last_page", "simconnect")
        self.show_page(start if start in self._pages else "simconnect")
        self._unsubscribe = None
        if hasattr(self.preferences, "subscribe"):
            self._unsubscribe = self.preferences.subscribe(self._preference_changed)
            self.destroyed.connect(lambda *_, unsubscribe=self._unsubscribe: unsubscribe())
        self.refresh()

    # Navigation -----------------------------------------------------------

    def show_page(self, name: str) -> None:
        if name not in self._pages:
            raise KeyError(f"Unknown Settings page {name!r}.")
        row = [key for key, _ in PAGES].index(name)
        if self.navigation.currentRow() != row:
            self.navigation.setCurrentRow(row)
        else:
            self._row_changed(row)

    def open_connection_diagnostics(self) -> None:
        """Destination of the footer's SimConnect status."""
        self.show_page("simconnect")
        self.refresh_diagnostics()
        self.diagnostics_card.setFocus(Qt.FocusReason.OtherFocusReason)

    def open_privacy(self) -> None:
        self.show_page("flight_logging")

    @property
    def current_page(self) -> str:
        return PAGES[self.stack.currentIndex()][0]

    def _row_changed(self, row):
        if row < 0:
            return
        key = PAGES[row][0]
        self.stack.setCurrentIndex(self._pages[key])
        self._update_timer()
        if key == "data_management":
            self.refresh_data_folder()
        elif key == "modules":
            self.refresh_modules()
        elif key == "simconnect":
            self.refresh_diagnostics()
        if self._pref("settings.last_page", None) != key and self.preferences is not None:
            self._set_pref("settings.last_page", key, quiet=True)
        self.page_changed.emit(key)

    def showEvent(self, event):
        super().showEvent(event)
        self._update_timer()
        self.refresh()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._update_timer()

    def _update_timer(self):
        wanted = self.isVisible() and self.current_page == "simconnect" and self.diagnostics_provider is not None
        if wanted and not self._timer.isActive():
            self._timer.start()
        elif not wanted and self._timer.isActive():
            self._timer.stop()

    @property
    def diagnostics_refreshing(self) -> bool:
        return self._timer.isActive()

    def refresh(self) -> None:
        self.refresh_diagnostics()
        self.refresh_data_folder()
        self.refresh_preferences()
        self.refresh_modules()

    # Preferences helpers --------------------------------------------------

    def _pref(self, key, default=None):
        if self.preferences is None:
            return default
        try:
            return self.preferences.get(key)
        except Exception:
            return default

    def _set_pref(self, key, value, *, quiet=False) -> bool:
        """Store and save one preference; show a plain message if it cannot be saved."""
        if self.preferences is None:
            return False
        try:
            self.preferences.set(key, value)
        except PreferencesError as error:
            self._show_pref_status(str(error))
            return False
        return self._save_prefs(quiet=quiet)

    def _save_prefs(self, *, quiet=False) -> bool:
        reason = getattr(self.preferences, "read_only_reason", None)
        if reason:
            self._show_pref_status(reason)
            return True
        try:
            self.preferences.save()
        except (PreferencesSaveError, OSError) as error:
            self._show_pref_status(f"This change applies now but could not be saved: {error}")
            return True
        if not quiet:
            self._show_pref_status("")
        return True

    def _show_pref_status(self, text):
        for widget in (self.layout_status, self.logging_status):
            widget.setText(text)
            widget.setVisible(bool(text))

    def _preference_changed(self, key, value):
        if key == "flight_logging.automatic":
            self.automatic_logging_changed.emit(bool(value))
        if not self._syncing:
            QTimer.singleShot(0, self, self.refresh_preferences)

    # SimConnect page ------------------------------------------------------

    def _build_simconnect(self, layout):
        self.diagnostics_card, card = _card(
            "Connection Diagnostics",
            "Technical details of Black Box Core's single SimConnect session with MSFS 2020. "
            "BBC EFB only reads from the simulator in this build.")
        self.diagnostics_card.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.diagnostics_card.setAccessibleName("Connection Diagnostics")
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.diag_fields: dict[str, QLabel] = {}
        for key, title in (("state", "State"), ("detail", "Detail"), ("since", "Since"),
                           ("source", "Data source"), ("session", "Session id"), ("dll", "SimConnect.dll")):
            value = _label(NOT_AVAILABLE, selectable=True)
            value.setAccessibleName(title)
            self.diag_fields[key] = value
            form.addRow(_label(title, "muted", wrap=False), value)
        card.addLayout(form)
        layout.addWidget(self.diagnostics_card)

        times, times_layout = _card("Response times",
                                    "Measured by SimConnect on request. Measuring adds a little simulator traffic, "
                                    "so it only runs when you ask.")
        self.response_label = _label("Not measured yet.", selectable=True)
        self.response_label.setAccessibleName("Response times")
        self.measure_button = _button("Measure response times", self.measure, accessible="Measure response times")
        self.measure_button.setEnabled(self.measure_response_times is not None)
        if self.measure_response_times is None:
            self.response_label.setText("Response times are not available until Black Box Core is running.")
        times_layout.addWidget(self.response_label)
        row = QHBoxLayout()
        row.addWidget(self.measure_button)
        row.addStretch(1)
        times_layout.addLayout(row)
        layout.addWidget(times)

        errors, errors_layout = _card("Recent errors")
        self.errors_view = QPlainTextEdit()
        self.errors_view.setReadOnly(True)
        self.errors_view.setAccessibleName("Recent SimConnect errors")
        self.errors_view.setMinimumHeight(90)
        self.errors_view.setMaximumHeight(180)
        self.errors_view.setPlainText("No errors reported.")
        errors_layout.addWidget(self.errors_view)
        layout.addWidget(errors)

        dll, dll_layout = _card(
            "SimConnect.dll location",
            "BBC EFB does not include SimConnect.dll. It uses the copy from your MSFS SDK. If it is not found "
            "automatically, choose it here. A new choice is used from the next connection attempt; a "
            "session that is already connected is not interrupted. If BBC EFB started without finding "
            "SimConnect.dll, restart BBC EFB after choosing it.")
        self.dll_choice_label = _label("", "muted", selectable=True)
        dll_layout.addWidget(self.dll_choice_label)
        row = QHBoxLayout()
        self.choose_dll_button = _button("Choose SimConnect.dll…", self.choose_dll)
        self.clear_dll_button = _button("Use automatic search", self.clear_dll)
        row.addWidget(self.choose_dll_button)
        row.addWidget(self.clear_dll_button)
        row.addStretch(1)
        dll_layout.addLayout(row)
        layout.addWidget(dll)

    def refresh_diagnostics(self) -> ConnectionDiagnostics:
        notes = []
        if self.diagnostics_provider is None:
            info = ConnectionDiagnostics()
            notes.append("Black Box Core is not connected to Settings yet.")
        else:
            try:
                info = coerce_diagnostics(self.diagnostics_provider())
            except Exception as error:  # show it rather than hide it
                log.warning("Connection diagnostics failed: %s", error)
                info = ConnectionDiagnostics(detail=f"Diagnostics could not be read: {error}")
        if self._measured is not None:
            info = replace(info, response_times_s=self._measured[0], response_measured_at=self._measured[1])
        self.last_diagnostics = info
        fields = self.diag_fields
        fields["state"].setText(STATE_TEXT.get(info.state, info.state.capitalize() or NOT_AVAILABLE))
        fields["detail"].setText(info.detail or (notes[0] if notes else "No further detail."))
        fields["since"].setText(_when(info.since) if info.since else NOT_AVAILABLE)
        fields["source"].setText({"msfs2020": "MSFS 2020 (SimConnect)", "simulated": "Simulated test data"}.get(
            info.source, info.source) or NOT_AVAILABLE)
        fields["session"].setText(info.session_id or "No session")
        if info.dll_path:
            found = {True: "found", False: "NOT found", None: "not checked"}[info.dll_found]
            fields["dll"].setText(f"{info.dll_path} ({found})")
        elif info.dll_found is False:
            where = ("\nLooked in:\n" + "\n".join(info.dll_candidates)) if info.dll_candidates else ""
            fields["dll"].setText("SimConnect.dll was not found. Install the MSFS SDK or choose the file below." + where)
        else:
            fields["dll"].setText(NOT_AVAILABLE)
        if self._measuring:
            self.response_label.setText("Measuring…")
        elif self._measure_error:
            self.response_label.setText(self._measure_error)
        elif info.response_times_s:
            rows = format_response_times(info.response_times_s)
            stamp = f"Measured {_when(info.response_measured_at)}." if info.response_measured_at else "Measured."
            self.response_label.setText("\n".join(f"{name}: {value}" for name, value in rows) + "\n" + stamp)
        elif self.measure_response_times is not None:
            self.response_label.setText("Not measured yet.")
        self.measure_button.setEnabled(self.measure_response_times is not None and not self._measuring
                                       and info.state == "connected")
        if self.measure_response_times is not None and info.state != "connected" and not self._measuring:
            self.measure_button.setToolTip("Available while connected to the simulator.")
        else:
            self.measure_button.setToolTip("")
        text = "\n".join(info.last_errors) if info.last_errors else "No errors reported."
        if info.notes:
            text += "\n\n" + "\n".join(info.notes)
        if self.errors_view.toPlainText() != text:
            self.errors_view.setPlainText(text)
        chosen = self._pref("simconnect.dll_path")
        self.dll_choice_label.setText(self._dll_choice_text(chosen, info))
        self.clear_dll_button.setEnabled(bool(chosen) and self.preferences is not None)
        self.choose_dll_button.setEnabled(self.preferences is not None)
        return info

    def _dll_choice_text(self, chosen: Any, info: ConnectionDiagnostics) -> str:
        """What Settings says about the chosen DLL.

        With a running Core host (``info.dll_live``) the choice is read again before every
        connection attempt (``bbc_efb.app.SimConnectDllSwitch``). Without one (no DLL was
        found at start-up) it is read only when BBC EFB starts.
        """
        in_use = info.dll_path if info.dll_found else None
        session = (f"This session uses {in_use}." if in_use
                   else "This session has no SimConnect.dll." if info.dll_found is False else "")
        if info.dll_live and in_use:
            connected = info.state == "connected"
            when = ("after the current session ends, at the next connection attempt" if connected
                    else "at the next connection attempt")
            if chosen:
                text = f"Chosen file: {chosen}"
                if not _same(Path(str(chosen)), Path(str(in_use))):
                    text += (f"\nBBC EFB switches to this file {when}. Until then it uses {in_use}. If the file "
                             "is missing by then, the automatic search is used instead.")
                return text
            text = "Automatic search (MSFS SDK locations)."
            if self._dll_choice_changed:
                text += f" BBC EFB searches again {when}. " + session
            return text.rstrip()
        if chosen:
            text = f"Chosen file: {chosen}"
            if in_use is None or not _same(Path(str(chosen)), Path(str(in_use))):
                text += "\nRestart BBC EFB to use this file. " + session
            return text.rstrip()
        text = "Automatic search (MSFS SDK locations)."
        if self._dll_choice_changed:
            text += " Restart BBC EFB to use it. " + session
        return text.rstrip()

    @Slot(object)
    def on_connection_changed(self, _connection=None):
        """Connect ``CoreBridge.connection_changed`` here."""
        if self.current_page == "simconnect":
            self.refresh_diagnostics()

    def measure(self):
        if self.measure_response_times is None or self._measuring:
            return
        self._measuring = True
        self._measure_error = None
        self.refresh_diagnostics()

        def done(result, error):
            self._measuring = False
            if error is not None:
                self._measure_error = f"Response times could not be measured: {error}"
            elif not result:
                self._measure_error = "SimConnect returned no response times."
            else:
                try:
                    self._measured = (tuple(float(item) for item in result), datetime.now(timezone.utc))
                except (TypeError, ValueError):
                    self._measure_error = "SimConnect returned response times in an unexpected form."
            self.refresh_diagnostics()

        self._run_task(self.measure_response_times, done)

    def choose_dll(self):
        current = self._pref("simconnect.dll_path")
        chosen = self.dialogs.choose_open_file("Choose SimConnect.dll", Path(current).parent if current else None,
                                               "SimConnect library (SimConnect.dll)")
        if chosen is None:
            return
        if chosen.name.lower() != "simconnect.dll" or not chosen.is_file():
            self.dialogs.warn("Not SimConnect.dll", f"{chosen}\n\nChoose the file named SimConnect.dll from the "
                                                    "MSFS SDK (SimConnect SDK\\lib).")
            return
        self._set_pref("simconnect.dll_path", str(chosen))
        self._dll_choice_changed = True
        self.refresh_diagnostics()

    def clear_dll(self):
        self._set_pref("simconnect.dll_path", None)
        self._dll_choice_changed = True
        self.refresh_diagnostics()

    # Flight Logging & Privacy -------------------------------------------------

    def _build_flight_logging(self, layout):
        logging_card, card = _card("Automatic Flight Logging")
        self.automatic_check = QCheckBox("Log flights automatically from simulator data")
        self.automatic_check.setAccessibleName("Automatic Flight Logging")
        self.automatic_check.toggled.connect(self._automatic_toggled)
        card.addWidget(self.automatic_check)
        card.addWidget(_label("Start and Stop Logging stay available in the Flight Logging status at the bottom of "
                              "the window. Changing this does not stop a flight that is already being logged. "
                              "Flight Logging only reads simulator data; it never changes the simulator.", "muted"))
        self.logging_status = _label("", "warning")
        self.logging_status.hide()
        card.addWidget(self.logging_status)
        layout.addWidget(logging_card)

        saved, saved_layout = _card("What is saved")
        planned = self.recorded_items is None
        if planned:
            saved_layout.addWidget(_label("Planned inventory: the exact list is confirmed when the Flight Logging "
                                          "rules are approved. Values the simulator does not provide stay unknown.",
                                          "muted"))
        self.recorded_label = _label(_bullets(self.recorded_items or PLANNED_RECORDED_ITEMS))
        saved_layout.addWidget(self.recorded_label)
        saved_layout.addWidget(_label("Not saved", "cardTitle", wrap=False))
        saved_layout.addWidget(_label(_bullets(NOT_RECORDED_ITEMS)))
        layout.addWidget(saved)

        where, where_layout = _card("Where it is kept")
        self.privacy_location = _label("", selectable=True)
        where_layout.addWidget(self.privacy_location)
        layout.addWidget(where)

        sources, sources_layout = _card("Sources and provenance")
        sources_layout.addWidget(_label(
            "Each flight is marked as entered by you, logged from the simulator, or imported. Every field keeps its "
            "original value and the history of your corrections. Data from the simulated test provider is always "
            "labelled simulated."))
        layout.addWidget(sources)

        retention, retention_layout = _card("Keeping and deleting")
        retention_layout.addWidget(_label("Flights are kept until you delete them. BBC EFB never deletes flights "
                                          "automatically."))
        row = QHBoxLayout()
        row.addWidget(_label("Keep Flight Logging journals for", wrap=False))
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 36500)
        self.retention_spin.setSpecialValueText("Until I delete them")
        self.retention_spin.setSuffix(" days")
        self.retention_spin.setAccessibleName("Journal retention in days")
        self.retention_spin.valueChanged.connect(self._retention_changed)
        row.addWidget(self.retention_spin)
        row.addStretch(1)
        retention_layout.addLayout(row)
        retention_layout.addWidget(_label("Saved for the Flight Recorder. Automatic journal clean-up is not active in "
                                          "this build, so nothing is deleted yet.", "muted"))
        retention_layout.addWidget(_label(self.delete_text))
        layout.addWidget(retention)

        review, review_layout = _card("Reviewing, exporting and transfers")
        review_layout.addWidget(_label("Review flights in Logbook > Flight History. Use Data Management > Export All "
                                       "Data for a readable copy."))
        review_layout.addWidget(_label(TRANSFER_TEXT))
        row = QHBoxLayout()
        row.addWidget(_button("Open Data Management", lambda: self.show_page("data_management")))
        row.addStretch(1)
        review_layout.addLayout(row)
        layout.addWidget(review)

    def _automatic_toggled(self, checked):
        if self._syncing:
            return
        self._set_pref("flight_logging.automatic", bool(checked))

    def _retention_changed(self, value):
        if self._syncing:
            return
        self._set_pref("flight_logging.journal_retention_days", value or None)

    def _privacy_location_text(self):
        folder = self.folders.current()
        lines = []
        if folder is None:
            lines.append("No data folder is chosen yet. Flights cannot be saved until you choose one in "
                         "Data Management.")
        else:
            lines.append(f"Data folder: {folder}")
            lines.append(f"Flights: {folder / dm.FLIGHTS_FOLDER} (one JSON file per flight)")
            lines.append("Flight Logging journals and diagnostic logs are kept in the same data folder.")
        path = getattr(self.preferences, "path", None)
        if path:
            lines.append(f"Display preferences and the data folder location: {Path(path).parent}")
        lines.append("Everything stays on this computer.")
        return "\n".join(lines)

    # Data Management -----------------------------------------------------------

    def _build_data_management(self, layout):
        folder, folder_layout = _card("Data folder", "Your Logbook is stored as plain files in this folder.")
        self.folder_label = _label("", selectable=True)
        self.folder_label.setAccessibleName("Current data folder")
        self.folder_summary = _label("", "muted")
        self.folder_notes = _label("", "warning")
        self.folder_notes.hide()
        folder_layout.addWidget(self.folder_label)
        folder_layout.addWidget(self.folder_summary)
        folder_layout.addWidget(self.folder_notes)
        row = QHBoxLayout()
        self.change_folder_button = _button("Change folder…", self.change_folder)
        self.show_folder_button = _button("Show folder", self.show_folder)
        row.addWidget(self.change_folder_button)
        row.addWidget(self.show_folder_button)
        row.addStretch(1)
        folder_layout.addLayout(row)
        folder_layout.addWidget(_label("Changing the folder does not move your flights. They stay in the old folder; "
                                       "to bring them along, create a backup and restore it into the new folder.",
                                       "muted"))
        layout.addWidget(folder)

        export, export_layout = _card(
            "Export All Data",
            "A readable copy for spreadsheets or other programs: a ZIP with every flight as JSON, a CSV summary "
            "and a README. An export is not a backup and cannot be restored.")
        row = QHBoxLayout()
        self.export_button = _button("Export All Data…", self.export_all, "primary")
        row.addWidget(self.export_button)
        row.addStretch(1)
        export_layout.addLayout(row)
        layout.addWidget(export)

        backup, backup_layout = _card(
            "Backup and restore",
            "A backup is a complete snapshot that BBC EFB can check and restore: every file with a checksum, "
            "plus your display preferences. Restoring never overwrites anything; existing data is set aside.")
        row = QHBoxLayout()
        self.backup_button = _button("Create Backup…", self.create_backup, "primary")
        self.restore_button = _button("Restore Backup…", self.restore_backup)
        row.addWidget(self.backup_button)
        row.addWidget(self.restore_button)
        row.addStretch(1)
        backup_layout.addLayout(row)
        layout.addWidget(backup)

        self.data_status = _label("", "muted", selectable=True)
        self.data_status.setAccessibleName("Data Management status")
        layout.addWidget(self.data_status)

    def refresh_data_folder(self):
        folder = self.folders.current()
        if folder is None:
            self.folder_label.setText("No data folder is chosen yet.")
            self.folder_summary.setText("Choose a folder to start keeping your Logbook.")
            self.folder_notes.hide()
        else:
            self.folder_label.setText(str(folder))
            summary = dm.summarize_data_folder(folder)
            self.folder_summary.setText(summary.describe())
            notes = dm.folder_notes(folder)
            self.folder_notes.setText(_bullets(notes))
            self.folder_notes.setVisible(bool(notes))
        available = folder is not None and folder.is_dir()
        self.change_folder_button.setText("Change folder…" if folder else "Choose folder…")
        self.change_folder_button.setEnabled(self.folders.can_change and not self._busy)
        self.show_folder_button.setEnabled(available)
        self.export_button.setEnabled(available and not self._busy)
        self.backup_button.setEnabled(available and not self._busy)
        self.restore_button.setEnabled(not self._busy)
        if hasattr(self, "privacy_location"):
            self.privacy_location.setText(self._privacy_location_text())

    def _set_busy(self, busy, text=""):
        self._busy = busy
        if text:
            self.data_status.setText(text)
        self.refresh_data_folder()

    def change_folder(self):
        if not self.folders.can_change:
            return
        current = self.folders.current()
        chosen = choose_data_folder(self, dialogs=self.dialogs, suggested=current or suggested_data_folder(),
                                    first_launch=current is None)
        if chosen is None:
            return
        if current is not None and _same(chosen, current):
            self.data_status.setText("That is already the data folder.")
            return
        try:
            self.folders.change(chosen)
        except Exception as error:
            self.dialogs.warn("The data folder was not changed", f"{chosen}\n\n{error}")
            return
        self.data_status.setText(f"Data folder changed to {chosen}.")
        self.refresh_data_folder()
        self.data_folder_changed.emit(chosen)

    def show_folder(self):
        folder = self.folders.current()
        if folder is not None and folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _ask_destination(self, title, default_name):
        start = documents_folder() / default_name
        chosen = self.dialogs.choose_save_file(title, start, "ZIP files (*.zip)")
        if chosen is None:
            return None, False
        if chosen.suffix.lower() != ".zip":
            chosen = chosen.with_name(chosen.name + ".zip")
        overwrite = False
        if chosen.exists():
            if not self.dialogs.confirm("Replace file?", f"{chosen.name} already exists. Replace it?",
                                        accept="Replace"):
                return None, False
            overwrite = True
        return chosen, overwrite

    def export_all(self):
        folder = self.folders.current()
        if folder is None or self._busy:
            return
        destination, overwrite = self._ask_destination("Export All Data", dm.default_export_name())
        if destination is None:
            return
        self._set_busy(True, "Exporting…")

        def done(result, error):
            self._set_busy(False)
            if error is not None:
                self.data_status.setText(f"Export failed: {error}")
                self.dialogs.warn("Export failed", str(error))
                return
            self.data_status.setText(result.describe())
            self.dialogs.inform("Export complete", result.describe())

        self._run_task(lambda: dm.export_all_data(folder, destination, app_version=self.app_version,
                                                  overwrite=overwrite), done)

    def create_backup(self):
        folder = self.folders.current()
        if folder is None or self._busy:
            return
        destination, overwrite = self._ask_destination("Create Backup", dm.default_backup_name())
        if destination is None:
            return
        config = {}
        path = getattr(self.preferences, "path", None)
        if path and Path(path).is_file():
            config["preferences.json"] = Path(path)
        self._set_busy(True, "Creating backup…")

        def done(result, error):
            self._set_busy(False)
            if error is not None:
                self.data_status.setText(f"Backup failed: {error}")
                self.dialogs.warn("Backup failed", str(error))
                return
            self.data_status.setText(result.describe())
            if result.skipped:
                self.dialogs.warn("Backup incomplete", result.describe())
            else:
                self.dialogs.inform("Backup complete", result.describe())

        self._run_task(lambda: dm.create_backup(folder, destination, app_version=self.app_version,
                                                record_schema=self.record_schema, config_files=config,
                                                overwrite=overwrite), done)

    def restore_backup(self):
        if self._busy:
            return
        chosen = self.dialogs.choose_open_file("Restore Backup", documents_folder(), "BBC EFB backups (*.zip)")
        if chosen is None:
            return
        self._set_busy(True, "Checking the backup…")

        def inspected(inspection, error):
            self._set_busy(False)
            if error is not None:
                self.dialogs.warn("Backup not checked", str(error))
                self.data_status.setText(f"The backup could not be checked: {error}")
                return
            if not inspection.ok:
                self.data_status.setText("The backup cannot be restored.")
                self.dialogs.warn("This backup cannot be restored", inspection.describe())
                return
            self._continue_restore(chosen, inspection)

        self._run_task(lambda: dm.inspect_backup(chosen), inspected)

    def _continue_restore(self, backup, inspection):
        if not self.dialogs.confirm("Restore this backup?", inspection.describe() +
                                    "\n\nNext, choose an empty folder to restore into (or the current data folder; "
                                    "its contents would be set aside, not deleted).", accept="Choose folder"):
            return
        current = self.folders.current()
        target = self.dialogs.choose_folder("Restore into folder", current.parent if current else documents_folder())
        if target is None:
            return
        target = Path(os.path.abspath(target))
        into_current = current is not None and _same(target, current)
        if into_current and not self.folders.can_release:
            self.dialogs.warn("Choose another folder", "The data folder in use cannot be replaced while BBC EFB "
                              "has it open. Restore into an empty folder and then switch to it.")
            return
        replace_existing = False
        entries = dm.folder_entries(target) if target.is_dir() else ()
        if entries:
            kept = f"{target.name}.pre-restore-<date and time>"
            if not self.dialogs.confirm(
                    "Folder is not empty",
                    f"{target} already contains {len(entries)} item(s).\n\nIf you continue, the current contents are "
                    f"renamed to '{kept}' beside it (nothing is deleted) and the backup is restored in their place.",
                    accept="Set aside and restore"):
                return
            replace_existing = True
        if into_current:
            try:
                self.folders.release()
            except Exception as error:
                self.dialogs.warn("Restore not started", f"The data folder could not be closed: {error}")
                return
        self._set_busy(True, "Restoring…")

        def restored(result, error):
            self._set_busy(False)
            if into_current:
                try:
                    self.folders.change(target)
                except Exception as reopen_error:
                    self.dialogs.warn("Data folder not reopened", f"{target}\n\n{reopen_error}")
            if error is not None:
                self.data_status.setText(f"Restore failed: {error}")
                self.dialogs.warn("Restore failed", str(error))
                return
            self.data_status.setText(result.describe())
            self.dialogs.inform("Restore complete", result.describe())
            if into_current:
                self.data_folder_changed.emit(target)
            elif self.folders.can_change and self.dialogs.confirm(
                    "Use the restored folder?", f"Switch the Logbook to {target} now?", accept="Switch",
                    reject="Keep current folder", default_accept=True):
                try:
                    self.folders.change(target)
                    self.data_folder_changed.emit(target)
                except Exception as change_error:
                    self.dialogs.warn("The data folder was not changed", f"{target}\n\n{change_error}")
            prefs = result.config.get("preferences.json")
            if prefs and hasattr(self.preferences, "adopt") and self.dialogs.confirm(
                    "Restore display preferences?", "The backup also holds your display and layout preferences. "
                    "Use them instead of the current ones?", accept="Use backup preferences", reject="Keep current"):
                try:
                    self.preferences.adopt(prefs)
                except Exception as adopt_error:
                    self.dialogs.warn("Preferences not restored", str(adopt_error))
            self.refresh()

        self._run_task(lambda: dm.restore_backup(backup, target, replace_existing=replace_existing), restored)

    # Layout ------------------------------------------------------------------------

    def _build_layout(self, layout):
        logbook, logbook_layout = _card("Logbook display", "The on-page selectors stay available; these are the "
                                                           "choices BBC EFB starts with.")
        form = QFormLayout()
        self.history_view_combo = QComboBox()
        for key, title in HISTORY_VIEWS.items():
            self.history_view_combo.addItem(title, key)
        self.history_view_combo.setAccessibleName("Flight History view")
        self.history_view_combo.currentIndexChanged.connect(
            lambda _: self._combo_changed("logbook.history_view", self.history_view_combo))
        form.addRow("Flight History view", self.history_view_combo)
        self.logbook_density_combo = QComboBox()
        for key, title in DENSITIES.items():
            self.logbook_density_combo.addItem(title, key)
        self.logbook_density_combo.setAccessibleName("Logbook density")
        self.logbook_density_combo.currentIndexChanged.connect(
            lambda _: self._combo_changed("logbook.density", self.logbook_density_combo))
        form.addRow("Density", self.logbook_density_combo)
        logbook_layout.addLayout(form)
        logbook_layout.addWidget(_label("Dashboard panels", "cardTitle", wrap=False))
        self.panel_checks: dict[tuple[str, str], QCheckBox] = {}
        for panel, title in LOGBOOK_PANELS.items():
            logbook_layout.addWidget(self._panel_check("logbook.dashboard_panels", panel, title))
        layout.addWidget(logbook)

        inflight, inflight_layout = _card("In-Flight display")
        form = QFormLayout()
        self.inflight_density_combo = QComboBox()
        for key, title in DENSITIES.items():
            self.inflight_density_combo.addItem(title, key)
        self.inflight_density_combo.setAccessibleName("In-Flight density")
        self.inflight_density_combo.currentIndexChanged.connect(
            lambda _: self._combo_changed("inflight.density", self.inflight_density_combo))
        form.addRow("Density", self.inflight_density_combo)
        inflight_layout.addLayout(form)
        for panel, title in INFLIGHT_PANELS.items():
            if panel == "modules_launcher":
                continue  # the launcher must stay available; only its start-up state is a preference
            inflight_layout.addWidget(self._panel_check("inflight.panels", panel, title))
        self.launcher_check = QCheckBox("Start with the Modules launcher collapsed")
        self.launcher_check.toggled.connect(lambda value: self._check_changed("inflight.launcher_collapsed", value))
        inflight_layout.addWidget(self.launcher_check)
        layout.addWidget(inflight)

        window, window_layout = _card("Window")
        self.maximized_check = QCheckBox("Start maximised")
        self.maximized_check.toggled.connect(lambda value: self._check_changed("window.maximized", value))
        window_layout.addWidget(self.maximized_check)
        window_layout.addWidget(_label("The window size and position are remembered automatically.", "muted"))
        row = QHBoxLayout()
        self.reset_layout_button = _button("Reset layout preferences", self.reset_layout)
        row.addWidget(self.reset_layout_button)
        row.addStretch(1)
        window_layout.addLayout(row)
        window_layout.addWidget(_label("Moving, docking and saved arrangements of panels come in a later version.",
                                       "muted"))
        self.layout_status = _label("", "warning")
        self.layout_status.hide()
        window_layout.addWidget(self.layout_status)
        layout.addWidget(window)

    # Modules (experimental) -----------------------------------------------

    def module_host(self):
        """The ``bbc_efb.modules.ModuleHost`` for the open data folder, or None."""
        modules = self._modules
        if callable(modules) and not hasattr(modules, "records"):
            try:
                return modules()
            except Exception:  # noqa: BLE001
                log.exception("The module host could not be read")
                return None
        return modules

    def _build_modules(self, layout):
        try:
            from .modules import EXPERIMENTAL_LABEL, TRUST_NOTICE
        except ImportError:  # pragma: no cover - part of every build
            EXPERIMENTAL_LABEL, TRUST_NOTICE = "Experimental", ""
        self.trust_notice = TRUST_NOTICE
        card, card_layout = _card(
            f"Drop-in modules ({EXPERIMENTAL_LABEL})",
            "Drop-in modules are folders placed in the modules folder inside your data folder. They are off by "
            "default. Built-in features do not depend on them.")
        card_layout.addWidget(_label(TRUST_NOTICE, "warning"))
        self.modules_switch = QCheckBox("Allow drop-in modules (experimental)")
        self.modules_switch.setAccessibleName("Allow drop-in modules, experimental")
        self.modules_switch.toggled.connect(self._modules_switch_toggled)
        card_layout.addWidget(self.modules_switch)
        self.modules_message = _label("", "muted", selectable=True)
        card_layout.addWidget(self.modules_message)
        row = QHBoxLayout()
        self.modules_rescan_button = _button("Rescan modules folder", self.rescan_modules)
        row.addWidget(self.modules_rescan_button)
        row.addStretch(1)
        card_layout.addLayout(row)
        layout.addWidget(card)
        listing, listing_layout = _card("Modules found")
        self.modules_list_layout = QVBoxLayout()
        self.modules_list_layout.setSpacing(8)
        listing_layout.addLayout(self.modules_list_layout)
        self.modules_ignored = _label("", "muted", selectable=True)
        listing_layout.addWidget(self.modules_ignored)
        layout.addWidget(listing)
        self.module_rows: dict[str, dict[str, Any]] = {}

    def refresh_modules(self) -> None:
        """Show the module host's switch, records and ignored folders (nothing is loaded here)."""
        host = self.module_host()
        self._syncing = True
        try:
            # Settings written by a newer EFB are shown (settings_problem) but cannot be changed here.
            read_only = bool(getattr(host, "settings_read_only", False))
            self.modules_switch.setEnabled(host is not None and not read_only)
            self.modules_rescan_button.setEnabled(host is not None)
            self.modules_switch.setChecked(bool(getattr(host, "experimental_enabled", False)))
        finally:
            self._syncing = False
        while self.modules_list_layout.count():
            item = self.modules_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.module_rows = {}
        if host is None:
            self.modules_message.setText("Drop-in modules need an open, writable data folder. Choose one in "
                                         "Data Management.")
            self.modules_ignored.setText("")
            return
        messages = [getattr(host, "feature_message", lambda: "")() or ""]
        problem = getattr(host, "settings_problem", "") or ""
        if problem:
            messages.append(problem)
        if bool(getattr(host, "settings_read_only", False)) and "newer EFB" not in problem:
            messages.append(_newer_module_settings_text())
        self.modules_message.setText("\n".join(text for text in messages if text))
        try:
            records = host.records()
        except Exception as error:  # noqa: BLE001
            records = ()
            self.modules_message.setText(f"The modules could not be listed: {error}")
        if not records:
            self.modules_list_layout.addWidget(_label("No modules were found.", "muted"))
        for record in records:
            self.modules_list_layout.addWidget(self._module_row(host, record))
        ignored = getattr(host, "ignored", lambda: ())()
        self.modules_ignored.setText(
            "Ignored: " + "; ".join(f"{Path(path).name}: {reason}" for path, reason in ignored) if ignored else "")

    def _module_row(self, host, record):
        frame = QFrame()
        frame.setObjectName("card")
        row = QVBoxLayout(frame)
        row.setContentsMargins(10, 8, 10, 8)
        title = f"{record.name}{' (built in)' if record.builtin else ''}: {record.summary}"
        row.addWidget(_label(title, wrap=True))
        for warning in getattr(record, "warnings", ()) or ():
            row.addWidget(_label(getattr(warning, "message", str(warning)), "muted"))
        controls = QHBoxLayout()
        enabled = QCheckBox("On")
        enabled.setAccessibleName(f"{record.name} on")
        enabled.setChecked(bool(record.enabled))
        read_only = bool(getattr(host, "settings_read_only", False))
        enabled.setEnabled((bool(getattr(host, "experimental_enabled", False)) or record.builtin) and not read_only)
        enabled.toggled.connect(lambda value, key=record.key: self.set_module_enabled(key, value))
        controls.addWidget(enabled)
        grant = None
        if record.missing_groups:
            grant = _button("Grant requested access…", lambda checked=False, key=record.key: self.grant_module(key))
            grant.setEnabled(not read_only)
            controls.addWidget(grant)
        restart = _button("Restart module", lambda checked=False, key=record.key: self.restart_module(key))
        restart.setEnabled(str(getattr(record.status, "value", record.status)) == "loaded")
        controls.addWidget(restart)
        controls.addStretch(1)
        row.addLayout(controls)
        self.module_rows[record.key] = {"enabled": enabled, "grant": grant, "restart": restart, "frame": frame}
        return frame

    def _modules_switch_toggled(self, value):
        if not self._syncing:
            self.set_modules_enabled(value)

    def set_modules_enabled(self, enabled: bool) -> bool:
        """Turn the experimental switch on (after the trust notice is accepted) or off."""
        host = self.module_host()
        if host is None:
            self.refresh_modules()
            return False
        if enabled and not self.dialogs.confirm(
                "Allow drop-in modules?", self.trust_notice, "Allow modules", "Cancel", default_accept=False):
            self.refresh_modules()
            return False
        try:
            host.set_experimental_enabled(bool(enabled))
            if enabled:
                host.scan()
                host.load_enabled()
        except Exception as error:  # noqa: BLE001 - report, never crash Settings
            log.exception("Changing the drop-in module switch failed")
            self.dialogs.warn("Modules", f"The module setting could not be changed: {error}")
        self.refresh_modules()
        return bool(getattr(host, "experimental_enabled", False)) == bool(enabled)

    def set_module_enabled(self, key: str, enabled: bool) -> None:
        if self._syncing:
            return
        host = self.module_host()
        if host is None:
            return
        try:
            host.set_enabled(key, bool(enabled))
            if enabled and getattr(host, "experimental_enabled", False):
                host.load(key)
            elif not enabled:
                host.unload(key)
        except Exception as error:  # noqa: BLE001
            log.exception("Module %s could not be switched", key)
            self.dialogs.warn("Modules", f"The module could not be switched {'on' if enabled else 'off'}: {error}")
        self.refresh_modules()

    def grant_module(self, key: str) -> bool:
        """Grant the control groups a module asks for, after showing each group's warning."""
        host = self.module_host()
        record = host.record(key) if host is not None else None
        if record is None or not record.missing_groups:
            return False
        try:
            from .modules import DEFAULT_CONTROL_GROUPS
        except ImportError:  # pragma: no cover
            DEFAULT_CONTROL_GROUPS = {}
        lines = []
        for name in record.missing_groups:
            group = DEFAULT_CONTROL_GROUPS.get(name)
            text = f"{name}: {group.description}" if group is not None else name
            if group is not None and group.warning:
                text += f" WARNING: {group.warning}"
            lines.append(text)
        question = (f"{record.name} asks to send these simulator commands through Black Box Core:\n\n"
                    + _bullets(lines) + "\n\nThe grant takes effect the next time the module starts.")
        if not self.dialogs.confirm("Grant module access?", question, "Grant", "Cancel", default_accept=False):
            return False
        try:
            host.grant(key, record.missing_groups)
        except Exception as error:  # noqa: BLE001
            self.dialogs.warn("Modules", f"The access could not be granted: {error}")
            return False
        self.refresh_modules()
        return True

    def restart_module(self, key: str) -> None:
        host = self.module_host()
        if host is None:
            return
        try:
            host.reload(key)
        except Exception as error:  # noqa: BLE001
            self.dialogs.warn("Modules", f"The module could not be restarted: {error}")
        self.refresh_modules()

    def rescan_modules(self) -> None:
        host = self.module_host()
        if host is None:
            return
        try:
            host.scan()
        except Exception as error:  # noqa: BLE001
            self.dialogs.warn("Modules", f"The modules folder could not be read: {error}")
        self.refresh_modules()

    def _panel_check(self, key, panel, title):
        check = QCheckBox(f"Show {title.lower()}" if not title.startswith("Show") else title)
        check.setAccessibleName(f"{title} visible")
        check.toggled.connect(lambda value: self._panel_changed(key, panel, value))
        self.panel_checks[(key, panel)] = check
        return check

    def _combo_changed(self, key, combo):
        if not self._syncing:
            self._set_pref(key, combo.currentData())

    def _check_changed(self, key, value):
        if not self._syncing:
            self._set_pref(key, bool(value))

    def _panel_changed(self, key, panel, value):
        if self._syncing or self.preferences is None:
            return
        set_one = getattr(self.preferences, "set_panel_visible", None)
        if not callable(set_one):
            panels = dict(self._pref(key, {}) or {})
            panels[panel] = bool(value)
            self._set_pref(key, panels)
            return
        # Change only this panel, so stored entries this build cannot use are kept as they are.
        try:
            set_one(key, panel, bool(value))
        except PreferencesError as error:
            self._show_pref_status(str(error))
            return
        self._save_prefs()

    def reset_layout(self):
        if self.preferences is None:
            return
        if not self.dialogs.confirm("Reset layout preferences?", "Logbook and In-Flight display choices, panel "
                                    "visibility and the remembered window size return to their defaults.",
                                    accept="Reset"):
            return
        self.preferences.reset(LAYOUT_KEYS)
        reason = getattr(self.preferences, "read_only_reason", None)
        try:
            if reason:
                self._show_pref_status(reason)
            else:
                self.preferences.save()
                self._show_pref_status("")
        except (PreferencesSaveError, OSError) as error:
            self._show_pref_status(f"Reset applies now but could not be saved: {error}")
        self.refresh_preferences()

    def refresh_preferences(self):
        enabled = self.preferences is not None
        self._syncing = True
        try:
            self.automatic_check.setChecked(bool(self._pref("flight_logging.automatic", True)))
            self.retention_spin.setValue(self._pref("flight_logging.journal_retention_days", None) or 0)
            for combo, key in ((self.history_view_combo, "logbook.history_view"),
                               (self.logbook_density_combo, "logbook.density"),
                               (self.inflight_density_combo, "inflight.density")):
                index = combo.findData(self._pref(key))
                combo.setCurrentIndex(max(0, index))
            for (key, panel), check in self.panel_checks.items():
                check.setChecked(bool((self._pref(key, {}) or {}).get(panel, True)))
            self.launcher_check.setChecked(bool(self._pref("inflight.launcher_collapsed", False)))
            self.maximized_check.setChecked(bool(self._pref("window.maximized", False)))
        finally:
            self._syncing = False
        for widget in (self.automatic_check, self.retention_spin, self.history_view_combo,
                       self.logbook_density_combo, self.inflight_density_combo, self.launcher_check,
                       self.maximized_check, self.reset_layout_button, *self.panel_checks.values()):
            widget.setEnabled(enabled)
        messages = []
        if not enabled:
            messages.append("Preferences are not available, so changes cannot be made.")
        else:
            for attr in ("problem", "read_only_reason"):
                text = getattr(self.preferences, attr, None)
                if text:
                    messages.append(text)
        current = self.layout_status.text()
        if messages and not current:
            self._show_pref_status(" ".join(messages))

    # Tasks -------------------------------------------------------------------------

    def _thread_task(self, fn, done):
        def work():
            result, error = _call(fn)
            self._relay.finished.emit((done, result, error))
        threading.Thread(target=work, name="settings-task", daemon=True).start()

    @Slot(object)
    def _task_finished(self, payload):
        done, result, error = payload
        done(result, error)

    @property
    def busy(self) -> bool:
        return self._busy

    def closeEvent(self, event):
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        super().closeEvent(event)


def _same(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        a, b = os.path.abspath(first), os.path.abspath(second)
        return os.path.normcase(a) == os.path.normcase(b)


__all__ = [
    "ConnectionDiagnostics", "PAGES", "QtDialogs", "SettingsPage", "choose_data_folder", "coerce_diagnostics",
    "documents_folder", "format_response_times", "run_inline", "suggested_data_folder",
]
