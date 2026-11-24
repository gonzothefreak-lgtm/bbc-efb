"""Flight tapes: record and replay Core updates so the Recorder can be driven offline.

A flight tape is a JSONL file. The first line is a header; every later line is one timed item.
``t`` is host seconds since the start of the tape::

    {"tape": "bbc-flight-tape", "version": 1, "name": "...", "description": "...",
     "start_host_utc": "2026-11-24T09:00:00+00:00", "source": {"name": "...", "simulated": true},
     "fields": [{"name": "SIM ON GROUND", "unit": "bool", "value_type": "boolean", "index": null}, ...],
     "fps": 30, "airports": [{"icao": "YSSY", "region": "YM", "lat": ..., "lon": ..., "alt_m": ...}],
     "expect": {...}}
    {"t": 0.0, "type": "connection", "state": "connected", "session_id": "s1", "detail": ""}
    {"t": 0.2, "type": "frame", "sim_time": 12.0, "abs_time": 63901234567.0, "rate": 1.0,
     "frames": 6, "paused": false, "values": {"SIM ON GROUND": true, "GROUND VELOCITY": 0.0, ...}}
    {"t": 5.0, "type": "event", "capability_id": "sim.pause_state", "value": 4}
    {"t": 9.0, "type": "gap", "dropped": 12, "reason": "Consumer update queue lost data."}
    {"t": 9.5, "type": "subscription_ended", "reason": "..."}
    {"t": 9.9, "type": "note", "text": "anything; ignored on replay"}

A ``frame`` is one simulator delivery of every tape field. Its values are in the units given
in the header. ``frames`` is how many simulation frames passed since the previous tape frame
(used to decimate per-frame subscriptions); ``paused`` frames are delivered only to
per-second subscriptions, because simulation frames stop in pause. A value missing from a
frame is unknown (None), never zero.

:class:`FakeConsumerAPI` implements the parts of ``bbc_core.core.ConsumerAPI`` the Recorder
uses (``connection``, ``subscribe``, ``subscribe_snapshot``, ``latest_snapshot``, ``poll``,
``unsubscribe``, ``request_nearby_airports``, ``facility_result``), so the service runs without
Core. :class:`TapeReplayer` plays a tape into it on a :class:`TapeClock`.
:class:`RecordingConsumerAPI` wraps a real ``ConsumerAPI`` and tees what it delivers into a
:class:`TapeWriter`, so a live session can be replayed later.

:class:`TapeBuilder` generates realistic tapes programmatically; ``SCENARIOS`` holds a few
(normal flight with pause, Active Pause, 4x rate and UTC midnight; bounced landing; mid-flight
disconnect; crash; queue gap; parked only). ``python -m bbc_recorder.tape generate DIR`` writes
them out. This module is Qt-free and SimConnect-free.
"""

from __future__ import annotations

import json
import math
import sys
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from bbc_core.contract import (
    Connection, ConnectionState, CoreRequestError, Event, Gap, Snapshot, SnapshotPeriod, Source, Subscription,
    SubscriptionEnded, ValueType, Capability, Kind,
)
from bbc_core import contract as _contract
from bbc_core.sdk_simvar import SimVar

TAPE_FORMAT = "bbc-flight-tape"
TAPE_VERSION = 1
ABSOLUTE_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)

SECOND_TOLERANCE_S = 0.15
ITEM_TYPES = ("connection", "frame", "event", "gap", "signal_error", "subscription_ended", "note")
# Contract C7: a provider per-signal error is its own type, not a Gap (not data loss).
_SignalError = getattr(_contract, "SignalError", None)


class TapeError(ValueError):
    """A tape cannot be read or written."""


def absolute_time_s(moment: datetime) -> float:
    """ABSOLUTE TIME (seconds since 0001-01-01 UTC) for an aware datetime."""
    return (moment.astimezone(timezone.utc) - ABSOLUTE_EPOCH).total_seconds()


def _field_json(variable: SimVar) -> dict[str, Any]:
    return {"name": variable.name, "unit": variable.unit, "value_type": variable.value_type.value,
            "index": variable.index}


def _field_from_json(payload: dict[str, Any]) -> SimVar:
    return SimVar(payload["name"], payload.get("unit"), ValueType(payload["value_type"]), payload.get("index"))


def _json_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# --------------------------------------------------------------------------------------------
# Tape model and file format
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TapeHeader:
    name: str
    fields: tuple[SimVar, ...]
    start_host_utc: datetime
    description: str = ""
    source_name: str = "Flight tape"
    simulated: bool = True
    fps: float = 30.0
    airports: tuple[dict[str, Any], ...] = ()
    expect: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "tape": TAPE_FORMAT, "version": TAPE_VERSION, "name": self.name, "description": self.description,
            "start_host_utc": self.start_host_utc.isoformat(),
            "source": {"name": self.source_name, "simulated": self.simulated},
            "fields": [_field_json(variable) for variable in self.fields], "fps": self.fps,
            "airports": [dict(item) for item in self.airports], "expect": dict(self.expect), "extra": dict(self.extra),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "TapeHeader":
        if payload.get("tape") != TAPE_FORMAT:
            raise TapeError("This file is not a BBC flight tape.")
        if int(payload.get("version", 0)) > TAPE_VERSION:
            raise TapeError(f"The tape was written by a newer version (tape version {payload.get('version')}).")
        source = payload.get("source") or {}
        start = datetime.fromisoformat(payload["start_host_utc"])
        return cls(
            name=str(payload.get("name") or "tape"),
            fields=tuple(_field_from_json(item) for item in payload.get("fields") or ()),
            start_host_utc=start if start.tzinfo else start.replace(tzinfo=timezone.utc),
            description=str(payload.get("description") or ""),
            source_name=str(source.get("name") or "Flight tape"), simulated=bool(source.get("simulated", True)),
            fps=float(payload.get("fps") or 30.0), airports=tuple(payload.get("airports") or ()),
            expect=dict(payload.get("expect") or {}), extra=dict(payload.get("extra") or {}),
        )


@dataclass(frozen=True, slots=True)
class TapeItem:
    t: float
    type: str
    data: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"t": round(self.t, 6), "type": self.type, **self.data}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "TapeItem":
        payload = dict(payload)
        t = payload.pop("t")
        kind = payload.pop("type")
        if type(t) not in (int, float) or not math.isfinite(t) or t < 0:
            raise TapeError("A tape item needs a finite, non-negative time 't'.")
        if kind not in ITEM_TYPES:
            raise TapeError(f"Unknown tape item type {kind!r}.")
        return cls(float(t), kind, payload)


@dataclass(frozen=True, slots=True)
class FlightTape:
    header: TapeHeader
    items: tuple[TapeItem, ...]
    issues: tuple[str, ...] = ()   # problems found while reading (torn last line etc.)

    @property
    def duration_s(self) -> float:
        return self.items[-1].t if self.items else 0.0

    def frames(self) -> Iterator[TapeItem]:
        return (item for item in self.items if item.type == "frame")

    def write(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(self.header.to_json(), separators=(",", ":")) + "\n")
            for item in self.items:
                handle.write(json.dumps(item.to_json(), separators=(",", ":")) + "\n")
        tmp.replace(path)
        return path

    @classmethod
    def read(cls, path: Path | str, *, strict: bool = False) -> "FlightTape":
        """Read a tape. A torn or damaged line is skipped and reported in ``issues``
        (``strict=True`` raises instead). Items must be in time order."""
        issues: list[str] = []
        header = None
        items: list[TapeItem] = []
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().split("\n")
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if header is None:
                    header = TapeHeader.from_json(payload)
                    continue
                item = TapeItem.from_json(payload)
            except (ValueError, KeyError, TypeError) as exc:
                if header is None or strict:
                    raise TapeError(f"Line {number} of the tape is unreadable: {exc}") from exc
                issues.append(f"line {number} skipped: {exc}")
                continue
            if items and item.t < items[-1].t:
                if strict:
                    raise TapeError(f"Line {number} goes back in time.")
                issues.append(f"line {number} goes back in time; skipped")
                continue
            items.append(item)
        if header is None:
            raise TapeError("The tape has no header.")
        return cls(header, tuple(items), tuple(issues))


# --------------------------------------------------------------------------------------------
# Recording a live session
# --------------------------------------------------------------------------------------------


class TapeWriter:
    """Write Core updates as tape items (JSONL, flushed per line).

    ``record(update)`` takes Snapshot, Event, Connection, Gap and SubscriptionEnded updates;
    other updates are ignored. Times come from the update's own receipt time relative to the
    first one recorded. Snapshot values are stored by SDK name in the units of ``fields``.
    """

    def __init__(self, path: Path | str, *, name: str, fields: tuple[SimVar, ...], description: str = "",
                 source_name: str = "MSFS", simulated: bool = False, start_host_utc: datetime | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "w", encoding="utf-8", newline="\n")
        self._start = start_host_utc
        self._header = TapeHeader(name, tuple(fields), start_host_utc or datetime.now(timezone.utc), description,
                                  source_name, simulated)
        self._header_written = False
        self._last_t = 0.0
        self._last_sim: float | None = None
        self.items = 0

    def _t(self, moment: datetime | None) -> float:
        if moment is None:
            return self._last_t
        if self._start is None:
            self._start = moment
            self._header = replace(self._header, start_host_utc=moment)
        t = max(self._last_t, (moment - self._start).total_seconds())
        self._last_t = t
        return t

    def _write(self, item: TapeItem) -> None:
        if not self._header_written:
            self._handle.write(json.dumps(self._header.to_json(), separators=(",", ":")) + "\n")
            self._header_written = True
        self._handle.write(json.dumps(item.to_json(), separators=(",", ":")) + "\n")
        self._handle.flush()
        self.items += 1

    def record(self, update: Any) -> None:
        if isinstance(update, Snapshot):
            t = self._t(update.received_at)
            values = {name: _json_value(value) for name, value in zip(update.names, update.values)}
            self._write(TapeItem(t, "frame", {
                "sim_time": update.simulation_time_s, "abs_time": update.absolute_time_s,
                "rate": update.simulation_rate, "frames": None,
                "paused": bool(self._last_sim is not None and update.simulation_time_s == self._last_sim),
                "values": values,
            }))
            self._last_sim = update.simulation_time_s
        elif isinstance(update, Event):
            self._write(TapeItem(self._t(update.received_at), "event",
                                 {"capability_id": update.capability_id, "value": _json_value(update.value)}))
        elif isinstance(update, Connection):
            self._write(TapeItem(self._t(update.occurred_at), "connection", {
                "state": update.state.value, "session_id": update.session_id, "detail": update.detail}))
        elif isinstance(update, SubscriptionEnded):
            self._write(TapeItem(self._t(None), "subscription_ended", {"reason": update.reason}))
        elif _SignalError is not None and isinstance(update, _SignalError):
            self._write(TapeItem(self._t(None), "signal_error", {
                "dropped": update.dropped_updates, "reason": update.reason,
                "capability_id": update.capability_id, "subscription_id": update.subscription_id}))
        elif isinstance(update, Gap):
            self._write(TapeItem(self._t(None), "gap", {"dropped": update.dropped_updates, "reason": update.reason}))

    def note(self, text: str) -> None:
        self._write(TapeItem(self._last_t, "note", {"text": text}))

    def close(self) -> Path:
        if not self._header_written:
            self._handle.write(json.dumps(self._header.to_json(), separators=(",", ":")) + "\n")
            self._header_written = True
        self._handle.close()
        return self.path


class RecordingConsumerAPI:
    """Wrap a ConsumerAPI and tee every polled update into a :class:`TapeWriter`.

    Only the snapshot subscription whose fields match the writer's fields is recorded as frames,
    so a tape holds one coherent stream. All other calls pass straight through.
    """

    def __init__(self, api: Any, writer: TapeWriter):
        self._api = api
        self._writer = writer
        self._snapshot_ids: set[str] = set()

    def subscribe_snapshot(self, fields, **kwargs):
        subscription = self._api.subscribe_snapshot(fields, **kwargs)
        self._snapshot_ids.add(subscription.id)
        return subscription

    def poll(self, **kwargs):
        batch = self._api.poll(**kwargs)
        for update in batch:
            if isinstance(update, Snapshot) and update.subscription_id not in self._snapshot_ids:
                continue
            self._writer.record(update)
        return batch

    def __getattr__(self, name):
        return getattr(self._api, name)


# --------------------------------------------------------------------------------------------
# Fake Core consumer handle
# --------------------------------------------------------------------------------------------

_UNIT_FACTORS = {
    ("feet per second", "feet per minute"): 60.0,
    ("feet per minute", "feet per second"): 1 / 60.0,
    ("feet", "meters"): 0.3048,
    ("meters", "feet"): 1 / 0.3048,
    ("knots", "meters per second"): 0.514444,
    ("meters per second", "knots"): 1 / 0.514444,
    ("pounds", "kilograms"): 0.45359237,
    ("kilograms", "pounds"): 1 / 0.45359237,
    ("degrees", "radians"): math.pi / 180,
    ("radians", "degrees"): 180 / math.pi,
}


def convert_unit(value: Any, from_unit: str | None, to_unit: str | None) -> Any:
    """Convert between the few units tapes need. Unknown conversions give None (unknown)."""
    if value is None or from_unit == to_unit or isinstance(value, (bool, str)):
        return value
    factor = _UNIT_FACTORS.get(((from_unit or "").lower(), (to_unit or "").lower()))
    if factor is None:
        if {(from_unit or "").lower(), (to_unit or "").lower()} <= {"bool", "boolean", "number", ""}:
            return value
        return None
    return value * factor


class TapeClock:
    """Manual clock shared by the fake API, the replayer and the service under test."""

    def __init__(self, start_utc: datetime | None = None, t: float = 0.0):
        self.start_utc = start_utc or datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
        self.t = float(t)

    def now(self) -> datetime:
        return self.start_utc + timedelta(seconds=self.t)

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("A tape clock never goes backwards.")
        self.t += seconds

    def advance_to(self, t: float) -> None:
        self.t = max(self.t, float(t))


@dataclass(slots=True)
class _FakeSnapshotSub:
    id: str
    fields: tuple[SimVar, ...]
    period: SnapshotPeriod
    every: int
    changed_only: bool
    next_due_t: float | None = None
    frame_credit: float = 0.0
    last_values: tuple | None = None
    latest: Snapshot | None = None


class FakeConsumerAPI:
    """A stand-in for ``bbc_core.core.ConsumerAPI`` driven by tapes or by test code.

    Known event capability IDs come from ``bbc_core.catalog.READS``; ``unsupported`` lists
    IDs to refuse. ``queue_limit`` bounds the update queue: on overflow the oldest data
    updates are dropped and one ``Gap`` is delivered at the next ``poll``, as Core does.
    ``fail_snapshot_periods`` makes ``subscribe_snapshot`` refuse those periods.
    """

    def __init__(self, *, clock: TapeClock | None = None, fields: tuple[SimVar, ...] = (),
                 source: Source | None = None, unsupported: Iterable[str] = (), queue_limit: int = 100_000,
                 fail_snapshot_periods: Iterable[SnapshotPeriod] = (), fps: float = 30.0,
                 airports: Iterable[dict[str, Any]] = ()):
        from bbc_core.catalog import READS  # noqa: PLC0415 - catalog only, no SimConnect

        self.clock = clock or TapeClock()
        self.source = source or Source("Flight tape", True)
        self.tape_fields = {variable.sdk_name: variable for variable in fields}
        self._reads = READS
        self._unsupported = set(unsupported)
        self._queue: deque = deque()
        self._queue_limit = queue_limit
        self._dropped = 0
        self._ids = 0
        self._snapshots: dict[str, _FakeSnapshotSub] = {}
        self._events: dict[str, str] = {}
        self._facility: dict[str, Any] = {}
        self._fail_periods = set(fail_snapshot_periods)
        self.fps = fps
        self.airports = list(airports)
        self.connection_state = Connection(ConnectionState.DISCONNECTED, self.source, None, self.clock.now(),
                                           "Not connected yet.")
        self.initial_connection = self.connection_state
        self.closed = False
        self.calls: list[tuple[str, Any]] = []     # (method, detail) for assertions
        self.control_groups: tuple[str, ...] = ()  # read-only handle

    # ---- ConsumerAPI surface ---------------------------------------------------------------

    def connection(self) -> Connection:
        return self.connection_state

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}-{self._ids}"

    def subscribe(self, capability_id: str, *, interval_ms: int | None = None,
                  max_age_ms: int | None = None) -> Subscription:
        self._check_open()
        mapping = self._reads.get(capability_id)
        if mapping is None or capability_id in self._unsupported:
            raise CoreRequestError(f"Capability {capability_id} is not available from this provider.")
        sub = Subscription(self._next_id("sub"), mapping.capability, interval_ms)
        self._events[sub.id] = capability_id
        self.calls.append(("subscribe", capability_id))
        return sub

    def subscribe_snapshot(self, fields: tuple[SimVar, ...], *, period: SnapshotPeriod = SnapshotPeriod.SECOND,
                           every: int = 1, changed_only: bool = False) -> Subscription:
        self._check_open()
        if not isinstance(fields, tuple) or not 1 <= len(fields) <= 64:
            raise CoreRequestError("Supply a tuple of 1 to 64 SimVar fields.")
        if not isinstance(period, SnapshotPeriod):
            raise CoreRequestError("Select a SnapshotPeriod.")
        if type(every) is not int or every < 1:
            raise CoreRequestError("Snapshot period count must be a positive integer.")
        names = [variable.sdk_name for variable in fields]
        if len(set(names)) != len(names):
            raise CoreRequestError("Each snapshot field may appear only once.")
        if period in self._fail_periods:
            raise CoreRequestError(f"The provider cannot deliver {period.value} snapshots.")
        sub_id = self._next_id("snap")
        self._snapshots[sub_id] = _FakeSnapshotSub(sub_id, fields, period, every, changed_only)
        self.calls.append(("subscribe_snapshot", (period.value, every)))
        capability = Capability(f"sdk.snapshot:{sub_id}", Kind.VALUE, ValueType.SNAPSHOT, None,
                                "Fake snapshot of " + ", ".join(names))
        return Subscription(sub_id, capability, None)

    def latest_snapshot(self, subscription_id: str) -> Snapshot | None:
        sub = self._snapshots.get(subscription_id)
        if sub is None:
            raise CoreRequestError("Unknown subscription.")
        return sub.latest

    def poll(self, *, limit: int = 128) -> tuple:
        self._check_open()
        batch: list = []
        if self._dropped:
            batch.append(Gap(self._dropped, "Consumer update queue lost data."))
            self._dropped = 0
        while self._queue and len(batch) < limit:
            batch.append(self._queue.popleft())
        return tuple(batch)

    def unsubscribe(self, subscription_id: str) -> None:
        self.calls.append(("unsubscribe", subscription_id))
        if self._snapshots.pop(subscription_id, None) is None and self._events.pop(subscription_id, None) is None:
            raise CoreRequestError("Unknown subscription.")
        # Like Core, updates already queued for the subscription stay queued.

    def request_nearby_airports(self):
        from bbc_core.facilities import FacilityResult, FacilityState  # noqa: PLC0415

        self._check_open()
        request_id = self._next_id("fac")
        pending = FacilityResult(request_id, FacilityState.PENDING, self.source, self._session())
        self._facility[request_id] = pending
        self.calls.append(("request_nearby_airports", request_id))
        if self.connection_state.state is not ConnectionState.CONNECTED:
            done = FacilityResult(request_id, FacilityState.UNAVAILABLE, self.source, None, self.clock.now(), None,
                                  "The simulator is not connected.")
        elif not self.airports:
            done = FacilityResult(request_id, FacilityState.NO_DATA, self.source, self._session(), self.clock.now(),
                                  (), "No airports in the local scenery cache.")
        else:
            from bbc_core.facilities import AirportReference  # noqa: PLC0415

            data = tuple(AirportReference(item["icao"], item.get("region", ""), float(item["lat"]),
                                          float(item["lon"]), float(item.get("alt_m", 0.0)))
                         for item in self.airports)
            done = FacilityResult(request_id, FacilityState.READY, self.source, self._session(), self.clock.now(),
                                  data)
        self._facility[request_id] = done
        self._offer(done)
        return pending

    def facility_result(self, request_id: str):
        return self._facility.get(request_id)

    def close(self) -> None:
        self.closed = True

    # ---- driving the fake ------------------------------------------------------------------

    def _check_open(self) -> None:
        if self.closed:
            raise CoreRequestError("This consumer handle is closed.")

    def _session(self) -> str | None:
        return self.connection_state.session_id

    def _offer(self, update: Any, *, control: bool = False) -> None:
        if not control and len(self._queue) >= self._queue_limit:
            # Drop the oldest data update, keep control updates (Connection, results).
            for index, queued in enumerate(self._queue):
                if isinstance(queued, (Snapshot, Event)):
                    del self._queue[index]
                    self._dropped += 1
                    break
            else:
                self._dropped += 1
                return
        self._queue.append(update)

    def push_connection(self, state: ConnectionState | str, session_id: str | None = None, detail: str = "") -> None:
        state = ConnectionState(state)
        self.connection_state = Connection(state, self.source, session_id if state is ConnectionState.CONNECTED
                                           else None, self.clock.now(), detail)
        if state is not ConnectionState.CONNECTED:
            for sub in self._snapshots.values():
                sub.latest = None
                sub.next_due_t = None
                sub.last_values = None
        self._offer(self.connection_state, control=True)

    def push_event(self, capability_id: str, value: Any) -> None:
        if self.connection_state.state is not ConnectionState.CONNECTED:
            return
        if capability_id not in self._events.values():
            return
        decoded = value
        if capability_id == "sim.pause_state" and value is not None:
            from bbc_core.contract import PauseState  # noqa: PLC0415

            decoded = PauseState(int(value))
        self._offer(Event(capability_id, decoded, self.source, self._session(), self.clock.now()))

    def push_gap(self, dropped: int, reason: str) -> None:
        self._offer(Gap(int(dropped), reason), control=True)

    def push_signal_error(self, dropped: int, reason: str, capability_id: str = "",
                          subscription_id: str = "") -> None:
        """Deliver a provider per-signal error (contract C7: not a Gap, not data loss)."""
        if _SignalError is None:  # an older Core without SignalError: it was a Gap there
            self.push_gap(dropped, reason)
            return
        self._offer(_SignalError(int(dropped), reason, capability_id, subscription_id), control=True)

    def end_snapshot_subscriptions(self, reason: str) -> None:
        for sub_id in list(self._snapshots):
            del self._snapshots[sub_id]
            self._offer(SubscriptionEnded(sub_id, f"sdk.snapshot:{sub_id}", reason), control=True)

    def push_frame(self, *, sim_time: float | None, abs_time: float | None, rate: float | None,
                   values: dict[str, Any], frames: int | None = None, paused: bool = False) -> int:
        """Deliver one simulator frame to every active snapshot subscription that is due.

        Returns how many snapshots were queued.
        """
        if self.connection_state.state is not ConnectionState.CONNECTED:
            return 0
        frames = frames if frames is not None else 1
        now = self.clock.now()
        t = self.clock.monotonic()
        delivered = 0
        sample_time = None
        if abs_time is not None:
            try:
                sample_time = ABSOLUTE_EPOCH + timedelta(seconds=abs_time)
            except OverflowError:
                sample_time = None
        for sub in list(self._snapshots.values()):
            if sub.period is SnapshotPeriod.SECOND:
                # A frame up to SECOND_TOLERANCE_S early still counts (simulator jitter).
                if sub.next_due_t is not None and t + SECOND_TOLERANCE_S < sub.next_due_t:
                    continue
                # Schedule against a due time so jitter does not halve the cadence.
                base = sub.next_due_t if sub.next_due_t is not None and t - sub.next_due_t < sub.every else t
                sub.next_due_t = base + sub.every
            else:
                if paused:
                    continue  # simulation frames stop while paused
                first = sub.latest is None
                sub.frame_credit += frames
                if not first and sub.frame_credit < sub.every:
                    continue
                sub.frame_credit = 0.0 if first or sub.every <= 1 else sub.frame_credit % sub.every
            snapshot_values = tuple(self._value(variable, values) for variable in sub.fields)
            if sub.changed_only and sub.last_values == snapshot_values:
                continue
            sub.last_values = snapshot_values
            snapshot = Snapshot(sub.id, tuple(variable.sdk_name for variable in sub.fields), snapshot_values,
                                sim_time, abs_time, rate, self.source, self._session() or "", now, sample_time)
            sub.latest = snapshot
            self._offer(snapshot)
            delivered += 1
        return delivered

    def _value(self, variable: SimVar, values: dict[str, Any]) -> Any:
        name = variable.sdk_name
        if name not in values:
            return None
        value = values[name]
        tape_field = self.tape_fields.get(name)
        if tape_field is not None and tape_field.unit != variable.unit:
            value = convert_unit(value, tape_field.unit, variable.unit)
        if value is None:
            return None
        if variable.value_type is ValueType.BOOLEAN:
            return bool(value)
        if variable.value_type is ValueType.INTEGER:
            return int(value) if not isinstance(value, str) else None
        if variable.value_type is ValueType.NUMBER:
            return float(value) if not isinstance(value, (str, bool)) else None
        if variable.value_type is ValueType.TEXT:
            return value if isinstance(value, str) else None
        return value

    @property
    def active_snapshot_subscriptions(self) -> tuple[_FakeSnapshotSub, ...]:
        return tuple(self._snapshots.values())

    @property
    def active_event_subscriptions(self) -> tuple[str, ...]:
        return tuple(self._events.values())

    @classmethod
    def for_tape(cls, tape: FlightTape, clock: TapeClock | None = None, **kwargs) -> "FakeConsumerAPI":
        clock = clock or TapeClock(tape.header.start_host_utc)
        return cls(clock=clock, fields=tape.header.fields, source=Source(tape.header.source_name, tape.header.simulated),
                   fps=tape.header.fps, airports=tape.header.airports, **kwargs)


# --------------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------------


class TapeReplayer:
    """Play a tape into a :class:`FakeConsumerAPI`, ticking a consumer on a fixed host cadence.

    ``run(tick)`` advances the shared clock in ``tick_s`` steps (default 0.1 s, like a host that
    pumps Core 10 times per second), releases every tape item whose time has come, then calls
    ``tick()``. ``hooks`` maps a tape time to a callable run once when the clock reaches it,
    for scripted pilot actions.
    """

    def __init__(self, tape: FlightTape, api: FakeConsumerAPI | None = None, *, clock: TapeClock | None = None):
        self.tape = tape
        self.clock = clock or (api.clock if api is not None else TapeClock(tape.header.start_host_utc))
        self.api = api or FakeConsumerAPI.for_tape(tape, self.clock)
        self._index = 0

    @property
    def finished(self) -> bool:
        return self._index >= len(self.tape.items)

    def release_until(self, t: float) -> int:
        released = 0
        items = self.tape.items
        while self._index < len(items) and items[self._index].t <= t + 1e-9:
            item = items[self._index]
            self.clock.advance_to(item.t)
            self._apply(item)
            self._index += 1
            released += 1
        self.clock.advance_to(t)
        return released

    def _apply(self, item: TapeItem) -> None:
        data = item.data
        api = self.api
        if item.type == "frame":
            api.push_frame(sim_time=data.get("sim_time"), abs_time=data.get("abs_time"), rate=data.get("rate"),
                           values=data.get("values") or {}, frames=data.get("frames"),
                           paused=bool(data.get("paused")))
        elif item.type == "event":
            api.push_event(data["capability_id"], data.get("value"))
        elif item.type == "connection":
            api.push_connection(data["state"], data.get("session_id"), data.get("detail") or "")
        elif item.type == "gap":
            api.push_gap(int(data.get("dropped") or 0), str(data.get("reason") or "Updates were lost."))
        elif item.type == "signal_error":
            api.push_signal_error(int(data.get("dropped") or 0),
                                  str(data.get("reason") or "The simulator reported an error for a signal."),
                                  str(data.get("capability_id") or ""), str(data.get("subscription_id") or ""))
        elif item.type == "subscription_ended":
            api.end_snapshot_subscriptions(str(data.get("reason") or "Core ended the subscription."))

    def run(self, tick: Callable[[], Any], *, tick_s: float = 0.1, until: float | None = None,
            hooks: dict[float, Callable[[], Any]] | None = None, settle_s: float = 1.0) -> int:
        """Replay to the end (or ``until``) and keep ticking ``settle_s`` longer. Returns ticks."""
        end = (self.tape.duration_s if until is None else until) + settle_s
        pending = sorted((hooks or {}).items())
        ticks = 0
        t = self.clock.monotonic()
        while t < end - 1e-9:
            t = min(end, t + tick_s)
            # Hooks run at their exact time, between tape items, like a pilot pressing a button.
            while pending and pending[0][0] <= t:
                at, hook = pending.pop(0)
                self.release_until(at)
                tick()
                hook()
            self.release_until(t)
            tick()
            ticks += 1
        return ticks


# --------------------------------------------------------------------------------------------
# Programmatic tapes
# --------------------------------------------------------------------------------------------


def tape_fields() -> tuple[SimVar, ...]:
    """The fields generated tapes carry: exactly what the Recorder service subscribes to
    (``rules.snapshot_simvars()`` and ``attribution.identity_simvars()``)."""
    from .attribution import identity_simvars  # noqa: PLC0415
    from .rules import snapshot_simvars  # noqa: PLC0415

    seen: dict[str, SimVar] = {}
    for variable in (*snapshot_simvars(), *identity_simvars()):
        seen.setdefault(variable.sdk_name, variable)
    return tuple(seen.values())


@dataclass(frozen=True, slots=True)
class CoreSession:
    """One simulator session of a tape, as a ``bbc_core.simulated.FlightTape``.

    ``start_t``/``end_t`` are tape seconds (``end_t`` is None when the tape ends connected).
    Play ``tape`` on a ``SimulatedProvider`` connected at ``start_t``, with its ABSOLUTE TIME
    starting at ``absolute_time_s`` and the simulation rate at ``rate``. Tape entry times are
    seconds from ``start_t``. A disconnect at ``end_t`` is the tape's last entry.
    """

    start_t: float
    end_t: float | None
    tape: Any
    absolute_time_s: float | None
    rate: float
    detail: str = ""


def core_sessions(tape: FlightTape) -> list[CoreSession]:
    """Convert a Recorder tape into per-session ``bbc_core.simulated.FlightTape`` scripts.

    The simulated provider then plays them through real Core: frame values become value
    entries (only changes after the first frame), ``sim.pause_state`` becomes a PAUSE action,
    rate changes a RATE action, Core queue gaps a GAP action and a disconnect a DISCONNECT
    action. The provider models SIMULATION TIME and ABSOLUTE TIME itself from the starting
    ABSOLUTE TIME, the rate and pauses, exactly as it does for any other script.
    """
    from bbc_core.simulated import FlightTape as CoreTape, TapeAction  # noqa: PLC0415

    fields = {variable.sdk_name: variable for variable in tape.header.fields}
    sessions: list[CoreSession] = []
    entries: list[tuple] | None = None
    start = 0.0
    last_values: dict[str, Any] = {}
    rate = 1.0
    absolute: float | None = None
    start_rate = 1.0

    def finish(end_t: float | None, detail: str = "") -> None:
        nonlocal entries
        if entries is None:
            return
        if end_t is not None:
            entries.append((max(0.0, end_t - start), TapeAction.DISCONNECT, detail or "Simulated transport loss."))
        sessions.append(CoreSession(start, end_t, CoreTape(entries), absolute, start_rate, detail))
        entries = None

    for item in tape.items:
        data = item.data
        if item.type == "connection":
            state = str(data.get("state") or "")
            if state == "connected" and entries is None:
                entries, start, last_values, absolute = [], item.t, {}, None
                start_rate = rate
            elif state != "connected":
                finish(item.t, str(data.get("detail") or ""))
            continue
        if entries is None:
            continue
        at = max(0.0, item.t - start)
        if item.type == "frame":
            if absolute is None and isinstance(data.get("abs_time"), (int, float)):
                absolute = float(data["abs_time"]) - at
                start_rate = float(data.get("rate") or 1.0)
            frame_rate = data.get("rate")
            if isinstance(frame_rate, (int, float)) and frame_rate > 0 and abs(frame_rate - rate) > 1e-9:
                if last_values:  # the first frame's rate is the session's starting rate
                    entries.append((at, TapeAction.RATE, float(frame_rate)))
                rate = float(frame_rate)
            for name, value in (data.get("values") or {}).items():
                variable = fields.get(name)
                if variable is None or (name in last_values and last_values[name] == value):
                    continue
                last_values[name] = value
                entries.append((at, variable, value))
        elif item.type == "event":
            capability_id = data.get("capability_id")
            if capability_id == "sim.pause_state":
                entries.append((at, TapeAction.PAUSE, int(data.get("value") or 0)))
            else:
                entries.append((at, capability_id, data.get("value")))
        elif item.type == "gap":
            entries.append((at, TapeAction.GAP, max(1, int(data.get("dropped") or 1))))
    finish(None)
    return sessions


AIRPORTS = {
    "YSSY": {"icao": "YSSY", "region": "YM", "lat": -33.9461, "lon": 151.1772, "alt_m": 6.0},
    "YSBK": {"icao": "YSBK", "region": "YM", "lat": -33.9244, "lon": 150.9883, "alt_m": 9.0},
    "YWLM": {"icao": "YWLM", "region": "YM", "lat": -32.7950, "lon": 151.8344, "alt_m": 9.0},
    "YSCN": {"icao": "YSCN", "region": "YM", "lat": -34.0403, "lon": 150.6872, "alt_m": 70.0},
}


def _bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _distance_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(a)))


class TapeBuilder:
    """Generate a plausible flight tape with simple kinematics.

    Host time ``t`` and simulator time advance together (scaled by the sim rate, frozen in
    pause). Frames are written five times per second near the ground and once per second
    elsewhere, which is what the tape can offer a per-frame subscription. The aircraft is a
    generic twin; every value is synthetic and the header says so.
    """

    def __init__(self, name: str, *, airport: str = "YSSY", start_host_utc: datetime | None = None,
                 sim_start_utc: datetime | None = None, description: str = "", fps: float = 30.0,
                 title: str = "Generic Twin Jet (synthetic tape)", atc_id: str = "VH-TPE",
                 previous_touchdown_fps: float = 6.5, airports: Iterable[str] = ("YSSY", "YSBK", "YWLM", "YSCN")):
        self.name = name
        self.description = description
        self.start = start_host_utc or datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
        self.sim_utc = sim_start_utc or datetime(2026, 11, 24, 21, 30, tzinfo=timezone.utc)
        self.fps = fps
        self.fields = tape_fields()
        self.airports = tuple(AIRPORTS[code] for code in airports)
        self.items: list[TapeItem] = []
        self.expect: dict[str, Any] = {}
        self.t = 0.0
        self.sim_time = 120.0       # SIMULATION TIME when the tape begins (the flight was loaded earlier)
        self.rate = 1.0
        self.paused = False
        self.connected = False
        self.session = 0
        home = AIRPORTS[airport]
        self.state: dict[str, Any] = {
            "lat": home["lat"] + 0.0040, "lon": home["lon"] - 0.0030, "alt_ground_ft": home["alt_m"] / 0.3048,
            "agl": 0.0, "gs": 0.0, "ias": 0.0, "tas": 0.0, "vs": 0.0, "hdg": 160.0, "pitch": 0.0, "bank": 0.0,
            "on_ground": True, "brake": True, "engines": [False, False], "gear_down": True, "flaps": 0,
            "td_fps": previous_touchdown_fps, "td_pitch": 3.1, "td_bank": 0.2, "fuel_lb": 9800.0,
            "slew": False, "on_runway": False, "g": 1.0,
        }
        self.title = title
        self.atc_id = atc_id

    # ---- low-level -------------------------------------------------------------------------

    def _add(self, kind: str, **data) -> None:
        self.items.append(TapeItem(round(self.t, 6), kind, data))

    def note(self, text: str) -> "TapeBuilder":
        self._add("note", text=text)
        return self

    def connect(self, detail: str = "Connected to the simulator.") -> "TapeBuilder":
        self.session += 1
        self.connected = True
        self._add("connection", state="connected", session_id=f"tape-session-{self.session}", detail=detail)
        return self

    def disconnect(self, detail: str = "The simulator closed the connection.") -> "TapeBuilder":
        self.connected = False
        self._add("connection", state="disconnected", session_id=None, detail=detail)
        return self

    def event(self, capability_id: str, value: Any = 0) -> "TapeBuilder":
        self._add("event", capability_id=capability_id, value=value)
        return self

    def gap(self, dropped: int, reason: str = "Consumer update queue lost data.") -> "TapeBuilder":
        self._add("gap", dropped=dropped, reason=reason)
        return self

    def _values(self) -> dict[str, Any]:
        st = self.state
        on_ground = st["on_ground"]
        values = {
            "SIM ON GROUND": on_ground, "GROUND VELOCITY": round(st["gs"], 3),
            "AIRSPEED INDICATED": round(st["ias"], 3), "AIRSPEED TRUE": round(st["tas"], 3),
            "VERTICAL SPEED": round(st["vs"], 3), "PLANE LATITUDE": round(st["lat"], 7),
            "PLANE LONGITUDE": round(st["lon"], 7), "PLANE ALTITUDE": round(st["alt_ground_ft"] + st["agl"], 2),
            "PLANE ALT ABOVE GROUND": round(st["agl"], 2),
            "PLANE ALT ABOVE GROUND MINUS CG": round(max(0.0, st["agl"] - 6.0), 2),
            "PLANE HEADING DEGREES TRUE": round(st["hdg"], 2), "PLANE PITCH DEGREES": round(st["pitch"], 2),
            "PLANE BANK DEGREES": round(st["bank"], 2), "G FORCE": round(st["g"], 3),
            "ON ANY RUNWAY": st["on_runway"],
            "GEAR IS ON GROUND:0": on_ground, "GEAR IS ON GROUND:1": on_ground, "GEAR IS ON GROUND:2": on_ground,
            "GEAR HANDLE POSITION": st["gear_down"], "BRAKE PARKING POSITION": st["brake"],
            "FLAPS HANDLE INDEX": st["flaps"],
            "PLANE TOUCHDOWN NORMAL VELOCITY": round(st["td_fps"], 3),
            "PLANE TOUCHDOWN PITCH DEGREES": st["td_pitch"], "PLANE TOUCHDOWN BANK DEGREES": st["td_bank"],
            "NUMBER OF ENGINES": 2,
            "GENERAL ENG COMBUSTION:1": st["engines"][0], "GENERAL ENG COMBUSTION:2": st["engines"][1],
            "GENERAL ENG COMBUSTION:3": False, "GENERAL ENG COMBUSTION:4": False,
            "FUEL TOTAL QUANTITY WEIGHT": round(st["fuel_lb"], 1), "TOTAL WEIGHT": round(98000 + st["fuel_lb"], 1),
            "IS SLEW ACTIVE": st["slew"], "TITLE": self.title, "ATC ID": self.atc_id,
            "ATC MODEL": "TT:ATCCOM.AC_MODEL_GTJ.0.text", "ATC TYPE": "TT:ATCCOM.ATC_NAME GENERIC.0.text",
            "ATC AIRLINE": "", "ATC FLIGHT NUMBER": "",
        }
        # Tapes carry the units of their header fields; the default fields use feet per second
        # for the touchdown velocity, the state machine's fields may use feet per minute.
        for variable in self.fields:
            name = variable.sdk_name
            if name == "PLANE TOUCHDOWN NORMAL VELOCITY" and variable.unit == "feet per minute":
                values[name] = round(st["td_fps"] * 60.0, 3)
        return {variable.sdk_name: values.get(variable.sdk_name) for variable in self.fields
                if variable.sdk_name in values}

    def _frame(self, frames: int) -> None:
        self._add("frame", sim_time=round(self.sim_time, 4), abs_time=round(absolute_time_s(self.sim_utc), 3),
                  rate=self.rate, frames=frames, paused=self.paused, values=self._values())

    def _near_ground(self) -> bool:
        return self.state["agl"] < 1500 or (self.state["on_ground"] and self.state["gs"] > 2)

    def advance(self, seconds: float, *, target: tuple[float, float] | None = None,
                step: Callable[[float], None] | None = None) -> "TapeBuilder":
        """Advance host time by ``seconds``, moving the aircraft and writing frames."""
        remaining = seconds
        while remaining > 1e-9:
            dt = min(remaining, 0.2 if self._near_ground() else 1.0)
            remaining -= dt
            self.t += dt
            sim_dt = 0.0 if self.paused else dt * self.rate
            self.sim_time += sim_dt
            self.sim_utc += timedelta(seconds=sim_dt)
            st = self.state
            if step is not None and not self.paused:
                step(sim_dt)
            if not self.paused and st["gs"] > 0:
                if target is not None:
                    st["hdg"] = _bearing(st["lat"], st["lon"], *target)
                distance_nm = st["gs"] * sim_dt / 3600.0
                heading = math.radians(st["hdg"])
                st["lat"] += distance_nm / 60.0 * math.cos(heading)
                st["lon"] += distance_nm / 60.0 * math.sin(heading) / max(0.2, math.cos(math.radians(st["lat"])))
                st["fuel_lb"] = max(0.0, st["fuel_lb"] - (0.9 if st["on_ground"] else 1.4) * sim_dt)
            if self.connected:
                self._frame(0 if self.paused else max(1, round(dt * self.fps)))
        return self

    # ---- flight segments -------------------------------------------------------------------

    def parked(self, seconds: float) -> "TapeBuilder":
        st = self.state
        st.update(gs=0.0, ias=0.0, tas=0.0, vs=0.0)
        return self.advance(seconds)

    def start_engines(self) -> "TapeBuilder":
        self.state["engines"] = [True, False]
        self.parked(20)
        self.state["engines"] = [True, True]
        return self.parked(30)

    def release_brake(self) -> "TapeBuilder":
        self.state["brake"] = False
        return self.parked(3)

    def taxi(self, seconds: float, speed_kt: float = 14.0, heading: float | None = None,
             toward: str | None = None) -> "TapeBuilder":
        st = self.state
        if heading is not None:
            st["hdg"] = heading
        st.update(gs=speed_kt, ias=max(0.0, speed_kt - 3), tas=speed_kt, vs=0.0, flaps=1, on_runway=False)
        target = (AIRPORTS[toward]["lat"], AIRPORTS[toward]["lon"]) if toward else None
        if target is None:
            return self.advance(seconds)
        # Taxi toward the terminal, stopping short of the reference point.
        remaining = seconds
        while remaining > 0 and _distance_nm(st["lat"], st["lon"], *target) > 0.15:
            self.advance(min(1.0, remaining), target=target)
            remaining -= 1.0
        return self

    def stop(self, seconds: float = 5.0) -> "TapeBuilder":
        st = self.state
        st.update(gs=0.0, ias=0.0, tas=0.0)
        return self.advance(seconds)

    def takeoff(self, heading: float = 160.0, rotate_kt: float = 150.0) -> "TapeBuilder":
        st = self.state
        st.update(hdg=heading, on_runway=True)
        st["gs"] = 5.0

        def roll(dt):
            st["gs"] = min(rotate_kt, st["gs"] + 4.5 * dt)
            st["ias"] = st["tas"] = st["gs"]

        while st["gs"] < rotate_kt - 1e-6:
            self.advance(0.2, step=roll)
        st.update(on_ground=False, on_runway=False, vs=1800.0, pitch=8.0)

        def climb(dt):
            st["agl"] += st["vs"] / 60.0 * dt
            st["gs"] = min(250.0, st["gs"] + 0.6 * dt)
            st["ias"] = st["tas"] = st["gs"]

        self.advance(20, step=climb)
        st.update(gear_down=False, flaps=0)
        return self

    def climb_to(self, agl_ft: float, vs_fpm: float = 2200.0, speed_kt: float = 280.0,
                 target: tuple[float, float] | None = None) -> "TapeBuilder":
        st = self.state
        st.update(vs=vs_fpm, pitch=6.0)

        def climb(dt):
            st["agl"] = min(agl_ft, st["agl"] + st["vs"] / 60.0 * dt)
            st["gs"] = min(speed_kt, st["gs"] + 1.0 * dt)
            st["ias"] = st["gs"] * 0.85
            st["tas"] = st["gs"]

        while st["agl"] < agl_ft - 1e-6:
            self.advance(1.0, step=climb, target=target)
        st.update(vs=0.0, pitch=2.0)
        return self

    def cruise(self, seconds: float, speed_kt: float = 300.0, target: tuple[float, float] | None = None) -> "TapeBuilder":
        st = self.state
        st.update(gs=speed_kt, tas=speed_kt, ias=speed_kt * 0.8, vs=0.0)
        return self.advance(seconds, target=target)

    def cruise_to(self, airport: str, *, descent_nm: float = 30.0, speed_kt: float = 300.0) -> "TapeBuilder":
        dest = AIRPORTS[airport]
        target = (dest["lat"], dest["lon"])
        st = self.state
        st.update(gs=speed_kt, tas=speed_kt, ias=speed_kt * 0.8, vs=0.0)
        while _distance_nm(st["lat"], st["lon"], *target) > descent_nm:
            self.advance(1.0, target=target)
        return self

    def descend_and_land(self, airport: str, *, touchdown_fpm: float = -180.0, bounce: bool = False,
                         approach_kt: float = 140.0) -> "TapeBuilder":
        dest = AIRPORTS[airport]
        target = (dest["lat"], dest["lon"])
        st = self.state
        st["alt_ground_ft"] = dest["alt_m"] / 0.3048
        distance = max(1.0, _distance_nm(st["lat"], st["lon"], *target))
        # Constant-angle descent to arrive at 50 ft over the field.
        while True:
            distance = _distance_nm(st["lat"], st["lon"], *target)
            if distance < 0.15 or st["agl"] <= 50:
                break
            st["gs"] = max(approach_kt, min(st["gs"], 140 + distance * 6))
            st["ias"] = st["tas"] = st["gs"]
            time_left_s = distance / max(st["gs"], 1) * 3600
            st["vs"] = -min(3000.0, max(500.0, (st["agl"] - 50) / max(time_left_s, 1) * 60))
            if st["agl"] < 3000:
                st.update(gear_down=True, flaps=3)
            st["pitch"] = 2.5

            def down(dt):
                st["agl"] = max(50.0, st["agl"] + st["vs"] / 60.0 * dt)

            self.advance(0.2 if self._near_ground() else 1.0, step=down, target=target)
        # Flare to touchdown.
        st["vs"] = touchdown_fpm * 2.5

        def flare(dt):
            st["vs"] = min(touchdown_fpm, st["vs"] * 0.92)
            st["agl"] = max(0.0, st["agl"] + st["vs"] / 60.0 * dt)

        while st["agl"] > 0.0:
            self.advance(0.2, step=flare)
        self._touchdown(touchdown_fpm)
        if bounce:
            self.advance(0.4)  # main gear contact for two frames, then airborne again
            st.update(on_ground=False, on_runway=False, vs=180.0, g=0.9)

            def hop(dt):
                st["agl"] = max(0.0, st["agl"] + st["vs"] / 60.0 * dt)
                st["vs"] -= 600 * dt

            self.advance(1.0, step=hop)
            while st["agl"] > 0.0:
                self.advance(0.2, step=hop)
            self._touchdown(-120.0)
        return self

    def _touchdown(self, fpm: float) -> None:
        st = self.state
        st.update(on_ground=True, agl=0.0, vs=0.0, pitch=0.5, on_runway=True, g=1.0 + abs(fpm) / 900)
        st["td_fps"] = round(fpm / 60.0, 3)
        st["td_pitch"] = 3.4

    def rollout(self, to_kt: float = 15.0, decel_kt_s: float = 5.0) -> "TapeBuilder":
        st = self.state

        def brake(dt):
            st["gs"] = max(to_kt, st["gs"] - decel_kt_s * dt)
            st["ias"] = st["tas"] = st["gs"]
            st["g"] = 1.0

        while st["gs"] > to_kt + 1e-6:
            self.advance(0.2, step=brake)
        st["on_runway"] = False
        return self

    def shutdown(self) -> "TapeBuilder":
        self.stop(5)
        self.state["brake"] = True
        self.parked(10)
        self.state["engines"] = [False, False]
        return self.parked(30)

    def pause(self, seconds: float, *, active: bool = False) -> "TapeBuilder":
        flags = 4 if active else 1
        self.event("sim.pause_state", flags)
        self.paused = True
        self.advance(seconds)
        self.paused = False
        self.event("sim.pause_state", 0)
        return self

    def set_rate(self, rate: float) -> "TapeBuilder":
        self.rate = rate
        return self

    def offline(self, seconds: float) -> "TapeBuilder":
        """Time passes with no connection (frames are not written while disconnected)."""
        return self.advance(seconds)

    def build(self) -> FlightTape:
        header = TapeHeader(self.name, self.fields, self.start, self.description or self.name,
                            "Flight tape (synthetic)", True, self.fps, self.airports, dict(self.expect))
        return FlightTape(header, tuple(self.items))


# ---- scenarios -------------------------------------------------------------------------------


def _depart(builder: TapeBuilder) -> TapeBuilder:
    return (builder.connect().parked(30).start_engines().release_brake()
            .taxi(90, 14.0, heading=200).taxi(60, 12.0, heading=160).stop(10))


def normal_flight() -> FlightTape:
    """YSSY to YWLM with a full pause, an Active Pause, 4x rate in cruise, crossing 00:00Z."""
    b = TapeBuilder("normal_flight", sim_start_utc=datetime(2026, 11, 24, 23, 35, tzinfo=timezone.utc),
                    description="YSSY-YWLM: pause, Active Pause, 4x sim rate, UTC midnight crossing.")
    target = (AIRPORTS["YWLM"]["lat"], AIRPORTS["YWLM"]["lon"])
    _depart(b).takeoff(160).climb_to(9000, target=target).cruise(60, target=target)
    b.pause(45).cruise(30, target=target).pause(60, active=True)
    b.set_rate(4.0).cruise(60, target=target).set_rate(1.0)
    b.cruise_to("YWLM", descent_nm=28).descend_and_land("YWLM", touchdown_fpm=-165).rollout()
    b.taxi(240, 12, toward="YWLM").shutdown().parked(10)
    b.expect = {"outcome": "complete", "takeoffs": 1, "landings": 1, "bounces": 0, "pauses": 2,
                "crosses_utc_midnight": True, "departure": "YSSY", "arrival": "YWLM", "max_rate": 4.0,
                "touchdown_fpm": -165}
    return b.build()


def bounced_landing() -> FlightTape:
    """A YSSY circuit with a bounce on landing."""
    b = TapeBuilder("bounced_landing", description="YSSY circuit with one bounce on landing.")
    yssy = (AIRPORTS["YSSY"]["lat"], AIRPORTS["YSSY"]["lon"])
    _depart(b).takeoff(160).climb_to(1500, speed_kt=180)
    b.cruise(150, speed_kt=180).state["hdg"] = 340.0
    b.cruise(120, speed_kt=180, target=yssy)
    b.descend_and_land("YSSY", touchdown_fpm=-420, bounce=True, approach_kt=130).rollout()
    b.taxi(240, 12, toward="YSSY").shutdown()
    b.expect = {"outcome": "complete", "takeoffs": 1, "landings": 1, "bounces": 1, "departure": "YSSY",
                "arrival": "YSSY"}
    return b.build()


def disconnect_midflight() -> FlightTape:
    """Connection lost for 90 s in cruise, then a new session; the flight continues."""
    b = TapeBuilder("disconnect_midflight", description="90 s disconnect in cruise, reconnect, normal landing.")
    target = (AIRPORTS["YWLM"]["lat"], AIRPORTS["YWLM"]["lon"])
    _depart(b).takeoff(160).climb_to(7000, target=target).cruise(60, target=target)
    b.disconnect("MSFS stopped responding.").offline(90).connect("Reconnected to the simulator.")
    b.cruise_to("YWLM", descent_nm=25).descend_and_land("YWLM", touchdown_fpm=-200).rollout()
    b.taxi(240, 12, toward="YWLM").shutdown()
    b.expect = {"outcome": "complete", "gaps_at_least": 1, "sessions": 2, "departure": "YSSY", "arrival": "YWLM"}
    return b.build()


def crash_in_cruise() -> FlightTape:
    """The aircraft crashes in flight; the flight ends abandoned."""
    b = TapeBuilder("crash_in_cruise", description="Crash in the climb, then crash reset and a reload.")
    _depart(b).takeoff(160).climb_to(3000).cruise(30)
    b.event("sim.aircraft_crashed", 0).advance(5).event("sim.crash_reset", 0)
    b.state.update(gs=0.0, ias=0.0, tas=0.0, vs=0.0)
    b.event("sim.flight_loaded", "flights\\\\other\\\\MainMenu.FLT").event("sim.stopped", 0)
    b.expect = {"outcome": "abandoned", "crashed": True}
    return b.build()


def queue_gap_on_approach() -> FlightTape:
    """Core reports lost updates on final approach; the landing is still recorded."""
    b = TapeBuilder("queue_gap_on_approach", description="Core queue overflow on short final.")
    yssy = (AIRPORTS["YSSY"]["lat"], AIRPORTS["YSSY"]["lon"])
    _depart(b).takeoff(160).climb_to(2000, speed_kt=200)
    b.cruise(120, speed_kt=200)
    b.state["hdg"] = 340.0
    b.cruise(100, speed_kt=180, target=yssy)
    b.gap(37, "Consumer update queue lost data.")
    b.descend_and_land("YSSY", touchdown_fpm=-240).rollout().taxi(240, 12, toward="YSSY").shutdown()
    b.expect = {"outcome": "complete", "gaps_at_least": 1}
    return b.build()


def parked_only() -> FlightTape:
    """Engines started and stopped at the stand; nothing should reach the Logbook."""
    b = TapeBuilder("parked_only", description="Engines run at the stand; the aircraft never moves.")
    b.connect().parked(30).start_engines().parked(60)
    b.state["engines"] = [False, False]
    b.parked(30).event("sim.stopped", 0)
    b.expect = {"logbook_records": 0}
    return b.build()


SCENARIOS: dict[str, Callable[[], FlightTape]] = {
    "normal_flight": normal_flight,
    "bounced_landing": bounced_landing,
    "disconnect_midflight": disconnect_midflight,
    "crash_in_cruise": crash_in_cruise,
    "queue_gap_on_approach": queue_gap_on_approach,
    "parked_only": parked_only,
}


def generate(directory: Path | str) -> list[Path]:
    """Write every scenario as ``<name>.tape.jsonl`` and return the paths."""
    directory = Path(directory)
    return [factory().write(directory / f"{name}.tape.jsonl") for name, factory in SCENARIOS.items()]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "generate":
        for path in generate(argv[1]):
            print(path)
        return 0
    if len(argv) == 2 and argv[0] == "info":
        tape = FlightTape.read(argv[1])
        counts: dict[str, int] = {}
        for item in tape.items:
            counts[item.type] = counts.get(item.type, 0) + 1
        print(f"{tape.header.name}: {tape.duration_s:.0f} s, {counts}, issues: {len(tape.issues)}")
        return 0
    print("Usage: python -m bbc_recorder.tape generate DIR | info FILE", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AIRPORTS", "CoreSession", "FakeConsumerAPI", "FlightTape", "RecordingConsumerAPI", "SCENARIOS", "TAPE_FORMAT",
    "TAPE_VERSION", "TapeBuilder", "TapeClock", "TapeError", "TapeHeader", "TapeItem", "TapeReplayer",
    "TapeWriter", "absolute_time_s", "convert_unit", "core_sessions", "generate", "tape_fields",
]
