"""RecorderService (contract C3) with the real FlightRecorder, journal and recovery, on a fake Core handle.

``FakeConsumerAPI`` (bbc_recorder.tape) stands in for Core so each test controls exactly what
arrives and when. The end-to-end runs through real Core are in test_service_acceptance.py.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bbc_core.contract import ConnectionState, SignalError, SnapshotPeriod
from bbc_recorder.journal import JournalLayout, read_journal
from bbc_recorder.model import EventKind
from bbc_recorder.service import (
    LIFECYCLE_EVENTS, STATES, RecorderConfig, RecorderService, RecorderStatus, SaveResult,
)
from bbc_recorder.tape import AIRPORTS, FakeConsumerAPI, TapeClock, absolute_time_s, tape_fields

T0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
SIM0 = datetime(2026, 11, 24, 14, 0, tzinfo=timezone.utc)
YSSY = AIRPORTS["YSSY"]


class ListSink:
    def __init__(self, fail=0):
        self.records = []
        self.fail = fail
        self.calls = 0

    def save_recorded_flight(self, record):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise OSError("disk full")
        json.dumps(record)
        for existing in self.records:
            if existing["flight_id"] == record["flight_id"]:
                return "rec-again"
        self.records.append(record)
        return f"rec-{len(self.records)}"


class Harness:
    def __init__(self, *, config=None, sink=None, data_dir=None, airports=(YSSY,), **api_kwargs):
        self.folder = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.folder.name) if data_dir is None else data_dir
        self.clock = TapeClock(T0)
        self.api = FakeConsumerAPI(clock=self.clock, fields=tape_fields(), airports=airports, **api_kwargs)
        self.sink = sink if sink is not None else ListSink()
        self.config = config or RecorderConfig(background_saves=False)
        self.service = RecorderService(self.api, self.sink, self.data_dir, None, self.clock.now,
                                       monotonic=self.clock.monotonic, config=self.config)
        self.sim_time = 100.0
        self.values = {
            "SIM ON GROUND": True, "GROUND VELOCITY": 0.0, "AIRSPEED INDICATED": 0.0, "VERTICAL SPEED": 0.0,
            "PLANE ALT ABOVE GROUND": 0.0, "PLANE ALTITUDE": 20.0, "PLANE LATITUDE": YSSY["lat"] + 0.002,
            "PLANE LONGITUDE": YSSY["lon"], "PLANE HEADING DEGREES TRUE": 160.0, "BRAKE PARKING POSITION": True,
            "PLANE TOUCHDOWN NORMAL VELOCITY": 3.0, "IS SLEW ACTIVE": False,
            "GENERAL ENG COMBUSTION:1": False, "GENERAL ENG COMBUSTION:2": False,
            "GENERAL ENG COMBUSTION:3": False, "GENERAL ENG COMBUSTION:4": False,
            "TITLE": "Test Twin", "ATC ID": "VH-TST", "ATC MODEL": "TT:ATCCOM.AC_MODEL_B738.0.text",
            "ATC TYPE": "Boeing", "ATC AIRLINE": "QFA", "ATC FLIGHT NUMBER": "12",
        }

    def close(self):
        self.service.close()
        self.folder.cleanup()

    @property
    def layout(self):
        return JournalLayout(self.data_dir)

    def connect(self, session="s1"):
        self.api.push_connection(ConnectionState.CONNECTED, session)
        self.service.tick()

    def frame(self, dt=1.0, *, paused=False, **values):
        self.clock.advance(dt)
        if not paused:
            self.sim_time += dt
        self.values.update({key.replace("_", " "): value for key, value in values.items()})
        self.api.push_frame(sim_time=self.sim_time, abs_time=absolute_time_s(SIM0) + self.sim_time, rate=1.0,
                            values=dict(self.values), frames=30, paused=paused)
        self.service.tick()
        return self.service.status()

    def frames(self, count, **values):
        status = None
        for _ in range(count):
            status = self.frame(**values)
        return status

    def taxi_out(self):
        self.frames(3)
        self.frames(3, **{"GENERAL ENG COMBUSTION:1": True, "GENERAL ENG COMBUSTION:2": True})
        self.frames(8, BRAKE_PARKING_POSITION=False, GROUND_VELOCITY=10.0)

    def takeoff(self):
        self.frames(4, GROUND_VELOCITY=80.0, AIRSPEED_INDICATED=80.0)
        self.frames(4, SIM_ON_GROUND=False, PLANE_ALT_ABOVE_GROUND=120.0, VERTICAL_SPEED=1500.0,
                    GROUND_VELOCITY=150.0)
        self.frames(3, PLANE_ALT_ABOVE_GROUND=2500.0)

    def land_and_park(self):
        self.frames(3, PLANE_ALT_ABOVE_GROUND=300.0, VERTICAL_SPEED=-700.0, GROUND_VELOCITY=140.0)
        self.frames(2, SIM_ON_GROUND=True, PLANE_ALT_ABOVE_GROUND=0.0, VERTICAL_SPEED=0.0,
                    PLANE_TOUCHDOWN_NORMAL_VELOCITY=-3.2, GROUND_VELOCITY=100.0)
        self.frames(4, GROUND_VELOCITY=15.0)
        self.frames(8, GROUND_VELOCITY=0.0, BRAKE_PARKING_POSITION=True,
                    **{"GENERAL ENG COMBUSTION:1": False, "GENERAL ENG COMBUSTION:2": False})

    def fly(self):
        self.connect()
        self.taxi_out()
        self.takeoff()
        self.land_and_park()


class ContractTests(unittest.TestCase):
    def test_c3_constructor_status_and_methods(self):
        params = list(inspect.signature(RecorderService.__init__).parameters)
        self.assertEqual(params[:6], ["self", "api", "sink", "data_dir", "rules", "clock"])
        names = [item.name for item in dataclasses.fields(RecorderStatus)]
        self.assertEqual(names[:10], ["state", "phase", "detail", "flight_id", "started_utc", "aircraft", "departure",
                                      "block_time_s", "airborne_time_s", "simulated"])
        self.assertEqual(set(STATES), {"unavailable", "inactive", "armed", "recording", "paused", "gap", "error",
                                       "saving"})
        self.assertTrue(RecorderStatus.__dataclass_params__.frozen)
        self.assertEqual(list(inspect.signature(RecorderService.manual_stop).parameters), ["self", "confirmed"])
        self.assertEqual(list(inspect.signature(RecorderService.save_and_close).parameters), ["self", "timeout_s"])
        for name in ("tick", "status", "manual_start", "recover_unfinished", "close"):
            self.assertTrue(callable(getattr(RecorderService, name)))

    def test_unavailable_without_core_or_data_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            no_core = RecorderService(None, ListSink(), Path(folder))
            self.assertEqual(no_core.status().state, "unavailable")
            self.assertIn("Core", no_core.status().detail)
            no_core.tick()
            self.assertFalse(no_core.manual_start().ok)
        no_folder = RecorderService(FakeConsumerAPI(), ListSink(), None)
        self.assertEqual(no_folder.status().state, "unavailable")
        self.assertIn("data folder", no_folder.status().detail)


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_read_only_subscriptions(self):
        h = self.h
        h.service.tick()
        calls = h.api.calls
        snapshots = [detail for name, detail in calls if name == "subscribe_snapshot"]
        self.assertEqual(snapshots, [("second", 1), ("second", 5)])  # recorder fields, identity
        events = {detail for name, detail in calls if name == "subscribe"}
        self.assertEqual(events, set(LIFECYCLE_EVENTS))
        self.assertNotIn("sim.paused", events)
        self.assertNotIn("sim.pause_changed", events)
        self.assertFalse(any(name.startswith("submit") for name, _ in calls))
        fields = h.api.active_snapshot_subscriptions[0].fields
        self.assertEqual(tuple(field.sdk_name for field in fields), tuple(f.sdk_name for f in h.service.fields))
        self.assertEqual(h.service.status().state, "inactive")

    def test_armed_once_connected(self):
        h = self.h
        h.connect()
        status = h.frame()
        self.assertEqual(status.state, "armed")
        self.assertTrue(status.simulated)
        self.assertTrue(status.can_start)
        self.assertFalse(status.can_stop)
        self.assertIn("B738", status.aircraft)  # from ATC MODEL, via bbc_recorder.attribution

    def test_snapshot_failure_is_an_error_and_retried(self):
        h = Harness(fail_snapshot_periods=(SnapshotPeriod.SECOND,))
        self.addCleanup(h.close)
        h.connect()
        self.assertEqual(h.service.status().state, "error")
        h.api._fail_periods.clear()
        h.clock.advance(11)
        h.frame()
        self.assertEqual(h.service.status().state, "armed")

    def test_subscription_ended_resubscribes_and_notes_it(self):
        h = self.h
        h.connect()
        h.taxi_out()
        h.api.end_snapshot_subscriptions("Aircraft changed inputs.")
        h.service.tick()
        self.assertEqual(len(h.api.active_snapshot_subscriptions), 2)
        status = h.frame()
        self.assertEqual(status.state, "recording")
        journal = read_journal(h.layout.journal_path(status.flight_id))
        notes = [e.data["note"] for e in journal.events if e.kind is EventKind.NOTE]
        self.assertIn("subscription_ended", notes)

    def test_signal_error_is_not_a_gap(self):
        h = self.h
        h.connect()
        h.taxi_out()
        main = h.service._main.id
        h.api._queue.append(SignalError(1, "PLANE ALT ABOVE GROUND failed.", "x", main))
        h.api._queue.append(SignalError(1, "Another module's SimVar failed.", "y", "not-ours"))
        status = h.frame()
        self.assertEqual(status.state, "recording")
        journal = read_journal(h.layout.journal_path(status.flight_id))
        self.assertFalse(any(e.kind is EventKind.GAP for e in journal.events))
        notes = [e.data for e in journal.events if e.kind is EventKind.NOTE]
        self.assertEqual([n["note"] for n in notes], ["signal_error"])
        self.assertEqual(notes[0]["subscription_id"], main)

    def test_queue_gap_reaches_the_flight(self):
        h = self.h
        h.connect()
        h.taxi_out()
        h.api.push_gap(7, "Consumer update queue lost data.")
        status = h.frame()
        journal = read_journal(h.layout.journal_path(status.flight_id))
        gaps = [e for e in journal.events if e.kind is EventKind.GAP]
        self.assertEqual([g.data["status"] for g in gaps], ["reported"])


class SamplingTests(unittest.TestCase):
    def test_switch_to_sim_frame_and_back_without_loss_or_duplicates(self):
        h = Harness()
        self.addCleanup(h.close)
        fed = []
        feed = h.service.recorder.feed

        def spy(sample):
            fed.append(sample.sim_time_s)
            return feed(sample)

        h.service.recorder.feed = spy
        h.fly()
        periods = [detail for name, detail in h.api.calls if name == "subscribe_snapshot"]
        self.assertIn(("sim_frame", 1), periods)
        self.assertEqual(periods[-1], ("second", 1))   # back to per second after landing
        self.assertEqual(fed, sorted(fed))
        self.assertEqual(len(fed), len(set(fed)))       # each simulator instant fed once
        steps = [b - a for a, b in zip(fed, fed[1:])]
        self.assertLessEqual(max(steps), 1.0 + 1e-9)    # no second lost while switching
        unsubscribed = [detail for name, detail in h.api.calls if name == "unsubscribe"]
        self.assertTrue(unsubscribed)

    def test_switch_that_never_delivers_falls_back(self):
        h = Harness(config=RecorderConfig(background_saves=False, switch_timeout_s=3.0))
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        for _ in range(6):  # takeoff roll: the state machine asks for high rate
            h.frame(GROUND_VELOCITY=80.0, AIRSPEED_INDICATED=80.0)
            if h.service._retiring is not None:
                break
        self.assertIsNotNone(h.service._retiring)
        self.assertIs(h.service._main.period, SnapshotPeriod.SIM_FRAME)
        # Paused frames reach per-second subscriptions only, so the frame cadence never delivers.
        for _ in range(5):
            h.frame(paused=True)
        self.assertIsNone(h.service._retiring)
        self.assertIs(h.service._main.period, SnapshotPeriod.SECOND)

    def test_refused_high_rate_keeps_logging_per_second(self):
        h = Harness(fail_snapshot_periods=(SnapshotPeriod.SIM_FRAME,))
        self.addCleanup(h.close)
        h.fly()
        self.assertEqual(len(h.sink.records), 1)


class FlightTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_automatic_flight_is_journaled_saved_and_archived(self):
        h = self.h
        states = []
        h.connect()
        for step in (h.taxi_out, h.takeoff, h.land_and_park):
            step()
            states.append(h.service.status().state)
        self.assertEqual(states, ["recording", "recording", "armed"])
        self.assertEqual(len(h.sink.records), 1)
        record = h.sink.records[0]
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["origin"], "recorder")
        block = record["logbook"]
        self.assertEqual(block["fields"]["departure"]["value"], "YSSY")
        self.assertEqual(block["fields"]["arrival"]["value"], "YSSY")
        self.assertEqual(block["fields"]["registration"]["value"], "VH-TST")
        self.assertEqual(block["fields"]["callsign"]["value"], "QFA12")
        self.assertEqual(block["touchdown_rate_fpm"], -192.0)
        self.assertIsNotNone(block["block_s"])
        archived = list((h.layout.archive_dir).rglob("*.jsonl"))
        self.assertEqual(len(archived), 1)
        journal = read_journal(archived[0])
        kinds = [e.kind for e in journal.events]
        self.assertIn(EventKind.SAMPLE, kinds)
        self.assertEqual([e.data["role"] for e in journal.events if e.kind is EventKind.ATTRIBUTION],
                         ["identity", "departure", "arrival"])
        self.assertEqual(h.service.status().last_flight.outcome, "complete")
        self.assertTrue(h.service.status().last_flight.saved)

    def test_status_paused_and_gap(self):
        h = self.h
        h.connect()
        h.taxi_out()
        h.api.push_event("sim.pause_state", 1)
        self.assertEqual(h.frame(paused=True).state, "paused")
        h.api.push_event("sim.pause_state", 0)
        self.assertEqual(h.frame().state, "recording")
        h.clock.advance(8)
        h.service.tick()
        self.assertEqual(h.service.status().state, "gap")  # quiet feed
        h.api.push_connection(ConnectionState.DISCONNECTED, None, "MSFS stopped responding.")
        h.service.tick()
        status = h.service.status()
        self.assertEqual(status.state, "gap")
        self.assertIn("MSFS stopped responding", status.detail)
        h.connect("s2")
        self.assertEqual(h.frame().state, "recording")

    def test_parked_flight_is_discarded_not_logged(self):
        h = self.h
        h.connect()
        h.frames(5)
        h.api.push_event("sim.stopped", 0)
        h.frame()
        self.assertEqual(h.sink.records, [])
        self.assertEqual(len(list(h.layout.discarded_dir.glob("*.jsonl"))), 1)
        self.assertEqual(h.service.status().last_flight.outcome, "discarded")
        self.assertEqual(h.service.status().state, "armed")

    def test_manual_stop_needs_confirmation(self):
        h = self.h
        h.connect()
        h.taxi_out()
        result = h.service.manual_stop()
        self.assertTrue(result.needs_confirmation)
        self.assertFalse(result.ok)
        self.assertEqual(h.service.status().state, "recording")
        result = h.service.manual_stop(confirmed=True)
        self.assertTrue(result.ok)
        h.frame()
        self.assertEqual([r["status"] for r in h.sink.records], ["abandoned"])
        self.assertIn("manual stop", h.service.status().detail)  # automatic logging is held

    def test_automatic_off_then_manual_start(self):
        h = Harness(config=RecorderConfig(background_saves=False, automatic=False))
        self.addCleanup(h.close)
        h.connect()
        status = h.frames(3)
        self.assertEqual(status.state, "inactive")
        self.assertIsNone(status.flight_id)
        result = h.service.manual_start()
        self.assertTrue(result.ok)
        status = h.frame()
        self.assertEqual(status.state, "recording")
        self.assertTrue(status.started_manually)
        journal = read_journal(h.layout.journal_path(status.flight_id))
        kinds = [e.kind for e in journal.events][:2]
        self.assertEqual(kinds, [EventKind.FLIGHT_STARTED, EventKind.MANUAL_START])
        self.assertEqual(journal.events[1].data["flight_id"], status.flight_id)

    def test_set_automatic_toggles(self):
        h = self.h
        h.connect()
        h.frame()
        self.assertIsNotNone(h.service.status().flight_id)
        h.service.set_automatic(False)
        status = h.frame()
        self.assertIsNone(status.flight_id)   # the armed, empty flight is dropped
        self.assertEqual(status.state, "inactive")
        h.service.set_automatic(True)
        self.assertEqual(h.frame().state, "armed")

    def test_suspected_diversion_is_kept_for_the_pilot(self):
        h = Harness(airports=(YSSY,))
        self.addCleanup(h.close)
        h.service.set_planned_destination("YWLM")
        h.fly()
        block = h.sink.records[0]["logbook"]
        self.assertEqual(block["fields"]["arrival"]["value"], "YSSY")  # where it actually landed
        self.assertEqual(block["arrival_assessment"]["verdict"], "diversion_suspected")
        self.assertTrue(block["arrival_assessment"]["needs_pilot_confirmation"])

    def test_unanswered_airport_lookup_ends_unknown(self):
        h = Harness(config=RecorderConfig(background_saves=False, airport_timeout_s=3.0, arrival_wait_s=5.0))
        self.addCleanup(h.close)
        from bbc_core.facilities import FacilityResult, FacilityState

        h.api.request_nearby_airports = lambda: FacilityResult("never", FacilityState.PENDING, h.api.source, "s1")
        h.connect()
        h.taxi_out()
        h.takeoff()
        h.frames(3, PLANE_ALT_ABOVE_GROUND=300.0, VERTICAL_SPEED=-700.0, GROUND_VELOCITY=140.0)
        h.frames(2, SIM_ON_GROUND=True, PLANE_ALT_ABOVE_GROUND=0.0, VERTICAL_SPEED=0.0,
                 PLANE_TOUCHDOWN_NORMAL_VELOCITY=-3.2, GROUND_VELOCITY=100.0)
        h.frames(4, GROUND_VELOCITY=15.0)
        states = [h.frame(GROUND_VELOCITY=0.0, BRAKE_PARKING_POSITION=True,
                          **{"GENERAL ENG COMBUSTION:1": False, "GENERAL ENG COMBUSTION:2": False}).state
                  for _ in range(12)]
        self.assertIn("saving", states)
        self.assertEqual(states[-1], "armed")
        fields = h.sink.records[0]["logbook"]["fields"]
        self.assertEqual(fields["departure"]["value"], "")
        self.assertEqual(fields["arrival"]["value"], "")
        self.assertIn("did not answer", fields["arrival"]["reason"])


class SaveTests(unittest.TestCase):
    def test_save_and_close_saves_the_open_flight_as_interrupted(self):
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        h.takeoff()
        result = h.service.save_and_close(5)
        self.assertIsInstance(result, SaveResult)
        self.assertTrue(result.ok, result.detail)
        record = h.sink.records[0]
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["ended"]["host_time"], None)
        self.assertEqual(record["interruption"]["to_is"], "recovery_time")
        self.assertIsNone(record["logbook"]["airborne_s"])
        self.assertEqual(h.service.status().state, "unavailable")
        self.assertEqual(h.service.save_and_close(5), result)  # idempotent
        h.service.tick()  # the next Core tick releases the subscriptions
        self.assertEqual(h.api.active_snapshot_subscriptions, ())
        self.assertEqual(h.api.active_event_subscriptions, ())

    def test_save_and_close_discards_an_armed_empty_flight(self):
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.frames(3)
        result = h.service.save_and_close(5)
        self.assertTrue(result.ok)
        self.assertEqual(h.sink.records, [])
        self.assertEqual(len(list(h.layout.discarded_dir.glob("*.jsonl"))), 1)
        self.assertEqual(list(h.layout.in_progress_dir.glob("*.jsonl")), [])

    def test_failed_save_is_an_error_and_is_retried(self):
        h = Harness(sink=ListSink(fail=1))
        self.addCleanup(h.close)
        h.fly()
        status = h.service.status()
        self.assertEqual(status.state, "error")
        self.assertIn("disk full", status.detail)
        self.assertFalse(status.last_flight.saved)
        self.assertEqual(len(list(h.layout.in_progress_dir.glob("*.jsonl"))), 2)  # kept + the re-armed one
        h.clock.advance(31)
        h.frame()
        self.assertEqual(len(h.sink.records), 1)
        self.assertEqual(h.service.status().state, "armed")
        self.assertTrue(h.service.status().last_flight.saved)

    def test_no_sink_keeps_the_journal(self):
        h = Harness(sink=None)
        h.sink = None
        h.service = RecorderService(h.api, None, h.data_dir, None, h.clock.now, monotonic=h.clock.monotonic,
                                    config=RecorderConfig(background_saves=False))
        self.addCleanup(h.close)
        h.fly()
        self.assertEqual(h.service.status().state, "error")
        self.assertIn("No Logbook", h.service.status().detail)
        self.assertEqual(list(h.layout.archive_dir.rglob("*.jsonl")), [])

    def test_journal_that_cannot_be_created_is_an_error(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        blocker = Path(folder.name) / "data"
        blocker.mkdir()
        (blocker / "recorder").write_text("not a folder")
        h = Harness(data_dir=blocker)
        self.addCleanup(h.close)
        h.connect()
        status = h.frame()
        self.assertEqual(status.state, "error")
        self.assertIn("journal", status.detail)
        h.api.push_event("sim.stopped", 0)   # the unjournalled, empty flight ends
        h.frame()
        self.assertEqual(h.service._failed, [])  # nothing to file, so no endless retries
        self.assertEqual(h.sink.records, [])

    def test_long_gap_saves_the_flight_as_interrupted(self):
        h = Harness(config=RecorderConfig(background_saves=False, max_open_gap_s=120.0))
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        h.takeoff()
        h.api.push_connection(ConnectionState.DISCONNECTED, None, "MSFS quit.")
        for _ in range(13):
            h.clock.advance(10)
            h.service.tick()
        self.assertEqual([r["status"] for r in h.sink.records], ["interrupted"])
        self.assertIn("No simulator data", h.sink.records[0]["close_detail"])

    def test_background_saves_and_status_from_another_thread(self):
        h = Harness(config=RecorderConfig(background_saves=True))
        self.addCleanup(h.close)
        seen = []
        stop = threading.Event()
        read_after_tick = threading.Event()

        def reader():
            while not stop.is_set():
                seen.append(h.service.status().state)
                read_after_tick.set()

        # Each tick waits until the other thread has read the status once more, so the reader
        # sees every phase even when the machine is busy (a free-running reader can miss the
        # short "recording" stretch). The reads still race freely with the saves and ticks.
        tick = h.service.tick

        def tick_then_let_the_reader_look(*args, **kwargs):
            read_after_tick.clear()
            result = tick(*args, **kwargs)
            read_after_tick.wait(2.0)
            return result

        h.service.tick = tick_then_let_the_reader_look
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            h.fly()
        finally:
            stop.set()
            thread.join()
            h.service.tick = tick
        result = h.service.save_and_close(10)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual([r["status"] for r in h.sink.records], ["complete"])
        self.assertTrue(set(seen) <= set(STATES))
        self.assertIn("recording", seen)


class RecoveryTests(unittest.TestCase):
    def test_unfinished_flight_from_an_earlier_run_is_saved_as_interrupted(self):
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        h.takeoff()
        flight = h.service._flight
        flight.journal.abandon()  # what a crash of BBC EFB leaves: no footer, lock released
        h.service._flight = None
        sink = ListSink()
        later = RecorderService(FakeConsumerAPI(clock=h.clock), sink, h.data_dir, None, h.clock.now,
                                monotonic=h.clock.monotonic, config=RecorderConfig(background_saves=False))
        self.assertEqual([f.flight_id for f in later.unfinished()], [flight.flight_id])
        saved = later.recover_unfinished()
        self.assertEqual([r.flight_id for r in saved], [flight.flight_id])
        record = sink.records[0]
        self.assertEqual(record["status"], "interrupted")
        self.assertIsNone(record["ended"]["sim_utc"])
        self.assertEqual(record["logbook"]["fields"]["departure"]["value"], "YSSY")
        self.assertEqual(later.unfinished(), [])
        self.assertEqual(later.recover_unfinished(), [])

    def test_recovery_discards_empty_journals_and_skips_live_ones(self):
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.frames(3)                       # an armed flight, journal open and locked
        sink = ListSink()
        other = RecorderService(FakeConsumerAPI(clock=h.clock), sink, h.data_dir, None, h.clock.now,
                                monotonic=h.clock.monotonic, config=RecorderConfig(background_saves=False))
        self.assertEqual(other.recover_unfinished(), [])
        self.assertEqual(len(list(h.layout.in_progress_dir.glob("*.jsonl"))), 1)  # live: left alone
        h.service._flight.journal.abandon()  # as a crash would leave it
        h.service._flight = None
        self.assertEqual(other.recover_unfinished(), [])
        self.assertEqual(sink.records, [])
        self.assertEqual(len(list(h.layout.discarded_dir.glob("*.jsonl"))), 1)

    def test_recovery_failure_is_reported_and_retried_later(self):
        h = Harness()
        self.addCleanup(h.close)
        h.connect()
        h.taxi_out()
        h.service._flight.journal.abandon()
        h.service._flight = None
        failing = RecorderService(FakeConsumerAPI(clock=h.clock), ListSink(fail=1), h.data_dir, None, h.clock.now,
                                  monotonic=h.clock.monotonic, config=RecorderConfig(background_saves=False))
        self.assertEqual(failing.recover_unfinished(), [])
        self.assertTrue(failing.recovery_errors)
        self.assertEqual(len(failing.recover_unfinished()), 1)


if __name__ == "__main__":
    unittest.main()
