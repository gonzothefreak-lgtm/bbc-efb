"""Record-based Logbook summaries (pure) and the Statistics page (Q5).

``summarise(views)`` computes every number the Dashboard and Statistics pages show,
only from what the records actually hold:

* Flight time adds flights whose time is known and *counts* the flights without one.
* Distance, landings and touchdown rates use measured (or explicitly entered /
  imported) values only; the number of flights without a value is reported beside
  every total. Nothing is estimated from routes or aircraft types.
* Touchdown-rate statistics use the magnitude of the measured descent rate, because
  stored values may be signed either way. Best is the softest, worst the firmest.
  The distribution uses neutral 50 fpm bands with no grading (grading thresholds are
  not approved by the owner).
* Simulated Recorder flights are excluded from summaries by default and the number
  excluded is shown (PROPOSAL; ``include_simulated=True`` includes them).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from .charts import BarChart, DonutChart, HorizontalBarChart, Segment
from .model import FlightView, format_hours, operation_label, rate_magnitude

# Neutral touchdown-rate bands (fpm, magnitude). Upper bound exclusive.
RATE_BANDS: tuple[tuple[float, float, str], ...] = (
    (0, 100, "< 100"), (100, 150, "100–150"), (150, 200, "150–200"), (200, 250, "200–250"),
    (250, 300, "250–300"), (300, 400, "300–400"), (400, math.inf, "≥ 400"),
)


@dataclass(frozen=True, slots=True)
class Total:
    """A sum over the flights that have the value, plus how many do not."""

    value: float = 0.0
    known: int = 0
    unknown: int = 0

    @property
    def complete(self) -> bool:
        return self.unknown == 0


@dataclass(frozen=True, slots=True)
class RateStats:
    measured: int = 0                     # flights with a measured (or imported) touchdown rate
    unmeasured: int = 0                   # flights that landed (or may have) without one
    average_fpm: float | None = None      # magnitudes
    best_fpm: float | None = None         # softest magnitude
    worst_fpm: float | None = None        # firmest magnitude
    imported: int = 0                     # of ``measured``, how many came from imports
    distribution: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class AircraftProfile:
    """One aircraft *type* (never livery) with its flights and known time."""

    key: str
    flights: int
    time: Total
    last_flown: date | None = None


@dataclass(frozen=True, slots=True)
class FreightStats:
    freight_kg: Total = Total()
    baggage_kg: Total = Total()
    passengers: Total = Total()
    flights_with_freight: int = 0


@dataclass(frozen=True, slots=True)
class LogbookSummary:
    flights: int = 0
    by_source: tuple[tuple[str, int], ...] = ()
    simulated_excluded: int = 0
    time: Total = Total()                  # logged flight time, seconds
    block: Total = Total()                 # Recorder block time, seconds (Recorder flights only)
    airborne: Total = Total()              # Recorder airborne time, seconds
    distance_nm: Total = Total()
    landings: Total = Total()
    rates: RateStats = RateStats()
    aircraft: tuple[AircraftProfile, ...] = ()
    aircraft_unknown: int = 0
    operations: tuple[tuple[str, int], ...] = ()
    operations_unset: int = 0
    airports: tuple[tuple[str, int], ...] = ()
    monthly: tuple[tuple[str, float, int], ...] = ()   # (YYYY-MM, known seconds, flights)
    undated: int = 0
    freight: FreightStats = FreightStats()
    with_gaps: int = 0
    needing_review: int = 0
    recent: tuple[FlightView, ...] = field(default=(), compare=False)


def _total(values: Iterable[float | None]) -> Total:
    known = unknown = 0
    value = 0.0
    for item in values:
        if item is None:
            unknown += 1
        else:
            known += 1
            value += float(item)
    return Total(value, known, unknown)


def rate_distribution(magnitudes: Sequence[float]) -> tuple[tuple[str, int], ...]:
    counts = [0] * len(RATE_BANDS)
    for value in magnitudes:
        for index, (low, high, _label) in enumerate(RATE_BANDS):
            if low <= value < high:
                counts[index] += 1
                break
    return tuple((label, count) for (_l, _h, label), count in zip(RATE_BANDS, counts))


def _month_keys(today: date, months: int) -> list[str]:
    keys = []
    year, month = today.year, today.month
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def recent_flights(views: Sequence[FlightView], count: int = 5) -> tuple[FlightView, ...]:
    """Newest first by flight date, then by last save time. Undated flights last."""
    ordered = sorted(views, key=lambda v: (v.sort_date, v.updated_at), reverse=True)
    dated = [view for view in ordered if view.flight_date is not None]
    undated = [view for view in ordered if view.flight_date is None]
    return tuple((dated + undated)[:count])


def summarise(views: Sequence[FlightView], *, include_simulated: bool = False, today: date | None = None,
              months: int = 12, recent: int = 5) -> LogbookSummary:
    today = today or date.today()
    excluded = 0 if include_simulated else sum(1 for view in views if view.simulated)
    flights = [view for view in views if include_simulated or not view.simulated]
    by_source = Counter(view.source for view in flights)

    rate_values = [(rate_magnitude(view.touchdown_rate_fpm), view.touchdown_rate_origin)
                   for view in flights if view.touchdown_rate_fpm is not None]
    magnitudes = [value for value, _origin in rate_values]
    # A flight "should have" a rate if it has a recorded or entered landing, or is a Recorder flight.
    unmeasured = sum(
        1 for view in flights if view.touchdown_rate_fpm is None
        and (view.source == "recorder" or (view.landings or 0) > 0 or view.landing is not None)
    )
    rates = RateStats(
        len(magnitudes), unmeasured,
        sum(magnitudes) / len(magnitudes) if magnitudes else None,
        min(magnitudes) if magnitudes else None, max(magnitudes) if magnitudes else None,
        sum(1 for _value, origin in rate_values if origin == "imported"),
        rate_distribution(magnitudes) if magnitudes else (),
    )

    per_type: dict[str, list[FlightView]] = defaultdict(list)
    aircraft_unknown = 0
    for view in flights:
        if view.aircraft_key:
            per_type[view.aircraft_key].append(view)
        else:
            aircraft_unknown += 1
    profiles = []
    for key, items in per_type.items():
        dates = [view.flight_date for view in items if view.flight_date is not None]
        profiles.append(AircraftProfile(key, len(items), _total(view.flight_time_s for view in items),
                                        max(dates) if dates else None))
    profiles.sort(key=lambda p: (-p.flights, -p.time.value, p.key))

    operations = Counter(operation_label(view.operation) for view in flights if view.operation.strip())
    airports = Counter()
    for view in flights:
        for code in {view.departure.upper(), view.arrival.upper()} - {""}:
            airports[code] += 1

    month_keys = _month_keys(today, months)
    monthly_time: dict[str, float] = {key: 0.0 for key in month_keys}
    monthly_count: dict[str, int] = {key: 0 for key in month_keys}
    undated = 0
    for view in flights:
        if view.flight_date is None:
            undated += 1
            continue
        key = f"{view.flight_date.year:04d}-{view.flight_date.month:02d}"
        if key in monthly_count:
            monthly_count[key] += 1
            if view.flight_time_s is not None:
                monthly_time[key] += view.flight_time_s

    recorder = [view for view in flights if view.source == "recorder"]
    freight = FreightStats(
        _total(view.freight_kg for view in flights), _total(view.baggage_kg for view in flights),
        _total(view.passengers for view in flights),
        sum(1 for view in flights if (view.freight_kg or 0) > 0),
    )
    return LogbookSummary(
        flights=len(flights),
        by_source=tuple(sorted(by_source.items())),
        simulated_excluded=excluded,
        time=_total(view.flight_time_s for view in flights),
        block=_total(view.block_s for view in recorder),
        airborne=_total(view.airborne_s for view in recorder),
        distance_nm=_total(view.distance_nm for view in flights),
        landings=_total(view.landings for view in flights),
        rates=rates,
        aircraft=tuple(profiles),
        aircraft_unknown=aircraft_unknown,
        operations=tuple(operations.most_common()),
        operations_unset=sum(1 for view in flights if not view.operation.strip()),
        airports=tuple(sorted(airports.items(), key=lambda item: (-item[1], item[0]))),
        monthly=tuple((key, monthly_time[key], monthly_count[key]) for key in month_keys),
        undated=undated,
        freight=freight,
        with_gaps=sum(1 for view in flights if view.gaps),
        needing_review=sum(1 for view in views if view.needs_attention),
        recent=recent_flights(views, recent),
    )


# Pilot-facing wording ------------------------------------------------------------------


def unknown_note(total: Total, what: str = "without a value") -> str:
    if total.unknown == 0:
        return ""
    noun = "flight" if total.unknown == 1 else "flights"
    return f"{total.unknown:,} {noun} {what}"


def time_text(total: Total) -> str:
    if total.known == 0:
        return "Unknown" if total.unknown else "0.0 h"
    return format_hours(total.value)


def distance_text(total: Total) -> str:
    if total.known == 0:
        return "Not recorded" if total.unknown else "0 nm"
    return f"{total.value:,.0f} nm"


def rate_text(value: float | None) -> str:
    return "Not measured" if value is None else f"{value:,.0f} fpm"


def time_by_aircraft_segments(summary: LogbookSummary) -> list[Segment]:
    return [Segment(p.key, p.time.value / 3600.0, f"{p.time.value / 3600.0:,.1f} h")
            for p in summary.aircraft if p.time.known]


def time_by_aircraft_footnote(summary: LogbookSummary) -> str:
    missing = sum(p.time.unknown for p in summary.aircraft)
    if not missing:
        return ""
    return f"{missing:,} flight{'s' if missing != 1 else ''} without a known time not included."


def rate_bars(summary: LogbookSummary) -> list[Segment]:
    return [Segment(label, count, str(count)) for label, count in summary.rates.distribution]


def rate_footnote(summary: LogbookSummary) -> str:
    rates = summary.rates
    text = f"Measured on {rates.measured:,} flight{'s' if rates.measured != 1 else ''}"
    if rates.imported:
        text += f" ({rates.imported:,} imported)"
    if rates.unmeasured:
        text += f"; {rates.unmeasured:,} without a measured rate"
    return text + "."


# Statistics page ----------------------------------------------------------------------


def _card(title: str, description: str = "") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    heading = QLabel(title)
    heading.setObjectName("cardTitle")
    layout.addWidget(heading)
    if description:
        note = QLabel(description)
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
    return frame, layout


class Metric(QFrame):
    """A KPI tile: title, value and an honest note about what is missing."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.title = QLabel(title)
        self.title.setObjectName("muted")
        self.value = QLabel("—")
        self.value.setObjectName("metric")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.note)
        self.setMinimumWidth(150)

    def set(self, value: str, note: str = "") -> None:
        self.value.setText(value)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.setAccessibleName(f"{self.title.text()}: {value}" + (f". {note}" if note else ""))


class StatisticsPage(QWidget):
    """Deeper, chart-driven view of the verifiable records (Q5)."""

    include_simulated_changed = Signal(bool)

    def __init__(self, parent=None, *, today=date.today):
        super().__init__(parent)
        self.setObjectName("page")
        self._today = today
        self._views: list[FlightView] = []
        self.summary = LogbookSummary()
        title = QLabel("Statistics")
        title.setObjectName("heading")
        subtitle = QLabel("Totals and trends from your saved records. Values that were not recorded are counted, never guessed.")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        self.include_simulated = QCheckBox("Include simulated-data flights")
        self.include_simulated.setToolTip("Flights logged from the simulated provider are not native simulator data.")
        self.include_simulated.toggled.connect(self._toggle_simulated)
        self.excluded_note = QLabel("")
        self.excluded_note.setObjectName("muted")

        self.metrics = {key: Metric(title) for key, title in (
            ("flights", "Flights"), ("time", "Flight time"), ("block", "Block time (Recorder)"),
            ("airborne", "Airborne time (Recorder)"), ("distance", "Distance"), ("landings", "Landings"),
            ("rate_avg", "Average touchdown rate"), ("rate_best", "Softest touchdown"), ("rate_worst", "Firmest touchdown"),
            ("freight", "Freight carried"), ("passengers", "Passengers carried"), ("gaps", "Flights with data gaps"),
        )}
        self.monthly = BarChart("Monthly flight time", colour="#3c9bdd")
        self.by_aircraft_time = DonutChart("Flight time by aircraft type")
        self.by_aircraft_flights = HorizontalBarChart("Flights by aircraft type")
        self.rates = BarChart("Touchdown rate distribution")
        self.operations = DonutChart("Flights by operation")
        self.airports = HorizontalBarChart("Most visited airports")
        self.sources = DonutChart("Flights by record source")

        grid = QGridLayout()
        grid.setSpacing(10)
        for index, metric in enumerate(self.metrics.values()):
            grid.addWidget(metric, index // 6, index % 6)
        charts = QGridLayout()
        charts.setSpacing(12)
        placements = (
            ("Monthly flight time", "Last 12 months; only flights with a known time add hours.", self.monthly, 0, 0, 1, 2),
            ("Flight time by aircraft type", "", self.by_aircraft_time, 1, 0, 1, 1),
            ("Flights by aircraft type", "", self.by_aircraft_flights, 1, 1, 1, 1),
            ("Touchdown rates", "Measured descent rate at touchdown, in fpm. No grading is applied.", self.rates, 2, 0, 1, 1),
            ("Operations", "", self.operations, 2, 1, 1, 1),
            ("Airports", "Departures and arrivals combined.", self.airports, 3, 0, 1, 1),
            ("Record sources", "", self.sources, 3, 1, 1, 1),
        )
        for title_text, description, chart, row, column, rspan, cspan in placements:
            frame, layout = _card(title_text, description)
            layout.addWidget(chart, 1)
            chart.setMinimumHeight(190)
            charts.addWidget(frame, row, column, rspan, cspan)
        charts.setColumnStretch(0, 1)
        charts.setColumnStretch(1, 1)

        body = QWidget()
        body.setObjectName("page")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading, 1)
        options = QVBoxLayout()
        options.addWidget(self.include_simulated)
        options.addWidget(self.excluded_note)
        header.addLayout(options)
        layout.addLayout(header)
        layout.addLayout(grid)
        layout.addLayout(charts)
        layout.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)

    def _toggle_simulated(self, checked: bool):
        self.include_simulated_changed.emit(checked)
        self._render()

    def set_flights(self, views: Sequence[FlightView]) -> None:
        self._views = list(views)
        self._render()

    def _render(self):
        s = summarise(self._views, include_simulated=self.include_simulated.isChecked(), today=self._today())
        self.summary = s
        self.excluded_note.setText(
            f"{s.simulated_excluded:,} simulated-data flight{'s' if s.simulated_excluded != 1 else ''} not included."
            if s.simulated_excluded else "")
        m = self.metrics
        m["flights"].set(f"{s.flights:,}", ", ".join(f"{count:,} {source}" for source, count in s.by_source))
        m["time"].set(time_text(s.time), unknown_note(s.time, "without a known time"))
        m["block"].set(time_text(s.block) if s.block.known else "Not measured", unknown_note(s.block, "without measured block time"))
        m["airborne"].set(time_text(s.airborne) if s.airborne.known else "Not measured",
                          unknown_note(s.airborne, "without measured airborne time"))
        m["distance"].set(distance_text(s.distance_nm), unknown_note(s.distance_nm, "without a recorded distance"))
        m["landings"].set(f"{s.landings.value:,.0f}" if s.landings.known else "Not recorded",
                          unknown_note(s.landings, "without a landing count"))
        m["rate_avg"].set(rate_text(s.rates.average_fpm), rate_footnote(s) if s.flights else "")
        m["rate_best"].set(rate_text(s.rates.best_fpm))
        m["rate_worst"].set(rate_text(s.rates.worst_fpm))
        freight = s.freight
        m["freight"].set(f"{freight.freight_kg.value:,.0f} kg" if freight.freight_kg.known else "Not entered",
                         unknown_note(freight.freight_kg, "without a freight value"))
        m["passengers"].set(f"{freight.passengers.value:,.0f}" if freight.passengers.known else "Not entered",
                            unknown_note(freight.passengers, "without a passenger count"))
        m["gaps"].set(f"{s.with_gaps:,}", f"{s.needing_review:,} need review" if s.needing_review else "")

        undated = f" {s.undated:,} undated flight{'s' if s.undated != 1 else ''} not shown." if s.undated else ""
        self.monthly.set_data(
            [Segment(key[2:].replace("-", "/"), seconds / 3600.0, f"{seconds / 3600.0:,.0f}") for key, seconds, _n in s.monthly],
            "Hours per month", undated.strip())
        self.by_aircraft_time.set_data(time_by_aircraft_segments(s), time_text(s.time), "known time",
                                       time_by_aircraft_footnote(s))
        self.by_aircraft_flights.set_data(
            [Segment(p.key, p.flights, f"{p.flights:,}") for p in s.aircraft],
            f"{s.aircraft_unknown:,} flight(s) without an aircraft type." if s.aircraft_unknown else "")
        self.rates.set_data(rate_bars(s), "Touchdown rate, fpm (descent)", rate_footnote(s) if s.flights else "")
        self.operations.set_data([Segment(label, count, f"{count:,}") for label, count in s.operations],
                                 f"{sum(c for _l, c in s.operations):,}", "with operation",
                                 f"{s.operations_unset:,} flight(s) without an operation." if s.operations_unset else "")
        self.airports.set_data([Segment(code, count, f"{count:,}") for code, count in s.airports])
        self.sources.set_data([Segment(source.capitalize(), count, f"{count:,}") for source, count in s.by_source],
                              f"{s.flights:,}", "flights")


__all__ = [
    "AircraftProfile", "LogbookSummary", "RATE_BANDS", "RateStats", "StatisticsPage", "Total", "Metric",
    "rate_distribution", "recent_flights", "summarise",
]
