"""Read-only view model for the Logbook pages. No Qt, no storage import.

Every Logbook page (Dashboard, Flight History, Flight Details, Recorder Management,
Statistics) works from ``FlightView`` objects. They are built from the plain JSON
dictionaries that ``bbc_efb/STORAGE_SCHEMA.md`` describes (record formats 1 and 2),
or from any object that looks like ``bbc_efb.storage.Flight`` (duck typed: its
``raw`` payload is used when present, otherwise its attributes).

Honesty rules carried through every value here:

* Unknown is ``None`` (numbers) or ``""`` (text), never zero.
* Measured values (Recorder evidence) and pilot/imported values keep their origin.
* Nothing is inferred: no landing rate, landing count, distance, payload or diversion
  appears unless the record actually holds it.

Mapping from the stored record (see ``LOGBOOK_VIEWS.md`` for the full table):

==========================  ===================================================
FlightView                  stored record
==========================  ===================================================
id, source, schema          ``id``, ``source`` (format 1 => manual), ``schema``
flight fields               latest ``revisions[-1].fields``
extra values (operation,    latest revision ``fields`` unknown keys first, then the
freight_description,        top-level ``extra`` object, then the Recorder evidence
distance_nm, landings,      block (``recorder``) for measured values
diversion)
block_s, airborne_s, times  ``recorder.block_s`` ... ``recorder.on_block``
touchdown_rate_fpm          ``recorder.touchdown_rate_fpm`` (measured) or an
                            imported ``touchdown_rate_fpm`` on an imported record
gaps                        ``recorder.gaps`` + ``acknowledgements``
record history              ``revisions`` (``saved_at``, ``by``, ``reason``,
                            ``origins``)
==========================  ===================================================
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any

# Field vocabulary of the stored record (kept local so this module never imports storage).
FIELD_NAMES = (
    "flight_date", "aircraft", "departure", "arrival", "callsign", "registration",
    "minutes", "notes", "crew", "passengers", "baggage_kg", "freight_kg",
)
NUMBER_FIELDS = ("minutes", "crew", "passengers", "baggage_kg", "freight_kg")
FIELD_LABELS = MappingProxyType({
    "flight_date": "Flight date", "aircraft": "Aircraft type", "departure": "From",
    "arrival": "To", "callsign": "Callsign", "registration": "Registration",
    "minutes": "Duration", "notes": "Notes", "crew": "Crew", "passengers": "Passengers",
    "baggage_kg": "Baggage", "freight_kg": "Freight",
})
SOURCES = ("manual", "recorder", "imported")
SOURCE_LABELS = MappingProxyType({"manual": "Manual", "recorder": "Recorder", "imported": "Imported"})
ORIGIN_LABELS = MappingProxyType({
    "pilot": "Pilot entry", "observed": "Observed by the Recorder",
    "derived": "Derived by the Recorder", "recorder": "Recorder", "imported": "Imported",
})
AUTHOR_LABELS = MappingProxyType({"pilot": "Pilot", "recorder": "Recorder", "import": "Import"})
_DEFAULT_AUTHOR = {"manual": "pilot", "recorder": "recorder", "imported": "import"}
_DEFAULT_ORIGIN = {"pilot": "pilot", "recorder": "recorder", "import": "imported"}

# Operation categories a record may carry in ``operation`` (free text is kept as written).
OPERATION_LABELS = MappingProxyType({
    "airline": "Airline", "passenger": "Passenger", "freight": "Freight", "cargo": "Freight",
    "charter": "Charter", "ga": "GA", "general aviation": "GA", "training": "Training",
    "military": "Military", "other": "Other",
})

# Record status keys (``FlightView.status``) and their pilot-facing labels.
STATUS_LABELS = MappingProxyType({
    "manual": "Manual entry",
    "imported": "Imported",
    "import_review": "Imported, needs review",
    "recorded": "Recorded",
    "recorded_simulated": "Recorded (simulated data)",
    "gaps": "Data gaps, needs review",
    "reviewed": "Reviewed, data gaps accepted",
    "interrupted": "Logging interrupted",
    "evidence_missing": "Recorder evidence missing",
})
#: History and card marker for a flight with Recorder fields the pilot has not confirmed.
#: It sits beside the record status rather than replacing it, so gap reviews stay visible.
NEEDS_CONFIRMATION_LABEL = "Needs confirmation"
ATTENTION_STATUSES = frozenset({"import_review", "gaps", "interrupted", "evidence_missing"})
INTERRUPTED_RECORDER_STATES = frozenset({"interrupted", "incomplete", "recovered", "abandoned"})

TIME_BASIS_LABELS = MappingProxyType({
    "simulator": "simulator UTC", "host": "computer clock, UTC", "": "time basis unknown",
})

# Journal reading limits: keep the overlay responsive for long SIM_FRAME recordings.
MAX_JOURNAL_BYTES = 256 * 1024 * 1024
MAX_PROFILE_SAMPLES = 2000
ALTITUDE_KEYS = ("altitude_ft", "indicated_altitude_ft", "plane_altitude_ft", "alt_ft", "altitude")
RATE_KEYS = ("touchdown_rate_fpm", "rate_fpm", "vertical_speed_fpm", "touchdown_normal_velocity_fpm")
_SEARCH_NOTES_LIMIT = 2000


# Item-model roles shared by the Qt views (Qt.UserRole is 0x0100; plain ints keep this module Qt-free).
ROLE_FLIGHT = 0x0100 + 1   # the FlightView of a row
ROLE_ID = 0x0100 + 2       # the flight id of a row
ROLE_SORT = 0x0100 + 3     # a primitive sort key for a cell


class ViewError(ValueError):
    """A record that cannot be shown at all (not a flight record object)."""


# Small value objects -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapView:
    """One Recorder gap. ``acknowledged`` changes attention only, never the evidence."""

    index: int
    reason: str
    start: str | None = None
    end: str | None = None
    kind: str = ""
    acknowledged: bool = False
    acknowledged_at: str = ""
    acknowledged_by: str = ""
    note: str = ""

    @property
    def duration_s(self) -> float | None:
        start, end = parse_instant(self.start), parse_instant(self.end)
        if start is None or end is None:
            return None
        return max(0.0, (end - start).total_seconds())


@dataclass(frozen=True, slots=True)
class TouchdownView:
    """A touchdown or bounce the Recorder measured. ``rate_fpm`` is None when not measured."""

    time: str | None
    rate_fpm: float | None
    bounce: bool = False
    origin: str = "recorder"


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One entry of a Black Box event timeline."""

    kind: str
    label: str
    time: str | None = None
    sim_time_s: float | None = None
    phase: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProfilePoint:
    seconds: float
    altitude_ft: float


@dataclass(frozen=True, slots=True)
class FieldChange:
    name: str
    previous: Any
    value: Any


@dataclass(frozen=True, slots=True)
class RevisionView:
    index: int
    saved_at: str
    by: str
    reason: str
    changes: tuple[FieldChange, ...] = ()
    confirmed: tuple[str, ...] = ()   # fields the pilot confirmed as correct in this revision

    @property
    def by_label(self) -> str:
        return AUTHOR_LABELS.get(self.by, self.by.capitalize() or "Unknown")


@dataclass(frozen=True, slots=True)
class CorrectionView:
    at: str
    previous: Any
    value: Any
    by: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProvenanceView:
    name: str
    origin: str
    original: Any
    value: Any
    corrections: tuple[CorrectionView, ...] = ()
    confirmations: tuple[str, ...] = ()   # saved_at of each pilot confirmation
    #: The pilot confirmed this field after its last change (see STORAGE_SCHEMA.md).
    confirmed: bool = False

    @property
    def corrected(self) -> bool:
        return bool(self.corrections)

    @property
    def origin_label(self) -> str:
        return ORIGIN_LABELS.get(self.origin, self.origin.capitalize() or "Unknown")


#: Recorder attribution statuses that mean "the pilot should check this field".
CONFIRM_STATUSES = {
    "needs_confirmation": "Needs confirmation",
    "ambiguous": "Ambiguous",
    "incomplete": "Incomplete",
}


@dataclass(frozen=True, slots=True)
class FieldCheck:
    """A Recorder field the pilot should confirm (weak fallback, ambiguous, empty with a
    candidate, or a suspected diversion). Read from the evidence ``field_status`` and
    ``arrival_assessment``; a pilot correction or a pilot confirmation clears it."""

    name: str
    status: str
    reason: str = ""
    confidence: str = ""
    candidate: str = ""

    @property
    def status_label(self) -> str:
        return CONFIRM_STATUSES.get(self.status, "Not confirmed")

    @property
    def text(self) -> str:
        label = FIELD_LABELS.get(self.name, self.name)
        parts = [f"{label}: {self.status_label.lower()}"]
        if self.confidence:
            parts.append(f"{self.confidence} confidence")
        line = ", ".join(parts)
        if self.candidate:
            line += f". Candidate: {self.candidate}"
        if self.reason:
            line += f". {self.reason.rstrip('.')}."
        elif not line.endswith("."):
            line += "."
        return line


def _resolved_by_pilot(prov: ProvenanceView | None) -> bool:
    """The pilot has set or confirmed this field since the Recorder wrote it."""
    return prov is not None and (prov.confirmed or any(c.by == "pilot" for c in prov.corrections))


def suspected_diversion_of(ev: Mapping[str, Any], diversion: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The Recorder's arrival assessment when it suspects a diversion the pilot has not answered."""
    assessment = ev.get("arrival_assessment")
    if not isinstance(assessment, Mapping) or _text(assessment.get("verdict")) != "diversion_suspected":
        return None
    if isinstance(diversion, Mapping) and isinstance(diversion.get("confirmed"), bool):
        return None  # the pilot answered it
    return assessment


def _field_checks(ev: Mapping[str, Any], provenance: Mapping[str, ProvenanceView],
                  diversion: Mapping[str, Any] | None = None) -> tuple[FieldCheck, ...]:
    status_map = ev.get("field_status")
    status_map = status_map if isinstance(status_map, Mapping) else {}
    flagged = ev.get("needs_confirmation")
    flagged = {name for name in flagged if isinstance(name, str)} if isinstance(flagged, list) else set()
    suspected = suspected_diversion_of(ev, diversion)
    checks = []
    for name in FIELD_NAMES:
        item = status_map.get(name)
        item = item if isinstance(item, Mapping) else {}
        status = _text(item.get("status")).lower()
        candidate = _text(item.get("candidate"))
        reason = _text(item.get("reason"))
        confidence = _text(item.get("confidence")).lower()
        if name == "arrival" and suspected is not None:
            # A suspected diversion is always shown, even from a record without field_status.
            status = status if status in CONFIRM_STATUSES else "needs_confirmation"
            reason = reason or _text(suspected.get("reason"))
            confidence = confidence or "low"
        elif name in flagged and status not in CONFIRM_STATUSES:
            status = "needs_confirmation"
        elif status not in CONFIRM_STATUSES and not (candidate and status in ("unknown", "")):
            continue
        if _resolved_by_pilot(provenance.get(name)):
            continue  # the pilot has set or confirmed this field since; it is the pilot's value now
        checks.append(FieldCheck(name, status or "unknown", reason, confidence, candidate))
    return tuple(checks)


@dataclass(frozen=True, slots=True)
class Payload:
    """Total payload with honest labelling. Never adds an assumed passenger or crew mass."""

    known_kg: int | None
    complete: bool
    missing: tuple[str, ...]
    pilot_total_kg: int | None = None

    @property
    def label(self) -> str:
        if self.pilot_total_kg is not None:
            return f"{self.pilot_total_kg:,} kg (pilot-entered total)"
        if self.known_kg is None:
            return "Unknown"
        if self.complete:
            return f"{self.known_kg:,} kg (baggage + freight)"
        missing = " and ".join(self.missing)
        return f"At least {self.known_kg:,} kg (incomplete: {missing} unknown)"


# The flight view ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlightView:
    """Everything a Logbook page shows about one flight, read-only and precomputed."""

    id: str
    source: str
    flight_date: date | None
    flight_date_text: str
    aircraft: str
    departure: str
    arrival: str
    callsign: str = ""
    registration: str = ""
    notes: str = ""
    minutes: int | None = None
    crew: int | None = None
    passengers: int | None = None
    baggage_kg: int | None = None
    freight_kg: int | None = None
    schema: int = 2
    simulated: bool = False
    read_only_reason: str = ""
    problems: tuple[str, ...] = ()
    operation: str = ""
    freight_description: str = ""
    payload_total_kg: int | None = None
    diversion: Mapping[str, Any] | None = None
    # Recorder evidence (None when not measured or not a Recorder flight)
    has_evidence: bool = False
    recorder_state: str = ""
    time_basis: str = ""
    journal: str = ""
    summary: str = ""
    rules_version: str = ""
    off_block: str | None = None
    takeoff: str | None = None
    landing: str | None = None
    on_block: str | None = None
    block_s: float | None = None
    airborne_s: float | None = None
    touchdown_rate_fpm: float | None = None
    touchdown_rate_origin: str = ""
    touchdowns: tuple[TouchdownView, ...] = ()
    landings: int | None = None
    landings_origin: str = ""
    distance_nm: float | None = None
    distance_origin: str = ""
    evidence_events: tuple[TimelineEvent, ...] = ()
    gaps: tuple[GapView, ...] = ()
    field_checks: tuple[FieldCheck, ...] = ()
    #: The Recorder's arrival assessment while a suspected diversion is unanswered.
    suspected_diversion: Mapping[str, Any] | None = field(default=None, compare=False)
    # Record history
    revisions: tuple[RevisionView, ...] = ()
    provenance: Mapping[str, ProvenanceView] = field(default_factory=dict, compare=False)
    importer: str = ""
    import_reference: str = ""
    migrated_from_schema: int | None = None
    created_at: str = ""
    updated_at: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    search_text: str = field(default="", compare=False, repr=False)

    # Derived, honest properties ----------------------------------------------------

    @property
    def read_only(self) -> bool:
        return bool(self.read_only_reason)

    @property
    def route(self) -> str:
        dep, arr = self.departure or "Unknown", self.arrival or "Unknown"
        return f"{dep} → {arr}"

    @property
    def aircraft_key(self) -> str:
        return aircraft_key(self.aircraft)

    @property
    def source_label(self) -> str:
        label = SOURCE_LABELS.get(self.source, self.source.capitalize())
        return f"{label} (simulated)" if self.simulated else label

    @property
    def flight_time_s(self) -> float | None:
        """Logged flight time: the record's duration field, else measured block time.

        The duration field wins because it carries pilot corrections; for a Recorder
        flight it is normally the Recorder's own block time. PROPOSAL, see docs.
        """
        if self.minutes is not None:
            return float(self.minutes) * 60.0
        if self.block_s is not None:
            return float(self.block_s)
        return None

    @property
    def flight_time_origin(self) -> str:
        if self.minutes is not None:
            prov = self.provenance.get("minutes")
            return prov.origin if prov is not None else "pilot"
        if self.block_s is not None:
            return "recorder"
        return ""

    @property
    def unacknowledged_gaps(self) -> tuple[GapView, ...]:
        return tuple(gap for gap in self.gaps if not gap.acknowledged)

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)

    @property
    def interrupted(self) -> bool:
        return self.recorder_state in INTERRUPTED_RECORDER_STATES

    @property
    def status(self) -> str:
        return record_status(self)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]

    @property
    def needs_attention(self) -> bool:
        return self.status in ATTENTION_STATUSES

    @property
    def payload(self) -> Payload:
        return payload_of(self)

    @property
    def operation_label(self) -> str:
        return operation_label(self.operation)

    @property
    def diverted(self) -> bool:
        """Shown only when the pilot confirmed a diversion (Q9)."""
        return bool(self.diversion) and self.diversion.get("confirmed") is True

    @property
    def corrected_fields(self) -> tuple[str, ...]:
        return tuple(name for name, prov in self.provenance.items() if prov.corrected)

    @property
    def needs_confirmation(self) -> bool:
        """Some Recorder field still waits for the pilot to confirm or correct it."""
        return bool(self.field_checks)

    @property
    def review_label(self) -> str:
        """The record status, led by a "Needs confirmation" marker while fields wait for the pilot."""
        if self.needs_confirmation:
            return f"{NEEDS_CONFIRMATION_LABEL} · {self.status_label}"
        return self.status_label

    @property
    def confirmed_fields(self) -> tuple[str, ...]:
        return tuple(name for name, prov in self.provenance.items() if prov.confirmed)

    @property
    def planned_destination(self) -> str:
        """The planned destination of an unanswered suspected diversion ("" otherwise)."""
        if self.suspected_diversion is None:
            return ""
        return _text(self.suspected_diversion.get("planned_destination"))

    @property
    def sort_date(self) -> str:
        return self.flight_date.isoformat() if self.flight_date else ""


# Helpers ------------------------------------------------------------------------------


def parse_instant(value: Any) -> datetime | None:
    """Parse an ISO 8601 time with offset; None for missing or unreadable values."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def aircraft_key(text: str) -> str:
    """Identity used for aircraft-type grouping and filtering: trimmed, upper case."""
    return " ".join(str(text or "").split()).upper()


def operation_label(text: str) -> str:
    if not text:
        return "Not set"
    return OPERATION_LABELS.get(text.strip().lower(), text.strip())


def format_duration(seconds: float | None, unknown: str = "Unknown") -> str:
    """``1:05`` style hours and minutes, or ``unknown``."""
    if seconds is None or not math.isfinite(seconds):
        return unknown
    minutes = int(round(seconds / 60.0))
    return f"{minutes // 60}:{minutes % 60:02d}"


def format_hours(seconds: float) -> str:
    hours = seconds / 3600.0
    return f"{hours:,.1f} h"


def format_rate(rate: float | None, unknown: str = "Not measured") -> str:
    if rate is None or not math.isfinite(rate):
        return unknown
    return f"{rate:,.0f} fpm"


def format_time(value: str | None, unknown: str = "Not recorded") -> str:
    parsed = parse_instant(value)
    if parsed is None:
        return unknown
    return parsed.strftime("%H:%M")


def format_date(view: FlightView) -> str:
    if view.flight_date is None:
        return view.flight_date_text or "Unknown date"
    return f"{view.flight_date.day} {view.flight_date.strftime('%b %Y')}"


def format_value(value: Any, unit: str = "", unknown: str = "Unknown") -> str:
    if value is None or value == "":
        return unknown
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}{(' ' + unit) if unit else ''}"
    if isinstance(value, float):
        return f"{value:,.1f}{(' ' + unit) if unit else ''}"
    return str(value)


def rate_magnitude(rate: float) -> float:
    """Touchdown rates may be stored as negative (descending) or positive. Compare magnitudes."""
    return abs(rate)


def payload_of(view: FlightView) -> Payload:
    parts = {"baggage": view.baggage_kg, "freight": view.freight_kg}
    known = [value for value in parts.values() if value is not None]
    missing = tuple(name for name, value in parts.items() if value is None)
    return Payload(
        sum(known) if known else None, not missing, missing, view.payload_total_kg,
    )


def record_status(view: FlightView) -> str:
    """Pilot-facing record state. Manual and imported flights never get gap warnings (Q4)."""
    if view.source == "recorder":
        if not view.has_evidence:
            return "evidence_missing"
        if view.unacknowledged_gaps:
            return "gaps"
        if view.interrupted and not view.gaps:
            return "interrupted"
        if view.gaps:
            return "reviewed"
        return "recorded_simulated" if view.simulated else "recorded"
    if view.source == "imported":
        return "import_review" if view.problems else "imported"
    return "manual"


# Adapters -----------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _lookup(name: str, *mappings: Mapping[str, Any] | None) -> Any:
    for mapping in mappings:
        if isinstance(mapping, Mapping) and mapping.get(name) is not None:
            return mapping[name]
    return None


def _first_origin(name: str, *pairs: tuple[Mapping[str, Any] | None, str]) -> str:
    for mapping, origin in pairs:
        if isinstance(mapping, Mapping) and mapping.get(name) is not None:
            return origin
    return ""


def _event_label(kind: str) -> str:
    return {
        "flight_started": "Flight logging started", "phase_changed": "Phase changed",
        "block_off": "Off-block", "takeoff": "Takeoff", "touchdown": "Touchdown",
        "bounce": "Bounce", "block_on": "On-block", "paused": "Simulator paused",
        "resumed": "Simulator resumed", "sim_rate_changed": "Simulation rate changed",
        "gap": "Data gap", "aircraft_changed": "Aircraft changed",
        "position_jump": "Position jump (slew, teleport or reload)", "crashed": "Crash",
        "manual_start": "Flight Logging started by pilot", "manual_stop": "Flight Logging stopped by pilot",
        "flight_ended": "Flight logging ended", "sample": "Track point",
    }.get(kind, kind.replace("_", " ").capitalize() or "Event")


def _event_detail(kind: str, data: Mapping[str, Any]) -> str:
    if not isinstance(data, Mapping):
        return ""
    if kind in ("touchdown", "bounce"):
        rate = _lookup_rate(data)
        return f"Rate {format_rate(rate)}" if rate is not None else "Rate not measured"
    if kind == "phase_changed":
        before, after = _text(data.get("from")), _text(data.get("to"))
        return f"{before} → {after}".strip() if before or after else ""
    if kind == "sim_rate_changed":
        rate = _number(data.get("rate") if "rate" in data else data.get("simulation_rate"))
        return f"{rate:g}×" if rate is not None else ""
    if kind == "gap":
        return _text(data.get("reason"))
    if kind in ("paused", "resumed"):
        return _text(data.get("detail") or data.get("state") or "")
    reason = data.get("reason") or data.get("detail")
    return str(reason) if isinstance(reason, (str, int, float)) else ""


def _lookup_rate(data: Mapping[str, Any]) -> float | None:
    for key in RATE_KEYS:
        value = _number(data.get(key))
        if value is not None:
            return value
    return None


def timeline_event_from_json(payload: Mapping[str, Any]) -> TimelineEvent | None:
    """Build a timeline entry from ``RecorderEvent.to_json()`` (or a journal line wrapping it)."""
    if not isinstance(payload, Mapping):
        return None
    if "kind" not in payload and isinstance(payload.get("event"), Mapping):
        payload = payload["event"]
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    time = payload.get("sim_utc") or payload.get("host_time") or payload.get("t_utc")
    return TimelineEvent(
        kind, _event_label(kind), time if isinstance(time, str) else None,
        _number(payload.get("sim_time_s")), _text(payload.get("phase")), _event_detail(kind, data),
    )


def _revision_parts(item: Mapping[str, Any]) -> tuple[Mapping[str, Any], dict[str, Any]]:
    fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
    extra = {key: value for key, value in fields.items() if key not in FIELD_NAMES}
    return fields, extra


def _field_value(fields: Mapping[str, Any], name: str) -> Any:
    value = fields.get(name)
    if name in NUMBER_FIELDS:
        return _whole(value)
    return _text(value)


def flight_view_from_record(
    payload: Mapping[str, Any], *, read_only_reason: str = "", problems: Iterable[str] = (),
) -> FlightView:
    """Build a ``FlightView`` from a decoded flight file (record format 1 or 2).

    Tolerant: anything optional that is missing or malformed reads as unknown and is
    listed in ``problems``. Raises ``ViewError`` only when the payload is not a flight.
    """
    if not isinstance(payload, Mapping):
        raise ViewError("The data is not a flight record object.")
    revisions_raw = payload.get("revisions")
    if not isinstance(revisions_raw, list) or not revisions_raw or not all(
        isinstance(item, Mapping) for item in revisions_raw
    ):
        raise ViewError("The record has no readable revisions.")
    flight_id = payload.get("id")
    if not isinstance(flight_id, str) or not flight_id:
        raise ViewError("The record has no flight id.")
    schema = payload.get("schema") if type(payload.get("schema")) is int else 1
    source = payload.get("source", "manual" if schema == 1 else "")
    issues = list(problems)
    if source not in SOURCES:
        issues.append(f"Unknown record source {source!r}; shown as imported.")
        source = "imported"
    if schema > 2 and not read_only_reason:
        read_only_reason = (f"This flight was saved by a newer BBC EFB (record format {schema}). "
                            "This version can show it but will not change it.")

    latest_fields, latest_extra = _revision_parts(revisions_raw[-1])
    values = {name: _field_value(latest_fields, name) for name in FIELD_NAMES}
    top_extra = payload.get("extra") if isinstance(payload.get("extra"), Mapping) else {}
    evidence = payload.get("recorder") if isinstance(payload.get("recorder"), Mapping) else None
    if source == "recorder" and evidence is None:
        issues.append("The Recorder evidence block is missing.")

    flight_date = None
    try:
        flight_date = date.fromisoformat(values["flight_date"]) if values["flight_date"] else None
    except ValueError:
        issues.append(f"Flight date {values['flight_date']!r} is not a valid date.")

    # Record history and provenance
    revisions: list[RevisionView] = []
    previous_fields: Mapping[str, Any] | None = None
    for index, item in enumerate(revisions_raw):
        fields, _extra = _revision_parts(item)
        by = item.get("by") if isinstance(item.get("by"), str) else (
            _DEFAULT_AUTHOR.get(source, "pilot") if index == 0 else "pilot")
        changes: list[FieldChange] = []
        if previous_fields is not None:
            for name in FIELD_NAMES:
                before, after = _field_value(previous_fields, name), _field_value(fields, name)
                if before != after:
                    changes.append(FieldChange(name, before, after))
        confirmed_raw = item.get("confirmed")
        confirmed = tuple(dict.fromkeys(
            name for name in confirmed_raw if isinstance(name, str) and name in FIELD_NAMES
        )) if isinstance(confirmed_raw, list) and by == "pilot" else ()
        revisions.append(RevisionView(
            index, _text(item.get("saved_at")), by, _text(item.get("reason")), tuple(changes), confirmed,
        ))
        previous_fields = fields
    first = revisions_raw[0]
    first_fields, _ = _revision_parts(first)
    first_origins = first.get("origins") if isinstance(first.get("origins"), Mapping) else {}
    provenance: dict[str, ProvenanceView] = {}
    for name in FIELD_NAMES:
        origin = first_origins.get(name) or _DEFAULT_ORIGIN.get(revisions[0].by, revisions[0].by)
        previous = _field_value(first_fields, name)
        corrections = []
        confirmations = []
        confirmed = name in revisions[0].confirmed
        for revision, item in zip(revisions[1:], revisions_raw[1:]):
            fields, _ = _revision_parts(item)
            value = _field_value(fields, name)
            if value != previous:
                corrections.append(CorrectionView(revision.saved_at, previous, value, revision.by, revision.reason))
                previous = value
                confirmed = False
            if name in revision.confirmed:
                confirmations.append(revision.saved_at)
                confirmed = True
        provenance[name] = ProvenanceView(
            name, str(origin), _field_value(first_fields, name), previous, tuple(corrections),
            tuple(confirmations), confirmed,
        )

    # Extra values: latest revision fields first, then the top-level extra object.
    operation = _text(_lookup("operation", latest_extra, top_extra))
    freight_description = _text(_lookup("freight_description", latest_extra, top_extra))
    payload_total = _whole(_lookup("payload_total_kg", latest_extra, top_extra))
    diversion = _lookup("diversion", latest_extra, top_extra)
    diversion = MappingProxyType(dict(diversion)) if isinstance(diversion, Mapping) else None

    # Recorder evidence
    gaps: list[GapView] = []
    touchdowns: list[TouchdownView] = []
    evidence_events: list[TimelineEvent] = []
    acks: dict[int, Mapping[str, Any]] = {}
    for ack in payload.get("acknowledgements", []) or []:
        if isinstance(ack, Mapping) and type(ack.get("gap")) is int:
            acks.setdefault(ack["gap"], ack)
    ev = evidence or {}
    if evidence is not None:
        raw_gaps = ev.get("gaps") if isinstance(ev.get("gaps"), list) else []
        for index, gap in enumerate(raw_gaps):
            if not isinstance(gap, Mapping):
                issues.append(f"Recorder gap {index + 1} is malformed.")
                gap = {}
            ack = acks.get(index)
            gaps.append(GapView(
                index, _text(gap.get("reason")) or "Reason not recorded",
                gap.get("start") if isinstance(gap.get("start"), str) else None,
                gap.get("end") if isinstance(gap.get("end"), str) else None,
                _text(gap.get("kind")), ack is not None,
                _text(ack.get("at")) if ack else "", _text(ack.get("by")) if ack else "",
                _text(ack.get("note")) if ack else "",
            ))
        for item in ev.get("touchdowns", []) or []:
            if isinstance(item, Mapping):
                touchdowns.append(TouchdownView(
                    item.get("time") if isinstance(item.get("time"), str) else None,
                    _lookup_rate(item), bool(item.get("bounce")), "recorder",
                ))
        for item in ev.get("events", []) or []:
            event = timeline_event_from_json(item) if isinstance(item, Mapping) else None
            if event is not None and event.kind != "sample":
                evidence_events.append(event)

    rate = _number(ev.get("touchdown_rate_fpm")) if evidence is not None else None
    rate_origin = "recorder" if rate is not None else ""
    if rate is None and source == "imported":
        rate = _number(_lookup("touchdown_rate_fpm", latest_extra, top_extra))
        rate_origin = "imported" if rate is not None else ""

    landings = _whole(ev.get("landings")) if evidence is not None else None
    landings_origin = "recorder" if landings is not None else ""
    if landings is None and touchdowns:
        landings = sum(1 for item in touchdowns if not item.bounce)
        landings_origin = "recorder"
    if landings is None and evidence is not None and isinstance(ev.get("landing"), str):
        landings, landings_origin = 1, "recorder"
    if landings is None:
        pilot = _whole(_lookup("landings", latest_extra, top_extra))
        if pilot is not None:
            landings = pilot
            landings_origin = "imported" if source == "imported" else "pilot"

    distance = _number(ev.get("distance_nm")) if evidence is not None else None
    distance_origin = "recorder" if distance is not None else ""
    if distance is None:
        distance = _number(_lookup("distance_nm", latest_extra, top_extra))
        if distance is not None:
            distance_origin = {"imported": "imported"}.get(source, "pilot")

    importer, reference = "", ""
    if isinstance(payload.get("import"), Mapping):
        importer = _text(payload["import"].get("importer"))
        reference = _text(payload["import"].get("reference"))
    migrated = payload.get("migrated_from")
    migrated_schema = migrated.get("schema") if isinstance(migrated, Mapping) else None

    search_parts = [
        values["flight_date"], values["aircraft"], values["departure"], values["arrival"],
        values["callsign"], values["registration"], operation, SOURCE_LABELS.get(source, ""),
        values["notes"][:_SEARCH_NOTES_LIMIT],
    ]
    if flight_date is not None:
        search_parts.append(f"{flight_date.day} {flight_date.strftime('%b %B %Y')}")
    search_text = " ".join(part for part in search_parts if part).casefold()

    suspected = suspected_diversion_of(ev, diversion) if evidence is not None else None
    if suspected is not None and _resolved_by_pilot(provenance.get("arrival")):
        suspected = None
    suspected = MappingProxyType(dict(suspected)) if suspected is not None else None

    extra_all = dict(top_extra)
    extra_all.update(latest_extra)
    return FlightView(
        id=flight_id.lower(), source=source, flight_date=flight_date,
        flight_date_text=values["flight_date"], aircraft=values["aircraft"],
        departure=values["departure"], arrival=values["arrival"], callsign=values["callsign"],
        registration=values["registration"], notes=values["notes"], minutes=values["minutes"],
        crew=values["crew"], passengers=values["passengers"], baggage_kg=values["baggage_kg"],
        freight_kg=values["freight_kg"], schema=schema,
        simulated=bool(evidence is not None and ev.get("simulated") is True),
        read_only_reason=read_only_reason, problems=tuple(dict.fromkeys(issues)),
        operation=operation, freight_description=freight_description,
        payload_total_kg=payload_total, diversion=diversion,
        has_evidence=evidence is not None,
        recorder_state=_text(ev.get("status") or ev.get("state")).lower(),
        time_basis=_text(ev.get("time_basis")), journal=_text(ev.get("journal")),
        summary=_text(ev.get("summary")), rules_version=_text(ev.get("rules_version")),
        off_block=ev.get("off_block") if isinstance(ev.get("off_block"), str) else None,
        takeoff=ev.get("takeoff") if isinstance(ev.get("takeoff"), str) else None,
        landing=ev.get("landing") if isinstance(ev.get("landing"), str) else None,
        on_block=ev.get("on_block") if isinstance(ev.get("on_block"), str) else None,
        block_s=_number(ev.get("block_s")), airborne_s=_number(ev.get("airborne_s")),
        touchdown_rate_fpm=rate, touchdown_rate_origin=rate_origin,
        touchdowns=tuple(touchdowns), landings=landings, landings_origin=landings_origin,
        distance_nm=distance, distance_origin=distance_origin,
        evidence_events=tuple(evidence_events), gaps=tuple(gaps),
        field_checks=_field_checks(ev, provenance, diversion) if evidence is not None else (),
        suspected_diversion=suspected,
        revisions=tuple(revisions), provenance=MappingProxyType(provenance),
        importer=importer, import_reference=reference,
        migrated_from_schema=migrated_schema if type(migrated_schema) is int else None,
        created_at=revisions[0].saved_at, updated_at=revisions[-1].saved_at,
        extra=MappingProxyType(extra_all), search_text=search_text,
    )


def _record_from_flight_object(flight: Any) -> dict[str, Any]:
    """Rebuild the stored JSON shape from a storage-like ``Flight`` object (duck typed)."""
    revisions = []
    for revision in getattr(flight, "revisions", ()):
        fields_obj = revision.fields
        if isinstance(fields_obj, Mapping):
            fields = dict(fields_obj)
        elif hasattr(fields_obj, "as_json"):
            fields = dict(fields_obj.as_json())
        else:
            fields = {name: getattr(fields_obj, name, None) for name in FIELD_NAMES}
        fields.update(dict(getattr(revision, "extra", {}) or {}))
        revisions.append({
            "saved_at": getattr(revision, "saved_at", ""), "fields": fields,
            "by": getattr(revision, "by", "pilot"), "reason": getattr(revision, "reason", ""),
            "origins": dict(getattr(revision, "origins", {}) or {}),
        })
    record: dict[str, Any] = {
        "schema": getattr(flight, "schema", 2), "id": flight.id,
        "source": getattr(flight, "source", "manual"), "revisions": revisions,
    }
    evidence = getattr(flight, "evidence", None)
    if evidence is not None:
        record["recorder"] = evidence.as_json() if hasattr(evidence, "as_json") else dict(evidence)
    details = getattr(flight, "import_details", None)
    if details is not None:
        record["import"] = {"importer": getattr(details, "importer", ""), "reference": getattr(details, "reference", "")}
    record["acknowledgements"] = [
        {"gap": ack.gap, "at": ack.at, "by": ack.by, "note": ack.note}
        for ack in getattr(flight, "acknowledgements", ())
    ]
    if getattr(flight, "migrated_from_schema", None) is not None:
        record["migrated_from"] = {"schema": flight.migrated_from_schema}
    record["extra"] = dict(getattr(flight, "extra", {}) or {})
    return record


def flight_view(item: Any) -> FlightView:
    """Accept a plain record dict, an existing ``FlightView`` or a storage-like ``Flight``."""
    if isinstance(item, FlightView):
        return item
    if isinstance(item, Mapping):
        return flight_view_from_record(item)
    read_only_reason = _text(getattr(item, "read_only_reason", ""))
    problems = tuple(getattr(item, "problems", ()) or ())
    raw = getattr(item, "raw", None)
    record = raw if isinstance(raw, Mapping) else _record_from_flight_object(item)
    return flight_view_from_record(record, read_only_reason=read_only_reason, problems=problems)


def flight_views(items: Iterable[Any]) -> tuple[list[FlightView], list[tuple[Any, str]]]:
    """Convert many records. Returns ``(views, failures)``; failures carry a plain reason."""
    views: list[FlightView] = []
    failures: list[tuple[Any, str]] = []
    for item in items:
        try:
            views.append(flight_view(item))
        except (ViewError, AttributeError, TypeError, KeyError) as error:
            failures.append((item, str(error) or type(error).__name__))
    return views, failures


# Journal (Black Box) ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalContents:
    """What a journal file could tell us. ``problem`` explains anything missing."""

    events: tuple[TimelineEvent, ...] = ()
    touchdowns: tuple[TouchdownView, ...] = ()
    profile: tuple[ProfilePoint, ...] = ()
    torn_lines: int = 0
    problem: str = ""
    profile_origin_sim_s: float | None = None     # profile seconds are measured from here
    profile_origin_time: str | None = None        # ... or from this time when sim time is absent


def resolve_journal(data_root: str | os.PathLike[str], relative: str) -> Path | None:
    """Return the journal path inside ``data_root``, or None if the path is unsafe."""
    if not relative:
        return None
    for flavour in (PurePosixPath, PureWindowsPath):
        path = flavour(relative)
        if path.is_absolute() or path.anchor or ".." in path.parts:
            return None
    root = Path(data_root).resolve()
    candidate = (root / PurePosixPath(relative.replace("\\", "/"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _iter_journal_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        yield from handle


def load_journal(
    data_root: str | os.PathLike[str], relative: str, *, max_samples: int = MAX_PROFILE_SAMPLES,
) -> JournalContents:
    """Read a Recorder JSONL journal for the Black Box tab. Never raises for bad content.

    Each line is ``RecorderEvent.to_json()`` (optionally wrapped as ``{"seq": .., "event": {...}}``).
    A torn or unreadable line is counted, not fatal. Track points (``sample``) are thinned
    to at most ``max_samples`` altitude points for the side-on profile.
    """
    path = resolve_journal(data_root, relative)
    if path is None:
        return JournalContents(problem="The journal path is not inside the data folder.")
    try:
        size = path.stat().st_size
    except OSError:
        return JournalContents(problem="The journal file is not available. The saved summary is shown instead.")
    if size > MAX_JOURNAL_BYTES:
        return JournalContents(problem="The journal is too large to show here. The saved summary is shown instead.")
    events: list[TimelineEvent] = []
    touchdowns: list[TouchdownView] = []
    samples: list[tuple[float, float]] = []
    torn = 0
    start_time: datetime | None = None
    start_sim: float | None = None
    try:
        for line in _iter_journal_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (ValueError, RecursionError):  # a deeply nested line is torn, not fatal
                torn += 1
                continue
            if isinstance(item, Mapping) and "kind" not in item and isinstance(item.get("event"), Mapping):
                item = item["event"]
            if not isinstance(item, Mapping) or not isinstance(item.get("kind"), str):
                torn += 1
                continue
            kind = item["kind"]
            data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
            if kind == "sample":
                altitude = None
                for key in ALTITUDE_KEYS:
                    altitude = _number(data.get(key))
                    if altitude is not None:
                        break
                if altitude is None:
                    continue
                sim_time = _number(item.get("sim_time_s"))
                when = parse_instant(item.get("sim_utc") or item.get("host_time"))
                if sim_time is not None:
                    start_sim = sim_time if start_sim is None else start_sim
                    seconds = sim_time - start_sim
                elif when is not None:
                    start_time = when if start_time is None else start_time
                    seconds = (when - start_time).total_seconds()
                else:
                    continue
                samples.append((seconds, altitude))
                continue
            event = timeline_event_from_json(item)
            if event is not None:
                events.append(event)
            if kind in ("touchdown", "bounce"):
                touchdowns.append(TouchdownView(event.time if event else None, _lookup_rate(data), kind == "bounce"))
    except OSError as error:
        return JournalContents(tuple(events), tuple(touchdowns), (), torn,
                               f"The journal could not be read completely ({error.strerror or error}).")
    profile = tuple(ProfilePoint(s, a) for s, a in _thin(samples, max_samples))
    return JournalContents(
        tuple(events), tuple(touchdowns), profile, torn, "", start_sim,
        start_time.isoformat() if start_time is not None else None,
    )


def event_offset_s(event: TimelineEvent, contents: JournalContents) -> float | None:
    """Seconds from the profile origin to ``event`` (None when the clocks cannot be related)."""
    if contents.profile_origin_sim_s is not None and event.sim_time_s is not None:
        return event.sim_time_s - contents.profile_origin_sim_s
    origin, when = parse_instant(contents.profile_origin_time), parse_instant(event.time)
    if origin is not None and when is not None and contents.profile_origin_sim_s is None:
        return (when - origin).total_seconds()
    return None


def _thin(samples: list[tuple[float, float]], limit: int) -> list[tuple[float, float]]:
    """Keep the shape of a long profile in at most ``limit`` points (min/max per bucket)."""
    if limit <= 0 or len(samples) <= limit:
        return samples
    bucket = math.ceil(len(samples) / max(1, limit // 2))
    result: list[tuple[float, float]] = []
    for start in range(0, len(samples), bucket):
        chunk = samples[start:start + bucket]
        low = min(chunk, key=lambda item: item[1])
        high = max(chunk, key=lambda item: item[1])
        result.extend(sorted({low, high}, key=lambda item: item[0]))
    return result


def evidence_timeline(view: FlightView) -> tuple[TimelineEvent, ...]:
    """Timeline from the saved evidence block alone (used when no journal is available)."""
    if view.evidence_events:
        return view.evidence_events
    items = [
        TimelineEvent("block_off", "Off-block", view.off_block),
        TimelineEvent("takeoff", "Takeoff", view.takeoff),
        TimelineEvent("touchdown", "Touchdown", view.landing,
                      detail=f"Rate {format_rate(view.touchdown_rate_fpm)}" if view.landing else ""),
        TimelineEvent("block_on", "On-block", view.on_block),
    ]
    for gap in view.gaps:
        items.append(TimelineEvent("gap", "Data gap", gap.start, detail=gap.reason))
    known = [item for item in items if item.time is not None]
    known.sort(key=lambda item: parse_instant(item.time) or datetime.max.replace(tzinfo=timezone.utc))
    return tuple(known)
