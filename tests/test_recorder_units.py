"""Unit tests for bbc_recorder adapters, rules, hints and boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from bbc_recorder import (
    BlockOnPolicy, EventKind, FlightRecorder, FlightSink, Lifecycle, LifecycleKind, Phase, RECORDER_FIELDS,
    RULES_VERSION, RecorderRules, Sample, build_flight_record, haversine_nm, lifecycle_from_update,
    sample_from_snapshot,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]

# The guarded offline runner (tests/test_repo_offline_guard.py) blocks subprocesses and sets
# BBC_OFFLINE_GUARD=1; these cross-process checks then skip and still run under plain unittest.
_GUARDED = os.environ.get("BBC_OFFLINE_GUARD") == "1"
_SKIP_REASON = "starts a subprocess, which the offline guard blocks"


# --- fakes shaped like bbc_core.contract types (duck typing; no Core import) ----------------
@dataclass(frozen=True)
class FakeSource:
    name: str = "simulated"
    simulated: bool = True


@dataclass(frozen=True)
class FakeSnapshot:
    subscription_id: str
    names: tuple
    values: tuple
    simulation_time_s: float | None
    absolute_time_s: float | None
    simulation_rate: float | None
    source: FakeSource
    session_id: str
    received_at: datetime
    sample_time: datetime | None = None


@dataclass(frozen=True)
class FakeEvent:
    capability_id: str
    value: object
    source: FakeSource
    session_id: str
    received_at: datetime
    sample_time: datetime | None = None


class FakeState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    ERROR = "error"
    CONNECTING = "connecting"


@dataclass(frozen=True)
class FakeConnection:
    state: FakeState
    source: FakeSource
    session_id: str | None
    occurred_at: datetime
    detail: str = ""


@dataclass(frozen=True)
class FakeGap:
    dropped_updates: int
    reason: str


def snap(t: float, **fields) -> FakeSnapshot:
    names = tuple(fields)
    return FakeSnapshot("sub1", names, tuple(fields.values()), 100.0 + t, 63_900_000_000.0 + t, 1.0,
                        FakeSource(), "S1", T0 + timedelta(seconds=t))


def ground(t: float, **kw) -> Sample:
    base = dict(host_time=T0 + timedelta(seconds=t), sim_time_s=1000.0 + t, sim_rate=1.0, on_ground=True,
                ground_speed_kt=0.0, lat=50.0, lon=8.0, parking_brake=True, engines_running=(False,))
    base.update(kw)
    return Sample(**base)


class AdapterTest(unittest.TestCase):
    def test_sample_from_snapshot_maps_fields(self) -> None:
        s = sample_from_snapshot(snap(5, **{
            "SIM ON GROUND": 1, "ground velocity": 12.5, "AIRSPEED INDICATED": 14.0, "VERTICAL SPEED": -3.0,
            "PLANE ALT ABOVE GROUND": 0.0, "PLANE ALTITUDE": 361.0, "PLANE LATITUDE": 50.03,
            "PLANE LONGITUDE": 8.57, "PLANE HEADING DEGREES TRUE": 250.0, "BRAKE PARKING POSITION": 0,
            "GENERAL ENG COMBUSTION:1": 1, "GENERAL ENG COMBUSTION:2": 0,
            "PLANE TOUCHDOWN NORMAL VELOCITY": 3.5, "IS SLEW ACTIVE": 0, "TITLE": "Airbus A320neo",
            "UNRELATED": 9}), high_rate=True)
        self.assertTrue(s.on_ground)
        self.assertEqual(s.ground_speed_kt, 12.5)
        self.assertFalse(s.parking_brake)
        self.assertEqual(s.engines_running, (True, False))
        self.assertEqual(s.touchdown_velocity_fpm, 210.0)  # 3.5 ft/s in the catalog unit, as ft/min
        self.assertEqual(s.aircraft, "Airbus A320neo")
        self.assertEqual(s.sim_time_s, 105.0)
        self.assertEqual(s.sim_rate, 1.0)
        self.assertTrue(s.simulated)
        self.assertTrue(s.high_rate)
        self.assertEqual(s.session_id, "S1")
        self.assertIsNotNone(s.sim_utc.tzinfo)

    def test_unknown_values_stay_unknown(self) -> None:
        s = sample_from_snapshot(snap(1, **{"SIM ON GROUND": None, "GROUND VELOCITY": float("nan")}))
        self.assertIsNone(s.on_ground)
        self.assertIsNone(s.ground_speed_kt)
        self.assertIsNone(s.engines_running)
        self.assertIsNone(s.lat)

    def test_indexed_name_variants_and_custom_mapping(self) -> None:
        s = sample_from_snapshot(snap(1, **{"GENERAL ENG COMBUSTION 1": 0, "GENERAL ENG COMBUSTION[2]": 0,
                                            "MY GEAR": 1}), mapping={"MY GEAR": "on_ground"})
        self.assertEqual(s.engines_running, (False, False))
        self.assertTrue(s.on_ground)

    def test_lifecycle_from_core_updates(self) -> None:
        src = FakeSource()
        pause = lifecycle_from_update(FakeEvent("sim.pause_state", 4, src, "S1", T0))
        self.assertEqual((pause.kind, pause.value), (LifecycleKind.PAUSE_FLAGS, 4))
        for cap, kind in (("sim.aircraft_crashed", LifecycleKind.CRASHED), ("sim.crashed", LifecycleKind.CRASHED),
                          ("sim.crash_reset", LifecycleKind.CRASH_RESET),
                          ("sim.flight_loaded", LifecycleKind.FLIGHT_LOADED),
                          ("sim.position_changed", LifecycleKind.POSITION_CHANGED),
                          ("sim.stopped", LifecycleKind.SIM_STOPPED)):
            self.assertEqual(lifecycle_from_update(FakeEvent(cap, 1, src, "S1", T0)).kind, kind)
        loaded = lifecycle_from_update(FakeEvent("sim.aircraft_loaded", "a320.cfg", src, "S1", T0))
        self.assertEqual((loaded.kind, loaded.value), (LifecycleKind.AIRCRAFT_LOADED, "a320.cfg"))
        self.assertIsNone(lifecycle_from_update(FakeEvent("sim.frame", 1, src, "S1", T0)))
        lost = lifecycle_from_update(FakeConnection(FakeState.ERROR, src, None, T0, "pipe broken"))
        self.assertEqual((lost.kind, lost.detail), (LifecycleKind.DISCONNECTED, "pipe broken"))
        self.assertEqual(lifecycle_from_update(FakeConnection(FakeState.CONNECTED, src, "S2", T0)).kind,
                         LifecycleKind.CONNECTED)
        self.assertIsNone(lifecycle_from_update(FakeConnection(FakeState.CONNECTING, src, None, T0)))
        gap = lifecycle_from_update(FakeGap(7, "consumer queue overflow"), now=lambda: T0)
        self.assertEqual((gap.kind, gap.value, gap.detail, gap.host_time), (LifecycleKind.GAP, 7,
                                                                            "consumer queue overflow", T0))
        self.assertIsNone(lifecycle_from_update(object()))

    def test_process_accepts_core_shapes(self) -> None:
        rec = FlightRecorder(id_factory=lambda: "F")
        out = rec.process(snap(0, **{"SIM ON GROUND": 1, "GROUND VELOCITY": 0.0, "PLANE LATITUDE": 50.0,
                                     "PLANE LONGITUDE": 8.0}))
        self.assertEqual(out[0].kind, EventKind.FLIGHT_STARTED)
        out = rec.process(FakeEvent("sim.pause_state", 1, FakeSource(), "S1", T0 + timedelta(seconds=1)))
        self.assertEqual(out[0].kind, EventKind.PAUSED)
        out = rec.process(FakeConnection(FakeState.DISCONNECTED, FakeSource(), None, T0 + timedelta(seconds=2)))
        self.assertEqual(out[0].kind, EventKind.GAP)
        self.assertEqual(rec.process("noise"), [])
        closed = rec.process_all([ground(3)])  # first sample after reconnection closes the gap
        self.assertEqual([(e.kind, e.data["status"]) for e in closed], [(EventKind.GAP, "ended")])
        self.assertEqual(closed[0].data["note"], "simulation time jumped forward")


class RulesTest(unittest.TestCase):
    def test_round_trip_and_unknown_keys(self) -> None:
        rules = RecorderRules(block_on_policy=BlockOnPolicy.PARKING_BRAKE, bounce_max_s=3.0)
        payload = json.loads(json.dumps(rules.to_json()))
        self.assertEqual(payload["version"], RULES_VERSION)
        payload["values"]["future_threshold"] = 1
        self.assertEqual(RecorderRules.from_json(payload), rules)
        self.assertEqual(rules.with_changes(bounce_max_s=4.0).bounce_max_s, 4.0)

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            RecorderRules(gap_after_s=-1)
        with self.assertRaises(ValueError):
            RecorderRules(high_rate_below_agl_ft=2000, low_rate_above_agl_ft=1000)
        with self.assertRaises(ValueError):
            RecorderRules(high_rate_every=0)
        with self.assertRaises(ValueError):
            RecorderRules(rejected_takeoff_speed_kt=50, takeoff_roll_speed_kt=40)

    def test_field_table(self) -> None:
        attrs = {row[4] for row in RECORDER_FIELDS}
        for needed in ("on_ground", "ground_speed_kt", "vs_fpm", "touchdown_velocity_fpm", "parking_brake",
                       "engine_1", "lat", "lon"):
            self.assertIn(needed, attrs)

    def test_snapshot_simvars_builds_core_simvars(self) -> None:
        try:
            from bbc_recorder import snapshot_simvars
            simvars = snapshot_simvars()
        except ImportError as exc:  # Core is being edited concurrently
            self.skipTest(f"bbc_core not importable: {exc}")
        self.assertEqual(len(simvars), len(RECORDER_FIELDS))
        self.assertEqual(simvars[-1].index, 4)


class BlockOnPolicyTest(unittest.TestCase):
    def taxi_in(self, rules: RecorderRules) -> FlightRecorder:
        rec = FlightRecorder(rules, id_factory=lambda: "F")
        rec.feed(ground(0, on_ground=False, alt_agl_ft=2000.0, ground_speed_kt=150.0,
                        engines_running=(True,), parking_brake=False))
        rec.feed(ground(1, ground_speed_kt=120.0, engines_running=(True,), parking_brake=False,
                        touchdown_velocity_fpm=150.0))
        for t in range(2, 8):
            rec.feed(ground(t, ground_speed_kt=10.0, engines_running=(True,), parking_brake=False))
        self.assertEqual(rec.phase, Phase.TAXI_IN)
        return rec

    def test_parking_brake_policy(self) -> None:
        rec = self.taxi_in(RecorderRules(block_on_policy=BlockOnPolicy.PARKING_BRAKE))
        for t in range(10, 17):
            rec.feed(ground(t, engines_running=(True,), parking_brake=True))
        self.assertEqual(rec.completed_flights[-1].block_on["basis"], "parking brake set")
        self.assertAlmostEqual(rec.completed_flights[-1].block_on["sim_time_s"], 1010.0)

    def test_engines_off_policy_falls_back_to_long_brake_when_engines_unknown(self) -> None:
        rec = self.taxi_in(RecorderRules())
        for t in range(10, 30):
            rec.feed(ground(t, engines_running=None, parking_brake=True))
        self.assertEqual(rec.completed_flights, ())
        for t in range(30, 45):
            rec.feed(ground(t, engines_running=None, parking_brake=True))
        self.assertEqual(rec.completed_flights[-1].block_on["basis"], "parking brake set")

    def test_engines_and_brake_policy(self) -> None:
        rec = self.taxi_in(RecorderRules(block_on_policy=BlockOnPolicy.ENGINES_OFF_AND_BRAKE))
        for t in range(10, 30):
            rec.feed(ground(t, engines_running=(False,), parking_brake=False))
        self.assertEqual(rec.completed_flights, ())
        for t in range(30, 37):
            rec.feed(ground(t, engines_running=(False,), parking_brake=True))
        self.assertEqual(rec.completed_flights[-1].block_on["basis"], "engines off and parking brake set")


class HintAndTimingTest(unittest.TestCase):
    def test_hint_hysteresis(self) -> None:
        rec = FlightRecorder(id_factory=lambda: "F")
        t = 0

        def air(agl: float) -> bool:
            nonlocal t
            t += 1
            rec.feed(Sample(T0 + timedelta(seconds=t), 1000.0 + t, sim_rate=1.0, on_ground=False,
                            ground_speed_kt=150.0, alt_agl_ft=agl))
            return rec.sampling_hint.high_rate

        self.assertFalse(air(3000))
        self.assertFalse(air(1200))   # inside the band, stays low
        self.assertTrue(air(900))
        self.assertTrue(air(1200))    # inside the band, stays high
        self.assertFalse(air(1600))
        self.assertEqual(rec.sampling_hint.period, "second")
        air(500)
        self.assertEqual(rec.sampling_hint.period, "sim_frame")
        self.assertEqual(rec.sampling_hint.every, 1)

    def test_debounce_uses_sim_time(self) -> None:
        # at 4x rate the 5 s block-off confirmation needs only 1.25 host seconds
        rec = FlightRecorder(id_factory=lambda: "F")
        events = []
        for i in range(6):
            events += rec.feed(Sample(T0 + timedelta(seconds=0.5 * i), 1000.0 + 2.0 * i, sim_rate=4.0,
                                      on_ground=True, ground_speed_kt=5.0 if i else 0.0, lat=50.0,
                                      lon=8.0 + i * 1e-5, parking_brake=False))
        kinds = [e.kind for e in events]
        self.assertIn(EventKind.BLOCK_OFF, kinds)

    def test_sim_time_jump_forward_is_not_credited(self) -> None:
        rec = FlightRecorder(id_factory=lambda: "F")
        rec.feed(ground(0, on_ground=False, alt_agl_ft=3000.0))
        rec.feed(ground(1, on_ground=False, alt_agl_ft=3000.0))
        rec.feed(ground(2, sim_time_s=99999.0, on_ground=False, alt_agl_ft=3000.0))
        s = rec.summary()
        self.assertAlmostEqual(s.airborne_s, 1.0)
        self.assertTrue(any("discontinuity" in w for w in s.warnings))

    def test_host_clock_backwards_ignored(self) -> None:
        rec = FlightRecorder(id_factory=lambda: "F")
        rec.feed(ground(10))
        rec.feed(ground(5, sim_time_s=None))
        self.assertTrue(any("Host clock went backwards" in w for w in rec.summary().warnings))

    def test_unknown_ground_state_does_not_arm(self) -> None:
        rec = FlightRecorder(id_factory=lambda: "F")
        self.assertEqual(rec.feed(ground(0, on_ground=None)), [])
        self.assertFalse(rec.recording)
        self.assertIsNone(rec.summary())

    def test_lifecycle_without_flight_is_quiet(self) -> None:
        rec = FlightRecorder(id_factory=lambda: "F")
        for kind in (LifecycleKind.DISCONNECTED, LifecycleKind.GAP, LifecycleKind.SIM_STOPPED,
                     LifecycleKind.PAUSE_FLAGS, LifecycleKind.POSITION_CHANGED):
            self.assertEqual(rec.on_lifecycle(Lifecycle(kind, T0, 1)), [])


class RecordAndBoundaryTest(unittest.TestCase):
    def test_build_flight_record_and_sink_protocol(self) -> None:
        rec = FlightRecorder(id_factory=lambda: "F")
        rec.feed(ground(0, on_ground=False, alt_agl_ft=3000.0))
        rec.manual_stop(T0 + timedelta(seconds=5))
        record = build_flight_record(rec.completed_flights[-1], rec.rules, journal="recorder/archive/F.jsonl")
        self.assertEqual(record["source"], "recorder")
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["recorder"]["journal"], "recorder/archive/F.jsonl")
        json.dumps(record)

        class Sink:
            def __init__(self) -> None:
                self.saved = []

            def save_recorded_flight(self, record: dict) -> str:
                self.saved.append(record)
                return "id"

        self.assertIsInstance(Sink(), FlightSink)

    def test_haversine(self) -> None:
        self.assertAlmostEqual(haversine_nm(0, 0, 1, 0), 60.04, delta=0.05)
        self.assertEqual(haversine_nm(10, 10, 10, 10), 0.0)

    @unittest.skipIf(_GUARDED, _SKIP_REASON)
    def test_recorder_is_qt_and_simconnect_free(self) -> None:
        code = ("import sys, bbc_recorder; "
                "bad=[m for m in sys.modules if m.startswith(('PySide6','bbc_core','bbc_efb','ctypes.wintypes'))]; "
                "print(bad); sys.exit(1 if bad else 0)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
