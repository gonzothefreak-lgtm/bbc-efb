"""Recorded-flight records built from journal evidence only (bbc_recorder.records)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from bbc_recorder import EventKind, LifecycleKind, RecorderEvent
from bbc_recorder.journal import Journal, JournalLayout, Outcome
from bbc_recorder.records import build_recorded_flight, journal_is_loggable, logbook_from_record
from bbc_recorder.recovery import finalize_closed, finalize_interrupted

try:
    from tests.test_recorder_flights import T0, Tape
except ImportError:
    from test_recorder_flights import T0, Tape


class ListSink:
    def __init__(self):
        self.records = []

    def save_recorded_flight(self, record):
        json.dumps(record)  # plain JSON only
        self.records.append(record)
        return f"rec-{len(self.records)}"


def airport(role, icao, status="known", flight_id="F1", phase=None):
    data = {"flight_id": flight_id, "role": role,
            "record_field": {"value": icao, "provenance": "derived", "original": icao, "status": status,
                             "confidence": "high", "reason": f"{icao} matched", "evidence": None},
            "attribution": {"role": role, "candidates": [{"icao": icao}]}}
    if role == "arrival":
        data["assessment"] = {"verdict": "diversion_suspected", "needs_pilot_confirmation": True}
    return data


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.layout = JournalLayout(Path(self.folder.name))

    def journal_flight(self, tape, *, extra_events=(), close=True):
        events = [e for e in tape.events if e.data.get("flight_id") == "F1"]
        journal = Journal.create(self.layout, "F1", app_version="test", started_host_time=events[0].host_time,
                                 started_sim_utc=events[0].sim_utc,
                                 source={"name": "BBC Core simulated provider", "simulated": True})
        ended = None
        for event in events:
            if event.kind is EventKind.FLIGHT_ENDED:
                ended = event
                continue
            journal.append(event)
        for data in extra_events:
            journal.append(RecorderEvent(EventKind.ATTRIBUTION, events[-1].phase, events[-1].host_time, None, None,
                                         data))
        if close and ended is not None:
            journal.append(ended)
            journal.close(Outcome.COMPLETE if ended.data["outcome"] == "complete" else Outcome.ABANDONED,
                          ended_host_time=ended.host_time, ended_sim_utc=ended.sim_utc, final_phase=ended.phase)
        else:
            journal.abandon()
        return journal

    def test_complete_flight_record(self):
        tape = Tape()
        tape.full_flight(cruise_s=120)
        self.journal_flight(tape, extra_events=(airport("departure", "EGLL"), airport("arrival", "EGKK")))
        sink = ListSink()
        result = finalize_closed(self.layout, "F1", sink=sink, record_builder=build_recorded_flight)
        record = sink.records[0]
        self.assertEqual(result.saved_id, "rec-1")
        block = record["logbook"]
        summary = tape.rec.completed_flights[-1]
        self.assertEqual(block["status"], "complete")
        self.assertEqual(block["time_basis"], "simulator")
        self.assertAlmostEqual(block["block_s"], summary.block_s)
        self.assertAlmostEqual(block["airborne_s"], summary.airborne_s)
        self.assertEqual(block["touchdown_rate_fpm"], summary.touchdowns[-1]["rate_fpm"])
        self.assertEqual(block["fields"]["departure"]["value"], "EGLL")
        self.assertEqual(block["fields"]["arrival"]["value"], "EGKK")
        self.assertEqual(block["fields"]["minutes"]["value"], round(summary.block_s / 60))
        self.assertEqual(block["fields"]["flight_date"]["value"], (T0 + timedelta(seconds=5000)).date().isoformat())
        self.assertEqual(block["arrival_assessment"]["verdict"], "diversion_suspected")  # kept, not confirmed
        self.assertTrue(block["simulated"])
        self.assertIn("SIMULATED", block["summary_text"])
        self.assertEqual(block["off_block"], tape.of(EventKind.BLOCK_OFF)[0].sim_utc.isoformat())
        self.assertEqual(record["evidence"]["journal"].split("/")[0], "recorder")

    def test_interrupted_flight_has_no_durations_and_an_explicit_gap(self):
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(3000)
        tape.cruise(60)
        self.journal_flight(tape, extra_events=(airport("departure", "EGLL"),), close=False)
        sink = ListSink()
        now = tape.host + timedelta(minutes=10)
        finalize_interrupted("F1", data_dir=self.layout, sink=sink, now=now, record_builder=build_recorded_flight)
        record = sink.records[0]
        block = record["logbook"]
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["ended"], {"host_time": None, "sim_time_s": None, "sim_utc": None,
                                           "final_phase": None})
        self.assertIsNone(block["block_s"])
        self.assertIsNone(block["airborne_s"])
        self.assertIsNone(block["on_block"])
        self.assertIsNone(block["landing"])
        self.assertIsNotNone(block["takeoff"])
        self.assertIsNone(block["fields"]["minutes"]["value"])
        self.assertEqual(block["fields"]["arrival"]["value"], "")
        self.assertEqual(block["fields"]["departure"]["value"], "EGLL")
        interruption = [gap for gap in block["gaps"] if gap["kind"] == "interrupted"]
        self.assertEqual(len(interruption), 1)
        self.assertIsNotNone(interruption[0]["start"])
        self.assertIsNone(interruption[0]["end"])  # simulator time at the end is unknown, never invented
        self.assertEqual(interruption[0]["host_end"], now.isoformat())

    def test_ambiguous_airport_leaves_the_field_empty_with_the_candidate(self):
        tape = Tape()
        tape.full_flight(cruise_s=60)
        self.journal_flight(tape, extra_events=(airport("departure", "EGLL", status="ambiguous"),))
        sink = ListSink()
        finalize_closed(self.layout, "F1", sink=sink, record_builder=build_recorded_flight)
        departure = sink.records[0]["logbook"]["fields"]["departure"]
        self.assertEqual(departure["value"], "")
        self.assertEqual(departure["status"], "ambiguous")
        self.assertEqual(departure["candidate"], "EGLL")

    def test_gap_from_events_when_no_summary(self):
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.life(LifecycleKind.DISCONNECTED, detail="lost")
        self.journal_flight(tape, close=False)
        sink = ListSink()
        finalize_interrupted("F1", data_dir=self.layout, sink=sink, now=tape.host + timedelta(seconds=60),
                             record_builder=build_recorded_flight)
        kinds = [gap["kind"] for gap in sink.records[0]["logbook"]["gaps"]]
        self.assertEqual(kinds, ["unresolved", "interrupted"])

    def test_logbook_from_a_plain_record_still_works(self):
        block = logbook_from_record({"status": "interrupted", "milestones": {}, "gaps": [],
                                     "started": {"host_time": T0.isoformat()}, "source": None})
        self.assertEqual(block["time_basis"], "host")
        self.assertEqual(block["fields"]["flight_date"]["value"], T0.date().isoformat())
        self.assertIsNone(block["simulated"])
        self.assertIn("not recorded", block["summary_text"])

    def test_journal_is_loggable(self):
        tape = Tape()
        tape.run(20)
        self.assertFalse(journal_is_loggable(tape.events))
        tape.taxi_out()
        self.assertTrue(journal_is_loggable(tape.events))
        self.assertTrue(journal_is_loggable([{"kind": "phase_changed", "data": {"reason": "started in the air"}}]))


if __name__ == "__main__":
    unittest.main()
