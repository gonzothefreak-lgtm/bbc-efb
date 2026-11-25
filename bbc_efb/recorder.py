"""Record a normal gate-to-gate flight from one read-only Core consumer."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from math import cos, hypot, isfinite, radians
from time import monotonic
from uuid import uuid4

from bbc_core import ConnectionState, ConsumerAPI, CoreRequestError, Event, Gap, Quality, SimVar, ValueType

from .storage import FlightFields, FlightRecording, FlightStore, RecordingCheckpoint, RecordingGap


_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
_SAMPLE_MS = 1000
_FRESH_MS = 3000
_PARKED_SAMPLES = 5
_CHECKPOINT_SECONDS = 15
_MAX_RESUME_SIM_SECONDS = 30


@dataclass(frozen=True)
class _Sample:
    time: float
    ground: bool
    speed: float
    brake: bool
    aircraft: str
    landing_speed: float | None
    latitude: float
    longitude: float


def _iso(sim_time):
    return (_EPOCH + timedelta(seconds=sim_time)).isoformat()


def _sim_seconds(value):
    return (datetime.fromisoformat(value) - _EPOCH).total_seconds()


class FlightRecorder:
    """Drive update serially after Core has delivered telemetry."""

    def __init__(self, reader: ConsumerAPI, store: FlightStore, *, real_date=date.today,
                 elapsed_clock=monotonic):
        self._reader = reader
        self._store = store
        self._real_date = real_date
        self._elapsed_clock = elapsed_clock
        self._subscriptions = {}
        self._session = None
        self._last_sample = None
        self._last_good = None
        self._last_clock_packet = None
        self._paused = False
        self._previous_paused = False
        self._elapsed = 0.0
        self._parked_before_departure = False
        self._moving = None
        self._flight_id = None
        self._events = {}
        self._phase = "waiting"
        self._contact = None
        self._landing_baseline = None
        self._landing_rate = None
        self._parking = []
        self._pending = None
        self._pending_hold = None
        self._stopped = None
        self._hold_samples = 0
        self._ready = False
        self._current_source = None
        self._manual_start_airborne = False
        self._saved = None
        self._source = None
        self._gaps = ()
        self._last_checkpoint = None
        self._checkpoint_dirty = False
        self._auto_resume = False
        self.interrupted_reason = None
        self._resume_candidate = None
        try:
            for key, capability in (
                ("ground", "aircraft.on_ground"), ("speed", "aircraft.ground_speed"),
                ("aircraft", "aircraft.title"),
                ("latitude", "aircraft.latitude"), ("longitude", "aircraft.longitude"),
            ):
                self._subscriptions[key] = reader.subscribe(
                    capability, interval_ms=_SAMPLE_MS, max_age_ms=_FRESH_MS,
                )
            for key, name, unit, kind in (
                ("time", "ABSOLUTE TIME", "seconds", ValueType.NUMBER),
                ("brake", "BRAKE PARKING POSITION", "bool", ValueType.BOOLEAN),
            ):
                self._subscriptions[key] = self._subscribe(name, unit, kind)
            self._pause_id = reader.subscribe("sim.pause_state").capability.id
            flights, _ = store.listing()
            self._stopped = next((flight for flight in flights if flight.manual_hold is not None), None)
            recoverable = next((flight for flight in flights if flight.checkpoint is not None), None)
            if recoverable is not None:
                self._restore(recoverable)
        except Exception:
            reader.close()
            raise

    def _subscribe(self, name, unit, kind):
        return self._reader.subscribe_sdk_simvar(
            SimVar(name, unit, kind), interval_ms=_SAMPLE_MS, max_age_ms=_FRESH_MS,
        )

    def close(self):
        self._reader.close()

    @property
    def status(self):
        if self.interrupted_reason:
            return "Interrupted"
        if self._flight_id is not None:
            return "Active"
        return "Ready" if self._ready else "Inactive"

    @property
    def flight_id(self):
        return self._flight_id

    @property
    def can_resume(self):
        return self._resume_candidate is not None

    @property
    def can_start(self):
        return self._flight_id is None and self._ready and not self._paused

    @property
    def can_stop(self):
        return self.status == "Active" and self._pending is None

    @property
    def waiting_for_next_flight(self):
        return self._stopped is not None and self._flight_id is None

    def start(self):
        self.update()
        if not self.can_start:
            return False
        sample = self._last_good
        self._begin(sample, self._current_source, "manual_start", (sample.time, self._elapsed))
        self._checkpoint_dirty = True
        self._checkpoint()
        return True

    def stop(self):
        if not self.can_stop:
            return None
        recording = replace(self._recording(gaps=self._gaps), manual_stop=_iso(self._last_good.time))
        self._prepare_save(recording)
        self._pending_hold = self._phase
        return self._save()

    def _restore(self, flight):
        recording = flight.recording
        checkpoint = flight.checkpoint
        self._flight_id = flight.id
        self._date = flight.revisions[0].fields.flight_date
        self._aircraft = flight.revisions[0].fields.aircraft
        self._elapsed = recording.block_seconds
        start_key = "manual_start" if recording.manual_start is not None else "gate_out"
        self._events = {start_key: (_sim_seconds(recording.manual_start or recording.gate_out), 0.0)}
        self._manual_start_airborne = recording.manual_start_airborne
        self._phase = "taxi_out"
        if recording.takeoff is not None:
            self._events["takeoff"] = (_sim_seconds(recording.takeoff), checkpoint.takeoff_elapsed)
            self._phase = "airborne"
        elif recording.manual_start_airborne:
            self._phase = "airborne"
        if recording.touchdown is not None:
            self._events["touchdown"] = (
                _sim_seconds(recording.touchdown), checkpoint.takeoff_elapsed + recording.airborne_seconds,
            )
            self._phase = "taxi_in"
        self._landing_rate = recording.landing_rate_fpm
        self._gaps = recording.gaps
        self._saved = flight
        self._source = (recording.provider, recording.simulated)
        self._last_good = _Sample(checkpoint.sim_time, checkpoint.on_ground, 0.0, False,
                                  self._aircraft, None, checkpoint.latitude, checkpoint.longitude)
        self.interrupted_reason = "A previous recording is available for recovery."
        if not self._gaps or self._gaps[-1].before is not None:
            self._gaps += (RecordingGap(_iso(checkpoint.sim_time), reason="App stopped between samples."),)

    def _interrupt(self, reason, *, auto_resume=False):
        if not self.interrupted_reason:
            self.interrupted_reason = reason
            self._auto_resume = auto_resume
            self._resume_candidate = None
            self._gaps += (RecordingGap(_iso(self._last_good.time), reason=reason),)
            self._remove_landing()
        self._checkpoint_dirty = True
        self._checkpoint()

    def save_and_exit(self):
        if self._pending is not None:
            self._save()
        elif self._flight_id is not None:
            self._interrupt("The EFB closed during recording.")

    def keep_partial(self):
        if self.interrupted_reason is None:
            return
        self._checkpoint(keep_partial=True)
        self._reset()
        self._last_good = self._last_sample = self._last_clock_packet = None
        self._parked_before_departure = False
        self._ready = False

    def resume(self):
        self.update()
        if not self.can_resume:
            return False
        self._resume()
        return True

    def _resume(self):
        session, sample = self._resume_candidate
        gaps = self._gaps[:-1] + (RecordingGap(
            self._gaps[-1].after, _iso(sample.time), self._gaps[-1].reason,
        ),)
        self._checkpoint(sample=sample, gaps=gaps)
        self._gaps = gaps
        self._last_good = self._last_sample = sample
        self._session = session
        self._previous_paused = False
        self.interrupted_reason = None
        self._resume_candidate = None
        self._auto_resume = False
        self._contact = self._landing_baseline = None
        self._parking.clear()
        if self._phase == "airborne":
            self._request_landing()

    def _compatible(self, sample, source):
        last = self._last_good
        if (self._source != (source.name, source.simulated) or not last.aircraft
                or sample.aircraft.casefold() != last.aircraft.casefold() or sample.ground != last.ground
                or not 0 < sample.time - last.time <= _MAX_RESUME_SIM_SECONDS):
            return False
        # Estimate short distances across the date line using current positions.
        longitude_delta = (sample.longitude - last.longitude + 180) % 360 - 180
        east = longitude_delta * cos(radians((sample.latitude + last.latitude) / 2))
        distance_nm = 60 * hypot(sample.latitude - last.latitude, east)
        return distance_nm <= (0.15 if sample.ground else 3)

    def update(self):
        """Return a newly saved flight. A failed write can be retried with update."""
        if self._pending is not None:
            return self._save()
        if self._checkpoint_dirty:
            self._checkpoint()
        connection = self._reader.connection()
        if connection.state != ConnectionState.CONNECTED or connection.session_id != self._session:
            if self._flight_id is not None and not self.interrupted_reason:
                self._interrupt("The Core session changed during recording.", auto_resume=True)
            self._session = connection.session_id
            self._resume_candidate = None
            self._ready = False
            self._hold_samples = 0
            if self._flight_id is None:
                self._last_sample = self._last_good = self._last_clock_packet = self._moving = None
                self._parked_before_departure = self._paused = self._previous_paused = False
                self._elapsed = 0.0
            else:
                self._last_clock_packet = None
                self._paused = self._previous_paused = False
        if connection.state != ConnectionState.CONNECTED:
            return None

        for update in self._reader.poll():
            if isinstance(update, Gap):
                if self._flight_id is not None and not self.interrupted_reason:
                    self._interrupt("Core reported a telemetry gap during recording.")
                    return None
                if self._flight_id is None:
                    self._last_sample = self._moving = None
                    self._parked_before_departure = False
                else:
                    self._resume_candidate = None
                    return None
            elif (isinstance(update, Event) and update.session_id == self._session
                  and update.capability_id == self._pause_id):
                self._paused = bool(update.value)
                if self._paused:
                    self._resume_candidate = None

        observed = {key: self._reader.latest(sub.id) for key, sub in self._subscriptions.items()}
        clock = observed["time"]
        required = ("time", "ground", "speed", "brake", "aircraft", "latitude", "longitude")
        if any(observed[key].quality != Quality.CURRENT or observed[key].session_id != self._session
               for key in required):
            self._resume_candidate = None
            self._ready = False
            if self._flight_id is not None and not self.interrupted_reason:
                self._interrupt("Essential flight telemetry became unavailable.")
            return None
        packet = (clock.received_at, clock.value)
        if packet == self._last_clock_packet:
            return None
        self._last_clock_packet = packet
        value = clock.value
        latitude, longitude = observed["latitude"].value, observed["longitude"].value
        if not (0 < value < 315537897600 and -90 <= latitude <= 90 and -180 <= longitude <= 180):
            self._resume_candidate = None
            self._ready = False
            if self._flight_id is not None and not self.interrupted_reason:
                self._interrupt("The simulator clock or position became invalid.")
            return None
        landing = observed.get("landing")
        landing_speed = (landing.value if landing and landing.quality == Quality.CURRENT
                         and landing.session_id == self._session else None)
        sample = _Sample(value, observed["ground"].value, observed["speed"].value,
                         observed["brake"].value, observed["aircraft"].value, landing_speed, latitude, longitude)
        if self.interrupted_reason:
            previous_candidate = self._resume_candidate
            self._resume_candidate = ((connection.session_id, sample)
                                      if not self._paused and self._compatible(sample, connection.source) else None)
            if (self._auto_resume and self.can_resume and previous_candidate is not None
                    and previous_candidate[0] == connection.session_id
                    and previous_candidate[1].time < sample.time):
                self._resume()
            return None
        previous = self._last_sample
        was_paused = self._previous_paused
        self._last_sample = sample
        self._previous_paused = self._paused
        if previous and sample.time < previous.time:
            self._ready = False
            if self._flight_id is not None:
                self._interrupt("The simulator clock moved backwards.")
            self._parked_before_departure = False
            self._moving = None
            return None
        if self._paused or previous and sample.time == previous.time:
            return None
        if self._flight_id is not None and sample.aircraft != self._aircraft:
            if sample.aircraft.casefold() != self._aircraft.casefold():
                self._interrupt("The aircraft changed during recording.")
                return None
        if previous and sample.aircraft != previous.aircraft:
            self._parked_before_departure = False
            self._moving = None
        phase = self._phase
        if phase == "taxi_in" and not sample.ground:
            self._interrupt("The aircraft became airborne again after touchdown.")
            return None
        if previous and not was_paused:
            advance = sample.time - previous.time
            if self._flight_id is not None and advance > 30:
                self._interrupt("The simulator clock jumped during recording.")
                return None
            self._elapsed += advance
        self._last_good = sample
        self._ready = True
        self._current_source = connection.source
        self._advance(sample, connection.source)
        if self._pending is not None:
            return self._save()
        if self._flight_id is not None and (
            phase != self._phase or self._elapsed_clock() - self._last_checkpoint >= _CHECKPOINT_SECONDS
        ):
            self._checkpoint_dirty = True
            self._checkpoint()
        return None

    def _advance(self, sample, source):
        mark = (sample.time, self._elapsed)
        if self._phase == "waiting":
            if self._stopped is not None and not self._observe_stopped_flight(sample):
                return
            if sample.ground and sample.speed <= 1 and sample.brake:
                self._parked_before_departure = True
            if self._parked_before_departure and sample.ground and sample.speed >= 2 and not sample.brake:
                if self._moving is None:
                    self._moving = mark
                    return
                self._begin(sample, source, "gate_out", self._moving)
            else:
                self._moving = None
        elif self._phase == "taxi_out" and not sample.ground:
            self._events["takeoff"] = mark
            self._phase = "airborne"
            self._request_landing()
        elif self._phase == "airborne":
            if not sample.ground:
                self._contact = None
                self._landing_rate = None
                if sample.landing_speed is not None:
                    self._landing_baseline = sample.landing_speed
            elif self._contact is None:
                self._contact = mark
                self._capture_rate(sample.landing_speed)
            else:
                self._events["touchdown"] = self._contact
                self._capture_rate(sample.landing_speed)
                self._phase = "taxi_in"
        elif self._phase == "taxi_in":
            if sample.speed <= 1 and sample.brake:
                self._parking.append(mark)
                if len(self._parking) == _PARKED_SAMPLES:
                    self._events["gate_in"] = self._parking[0]
                    self._finish()
            else:
                self._parking.clear()

    def _begin(self, sample, source, start_key, mark):
        self._events[start_key] = mark
        self._flight_id = str(uuid4())
        self._date = self._real_date().isoformat()
        self._aircraft = sample.aircraft
        self._source = (source.name, source.simulated)
        self._manual_start_airborne = start_key == "manual_start" and not sample.ground
        self._phase = "airborne" if self._manual_start_airborne else "taxi_out"
        if self._manual_start_airborne:
            self._request_landing()

    def _set_hold(self, phase):
        flight = self._store.set_manual_hold(self._stopped, phase)
        self._stopped = flight if phase is not None else None
        self._hold_samples = 0

    def _observe_stopped_flight(self, sample):
        phase = self._stopped.manual_hold
        changed_aircraft = sample.aircraft.casefold() != self._stopped.revisions[0].fields.aircraft.casefold()
        if not sample.ground:
            self._hold_samples = 0
            if phase != "airborne":
                self._set_hold("airborne")
        elif changed_aircraft or phase == "taxi_in":
            self._hold_samples = self._hold_samples + 1 if sample.speed <= 1 and sample.brake else 0
            if self._hold_samples >= _PARKED_SAMPLES:
                self._set_hold(None)
                return True
        elif phase == "airborne":
            self._hold_samples += 1
            if self._hold_samples >= 2:
                self._set_hold("taxi_in")
        return False

    def _capture_rate(self, speed):
        if self._landing_baseline is not None and speed and speed != self._landing_baseline:
            rate = -abs(speed) * 60
            if isfinite(rate):
                self._landing_rate = rate

    def _request_landing(self):
        try:
            self._subscriptions["landing"] = self._subscribe(
                "PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", ValueType.NUMBER,
            )
        except CoreRequestError:
            pass

    def _remove_landing(self):
        landing = self._subscriptions.pop("landing", None)
        if landing is not None:
            self._reader.unsubscribe(landing.id)

    def _recording(self, *, gaps):
        events = self._events
        block_end = events["gate_in"][1] if "gate_in" in events else self._elapsed
        block = block_end - self._start_mark[1]
        takeoff = self._air_start_mark
        air_end = events["touchdown"][1] if "touchdown" in events else block_end
        return FlightRecording(
            *self._source, *(_iso(events[key][0]) if key in events else None
                             for key in ("gate_out", "takeoff", "touchdown", "gate_in")),
            block_seconds=block, airborne_seconds=air_end - takeoff[1] if takeoff else 0.0,
            landing_rate_fpm=self._landing_rate if "touchdown" in events else None, gaps=gaps,
            manual_start=_iso(events["manual_start"][0]) if "manual_start" in events else None,
            manual_start_airborne=self._manual_start_airborne,
        )

    @property
    def _start_mark(self):
        return self._events.get("gate_out") or self._events["manual_start"]

    @property
    def _air_start_mark(self):
        return self._start_mark if self._manual_start_airborne else self._events.get("takeoff")

    def _checkpoint(self, *, sample=None, gaps=None, keep_partial=False):
        sample = sample or self._last_good
        takeoff = self._air_start_mark
        checkpoint = None if keep_partial else RecordingCheckpoint(
            sample.time, sample.latitude, sample.longitude, sample.ground,
            takeoff[1] - self._start_mark[1] if takeoff else None,
        )
        self._saved = self._store.save_recording(
            self._flight_id, FlightFields(self._date, self._aircraft, "Unknown", "Unknown"),
            self._recording(gaps=self._gaps if gaps is None else gaps), checkpoint=checkpoint,
            previous=self._saved,
        )
        if "manual_start" in self._events and self._stopped is not None:
            self._set_hold(None)
        self._last_checkpoint = self._elapsed_clock()
        self._checkpoint_dirty = False

    def _finish(self):
        self._prepare_save(self._recording(gaps=self._gaps))

    def _prepare_save(self, recording):
        block = recording.block_seconds
        fields = FlightFields(self._date, self._aircraft, "Unknown", "Unknown",
                              minutes=round(block / 60) if block < 599970 else None)
        self._pending = (self._flight_id, fields, recording)

    def _save(self):
        flight = self._store.save_recording(*self._pending, previous=self._saved, manual_hold=self._pending_hold)
        stopped = self._pending_hold is not None
        self._reset()
        if stopped:
            self._stopped = flight
        self._parked_before_departure = not stopped
        return flight

    def _reset(self):
        self._pending = None
        self._pending_hold = None
        self._manual_start_airborne = False
        self._saved = self._source = self._last_checkpoint = None
        self._gaps = ()
        self._checkpoint_dirty = False
        self.interrupted_reason = self._resume_candidate = None
        self._auto_resume = False
        self._flight_id = self._moving = self._contact = None
        self._landing_baseline = self._landing_rate = None
        self._events.clear()
        self._parking.clear()
        self._remove_landing()
        self._phase = "waiting"
