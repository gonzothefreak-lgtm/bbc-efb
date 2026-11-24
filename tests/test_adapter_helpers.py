"""Regression tests for the input, client-event, client-data and advanced helpers."""

from struct import pack
from unittest import TestCase

from bbc_core import (
    ClientDataArea, ClientEventData, CommandState, ConnectionState, ControllerInput, Core, Event, ObjectField,
    ObjectFormat, Quality, SimulatorOperation as Operation,
)
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import (
    EXCEPTION_ALREADY_CREATED, DataType, ReceiveAssignedObject, ReceiveEvent, ReceiveException, ReceiveId,
    ReceiveReservedKey, ReceiveSimObjectDataPrefix,
)
from test_adapter_support import FakeClock
from test_aircraft_inputs_offline import INPUT_HASH, FakeInputSdk
from test_client_data_offline import ClientDataSdk
from test_simconnect_provider_offline import FakeSdk, packet


class InputEventTests(FakeClock, TestCase):
    """Findings A15, A56 and A113."""

    def setUp(self):
        self.start_clock()
        self.sdk = FakeInputSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.inputs = self.core.register_consumer(control_groups=("aircraft_inputs",))
        self.core.connect()

    def discover(self, descriptor_type=DataType.FLOAT64):
        self.inputs.request_aircraft_inputs()
        self.core.pump()
        request = self.sdk.matching_calls("SimConnect_EnumerateInputEvents")[-1][0][0]
        self.sdk.page(request, 0, 1, (b"TestControl", INPUT_HASH, descriptor_type))
        self.core.pump()
        return self.inputs.aircraft_inputs().inputs[0]

    def test_descriptor_type_is_a_simconnect_datatype(self):
        from bbc_core import ValueType
        self.assertIs(self.discover(DataType.FLOAT32).value_type, ValueType.NUMBER)
        with self.assertLogs("bbc_core.simconnect_input_events", "WARNING"):
            self.assertIsNone(self.discover(0).value_type)  # SIMCONNECT_DATATYPE_INVALID

    def test_unsubscribe_rejection_after_aircraft_change_keeps_the_session(self):
        ref = self.discover().id
        self.inputs.subscribe_input(ref)
        self.core.pump()
        self.sdk.aircraft_changed()
        self.core.pump()
        (_, sent), = self.sdk.matching_calls("SimConnect_UnsubscribeInputEvent")
        self.sdk.exception(sent)
        self.core.pump()
        self.assertIs(self.inputs.connection().state, ConnectionState.CONNECTED)

    def test_rejected_subscribe_is_rolled_back_and_never_unsubscribed(self):
        ref = self.discover().id
        subscription = self.inputs.subscribe_input(ref)
        self.core.pump()
        (_, sent), = self.sdk.matching_calls("SimConnect_SubscribeInputEvent")
        self.sdk.exception(sent)
        self.core.pump()
        self.assertIs(self.inputs.latest(subscription.id).quality, Quality.UNAVAILABLE)
        self.inputs.unsubscribe(subscription.id)
        self.core.pump()
        self.assertEqual(self.sdk.matching_calls("SimConnect_UnsubscribeInputEvent"), [])
        self.inputs.subscribe_input(ref)
        self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_SubscribeInputEvent")), 2)

    def test_timed_out_get_is_retried_and_a_late_reply_still_primes(self):
        ref = self.discover().id
        subscription = self.inputs.subscribe_input(ref)
        self.core.pump()
        first = self.sdk.matching_calls("SimConnect_GetInputEvent")[-1][0][0]
        self.advance(3.5)
        self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_GetInputEvent")), 2)
        self.sdk.input_value(first, pack("<d", 7))
        self.sdk.changed_value(INPUT_HASH, pack("<d", 8))
        self.core.pump()
        self.assertEqual(self.inputs.latest(subscription.id).value, 8)


class ClientEventTests(FakeClock, TestCase):
    """Finding A104."""

    def setUp(self):
        self.start_clock()
        self.sdk = FakeSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.first = self.core.register_consumer()
        self.core.connect()

    def test_failed_mapping_leaves_the_group_and_needs_no_remove(self):
        bad = self.first.subscribe_client_event("PARKING_BRAKE_X")
        self.first.subscribe_client_event("PARKING_BRAKES")
        self.core.pump()
        sent = next(packet_id for args, packet_id in self.sdk.matching_calls("SimConnect_MapClientEventToSimEvent")
                    if args[1] == b"PARKING_BRAKE_X")
        self.sdk.exception(sent)
        self.core.pump()
        self.first.unsubscribe(bad.id)
        self.core.pump()
        self.assertEqual(self.sdk.matching_calls("SimConnect_RemoveClientEvent"), [])
        self.assertIs(self.first.connection().state, ConnectionState.CONNECTED)


class AdvancedTests(FakeClock, TestCase):
    """Findings A47, A48, A100 and A101."""

    def setUp(self):
        self.start_clock()
        self.sdk = FakeSdk()
        self.sdk.supply_radio_reads = self.sdk.apply_write = False
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.first = self.core.register_consumer(
            control_groups=("ai_objects", "reserved_keys"),
        )
        self.core.connect()

    def last_call(self, name):
        return self.sdk.matching_calls(name)[-1][0]

    def create(self):
        result = self.first.submit_simulator_operation(Operation.CREATE_PARKED_AIRCRAFT, "Plane", "ABC", "YSSY")
        self.core.pump()
        return result, self.last_call("SimConnect_AICreateParkedATCAircraft")[-1]

    def test_bad_object_field_write_is_rejected_without_ending_the_session(self):
        result, callback = self.create()
        self.sdk.incoming.append(packet(ReceiveAssignedObject, ReceiveId.ASSIGNED_OBJECT_ID,
                                        request_id=callback, object_id=77))
        self.core.pump()
        target = self.first.command_result(result.id).data
        field = ObjectField("NOT A REAL VAR", ObjectFormat.XYZ)
        self.first.request_object_data(field, target)
        self.core.pump()
        (_, sent), = self.sdk.matching_calls("SimConnect_AddToDataDefinition")
        self.sdk.exception(sent)
        self.core.pump()
        write = self.first.submit_simulator_operation(Operation.WRITE_OBJECT_DATA, target, field, bytes(24))
        self.core.pump()
        self.core.pump()
        self.assertIs(self.first.command_result(write.id).state, CommandState.REJECTED)
        self.assertIs(self.first.connection().state, ConnectionState.CONNECTED)

    def test_late_assigned_object_is_adopted_and_removed(self):
        result, callback = self.create()
        self.advance(4.5)
        self.core.pump()
        self.assertIs(self.first.command_result(result.id).state, CommandState.UNVERIFIED)
        self.sdk.incoming.append(packet(ReceiveAssignedObject, ReceiveId.ASSIGNED_OBJECT_ID,
                                        request_id=callback, object_id=88))
        with self.assertLogs("bbc_core.simconnect_advanced", "WARNING"):
            self.core.pump()
        self.assertEqual(self.last_call("SimConnect_AIRemoveObject")[0], 88)

    def test_hardware_input_events_join_a_notification_group(self):
        subscription = self.first.subscribe_controller(ControllerInput(0, "button", 2))
        self.core.pump()
        adds = [args for args, _ in self.sdk.matching_calls("SimConnect_AddClientEventToNotificationGroup")]
        mapping = self.last_call("SimConnect_MapInputEventToClientEvent_EX1")
        self.assertEqual({args[1] for args in adds}, {mapping[2], mapping[4]})
        notify_group = adds[0][0]
        self.assertNotEqual(notify_group, mapping[0])
        self.sdk.incoming.append(packet(ReceiveEvent, ReceiveId.EVENT, event_id=mapping[2],
                                        group_id=notify_group, data=1))
        self.core.pump()
        events = [item for item in self.first.poll() if isinstance(item, Event)]
        self.assertEqual([(item.value.value, item.value.pressed) for item in events], [(1, True)])
        self.first.unsubscribe(subscription.id)
        self.core.pump()

    def test_reserved_key_presses_are_delivered_as_a_client_event(self):
        command = self.first.submit_simulator_operation(Operation.RESERVE_KEY, "q")
        self.core.pump()
        private = self.last_call("SimConnect_RequestReservedKey")[0]
        self.sdk.incoming.append(packet(ReceiveReservedKey, ReceiveId.RESERVED_KEY, choice=b"q", key=b"Tab+Q"))
        self.core.pump()
        result = self.first.command_result(command.id)
        self.assertIs(result.state, CommandState.CONFIRMED)
        self.assertEqual(result.data, "Tab+Q")
        self.assertIn("RESERVED_KEY_TAB_Q", result.detail)
        maps_before = len(self.sdk.matching_calls("SimConnect_MapClientEventToSimEvent"))
        self.first.subscribe_client_event("RESERVED_KEY_TAB_Q")
        self.core.pump()
        self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_MapClientEventToSimEvent")), maps_before)
        group, event, _ = self.last_call("SimConnect_AddClientEventToNotificationGroup")
        self.assertEqual(event, private)
        self.sdk.incoming.append(packet(ReceiveEvent, ReceiveId.EVENT, event_id=private, group_id=group, data=0))
        self.core.pump()
        presses = [item for item in self.first.poll() if isinstance(item, Event)]
        self.assertEqual([item.value for item in presses], [ClientEventData((0,))])


class AlreadyCreatedSdk(ClientDataSdk):
    def call(self, name, handle, *args):
        if name == "SimConnect_CreateClientData" and self.names.get(args[0]) in self.areas:
            FakeSdk.call(self, name, handle, *args)
            self.incoming.append(packet(ReceiveException, ReceiveId.EXCEPTION, exception=EXCEPTION_ALREADY_CREATED,
                                        send_id=self.packet_id, index=1))
            return
        super().call(name, handle, *args)


class ClientDataTests(FakeClock, TestCase):
    """Findings A49, A50 and A102."""

    def setUp(self):
        self.start_clock()
        self.sdk = AlreadyCreatedSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.module = self.core.register_consumer(control_groups=("client_data",))

    def test_already_created_area_after_reconnect_stays_usable(self):
        area = ClientDataArea("owned", 8, create=True, writable=True)
        self.core.register_client_data(area)
        subscription = self.module.subscribe_client_data(area.region(0, 4))
        self.core.connect()
        self.core.pump()
        self.core.disconnect()
        self.core.connect()
        self.core.pump()
        self.core.pump()
        self.assertEqual(self.module.latest(subscription.id).quality, Quality.CURRENT)
        self.assertIs(self.module.connection().state, ConnectionState.CONNECTED)

    def test_unsubscribe_after_failed_area_never_ends_the_session(self):
        area = ClientDataArea("missing", 8)
        self.core.register_client_data(area)
        subscription = self.module.subscribe_client_data(area.region(0, 4))
        self.core.connect()
        self.core.pump()
        self.core.pump()
        self.assertIs(self.module.latest(subscription.id).quality, Quality.UNAVAILABLE)
        self.module.unsubscribe(subscription.id)
        self.core.pump()
        for _, sent in self.sdk.matching_calls("SimConnect_ClearClientDataDefinition"):
            self.sdk.exception(sent)
        self.core.pump()
        self.assertIs(self.module.connection().state, ConnectionState.CONNECTED)

    def test_padded_payload_is_accepted(self):
        area = ClientDataArea("external", 8)
        self.core.register_client_data(area)
        subscription = self.module.subscribe_client_data(area.region(1, 3))
        self.core.connect()
        self.core.pump()
        ((request, (identifier, definition)),) = self.sdk.streams.items()
        self.sdk.deliver(identifier, request, definition, payload=b"XYZ\0")
        self.core.pump()
        self.assertEqual(self.module.latest(subscription.id).value, b"XYZ")
