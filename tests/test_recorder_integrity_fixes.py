"""Regression tests for four Flight Recorder integrity bugs found in review.

1. A journal write that fails before any byte reaches the disk (or a line refused as too
   large) must not leave a record that says its evidence is clean.
2. The reader's bound on non-sample events must never drop flight milestones (TAKEOFF,
   BLOCK_ON, FLIGHT_ENDED, ...), so a long flight full of notes is still logged correctly
   and is not discarded at startup.
3. A crash during the post-block-on arrival wait must recover a completed flight as
   complete, not as "interrupted before the flight ended".
4. After a cadence switch, a simulator-time reset must not make the service drop every
   sample of the new subscription.
"""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bbc_core.contract import SignalError, Snapshot, SnapshotPeriod, Source
from bbc_core.facilities import FacilityResult, FacilityState
from bbc_recorder import journal as jmod
from bbc_recorder import recovery
from bbc_recorder import service as svc_mod
from bbc_recorder.journal import (
    FsyncPolicy, IssueKind, Journal, JournalError, JournalLayout, JournalWriteError, MAX_NOTABLE_EVENTS,
    Outcome, read_journal,
)
from bbc_recorder.model import EventKind, Phase, RecorderEvent
from bbc_recorder.records import build_recorded_flight
from bbc_recorder.service import RecorderConfig, RecorderService
from tests.test_service_unit import Harness, ListSink

T0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
FID = "20261124T090000Z-aaaaaaaaaaaa"
QUIET = FsyncPolicy(interval_s=None, on_kinds=frozenset(), on_phase_change=False)


def ev(kind: EventKind, phase: Phase, i: int, **data: object) -> RecorderEvent:
    return RecorderEvent(kind, phase, T0 + timedelta(seconds=i), float(i), T0 + timedelta(seconds=i),
                         {"flight_id": FID, **data})


def _no_space(fd: int, data: bytes) -> int:
    raise OSError(errno.ENOSPC, "No space left on device")


class _TempLayout(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.layout = JournalLayout(Path(tmp.name))

    def create(self) -> Journal:
        journal = Journal.create(self.layout, FID, app_version="t", started_host_time=T0, policy=QUIET)
        self.addCleanup(journal.abandon)
        return journal


# --------------------------------------------------------------------------- 1. lost writes


class LostWriteTests(_TempLayout):
    def test_zero_byte_failure_not_retried_is_reported_missing_and_counted_in_the_footer(self) -> None:
        j = self.create()
        j.append(ev(EventKind.FLIGHT_STARTED, Phase.PREFLIGHT, 0))
        with mock.patch.object(jmod.os, "write", side_effect=_no_space):
            with self.assertRaises(JournalWriteError):
                j.append(ev(EventKind.TAKEOFF, Phase.AIRBORNE, 1))
        j.append(ev(EventKind.TOUCHDOWN, Phase.LANDING_ROLL, 2))  # a different event: the takeoff is lost
        self.assertEqual(j.lost_lines, 1)
        j.close(Outcome.COMPLETE)
        result = read_journal(j.path)
        self.assertFalse(result.clean)
        self.assertEqual([i.kind for i in result.issues], [IssueKind.MISSING_LINES])
        self.assertEqual(result.lost_lines, 1)
        self.assertEqual(result.footer["lost_lines"]["kinds"], {"takeoff": 1})

    def test_loss_as_the_final_line_is_still_in_the_footer(self) -> None:
        j = self.create()
        with mock.patch.object(jmod.os, "write", side_effect=_no_space):
            with self.assertRaises(JournalWriteError):
                j.append(ev(EventKind.BLOCK_ON, Phase.TAXI_IN, 1))
        j.close(Outcome.COMPLETE)
        result = read_journal(j.path)
        self.assertEqual(result.lost_lines, 1)
        self.assertFalse(result.clean)  # the footer's seq skips the lost line's seq

    def test_oversize_line_is_counted_as_lost(self) -> None:
        j = self.create()
        with self.assertRaises(JournalError):
            j.append(ev(EventKind.NOTE, Phase.AIRBORNE, 1, blob="x" * (jmod.MAX_LINE_BYTES + 10)))
        j.append(ev(EventKind.NOTE, Phase.AIRBORNE, 2))
        j.close(Outcome.COMPLETE)
        record = recovery.build_record(read_journal(j.path), flight_id=FID, journal_ref="x",
                                       journal_sha256=None, recovered_host_time=None)
        self.assertFalse(record["integrity"]["clean"])
        self.assertEqual(record["integrity"]["lost_lines"], 1)
        self.assertEqual(record["integrity"]["issues"][0]["kind"], "write_failed")

    def test_retrying_the_same_event_is_not_a_loss(self) -> None:
        j = self.create()
        takeoff = ev(EventKind.TAKEOFF, Phase.AIRBORNE, 1)
        with mock.patch.object(jmod.os, "write", side_effect=_no_space):
            with self.assertRaises(JournalWriteError):
                j.append(takeoff)
        j.append(takeoff)
        j.close(Outcome.COMPLETE)
        result = read_journal(j.path)
        self.assertTrue(result.clean)
        self.assertEqual(result.lost_lines, 0)
        self.assertNotIn("lost_lines", result.footer)

    def test_service_flight_with_a_lost_takeoff_is_not_labelled_clean(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        real = Journal.append
        state = {"done": False}

        def append(journal: Journal, event: RecorderEvent) -> int:
            if event.kind is EventKind.TAKEOFF and not state["done"]:
                state["done"] = True
                with mock.patch.object(jmod.os, "write", side_effect=_no_space):
                    return real(journal, event)
            return real(journal, event)

        with mock.patch.object(Journal, "append", append):
            h.takeoff()
            h.land_and_park()
            h.frames(20)
        record = h.sink.records[-1]
        self.assertEqual(record["status"], "complete")
        self.assertNotIn("takeoff", record["milestones"])
        integrity = record["integrity"]
        self.assertFalse(integrity["clean"])
        self.assertGreaterEqual(integrity["issue_count"], 1)
        self.assertEqual(integrity["lost_lines"], 1)
        self.assertEqual(integrity["lost_kinds"], {"takeoff": 1})
        self.assertIn("No space left", integrity["writer_error"])
        self.assertIn("Evidence is missing", record["logbook"]["summary_text"])

    def _interrupted_after_a_lost_line(self, *, note_fails: bool) -> dict:
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        h.takeoff()
        flight = h.service._flight
        with mock.patch.object(jmod.os, "write", side_effect=_no_space):
            with self.assertRaises(JournalWriteError):
                flight.journal.append(ev(EventKind.BOUNCE, Phase.AIRBORNE, 1))
        real = Journal.append

        def append(journal: Journal, event: RecorderEvent) -> int:
            if note_fails and event.data.get("note") == recovery.LOST_LINES_NOTE:
                with mock.patch.object(jmod.os, "write", side_effect=_no_space):
                    return real(journal, event)
            return real(journal, event)

        with mock.patch.object(Journal, "append", append):
            h.service.save_and_close()  # Save & Exit: the footer is written later, by recovery
        record = h.sink.records[-1]
        self.assertEqual(record["status"], "interrupted")
        self.assertFalse(record["integrity"]["clean"])
        return record

    def test_interrupted_flight_carries_the_lost_count_in_a_note(self) -> None:
        record = self._interrupted_after_a_lost_line(note_fails=False)
        self.assertEqual(record["integrity"]["lost_lines"], 1)
        self.assertEqual(record["integrity"]["lost_kinds"], {"bounce": 1})

    def test_interrupted_flight_carries_the_lost_count_when_the_note_also_fails(self) -> None:
        record = self._interrupted_after_a_lost_line(note_fails=True)
        self.assertEqual(record["integrity"]["lost_lines"], 2)


class LogbookSinkIntegrityTests(_TempLayout):
    def _record(self, *, lose_takeoff: bool, aircraft_field: dict | None = None) -> dict:
        j = self.create()
        j.append(ev(EventKind.FLIGHT_STARTED, Phase.PREFLIGHT, 0, trigger="automatic"))
        if aircraft_field is not None:
            j.append(ev(EventKind.ATTRIBUTION, Phase.PREFLIGHT, 1, role="identity", fields={"aircraft": aircraft_field}))
        j.append(ev(EventKind.BLOCK_OFF, Phase.TAXI_OUT, 2))
        if lose_takeoff:
            with mock.patch.object(jmod.os, "write", side_effect=_no_space):
                with self.assertRaises(JournalWriteError):
                    j.append(ev(EventKind.TAKEOFF, Phase.AIRBORNE, 3))
        j.append(ev(EventKind.TOUCHDOWN, Phase.LANDING_ROLL, 4))
        j.close(Outcome.ABANDONED)
        return recovery.finalize_closed(self.layout, FID, archive=False, record_builder=build_recorded_flight).record

    def test_sink_keeps_the_lost_line_evidence(self) -> None:
        from bbc_efb.recorder_sink import LogbookSink
        record = self._record(lose_takeoff=True)
        _fields, _origins, evidence, _reason = LogbookSink(lambda: None).convert(record)
        integrity = evidence.extra["integrity"]
        self.assertFalse(integrity["clean"])
        self.assertEqual(integrity["lost_lines"], 1)
        self.assertEqual(integrity["lost_kinds"], {"takeoff": 1})
        self.assertTrue(evidence.extra["evidence_warnings"])
        self.assertIn("Evidence is missing", evidence.summary)

    def test_low_confidence_aircraft_is_flagged_for_confirmation(self) -> None:
        from bbc_efb.recorder_sink import LogbookSink
        record = self._record(lose_takeoff=False, aircraft_field={
            "value": "Kodiak 100 Wheels Asobo", "provenance": "derived", "status": "needs_confirmation",
            "confidence": "low", "reason": "From the aircraft title; please confirm."})
        logbook = record["logbook"]
        self.assertEqual(logbook["needs_confirmation"], ["aircraft"])
        self.assertIn("Aircraft type needs confirmation", logbook["summary_text"])
        fields, _origins, evidence, _reason = LogbookSink(lambda: None).convert(record)
        self.assertEqual(fields.aircraft, "Kodiak 100 Wheels Asobo")
        self.assertEqual(evidence.extra["needs_confirmation"], ["aircraft"])
        self.assertIn("needs confirmation", evidence.summary)

    def test_known_fields_are_not_flagged(self) -> None:
        record = self._record(lose_takeoff=False, aircraft_field={
            "value": "B738", "provenance": "observed", "status": "known", "confidence": "high", "reason": "ATC MODEL"})
        self.assertEqual(record["logbook"]["needs_confirmation"], [])
        self.assertEqual(record["logbook"]["evidence_warnings"], [])
        self.assertTrue(record["integrity"]["clean"])


# --------------------------------------------------------------------------- 2. notable cap


class NotableBoundTests(_TempLayout):
    def _long_noisy_journal(self, *, notes_before_block_off: bool) -> None:
        j = self.create()
        j.append(ev(EventKind.FLIGHT_STARTED, Phase.PREFLIGHT, 0, trigger="automatic"))
        i = 1
        if notes_before_block_off:
            for _ in range(MAX_NOTABLE_EVENTS + 5):
                j.append(ev(EventKind.NOTE, Phase.PREFLIGHT, i, note="signal_error"))
                i += 1
        j.append(ev(EventKind.BLOCK_OFF, Phase.TAXI_OUT, i, lat=0.0, lon=0.0))
        j.append(ev(EventKind.TAKEOFF, Phase.AIRBORNE, i + 1))
        i += 2
        if not notes_before_block_off:
            for _ in range(MAX_NOTABLE_EVENTS + 5):
                j.append(ev(EventKind.NOTE, Phase.AIRBORNE, i, note="signal_error"))
                i += 1
            j.append(ev(EventKind.TOUCHDOWN, Phase.LANDING_ROLL, i, rate_fpm=-150.0))
            j.append(ev(EventKind.BLOCK_ON, Phase.TAXI_IN, i + 300))
            j.append(ev(EventKind.FLIGHT_ENDED, Phase.COMPLETE, i + 301, outcome="complete", loggable=True,
                        summary={"outcome": "complete", "block_off": {"x": 1}, "block_on": {"x": 1},
                                 "block_s": 20400.0}))
            j.close(Outcome.COMPLETE)
        else:
            j.abandon()

    def test_milestones_after_many_notes_are_kept(self) -> None:
        self._long_noisy_journal(notes_before_block_off=False)
        result = read_journal(self.layout.journal_path(FID), keep_events=False)
        kinds = [e.kind for e in result.notable_events]
        for kind in (EventKind.TAKEOFF, EventKind.TOUCHDOWN, EventKind.BLOCK_ON, EventKind.FLIGHT_ENDED):
            self.assertIn(kind, kinds)
        self.assertEqual(result.notable_omitted, 5)
        self.assertEqual(result.notable_omitted_by_kind, {"note": 5})
        self.assertLessEqual(kinds.count(EventKind.NOTE), MAX_NOTABLE_EVENTS)
        # Chronological order is kept.
        times = [e.host_time for e in result.notable_events]
        self.assertEqual(times, sorted(times))
        sink = ListSink()
        recovery.finalize_closed(self.layout, FID, sink=sink, record_builder=build_recorded_flight)
        record = sink.records[0]
        self.assertEqual(record["logbook"]["block_s"], 20400.0)
        self.assertEqual(record["logbook"]["fields"]["minutes"]["value"], 340)
        self.assertIsNotNone(record["logbook"]["on_block"])
        self.assertEqual(record["integrity"]["notes_omitted"], 5)

    def test_recovery_does_not_discard_a_flight_whose_block_off_follows_many_notes(self) -> None:
        self._long_noisy_journal(notes_before_block_off=True)
        h = Harness(data_dir=self.layout.data_dir)
        self.addCleanup(h.close)
        saved = h.service.recover_unfinished()
        self.assertEqual(len(saved), 1)
        self.assertEqual(h.sink.records[0]["status"], "interrupted")
        self.assertFalse((self.layout.discarded_dir / f"{FID}.jsonl").exists())

    def test_signal_errors_are_coalesced_per_capability(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        main = h.service._main.id
        for _ in range(50):
            h.api._queue.append(SignalError(1, "PLANE ALT ABOVE GROUND failed.", "x", main))
            h.frame(0.1)
        h.frame(61.0)
        h.api._queue.append(SignalError(1, "PLANE ALT ABOVE GROUND failed.", "x", main))
        status = h.frame(0.1)
        notes = [e.data for e in read_journal(h.layout.journal_path(status.flight_id)).events
                 if e.kind is EventKind.NOTE and e.data.get("note") == "signal_error"]
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0]["repeats_not_journalled"], 0)
        self.assertEqual(notes[1]["repeats_not_journalled"], 49)


# --------------------------------------------------------------------------- 3. crash while finishing


class CrashWhileFinishingTests(unittest.TestCase):
    def test_flight_ended_journal_without_footer_recovers_as_complete(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        h.takeoff()

        def slow():
            rid = h.api._next_id("fac")
            pending = FacilityResult(rid, FacilityState.PENDING, h.api.source, h.api._session())
            h.api._facility[rid] = pending
            return pending

        h.api.request_nearby_airports = slow
        h.land_and_park()
        finishing = dict(h.service._finishing)
        self.assertEqual(len(finishing), 1)
        for flight in finishing.values():
            flight.journal.abandon()  # the process dies: no footer
        h.service._finishing.clear()
        sink = ListSink()
        service = RecorderService(h.api, sink, h.data_dir, None, h.clock.now, monotonic=h.clock.monotonic,
                                  config=RecorderConfig(background_saves=False))
        saved = service.recover_unfinished()
        self.assertEqual([r.outcome for r in saved], [Outcome.COMPLETE])
        record = sink.records[-1]
        self.assertEqual(record["status"], "complete")
        self.assertIsNone(record["interruption"])
        self.assertIsNotNone(record["ended"]["host_time"])
        self.assertEqual(record["ended"]["final_phase"], "complete")
        logbook = record["logbook"]
        self.assertFalse(any(g["kind"] == "interrupted" for g in logbook["gaps"]))
        self.assertNotIn("interrupted", logbook["fields"]["arrival"]["reason"])
        self.assertTrue(logbook["summary_text"].startswith("Completed flight"))
        notes = [m["data"].get("note") for m in record["milestones"].get("note", [])]
        self.assertIn(recovery.FINISH_INTERRUPTED_NOTE, notes)
        service.close()

    def test_journal_without_flight_ended_is_still_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = JournalLayout(Path(tmp))
            j = Journal.create(layout, FID, app_version="t", started_host_time=T0, policy=QUIET)
            j.append(ev(EventKind.BLOCK_OFF, Phase.TAXI_OUT, 1))
            j.abandon()
            result = recovery.finalize_interrupted(FID, data_dir=layout, now=T0 + timedelta(hours=1))
            self.assertEqual(result.outcome, Outcome.INTERRUPTED)
            self.assertIsNotNone(result.record["interruption"])


# --------------------------------------------------------------------------- 4. overlap gate


class OverlapGateTests(unittest.TestCase):
    def _service(self):
        service = object.__new__(svc_mod.RecorderService)
        fed: list[float] = []
        service._identity_sub = "identity"
        service._flight = None
        service._config = RecorderConfig()
        service._feed = lambda sample: fed.append(sample.sim_time_s)
        service._mono = lambda: 0.0
        service._unsubscribe = lambda _id: None
        service._main = svc_mod._Sub("A", SnapshotPeriod.SECOND, 1, 0.0)
        service._retiring = None
        service._last_fed = None
        return service, fed

    @staticmethod
    def _snap(sub_id: str, t: float) -> Snapshot:
        return Snapshot(sub_id, (), (), t, 63e9 + t, 1.0, Source("x", True), "sess", T0)

    def test_new_subscription_is_fed_after_a_simulator_time_reset(self) -> None:
        service, fed = self._service()
        service._on_snapshot(self._snap("A", 5000.0))
        service._retiring, service._main = service._main, svc_mod._Sub("B", SnapshotPeriod.SIM_FRAME, 1, 0.0)
        for i in range(120):
            service._on_snapshot(self._snap("B", 10.0 + i / 30))
        self.assertEqual(len(fed), 121)
        self.assertEqual(service._last_fed[1], "B")

    def test_true_overlap_duplicates_are_still_skipped(self) -> None:
        service, fed = self._service()
        service._on_snapshot(self._snap("A", 5000.0))
        service._retiring, service._main = service._main, svc_mod._Sub("B", SnapshotPeriod.SIM_FRAME, 1, 0.0)
        service._on_snapshot(self._snap("B", 4999.5))   # already covered by A's 5000 s sample
        service._on_snapshot(self._snap("B", 5000.0))
        service._on_snapshot(self._snap("B", 5000.1))
        self.assertEqual(fed, [5000.0, 5000.1])


if __name__ == "__main__":
    unittest.main()
