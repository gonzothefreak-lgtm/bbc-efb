"""Flight attribution helpers for the Flight Recorder: airports, aircraft identity and payload.

This module turns raw Core evidence (SimVar readings and facility query results) into
labelled *candidate* values for a flight record. It is Qt-free and SimConnect-free: it reads
plain values and duck-typed Core results, and only imports ``bbc_core.sdk_simvar`` lazily
when a caller asks for the SimVar request tuples.

Ground rules, taken from the Data Requirements Register (identity, airports, payload) and
sources/04 (Q9, Q10):

* Nothing here is ever "confirmed". Every value is a simulator-derived candidate with a
  status, a confidence and a plain-English reason. Only the pilot confirms values, in the
  Logbook.
* Unknown stays unknown. A missing, empty or undecodable reading never becomes a guess.
* Every value carries provenance: the exact SimConnect field names it came from, the raw
  values as received, the host receipt time, the simulator clocks when known, the Core
  session and whether the source was simulated.
* TITLE, ATC TYPE, ATC MODEL, ATC ID, ATC AIRLINE and ATC FLIGHT NUMBER do not guarantee a
  canonical type, registration or network callsign. Livery titles never change the type
  grouping key; only a type designator does.
* A position is not an airport. Airport matching uses the simulator's nearby-airport list,
  a distance threshold, an ambiguity margin and (optionally) runway geometry, and reports
  "unknown" when nothing is close enough.
* A diversion is never auto-confirmed. An arrival that differs from a planned destination is
  ``diversion_suspected`` and needs pilot confirmation (``record_pilot_diversion_decision``).
* Payload stations are not crew, passengers, baggage or freight. Those categories stay
  unknown unless the pilot (or a trusted import) supplies them. Station weights are reported
  as station weights, with incomplete sums labelled as incomplete.

PROPOSALS FOR THE OWNER (defaults chosen here; all configurable, none approved yet)
----------------------------------------------------------------------------------
P-ATTR-1  Airport match radius. ``AirportMatchPolicy.max_distance_m = 6000``: an airport
          whose reference point (or nearest runway surface, when runway details are given)
          lies further than 6 km from the aircraft is not attributed. Within 1.5 km
          (``high_confidence_m``) or on a runway surface the match is "high" confidence,
          otherwise "medium" up to 3.75 km and "low" beyond. Large airports can extend
          beyond 4 km from their reference point; supply runway details to avoid false
          "unknown" results at such airports.
P-ATTR-2  Ambiguity. When the second-nearest airport is within 750 m
          (``ambiguity_margin_m``) of the nearest, the result is ``ambiguous``: the nearest
          is kept as the best guess, every candidate is listed, and the pilot must choose.
P-ATTR-3  Ground state. By default (``require_on_ground=True``) departure/arrival
          attribution requires SIM ON GROUND to be true. When the ground state is unknown
          the match is still made but confidence drops one step.
P-ATTR-4  Freshness. A nearby-airport list received more than 120 s away from the
          position sample (``max_result_age_s``) still matches, with confidence one step
          lower and a note, because the simulator's local cache may have moved. A list from
          a different Core session is never used.
P-ATTR-5  Arrival versus plan. Arrival equals planned destination -> ``matches_plan``.
          Arrival differs -> ``diversion_suspected`` (also when it equals a planned
          alternate or the departure airport, which are noted). When the arrival is
          ambiguous and the planned destination is one of the candidates, or the planned
          destination is within the match radius and no more than the ambiguity margin
          further away than the nearest airport, the plan resolves it (``resolved_by_plan``).
          Unknown arrival is ``arrival_unknown`` and is never called a diversion.
P-ATTR-6  Aircraft type. ATC MODEL, after unwrapping MSFS localisation keys such as
          ``TT:ATCCOM.AC_MODEL A320.0.text``, is accepted as a type designator when it is
          shaped like an ICAO Doc 8643 designator (a letter then one to three letters or
          digits; ``ZZZZ`` means none). Confidence is "medium" because add-ons may set any
          string. Otherwise the type is unknown and the display falls back to the TITLE with
          "low" confidence and a request for pilot confirmation. Owners/pilots can supply
          ``IdentityPolicy.title_aliases`` (exact TITLE -> designator) as standing
          corrections.
P-ATTR-7  Callsign. Built only from ATC AIRLINE + ATC FLIGHT NUMBER. A three-letter
          upper-case airline value is treated as an ICAO operator code and compacted
          (``BAW123``); anything else is joined as a spoken callsign (``Speedbird 123``). It
          is labelled as the simulator ATC callsign, never a VATSIM/network callsign. Using
          the registration as a callsign for general aviation is off by default
          (``registration_as_callsign=False``).
P-ATTR-8  Payload. Only PAYLOAD STATION COUNT / NAME:i / WEIGHT:i are read (pounds, the SDK
          unit), stations 1 to 15. The station sum is ``known`` only when every station
          weight was reported; otherwise it is ``incomplete`` with the partial sum. Crew,
          passengers, baggage and freight are always ``unknown`` from SimConnect.

Threading: ``NearbyAirportLookup`` and ``resolve_airport`` call ConsumerAPI methods. Call them
on whatever thread the host allows ConsumerAPI use (the Core host thread in the planned
design). They never call ``poll()`` themselves, so they do not steal updates from the
Recorder; feed facility results with ``offer()`` or let them pull ``facility_result(id)``.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

RULES_VERSION = "attribution-2026-11-24.1"

LB_TO_KG = 0.45359237
EARTH_RADIUS_M = 6_371_008.8  # IUGG mean Earth radius

# Exact MSFS 2020 SDK names used by this module (Data Requirements Register I01-I06, F01, F02).
TITLE = "TITLE"
ATC_TYPE = "ATC TYPE"
ATC_MODEL = "ATC MODEL"
ATC_ID = "ATC ID"
ATC_AIRLINE = "ATC AIRLINE"
ATC_FLIGHT_NUMBER = "ATC FLIGHT NUMBER"
PLANE_LATITUDE = "PLANE LATITUDE"
PLANE_LONGITUDE = "PLANE LONGITUDE"
SIM_ON_GROUND = "SIM ON GROUND"
PAYLOAD_STATION_COUNT = "PAYLOAD STATION COUNT"
PAYLOAD_STATION_NAME = "PAYLOAD STATION NAME"
PAYLOAD_STATION_WEIGHT = "PAYLOAD STATION WEIGHT"
MAX_PAYLOAD_STATIONS = 15  # SDK notes stations 1-15

IDENTITY_FIELDS = (TITLE, ATC_TYPE, ATC_MODEL, ATC_ID, ATC_AIRLINE, ATC_FLIGHT_NUMBER)
POSITION_FIELDS = (PLANE_LATITUDE, PLANE_LONGITUDE, SIM_ON_GROUND)


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value

    __format__ = str.__format__


class Origin(_ValueEnum):
    OBSERVED = "observed"        # value as the simulator reported it (whitespace trimmed only)
    DERIVED = "derived"          # normalised, combined or matched from observations
    UNAVAILABLE = "unavailable"  # no usable evidence


class Status(_ValueEnum):
    KNOWN = "known"                            # a candidate value with usable evidence
    UNKNOWN = "unknown"                        # no usable evidence; value is None
    UNSUPPORTED = "unsupported"                # the source cannot provide this field
    AMBIGUOUS = "ambiguous"                    # several candidates fit; pilot must choose
    INCOMPLETE = "incomplete"                  # partial value (for example a partial sum)
    NEEDS_CONFIRMATION = "needs_confirmation"  # a weak fallback the pilot should check


class Confidence(_ValueEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


_CONFIDENCE_ORDER = (Confidence.NONE, Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)


def lower_confidence(confidence: Confidence, steps: int = 1) -> Confidence:
    """Return the confidence ``steps`` levels lower, never below LOW for a present value."""
    if confidence is Confidence.NONE:
        return Confidence.NONE
    index = max(1, _CONFIDENCE_ORDER.index(confidence) - steps)
    return _CONFIDENCE_ORDER[index]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return repr(value)


# --------------------------------------------------------------------------------------------
# Evidence and provenance
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FieldReading:
    """One SimConnect field value as Core delivered it.

    ``name`` is the SDK name including any index (``PAYLOAD STATION WEIGHT:2``). ``value`` is
    the decoded value or None when the provider could not decode it. ``observed_at`` is the
    host receipt time; ``sim_time_s`` / ``sim_utc`` are the simulator clocks when known.
    """

    name: str
    value: Any
    observed_at: datetime | None = None
    session_id: str | None = None
    source_name: str | None = None
    simulated: bool | None = None
    sim_time_s: float | None = None
    sim_utc: datetime | None = None


Readings = Mapping[str, FieldReading]


def readings_from_snapshot(snapshot: Any) -> dict[str, FieldReading]:
    """Turn a Core ``Snapshot`` (duck-typed) into readings keyed by upper-case SDK name."""
    names = tuple(getattr(snapshot, "names", ()) or ())
    values = tuple(getattr(snapshot, "values", ()) or ())
    source = getattr(snapshot, "source", None)
    common = dict(
        observed_at=getattr(snapshot, "received_at", None),
        session_id=getattr(snapshot, "session_id", None),
        source_name=getattr(source, "name", None),
        simulated=getattr(source, "simulated", None),
        sim_time_s=getattr(snapshot, "simulation_time_s", None),
        sim_utc=getattr(snapshot, "sample_time", None),
    )
    readings: dict[str, FieldReading] = {}
    for index, name in enumerate(names):
        value = values[index] if index < len(values) else None
        key = str(name).strip().upper()
        readings[key] = FieldReading(key, value, **common)
    return readings


def readings_from_values(
    values: Mapping[str, Any], *, observed_at: datetime | None = None, session_id: str | None = None,
    source_name: str | None = None, simulated: bool | None = None, sim_time_s: float | None = None,
    sim_utc: datetime | None = None,
) -> dict[str, FieldReading]:
    """Build readings from a plain mapping (useful for tests, tapes and imports)."""
    return {
        str(name).strip().upper(): FieldReading(
            str(name).strip().upper(), value, observed_at, session_id, source_name, simulated, sim_time_s, sim_utc,
        )
        for name, value in values.items()
    }


def merge_readings(*groups: Readings) -> dict[str, FieldReading]:
    """Merge several reading groups; a later group wins only with a newer or equal receipt time."""
    merged: dict[str, FieldReading] = {}
    for group in groups:
        for key, reading in group.items():
            current = merged.get(key)
            if (current is None or current.observed_at is None or reading.observed_at is None
                    or reading.observed_at >= current.observed_at):
                merged[key] = reading
    return merged


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where an attributed value came from."""

    origin: Origin
    method: str                              # e.g. "simvar", "atc_model.designator", "facility.nearby_airports"
    fields: tuple[str, ...] = ()             # exact SimConnect field names used
    observed_at: datetime | None = None      # newest host receipt time of the evidence
    session_id: str | None = None
    source_name: str | None = None
    simulated: bool | None = None
    sim_time_s: float | None = None
    sim_utc: datetime | None = None
    raw: tuple[tuple[str, Any], ...] = ()    # raw values exactly as received
    request_id: str | None = None            # facility request id, when used
    facility_received_at: datetime | None = None
    rules_version: str = RULES_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "origin": self.origin.value,
            "method": self.method,
            "fields": list(self.fields),
            "observed_at": _iso(self.observed_at),
            "session_id": self.session_id,
            "source_name": self.source_name,
            "simulated": self.simulated,
            "sim_time_s": self.sim_time_s,
            "sim_utc": _iso(self.sim_utc),
            "raw": {name: _jsonable(value) for name, value in self.raw},
            "request_id": self.request_id,
            "facility_received_at": _iso(self.facility_received_at),
            "rules_version": self.rules_version,
        }


def _provenance(origin: Origin, method: str, readings: Sequence[FieldReading], **extra: Any) -> Provenance:
    present = [reading for reading in readings if reading is not None]
    timed = [reading for reading in present if reading.observed_at is not None]
    newest = max(timed, key=lambda r: r.observed_at) if timed else (present[0] if present else None)
    simulated_flags = {reading.simulated for reading in present if reading.simulated is not None}
    return Provenance(
        origin=origin,
        method=method,
        fields=tuple(reading.name for reading in present),
        observed_at=newest.observed_at if newest else None,
        session_id=newest.session_id if newest else None,
        source_name=newest.source_name if newest else None,
        # Any simulated evidence makes the whole value simulated.
        simulated=(True in simulated_flags) if simulated_flags else None,
        sim_time_s=newest.sim_time_s if newest else None,
        sim_utc=newest.sim_utc if newest else None,
        raw=tuple((reading.name, reading.value) for reading in present),
        **extra,
    )


@dataclass(frozen=True, slots=True)
class Attributed:
    """A candidate value with its status, confidence, reason and provenance."""

    value: Any
    status: Status
    confidence: Confidence
    reason: str
    provenance: Provenance | None = None

    @property
    def known(self) -> bool:
        return self.value is not None and self.status in (Status.KNOWN, Status.NEEDS_CONFIRMATION, Status.AMBIGUOUS)

    @property
    def needs_pilot(self) -> bool:
        return self.status in (Status.AMBIGUOUS, Status.NEEDS_CONFIRMATION, Status.INCOMPLETE)

    def to_json(self) -> dict[str, Any]:
        return {
            "value": _jsonable(self.value),
            "status": self.status.value,
            "confidence": self.confidence.value,
            "reason": self.reason,
            "provenance": self.provenance.to_json() if self.provenance else None,
        }

    def to_record_field(self) -> dict[str, Any]:
        """Shape for a schema-v2 style field: value, provenance kind, evidence and original."""
        return {
            "value": _jsonable(self.value),
            "provenance": (self.provenance.origin.value if self.provenance else Origin.UNAVAILABLE.value),
            "original": _jsonable(self.value),
            "status": self.status.value,
            "confidence": self.confidence.value,
            "reason": self.reason,
            "evidence": self.provenance.to_json() if self.provenance else None,
        }


def unknown(reason: str, provenance: Provenance | None = None, *, status: Status = Status.UNKNOWN) -> Attributed:
    return Attributed(None, status, Confidence.NONE, reason, provenance)


# --------------------------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")
# MSFS ATC strings are often localisation keys, for example
# "TT:ATCCOM.AC_MODEL A320.0.text", "TT:ATCCOM.AC_MODEL_B738.0.text" or "TT:ATCCOM.ATC_NAME CESSNA.0.text".
_LOCALISATION_KEY = re.compile(r"^(?:TT:)?ATCCOM\.(AC_MODEL|ATC_NAME)[ _](.+?)\.\d+\.TEXT$", re.IGNORECASE)
_DESIGNATOR = re.compile(r"^[A-Z][A-Z0-9]{1,3}$")
_NO_DESIGNATOR = frozenset({"ZZZZ"})
_REGISTRATION = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_FLIGHT_NUMBER = re.compile(r"^[A-Z0-9]{1,7}$")
_ICAO_OPERATOR = re.compile(r"^[A-Z]{3}$")
_AIRPORT_IDENT = re.compile(r"^[A-Z0-9]{1,7}$")


def clean_text(value: Any) -> str | None:
    """Trim a simulator string: cut at the first NUL, drop control characters, collapse spaces.

    Returns None for non-strings and for strings that are empty after trimming.
    """
    if isinstance(value, bytes):
        try:
            value = value.split(b"\x00", 1)[0].decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    value = value.split("\x00", 1)[0]
    value = _CONTROL.sub(" ", value)
    value = _SPACES.sub(" ", value).strip()
    return value or None


def unwrap_localisation_key(value: str) -> tuple[str, str | None]:
    """Return (payload, key kind) for an MSFS ATCCOM localisation key, or (value, None)."""
    match = _LOCALISATION_KEY.match(value)
    if not match:
        return value, None
    payload = match.group(2).replace("_", " ").strip()
    return payload, match.group(1).upper()


def type_designator(value: Any) -> str | None:
    """Return an ICAO-shaped type designator from an ATC MODEL value, or None."""
    text = clean_text(value)
    if text is None:
        return None
    payload, _ = unwrap_localisation_key(text)
    candidate = payload.replace(" ", "").upper()
    if _DESIGNATOR.match(candidate) and candidate not in _NO_DESIGNATOR:
        return candidate
    return None


def normalise_airport_ident(value: Any) -> str | None:
    """Upper-case simulator airport identifier (one to seven letters or digits), or None."""
    text = clean_text(value)
    if text is None:
        return None
    text = text.upper()
    return text if _AIRPORT_IDENT.match(text) else None


def _reading(readings: Readings, name: str) -> FieldReading | None:
    reading = readings.get(name)
    if reading is None:
        reading = readings.get(name.upper())
    return reading


# --------------------------------------------------------------------------------------------
# Aircraft identity
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class IdentityPolicy:
    title_aliases: tuple[tuple[str, str], ...] | Mapping[str, str] = ()
    compact_icao_callsign: bool = True
    registration_as_callsign: bool = False

    def __post_init__(self):
        aliases = self.title_aliases
        items = aliases.items() if isinstance(aliases, Mapping) else aliases
        normalised = []
        for title, designator in items:
            key = clean_text(title)
            code = type_designator(designator)
            if key is None or code is None:
                raise ValueError("Each title alias needs a non-empty TITLE and an ICAO-shaped type designator.")
            normalised.append((key.casefold(), code))
        object.__setattr__(self, "title_aliases", tuple(sorted(dict(normalised).items())))

    def alias_for(self, title: str | None) -> str | None:
        if title is None:
            return None
        return dict(self.title_aliases).get(title.casefold())


@dataclass(frozen=True, slots=True)
class AircraftTypeAttribution:
    designator: Attributed     # ICAO-shaped type designator; the type grouping key
    manufacturer: Attributed   # from ATC TYPE
    title: Attributed          # TITLE (may encode a livery)
    display: Attributed        # what to show the pilot

    @property
    def group_key(self) -> str | None:
        """Type grouping key. Never derived from a livery title."""
        return self.designator.value if self.designator.known else None

    def to_json(self) -> dict[str, Any]:
        return {
            "designator": self.designator.to_json(),
            "manufacturer": self.manufacturer.to_json(),
            "title": self.title.to_json(),
            "display": self.display.to_json(),
            "group_key": self.group_key,
        }


def _observed_text(readings: Readings, name: str, label: str) -> tuple[Attributed, FieldReading | None]:
    reading = _reading(readings, name)
    if reading is None:
        return unknown(f"{label} was not requested or not received from the simulator."), None
    text = clean_text(reading.value)
    if text is None:
        return unknown(f"The simulator reported no {label}.", _provenance(Origin.UNAVAILABLE, "simvar", [reading])), reading
    origin = Origin.OBSERVED if text == reading.value else Origin.DERIVED
    return Attributed(text, Status.KNOWN, Confidence.MEDIUM, f"{label} as reported by the simulator.",
                      _provenance(origin, "simvar", [reading])), reading


def aircraft_type(readings: Readings, policy: IdentityPolicy | None = None) -> AircraftTypeAttribution:
    """Attribute aircraft type from ATC MODEL, ATC TYPE and TITLE with an honest fallback."""
    policy = policy or IdentityPolicy()
    title, title_reading = _observed_text(readings, TITLE, "aircraft title")
    if title.known:
        title = replace(title, reason="Aircraft title as reported by the simulator; it may name a livery.")

    model_reading = _reading(readings, ATC_MODEL)
    model_text = clean_text(model_reading.value) if model_reading else None
    designator_value = type_designator(model_text)
    if designator_value is not None:
        _, key_kind = unwrap_localisation_key(model_text)
        origin = Origin.OBSERVED if (key_kind is None and designator_value == model_reading.value) else Origin.DERIVED
        method = "atc_model.localisation_key" if key_kind else "atc_model.designator"
        designator = Attributed(
            designator_value, Status.KNOWN, Confidence.MEDIUM,
            "Type designator from the simulator's ATC MODEL. Add-ons can set any value; the pilot can correct it.",
            _provenance(origin, method, [model_reading]),
        )
    else:
        alias = policy.alias_for(title.value if title.known else None)
        if alias is not None:
            designator = Attributed(
                alias, Status.KNOWN, Confidence.MEDIUM,
                "Type designator from a configured correction for this aircraft title.",
                _provenance(Origin.DERIVED, "configured_title_alias", [title_reading, model_reading]),
            )
        elif model_reading is None:
            designator = unknown("ATC MODEL was not requested or not received, so the aircraft type is unknown.")
        elif model_text is None:
            designator = unknown("The simulator reported an empty ATC MODEL, so the aircraft type is unknown.",
                                 _provenance(Origin.UNAVAILABLE, "atc_model.designator", [model_reading]))
        else:
            designator = unknown(
                f"ATC MODEL '{model_text}' is not a recognisable type designator, so the aircraft type is unknown.",
                _provenance(Origin.UNAVAILABLE, "atc_model.designator", [model_reading]),
            )

    type_reading = _reading(readings, ATC_TYPE)
    type_text = clean_text(type_reading.value) if type_reading else None
    if type_text is None:
        manufacturer = unknown(
            "ATC TYPE was not received." if type_reading is None else "The simulator reported an empty ATC TYPE.",
            _provenance(Origin.UNAVAILABLE, "atc_type", [type_reading]) if type_reading else None,
        )
    else:
        payload, key_kind = unwrap_localisation_key(type_text)
        name = payload.title() if payload.isupper() and len(payload) > 3 else payload
        origin = Origin.OBSERVED if (name == type_reading.value) else Origin.DERIVED
        manufacturer = Attributed(
            name, Status.KNOWN, Confidence.LOW,
            "Simulator ATC TYPE; usually the manufacturer or ATC type name, not an ICAO type.",
            _provenance(origin, "atc_type.localisation_key" if key_kind else "atc_type", [type_reading]),
        )

    if designator.known:
        text = designator.value
        if manufacturer.known and manufacturer.value.casefold() not in text.casefold():
            text = f"{manufacturer.value} {designator.value}"
        display = Attributed(
            text, Status.KNOWN, designator.confidence, designator.reason,
            _provenance(Origin.DERIVED, "display.designator",
                        [r for r in (model_reading, type_reading, title_reading) if r is not None]),
        )
    elif title.known:
        display = Attributed(
            title.value, Status.NEEDS_CONFIRMATION, Confidence.LOW,
            "The aircraft type is unknown; showing the aircraft title instead. Please confirm the type.",
            _provenance(Origin.DERIVED, "display.title_fallback", [title_reading]),
        )
    elif manufacturer.known:
        display = Attributed(
            manufacturer.value, Status.NEEDS_CONFIRMATION, Confidence.LOW,
            "The aircraft type is unknown; showing the simulator ATC TYPE instead. Please confirm the type.",
            _provenance(Origin.DERIVED, "display.atc_type_fallback", [type_reading]),
        )
    else:
        display = unknown("The simulator gave no usable aircraft type, model or title. Please enter the type.")
    return AircraftTypeAttribution(designator, manufacturer, title, display)


def registration(readings: Readings) -> Attributed:
    """Candidate registration from ATC ID (not always authoritative)."""
    reading = _reading(readings, ATC_ID)
    if reading is None:
        return unknown("ATC ID was not requested or not received, so the registration is unknown.")
    text = clean_text(reading.value)
    if text is None:
        return unknown("The simulator reported an empty ATC ID, so the registration is unknown.",
                       _provenance(Origin.UNAVAILABLE, "atc_id", [reading]))
    candidate = text.upper().replace(" ", "")
    origin = Origin.OBSERVED if candidate == reading.value else Origin.DERIVED
    if 2 <= len(candidate) <= 10 and _REGISTRATION.match(candidate):
        return Attributed(candidate, Status.KNOWN, Confidence.MEDIUM,
                          "Registration from the simulator's ATC ID; the pilot can correct it.",
                          _provenance(origin, "atc_id", [reading]))
    return Attributed(text, Status.NEEDS_CONFIRMATION, Confidence.LOW,
                      f"ATC ID '{text}' does not look like a registration. Please confirm it.",
                      _provenance(Origin.OBSERVED if text == reading.value else Origin.DERIVED, "atc_id", [reading]))


@dataclass(frozen=True, slots=True)
class CallsignAttribution:
    callsign: Attributed
    airline: Attributed
    flight_number: Attributed

    def to_json(self) -> dict[str, Any]:
        return {"callsign": self.callsign.to_json(), "airline": self.airline.to_json(),
                "flight_number": self.flight_number.to_json()}


def callsign(readings: Readings, policy: IdentityPolicy | None = None,
             registration_value: Attributed | None = None) -> CallsignAttribution:
    """Simulator ATC callsign from ATC AIRLINE + ATC FLIGHT NUMBER, when both are present."""
    policy = policy or IdentityPolicy()
    airline, airline_reading = _observed_text(readings, ATC_AIRLINE, "ATC airline")
    number_reading = _reading(readings, ATC_FLIGHT_NUMBER)
    number_text = clean_text(number_reading.value) if number_reading else None
    if number_reading is None:
        number = unknown("ATC FLIGHT NUMBER was not requested or not received.")
    elif number_text is None:
        number = unknown("The simulator reported no ATC flight number.",
                         _provenance(Origin.UNAVAILABLE, "atc_flight_number", [number_reading]))
    else:
        compact = number_text.upper().replace(" ", "")
        if _FLIGHT_NUMBER.match(compact):
            number = Attributed(compact, Status.KNOWN, Confidence.MEDIUM, "Simulator ATC flight number.",
                                _provenance(Origin.OBSERVED if compact == number_reading.value else Origin.DERIVED,
                                            "atc_flight_number", [number_reading]))
        else:
            number = Attributed(number_text, Status.NEEDS_CONFIRMATION, Confidence.LOW,
                                f"ATC flight number '{number_text}' has unexpected characters. Please confirm it.",
                                _provenance(Origin.OBSERVED, "atc_flight_number", [number_reading]))

    if airline.known and number.known:
        operator = airline.value.replace(" ", "")
        if policy.compact_icao_callsign and _ICAO_OPERATOR.match(operator) and number.status is Status.KNOWN:
            value = f"{operator}{number.value}"
        else:
            value = f"{airline.value} {number.value}"
        status = Status.KNOWN if number.status is Status.KNOWN else Status.NEEDS_CONFIRMATION
        sign = Attributed(
            value, status, Confidence.MEDIUM if status is Status.KNOWN else Confidence.LOW,
            "Simulator ATC callsign from ATC AIRLINE and ATC FLIGHT NUMBER; not a VATSIM or network callsign.",
            _provenance(Origin.DERIVED, "atc_airline+atc_flight_number", [airline_reading, number_reading]),
        )
    elif (policy.registration_as_callsign and registration_value is not None
          and registration_value.status is Status.KNOWN):
        sign = Attributed(
            registration_value.value, Status.NEEDS_CONFIRMATION, Confidence.LOW,
            "No simulator airline callsign; using the registration as the callsign. Please confirm it.",
            replace(registration_value.provenance, origin=Origin.DERIVED, method="registration_as_callsign")
            if registration_value.provenance else None,
        )
    elif number.known:
        sign = unknown("The simulator gave a flight number but no airline, so the callsign is unknown.",
                       _provenance(Origin.UNAVAILABLE, "atc_airline+atc_flight_number",
                                   [airline_reading, number_reading]))
    elif airline.known:
        sign = unknown("The simulator gave an airline but no flight number, so the callsign is unknown.",
                       _provenance(Origin.UNAVAILABLE, "atc_airline+atc_flight_number",
                                   [airline_reading, number_reading]))
    else:
        sign = unknown("The simulator gave no airline or flight number, so the callsign is unknown.")
    return CallsignAttribution(sign, airline, number)


@dataclass(frozen=True, slots=True)
class AircraftIdentity:
    type: AircraftTypeAttribution
    registration: Attributed
    callsign: CallsignAttribution

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type.to_json(), "registration": self.registration.to_json(),
                "callsign": self.callsign.to_json()}

    def record_fields(self) -> dict[str, dict[str, Any]]:
        """Logbook-facing fields: aircraft (type display), registration and callsign."""
        return {
            "aircraft": self.type.display.to_record_field(),
            "aircraft_type": self.type.designator.to_record_field(),
            "aircraft_title": self.type.title.to_record_field(),
            "registration": self.registration.to_record_field(),
            "callsign": self.callsign.callsign.to_record_field(),
        }


def identify_aircraft(readings: Readings, policy: IdentityPolicy | None = None) -> AircraftIdentity:
    policy = policy or IdentityPolicy()
    reg = registration(readings)
    return AircraftIdentity(aircraft_type(readings, policy), reg, callsign(readings, policy, reg))


def identity_changes(before: AircraftIdentity | None, after: AircraftIdentity) -> tuple[str, ...]:
    """Names of identity fields whose values changed. A title-only change is a livery change."""
    if before is None:
        return ("type", "title", "registration", "callsign")
    changes = []
    if before.type.group_key != after.type.group_key:
        changes.append("type")
    if before.type.title.value != after.type.title.value:
        changes.append("title")
    if before.registration.value != after.registration.value:
        changes.append("registration")
    if before.callsign.callsign.value != after.callsign.callsign.value:
        changes.append("callsign")
    return tuple(changes)


# --------------------------------------------------------------------------------------------
# Payload stations
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PayloadStation:
    index: int
    name: str | None
    weight_lb: float | None

    @property
    def weight_kg(self) -> float | None:
        return None if self.weight_lb is None else self.weight_lb * LB_TO_KG

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name, "weight_lb": self.weight_lb, "weight_kg": self.weight_kg}


@dataclass(frozen=True, slots=True)
class PayloadAttribution:
    stations: tuple[PayloadStation, ...]
    station_total_kg: Attributed
    crew: Attributed
    passengers: Attributed
    baggage_kg: Attributed
    freight_kg: Attributed

    def to_json(self) -> dict[str, Any]:
        return {
            "stations": [station.to_json() for station in self.stations],
            "station_total_kg": self.station_total_kg.to_json(),
            "crew": self.crew.to_json(),
            "passengers": self.passengers.to_json(),
            "baggage_kg": self.baggage_kg.to_json(),
            "freight_kg": self.freight_kg.to_json(),
        }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


_CATEGORY_REASON = ("SimConnect payload stations do not say how much is {what}. "
                    "It stays unknown until the pilot or a trusted import supplies it.")


def payload(readings: Readings, *, supported: bool | None = None) -> PayloadAttribution:
    """Report payload station evidence. Crew, passengers, baggage and freight stay unknown.

    ``supported=False`` means the Core provider or aircraft cannot supply payload stations; the
    station total is then ``unsupported``. ``None`` means not known: readings decide.
    """
    categories = dict(
        crew=unknown(_CATEGORY_REASON.format(what="crew")),
        passengers=unknown(_CATEGORY_REASON.format(what="passengers")),
        baggage_kg=unknown(_CATEGORY_REASON.format(what="baggage")),
        freight_kg=unknown(_CATEGORY_REASON.format(what="freight")),
    )
    if supported is False:
        return PayloadAttribution((), unknown("This simulator source does not support payload stations.",
                                              status=Status.UNSUPPORTED), **categories)
    count_reading = _reading(readings, PAYLOAD_STATION_COUNT)
    count_value = _number(count_reading.value) if count_reading else None
    if count_reading is None:
        return PayloadAttribution((), unknown("PAYLOAD STATION COUNT was not requested or not received."), **categories)
    if count_value is None or count_value < 0 or count_value != int(count_value):
        return PayloadAttribution((), unknown("The simulator did not report a usable payload station count.",
                                              _provenance(Origin.UNAVAILABLE, "payload_stations", [count_reading])),
                                  **categories)
    count = int(count_value)
    used = [count_reading]
    stations = []
    for index in range(1, min(count, MAX_PAYLOAD_STATIONS) + 1):
        weight_reading = _reading(readings, f"{PAYLOAD_STATION_WEIGHT}:{index}")
        name_reading = _reading(readings, f"{PAYLOAD_STATION_NAME}:{index}")
        weight = _number(weight_reading.value) if weight_reading else None
        if weight is not None and weight < 0:
            weight = None
        used.extend(r for r in (weight_reading, name_reading) if r is not None)
        stations.append(PayloadStation(index, clean_text(name_reading.value) if name_reading else None, weight))
    stations_t = tuple(stations)
    if count == 0:
        total = Attributed(0.0, Status.KNOWN, Confidence.MEDIUM,
                           "The simulator reports no payload stations for this aircraft.",
                           _provenance(Origin.OBSERVED, "payload_stations", used))
    else:
        reported = [s.weight_lb for s in stations_t if s.weight_lb is not None]
        missing = len(stations_t) - len(reported) + max(0, count - MAX_PAYLOAD_STATIONS)
        if not reported:
            total = unknown("No payload station weights were received.",
                            _provenance(Origin.UNAVAILABLE, "payload_stations", used))
        elif missing:
            total = Attributed(
                sum(reported) * LB_TO_KG, Status.INCOMPLETE, Confidence.LOW,
                f"Incomplete: {len(reported)} of {count} station weights were received. "
                "Station weights are not crew, passengers, baggage or freight.",
                _provenance(Origin.DERIVED, "payload_stations.sum", used),
            )
        else:
            total = Attributed(
                sum(reported) * LB_TO_KG, Status.KNOWN, Confidence.MEDIUM,
                "Sum of the simulator's payload station weights (converted from pounds). "
                "Station weights are not crew, passengers, baggage or freight.",
                _provenance(Origin.DERIVED, "payload_stations.sum", used),
            )
    return PayloadAttribution(stations_t, total, **categories)


# --------------------------------------------------------------------------------------------
# Position and airport matching
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Position:
    latitude_deg: float
    longitude_deg: float
    on_ground: bool | None = None
    observed_at: datetime | None = None
    session_id: str | None = None
    source_name: str | None = None
    simulated: bool | None = None
    sim_time_s: float | None = None
    sim_utc: datetime | None = None
    fields: tuple[str, ...] = POSITION_FIELDS

    def __post_init__(self):
        for name, value, limit in (("latitude", self.latitude_deg, 90.0), ("longitude", self.longitude_deg, 180.0)):
            number = _number(value)
            if number is None or abs(number) > limit:
                raise ValueError(f"The aircraft {name} must be a finite number of degrees within ±{limit:g}.")
            object.__setattr__(self, f"{name}_deg", number)
        if self.on_ground is not None and not isinstance(self.on_ground, bool):
            object.__setattr__(self, "on_ground", bool(self.on_ground))

    @classmethod
    def from_readings(cls, readings: Readings) -> "Position | None":
        """Position from PLANE LATITUDE/LONGITUDE (requested in degrees) and SIM ON GROUND.

        Returns None when latitude or longitude is missing or invalid.
        """
        lat = _reading(readings, PLANE_LATITUDE)
        lon = _reading(readings, PLANE_LONGITUDE)
        if lat is None or lon is None:
            return None
        lat_v, lon_v = _number(lat.value), _number(lon.value)
        if lat_v is None or lon_v is None or abs(lat_v) > 90 or abs(lon_v) > 180:
            return None
        ground = _reading(readings, SIM_ON_GROUND)
        ground_value = None
        if ground is not None and ground.value is not None and not isinstance(ground.value, str):
            ground_value = bool(ground.value)
        newest = max((r for r in (lat, lon, ground) if r is not None and r.observed_at is not None),
                     key=lambda r: r.observed_at, default=lat)
        return cls(lat_v, lon_v, ground_value, newest.observed_at, newest.session_id, newest.source_name,
                   newest.simulated, newest.sim_time_s, newest.sim_utc,
                   tuple(r.name for r in (lat, lon, ground) if r is not None))

    def to_json(self) -> dict[str, Any]:
        return {
            "latitude_deg": self.latitude_deg, "longitude_deg": self.longitude_deg, "on_ground": self.on_ground,
            "observed_at": _iso(self.observed_at), "session_id": self.session_id, "source_name": self.source_name,
            "simulated": self.simulated, "sim_time_s": self.sim_time_s, "sim_utc": _iso(self.sim_utc),
            "fields": list(self.fields),
        }


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two points given in degrees."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def runway_surface_distance_m(position: Position, runway: Any) -> float | None:
    """Distance from the aircraft to a runway rectangle (0 inside), or None for unusable data.

    ``runway`` is duck-typed like ``bbc_core.facilities.Runway``: centre latitude/longitude in
    degrees, true heading in degrees, length and width in metres. Uses a local flat projection,
    which is accurate to well under a metre at runway scale.
    """
    lat = _number(getattr(runway, "latitude_deg", None))
    lon = _number(getattr(runway, "longitude_deg", None))
    heading = _number(getattr(runway, "heading_deg", None))
    length = _number(getattr(runway, "length_m", None))
    width = _number(getattr(runway, "width_m", None))
    if None in (lat, lon, heading, length, width) or length <= 0 or width <= 0 or abs(lat) > 90:
        return None
    dlon = (position.longitude_deg - lon + 540.0) % 360.0 - 180.0
    east = math.radians(dlon) * EARTH_RADIUS_M * math.cos(math.radians((lat + position.latitude_deg) / 2))
    north = math.radians(position.latitude_deg - lat) * EARTH_RADIUS_M
    if math.hypot(east, north) > 50_000:
        return math.hypot(east, north)
    h = math.radians(heading)
    along = east * math.sin(h) + north * math.cos(h)
    cross = east * math.cos(h) - north * math.sin(h)
    return math.hypot(max(0.0, abs(along) - length / 2), max(0.0, abs(cross) - width / 2))


@dataclass(frozen=True, slots=True)
class AirportMatchPolicy:
    max_distance_m: float = 6000.0
    high_confidence_m: float = 1500.0
    ambiguity_margin_m: float = 750.0
    runway_margin_m: float = 60.0
    require_on_ground: bool = True
    max_result_age_s: float = 120.0
    max_candidates: int = 5

    def __post_init__(self):
        for name in ("max_distance_m", "high_confidence_m", "ambiguity_margin_m", "runway_margin_m",
                     "max_result_age_s"):
            value = _number(getattr(self, name))
            if value is None or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number.")
        if self.high_confidence_m > self.max_distance_m:
            raise ValueError("high_confidence_m cannot exceed max_distance_m.")
        if type(self.max_candidates) is not int or self.max_candidates < 1:
            raise ValueError("max_candidates must be a positive integer.")

    def to_json(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "max_distance_m", "high_confidence_m", "ambiguity_margin_m", "runway_margin_m",
            "require_on_ground", "max_result_age_s", "max_candidates")}


@dataclass(frozen=True, slots=True)
class AirportCandidate:
    icao: str
    region: str | None
    latitude_deg: float
    longitude_deg: float
    distance_m: float                     # to the airport reference point
    runway_distance_m: float | None = None  # to the nearest runway surface, when details were supplied

    @property
    def effective_distance_m(self) -> float:
        if self.runway_distance_m is None:
            return self.distance_m
        return min(self.distance_m, self.runway_distance_m)

    def to_json(self) -> dict[str, Any]:
        return {"icao": self.icao, "region": self.region, "latitude_deg": self.latitude_deg,
                "longitude_deg": self.longitude_deg, "distance_m": round(self.distance_m, 1),
                "runway_distance_m": None if self.runway_distance_m is None else round(self.runway_distance_m, 1)}


def _airport_details_runways(details: Any) -> tuple[Any, ...]:
    return tuple(getattr(details, "runways", ()) or ())


def rank_airports(position: Position, airports: Iterable[Any], *,
                  details: Mapping[str, Any] | None = None) -> tuple[AirportCandidate, ...]:
    """Rank airport references (duck-typed ``AirportReference``) by effective distance.

    Entries with an unusable identifier or position are skipped. Duplicate identifiers keep the
    nearest entry. ``details`` maps ICAO to ``AirportDetails``-like objects whose runways refine
    the distance.
    """
    best: dict[str, AirportCandidate] = {}
    for airport in airports:
        icao = normalise_airport_ident(getattr(airport, "icao", None))
        lat = _number(getattr(airport, "latitude_deg", None))
        lon = _number(getattr(airport, "longitude_deg", None))
        if icao is None or lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            continue
        distance = haversine_m(position.latitude_deg, position.longitude_deg, lat, lon)
        runway_distance = None
        detail = (details or {}).get(icao)
        if detail is not None:
            values = [d for d in (runway_surface_distance_m(position, r) for r in _airport_details_runways(detail))
                      if d is not None]
            runway_distance = min(values) if values else None
        region = clean_text(getattr(airport, "region", None))
        candidate = AirportCandidate(icao, region.upper() if region else None, lat, lon, distance, runway_distance)
        current = best.get(icao)
        if current is None or candidate.effective_distance_m < current.effective_distance_m:
            best[icao] = candidate
    return tuple(sorted(best.values(), key=lambda c: (c.effective_distance_m, c.icao)))


@dataclass(frozen=True, slots=True)
class AirportAttribution:
    role: str                                   # "departure", "arrival" or a caller-defined role
    airport: Attributed                         # ICAO candidate
    candidates: tuple[AirportCandidate, ...]    # nearest first, within the match radius
    position: Position | None
    policy: AirportMatchPolicy

    @property
    def icao(self) -> str | None:
        return self.airport.value

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "airport": self.airport.to_json(),
            "candidates": [candidate.to_json() for candidate in self.candidates],
            "position": self.position.to_json() if self.position else None,
            "policy": self.policy.to_json(),
        }


def _airport_provenance(position: Position | None, origin: Origin, *, request_id: str | None,
                        received_at: datetime | None, method: str = "facility.nearby_airports") -> Provenance:
    if position is None:
        return Provenance(origin, method, request_id=request_id, facility_received_at=received_at)
    return Provenance(
        origin=origin, method=method, fields=position.fields, observed_at=position.observed_at,
        session_id=position.session_id, source_name=position.source_name, simulated=position.simulated,
        sim_time_s=position.sim_time_s, sim_utc=position.sim_utc,
        raw=(("latitude_deg", position.latitude_deg), ("longitude_deg", position.longitude_deg),
             ("on_ground", position.on_ground)),
        request_id=request_id, facility_received_at=received_at,
    )


def _km(metres: float) -> str:
    return f"{metres / 1000:.1f} km"


def match_airport(
    position: Position | None, airports: Iterable[Any], *, role: str = "airport",
    policy: AirportMatchPolicy | None = None, details: Mapping[str, Any] | None = None,
    request_id: str | None = None, received_at: datetime | None = None, result_session_id: str | None = None,
) -> AirportAttribution:
    """Attribute the airport the aircraft is at from a nearby-airport list.

    Returns ``unknown`` when no airport lies within ``policy.max_distance_m``, ``ambiguous``
    when two airports are within the ambiguity margin of each other, and otherwise a ``known``
    candidate whose confidence reflects distance, ground state and list freshness.
    """
    policy = policy or AirportMatchPolicy()

    def result(airport: Attributed, candidates: tuple[AirportCandidate, ...] = ()) -> AirportAttribution:
        return AirportAttribution(role, airport, candidates, position, policy)

    def none(reason: str) -> AirportAttribution:
        return result(unknown(reason, _airport_provenance(position, Origin.UNAVAILABLE, request_id=request_id,
                                                          received_at=received_at)))

    if position is None:
        return none("The aircraft position is unknown, so the airport is unknown.")
    if policy.require_on_ground and position.on_ground is False:
        return none("The aircraft was airborne, so it is not attributed to an airport.")
    if (result_session_id is not None and position.session_id is not None
            and result_session_id != position.session_id):
        return none("The airport list came from a different simulator session, so it was not used.")

    ranked = rank_airports(position, airports, details=details)
    within = tuple(c for c in ranked if c.effective_distance_m <= policy.max_distance_m)[:policy.max_candidates]
    if not within:
        if ranked:
            nearest = ranked[0]
            return none(f"No simulator airport within {_km(policy.max_distance_m)} of the aircraft "
                        f"(nearest: {nearest.icao} at {_km(nearest.effective_distance_m)}). "
                        "The airport stays unknown until the pilot enters it.")
        return none("The simulator reported no nearby airports. The airport stays unknown until the pilot enters it.")

    best = within[0]
    notes = []
    on_runway = best.runway_distance_m is not None and best.runway_distance_m <= policy.runway_margin_m
    if on_runway or best.effective_distance_m <= policy.high_confidence_m:
        confidence = Confidence.HIGH
    elif best.effective_distance_m <= (policy.high_confidence_m + policy.max_distance_m) / 2:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW
    where = "on a runway of" if on_runway else f"{_km(best.effective_distance_m)} from"
    if position.on_ground is None:
        confidence = lower_confidence(confidence)
        notes.append("The ground state was unknown.")
    if received_at is not None and position.observed_at is not None:
        age = abs((received_at - position.observed_at).total_seconds())
        if age > policy.max_result_age_s:
            confidence = lower_confidence(confidence)
            notes.append(f"The airport list was {age:.0f} s away from the position sample.")
    provenance = _airport_provenance(position, Origin.DERIVED, request_id=request_id, received_at=received_at)
    tail = (" " + " ".join(notes)) if notes else ""
    if len(within) > 1 and within[1].effective_distance_m - best.effective_distance_m <= policy.ambiguity_margin_m:
        names = ", ".join(f"{c.icao} ({_km(c.effective_distance_m)})" for c in within)
        airport = Attributed(
            best.icao, Status.AMBIGUOUS, Confidence.LOW,
            f"Several airports are close to the aircraft: {names}. {best.icao} is nearest; please confirm.{tail}",
            provenance,
        )
    else:
        airport = Attributed(
            best.icao, Status.KNOWN, confidence,
            f"Nearest simulator airport; the aircraft was {where} {best.icao}.{tail}", provenance,
        )
    return result(airport, within)


def _state_name(state: Any) -> str:
    return str(getattr(state, "value", state) or "").lower()


def match_from_facility_result(position: Position | None, result: Any, *, role: str = "airport",
                               policy: AirportMatchPolicy | None = None,
                               details: Mapping[str, Any] | None = None) -> AirportAttribution | None:
    """Match against a Core ``FacilityResult`` (duck-typed). Returns None while it is pending."""
    policy = policy or AirportMatchPolicy()
    state = _state_name(getattr(result, "state", None))
    request_id = getattr(result, "id", None)
    received_at = getattr(result, "received_at", None)
    if state == "pending":
        return None
    if state == "ready":
        data = getattr(result, "data", None)
        if data is None:
            airports: tuple[Any, ...] = ()
        elif hasattr(data, "airport") and hasattr(data, "runways"):
            # A single AirportDetails reply: use it as a one-entry list with its runways.
            airports = (data.airport,)
            icao = normalise_airport_ident(getattr(data.airport, "icao", None))
            if icao:
                details = {**(details or {}), icao: data}
        else:
            airports = tuple(data)
        return match_airport(position, airports, role=role, policy=policy, details=details, request_id=request_id,
                             received_at=received_at, result_session_id=getattr(result, "session_id", None))
    detail = clean_text(getattr(result, "detail", None))
    if state == "no_data":
        reason = "The simulator had no airport record for this request, so the airport is unknown."
    else:
        reason = "The simulator airport list was unavailable"
        reason += f" ({detail})." if detail else "."
        reason += " The airport stays unknown until the pilot enters it."
    return AirportAttribution(role, unknown(reason, _airport_provenance(
        position, Origin.UNAVAILABLE, request_id=request_id, received_at=received_at)), (), position, policy)


# --------------------------------------------------------------------------------------------
# Arrival versus plan: diversions are suspected, never confirmed automatically
# --------------------------------------------------------------------------------------------

class ArrivalVerdict(_ValueEnum):
    NO_PLAN = "no_plan"
    MATCHES_PLAN = "matches_plan"
    DIVERSION_SUSPECTED = "diversion_suspected"
    ARRIVAL_UNKNOWN = "arrival_unknown"
    DIVERSION_CONFIRMED = "diversion_confirmed"   # only through record_pilot_diversion_decision
    DIVERSION_REJECTED = "diversion_rejected"     # pilot said it was not a diversion


@dataclass(frozen=True, slots=True)
class ArrivalAssessment:
    verdict: ArrivalVerdict
    actual: AirportAttribution
    planned_destination: str | None
    reason: str
    needs_pilot_confirmation: bool
    resolved_by_plan: bool = False
    matched_alternate: str | None = None
    returned_to_departure: bool = False
    pilot_decided_at: datetime | None = None
    pilot_arrival: str | None = None

    @property
    def diversion_confirmed(self) -> bool:
        return self.verdict is ArrivalVerdict.DIVERSION_CONFIRMED

    def logbook_arrival(self) -> Attributed:
        """The value to put in the Logbook's arrival ("To") field, labelled honestly."""
        prov = self.actual.airport.provenance
        if self.verdict is ArrivalVerdict.MATCHES_PLAN:
            return Attributed(self.planned_destination, Status.KNOWN,
                              self.actual.airport.confidence if not self.resolved_by_plan else Confidence.MEDIUM,
                              self.reason, prov)
        if self.verdict in (ArrivalVerdict.DIVERSION_CONFIRMED, ArrivalVerdict.DIVERSION_REJECTED):
            return Attributed(self.pilot_arrival, Status.KNOWN, Confidence.HIGH, self.reason, prov)
        if self.verdict is ArrivalVerdict.DIVERSION_SUSPECTED:
            return Attributed(self.actual.icao, Status.NEEDS_CONFIRMATION, Confidence.LOW, self.reason, prov)
        if self.verdict is ArrivalVerdict.NO_PLAN:
            return self.actual.airport
        return unknown(self.reason, prov)

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "planned_destination": self.planned_destination,
            "reason": self.reason,
            "needs_pilot_confirmation": self.needs_pilot_confirmation,
            "resolved_by_plan": self.resolved_by_plan,
            "matched_alternate": self.matched_alternate,
            "returned_to_departure": self.returned_to_departure,
            "pilot_decided_at": _iso(self.pilot_decided_at),
            "pilot_arrival": self.pilot_arrival,
            "actual": self.actual.to_json(),
            "rules_version": RULES_VERSION,
        }


def assess_arrival(
    actual: AirportAttribution, planned_destination: Any, *, alternates: Iterable[Any] = (),
    departure_icao: Any = None, planned_position: tuple[float, float] | None = None,
) -> ArrivalAssessment:
    """Compare an attributed arrival with a planned destination. Never confirms a diversion.

    ``planned_position`` (latitude, longitude in degrees) is the planned destination's
    reference point, for example from ``request_airport(planned)``; it lets the plan resolve
    an airport that the nearby list missed or ranked second.
    """
    policy = actual.policy
    planned = normalise_airport_ident(planned_destination)
    alternate_set = tuple(a for a in (normalise_airport_ident(x) for x in alternates) if a)
    departure = normalise_airport_ident(departure_icao)
    if planned is None:
        reason = ("No planned destination was supplied, so no diversion check was made." if planned_destination
                  in (None, "") else f"The planned destination '{planned_destination}' is not a valid identifier, "
                  "so no diversion check was made.")
        return ArrivalAssessment(ArrivalVerdict.NO_PLAN, actual, None, reason,
                                 needs_pilot_confirmation=actual.airport.needs_pilot or not actual.airport.known)

    planned_distance = None
    if planned_position is not None and actual.position is not None:
        lat, lon = (_number(planned_position[0]), _number(planned_position[1]))
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            planned_distance = haversine_m(actual.position.latitude_deg, actual.position.longitude_deg, lat, lon)
    candidate_planned = next((c for c in actual.candidates if c.icao == planned), None)
    if candidate_planned is not None:
        planned_distance = (candidate_planned.effective_distance_m if planned_distance is None
                            else min(planned_distance, candidate_planned.effective_distance_m))

    def matches(reason: str, resolved: bool) -> ArrivalAssessment:
        return ArrivalAssessment(ArrivalVerdict.MATCHES_PLAN, actual, planned, reason, False, resolved)

    if actual.icao == planned and actual.airport.status is Status.KNOWN:
        return matches(f"Arrived at the planned destination {planned}.", False)
    if actual.icao == planned and actual.airport.status is Status.AMBIGUOUS:
        return matches(f"Several airports were close; the nearest is the planned destination {planned}.", True)
    if not actual.airport.known:
        if planned_distance is not None and planned_distance <= policy.max_distance_m:
            return matches(f"The nearby list did not identify the airport, but the aircraft was "
                           f"{_km(planned_distance)} from the planned destination {planned}.", True)
        return ArrivalAssessment(
            ArrivalVerdict.ARRIVAL_UNKNOWN, actual, planned,
            "The arrival airport is unknown, so it cannot be compared with the plan. Please enter the arrival.",
            needs_pilot_confirmation=True,
        )
    best = actual.candidates[0].effective_distance_m if actual.candidates else None
    if (planned_distance is not None and best is not None and planned_distance <= policy.max_distance_m
            and planned_distance - best <= policy.ambiguity_margin_m):
        return matches(f"The planned destination {planned} is {_km(planned_distance)} away, within "
                       f"{policy.ambiguity_margin_m:.0f} m of the nearest airport {actual.icao}; "
                       f"treated as the planned destination.", True)
    notes = []
    alternate = actual.icao if actual.icao in alternate_set else None
    if alternate:
        notes.append(f"{actual.icao} is a planned alternate.")
    returned = departure is not None and actual.icao == departure
    if returned:
        notes.append("This is the departure airport.")
    tail = (" " + " ".join(notes)) if notes else ""
    return ArrivalAssessment(
        ArrivalVerdict.DIVERSION_SUSPECTED, actual, planned,
        f"Possible diversion: the aircraft arrived at {actual.icao}, not the planned destination {planned}."
        f"{tail} Please confirm whether this was a diversion.",
        needs_pilot_confirmation=True, matched_alternate=alternate, returned_to_departure=returned,
    )


def record_pilot_diversion_decision(assessment: ArrivalAssessment, *, confirmed: bool, decided_at: datetime,
                                    arrival_icao: Any = None) -> ArrivalAssessment:
    """Record the pilot's answer to a suspected diversion (or to an unknown arrival).

    ``confirmed=True`` records a confirmed diversion to ``arrival_icao`` (default: the attributed
    arrival). ``confirmed=False`` records that the flight arrived at ``arrival_icao`` (default:
    the planned destination) and was not a diversion.
    """
    if assessment.verdict not in (ArrivalVerdict.DIVERSION_SUSPECTED, ArrivalVerdict.ARRIVAL_UNKNOWN):
        raise ValueError("Only a suspected diversion or an unknown arrival needs a pilot decision.")
    if not isinstance(decided_at, datetime):
        raise ValueError("Record when the pilot decided as a datetime.")
    if confirmed:
        arrival = normalise_airport_ident(arrival_icao) or assessment.actual.icao
        if arrival is None:
            raise ValueError("A confirmed diversion needs the arrival airport.")
        if arrival == assessment.planned_destination:
            raise ValueError("A diversion cannot end at the planned destination.")
        return replace(assessment, verdict=ArrivalVerdict.DIVERSION_CONFIRMED, needs_pilot_confirmation=False,
                       pilot_decided_at=decided_at, pilot_arrival=arrival,
                       reason=f"The pilot confirmed a diversion to {arrival} "
                              f"(planned destination {assessment.planned_destination}).")
    arrival = normalise_airport_ident(arrival_icao) or assessment.planned_destination
    return replace(assessment, verdict=ArrivalVerdict.DIVERSION_REJECTED, needs_pilot_confirmation=False,
                   pilot_decided_at=decided_at, pilot_arrival=arrival,
                   reason=f"The pilot recorded the arrival as {arrival}; not a diversion.")


# --------------------------------------------------------------------------------------------
# Core facility helpers (poll-driven and asyncio)
# --------------------------------------------------------------------------------------------

@runtime_checkable
class FacilitySource(Protocol):
    """The part of ``bbc_core.core.ConsumerAPI`` these helpers use."""

    def request_nearby_airports(self) -> Any: ...

    def facility_result(self, request_id: str) -> Any | None: ...


_TERMINAL_STATES = frozenset({"ready", "no_data", "unavailable"})


@dataclass(slots=True)
class _PendingLookup:
    token: str
    role: str
    position: Position | None
    request_id: str | None
    started: float
    details: Mapping[str, Any] | None


class NearbyAirportLookup:
    """Poll-driven nearby-airport attribution with timeouts. No threads, no asyncio needed.

    ``begin()`` issues ``api.request_nearby_airports()`` and returns a token. Pass every item
    the host gets from ``api.poll()`` to ``offer()`` (non-facility items are ignored), and call
    ``check()`` periodically; each returns the attributions that finished. A request that
    raises, never answers within ``timeout_s`` or answers ``unavailable`` finishes as
    ``unknown`` with a plain reason, never as a guess.
    """

    def __init__(self, api: FacilitySource, *, policy: AirportMatchPolicy | None = None, timeout_s: float = 10.0,
                 clock: Callable[[], float] = time.monotonic):
        if _number(timeout_s) is None or timeout_s <= 0:
            raise ValueError("timeout_s must be a positive number of seconds.")
        self._api = api
        self._policy = policy or AirportMatchPolicy()
        self._timeout_s = float(timeout_s)
        self._clock = clock
        self._pending: dict[str, _PendingLookup] = {}
        self._ready: list[AirportAttribution] = []
        self._counter = 0

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(self._pending)

    def begin(self, role: str, position: Position | None, *, details: Mapping[str, Any] | None = None) -> str:
        self._counter += 1
        token = f"{role}#{self._counter}"
        if position is None:
            self._ready.append(match_airport(None, (), role=role, policy=self._policy))
            return token
        try:
            result = self._api.request_nearby_airports()
        except Exception as exc:  # the Recorder must keep running whatever Core says
            message = clean_text(str(exc)) or type(exc).__name__
            self._ready.append(AirportAttribution(role, unknown(
                f"The nearby-airport request could not be made ({message}). The airport stays unknown.",
                _airport_provenance(position, Origin.UNAVAILABLE, request_id=None, received_at=None)),
                (), position, self._policy))
            return token
        pending = _PendingLookup(token, role, position, getattr(result, "id", None), self._clock(), details)
        self._pending[token] = pending
        self._settle(pending, result)
        return token

    def cancel(self, token: str) -> None:
        self._pending.pop(token, None)

    def offer(self, update: Any) -> tuple[AirportAttribution, ...]:
        request_id = getattr(update, "id", None)
        if request_id is not None and hasattr(update, "state"):
            for pending in tuple(self._pending.values()):
                if pending.request_id == request_id:
                    self._settle(pending, update)
        return self._drain()

    def check(self) -> tuple[AirportAttribution, ...]:
        now = self._clock()
        for pending in tuple(self._pending.values()):
            if pending.request_id is not None:
                try:
                    result = self._api.facility_result(pending.request_id)
                except Exception:  # noqa: BLE001 - treat as no answer yet; the timeout decides
                    result = None
                if result is not None and self._settle(pending, result):
                    continue
            if now - pending.started >= self._timeout_s:
                self._pending.pop(pending.token, None)
                self._ready.append(AirportAttribution(pending.role, unknown(
                    f"The simulator did not answer the nearby-airport request within {self._timeout_s:g} s. "
                    "The airport stays unknown.",
                    _airport_provenance(pending.position, Origin.UNAVAILABLE, request_id=pending.request_id,
                                        received_at=None)), (), pending.position, self._policy))
        return self._drain()

    def _settle(self, pending: _PendingLookup, result: Any) -> bool:
        if pending.token not in self._pending or _state_name(getattr(result, "state", None)) not in _TERMINAL_STATES:
            return False
        attribution = match_from_facility_result(pending.position, result, role=pending.role, policy=self._policy,
                                                 details=pending.details)
        if attribution is None:
            return False
        self._pending.pop(pending.token, None)
        self._ready.append(attribution)
        return True

    def _drain(self) -> tuple[AirportAttribution, ...]:
        ready, self._ready = tuple(self._ready), []
        return ready


async def resolve_airport(
    api: FacilitySource, position: Position | None, *, role: str = "airport",
    policy: AirportMatchPolicy | None = None, details: Mapping[str, Any] | None = None, timeout_s: float = 10.0,
    interval_s: float = 0.25, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> AirportAttribution:
    """Await a nearby-airport attribution by pulling ``facility_result()`` (never ``poll()``)."""
    if _number(interval_s) is None or interval_s <= 0:
        raise ValueError("interval_s must be a positive number of seconds.")
    lookup = NearbyAirportLookup(api, policy=policy, timeout_s=timeout_s, clock=clock)
    token = lookup.begin(role, position, details=details)
    while True:
        done = lookup.check()
        for item in done:
            return item
        if token not in lookup.pending:  # finished during begin() and already drained
            raise RuntimeError("Airport lookup finished without a result.")  # pragma: no cover - defensive
        await sleep(interval_s)


# --------------------------------------------------------------------------------------------
# SimVar request tuples (lazy Core import; this module stays usable without bbc_core)
# --------------------------------------------------------------------------------------------

def identity_simvars() -> tuple[Any, ...]:
    """SimVars for identity: TITLE and the ATC strings (text, no unit)."""
    from bbc_core.contract import ValueType
    from bbc_core.sdk_simvar import SimVar
    return tuple(SimVar(name, None, ValueType.TEXT) for name in IDENTITY_FIELDS)


def position_simvars() -> tuple[Any, ...]:
    """PLANE LATITUDE/LONGITUDE in degrees and SIM ON GROUND as a Boolean."""
    from bbc_core.contract import ValueType
    from bbc_core.sdk_simvar import SimVar
    return (SimVar(PLANE_LATITUDE, "degrees", ValueType.NUMBER), SimVar(PLANE_LONGITUDE, "degrees", ValueType.NUMBER),
            SimVar(SIM_ON_GROUND, "bool", ValueType.BOOLEAN))


def payload_simvars(station_count: int | None = None) -> tuple[Any, ...]:
    """PAYLOAD STATION COUNT, plus NAME:i and WEIGHT:i (pounds) for stations 1..count (max 15).

    Request the count first (``station_count=None``), then the stations it reports.
    """
    from bbc_core.contract import ValueType
    from bbc_core.sdk_simvar import SimVar
    fields = [SimVar(PAYLOAD_STATION_COUNT, "number", ValueType.INTEGER)]
    if station_count is not None:
        if type(station_count) is not int or station_count < 0:
            raise ValueError("station_count must be a non-negative integer.")
        for index in range(1, min(station_count, MAX_PAYLOAD_STATIONS) + 1):
            fields.append(SimVar(PAYLOAD_STATION_NAME, None, ValueType.TEXT, index))
            fields.append(SimVar(PAYLOAD_STATION_WEIGHT, "pounds", ValueType.NUMBER, index))
    return tuple(fields)


__all__ = [
    "RULES_VERSION", "Origin", "Status", "Confidence", "FieldReading", "Readings", "Provenance", "Attributed",
    "readings_from_snapshot", "readings_from_values", "merge_readings", "clean_text", "unwrap_localisation_key",
    "type_designator", "normalise_airport_ident", "IdentityPolicy", "AircraftTypeAttribution", "aircraft_type",
    "registration", "CallsignAttribution", "callsign", "AircraftIdentity", "identify_aircraft", "identity_changes",
    "PayloadStation", "PayloadAttribution", "payload", "Position", "haversine_m", "runway_surface_distance_m",
    "AirportMatchPolicy", "AirportCandidate", "rank_airports", "AirportAttribution", "match_airport",
    "match_from_facility_result", "ArrivalVerdict", "ArrivalAssessment", "assess_arrival",
    "record_pilot_diversion_decision", "FacilitySource", "NearbyAirportLookup", "resolve_airport",
    "identity_simvars", "position_simvars", "payload_simvars", "lower_confidence", "unknown",
]
