"""Offline acceptance harness: replay timed flight tapes through SimulatedProvider and Core.

These tapes give a future Recorder the stream it will see live: one coherent snapshot per
second with simulator clocks, pause flags, rate changes, provider gaps and a disconnect. The
assertions check what Core promises the Recorder, not Recorder rules themselves.
"""

from datetime import datetime, timezone
from unittest import TestCase

from bbc_core import Connection, ConnectionState, Core, Event, Gap, SimVar, ValueType
from bbc_core.contract import Snapshot
from bbc_core.simulated import FlightTape, SimulatedProvider, TapeAction

from test_core_support import FakeClock, drive

ON_GROUND = SimVar("SIM ON GROUND", "bool", ValueType.BOOLEAN)
ALTITUDE = SimVar("PLANE ALT ABOVE GROUND", "feet", ValueType.NUMBER)
VS = SimVar("VERTICAL SPEED", "feet per minute", ValueType.NUMBER)
TOUCHDOWN = SimVar("PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", ValueType.NUMBER)
ENGINE = SimVar("GENERAL ENG COMBUSTION", "bool", ValueType.BOOLEAN, index=1)
PARKING = SimVar("BRAKE PARKING POSITION", "bool", ValueType.BOOLEAN)
FIELDS = (ON_GROUND, ALTITUDE, VS, TOUCHDOWN, ENGINE, PARKING)

# Two minutes before 00:00Z on 25 November 2026, as ABSOLUTE TIME seconds since 0001-01-01.
BEFORE_MIDNIGHT = (datetime(2026, 11, 24, 23, 58, tzinfo=timezone.utc)
                   - datetime(1, 1, 1, tzinfo=timezone.utc)).total_seconds()

CIRCUIT = FlightTape([
    (0, ON_GROUND, True), (0, ALTITUDE, 0.0), (0, VS, 0.0), (0, TOUCHDOWN, 0.0), (0, ENGINE, False),
    (0, PARKING, True),
    (5, ENGINE, True), (10, PARKING, False),
    (30, ON_GROUND, False), (30, VS, 700.0), (31, ALTITUDE, 20.0), (40, ALTITUDE, 300.0),
    (45, TapeAction.PAUSE, 4), (55, TapeAction.PAUSE, 0),                        # Active Pause 10 s
    (60, TapeAction.RATE, 4), (70, TapeAction.RATE, 1),                         # 4x for 10 s
    (75, VS, -400.0), (80, ALTITUDE, 15.0),
    (82, ON_GROUND, True), (82, ALTITUDE, 0.0), (82, TOUCHDOWN, -6.5), (82, VS, -390.0),  # touchdown
    (83, ON_GROUND, False), (83, ALTITUDE, 2.0),                                # bounce
    (85, ON_GROUND, True), (85, ALTITUDE, 0.0), (85, TOUCHDOWN, -2.1), (85, VS, 0.0),
    (95, TapeAction.GAP, 4),
    (110, PARKING, True), (115, ENGINE, False),
])


class FlightTapeTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, wall_clock=self.clock.wall,
                                          absolute_time_s=BEFORE_MIDNIGHT, jitter_ms=30, seed=2026)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.recorder = self.core.register_consumer(queue_size=4096)
        self.snapshot = self.recorder.subscribe_snapshot(FIELDS)
        self.pause = self.recorder.subscribe("sim.pause_state")

    def replay(self, tape, seconds):
        self.core.connect()
        self.provider.play(tape)
        (updates,) = drive(self.core, self.clock, seconds, consumers=(self.recorder,))
        return updates

    def test_normal_circuit_with_pause_rate_bounce_gap_and_midnight(self):
        updates = self.replay(CIRCUIT, 120)
        snapshots = [u for u in updates if isinstance(u, Snapshot)]
        self.assertTrue(self.provider.tape_finished)
        # One coherent sample per host second, none lost or merged.
        self.assertIn(len(snapshots), range(119, 122))
        gaps = [(i, u) for i, u in enumerate(updates) if type(u) is Gap]
        self.assertEqual([(g.dropped_updates, g.reason) for _, g in gaps], [(4, "Provider update queue lost data.")])
        # Air/ground transitions arrive in order and carry the same-frame touchdown velocity.
        transitions = []
        for previous, current in zip(snapshots, snapshots[1:]):
            if previous.values[0] != current.values[0]:
                transitions.append((current.values[0], current.values[3]))
        self.assertEqual(transitions, [(False, 0.0), (True, -6.5), (False, -6.5), (True, -2.1)])
        # Simulation time stops in Active Pause and runs four times faster at 4x.
        def sim_time_at(host_offset):
            started = FakeClock().wall()
            return min(snapshots, key=lambda s: abs((s.received_at - started).total_seconds()
                                                    - host_offset)).simulation_time_s
        self.assertAlmostEqual(sim_time_at(54) - sim_time_at(46), 0.0, delta=0.2)
        self.assertAlmostEqual(sim_time_at(69) - sim_time_at(61), 32.0, delta=4.5)
        # Simulator UTC crosses midnight and the date follows it.
        dates = {s.sample_time.date() for s in snapshots}
        self.assertEqual(dates, {datetime(2026, 11, 24).date(), datetime(2026, 11, 25).date()})
        self.assertEqual([int(u.value) for u in updates if isinstance(u, Event)], [4, 0])

    def test_disconnect_keeps_samples_received_before_it(self):
        tape = FlightTape([(0, ON_GROUND, True), (8, ON_GROUND, False), (10, TapeAction.DISCONNECT, "MSFS quit.")])
        for variable in FIELDS[1:]:
            self.provider.set_value(variable, 0)
        updates = self.replay(tape, 15)
        kinds = [type(u).__name__ for u in updates]
        disconnected = next(i for i, u in enumerate(updates)
                            if isinstance(u, Connection) and u.state is ConnectionState.DISCONNECTED)
        before = [u for u in updates[:disconnected] if isinstance(u, Snapshot)]
        self.assertIn(len(before), range(9, 12))
        self.assertIs(before[-1].values[0], False)
        self.assertFalse(any(isinstance(u, Snapshot) for u in updates[disconnected:]))
        self.assertEqual(updates[disconnected].detail, "MSFS quit.")
        self.assertNotIn("Gap", kinds)
        self.assertIsNone(self.recorder.latest_snapshot(self.snapshot.id))

    def test_tapes_load_from_json_lines(self):
        tape = FlightTape.from_jsonl([
            '# A short taxi.',
            '{"t": 0, "id": "aircraft.on_ground", "value": true}',
            '{"t": 1.5, "simvar": ["VERTICAL SPEED", "feet per minute", "number"], "value": 120}',
            '{"t": 2, "simvar": ["GEAR IS ON GROUND", "bool", "boolean", 0], "value": 1}',
            '{"t": 3, "action": "pause", "value": 4}',
            '{"t": 4, "action": "disconnect"}',
        ])
        self.assertEqual(len(tape.entries), 5)
        self.assertEqual(tape.duration_s, 4)
        self.assertEqual(tape.entries[2].target.sdk_name, "GEAR IS ON GROUND:0")
        self.assertIs(tape.entries[3].target, TapeAction.PAUSE)
        with self.assertRaisesRegex(ValueError, "line 1"):
            FlightTape.from_jsonl(['{"t": 0, "id": "aircraft.unknown", "value": 1}'])
