"""Pilot-controlled CSV exchange and full Logbook backups."""

import csv
from collections import defaultdict
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .storage import Flight, FlightFields, FlightStore, RecordConflict


EXCHANGE_FIELDS = tuple(field.name for field in fields(FlightFields) if field.name != "aircraft_code")
INTEGER_FIELDS = {"minutes", "crew", "passengers"}
DECIMAL_FIELDS = {"planned_nm", "flown_nm", "baggage_kg", "freight_kg"}
IDENTITY_FIELDS = ("departure", "arrival", "callsign", "registration")


@dataclass(frozen=True)
class ImportChange:
    existing: Flight | None
    fields: FlightFields
    reference: tuple[str, str]
    rate: float | None
    rate_source: str


def read_records(store):
    records, unreadable = store.listing()
    if unreadable:
        raise RecordConflict("Repair or recover unreadable records first: " + ", ".join(unreadable))
    return tuple(records)


def _blank(value):
    return value is None or isinstance(value, str) and value.casefold() in ("", "unknown")


def _match(incoming, candidates):
    matches = []
    possible = False
    for candidate, identity in candidates:
        if incoming.aircraft.casefold() != candidate.aircraft.casefold() or incoming.flight_date != candidate.flight_date:
            continue
        pairs = [(getattr(incoming, key), getattr(candidate, key)) for key in IDENTITY_FIELDS]
        comparisons = [left == right for left, right in pairs if not _blank(left) and not _blank(right)]
        if comparisons and not all(comparisons):
            continue
        if incoming.real_departure and candidate.real_departure:
            delta = datetime.fromisoformat(incoming.real_departure) - datetime.fromisoformat(candidate.real_departure)
            if abs(delta.total_seconds()) <= 600:
                if comparisons:
                    matches.append(identity)
                else:
                    possible = True
        elif comparisons:
            possible = True
    return matches, possible


def preview_csv(store, path):
    records = read_records(store)
    candidates = [(flight.fields, flight) for flight in records]
    by_reference = {ref: flight for flight in records for ref in flight.import_refs}
    by_id = {flight.id: flight for flight in records}
    claimed = set()
    seen = set()
    new_fields = []
    results, changes = [], []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"flight_date", "aircraft"} <= set(reader.fieldnames):
            raise ValueError("CSV must include the flight_date and aircraft columns. Export a CSV to see all supported columns.")
        for number, raw in enumerate(reader, 2):
            try:
                if None in raw:
                    raise ValueError("There are more values than column headers.")
                values = {name: (raw.get(name) or "").strip() for name in EXCHANGE_FIELDS}
                for name in INTEGER_FIELDS | DECIMAL_FIELDS:
                    convert = int if name in INTEGER_FIELDS else float
                    values[name] = None if _blank(values[name]) else convert(values[name])
                for name in ("departure", "arrival"):
                    values[name] = values[name] or "UNKNOWN"
                incoming = FlightFields(**values).validated()
                raw_rate = (raw.get("landing_rate_fpm") or "").strip()
                rate = None if _blank(raw_rate) else float(raw_rate)
                if rate is not None and (not isfinite(rate) or rate > 0):
                    raise ValueError("Landing rate must be a finite value of zero or less in fpm.")
                source = (raw.get("source") or "").strip() or "CSV"
                rate_source = (raw.get("landing_rate_source") or "").strip() or source
                source_id = (raw.get("source_id") or "").strip()
                if not source_id:
                    normalized = json.dumps([asdict(incoming), rate], sort_keys=True)
                    source_id = sha256(normalized.encode("utf-8")).hexdigest()
                reference = (source, source_id)
                if reference in by_reference or reference in seen:
                    results.append((number, "Duplicate", f"{source} · already accounted for"))
                    continue
                match = by_id.get(source_id) if source == "BBC EFB" else None
                if match is None:
                    matches, possible = _match(incoming, candidates)
                    repeats, similar = _match(incoming, ((item, item) for item in new_fields))
                    if len(matches) > 1 or possible or repeats or similar:
                        results.append((number, "Review", "More than one possible report or missing matching time"))
                        continue
                    if matches:
                        match = matches[0]
                if match is not None and match.id in claimed:
                    results.append((number, "Review", "Another row updates this flight. Preview again after importing."))
                    continue
                if match is not None and source == "BBC EFB":
                    results.append((number, "Duplicate", "This BBC EFB flight is already saved"))
                    continue
                if match is None:
                    new_fields.append(incoming)
                    result = "Add flight"
                else:
                    fill = {}
                    conflicts = []
                    for name in EXCHANGE_FIELDS:
                        old, new = getattr(match.fields, name), getattr(incoming, name)
                        if _blank(old) and not _blank(new):
                            fill[name] = new
                        elif not _blank(old) and not _blank(new) and old != new:
                            conflicts.append(name)
                    incoming = replace(match.fields, **fill).validated()
                    if match.landing_rate is not None:
                        if rate is not None and rate != match.landing_rate:
                            conflicts.append("landing rate")
                        rate, rate_source = match.reported_landing_rate, match.reported_landing_source
                    result = "Fill blanks" if fill or rate != match.reported_landing_rate else "Link report"
                    if conflicts:
                        result += " · keep saved " + ", ".join(name.replace("_", " ") for name in conflicts)
                    claimed.add(match.id)
                if rate is None:
                    rate_source = ""
                changes.append(ImportChange(match, incoming, reference, rate, rate_source))
                seen.add(reference)
                results.append((number, result, f"{incoming.flight_date} · {incoming.aircraft} · {incoming.departure} → {incoming.arrival}"))
            except (ValueError, TypeError) as error:
                results.append((number, "Invalid", str(error)))
    return records, results, changes


def export_csv(store, path):
    if path.resolve().is_relative_to(store.records.resolve()):
        raise ValueError("Choose a location outside the managed flights folder.")
    records = read_records(store)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=path.parent,
                                delete=False) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, (*EXCHANGE_FIELDS, "source", "source_id", "record_origin",
                                             "status", "landing_rate_fpm", "landing_rate_source"))
            writer.writeheader()
            for flight in records:
                values = asdict(flight.fields)
                values.pop("aircraft_code")
                captured = flight.recording and flight.recording.landing_rate_fpm is not None
                writer.writerow(dict(values, source="BBC EFB", source_id=flight.id, record_origin=flight.source,
                                     status=flight.status, landing_rate_fpm=flight.landing_rate,
                                     landing_rate_source=flight.recording.provider if captured else flight.reported_landing_source))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(records)


def write_backup(store, path):
    if path.resolve().is_relative_to(store.records.resolve()):
        raise ValueError("Choose a location outside the managed flights folder.")
    with store._locked():
        records, unreadable = store._listing()
        if unreadable:
            raise RecordConflict("Repair or recover unreadable records first: " + ", ".join(unreadable))
        replay = store.replay_batches()
    FlightStore._write_json(path, {"format": "BBC EFB Logbook backup", "schema": 1,
                                   "flights": [asdict(flight) for flight in records], "replay": replay})
    return len(records)


def restore_backup(path, destination):
    if any(destination.iterdir()):
        raise ValueError("Choose an empty folder for the restored Logbook.")
    payload = FlightStore._read_json(path)
    if payload["format"] != "BBC EFB Logbook backup" or payload["schema"] != 1:
        raise ValueError("This file is not a supported BBC EFB Logbook backup.")
    years = defaultdict(dict)
    ids = set()
    for record in payload["flights"]:
        flight = FlightStore._decode_flight(record)
        if flight.id in ids:
            raise ValueError("The backup contains a repeated flight ID.")
        ids.add(flight.id)
        years[flight.fields.flight_date[:4]][flight.id] = flight
    with TemporaryDirectory(prefix=".restore-", dir=destination) as folder:
        store = FlightStore(Path(folder))
        for year, flights in years.items():
            store._write_year(year, flights)
        for name, contents in payload.get("replay", {}).items():
            if not re.fullmatch(r"replay-[0-9]{4}-Q[1-4]\.jsonl", name):
                raise ValueError("The backup contains an invalid replay filename.")
            for line in contents.splitlines():
                batch = json.loads(line)
                if batch["flight_id"] not in ids or not isinstance(batch["samples"], list):
                    raise ValueError("The backup contains an invalid replay batch.")
            (store.records / name).write_text(contents, encoding="utf-8", newline="\n")
        if any(item != Path(folder) for item in destination.iterdir()):
            raise ValueError("The restore folder is no longer empty.")
        store.records.rename(destination / "flights")
    return len(ids)
