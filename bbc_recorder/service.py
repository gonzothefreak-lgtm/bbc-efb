"""Flight Recorder service: Black Box Core in, crash-safe journals and Logbook records out.

This is the only part of the Recorder that talks to Core. It holds one read-only
``ConsumerAPI`` (registered with no control groups) and, on every :meth:`RecorderService.tick`
(called on the Core host thread after each pump), it:

1. drains ``poll()``;
2. turns each recorder ``Snapshot`` into a ``Sample`` (``sample_from_snapshot``) and feeds the
   real ``FlightRecorder`` state machine, together with the lifecycle events, connection
   changes and Core gaps (``FlightRecorder.process``);
3. appends every ``RecorderEvent`` to the flight's crash-safe ``Journal``, plus a SAMPLE track
   point (the Sample it fed) once per second, or every 0.2 s at high rate;
4. follows the state machine's ``sampling_hint``: it switches the snapshot between
   ``SnapshotPeriod.SECOND`` and ``SnapshotPeriod.SIM_FRAME`` by subscribing the new cadence
   first and releasing the old one only after the new one delivered, so no sample is lost and
   none is fed twice;
5. asks Core for the nearest airports at block-off and block-on and journals what
   ``bbc_recorder.attribution`` makes of them (departure, arrival, suspected diversion), and
   journals the aircraft identity (TITLE and the ATC SimVars) when a flight starts or the
   identity changes;
6. when a flight ends, closes its journal and hands it to ``bbc_recorder.recovery``
   (``finalize_closed``), which builds the record from the journal
   (``bbc_recorder.records.build_recorded_flight``), saves it through the sink and archives
   the journal. Save & Exit and a very long data gap use ``finalize_interrupted`` instead: an
   explicit gap and *interrupted*, never an invented end time.

Saving runs on a small background thread by default, so the Core thread never waits on the
Logbook's disk writes. A failed save keeps the journal (closed, not archived) and is retried;
the next start offers it again through :meth:`RecorderService.recover_unfinished`.

Threading: :meth:`tick` runs on the Core thread. :meth:`status` may be called from any thread;
it returns the snapshot taken at the end of the last tick or action. :meth:`manual_start`,
:meth:`manual_stop`, :meth:`save_and_close` and :meth:`close` may be called from the UI thread:
they never call Core; anything that needs Core is queued for the next tick.

No Qt and no SimConnect: only Core's public contract types.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
from collections import deque
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _system_monotonic
from typing import Any, Callable, Iterable, Mapping

from bbc_core import contract as _contract
from bbc_core.contract import Connection, ConnectionState, Event, Gap, Snapshot, SnapshotPeriod, SubscriptionEnded

from . import attribution as attr
from . import recovery
from .journal import FsyncPolicy, Journal, JournalError, JournalLayout, Outcome, new_flight_id
from .model import EventKind, Phase, RecorderEvent
from .records import build_recorded_flight, journal_is_loggable
from .rules import RULES_VERSION, RecorderRules, snapshot_simvars
from .state_machine import FlightRecorder, Sample, sample_from_snapshot

log = logging.getLogger(__name__)

SERVICE_VERSION = "2"

# SignalError is being split from Gap in Core (contract C7). Handle both shapes: check it first.
SignalError = getattr(_contract, "SignalError", None)

STATES = ("unavailable", "inactive", "armed", "recording", "paused", "gap", "error", "saving")

# Catalog capability IDs of the lifecycle events the Recorder needs. Not sim.paused or
# sim.pause_changed: sim.pause_state carries the full Pause_EX1 flags, and subscribing to more
# than one would count a pause twice.
LIFECYCLE_EVENTS: tuple[str, ...] = (
    "sim.pause_state", "sim.aircraft_crashed", "sim.crash_reset", "sim.aircraft_loaded",
    "sim.flight_loaded", "sim.position_changed", "sim.started", "sim.stopped",
)

PHASE_LABELS = {
    Phase.IDLE: "Idle", Phase.PREFLIGHT: "Preflight", Phase.TAXI_OUT: "Taxi out",
    Phase.TAKEOFF_ROLL: "Takeoff roll", Phase.AIRBORNE: "Airborne", Phase.LANDING_ROLL: "Landing roll",
    Phase.TAXI_IN: "Taxi in", Phase.COMPLETE: "Complete", Phase.ABANDONED: "Abandoned",
}

# Pilot-facing wording follows the UI Design Standards: "Flight Logging", not "Recording".
STATE_LABELS = {
    "unavailable": "Flight Logging not available", "inactive": "Flight Logging inactive", "armed": "Ready",
    "recording": "Logging active", "paused": "Logging paused", "gap": "Logging interrupted",
    "error": "Flight Logging error", "saving": "Saving the flight",
}

PROPOSALS: tuple[str, ...] = (
    "P-S1 Sampling: one coherent snapshot per simulator second normally; per simulation frame (every "
    "RecorderRules.high_rate_every frames) while the state machine asks for high rate near the ground. A switch "
    "subscribes the new cadence first and releases the old one only after the new one delivered; if the new one "
    "delivers nothing within 10 s the service keeps the old one.",
    "P-S2 Track: the service journals the Sample it fed once per second (every 0.2 s at high rate) while a flight "
    "is open, so the journal holds the evidence the state machine saw.",
    "P-S3 Quiet feed: no snapshot for 5 s while connected and not paused shows as 'Logging interrupted'. The state "
    "machine journals the gap when data resumes.",
    "P-S4 Long gap: after 30 minutes without data the open flight is saved as interrupted (no invented end time).",
    "P-S5 Manual mode: with automatic logging off the state machine does not arm flights; Start Logging starts one.",
    "P-S6 Stop Logging needs explicit confirmation; the state machine decides the outcome (complete only after a "
    "landing, otherwise abandoned, or discarded when nothing loggable happened).",
    "P-S7 Save & Exit saves an open flight as interrupted, with an explicit gap from the last journalled data to "
    "the exit time.",
    "P-S8 Airports: nearest-airport facility queries at block-off and block-on, judged by "
    "bbc_recorder.attribution (6 km radius, ambiguity margin, runways). Only a 'known' match fills the Logbook "
    "field. A different arrival than a planned destination is a suspected diversion for the pilot to confirm.",
    "P-S9 Empty flights (no block-off, takeoff or landing) never reach the Logbook; their journal is kept in "
    "recorder/discarded/.",
)


# --------------------------------------------------------------------------------------------
# Public value types
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LastFlight:
    flight_id: str
    outcome: str
    saved: bool
    record_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class RecorderStatus:
    """Flight Logging status (contract C3). Plain values; safe to read from any thread.

    ``state`` is one of :data:`STATES`. Times are simulator UTC when known. Durations are the
    state machine's running clocks (simulator time) and stay None when not measured.
    """

    state: str
    phase: Phase | None
    detail: str
    flight_id: str | None
    started_utc: datetime | None
    aircraft: str | None
    departure: str | None
    block_time_s: float | None
    airborne_time_s: float | None
    simulated: bool
    label: str = ""
    can_start: bool = False
    can_stop: bool = False
    automatic: bool = True
    flight_open: bool = False
    started_manually: bool = False
    pending_saves: int = 0
    sampling: str = "second"
    connection: str = "unknown"
    last_flight: LastFlight | None = None


@dataclass(frozen=True, slots=True)
class SaveResult:
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    message: str
    needs_confirmation: bool = False
    flight_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    """Service tunables. The defaults are proposals (see ``PROPOSALS``)."""

    automatic: bool = True
    stale_after_s: float = 5.0
    max_open_gap_s: float | None = 1800.0
    switch_timeout_s: float = 10.0
    resubscribe_after_s: float = 10.0
    poll_limit: int = 256
    max_batches_per_tick: int = 16
    track_interval_s: float = 1.0
    track_interval_high_s: float = 0.2
    high_every: int | None = None            # None: the state machine's hint (RecorderRules.high_rate_every)
    identity_every_s: int = 5
    airport_lookup: bool = True
    airport_timeout_s: float = 10.0
    arrival_wait_s: float = 12.0
    retry_saves_s: float = 30.0
    background_saves: bool = True
    close_timeout_s: float = 5.0
    recent_events: int = 50
    signal_note_interval_s: float = 60.0      # one journalled signal_error NOTE per capability and reason
    overlap_window_s: float = 2.0             # how far behind the last fed sample a cadence-switch duplicate can be

    def to_json(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in dataclass_fields(self)}


# --------------------------------------------------------------------------------------------
# Internal state
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Sub:
    id: str
    period: SnapshotPeriod
    every: int
    created_mono: float
    delivered: int = 0


@dataclass(slots=True)
class _Flight:
    flight_id: str
    journal: Journal | None
    started_host: datetime
    started_sim_utc: datetime | None
    trigger: str
    simulated: bool
    started_manually: bool = False
    phase: Phase = Phase.PREFLIGHT
    journal_error: str | None = None
    last_track_mono: float | None = None
    departure: str | None = None
    identity_key: tuple | None = None
    lookups: set[str] = field(default_factory=set)
    ended: RecorderEvent | None = None
    loggable: bool = False
    finish_deadline: float | None = None


@dataclass(slots=True)
class _Job:
    kind: str                # "closed", "interrupted" or "discard"
    flight_id: str
    outcome: str
    detail: str
    attempts: int = 0
    error: str | None = None
    footer_extra: dict[str, Any] | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _sample_json(sample: Sample, period: SnapshotPeriod) -> dict[str, Any]:
    data: dict[str, Any] = {"origin": "service", "sampling": period.value}
    for name in Sample.__slots__:  # type: ignore[attr-defined]
        if name in ("host_time", "sim_time_s", "sim_utc"):
            continue
        value = getattr(sample, name)
        if value is None:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        data[name] = list(value) if isinstance(value, tuple) else value
    return data


def _plain(text: Any) -> str:
    return str(text or "").strip().rstrip(".")


# --------------------------------------------------------------------------------------------
# Background saver
# --------------------------------------------------------------------------------------------


class _Saver:
    """Runs save jobs off the Core thread (or inline when ``background`` is False)."""

    def __init__(self, run: Callable[[_Job], None], *, background: bool):
        self._run = run
        self._background = background
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._busy = 0
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> int:
        with self._cond:
            return self._busy

    def submit(self, job: _Job) -> None:
        if not self._background:
            self._run(job)
            return
        with self._cond:
            self._busy += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="bbc-recorder-save", daemon=True)
                self._thread.start()
        self._queue.put(job)

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                self._run(job)
            except Exception:  # noqa: BLE001 - _run reports its own failures; never kill the thread
                log.exception("Recorder save job failed unexpectedly")
            finally:
                with self._cond:
                    self._busy -= 1
                    self._cond.notify_all()

    def wait(self, timeout_s: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._busy == 0, timeout=max(0.0, timeout_s))

    def stop(self) -> None:
        if self._thread is not None:
            self._queue.put(None)


# --------------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------------


class RecorderService:
    """Automatic Flight Logging from read-only MSFS telemetry (contract C3).

    ``api`` is a Core ``ConsumerAPI`` with no control groups. ``sink`` has
    ``save_recorded_flight(record: dict) -> str`` and must be idempotent by
    ``record["flight_id"]`` (``bbc_efb.recorder_sink.LogbookSink``). Journals live under
    ``data_dir/recorder/``. ``rules`` are the state machine's ``RecorderRules``. ``clock``
    returns the aware UTC host time (default: now); ``monotonic`` defaults to the system
    monotonic clock, or to ``clock()`` when only a clock is injected (tests).
    """

    def __init__(self, api: Any, sink: Any, data_dir: Path | str | None, rules: RecorderRules | None = None,
                 clock: Callable[[], datetime] | None = None, *, monotonic: Callable[[], float] | None = None,
                 config: RecorderConfig | None = None, app_version: str = SERVICE_VERSION,
                 journal_policy: FsyncPolicy | None = None, planned_destination: str | None = None):
        self._api = api
        self._sink = sink
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._layout = JournalLayout.of(self._data_dir) if self._data_dir is not None else None
        self._config = config or RecorderConfig()
        self._clock = clock or _utc_now
        if monotonic is not None:
            self._mono = monotonic
        elif clock is not None:
            self._mono = lambda: self._clock().timestamp()
        else:
            self._mono = _system_monotonic
        self._app_version = app_version
        self._journal_policy = journal_policy
        self._lock = threading.RLock()
        rules = rules or RecorderRules()
        self._automatic = bool(self._config.automatic)
        if not self._automatic:
            rules = rules.with_changes(auto_start=False)
        self._recorder = FlightRecorder(rules, id_factory=lambda: new_flight_id(self._clock()), now=self._clock)
        self._fields = snapshot_simvars()
        self._identity_fields = attr.identity_simvars()
        self._lookup = (attr.NearbyAirportLookup(api, timeout_s=self._config.airport_timeout_s, clock=self._mono)
                        if api is not None and self._config.airport_lookup else None)
        self._planned: tuple[str | None, tuple[str, ...]] = (planned_destination, ())

        self._connection: Connection | None = None
        self._main: _Sub | None = None
        self._retiring: _Sub | None = None
        self._switch_backoff_until = 0.0
        self._identity_sub: str | None = None
        self._event_subs: dict[str, str] = {}      # subscription id -> capability id
        self._unsupported: dict[str, str] = {}
        self._subscribe_error: str | None = None
        self._next_subscribe = 0.0
        self._released = False
        self._last_fed: tuple[str | None, str, float | None] | None = None  # (session, sub id, sim time)
        # (capability, reason) -> [monotonic time of the last journalled note, repeats not journalled since]
        self._signal_notes: dict[tuple[str, str], list[float | int]] = {}
        self._last_sample: Sample | None = None
        self._last_sample_mono: float | None = None
        self._identity: attr.AircraftIdentity | None = None
        self._identity_key: tuple | None = None

        self._flight: _Flight | None = None
        self._finishing: dict[str, _Flight] = {}
        self._lookup_tokens: dict[str, tuple[str, str]] = {}  # token -> (flight id, role)
        self._lookup_queue: list[tuple[str, str, attr.Position | None]] = []
        self._held: list[RecorderEvent] = []                # flightless MANUAL_START, see _route
        self._error: str | None = None
        self._error_mono = 0.0
        self._failed: list[_Job] = []
        self._next_retry = 0.0
        self._last_flight: LastFlight | None = None
        self._recent: deque[RecorderEvent] = deque(maxlen=self._config.recent_events)
        self._recovery_errors: list[str] = []
        self._closed = False
        self._close_result: SaveResult | None = None
        self._saver = _Saver(self._run_job, background=self._config.background_saves)
        self._status = self._compute_status()

    # ---- properties -----------------------------------------------------------------------

    @property
    def recorder(self) -> FlightRecorder:
        """The state machine (read it only on the Core thread or while the service is idle)."""
        return self._recorder

    @property
    def fields(self) -> tuple[Any, ...]:
        return self._fields

    @property
    def layout(self) -> JournalLayout | None:
        return self._layout

    @property
    def closed(self) -> bool:
        return self._closed

    def recent_events(self) -> tuple[RecorderEvent, ...]:
        with self._lock:
            return tuple(self._recent)

    # ---- tick (Core thread) ---------------------------------------------------------------

    def tick(self) -> None:
        """Drain Core and advance Flight Logging. Call on the Core thread after each pump."""
        with self._lock:
            if self._closed:
                if not self._released:
                    self._release_subscriptions()
                return
            if self._api is None or self._layout is None:
                return
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - never take the Core thread down
                log.exception("Flight Recorder tick failed")
                self._set_error(f"The Flight Recorder failed: {exc}")
            self._status = self._compute_status()

    def _tick(self) -> None:
        api = self._api
        now = self._mono()
        if self._connection is None:
            try:
                self._connection = api.connection()
            except Exception:  # noqa: BLE001 - a closed handle; poll reports it
                self._connection = None
        if self._main is None and now >= self._next_subscribe:
            self._subscribe_all()
        for _ in range(max(1, self._config.max_batches_per_tick)):
            try:
                batch = api.poll(limit=self._config.poll_limit)
            except Exception as exc:  # noqa: BLE001
                self._set_error(f"Black Box Core could not deliver simulator data: {exc}")
                log.exception("Recorder poll failed")
                break
            for update in batch:
                self._dispatch(update)
            if len(batch) < self._config.poll_limit:
                break
        self._start_queued_lookups()
        if self._lookup is not None:
            self._settle_lookups(self._lookup.check())
        self._check_sampling()
        self._check_long_gap()
        self._advance_finishing()
        self._journal_ticks()
        if self._failed and self._mono() >= self._next_retry:
            self._retry_failed()

    # ---- subscriptions --------------------------------------------------------------------

    def _desired_cadence(self) -> tuple[SnapshotPeriod, int]:
        hint = self._recorder.sampling_hint
        if hint.high_rate:
            every = self._config.high_every or hint.every
            return SnapshotPeriod(hint.period), max(1, int(every))
        return SnapshotPeriod.SECOND, 1

    def _subscribe_snapshot(self, period: SnapshotPeriod, every: int) -> _Sub | None:
        try:
            subscription = self._api.subscribe_snapshot(self._fields, period=period, every=every)
        except Exception as exc:  # noqa: BLE001 - reported to the pilot, retried
            self._subscribe_error = str(exc) or type(exc).__name__
            log.warning("Recorder snapshot subscription (%s every %s) failed: %s", period.value, every, exc)
            return None
        self._subscribe_error = None
        return _Sub(subscription.id, period, every, self._mono())

    def _subscribe_all(self) -> None:
        self._released = False
        if self._main is None:
            self._main = self._subscribe_snapshot(*self._desired_cadence())
            if self._main is None:
                self._next_subscribe = self._mono() + self._config.resubscribe_after_s
        if self._identity_sub is None:
            try:
                self._identity_sub = self._api.subscribe_snapshot(
                    self._identity_fields, period=SnapshotPeriod.SECOND, every=self._config.identity_every_s).id
            except Exception as exc:  # noqa: BLE001 - identity is optional evidence
                self._unsupported["identity"] = str(exc)
        subscribed = set(self._event_subs.values())
        for capability_id in LIFECYCLE_EVENTS:
            if capability_id in subscribed or capability_id in self._unsupported:
                continue
            try:
                self._event_subs[self._api.subscribe(capability_id).id] = capability_id
            except Exception as exc:  # noqa: BLE001 - not offered by this provider
                self._unsupported[capability_id] = str(exc) or "Not supported."

    def _unsubscribe(self, subscription_id: str | None) -> None:
        if subscription_id is None or self._api is None:
            return
        try:
            self._api.unsubscribe(subscription_id)
        except Exception:  # noqa: BLE001 - the handle may already be closed
            pass

    def _release_subscriptions(self) -> None:
        for sub in (self._main, self._retiring):
            if sub is not None:
                self._unsubscribe(sub.id)
        self._unsubscribe(self._identity_sub)
        for subscription_id in list(self._event_subs):
            self._unsubscribe(subscription_id)
        self._main = self._retiring = None
        self._identity_sub = None
        self._event_subs.clear()
        self._released = True

    def _check_sampling(self) -> None:
        if self._main is None or not self._connected():
            return
        now = self._mono()
        if self._retiring is not None:
            if self._main.delivered == 0 and now - self._main.created_mono > self._config.switch_timeout_s:
                failed, self._main, self._retiring = self._main, self._retiring, None
                self._unsubscribe(failed.id)
                self._switch_backoff_until = now + self._config.resubscribe_after_s
                self._note("sampling_switch_failed", {"to": failed.period.value, "every": failed.every,
                                                      "kept": self._main.period.value})
            return
        if now < self._switch_backoff_until:
            return
        period, every = self._desired_cadence()
        if (period, every) == (self._main.period, self._main.every):
            return
        new = self._subscribe_snapshot(period, every)
        if new is None:
            self._switch_backoff_until = now + self._config.resubscribe_after_s
            return
        self._retiring, self._main = self._main, new

    # ---- dispatch -------------------------------------------------------------------------

    def _dispatch(self, update: Any) -> None:
        try:
            if self._lookup is not None and type(update).__name__ == "FacilityResult":
                self._settle_lookups(self._lookup.offer(update))
                return
            if isinstance(update, Snapshot):
                self._on_snapshot(update)
            elif SignalError is not None and isinstance(update, SignalError):
                self._on_signal_error(update)
            elif isinstance(update, Gap):
                self._feed(update)
            elif isinstance(update, Connection):
                self._on_connection(update)
            elif isinstance(update, Event):
                if self._event_subs.get(getattr(update, "subscription_id", "") or "") or \
                        update.capability_id in LIFECYCLE_EVENTS:
                    self._feed(update)
            elif isinstance(update, SubscriptionEnded):
                self._on_subscription_ended(update)
        except Exception as exc:  # noqa: BLE001 - one bad update must not stop Flight Logging
            log.exception("Recorder failed to handle %s", type(update).__name__)
            self._set_error(f"The Flight Recorder could not process simulator data: {exc}")

    def _on_connection(self, connection: Connection) -> None:
        previous = self._connection
        self._connection = connection
        if connection.state is ConnectionState.CONNECTED and previous is not None \
                and previous.state is ConnectionState.CONNECTED and previous.session_id == connection.session_id:
            return
        self._feed(connection)

    def _on_snapshot(self, snapshot: Snapshot) -> None:
        sub_id = snapshot.subscription_id
        if sub_id == self._identity_sub:
            self._on_identity(snapshot)
            return
        main, retiring = self._main, self._retiring
        if main is not None and sub_id == main.id:
            sub = main
            if retiring is not None:
                # The new cadence delivered: the old one can go. Overlap is fed once (below).
                self._retiring = None
                self._unsubscribe(retiring.id)
        elif retiring is not None and sub_id == retiring.id:
            sub = retiring
        else:
            return
        sub.delivered += 1
        session = snapshot.session_id
        sim_time = _finite(snapshot.simulation_time_s)
        last = self._last_fed
        if last is not None and last[1] != sub_id and last[0] == session and sim_time is not None \
                and last[2] is not None and last[2] - self._config.overlap_window_s < sim_time <= last[2]:
            # The other (retiring) cadence already delivered this simulator instant. Only a sample
            # just behind the last fed one is an overlap duplicate: a larger backwards jump is a
            # simulator time reset (flight restart or reload) and is fed, so the state machine
            # records it as a discontinuity instead of the new subscription being ignored.
            return
        self._last_fed = (session, sub_id, sim_time)
        sample = sample_from_snapshot(snapshot, high_rate=sub.period is not SnapshotPeriod.SECOND)
        self._last_sample = sample
        self._last_sample_mono = self._mono()
        self._feed(sample)
        flight = self._flight
        if flight is not None and flight.journal is not None:
            interval = (self._config.track_interval_s if sub.period is SnapshotPeriod.SECOND
                        else self._config.track_interval_high_s)
            now = self._mono()
            if flight.last_track_mono is None or now - flight.last_track_mono >= interval * 0.95:
                flight.last_track_mono = now
                self._append(flight, RecorderEvent(EventKind.SAMPLE, flight.phase, sample.host_time,
                                                   sample.sim_time_s, sample.sim_utc, _sample_json(sample, sub.period)))

    def _on_identity(self, snapshot: Snapshot) -> None:
        identity = attr.identify_aircraft(attr.readings_from_snapshot(snapshot))
        key = (identity.type.display.value, identity.type.designator.value, identity.registration.value,
               identity.callsign.callsign.value)
        if key == self._identity_key:
            return
        self._identity, self._identity_key = identity, key
        if self._flight is not None:
            self._journal_identity(self._flight)

    def _journal_identity(self, flight: _Flight) -> None:
        identity = self._identity
        if identity is None or flight.identity_key == self._identity_key:
            return
        flight.identity_key = self._identity_key
        self._append(flight, self._service_event(flight, EventKind.ATTRIBUTION, {
            "role": "identity", "identity": identity.to_json(), "fields": identity.record_fields(),
            "rules_version": attr.RULES_VERSION,
        }))

    def _on_signal_error(self, error: Any) -> None:
        ours = {sub.id for sub in (self._main, self._retiring) if sub is not None}
        ours.update(self._event_subs)
        if self._identity_sub:
            ours.add(self._identity_sub)
        sub_id = getattr(error, "subscription_id", "") or ""
        if sub_id and sub_id not in ours:
            return
        # A provider error for one signal: that value is unknown (None in the snapshot), not lost data.
        # Core repeats it with every delivery while the reading is in error, so one note per
        # capability and reason per interval is journalled, with the count of repeats in between.
        capability, reason = str(getattr(error, "capability_id", "") or ""), str(getattr(error, "reason", "") or "")
        if self._flight is None:
            return
        now = self._mono()
        entry = self._signal_notes.get((capability, reason))
        if entry is not None and now - float(entry[0]) < self._config.signal_note_interval_s:
            entry[1] = int(entry[1]) + 1
            return
        repeats = int(entry[1]) if entry is not None else 0
        self._signal_notes[(capability, reason)] = [now, 0]
        self._note("signal_error", {"capability_id": capability, "subscription_id": sub_id, "reason": reason,
                                    "replaced_updates": getattr(error, "dropped_updates", None),
                                    "repeats_not_journalled": repeats})

    def _flush_signal_notes(self, flight: _Flight) -> None:
        """Journal the repeats of signal errors not yet journalled, then forget them (flight end)."""
        pending = [{"capability_id": capability, "reason": reason, "repeats_not_journalled": int(entry[1])}
                   for (capability, reason), entry in sorted(self._signal_notes.items()) if int(entry[1])]
        self._signal_notes.clear()
        if pending:
            self._append(flight, self._service_event(flight, EventKind.NOTE, {
                "note": "signal_error_repeats", "errors": pending}))

    def _on_subscription_ended(self, ended: SubscriptionEnded) -> None:
        sub_id = ended.subscription_id
        if self._main is not None and sub_id == self._main.id:
            self._main, self._retiring = self._retiring, None
            if self._main is None:
                self._main = self._subscribe_snapshot(*self._desired_cadence())
                if self._main is None:
                    self._next_subscribe = self._mono() + self._config.resubscribe_after_s
            self._note("subscription_ended", {"reason": ended.reason, "resubscribed": self._main is not None})
        elif self._retiring is not None and sub_id == self._retiring.id:
            self._retiring = None
        elif sub_id == self._identity_sub:
            self._identity_sub = None
            self._subscribe_all()
        elif sub_id in self._event_subs:
            capability_id = self._event_subs.pop(sub_id)
            try:
                self._event_subs[self._api.subscribe(capability_id).id] = capability_id
            except Exception as exc:  # noqa: BLE001
                self._unsupported[capability_id] = str(exc)

    # ---- the state machine and journals ---------------------------------------------------

    def _feed(self, update: Any) -> None:
        self._handle(self._recorder.process(update))

    def _handle(self, events: Iterable[RecorderEvent]) -> None:
        for event in events:
            self._recent.append(event)
            self._route(event)

    def _route(self, event: RecorderEvent) -> None:
        flight_id = event.data.get("flight_id") if isinstance(event.data, Mapping) else None
        if event.kind is EventKind.FLIGHT_STARTED:
            self._open_flight(event)
            for held in self._held:
                self._append(self._flight, replace(held, data={**held.data, "flight_id": self._flight.flight_id}))
                self._flight.started_manually = True
            self._held.clear()
            self._journal_identity(self._flight)
            return
        if flight_id is None:
            if event.kind is EventKind.MANUAL_START:
                self._held.append(event)  # emitted just before the FLIGHT_STARTED it causes
            return
        flight = self._flight if self._flight is not None and self._flight.flight_id == flight_id \
            else self._finishing.get(flight_id)
        if flight is None:
            return
        if event.kind is not EventKind.SAMPLE:
            flight.phase = event.phase
        self._append(flight, event)
        if event.kind is EventKind.BLOCK_OFF:
            self._queue_lookup(flight, "departure", event)
        elif event.kind is EventKind.BLOCK_ON:
            self._queue_lookup(flight, "arrival", event)
        elif event.kind is EventKind.FLIGHT_ENDED:
            self._flight_ended(flight, event)

    def _open_flight(self, event: RecorderEvent) -> None:
        if self._flight is not None:
            # The state machine never opens a flight over an open one; be safe anyway.
            self._interrupt_current("A new flight started before the previous one was closed.")
        flight_id = str(event.data.get("flight_id"))
        self._signal_notes.clear()  # errors before this flight are not its evidence
        connection = self._connection
        source = getattr(connection, "source", None)
        simulated = bool(getattr(source, "simulated", False))
        identity = self._identity
        aircraft = None
        if identity is not None:
            aircraft = {name: value.get("value") for name, value in identity.record_fields().items()}
        journal, error = None, None
        try:
            journal = Journal.create(
                self._layout, flight_id, app_version=self._app_version, started_host_time=event.host_time,
                started_sim_time_s=event.sim_time_s, started_sim_utc=event.sim_utc, aircraft=aircraft,
                source={"name": getattr(source, "name", None), "simulated": simulated,
                        "session_id": getattr(connection, "session_id", None)},
                rules_version=RULES_VERSION,
                extra={"service_version": SERVICE_VERSION, "trigger": event.data.get("trigger"),
                       "attribution_rules": attr.RULES_VERSION},
                policy=self._journal_policy, clock=self._mono, now=self._clock)
        except (JournalError, OSError) as exc:
            error = str(exc) or type(exc).__name__
            log.error("Flight journal %s could not be created: %s", flight_id, error)
            self._set_error((f"Flight Logging cannot write its journal ({error}). This flight is not being "
                           "logged; check the data folder."))
        sim_utc = event.sim_utc or (self._last_sample.sim_utc if self._last_sample else None)
        self._flight = _Flight(flight_id, journal, event.host_time, sim_utc, str(event.data.get("trigger") or ""),
                               simulated, started_manually=event.data.get("trigger") == "manual",
                               journal_error=error)
        self._append(self._flight, event)

    def _append(self, flight: _Flight, event: RecorderEvent) -> None:
        journal = flight.journal
        if journal is None:
            return
        try:
            journal.append(event)
        except JournalError as exc:
            flight.journal_error = str(exc) or type(exc).__name__
            log.error("Flight journal %s write failed: %s", flight.flight_id, flight.journal_error)
            self._set_error(f"Flight Logging could not write its journal: {flight.journal_error}")

    def _service_event(self, flight: _Flight, kind: EventKind, data: dict[str, Any]) -> RecorderEvent:
        return RecorderEvent(kind, flight.phase, self._clock(), None, None, {"flight_id": flight.flight_id, **data})

    def _note(self, note: str, data: dict[str, Any]) -> None:
        flight = self._flight
        if flight is not None:
            self._append(flight, self._service_event(flight, EventKind.NOTE, {"note": note, **data}))

    def _journal_ticks(self) -> None:
        for flight in [self._flight, *self._finishing.values()]:
            if flight is not None and flight.journal is not None and not flight.journal.closed:
                try:
                    flight.journal.tick()
                except JournalError as exc:
                    flight.journal_error = str(exc)
                    self._set_error(f"Flight Logging could not save its journal to disk: {exc}")

    # ---- airports -------------------------------------------------------------------------

    def _queue_lookup(self, flight: _Flight, role: str, event: RecorderEvent | None) -> None:
        if self._lookup is None:
            return
        data = event.data if event is not None else {}
        lat, lon = _finite(data.get("lat")), _finite(data.get("lon"))
        on_ground = "airborne" not in str(data.get("basis") or "")
        sample = self._last_sample
        if (lat is None or lon is None) and sample is not None and sample.on_ground:
            lat, lon = _finite(sample.lat), _finite(sample.lon)
        position = None
        if lat is not None and lon is not None:
            connection = self._connection
            source = getattr(connection, "source", None)
            try:
                position = attr.Position(
                    lat, lon, on_ground, event.host_time if event is not None else self._clock(),
                    getattr(connection, "session_id", None), getattr(source, "name", None),
                    getattr(source, "simulated", None), event.sim_time_s if event is not None else None,
                    event.sim_utc if event is not None else None)
            except ValueError:
                position = None
        flight.lookups.add(role)
        self._lookup_queue.append((flight.flight_id, role, position))

    def _start_queued_lookups(self) -> None:
        if self._lookup is None:
            self._lookup_queue.clear()
            return
        queued, self._lookup_queue = self._lookup_queue, []
        for flight_id, role, position in queued:
            # The lookup's role label carries the flight, so each answer finds its flight exactly.
            key = f"{role}@{flight_id}"
            self._lookup_tokens[self._lookup.begin(key, position)] = (flight_id, role)
        # begin() may finish at once (no position, request refused): drain those now.
        self._settle_lookups(self._lookup.offer(None))

    def _cancel_lookups(self, flight_id: str) -> None:
        for token, (owner, _role) in list(self._lookup_tokens.items()):
            if owner == flight_id:
                self._lookup_tokens.pop(token, None)
                if self._lookup is not None:
                    self._lookup.cancel(token)
        self._lookup_queue = [item for item in self._lookup_queue if item[0] != flight_id]

    def _settle_lookups(self, results: Iterable[attr.AirportAttribution]) -> None:
        for result in results:
            role, _, flight_id = str(result.role).partition("@")
            for token, owner in list(self._lookup_tokens.items()):
                if owner == (flight_id, role):
                    self._lookup_tokens.pop(token, None)
                    break
            flight = self._flight if self._flight is not None and self._flight.flight_id == flight_id \
                else self._finishing.get(flight_id)
            if flight is None:
                continue
            flight.lookups.discard(role)
            result = replace(result, role=role)
            data: dict[str, Any] = {"role": role, "attribution": result.to_json(),
                                    "record_field": result.airport.to_record_field(),
                                    "rules_version": attr.RULES_VERSION}
            if role == "departure":
                flight.departure = result.icao if result.airport.status is attr.Status.KNOWN else None
            elif role == "arrival":
                planned, alternates = self._planned
                assessment = attr.assess_arrival(result, planned, alternates=alternates,
                                                 departure_icao=flight.departure)
                data["assessment"] = assessment.to_json()
            self._append(flight, self._service_event(flight, EventKind.ATTRIBUTION, data))

    def set_planned_destination(self, icao: str | None, alternates: Iterable[str] = ()) -> None:
        """The flight plan's destination, for the suspected-diversion check (never a confirmation)."""
        with self._lock:
            self._planned = (icao, tuple(alternates))

    # ---- ending flights -------------------------------------------------------------------

    def _flight_ended(self, flight: _Flight, end: RecorderEvent) -> None:
        if self._flight is flight:
            self._flight = None
            self._flush_signal_notes(flight)
        flight.ended = end
        outcome = str(end.data.get("outcome") or "abandoned")
        flight.loggable = bool(end.data.get("loggable")) and outcome != "discarded"
        if not flight.loggable:
            if self._close_journal(flight, Outcome.DISCARDED, end, "Nothing loggable was observed: "
                                   + _plain(end.data.get("reason") or "no block-off, takeoff or landing") + "."):
                self._submit(_Job("discard", flight.flight_id, "discarded", "No block-off, takeoff or landing "
                                  "was observed, so nothing was added to the Logbook."))
            return
        summary = end.data.get("summary") if isinstance(end.data.get("summary"), Mapping) else {}
        landed = bool(summary.get("touchdowns"))
        if outcome == "complete" and "arrival" not in flight.lookups and landed and not summary.get("block_on"):
            # Landed and taxiing, then closed without block-on (aircraft change, flight load, Stop).
            sample = self._last_sample
            if sample is not None and sample.on_ground:
                self._queue_lookup(flight, "arrival", RecorderEvent(
                    EventKind.BLOCK_ON, end.phase, sample.host_time, sample.sim_time_s, sample.sim_utc,
                    {"lat": sample.lat, "lon": sample.lon, "basis": "position at flight end"}))
        self._finishing[flight.flight_id] = flight
        flight.finish_deadline = self._mono() + self._config.arrival_wait_s
        if not flight.lookups:
            self._finish(flight)

    def _advance_finishing(self) -> None:
        now = self._mono()
        for flight in list(self._finishing.values()):
            if not flight.lookups or (flight.finish_deadline is not None and now >= flight.finish_deadline):
                self._finish(flight)

    def _finish(self, flight: _Flight) -> None:
        self._finishing.pop(flight.flight_id, None)
        self._cancel_lookups(flight.flight_id)
        if flight.lookups:
            self._append(flight, self._service_event(flight, EventKind.NOTE, {
                "note": "attribution_timeout", "roles": sorted(flight.lookups)}))
        end = flight.ended
        outcome = str(end.data.get("outcome") or "abandoned")
        journal_outcome = Outcome.COMPLETE if outcome == "complete" else Outcome.ABANDONED
        if self._close_journal(flight, journal_outcome, end, str(end.data.get("reason") or "")):
            self._submit(_Job("closed", flight.flight_id, outcome, ""))
        else:
            self._last_flight = LastFlight(flight.flight_id, outcome, False, None,
                                           "The flight could not be saved: its journal could not be written"
                                           + (f" ({flight.journal_error})." if flight.journal_error else "."))

    def _close_journal(self, flight: _Flight, outcome: Outcome, end: RecorderEvent | None, detail: str) -> bool:
        journal = flight.journal
        if journal is None or journal.closed:
            return False
        try:
            journal.close(outcome, ended_host_time=end.host_time if end else None,
                          ended_sim_time_s=end.sim_time_s if end else None,
                          ended_sim_utc=end.sim_utc if end else None,
                          final_phase=end.phase if end else None, detail=detail or None,
                          extra=self._footer_extra(flight))
            return True
        except JournalError as exc:
            flight.journal_error = str(exc)
            log.error("Flight journal %s could not be closed: %s", flight.flight_id, exc)
            self._set_error(f"Flight Logging could not close its journal: {exc}. It is kept for recovery.")
            journal.abandon()
            return False

    def _footer_extra(self, flight: _Flight) -> dict[str, Any]:
        """Footer ``extra``: the service version and any journal write error this flight hit.

        The journal adds its own ``lost_lines`` block when an event could not be written.
        """
        extra: dict[str, Any] = {"service_version": SERVICE_VERSION}
        if flight.journal_error:
            extra["journal_error"] = flight.journal_error
        return extra

    def _interrupt_current(self, detail: str) -> None:
        """End the open flight as *interrupted* (Save & Exit, long gap): no invented end time."""
        flight = self._flight
        if flight is None:
            return
        summary = self._recorder.summary()
        loggable = bool(summary is not None and summary.flight_id == flight.flight_id and summary.loggable)
        self._handle(self._recorder.interrupt_flight(detail))
        self._flight = None
        self._cancel_lookups(flight.flight_id)
        self._flush_signal_notes(flight)
        if flight.journal is None:
            return
        if not loggable:
            if self._close_journal(flight, Outcome.DISCARDED, None, "Nothing loggable was observed before: "
                                   + _plain(detail) + "."):
                self._submit(_Job("discard", flight.flight_id, "discarded", detail))
            return
        lost = flight.journal.lost_summary()
        if lost is not None:
            # The footer is written later by recovery, which cannot see a write that left no bytes:
            # journal the writer's count now (if the disk is back) and hand it to the save job too.
            self._append(flight, self._service_event(flight, EventKind.NOTE, {
                "note": recovery.LOST_LINES_NOTE, "lost_lines": lost}))
            lost = flight.journal.lost_summary()
        footer_extra = self._footer_extra(flight)
        if lost is not None:
            footer_extra["lost_lines"] = lost
        flight.journal.abandon()  # no footer: recovery appends the interruption gap and closes it
        self._submit(_Job("interrupted", flight.flight_id, "interrupted", detail, footer_extra=footer_extra))

    def _check_long_gap(self) -> None:
        limit = self._config.max_open_gap_s
        if limit is None or self._flight is None or self._last_sample_mono is None:
            return
        quiet = self._mono() - self._last_sample_mono
        if quiet >= limit and not (self._connected() and self._recorder.paused):
            minutes = int(quiet // 60)
            self._interrupt_current(f"No simulator data for {minutes} minutes; the flight was saved as interrupted")

    # ---- saving (worker thread or inline) -------------------------------------------------

    def _submit(self, job: _Job) -> None:
        self._saver.submit(job)

    def _run_job(self, job: _Job) -> None:
        layout = self._layout
        message = ""
        record_id = None
        try:
            if job.kind == "discard":
                recovery.discard_unfinished(job.flight_id, data_dir=layout, now=self._clock(), detail=job.detail)
                message = "Nothing was logged: no block-off, takeoff or landing was observed."
            else:
                if self._sink is None:
                    raise recovery.RecoveryError("No Logbook is connected, so the flight cannot be saved yet.")
                if job.kind == "closed":
                    result = recovery.finalize_closed(layout, job.flight_id, sink=self._sink,
                                                      record_builder=build_recorded_flight)
                else:
                    result = recovery.finalize_interrupted(job.flight_id, data_dir=layout, sink=self._sink,
                                                           now=self._clock(), record_builder=build_recorded_flight,
                                                           detail=job.detail, footer_extra=job.footer_extra)
                record_id = result.saved_id
                message = f"Flight logged ({job.outcome})."
        except Exception as exc:  # noqa: BLE001 - keep the journal; integrity first
            job.attempts += 1
            job.error = str(exc) or type(exc).__name__
            log.warning("Recorder could not save flight %s: %s", job.flight_id, job.error)
            with self._lock:
                if job not in self._failed:
                    self._failed.append(job)
                self._next_retry = self._mono() + self._config.retry_saves_s
                if job.kind != "discard":
                    self._last_flight = LastFlight(job.flight_id, job.outcome, False, None,
                                                   f"The flight was not saved yet: {job.error}")
                self._status = self._compute_status()
            return
        with self._lock:
            if job in self._failed:
                self._failed.remove(job)
            self._last_flight = LastFlight(job.flight_id, job.outcome, job.kind != "discard", record_id, message)
            self._status = self._compute_status()

    def _retry_failed(self) -> None:
        jobs, self._failed = list(self._failed), []
        for job in jobs:
            self._submit(job)

    def retry_saves(self) -> int:
        """Retry flights the Logbook refused now. Returns how many are still waiting."""
        with self._lock:
            self._retry_failed()
        self._saver.wait(self._config.close_timeout_s)
        with self._lock:
            return len(self._failed)

    # ---- pilot controls (any thread) ------------------------------------------------------

    def set_automatic(self, enabled: bool) -> None:
        """Automatic Flight Logging on or off. Off: flights start only with Start Logging."""
        with self._lock:
            self._automatic = bool(enabled)
            recorder = self._recorder
            recorder.rules = recorder.rules.with_changes(auto_start=self._automatic)
            if self._automatic:
                recorder.resume_automatic()
            elif self._flight is not None and not self._flight.started_manually:
                summary = recorder.summary()
                if summary is not None and not summary.loggable:
                    self._interrupt_current("Automatic Flight Logging was switched off")
            self._status = self._compute_status()

    def manual_start(self) -> ActionResult:
        """Start Logging (manual override)."""
        with self._lock:
            if self._closed or self._api is None or self._layout is None:
                return ActionResult(False, self._status.detail or "Flight Logging is not available.")
            flight = self._flight
            if flight is not None:
                if not flight.started_manually:
                    flight.started_manually = True
                    self._note("manual_start", {"detail": "Start Logging pressed while the flight was armed."})
                    self._status = self._compute_status()
                    return ActionResult(True, "Flight Logging is on for this flight.", flight_id=flight.flight_id)
                return ActionResult(False, "A flight is already being logged.", flight_id=flight.flight_id)
            if not self._connected():
                return ActionResult(False, "Connect to the simulator before starting Flight Logging.")
            self._handle(self._recorder.manual_start(self._clock(), "Start Logging pressed by the pilot"))
            self._status = self._compute_status()
            if self._flight is None:
                return ActionResult(False, "The Flight Recorder did not start a flight.")
            return ActionResult(True, "Flight Logging started.", flight_id=self._flight.flight_id)

    def manual_stop(self, confirmed: bool = False) -> ActionResult:
        """Stop Logging. Nothing changes unless ``confirmed`` is True."""
        with self._lock:
            flight = self._flight
            if flight is None:
                return ActionResult(False, "No flight is being logged.")
            if confirmed is not True:
                return ActionResult(False, "Stop Flight Logging for this flight? It is saved to the Logbook as it "
                                           "stands: complete only if the aircraft has landed.",
                                    needs_confirmation=True, flight_id=flight.flight_id)
            self._handle(self._recorder.manual_stop(self._clock(), "Stop Logging confirmed by the pilot"))
            if self._flight is flight:  # the state machine did not end it (it always does today)
                self._interrupt_current("Stop Logging confirmed by the pilot")
            self._status = self._compute_status()
            return ActionResult(True, "Flight Logging stopped.", flight_id=flight.flight_id)

    def save_and_close(self, timeout_s: float = 10.0) -> SaveResult:
        """Save & Exit: save an open flight as interrupted, finish pending saves, stop logging."""
        with self._lock:
            if self._closed and self._close_result is not None:
                return self._close_result
            had_open = self._flight is not None and self._recorder.summary() is not None \
                and self._recorder.summary().loggable
            self._interrupt_current("Saved as interrupted at Save & Exit")
            for flight in list(self._finishing.values()):
                flight.lookups.clear()
                self._finish(flight)
            self._lookup_queue.clear()
            self._retry_failed()
            self._closed = True
        finished = self._saver.wait(timeout_s)
        with self._lock:
            failed = list(self._failed)
            if not finished:
                result = SaveResult(False, f"Saving did not finish within {timeout_s:g} s. The flight journal is "
                                           "kept and will be offered again at the next start.")
            elif failed:
                result = SaveResult(False, f"{len(failed)} flight(s) could not be saved to the Logbook "
                                           f"({failed[0].error}). Their journals are kept and will be offered "
                                           "again at the next start.")
            elif had_open:
                result = SaveResult(True, "The open flight was saved to the Logbook as interrupted.")
            else:
                result = SaveResult(True, "No flight was being logged." if self._last_flight is None
                                    else "Flight Logging stopped; every finished flight is saved.")
            self._close_result = result
            self._status = self._compute_status()
            return result

    def close(self) -> None:
        """Stop Flight Logging. An open flight is saved as interrupted first, as in Save & Exit."""
        if not self._closed:
            self.save_and_close(self._config.close_timeout_s)
        self._saver.stop()

    # ---- recovery (startup) ---------------------------------------------------------------

    def unfinished(self) -> list[recovery.RecoveredFlight]:
        """Journals left unfinished by an earlier run (read-only), oldest first."""
        if self._layout is None:
            return []
        return recovery.find_unfinished(self._layout, load_events=False)

    def recover_unfinished(self) -> list[recovery.FinalizeResult]:
        """Save every flight an earlier run left unfinished as an *interrupted* record.

        A journal that already holds its FLIGHT_ENDED (the app stopped during the post-block-on
        arrival wait) is saved with that journalled outcome instead (see ``recovery.finalize_interrupted``).

        Journals with nothing loggable in them are moved to ``recorder/discarded/`` instead (kept,
        never deleted). A flight the Logbook refuses stays where it is and is offered again next
        time; the reasons are in :attr:`recovery_errors`. Journals this service is writing are
        locked and left alone. Call from the app at startup (any thread).
        """
        saved: list[recovery.FinalizeResult] = []
        errors: list[str] = []
        if self._layout is None:
            return saved
        for found in recovery.find_unfinished(self._layout, load_events=False):
            if not found.recoverable:
                errors.append(f"{found.flight_id}: {found.reason}")
                continue
            footer_outcome = (found.footer or {}).get("outcome")
            try:
                if footer_outcome == Outcome.DISCARDED.value or not journal_is_loggable(found.notable_events):
                    recovery.discard_unfinished(found, data_dir=self._layout, now=self._clock(),
                                                detail="Nothing loggable was recorded; discarded at startup.")
                    continue
                if self._sink is None:
                    errors.append(f"{found.flight_id}: no Logbook is connected.")
                    continue
                saved.append(recovery.finalize_interrupted(
                    found, data_dir=self._layout, sink=self._sink, now=self._clock(),
                    record_builder=build_recorded_flight,
                    detail="BBC EFB stopped before the flight was closed; saved as interrupted at startup."))
            except Exception as exc:  # noqa: BLE001 - leave it for next time
                log.warning("Recovery of flight %s failed: %s", found.flight_id, exc)
                errors.append(f"{found.flight_id}: {exc}")
        with self._lock:
            self._recovery_errors = errors
        return saved

    @property
    def recovery_errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._recovery_errors)

    # ---- status ---------------------------------------------------------------------------

    def status(self) -> RecorderStatus:
        """The Flight Logging status as of the last tick or action. Any thread."""
        return self._status

    ERROR_HOLD_S = 10.0

    def _set_error(self, message: str) -> None:
        """Show ``message`` as an error for ERROR_HOLD_S seconds after it last happened."""
        self._error = message
        self._error_mono = self._mono()

    def _active_error(self) -> str | None:
        flight = self._flight
        if flight is not None and flight.journal_error:
            if flight.journal is None:
                return (f"Flight Logging cannot write its journal ({flight.journal_error}). This flight is not "
                        "being logged; check the data folder.")
            return f"Flight Logging could not write its journal: {flight.journal_error}"
        if self._error and self._mono() - self._error_mono <= self.ERROR_HOLD_S:
            return self._error
        return None

    def _connected(self) -> bool:
        return self._connection is not None and self._connection.state is ConnectionState.CONNECTED

    def _compute_status(self) -> RecorderStatus:
        connection = self._connection
        simulated = bool(getattr(getattr(connection, "source", None), "simulated", False))
        flight = self._flight
        recorder = self._recorder
        summary = recorder.summary() if flight is not None else None
        if summary is not None and summary.flight_id != flight.flight_id:
            summary = None
        phase = recorder.phase if flight is not None else None
        pending = len(self._failed) + self._saver.busy + len(self._finishing)
        armed_only = (flight is not None and phase is Phase.PREFLIGHT and not flight.started_manually
                      and not (summary is not None and summary.loggable))
        connection_text = (connection.state.value if connection is not None
                           else "unavailable" if self._api is None else "unknown")
        quiet = (self._last_sample_mono is not None and self._mono() - self._last_sample_mono
                 > self._config.stale_after_s)

        if self._api is None or self._layout is None:
            state = "unavailable"
            detail = ("Black Box Core is not running, so flights cannot be logged." if self._api is None
                      else "No data folder is open, so flights cannot be logged. Choose a data folder.")
        elif self._closed:
            state = "unavailable"
            detail = self._close_result.detail if self._close_result else "Flight Logging has stopped."
        elif self._active_error():
            state = "error"
            detail = self._active_error()
        elif self._failed:
            state = "error"
            job = self._failed[0]
            if job.kind == "discard":
                detail = (f"An empty recording could not be filed under recorder/discarded ({job.error}). It is "
                          "kept and retried.")
            else:
                detail = (f"A finished flight is not saved yet ({job.error}). Its journal is kept and saving is "
                          "retried; it is also offered again at the next start.")
        elif self._finishing or self._saver.busy:
            state = "saving"
            detail = "Saving the last flight to the Logbook."
        elif flight is not None and not armed_only:
            if not self._connected():
                state = "gap"
                detail = (_plain(getattr(connection, "detail", "")) or "The simulator connection was lost") + \
                    ". The flight stays open and the missing period is logged as a gap."
            elif self._main is None:
                state = "gap"
                detail = (f"Flight Logging cannot subscribe to simulator data ({self._subscribe_error}). "
                          "The flight stays open.")
            elif recorder.paused:
                state = "paused"
                detail = "The simulator is paused. The flight stays open and pause time is not credited."
            elif quiet:
                state = "gap"
                detail = "No simulator data for a few seconds. The flight stays open and the gap is logged."
            else:
                state = "recording"
                detail = PHASE_LABELS.get(phase, str(phase)) + "."
        elif not self._connected():
            state = "inactive"
            detail = _plain(getattr(connection, "detail", "")) or "Waiting for the simulator"
            detail += "."
        elif self._main is None:
            state = "error"
            detail = f"Flight Logging cannot subscribe to simulator data: {self._subscribe_error}"
        elif not self._automatic:
            state = "inactive"
            detail = "Automatic Flight Logging is off. Use Start Logging to log a flight."
        else:
            state = "armed"
            detail = ("Waiting for block-off. The flight is logged automatically." if flight is not None
                      else "A flight is logged automatically when it starts.")
            if recorder.suspended_reason:
                detail = (f"Automatic logging is held after a {recorder.suspended_reason}. Use Start Logging, "
                          "or load a flight or aircraft.")
        label = STATE_LABELS[state]
        if state == "recording" and phase is not None:
            label = f"{label} · {PHASE_LABELS.get(phase, str(phase))}"
        aircraft = None
        if self._identity is not None:
            aircraft = self._identity.type.display.value
        if aircraft is None and summary is not None:
            aircraft = summary.aircraft
        block = summary.block_s if summary is not None else None
        airborne = summary.airborne_s if summary is not None else None
        sampling = self._main.period.value if self._main is not None else SnapshotPeriod.SECOND.value
        return RecorderStatus(
            state=state, phase=phase, detail=detail, flight_id=flight.flight_id if flight else None,
            started_utc=flight.started_sim_utc if flight else None, aircraft=aircraft,
            departure=flight.departure if flight else None, block_time_s=block, airborne_time_s=airborne,
            simulated=simulated, label=label,
            can_start=(state != "unavailable" and self._connected() and not self._closed
                       and (flight is None or armed_only)),
            can_stop=flight is not None and not armed_only and not self._closed,
            automatic=self._automatic,
            # Saving counts as open: closing the EFB now must still go through Save & Exit.
            flight_open=(flight is not None and not armed_only) or state == "saving",
            started_manually=bool(flight and flight.started_manually), pending_saves=pending,
            sampling=sampling, connection=connection_text, last_flight=self._last_flight,
        )


__all__ = [
    "ActionResult", "LIFECYCLE_EVENTS", "LastFlight", "PHASE_LABELS", "PROPOSALS", "RecorderConfig",
    "RecorderService", "RecorderStatus", "SERVICE_VERSION", "STATES", "STATE_LABELS", "SaveResult",
]
