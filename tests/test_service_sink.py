"""bbc_efb.recorder_sink.LogbookSink (contract C2): recorded flights into the one FlightStore."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from bbc_efb.recorder_sink import RECORDER_FLIGHT_ID, LogbookSink
from bbc_efb.storage import FlightStore
from bbc_recorder import EventKind, RecorderEvent
from bbc_recorder.journal import Journal, JournalLayout, Outcome
from bbc_recorder.records import build_recorded_flight
from bbc_recorder.recovery import finalize_closed, finalize_interrupted

try:
    from tests.test_recorder_flights import Tape
except ImportError:
    from test_recorder_flights import Tape


class Capture:
    def __init__(self):
        self.records = []

    def save_recorded_flight(self, record):
        self.records.append(copy.deepcopy(record))
        return "captured"


def _identity(flight_id):
    def field(value, origin="observed", status="known"):
        return {"value": value, "provenance": origin, "original": value, "status": status, "confidence": "medium",
                "reason": "test", "evidence": None}
    return {"flight_id": flight_id, "role": "identity",
            "fields": {"aircraft": field("A320", "derived"), "registration": field("G-EUUA"),
                       "callsign": field("BAW123", "derived"), "aircraft_type": field("A320", "derived"),
                       "aircraft_title": field("Airbus A320 Neo BA")}}


def _airport(flight_id, role, icao):
    return {"flight_id": flight_id, "role": role,
            "record_field": {"value": icao, "provenance": "derived", "status": "known", "confidence": "high",
                             "reason": "nearest airport"}}


class SinkTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = FlightStore(self.root, create=True)
        self.addCleanup(self.store.close)
        self.layout = JournalLayout(self.root)

    def record(self, *, complete=True, simulated=True):
        tape = Tape()
        if complete:
            tape.full_flight(cruise_s=120)
        else:
            tape.preflight()
            tape.taxi_out()
            tape.takeoff()
            tape.climb_to(2000)
        events = [e for e in tape.events if e.data.get("flight_id") == "F1"]
        source = {"name": "test", "simulated": simulated} if simulated is not None else None
        journal = Journal.create(self.layout, "F1", app_version="test", started_host_time=events[0].host_time,
                                 started_sim_utc=events[0].sim_utc, source=source)
        ended = None
        for event in events:
            if event.kind is EventKind.FLIGHT_ENDED:
                ended = event
                continue
            journal.append(event)
        attributions = [_identity("F1"), _airport("F1", "departure", "EGLL")]
        if complete:
            attributions.append(_airport("F1", "arrival", "EGLL"))  # looked up at block-on
        for data in attributions:
            journal.append(RecorderEvent(EventKind.ATTRIBUTION, events[-1].phase, events[-1].host_time, None, None,
                                         data))
        capture = Capture()
        if ended is not None:
            journal.append(ended)
            journal.close(Outcome.COMPLETE, ended_host_time=ended.host_time, ended_sim_utc=ended.sim_utc,
                          final_phase=ended.phase)
            finalize_closed(self.layout, "F1", sink=capture, archive=False, record_builder=build_recorded_flight)
        else:
            journal.abandon()
            finalize_interrupted("F1", data_dir=self.layout, sink=capture, now=tape.host + timedelta(minutes=5),
                                 archive=False, record_builder=build_recorded_flight)
        return capture.records[0], tape

    def test_complete_flight_becomes_a_recorder_flight_with_evidence(self):
        record, tape = self.record()
        sink = LogbookSink(lambda: self.store)
        flight_id = sink.save_recorded_flight(record)
        flight = self.store.get(flight_id)  # re-read from disk: the file round-trips
        summary = tape.rec.completed_flights[-1]
        self.assertEqual(flight.source, "recorder")
        self.assertEqual(flight.fields.aircraft, "A320")
        self.assertEqual(flight.fields.registration, "G-EUUA")
        self.assertEqual(flight.fields.callsign, "BAW123")
        self.assertEqual((flight.fields.departure, flight.fields.arrival), ("EGLL", "EGLL"))
        self.assertEqual(flight.fields.minutes, round(summary.block_s / 60))
        self.assertEqual(flight.fields.notes, "")
        self.assertIsNone(flight.fields.passengers)
        self.assertEqual(flight.provenance("registration").origin, "observed")
        self.assertEqual(flight.provenance("departure").origin, "derived")
        self.assertEqual(flight.provenance("notes").origin, "recorder")
        evidence = flight.evidence
        self.assertTrue(evidence.simulated)
        self.assertTrue(flight.simulated)
        self.assertEqual(evidence.time_basis, "simulator")
        self.assertAlmostEqual(evidence.block_s, summary.block_s)
        self.assertAlmostEqual(evidence.airborne_s, summary.airborne_s)
        self.assertEqual(evidence.touchdown_rate_fpm, summary.touchdowns[-1]["rate_fpm"])
        self.assertTrue(evidence.journal.startswith("recorder/"))
        self.assertEqual(evidence.extra[RECORDER_FLIGHT_ID], "F1")
        self.assertEqual(evidence.extra["status"], "complete")
        self.assertIn("SIMULATED", evidence.summary)

    def test_saving_twice_is_idempotent(self):
        record, _ = self.record()
        sink = LogbookSink(lambda: self.store)
        first = sink.save_recorded_flight(record)
        self.assertEqual(sink.save_recorded_flight(record), first)
        flights, _ = self.store.listing()
        self.assertEqual(len(flights), 1)

    def test_unreadable_earlier_save_is_not_duplicated(self):
        """Integration follow-up: an unreadable file holding this flight blocks a second copy."""
        record, _ = self.record()
        sink = LogbookSink(lambda: self.store)
        first = sink.save_recorded_flight(record)
        path = self.store.records / f"{first}.json"
        path.write_bytes(path.read_bytes()[:-40])  # damaged, but still names Recorder flight F1
        flights, unreadable = self.store.listing()
        self.assertEqual((len(flights), len(unreadable)), (0, 1))
        with self.assertRaisesRegex(OSError, "A second copy was not created"):
            sink.save_recorded_flight(record)
        self.assertEqual(sorted(p.name for p in self.store.records.glob("*.json")), [path.name])

    def test_misnamed_copy_of_earlier_save_is_not_duplicated(self):
        """A sync tool renamed the only copy: listing() reports it misnamed, never parses it."""
        record, _ = self.record()
        sink = LogbookSink(lambda: self.store)
        first = sink.save_recorded_flight(record)
        path = self.store.records / f"{first}.json"
        renamed = path.with_name(f"{first}-DESKTOP-ABC.json")
        path.rename(renamed)
        flights, unreadable = self.store.listing()
        self.assertEqual((len(flights), [item.kind for item in unreadable]), (0, ["misnamed"]))
        with self.assertRaisesRegex(OSError, "A second copy was not created"):
            sink.save_recorded_flight(record)
        self.assertEqual(sorted(p.name for p in self.store.records.glob("*.json")), [renamed.name])

    def test_unrelated_misnamed_file_does_not_block_saving(self):
        (self.store.records / "notes-backup.json").write_text('{"hello": 1}', encoding="utf-8")
        record, _ = self.record()
        flight_id = LogbookSink(lambda: self.store).save_recorded_flight(record)
        self.assertEqual(self.store.get(flight_id).source, "recorder")

    def test_unrelated_unreadable_file_does_not_block_saving(self):
        (self.store.records / "00000000-0000-4000-8000-000000000001.json").write_text("{not json", encoding="utf-8")
        record, _ = self.record()
        flight_id = LogbookSink(lambda: self.store).save_recorded_flight(record)
        self.assertEqual(self.store.get(flight_id).source, "recorder")

    def test_file_that_cannot_be_read_now_keeps_the_journal(self):
        from unittest import mock

        (self.store.records / "00000000-0000-4000-8000-000000000002.json").write_text("{}", encoding="utf-8")
        record, _ = self.record()
        sink = LogbookSink(lambda: self.store)
        real = Path.read_bytes

        def busy(path_self):
            if path_self.name.startswith("00000000-0000-4000-8000-000000000002"):
                raise PermissionError(13, "in use (test)")
            return real(path_self)

        with mock.patch.object(Path, "read_bytes", busy):
            with self.assertRaisesRegex(OSError, "cannot check whether this flight was already saved"):
                sink.save_recorded_flight(record)
        flights, _ = self.store.listing()
        self.assertEqual(flights, [])

    def test_interrupted_flight_keeps_unknowns_unknown(self):
        record, _ = self.record(complete=False)
        flight = self.store.get(LogbookSink(lambda: self.store).save_recorded_flight(record))
        self.assertEqual(flight.fields.arrival, "")
        self.assertIsNone(flight.fields.minutes)
        self.assertIsNone(flight.evidence.block_s)
        self.assertIsNone(flight.evidence.airborne_s)
        self.assertIsNone(flight.evidence.on_block)
        self.assertEqual(flight.evidence.gaps[-1].kind, "interrupted")
        self.assertIsNone(flight.evidence.gaps[-1].end)
        self.assertEqual(flight.evidence.extra["status"], "interrupted")

    def test_unknown_source_is_never_shown_as_native(self):
        record, _ = self.record(simulated=None)
        flight = self.store.get(LogbookSink(lambda: self.store).save_recorded_flight(record))
        self.assertTrue(flight.evidence.simulated)
        self.assertTrue(flight.evidence.extra["simulated_unknown"])

    def test_plain_recovery_record_without_logbook_block(self):
        record, _ = self.record()
        record.pop("logbook")
        flight = self.store.get(LogbookSink(lambda: self.store).save_recorded_flight(record))
        self.assertEqual(flight.fields.departure, "EGLL")

    def test_refusals_keep_the_journal(self):
        record, _ = self.record()
        with self.assertRaises(OSError):
            LogbookSink(lambda: None).save_recorded_flight(record)
        reader = FlightStore(self.root, read_only=True)
        with self.assertRaises(OSError):
            LogbookSink(lambda: reader).save_recorded_flight(record)
        with self.assertRaises(ValueError):
            LogbookSink(lambda: self.store).save_recorded_flight({**record, "status": "discarded"})
        with self.assertRaises(ValueError):
            LogbookSink(lambda: self.store).save_recorded_flight({**record, "evidence": {"journal": ""}})
        with self.assertRaises(ValueError):
            LogbookSink(lambda: self.store).save_recorded_flight({**record, "flight_id": None})
        with self.assertRaises(TypeError):
            LogbookSink(None)
        self.assertEqual(self.store.listing()[0], [])

    def test_store_is_looked_up_on_every_save(self):
        record, _ = self.record()
        current = {"store": None}
        sink = LogbookSink(lambda: current["store"])
        with self.assertRaises(OSError):
            sink.save_recorded_flight(record)
        current["store"] = self.store
        self.assertTrue(sink.save_recorded_flight(record))

    def test_build_record_shape_is_accepted(self):
        # A record shaped exactly like bbc_recorder.recovery.build_record (no 'summary', no 'logbook').
        record, _ = self.record()
        plain = {key: value for key, value in record.items() if key not in ("summary", "logbook")}
        fields, origins, evidence, reason = LogbookSink(lambda: self.store).convert(plain)
        self.assertEqual(fields.departure, "EGLL")
        self.assertIn("complete", reason)
        self.assertEqual(set(origins), set(fields.__slots__))


if __name__ == "__main__":
    unittest.main()
