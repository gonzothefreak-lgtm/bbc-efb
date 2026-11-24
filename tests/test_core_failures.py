"""Provider failure boundaries, cleanup retry, facility limits and one-shot state freshness."""

from unittest import TestCase
from unittest.mock import patch

from bbc_core import ConnectionState, Core, FacilityState, Quality
from bbc_core.catalog import READS
from bbc_core.provider import ProviderError
from bbc_core.simulated import SimulatedProvider

from test_core_support import FacilitySimulatedProvider, FakeClock


class ConnectionBoundaryTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, periodic=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.consumer = self.core.register_consumer()

    def test_failed_cleanup_is_retried_by_connect_and_then_reconnects(self):
        first = self.core.connect().session_id
        with patch.object(self.provider, "disconnect", side_effect=ProviderError("close failed")), \
                self.assertLogs("bbc_core.core", "WARNING"):
            self.core.disconnect()
            again = self.core.connect()
        self.assertIs(again.state, ConnectionState.ERROR)
        self.assertIn("still unresolved", again.detail)
        reconnected = self.core.connect()
        self.assertIs(reconnected.state, ConnectionState.CONNECTED)
        self.assertNotEqual(reconnected.session_id, first)

    def test_native_fault_during_connect_ends_in_error_and_can_be_retried(self):
        with patch.object(self.provider, "connect", side_effect=OSError("exception: access violation")), \
                self.assertLogs("bbc_core.core", "ERROR"):
            failed = self.core.connect()
        self.assertIs(failed.state, ConnectionState.ERROR)
        self.assertIn("access violation", failed.detail)
        self.assertIs(self.core.connect().state, ConnectionState.CONNECTED)

    def test_interrupt_during_connect_rolls_back_and_propagates(self):
        with patch.object(self.provider, "connect", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.core.connect()
        self.assertIs(self.consumer.connection().state, ConnectionState.ERROR)
        self.assertIs(self.core.connect().state, ConnectionState.CONNECTED)

    def test_unexpected_provider_fault_in_pump_reports_error_instead_of_connected(self):
        self.consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        with patch.object(self.provider, "replace_demand", side_effect=TypeError("bad argument")), \
                self.assertLogs("bbc_core.core", "ERROR"):
            self.core.pump()
        connection = self.consumer.connection()
        self.assertIs(connection.state, ConnectionState.ERROR)
        self.assertIn("bad argument", connection.detail)
        self.assertIs(self.core.connect().state, ConnectionState.CONNECTED)

    def test_one_update_core_cannot_process_is_skipped_without_ending_the_session(self):
        speed = self.consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        original = self.core._receive_reading
        calls = []

        def flaky(reading):
            calls.append(reading)
            if len(calls) == 1:
                raise RuntimeError("decoder bug")
            return original(reading)

        self.clock.advance(1)
        self.provider.publish("aircraft.ground_speed", 5)
        self.provider.publish("aircraft.ground_speed", 6)
        with patch.object(self.core, "_receive_reading", side_effect=flaky), \
                self.assertLogs("bbc_core.core", "ERROR"):
            self.core.pump()
        self.assertIs(self.consumer.connection().state, ConnectionState.CONNECTED)
        self.assertEqual(self.consumer.latest(speed.id).value, 6.0)


class FacilityLimitTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = FacilitySimulatedProvider(clock=self.clock, periodic=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.dispatch = self.core.register_consumer()
        self.inflight = self.core.register_consumer()
        self.core.connect()

    def test_one_module_cannot_take_every_facility_slot(self):
        results = [self.dispatch.request_airport(icao) for icao in ("YSSY", "YMML", "YBBN", "YPPH", "YPAD")]
        self.assertEqual([r.state for r in results[:4]], [FacilityState.PENDING] * 4)
        self.assertIs(results[4].state, FacilityState.UNAVAILABLE)
        self.assertIn("This module", results[4].detail)
        self.assertIs(self.inflight.request_airport("NZAA").state, FacilityState.PENDING)

    def test_unanswered_dispatched_read_fails_at_core_deadline_and_frees_its_slot(self):
        pending = self.dispatch.request_airport("YSSY")
        self.core.pump()
        self.assertEqual(len(self.provider.facility_requests), 1)
        self.clock.advance(30)
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(pending.id).state, FacilityState.PENDING)
        self.clock.advance(6)
        with self.assertLogs("bbc_core.core", "WARNING"):
            self.core.pump()
        result = self.dispatch.facility_result(pending.id)
        self.assertIs(result.state, FacilityState.UNAVAILABLE)
        self.assertIn("no answer", result.detail)
        self.assertEqual(self.core._facility_reads, {})

    def test_pump_fault_after_dispatch_fails_reads_with_a_reason_instead_of_stranding_them(self):
        pending = self.dispatch.request_airport("YSSY")
        with patch.object(self.provider, "request_facility", side_effect=ValueError("bad query")), \
                self.assertLogs("bbc_core.core", "ERROR"):
            self.core.pump()
        result = self.dispatch.facility_result(pending.id)
        self.assertIs(result.state, FacilityState.UNAVAILABLE)
        self.assertIn("bad query", result.detail)
        self.assertEqual(self.core._facility_reads, {})


class SystemStateFreshnessTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, periodic=False)
        self.requests = []
        original = self.provider.request_system_state

        def record(signal):
            self.requests.append(signal.name)
            original(signal)

        self.provider.request_system_state = record
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.first = self.core.register_consumer()

    def test_late_subscriber_gets_a_fresh_reply_not_an_hours_old_one(self):
        self.first.subscribe("sim.loaded_aircraft_file")
        self.core.connect()
        self.core.pump()
        self.assertEqual(self.requests, ["AIRCRAFTLOADED"])
        self.clock.advance(7200)
        second = self.core.register_consumer().subscribe("sim.loaded_aircraft_file")
        self.core.pump()
        self.assertEqual(self.requests, ["AIRCRAFTLOADED", "AIRCRAFTLOADED"])
        self.assertTrue(second.id)

    def test_aircraft_change_invalidates_and_refreshes_the_loaded_aircraft_state(self):
        state = self.first.subscribe("sim.loaded_aircraft_file")
        self.core.connect()
        self.core.pump()
        self.assertTrue(any(item.signal == READS["sim.aircraft_loaded"].signal for item in self.provider.active_demand))
        self.assertIs(self.first.latest(state.id).quality, Quality.CURRENT)
        self.provider.set_value("sim.loaded_aircraft_file", "SimObjects\\Airplanes\\A320\\aircraft.cfg")
        self.provider.emit_event("sim.aircraft_loaded", "SimObjects\\Airplanes\\A320\\aircraft.cfg")
        self.core.pump()
        invalid = self.first.latest(state.id)
        self.assertIs(invalid.quality, Quality.UNAVAILABLE)
        self.assertIn("reported a change", invalid.reason)
        self.core.pump()
        self.assertEqual(self.requests, ["AIRCRAFTLOADED", "AIRCRAFTLOADED"])
        fresh = self.first.latest(state.id)
        self.assertIs(fresh.quality, Quality.CURRENT)
        self.assertIn("A320", fresh.value)

    def test_lifecycle_event_is_released_with_the_last_state_subscription(self):
        state = self.first.subscribe("sim.running")
        self.core.connect()
        self.core.pump()
        self.assertEqual({item.signal.name for item in self.provider.active_demand}, {"SIM"})
        self.assertEqual(len(self.provider.active_demand), 2)
        self.first.unsubscribe(state.id)
        self.core.pump()
        self.assertEqual(self.provider.active_demand, ())
