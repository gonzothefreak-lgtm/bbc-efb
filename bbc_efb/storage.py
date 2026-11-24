"""Local, versioned manual Logbook records."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4


class RecordConflict(ValueError):
    """The record changed since it was opened."""


@dataclass(frozen=True)
class FlightFields:
    flight_date: str
    aircraft: str
    departure: str
    arrival: str
    callsign: str = ""
    registration: str = ""
    minutes: int | None = None
    notes: str = ""

    def validated(self):
        if self.minutes is not None and (
            type(self.minutes) is not int or not 0 <= self.minutes <= 9999
        ):
            raise ValueError("Enter a duration from 0 to 9,999 minutes.")
        flight_date = date.fromisoformat(self.flight_date).isoformat()
        aircraft = self.aircraft.strip().upper()
        departure = self.departure.strip().upper()
        arrival = self.arrival.strip().upper()
        if not all((aircraft, departure, arrival)):
            raise ValueError("Aircraft type, departure and destination are required.")
        return FlightFields(
            flight_date, aircraft, departure, arrival, self.callsign.strip().upper(),
            self.registration.strip().upper(), self.minutes, self.notes.strip(),
        )


@dataclass(frozen=True)
class Revision:
    saved_at: str
    fields: FlightFields


@dataclass(frozen=True)
class Flight:
    id: str
    revisions: tuple[Revision, ...]

    @property
    def fields(self):
        return self.revisions[-1].fields


class FlightStore:
    """Use one JSON file per flight. The desktop holds the folder's writer lock."""

    def __init__(self, root: Path):
        self.root = root
        self.records = root / "flights"
        self.records.mkdir(exist_ok=True)
        with NamedTemporaryFile(prefix=".writable-", dir=self.records):
            pass

    def _path(self, flight_id):
        return self.records / f"{UUID(flight_id)}.json"

    def get(self, flight_id):
        path = self._path(flight_id)
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload["schema"] != 1 or payload["source"] != "manual" or payload["id"] != flight_id:
            raise ValueError("Unsupported flight record.")
        revisions = tuple(
            Revision(item["saved_at"], FlightFields(**item["fields"]).validated())
            for item in payload["revisions"]
        )
        if not revisions:
            raise ValueError("The record has no revisions.")
        return Flight(flight_id, revisions)

    def listing(self):
        flights = []
        unreadable = []
        for path in sorted(self.records.glob("*.json")):
            try:
                flights.append(self.get(path.stem))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                unreadable.append(path.name)
        flights.sort(key=lambda flight: (flight.fields.flight_date, flight.revisions[0].saved_at),
                     reverse=True)
        return flights, unreadable

    def save(self, fields, existing=None):
        fields = fields.validated()
        if existing is not None:
            current = self.get(existing.id)
            if current != existing:
                raise RecordConflict("This flight changed. Reopen it before saving your edits.")
            if current.fields == fields:
                return current
        revision = Revision(datetime.now(timezone.utc).isoformat(timespec="seconds"), fields)
        flight = Flight(
            str(uuid4()) if existing is None else existing.id,
            (revision,) if existing is None else existing.revisions + (revision,),
        )
        path = self._path(flight.id)
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.records, prefix=".flight-",
                suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    {"schema": 1, "source": "manual", **asdict(flight)}, handle,
                    ensure_ascii=False, indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return flight
