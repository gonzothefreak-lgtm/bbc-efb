"""Drive the Recorder through the real Core consumer and yearly store."""

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from bbc_core import Core
from bbc_core.simulated import SimulatedProvider

from bbc_efb.recorder import FlightRecorder
from bbc_efb.storage import FlightStore


class FlightReplay(SimulatedProvider):
    """Publish SDK variables on Core's requested signals."""

    def supports_signal(self, signal):
        return not signal.event or super().supports_signal(signal)

    def frame(self, seconds, *, ground=True, speed=0.0, brake=True, landing=0.0, pause=None,
              latitude=0.0, longitude=0.0, aircraft="Offline 747"):
        clock = (datetime(2027, 1, 2, tzinfo=timezone.utc)
                 - datetime(1, 1, 1, tzinfo=timezone.utc)).total_seconds() + seconds
        self.publish("aircraft.title", aircraft)
        self.publish("aircraft.on_ground", ground)
        self.publish("aircraft.ground_speed", speed)
        self.publish("aircraft.latitude", latitude)
        self.publish("aircraft.longitude", longitude)
        if pause is not None:
            self.publish("sim.pause_state", pause)
        values = {
            "ABSOLUTE TIME": clock,
            "BRAKE PARKING POSITION": brake,
            "PLANE TOUCHDOWN NORMAL VELOCITY": landing,
        }
        for demand in self.active_demand:
            if demand.signal.name in values:
                self._emit_reading(demand.signal, values[demand.signal.name])


class RecorderReplay:
    def __init__(self, store):
        self.provider = FlightReplay()
        self.core = Core(self.provider)
        self.recorder = FlightRecorder(self.core.register_consumer(), store,
                                       real_date=lambda: date(2026, 11, 25))
        self.core.connect()
        self.core.pump()

    def close(self):
        self.recorder.close()
        self.core.disconnect()

    def feed(self, seconds, **measurements):
        self.provider.frame(seconds, **measurements)
        self.core.pump()
        result = self.recorder.update()
        self.core.pump()
        return result

    def approach_and_park(self, *, landing_speed=3.0, pause=False):
        self.depart()
        self.feed(22, ground=False, speed=170, brake=False)
        if pause:
            self.feed(32, ground=False, speed=170, brake=False, pause=4)
            self.feed(42, ground=False, speed=170, brake=False, pause=0)
        self.feed(52, ground=False, speed=150, brake=False)
        self.feed(62, ground=False, speed=140, brake=False)
        self.feed(63, speed=130, brake=False, landing=landing_speed)
        self.feed(64, speed=120, brake=False, landing=landing_speed)
        self.feed(74, speed=12, brake=False, landing=landing_speed)
        for seconds in range(75, 79):
            self.feed(seconds, landing=landing_speed)
        return self.feed(79, landing=landing_speed)

    def depart(self):
        self.feed(0)
        self.feed(1, speed=3, brake=False)
        self.feed(2, speed=4, brake=False)
        self.feed(12, ground=False, speed=170, brake=False)


class FlightRecorderOfflineTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FlightStore(Path(temporary.name))
        self.replay = RecorderReplay(self.store)
        self.addCleanup(self.replay.close)

    def test_normal_flight_reopens_with_a_separate_real_date_and_fixed_measurements(self):
        flight = self.replay.approach_and_park()
        self.assertEqual(flight.source, "automatic")
        self.assertEqual(flight.fields.flight_date, "2026-11-25")
        self.assertEqual(flight.fields.departure, "UNKNOWN")
        self.assertEqual(flight.fields.arrival, "UNKNOWN")
        self.assertTrue(flight.recording.simulated)
        self.assertEqual(flight.recording.gate_out, "2027-01-02T00:00:01+00:00")
        self.assertEqual(flight.recording.takeoff, "2027-01-02T00:00:12+00:00")
        self.assertEqual(flight.recording.touchdown, "2027-01-02T00:01:03+00:00")
        self.assertEqual(flight.recording.gate_in, "2027-01-02T00:01:15+00:00")
        self.assertEqual(flight.recording.block_seconds, 74)
        self.assertEqual(flight.recording.airborne_seconds, 51)
        self.assertEqual(flight.recording.landing_rate_fpm, -180)
        reopened = FlightStore(self.store.root)
        self.assertEqual(reopened.listing(), ([flight], []))
        self.assertEqual(reopened.save_recording(flight.id, flight.fields, flight.recording), flight)

    def test_paused_clock_advancement_is_excluded_and_no_old_touchdown_is_reused(self):
        flight = self.replay.approach_and_park(landing_speed=0, pause=True)
        self.assertEqual(flight.recording.block_seconds, 54)
        self.assertEqual(flight.recording.airborne_seconds, 31)
        self.assertIsNone(flight.recording.landing_rate_fpm)

    def test_failed_final_write_can_be_retried_without_a_second_flight(self):
        save = self.store.save_recording

        def fail_completion(flight_id, fields, recording, **kwargs):
            if recording.gate_in is not None:
                raise OSError("Disk busy")
            return save(flight_id, fields, recording, **kwargs)

        with patch.object(self.store, "save_recording", side_effect=fail_completion):
            with self.assertRaisesRegex(OSError, "Disk busy"):
                self.replay.approach_and_park()
        self.assertEqual(self.store.listing()[0][0].status, "interrupted")
        flight = self.replay.recorder.update()
        self.assertIsNotNone(flight)
        self.assertIsNone(self.replay.recorder.update())
        self.assertEqual(self.store.listing(), ([flight], []))

    def test_connection_change_does_not_save_a_false_complete_flight(self):
        self.replay.feed(0)
        self.replay.feed(1, speed=4, brake=False)
        self.replay.feed(2, speed=4, brake=False)
        self.replay.core.disconnect()
        self.assertIsNone(self.replay.recorder.update())
        self.assertIn("session changed", self.replay.recorder.interrupted_reason)
        flights, unreadable = self.store.listing()
        self.assertEqual(unreadable, [])
        self.assertEqual(len(flights), 1)
        self.assertEqual(flights[0].status, "interrupted")
        self.assertIsNone(flights[0].recording.takeoff)
        self.assertIsNone(flights[0].recording.gaps[0].before)

    def test_restart_resumes_same_flight_and_keeps_gap_out_of_elapsed_time(self):
        self.replay.depart()
        self.replay.feed(22, ground=False, speed=140, brake=False)
        recorder = self.replay.recorder
        original_id = recorder.flight_id
        recorder.save_and_exit()
        self.replay.close()
        partial = self.store.get(original_id)
        self.store.save(replace(partial.fields, arrival="YSSY"), partial)

        reopened = RecorderReplay(FlightStore(self.store.root))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.recorder.status, "Interrupted")
        self.assertEqual(reopened.recorder.flight_id, original_id)
        reopened.feed(25, ground=False, speed=140, brake=False, longitude=2.0)
        self.assertFalse(reopened.recorder.can_resume)
        reopened.feed(27, ground=False, speed=140, brake=False, longitude=.001)
        self.assertTrue(reopened.recorder.can_resume)
        self.assertTrue(reopened.recorder.resume())
        self.assertEqual(reopened.recorder.status, "Active")
        reopened.core.pump()
        reopened.feed(30, ground=False, speed=140, brake=False)
        reopened.feed(35, speed=110, brake=False, landing=2)
        reopened.feed(36, speed=95, brake=False, landing=2)
        saved = None
        for second in range(37, 42):
            saved = reopened.feed(second, landing=2)
        self.assertEqual(saved.id, original_id)
        self.assertEqual(saved.status, "complete")
        self.assertEqual(saved.fields.arrival, "YSSY")
        self.assertIsNone(saved.checkpoint)
        self.assertEqual(saved.recording.airborne_seconds, 18)
        self.assertEqual(saved.recording.gaps[0].after, "2027-01-02T00:00:22+00:00")
        self.assertEqual(saved.recording.gaps[0].before, "2027-01-02T00:00:27+00:00")
        self.assertEqual(self.store.listing(), ([saved], []))

    def test_last_durable_checkpoint_can_be_kept_without_resuming_a_different_aircraft(self):
        self.replay.depart()
        original_id = self.replay.recorder.flight_id
        self.replay.close()  # Simulate process loss with just the last checkpoint on disk.
        reopened = RecorderReplay(FlightStore(self.store.root))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.recorder.status, "Interrupted")
        reopened.feed(14, ground=False, speed=140, brake=False, aircraft="Different aircraft")
        self.assertFalse(reopened.recorder.can_resume)
        reopened.recorder.keep_partial()
        partial = self.store.get(original_id)
        self.assertIsNone(partial.checkpoint)
        self.assertEqual(partial.status, "interrupted")
        self.assertEqual(partial.recording.gaps[0].reason, "App stopped between samples.")
        self.assertIsNone(partial.recording.touchdown)

    def test_reconnect_requires_two_compatible_samples_to_auto_resume(self):
        self.replay.depart()
        flight_id = self.replay.recorder.flight_id
        self.replay.core.disconnect()
        self.replay.recorder.update()
        self.assertEqual(self.replay.recorder.status, "Interrupted")
        self.replay.core.connect()
        self.replay.core.pump()
        self.replay.feed(14, ground=False, speed=150, brake=False)
        self.assertEqual(self.replay.recorder.status, "Interrupted")
        self.replay.feed(15, ground=False, speed=150, brake=False)
        self.assertEqual(self.replay.recorder.status, "Active")
        self.assertEqual(self.replay.recorder.flight_id, flight_id)
        self.assertEqual(self.store.get(flight_id).recording.gaps[0].before, "2027-01-02T00:00:15+00:00")

    def test_midflight_start_keeps_takeoff_unknown_and_records_observed_arrival(self):
        recorder = self.replay.recorder
        self.replay.feed(30, ground=False, speed=150, brake=False)
        self.assertTrue(recorder.start())
        self.replay.core.pump()
        self.replay.feed(31, ground=False, speed=140, brake=False)
        self.replay.feed(34, speed=120, brake=False, landing=2)
        self.replay.feed(35, speed=90, brake=False, landing=2)
        for second in range(36, 41):
            flight = self.replay.feed(second, landing=2)
        self.assertIsNone(flight.recording.gate_out)
        self.assertIsNone(flight.recording.takeoff)
        self.assertEqual(flight.recording.manual_start, "2027-01-02T00:00:30+00:00")
        self.assertEqual(flight.recording.airborne_seconds, 4)
        self.assertEqual(flight.recording.landing_rate_fpm, -120)
        self.assertEqual(flight.status, "complete")

    def test_manual_stop_survives_restart_and_rearms_only_after_parking(self):
        self.replay.depart()
        stopped = self.replay.recorder.stop()
        self.assertEqual(stopped.manual_hold, "airborne")
        self.assertIsNone(stopped.checkpoint)
        self.replay.close()
        reopened = RecorderReplay(FlightStore(self.store.root))
        self.addCleanup(reopened.close)
        reopened.feed(14, speed=90, brake=False)
        reopened.feed(15, speed=50, brake=False)
        reopened.feed(16)
        reopened.feed(17, speed=5, brake=False)
        reopened.feed(18, speed=5, brake=False)
        self.assertTrue(reopened.recorder.waiting_for_next_flight)
        self.assertTrue(reopened.recorder.can_start)
        self.assertIsNone(reopened.recorder.flight_id)
        for second in range(19, 24):
            reopened.feed(second)
        self.assertFalse(reopened.recorder.waiting_for_next_flight)
        reopened.feed(24, speed=5, brake=False)
        reopened.feed(25, speed=5, brake=False)
        self.assertIsNotNone(reopened.recorder.flight_id)
        self.assertNotEqual(reopened.recorder.flight_id, stopped.id)
        self.assertEqual(self.store.get(stopped.id).recording, stopped.recording)

    def test_failed_stop_save_retries_with_the_same_cutoff_and_manual_restart_remains_available(self):
        self.replay.depart()
        recorder = self.replay.recorder
        flight_id = recorder.flight_id
        with patch.object(self.store, "save_recording", side_effect=OSError("Disk busy")):
            with self.assertRaisesRegex(OSError, "Disk busy"):
                recorder.stop()
        self.assertFalse(recorder.can_stop)
        stopped = self.replay.feed(25, ground=False, speed=140, brake=False)
        self.assertEqual(stopped.id, flight_id)
        self.assertEqual(stopped.recording.manual_stop, "2027-01-02T00:00:12+00:00")
        self.assertTrue(recorder.start())
        self.assertNotEqual(recorder.flight_id, flight_id)
        self.assertIsNone(self.store.get(flight_id).manual_hold)
