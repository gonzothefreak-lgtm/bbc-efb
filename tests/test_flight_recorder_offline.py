"""Drive the Recorder through the real Core consumer and yearly store."""

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

    def frame(self, seconds, *, ground=True, speed=0.0, brake=True, landing=0.0, pause=None):
        clock = (datetime(2027, 1, 2, tzinfo=timezone.utc)
                 - datetime(1, 1, 1, tzinfo=timezone.utc)).total_seconds() + seconds
        self.publish("aircraft.title", "Offline 747")
        self.publish("aircraft.on_ground", ground)
        self.publish("aircraft.ground_speed", speed)
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
        self.feed(0)
        self.feed(1, speed=3, brake=False)
        self.feed(2, speed=4, brake=False)
        self.feed(12, ground=False, speed=170, brake=False)
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
        with patch.object(self.store, "_write_year", side_effect=OSError("Disk busy")):
            with self.assertRaisesRegex(OSError, "Disk busy"):
                self.replay.approach_and_park()
        self.assertEqual(self.store.listing(), ([], []))
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
        self.assertEqual(self.store.listing(), ([], []))
