"""Local, versioned Logbook records.

One JSON file per flight in ``<data folder>/flights/<uuid>.json``. The record
format is described in ``STORAGE_SCHEMA.md`` next to this module. In short:

* Schema 2 is written. Schema 1 files (manual flights from the first concept)
  are read as they are and migrated only when that flight is next saved. The
  original bytes are copied to ``<data folder>/migration/schema-1/`` first.
* Past revisions are written back exactly as they were read. Only the new
  revision is normalised, and only for the values that actually changed.
* Keys this version does not know are kept and written back unchanged.
* One process at a time owns a data folder for writing (``.bbc-efb-writer.lock``).
  Inside that process every write goes through one ``FlightStore``, whose
  compare-and-write is serialised by a lock, so the Recorder and the Logbook UI
  can share it from different threads. ``listing()`` and ``get()`` never wait for a
  writer, so the UI stays responsive while a write retries.
* A write can take up to about a second when another program (OneDrive, antivirus,
  a backup tool) briefly holds the file, because the replace is retried. Never call
  the write methods (``save``, ``create_recorded`` ...) from the Core pump thread;
  hand them to a worker thread so simulator ticks never wait on disk I/O.
"""

from __future__ import annotations

import copy
import errno
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from datetime import date, datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import threading
import time
from tempfile import NamedTemporaryFile
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID, uuid4

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SOURCES = ("manual", "recorder", "imported")
AUTHORS = ("pilot", "recorder", "import")
ORIGINS = ("pilot", "observed", "derived", "recorder", "imported")
TIME_BASES = ("simulator", "host")
DEFAULT_AUTHOR = {"manual": "pilot", "recorder": "recorder", "imported": "import"}
DEFAULT_ORIGIN = {"pilot": "pilot", "recorder": "recorder", "import": "imported"}

TEXT_FIELDS = ("aircraft", "departure", "arrival", "callsign", "registration", "notes")
CODE_FIELDS = ("aircraft", "departure", "arrival", "callsign", "registration")
NUMBER_FIELDS = ("minutes", "crew", "passengers", "baggage_kg", "freight_kg")
REQUIRED_FIELDS = ("aircraft", "departure", "arrival")
#: Maximum length of a new or changed value, measured after normalisation.
#: Stored values longer than this are still read and kept unchanged.
TEXT_LIMITS = MappingProxyType({
    "aircraft": 256, "departure": 64, "arrival": 64, "callsign": 32,
    "registration": 32, "notes": 20000,
})
NUMBER_LIMITS = MappingProxyType({
    "minutes": 9999, "crew": 99, "passengers": 9999, "baggage_kg": 999999,
    "freight_kg": 999999,
})
FIELD_LABELS = MappingProxyType({
    "flight_date": "Flight date", "aircraft": "Aircraft type", "departure": "From",
    "arrival": "To", "callsign": "Callsign", "registration": "Registration",
    "minutes": "Duration", "notes": "Notes", "crew": "Crew", "passengers": "Passengers",
    "baggage_kg": "Baggage", "freight_kg": "Freight",
})
NUMBER_UNITS = MappingProxyType({
    "minutes": "minutes", "crew": "", "passengers": "", "baggage_kg": "kg", "freight_kg": "kg",
})

LOCK_NAME = ".bbc-efb-writer.lock"
MIGRATION_FOLDER = "migration"
MAX_RECORD_BYTES = 16 * 1024 * 1024
LEFTOVER_AGE_S = 60.0
_RETRY_WINERRORS = frozenset({5, 32, 33})  # access denied, sharing violation, lock violation
_RETRY_DELAYS_S = (0.02, 0.04, 0.08, 0.16, 0.25, 0.25, 0.25)
_KNOWN_TOP = frozenset({
    "schema", "id", "source", "revisions", "recorder", "import", "acknowledgements",
    "migrated_from", "extra",
})
_KNOWN_REVISION = frozenset({"saved_at", "fields", "by", "reason", "origins", "confirmed"})
_EMPTY = MappingProxyType({})


def _empty_mapping():
    # A default_factory, not default=_EMPTY: Python 3.11 rejects an unhashable
    # dataclass default (mappingproxy is hashable only from 3.12).
    return _EMPTY


# Errors -------------------------------------------------------------------

class RecordConflict(ValueError):
    """The record changed on disk since it was opened."""


class RecordReadOnly(ValueError):
    """This version may show the record but must not change it."""


class UnreadableRecord(ValueError):
    """A flight file exists but cannot be read safely."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class RecordMissing(FileNotFoundError):
    """The flight is not (or no longer) in this data folder."""


class DataFolderMissing(FileNotFoundError):
    """A data folder chosen earlier is not there (renamed, deleted or drive detached)."""


class DataFolderNotWritable(PermissionError):
    """The data folder cannot be written."""


class StoreLocked(OSError):
    """Another BBC EFB (or another FlightStore) already writes to this data folder."""


class StoreReadOnly(PermissionError):
    """This FlightStore was opened read-only."""


class FlightFileBusy(PermissionError):
    """Another program kept the flight file open while it was being replaced."""


# Value checks -------------------------------------------------------------

def _parse_instant(value, what="saved_at"):
    """Return an aware UTC datetime for an ISO 8601 string with an offset."""
    if not isinstance(value, str):
        raise UnreadableRecord(f"{what} must be an ISO 8601 date and time, not {type(value).__name__}.")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise UnreadableRecord(f"{what} {value!r} is not an ISO 8601 date and time.") from None
    if moment.tzinfo is None:
        raise UnreadableRecord(f"{what} {value!r} has no UTC offset.")
    return moment.astimezone(timezone.utc)


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _check_text_encodable(name, value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{FIELD_LABELS[name]} contains characters that cannot be saved.") from None


def _normalise_field(name, value):
    """Validate and normalise one new or changed value (pilot or programme input)."""
    label = FIELD_LABELS[name]
    if name == "flight_date":
        if not isinstance(value, str):
            raise ValueError("Enter the flight date as YYYY-MM-DD.")
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            raise ValueError(f"{value!r} is not a valid flight date. Enter it as YYYY-MM-DD.") from None
    if name in NUMBER_FIELDS:
        if value is None:
            return None
        limit = NUMBER_LIMITS[name]
        if type(value) is not int or not 0 <= value <= limit:
            if name == "minutes":
                raise ValueError("Enter a duration from 0 to 9,999 minutes.")
            unit = f" {NUMBER_UNITS[name]}" if NUMBER_UNITS[name] else ""
            raise ValueError(f"Enter {label.lower()} as a whole number from 0 to {limit:,}{unit}, or leave it unknown.")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    _check_text_encodable(name, value)
    value = value.strip()
    if name in CODE_FIELDS:
        value = value.upper()
    limit = TEXT_LIMITS[name]
    if len(value) > limit:
        raise ValueError(f"{label} is longer than {limit:,} characters.")
    return value


def _check_capture_field(name, value):
    """Validate a value supplied by the Recorder or an importer. Nothing is reformatted."""
    label = FIELD_LABELS[name]
    if name == "flight_date":
        if not isinstance(value, str):
            raise ValueError("flight_date must be text in YYYY-MM-DD form.")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"flight_date {value!r} is not a valid YYYY-MM-DD date.") from None
        return value
    if name in NUMBER_FIELDS:
        if value is not None and (type(value) is not int or not 0 <= value <= NUMBER_LIMITS[name]):
            raise ValueError(f"{name} must be None (unknown) or a whole number from 0 to {NUMBER_LIMITS[name]:,}.")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text (empty when unknown).")
    _check_text_encodable(name, value)
    if len(value) > TEXT_LIMITS[name]:
        raise ValueError(f"{label} is longer than {TEXT_LIMITS[name]:,} characters.")
    return value


# Records ------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlightFields:
    """Pilot-facing values of one flight. Payload values are ``None`` when unknown (not 0)."""

    flight_date: str
    aircraft: str
    departure: str
    arrival: str
    callsign: str = ""
    registration: str = ""
    minutes: int | None = None
    notes: str = ""
    crew: int | None = None
    passengers: int | None = None
    baggage_kg: int | None = None
    freight_kg: int | None = None

    def validated(self):
        """Return a normalised copy of a new manual entry, or raise ValueError."""
        values = {name: _normalise_field(name, getattr(self, name)) for name in FIELD_NAMES}
        if not all(values[name] for name in REQUIRED_FIELDS):
            raise ValueError("Aircraft type, departure and destination are required.")
        return FlightFields(**values)

    def as_json(self):
        return {name: getattr(self, name) for name in FIELD_NAMES}


FIELD_NAMES = tuple(item.name for item in dataclass_fields(FlightFields))


@dataclass(frozen=True, slots=True)
class Revision:
    """One saved state of a flight. ``by`` says who saved it, ``reason`` why."""

    saved_at: str
    fields: FlightFields
    by: str = "pilot"
    reason: str = ""
    origins: Mapping[str, str] = field(default_factory=_empty_mapping, compare=False)
    extra: Mapping[str, Any] = field(default_factory=_empty_mapping, compare=False)  # unknown keys inside "fields"
    #: Fields the pilot checked in this revision and confirmed as correct (values unchanged).
    confirmed: tuple[str, ...] = ()

    @property
    def saved_at_utc(self):
        return _parse_instant(self.saved_at)


@dataclass(frozen=True, slots=True)
class Correction:
    at: str
    previous: Any
    value: Any
    by: str
    reason: str


@dataclass(frozen=True, slots=True)
class Confirmation:
    """The pilot checked a field and confirmed ``value`` as correct without changing it."""

    at: str
    value: Any
    by: str
    reason: str


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    """Where a field's value came from and how it was changed or confirmed afterwards.

    ``confirmed`` is True when the pilot confirmed the field after its last change, so the
    value shown now is the one the pilot checked.
    """

    name: str
    origin: str
    original: Any
    value: Any
    corrections: tuple[Correction, ...] = ()
    confirmations: tuple[Confirmation, ...] = ()
    confirmed: bool = False

    @property
    def corrected(self):
        return bool(self.corrections)


@dataclass(frozen=True, slots=True)
class Gap:
    """A period without trustworthy evidence. Times use the evidence's time basis."""

    reason: str
    start: str | None = None
    end: str | None = None
    kind: str = ""

    def as_json(self):
        data = {"reason": self.reason, "start": self.start, "end": self.end}
        if self.kind:
            data["kind"] = self.kind
        return data


@dataclass(frozen=True, slots=True)
class RecorderEvidence:
    """What the Recorder observed. ``None`` means not measured, never zero."""

    journal: str
    summary: str
    time_basis: str
    simulated: bool
    off_block: str | None = None
    takeoff: str | None = None
    landing: str | None = None
    on_block: str | None = None
    block_s: float | None = None
    airborne_s: float | None = None
    touchdown_rate_fpm: float | None = None
    gaps: tuple[Gap, ...] = ()
    rules_version: str = ""
    extra: Mapping[str, Any] = field(default_factory=_empty_mapping, compare=False)

    def checked(self):
        if not isinstance(self.journal, str) or not self.journal.strip():
            raise ValueError("Recorder evidence needs the journal path.")
        for flavour in (PurePosixPath, PureWindowsPath):
            path = flavour(self.journal)
            if path.is_absolute() or path.anchor or ".." in path.parts:
                raise ValueError("The journal path must be relative to the data folder.")
        if not isinstance(self.summary, str):
            raise ValueError("Recorder evidence summary must be text.")
        if self.time_basis not in TIME_BASES:
            raise ValueError("Recorder evidence time_basis must be 'simulator' or 'host'.")
        if type(self.simulated) is not bool:
            raise ValueError("Recorder evidence must say whether the data was simulated.")
        for name in ("off_block", "takeoff", "landing", "on_block"):
            value = getattr(self, name)
            if value is not None:
                _parse_instant(value, name)
        for name in ("block_s", "airborne_s", "touchdown_rate_fpm"):
            value = getattr(self, name)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(f"{name} must be a finite number, or None when not measured.")
        for name in ("block_s", "airborne_s"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative.")
        if not isinstance(self.gaps, tuple):
            raise ValueError("Recorder evidence gaps must be a tuple of Gap.")
        for gap in self.gaps:
            if not isinstance(gap, Gap) or not isinstance(gap.reason, str) or not gap.reason.strip():
                raise ValueError("Every gap needs a reason.")
            for value, what in ((gap.start, "gap start"), (gap.end, "gap end")):
                if value is not None:
                    _parse_instant(value, what)
            if not isinstance(gap.kind, str):
                raise ValueError("Gap kind must be text.")
        if not isinstance(self.rules_version, str):
            raise ValueError("rules_version must be text.")
        return self

    def as_json(self):
        data = dict(self.extra)
        data.update({
            "journal": self.journal, "summary": self.summary, "time_basis": self.time_basis,
            "simulated": self.simulated, "off_block": self.off_block, "takeoff": self.takeoff,
            "landing": self.landing, "on_block": self.on_block, "block_s": self.block_s,
            "airborne_s": self.airborne_s, "touchdown_rate_fpm": self.touchdown_rate_fpm,
            "gaps": [gap.as_json() for gap in self.gaps], "rules_version": self.rules_version,
        })
        return data


@dataclass(frozen=True, slots=True)
class ImportDetails:
    importer: str
    reference: str = ""


@dataclass(frozen=True, slots=True)
class GapAcknowledgement:
    gap: int
    at: str
    by: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class UnreadableFile:
    """A file in the flights folder that is not shown as a flight, and why."""

    name: str
    kind: str  # "unreadable", "misnamed" or "duplicate"
    reason: str

    def __str__(self):
        return self.name


@dataclass(frozen=True, slots=True)
class Flight:
    id: str
    revisions: tuple[Revision, ...]
    source: str = "manual"
    schema: int = SCHEMA_VERSION
    evidence: RecorderEvidence | None = None
    import_details: ImportDetails | None = None
    acknowledgements: tuple[GapAcknowledgement, ...] = ()
    read_only_reason: str = ""
    problems: tuple[str, ...] = ()
    migrated_from_schema: int | None = None
    extra: Mapping[str, Any] = field(default_factory=_empty_mapping, compare=False)
    file_name: str = field(default="", compare=False)
    digest: str = field(default="", compare=False, repr=False)
    raw: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @property
    def fields(self):
        return self.revisions[-1].fields

    @property
    def read_only(self):
        return bool(self.read_only_reason)

    @property
    def simulated(self):
        return self.evidence is not None and self.evidence.simulated

    def provenance(self, name):
        if name not in FIELD_NAMES:
            raise KeyError(name)
        first = self.revisions[0]
        origin = first.origins.get(name) or DEFAULT_ORIGIN.get(first.by, first.by)
        previous = getattr(first.fields, name)
        corrections, confirmations = [], []
        confirmed = first.by == "pilot" and name in first.confirmed
        for revision in self.revisions[1:]:
            value = getattr(revision.fields, name)
            if value != previous or type(value) is not type(previous):
                corrections.append(Correction(revision.saved_at, previous, value, revision.by, revision.reason))
                previous = value
                confirmed = False
            if name in revision.confirmed and revision.by == "pilot":
                confirmations.append(Confirmation(revision.saved_at, value, revision.by, revision.reason))
                confirmed = True
        return FieldProvenance(name, origin, getattr(first.fields, name), previous, tuple(corrections),
                               tuple(confirmations), confirmed)

    def corrected_fields(self):
        return tuple(name for name in FIELD_NAMES if self.provenance(name).corrected)

    def confirmed_fields(self):
        """Names of fields the pilot confirmed as correct after their last change."""
        return tuple(name for name in FIELD_NAMES if self.provenance(name).confirmed)

    def changed_fields(self, index):
        """Names of fields that revision ``index`` changed from the one before."""
        if index <= 0:
            return ()
        before, after = self.revisions[index - 1].fields, self.revisions[index].fields
        return tuple(
            name for name in FIELD_NAMES
            if getattr(before, name) != getattr(after, name)
            or type(getattr(before, name)) is not type(getattr(after, name))
        )


# Parsing ------------------------------------------------------------------

def _canonical_id(value):
    if not isinstance(value, str):
        raise ValueError("A flight id must be text.")
    return str(UUID(value))


def _parse_fields(raw, problems, latest):
    if not isinstance(raw, dict):
        raise UnreadableRecord("A revision's fields must be an object.")
    values = {}
    for name in FIELD_NAMES:
        if name not in raw:
            if name in ("flight_date",) + REQUIRED_FIELDS:
                raise UnreadableRecord(f"A revision has no {name}.")
            values[name] = None if name in NUMBER_FIELDS else ""
            continue
        value = raw[name]
        if name in NUMBER_FIELDS:
            if value is not None and type(value) is not int:
                raise UnreadableRecord(f"{name} must be a whole number or null, not {value!r}.")
            if latest and value is not None and not 0 <= value <= NUMBER_LIMITS[name]:
                problems.append(f"{FIELD_LABELS[name]} {value:,} is outside the range this version accepts; it is kept as stored.")
        else:
            if not isinstance(value, str):
                raise UnreadableRecord(f"{name} must be text, not {value!r}.")
            if latest and name == "flight_date":
                try:
                    date.fromisoformat(value)
                except ValueError:
                    problems.append(f"Flight date {value!r} is not a valid date; it is kept as stored.")
            if latest and name in TEXT_LIMITS and len(value) > TEXT_LIMITS[name]:
                problems.append(f"{FIELD_LABELS[name]} is longer than this version accepts; it is kept as stored.")
        values[name] = value
    extra = {key: value for key, value in raw.items() if key not in FIELD_NAMES}
    return FlightFields(**values), MappingProxyType(extra)


def _parse_optional_instant(data, name, what):
    value = data.get(name)
    if value is not None:
        _parse_instant(value, what)
    return value


def _parse_number(data, name):
    value = data.get(name)
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise UnreadableRecord(f"Recorder evidence {name} must be a number or null.")
    return value


def _parse_evidence(data):
    if not isinstance(data, dict):
        raise UnreadableRecord("The recorder evidence block must be an object.")
    try:
        gaps = []
        for item in data.get("gaps", []) or []:
            if not isinstance(item, dict) or not isinstance(item.get("reason"), str):
                raise UnreadableRecord("Every recorder gap needs a reason.")
            gaps.append(Gap(
                item["reason"], _parse_optional_instant(item, "start", "gap start"),
                _parse_optional_instant(item, "end", "gap end"), str(item.get("kind", "")),
            ))
        simulated = data.get("simulated")
        if type(simulated) is not bool:
            raise UnreadableRecord("The recorder evidence does not say whether the data was simulated.")
        basis = data.get("time_basis")
        if basis not in TIME_BASES:
            raise UnreadableRecord(f"Unknown recorder time basis {basis!r}.")
        journal, summary = data.get("journal", ""), data.get("summary", "")
        if not isinstance(journal, str) or not isinstance(summary, str):
            raise UnreadableRecord("Recorder journal and summary must be text.")
        known = {
            "journal", "summary", "time_basis", "simulated", "off_block", "takeoff", "landing",
            "on_block", "block_s", "airborne_s", "touchdown_rate_fpm", "gaps", "rules_version",
        }
        return RecorderEvidence(
            journal, summary, basis, simulated,
            _parse_optional_instant(data, "off_block", "off-block time"),
            _parse_optional_instant(data, "takeoff", "takeoff time"),
            _parse_optional_instant(data, "landing", "landing time"),
            _parse_optional_instant(data, "on_block", "on-block time"),
            _parse_number(data, "block_s"), _parse_number(data, "airborne_s"),
            _parse_number(data, "touchdown_rate_fpm"), tuple(gaps),
            str(data.get("rules_version", "")),
            MappingProxyType({key: value for key, value in data.items() if key not in known}),
        )
    except (TypeError, AttributeError) as error:
        raise UnreadableRecord(f"The recorder evidence block is malformed ({error}).") from None


def parse_record(payload, expected_id=None, *, file_name="", digest=""):
    """Turn a decoded flight file into a ``Flight``. Raises UnreadableRecord."""
    if not isinstance(payload, dict):
        raise UnreadableRecord("The file does not hold a flight record object.")
    schema = payload.get("schema")
    if type(schema) is not int or schema < 1:
        raise UnreadableRecord("The file has no valid record format number (schema).")
    read_only = ""
    if schema > SCHEMA_VERSION:
        read_only = (f"This flight was saved by a newer BBC EFB (record format {schema}). "
                     "This version can show it but will not change it.")
    try:
        flight_id = _canonical_id(payload.get("id"))
    except ValueError:
        raise UnreadableRecord("The record's id is not a flight id.") from None
    if expected_id is not None and flight_id != expected_id:
        raise UnreadableRecord("The id inside the file does not match its file name.")
    source = payload.get("source", "manual" if schema == 1 else None)
    if source not in SOURCES:
        if read_only:
            raise UnreadableRecord(read_only.replace("can show it but will not change it", "cannot read it"))
        raise UnreadableRecord(f"Unknown record source {source!r}.")
    items = payload.get("revisions")
    if not isinstance(items, list) or not items:
        raise UnreadableRecord("The record has no revisions.")
    problems = []
    revisions = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise UnreadableRecord("A revision is not an object.")
        saved_at = item.get("saved_at")
        _parse_instant(saved_at)
        by = item.get("by", DEFAULT_AUTHOR[source] if index == 0 else "pilot")
        reason = item.get("reason", "")
        origins = item.get("origins", {})
        if not isinstance(by, str) or not isinstance(reason, str):
            raise UnreadableRecord("A revision's author and reason must be text.")
        if not isinstance(origins, dict) or not all(isinstance(v, str) for v in origins.values()):
            raise UnreadableRecord("A revision's origins must map field names to text.")
        fields, extra = _parse_fields(item.get("fields"), problems, index == len(items) - 1)
        confirmed = item.get("confirmed", [])
        if not isinstance(confirmed, list) or not all(isinstance(name, str) for name in confirmed):
            problems.append("A revision's list of confirmed fields is malformed; it is kept as stored and ignored.")
            confirmed = []
        confirmed = tuple(dict.fromkeys(name for name in confirmed if name in FIELD_NAMES))
        revisions.append(Revision(saved_at, fields, by, reason, MappingProxyType(dict(origins)), extra, confirmed))
    evidence = None
    if payload.get("recorder") is not None:
        evidence = _parse_evidence(payload["recorder"])
    elif source == "recorder":
        problems.append("The Recorder evidence block is missing.")
    import_details = None
    if isinstance(payload.get("import"), dict):
        details = payload["import"]
        import_details = ImportDetails(str(details.get("importer", "")), str(details.get("reference", "")))
    acknowledgements = []
    raw_acks = payload.get("acknowledgements", [])
    if not isinstance(raw_acks, list):
        raise UnreadableRecord("Gap acknowledgements must be a list.")
    for item in raw_acks:
        if not isinstance(item, dict) or type(item.get("gap")) is not int:
            raise UnreadableRecord("A gap acknowledgement is malformed.")
        _parse_instant(item.get("at"), "acknowledgement time")
        acknowledgements.append(GapAcknowledgement(item["gap"], item["at"], str(item.get("by", "pilot")), str(item.get("note", ""))))
    migrated = payload.get("migrated_from")
    migrated_schema = migrated.get("schema") if isinstance(migrated, dict) else None
    extra = dict(payload["extra"]) if isinstance(payload.get("extra"), dict) else {}
    extra.update({key: value for key, value in payload.items() if key not in _KNOWN_TOP})
    return Flight(
        flight_id, tuple(revisions), source, schema, evidence, import_details,
        tuple(acknowledgements), read_only, tuple(problems),
        migrated_schema if type(migrated_schema) is int else None,
        MappingProxyType(extra), file_name, digest, payload,
    )


def _decode(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UnreadableRecord("The file is not UTF-8 text.") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise UnreadableRecord(f"The file is not valid JSON (line {error.lineno}, column {error.colno}).") from None
    except RecursionError:
        raise UnreadableRecord("The file is nested too deeply to read.") from None


def _encode(payload):
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        data = (text + "\n").encode("utf-8")
    except UnicodeEncodeError:
        # Stored text with lone surrogates (only possible from hand edits) is kept as \u escapes.
        data = (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode("ascii")
    return data


def _revision_json(revision):
    data = {"saved_at": revision.saved_at, "by": revision.by}
    if revision.reason:
        data["reason"] = revision.reason
    if revision.origins:
        data["origins"] = dict(revision.origins)
    if revision.confirmed:
        data["confirmed"] = list(revision.confirmed)
    data["fields"] = {**dict(revision.extra), **revision.fields.as_json()}
    return data


def _sort_key(flight):
    flight_date = flight.fields.flight_date
    return (flight_date if isinstance(flight_date, str) else "",
            flight.revisions[0].saved_at_utc.isoformat(), flight.id)


# File operations ----------------------------------------------------------

_sleep = time.sleep


def _is_transient_replace_error(error):
    return getattr(error, "winerror", None) in _RETRY_WINERRORS


def _replace_with_retry(source, target):
    """os.replace, retried briefly while Windows reports a sharing or access error."""
    for attempt, delay in enumerate((*_RETRY_DELAYS_S, None)):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if not _is_transient_replace_error(error):
                raise
            if delay is None:
                raise FlightFileBusy(
                    f"Another program may be using {Path(target).name} (for example OneDrive, "
                    "antivirus or a backup tool), or the file is read-only, so it could not be "
                    "replaced. Nothing was changed. Try again in a moment."
                ) from error
            log.info("Replacing %s failed (%s); retry %d", target, error, attempt + 1)
            _sleep(delay)


def _name_rank(name):
    """Order for file names that differ only in case: the exact lower-case (canonical)
    spelling first, then a fixed order, so ``listing()`` and ``_find()`` agree."""
    return (name.lower(), name != name.lower(), name)


def _stat_key(stat):
    # DirEntry.stat() reports st_ino as 0 on Windows, so it is only used elsewhere.
    return (stat.st_mtime_ns, stat.st_size, 0 if os.name == "nt" else stat.st_ino)


def _fsync_directory(path):
    if os.name == "nt":
        return  # NTFS journals the rename; directories cannot be opened for fsync.
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_atomically(path, data):
    temporary = None
    try:
        with NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".flight-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# errno values that mean "someone else holds the lock". Anything else (for example
# ENOTSUP or ENOLCK on a share without lock support) is a folder problem, not contention.
_CONTENTION_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None) for name in ("EWOULDBLOCK", "EAGAIN", "EACCES", "EDEADLOCK", "EDEADLK"))
    if code is not None
)


def _is_lock_contention(error):
    if getattr(error, "winerror", None) in (33, 36):  # lock violation, sharing buffer exceeded
        return True
    return error.errno in _CONTENTION_ERRNOS


def _lock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as error:
        log.warning("Releasing the data folder lock failed: %s", error)


# Store --------------------------------------------------------------------

class FlightStore:
    """One JSON file per flight in ``root/flights``.

    ``FlightStore(root)`` opens the folder for writing and holds its writer lock
    until ``close()``. Pass ``create=True`` only when the pilot has just chosen a
    new folder; otherwise a missing folder raises ``DataFolderMissing`` instead of
    silently starting an empty logbook. ``read_only=True`` never locks or writes.
    """

    def __init__(self, root, *, create=False, read_only=False):
        root = Path(root)
        if create and not read_only:
            try:
                root.mkdir(parents=True, exist_ok=True)
            except PermissionError as error:
                raise DataFolderNotWritable(
                    f"BBC EFB cannot create the data folder {root}: {error.strerror or error}."
                ) from error
        if not root.is_dir():
            raise DataFolderMissing(
                f"The data folder {root} was not found. Reconnect its drive, or choose the folder again in Settings."
            )
        self.root = root
        self.records = root / "flights"
        self.read_only = read_only
        self._mutex = threading.RLock()       # serialises compare-and-write
        self._cache_lock = threading.Lock()   # guards _cache only; never held during I/O
        self._lock_handle = None
        self._cache = {}
        if read_only:
            return
        self._acquire_writer_lock()
        try:
            try:
                self.records.mkdir(exist_ok=True)
            except PermissionError as error:
                raise DataFolderNotWritable(
                    f"BBC EFB cannot write to the data folder {root}: {error.strerror or error}."
                ) from error
            self._probe()
            self._sweep_leftovers()
        except BaseException:
            self.close()
            raise

    # Lifetime

    def _acquire_writer_lock(self):
        path = self.root / LOCK_NAME
        try:
            handle = open(path, "a+b")
        except PermissionError as error:
            raise DataFolderNotWritable(
                f"BBC EFB cannot write to the data folder {self.root}: {error.strerror or error}."
            ) from error
        try:
            _lock(handle)
        except OSError as error:
            handle.close()
            if _is_lock_contention(error):
                raise StoreLocked(
                    f"Another BBC EFB window or program is already saving flights in {self.root}. "
                    "Close it and try again, or open this folder read-only."
                ) from error
            # Some network shares and sync folders do not support file locks at all
            # (ENOTSUP, ENOLCK ...). That is not another writer, so say what it is.
            raise DataFolderNotWritable(
                f"The data folder {self.root} does not support the file locking BBC EFB needs to "
                f"keep flights safe ({error.strerror or error}). Choose a folder on a local drive."
            ) from error
        self._lock_handle = handle

    def close(self):
        """Release the writer lock. The store cannot save afterwards."""
        with self._mutex:
            handle, self._lock_handle = self._lock_handle, None
            if handle is not None:
                _unlock(handle)
                handle.close()

    @property
    def writable(self):
        return self._lock_handle is not None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _probe(self):
        """Prove the folder accepts create, fsync, replace-over-existing and delete."""
        first = second = None
        try:
            for name in ("first", "second"):
                with NamedTemporaryFile(mode="wb", dir=self.records, prefix=".writable-", suffix=".tmp", delete=False) as handle:
                    if name == "first":
                        first = Path(handle.name)
                    else:
                        second = Path(handle.name)
                    handle.write(b"BBC EFB write test\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            _replace_with_retry(first, second)
            first = None
        except OSError as error:
            raise DataFolderNotWritable(
                f"BBC EFB cannot save files in {self.records}: {getattr(error, 'strerror', None) or error}."
            ) from error
        finally:
            for path in (first, second):
                if path is not None:
                    path.unlink(missing_ok=True)

    def _sweep_leftovers(self):
        """Remove temporary files a crashed writer left behind (we hold the lock)."""
        now = time.time()
        for pattern in (".flight-*.tmp", ".writable-*.tmp"):
            for path in self.records.glob(pattern):
                try:
                    if now - path.stat().st_mtime >= LEFTOVER_AGE_S:
                        path.unlink()
                        log.info("Removed leftover temporary file %s", path)
                except OSError as error:
                    log.warning("Could not remove leftover temporary file %s: %s", path, error)

    def _require_writer(self):
        if self.read_only:
            raise StoreReadOnly("This data folder is open read-only, so changes cannot be saved.")
        if self._lock_handle is None:
            raise StoreReadOnly("This data folder has been closed, so changes cannot be saved.")

    # Reading

    def _path(self, flight_id):
        return self.records / f"{_canonical_id(flight_id)}.json"

    def _find(self, canonical):
        """The file holding ``canonical``: the same one ``listing()`` shows for that id."""
        path = self.records / f"{canonical}.json"
        if path.is_file():
            return path
        matches = []
        try:
            for entry in os.scandir(self.records):
                if entry.name.lower() == f"{canonical}.json" and entry.is_file():
                    matches.append(entry.name)
        except FileNotFoundError:
            pass
        return self.records / min(matches, key=_name_rank) if matches else None

    def _read(self, path, canonical):
        size = path.stat().st_size
        if size > MAX_RECORD_BYTES:
            raise UnreadableRecord("The file is too large to be a flight record.")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        flight = parse_record(_decode(data), canonical, file_name=path.name, digest=digest)
        return flight, data

    def _read_for_listing(self, path, canonical):
        """``_read`` retried once after a short pause for a Windows sharing or lock error."""
        try:
            return self._read(path, canonical)[0]
        except PermissionError as error:
            if not _is_transient_replace_error(error):
                raise
            _sleep(_RETRY_DELAYS_S[0])
            return self._read(path, canonical)[0]

    def get(self, flight_id):
        canonical = _canonical_id(flight_id)
        path = self._find(canonical)
        if path is None:
            raise RecordMissing(
                "This flight is not in the current data folder. It may have been moved or deleted."
            )
        try:
            return self._read(path, canonical)[0]
        except (KeyError, TypeError, AttributeError) as error:
            raise UnreadableRecord(f"The record is not in the expected format ({error}).") from None

    def listing(self):
        """Return ``(flights, unreadable)``, newest flight date first.

        ``unreadable`` lists ``UnreadableFile`` entries (name, kind, reason) for
        files that are not shown. Files whose modification time, size and inode
        are unchanged since the last call are not read again.
        """
        # No write mutex here: a writer retrying a busy file (OneDrive, antivirus) must not
        # stall the UI's listing. Files are replaced atomically, so a scan sees either the
        # old or the new file; only the cache is shared, under its own small lock.
        flights, unreadable, seen, live = [], [], {}, set()
        try:
            entries = sorted(os.scandir(self.records), key=lambda entry: _name_rank(entry.name))
        except FileNotFoundError:
            entries = []
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not name.lower().endswith(".json"):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError as error:
                unreadable.append(UnreadableFile(name, "unreadable", f"The file cannot be opened ({error.strerror or error})."))
                continue
            stem = name[:-5]
            try:
                canonical = _canonical_id(stem)
            except ValueError:
                unreadable.append(UnreadableFile(
                    name, "misnamed",
                    "The file name is not a flight id. It may be a copy made by a sync or backup "
                    "tool; compare it with the original before removing either.",
                ))
                continue
            if stem.lower() != canonical:
                unreadable.append(UnreadableFile(
                    name, "misnamed", f"The file name is spelled differently from its flight id. Rename it to {canonical}.json.",
                ))
                continue
            if canonical in seen:
                unreadable.append(UnreadableFile(
                    name, "duplicate", f"{seen[canonical]} holds the same flight id. Compare the two files before removing either.",
                ))
                continue
            seen[canonical] = name
            live.add(name)
            key = _stat_key(stat)
            with self._cache_lock:
                cached = self._cache.get(name)
            if cached is not None and cached[0] == key:
                result = cached[1]
            else:
                # Only results computed from bytes actually read are cached. An OSError
                # (for example a sharing violation while antivirus or OneDrive holds the
                # file) is transient and leaves mtime/size unchanged, so caching it would
                # hide the flight for the rest of the session.
                cacheable = True
                try:
                    result = self._read_for_listing(Path(entry.path), canonical)
                except UnreadableRecord as error:
                    result = UnreadableFile(name, "unreadable", error.reason)
                except OSError as error:
                    cacheable = False
                    result = UnreadableFile(
                        name, "unreadable",
                        f"The file cannot be read right now ({error.strerror or error}). Another program "
                        "may be using it; it is read again at the next refresh.",
                    )
                except (KeyError, TypeError, AttributeError, ValueError, RecursionError) as error:
                    result = UnreadableFile(name, "unreadable", f"The file cannot be read ({error}).")
                if isinstance(result, UnreadableFile):
                    log.warning("Flight file %s not shown: %s", name, result.reason)
                with self._cache_lock:
                    if cacheable:
                        self._cache[name] = (key, result)
                    else:
                        self._cache.pop(name, None)
            (unreadable if isinstance(result, UnreadableFile) else flights).append(result)
        with self._cache_lock:
            for name in set(self._cache) - live:
                del self._cache[name]
        flights.sort(key=_sort_key, reverse=True)
        return flights, unreadable

    # Writing

    def save(self, fields, existing=None, *, reason="", by="pilot"):
        """Create a manual flight (``existing`` is None) or save an edit as a new revision.

        For an edit, a value equal to the stored one is kept exactly as stored;
        only changed values are normalised and checked. Saving with no change
        returns the stored flight and writes nothing.
        """
        if existing is None:
            return self._create("manual", fields.validated(), by=by, reason=reason)
        return self._update(existing, fields, reason=reason, by=by)

    def create_recorded(self, fields, evidence, *, origins=None, reason=""):
        """Create a Recorder flight. Values are stored exactly as given (empty or None = unknown).

        May block for about a second while a busy file is retried: call it from a worker
        thread, never from the Core pump thread.
        """
        if not isinstance(evidence, RecorderEvidence):
            raise TypeError("evidence must be RecorderEvidence.")
        evidence.checked()
        fields = FlightFields(**{name: _check_capture_field(name, getattr(fields, name)) for name in FIELD_NAMES})
        return self._create("recorder", fields, by="recorder", reason=reason, origins=origins,
                            top={"recorder": evidence.as_json()})

    def create_imported(self, fields, *, importer, reference="", origins=None, extra=None, reason=""):
        """Create an imported flight. ``extra`` keeps importer data this version does not model."""
        if not isinstance(importer, str) or not importer.strip():
            raise ValueError("Name the importer that produced this flight.")
        if not isinstance(reference, str):
            raise ValueError("The import reference must be text.")
        fields = FlightFields(**{name: _check_capture_field(name, getattr(fields, name)) for name in FIELD_NAMES})
        top = {"import": {"importer": importer, "reference": reference}}
        if extra:
            top["extra"] = dict(extra)
        return self._create("imported", fields, by="import", reason=reason, origins=origins, top=top)

    def correct(self, flight, changes, *, reason, by="pilot"):
        """Apply a correction to some fields of ``flight`` as a new revision."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Give a reason for the correction.")
        unknown = set(changes) - set(FIELD_NAMES)
        if unknown:
            raise ValueError(f"Unknown flight field(s): {', '.join(sorted(unknown))}.")
        return self._update(flight, replace(flight.fields, **changes), reason=reason.strip(), by=by)

    def confirm(self, flight, names, *, reason="", diversion=None):
        """Record that the pilot checked ``names`` and their current values are correct.

        Nothing is changed: a new pilot revision repeats the current values exactly as
        stored and lists the fields in its ``confirmed`` entry, so the confirmation is part
        of the flight's history. ``diversion`` optionally records the pilot's answer to a
        suspected diversion beside the fields (``{"confirmed": bool, ...}`` with text
        values). Confirming fields that are already confirmed, with no diversion answer,
        writes nothing and returns the stored flight.
        """
        if isinstance(names, str):
            names = (names,)
        names = tuple(dict.fromkeys(names))
        if not names:
            raise ValueError("Name the fields to confirm.")
        unknown = [name for name in names if name not in FIELD_NAMES]
        if unknown:
            raise ValueError(f"Unknown flight field(s): {', '.join(sorted(map(str, unknown)))}.")
        if not isinstance(reason, str):
            raise ValueError("The reason must be text.")
        extra = self._checked_confirmation_extra(diversion)
        with self._mutex:
            self._require_writer()
            current, data, path = self._current(flight)
            if current.read_only:
                raise RecordReadOnly(current.read_only_reason)
            if not extra and all(current.provenance(name).confirmed for name in names):
                return current
            labels = ", ".join(FIELD_LABELS[name] for name in names)
            reason = reason.strip() or f"Pilot confirmed {labels} as correct."
            payload = self._payload_for_write(current, data)
            carried = {**dict(current.revisions[-1].extra), **extra}
            revision = Revision(_utcnow(), current.fields, "pilot", reason, _EMPTY,
                                MappingProxyType(carried), names)
            payload["revisions"].append(_revision_json(revision))
            return self._commit(path, payload, current.id)

    @staticmethod
    def _checked_confirmation_extra(diversion):
        if diversion is None:
            return {}
        if not isinstance(diversion, Mapping) or type(diversion.get("confirmed")) is not bool:
            raise ValueError("A diversion answer must say whether it was a diversion (confirmed: true or false).")
        answer = {}
        for key, value in diversion.items():
            if not isinstance(key, str) or not (value is None or isinstance(value, (bool, str))):
                raise ValueError("A diversion answer holds only text, true/false or empty values.")
            if isinstance(value, str):
                _check_text_encodable("notes", value)
                if len(value) > TEXT_LIMITS["notes"]:
                    raise ValueError("The diversion answer is too long.")
            answer[key] = value
        return {"diversion": answer}

    def acknowledge_gap(self, flight, gap, *, note="", by="pilot"):
        """Record that the pilot has seen a Recorder gap. The evidence is not changed."""
        if by not in AUTHORS:
            raise ValueError(f"Unknown author {by!r}.")
        if not isinstance(note, str):
            raise ValueError("The note must be text.")
        with self._mutex:
            self._require_writer()
            current, data, path = self._current(flight)
            if current.read_only:
                raise RecordReadOnly(current.read_only_reason)
            if current.evidence is None or type(gap) is not int or not 0 <= gap < len(current.evidence.gaps):
                raise ValueError("That gap is not part of this flight's Recorder evidence.")
            if any(item.gap == gap for item in current.acknowledgements):
                return current
            payload = self._payload_for_write(current, data)
            payload.setdefault("acknowledgements", []).append(
                {"gap": gap, "at": _utcnow(), "by": by, "note": note.strip()}
            )
            return self._commit(path, payload, current.id)

    def _create(self, source, fields, *, by, reason, origins=None, top=None):
        if by not in AUTHORS:
            raise ValueError(f"Unknown author {by!r}.")
        if not isinstance(reason, str):
            raise ValueError("The reason must be text.")
        origins = self._checked_origins(origins)
        with self._mutex:
            self._require_writer()
            flight_id = str(uuid4())
            revision = Revision(_utcnow(), fields, by, reason.strip(), origins)
            payload = {"schema": SCHEMA_VERSION, "id": flight_id, "source": source}
            payload.update(top or {})
            payload["revisions"] = [_revision_json(revision)]
            return self._commit(self.records / f"{flight_id}.json", payload, flight_id)

    @staticmethod
    def _checked_origins(origins):
        origins = dict(origins or {})
        for name, origin in origins.items():
            if name not in FIELD_NAMES:
                raise ValueError(f"Unknown flight field {name!r} in origins.")
            if origin not in ORIGINS:
                raise ValueError(f"Unknown origin {origin!r}; use one of {', '.join(ORIGINS)}.")
        return MappingProxyType(origins)

    def _current(self, existing):
        """Re-read ``existing`` from disk and refuse if it changed since it was read."""
        canonical = _canonical_id(existing.id)
        path = self._find(canonical)
        if path is None:
            raise RecordMissing(
                "This flight is not in the current data folder. It may have been moved or deleted."
            )
        try:
            current, data = self._read(path, canonical)
        except (KeyError, TypeError, AttributeError) as error:
            raise UnreadableRecord(f"The record is not in the expected format ({error}).") from None
        changed = current.digest != existing.digest if existing.digest else current != existing
        if changed:
            raise RecordConflict(
                "This flight changed on disk after you opened it (another window or program saved it). "
                "Reopen it to see the latest version."
            )
        return current, data, path

    def _update(self, existing, fields, *, reason, by):
        if by not in AUTHORS:
            raise ValueError(f"Unknown author {by!r}.")
        if not isinstance(reason, str):
            raise ValueError("The reason must be text.")
        with self._mutex:
            self._require_writer()
            current, data, path = self._current(existing)
            if current.read_only:
                raise RecordReadOnly(current.read_only_reason)
            merged, changed = _merge(current.fields, fields)
            if not changed:
                return current
            payload = self._payload_for_write(current, data)
            revision = Revision(_utcnow(), merged, by, reason.strip(), _EMPTY, current.revisions[-1].extra)
            payload["revisions"].append(_revision_json(revision))
            return self._commit(path, payload, current.id)

    def _payload_for_write(self, current, data):
        payload = copy.deepcopy(dict(current.raw))
        if current.schema < SCHEMA_VERSION:
            backup = self._keep_original(current, data)
            payload["schema"] = SCHEMA_VERSION
            payload["source"] = current.source
            payload["migrated_from"] = {"schema": current.schema, "at": _utcnow(), "original_copy": backup}
        return payload

    def _keep_original(self, current, data):
        folder = self.root / MIGRATION_FOLDER / f"schema-{current.schema}"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{current.id}.json"
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != current.digest:
            target = folder / f"{current.id}-{current.digest[:12]}.json"
        if not target.exists():
            _write_atomically(target, data)
            log.info("Kept the schema %d original of %s at %s", current.schema, current.id, target)
        return target.relative_to(self.root).as_posix()

    def _commit(self, path, payload, flight_id):
        data = _encode(payload)
        flight = parse_record(json.loads(data.decode("utf-8")), flight_id, file_name=path.name,
                              digest=hashlib.sha256(data).hexdigest())
        _write_atomically(path, data)
        try:
            stat = path.stat()
            with self._cache_lock:
                self._cache[path.name] = (_stat_key(stat), flight)
        except OSError:
            with self._cache_lock:
                self._cache.pop(path.name, None)
        return flight


def _merge(base, new):
    """Keep unchanged values exactly as stored; normalise and check changed ones."""
    values, changed = {}, []
    for name in FIELD_NAMES:
        stored, value = getattr(base, name), getattr(new, name)
        if value == stored and type(value) is type(stored):
            values[name] = stored
            continue
        value = _normalise_field(name, value)
        if value == stored and type(value) is type(stored):
            values[name] = stored
            continue
        if name in REQUIRED_FIELDS and not value:
            raise ValueError("Aircraft type, departure and destination are required.")
        values[name] = value
        changed.append(name)
    return FlightFields(**values), tuple(changed)
