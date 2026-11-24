"""Configurable Flight Recorder rules: thresholds, debounce windows and owner-decision switches.

Every value here is a *default proposal* (see ``bbc_recorder/RULES.md``). The owner has not yet
approved the Recorder semantics, so each open decision is a switch rather than hard-coded logic.
A flight record should embed ``RecorderRules.to_json()`` and ``RULES_VERSION`` so a later
reader can tell which rules produced the derived values.

This module has no Qt, SimConnect or Core dependency. ``snapshot_simvars()`` imports
``bbc_core.sdk_simvar`` lazily, only when the caller asks for SimVar objects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from enum import Enum
from typing import Any

RULES_VERSION = "bbc-recorder-rules/1"


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value

    __format__ = str.__format__


class BlockOnPolicy(_ValueEnum):
    """What must be true, while stationary on the ground, to call it block-on."""

    ENGINES_OFF = "engines_off"                      # all known engines stopped
    PARKING_BRAKE = "parking_brake"                  # parking brake set
    ENGINES_OFF_OR_BRAKE = "engines_off_or_brake"    # either of the above
    ENGINES_OFF_AND_BRAKE = "engines_off_and_brake"  # both


class PausePolicy(_ValueEnum):
    """Which pause flags stop the credited block and airborne clocks."""

    ALL = "all"                      # full pause, Active Pause and sim pause all stop the clocks
    FULL_AND_SIM_ONLY = "full_and_sim_only"  # Active Pause time is credited (aircraft is frozen though)


@dataclass(frozen=True, slots=True)
class RecorderRules:
    """Thresholds for the Recorder state machine. Times are simulator seconds unless noted.

    "Simulator seconds" means the Recorder timeline: SIMULATION TIME deltas when the sample
    carries them, otherwise host-time deltas scaled by the reported simulation rate (labelled
    as host fallback). Pauses never advance this timeline.
    """

    # --- automatic start / arming --------------------------------------------------------
    auto_start: bool = True               # arm a flight automatically on the first usable sample
    start_in_air: bool = True             # also arm when the first sample is already airborne
    auto_rearm_after_end: bool = True     # arm the next flight after a normal block-on

    # --- block-off (PREFLIGHT -> TAXI_OUT) ------------------------------------------------
    block_off_speed_kt: float = 3.0
    block_off_confirm_s: float = 5.0
    block_off_requires_brake_release: bool = True  # when parking-brake data exists

    # --- takeoff roll and takeoff --------------------------------------------------------
    takeoff_roll_speed_kt: float = 40.0
    takeoff_roll_confirm_s: float = 2.0
    takeoff_confirm_s: float = 2.0         # continuous airborne time to confirm a takeoff
    takeoff_confirm_agl_ft: float = 50.0   # or this height above ground, whichever first
    rejected_takeoff_speed_kt: float = 30.0  # back below this on the ground = rejected takeoff

    # --- touchdown, bounce, touch-and-go ---------------------------------------------------
    touchdown_simvar_wait_s: float = 1.0   # wait this long for PLANE TOUCHDOWN NORMAL VELOCITY
    touchdown_vs_max_age_s: float = 0.25   # last airborne VS sample must be this recent
    bounce_max_s: float = 5.0              # airborne for less than this after contact = bounce
    touch_and_go_agl_ft: float = 100.0     # higher than this after contact = touch-and-go
    landing_roll_end_speed_kt: float = 30.0
    landing_roll_end_confirm_s: float = 2.0

    # --- go-around / low approach (no ground contact) -------------------------------------
    go_around_max_agl_ft: float = 500.0    # descended below this height above ground
    go_around_climb_ft: float = 300.0      # then climbed at least this much without contact

    # --- block-on (TAXI_IN / TAXI_OUT -> COMPLETE) ----------------------------------------
    block_on_speed_kt: float = 1.0
    block_on_confirm_s: float = 5.0
    block_on_policy: BlockOnPolicy = BlockOnPolicy.ENGINES_OFF
    block_on_brake_only_confirm_s: float = 30.0  # longer wait when only the brake is evidence
    complete_after_taxi_out_return: bool = True  # block-on before takeoff still closes the flight

    # --- clocks, pause, sim rate ----------------------------------------------------------
    pause_policy: PausePolicy = PausePolicy.ALL
    sim_rate_change_epsilon: float = 0.01
    max_sim_time_lead: float = 1.5         # sim dt may exceed host dt x rate by this factor
    max_sim_time_slack_s: float = 2.0      # ...plus this many seconds, before it is distrusted

    # --- gaps ------------------------------------------------------------------------------
    gap_after_s: float = 5.0               # host seconds without a sample (unpaused) = gap
    credit_gap_max_s: float = 60.0         # credit continuous sim time across gaps up to this
    bridge_distance_across_gaps: bool = True

    # --- position jumps / slew --------------------------------------------------------------
    jump_min_nm: float = 0.2
    jump_speed_factor: float = 1.5
    jump_speed_margin_kt: float = 150.0
    reported_jump_min_nm: float = 0.01     # after a PositionChanged event

    # --- adaptive sampling ------------------------------------------------------------------
    high_rate_below_agl_ft: float = 1000.0
    low_rate_above_agl_ft: float = 1500.0
    high_rate_ground_speed_kt: float = 30.0
    high_rate_every: int = 1               # snapshot every N simulator frames when raised

    # --- lifecycle --------------------------------------------------------------------------
    close_on_sim_stop: bool = True
    close_on_flight_loaded: bool = True

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or isinstance(value, Enum):
                continue
            if isinstance(value, (int, float)) and value < 0:
                raise ValueError(f"Recorder rule {item.name} cannot be negative.")
        if self.low_rate_above_agl_ft < self.high_rate_below_agl_ft:
            raise ValueError("The low-rate height must be at or above the high-rate height (hysteresis).")
        if self.high_rate_every < 1:
            raise ValueError("high_rate_every must be at least 1.")
        if self.rejected_takeoff_speed_kt > self.takeoff_roll_speed_kt:
            raise ValueError("The rejected-takeoff speed must not exceed the takeoff-roll speed.")

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in payload.items():
            if isinstance(value, Enum):
                payload[key] = value.value
        return {"version": RULES_VERSION, "values": payload}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "RecorderRules":
        values = dict(payload.get("values", payload))
        known = {item.name: item for item in fields(cls)}
        clean: dict[str, Any] = {}
        for key, value in values.items():
            if key not in known:
                continue  # rules written by a newer version: ignore unknown thresholds
            if key == "block_on_policy":
                value = BlockOnPolicy(value)
            elif key == "pause_policy":
                value = PausePolicy(value)
            clean[key] = value
        return cls(**clean)

    def with_changes(self, **changes: Any) -> "RecorderRules":
        return replace(self, **changes)


# --- Snapshot fields -------------------------------------------------------------------------
# (SimVar name, unit, value type, index, Sample attribute). Value types are bbc_core ValueType
# values as plain strings so this module stays Core-free. Names, units and value types match
# the bbc_core catalog entries for the same SimVars (a test checks this), so the Recorder's
# snapshot asks MSFS for exactly what the catalog documents.

ENGINE_COUNT = 4

RECORDER_FIELDS: tuple[tuple[str, str | None, str, int | None, str], ...] = (
    ("SIM ON GROUND", "bool", "boolean", None, "on_ground"),
    ("GROUND VELOCITY", "knots", "number", None, "ground_speed_kt"),
    ("AIRSPEED INDICATED", "knots", "number", None, "ias_kt"),
    ("VERTICAL SPEED", "feet per minute", "number", None, "vs_fpm"),
    ("PLANE ALT ABOVE GROUND", "feet", "number", None, "alt_agl_ft"),
    ("PLANE ALTITUDE", "feet", "number", None, "alt_msl_ft"),
    ("PLANE LATITUDE", "degrees", "number", None, "lat"),
    ("PLANE LONGITUDE", "degrees", "number", None, "lon"),
    ("PLANE HEADING DEGREES TRUE", "degrees", "number", None, "heading_deg"),
    ("BRAKE PARKING POSITION", "bool", "boolean", None, "parking_brake"),
    # The catalog unit (feet per second); sample_from_snapshot converts it to feet per minute.
    ("PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", "number", None, "touchdown_velocity_fpm"),
    ("IS SLEW ACTIVE", "bool", "boolean", None, "slew_active"),
) + tuple(
    ("GENERAL ENG COMBUSTION", "bool", "boolean", n, f"engine_{n}") for n in range(1, ENGINE_COUNT + 1)
)


def snapshot_simvars() -> tuple[Any, ...]:
    """The Recorder's snapshot fields as ``bbc_core.sdk_simvar.SimVar`` objects (lazy import)."""
    from bbc_core.contract import ValueType  # noqa: PLC0415 - keep Recorder import Core-free
    from bbc_core.sdk_simvar import SimVar  # noqa: PLC0415

    return tuple(SimVar(name, unit, ValueType(kind), index) for name, unit, kind, index, _ in RECORDER_FIELDS)
