"""Flight History: Table, Cards and Combined views over one filtered, sorted model.

* ``FlightTableModel`` (``QAbstractTableModel``) holds the ``FlightView`` list.
* ``FlightFilterProxy`` (``QSortFilterProxyModel``) applies the shared ``FilterState``
  and sorts on primitive keys in C++ (``ROLE_SORT``), so 2,000+ flights stay fast.
* ``FlightHistoryPage`` shows the table, the cards, or both (Combined) over the same
  proxy *and the same selection model*, so selection is coordinated and switching
  modes keeps the scroll position, filters and selection. Opening Flight Details
  covers the page with an in-EFB overlay and leaves all of that untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import (
    QAbstractTableModel, QEvent, QItemSelectionModel, QModelIndex, QObject,
    QSortFilterProxyModel, Qt, Signal,
)
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton,
    QSplitter, QTableView, QVBoxLayout, QWidget,
)

from .aircraft_art import AircraftArtLibrary
from .cards import FlightCardView, card_summary
from .filters import FilterBar, FilterState, FilterSummary, compile_filter
from .model import (
    ROLE_FLIGHT, ROLE_ID, ROLE_SORT, FlightView, format_date, format_duration, format_rate,
    format_time, rate_magnitude,
)

UNKNOWN = "—"
MUTED = QColor("#8ea3b8")
WARNING = QColor("#ffd693")
VIEW_MODES = ("table", "cards", "combined")
VIEW_MODE_LABELS = {"table": "Table", "cards": "Cards", "combined": "Combined"}
_NEG_INF = float("-inf")


@dataclass(frozen=True, slots=True)
class Column:
    key: str
    title: str
    text: Callable[[FlightView], str | None]   # None => unknown, shown as a muted dash
    sort: Callable[[FlightView], object]
    unknown_tip: str = "Not recorded"
    hidden: bool = False                         # hidden by default (pilot can show it)
    align_right: bool = False


def _num(value):
    return _NEG_INF if value is None else float(value)


def _time_text(value):
    return format_time(value, "") or None


COLUMNS: tuple[Column, ...] = (
    Column("date", "Date", lambda v: format_date(v), lambda v: v.sort_date, "Date unknown"),
    Column("callsign", "Callsign", lambda v: v.callsign or None, lambda v: v.callsign.casefold(), "No callsign"),
    Column("aircraft", "Aircraft", lambda v: v.aircraft or None, lambda v: v.aircraft_key, "Aircraft unknown"),
    Column("registration", "Registration", lambda v: v.registration or None, lambda v: v.registration.casefold(),
           "No registration", hidden=True),
    Column("departure", "From", lambda v: v.departure or None, lambda v: v.departure, "Departure unknown"),
    Column("arrival", "To", lambda v: v.arrival or None, lambda v: v.arrival, "Arrival unknown"),
    Column("operation", "Operation", lambda v: v.operation_label if v.operation else None,
           lambda v: v.operation_label.casefold() if v.operation else "", "Operation not set"),
    Column("off_block", "Off-block", lambda v: _time_text(v.off_block), lambda v: v.off_block or "", hidden=True),
    Column("takeoff", "Takeoff", lambda v: _time_text(v.takeoff), lambda v: v.takeoff or "", hidden=True),
    Column("landing", "Landing", lambda v: _time_text(v.landing), lambda v: v.landing or "", hidden=True),
    Column("on_block", "On-block", lambda v: _time_text(v.on_block), lambda v: v.on_block or "", hidden=True),
    Column("block", "Block", lambda v: format_duration(v.block_s, "") or None, lambda v: _num(v.block_s),
           "Not measured", align_right=True),
    Column("airborne", "Airborne", lambda v: format_duration(v.airborne_s, "") or None, lambda v: _num(v.airborne_s),
           "Not measured", hidden=True, align_right=True),
    Column("flight_time", "Flight time", lambda v: format_duration(v.flight_time_s, "") or None,
           lambda v: _num(v.flight_time_s), "Unknown", align_right=True),
    Column("landing_rate", "Touchdown rate", lambda v: format_rate(v.touchdown_rate_fpm, "") or None,
           # Stored rates may be negative (Recorder descent) or positive (imported): sort by
           # magnitude, as Statistics does, so the order is softest to firmest.
           lambda v: _num(None if v.touchdown_rate_fpm is None else rate_magnitude(v.touchdown_rate_fpm)), "Not measured", align_right=True),
    Column("source", "Source", lambda v: v.source_label, lambda v: v.source_label.casefold()),
    Column("status", "Status", lambda v: v.review_label,
           lambda v: ("0" if v.needs_confirmation else "1") + v.status),
)
COLUMN_KEYS = tuple(column.key for column in COLUMNS)
DEFAULT_HIDDEN = frozenset(column.key for column in COLUMNS if column.hidden)


class FlightTableModel(QAbstractTableModel):
    """Read-only table of ``FlightView`` rows. Replace the rows with ``set_flights``.

    Sorting happens here, in one Python ``list.sort`` with precomputed keys, rather than
    in the proxy (which would call ``data()`` ~n log n times): 2,000 rows sort in a few ms.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._flights: list[FlightView] = []
        self._rows: dict[str, int] = {}
        self._sort: tuple[int, Qt.SortOrder] | None = None
        self._italic = QFont()
        self._italic.setItalic(True)

    # Qt API ------------------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self._flights)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(COLUMNS):
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section].title
            if role == Qt.ItemDataRole.ToolTipRole and COLUMNS[section].key == "landing_rate":
                return "Vertical speed at touchdown as measured by the Recorder (or as imported). Never estimated."
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        view = self._flights[index.row()]
        column = COLUMNS[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            text = column.text(view)
            return UNKNOWN if text is None else text
        if role == ROLE_SORT:
            return column.sort(view)
        if role == ROLE_FLIGHT:
            return view
        if role == ROLE_ID:
            return view.id
        if role == Qt.ItemDataRole.ToolTipRole:
            if column.text(view) is None:
                return column.unknown_tip
            if index.column() == 0:
                return card_summary(view)
            if column.key == "status" and (view.problems or view.field_checks):
                return "\n".join([check.text for check in view.field_checks] + list(view.problems))
            return None
        if role == Qt.ItemDataRole.ForegroundRole:
            if column.text(view) is None:
                return MUTED
            if column.key == "status" and (view.needs_attention or view.needs_confirmation):
                return WARNING
            return None
        if role == Qt.ItemDataRole.FontRole and column.text(view) is None:
            return self._italic
        if role == Qt.ItemDataRole.TextAlignmentRole and column.align_right:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            text = column.text(view)
            return f"{column.title}: {column.unknown_tip if text is None else text}"
        return None

    # Rows --------------------------------------------------------------------------

    def set_flights(self, flights: Sequence[FlightView]) -> None:
        self.beginResetModel()
        self._flights = list(flights)
        if self._sort is not None:
            self._sort_list(*self._sort)
        self._rows = {view.id: row for row, view in enumerate(self._flights)}
        self.endResetModel()

    def _sort_list(self, column: int, order: Qt.SortOrder) -> None:
        key = COLUMNS[column].sort
        keys = {id(view): key(view) for view in self._flights}
        self._flights.sort(key=lambda view: keys[id(view)], reverse=order == Qt.SortOrder.DescendingOrder)

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if not 0 <= column < len(COLUMNS):
            return
        self._sort = (column, order)
        self.layoutAboutToBeChanged.emit()
        persistent = self.persistentIndexList()
        anchors = [(self._flights[index.row()].id, index.column()) for index in persistent]
        self._sort_list(column, order)
        self._rows = {view.id: row for row, view in enumerate(self._flights)}
        self.changePersistentIndexList(persistent, [self.index(self._rows[fid], col) for fid, col in anchors])
        self.layoutChanged.emit()

    def clear_sort(self) -> None:
        """Keep rows in the order given to ``set_flights`` (used by the Dashboard's recent list)."""
        self._sort = None

    @property
    def sort_state(self) -> tuple[int, Qt.SortOrder] | None:
        return self._sort

    def flights(self) -> list[FlightView]:
        return list(self._flights)

    def flight_at(self, row: int) -> FlightView:
        return self._flights[row]

    def row_of(self, flight_id: str) -> int:
        return self._rows.get(flight_id.lower(), -1)

    def flight(self, flight_id: str) -> FlightView | None:
        row = self.row_of(flight_id)
        return self._flights[row] if row >= 0 else None


class FlightFilterProxy(QSortFilterProxyModel):
    """Applies a ``FilterState``. Sorting is delegated to the source model (unknown sorts lowest)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = FilterState()
        self._accept = compile_filter(self._state)
        self.setSortRole(ROLE_SORT)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setDynamicSortFilter(True)

    @property
    def state(self) -> FilterState:
        return self._state

    def set_filter(self, state: FilterState) -> None:
        if state == self._state:
            return
        self._state = state
        self._accept = compile_filter(state)
        if hasattr(self, "beginFilterChange"):  # Qt 6.9+
            self.beginFilterChange()
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.invalidateRowsFilter()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        model = self.sourceModel()
        if model is not None:
            model.sort(column, order)  # the proxy keeps source order, so it shows the sorted rows

    def filterAcceptsRow(self, source_row, source_parent):  # noqa: N802
        model = self.sourceModel()
        if model is None:
            return False
        return self._accept(model.flight_at(source_row))

    def flight_at(self, row: int) -> FlightView:
        return self.index(row, 0).data(ROLE_FLIGHT)

    def visible_ids(self) -> list[str]:
        return [self.index(row, 0).data(ROLE_ID) for row in range(self.rowCount())]

    def proxy_row_of(self, flight_id: str) -> int:
        model = self.sourceModel()
        source_row = model.row_of(flight_id) if model is not None else -1
        if source_row < 0:
            return -1
        return self.mapFromSource(model.index(source_row, 0)).row()


class EnterActivates(QObject):
    """Enter/Return opens the current item on every platform (macOS would start editing)."""

    KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)

    def __init__(self, view, callback):
        super().__init__(view)
        self._view = view
        self._callback = callback
        view.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if (watched is self._view and event.type() == QEvent.Type.KeyPress and event.key() in self.KEYS
                and not event.isAutoRepeat() and event.modifiers() in (Qt.KeyboardModifier.NoModifier,
                                                                       Qt.KeyboardModifier.KeypadModifier)):
            index = self._view.currentIndex()
            if index.isValid():
                self._callback(index)
            return True
        return False


def make_table_view(model, *, accessible_name="Flight history table") -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.setAlternatingRowColors(True)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSortingEnabled(True)
    table.setWordWrap(False)
    table.setTabKeyNavigation(False)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(max(26, table.fontMetrics().height() + 10))
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    header.setHighlightSections(False)
    header.setSortIndicatorShown(True)
    table.setAccessibleName(accessible_name)
    metrics = table.fontMetrics()
    if isinstance(model, (FlightTableModel, FlightFilterProxy)):
        wide = {"date": 13, "aircraft": 14, "status": 26, "source": 16, "operation": 11, "landing_rate": 12}
        for index, column in enumerate(COLUMNS):
            text = max(metrics.horizontalAdvance(column.title) + 34, metrics.averageCharWidth() * wide.get(column.key, 9))
            table.setColumnWidth(index, int(text))
    table.setStyleSheet(
        "QTableView { background: #101a26; alternate-background-color: #162333; border: 1px solid #2a4055;"
        " border-radius: 5px; gridline-color: #24374a; selection-background-color: #1a5682; }"
        "QTableView:focus { border: 1px solid #86caff; }"
    )
    return table


class FlightHistoryPage(QWidget):
    """Flight History page. Pages and the workspace talk to it through signals."""

    flight_opened = Signal(str)          # a flight was activated (overlay opened if attached)
    edit_requested = Signal(str)
    new_flight_requested = Signal()
    view_mode_changed = Signal(str)
    columns_changed = Signal(object)     # frozenset of hidden column keys
    filter_changed = Signal(object)      # FilterState

    def __init__(self, parent=None, *, library: AircraftArtLibrary | None = None, details=None,
                 today: Callable[[], date] = date.today, view_mode: str = "combined",
                 hidden_columns: frozenset[str] | None = None, can_edit: bool = True):
        super().__init__(parent)
        self.setObjectName("page")
        self.details = details  # a FlightDetailsOverlay (shared), attached by the workspace
        self._can_edit = can_edit
        self.model = FlightTableModel(self)
        self.proxy = FlightFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.title = QLabel("Flight History")
        self.title.setObjectName("heading")
        self.subtitle = QLabel("Browse, search and open your flights.")
        self.subtitle.setObjectName("muted")
        self.mode_buttons = QButtonGroup(self)
        self.mode_buttons.setExclusive(True)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        for mode in VIEW_MODES:
            button = QPushButton(VIEW_MODE_LABELS[mode])
            button.setCheckable(True)
            button.setAccessibleName(f"{VIEW_MODE_LABELS[mode]} view")
            button.setProperty("mode", mode)
            self.mode_buttons.addButton(button)
            mode_row.addWidget(button)
        self.mode_buttons.buttonClicked.connect(lambda button: self.set_view_mode(button.property("mode")))
        self.open_button = QPushButton("Open")
        self.open_button.setAccessibleName("Open selected flight details")
        self.open_button.clicked.connect(self.open_selected)
        self.edit_button = QPushButton("Edit")
        self.edit_button.setAccessibleName("Edit selected flight")
        self.edit_button.clicked.connect(self._edit_selected)
        self.new_button = QPushButton("New flight")
        self.new_button.setObjectName("primary")
        self.new_button.clicked.connect(self.new_flight_requested)

        self.filter_bar = FilterBar(self, today=today)
        self.filter_bar.changed.connect(self.set_filter)
        self.summary = FilterSummary(self)

        self.table = make_table_view(self.proxy)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.cards = FlightCardView(self, library)
        self.cards.setModel(self.proxy)
        self.cards.setSelectionModel(self.table.selectionModel())  # coordinated selection
        self._enter_filters = []
        for view in (self.table, self.cards):
            view.doubleClicked.connect(self._activated)
            self._enter_filters.append(EnterActivates(view, self._activated))
        self.table.selectionModel().currentRowChanged.connect(lambda *_: self._sync_buttons())
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._sync_buttons())
        header = self.table.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._column_menu)

        self.empty = QLabel()
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.hide()

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.cards)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)

        top = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(self.title)
        heading.addWidget(self.subtitle)
        top.addLayout(heading, 1)
        top.addLayout(mode_row)
        actions = QHBoxLayout()
        actions.addWidget(self.summary, 1)
        actions.addWidget(self.open_button)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.new_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addWidget(self.filter_bar)
        layout.addLayout(actions)
        layout.addWidget(self.empty)
        layout.addWidget(self.splitter, 1)

        self._hidden: frozenset[str] = frozenset()
        self.set_hidden_columns(DEFAULT_HIDDEN if hidden_columns is None else hidden_columns, emit=False)
        self._mode = ""
        self.set_view_mode(view_mode if view_mode in VIEW_MODES else "combined", emit=False)
        self._update_summary()
        self._sync_buttons()

    # Data ---------------------------------------------------------------------------

    def set_flights(self, flights: Sequence[FlightView]) -> None:
        """Replace the flights, keeping filters, selection and scroll position."""
        selected = self.selected_id()
        scroll = (self.table.verticalScrollBar().value(), self.cards.verticalScrollBar().value())
        self.model.set_flights(flights)
        self.filter_bar.set_options(self.model.flights())
        self.filter_bar.refresh_relative_dates()  # a flight logged after midnight is not hidden
        if selected:
            self.select_flight(selected, scroll_to=False)
        self.table.verticalScrollBar().setValue(scroll[0])
        self.cards.verticalScrollBar().setValue(scroll[1])
        self._update_summary()
        self._sync_buttons()
        if self.details is not None and self.details.is_open:
            self.details.refresh_from(self.model.flight)

    def flights(self) -> list[FlightView]:
        return self.model.flights()

    def set_filter(self, state: FilterState) -> None:
        if self.filter_bar.state() != state:
            self.filter_bar.set_state(state)  # re-enters through changed
            return
        self.proxy.set_filter(state)
        self._update_summary()
        self._sync_buttons()
        self.filter_changed.emit(state)

    def filter_state(self) -> FilterState:
        return self.proxy.state

    def _update_summary(self):
        shown, total = self.proxy.rowCount(), self.model.rowCount()
        self.summary.show_counts(shown, total, self.proxy.state)
        if total == 0:
            self.empty.setText("No flights in the Logbook yet. Add a flight, or turn on Flight Logging.")
        elif shown == 0:
            self.empty.setText("No flights match the search and filters. Reset filters to see every flight.")
        self.empty.setVisible(shown == 0)

    # View modes and columns -------------------------------------------------------

    @property
    def view_mode(self) -> str:
        return self._mode

    def set_view_mode(self, mode: str, emit: bool = True) -> None:
        if mode not in VIEW_MODES:
            raise ValueError(f"Unknown Flight History view {mode!r}.")
        for button in self.mode_buttons.buttons():
            button.setChecked(button.property("mode") == mode)
        if mode == self._mode:
            return
        self._mode = mode
        self.table.setVisible(mode in ("table", "combined"))
        self.cards.setVisible(mode in ("cards", "combined"))
        if emit:
            self.view_mode_changed.emit(mode)

    @property
    def hidden_columns(self) -> frozenset[str]:
        return self._hidden

    def set_hidden_columns(self, keys, emit: bool = True) -> None:
        keys = frozenset(key for key in keys if key in COLUMN_KEYS and key != "date")
        for index, column in enumerate(COLUMNS):
            self.table.setColumnHidden(index, column.key in keys)
        changed = keys != self._hidden
        self._hidden = keys
        if emit and changed:
            self.columns_changed.emit(keys)

    def _column_menu(self, position):
        menu = QMenu(self)
        for column in COLUMNS:
            action = QAction(column.title, menu)
            action.setCheckable(True)
            action.setChecked(column.key not in self._hidden)
            action.setEnabled(column.key != "date")
            action.toggled.connect(lambda checked, key=column.key: self._toggle_column(key, checked))
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction("Reset columns", lambda: self.set_hidden_columns(DEFAULT_HIDDEN))
        menu.popup(self.table.horizontalHeader().mapToGlobal(position))

    def _toggle_column(self, key: str, visible: bool):
        hidden = set(self._hidden)
        (hidden.discard if visible else hidden.add)(key)
        self.set_hidden_columns(hidden)

    # Selection and opening ----------------------------------------------------------

    def selected_id(self) -> str:
        indexes = self.table.selectionModel().selectedRows()
        if indexes:
            return indexes[0].data(ROLE_ID) or ""
        current = self.table.selectionModel().currentIndex()
        if current.isValid() and self.table.selectionModel().isRowSelected(current.row(), QModelIndex()):
            return current.data(ROLE_ID) or ""
        return ""

    def select_flight(self, flight_id: str, scroll_to: bool = True) -> bool:
        row = self.proxy.proxy_row_of(flight_id)
        if row < 0:
            self.table.selectionModel().clearSelection()
            return False
        index = self.proxy.index(row, 0)
        self.table.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        if scroll_to:
            if self.table.isVisible() or self._mode != "cards":
                self.table.scrollTo(index)
            if self.cards.isVisible() or self._mode == "cards":
                self.cards.scrollTo(index)
        return True

    def _sync_buttons(self):
        selected = bool(self.selected_id())
        self.open_button.setEnabled(selected)
        view = self.model.flight(self.selected_id()) if selected else None
        self.edit_button.setEnabled(bool(view) and self._can_edit and not view.read_only)
        if view is not None and view.read_only:
            self.edit_button.setToolTip(view.read_only_reason)
        else:
            self.edit_button.setToolTip("")
        self.new_button.setEnabled(self._can_edit)

    def set_can_edit(self, can_edit: bool, reason: str = "") -> None:
        self._can_edit = can_edit
        self.new_button.setToolTip("" if can_edit else reason)
        self._sync_buttons()
        if self.details is not None:
            self.details.set_can_edit(can_edit, reason)

    def _activated(self, index: QModelIndex):
        flight_id = index.data(ROLE_ID)
        if flight_id:
            self.open_flight(flight_id)

    def open_selected(self):
        flight_id = self.selected_id()
        if flight_id:
            self.open_flight(flight_id)

    def open_flight(self, flight_id: str, tab: str = "overview") -> bool:
        view = self.model.flight(flight_id)
        if view is None:
            return False
        self.select_flight(flight_id, scroll_to=False)
        if self.details is not None:
            self.details.open_flight(view, tab)
        self.flight_opened.emit(view.id)
        return True

    def _edit_selected(self):
        flight_id = self.selected_id()
        if flight_id:
            self.edit_requested.emit(flight_id)

    # Context capture (used by tests and by the workspace when switching pages) -------

    def browsing_context(self) -> dict:
        return {
            "filter": self.proxy.state, "mode": self._mode, "selected": self.selected_id(),
            "table_scroll": self.table.verticalScrollBar().value(),
            "cards_scroll": self.cards.verticalScrollBar().value(),
            "sort": (self.table.horizontalHeader().sortIndicatorSection(),
                     self.table.horizontalHeader().sortIndicatorOrder()),
        }


__all__ = [
    "COLUMNS", "COLUMN_KEYS", "DEFAULT_HIDDEN", "Column", "FlightFilterProxy", "FlightHistoryPage",
    "FlightTableModel", "VIEW_MODES", "make_table_view",
]