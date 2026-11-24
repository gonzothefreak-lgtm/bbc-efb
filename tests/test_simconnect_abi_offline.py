"""Match the starter ctypes declarations to MSFS 2020 SDK 0.24.6.0."""

from ctypes import POINTER, alignment, c_bool, c_char, c_char_p, c_double, c_float, c_int32, c_void_p, sizeof
from unittest import TestCase

from bbc_core import simconnect_abi as abi


class SimConnectAbiOfflineTests(TestCase):
    def test_packed_receive_structures_match_header_field_offsets(self):
        layouts = (
            (abi.Receive, 12, {"size": 0, "version": 4, "id": 8}),
            (abi.ReceiveException, 24, {"exception": 12, "send_id": 16, "index": 20}),
            (abi.ReceiveOpen, 308, {
                "application_name": 12, "application_version_major": 268,
                "simconnect_version_major": 284, "reserved_2": 304,
            }),
            (abi.ReceiveEvent, 24, {"group_id": 12, "event_id": 16, "data": 20}),
            (abi.ReceiveEventEx1, 40, {"group_id": 12, "event_id": 16, "data": 20, "remaining_words": 24}),
            (abi.ReceiveAssignedObject, 20, {"request_id": 12, "object_id": 16}),
            (abi.ReceiveReservedKey, 92, {"choice": 12, "key": 42}),
            (abi.ReceiveActionCallback, 276, {"action": 12, "request_id": 272}),
            (abi.NativeInitialPosition, 56, {"latitude": 0, "heading": 40, "on_ground": 48, "airspeed": 52}),
            (abi.ReceiveEventFilename, 288, {"filename": 24, "flags": 284}),
            (abi.ReceiveEventFrame, 32, {"frame_rate": 24, "simulation_rate": 28}),
            (abi.ReceiveEventObject, 28, {"object_type": 24}),
            (abi.ReceiveFacilityDataPrefix, 40, {
                "request_id": 12, "unique_id": 16, "parent_id": 20, "facility_type": 24,
                "is_list_item": 28, "item_index": 32, "list_size": 36,
            }),
            (abi.ReceiveCustomActionPrefix, 44, {"instance_id": 24, "wait_for_completion": 40}),
            (abi.ReceiveSystemState, 284, {"request_id": 12, "integer": 16, "number": 20, "text": 24}),
            (abi.ReceiveSimObjectDataPrefix, 40, {
                "request_id": 12, "object_id": 16, "definition_id": 20, "flags": 24,
                "entry_number": 28, "out_of": 32, "definition_count": 36,
            }),
            (abi.ReceiveListPrefix, 28, {
                "request_id": 12, "array_size": 16, "entry_number": 20, "out_of": 24,
            }),
            (abi.InputEventDescriptor, 76, {"name": 0, "hash": 64, "type": 72}),
            (abi.ReceiveGetInputPrefix, 20, {"request_id": 12, "type": 16}),
            (abi.ReceiveSubscribeInputPrefix, 24, {"hash": 12, "type": 20}),
            (abi.ReceiveInputParameters, 280, {"hash": 12, "value": 20}),
        )
        for structure, size, fields in layouts:
            with self.subTest(structure=structure.__name__):
                self.assertEqual(sizeof(structure), size)
                self.assertEqual(alignment(structure), 1)
                for field, offset in fields.items():
                    self.assertEqual(getattr(structure, field).offset, offset)

    def test_x64_windows_types_and_initial_enum_values(self):
        self.assertEqual(sizeof(abi.HANDLE), 8)
        self.assertEqual(sizeof(abi.DWORD), 4)
        self.assertEqual(sizeof(abi.HRESULT), 4)
        self.assertEqual(abi.HRESULT(-1).value, -1)
        self.assertEqual(sizeof(abi.UINT64), 8)
        self.assertEqual([item.value for item in abi.ReceiveId],
                         [1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                          21, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37])
        self.assertEqual([item.value for item in abi.FacilityDataType], [0, 1, 15, 18, 19, 20, 21])
        self.assertEqual([item.value for item in abi.FacilityListType], [0, 1, 2, 3])
        self.assertEqual(sizeof(c_bool), 1)
        self.assertEqual([item.value for item in abi.DataType], list(range(1, 17)))
        self.assertEqual([item.value for item in abi.Period], [0, 1, 2, 3, 4])
        self.assertEqual([item.value for item in abi.ClientDataPeriod], [0, 1, 2, 3, 4])

    def test_initial_signatures_match_the_header(self):
        handle, word = abi.HANDLE, abi.DWORD
        expected = {
            "SimConnect_Open": (POINTER(handle), c_char_p, handle, word, handle, word),
            "SimConnect_Close": (handle,),
            "SimConnect_CallDispatch": (handle, abi.DispatchProc, c_void_p),
            "SimConnect_RequestResponseTimes": (handle, word, POINTER(c_float)),
            "SimConnect_RetrieveString": (POINTER(abi.Receive), word, c_void_p, POINTER(c_char_p), POINTER(word)),
            "SimConnect_InsertString": (c_void_p, word, POINTER(c_void_p), POINTER(word), c_char_p),
            "SimConnect_RequestNotificationGroup": (handle, word, word, word),
            "SimConnect_EnumerateControllers": (handle,),
            "SimConnect_MapInputEventToClientEvent_EX1": (handle, word, c_char_p, word, word, word, word, c_int32),
            "SimConnect_SetInputGroupPriority": (handle, word, word),
            "SimConnect_RemoveInputEvent": (handle, word, c_char_p),
            "SimConnect_ClearInputGroup": (handle, word),
            "SimConnect_SetInputGroupState": (handle, word, word),
            "SimConnect_RequestReservedKey": (handle, word, c_char_p, c_char_p, c_char_p),
            "SimConnect_RequestDataOnSimObjectType": (handle, word, word, word, word),
            "SimConnect_AICreateParkedATCAircraft": (handle, c_char_p, c_char_p, c_char_p, word),
            "SimConnect_AICreateEnrouteATCAircraft": (handle, c_char_p, c_char_p, c_int32, c_char_p, c_double, c_int32, word),
            "SimConnect_AICreateNonATCAircraft": (handle, c_char_p, c_char_p, abi.NativeInitialPosition, word),
            "SimConnect_AICreateSimulatedObject": (handle, c_char_p, abi.NativeInitialPosition, word),
            "SimConnect_AIReleaseControl": (handle, word, word),
            "SimConnect_AIRemoveObject": (handle, word, word),
            "SimConnect_AISetAircraftFlightPlan": (handle, word, c_char_p, word),
            "SimConnect_FlightLoad": (handle, c_char_p),
            "SimConnect_FlightSave": (handle, c_char_p, c_char_p, c_char_p, word),
            "SimConnect_FlightPlanLoad": (handle, c_char_p),
            "SimConnect_ExecuteAction": (handle, word, c_char_p, word, c_void_p),
            "SimConnect_SubscribeToFacilities": (handle, word, word),
            "SimConnect_UnsubscribeToFacilities": (handle, word),
            "SimConnect_AddToFacilityDefinition": (handle, word, c_char_p),
            "SimConnect_AddFacilityDataDefinitionFilter": (handle, word, c_char_p, word, c_void_p),
            "SimConnect_ClearAllFacilityDataDefinitionFilters": (handle, word),
            "SimConnect_RequestFacilityData": (handle, word, word, c_char_p, c_char_p),
            "SimConnect_RequestFacilityData_EX1": (handle, word, word, c_char_p, c_char_p, c_char),
            "SimConnect_RequestFacilitiesList_EX1": (handle, word, word),
            "SimConnect_RequestFacilitiesList": (handle, word, word),
            "SimConnect_SubscribeToFacilities_EX1": (handle, word, word, word),
            "SimConnect_UnsubscribeToFacilities_EX1": (handle, word, c_bool, c_bool),
            "SimConnect_RequestJetwayData": (handle, c_char_p, word, POINTER(c_int32)),
            "SimConnect_GetNextDispatch": (handle, POINTER(POINTER(abi.Receive)), POINTER(word)),
            "SimConnect_GetLastSentPacketID": (handle, POINTER(word)),
            "SimConnect_AddToDataDefinition": (handle, word, c_char_p, c_char_p, word, c_float, word),
            "SimConnect_ClearDataDefinition": (handle, word),
            "SimConnect_RequestDataOnSimObject": (handle,) + (word,) * 8,
            "SimConnect_SetDataOnSimObject": (handle,) + (word,) * 5 + (c_void_p,),
            "SimConnect_SubscribeToSystemEvent": (handle, word, c_char_p),
            "SimConnect_UnsubscribeFromSystemEvent": (handle, word),
            "SimConnect_SetSystemEventState": (handle, word, word),
            "SimConnect_RequestSystemState": (handle, word, c_char_p),
            "SimConnect_MapClientEventToSimEvent": (handle, word, c_char_p),
            "SimConnect_AddClientEventToNotificationGroup": (handle, word, word, c_int32),
            "SimConnect_RemoveClientEvent": (handle, word, word),
            "SimConnect_SetNotificationGroupPriority": (handle, word, word),
            "SimConnect_ClearNotificationGroup": (handle, word),
            "SimConnect_TransmitClientEvent": (handle,) + (word,) * 5,
            "SimConnect_TransmitClientEvent_EX1": (handle,) + (word,) * 9,
            "SimConnect_EnumerateInputEvents": (handle, word),
            "SimConnect_EnumerateInputEventParams": (handle, abi.UINT64),
            "SimConnect_GetInputEvent": (handle, word, abi.UINT64),
            "SimConnect_SetInputEvent": (handle, abi.UINT64, word, c_void_p),
            "SimConnect_SubscribeInputEvent": (handle, abi.UINT64),
            "SimConnect_UnsubscribeInputEvent": (handle, abi.UINT64),
            "SimConnect_MapClientDataNameToID": (handle, c_char_p, word),
            "SimConnect_CreateClientData": (handle, word, word, word),
            "SimConnect_AddToClientDataDefinition": (handle, word, word, word, c_float, word),
            "SimConnect_ClearClientDataDefinition": (handle, word),
            "SimConnect_RequestClientData": (handle,) + (word,) * 8,
            "SimConnect_SetClientData": (handle,) + (word,) * 5 + (c_void_p,),
        }
        self.assertEqual(abi.SIGNATURES, expected)
