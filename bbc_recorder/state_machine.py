"""Flight Recorder state machine: pure, deterministic and offline-testable.

Feed it coherent telemetry ``Sample`` objects (normally built from a Core ``Snapshot``) and
``Lifecycle`` notices (pause flags, crash, aircraft or flight loaded, position changed,
disconnect, provider gap). It returns ``RecorderEvent`` objects to journal and keeps a running
``FlightSummary``. It never reads a clock of its own (except an optional ``now`` callable used
only for Core updates that carry no timestamp), never touches Qt or SimConnect, and never
invents values: anything it cannot measure stays ``None`` and carries a reason.

Rules (thresholds and owner-decision switches) live in ``bbc_recorder.rules``; the semantics
and the open owner decisions are documented in ``bbc_recorder/RULES.md``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from .model import EventKind, Phase, RecorderEvent
from .rules import ENGINE_COUNT, RECORDER_FIELDS, RULES_VERSION, BlockOnPolicy, PausePolicy, RecorderRules

UTC = timezone.utc
_SIM_EPOCH = datetime(1, 1, 1, tzinfo=UTC)

# Pause_EX1 bits (mirrors bbc_core.contract.PauseState without importing Core).
PAUSE_FULL = 1
PAUSE_FULL_WITH_SOUND = 2
PAUSE_ACTIVE = 4
PAUSE_SIM = 8

_UNKNOWN_RATE_CEILING = 16.0  # allowed sim/host ratio when the sim rate is not reported

ACTIVE_PHASES = frozenset({Phase.PREFLIGHT, Phase.TAXI_OUT, Phase.TAKEOFF_ROLL, Phase.AIRBORNE,
                           Phase.LANDING_ROLL, Phase.TAXI_IN})


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value

    __format__ = str.__format__


# ============================================================================================
# Inputs
# ============================================================================================

@dataclass(frozen=True, slots=True)
class Sample:
    """One coherent telemetry sample. ``None`` always means "not known", never zero."""

    host_time: datetime                      # aware UTC host receipt time
    sim_time_s: float | None = None          # SIMULATION TIME
    sim_utc: datetime | None = None          # simulator UTC (ABSOLUTE TIME)
    sim_rate: float | None = None
    on_ground: bool | None = None
    ground_speed_kt: float | None = None
    ias_kt: float | None = None
    vs_fpm: float | None = None
    alt_agl_ft: float | None = None
    alt_msl_ft: float | None = None
    lat: float | None = None
    lon: float | None = None
    heading_deg: float | None = None
    parking_brake: bool | None = None
    engines_running: tuple[bool | None, ...] | None = None
    touchdown_velocity_fpm: float | None = None  # PLANE TOUCHDOWN NORMAL VELOCITY, latched by MSFS
    slew_active: bool | None = None
    pause_flags: int | None = None           # Pause_EX1 flags when the caller merges them in
    aircraft: str | None = None              # TITLE or aircraft file, when known
    high_rate: bool | None = None            # sample came from a per-frame subscription
    simulated: bool | None = None            # provider is the simulated one
    session_id: str | None = None


class LifecycleKind(_ValueEnum):
    PAUSE_FLAGS = "pause_flags"          # value: int Pause_EX1 flags
    CRASHED = "crashed"
    CRASH_RESET = "crash_reset"
    AIRCRAFT_LOADED = "aircraft_loaded"  # value: aircraft file or title
    FLIGHT_LOADED = "flight_loaded"      # value: flight file
    POSITION_CHANGED = "position_changed"
    DISCONNECTED = "disconnected"        # detail: why
    CONNECTED = "connected"
    GAP = "gap"                          # value: dropped update count, detail: reason
    SIM_STARTED = "sim_started"
    SIM_STOPPED = "sim_stopped"


@dataclass(frozen=True, slots=True)
class Lifecycle:
    kind: LifecycleKind
    host_time: datetime
    value: Any = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SamplingHint:
    """Advice for the caller's snapshot subscription. ``period`` uses SnapshotPeriod values."""

    period: str          # "second" or "sim_frame"
    every: int
    high_rate: bool
    reason: str


@runtime_checkable
class FlightSink(Protocol):
    """Where a finished flight goes. The integration phase adapts bbc_efb.storage to this."""

    def save_recorded_flight(self, record: dict[str, Any]) -> str: ...


@runtime_checkable
class EventJournal(Protocol):
    """Append-only journal of RecorderEvents (crash-safe JSONL in the integration phase)."""

    def append(self, event: RecorderEvent) -> None: ...


# ============================================================================================
# Adapters from Core updates (duck typed: no bbc_core import)
# ============================================================================================

def _norm(name: str) -> str:
    return " ".join(str(name).strip().upper().split())


# Sample attributes are in the units their names say. A RECORDER_FIELDS entry subscribed in
# another unit (the catalog's) is converted here; the factor multiplies the delivered value.
_UNIT_FACTORS: dict[tuple[str, str], float] = {
    ("touchdown_velocity_fpm", "feet per second"): 60.0,
    ("vs_fpm", "feet per second"): 60.0,
}


def _field_lookup() -> dict[str, str]:
    table: dict[str, str] = {}
    for name, _unit, _kind, index, attr in RECORDER_FIELDS:
        base = _norm(name)
        if index is None:
            table[base] = attr
        else:
            table[f"{base}:{index}"] = attr
            table[f"{base} {index}"] = attr
            table[f"{base}[{index}]"] = attr
    table.setdefault("TITLE", "aircraft")
    return table


def _factor_lookup() -> dict[str, float]:
    factors: dict[str, float] = {}
    for _name, unit, _kind, _index, attr in RECORDER_FIELDS:
        factor = _UNIT_FACTORS.get((attr, str(unit or "").lower()))
        if factor is not None:
            factors[attr] = factor
    return factors


_LOOKUP = _field_lookup()
_FACTORS = _factor_lookup()


def _scaled(attr: str, value: Any, factors: dict[str, float]) -> float | None:
    number = _num(value)
    factor = factors.get(attr)
    return number if number is None or factor is None else number * factor


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, (str, bytes)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    number = _num(value)
    return None if number is None else number != 0


def sample_from_snapshot(snapshot: Any, *, high_rate: bool | None = None,
                         mapping: dict[str, str] | None = None) -> Sample:
    """Build a ``Sample`` from a Core ``Snapshot`` (or anything with the same attributes).

    Field names are matched case-insensitively; indexed fields may be written ``NAME:1``,
    ``NAME 1`` or ``NAME[1]``. ``mapping`` overrides or extends the name -> attribute table.
    """
    table = dict(_LOOKUP)
    factors = dict(_FACTORS)
    if mapping:
        table.update({_norm(k): v for k, v in mapping.items()})
        # An explicit mapping names the attribute directly; its values are already in that unit.
        for attr in mapping.values():
            factors.pop(attr, None)
    raw: dict[str, Any] = {}
    for name, value in zip(snapshot.names, snapshot.values):
        attr = table.get(_norm(name))
        if attr is not None:
            raw[attr] = value
    engines: list[bool | None] = []
    for n in range(1, ENGINE_COUNT + 1):
        key = f"engine_{n}"
        if key in raw:
            engines.append(_bool(raw.pop(key)))
    absolute = _num(getattr(snapshot, "absolute_time_s", None))
    sim_utc = getattr(snapshot, "sample_time", None)
    if sim_utc is None and absolute is not None:
        try:
            sim_utc = _SIM_EPOCH + timedelta(seconds=absolute)
        except OverflowError:
            sim_utc = None
    source = getattr(snapshot, "source", None)
    aircraft = raw.get("aircraft")
    return Sample(
        host_time=snapshot.received_at,
        sim_time_s=_num(getattr(snapshot, "simulation_time_s", None)),
        sim_utc=sim_utc,
        sim_rate=_num(getattr(snapshot, "simulation_rate", None)),
        on_ground=_bool(raw.get("on_ground")),
        ground_speed_kt=_num(raw.get("ground_speed_kt")),
        ias_kt=_num(raw.get("ias_kt")),
        vs_fpm=_scaled("vs_fpm", raw.get("vs_fpm"), factors),
        alt_agl_ft=_num(raw.get("alt_agl_ft")),
        alt_msl_ft=_num(raw.get("alt_msl_ft")),
        lat=_num(raw.get("lat")),
        lon=_num(raw.get("lon")),
        heading_deg=_num(raw.get("heading_deg")),
        parking_brake=_bool(raw.get("parking_brake")),
        engines_running=tuple(engines) if engines else None,
        touchdown_velocity_fpm=_scaled("touchdown_velocity_fpm", raw.get("touchdown_velocity_fpm"), factors),
        slew_active=_bool(raw.get("slew_active")),
        pause_flags=None,
        aircraft=aircraft if isinstance(aircraft, str) and aircraft.strip() else None,
        high_rate=high_rate,
        simulated=getattr(source, "simulated", None),
        session_id=getattr(snapshot, "session_id", None),
    )


_EVENT_MAP = {
    "sim.pause_state": LifecycleKind.PAUSE_FLAGS,
    "sim.crashed": LifecycleKind.CRASHED,
    "sim.aircraft_crashed": LifecycleKind.CRASHED,
    "sim.crash_reset": LifecycleKind.CRASH_RESET,
    "sim.aircraft_loaded": LifecycleKind.AIRCRAFT_LOADED,
    "sim.flight_loaded": LifecycleKind.FLIGHT_LOADED,
    "sim.position_changed": LifecycleKind.POSITION_CHANGED,
    "sim.started": LifecycleKind.SIM_STARTED,
    "sim.stopped": LifecycleKind.SIM_STOPPED,
}


def _now() -> datetime:
    return datetime.now(UTC)


def lifecycle_from_update(update: Any, *, now: Callable[[], datetime] = _now) -> Lifecycle | None:
    """Translate a Core ``Event``, ``Connection`` or ``Gap`` into a ``Lifecycle``; else None."""
    capability = getattr(update, "capability_id", None)
    if capability is not None and hasattr(update, "received_at") and hasattr(update, "value"):
        kind = _EVENT_MAP.get(capability)
        if kind is None:
            return None
        value = update.value
        if kind is LifecycleKind.PAUSE_FLAGS:
            value = int(value) if value is not None else 0
        return Lifecycle(kind, update.received_at, value)
    state = getattr(update, "state", None)
    if state is not None and hasattr(update, "occurred_at"):
        text = str(getattr(state, "value", state)).lower()
        detail = str(getattr(update, "detail", "") or "")
        if text in {"disconnected", "error"}:
            return Lifecycle(LifecycleKind.DISCONNECTED, update.occurred_at, text, detail or f"simulator {text}")
        if text == "connected":
            return Lifecycle(LifecycleKind.CONNECTED, update.occurred_at, text, detail)
        return None
    if type(update).__name__ == "SignalError":
        return None  # one provider signal failed: its value is unknown, but no data was lost (contract C7)
    if hasattr(update, "dropped_updates") and hasattr(update, "reason"):
        return Lifecycle(LifecycleKind.GAP, now(), int(update.dropped_updates or 0), str(update.reason))
    return None


def _looks_like_snapshot(update: Any) -> bool:
    return all(hasattr(update, a) for a in ("names", "values", "simulation_time_s", "received_at"))


# ============================================================================================
# Helpers
# ============================================================================================

def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(min(1.0, math.sqrt(a)))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _speed(s: Sample) -> float | None:
    return s.ground_speed_kt if s.ground_speed_kt is not None else s.ias_kt


def _engines(s: Sample) -> str:
    """'off' when every known engine is stopped, 'running', or 'unknown'."""
    known = [e for e in (s.engines_running or ()) if e is not None]
    if not known:
        return "unknown"
    return "running" if any(known) else "off"


def _clock_source(total: float, fallback: float) -> str:
    if total <= 1e-9:
        return "none"
    if fallback <= 1e-9:
        return "simulator"
    if fallback >= total - 1e-9:
        return "host_fallback"
    return "mixed"


@dataclass(frozen=True, slots=True)
class _Mark:
    credit: float    # Recorder timeline (credited seconds, simulator time where available)
    host: float      # unpaused host seconds
    fallback: float  # portion of ``credit`` measured with the host-time fallback
    uncredited: float  # seconds not credited across gaps


@dataclass(slots=True)
class _Interval:
    host_dt: float = 0.0
    unpaused_s: float = 0.0
    credit: float = 0.0
    host_credit: float = 0.0
    fallback: float = 0.0
    uncredited: float = 0.0
    sim_dt: float | None = None
    discontinuity: str | None = None
    gap: bool = False
    jump: str | None = None
    distance_nm: float | None = None
    rate: float | None = None


@dataclass(slots=True)
class _Flight:
    flight_id: str
    trigger: str
    start_mark: _Mark
    start_host: datetime
    start_sim_utc: datetime | None
    aircraft: str | None
    simulated: bool | None
    started_in_air: bool = False
    block_off: dict[str, Any] | None = None
    block_off_mark: _Mark | None = None
    block_on: dict[str, Any] | None = None
    block_on_mark: _Mark | None = None
    takeoffs: list[dict[str, Any]] = field(default_factory=list)
    touchdowns: list[dict[str, Any]] = field(default_factory=list)
    segments: list[tuple[_Mark, _Mark | None]] = field(default_factory=list)
    rejected_takeoffs: list[dict[str, Any]] = field(default_factory=list)
    go_arounds: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    jumps: list[dict[str, Any]] = field(default_factory=list)
    pauses: list[dict[str, Any]] = field(default_factory=list)
    rate_changes: int = 0
    time_at_rate: dict[str, float] = field(default_factory=dict)
    max_sim_rate: float | None = None
    distance_nm: float = 0.0
    bridged_nm: float = 0.0
    paused_host_s: float = 0.0
    active_paused_host_s: float = 0.0
    max_alt_msl_ft: float | None = None
    warnings: list[str] = field(default_factory=list)
    outcome: str = "open"
    end_reason: str | None = None
    end_host: datetime | None = None
    end_mark: _Mark | None = None
    last_sample: Sample | None = None
    engines_ran_since_block_off: bool = False
    samples: int = 0


@dataclass(frozen=True, slots=True)
class FlightSummary:
    """Everything the Recorder derived for one flight, with provenance. JSON-ready via to_json."""

    flight_id: str
    outcome: str                    # open | complete | abandoned | discarded
    phase: Phase
    trigger: str                    # automatic | manual
    end_reason: str | None
    started_in_air: bool
    aircraft: str | None
    simulated: bool | None
    started_host: str
    ended_host: str | None
    block_off: dict[str, Any] | None
    block_on: dict[str, Any] | None
    block_s: float | None
    block_host_s: float | None
    block_source: str
    block_note: str | None
    airborne_s: float | None
    airborne_host_s: float | None
    airborne_source: str
    airborne_note: str | None
    paused_host_s: float
    active_paused_host_s: float
    uncredited_gap_s: float
    max_sim_rate: float | None
    time_at_rate_s: dict[str, float]
    distance_nm: float
    distance_bridged_nm: float
    distance_excluded_nm: float
    takeoffs: tuple[dict[str, Any], ...]
    touchdowns: tuple[dict[str, Any], ...]
    landings: int
    bounces: int
    touch_and_goes: int
    go_arounds: tuple[dict[str, Any], ...]
    rejected_takeoffs: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, Any], ...]
    position_jumps: tuple[dict[str, Any], ...]
    pauses: tuple[dict[str, Any], ...]
    max_alt_msl_ft: float | None
    warnings: tuple[str, ...]
    rules_version: str
    loggable: bool

    @property
    def landing_rates(self) -> tuple[dict[str, Any], ...]:
        return tuple({"rate_fpm": t["rate_fpm"], "rate_source": t["rate_source"], "kind": t["kind"]}
                     for t in self.touchdowns)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in self.__slots__:  # type: ignore[attr-defined]
            value = getattr(self, name)
            if isinstance(value, Enum):
                value = value.value
            elif isinstance(value, tuple):
                value = [dict(v) if isinstance(v, dict) else v for v in value]
            elif isinstance(value, dict):
                value = dict(value)
            out[name] = value
        return out


# ============================================================================================
# The state machine
# ============================================================================================

class FlightRecorder:
    """Deterministic Flight Recorder. Not thread-safe: drive it from one thread.

    ``feed(sample)``, ``on_lifecycle(notice)``, ``process(core_update)``, ``manual_start`` and
    ``manual_stop`` all return the list of ``RecorderEvent`` objects they produced, in order.
    """

    def __init__(self, rules: RecorderRules | None = None, *,
                 id_factory: Callable[[], str] | None = None,
                 now: Callable[[], datetime] = _now) -> None:
        self.rules = rules or RecorderRules()
        self._ids = id_factory or (lambda: uuid4().hex)
        self._now = now
        # global timeline (independent of flights)
        self._credit = 0.0
        self._host_credit = 0.0
        self._fallback = 0.0
        self._uncredited = 0.0
        self._prev: Sample | None = None
        self._pause_flags = 0
        self._pause_since: datetime | None = None
        self._pause_log: list[tuple[datetime, datetime, bool]] = []  # recent closed pauses
        self._sim_rate: float | None = None
        self._aircraft: str | None = None
        self._open_gap: dict[str, Any] | None = None
        self._core_gap_pending = False
        self._position_reported = False
        self._slewing = False
        self._sim_stopped = False   # SimStop seen and no SimStart since: the user left the flight
        # flight state
        self._phase = Phase.IDLE
        self._flight: _Flight | None = None
        self._last_flight: _Flight | None = None
        self._completed: list[FlightSummary] = []
        self._suspended: str | None = None
        self._runs: dict[str, tuple[Sample, _Mark]] = {}
        self._pending_td: dict[str, Any] | None = None
        self._bounce: dict[str, Any] | None = None
        self._tdv_baseline: float | None = None
        self._ga_armed = False
        self._ga_low: tuple[float, Sample] | None = None
        self._last_airborne: tuple[Sample, _Mark] | None = None
        self._await_first_sample = False
        self._high = False
        self._hint = SamplingHint("second", 1, False, "no flight in progress")

    # ---------------------------------------------------------------- public state
    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def flight_id(self) -> str | None:
        return self._flight.flight_id if self._flight else None

    @property
    def recording(self) -> bool:
        return self._flight is not None

    @property
    def paused(self) -> bool:
        return self._pause_flags != 0

    @property
    def suspended_reason(self) -> str | None:
        """Why automatic logging is held (manual stop, crash), or None."""
        return self._suspended

    @property
    def sampling_hint(self) -> SamplingHint:
        return self._hint

    @property
    def completed_flights(self) -> tuple[FlightSummary, ...]:
        return tuple(self._completed)

    def summary(self) -> FlightSummary | None:
        """The current flight, or the most recently ended one, or None."""
        flight = self._flight or self._last_flight
        return self._summarise(flight) if flight else None

    # ---------------------------------------------------------------- dispatch
    def process(self, update: Any) -> list[RecorderEvent]:
        """Accept a Sample, Lifecycle, Core Snapshot, Event, Connection or Gap."""
        if isinstance(update, Sample):
            return self.feed(update)
        if isinstance(update, Lifecycle):
            return self.on_lifecycle(update)
        if _looks_like_snapshot(update):
            return self.feed(sample_from_snapshot(update, high_rate=self._high))
        notice = lifecycle_from_update(update, now=self._now)
        return self.on_lifecycle(notice) if notice else []

    def process_all(self, updates: Iterable[Any]) -> list[RecorderEvent]:
        out: list[RecorderEvent] = []
        for update in updates:
            out.extend(self.process(update))
        return out

    # ---------------------------------------------------------------- manual overrides
    def manual_start(self, host_time: datetime, note: str = "") -> list[RecorderEvent]:
        """Pilot pressed Start Logging. No-op (empty list) when a flight is already open."""
        out: list[RecorderEvent] = []
        self._suspended = None
        if self._flight is not None:
            return out
        self._emit(EventKind.MANUAL_START, out, host_time=host_time, note=note)
        prev = self._prev
        recent = prev is not None and (host_time - prev.host_time).total_seconds() <= self.rules.gap_after_s
        self._start_flight("manual", out, prev if recent else None, host_time=host_time)
        if not recent:
            self._await_first_sample = True
        return out

    def manual_stop(self, host_time: datetime, note: str = "") -> list[RecorderEvent]:
        """Pilot confirmed Stop Logging. Automatic start is then held until a reset or Start."""
        out: list[RecorderEvent] = []
        if self._flight is None:
            return out
        self._emit(EventKind.MANUAL_STOP, out, host_time=host_time, note=note)
        self._end_flight(self._interrupt_outcome(), "stopped by pilot", out, host_time=host_time)
        self._suspended = "manual stop"
        return out

    def interrupt_flight(self, reason: str = "") -> list[RecorderEvent]:
        """Drop the open flight without ending it, for Save & Exit or a very long data gap.

        Nothing is invented: no FLIGHT_ENDED event, no end time, no closed clocks. Pending
        touchdown evidence is flushed first (those events are returned so they reach the
        journal); the caller then closes the journal as *interrupted*. The summary stays
        available through ``summary()`` with ``outcome == "interrupted"`` and running clocks.
        The next usable sample arms a new flight as usual.
        """
        out: list[RecorderEvent] = []
        f = self._flight
        if f is None:
            return out
        self._flush_touchdown(out)
        f.outcome = "interrupted"
        f.end_reason = reason or "interrupted"
        self._last_flight = f
        self._flight = None
        self._phase = Phase.IDLE
        self._runs.clear()
        self._bounce = None
        self._pending_td = None
        self._open_gap = None
        self._await_first_sample = False
        self._ga_armed, self._ga_low = False, None
        self._prev = None
        self._high = False
        self._hint = SamplingHint("second", 1, False, "no flight in progress")
        return out

    def resume_automatic(self) -> None:
        """Clear a manual-stop or crash hold so automatic logging can arm again."""
        self._suspended = None

    # ---------------------------------------------------------------- lifecycle
    def on_lifecycle(self, notice: Lifecycle) -> list[RecorderEvent]:
        out: list[RecorderEvent] = []
        kind, at = notice.kind, notice.host_time
        if kind is LifecycleKind.PAUSE_FLAGS:
            self._set_pause(int(notice.value or 0), at, out)
        elif kind is LifecycleKind.CRASHED:
            if self._flight is not None:
                self._flush_touchdown(out)
                self._emit(EventKind.CRASHED, out, host_time=at, detail=notice.detail)
                self._end_flight("abandoned", "aircraft crashed", out, host_time=at)
            self._suspended = "crash"
            self._prev = None
        elif kind is LifecycleKind.CRASH_RESET:
            if self._suspended == "crash":
                self._suspended = None
            self._prev = None
        elif kind is LifecycleKind.AIRCRAFT_LOADED:
            name = str(notice.value).strip() if notice.value else None
            if name:
                self._aircraft_changed(name, at, out)
        elif kind is LifecycleKind.FLIGHT_LOADED:
            if self._flight is not None and self.rules.close_on_flight_loaded:
                self._end_flight(self._interrupt_outcome(), "a new flight was loaded", out, host_time=at)
            self._suspended = None
            self._position_reported = True
            self._prev = None
        elif kind is LifecycleKind.POSITION_CHANGED:
            self._position_reported = True
        elif kind is LifecycleKind.DISCONNECTED:
            if self._flight is not None and self._open_gap is None:
                self._flush_touchdown(out)
                reason = notice.detail or "simulator disconnected"
                last_utc = _iso(self._prev.sim_utc) if self._prev is not None else None
                self._open_gap = {"reason": reason, "started_host": at, "phase": self._phase.value,
                                  "started_sim_utc": last_utc}
                self._emit(EventKind.GAP, out, host_time=at, status="started", reason=reason,
                           started_host=_iso(at), started_sim_utc=last_utc, gap_phase=self._phase.value)
            self._runs.clear()
        elif kind is LifecycleKind.CONNECTED:
            pass  # the gap closes on the first sample after reconnection
        elif kind is LifecycleKind.GAP:
            if self._flight is not None:
                last_utc = _iso(self._prev.sim_utc) if self._prev is not None else None
                record = {"reason": notice.detail or "provider reported a gap", "status": "reported",
                          "dropped_updates": int(notice.value or 0), "started_host": _iso(at),
                          "ended_host": _iso(at), "host_s": None, "sim_s": None,
                          "credited_s": 0.0, "uncredited_s": 0.0, "phase": self._phase.value,
                          "started_sim_utc": last_utc, "ended_sim_utc": None}
                self._flight.gaps.append(record)
                self._emit(EventKind.GAP, out, host_time=at, status="reported", reason=record["reason"],
                           dropped_updates=record["dropped_updates"], started_host=record["started_host"],
                           started_sim_utc=last_utc, gap_phase=self._phase.value)
            self._core_gap_pending = True
        elif kind is LifecycleKind.SIM_STOPPED:
            if self._flight is not None and self.rules.close_on_sim_stop:
                self._end_flight(self._interrupt_outcome(), "simulator left the flight", out, host_time=at)
            self._prev = None
            self._sim_stopped = True
        elif kind is LifecycleKind.SIM_STARTED:
            self._sim_stopped = False
        return out

    # ---------------------------------------------------------------- samples
    def feed(self, s: Sample) -> list[RecorderEvent]:
        out: list[RecorderEvent] = []
        if s.aircraft and s.aircraft != self._aircraft:
            self._aircraft_changed(s.aircraft, s.host_time, out)
        prev = self._prev
        iv = self._interval(prev, s) if prev is not None else None
        if iv is not None:
            self._credit += iv.credit
            self._host_credit += iv.host_credit
            self._fallback += iv.fallback
            self._uncredited += iv.uncredited
        flight = self._flight
        if flight is not None and iv is not None:
            self._account(flight, prev, s, iv, out)
        if s.sim_rate is not None:
            self._sim_rate = s.sim_rate
        if s.pause_flags is not None:
            self._set_pause(int(s.pause_flags), s.host_time, out)

        if self._flight is None:
            self._maybe_arm(s, out)
        elif self._await_first_sample:
            self._await_first_sample = False
            if s.on_ground is False:
                self._set_in_air_start(s, out)
        flight = self._flight
        if flight is not None:
            flight.samples += 1
            flight.last_sample = s
            if flight.aircraft is None and self._aircraft:
                flight.aircraft = self._aircraft
            if s.alt_msl_ft is not None:
                flight.max_alt_msl_ft = max(flight.max_alt_msl_ft or s.alt_msl_ft, s.alt_msl_ft)
            if iv is not None and iv.jump:
                self._relocate(s, out)
            else:
                self._step(s, out, after_gap=bool(iv and iv.gap))
            self._resolve_touchdown(s, out)
            if s.on_ground is False and s.touchdown_velocity_fpm is not None:
                self._tdv_baseline = s.touchdown_velocity_fpm
        if s.on_ground is False:
            self._last_airborne = (s, self._mark())
        self._update_hint(s)
        self._prev = s
        return out

    # ================================================================ internals: time
    def _mark(self) -> _Mark:
        return _Mark(self._credit, self._host_credit, self._fallback, self._uncredited)

    def _stops_clock(self, flags: int) -> bool:
        if not flags:
            return False
        if self.rules.pause_policy is PausePolicy.FULL_AND_SIM_ONLY:
            return bool(flags & (PAUSE_FULL | PAUSE_FULL_WITH_SOUND | PAUSE_SIM))
        return True

    def _paused_overlap(self, start: datetime, end: datetime) -> float:
        total = 0.0
        spans = list(self._pause_log)
        if self._pause_since is not None and self._stops_clock(self._pause_flags):
            spans.append((self._pause_since, end, True))
        for a, b, stopping in spans:
            if not stopping:
                continue
            lo, hi = max(a, start), min(b, end)
            if hi > lo:
                total += (hi - lo).total_seconds()
        return total

    def _interval(self, p: Sample, s: Sample) -> _Interval:
        r = self.rules
        iv = _Interval()
        host_dt = (s.host_time - p.host_time).total_seconds()
        if host_dt < 0:
            host_dt = 0.0
            if self._flight is not None:
                self._warn(self._flight, "Host clock went backwards between two samples; interval ignored.")
        iv.host_dt = host_dt
        paused = min(host_dt, self._paused_overlap(p.host_time, s.host_time)) if host_dt else 0.0
        unpaused_s = host_dt - paused
        iv.unpaused_s = unpaused_s
        frac = unpaused_s / host_dt if host_dt > 0 else (0.0 if self._stops_clock(self._pause_flags) else 1.0)
        rate = s.sim_rate if s.sim_rate is not None else (p.sim_rate if p.sim_rate is not None else self._sim_rate)
        iv.rate = rate
        if p.sim_time_s is not None and s.sim_time_s is not None:
            d = s.sim_time_s - p.sim_time_s
            limit = host_dt * max(rate or _UNKNOWN_RATE_CEILING, 1.0) * r.max_sim_time_lead + r.max_sim_time_slack_s
            if d < -0.5:
                iv.discontinuity = "simulation time went backwards"
            elif d > limit:
                iv.discontinuity = "simulation time jumped forward"
            else:
                iv.sim_dt = max(d, 0.0)
        iv.gap = self._open_gap is not None or unpaused_s > r.gap_after_s or self._core_gap_pending
        long_gap = (self._open_gap is not None or unpaused_s > r.gap_after_s)
        self._core_gap_pending = False
        if iv.sim_dt is not None:
            credit, fallback = iv.sim_dt * frac, 0.0
        elif iv.discontinuity:
            credit, fallback = 0.0, 0.0
        elif long_gap:
            credit, fallback = 0.0, 0.0   # no simulator clock across a gap: do not guess
            iv.uncredited = unpaused_s * (rate or 1.0)
        else:
            credit = unpaused_s * (rate if rate and rate > 0 else 1.0)
            fallback = credit
        if long_gap and credit > 0 and unpaused_s > r.credit_gap_max_s:
            iv.uncredited, credit, fallback = credit, 0.0, 0.0
        iv.credit, iv.fallback = credit, fallback
        iv.host_credit = 0.0 if (long_gap and credit == 0.0) else unpaused_s
        # position continuity
        if None not in (p.lat, p.lon, s.lat, s.lon):
            dist = haversine_nm(p.lat, p.lon, s.lat, s.lon)  # type: ignore[arg-type]
            iv.distance_nm = dist
            ref_dt = iv.sim_dt if iv.sim_dt is not None else host_dt * (rate or 1.0)
            fastest = max(p.ground_speed_kt or 0.0, s.ground_speed_kt or 0.0, 0.0)
            allowed = (fastest * r.jump_speed_factor + r.jump_speed_margin_kt) * ref_dt / 3600 + r.jump_min_nm
            if p.slew_active or s.slew_active:
                iv.jump = "slew mode"
            elif self._position_reported and dist > r.reported_jump_min_nm:
                iv.jump = "simulator reported a position change"
            elif iv.discontinuity and dist > r.reported_jump_min_nm:
                iv.jump = iv.discontinuity
            elif dist > allowed:
                iv.jump = "implausible movement between samples"
            self._position_reported = False
        return iv

    def _account(self, f: _Flight, p: Sample, s: Sample, iv: _Interval, out: list[RecorderEvent]) -> None:
        r = self.rules
        # gaps
        if self._open_gap is not None or iv.unpaused_s > r.gap_after_s:
            started = self._open_gap["started_host"] if self._open_gap else p.host_time
            reason = self._open_gap["reason"] if self._open_gap else f"no telemetry for {iv.unpaused_s:.0f} s"
            status = "ended" if self._open_gap else "detected"
            record = {"reason": reason, "status": status, "dropped_updates": None,
                      "started_host": _iso(started), "ended_host": _iso(s.host_time),
                      "host_s": round((s.host_time - started).total_seconds(), 3),
                      "sim_s": _round(iv.sim_dt, 3), "credited_s": round(iv.credit, 3),
                      "uncredited_s": round(iv.uncredited, 3),
                      "phase": (self._open_gap or {}).get("phase", self._phase.value),
                      "sim_time_continuous": iv.sim_dt is not None,
                      "started_sim_utc": (self._open_gap["started_sim_utc"] if self._open_gap
                                          else _iso(p.sim_utc)),
                      "ended_sim_utc": _iso(s.sim_utc)}
            if iv.discontinuity:
                record["note"] = iv.discontinuity
            f.gaps.append(record)
            self._open_gap = None
            self._runs.clear()
            self._flush_touchdown(out)
            self._emit(EventKind.GAP, out, s, **self._event_fields(record, drop=("dropped_updates",)))
        elif iv.discontinuity and not iv.jump:
            self._warn(f, f"Clock discontinuity ({iv.discontinuity}); that interval was not credited.")
        # sim rate
        if iv.rate is not None:
            key = f"{iv.rate:g}"
            f.time_at_rate[key] = f.time_at_rate.get(key, 0.0) + iv.credit
            f.max_sim_rate = iv.rate if f.max_sim_rate is None else max(f.max_sim_rate, iv.rate)
        if (p.sim_rate is not None and s.sim_rate is not None
                and abs(s.sim_rate - p.sim_rate) > r.sim_rate_change_epsilon):
            f.rate_changes += 1
            self._emit(EventKind.SIM_RATE_CHANGED, out, s, from_rate=p.sim_rate, to_rate=s.sim_rate)
        # position jumps and distance
        if iv.jump:
            dist = iv.distance_nm or 0.0
            if iv.jump == "slew mode":
                if not self._slewing:
                    self._slewing = True
                    self._record_jump(f, p, s, iv, out)
                else:
                    f.jumps[-1]["distance_nm"] = round(f.jumps[-1]["distance_nm"] + dist, 3)
            else:
                self._slewing = False
                self._record_jump(f, p, s, iv, out)
        else:
            self._slewing = False
            moving = self._phase in (Phase.TAXI_OUT, Phase.TAKEOFF_ROLL, Phase.AIRBORNE,
                                     Phase.LANDING_ROLL, Phase.TAXI_IN)
            if iv.distance_nm is not None and moving:
                if iv.unpaused_s > r.gap_after_s:
                    if r.bridge_distance_across_gaps:
                        f.bridged_nm += iv.distance_nm
                else:
                    f.distance_nm += iv.distance_nm

    def _record_jump(self, f: _Flight, p: Sample, s: Sample, iv: _Interval, out: list[RecorderEvent]) -> None:
        record = {"reason": iv.jump, "distance_nm": round(iv.distance_nm or 0.0, 3),
                  "from": [p.lat, p.lon], "to": [s.lat, s.lon], "host_time": _iso(s.host_time),
                  "sim_time_s": s.sim_time_s, "phase": self._phase.value}
        f.jumps.append(record)
        self._runs.clear()
        self._flush_touchdown(out)
        self._emit(EventKind.POSITION_JUMP, out, s, **self._event_fields(record, drop=("host_time", "sim_time_s")))

    # ================================================================ internals: pause
    def _set_pause(self, flags: int, at: datetime, out: list[RecorderEvent]) -> None:
        old = self._pause_flags
        if flags == old:
            return
        if self._pause_since is not None:
            self._pause_log.append((self._pause_since, at, self._stops_clock(old)))
            del self._pause_log[:-32]
            if self._flight is not None:
                span = max(0.0, (at - max(self._pause_since, self._flight.start_host)).total_seconds())
                self._flight.paused_host_s += span
                if old & PAUSE_ACTIVE:
                    self._flight.active_paused_host_s += span
                if self._flight.pauses and self._flight.pauses[-1].get("ended_host") is None:
                    self._flight.pauses[-1]["ended_host"] = _iso(at)
                    self._flight.pauses[-1]["host_s"] = round(span, 3)
        self._pause_flags = flags
        self._pause_since = at if flags else None
        if self._flight is None:
            return
        info = {"flags": flags, "full": bool(flags & (PAUSE_FULL | PAUSE_FULL_WITH_SOUND)),
                "active": bool(flags & PAUSE_ACTIVE), "sim": bool(flags & PAUSE_SIM),
                "clocks_stopped": self._stops_clock(flags)}
        if flags:
            self._flight.pauses.append({**info, "started_host": _iso(at), "ended_host": None, "host_s": None,
                                        "phase": self._phase.value})
            self._emit(EventKind.PAUSED, out, host_time=at, changed_from=old, **info)
        else:
            self._emit(EventKind.RESUMED, out, host_time=at, previous_flags=old)

    # ================================================================ internals: flights
    def _emit(self, kind: EventKind, out: list[RecorderEvent], sample: Sample | None = None, *,
              host_time: datetime | None = None, phase: Phase | None = None, **data: Any) -> RecorderEvent:
        if sample is not None:
            host, sim_t, sim_utc = sample.host_time, sample.sim_time_s, sample.sim_utc
        else:
            host, sim_t, sim_utc = host_time or self._now(), None, None
        payload: dict[str, Any] = {}
        if self._flight is not None:
            payload["flight_id"] = self._flight.flight_id
        elif self._last_flight is not None and kind in (EventKind.FLIGHT_ENDED,):
            payload["flight_id"] = self._last_flight.flight_id
        for key, value in data.items():
            payload[key] = _iso(value) if isinstance(value, datetime) else value
        event = RecorderEvent(kind, phase or self._phase, host, sim_t, sim_utc, payload)
        out.append(event)
        return event

    @staticmethod
    def _event_fields(record: dict[str, Any], *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
        """Event data from a summary record. ``phase`` would collide with ``_emit``'s own phase
        argument (the event's phase), so the record's phase travels as ``gap_phase``/``jump_phase``."""
        out = {k: v for k, v in record.items() if k not in drop and k != "phase"}
        if "phase" in record:
            out["jump_phase" if "distance_nm" in record else "gap_phase"] = record["phase"]
        return out

    def _warn(self, f: _Flight, text: str) -> None:
        if text not in f.warnings:
            f.warnings.append(text)

    def _set_phase(self, phase: Phase, out: list[RecorderEvent], sample: Sample | None = None,
                   *, host_time: datetime | None = None, reason: str = "") -> None:
        if phase is self._phase:
            return
        old = self._phase
        self._phase = phase
        self._emit(EventKind.PHASE_CHANGED, out, sample, host_time=host_time,
                   **{"from": old.value, "to": phase.value, "reason": reason})

    def _maybe_arm(self, s: Sample, out: list[RecorderEvent]) -> None:
        r = self.rules
        if not r.auto_start or self._suspended or s.on_ground is None:
            return
        if self._sim_stopped and r.close_on_sim_stop:
            return  # the simulator left the flight (menu, loading): nothing to arm until SimStart
        if self._last_flight is not None and self._last_flight.outcome == "complete" and not r.auto_rearm_after_end:
            return
        if s.on_ground is False and not r.start_in_air:
            return
        self._start_flight("automatic", out, s)

    def _start_flight(self, trigger: str, out: list[RecorderEvent], s: Sample | None,
                      *, host_time: datetime | None = None) -> None:
        at = host_time or (s.host_time if s is not None else self._now())
        f = _Flight(self._ids(), trigger, self._mark(), at, s.sim_utc if s else None, self._aircraft,
                    s.simulated if s else None)
        self._flight = f
        self._runs.clear()
        self._pending_td = None
        self._bounce = None
        self._ga_armed, self._ga_low = False, None
        self._tdv_baseline = s.touchdown_velocity_fpm if s is not None else None
        self._emit(EventKind.FLIGHT_STARTED, out, None if host_time else s, host_time=at, phase=Phase.PREFLIGHT, trigger=trigger,
                   aircraft=f.aircraft, rules_version=RULES_VERSION, simulated=f.simulated,
                   rules=self.rules.to_json()["values"])
        if self._pause_flags and self._pause_since is not None:
            f.pauses.append({"flags": self._pause_flags, "started_host": _iso(at), "ended_host": None,
                             "host_s": None, "phase": Phase.PREFLIGHT.value,
                             "clocks_stopped": self._stops_clock(self._pause_flags)})
        if s is not None and s.on_ground is False:
            self._phase = Phase.PREFLIGHT
            self._set_in_air_start(s, out)
        else:
            self._set_phase(Phase.PREFLIGHT, out, None if host_time else s, host_time=at, reason=f"{trigger} start")

    def _set_in_air_start(self, s: Sample, out: list[RecorderEvent] | None = None) -> None:
        f = self._flight
        assert f is not None
        f.started_in_air = True
        f.segments.append((self._mark(), None))
        self._warn(f, "Flight started in the air: block-off and takeoff were not observed.")
        if out is not None:
            self._set_phase(Phase.AIRBORNE, out, s, reason="started in the air")
        else:
            self._phase = Phase.AIRBORNE

    def _interrupt_outcome(self) -> str:
        f = self._flight
        if f is None:
            return "discarded"
        if not self._loggable(f):
            return "discarded"
        if f.touchdowns and self._phase in (Phase.LANDING_ROLL, Phase.TAXI_IN):
            return "complete"
        return "abandoned"

    @staticmethod
    def _loggable(f: _Flight) -> bool:
        return bool(f.block_off or f.takeoffs or f.started_in_air or f.touchdowns)

    def _end_flight(self, outcome: str, reason: str, out: list[RecorderEvent], s: Sample | None = None,
                    *, host_time: datetime | None = None) -> None:
        f = self._flight
        if f is None:
            return
        self._flush_touchdown(out)
        at = s.host_time if s is not None else (host_time or self._now())
        mark = self._mark()
        for i, (start, end) in enumerate(f.segments):
            if end is None:
                f.segments[i] = (start, mark)
        if self._pause_since is not None:
            span = max(0.0, (at - max(self._pause_since, f.start_host)).total_seconds())
            f.paused_host_s += span
            if self._pause_flags & PAUSE_ACTIVE:
                f.active_paused_host_s += span
            if f.pauses and f.pauses[-1].get("ended_host") is None:
                f.pauses[-1]["ended_host"] = _iso(at)
                f.pauses[-1]["host_s"] = round(span, 3)
                f.pauses[-1]["note"] = "flight ended during the pause"
        if self._open_gap is not None:
            g = self._open_gap
            f.gaps.append({"reason": g["reason"], "status": "unresolved", "dropped_updates": None,
                           "started_host": _iso(g["started_host"]), "ended_host": _iso(at),
                           "host_s": round((at - g["started_host"]).total_seconds(), 3), "sim_s": None,
                           "credited_s": 0.0, "uncredited_s": None, "phase": g["phase"],
                           "sim_time_continuous": False, "started_sim_utc": g.get("started_sim_utc"),
                           "ended_sim_utc": None})
            self._open_gap = None
        for td in f.touchdowns:
            if td["kind"] == "landing":
                td["kind"] = "full_stop" if outcome == "complete" else "unconfirmed"
        f.outcome = outcome
        f.end_reason = reason
        f.end_host = at
        f.end_mark = mark
        final = {"complete": Phase.COMPLETE, "abandoned": Phase.ABANDONED}.get(outcome, Phase.IDLE)
        self._set_phase(final, out, s, host_time=at, reason=reason)
        summary = self._summarise(f)
        self._emit(EventKind.FLIGHT_ENDED, out, s, host_time=at, outcome=outcome, reason=reason,
                   loggable=summary.loggable, summary=summary.to_json())
        self._completed.append(summary)
        del self._completed[:-16]
        self._last_flight = f
        self._flight = None
        self._runs.clear()
        self._bounce = None
        self._await_first_sample = False
        if outcome == "discarded":
            self._phase = Phase.IDLE

    def _aircraft_changed(self, name: str, at: datetime, out: list[RecorderEvent]) -> None:
        old = self._aircraft
        if name == old:
            return
        self._aircraft = name
        if old is None:
            if self._flight is not None and self._flight.aircraft is None:
                self._flight.aircraft = name
            return
        if self._flight is not None:
            self._emit(EventKind.AIRCRAFT_CHANGED, out, host_time=at, **{"from": old, "to": name})
            self._end_flight(self._interrupt_outcome(), "aircraft changed", out, host_time=at)
        if self._suspended == "manual stop":
            self._suspended = None
        self._prev = None

    # ================================================================ internals: phases
    def _run(self, name: str, condition: bool, s: Sample, confirm_s: float) -> tuple[Sample, _Mark] | None:
        if not condition:
            self._runs.pop(name, None)
            return None
        start = self._runs.get(name)
        if start is None:
            start = self._runs[name] = (s, self._mark())
        if self._credit - start[1].credit >= confirm_s - 1e-9:
            self._runs.pop(name, None)
            return start
        return None

    def _where(self, s: Sample, mark: _Mark | None = None) -> dict[str, Any]:
        return {"host_time": _iso(s.host_time), "sim_time_s": s.sim_time_s, "sim_utc": _iso(s.sim_utc),
                "lat": s.lat, "lon": s.lon, "heading_deg": _round(s.heading_deg),
                "ground_speed_kt": _round(s.ground_speed_kt), "ias_kt": _round(s.ias_kt),
                "alt_msl_ft": _round(s.alt_msl_ft), "alt_agl_ft": _round(s.alt_agl_ft)}

    def _step(self, s: Sample, out: list[RecorderEvent], *, after_gap: bool) -> None:
        if s.on_ground is None:
            return
        r = self.rules
        f = self._flight
        assert f is not None
        ph, ground, speed = self._phase, s.on_ground, _speed(s)
        if f.block_off is not None and _engines(s) == "running":
            f.engines_ran_since_block_off = True
        if ph is Phase.PREFLIGHT:
            if not ground:
                self._try_takeoff(s, out, from_phase=ph)
                return
            self._runs.pop("airborne", None)
            brake_ok = not (r.block_off_requires_brake_release and s.parking_brake is True)
            moving = speed is not None and speed > r.block_off_speed_kt and brake_ok
            start = self._run("block_off", moving, s, r.block_off_confirm_s)
            if start:
                self._block_off(start, out, basis="moving with parking brake released"
                                if s.parking_brake is False else "moving (parking brake state unknown)"
                                if s.parking_brake is None else "moving")
        elif ph is Phase.TAXI_OUT:
            if not ground:
                self._try_takeoff(s, out, from_phase=ph)
                return
            self._runs.pop("airborne", None)
            if (r.complete_after_taxi_out_return and f.engines_ran_since_block_off
                    and self._check_block_on(s, out, engines_only=True)):
                return
            fast = speed is not None and speed > r.takeoff_roll_speed_kt
            start = self._run("roll", fast, s, r.takeoff_roll_confirm_s)
            if start:
                self._set_phase(Phase.TAKEOFF_ROLL, out, start[0], reason="accelerating for takeoff")
        elif ph is Phase.TAKEOFF_ROLL:
            if not ground:
                self._try_takeoff(s, out, from_phase=ph)
                return
            self._runs.pop("airborne", None)
            if speed is not None and speed < r.rejected_takeoff_speed_kt:
                record = {"reason": "decelerated on the runway before takeoff", **self._where(s)}
                f.rejected_takeoffs.append(record)
                self._set_phase(Phase.TAXI_OUT, out, s, reason="rejected takeoff")
        elif ph is Phase.AIRBORNE:
            if ground:
                self._set_phase(Phase.LANDING_ROLL, out, s, reason="touchdown")
                self._touchdown(s, out, kind="landing", after_gap=after_gap)
                self._ga_armed, self._ga_low = False, None
            else:
                self._track_go_around(s)
        elif ph is Phase.LANDING_ROLL:
            if not ground:
                if self._bounce is None:
                    self._bounce = {"sample": s, "mark": self._mark(), "max_agl": s.alt_agl_ft or 0.0}
                b = self._bounce
                if s.alt_agl_ft is not None:
                    b["max_agl"] = max(b["max_agl"], s.alt_agl_ft)
                elapsed = self._credit - b["mark"].credit
                if elapsed >= r.bounce_max_s or b["max_agl"] > r.touch_and_go_agl_ft:
                    self._touch_and_go(out)
                return
            if self._bounce is not None:
                self._bounce = None
                self._touchdown(s, out, kind="bounce", after_gap=after_gap)
            slow = speed is not None and speed < r.landing_roll_end_speed_kt
            if self._run("rollout", slow, s, r.landing_roll_end_confirm_s):
                for td in reversed(f.touchdowns):
                    if td["kind"] == "landing":
                        td["kind"] = "full_stop"
                        break
                self._set_phase(Phase.TAXI_IN, out, s, reason="landing roll complete")
        elif ph is Phase.TAXI_IN:
            if not ground:
                self._try_takeoff(s, out, from_phase=ph)
                return
            self._runs.pop("airborne", None)
            if self._check_block_on(s, out):
                return
            fast = speed is not None and speed > r.takeoff_roll_speed_kt
            start = self._run("roll", fast, s, r.takeoff_roll_confirm_s)
            if start:
                self._set_phase(Phase.TAKEOFF_ROLL, out, start[0], reason="accelerating for another takeoff")

    def _block_off(self, start: tuple[Sample, _Mark], out: list[RecorderEvent], *, basis: str) -> None:
        f = self._flight
        assert f is not None
        s, mark = start
        f.block_off = {**self._where(s), "basis": basis}
        f.block_off_mark = mark
        self._emit(EventKind.BLOCK_OFF, out, s, basis=basis, ground_speed_kt=_round(s.ground_speed_kt),
                   lat=s.lat, lon=s.lon)
        self._set_phase(Phase.TAXI_OUT, out, s, reason="block-off")

    def _try_takeoff(self, s: Sample, out: list[RecorderEvent], *, from_phase: Phase) -> None:
        r = self.rules
        start = self._runs.get("airborne")
        if start is None:
            start = self._runs["airborne"] = (s, self._mark())
        elapsed = self._credit - start[1].credit
        if not (elapsed >= r.takeoff_confirm_s - 1e-9
                or (s.alt_agl_ft is not None and s.alt_agl_ft >= r.takeoff_confirm_agl_ft)):
            return
        self._runs.pop("airborne", None)
        f = self._flight
        assert f is not None
        liftoff, mark = start
        if from_phase is Phase.PREFLIGHT and f.block_off is None:
            moving = self._runs.get("block_off")
            off = moving if moving is not None else start
            self._block_off(off, out, basis="airborne before taxi movement was confirmed")
        if self._phase is not Phase.TAKEOFF_ROLL:
            self._set_phase(Phase.TAKEOFF_ROLL, out, liftoff, reason="takeoff without a confirmed roll")
        self._takeoff(liftoff, mark, out, touch_and_go=False)

    def _takeoff(self, s: Sample, mark: _Mark, out: list[RecorderEvent], *, touch_and_go: bool) -> None:
        f = self._flight
        assert f is not None
        record = {**self._where(s), "touch_and_go": touch_and_go, "index": len(f.takeoffs) + 1}
        f.takeoffs.append(record)
        f.segments.append((mark, None))
        self._runs.clear()
        self._ga_armed, self._ga_low = False, None
        self._emit(EventKind.TAKEOFF, out, s, touch_and_go=touch_and_go, index=record["index"],
                   ias_kt=_round(s.ias_kt), heading_deg=_round(s.heading_deg), lat=s.lat, lon=s.lon)
        self._set_phase(Phase.AIRBORNE, out, s, reason="touch-and-go" if touch_and_go else "takeoff")

    def _touch_and_go(self, out: list[RecorderEvent]) -> None:
        f = self._flight
        assert f is not None and self._bounce is not None
        liftoff, mark = self._bounce["sample"], self._bounce["mark"]
        self._bounce = None
        self._flush_touchdown(out)
        for td in reversed(f.touchdowns):
            if td["kind"] == "landing":
                td["kind"] = "touch_and_go"
                break
        self._takeoff(liftoff, mark, out, touch_and_go=True)

    def _track_go_around(self, s: Sample) -> None:
        r = self.rules
        agl = s.alt_agl_ft
        if agl is None:
            return
        if agl > r.go_around_max_agl_ft:
            if self._ga_low is None:
                self._ga_armed = True
            elif agl - self._ga_low[0] >= r.go_around_climb_ft:
                self._record_go_around()
            return
        if not self._ga_armed:
            return
        if self._ga_low is None or agl < self._ga_low[0]:
            self._ga_low = (agl, s)
        elif agl - self._ga_low[0] >= r.go_around_climb_ft:
            self._record_go_around()

    def _record_go_around(self) -> None:
        f = self._flight
        assert f is not None and self._ga_low is not None
        low, sample = self._ga_low
        f.go_arounds.append({**self._where(sample), "min_agl_ft": round(low, 1),
                             "basis": "descended below the go-around height and climbed without ground contact"})
        self._ga_low = None
        self._ga_armed = True

    def _check_block_on(self, s: Sample, out: list[RecorderEvent], *, engines_only: bool = False) -> bool:
        r = self.rules
        f = self._flight
        assert f is not None
        speed = _speed(s)
        stationary = speed is not None and speed < r.block_on_speed_kt
        engines = _engines(s)
        brake = s.parking_brake is True
        policy = r.block_on_policy
        evidence: str | None = None
        confirm = r.block_on_confirm_s
        if policy is BlockOnPolicy.ENGINES_OFF_AND_BRAKE:
            if engines == "off" and brake:
                evidence = "engines off and parking brake set"
            elif engines == "unknown" and brake:
                evidence, confirm = "parking brake set (engine state unknown)", r.block_on_brake_only_confirm_s
        elif policy is BlockOnPolicy.PARKING_BRAKE:
            evidence = "parking brake set" if brake else None
        else:  # ENGINES_OFF or ENGINES_OFF_OR_BRAKE
            if engines == "off":
                evidence = "engines off"
            elif brake and (policy is BlockOnPolicy.ENGINES_OFF_OR_BRAKE or engines == "unknown"):
                evidence, confirm = "parking brake set", r.block_on_brake_only_confirm_s
        if engines_only and evidence != "engines off":
            evidence = None  # returning to stand before takeoff needs an observed engine shutdown
        start = self._run(f"block_on:{evidence}", stationary and evidence is not None, s, confirm)
        for key in [k for k in self._runs if k.startswith("block_on:") and k != f"block_on:{evidence}"]:
            self._runs.pop(key, None)
        if not start:
            return False
        at, mark = start
        f.block_on = {**self._where(at), "basis": evidence}
        f.block_on_mark = mark
        self._emit(EventKind.BLOCK_ON, out, at, basis=evidence, lat=at.lat, lon=at.lon)
        if not f.takeoffs and not f.started_in_air:
            self._warn(f, "Returned to block-on without taking off.")
        self._end_flight("complete", "block-on", out, s)
        return True

    def _relocate(self, s: Sample, out: list[RecorderEvent]) -> None:
        """A position jump: follow the new ground state without inventing takeoffs or landings."""
        f = self._flight
        assert f is not None
        self._bounce = None
        if s.on_ground is None:
            return
        ph = self._phase
        if s.on_ground and ph is Phase.AIRBORNE:
            for i, (start, end) in enumerate(f.segments):
                if end is None:
                    f.segments[i] = (start, self._mark())
            self._warn(f, "Moved from the air to the ground by a position jump: no landing was observed.")
            self._set_phase(Phase.TAXI_IN, out, s, reason="position jump to the ground")
        elif not s.on_ground and ph in (Phase.PREFLIGHT, Phase.TAXI_OUT, Phase.TAKEOFF_ROLL,
                                        Phase.LANDING_ROLL, Phase.TAXI_IN):
            f.segments.append((self._mark(), None))
            self._warn(f, "Moved into the air by a position jump: no takeoff was observed.")
            self._set_phase(Phase.AIRBORNE, out, s, reason="position jump into the air")
            self._ga_armed, self._ga_low = False, None

    # ================================================================ internals: touchdown
    def _touchdown(self, s: Sample, out: list[RecorderEvent], *, kind: str, after_gap: bool) -> None:
        f = self._flight
        assert f is not None
        self._flush_touchdown(out)
        if kind == "landing":
            for i, (start, end) in enumerate(f.segments):
                if end is None:
                    f.segments[i] = (start, self._mark())
        vs_candidate: float | None = None
        vs_age: float | None = None
        if self._last_airborne is not None and not after_gap:
            last, last_mark = self._last_airborne
            age = self._credit - last_mark.credit
            if last.vs_fpm is not None and age <= self.rules.touchdown_vs_max_age_s + 1e-9:
                vs_candidate, vs_age = last.vs_fpm, age
        record: dict[str, Any] = {**self._where(s), "kind": kind, "rate_fpm": None, "rate_source": "pending",
                                  "rate_note": None, "during_gap": after_gap, "bounces": []}
        if kind == "landing":
            f.touchdowns.append(record)
        elif f.touchdowns:
            f.touchdowns[-1]["bounces"].append(record)
        else:
            f.touchdowns.append(record)
        self._pending_td = {"sample": s, "mark": self._mark(), "record": record, "kind": kind,
                            "baseline": self._tdv_baseline, "vs": vs_candidate, "vs_age": vs_age}
        self._resolve_touchdown(s, out)

    def _resolve_touchdown(self, s: Sample, out: list[RecorderEvent], *, force: bool = False) -> None:
        pend = self._pending_td
        if pend is None:
            return
        tdv, base = s.touchdown_velocity_fpm, pend["baseline"]
        fresh = tdv is not None and base is not None and abs(tdv - base) > 0.01
        waited = self._credit - pend["mark"].credit
        if not fresh and not force and waited < self.rules.touchdown_simvar_wait_s - 1e-9:
            return
        rec = pend["record"]
        if fresh:
            rec["rate_fpm"] = round(-abs(tdv), 1)  # type: ignore[arg-type]
            rec["rate_source"] = "touchdown_simvar"
            rec["rate_note"] = "PLANE TOUCHDOWN NORMAL VELOCITY changed at ground contact"
        elif pend["vs"] is not None:
            rec["rate_fpm"] = round(pend["vs"], 1)
            rec["rate_source"] = "vertical_speed_high_rate"
            rec["rate_note"] = f"VERTICAL SPEED {pend['vs_age']:.2f} s before ground contact"
        else:
            rec["rate_source"] = "unavailable"
            rec["rate_note"] = ("touchdown happened during a data gap" if rec["during_gap"] else
                                "no touchdown SimVar change and no high-rate vertical speed near the ground")
        contact: Sample = pend["sample"]
        self._pending_td = None
        kind = EventKind.BOUNCE if pend["kind"] == "bounce" else EventKind.TOUCHDOWN
        phase = Phase.LANDING_ROLL if self._phase in ACTIVE_PHASES else self._phase
        self._emit(kind, out, contact, phase=phase, rate_fpm=rec["rate_fpm"], rate_source=rec["rate_source"],
                   rate_note=rec["rate_note"], during_gap=rec["during_gap"],
                   ground_speed_kt=rec["ground_speed_kt"], ias_kt=rec["ias_kt"],
                   heading_deg=rec["heading_deg"], lat=contact.lat, lon=contact.lon)

    def _flush_touchdown(self, out: list[RecorderEvent]) -> None:
        if self._pending_td is not None:
            last = self._flight.last_sample if self._flight else None
            self._resolve_touchdown(last or self._pending_td["sample"], out, force=True)

    # ================================================================ internals: hint
    def _update_hint(self, s: Sample) -> None:
        r = self.rules
        ph = self._phase
        high = self._high
        if self._flight is None:
            high, reason = False, "no flight in progress"
        elif ph in (Phase.TAKEOFF_ROLL, Phase.LANDING_ROLL) or self._pending_td or self._bounce:
            high, reason = True, "takeoff or landing in progress"
        elif ph is Phase.AIRBORNE:
            agl = s.alt_agl_ft
            if agl is None:
                reason = "height above ground unavailable; keeping the current rate"
            elif agl < r.high_rate_below_agl_ft:
                high, reason = True, "near the ground"
            elif agl > r.low_rate_above_agl_ft:
                high, reason = False, "well clear of the ground"
            else:
                reason = "within the hysteresis band; keeping the current rate"
        elif ph in (Phase.TAXI_OUT, Phase.TAXI_IN):
            speed = _speed(s)
            high = speed is not None and speed >= r.high_rate_ground_speed_kt
            reason = "fast on the ground" if high else "taxiing"
        else:
            high, reason = False, "parked"
        self._high = high
        hint = (SamplingHint("sim_frame", r.high_rate_every, True, reason) if high
                else SamplingHint("second", 1, False, reason))
        if (hint.period, hint.every, hint.high_rate) != (self._hint.period, self._hint.every, self._hint.high_rate):
            self._hint = hint
        elif hint.reason != self._hint.reason:
            self._hint = replace(self._hint, reason=hint.reason)

    # ================================================================ internals: summary
    def _summarise(self, f: _Flight) -> FlightSummary:
        now = f.end_mark or self._mark()
        # block clock
        if f.block_off_mark is None:
            block_s = block_host = None
            block_src = "unavailable"
            block_note = "Block-off was not observed." if (f.started_in_air or f.takeoffs) else "Not off-block yet."
        else:
            end = f.block_on_mark or now
            block_s = end.credit - f.block_off_mark.credit
            block_host = end.host - f.block_off_mark.host
            block_src = _clock_source(block_s, end.fallback - f.block_off_mark.fallback)
            block_note = None if f.block_on_mark else ("Block-on was not observed." if f.outcome != "open"
                                                       else "Block clock running.")
        # airborne clock
        if not f.segments:
            air_s = air_host = None
            air_src = "unavailable"
            air_note = "No takeoff observed."
        else:
            air_s = air_host = air_fb = 0.0
            for start, end in f.segments:
                stop = end or now
                air_s += stop.credit - start.credit
                air_host += stop.host - start.host
                air_fb += stop.fallback - start.fallback
            air_src = _clock_source(air_s, air_fb)
            notes = []
            if f.started_in_air:
                notes.append("Started in the air: time before the first sample is not included.")
            if any(end is None for _, end in f.segments):
                notes.append("Airborne clock running.")
            air_note = " ".join(notes) or None
        uncredited = sum(g.get("uncredited_s") or 0.0 for g in f.gaps)
        landings = sum(1 for t in f.touchdowns if t["kind"] != "bounce")
        bounces = sum(len(t["bounces"]) for t in f.touchdowns)
        tng = sum(1 for t in f.touchdowns if t["kind"] == "touch_and_go")
        excluded = sum(j["distance_nm"] for j in f.jumps)
        return FlightSummary(
            flight_id=f.flight_id, outcome=f.outcome,
            phase=self._phase if f is self._flight else
            ({"complete": Phase.COMPLETE, "abandoned": Phase.ABANDONED}.get(f.outcome, Phase.IDLE)),
            trigger=f.trigger, end_reason=f.end_reason, started_in_air=f.started_in_air,
            aircraft=f.aircraft, simulated=f.simulated, started_host=f.start_host.isoformat(),
            ended_host=_iso(f.end_host),
            block_off=dict(f.block_off) if f.block_off else None,
            block_on=dict(f.block_on) if f.block_on else None,
            block_s=_round(block_s, 3), block_host_s=_round(block_host, 3), block_source=block_src,
            block_note=block_note, airborne_s=_round(air_s, 3), airborne_host_s=_round(air_host, 3),
            airborne_source=air_src, airborne_note=air_note,
            paused_host_s=round(f.paused_host_s, 3), active_paused_host_s=round(f.active_paused_host_s, 3),
            uncredited_gap_s=round(uncredited, 3), max_sim_rate=f.max_sim_rate,
            time_at_rate_s={k: round(v, 3) for k, v in f.time_at_rate.items()},
            distance_nm=round(f.distance_nm, 3), distance_bridged_nm=round(f.bridged_nm, 3),
            distance_excluded_nm=round(excluded, 3),
            takeoffs=tuple(dict(t) for t in f.takeoffs),
            touchdowns=tuple({**t, "bounces": [dict(b) for b in t["bounces"]]} for t in f.touchdowns),
            landings=landings, bounces=bounces, touch_and_goes=tng,
            go_arounds=tuple(dict(g) for g in f.go_arounds),
            rejected_takeoffs=tuple(dict(r) for r in f.rejected_takeoffs),
            gaps=tuple(dict(g) for g in f.gaps), position_jumps=tuple(dict(j) for j in f.jumps),
            pauses=tuple(dict(p) for p in f.pauses),
            max_alt_msl_ft=_round(f.max_alt_msl_ft), warnings=tuple(f.warnings),
            rules_version=RULES_VERSION, loggable=self._loggable(f),
        )


def build_flight_record(summary: FlightSummary, rules: RecorderRules, *, journal: str | None = None) -> dict[str, Any]:
    """A storage-neutral record for ``FlightSink.save_recorded_flight``. Integration maps it to schema v2."""
    return {
        "source": "recorder",
        "status": {"complete": "complete", "abandoned": "interrupted"}.get(summary.outcome, "incomplete"),
        "recorder": {"rules": rules.to_json(), "journal": journal, "summary": summary.to_json(),
                     "simulated": summary.simulated},
    }
