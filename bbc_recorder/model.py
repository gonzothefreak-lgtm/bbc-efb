"""Shared Flight Recorder vocabulary. The Recorder has no Qt or SimConnect dependency.

Core delivers observations; the Recorder owns flight state, events and journals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class _ValueEnum(str, Enum):
    def __str__(self) -> str:  # format as the value on every supported Python
        return self.value

    __format__ = str.__format__


class Phase(_ValueEnum):
    IDLE = "idle"                # no flight being recorded
    PREFLIGHT = "preflight"      # aircraft loaded, parked, engines may run
    TAXI_OUT = "taxi_out"
    TAKEOFF_ROLL = "takeoff_roll"
    AIRBORNE = "airborne"
    LANDING_ROLL = "landing_roll"
    TAXI_IN = "taxi_in"
    COMPLETE = "complete"        # flight closed normally
    ABANDONED = "abandoned"      # closed without a normal arrival (quit, crash, reset)


class EventKind(_ValueEnum):
    FLIGHT_STARTED = "flight_started"
    PHASE_CHANGED = "phase_changed"
    BLOCK_OFF = "block_off"
    TAKEOFF = "takeoff"
    TOUCHDOWN = "touchdown"
    BOUNCE = "bounce"
    BLOCK_ON = "block_on"
    PAUSED = "paused"
    RESUMED = "resumed"
    SIM_RATE_CHANGED = "sim_rate_changed"
    GAP = "gap"                  # data missing: disconnect, provider gap, stale feed
    AIRCRAFT_CHANGED = "aircraft_changed"
    POSITION_JUMP = "position_jump"  # slew, teleport, flight reload
    CRASHED = "crashed"
    MANUAL_START = "manual_start"
    MANUAL_STOP = "manual_stop"
    FLIGHT_ENDED = "flight_ended"
    SAMPLE = "sample"            # periodic track point (the service journals the Sample it fed)
    ATTRIBUTION = "attribution"  # service: departure/arrival airport or aircraft identity evidence
    NOTE = "note"                # service: a fact worth keeping that is not a flight event
                                 # (provider signal error, sampling switch, resubscription)


@dataclass(frozen=True, slots=True)
class RecorderEvent:
    kind: EventKind
    phase: Phase
    host_time: datetime                    # aware UTC host clock at receipt
    sim_time_s: float | None = None        # SIMULATION TIME, None when unknown
    sim_utc: datetime | None = None        # simulator UTC (from ABSOLUTE TIME), None when unknown
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "phase": self.phase.value,
            "host_time": self.host_time.isoformat(),
            "sim_time_s": self.sim_time_s,
            "sim_utc": self.sim_utc.isoformat() if self.sim_utc else None,
            "data": self.data,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "RecorderEvent":
        return cls(
            EventKind(payload["kind"]), Phase(payload["phase"]),
            datetime.fromisoformat(payload["host_time"]), payload.get("sim_time_s"),
            datetime.fromisoformat(payload["sim_utc"]) if payload.get("sim_utc") else None,
            dict(payload.get("data") or {}),
        )
