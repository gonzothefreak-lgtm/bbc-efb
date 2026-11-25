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

from .aircraft import aircraft_identity, aircraft_type_code


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
    aircraft_code: str = ""
    real_departure: str = ""
    planned_departure: str = ""
    planned_arrival: str = ""
    actual_arrival: str = ""
    planned_nm: float | None = None
    flown_nm: float | None = None
    flight_kind: str = ""
    operating_region: str = ""
    crew: int | None = None
    passengers: int | None = None
    baggage_kg: float | None = None
    freight_kg: float | None = None

    @property
    def visited_arrival(self):
        return self.actual_arrival or self.arrival

    def validated(self):
        if self.minutes is not None and (
            type(self.minutes) is not int or not 0 <= self.minutes <= 9999
        ):
            raise ValueError("Enter a duration from 0 to 9,999 minutes.")
        flight_date = date.fromisoformat(self.flight_date).isoformat()
        aircraft = aircraft_identity(self.aircraft)
        departure = self.departure.strip().upper()
        arrival = self.arrival.strip().upper()
        if not all((aircraft, departure, arrival)):
            raise ValueError("Aircraft type, departure and destination are required.")
        for value in (self.crew, self.passengers):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Crew and passenger counts must be whole numbers of zero or more.")
        for value in (self.planned_nm, self.flown_nm, self.baggage_kg, self.freight_kg):
            if value is not None and (type(value) not in (int, float) or not isfinite(value) or value < 0):
                raise ValueError("Distances and weights must be finite numbers of zero or more.")
        real_departure = self.real_departure.strip()
        if real_departure:
            stamp = datetime.fromisoformat(real_departure)
            if stamp.utcoffset() is None:
                raise ValueError("Real departure time needs a time zone, for example 2026-11-25T03:30Z.")
            real_departure = stamp.astimezone(timezone.utc).isoformat()
        return replace(
            self, flight_date=flight_date, aircraft=aircraft, departure=departure, arrival=arrival,
            callsign=self.callsign.strip().upper(), registration=self.registration.strip().upper(),
            notes=self.notes.strip(), aircraft_code=aircraft_type_code(aircraft), real_departure=real_departure,
            planned_departure=self.planned_departure.strip().upper(),
            planned_arrival=self.planned_arrival.strip().upper(), actual_arrival=self.actual_arrival.strip().upper(),
            flight_kind=self.flight_kind.strip(), operating_region=self.operating_region.strip(),
        )


@dataclass(frozen=True)
class Revision:
    saved_at: str
    fields: FlightFields
    source: str = ""


@dataclass(frozen=True)
class RecordingGap:
    after: str
    before: str | None = None
    reason: str = ""

    @property
    def review_key(self):
        return f"{self.after}|{self.reason}"


@dataclass(frozen=True)
class RecordingCheckpoint:
    sim_time: float
    latitude: float
    longitude: float
    on_ground: bool
    takeoff_elapsed: float | None

    def validated(self):
        numbers = (self.sim_time, self.latitude, self.longitude)
        if any(type(x) not in (int, float) or not isfinite(x) for x in numbers) or not (
            0 < self.sim_time < 315537897600 and -90 <= self.latitude <= 90
            and -180 <= self.longitude <= 180 and type(self.on_ground) is bool
        ):
            raise ValueError("Invalid Recorder checkpoint position or clock.")
        value = self.takeoff_elapsed
        if value is not None and (type(value) not in (int, float) or not isfinite(value) or value < 0):
            raise ValueError("Invalid Recorder takeoff checkpoint.")
        return self


@dataclass(frozen=True)
class FlightRecording:
    provider: str
    simulated: bool
    gate_out: str | None
    takeoff: str | None
    touchdown: str | None
    gate_in: str | None
    block_seconds: float
    airborne_seconds: float
    landing_rate_fpm: float | None = None
    gaps: tuple[RecordingGap, ...] = ()
    manual_start: str | None = None
    manual_start_airborne: bool = False
    manual_stop: str | None = None

    def validated(self):
        times = [datetime.fromisoformat(value) for value in (
            self.gate_out, self.manual_start, self.takeoff, self.touchdown, self.gate_in, self.manual_stop,
        ) if value is not None]
        if (bool(self.gate_out) == bool(self.manual_start)
                or type(self.manual_start_airborne) is not bool
                or self.manual_start_airborne and (not self.manual_start or self.takeoff is not None)
                or self.touchdown is not None and self.takeoff is None and not self.manual_start_airborne
                or self.gate_in is not None and self.touchdown is None
                or self.gate_in is not None and self.manual_stop is not None
                or any(value.utcoffset() is None for value in times) or times != sorted(times)):
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
        for gap in self.gaps:
            after = datetime.fromisoformat(gap.after)
            before = datetime.fromisoformat(gap.before) if gap.before is not None else None
            if (after.utcoffset() is None or before is not None
                    and (before.utcoffset() is None or before < after) or not gap.reason):
                raise ValueError("Invalid recording gap.")
        return self

    def validate_checkpoint(self, checkpoint):
        checkpoint.validated()
        takeoff = checkpoint.takeoff_elapsed
        if (self.gate_in is not None or self.manual_stop is not None
                or (self.takeoff is not None or self.manual_start_airborne) != (takeoff is not None)
                or takeoff is not None and takeoff + self.airborne_seconds > self.block_seconds + 1e-6):
            raise ValueError("The Recorder checkpoint does not match its flight events.")


@dataclass(frozen=True)
class Flight:
    id: str
    revisions: tuple[Revision, ...]
    source: str = "manual"
    status: str = "complete"
    recording: FlightRecording | None = None
    checkpoint: RecordingCheckpoint | None = None
    manual_hold: str | None = None
    acknowledged_gaps: tuple[str, ...] = ()
    import_refs: tuple[tuple[str, str], ...] = ()
    reported_landing_rate: float | None = None
    reported_landing_source: str = ""

    @property
    def fields(self):
        return self.revisions[-1].fields

    @property
    def landing_rate(self):
        if self.recording is not None and self.recording.landing_rate_fpm is not None:
            return self.recording.landing_rate_fpm
        return self.reported_landing_rate

    @property
    def pending_gaps(self):
        if self.recording is None or self.recording.simulated:
            return ()
        return tuple(gap for gap in self.recording.gaps if gap.review_key not in self.acknowledged_gaps)


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
        checkpoint = payload.get("checkpoint")
        manual_hold = payload.get("manual_hold")
        if source not in ("manual", "automatic", "imported") or status not in ("complete", "interrupted") or (
            (source == "automatic") != (recording is not None)
            or status == "interrupted" and source != "automatic"
            or checkpoint is not None and status != "interrupted"
        ):
            raise ValueError("Unsupported flight source or status.")
        if recording is not None:
            recording = dict(recording)
            recording["gaps"] = tuple(RecordingGap(**gap) for gap in recording.get("gaps", ()))
            recording = FlightRecording(**recording).validated()
            if (status == "complete") != (recording.gate_in is not None):
                raise ValueError("Flight status does not match its recorded events.")
        if manual_hold is not None:
            FlightStore._validate_manual_hold(recording, manual_hold)
        if checkpoint is not None:
            checkpoint = RecordingCheckpoint(**checkpoint)
            recording.validate_checkpoint(checkpoint)
        revisions = []
        for item in payload["revisions"]:
            saved_at = datetime.fromisoformat(item["saved_at"])
            if saved_at.utcoffset() is None:
                raise ValueError("A record revision is missing its time zone.")
            revisions.append(Revision(item["saved_at"], FlightFields(**item["fields"]).validated(), item.get("source", "")))
        if not revisions:
            raise ValueError("The record has no revisions.")
        acknowledgements = tuple(payload.get("acknowledged_gaps", ()))
        keys = {gap.review_key for gap in recording.gaps} if recording is not None and not recording.simulated else set()
        if any(key not in keys for key in acknowledgements):
            raise ValueError("Acknowledgement does not refer to a native Recorder gap.")
        imports = tuple(tuple(ref) for ref in payload.get("import_refs", ()))
        if any(len(ref) != 2 or any(not isinstance(value, str) or not value for value in ref) for ref in imports):
            raise ValueError("Invalid source record reference.")
        rate = payload.get("reported_landing_rate")
        rate_source = payload.get("reported_landing_source", "")
        if rate is not None and (type(rate) not in (int, float) or not isfinite(rate) or rate > 0 or not rate_source):
            raise ValueError("Invalid imported landing rate.")
        return Flight(flight_id, tuple(revisions), source, status, recording, checkpoint, manual_hold,
                      acknowledgements, imports, rate, rate_source)

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
            return self._listing()

    def _listing(self):
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

    def apply_import(self, preview_records, changes):
        with self._locked():
            records, unreadable = self._listing()
            if unreadable or tuple(records) != preview_records:
                raise RecordConflict("The Logbook changed after preview. Open a new import preview.")
            written = 0
            for change in changes:
                existing = change.existing
                saved_at = datetime.now(timezone.utc).isoformat()
                revision = Revision(saved_at, change.fields, change.reference[0])
                if existing is None:
                    flight = Flight(str(uuid4()), (revision,), source="imported")
                else:
                    flight = next(item for item in records if item.id == existing.id)
                    flight = replace(flight, revisions=(*flight.revisions, revision))
                flight = replace(
                    flight, import_refs=(*flight.import_refs, change.reference),
                    reported_landing_rate=change.rate, reported_landing_source=change.rate_source,
                )
                try:
                    self._replace_record(flight)
                except (OSError, ValueError) as error:
                    raise RecordConflict(f"Import stopped after saving {written} rows. Preview the file again. {error}") from error
                written += 1
            return written

    def save(self, fields, existing=None):
        fields = fields.validated()
        with self._locked():
            return self._save_locked(fields, existing)

    def acknowledge_gap(self, flight_id, key):
        with self._locked():
            flight, _ = self._find(str(UUID(flight_id)))
            if key in flight.acknowledged_gaps:
                return flight
            if not any(gap.review_key == key for gap in flight.pending_gaps):
                raise RecordConflict("This Recorder gap is no longer available.")
            flight = replace(flight, acknowledged_gaps=(*flight.acknowledged_gaps, key))
            self._replace_record(flight)
            return flight

    def _replace_record(self, flight):
        year = flight.fields.flight_date[:4]
        flights = self._read_year_for_save(year)
        flights[flight.id] = flight
        self._write_year(year, flights)

    def append_replay(self, flight, samples):
        start = date.fromisoformat(flight.revisions[0].fields.flight_date)
        quarter = (start.month - 1) // 3 + 1
        path = self.records / f"replay-{start.year:04}-Q{quarter}.jsonl"
        batch = json.dumps({"flight_id": flight.id, "samples": samples}, allow_nan=False).encode("utf-8") + b"\n"
        with self._locked(), path.open("a+b") as handle:
            # A killed process may have left an incomplete batch at the end.
            offset = handle.seek(0, os.SEEK_END)
            while offset:
                start = max(0, offset - 4096)
                handle.seek(start)
                newline = handle.read(offset - start).rfind(b"\n")
                if newline >= 0:
                    offset = start + newline + 1
                    break
                offset = start
            handle.truncate(offset)
            handle.seek(0, os.SEEK_END)
            offset = handle.tell()
            try:
                handle.write(batch)
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                handle.truncate(offset)
                raise

    def replay_batches(self):
        batches = {}
        for path in sorted(self.records.glob("replay-????-Q[1-4].jsonl")):
            data = path.read_bytes()
            batches[path.name] = data[:data.rfind(b"\n") + 1].decode("utf-8")
        return batches

    def landing_replay(self, flight):
        if flight.recording is None or flight.recording.touchdown is None:
            return []
        start = date.fromisoformat(flight.revisions[0].fields.flight_date)
        quarter = (start.month - 1) // 3 + 1
        path = self.records / f"replay-{start.year:04}-Q{quarter}.jsonl"
        touchdown = datetime.fromisoformat(flight.recording.touchdown)
        samples = {}
        with self._locked():
            if not path.exists():
                return []
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.endswith("\n"):
                        break
                    batch = json.loads(line)
                    if batch["flight_id"] == flight.id:
                        for sample in batch["samples"]:
                            stamp = datetime.fromisoformat(sample["sim_time"])
                            if -30 <= (stamp - touchdown).total_seconds() <= 20:
                                samples[stamp] = sample
        return [samples[stamp] for stamp in sorted(samples)]

    def save_recording(self, flight_id, fields, recording, *, checkpoint=None, previous=None, manual_hold=None):
        flight_id = str(UUID(flight_id))
        fields = fields.validated()
        recording = recording.validated()
        if checkpoint is not None:
            recording.validate_checkpoint(checkpoint)
        if manual_hold is not None:
            self._validate_manual_hold(recording, manual_hold)
        status = "interrupted" if recording.gate_in is None else "complete"
        with self._locked():
            try:
                current, _ = self._find(flight_id)
            except FileNotFoundError:
                revision = Revision(datetime.now(timezone.utc).isoformat(timespec="seconds"), fields)
                if previous is not None:
                    raise RecordConflict("This recording is no longer available.")
                flight = Flight(flight_id, (revision,), "automatic", status, recording, checkpoint, manual_hold)
            else:
                if (current.recording == recording and current.checkpoint == checkpoint
                        and current.manual_hold == manual_hold
                        and current.revisions[0].fields.aircraft == fields.aircraft):
                    return current
                if (previous is None or current.id != previous.id or current.source != "automatic"
                        or current.status != "interrupted" or current.recording != previous.recording
                        or current.checkpoint != previous.checkpoint):
                    raise RecordConflict("This recording changed. Reopen it before continuing.")
                # Keep pilot corrections while advancing the saved capture.
                revisions = current.revisions
                if not any(revision.fields.minutes is not None for revision in revisions):
                    revised = replace(current.fields, minutes=fields.minutes)
                    if revised != current.fields:
                        revisions += (Revision(datetime.now(timezone.utc).isoformat(timespec="seconds"), revised),)
                flight = replace(current, revisions=revisions, status=status, recording=recording,
                                 checkpoint=checkpoint, manual_hold=manual_hold)
            year = flight.fields.flight_date[:4]
            flights = self._read_year_for_save(year)
            flights[flight_id] = flight
            self._write_year(year, flights)
            return flight

    @staticmethod
    def _validate_manual_hold(recording, phase):
        if recording is None or recording.manual_stop is None or phase not in ("taxi_out", "airborne", "taxi_in"):
            raise ValueError("Manual stop suppression requires a stopped flight and valid phase.")

    def set_manual_hold(self, previous, phase):
        with self._locked():
            current, _ = self._find(previous.id)
            if current.manual_hold != previous.manual_hold:
                raise RecordConflict("The stopped flight changed. Reopen it before continuing.")
            if phase is not None:
                self._validate_manual_hold(current.recording, phase)
            updated = replace(current, manual_hold=phase)
            year = updated.fields.flight_date[:4]
            flights = self._read_year_for_save(year)
            flights[updated.id] = updated
            self._write_year(year, flights)
            return updated

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
