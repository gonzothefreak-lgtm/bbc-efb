"""Logbook Dashboard (Q1-Q2) and the Logbook workspace that hosts every Logbook page.

Dashboard: real record-based KPIs (flights, time with unknowns counted, distance where
recorded, landings and measured touchdown rates with the number unmeasured), aircraft
type profile cards (click filters Flight History by *type*), flight time by aircraft
donut, touchdown-rate distribution, operations, recent flights and quick links.

``LogbookWorkspace`` is the Logbook section body: a page list (Dashboard, Flight
History, Black Box, Statistics; no Aircraft or Reports pages, Q6-Q7), the pages, and
one shared Flight Details overlay covering the workspace. Hidden pages are refreshed
only when shown, so a large Logbook costs nothing on pages the pilot is not looking at.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from .aircraft_art import AircraftArtLibrary, AircraftArtWidget, shared_library
from .charts import BarChart, DonutChart, Segment
from .details import FlightDetailsOverlay, JournalLoader
from .filters import FilterState
from .history import COLUMN_KEYS, EnterActivates, FlightHistoryPage, FlightTableModel, make_table_view
from .model import ROLE_ID, FlightView, flight_views
from .recorder_management import GapAcknowledger, RecorderManagementPage
from .statistics import (
    Metric, StatisticsPage, distance_text, rate_bars, rate_footnote, rate_text, summarise,
    time_by_aircraft_footnote, time_by_aircraft_segments, time_text, unknown_note,
)

PAGES = ("dashboard", "history", "black_box", "statistics")
PAGE_TITLES = {"dashboard": "Dashboard", "history": "Flight History", "black_box": "Black Box", "statistics": "Statistics"}
RECENT_COLUMNS = ("date", "callsign", "departure", "arrival", "aircraft", "flight_time", "landing_rate", "status")
MAX_AIRCRAFT_CARDS = 4


def _card(title: str, link_text: str = "", link: Callable[[], None] | None = None) -> tuple[QFrame, QVBoxLayout, QPushButton | None]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    row = QHBoxLayout()
    heading = QLabel(title)
    heading.setObjectName("cardTitle")
    row.addWidget(heading, 1)
    button = None
    if link_text:
        button = QPushButton(link_text)
        button.setObjectName("status")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if link is not None:
            button.clicked.connect(link)
        row.addWidget(button)
    layout.addLayout(row)
    return frame, layout, button


class AircraftProfileCard(QPushButton):
    """Side-on profile, type, flights and known hours. Click: History filtered by type."""

    def __init__(self, library: AircraftArtLibrary, parent=None):
        super().__init__(parent)
        self.type_key = ""
        self.setObjectName("aircraftCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton#aircraftCard { background: #101b28; border: 1px solid #283b50; border-radius: 9px;"
            " padding: 6px; text-align: left; }"
            "QPushButton#aircraftCard:hover { background: #182a3c; }"
            "QPushButton#aircraftCard:focus { border: 1px solid #86caff; }")
        self.art = AircraftArtWidget(library)
        self.art.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.art.setMinimumSize(170, 68)
        self.type_label = QLabel()
        self.type_label.setStyleSheet("font-weight: 650;")
        self.facts = QLabel()
        self.facts.setObjectName("muted")
        for label in (self.type_label, self.facts):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)
        layout.addWidget(self.art, 1)
        row = QHBoxLayout()
        row.addWidget(self.type_label, 1)
        row.addWidget(self.facts)
        layout.addLayout(row)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(130)

    def sizeHint(self):
        return QSize(260, 150)

    def show_profile(self, key: str, flights: int, hours_text: str, unknown: int) -> None:
        self.type_key = key
        self.art.set_type(key)
        self.type_label.setText(key)
        facts = f"{flights:,} flight{'s' if flights != 1 else ''} | {hours_text}"
        self.facts.setText(facts)
        tip = f"{self.art.caption}. Show all {key} flights in Flight History."
        if unknown:
            tip += f" {unknown:,} flight(s) without a known time are not in the hours."
        self.setToolTip(tip)
        self.setAccessibleName(f"{key}: {facts}. Opens Flight History filtered to this aircraft type.")


class DashboardPage(QWidget):
    navigate_requested = Signal(str)       # "history", "black_box", "statistics"
    aircraft_selected = Signal(str)        # aircraft type key
    flight_opened = Signal(str)
    new_flight_requested = Signal()

    def __init__(self, parent=None, *, library: AircraftArtLibrary | None = None, today=date.today):
        super().__init__(parent)
        self.setObjectName("page")
        self.library = library or shared_library()
        self._today = today
        self.summary = summarise([], today=today())
        title = QLabel("Logbook — Dashboard")
        title.setObjectName("heading")
        self.data_note = QLabel("")
        self.data_note.setObjectName("muted")
        self.data_note.setWordWrap(True)

        self.metrics = {key: Metric(text) for key, text in (
            ("flights", "Total flights"), ("time", "Total flight time"), ("distance", "Total distance"),
            ("landings", "Total landings"), ("rate", "Touchdown rate (average)"),
        )}
        self.rate_extremes = QLabel("")
        self.rate_extremes.setObjectName("muted")
        self.metrics["rate"].layout().addWidget(self.rate_extremes)
        kpis = QHBoxLayout()
        kpis.setSpacing(10)
        for metric in self.metrics.values():
            kpis.addWidget(metric, 1)

        aircraft_card, aircraft_layout, self.all_aircraft = _card(
            "My aircraft", "All aircraft types ›", lambda: self.navigate_requested.emit("statistics"))
        self.aircraft_grid = QGridLayout()
        self.aircraft_grid.setSpacing(8)
        self.aircraft_cards = [AircraftProfileCard(self.library) for _ in range(MAX_AIRCRAFT_CARDS)]
        for index, card in enumerate(self.aircraft_cards):
            card.clicked.connect(lambda _checked=False, c=card: self.aircraft_selected.emit(c.type_key))
            self.aircraft_grid.addWidget(card, index // 2, index % 2)
        self.aircraft_empty = QLabel("Aircraft types appear here after your first flights.")
        self.aircraft_empty.setObjectName("muted")
        aircraft_layout.addWidget(self.aircraft_empty)
        aircraft_layout.addLayout(self.aircraft_grid, 1)

        time_card, time_layout, _ = _card("Flight time by aircraft")
        self.time_chart = DonutChart("Flight time by aircraft type")
        time_layout.addWidget(self.time_chart, 1)
        rate_card, rate_layout, _ = _card("Touchdown rate distribution")
        self.rate_chart = BarChart("Touchdown rate distribution",
                                   empty_text="No measured touchdown rates yet. Rates come only from Flight Logging or imports that include them.")
        rate_layout.addWidget(self.rate_chart, 1)
        ops_card, ops_layout, _ = _card("Operations")
        self.ops_chart = DonutChart("Flights by operation", empty_text="No flights have an operation set yet.")
        ops_layout.addWidget(self.ops_chart, 1)

        recent_card, recent_layout, _ = _card(
            "Recent flights", "View Flight History ›", lambda: self.navigate_requested.emit("history"))
        self.recent_model = FlightTableModel(self)
        self.recent_table = make_table_view(self.recent_model, accessible_name="Recent flights")
        self.recent_table.setSortingEnabled(False)
        self.recent_model.clear_sort()
        for index, key in enumerate(COLUMN_KEYS):
            self.recent_table.setColumnHidden(index, key not in RECENT_COLUMNS)
        self.recent_table.doubleClicked.connect(self._open_recent)
        self._recent_enter = EnterActivates(self.recent_table, self._open_recent)
        self.recent_table.setMinimumHeight(170)
        self.recent_empty = QLabel("No flights yet.")
        self.recent_empty.setObjectName("muted")
        recent_layout.addWidget(self.recent_empty)
        recent_layout.addWidget(self.recent_table, 1)

        links_card, links_layout, _ = _card("Quick links")
        self.link_buttons: dict[str, QPushButton] = {}
        for key, text, detail in (
            ("history", "Flight History", "Search, view and edit flights"),
            ("black_box", "Black Box", "Recordings that need attention"),
            ("statistics", "Statistics", "Trends, totals and analysis"),
            ("new_flight", "New flight", "Add a flight by hand"),
        ):
            button = QPushButton(f"{text}\n{detail}")
            button.setStyleSheet("text-align: left; padding: 8px 12px;")
            button.setAccessibleName(f"{text}: {detail}")
            if key == "new_flight":
                button.clicked.connect(self.new_flight_requested)
            else:
                button.clicked.connect(lambda _checked=False, k=key: self.navigate_requested.emit(k))
            self.link_buttons[key] = button
            links_layout.addWidget(button)
        links_layout.addStretch(1)

        grid = QGridLayout()
        grid.setSpacing(12)
        grid.addWidget(aircraft_card, 0, 0, 2, 1)
        grid.addWidget(time_card, 0, 1)
        grid.addWidget(rate_card, 0, 2)
        grid.addWidget(ops_card, 1, 1, 1, 2)
        grid.addWidget(recent_card, 2, 0, 1, 2)
        grid.addWidget(links_card, 2, 2)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        for chart in (self.time_chart, self.rate_chart, self.ops_chart):
            chart.setMinimumHeight(170)

        body = QWidget()
        body.setObjectName("page")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(self.data_note)
        layout.addLayout(kpis)
        layout.addLayout(grid, 1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)
        self.set_flights([])

    def _open_recent(self, index):
        flight_id = index.data(ROLE_ID)
        if flight_id:
            self.flight_opened.emit(flight_id)

    def set_flights(self, views: Sequence[FlightView], include_simulated: bool = False) -> None:
        s = summarise(views, include_simulated=include_simulated, today=self._today())
        self.summary = s
        notes = []
        if s.simulated_excluded:
            notes.append(f"{s.simulated_excluded:,} simulated-data flight{'s' if s.simulated_excluded != 1 else ''} "
                         "not included in these totals.")
        if not views:
            notes.append("No flights yet. Totals appear as soon as you add or log a flight.")
        self.data_note.setText(" ".join(notes))
        self.data_note.setVisible(bool(notes))
        m = self.metrics
        m["flights"].set(f"{s.flights:,}", ", ".join(f"{count:,} {source}" for source, count in s.by_source))
        m["time"].set(time_text(s.time), unknown_note(s.time, "without a known time"))
        m["distance"].set(distance_text(s.distance_nm), unknown_note(s.distance_nm, "without a recorded distance"))
        m["landings"].set(f"{s.landings.value:,.0f}" if s.landings.known else "Not recorded",
                          unknown_note(s.landings, "without a landing count"))
        m["rate"].set(rate_text(s.rates.average_fpm), rate_footnote(s) if s.flights else "")
        self.rate_extremes.setText(
            f"Softest {rate_text(s.rates.best_fpm)} | Firmest {rate_text(s.rates.worst_fpm)}" if s.rates.measured else "")
        self.rate_extremes.setVisible(bool(s.rates.measured))

        profiles = s.aircraft[:MAX_AIRCRAFT_CARDS]
        for card, profile in zip(self.aircraft_cards, profiles):
            card.show_profile(profile.key, profile.flights, time_text(profile.time), profile.time.unknown)
        for index, card in enumerate(self.aircraft_cards):
            card.setVisible(index < len(profiles))
        self.aircraft_empty.setVisible(not profiles)
        self.all_aircraft.setText(f"All aircraft types ({len(s.aircraft):,}) ›")

        self.time_chart.set_data(time_by_aircraft_segments(s), time_text(s.time), "known time",
                                 time_by_aircraft_footnote(s))
        self.rate_chart.set_data(rate_bars(s), "Touchdown rate, fpm (descent)", rate_footnote(s) if s.flights else "")
        self.ops_chart.set_data([Segment(label, count, f"{count:,}") for label, count in s.operations],
                                f"{sum(c for _l, c in s.operations):,}", "with operation",
                                f"{s.operations_unset:,} flight(s) without an operation." if s.operations_unset else "")
        self.recent_model.set_flights(s.recent)
        self.recent_table.setVisible(bool(s.recent))
        self.recent_empty.setVisible(not s.recent)
        attention = s.needing_review
        self.link_buttons["black_box"].setText(
            "Black Box\n" + (f"{attention:,} recording{'s' if attention != 1 else ''} need attention" if attention
                             else "Recordings that need attention"))


class LogbookWorkspace(QWidget):
    """The Logbook section: page list, pages and one shared Flight Details overlay."""

    edit_requested = Signal(str)
    #: Flight Details asked to confirm fields: (flight id, field names, diversion answer or None)
    confirm_requested = Signal(str, object, object)
    new_flight_requested = Signal()
    diagnostics_requested = Signal()
    flight_logging_settings_requested = Signal()
    records_changed = Signal()
    page_changed = Signal(str)
    preferences_changed = Signal(object)   # {"view_mode": str, "hidden_columns": [..]}

    def __init__(self, parent=None, *, actions: GapAcknowledger | None = None,
                 journal_loader: JournalLoader | None = None, library: AircraftArtLibrary | None = None,
                 today: Callable[[], date] = date.today, preferences: dict | None = None, can_edit: bool = True):
        super().__init__(parent)
        self.setObjectName("surface")
        preferences = dict(preferences or {})
        self.library = library or shared_library()
        self._views: list[FlightView] = []
        self._unreadable: list[Any] = []
        self._dirty: set[str] = set(PAGES)
        self.conversion_failures: list[tuple[Any, str]] = []

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(10, 12, 10, 12)
        heading = QLabel("Logbook")
        heading.setObjectName("brand")
        side.addWidget(heading)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for key in PAGES:
            button = QPushButton(PAGE_TITLES[key])
            button.setCheckable(True)
            button.setStyleSheet("text-align: left; padding: 8px 12px;")
            button.clicked.connect(lambda _checked=False, k=key: self.navigate(k))
            self.nav.addButton(button)
            self.nav_buttons[key] = button
            side.addWidget(button)
        side.addStretch(1)
        self.sidebar.setFixedWidth(190)

        self.stack = QStackedWidget()
        self.details = FlightDetailsOverlay(self, library=self.library, journal_loader=journal_loader, can_edit=can_edit)
        self.dashboard = DashboardPage(library=self.library, today=today)
        self.history = FlightHistoryPage(
            library=self.library, details=self.details, today=today,
            view_mode=preferences.get("view_mode", "combined"),
            hidden_columns=frozenset(preferences["hidden_columns"]) if "hidden_columns" in preferences else None,
            can_edit=can_edit)
        self.recorder = RecorderManagementPage(actions=actions)
        self.statistics = StatisticsPage(today=today)
        self.pages = {"dashboard": self.dashboard, "history": self.history,
                      "black_box": self.recorder, "statistics": self.statistics}
        for key in PAGES:
            self.stack.addWidget(self.pages[key])

        self.dashboard.navigate_requested.connect(self.navigate)
        self.dashboard.aircraft_selected.connect(self.show_aircraft)
        self.dashboard.flight_opened.connect(self.open_flight)
        self.dashboard.new_flight_requested.connect(self.new_flight_requested)
        self.history.edit_requested.connect(self.edit_requested)
        self.history.new_flight_requested.connect(self.new_flight_requested)
        self.history.view_mode_changed.connect(lambda _mode: self._emit_preferences())
        self.history.columns_changed.connect(lambda _cols: self._emit_preferences())
        self.recorder.review_requested.connect(lambda flight_id: self.open_flight(flight_id, "black_box"))
        self.recorder.diagnostics_requested.connect(self.diagnostics_requested)
        self.recorder.flight_logging_settings_requested.connect(self.flight_logging_settings_requested)
        self.recorder.records_changed.connect(self.records_changed)
        self.statistics.include_simulated_changed.connect(lambda _checked: self._mark_dirty({"dashboard"}))
        self.details.edit_requested.connect(self.edit_requested)
        self.details.confirm_requested.connect(self.confirm_requested)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(14)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        QShortcut(QKeySequence("Ctrl+F"), self, self._focus_search, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._current = ""
        self.navigate("dashboard")

    # Data -----------------------------------------------------------------------------

    def set_flights(self, items: Iterable[Any], unreadable: Iterable[Any] = ()) -> None:
        """Accept storage ``Flight`` objects, plain record dicts or ``FlightView`` objects."""
        views, failures = flight_views(items)
        self.conversion_failures = failures
        self._views = views
        extra = [_Unshowable(_failure_name(item), reason) for item, reason in failures]
        self._unreadable = list(unreadable) + extra
        self.history.set_flights(views)  # History keeps its own context, always current
        self._dirty = set(PAGES) - {"history"}
        self._refresh(self._current)

    def views(self) -> list[FlightView]:
        return list(self._views)

    def _mark_dirty(self, pages: set[str]):
        self._dirty |= pages
        self._refresh(self._current)

    def _refresh(self, key: str):
        if key not in self._dirty:
            return
        self._dirty.discard(key)
        if key == "dashboard":
            self.dashboard.set_flights(self._views, include_simulated=self.statistics.include_simulated.isChecked())
        elif key == "black_box":
            self.recorder.set_flights(self._views, self._unreadable)
        elif key == "statistics":
            self.statistics.set_flights(self._views)
        elif key == "history":
            self.history.set_flights(self._views)

    # Navigation ---------------------------------------------------------------------

    @property
    def current_page(self) -> str:
        return self._current

    def navigate(self, key: str) -> None:
        if key not in self.pages:
            raise ValueError(f"Unknown Logbook page {key!r}.")
        self._refresh(key)
        self.stack.setCurrentWidget(self.pages[key])
        self.nav_buttons[key].setChecked(True)
        if key != self._current:
            self._current = key
            self.page_changed.emit(key)

    def show_aircraft(self, type_key: str) -> None:
        """Dashboard aircraft card: Flight History filtered to every flight of that type."""
        self.history.set_filter(FilterState(aircraft=frozenset({type_key}),
                                            include_simulated=self.history.filter_state().include_simulated))
        self.navigate("history")

    def open_flight(self, flight_id: str, tab: str = "overview") -> bool:
        view = next((item for item in self._views if item.id == flight_id), None)
        if view is None:
            return False
        if not self.history.open_flight(flight_id, tab):
            self.details.open_flight(view, tab)
        return True

    def set_flight_logging_status(self, text: str) -> None:
        self.recorder.set_flight_logging_status(text)

    def set_recorder_status(self, status: Any):
        """Live Flight Logging status for the Black Box page (from the Recorder only)."""
        return self.recorder.set_recorder_status(status)

    def set_can_edit(self, can_edit: bool, reason: str = "") -> None:
        self.history.set_can_edit(can_edit, reason)
        for button in (self.dashboard.link_buttons.get("new_flight"),):
            if button is not None:
                button.setEnabled(can_edit)
                button.setToolTip("" if can_edit else reason)

    def set_actions(self, actions: GapAcknowledger | None) -> None:
        """Gap acknowledgement for the data folder in use (None: read-only or no folder)."""
        self.recorder.set_actions(actions)

    def set_journal_loader(self, loader: JournalLoader | None, *, background: bool | None = None) -> None:
        """Journals are read from the data folder in use; a new folder brings a new loader.
        ``background=True`` parses journals on a worker thread instead of the UI thread."""
        self.details.journal_loader = loader
        if background is not None:
            self.details.load_in_background = background

    def reset_browsing(self) -> None:
        """Forget the open flight, selection and filters (used when the data folder changes)."""
        self.details.close_overlay()
        self.details.view = None
        self.history.table.selectionModel().clearSelection()
        self.history.set_filter(FilterState())

    def _focus_search(self):
        self.navigate("history")
        self.history.filter_bar.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.history.filter_bar.search.selectAll()

    def preferences(self) -> dict:
        return {"view_mode": self.history.view_mode, "hidden_columns": sorted(self.history.hidden_columns)}

    def _emit_preferences(self):
        self.preferences_changed.emit(self.preferences())


class _Unshowable:
    """A record that could not be turned into a view, listed in Recorder Management."""

    kind = "unreadable"

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = f"The record could not be shown ({reason})."


def _failure_name(item: Any) -> str:
    for attr in ("file_name", "name", "id"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(item, dict):
        value = item.get("id")
        if isinstance(value, str) and value:
            return value
    return "Unnamed record"


__all__ = ["AircraftProfileCard", "DashboardPage", "LogbookWorkspace", "PAGES"]
