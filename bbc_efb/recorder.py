"""Record a normal gate-to-gate flight from one read-only Core consumer."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from uuid import uuid4

from bbc_core import ConnectionState, ConsumerAPI, CoreRequestError, Event, Gap, Quality, SimVar, ValueType

from .storage import FlightFields, FlightRecording, FlightStore


_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
_SAMPLE_MS = 1000
_FRESH_MS = 3000
_PARKED_SAMPLES = 5


@dataclass(frozen=True)
class _Sample:
    time: float
    ground: bool
    speed: float
    brake: bool
    aircraft: str
    landing_speed: float | None


class FlightRecorder:
    """Drive update serially after Core has delivered telemetry."""

    def __init__(self, reader: ConsumerAPI, store: FlightStore, *, real_date=date.today):
        self._reader = reader
        self._store = store
        self._real_date = real_date
        self._subscriptions = {}
        self._session = None
        self._last_sample = None
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
        self.interrupted_reason = None
        try:
            for key, capability in (
                ("ground", "aircraft.on_ground"), ("speed", "aircraft.ground_speed"),
                ("aircraft", "aircraft.title"),
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
        except Exception:
            reader.close()
            raise

    def _subscribe(self, name, unit, kind):
        return self._reader.subscribe_sdk_simvar(
            SimVar(name, unit, kind), interval_ms=_SAMPLE_MS, max_age_ms=_FRESH_MS,
        )

    def close(self):
        self._reader.close()

    def update(self):
        """Return a newly saved flight. A failed write can be retried with update."""
        if self._pending is not None:
            return self._save()
        if self.interrupted_reason:
            return None
        connection = self._reader.connection()
        if connection.state != ConnectionState.CONNECTED or connection.session_id != self._session:
            if self._flight_id is not None:
                self.interrupted_reason = "The Core session changed during recording."
                return None
            self._session = connection.session_id
            self._last_sample = self._last_clock_packet = self._moving = None
            self._parked_before_departure = self._paused = self._previous_paused = False
            self._elapsed = 0.0
        if connection.state != ConnectionState.CONNECTED:
            return None

        for update in self._reader.poll():
            if isinstance(update, Gap):
                if self._flight_id is not None:
                    self.interrupted_reason = "Core reported a telemetry gap during recording."
                    return None
                self._last_sample = self._moving = None
                self._parked_before_departure = False
            elif (isinstance(update, Event) and update.session_id == self._session
                  and update.capability_id == self._pause_id):
                self._paused = bool(update.value)

        observed = {key: self._reader.latest(sub.id) for key, sub in self._subscriptions.items()}
        clock = observed["time"]
        required = ("time", "ground", "speed", "brake", "aircraft")
        if any(observed[key].quality != Quality.CURRENT or observed[key].session_id != self._session
               for key in required):
            if self._flight_id is not None:
                self.interrupted_reason = "Essential flight telemetry became unavailable."
            return None
        packet = (clock.received_at, clock.value)
        if packet == self._last_clock_packet:
            return None
        self._last_clock_packet = packet
        value = clock.value
        if not 0 < value < 315537897600 or not isfinite(value):
            if self._flight_id is not None:
                self.interrupted_reason = "The simulator clock became invalid."
            return None
        landing = observed.get("landing")
        landing_speed = (landing.value if landing and landing.quality == Quality.CURRENT
                         and landing.session_id == self._session else None)
        sample = _Sample(value, observed["ground"].value, observed["speed"].value,
                         observed["brake"].value, observed["aircraft"].value, landing_speed)
        previous = self._last_sample
        was_paused = self._previous_paused
        self._last_sample = sample
        self._previous_paused = self._paused
        if previous and sample.time < previous.time:
            if self._flight_id is not None:
                self.interrupted_reason = "The simulator clock moved backwards."
            self._parked_before_departure = False
            self._moving = None
            return None
        if self._paused or previous and sample.time == previous.time:
            return None
        if self._flight_id is not None and sample.aircraft != self._aircraft:
            self.interrupted_reason = "The aircraft changed during recording."
            return None
        if previous and sample.aircraft != previous.aircraft:
            self._parked_before_departure = False
            self._moving = None
        if previous and not was_paused:
            advance = sample.time - previous.time
            if self._flight_id is not None and advance > 30:
                self.interrupted_reason = "The simulator clock jumped during recording."
                return None
            self._elapsed += advance
        self._advance(sample, connection.source)
        if self._pending is not None:
            return self._save()
        return None

    def _advance(self, sample, source):
        mark = (sample.time, self._elapsed)
        if self._phase == "waiting":
            if sample.ground and sample.speed <= 1 and sample.brake:
                self._parked_before_departure = True
            if self._parked_before_departure and sample.ground and sample.speed >= 2 and not sample.brake:
                if self._moving is None:
                    self._moving = mark
                    return
                self._events["gate_out"] = self._moving
                self._flight_id = str(uuid4())
                self._date = self._real_date().isoformat()
                self._aircraft = sample.aircraft
                self._phase = "taxi_out"
            else:
                self._moving = None
        elif self._phase == "taxi_out" and not sample.ground:
            self._events["takeoff"] = mark
            self._phase = "airborne"
            try:
                self._subscriptions["landing"] = self._subscribe(
                    "PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", ValueType.NUMBER,
                )
            except CoreRequestError:
                pass
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
            if not sample.ground:
                self.interrupted_reason = "The aircraft became airborne again after touchdown."
                return
            if sample.speed <= 1 and sample.brake:
                self._parking.append(mark)
                if len(self._parking) == _PARKED_SAMPLES:
                    self._events["gate_in"] = self._parking[0]
                    self._finish(source)
            else:
                self._parking.clear()

    def _capture_rate(self, speed):
        if self._landing_baseline is not None and speed and speed != self._landing_baseline:
            rate = -abs(speed) * 60
            if isfinite(rate):
                self._landing_rate = rate

    def _finish(self, source):
        events = self._events
        block = events["gate_in"][1] - events["gate_out"][1]
        airborne = events["touchdown"][1] - events["takeoff"][1]
        recording = FlightRecording(
            source.name, source.simulated,
            **{key: (_EPOCH + timedelta(seconds=mark[0])).isoformat() for key, mark in events.items()},
            block_seconds=block, airborne_seconds=airborne, landing_rate_fpm=self._landing_rate,
        )
        fields = FlightFields(self._date, self._aircraft, "Unknown", "Unknown",
                              minutes=round(block / 60) if block < 599970 else None)
        self._pending = (self._flight_id, fields, recording)

    def _save(self):
        flight = self._store.save_recording(*self._pending)
        self._pending = None
        self._flight_id = self._moving = self._contact = None
        self._landing_baseline = self._landing_rate = None
        self._events.clear()
        self._parking.clear()
        landing = self._subscriptions.pop("landing", None)
        if landing is not None:
            self._reader.unsubscribe(landing.id)
        self._parked_before_departure = True
        self._phase = "waiting"
        return flight
