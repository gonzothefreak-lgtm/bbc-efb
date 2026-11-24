"""Match the starter ctypes declarations to MSFS 2020 SDK 0.24.6.0."""

from ctypes import alignment, c_bool, sizeof
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

    # The former test_initial_signatures_match_the_header compared SIGNATURES with a
    # hand-written copy of itself, so it proved nothing about the header (A72). The real
    # checks are test_adapter_abi.AbiPrototypeTests: every argument is classified by its
    # C type from the SimConnect.h prototypes, and the installed header is parsed when
    # an MSFS SDK is present (skipped otherwise).
