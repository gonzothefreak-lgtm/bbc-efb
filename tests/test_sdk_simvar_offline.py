"""Verify general SimVar and client event calls through the consumer API."""

from ctypes import string_at
from struct import pack, unpack
from unittest import TestCase

from bbc_core import CommandState, ConnectionState, Core, CoreRequestError, Quality, SimVar, SimVarFormat, ValueType
from bbc_core.provider import ProviderError
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import DataType, Period
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk


class GenericSdk(FakeSdk):
    def __init__(self):
        super().__init__()
        self.values = {
            b"LIGHT POTENTIOMETER:2": pack("<f", 25.0),
            b"ATC ID": pack("<8s", b"OLD"),
        }
        self.supply_radio_reads = False
        self.reject_mapping = False

    def call(self, name, handle, *args):
        if name == "SimConnect_AddToDataDefinition" and args[1] == b"UNAVAILABLE SIMVAR":
            raise ProviderError("SDK refused the SimVar definition.")
        if name in ("SimConnect_TransmitClientEvent", "SimConnect_SetDataOnSimObject"):
            self._handle_is_current(handle)
            self.packet_id += 1
            self.calls.append((name, args, self.packet_id))
        else:
            super().call(name, handle, *args)
        if name == "SimConnect_RequestDataOnSimObject" and args[3] is Period.ONCE:
            simvar = self.definitions[args[1]][0][0]
            self.data(args[0], self.values[simvar], definition=args[1])
        elif name == "SimConnect_SetDataOnSimObject" and self.apply_write:
            simvar = self.definitions[args[0]][0][0]
            self.values[simvar] = string_at(args[5], args[4])
        elif name == "SimConnect_MapClientEventToSimEvent" and self.reject_mapping:
            self.exception(self.packet_id)


class SdkSimVarOfflineTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100.0)  # Every bbc_core clock and simconnect.sleep (A139).
        self.sdk = GenericSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.pilot = self.core.register_consumer(control_groups=("simvar_writes", "client_events"))
        self.reader = self.core.register_consumer()
        self.light = SimVar("Light Potentiometer", "Percent", ValueType.NUMBER, 2, SimVarFormat.FLOAT32)
        self.core.connect()

    def complete(self, command):
        for _ in range(3):
            self.core.pump()
        return self.pilot.command_result(command.id)

    def test_generic_read_normalizes_requests_and_shares_matching_subscriptions(self):
        first = self.reader.subscribe_sdk_simvar(self.light, interval_ms=1000)
        second = self.pilot.subscribe_sdk_simvar(
            SimVar("light potentiometer", "percent", ValueType.NUMBER, index=2, format=SimVarFormat.FLOAT32),
        )
        self.core.pump()
        ((define, _),) = self.sdk.matching_calls("SimConnect_AddToDataDefinition")
        self.assertEqual(define[1:4], (b"LIGHT POTENTIOMETER:2", b"percent", DataType.FLOAT32))
        ((request, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.sdk.data(request[0], pack("<f", 64.5), definition=define[0])
        self.core.pump()
        self.assertEqual(self.reader.latest(first.id).value, 64.5)
        self.assertEqual(self.pilot.latest(second.id).value, 64.5)
        self.reader.unsubscribe(first.id)
        self.core.pump()
        self.assertFalse(self.sdk.matching_calls("SimConnect_ClearDataDefinition"))
        self.pilot.unsubscribe(second.id)
        self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_ClearDataDefinition")), 1)

    def test_default_format_shares_named_path_and_int64_remains_distinct(self):
        named = self.reader.subscribe("aircraft.on_ground")
        typed = self.reader.subscribe_sdk_simvar(SimVar("sim on ground", "bool", ValueType.BOOLEAN))
        large = self.reader.subscribe_sdk_simvar(SimVar("ADF ACTIVE FREQUENCY", "Hz", ValueType.INTEGER,
                                                       index=1, format=SimVarFormat.INT64))
        self.core.pump()
        definitions = {args[1]: args for args, _ in self.sdk.matching_calls("SimConnect_AddToDataDefinition")}
        self.assertEqual(len(definitions), 2)
        self.assertIs(definitions[b"ADF ACTIVE FREQUENCY:1"][3], DataType.INT64)
        self.sdk.data(definitions[b"SIM ON GROUND"][0], pack("<i", -1))
        self.sdk.data(definitions[b"ADF ACTIVE FREQUENCY:1"][0], pack("<q", 1 << 40))
        self.core.pump()
        self.assertIs(self.reader.latest(named.id).value, True)
        self.assertIs(self.reader.latest(typed.id).value, True)
        self.assertEqual(self.reader.latest(large.id).value, 1 << 40)

    def test_generic_float_and_text_writes_use_appropriate_encodings_and_readback(self):
        write = self.pilot.submit_simvar(self.light, 73.7)
        self.assertIs(self.complete(write).state, CommandState.CONFIRMED)
        self.assertAlmostEqual(unpack("<f", self.sdk.values[b"LIGHT POTENTIOMETER:2"])[0], 73.7, places=5)
        tail_number = SimVar("ATC ID", None, ValueType.TEXT, format=SimVarFormat.STRING8)
        write = self.pilot.submit_simvar(tail_number, "TEST42")
        self.assertIs(self.complete(write).state, CommandState.CONFIRMED)
        self.assertEqual(self.sdk.values[b"ATC ID"], b"TEST42\0\0")
        for invalid in ("too-long", "has\0nul", "\ud800"):
            write = self.pilot.submit_simvar(tail_number, invalid)
            self.assertIs(self.complete(write).state, CommandState.REJECTED)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_SetDataOnSimObject")), 2)

    def test_generic_event_mapping_and_argument_packing_and_unverified_outcome(self):
        for event, arguments in (
            ("LANDING_LIGHTS_ON", ()), ("LANDING_LIGHTS_SET", (True,)),
            ("AP_HEADING_BUG_SET_EX1", (90, 1)),
            ("ap_heading_bug_set_ex1", (-1, 2, 3, 4, 0xFFFFFFFF)),
        ):
            command = self.pilot.submit_client_event(event, arguments)
            self.core.pump()
            self.assertIs(self.pilot.command_result(command.id).state, CommandState.SENT)
            self.now += 2
            self.core.pump()
            self.assertIs(self.pilot.command_result(command.id).state, CommandState.UNVERIFIED)
        mapped = self.sdk.matching_calls("SimConnect_MapClientEventToSimEvent")
        self.assertEqual([args[1] for args, _ in mapped],
                         [b"LANDING_LIGHTS_ON", b"LANDING_LIGHTS_SET", b"AP_HEADING_BUG_SET_EX1"])
        legacy = self.sdk.matching_calls("SimConnect_TransmitClientEvent")
        self.assertEqual([args[2] for args, _ in legacy], [0, 1])
        extended = self.sdk.matching_calls("SimConnect_TransmitClientEvent_EX1")
        self.assertEqual([args[4:] for args, _ in extended],
                         [(90, 1, 0, 0, 0), (0xFFFFFFFF, 2, 3, 4, 0xFFFFFFFF)])

    def test_mapping_exception_is_local_and_the_failed_mapping_is_not_reused(self):
        self.sdk.reject_mapping = True
        first = self.pilot.submit_client_event("LANDING_LIGHTS_ON")
        self.assertIs(self.complete(first).state, CommandState.REJECTED)
        self.sdk.reject_mapping = False
        retry = self.pilot.submit_client_event("LANDING_LIGHTS_ON")
        self.assertIs(self.complete(retry).state, CommandState.REJECTED)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_TransmitClientEvent")), 1)
        self.assertIs(self.complete(self.pilot.submit_simvar(self.light, 15.0)).state, CommandState.CONFIRMED)
        self.assertIs(self.reader.connection().state, ConnectionState.CONNECTED)

    def test_denied_groups_invalid_types_and_formats_do_not_dispatch(self):
        self.assertIs(self.reader.submit_simvar(self.light, 42).state, CommandState.REJECTED)
        self.assertIs(self.reader.submit_client_event("LANDING_LIGHTS_ON").state, CommandState.REJECTED)
        for invalid in (True, float("inf"), "42"):
            self.assertIs(self.pilot.submit_simvar(self.light, invalid).state, CommandState.REJECTED)
        for event, arguments in (("K:LANDING_LIGHTS_ON", ()), ("LANDING_LIGHTS_SET", (1.0,)),
                                 ("LANDING_LIGHTS_SET", (0,) * 6), ("LANDING_LIGHTS_SET", (1 << 32,))):
            self.assertIs(self.pilot.submit_client_event(event, arguments).state, CommandState.REJECTED)
        for index in (0, True, -1, 1 << 32):
            with self.assertRaises(CoreRequestError):
                SimVar("LIGHT POTENTIOMETER", "percent", ValueType.NUMBER, index=index)
        with self.assertRaises(CoreRequestError):
            SimVar("ATC ID", None, ValueType.TEXT, format=SimVarFormat.INT64)
        self.core.pump()
        self.assertFalse(self.sdk.calls)

    def test_unavailable_simvar_reports_without_disconnecting_or_clearing_missing_definition(self):
        missing = SimVar("UNAVAILABLE SIMVAR", "number", ValueType.NUMBER)
        sub = self.reader.subscribe_sdk_simvar(missing)
        self.core.pump()
        self.assertIs(self.reader.latest(sub.id).quality, Quality.UNAVAILABLE)
        self.assertIn("refused", self.reader.latest(sub.id).reason)
        self.reader.unsubscribe(sub.id)
        self.core.pump()
        self.assertFalse(self.sdk.matching_calls("SimConnect_ClearDataDefinition"))
        self.assertIs(self.complete(self.pilot.submit_simvar(missing, 1)).state, CommandState.REJECTED)
        self.assertIs(self.reader.connection().state, ConnectionState.CONNECTED)

    def test_local_simvar_explicit_format_and_readback_mismatch_without_replay(self):
        local = SimVar("L:BBC_TEST_PANEL", "number", ValueType.NUMBER)
        with self.assertRaises(CoreRequestError):
            SimVar(local.name, "number", ValueType.NUMBER, format=SimVarFormat.FLOAT32)
        self.sdk.values[local.name.encode("ascii")] = pack("<d", 0.0)
        self.sdk.apply_write = False
        write = self.pilot.submit_simvar(local, 2.0)
        self.assertIs(self.complete(write).state, CommandState.SENT)
        self.now += 2
        self.core.pump()
        self.assertIs(self.pilot.command_result(write.id).state, CommandState.UNCERTAIN)
        ((define, _),) = self.sdk.matching_calls("SimConnect_AddToDataDefinition")
        self.assertEqual(define[1:4], (b"L:BBC_TEST_PANEL", b"number", DataType.FLOAT64))
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_SetDataOnSimObject")), 1)
