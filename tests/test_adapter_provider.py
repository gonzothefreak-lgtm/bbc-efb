"""Regression tests for SimConnectProvider failure scope, queueing, retries and readback."""

from collections import deque
from datetime import datetime, timezone
from struct import pack, unpack
from ctypes import string_at
from unittest import TestCase

from bbc_core import CommandState, ConnectionState, Core, Event, Gap, Quality, SimVar, ValueType
from bbc_core.provider import CommandOutcome, Demand, IngressGap, Outcome, Reading, Signal
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import Period, ReceiveException, ReceiveId
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk, packet


class ProviderCase(FakeClock, TestCase):
    sdk_class = FakeSdk

    def setUp(self):
        self.start_clock()
        self.sdk = self.sdk_class()
        self.provider = SimConnectProvider(self.sdk)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)

    def calls(self, name):
        return self.sdk.matching_calls(name)


class FailureScopeTests(ProviderCase):
    """Findings A42 and A44: late or unmatched exceptions are logged, not fatal."""

    def test_late_exception_for_a_finished_command_is_logged(self):
        electrical = self.core.register_consumer(control_groups=("electrical",))
        self.core.connect()
        command = electrical.submit_command("aircraft.electrical.toggle_bus_connection", (2, 5))
        self.core.pump()
        self.advance(2)
        self.core.pump()
        self.assertIs(electrical.command_result(command.id).state, CommandState.UNVERIFIED)
        (_, sent), = self.calls("SimConnect_TransmitClientEvent_EX1")
        self.sdk.exception(sent)
        with self.assertLogs("bbc_core.simconnect", "WARNING") as logs:
            self.core.pump()
        self.assertIn("late SimConnect exception for finished command", logs.output[0])
        self.assertIs(electrical.connection().state, ConnectionState.CONNECTED)

    def test_unattributed_exception_is_logged(self):
        consumer = self.core.register_consumer()
        self.core.connect()
        self.sdk.exception(999_999)
        with self.assertLogs("bbc_core.simconnect", "WARNING") as logs:
            self.core.pump()
        self.assertIn("unattributed", logs.output[0])
        self.assertIs(consumer.connection().state, ConnectionState.CONNECTED)

    def test_teardown_exceptions_do_not_end_the_session(self):
        consumer = self.core.register_consumer()
        event = consumer.subscribe("sim.started")
        speed = consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        consumer.unsubscribe(event.id)
        consumer.unsubscribe(speed.id)
        self.core.pump()
        for name in ("SimConnect_UnsubscribeFromSystemEvent", "SimConnect_ClearDataDefinition"):
            self.sdk.exception(self.calls(name)[-1][1])
        self.sdk.exception(self.calls("SimConnect_RequestDataOnSimObject")[-1][1])
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            self.core.pump()
        self.assertIs(consumer.connection().state, ConnectionState.CONNECTED)

    def test_required_lifecycle_registration_failure_stays_fatal(self):
        inputs = self.core.register_consumer(control_groups=("aircraft_inputs",))
        self.core.connect()
        inputs.request_aircraft_inputs()
        self.core.pump()
        sent = next(packet_id for args, packet_id in self.calls("SimConnect_SubscribeToSystemEvent")
                    if args[1] == b"AircraftLoaded")
        self.sdk.exception(sent)
        self.core.pump()
        self.assertIs(inputs.connection().state, ConnectionState.ERROR)
        self.assertIn("lifecycle", inputs.connection().detail)

    def test_quit_stays_fatal(self):
        consumer = self.core.register_consumer()
        self.core.connect()
        self.sdk.quit()
        self.core.pump()
        self.assertIsNot(consumer.connection().state, ConnectionState.CONNECTED)

    def test_ledger_attributes_exceptions_long_after_a_count_window(self):
        # Many sends in between must not push a recent packet out of the ledger.
        electrical = self.core.register_consumer(control_groups=("electrical",))
        reader = self.core.register_consumer(diagnostics=True)
        self.core.connect()
        command = electrical.submit_command("aircraft.electrical.toggle_bus_connection", (2, 5))
        self.core.pump()
        (_, sent), = self.calls("SimConnect_TransmitClientEvent_EX1")
        self.advance(2)
        self.core.pump()
        readers = (reader, self.core.register_consumer(diagnostics=True), self.core.register_consumer(diagnostics=True))
        for index in range(1, 301):
            readers[index % 3].subscribe_simvar(f"GENERAL ENG RPM:{index}", "rpm", ValueType.NUMBER)
        self.core.pump()
        self.assertGreater(self.sdk.packet_id - sent, 512)
        self.sdk.exception(sent)
        with self.assertLogs("bbc_core.simconnect", "WARNING") as logs:
            self.core.pump()
        self.assertTrue(any("finished command" in line for line in logs.output))
        self.assertIs(electrical.command_result(command.id).state, CommandState.UNVERIFIED)


class QueueTests(ProviderCase):
    """Findings A43 and A46: back-pressure and non-evictable control updates."""

    def flood(self, count):
        consumer = self.core.register_consumer()
        speed = consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        ((args, _),) = self.calls("SimConnect_RequestDataOnSimObject")
        for index in range(count):
            self.sdk.data(args[0], pack("<d", float(index)))
        return consumer, speed

    def test_large_pump_limit_never_creates_a_self_inflicted_gap(self):
        consumer, speed = self.flood(1500)
        # Each drain also has a real-time budget (_DRAIN_TIME_BUDGET_S), so on a loaded machine
        # two pumps may not reach the end; keep pumping with the large limit until the SDK is empty.
        for _ in range(50):
            self.core.pump(limit=1024)
            if not self.sdk.incoming and not self.provider._queued():
                break
        self.assertFalse(self.sdk.incoming, "the flood should be fully drained")
        # Core's own consumer queue may report overflow; the provider must not.
        reasons = [item.reason for item in consumer.poll(limit=4096) if isinstance(item, Gap)]
        self.assertFalse([reason for reason in reasons if "Provider" in reason], reasons)
        self.assertEqual(consumer.latest(speed.id).value, 1499.0)

    def test_drain_stops_reading_when_limit_updates_are_waiting(self):
        self.flood(300)
        batch = self.provider.drain(100)
        self.assertEqual(len(batch), 100)
        self.assertFalse(any(isinstance(item, IngressGap) for item in batch))
        self.assertEqual(len(self.sdk.incoming), 200)

    def test_one_drain_has_a_time_budget_and_leaves_the_rest_queued(self):
        # Findings A111/A112: a large burst (for example a worldwide list) is
        # spread over several pumps instead of stalling one.
        from unittest.mock import patch
        from bbc_core import simconnect
        self.flood(50)
        with patch.object(simconnect, "_DRAIN_TIME_BUDGET_S", -1.0), patch.object(simconnect, "_DRAIN_MIN_PACKETS", 5):
            batch = self.provider.drain(256)
        self.assertEqual(len(batch), 5)
        self.assertEqual(len(self.sdk.incoming), 45)

    def test_control_updates_are_never_evicted(self):
        self.core.connect()
        self.provider._offer(CommandOutcome("s", "keep", Outcome.CONFIRMED, "kept"))
        signal = Signal("GROUND VELOCITY", "knots", ValueType.NUMBER)
        now = datetime.now(timezone.utc)
        for index in range(9000):
            self.provider._offer(Reading("s", signal, float(index), now, 0.0))
        batch = self.provider.drain(4096)
        self.assertIsInstance(batch[0], IngressGap)
        self.assertGreater(batch[0].dropped_updates, 0)
        self.assertIsInstance(batch[1], CommandOutcome)

    def test_baseline_loop_is_bounded_by_a_packet_budget(self):
        radio = self.core.register_consumer(control_groups=("radios",))
        self.flood(400)
        command = radio.submit_command("radio.com1.swap")
        self.core.pump(limit=1)
        result = radio.command_result(command.id)
        self.assertIs(result.state, CommandState.REJECTED)
        self.assertIn("backlog", result.detail)
        self.assertIs(radio.connection().state, ConnectionState.CONNECTED)


class FailingDefinitionSdk(FakeSdk):
    def __init__(self):
        super().__init__()
        self.refuse = {b"GROUND VELOCITY"}

    def call(self, name, handle, *args):
        super().call(name, handle, *args)
        if name == "SimConnect_AddToDataDefinition" and args[1] in self.refuse:
            self.exception(self.packet_id)


class RetryTests(ProviderCase):
    """Finding A92: failed registrations are retried with capped backoff."""

    sdk_class = FailingDefinitionSdk

    def test_failed_registration_is_retried_with_backoff_and_recovers(self):
        consumer = self.core.register_consumer()
        speed = consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        self.core.pump()
        self.assertIs(consumer.latest(speed.id).quality, Quality.UNAVAILABLE)
        self.assertEqual(len(self.calls("SimConnect_AddToDataDefinition")), 1)
        self.advance(1)
        self.core.pump()
        self.assertEqual(len(self.calls("SimConnect_AddToDataDefinition")), 1)
        self.advance(1.1)
        self.core.pump()
        self.core.pump()
        self.assertEqual(len(self.calls("SimConnect_AddToDataDefinition")), 2)
        self.advance(3)
        self.core.pump()
        self.assertEqual(len(self.calls("SimConnect_AddToDataDefinition")), 2)  # Backoff doubled to 4 s.
        self.sdk.refuse.clear()
        self.advance(1.5)
        self.core.pump()
        self.assertEqual(len(self.calls("SimConnect_AddToDataDefinition")), 3)
        args, _ = self.calls("SimConnect_RequestDataOnSimObject")[-1]
        self.sdk.data(args[0], pack("<d", 61.0))
        self.core.pump()
        self.assertEqual(consumer.latest(speed.id).value, 61.0)
        self.assertIs(consumer.connection().state, ConnectionState.CONNECTED)


class Float32Sdk(FakeSdk):
    def call(self, name, handle, *args):
        super().call(name, handle, *args)
        if name == "SimConnect_SetDataOnSimObject" and self.apply_write:
            # MSFS may store the value at float32 precision.
            self.throttle = unpack("<f", pack("<f", self.throttle))[0] + self.offset

    offset = 0.0


class ReadbackToleranceTests(ProviderCase):
    """Finding A91: float readback compares at the value's real precision."""

    sdk_class = Float32Sdk

    def write(self, value):
        engine = self.core.register_consumer(control_groups=("engine_controls",))
        self.core.connect()
        command = engine.submit_command("aircraft.engine1.set_throttle", value)
        for _ in range(3):
            self.core.pump()
        self.advance(2)
        self.core.pump()
        return engine.command_result(command.id)

    def test_float32_stored_value_confirms(self):
        self.assertIs(self.write(33.3).state, CommandState.CONFIRMED)

    def test_different_value_stays_uncertain_and_reports_the_readback(self):
        self.sdk.offset = 0.01
        result = self.write(33.3)
        self.assertIs(result.state, CommandState.UNCERTAIN)
        self.assertIn("Last readback", result.detail)


class SnappingRadioSdk(FakeSdk):
    def call(self, name, handle, *args):
        super().call(name, handle, *args)
        if name == "SimConnect_TransmitClientEvent" and self.apply_write and self.mapped[args[1]] == b"COM_STBY_RADIO_SET_HZ":
            self.radio[1] = 118_000_000 + round((args[2] - 118_000_000) / 25_000) * 25_000


class ComChannelTests(ProviderCase):
    """Finding A97: COM readback accepts the channel MSFS snaps the request to."""

    sdk_class = SnappingRadioSdk

    def tune(self, frequency):
        radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()
        command = radio.submit_command("radio.com1.set_standby", frequency)
        for _ in range(3):
            self.core.pump()
        self.advance(2)
        self.core.pump()
        return radio.command_result(command.id)

    def test_channel_name_confirms_against_the_stored_carrier(self):
        # An 8.33 kHz channel name the simulator stores as its 25 kHz carrier.
        result = self.tune(122_805_000)
        self.assertIs(result.state, CommandState.CONFIRMED)
        self.assertIn("122800000", result.detail)

    def test_snap_to_the_previous_standby_is_not_a_confirmation(self):
        self.sdk.radio = [118_100_000, 122_800_000]
        self.assertIs(self.tune(122_805_000).state, CommandState.UNCERTAIN)

    def test_exact_request_still_confirms(self):
        self.assertIs(self.tune(123_450_000).state, CommandState.CONFIRMED)


class NamesAndIntervalTests(ProviderCase):
    """Findings A93 and A96."""

    def test_system_events_use_canonical_sdk_spelling(self):
        consumer = self.core.register_consumer()
        for capability in ("sim.started", "sim.tick_6hz", "sim.mission_action", "sim.frame"):
            consumer.subscribe(capability)
        self.core.connect()
        self.core.pump()
        names = {args[1] for args, _ in self.calls("SimConnect_SubscribeToSystemEvent")}
        self.assertEqual(names, {b"SimStart", b"6Hz", b"CustomMissionActionExecuted", b"Frame"})

    def test_provider_reports_native_interval_granularity(self):
        self.assertEqual(SimConnectProvider.interval_granularity_ms, 1000)
        consumer = self.core.register_consumer()
        consumer.subscribe("aircraft.ground_speed", interval_ms=2000)
        self.core.connect()
        self.core.pump()
        ((args, _),) = self.calls("SimConnect_RequestDataOnSimObject")
        self.assertEqual(args[6], 1)  # One skipped period: every 2 s.


class PerformanceTests(ProviderCase):
    """Finding A141: a generous guard against quadratic pump regressions."""

    def test_hundred_signals_burst_pumps_quickly(self):
        from time import perf_counter
        consumer = self.core.register_consumer(diagnostics=True)
        for index in range(1, 101):
            consumer.subscribe_simvar(f"GENERAL ENG RPM:{index}", "rpm", ValueType.NUMBER)
        self.core.connect()
        self.core.pump()
        requests = [args[0] for args, _ in self.calls("SimConnect_RequestDataOnSimObject")]
        for second in range(5):
            for request in requests:
                self.sdk.data(request, pack("<d", float(second)))
        # The adapter's 20 ms drain time slice would make "all drained in one pump" depend on
        # machine load; lift it so this test measures only the per-packet cost (A141).
        from unittest import mock
        import bbc_core.simconnect as adapter
        started = perf_counter()
        with mock.patch.object(adapter, "_DRAIN_TIME_BUDGET_S", 60.0):
            self.core.pump(limit=1024)
        elapsed = perf_counter() - started
        self.assertLess(elapsed, 0.5)
        self.assertFalse(self.sdk.incoming)


class SystemEventNameTableTests(TestCase):
    """A93: every catalog system event has its SDK spelling in the provider's table."""

    def test_every_catalog_system_event_has_a_canonical_sdk_name(self):
        from bbc_core import catalog, simconnect

        names = set()
        for value in vars(catalog).values():
            if not isinstance(value, dict):
                continue
            for entry in value.values():
                signal = getattr(entry, "signal", None)
                if (signal is not None and getattr(signal, "event", False) and not signal.client_event
                        and signal.hardware_input is None and signal.input_id is None):
                    names.add(signal.name)
        self.assertTrue(names, "the catalog should list system events")
        missing = sorted(name for name in names if name not in simconnect._SYSTEM_EVENT_NAMES)
        self.assertEqual(missing, [])
