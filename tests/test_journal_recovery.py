"""Startup recovery: find, finalise as interrupted, complete a pending save, resume, discard."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bbc_recorder.journal import (
    Journal, JournalClosedError, JournalLayout, JournalLockedError, Outcome, file_sha256,
    list_archived, read_journal,
)
from bbc_recorder.model import EventKind, Phase, RecorderEvent
from bbc_recorder.recovery import (
    INTERRUPTION_REASON, FlightSink, RecoveryError, RecoveryState, discard_unfinished,
    finalize_closed, finalize_interrupted, find_unfinished, inspect_journal, resume_recording,
    stale_lock_files,
)

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=5)


def ev(i: int, kind: EventKind = EventKind.SAMPLE, phase: Phase = Phase.AIRBORNE,
       **data: Any) -> RecorderEvent:
    return RecorderEvent(kind, phase, T0 + timedelta(seconds=i), 500.0 + i,
                         T0 + timedelta(seconds=i, hours=-1), {"i": i, **data})


class Sink:
    def __init__(self, fail: int = 0) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self.fail = fail

    def save_recorded_flight(self, record: dict[str, Any]) -> str:
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise OSError("disk full")
        self.records[record["flight_id"]] = record  # idempotent by flight id
        return "rec-" + record["flight_id"]


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.layout = JournalLayout(self.data)

    def flight(self, flight_id: str, events: list[RecorderEvent], *, close: Outcome | None = None,
               start: datetime = T0) -> Journal:
        j = Journal.create(self.layout, flight_id, app_version="0.1.0a1", started_host_time=start,
                           aircraft={"title": "Cessna 172", "atc_id": "G-ABCD"},
                           source={"name": "simconnect", "simulated": False})
        for e in events:
            j.append(e)
        if close is not None:
            j.close(close, ended_host_time=events[-1].host_time, final_phase=Phase.COMPLETE)
        else:
            j.abandon()
        return j

    def typical(self) -> list[RecorderEvent]:
        return [ev(0, EventKind.FLIGHT_STARTED, Phase.PREFLIGHT), ev(60, EventKind.BLOCK_OFF, Phase.TAXI_OUT),
                ev(300, EventKind.TAKEOFF, Phase.AIRBORNE), *[ev(300 + k) for k in range(1, 50)]]

    def test_sink_protocol(self) -> None:
        self.assertIsInstance(Sink(), FlightSink)

    def test_nothing_to_recover(self) -> None:
        self.assertEqual(find_unfinished(self.data), [])
        self.flight("DONE", self.typical(), close=Outcome.COMPLETE)
        finalize_closed(self.data, "DONE")
        self.assertEqual(find_unfinished(self.data), [])

    def test_find_unfinished_describes_the_interruption(self) -> None:
        events = self.typical()
        j = self.flight("F1", events)
        data = j.path.read_bytes()
        j.path.write_bytes(data[:-9])
        [flight] = find_unfinished(self.data)
        self.assertEqual(flight.flight_id, "F1")
        self.assertEqual(flight.state, RecoveryState.INTERRUPTED)
        self.assertTrue(flight.recoverable)
        self.assertEqual(flight.event_count, len(events) - 1)
        self.assertEqual(list(flight.events), events[:-1])
        self.assertEqual(flight.last_good_host_time, events[-2].host_time)
        self.assertEqual(flight.last_good_sim_time_s, events[-2].sim_time_s)
        self.assertEqual(flight.last_phase, Phase.AIRBORNE)
        self.assertEqual(flight.started_host_time, T0)
        self.assertEqual(flight.aircraft["title"], "Cessna 172")
        self.assertGreater(flight.torn_tail_bytes, 0)
        self.assertIn("stopped while this flight was being recorded", flight.reason)
        self.assertIn("10:05:48 UTC", flight.reason)
        self.assertIn("incomplete line", flight.reason)
        self.assertEqual([e.kind for e in flight.notable_events],
                         [EventKind.FLIGHT_STARTED, EventKind.BLOCK_OFF, EventKind.TAKEOFF])
        self.assertEqual(j.path.read_bytes(), data[:-9])  # find is read-only

    def test_load_events_false_bounds_memory(self) -> None:
        self.flight("F2", self.typical())
        [flight] = find_unfinished(self.data, load_events=False)
        self.assertEqual(flight.events, ())
        self.assertEqual(flight.event_count, len(self.typical()))
        self.assertEqual(len(flight.notable_events), 3)

    def test_finalize_interrupted_never_invents_an_end(self) -> None:
        events = self.typical()
        self.flight("F3", events)
        sink = Sink()
        [flight] = find_unfinished(self.data)
        result = finalize_interrupted(flight, data_dir=self.data, sink=sink, now=NOW)
        self.assertEqual(result.outcome, Outcome.INTERRUPTED)
        self.assertEqual(result.saved_id, "rec-F3")
        record = sink.records["F3"]
        self.assertIs(record, result.record)
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["origin"], "recorder")
        self.assertEqual(record["ended"], {"host_time": None, "sim_time_s": None, "sim_utc": None,
                                           "final_phase": None})
        self.assertEqual(record["started"]["host_time"], T0.isoformat())
        gap = record["interruption"]
        self.assertEqual(gap["from_host_time"], events[-1].host_time.isoformat())
        self.assertEqual(gap["to_host_time"], NOW.isoformat())
        self.assertEqual(gap["to_is"], "recovery_time")
        self.assertEqual(gap["from_sim_time_s"], events[-1].sim_time_s)
        self.assertIsNone(gap["to_sim_time_s"])
        self.assertEqual(record["last_good_event"], events[-1].to_json())
        self.assertEqual(record["event_count"], len(events) + 1)  # + the gap event
        self.assertEqual([g["reason"] for g in record["gaps"]], [INTERRUPTION_REASON])
        self.assertEqual(sorted(record["milestones"]), ["block_off", "flight_started", "takeoff"])
        self.assertEqual(record["aircraft"]["atc_id"], "G-ABCD")
        self.assertEqual(record["source"], {"name": "simconnect", "simulated": False})
        self.assertTrue(record["integrity"]["clean"])
        # evidence pointer and checksum match the archived journal
        self.assertEqual(record["evidence"]["journal"], "recorder/journals/2026-11/F3.jsonl")
        self.assertEqual(result.archived_path, self.data / record["evidence"]["journal"])
        self.assertEqual(record["evidence"]["sha256"], file_sha256(result.archived_path))
        # the journal itself
        archived = read_journal(result.archived_path)
        self.assertEqual(list(archived.events[:-1]), events)
        last = archived.events[-1]
        self.assertEqual((last.kind, last.phase, last.host_time, last.sim_time_s),
                         (EventKind.GAP, Phase.AIRBORNE, NOW, None))
        self.assertEqual(archived.footer["outcome"], "interrupted")
        self.assertIsNone(archived.footer["ended_host_time"])
        self.assertEqual(archived.footer["extra"]["recovered_host_time"], NOW.isoformat())
        self.assertEqual(find_unfinished(self.data), [])
        self.assertEqual(list(self.layout.in_progress_dir.iterdir()), [])

    def test_torn_tail_is_kept_as_evidence_and_reported(self) -> None:
        j = self.flight("F4", self.typical())
        torn = j.path.read_bytes()[:-20]
        j.path.write_bytes(torn)
        result = finalize_interrupted("F4", data_dir=self.data, now=NOW)
        archived_bytes = result.archived_path.read_bytes()
        self.assertTrue(archived_bytes.startswith(torn))
        gap = read_journal(result.archived_path).events[-1]
        self.assertGreater(gap.data["ignored_tail_bytes"], 0)
        self.assertEqual(result.record["integrity"]["issue_count"], 1)
        self.assertFalse(result.record["integrity"]["clean"])

    def test_sink_failure_leaves_journal_for_next_start(self) -> None:
        self.flight("F5", self.typical())
        sink = Sink(fail=1)
        with self.assertRaises(RecoveryError) as caught:
            finalize_interrupted("F5", data_dir=self.data, sink=sink, now=NOW)
        self.assertIn("could not be saved", str(caught.exception))
        self.assertEqual(list_archived(self.data), [])
        [pending] = find_unfinished(self.data)
        self.assertEqual(pending.state, RecoveryState.CLOSED_NOT_ARCHIVED)
        self.assertIn("before the flight was saved", pending.reason)
        size = pending.path.stat().st_size
        later = NOW + timedelta(hours=1)
        result = finalize_interrupted(pending, data_dir=self.data, sink=sink, now=later)
        self.assertEqual(sink.calls, 2)
        # nothing was appended the second time; the original recovery time is kept
        self.assertEqual(result.archived_path.stat().st_size, size)
        self.assertEqual(result.record["interruption"]["to_host_time"], NOW.isoformat())
        self.assertEqual(result.record["interruption"]["recovered_host_time"], NOW.isoformat())
        self.assertEqual(find_unfinished(self.data), [])

    def test_normal_close_then_finalize_closed(self) -> None:
        events = self.typical()
        self.flight("F6", events, close=Outcome.COMPLETE)
        with self.assertRaises(FileNotFoundError):
            finalize_closed(self.data, "NOPE")
        self.assertFalse(self.layout.lock_path("NOPE").exists())
        sink = Sink()
        result = finalize_closed(self.data, "F6", sink=sink)
        self.assertEqual(result.outcome, Outcome.COMPLETE)
        record = sink.records["F6"]
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["ended"]["host_time"], events[-1].host_time.isoformat())
        self.assertIsNone(record["interruption"])
        self.assertEqual(record["last_good_event"], events[-1].to_json())
        self.assertEqual(read_journal(result.archived_path).event_count, len(events))

    def test_finalize_closed_refuses_unfinished(self) -> None:
        self.flight("F7", self.typical())
        with self.assertRaises(RecoveryError):
            finalize_closed(self.data, "F7")
        self.assertEqual(len(find_unfinished(self.data)), 1)

    def test_crash_between_close_and_save_is_recoverable(self) -> None:
        self.flight("F8", self.typical(), close=Outcome.COMPLETE)
        [pending] = find_unfinished(self.data)
        self.assertEqual(pending.state, RecoveryState.CLOSED_NOT_ARCHIVED)
        self.assertIn("closed (complete)", pending.reason)
        result = finalize_interrupted(pending, data_dir=self.data, sink=Sink(), now=NOW)
        self.assertEqual(result.outcome, Outcome.COMPLETE)  # honest: it did close normally
        self.assertIsNone(result.record["interruption"])

    def test_empty_journal(self) -> None:
        j = Journal.create(self.layout, "F9", app_version="x")
        j.abandon()
        [flight] = find_unfinished(self.data)
        self.assertEqual(flight.state, RecoveryState.EMPTY)
        self.assertIn("before anything was recorded", flight.reason)
        path = discard_unfinished(flight, data_dir=self.data, now=NOW)
        self.assertEqual(path, self.layout.discarded_dir / "F9.jsonl")
        footer = read_journal(path).footer
        self.assertEqual(footer["outcome"], "discarded")
        self.assertEqual(find_unfinished(self.data), [])

    def test_zero_length_file_is_offered(self) -> None:
        self.layout.in_progress_dir.mkdir(parents=True)
        (self.layout.in_progress_dir / "Z1.jsonl").write_bytes(b"")
        [flight] = find_unfinished(self.data)
        self.assertEqual(flight.state, RecoveryState.EMPTY)
        self.assertIn("not even the opening line", flight.reason)
        result = finalize_interrupted(flight, data_dir=self.data, now=NOW)
        self.assertEqual(result.archived_path.parent.name, "undated")
        self.assertEqual(result.record["status"], "interrupted")
        self.assertIsNone(result.record["started"]["host_time"])

    def test_discard_keeps_the_file(self) -> None:
        self.flight("D1", self.typical())
        path = discard_unfinished("D1", data_dir=self.data, now=NOW)
        r = read_journal(path)
        self.assertEqual(r.event_count, len(self.typical()))
        self.assertEqual(r.footer["extra"]["discarded_host_time"], NOW.isoformat())

    def test_resume_recording(self) -> None:
        events = self.typical()
        self.flight("R1", events)
        [flight] = find_unfinished(self.data)
        j = resume_recording(flight, data_dir=self.data, now=NOW)
        self.addCleanup(j.abandon)
        self.assertEqual(find_unfinished(self.data), [])  # locked: in use
        self.assertEqual(len(find_unfinished(self.data, include_in_use=True)), 1)
        self.assertEqual(find_unfinished(self.data, include_in_use=True)[0].state, RecoveryState.IN_USE)
        j.append(ev(10_000, EventKind.TOUCHDOWN, Phase.LANDING_ROLL))
        j.close(ended_host_time=NOW + timedelta(minutes=5), final_phase=Phase.COMPLETE)
        r = read_journal(j.path)
        self.assertTrue(r.clean)
        gap = r.events[len(events)]
        self.assertEqual(gap.kind, EventKind.GAP)
        self.assertEqual(gap.data["reason"], INTERRUPTION_REASON)
        self.assertIn("resumed", gap.data["detail"])
        self.assertEqual(r.events[-1].kind, EventKind.TOUCHDOWN)
        with self.assertRaises(JournalClosedError):
            resume_recording("R1", data_dir=self.data)

    def test_in_use_journal_is_never_touched(self) -> None:
        j = Journal.create(self.layout, "U1", app_version="x")
        self.addCleanup(j.abandon)
        j.append(ev(0))
        self.assertEqual(find_unfinished(self.data), [])
        info = inspect_journal(self.data, "U1")
        self.assertEqual(info.state, RecoveryState.IN_USE)
        self.assertFalse(info.recoverable)
        with self.assertRaises(JournalLockedError):
            finalize_interrupted("U1", data_dir=self.data)
        with self.assertRaises(JournalLockedError):
            discard_unfinished("U1", data_dir=self.data)

    def test_several_flights_sorted_oldest_first_and_foreign_files_ignored(self) -> None:
        self.flight("B", [ev(0)], start=T0 + timedelta(days=1))
        self.flight("A", [ev(0)], start=T0)
        (self.layout.in_progress_dir / "notes.txt").write_text("x")
        (self.layout.in_progress_dir / "bad name.jsonl").write_text("x")
        self.assertEqual([f.flight_id for f in find_unfinished(self.data)], ["A", "B"])

    def test_stale_lock_files(self) -> None:
        self.layout.in_progress_dir.mkdir(parents=True)
        (self.layout.in_progress_dir / "GONE.lock").write_text("123\n")
        self.assertEqual([p.name for p in stale_lock_files(self.data)], ["GONE.lock"])

    def test_custom_record_builder(self) -> None:
        self.flight("CB", self.typical())
        seen: dict[str, Any] = {}

        def builder(result: Any, **kw: Any) -> dict[str, Any]:
            seen.update(kw)
            return {"flight_id": kw["flight_id"], "events": result.event_count}

        sink = Sink()
        finalize_interrupted("CB", data_dir=self.data, sink=sink, now=NOW, record_builder=builder)
        self.assertEqual(sink.records["CB"]["events"], len(self.typical()) + 1)
        self.assertEqual(seen["recovered_host_time"], NOW)
        self.assertTrue(seen["journal_ref"].endswith("CB.jsonl"))

    def test_archive_false_keeps_journal_in_place(self) -> None:
        self.flight("K1", self.typical())
        result = finalize_interrupted("K1", data_dir=self.data, now=NOW, archive=False)
        self.assertIsNone(result.archived_path)
        self.assertEqual(result.record["evidence"]["journal"], "recorder/in-progress/K1.jsonl")
        [pending] = find_unfinished(self.data)
        self.assertEqual(pending.state, RecoveryState.CLOSED_NOT_ARCHIVED)


if __name__ == "__main__":
    unittest.main()
