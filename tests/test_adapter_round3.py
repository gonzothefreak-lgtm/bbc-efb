"""Round-3 adapter regressions: snapshot exclusions, registration cleanup, event lanes,
COM decode and spacing (contract C6), Input Event priming (A113), catalog restart (A38),
provider timing attributes (C5), native error wrapping (A31/A81) and the dispatch default.
"""

import ctypes
from datetime import datetime, timezone
from pathlib import Path
from struct import pack
from unittest import TestCase

from bbc_core import CommandState, ConnectionState, Core, InputCatalogState, ValueType
from bbc_core.contract import SnapshotPeriod
from bbc_core.provider import Demand, ProviderError, Reading, Signal, SnapshotFrame, SnapshotSpec
from bbc_core import simconnect_sdk
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import DataType, Period, ReceiveId, ReceiveSimObjectDataPrefix
from bbc_core.simconnect_sdk import SimConnectSdk
from test_adapter_support import FakeClock
from test_aircraft_inputs_offline import INPUT_HASH, FakeInputSdk
from test_simconnect_provider_offline import FakeSdk, packet


LATITUDE = Signal("PLANE LATITUDE", "degrees", ValueType.NUMBER)
LONGITUDE = Signal("PLANE LONGITUDE", "degrees", ValueType.NUMBER)
ON_GROUND = Signal("SIM ON GROUND", "bool", ValueType.BOOLEAN)


def snapshot_signal(*fields, changed_only=False):
    return Signal("SNAPSHOT", None, ValueType.SNAPSHOT,
                  snapshot=SnapshotSpec(fields, SnapshotPeriod.SECOND, 1, changed_only))


class SelectiveSdk(FakeSdk):
    """Refuses named SimVars (async exception) and, optionally, snapshot requests."""

    def __init__(self):
        super().__init__()
        self.refuse = set()
        self.refuse_requests = 0
        self.raise_request = 0

    def call(self, name, handle, *args):
        if name == "SimConnect_RequestDataOnSimObject" and self.raise_request and args[3] != Period.NEVER:
            self.raise_request -= 1
            self.packet_id += 1
            self.calls.append((name, args, self.packet_id))
            raise ProviderError("SimConnect_RequestDataOnSimObject failed with HRESULT 0x80004005.")
        super().call(name, handle, *args)
        if name == "SimConnect_AddToDataDefinition" and args[1] in self.refuse:
            self.exception(self.packet_id)
        elif (name == "SimConnect_RequestDataOnSimObject" and self.refuse_requests
              and args[3] not in (Period.NEVER, Period.ONCE)):
            self.refuse_requests -= 1
            self.exception(self.packet_id)


class AdapterCase(FakeClock, TestCase):
    sdk_class = SelectiveSdk

    def setUp(self):
        self.start_clock()
        self.sdk = self.sdk_class()
        self.provider = SimConnectProvider(self.sdk)
        self.provider.connect("session")
        self.addCleanup(self.provider.disconnect)

    def demand(self, *signals):
        self.provider.replace_demand(tuple(Demand(signal, None if signal.snapshot else 1000) for signal in signals))

    def names(self, calls=None):
        return [args[1] for args, _ in (calls or self.sdk.matching_calls("SimConnect_AddToDataDefinition"))]

    def readings(self, batch):
        return [item for item in batch if isinstance(item, Reading)]


class SnapshotExclusionTests(AdapterCase):
    """A snapshot rebuild or retry remembers the fields SimConnect refused."""

    def test_two_refused_fields_cost_one_rebuild(self):
        self.sdk.refuse = {b"PLANE LONGITUDE", b"SIM ON GROUND"}
        self.demand(snapshot_signal(LATITUDE, LONGITUDE, ON_GROUND))
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            self.provider.drain(16)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_ClearDataDefinition")), 1)
        request, _ = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")[-1]
        self.assertEqual([name for name, _, _ in self.sdk.definitions[request[1]]],
                         [b"PLANE LATITUDE", b"SIMULATION TIME", b"ABSOLUTE TIME", b"SIMULATION RATE"])

    def test_exception_for_an_earlier_definition_still_excludes_its_field(self):
        self.demand(snapshot_signal(LATITUDE, LONGITUDE, ON_GROUND))
        first = self.sdk.matching_calls("SimConnect_AddToDataDefinition")
        self.sdk.exception(first[1][1])  # LONGITUDE in the first definition.
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            self.provider.drain(16)
        self.sdk.exception(first[2][1])  # ON_GROUND, still naming the replaced first definition.
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            self.provider.drain(16)
        request, _ = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")[-1]
        self.assertEqual([name for name, _, _ in self.sdk.definitions[request[1]]][:1], [b"PLANE LATITUDE"])
        self.assertNotIn(b"SIM ON GROUND", [name for name, _, _ in self.sdk.definitions[request[1]]])
        self.assertNotIn(b"PLANE LONGITUDE", [name for name, _, _ in self.sdk.definitions[request[1]]])

    def test_backoff_retry_does_not_re_add_a_refused_field(self):
        self.sdk.refuse = {b"SIM ON GROUND"}
        signal = snapshot_signal(LATITUDE, ON_GROUND)
        self.demand(signal)
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            self.provider.drain(16)  # Rebuilt without SIM ON GROUND.
        self.sdk.refuse_requests = 1
        self.sdk.refuse.clear()
        _, sent = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")[-1]
        self.sdk.exception(sent)  # The rebuilt request itself is refused: back off.
        errors = [item for item in self.readings(self.provider.drain(16)) if item.error]
        self.assertTrue(errors)
        before = len(self.sdk.matching_calls("SimConnect_AddToDataDefinition"))
        self.advance(2.5)
        self.provider.drain(16)
        retried = self.names()[before:]
        self.assertIn(b"PLANE LATITUDE", retried)
        self.assertNotIn(b"SIM ON GROUND", retried)

    def test_a_total_refusal_is_retried_with_every_field(self):
        self.sdk.refuse = {b"PLANE LATITUDE", b"SIM ON GROUND"}
        self.demand(snapshot_signal(LATITUDE, ON_GROUND))
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            batch = self.provider.drain(16)
        self.assertTrue(any("every field" in (item.error or "") for item in self.readings(batch)))
        self.sdk.refuse.clear()
        before = len(self.sdk.matching_calls("SimConnect_AddToDataDefinition"))
        self.advance(2.5)
        self.provider.drain(16)
        self.assertEqual(self.names()[before:before + 2], [b"PLANE LATITUDE", b"SIM ON GROUND"])

    def test_ending_the_demand_forgets_the_exclusions(self):
        self.sdk.refuse = {b"SIM ON GROUND"}
        signal = snapshot_signal(LATITUDE, ON_GROUND)
        self.demand(signal)
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            self.provider.drain(16)
        self.provider.replace_demand(())
        self.assertNotIn(signal, self.provider._snapshot_exclusions)

    def test_negative_absolute_time_has_no_sample_time(self):
        # ABSOLUTE TIME conversion is contract.simulator_utc, with its range guards.
        self.demand(snapshot_signal(LATITUDE))
        ((request, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.sdk.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, pack("<dddd", 1.0, 2.0, -5.0, 1.0),
            request_id=request[0], definition_id=request[1], definition_count=4,
        ))
        (reading,) = self.readings(self.provider.drain(16))
        self.assertEqual(reading.value, SnapshotFrame((1.0,), 2.0, -5.0, 1.0))
        self.assertIsNone(reading.sample_time)


class RegistrationCleanupTests(AdapterCase):
    """A31/A81 and the rejected-AddToDataDefinition leak."""

    SPEED = Signal("GROUND VELOCITY", "knots", ValueType.NUMBER)

    def test_retry_after_a_refused_definition_clears_the_old_request_and_definition(self):
        self.sdk.refuse = {b"GROUND VELOCITY"}
        self.demand(self.SPEED)
        ((first, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.provider.drain(16)
        self.sdk.refuse.clear()
        self.advance(2.5)
        self.provider.drain(16)
        cleared = [args[0] for args, _ in self.sdk.matching_calls("SimConnect_ClearDataDefinition")]
        self.assertIn(first[1], cleared)
        stopped = [args for args, _ in self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
                   if args[3] == Period.NEVER]
        self.assertEqual([args[0] for args in stopped], [first[0]])
        latest, _ = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")[-1]
        self.assertNotEqual(latest[1], first[1])
        self.assertEqual(latest[3], Period.SECOND)

    def test_request_failure_after_a_definition_is_cleaned_up_on_retry(self):
        self.sdk.raise_request = 1
        self.demand(self.SPEED)
        (definition_call, _), = self.sdk.matching_calls("SimConnect_AddToDataDefinition")
        batch = self.provider.drain(16)
        self.assertTrue(any(item.error for item in self.readings(batch)))
        self.advance(2.5)
        self.provider.drain(16)
        cleared = [args[0] for args, _ in self.sdk.matching_calls("SimConnect_ClearDataDefinition")]
        self.assertEqual(cleared, [definition_call[0]])
        self.assertIs(self.provider._ready, True)


class EventLaneTests(AdapterCase):
    """Discrete events are never evicted as if they were telemetry."""

    def test_event_readings_survive_a_telemetry_overflow(self):
        now = datetime.now(timezone.utc)
        crashed = Signal("CRASHED", None, ValueType.INTEGER, event=True)
        self.provider._offer(Reading("s", crashed, 1, now, 0.0))
        speed = Signal("GROUND VELOCITY", "knots", ValueType.NUMBER)
        for index in range(9000):
            self.provider._offer(Reading("s", speed, float(index), now, 0.0))
        batch = self.provider.drain(10_000)
        self.assertGreater(batch[0].dropped_updates, 0)
        self.assertEqual([item.signal for item in batch[1:] if isinstance(item, Reading)][0], crashed)
        self.assertEqual(sum(isinstance(item, Reading) and item.signal == crashed for item in batch), 1)


class ProviderInfoTests(TestCase):
    """Contract C5 and the dispatch default."""

    def test_provider_exposes_command_timeout_and_interval_granularity(self):
        provider = SimConnectProvider(FakeSdk(), readback_timeout=6.0)
        self.assertEqual(provider.max_command_timeout_seconds, 6.0)
        self.assertEqual(SimConnectProvider(FakeSdk()).max_command_timeout_seconds, 4.0)
        self.assertEqual(provider.interval_granularity_ms, 1000)
        self.assertIsInstance(provider.interval_granularity_ms, int)
        self.assertIsInstance(provider.max_command_timeout_seconds, float)

    def test_wake_forwards_to_the_sdk_event_when_there_is_one(self):
        class WakingSdk(FakeSdk):
            woken = 0

            def wake(self):
                self.woken += 1
        sdk = WakingSdk()
        SimConnectProvider(sdk).wake()
        self.assertEqual(sdk.woken, 1)
        SimConnectProvider(FakeSdk()).wake()  # No wake event: a no-op, never an error.

    def test_callback_dispatch_is_the_documented_default(self):
        self.assertTrue(SimConnectSdk(Path.cwd() / "SimConnect.dll").callback_dispatch)
        doc = simconnect_sdk.__doc__
        self.assertIn("callback dispatch (``SimConnect_CallDispatch``) by\ndefault", doc)
        self.assertIn("opt-in", doc)


class NativeErrorTests(TestCase):
    """A31/A81: native faults become ProviderError at the SDK boundary."""

    def sdk(self, function):
        sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll", callback_dispatch=False)
        sdk._functions = {"SimConnect_Close": function, "SimConnect_GetNextDispatch": function}
        return sdk

    def test_argument_error_becomes_provider_error(self):
        def bad(*_):
            raise ctypes.ArgumentError("argument 2: TypeError: wrong type")
        with self.assertLogs("bbc_core.simconnect_sdk", "ERROR"):
            with self.assertRaisesRegex(ProviderError, "invalid argument"):
                self.sdk(bad).call("SimConnect_Close", None)

    def test_os_error_in_pull_dispatch_becomes_provider_error(self):
        def crash(*_):
            raise OSError("exception: access violation reading 0x0")
        with self.assertLogs("bbc_core.simconnect_sdk", "ERROR"):
            with self.assertRaisesRegex(ProviderError, "inside SimConnect.dll"):
                self.sdk(crash).next_packet(None)


class FloatRadioSdk(FakeSdk):
    """Stores COM frequencies with a sub-Hz float error, like a float32 round trip."""

    error_hz = 0.4

    def call(self, name, handle, *args):
        if (name == "SimConnect_RequestDataOnSimObject" and args[3] == Period.ONCE
                and len(self.definitions.get(args[1], ())) == 2 and self.supply_radio_reads):
            self.packet_id += 1
            self.calls.append((name, args, self.packet_id))
            values = [value + self.error_hz for value in self.radio]
            self.data(args[0], pack("<dd", *values), definition=args[1], count=2)
            return
        super().call(name, handle, *args)


class ComCase(FakeClock, TestCase):
    sdk_class = FakeSdk

    def setUp(self):
        self.start_clock()
        self.sdk = self.sdk_class()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()

    def tune(self, frequency):
        command = self.radio.submit_command("radio.com1.set_standby", frequency)
        for _ in range(3):
            self.core.pump()
        self.advance(2)
        self.core.pump()
        return self.radio.command_result(command.id)

    def transmitted(self):
        return [args[2] for args, _ in self.sdk.matching_calls("SimConnect_TransmitClientEvent")]


class ComSpacingTests(ComCase):
    """Contract C6: the channel is checked against COM SPACING MODE:1 before sending."""

    def test_25_khz_radio_refuses_an_8_33_channel_before_sending(self):
        self.sdk.spacing = 0
        result = self.tune(118_005_000)
        self.assertIs(result.state, CommandState.REJECTED)
        self.assertIn("25 kHz", result.detail)
        # C6 parity: the adapter's detail is the shared rule's text, as in Core and SimulatedProvider.
        from bbc_core.catalog import com_channel_problem
        self.assertEqual(result.detail, com_channel_problem(118_005_000, 0))
        self.assertEqual(self.transmitted(), [])
        self.assertIs(self.radio.connection().state, ConnectionState.CONNECTED)
        # The session is not blocked: a valid 25 kHz channel still works.
        self.assertIs(self.tune(118_025_000).state, CommandState.CONFIRMED)

    def test_8_33_radio_sends_an_8_33_channel(self):
        self.sdk.spacing = 1
        self.assertIs(self.tune(118_005_000).state, CommandState.CONFIRMED)
        self.assertEqual(self.transmitted(), [118_005_000])

    def test_refused_spacing_simvar_leaves_spacing_unknown(self):
        self.sdk.spacing = None
        with self.assertLogs("bbc_core.simconnect", "INFO"):
            result = self.tune(118_005_000)
        self.assertIs(result.state, CommandState.CONFIRMED)
        self.assertIs(self.tune(118_030_000).state, CommandState.CONFIRMED)


class ComDecodeTests(ComCase):
    """Contract C6: FLOAT64 Hz rounded to the nearest Hz, never INT32-truncated."""

    sdk_class = FloatRadioSdk

    def test_float_readback_rounds_to_the_requested_channel(self):
        self.sdk.error_hz = -0.4  # 123449999.6 Hz would truncate to ...999 Hz.
        result = self.tune(123_450_000)
        self.assertIs(result.state, CommandState.CONFIRMED)
        self.assertNotIn("nearest", result.detail)

    def test_non_finite_baseline_is_refused(self):
        self.sdk.error_hz = float("nan")
        result = self.tune(123_450_000)
        self.assertIs(result.state, CommandState.REJECTED)
        self.assertEqual(self.transmitted(), [])


class InputPrimingTests(FakeClock, TestCase):
    """A113: when every initial GetInputEvent fails, the next change value primes."""

    def setUp(self):
        self.start_clock()
        self.sdk = FakeInputSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.inputs = self.core.register_consumer(control_groups=("aircraft_inputs",))
        self.core.connect()
        self.addCleanup(self.core.disconnect)
        self.inputs.request_aircraft_inputs()
        self.core.pump()
        request = self.sdk.matching_calls("SimConnect_EnumerateInputEvents")[-1][0][0]
        self.sdk.page(request, 0, 1, (b"TestControl", INPUT_HASH, DataType.FLOAT64))
        self.core.pump()
        self.ref = self.inputs.aircraft_inputs().inputs[0].id

    def test_value_arrives_after_every_get_timed_out(self):
        subscription = self.inputs.subscribe_input(self.ref)
        self.core.pump()
        for wait in (3.1, 6.1, 12.1):
            self.advance(wait)
            self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_GetInputEvent")), 3)
        self.assertIn("next change", self.inputs.latest(subscription.id).reason)
        self.sdk.changed_value(INPUT_HASH, pack("<d", 42))
        self.core.pump()
        self.assertEqual(self.inputs.latest(subscription.id).value, 42)
        self.sdk.changed_value(INPUT_HASH, pack("<d", 43))
        self.core.pump()
        self.assertEqual(self.inputs.latest(subscription.id).value, 43)

    def test_change_values_are_still_held_while_a_get_is_outstanding(self):
        subscription = self.inputs.subscribe_input(self.ref)
        self.core.pump()
        self.sdk.changed_value(INPUT_HASH, pack("<d", 30))
        self.core.pump()
        self.assertIsNone(self.inputs.latest(subscription.id).value)  # Unprimed: the GET is still due.


class CatalogRestartTests(FakeClock, TestCase):
    """A38: a catalog request during an enumeration restarts it under a new generation."""

    def test_second_request_restarts_the_enumeration(self):
        self.start_clock()
        sdk = FakeInputSdk()
        provider = SimConnectProvider(sdk)
        provider.connect("session")
        self.addCleanup(provider.disconnect)
        provider.request_input_catalog()
        provider.request_input_catalog()
        (first, _), (second, _) = sdk.matching_calls("SimConnect_EnumerateInputEvents")
        self.assertNotEqual(first[0], second[0])
        sdk.page(first[0], 0, 1, (b"Stale", INPUT_HASH, DataType.FLOAT64))
        sdk.page(second[0], 0, 1, (b"Fresh", INPUT_HASH, DataType.FLOAT64))
        catalogs = [item for item in provider.drain(64) if type(item).__name__ == "InputCatalog"]
        ready = [item for item in catalogs if item.state is InputCatalogState.READY]
        self.assertEqual(len(ready), 1)
        self.assertEqual([item.name for item in ready[0].inputs], ["Fresh"])
        loading = [item.generation for item in catalogs if item.state is InputCatalogState.LOADING]
        self.assertEqual(ready[0].generation, max(loading))
        self.assertEqual(len(set(loading)), 2)
