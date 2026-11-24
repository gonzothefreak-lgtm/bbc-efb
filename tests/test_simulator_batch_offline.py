"""End-to-end checks for the advanced offline SimConnect batch."""

from ctypes import POINTER, byref, c_void_p, cast, memmove, string_at
from struct import pack
from unittest import TestCase
from unittest.mock import patch

from bbc_core import (
    AirportFilter, AirportFilterField, ClientDataArea, ClientDataMode, CommandState, ControllerInput, Core,
    CoreRequestError, Event, FacilityKind, InitialPosition, ObjectField, ObjectFormat, ObjectKind,
    SimulatorOperation as Operation, SimulatorReadState,
)
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import (
    DWORD, ClientDataPeriod, Receive, ReceiveActionCallback, ReceiveAssignedObject, ReceiveEvent,
    ReceiveId, ReceiveListPrefix, ReceiveReservedKey, ReceiveSimObjectDataPrefix,
)
from bbc_core.simconnect_sdk import SimConnectSdk
from test_client_data_offline import ClientDataSdk
from test_simconnect_provider_offline import FakeSdk, packet


class SimulatorBatchOfflineTests(TestCase):
    def setUp(self):
        self.now = 100.0
        for module in ("core", "simconnect", "simconnect_advanced"):
            clock = patch(f"bbc_core.{module}.monotonic", side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.sdk = FakeSdk()
        self.sdk.supply_radio_reads = self.sdk.apply_write = False
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.first = self.core.register_consumer(
            control_groups=("ai_objects", "flight_files", "reserved_keys", "simulator_actions"),
        )
        self.core.connect()

    def last_call(self, function):
        return self.sdk.matching_calls(function)[-1][0]

    def finish_without_response(self, result):
        self.now += 4.5
        self.core.pump()
        return self.first.command_result(result.id)

    def test_experimental_calls_dispatch_and_report_unknown_effect_when_silent(self):
        for operation, args, function, expected in (
            (Operation.SAVE_FLIGHT, (r"C:\Flights\ test.flt", "Test", ""), "SimConnect_FlightSave",
             (b"C:\\Flights\\ test.flt", b"Test", b"", 0)),
            (Operation.LOAD_FLIGHT_PLAN, (r"C:\Flights\route.pln",), "SimConnect_FlightPlanLoad",
             (b"C:\\Flights\\route.pln",)),
            (Operation.RESERVE_KEY, ("q", "Q"), "SimConnect_RequestReservedKey", (b"q", b"Q", b"")),
        ):
            result = self.first.submit_simulator_operation(operation, *args)
            self.core.pump()
            call = self.last_call(function)
            self.assertEqual(call[1:] if operation is Operation.RESERVE_KEY else call, expected)
            completed = self.finish_without_response(result)
            self.assertIs(completed.state, CommandState.UNVERIFIED)
            self.assertIsNone(completed.data)
            self.assertIn("no response", completed.detail)
        denied = self.first.submit_simulator_operation(Operation.RESERVE_KEY, "x")
        self.core.pump()
        self.assertIs(self.first.command_result(denied.id).state, CommandState.REJECTED)

    def test_key_callback_and_file_and_action_arguments_are_scoped(self):
        command = self.first.submit_simulator_operation(Operation.RESERVE_KEY, "q", "Q")
        self.core.pump()
        self.sdk.incoming.append(packet(ReceiveReservedKey, ReceiveId.RESERVED_KEY, choice=b"q", key=b"TAB+Q"))
        self.core.pump()
        result = self.first.command_result(command.id)
        self.assertIs(result.state, CommandState.CONFIRMED)
        self.assertEqual(result.data, "TAB+Q")
        denied = self.first.submit_simulator_operation(Operation.LOAD_FLIGHT, r"\\server\folder\x.flt")
        self.assertIs(denied.state, CommandState.REJECTED)
        result = self.first.submit_simulator_operation(Operation.LOAD_FLIGHT, r"C:\Flights\one.flt")
        self.core.pump()
        self.assertEqual(self.last_call("SimConnect_FlightLoad"), (b"C:\\Flights\\one.flt",))
        self.assertIs(self.finish_without_response(result).state, CommandState.UNVERIFIED)
        action = self.first.submit_simulator_operation(Operation.EXECUTE_ACTION, "TestAction", b"\0\x20")
        self.core.pump()
        id, name, size, payload = self.last_call("SimConnect_ExecuteAction")
        self.assertEqual((name, size, string_at(payload, size)), (b"TestAction", 2, b"\0\x20"))
        self.sdk.incoming.append(packet(ReceiveActionCallback, ReceiveId.ACTION_CALLBACK, action=name, request_id=id))
        self.core.pump()
        self.assertIs(self.first.command_result(action.id).state, CommandState.UNVERIFIED)

    def test_controller_discovery_and_shared_passive_button_and_axis_mapping(self):
        result = self.first.request_controllers()
        peer = self.core.register_consumer()
        self.assertEqual(peer.request_controllers().id, result.id)
        button = ControllerInput(0, "button", 2)
        axis = ControllerInput(0, "xaxis")
        first_button = self.first.subscribe_controller(button)
        self.first.subscribe_controller(axis)
        peer.subscribe_controller(button)
        self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_EnumerateControllers")), 1)
        controller = pack("<256sIII4H", b"Test Stick", 0, 123, 456, 1, 0, 2, 3)
        self.sdk.incoming.append(packet(ReceiveListPrefix, ReceiveId.CONTROLLERS_LIST, controller,
                                        request_id=0, array_size=1, entry_number=0, out_of=1))
        maps = {args[1]: args for args, _ in self.sdk.matching_calls("SimConnect_MapInputEventToClientEvent_EX1")}
        self.assertEqual(len(maps), 2)
        button_map = maps[b"joystick:0:button:2"]
        axis_map = maps[b"joystick:0:XAxis"]
        self.assertEqual((button_map[-1], axis_map[-1]), (0, 0))
        self.assertEqual(self.last_call("SimConnect_SetInputGroupState"), (button_map[0], 1))
        for mapping, index, value in ((button_map, 2, 1), (button_map, 4, 0), (axis_map, 2, 0xFFFF8000)):
            self.sdk.incoming.append(packet(ReceiveEvent, ReceiveId.EVENT, event_id=mapping[index],
                                            group_id=mapping[0], data=value))
        self.core.pump()
        self.assertEqual(peer.simulator_result(result.id).data[0].version, (1, 0, 2, 3))
        received = [item.value for item in self.first.poll() if isinstance(item, Event)]
        self.assertEqual([(item.value, item.pressed) for item in received], [(1, True), (0, False), (-32768, None)])
        self.first.unsubscribe(first_button.id)
        peer.close()
        self.core.pump()
        self.assertEqual(self.last_call("SimConnect_RemoveInputEvent"), (button_map[0], b"joystick:0:button:2"))
        self.first.close()
        self.core.pump()
        self.assertEqual(self.last_call("SimConnect_ClearInputGroup"), (button_map[0],))

    def test_ai_creation_and_owned_commands_preserve_assigned_ids(self):
        position = InitialPosition(-33.0, 151.0, 14.0, heading_deg=92, on_ground=True)
        specs = (
            (Operation.CREATE_PARKED_AIRCRAFT, ("Plane", "ABC", "YSSY"), "SimConnect_AICreateParkedATCAircraft"),
            (Operation.CREATE_ENROUTE_AIRCRAFT, ("Plane", "ABC", 7, r"C:\Flights\route.pln", 1.5, True),
             "SimConnect_AICreateEnrouteATCAircraft"),
            (Operation.CREATE_NON_ATC_AIRCRAFT, ("Plane", "ABC", position), "SimConnect_AICreateNonATCAircraft"),
            (Operation.CREATE_SIMULATED_OBJECT, ("Ground Object", position), "SimConnect_AICreateSimulatedObject"),
        )
        for index, (operation, arguments, function) in enumerate(specs):
            result = self.first.submit_simulator_operation(operation, *arguments)
            self.core.pump()
            call = self.last_call(function)
            if index >= 2:
                self.assertEqual(bytes(call[-2]), position.packed())
            self.sdk.incoming.append(packet(ReceiveAssignedObject, ReceiveId.ASSIGNED_OBJECT_ID,
                                            request_id=call[-1], object_id=100 + index))
            self.core.pump()
            result = self.first.command_result(result.id)
            self.assertIs(result.state, CommandState.CONFIRMED)
            self.assertEqual(result.data.object_id, 100 + index)
        target = result.data
        other = self.core.register_consumer(control_groups=("ai_objects",))
        wrong = other.submit_simulator_operation(Operation.REMOVE_OBJECT, target)
        self.core.pump()
        self.assertIs(other.command_result(wrong.id).state, CommandState.REJECTED)
        for operation, arguments, function in (
            (Operation.WRITE_OBJECT_DATA, (target, ObjectField("INITIAL POSITION", ObjectFormat.POSITION), position),
             "SimConnect_SetDataOnSimObject"),
            (Operation.RELEASE_AI, (target,), "SimConnect_AIReleaseControl"),
            (Operation.SET_AI_FLIGHT_PLAN, (target, r"C:\Flights\route.pln"), "SimConnect_AISetAircraftFlightPlan"),
            (Operation.REMOVE_OBJECT, (target,), "SimConnect_AIRemoveObject"),
        ):
            command = self.first.submit_simulator_operation(operation, *arguments)
            self.core.pump()
            call = self.last_call(function)
            self.assertEqual(call[1] if operation is Operation.WRITE_OBJECT_DATA else call[0], 103)
            self.assertIs(self.finish_without_response(command).state, CommandState.UNVERIFIED)
        again = self.first.submit_simulator_operation(Operation.REMOVE_OBJECT, target)
        self.core.pump()
        self.assertIs(self.first.command_result(again.id).state, CommandState.REJECTED)

    def test_object_query_assembles_nearby_records_and_reads_variable_text(self):
        field = ObjectField("PLANE LATITUDE LONGITUDE ALTITUDE", ObjectFormat.LATLONALT)
        query = self.first.request_nearby_objects(ObjectKind.AIRCRAFT, 5000, field)
        self.core.pump()
        req, definition, radius, kind = self.last_call("SimConnect_RequestDataOnSimObjectType")
        self.assertEqual((radius, kind), (5000, 2))
        for index in (2, 1):
            self.sdk.incoming.append(packet(
                ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA_BYTYPE, pack("<3d", index, 151, 23),
                request_id=req, object_id=8 + index, definition_id=definition,
                entry_number=index, out_of=2, definition_count=3,
            ))
        self.core.pump()
        results = self.first.simulator_result(query.id).data
        self.assertEqual([sample.object.object_id for sample in results], [9, 10])
        text = self.first.request_object_data(ObjectField("TITLE", ObjectFormat.VARIABLE_TEXT), results[0].object)
        self.core.pump()
        req, definition, target, *_ = self.last_call("SimConnect_RequestDataOnSimObject")
        self.assertEqual(target, 9)
        self.sdk.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, b"Test aircraft\0",
            request_id=req, object_id=9, definition_id=definition, definition_count=1,
        ))
        self.core.pump()
        self.assertEqual(self.first.simulator_result(text.id).data.value, "Test aircraft")

    def test_dialog_notification_dispatch_requires_a_reader_and_uses_the_group(self):
        with self.assertRaises(CoreRequestError):
            self.first.request_dialog_notifications()
        self.first.subscribe_client_event("PARKING_BRAKES")
        self.core.pump()
        request = self.first.request_dialog_notifications()
        self.core.pump()
        group = self.last_call("SimConnect_AddClientEventToNotificationGroup")[0]
        self.assertEqual(self.last_call("SimConnect_RequestNotificationGroup"), (group, 0, 0))
        self.assertIs(self.first.simulator_result(request.id).state, SimulatorReadState.READY)

    def test_legacy_facility_mode_has_additions_only_and_typed_filter_payloads(self):
        legacy_sdk = FakeSdk()
        legacy = Core(SimConnectProvider(legacy_sdk, legacy_facility_additions=True))
        self.addCleanup(legacy.disconnect)
        reader = legacy.register_consumer()
        watch = reader.subscribe_facility_cache(FacilityKind.AIRPORT)
        legacy.connect()
        legacy.pump()
        self.assertEqual(len(legacy_sdk.matching_calls("SimConnect_SubscribeToFacilities")), 1)
        self.assertFalse(legacy_sdk.matching_calls("SimConnect_SubscribeToFacilities_EX1"))
        reader.unsubscribe(watch.id)
        legacy.pump()
        self.assertEqual(len(legacy_sdk.matching_calls("SimConnect_UnsubscribeToFacilities")), 1)
        rules = (AirportFilter(AirportFilterField.AIRPORT_NAME, "Test Airport"),
                 AirportFilter(AirportFilterField.RUNWAY_LENGTH, 2400.25))
        self.first.request_airport("YSSY", filters=rules)
        self.core.pump()
        filters = {args[1]: string_at(args[3], args[2]) for args, _ in
                   self.sdk.matching_calls("SimConnect_AddFacilityDataDefinitionFilter")}
        self.assertEqual(filters[b"AIRPORT:NAME64"], pack("<64s", b"Test Airport"))
        self.assertEqual(filters[b"AIRPORT:RUNWAY:LENGTH"], pack("<f", 2400.25))

    def test_client_data_formats_share_equal_demand_and_support_explicit_modes(self):
        sdk = ClientDataSdk()
        core = Core(SimConnectProvider(sdk))
        self.addCleanup(core.disconnect)
        host_area = ClientDataArea("EXTERNAL", 8)
        core.register_client_data(host_area)
        reader = core.register_consumer()
        region = host_area.region(2, 4)
        for mode in ClientDataMode:
            reader.subscribe_client_data(region, mode=mode)
        core.connect()
        core.pump()
        self.assertEqual(len(sdk.matching_calls("SimConnect_MapClientDataNameToID")), 1)
        periods = [args[3] for args, _ in sdk.matching_calls("SimConnect_RequestClientData")]
        self.assertEqual(periods.count(ClientDataPeriod.ONCE), 4)
        self.assertEqual(set(periods), {ClientDataPeriod.ONCE, ClientDataPeriod.SECOND,
                                        ClientDataPeriod.ON_SET, ClientDataPeriod.VISUAL_FRAME})

    def test_host_only_callback_timing_and_string_helpers_use_in_memory_buffers(self):
        sdk = SimConnectSdk(r"C:\SDK\SimConnect.dll", callback_dispatch=True)
        response = Receive(12, 1, ReceiveId.QUIT)

        def dispatch(_handle, callback, context):
            callback(byref(response), 12, context)
            return 0

        def timings(_handle, count, values):
            values[:] = [float(index + 1) for index in range(count)]
            return 0

        def retrieve(_packet, _length, start, result, length):
            cast(result, POINTER(c_void_p))[0] = start
            cast(length, POINTER(DWORD))[0] = 4
            return 0

        def insert(destination, _capacity, end, length, value):
            memmove(destination, value + b"\0", len(value) + 1)
            cast(end, POINTER(c_void_p))[0] = destination + len(value) + 1
            cast(length, POINTER(DWORD))[0] = len(value)
            return 0

        sdk._functions = {"SimConnect_CallDispatch": dispatch, "SimConnect_RequestResponseTimes": timings,
                          "SimConnect_RetrieveString": retrieve, "SimConnect_InsertString": insert}
        self.assertEqual(sdk.next_packet(None), bytes(response))
        self.assertEqual(sdk.response_times(None), (1, 2, 3, 4, 5))
        self.assertEqual(sdk.retrieve_string(bytes(response) + b"abc\0", 12), b"abc")
        self.assertEqual(sdk.insert_string(b"abc"), b"abc\0")
