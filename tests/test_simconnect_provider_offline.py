"""Exercise the direct adapter with in-memory packets and an injected SDK."""

from collections import deque
from ctypes import cast, POINTER, sizeof, string_at
from pathlib import Path
from struct import pack, unpack
from unittest import TestCase
from unittest.mock import patch

from bbc_core import CommandState, ConnectionState, Core, CoreRequestError, Event, Quality, ValueType
from bbc_core.provider import ProviderError
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import (
    HANDLE, DataType, Period, Receive, ReceiveEvent, ReceiveException, ReceiveId, ReceiveOpen,
    ReceiveSimObjectDataPrefix,
)
from bbc_core.simconnect_sdk import SimConnectSdk
from test_adapter_support import FakeClock


def packet(cls, kind, payload=b"", **fields):
    message = cls()
    message.id = kind
    message.version = 1
    message.size = sizeof(cls) + len(payload)
    for field, value in fields.items():
        setattr(message, field, value)
    return bytes(message) + payload


class FakeSdk:
    simulated = True

    def __init__(self):
        self.start = [packet(ReceiveOpen, ReceiveId.OPEN)]
        self.incoming = deque()
        self.calls = []
        self.closed = []
        self.handle = None
        self.definitions = {}
        self.mapped = {}
        self.radio = [118_100_000, 121_500_000]
        # COM SPACING MODE:1 reply: 0 (25 kHz), 1 (8.33 kHz), or None to refuse the SimVar.
        self.spacing = 1
        self.throttle = 0.0
        self.apply_write = True
        self.reject_write = False
        self.supply_radio_reads = True
        self.fail_readback = False
        self.fail_close = False
        self.packet_id = 100

    def open(self, application):
        if self.handle is not None:
            raise AssertionError("Tried to open over an existing handle")
        self.handle = object()
        self.incoming.extend(self.start)
        return self.handle

    def close(self, handle):
        self._handle_is_current(handle)
        if self.fail_close:
            raise ProviderError("fake close failure")
        self.closed.append(handle)
        self.handle = None
        self.incoming.clear()
        self.definitions.clear()
        self.mapped.clear()

    def next_packet(self, handle):
        # Like the real SDK, an empty queue yields no packet (finding A27).
        self._handle_is_current(handle)
        return self.incoming.popleft() if self.incoming else None

    def last_packet_id(self, handle):
        self._handle_is_current(handle)
        return self.packet_id

    def call(self, name, handle, *args):
        self._handle_is_current(handle)
        self.packet_id += 1
        self.calls.append((name, args, self.packet_id))
        if name == "SimConnect_AddToDataDefinition":
            self.definitions.setdefault(args[0], []).append(args[1:4])
            if args[1] == b"COM SPACING MODE:1" and self.spacing is None:
                self.exception(self.packet_id)
        elif name == "SimConnect_ClearDataDefinition":
            self.definitions.pop(args[0])
        elif name == "SimConnect_RequestDataOnSimObject":
            request, definition, _, period, *_ = args
            if period == Period.ONCE and self.supply_radio_reads:
                fields = self.definitions[definition]
                if fields[0][0] == b"COM SPACING MODE:1":
                    if self.spacing is not None:
                        self.data(request, pack("<i", self.spacing), definition=definition)
                elif len(fields) == 1:
                    self.data(request, pack("<d", self.throttle), definition=definition)
                elif self.fail_readback and self.matching_calls("SimConnect_TransmitClientEvent"):
                    self.exception(self.packet_id)
                else:
                    # COM frequencies are FLOAT64 Hz on the wire (contract C6).
                    self.data(request, pack("<dd", *map(float, self.radio)), definition=definition, count=2)
        elif name == "SimConnect_MapClientEventToSimEvent":
            self.mapped[args[0]] = args[1]
        elif name == "SimConnect_TransmitClientEvent":
            if self.reject_write:
                self.exception(self.packet_id)
            elif self.apply_write:
                operation = self.mapped[args[1]]
                if operation == b"COM_STBY_RADIO_SET_HZ":
                    self.radio[1] = args[2]
                elif operation == b"COM_STBY_RADIO_SWAP":
                    self.radio.reverse()
                else:
                    raise AssertionError(f"Unexpected operation: {operation!r}")
        elif name == "SimConnect_SetDataOnSimObject" and self.apply_write:
            self.throttle, = unpack("<d", string_at(args[5], args[4]))
        elif name == "SimConnect_TransmitClientEvent_EX1" and self.reject_write:
            self.exception(self.packet_id)

    def matching_calls(self, name):
        return [(args, packet_id) for operation, args, packet_id in self.calls if operation == name]

    def data(self, request, payload, *, definition=None, count=1):
        self.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, payload,
            request_id=request, definition_id=request if definition is None else definition,
            definition_count=count,
        ))

    def event(self, event_id, data):
        self.incoming.append(packet(ReceiveEvent, ReceiveId.EVENT, event_id=event_id, data=data))

    def exception(self, send_id):
        self.incoming.append(packet(
            ReceiveException, ReceiveId.EXCEPTION, exception=7, send_id=send_id, index=1,
        ))

    def quit(self):
        self.incoming.append(packet(Receive, ReceiveId.QUIT))

    def _handle_is_current(self, handle):
        if handle is not self.handle or self.handle is None:
            raise AssertionError("Fake SDK received the wrong handle")


class SimConnectProviderOfflineTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100.0)  # Shared clock for every bbc_core module, sleep included (A139).
        self.sdk = FakeSdk()
        self.provider = SimConnectProvider(self.sdk)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)

    def test_open_requires_confirmation_and_close_retires_handle(self):
        self.sdk.start = [packet(Receive, 0), packet(ReceiveOpen, ReceiveId.OPEN)]
        consumer = self.core.register_consumer()
        connection = self.core.connect()
        self.assertEqual(connection.state, ConnectionState.CONNECTED)
        self.assertTrue(connection.source.simulated)
        self.assertGreater(self.now, 100.0)
        handle = self.sdk.handle
        self.assertEqual(self.core.disconnect().state, ConnectionState.DISCONNECTED)
        self.assertEqual(self.sdk.closed, [handle])
        self.assertIsNone(consumer.connection().session_id)

    def test_startup_quit_and_open_timeout_leave_no_handle(self):
        for startup, message in (([packet(Receive, ReceiveId.QUIT)], "quit"),
                                 ([], "Timed out")):
            with self.subTest(message=message):
                self.sdk.start = startup
                status = self.core.connect()
                self.assertEqual(status.state, ConnectionState.ERROR)
                self.assertIn(message, status.detail)
                self.assertIsNone(self.sdk.handle)

    def test_open_uses_the_sdk_local_only_config_constant(self):
        sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll")
        self.assertFalse(sdk.simulated)

        def fake_open(name, pointer, *_):
            self.assertEqual(name, "SimConnect_Open")
            cast(pointer, POINTER(HANDLE))[0] = HANDLE(123)

        with patch.object(sdk, "call", side_effect=fake_open) as call:
            self.assertEqual(sdk.open(b"EFB").value, 123)
        self.assertEqual(call.call_args.args[2:], (b"EFB", None, 0, None, 0xFFFFFFFF))

    def test_numeric_fixed_width_text_and_pause_packets(self):
        consumer = self.core.register_consumer()
        speed = consumer.subscribe("aircraft.ground_speed")
        title = consumer.subscribe("aircraft.title")
        ground = consumer.subscribe("aircraft.on_ground")
        consumer.subscribe("sim.pause_state")
        self.core.connect()
        self.core.pump()
        definitions = self.sdk.matching_calls("SimConnect_AddToDataDefinition")
        ids = {args[1]: args[0] for args, _ in definitions}
        types = {args[1]: args[2:4] for args, _ in definitions}
        self.assertEqual(types[b"TITLE"], (None, DataType.STRING256))
        self.sdk.data(ids[b"GROUND VELOCITY"], pack("<d", 82.25))
        self.sdk.data(ids[b"TITLE"], pack("<256s", b"Example aircraft"))
        self.sdk.data(ids[b"SIM ON GROUND"], pack("<i", -1))
        (((event_id, _), _),) = self.sdk.matching_calls("SimConnect_SubscribeToSystemEvent")
        self.sdk.event(event_id, 4)
        self.core.pump()
        self.assertEqual(consumer.latest(speed.id).value, 82.25)
        self.assertEqual(consumer.latest(title.id).value, "Example aircraft")
        self.assertIs(consumer.latest(ground.id).value, True)
        self.assertEqual([event.value for event in consumer.poll() if isinstance(event, Event)], [4])

    def test_shared_subscription_retires_only_after_last_consumer(self):
        first = self.core.register_consumer()
        second = self.core.register_consumer()
        first.subscribe("aircraft.ground_speed", interval_ms=1500)
        subscription = second.subscribe("aircraft.ground_speed", interval_ms=1500)
        self.core.connect()
        self.core.pump()
        ((request_args, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        request = request_args[0]
        self.assertEqual(request_args[3:7], (Period.SECOND, 0, 0, 1))
        first.close()
        self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")), 1)
        second.unsubscribe(subscription.id)
        self.core.pump()
        retired, _ = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")[-1]
        self.assertEqual(retired[:4], (request, request, 0, Period.NEVER))
        self.assertEqual(self.sdk.matching_calls("SimConnect_ClearDataDefinition")[0][0], (request,))

    def test_diagnostics_accepts_indexed_simvars_but_not_other_variable_namespaces(self):
        consumer = self.core.register_consumer(diagnostics=True)
        consumer.subscribe_simvar("GENERAL ENG RPM:1", "rpm", ValueType.NUMBER)
        with self.assertRaises(CoreRequestError):
            consumer.subscribe_simvar("L:custom_variable", "number", ValueType.NUMBER)
        self.core.connect()
        self.core.pump()
        ((args, _),) = self.sdk.matching_calls("SimConnect_AddToDataDefinition")
        self.assertEqual(args[1:3], (b"GENERAL ENG RPM:1", b"rpm"))

    def test_invalid_signal_exception_does_not_break_another_read(self):
        consumer = self.core.register_consumer()
        bad = consumer.subscribe("aircraft.title")
        good = consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        ids = {args[1]: (args[0], send) for args, send in
               self.sdk.matching_calls("SimConnect_AddToDataDefinition")}
        self.sdk.exception(ids[b"TITLE"][1])
        self.sdk.data(ids[b"GROUND VELOCITY"][0], pack("<d", 35.0))
        self.core.pump()
        self.assertEqual(consumer.latest(bad.id).quality, Quality.UNAVAILABLE)
        self.assertIn("SimConnect exception 7", consumer.latest(bad.id).reason)
        self.assertEqual(consumer.latest(good.id).value, 35.0)
        self.assertEqual(consumer.connection().state, ConnectionState.CONNECTED)

    def test_truncated_known_packet_is_local_to_its_signal(self):
        # Finding A45: one malformed reply makes its own signal unavailable and
        # the shared session continues.
        consumer = self.core.register_consumer()
        speed = consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        ((args, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.sdk.data(args[0], b"\0")
        self.core.pump()
        self.assertEqual(consumer.connection().state, ConnectionState.CONNECTED)
        self.assertEqual(consumer.latest(speed.id).quality, Quality.UNAVAILABLE)
        self.assertIn("Invalid SimConnect data", consumer.latest(speed.id).reason)
        self.sdk.data(args[0], pack("<d", 12.5))
        self.core.pump()
        self.assertEqual(consumer.latest(speed.id).value, 12.5)

    def test_tune_and_swap_use_one_shot_readback_without_a_telemetry_subscription(self):
        radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()

        tune = radio.submit_command("radio.com1.set_standby", 123_450_000)
        self.core.pump()
        self.assertEqual(radio.command_result(tune.id).state, CommandState.SENT)
        self.core.pump()
        self.core.pump()
        self.assertEqual(radio.command_result(tune.id).state, CommandState.CONFIRMED)
        self.assertEqual(self.sdk.radio, [118_100_000, 123_450_000])

        swap = radio.submit_command("radio.com1.swap")
        self.core.pump()
        self.core.pump()
        self.core.pump()
        self.assertEqual(radio.command_result(swap.id).state, CommandState.CONFIRMED)
        self.assertEqual(self.sdk.radio, [123_450_000, 118_100_000])
        transmits = self.sdk.matching_calls("SimConnect_TransmitClientEvent")
        self.assertEqual([args[2] for args, _ in transmits], [123_450_000, 0])
        self.assertTrue(all(args[0] == 0 and args[-1] == 0x10 for args, _ in transmits))
        reads = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        # Per command: the COM1 pair and COM SPACING MODE:1 baseline, then one readback.
        self.assertEqual(len(reads), 6)
        self.assertTrue(all(args[3] == Period.ONCE for args, _ in reads))
        fields = {args[1]: args[3] for args, _ in self.sdk.matching_calls("SimConnect_AddToDataDefinition")}
        self.assertEqual(fields[b"COM ACTIVE FREQUENCY:1"], DataType.FLOAT64)  # Contract C6.
        self.assertEqual(fields[b"COM STANDBY FREQUENCY:1"], DataType.FLOAT64)
        self.assertEqual(fields[b"COM SPACING MODE:1"], DataType.INT32)

    def test_missing_swap_baseline_and_invalid_frequency_prevent_transmission(self):
        radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()
        self.sdk.supply_radio_reads = False
        swap = radio.submit_command("radio.com1.swap")
        self.core.pump()
        tune = radio.submit_command("radio.com1.set_standby", 99_000_000)
        self.core.pump()
        self.assertEqual(radio.command_result(swap.id).state, CommandState.REJECTED)
        self.assertEqual(radio.command_result(tune.id).state, CommandState.REJECTED)
        self.assertEqual(self.sdk.matching_calls("SimConnect_TransmitClientEvent"), [])
        self.assertEqual(radio.connection().state, ConnectionState.CONNECTED)

    def test_correlated_native_exception_reports_rejected_write(self):
        self.sdk.reject_write = True
        radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()
        command = radio.submit_command("radio.com1.set_standby", 123_450_000)
        self.core.pump()
        self.core.pump()
        result = radio.command_result(command.id)
        self.assertEqual(result.state, CommandState.REJECTED)
        self.assertIn("SimConnect exception 7", result.detail)
        self.assertEqual(radio.connection().state, ConnectionState.CONNECTED)

    def test_wrong_readback_times_out_without_resending_command(self):
        self.sdk.apply_write = False
        radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()
        command = radio.submit_command("radio.com1.set_standby", 123_450_000)
        self.core.pump()
        self.core.pump()
        self.core.pump()
        self.advance(0.2)
        self.core.pump()
        self.advance(2)
        self.core.pump()
        self.assertEqual(radio.command_result(command.id).state, CommandState.UNCERTAIN)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_TransmitClientEvent")), 1)
        self.assertEqual(radio.connection().state, ConnectionState.CONNECTED)

    def test_readback_exception_leaves_telemetry_running(self):
        self.sdk.fail_readback = True
        radio = self.core.register_consumer(control_groups=("radios",))
        speed = radio.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        ((args, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        command = radio.submit_command("radio.com1.set_standby", 123_450_000)
        self.core.pump()
        self.core.pump()
        self.sdk.data(args[0], pack("<d", 20.0))
        self.core.pump()
        result = radio.command_result(command.id)
        self.assertEqual(result.state, CommandState.UNCERTAIN)
        self.assertIn("SimConnect exception 7", result.detail)
        self.assertEqual(radio.latest(speed.id).value, 20.0)
        self.assertEqual(radio.connection().state, ConnectionState.CONNECTED)

    def test_writable_simvar_sends_one_typed_element_and_confirms_against_readback(self):
        engine = self.core.register_consumer(control_groups=("engine_controls",))
        self.core.connect()
        command = engine.submit_command("aircraft.engine1.set_throttle", 32.5)
        self.core.pump()
        self.assertEqual(engine.command_result(command.id).state, CommandState.SENT)
        self.core.pump()
        self.core.pump()
        self.assertEqual(engine.command_result(command.id).state, CommandState.CONFIRMED)
        self.assertEqual(self.sdk.throttle, 32.5)
        ((write, _),) = self.sdk.matching_calls("SimConnect_SetDataOnSimObject")
        self.assertEqual(write[1:5], (0, 0, 1, 8))
        self.assertEqual(self.sdk.definitions[write[0]], [
            (b"GENERAL ENG THROTTLE LEVER POSITION:1", b"percent", DataType.FLOAT64),
        ])
        ((request, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.assertEqual(request[1:4], (write[0], 0, Period.ONCE))

    def test_writable_simvar_rejects_invalid_values_and_reports_readback_mismatch(self):
        engine = self.core.register_consumer(control_groups=("engine_controls",))
        self.core.connect()
        for invalid in (True, float("nan"), -0.25, 101):
            result = engine.submit_command("aircraft.engine1.set_throttle", invalid)
            self.assertEqual(result.state, CommandState.REJECTED)
        self.assertEqual(self.sdk.matching_calls("SimConnect_SetDataOnSimObject"), [])
        self.sdk.apply_write = False
        command = engine.submit_command("aircraft.engine1.set_throttle", 24)
        self.core.pump()
        self.core.pump()
        self.core.pump()
        self.advance(2)
        self.core.pump()
        self.assertEqual(engine.command_result(command.id).state, CommandState.UNCERTAIN)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_SetDataOnSimObject")), 1)
        self.assertEqual(engine.connection().state, ConnectionState.CONNECTED)

    def test_extended_event_argument_schema_and_dispatch_without_readback(self):
        electrical = self.core.register_consumer(control_groups=("electrical",))
        self.core.connect()
        capability_id = "aircraft.electrical.toggle_bus_connection"
        info = next(item.capability for item in electrical.discover().capabilities
                    if item.capability.id == capability_id)
        self.assertEqual([param.name for param in info.parameters], ["source_bus", "target_bus"])
        for invalid in (1, (1, True), (-1, 2), (1, 2, 3), (0x100000000, 2)):
            self.assertEqual(electrical.submit_command(capability_id, invalid).state, CommandState.REJECTED)
        command = electrical.submit_command(capability_id, (2, 5))
        self.core.pump()
        self.assertEqual(electrical.command_result(command.id).state, CommandState.SENT)
        self.advance(2)
        self.core.pump()
        self.assertEqual(electrical.command_result(command.id).state, CommandState.UNVERIFIED)
        ((sent, _),) = self.sdk.matching_calls("SimConnect_TransmitClientEvent_EX1")
        self.assertEqual(sent[0], 0)
        self.assertEqual(sent[3:], (0x10, 2, 5, 0, 0, 0))
        self.assertEqual(self.sdk.mapped[sent[1]], b"ELECTRICAL_BUS_TO_BUS_CONNECTION_TOGGLE")
        self.assertEqual(self.sdk.matching_calls("SimConnect_RequestDataOnSimObject"), [])
        self.assertEqual(electrical.submit_command(capability_id, (2, 5)).state, CommandState.ACCEPTED)

    def test_extended_event_correlates_an_async_rejection(self):
        electrical = self.core.register_consumer(control_groups=("electrical",))
        self.core.connect()
        self.sdk.reject_write = True
        command = electrical.submit_command("aircraft.electrical.toggle_bus_connection", (2, 5))
        self.core.pump()
        self.core.pump()
        result = electrical.command_result(command.id)
        self.assertEqual(result.state, CommandState.REJECTED)
        self.assertIn("SimConnect exception 7", result.detail)
        self.assertEqual(electrical.connection().state, ConnectionState.CONNECTED)

    def test_quit_marks_command_uncertain_and_failed_close_blocks_reconnect(self):
        radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()
        command = radio.submit_command("radio.com1.set_standby", 123_450_000)
        self.core.pump()
        self.sdk.fail_close = True
        self.addCleanup(setattr, self.sdk, "fail_close", False)
        self.sdk.quit()
        self.core.pump()
        self.assertEqual(radio.command_result(command.id).state, CommandState.UNCERTAIN)
        self.assertIn("fake close failure", radio.connection().detail)
        self.assertEqual(self.core.connect().state, ConnectionState.ERROR)
        self.sdk.fail_close = False
        self.core.disconnect()
        self.assertEqual(self.core.connect().state, ConnectionState.CONNECTED)
