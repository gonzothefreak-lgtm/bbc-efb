"""EFB Sections: the registry the shell loads on demand, and the honest placeholders.

The shell (``bbc_efb.shell``) knows Sections only through ``SectionSpec``. Each spec
has a factory that builds the Section's widget the first time the pilot opens it,
so start-up only builds Dispatch (the launch Section) and the permanent chrome.

Dispatch and In-Flight are placeholders in this build. They list the planned
workflows and modules from the project sources and label every one of them
"Not available in this build". They show no sample values, so nothing here can be
mistaken for live data.

Logbook embeds ``bbc_efb.logbook.Logbook`` and Settings embeds
``bbc_efb.settings.SettingsPage``. Both are imported lazily. When the Settings
module is not in the build, ``BasicSettingsPage`` stands in: it keeps the
SimConnect Connection Diagnostics destination, the data folder and the Flight
Logging & Privacy explanation available and labels everything else unavailable.

The context shared with every factory is ``ShellServices``. ``bbc_efb.app`` fills it;
tests fill it with fakes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import inspect
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QScrollArea, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .footer import FlightLogging, UnavailableFlightLogging, describe_connection

log = logging.getLogger(__name__)

NOT_AVAILABLE = "Not available in this build"

DISPATCH = "dispatch"
IN_FLIGHT = "inflight"
LOGBOOK = "logbook"
SETTINGS = "settings"
LAUNCH_SECTION = DISPATCH
"""The approved start-up Section. The shell always opens here."""

DIAGNOSTICS_PAGE = "simconnect/diagnostics"
"""Settings > SimConnect > Connection Diagnostics."""
PRIVACY_PAGE = "flight-logging/privacy"
"""Settings > Flight Logging & Privacy."""
DATA_PAGE = "data-management"
"""Settings > Data Management."""

SETTINGS_MODULE = "bbc_efb.settings"


# ----------------------------------------------------------------------------
# Shared context
# ----------------------------------------------------------------------------


@dataclass(slots=True)
class ShellServices:
    """Everything the shell and its Sections may use. Every field is optional.

    ``bridge`` is a ``bbc_efb.core_bridge.CoreBridge`` (signals ``connection_changed``
    and ``updates``), ``host`` the ``bbc_core.host.CoreHost`` behind it, ``logging``
    the Flight Logging control the footer shows (see ``bbc_efb.footer``), ``store``
    the open ``FlightStore`` and ``folder_settings`` the ``DataFolderSettings`` that
    remembers the data folder. ``initial_connection`` is shown until the bridge
    reports, which matters when no Core host could be created at all.
    ``startup_notes`` are plain-English facts about how this session started, shown
    in Connection Diagnostics (for example that SimConnect.dll was not found).
    """

    bridge: Any = None
    host: Any = None
    logging: FlightLogging = field(default_factory=UnavailableFlightLogging)
    store: Any = None
    folder_settings: Any = None
    data_dir: Path | None = None
    initial_connection: Any = None
    simulated: bool = False
    dll_path: Path | None = None
    dll_switch: Any = None
    """``bbc_efb.app.SimConnectDllSwitch`` when a new SimConnect.dll choice applies without a restart."""
    startup_notes: tuple[str, ...] = ()
    logging_config: Any = None
    version: str = ""
    settings_module: str = SETTINGS_MODULE
    """Module that provides ``SettingsPage``. Tests point this elsewhere."""
    shutdown_hooks: list[Callable[[], None]] = field(default_factory=list)
    preferences: Any = None
    """``bbc_efb.preferences.Preferences`` (basic saved UI preferences), or None."""
    data_folder: Any = None
    """What Settings uses to show and change the data folder (``DataFolderControl``)."""
    diagnostics: Callable[[], Any] | None = None
    """Returns what Connection Diagnostics shows; ``default_diagnostics`` when None."""
    measure_response_times: Callable[[], Any] | None = None
    notices: list[str] = field(default_factory=list)
    """Pilot-facing notices not shown yet (for example flights recovered at start-up)."""
    module_host: Any = None
    """``bbc_efb.modules.ModuleHost`` for the open data folder (experimental, off by default)."""

    def take_notices(self) -> tuple[str, ...]:
        """Notices to show now; each is returned once."""
        taken = tuple(self.notices)
        self.notices.clear()
        return taken

    def current_store(self, logbook: Any = None) -> Any:
        """The store in use. The Logbook may have switched folders since start-up."""
        store = getattr(logbook, "store", None) if logbook is not None else None
        return store if store is not None else self.store


class DataFolderControl:
    """The data folder as Settings and the Logbook see it: ``current()``, ``change(path)``, ``release()``.

    The app owns the single ``FlightStore`` (contract C1); this control is the only
    way to switch it. ``change`` opens the new folder for writing, remembers it, makes
    it the store every part of the EFB uses (Logbook, Settings and the Recorder's
    Logbook sink, through ``listeners``), moves the log file into the new folder and
    closes the old store. Choosing the folder that is already open just returns it
    (it is never opened a second time, which the writer lock would refuse).

    It refuses while a flight is being logged, because that flight's crash-safe
    journal lives in the current folder, and when a ``vetoes`` callable returns a
    reason (the shell asks the Logbook about unsaved edits). ``release`` first moves
    the log out of the folder, so a restore can rename it on Windows. Errors are
    ``OSError`` with a plain-English message, which Settings shows as is.
    """

    def __init__(self, services: ShellServices, *, open_folder: Callable[..., Any] | None = None):
        self.services = services
        self._open_folder = open_folder
        self.listeners: list[Callable[[Any], None]] = []
        self.vetoes: list[Callable[[], str | None]] = []

    def current(self) -> Path | None:
        store = self.services.store
        root = getattr(store, "root", None) if store is not None else None
        return Path(root) if root else None

    def _ensure_no_open_flight(self, action: str) -> None:
        try:
            open_flight = self.services.logging.status().flight_open
        except Exception:
            open_flight = False
        if open_flight:
            raise OSError(f"A flight is being logged. Stop Flight Logging before you {action}: the open "
                          "flight's crash-safe journal is in the current data folder.")

    def _check_vetoes(self) -> None:
        for veto in list(self.vetoes):
            try:
                reason = veto()
            except Exception:
                log.exception("Data folder veto failed")
                reason = None
            if reason:
                raise OSError(str(reason))

    @staticmethod
    def _same_folder(first: Path, second: Path) -> bool:
        try:
            return first.resolve() == second.resolve()
        except OSError:
            return first == second

    def change(self, folder: Path) -> Any:
        folder = Path(folder)
        current = self.current()
        if current is not None and self._same_folder(current, folder):
            store = self.services.store
            self._remember(store)
            self._notify(store)
            return store
        # The vetoes may show a modal dialog (unsaved Logbook edits) while the Core thread keeps
        # ticking the Recorder, which can open a flight meanwhile. So they run first, and the
        # open-flight check runs again right before the swap, with no dialog in between.
        self._ensure_no_open_flight("change the data folder")
        self._check_vetoes()
        opener = self._open_folder
        if opener is None:
            from .storage_settings import open_data_folder as opener
        store = opener(folder, create=True)
        try:
            self._ensure_no_open_flight("change the data folder")
        except OSError:
            self._close_quietly(store)
            raise
        self._remember(store)
        previous = self.services.store
        self.services.store = store
        self.services.data_dir = Path(store.root)
        self._relocate_logging(store)
        self._notify(store)
        if previous is not None and previous is not store:
            close = getattr(previous, "close", None)
            if callable(close):
                close()
        return store

    def release(self) -> None:
        self._ensure_no_open_flight("release the data folder")
        self._check_vetoes()
        self._ensure_no_open_flight("release the data folder")  # again: a veto dialog may have been up
        previous = self.services.store
        root = getattr(previous, "root", None) if previous is not None else None
        if root:
            try:
                from . import logging_setup

                moved = logging_setup.release_folder(Path(root))
                if moved is not None:
                    self.services.logging_config = moved
            except Exception:
                log.exception("The log could not be moved out of the data folder")
        self.services.store = None
        self._notify(None)
        close = getattr(previous, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _close_quietly(store: Any) -> None:
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - the refusal is what the pilot needs to see
                log.exception("The data folder opened for the switch did not close cleanly")

    def _remember(self, store: Any) -> None:
        remember = getattr(self.services.folder_settings, "remember", None)
        if callable(remember) and store is not None and not getattr(store, "read_only", False):
            try:
                remember(store.root)
            except Exception as error:  # noqa: BLE001 - the switch stands; the pilot is told it is not remembered
                log.warning("Data folder opened but not remembered: %s", error)
                reason = getattr(error, "strerror", None) or str(error) or type(error).__name__
                text = (f"The data folder {store.root} is open for this session, but BBC EFB could not "
                        f"remember it for the next start ({reason}). Choose it again in "
                        "Settings > Data Management if it is not opened next time.")
                notices = getattr(self.services, "notices", None)
                if isinstance(notices, list):
                    # Shown by the shell's notice banner when the folder change is announced.
                    if text not in notices:
                        notices.append(text)

    def _relocate_logging(self, store: Any) -> None:
        try:
            from . import logging_setup

            if getattr(store, "read_only", False):
                moved = logging_setup.release_folder(Path(store.root))
            else:
                moved = logging_setup.relocate_logging(Path(store.root))
            if moved is not None:
                self.services.logging_config = moved
        except Exception:
            log.exception("The log could not be moved to the new data folder")

    def _notify(self, store: Any) -> None:
        for listener in list(self.listeners):
            try:
                listener(store)
            except Exception:
                log.exception("Data folder listener failed")


def _dll_facts(services: ShellServices) -> dict[str, Any]:
    if services.simulated:
        return {"dll_path": None, "dll_found": None, "dll_candidates": (), "dll_live": False}
    candidates: tuple[str, ...] = ()
    try:
        from bbc_core.simconnect_sdk import simconnect_dll_candidates

        candidates = tuple(str(item) for item in simconnect_dll_candidates(services.dll_path))
    except Exception:  # noqa: BLE001 - diagnostics only
        log.debug("SimConnect.dll candidates could not be listed", exc_info=True)
    dll = services.dll_path
    return {"dll_path": str(dll) if dll else None, "dll_found": dll is not None, "dll_candidates": candidates,
            "dll_live": services.dll_switch is not None}


def default_diagnostics(services: ShellServices) -> Callable[[], dict[str, Any]]:
    """A Connection Diagnostics provider: the Core bridge's facts (C4), DLL facts and start-up notes."""

    def provide() -> dict[str, Any]:
        connection = services.initial_connection
        facts: dict[str, Any] = {"connection": connection,
                                 "detail": getattr(connection, "detail", "") if connection is not None else ""}
        from_bridge = getattr(services.bridge, "diagnostics", None)
        status_of = getattr(services.host, "status", None)
        try:
            if callable(from_bridge):
                reported = from_bridge()
                if isinstance(reported, dict):
                    facts.update({key: value for key, value in reported.items() if value is not None})
            elif callable(status_of):
                status = status_of()
                facts["connection"] = getattr(status, "connection", connection)
                facts["detail"] = getattr(status, "detail", "") or getattr(facts["connection"], "detail", "")
                last_error = getattr(status, "last_error", "")
                facts["last_errors"] = (str(last_error),) if last_error else ()
        except Exception as error:
            facts["detail"] = f"Black Box Core status could not be read: {error}"
        notes = list(facts.get("notes") or ())
        notes.extend(services.startup_notes)
        if services.simulated and not any("SIMULATED" in note for note in notes):
            notes.insert(0, "SIMULATED: started with --simulated. No data comes from MSFS.")
        config = services.logging_config
        log_file = getattr(config, "log_file", None)
        if log_file is not None:
            notes.append(f"Log file: {log_file}")
        reason = getattr(config, "fallback_reason", None)
        if reason:
            notes.append(f"The log is not in the data folder: {reason}")
        facts["notes"] = tuple(dict.fromkeys(notes))
        facts.setdefault("last_errors", ())
        facts.update(_dll_facts(services))
        return facts

    return provide


@dataclass(frozen=True, slots=True)
class SectionSpec:
    """One Section the shell can show.

    ``factory(services)`` builds the widget the first time the Section opens.
    ``tab`` is False for Settings, which has its own title-bar control rather than
    a fourth primary tab (UI Design Standards §2).
    """

    key: str
    title: str
    factory: Callable[[ShellServices], QWidget]
    shortcut: str = ""
    tab: bool = True
    description: str = ""


# ----------------------------------------------------------------------------
# Small building blocks (local so the shell does not depend on edits elsewhere)
# ----------------------------------------------------------------------------


def _label(text: str, name: str = "", *, wrap: bool = False, rich: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def _card(title: str | None = None, description: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(8)
    if title:
        heading = _label(title, "cardTitle", wrap=True)
        layout.addWidget(heading)
        frame.setAccessibleName(title)
    if description:
        layout.addWidget(_label(description, "muted", wrap=True))
    return frame, layout


def _scroll(widget: QWidget, *, name: str = "") -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    if name:
        area.setAccessibleName(name)
    return area


def _page(heading: str, intro: str = "") -> tuple[QWidget, QVBoxLayout]:
    widget = QWidget()
    widget.setObjectName("page")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(12)
    title = _label(heading, "heading")
    title.setAccessibleName(f"{heading} heading")
    layout.addWidget(title)
    if intro:
        layout.addWidget(_label(intro, "muted", wrap=True))
    return widget, layout


def unavailable_badge(text: str = NOT_AVAILABLE) -> QLabel:
    """A plain-text badge. The words carry the meaning; colour only reinforces it."""
    badge = _label(text, "warning")
    badge.setAccessibleName(text)
    return badge


def feature_card(title: str, description: str, *, status: str = NOT_AVAILABLE) -> QFrame:
    """A card for one planned feature, clearly labelled as not available."""
    frame, layout = _card(title)
    layout.addWidget(unavailable_badge(status))
    layout.addWidget(_label(description, "muted", wrap=True))
    layout.addStretch()
    frame.setAccessibleName(f"{title}. {status}.")
    frame.setAccessibleDescription(description)
    frame.setMinimumWidth(220)
    frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    return frame


class ReflowGrid(QWidget):
    """Lay cards out in as many columns as fit, down to one (hybrid adaptive layout).

    Cards never shrink below ``minimum_card_width``; when even one column does not
    fit, the enclosing scroll area scrolls instead.
    """

    def __init__(self, cards: Iterable[QWidget] = (), *, minimum_card_width: int = 260,
                 maximum_columns: int = 3, spacing: int = 12, parent: QWidget | None = None):
        super().__init__(parent)
        self._cards: list[QWidget] = []
        self._minimum = minimum_card_width
        self._maximum_columns = max(1, maximum_columns)
        self._columns = 0
        self._grid = QGridLayout(self)
        # The layout's own minimum grows with the column count; without this the
        # grid could never shrink back to fewer columns.
        self._grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        for card in cards:
            self.add_card(card)
        self.setMinimumWidth(minimum_card_width)

    @property
    def columns(self) -> int:
        return self._columns

    @property
    def cards(self) -> tuple[QWidget, ...]:
        return tuple(self._cards)

    def add_card(self, card: QWidget) -> None:
        self._cards.append(card)
        self._relayout(force=True)

    def columns_for(self, width: int) -> int:
        spacing = self._grid.spacing()
        columns = max(1, (width + spacing) // (self._minimum + spacing))
        return int(min(columns, self._maximum_columns, max(1, len(self._cards))))

    def _relayout(self, force: bool = False) -> None:
        columns = self.columns_for(self.width())
        if columns == self._columns and not force:
            return
        self._columns = columns
        for card in self._cards:
            self._grid.removeWidget(card)
        for index, card in enumerate(self._cards):
            self._grid.addWidget(card, index // columns, index % columns, Qt.AlignmentFlag.AlignTop)
        for column in range(self._maximum_columns):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(self._minimum, self._grid.minimumSize().height())

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(self._minimum, self._grid.sizeHint().height())

    def resizeEvent(self, event):  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._relayout()


# ----------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------

DISPATCH_WORKFLOWS: tuple[tuple[str, str], ...] = (
    ("SimBrief dispatch and OFP",
     "Retrieve your SimBrief operational flight plan and dispatch release. No SimBrief "
     "account is linked and nothing is downloaded in this build."),
    ("Flight plan and route",
     "Review the planned route, departure and arrival procedures and alternates. "
     "Flight plan prefiling is planned only where a service supports it."),
    ("Departure and destination",
     "Airport details, runways, gates and parking for the planned airports."),
    ("Aircraft and fuel",
     "Aircraft type, registration and planned fuel figures for the flight."),
    ("Weather",
     "METAR, TAF and planning weather for the route. No weather service is connected."),
    ("Charts and navigation",
     "Charts and navigation references for the planned airports."),
    ("Pre-flight checklist",
     "A preparation checklist that follows the dispatch workflow."),
    ("Flight plan history",
     "Earlier plans you dispatched, ready to reuse."),
)


class DispatchPage(QWidget):
    """Dispatch placeholder: the approved pre-flight structure, nothing live."""

    def __init__(self, services: ShellServices | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("surface")
        self.setAccessibleName("Dispatch section")
        content, layout = _page(
            "Dispatch",
            "Pre-flight planning and preparation. These workflows are planned for a later build. "
            "None of them is connected in this build, and nothing on this page is live or sample data. "
            "The Logbook and Flight Logging work independently of this page.",
        )
        self.grid = ReflowGrid(feature_card(title, text) for title, text in DISPATCH_WORKFLOWS)
        layout.addWidget(self.grid)
        layout.addStretch()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = _scroll(content, name="Dispatch workflows")
        root.addWidget(self.scroll_area)

    @property
    def workflow_titles(self) -> tuple[str, ...]:
        return tuple(title for title, _ in DISPATCH_WORKFLOWS)


# ----------------------------------------------------------------------------
# In-Flight
# ----------------------------------------------------------------------------

IN_FLIGHT_MODULES: tuple[tuple[str, str], ...] = (
    ("Aircraft Status",
     "Live aircraft state from Black Box Core where it has been implemented and verified. "
     "Not connected to this page yet."),
    ("vPilot",
     "Communications with vPilot through a supported integration, once one is researched and approved."),
    ("VATSIM Radar",
     "Network traffic and controllers. Embedding and data access still need research."),
    ("SimBrief OFP",
     "The operational flight plan you dispatched, for reference in flight."),
    ("Weather",
     "Weather along the route and at the destination."),
    ("Hoppie ACARS / CPDLC",
     "Datalink messages through Hoppie's service, once an integration is approved."),
    ("Charts",
     "Airport and procedure charts for the current flight."),
)

LAUNCHER_WIDTH = 196


class ModulesLauncher(QFrame):
    """The right-hand Modules launcher. It has a fixed width and its own scrolling,
    so the In-Flight workspace shrinks or scrolls instead of pushing it off-screen."""

    module_requested = Signal(str)

    def __init__(self, modules: Iterable[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAccessibleName("Modules launcher")
        self.setFixedWidth(LAUNCHER_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 12, 10, 12)
        outer.setSpacing(8)
        outer.addWidget(_label("Modules", "cardTitle"))
        outer.addWidget(_label(NOT_AVAILABLE, "warning", wrap=True))
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(6)
        from PySide6.QtWidgets import QPushButton  # local: keeps the import list short

        self.buttons: dict[str, QPushButton] = {}
        for name in modules:
            control = QPushButton(name)
            control.setAccessibleName(f"{name} module, {NOT_AVAILABLE.lower()}")
            control.setToolTip(f"{name}: {NOT_AVAILABLE.lower()}. Shows what is planned.")
            control.clicked.connect(lambda checked=False, module=name: self.module_requested.emit(module))
            inner_layout.addWidget(control)
            self.buttons[name] = control
        inner_layout.addStretch()
        area = _scroll(inner, name="Modules list")
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(area, 1)


class InFlightPage(QWidget):
    """In-Flight placeholder: a scrolling workspace plus the anchored Modules launcher."""

    WORKSPACE_MINIMUM_WIDTH = 280

    def __init__(self, services: ShellServices | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("surface")
        self.setAccessibleName("In-Flight section")
        content, layout = _page(
            "In-Flight",
            "The modular in-flight workspace. Its modules are planned for a later build and none of them "
            "is connected in this build. Flight Logging does not depend on this page: it keeps running "
            "while you use other Sections or minimise the EFB.",
        )
        self.cards: dict[str, QFrame] = {}
        for title, text in IN_FLIGHT_MODULES:
            self.cards[title] = feature_card(title, text)
        self.grid = ReflowGrid(self.cards.values(), minimum_card_width=240)
        layout.addWidget(self.grid)
        layout.addStretch()
        self.workspace = _scroll(content, name="In-Flight workspace")
        self.workspace.setMinimumWidth(self.WORKSPACE_MINIMUM_WIDTH)
        self.launcher = ModulesLauncher(title for title, _ in IN_FLIGHT_MODULES)
        self.launcher.module_requested.connect(self.show_module)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 8, 8)
        root.setSpacing(8)
        root.addWidget(self.workspace, 1)
        root.addWidget(self.launcher, 0)
        self.highlighted: str | None = None

    def show_module(self, name: str) -> None:
        """Bring the module's description into view. Nothing is launched."""
        card = self.cards.get(name)
        if card is None:
            return
        self.highlighted = name
        self.workspace.ensureWidgetVisible(card)
        card.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        card.setFocus(Qt.FocusReason.OtherFocusReason)


# ----------------------------------------------------------------------------
# Logbook
# ----------------------------------------------------------------------------


def construct_with(target: Callable[..., Any], candidates: dict[str, Callable[[], Any]]) -> Any:
    """Call ``target`` passing only the keyword arguments its signature accepts.

    ``candidates`` maps parameter names to zero-argument callables, so a value is
    only produced when the target asks for it. Used where another component's
    constructor is still being finalised; it keeps integration a one-line change.
    """
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return target()
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    kwargs: dict[str, Any] = {}
    for name, produce in candidates.items():
        parameter = parameters.get(name)
        if accepts_any or (parameter is not None and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)):
            kwargs[name] = produce()
    return target(**kwargs)


class SectionLoadFailure(QWidget):
    """Shown when a Section could not be built. Says so plainly; never hides the error."""

    def __init__(self, title: str, error: BaseException | str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("surface")
        self.error_text = str(error) or type(error).__name__
        content, layout = _page(title)
        card, card_layout = _card(f"{title} could not be opened")
        card_layout.addWidget(_label(
            f"BBC EFB could not build this Section: {self.error_text}. The rest of the EFB, the Logbook "
            "files and Flight Logging are not affected. The details are in the log file.", "muted", wrap=True))
        layout.addWidget(card)
        layout.addStretch()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_scroll(content))
        self.setAccessibleName(f"{title} could not be opened")


class LogbookSection(QWidget):
    """Hosts ``bbc_efb.logbook.Logbook`` with a minimum readable size and scrolling."""

    MINIMUM_SIZE = (640, 360)

    def __init__(self, logbook: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("surface")
        self.logbook = logbook
        self.setAccessibleName("Logbook section")
        holder = QWidget()
        holder.setObjectName("surface")
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(12, 12, 12, 8)
        holder_layout.addWidget(logbook)
        holder.setMinimumSize(*self.MINIMUM_SIZE)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = _scroll(holder, name="Logbook")
        root.addWidget(self.scroll_area)

    def can_close(self) -> bool:
        """Ask the Logbook about unsaved manual edits before the EFB closes."""
        check = getattr(self.logbook, "can_leave_editor", None)
        return bool(check()) if callable(check) else True

    def can_switch_store(self) -> bool:
        """Ask the Logbook (once) whether its view may switch to another data folder."""
        for name in ("can_switch_store", "can_leave_editor"):
            check = getattr(self.logbook, name, None)
            if callable(check):
                return bool(check())
        return True

    def refresh(self, store: Any) -> None:
        """Show ``store`` (the app's store after a folder change, or None when released)."""
        set_store = getattr(self.logbook, "set_store", None)
        if callable(set_store):
            set_store(store)
            return
        refresh = getattr(self.logbook, "refresh", None)
        if callable(refresh):
            refresh(store)

    def reload(self) -> None:
        """Re-read the current store (for example after the Recorder saved a flight)."""
        reload = getattr(self.logbook, "reload", None)
        if callable(reload):
            reload()
            return
        refresh = getattr(self.logbook, "refresh", None)
        if callable(refresh):
            try:
                refresh()
                return
            except TypeError:
                pass
        store = getattr(self.logbook, "store", None)
        set_store = getattr(self.logbook, "set_store", None)
        if callable(set_store) and store is not None:
            set_store(store)

    def set_recorder_status(self, status: Any) -> None:
        method = getattr(self.logbook, "set_recorder_status", None)
        if callable(method):
            method(status)


def build_logbook(services: ShellServices) -> QWidget:
    """Lazily import and embed the Logbook, giving it the app's one store (C1).

    The Logbook never opens a store of its own: ``set_store`` gives it the open one,
    and its ``data_folder_requested`` signal goes to the shell (which uses
    ``DataFolderControl``).
    """
    from .logbook import Logbook  # lazy: only when the pilot opens the Logbook

    logbook = construct_with(Logbook, {
        "settings": lambda: services.folder_settings,
        "preferences": lambda: services.preferences,
    })
    if services.store is not None:
        set_store = getattr(logbook, "set_store", None)
        if callable(set_store):
            set_store(services.store)
        else:
            logbook.refresh(services.store)
    return LogbookSection(logbook)


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

_SETTINGS_NAVIGATION = ("open_page", "show_page", "navigate_to", "open_path", "navigate")
_PAGE_METHODS = {
    DIAGNOSTICS_PAGE: ("open_connection_diagnostics", "open_diagnostics", "show_connection_diagnostics"),
    PRIVACY_PAGE: ("open_privacy",),
}
_PAGE_ALIASES = {
    DIAGNOSTICS_PAGE: ("simconnect",),
    PRIVACY_PAGE: ("flight_logging",),
    DATA_PAGE: ("data_management",),
}


def open_settings_page(widget: Any, page: str) -> bool:
    """Ask a Settings widget to show ``page`` (for example ``DIAGNOSTICS_PAGE``).

    Tries a dedicated method first (``open_connection_diagnostics()``,
    ``open_privacy()``), then a navigation method with the shell's page path and
    with ``bbc_efb.settings`` page names (``simconnect``, ``flight_logging``...).
    """
    for name in _PAGE_METHODS.get(page, ()):
        method = getattr(widget, name, None)
        if callable(method):
            method()
            return True
    for name in _SETTINGS_NAVIGATION:
        method = getattr(widget, name, None)
        if not callable(method):
            continue
        for candidate in (page, *_PAGE_ALIASES.get(page, ())):
            try:
                result = method(candidate)
            except (KeyError, ValueError, LookupError):
                continue
            if result is not False:
                return True
        log.warning("Settings could not open %s", page)
        return False
    return False


class _InfoRows(QWidget):
    """Label/value rows whose values can be replaced."""

    def __init__(self, names: Iterable[str], parent: QWidget | None = None):
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self.values: dict[str, QLabel] = {}
        for row, name in enumerate(names):
            key = _label(name, "muted")
            value = _label("Unknown", wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setAccessibleName(name)
            key.setBuddy(value)
            grid.addWidget(key, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
            self.values[name] = value

    def set(self, name: str, text: str) -> None:
        label = self.values[name]
        label.setText(text)
        label.setAccessibleDescription(text)


class BasicSettingsPage(QWidget):
    """Stand-in Settings used when ``bbc_efb.settings`` is not part of the build.

    It keeps the destinations the shell links to (Connection Diagnostics, Flight
    Logging & Privacy, Data Management) and labels everything else unavailable.
    """

    PAGES = (
        (DIAGNOSTICS_PAGE, "SimConnect diagnostics"),
        (PRIVACY_PAGE, "Flight Logging && Privacy"),
        (DATA_PAGE, "Data Management"),
        ("logbook/display", "Logbook display"),
    )

    def __init__(self, services: ShellServices | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.services = services if services is not None else ShellServices()
        self.setObjectName("surface")
        self.setAccessibleName("Settings section")
        self.current_page = ""
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(12)
        nav, nav_layout = _card("Settings")
        nav.setObjectName("sidebar")
        nav.setFixedWidth(220)
        nav_layout.addWidget(_label("Basic Settings. The full Settings section is not available in this build.",
                                    "warning", wrap=True))
        from PySide6.QtWidgets import QPushButton

        self.nav_group = QButtonGroup(self)
        self.nav_buttons: dict[str, QPushButton] = {}
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        builders = {
            DIAGNOSTICS_PAGE: self._diagnostics_page,
            PRIVACY_PAGE: self._privacy_page,
            DATA_PAGE: self._data_page,
            "logbook/display": self._display_page,
        }
        for key, title in self.PAGES:
            control = QPushButton(title)
            control.setCheckable(True)
            control.setAccessibleName(f"{plain_title(title)} settings page")
            control.clicked.connect(lambda checked=False, page=key: self.open_page(page))
            self.nav_group.addButton(control)
            nav_layout.addWidget(control)
            self.nav_buttons[key] = control
            widget = builders[key]()
            self.pages[key] = _scroll(widget, name=plain_title(title))
            self.stack.addWidget(self.pages[key])
        nav_layout.addStretch()
        root.addWidget(nav)
        root.addWidget(self.stack, 1)
        self.set_connection(self.services.initial_connection)
        self.open_page(DIAGNOSTICS_PAGE)

    # Navigation

    def open_page(self, page: str) -> bool:
        if page not in self.pages:
            return False
        self.current_page = page
        self.nav_buttons[page].setChecked(True)
        self.stack.setCurrentWidget(self.pages[page])
        if page == DATA_PAGE:
            self._refresh_data_page()
        return True

    def open_connection_diagnostics(self) -> None:
        self.open_page(DIAGNOSTICS_PAGE)

    # Pages

    def _diagnostics_page(self) -> QWidget:
        content, layout = _page(
            "Connection Diagnostics",
            "The Black Box Core connection to Microsoft Flight Simulator 2020 through SimConnect. "
            "This page shows what Core has reported. It does not change the connection.",
        )
        card, card_layout = _card("SimConnect connection")
        self.connection_rows = _InfoRows(("State", "Detail", "Data source", "Session", "Reported at"))
        card_layout.addWidget(self.connection_rows)
        layout.addWidget(card)
        start, start_layout = _card("This session")
        self.session_rows = _InfoRows(("Mode", "SimConnect.dll at start-up", "Start-up notes"))
        start_layout.addWidget(self.session_rows)
        layout.addWidget(start)
        services = self.services
        self.session_rows.set(
            "Mode",
            "SIMULATED provider (started with --simulated). No data comes from MSFS."
            if services.simulated else "MSFS 2020 through SimConnect",
        )
        self.session_rows.set("SimConnect.dll at start-up", str(services.dll_path) if services.dll_path else
                              ("Not used in simulated mode" if services.simulated else "Not found"))
        self.session_rows.set("Start-up notes", "\n".join(services.startup_notes) or "None")
        controls, controls_layout = _card(
            "Connection controls",
            "Connecting, disconnecting and the advanced SimConnect diagnostics are part of the full "
            "Settings section.")
        controls_layout.addWidget(unavailable_badge())
        layout.addWidget(controls)
        layout.addStretch()
        return content

    def _privacy_page(self) -> QWidget:
        content, layout = _page(
            "Flight Logging & Privacy",
            "What BBC EFB saves, where, and where it comes from.",
        )
        saved, saved_layout = _card("What is saved")
        saved_layout.addWidget(_label(
            "Flight records are saved as files in the data folder you chose, on this PC. BBC EFB does not "
            "send flight records anywhere, and this build has no online services or network listeners. "
            "The application log is written to the logs folder inside the data folder.", wrap=True))
        layout.addWidget(saved)
        source, source_layout = _card("Where automatic flight data comes from")
        source_layout.addWidget(_label(
            "Automatic Flight Logging reads aircraft and simulator state from MSFS 2020 through SimConnect, "
            "read-only. It does not change anything in the simulator. Values the simulator does not provide "
            "are saved as unknown, never guessed.", wrap=True))
        self.logging_status_label = _label("", "muted", wrap=True)
        source_layout.addWidget(self.logging_status_label)
        layout.addWidget(source)
        manage, manage_layout = _card("Review, export and delete",
                                      "Review and edit flights in the Logbook. Export All Data and "
                                      "restorable backups are in Data Management.")
        layout.addWidget(manage)
        layout.addStretch()
        self.refresh_logging_status()
        return content

    def refresh_logging_status(self) -> None:
        try:
            status = self.services.logging.status()
            text = f"Flight Logging now: {status.label}."
            if status.detail:
                text += f" {status.detail}"
        except Exception as error:  # status providers are other components; report, don't crash
            text = f"Flight Logging status could not be read: {error}"
        self.logging_status_label.setText(text)

    def _data_page(self) -> QWidget:
        content, layout = _page("Data Management", "Your Logbook data folder and data tools.")
        folder, folder_layout = _card("Data folder")
        self.data_rows = _InfoRows(("Location", "Access"))
        folder_layout.addWidget(self.data_rows)
        folder_layout.addWidget(_label("To choose another folder, use Choose data folder on the Logbook "
                                       "Dashboard.", "muted", wrap=True))
        layout.addWidget(folder)
        layout.addWidget(feature_card(
            "Export All Data",
            "Copies every flight record into one export for use elsewhere. An export is not a backup: it "
            "cannot be restored into BBC EFB."))
        layout.addWidget(feature_card(
            "Restorable backup",
            "A complete snapshot of the data folder with a manifest and checksums, which BBC EFB can "
            "restore. Separate from Export All Data."))
        layout.addStretch()
        return content

    def _refresh_data_page(self) -> None:
        store = self.services.store
        root = getattr(store, "root", None) or self.services.data_dir
        self.data_rows.set("Location", str(root) if root else "No data folder is selected")
        if store is None:
            access = "Not open"
        elif getattr(store, "read_only", False):
            access = "Read-only (flights cannot be saved)"
        else:
            access = "Open for writing"
        self.data_rows.set("Access", access)

    def _display_page(self) -> QWidget:
        content, layout = _page("Logbook: Display Preferences")
        layout.addWidget(feature_card(
            "Flight History view",
            "Remember Table, Cards or Combined view and related preferences."))
        layout.addStretch()
        return content

    # Live updates from the shell

    def set_connection(self, connection: Any) -> None:
        view = describe_connection(connection)
        self.connection_rows.set("State", view.text)
        self.connection_rows.set("Detail", view.detail or "None reported")
        self.connection_rows.set("Data source", view.source_text)
        session = getattr(connection, "session_id", None) if connection is not None else None
        self.connection_rows.set("Session", str(session) if session else "No session")
        occurred = getattr(connection, "occurred_at", None) if connection is not None else None
        if isinstance(occurred, datetime):
            when = occurred.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ (PC clock)")
        else:
            when = "Not reported"
        self.connection_rows.set("Reported at", when)

    def set_ui_active(self, active: bool) -> None:
        if active:
            self.refresh_logging_status()


def _record_schema() -> int | None:
    try:
        from .storage import SCHEMA_VERSION
    except ImportError:  # pragma: no cover - storage is part of every build
        return None
    return SCHEMA_VERSION


def build_settings(services: ShellServices) -> QWidget:
    """Use ``bbc_efb.settings.SettingsPage`` when the build has it, else ``BasicSettingsPage``."""
    try:
        module = importlib.import_module(services.settings_module)
        page_class = getattr(module, "SettingsPage")
    except (ImportError, AttributeError) as error:
        log.info("Full Settings not available (%s); using basic Settings.", error)
        return BasicSettingsPage(services)
    if services.data_folder is None:
        services.data_folder = DataFolderControl(services)
    candidates: dict[str, Callable[[], Any]] = {
        "services": lambda: services,
        "preferences": lambda: services.preferences,
        "data_folder": lambda: services.data_folder,
        "diagnostics": lambda: services.diagnostics or default_diagnostics(services),
        "measure_response_times": lambda: services.measure_response_times,
        "store": lambda: services.store,
        "folder_settings": lambda: services.folder_settings,
        "bridge": lambda: services.bridge,
        "host": lambda: services.host,
        "logging_config": lambda: services.logging_config,
        "flight_logging": lambda: services.logging,
        "modules": lambda: (lambda: services.module_host),
        "record_schema": _record_schema,
    }
    if services.version:
        candidates["app_version"] = lambda: services.version
    page = construct_with(page_class, candidates)
    layout = page.layout() if isinstance(page, QWidget) else None
    if layout is not None:
        margins = layout.contentsMargins()
        if (margins.left(), margins.top(), margins.right(), margins.bottom()) == (0, 0, 0, 0):
            layout.setContentsMargins(12, 12, 12, 8)  # match the other Sections inside the frameless shell
    return page


# ----------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------


def default_sections() -> tuple[SectionSpec, ...]:
    """Dispatch, In-Flight and Logbook as primary tabs, plus Settings."""
    return (
        SectionSpec(DISPATCH, "&Dispatch", DispatchPage, "Ctrl+1", description="Pre-flight planning"),
        SectionSpec(IN_FLIGHT, "&In-Flight", InFlightPage, "Ctrl+2", description="In-flight workspace"),
        SectionSpec(LOGBOOK, "&Logbook", build_logbook, "Ctrl+3", description="Flight records"),
        SectionSpec(SETTINGS, "&Settings", build_settings, "Ctrl+,", tab=False, description="Settings"),
    )


def plain_title(title: str) -> str:
    """``"&In-Flight"`` -> ``"In-Flight"`` (mnemonic markers removed)."""
    return title.replace("&&", "\0").replace("&", "").replace("\0", "&")
