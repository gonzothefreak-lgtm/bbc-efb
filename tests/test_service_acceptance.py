"""End-to-end acceptance: flight tapes through SimulatedProvider and real Core into a FlightStore.

Each scenario (bbc_recorder.tape.SCENARIOS) is converted to bbc_core.simulated FlightTape
sessions and played on a fake clock. The real RecorderService (FlightRecorder, journal,
recovery, attribution) is ticked after every Core pump and saves through
bbc_efb.recorder_sink.LogbookSink into a temporary FlightStore. All data is SIMULATED.
"""

from __future__ import annotations

import logging
import unittest
from datetime import datetime

from bbc_core.contract import SnapshotPeriod
from bbc_recorder.journal import file_sha256, read_journal
from bbc_recorder.model import EventKind
from bbc_recorder.service import RecorderConfig, RecorderService
from bbc_recorder.tape import SCENARIOS, core_sessions

try:
    from tests.test_recorder_support import CoreTapeRun
except ImportError:
    from test_recorder_support import CoreTapeRun

logging.getLogger("bbc_recorder").setLevel(logging.ERROR)


class ScenarioTests(unittest.TestCase):
    def play(self, name, **options):
        run = CoreTapeRun(SCENARIOS[name](), **options)
        self.addCleanup(run.close)
        return run.run()

    def only_flight(self, run):
        flights = run.flights()
        self.assertEqual(len(flights), 1, [f.fields for f in flights])
        return flights[0]

    def assert_archived_evidence(self, run, flight):
        evidence = flight.evidence
        path = run.data_dir / evidence.journal
        self.assertTrue(path.is_file(), evidence.journal)
        self.assertTrue(evidence.journal.startswith("recorder/journals/"))
        self.assertEqual(file_sha256(path), evidence.extra["journal_sha256"])
        journal = read_journal(path)
        self.assertTrue(journal.clean)
        self.assertEqual(journal.header["flight_id"], evidence.extra["recorder_flight_id"])
        return journal

    def test_normal_flight(self):
        run = self.play("normal_flight")
        flight = self.only_flight(run)
        expect = run.tape.header.expect
        fields, evidence = flight.fields, flight.evidence
        figures = evidence.extra["figures"]
        self.assertEqual(flight.source, "recorder")
        self.assertEqual(evidence.extra["status"], expect["outcome"])
        self.assertEqual((fields.departure, fields.arrival), (expect["departure"], expect["arrival"]))
        self.assertEqual(figures["landings"], expect["landings"])
        self.assertEqual(figures["bounces"], expect["bounces"])
        self.assertEqual(figures["max_sim_rate"], expect["max_rate"])
        self.assertAlmostEqual(evidence.touchdown_rate_fpm, expect["touchdown_fpm"], delta=1.0)
        self.assertEqual(evidence.extra["touchdown_rate_source"], "touchdown_simvar")
        self.assertTrue(evidence.simulated)
        self.assertIn("SIMULATED", evidence.summary)
        self.assertEqual(evidence.time_basis, "simulator")
        # The flight crosses 00:00Z in simulator time: the date is the block-off date.
        off, on = (datetime.fromisoformat(evidence.off_block), datetime.fromisoformat(evidence.on_block))
        self.assertEqual((off.day, on.day), (24, 25))
        self.assertEqual(fields.flight_date, "2026-11-24")
        # Pauses are not credited: block time is shorter than the host time between the two.
        self.assertGreater(figures["paused_host_s"], 90)
        self.assertLess(evidence.block_s, (on - off).total_seconds())
        self.assertEqual(fields.minutes, round(evidence.block_s / 60))
        self.assertEqual(fields.registration, "VH-TPE")
        self.assertEqual(flight.provenance("registration").origin, "observed")
        self.assertEqual(flight.provenance("departure").origin, "derived")
        self.assertEqual(evidence.gaps, ())
        journal = self.assert_archived_evidence(run, flight)
        kinds = [e.kind for e in journal.events]
        for kind in (EventKind.FLIGHT_STARTED, EventKind.BLOCK_OFF, EventKind.TAKEOFF, EventKind.PAUSED,
                     EventKind.TOUCHDOWN, EventKind.BLOCK_ON, EventKind.FLIGHT_ENDED, EventKind.SAMPLE):
            self.assertIn(kind, kinds)
        self.assertEqual(journal.footer["outcome"], "complete")
        self.assertEqual(run.service.status().state, "armed")  # ready for the next sector

    def test_bounced_landing(self):
        run = self.play("bounced_landing")
        flight = self.only_flight(run)
        figures = flight.evidence.extra["figures"]
        self.assertEqual((figures["landings"], figures["bounces"]), (1, 1))
        self.assertEqual((flight.fields.departure, flight.fields.arrival), ("YSSY", "YSSY"))
        self.assertAlmostEqual(flight.evidence.touchdown_rate_fpm, -420.0, delta=1.0)  # the first contact

    def test_disconnect_midflight_keeps_one_flight_with_a_gap(self):
        run = self.play("disconnect_midflight")
        flight = self.only_flight(run)
        evidence = flight.evidence
        self.assertEqual(evidence.extra["status"], "complete")
        self.assertEqual(len(evidence.gaps), 1)
        gap = evidence.gaps[0]
        self.assertIn("MSFS stopped responding", gap.reason)
        self.assertIsNotNone(gap.start)
        self.assertIsNotNone(gap.end)
        self.assertEqual((flight.fields.departure, flight.fields.arrival), ("YSSY", "YWLM"))
        journal = self.assert_archived_evidence(run, flight)
        sessions = {e.data.get("session_id") for e in journal.events if e.kind is EventKind.SAMPLE}
        self.assertEqual(len(sessions), 2)
        self.assertEqual(len(core_sessions(run.tape)), 2)

    def test_crash_in_cruise_is_abandoned_without_arrival(self):
        run = self.play("crash_in_cruise")
        flight = self.only_flight(run)
        evidence = flight.evidence
        self.assertEqual(evidence.extra["status"], "abandoned")
        self.assertEqual(evidence.extra["figures"]["end_reason"], "aircraft crashed")
        self.assertEqual(flight.fields.arrival, "")
        self.assertIsNone(flight.fields.minutes)
        self.assertIsNone(evidence.block_s)       # no block-on observed
        self.assertIsNone(evidence.landing)
        self.assertEqual(flight.fields.departure, "YSSY")

    def test_queue_gap_on_approach_is_listed(self):
        run = self.play("queue_gap_on_approach")
        flight = self.only_flight(run)
        kinds = [gap.kind for gap in flight.evidence.gaps]
        self.assertEqual(kinds, ["reported"])
        self.assertEqual(flight.evidence.extra["status"], "complete")

    def test_parked_only_never_reaches_the_logbook(self):
        run = self.play("parked_only")
        self.assertEqual(run.flights(), [])
        layout = run.service.layout
        self.assertGreaterEqual(len(list(layout.discarded_dir.glob("*.jsonl"))), 1)
        self.assertEqual(run.service.status().last_flight.outcome, "discarded")

    def test_sampling_follows_the_hint_through_core(self):
        run = CoreTapeRun(SCENARIOS["bounced_landing"]())
        self.addCleanup(run.close)
        fed, periods = [], set()
        feed = run.service.recorder.feed

        def spy(sample):
            fed.append((sample.session_id, sample.sim_time_s))
            periods.add(run.service._main.period)
            return feed(sample)

        run.service.recorder.feed = spy
        run.run()
        self.assertEqual(periods, {SnapshotPeriod.SECOND, SnapshotPeriod.SIM_FRAME})
        self.assertEqual(len(fed), len(set(fed)))
        times = [t for _, t in fed]
        self.assertEqual(times, sorted(times))
        self.assertLessEqual(max(b - a for a, b in zip(times, times[1:])), 1.0 + 1e-6)
        self.assertEqual(len(run.flights()), 1)

    def test_background_saving_with_core(self):
        run = CoreTapeRun(SCENARIOS["bounced_landing"](), config=RecorderConfig(background_saves=True))
        self.addCleanup(run.close)
        run.run()
        result = run.service.save_and_close(10)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(len(run.flights()), 1)


class InterruptionTests(unittest.TestCase):
    def test_save_and_exit_in_cruise_saves_an_interrupted_flight(self):
        run = CoreTapeRun(SCENARIOS["normal_flight"]())
        self.addCleanup(run.close)
        run.run(until=500)
        self.assertEqual(run.service.status().state, "recording")
        result = run.service.save_and_close(5)
        self.assertTrue(result.ok, result.detail)
        flight = run.flights()[0]
        evidence = flight.evidence
        self.assertEqual(evidence.extra["status"], "interrupted")
        self.assertIsNone(evidence.on_block)
        self.assertIsNone(evidence.landing)
        self.assertIsNone(evidence.block_s)
        self.assertIsNone(evidence.airborne_s)
        self.assertEqual(flight.fields.arrival, "")
        self.assertEqual(flight.fields.departure, "YSSY")
        self.assertEqual(evidence.gaps[-1].kind, "interrupted")
        self.assertIsNone(evidence.gaps[-1].end)   # never an invented end time
        self.assertIsNotNone(evidence.extra["interruption"]["from_host_time"])
        self.assertEqual(run.service.status().state, "unavailable")

    def test_crash_of_the_efb_is_recovered_at_the_next_start(self):
        run = CoreTapeRun(SCENARIOS["normal_flight"]())
        self.addCleanup(run.close)
        run.run(until=700)
        service = run.service
        flight_id = service.status().flight_id
        # A crash: the journal is left without a footer, nothing else runs.
        service._flight.journal.abandon()
        service._flight = None
        service._closed = True
        later = RecorderService(run.api, run.sink, run.data_dir, None, run.clock.wall, monotonic=run.clock,
                                config=RecorderConfig(background_saves=False))
        found = later.unfinished()
        self.assertEqual([f.flight_id for f in found], [flight_id])
        saved = later.recover_unfinished()
        self.assertEqual([r.flight_id for r in saved], [flight_id])
        flight = run.flights()[0]
        self.assertEqual(flight.evidence.extra["status"], "interrupted")
        self.assertEqual(flight.evidence.extra["recorder_flight_id"], flight_id)
        self.assertIsNotNone(flight.evidence.takeoff)
        self.assertIsNone(flight.evidence.on_block)
        self.assertTrue(flight.evidence.simulated)
        # Saving it again (for example after a crash between save and archive) adds nothing.
        self.assertEqual(later.recover_unfinished(), [])
        self.assertEqual(len(run.flights()), 1)
        later.close()


if __name__ == "__main__":
    unittest.main()
