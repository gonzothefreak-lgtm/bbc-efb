"""Versioned yearly Logbook files and recoverable manual edits."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
import json
from math import isfinite
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
class FlightRecording:
    provider: str
    simulated: bool
    gate_out: str
    takeoff: str
    touchdown: str
    gate_in: str
    block_seconds: float
    airborne_seconds: float
    landing_rate_fpm: float | None = None

    def validated(self):
        times = [datetime.fromisoformat(value) for value in (
            self.gate_out, self.takeoff, self.touchdown, self.gate_in,
        )]
        if any(value.utcoffset() is None for value in times) or times != sorted(times):
            raise ValueError("Recorded simulator event times must be ordered and have a time zone.")
        durations = (self.airborne_seconds, self.block_seconds)
        if any(type(value) not in (float, int) or not isfinite(value) for value in durations) or not (
            0 <= self.airborne_seconds <= self.block_seconds
        ):
            raise ValueError("Invalid recorded flight durations.")
        rate = self.landing_rate_fpm
        if (rate is not None and (type(rate) not in (float, int) or not isfinite(rate) or rate > 0)
                or not isinstance(self.provider, str) or not self.provider.strip()
                or type(self.simulated) is not bool):
            raise ValueError("Invalid recording source or landing rate.")
        return self


@dataclass(frozen=True)
class Flight:
    id: str
    revisions: tuple[Revision, ...]
    source: str = "manual"
    status: str = "complete"
    recording: FlightRecording | None = None

    @property
    def fields(self):
        return self.revisions[-1].fields


class FlightStore:
    """Store current records by year and keep existing per-flight files readable."""

    def __init__(self, root: Path):
        self.root = root
        self.records = root / "flights"
        self.records.mkdir(exist_ok=True)
        self._pending_move = self.records / ".year-move.json"
        with NamedTemporaryFile(prefix=".writable-", dir=self.records):
            pass

    @contextmanager
    def _locked(self):
        # Keep the lock file in place. The OS releases the lock on process exit.
        with (self.records / ".store.lock").open("a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise OSError("The Logbook folder is busy. Try again shortly.") from error
            self._recover_year_move()
            yield

    def _year_path(self, year):
        return self.records / f"flights-{year}.json"

    def _year_paths(self):
        return sorted(self.records.glob("flights-[0-9][0-9][0-9][0-9].json"))

    @staticmethod
    def _decode_flight(payload):
        flight_id = payload["id"]
        if str(UUID(flight_id)) != flight_id:
            raise ValueError("Invalid flight ID.")
        source = payload["source"]
        status = payload["status"]
        recording = payload.get("recording")
        if source not in ("manual", "automatic") or status != "complete" or (
            (source == "automatic") != (recording is not None)
        ):
            raise ValueError("Unsupported flight source or status.")
        if recording is not None:
            recording = FlightRecording(**recording).validated()
        revisions = []
        for item in payload["revisions"]:
            saved_at = datetime.fromisoformat(item["saved_at"])
            if saved_at.utcoffset() is None:
                raise ValueError("A record revision is missing its time zone.")
            revisions.append(Revision(item["saved_at"], FlightFields(**item["fields"]).validated()))
        if not revisions:
            raise ValueError("The record has no revisions.")
        return Flight(flight_id, tuple(revisions), source, status, recording)

    @staticmethod
    def _read_json(path):
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _read_year(self, path):
        try:
            payload = self._read_json(path)
            if payload["schema"] != 1 or path != self._year_path(payload["year"]):
                raise ValueError("Unsupported Logbook year file.")
            flights = {}
            for item in payload["flights"]:
                flight = self._decode_flight(item)
                if flight.fields.flight_date[:4] != payload["year"] or flight.id in flights:
                    raise ValueError("A record belongs to a different year or has a duplicate ID.")
                flights[flight.id] = flight
            return flights
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ValueError(f"Could not read {path.name}: {error}") from error

    def _read_year_for_save(self, year):
        try:
            return self._read_year(self._year_path(year))
        except FileNotFoundError:
            return {}

    def _read_legacy(self, path):
        payload = self._read_json(path)
        if payload["schema"] != 1 or payload["id"] != path.stem:
            raise ValueError("Unsupported flight record.")
        return self._decode_flight({**payload, "status": "complete"})

    def _find(self, flight_id):
        match = None
        for path in self._year_paths():
            flight = self._read_year(path).get(flight_id)
            if flight is not None:
                if match is not None:
                    raise RecordConflict("This flight appears in more than one year file.")
                match = flight
        if match is not None:
            return match, True
        return self._read_legacy(self.records / f"{flight_id}.json"), False

    def get(self, flight_id):
        with self._locked():
            flight, _ = self._find(str(UUID(flight_id)))
            return flight

    def listing(self):
        with self._locked():
            flights = {}
            locations = {}
            unreadable = set()
            year_paths = set(self._year_paths())
            for path in sorted(year_paths):
                try:
                    for flight in self._read_year(path).values():
                        if flight.id in locations:
                            unreadable.update((path.name, locations[flight.id]))
                            flights.pop(flight.id, None)
                        else:
                            flights[flight.id] = flight
                            locations[flight.id] = path.name
                except (OSError, ValueError):
                    unreadable.add(path.name)
            # A damaged year may contain a newer version of a legacy flight.
            legacy_paths = [] if unreadable else sorted(self.records.glob("*.json"))
            for path in legacy_paths:
                if path in year_paths or path.stem in locations:
                    continue
                try:
                    flight = self._read_legacy(path)
                    flights[flight.id] = flight
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    unreadable.add(path.name)
            ordered = sorted(flights.values(), reverse=True,
                             key=lambda flight: (flight.fields.flight_date, flight.revisions[0].saved_at))
            return ordered, sorted(unreadable)

    def save(self, fields, existing=None):
        fields = fields.validated()
        with self._locked():
            return self._save_locked(fields, existing)

    def save_recording(self, flight_id, fields, recording):
        flight_id = str(UUID(flight_id))
        fields = fields.validated()
        recording = recording.validated()
        with self._locked():
            try:
                current, _ = self._find(flight_id)
            except FileNotFoundError:
                revision = Revision(datetime.now(timezone.utc).isoformat(timespec="seconds"), fields)
                flight = Flight(flight_id, (revision,), source="automatic", recording=recording)
                year = fields.flight_date[:4]
                flights = self._read_year_for_save(year)
                flights[flight_id] = flight
                self._write_year(year, flights)
                return flight
            if current.recording == recording and current.revisions[0].fields == fields:
                return current
            raise RecordConflict("This recording ID already belongs to a different flight.")

    def _save_locked(self, fields, existing):
        in_year_file = False
        if existing is not None:
            try:
                current, in_year_file = self._find(str(UUID(existing.id)))
            except FileNotFoundError as error:
                raise RecordConflict("This flight is no longer available. Reopen the Logbook.") from error
            if current != existing:
                raise RecordConflict("This flight changed. Reopen it before saving your edits.")
            if current.fields == fields:
                return current
        revision = Revision(datetime.now(timezone.utc).isoformat(timespec="seconds"), fields)
        previous = existing or Flight(str(uuid4()), ())
        flight = replace(previous, revisions=previous.revisions + (revision,))
        year = fields.flight_date[:4]
        if in_year_file and existing.fields.flight_date[:4] != year:
            destination = self._read_year_for_save(year)
            if flight.id in destination:
                raise RecordConflict("The destination year already contains this flight.")
            self._write_json(self._pending_move, {"schema": 1, "flight": asdict(flight)})
            self._recover_year_move()
        else:
            flights = self._read_year_for_save(year)
            flights[flight.id] = flight
            self._write_year(year, flights)
        return flight

    def _recover_year_move(self):
        try:
            pending = self._read_json(self._pending_move)
        except FileNotFoundError:
            return
        if pending["schema"] != 1:
            raise ValueError("Unsupported pending Logbook move.")
        flight = self._decode_flight(pending["flight"])
        previous = replace(flight, revisions=flight.revisions[:-1])
        if not previous.revisions:
            raise ValueError("Pending Logbook move has no previous revision.")
        old_year = previous.fields.flight_date[:4]
        new_year = flight.fields.flight_date[:4]
        if old_year == new_year:
            raise ValueError("Pending Logbook move must cross a year boundary.")
        source = self._read_year_for_save(old_year)
        destination = self._read_year_for_save(new_year)
        old_record = source.get(flight.id)
        new_record = destination.get(flight.id)
        if (old_record not in (None, previous) or new_record not in (None, flight)
                or old_record is None and new_record is None):
            raise RecordConflict("A pending year move conflicts with the saved records.")
        if new_record is None:
            destination[flight.id] = flight
            self._write_year(new_year, destination)
        if old_record is not None:
            del source[flight.id]
            self._write_year(old_year, source)
        self._pending_move.unlink()

    def _write_year(self, year, flights):
        self._write_json(self._year_path(year), {
            "schema": 1, "year": year,
            "flights": [asdict(flight) for flight in flights.values()],
        })

    @staticmethod
    def _write_json(path, payload):
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=".flight-",
                suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            with temporary.open(encoding="utf-8") as handle:
                json.load(handle)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
