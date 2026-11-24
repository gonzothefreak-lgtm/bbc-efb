"""Shared Flight History search and filters (pure matching plus the filter bar widget).

One ``FilterState`` drives the Table, Cards and Combined views, so they always show
the same flights. Matching is compiled once per change into a predicate, which keeps
filtering 2,000+ flights well under a frame.

Rules (documented for the owner):

* Search: every word typed must appear somewhere in the flight's date, aircraft,
  route, callsign, registration, operation, source or notes (case-insensitive).
* Date range: inclusive. Flights whose date is unknown are hidden while a date range
  is active and shown again when the range is cleared.
* Aircraft: the type identity (trimmed, upper case), never the livery or airline.
* Airport: matches From or To. A confirmed diversion's planned destination also matches.
* Source: manual, recorder, imported. Has gaps: only Recorder flights can have gaps.
* Confirmation: "Needs confirmation" shows only flights with Recorder fields (a weak
  aircraft title, an ambiguous airport, a suspected diversion) the pilot has not yet
  confirmed or corrected; "Confirmed by the pilot" shows flights with a confirmed field.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from PySide6.QtCore import QDate, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QWidget,
)

from .model import SOURCE_LABELS, FlightView, aircraft_key, operation_label

GAP_FILTERS = {
    "any": "All records",
    "with": "With data gaps",
    "needs_review": "Gaps needing review",
    "without": "Without data gaps",
}
DATE_PRESETS = {
    "all": "All dates",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "12m": "Last 12 months",
    "year": "This year",
    "custom": "Custom range",
}
CONFIRMATION_FILTERS = {
    "any": "Any confirmation",
    "needs": "Needs confirmation",
    "confirmed": "Confirmed by the pilot",
}
SEARCH_DEBOUNCE_MS = 150


@dataclass(frozen=True, slots=True)
class FilterState:
    text: str = ""
    date_from: date | None = None
    date_to: date | None = None
    date_preset: str = "all"
    aircraft: frozenset[str] = field(default_factory=frozenset)
    airport: str = ""
    sources: frozenset[str] = field(default_factory=frozenset)
    gaps: str = "any"
    operation: str = ""
    include_simulated: bool = True
    confirmation: str = "any"

    @property
    def active(self) -> bool:
        return self != FilterState()

    def describe(self) -> str:
        """Short plain-English description of what is filtered (for the count label)."""
        parts = []
        if self.text.strip():
            parts.append(f"search “{self.text.strip()}”")
        if self.date_from or self.date_to:
            start = self.date_from.isoformat() if self.date_from else "earliest"
            end = self.date_to.isoformat() if self.date_to else "latest"
            parts.append(f"dates {start} to {end}")
        if self.aircraft:
            parts.append("aircraft " + ", ".join(sorted(self.aircraft)))
        if self.airport.strip():
            parts.append(f"airport {self.airport.strip().upper()}")
        if self.sources:
            parts.append("source " + ", ".join(SOURCE_LABELS.get(s, s) for s in sorted(self.sources)))
        if self.gaps != "any":
            parts.append(GAP_FILTERS[self.gaps].lower())
        if self.operation:
            parts.append(f"operation {operation_label(self.operation)}")
        if self.confirmation in CONFIRMATION_FILTERS and self.confirmation != "any":
            parts.append(CONFIRMATION_FILTERS[self.confirmation].lower())
        if not self.include_simulated:
            parts.append("simulated data hidden")
        return "; ".join(parts)


def preset_range(preset: str, today: date | None = None) -> tuple[date | None, date | None]:
    today = today or date.today()
    if preset == "30d":
        return today - timedelta(days=29), today
    if preset == "90d":
        return today - timedelta(days=89), today
    if preset == "12m":
        try:
            start = today.replace(year=today.year - 1) + timedelta(days=1)
        except ValueError:  # 29 February
            start = today.replace(year=today.year - 1, day=28) + timedelta(days=1)
        return start, today
    if preset == "year":
        return date(today.year, 1, 1), today
    return None, None


def compile_filter(state: FilterState) -> Callable[[FlightView], bool]:
    """Return a fast predicate for ``state``."""
    tokens = tuple(token.casefold() for token in state.text.split() if token)
    date_from, date_to = state.date_from, state.date_to
    dated = date_from is not None or date_to is not None
    aircraft = frozenset(aircraft_key(item) for item in state.aircraft if aircraft_key(item))
    airport = state.airport.strip().upper()
    sources = state.sources
    gaps = state.gaps
    operation = state.operation.strip().casefold()
    include_simulated = state.include_simulated
    confirmation = state.confirmation

    def accept(view: FlightView) -> bool:
        if tokens:
            haystack = view.search_text
            for token in tokens:
                if token not in haystack:
                    return False
        if dated:
            when = view.flight_date
            if when is None:
                return False
            if date_from is not None and when < date_from:
                return False
            if date_to is not None and when > date_to:
                return False
        if aircraft and view.aircraft_key not in aircraft:
            return False
        if airport:
            codes = {view.departure.upper(), view.arrival.upper()}
            if view.diverted:
                planned = view.diversion.get("planned_arrival")
                if isinstance(planned, str):
                    codes.add(planned.upper())
            if airport not in codes:
                return False
        if sources and view.source not in sources:
            return False
        if gaps != "any":
            if gaps == "with" and not view.gaps:
                return False
            if gaps == "without" and view.gaps:
                return False
            if gaps == "needs_review" and not view.unacknowledged_gaps:
                return False
        if operation and view.operation.strip().casefold() != operation:
            return False
        if not include_simulated and view.simulated:
            return False
        if confirmation == "needs" and not view.needs_confirmation:
            return False
        if confirmation == "confirmed" and not view.confirmed_fields:
            return False
        return True

    return accept


def matches(view: FlightView, state: FilterState) -> bool:
    return compile_filter(state)(view)


def apply(views: Iterable[FlightView], state: FilterState) -> list[FlightView]:
    accept = compile_filter(state)
    return [view for view in views if accept(view)]


def aircraft_options(views: Sequence[FlightView]) -> list[str]:
    seen: dict[str, str] = {}
    for view in views:
        key = view.aircraft_key
        if key and key not in seen:
            seen[key] = key
    return sorted(seen)


def operation_options(views: Sequence[FlightView]) -> list[str]:
    return sorted({view.operation.strip() for view in views if view.operation.strip()}, key=str.casefold)


# Widget --------------------------------------------------------------------------------


def _qdate(value: date | None) -> QDate:
    return QDate(value.year, value.month, value.day) if value else QDate.currentDate()


def _pydate(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


#: How often a relative date preset is checked against the calendar.
DAY_CHECK_MS = 60_000


class FilterBar(QWidget):
    """Search box and filters shared by every Flight History view. Emits ``changed``."""

    changed = Signal(object)  # FilterState

    def __init__(self, parent=None, *, today: Callable[[], date] = date.today):
        super().__init__(parent)
        self._today = today
        self._state = FilterState()
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit)
        # Relative presets ("Last 30 days", "This year") follow the calendar: re-resolve
        # them when the day changes, not only when a control is touched.
        self._day_timer = QTimer(self)
        self._day_timer.setInterval(DAY_CHECK_MS)
        self._day_timer.timeout.connect(self.refresh_relative_dates)
        self._day_timer.start()

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search flights (callsign, route, aircraft, notes)")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search flights")
        self.search.setMinimumWidth(180)
        self.search.textChanged.connect(lambda _text: self._debounce.start())
        self.search.returnPressed.connect(self.apply_now)

        self.date_preset = self._combo("Date range", DATE_PRESETS)
        self.date_from = QDateEdit()
        self.date_to = QDateEdit()
        for edit, name in ((self.date_from, "From date"), (self.date_to, "To date")):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setAccessibleName(name)
            edit.setDate(QDate.currentDate())
            edit.dateChanged.connect(self._emit)
            edit.hide()
        self.date_preset.currentIndexChanged.connect(self._preset_changed)

        self.aircraft = self._combo("Aircraft type", {"": "All aircraft types"})
        self.airport = QLineEdit()
        self.airport.setPlaceholderText("Airport (From or To)")
        self.airport.setAccessibleName("Airport filter")
        self.airport.setMaxLength(8)
        self.airport.setMaximumWidth(150)
        self.airport.textChanged.connect(lambda _text: self._debounce.start())
        self.source = self._combo("Record source", {"": "All sources", **dict(SOURCE_LABELS)})
        self.gaps = self._combo("Data gaps", GAP_FILTERS)
        self.operation = self._combo("Operation", {"": "All operations"})
        self.confirmation = self._combo("Field confirmation", CONFIRMATION_FILTERS)
        self.confirmation.setToolTip("Recorder fields the pilot still needs to confirm or correct")
        for combo in (self.aircraft, self.source, self.gaps, self.operation, self.confirmation):
            combo.currentIndexChanged.connect(self._emit)
        self.reset_button = QPushButton("Reset filters")
        self.reset_button.setAccessibleName("Reset search and filters")
        self.reset_button.clicked.connect(self.reset)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.search, 3)
        for widget in (self.date_preset, self.date_from, self.date_to, self.aircraft, self.airport,
                       self.source, self.gaps, self.operation, self.confirmation, self.reset_button):
            layout.addWidget(widget)
        self._sync_reset()

    @staticmethod
    def _combo(name: str, items: dict[str, str]) -> QComboBox:
        combo = QComboBox()
        combo.setAccessibleName(name)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        for key, label in items.items():
            combo.addItem(label, key)
        return combo

    # State -------------------------------------------------------------------------

    def state(self) -> FilterState:
        return self._state

    def _read(self) -> FilterState:
        preset = self.date_preset.currentData() or "all"
        if preset == "custom":
            start, end = _pydate(self.date_from.date()), _pydate(self.date_to.date())
            if start > end:
                start, end = end, start
        else:
            start, end = preset_range(preset, self._today())
        aircraft = self.aircraft.currentData() or ""
        source = self.source.currentData() or ""
        return replace(
            self._state, text=self.search.text(), date_from=start, date_to=end, date_preset=preset,
            aircraft=frozenset({aircraft}) if aircraft else frozenset(),
            airport=self.airport.text().strip().upper(),
            sources=frozenset({source}) if source else frozenset(),
            gaps=self.gaps.currentData() or "any", operation=self.operation.currentData() or "",
            confirmation=self.confirmation.currentData() or "any",
        )

    def apply_now(self):
        self._debounce.stop()
        self._emit()

    def refresh_relative_dates(self) -> bool:
        """Re-resolve a relative date preset against today. Emits ``changed`` (and returns
        True) only when the resolved dates moved, for example after midnight."""
        preset = self._state.date_preset
        if preset in ("all", "custom"):
            return False
        start, end = preset_range(preset, self._today())
        if (start, end) == (self._state.date_from, self._state.date_to):
            return False
        self._state = replace(self._state, date_from=start, date_to=end)
        self._sync_reset()
        self.changed.emit(self._state)
        return True

    def _emit(self, *_args):
        self._debounce.stop()
        state = self._read()
        if state != self._state:
            self._state = state
            self._sync_reset()
            self.changed.emit(state)

    def _sync_reset(self):
        self.reset_button.setEnabled(self._state.active)

    def _preset_changed(self, _index):
        custom = self.date_preset.currentData() == "custom"
        self.date_from.setVisible(custom)
        self.date_to.setVisible(custom)
        self._emit()

    def reset(self):
        self.set_state(FilterState(include_simulated=self._state.include_simulated))

    def set_state(self, state: FilterState) -> None:
        """Show ``state`` in the controls and emit ``changed`` once if it differs."""
        blockers = [QSignalBlocker(widget) for widget in (
            self.search, self.date_preset, self.date_from, self.date_to, self.aircraft,
            self.airport, self.source, self.gaps, self.operation, self.confirmation)]
        self.search.setText(state.text)
        self._select(self.date_preset, state.date_preset if state.date_preset in DATE_PRESETS else "all")
        if state.date_preset == "custom" or (state.date_preset == "all" and (state.date_from or state.date_to)):
            self._select(self.date_preset, "custom")
            self.date_from.setDate(_qdate(state.date_from))
            self.date_to.setDate(_qdate(state.date_to))
        custom = self.date_preset.currentData() == "custom"
        self.date_from.setVisible(custom)
        self.date_to.setVisible(custom)
        aircraft = next(iter(sorted(state.aircraft)), "")
        if aircraft and self.aircraft.findData(aircraft) < 0:
            self.aircraft.addItem(aircraft, aircraft)
        self._select(self.aircraft, aircraft)
        self.airport.setText(state.airport)
        self._select(self.source, next(iter(sorted(state.sources)), ""))
        self._select(self.gaps, state.gaps)
        if state.operation and self.operation.findData(state.operation) < 0:
            self.operation.addItem(operation_label(state.operation), state.operation)
        self._select(self.operation, state.operation)
        self._select(self.confirmation, state.confirmation if state.confirmation in CONFIRMATION_FILTERS else "any")
        for blocker in blockers:
            blocker.unblock()
        self._debounce.stop()
        self._state = replace(state, date_preset=self.date_preset.currentData() or "all")
        # Re-read so presets resolve to concrete dates exactly as the controls show them.
        resolved = self._read()
        self._state = resolved
        self._sync_reset()
        self.changed.emit(resolved)

    @staticmethod
    def _select(combo: QComboBox, key: str) -> None:
        index = combo.findData(key)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def set_options(self, views: Sequence[FlightView]) -> None:
        """Refresh the aircraft and operation choices from the flights, keeping selections."""
        for combo, options, first, label in (
            (self.aircraft, aircraft_options(views), "All aircraft types", lambda key: key),
            (self.operation, operation_options(views), "All operations", operation_label),
        ):
            current = combo.currentData() or ""
            blocker = QSignalBlocker(combo)
            combo.clear()
            combo.addItem(first, "")
            for key in options:
                combo.addItem(label(key), key)
            if current and combo.findData(current) < 0:
                combo.addItem(label(current), current)  # keep an active filter visible
            self._select(combo, current)
            blocker.unblock()


class FilterSummary(QLabel):
    """'Showing 12 of 642 flights' with the active filters spelled out."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("muted")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)

    def show_counts(self, shown: int, total: int, state: FilterState) -> None:
        noun = "flight" if total == 1 else "flights"
        if state.active:
            text = f"Showing {shown:,} of {total:,} {noun}"
            description = state.describe()
            if description:
                text += f" ({description})"
        else:
            text = f"{total:,} {noun}"
        self.setText(text)
