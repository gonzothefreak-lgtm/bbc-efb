"""Round-3 reconciliation of the Recorder state machine with Core, the journal and the service.

Covers the changes made so the real ``FlightRecorder`` can drive the service: event phases
stay ``Phase`` values (journals can encode them), gaps carry simulator UTC at both ends,
``interrupt_flight`` drops an open flight without inventing an end, no flight arms after
SimStop, and the snapshot fields match the Core catalog (units included).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from bbc_recorder import EventKind, LifecycleKind, Phase, RECORDER_FIELDS, sample_from_snapshot, snapshot_simvars
from bbc_recorder.journal import Journal, read_journal

try:
    from tests.test_recorder_flights import T0, Tape
except ImportError:  # run from inside tests/
    from test_recorder_flights import T0, Tape


class EventPhaseTests(unittest.TestCase):
    def test_gap_and_jump_events_keep_a_phase_enum_and_journal_cleanly(self):
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(3000)
        tape.cruise(30)
        tape.wait(20)                      # silent feed -> GAP status=detected
        tape.cruise(10)
        tape.step(slew=True)               # slew -> POSITION_JUMP
        tape.st["lat"] += 0.5
        tape.step()
        tape.step(slew=False)
        gaps, jumps = tape.of(EventKind.GAP), tape.of(EventKind.POSITION_JUMP)
        self.assertTrue(gaps and jumps)
        for event in gaps + jumps:
            self.assertIsInstance(event.phase, Phase)
            self.assertNotIn("phase", event.data)
        self.assertEqual(gaps[0].data["gap_phase"], "airborne")
        self.assertEqual(jumps[0].data["jump_phase"], "airborne")
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal.create(Path(folder), "F1", app_version="test")
            for event in tape.events:
                journal.append(event)  # raised AttributeError before the fix
            journal.close()
            result = read_journal(journal.path)
        self.assertTrue(result.clean)
        self.assertEqual(result.event_count, len(tape.events))

    def test_gaps_carry_simulator_utc_at_both_ends(self):
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(2000)
        last_utc = tape.rec._prev.sim_utc
        tape.life(LifecycleKind.DISCONNECTED, detail="MSFS stopped responding.")
        tape.wait(30)
        tape.cruise(5)
        started = [e for e in tape.of(EventKind.GAP) if e.data["status"] == "started"][0]
        ended = [e for e in tape.of(EventKind.GAP) if e.data["status"] == "ended"][0]
        self.assertEqual(started.data["started_sim_utc"], last_utc.isoformat())
        self.assertEqual(ended.data["started_sim_utc"], last_utc.isoformat())
        self.assertEqual(ended.data["ended_sim_utc"], ended.sim_utc.isoformat())
        summary_gap = tape.rec.summary().gaps[0]
        self.assertEqual(summary_gap["started_sim_utc"], last_utc.isoformat())

    def test_reported_core_gap_has_start_but_no_invented_end(self):
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.life(LifecycleKind.GAP, 12, "Consumer update queue lost data.")
        event = tape.of(EventKind.GAP)[-1]
        self.assertEqual(event.data["status"], "reported")
        self.assertIsNotNone(event.data["started_sim_utc"])
        self.assertIsNone(tape.rec.summary().gaps[-1]["ended_sim_utc"])


class InterruptTests(unittest.TestCase):
    def test_interrupt_flight_drops_the_flight_without_an_end(self):
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(1500)
        before = len(tape.events)
        out = tape.rec.interrupt_flight("Save & Exit")
        self.assertNotIn(EventKind.FLIGHT_ENDED, [e.kind for e in out])
        self.assertIsNone(tape.rec.flight_id)
        self.assertIs(tape.rec.phase, Phase.IDLE)
        summary = tape.rec.summary()
        self.assertEqual(summary.outcome, "interrupted")
        self.assertIsNone(summary.ended_host)
        self.assertIsNone(summary.block_on)
        self.assertEqual(len(tape.events), before)
        self.assertEqual(tape.rec.interrupt_flight("again"), [])

    def test_interrupt_flushes_a_pending_touchdown(self):
        tape = Tape(with_tdv=False)
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(800)
        tape.adaptive = False
        tape.land(200)
        out = tape.rec.interrupt_flight("exit")
        kinds = [e.kind for e in out]
        self.assertIn(EventKind.TOUCHDOWN, kinds)
        self.assertIn(EventKind.TOUCHDOWN, [e.kind for e in tape.events] + kinds)

    def test_next_sample_after_interrupt_arms_a_new_flight(self):
        tape = Tape()
        tape.preflight()
        first = tape.rec.flight_id
        tape.rec.interrupt_flight("long gap")
        tape.run(2)
        self.assertIsNotNone(tape.rec.flight_id)
        self.assertNotEqual(tape.rec.flight_id, first)


class SimStopHoldTests(unittest.TestCase):
    def test_no_flight_arms_after_sim_stop_until_sim_start(self):
        tape = Tape()
        tape.run(2)
        tape.life(LifecycleKind.SIM_STOPPED)
        self.assertIsNone(tape.rec.flight_id)  # the armed, empty flight was closed (discarded)
        tape.run(5)
        self.assertIsNone(tape.rec.flight_id)  # menu samples arm nothing
        tape.life(LifecycleKind.SIM_STARTED)
        tape.run(1)
        self.assertIsNotNone(tape.rec.flight_id)

    def test_manual_start_still_works_after_sim_stop(self):
        tape = Tape()
        tape.run(1)
        tape.life(LifecycleKind.SIM_STOPPED)
        tape.run(1)
        tape.events.extend(tape.rec.manual_start(tape.host, "pilot"))
        self.assertIsNotNone(tape.rec.flight_id)


class SignalErrorTests(unittest.TestCase):
    def test_signal_error_is_not_a_gap_whatever_its_base_class(self):
        from bbc_core.contract import Gap, SignalError

        from bbc_recorder import lifecycle_from_update

        self.assertIsNone(lifecycle_from_update(SignalError(1, "PLANE ALTITUDE failed.", "x", "s1")))

        class SignalError2:  # the shape Core moves to under contract C7 (not a Gap subclass)
            dropped_updates, reason = 1, "failed"

        SignalError2.__name__ = "SignalError"
        self.assertIsNone(lifecycle_from_update(SignalError2()))
        self.assertIs(lifecycle_from_update(Gap(3, "lost")).kind, LifecycleKind.GAP)
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        self.assertEqual(tape.rec.process(SignalError(1, "failed", "x", "s1")), [])


class FieldsTests(unittest.TestCase):
    def test_recorder_fields_match_the_core_catalog(self):
        from bbc_core.catalog import READS

        signals = {entry.signal.name: entry.signal for entry in READS.values()}
        for variable in snapshot_simvars():
            with self.subTest(field=variable.sdk_name):
                signal = signals.get(variable.sdk_name)
                self.assertIsNotNone(signal, "not in the catalog")
                self.assertEqual(variable.unit, signal.unit)
                self.assertIs(variable.value_type, signal.value_type)

    def test_touchdown_velocity_is_converted_from_the_catalog_unit(self):
        unit = next(unit for name, unit, *_ in RECORDER_FIELDS if name == "PLANE TOUCHDOWN NORMAL VELOCITY")
        self.assertEqual(unit, "feet per second")
        snapshot = SimpleNamespace(
            names=("PLANE TOUCHDOWN NORMAL VELOCITY", "VERTICAL SPEED"), values=(-2.75, -150.0),
            simulation_time_s=1.0, absolute_time_s=None, sample_time=T0, simulation_rate=1.0,
            received_at=T0 + timedelta(seconds=1), session_id="S", source=SimpleNamespace(simulated=False))
        sample = sample_from_snapshot(snapshot)
        self.assertAlmostEqual(sample.touchdown_velocity_fpm, -165.0)
        self.assertEqual(sample.vs_fpm, -150.0)  # already feet per minute
        mapped = sample_from_snapshot(snapshot, mapping={"PLANE TOUCHDOWN NORMAL VELOCITY": "touchdown_velocity_fpm"})
        self.assertEqual(mapped.touchdown_velocity_fpm, -2.75)  # an explicit mapping is taken as given


if __name__ == "__main__":
    unittest.main()
