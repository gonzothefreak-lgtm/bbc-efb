"""First ABI declarations from the installed MSFS 2020 SDK 0.24.6.0 header.

This module describes packet layouts and signatures for the adapter. It
performs no DLL loading or calls. Packed structures declare the MSVC layout
explicitly (``_layout_ = "ms"``) because SimConnect.h uses ``#pragma pack(1)``
with MSVC rules; Python 3.14+ requires the explicit choice on non-Windows hosts.
Sizes and offsets are checked offline in tests/test_simconnect_abi_offline.py.
"""

from __future__ import annotations

import ctypes
from ctypes import (
    POINTER, Structure, c_bool, c_char, c_char_p, c_double, c_float, c_int32,
    c_uint8, c_uint32, c_uint64, c_void_p,
)
from enum import IntEnum


DWORD = c_uint32
UINT64 = c_uint64
HANDLE = c_void_p
HRESULT = c_int32


class ReceiveId(IntEnum):
    EXCEPTION = 1
    OPEN = 2
    QUIT = 3
    EVENT = 4
    EVENT_OBJECT = 5
    EVENT_FILENAME = 6
    EVENT_FRAME = 7
    SIMOBJECT_DATA = 8
    SIMOBJECT_DATA_BYTYPE = 9
    ASSIGNED_OBJECT_ID = 12
    RESERVED_KEY = 13
    CUSTOM_ACTION = 14
    SYSTEM_STATE = 15
    CLIENT_DATA = 16
    EVENT_WEATHER_MODE = 17
    AIRPORT_LIST = 18
    VOR_LIST = 19
    NDB_LIST = 20
    WAYPOINT_LIST = 21
    EVENT_EX1 = 27
    FACILITY_DATA = 28
    FACILITY_DATA_END = 29
    FACILITY_MINIMAL_LIST = 30
    JETWAY_DATA = 31
    CONTROLLERS_LIST = 32
    ACTION_CALLBACK = 33
    ENUMERATE_INPUT_EVENTS = 34
    GET_INPUT_EVENT = 35
    SUBSCRIBE_INPUT_EVENT = 36
    ENUMERATE_INPUT_EVENT_PARAMS = 37


class DataType(IntEnum):
    INT32 = 1
    INT64 = 2
    FLOAT32 = 3
    FLOAT64 = 4
    STRING8 = 5
    STRING32 = 6
    STRING64 = 7
    STRING128 = 8
    STRING256 = 9
    STRING260 = 10
    STRINGV = 11
    INITPOSITION = 12
    MARKERSTATE = 13
    WAYPOINT = 14
    LATLONALT = 15
    XYZ = 16


class Period(IntEnum):
    NEVER = 0
    ONCE = 1
    VISUAL_FRAME = 2
    SIM_FRAME = 3
    SECOND = 4


class ClientDataPeriod(IntEnum):
    NEVER = 0
    ONCE = 1
    VISUAL_FRAME = 2
    ON_SET = 3
    SECOND = 4


# SIMCONNECT_DATA_REQUEST_FLAG_* and SIMCONNECT_CLIENT_DATA_REQUEST_FLAG_*.
DATA_REQUEST_FLAG_CHANGED = 0x1
DATA_REQUEST_FLAG_TAGGED = 0x2
# SIMCONNECT_EXCEPTION values used for specific recovery decisions.
EXCEPTION_NAME_UNRECOGNIZED = 7
EXCEPTION_ALREADY_CREATED = 32
# SimConnect_GetNextDispatch returns E_FAIL when no message is waiting.
E_FAIL = -2147467259  # 0x80004005 as a signed HRESULT
SIMCONNECT_UNUSED = 0xFFFFFFFF
UNKNOWN_GROUP = 0xFFFFFFFF


class Receive(Structure):
    _layout_ = "ms"
    _pack_ = 1
    _fields_ = [
        ("size", DWORD),
        ("version", DWORD),
        ("id", DWORD),
    ]


class ReceiveException(Receive):
    _fields_ = [
        ("exception", DWORD),
        ("send_id", DWORD),
        ("index", DWORD),
    ]


class ReceiveOpen(Receive):
    _fields_ = [
        ("application_name", c_char * 256),
        ("application_version_major", DWORD),
        ("application_version_minor", DWORD),
        ("application_build_major", DWORD),
        ("application_build_minor", DWORD),
        ("simconnect_version_major", DWORD),
        ("simconnect_version_minor", DWORD),
        ("simconnect_build_major", DWORD),
        ("simconnect_build_minor", DWORD),
        ("reserved_1", DWORD),
        ("reserved_2", DWORD),
    ]


class ReceiveEventHeader(Receive):
    _fields_ = [
        ("group_id", DWORD),
        ("event_id", DWORD),
    ]


class ReceiveEvent(ReceiveEventHeader):
    _fields_ = [("data", DWORD)]


class ReceiveEventEx1(ReceiveEvent):
    _fields_ = [("remaining_words", DWORD * 4)]


class ReceiveEventFilename(ReceiveEvent):
    _fields_ = [
        ("filename", c_char * 260),
        ("flags", DWORD),
    ]


class ReceiveEventFrame(ReceiveEvent):
    _fields_ = [("frame_rate", c_float), ("simulation_rate", c_float)]


class ReceiveEventObject(ReceiveEvent):
    _fields_ = [("object_type", DWORD)]


class ReceiveCustomActionPrefix(ReceiveEvent):
    _fields_ = [("instance_id", c_uint8 * 16), ("wait_for_completion", DWORD)]


class ReceiveRequest(Receive):
    _fields_ = [("request_id", DWORD)]


class ReceiveAssignedObject(ReceiveRequest):
    _fields_ = [("object_id", DWORD)]


class ReceiveReservedKey(Receive):
    _fields_ = [("choice", c_char * 30), ("key", c_char * 50)]


class ReceiveActionCallback(Receive):
    _fields_ = [("action", c_char * 260), ("request_id", DWORD)]


class NativeInitialPosition(Structure):
    _layout_ = "ms"
    _pack_ = 1
    _fields_ = [(name, c_double) for name in ("latitude", "longitude", "altitude", "pitch", "bank", "heading")]
    _fields_ += [("on_ground", DWORD), ("airspeed", DWORD)]


class ReceiveSystemState(ReceiveRequest):
    _fields_ = [
        ("integer", DWORD),
        ("number", c_float),
        ("text", c_char * 260),
    ]


class ReceiveSimObjectDataPrefix(Receive):
    """Prefix up to the SDK's trailing dwData marker. Payload follows it."""

    _fields_ = [
        ("request_id", DWORD),
        ("object_id", DWORD),
        ("definition_id", DWORD),
        ("flags", DWORD),
        ("entry_number", DWORD),
        ("out_of", DWORD),
        ("definition_count", DWORD),
    ]


class ReceiveListPrefix(Receive):
    _fields_ = [
        ("request_id", DWORD),
        ("array_size", DWORD),
        ("entry_number", DWORD),
        ("out_of", DWORD),
    ]


class ReceiveFacilityDataPrefix(ReceiveRequest):
    _fields_ = [
        ("unique_id", DWORD),
        ("parent_id", DWORD),
        ("facility_type", DWORD),
        ("is_list_item", DWORD),
        ("item_index", DWORD),
        ("list_size", DWORD),
    ]


class FacilityDataType(IntEnum):
    AIRPORT = 0
    RUNWAY = 1
    TAXI_PARKING = 15
    JETWAY = 18
    VOR = 19
    NDB = 20
    WAYPOINT = 21


class FacilityListType(IntEnum):
    AIRPORT = 0
    WAYPOINT = 1
    NDB = 2
    VOR = 3


class InputEventDescriptor(Structure):
    _layout_ = "ms"
    _pack_ = 1
    _fields_ = [("name", c_char * 64), ("hash", UINT64), ("type", DWORD)]


class ReceiveGetInputPrefix(Receive):
    _fields_ = [("request_id", DWORD), ("type", DWORD)]


class ReceiveSubscribeInputPrefix(Receive):
    _fields_ = [("hash", UINT64), ("type", DWORD)]


class ReceiveInputParameters(Receive):
    _fields_ = [("hash", UINT64), ("value", c_char * 260)]


# All functions below return an HRESULT. The loader binds these argument lists
# explicitly before exposing the DLL to the adapter.
DispatchProc = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(None, POINTER(Receive), DWORD, c_void_p)


SIGNATURES = {
    "SimConnect_Open": (
        POINTER(HANDLE), c_char_p, HANDLE, DWORD, HANDLE, DWORD,
    ),
    "SimConnect_Close": (HANDLE,),
    "SimConnect_CallDispatch": (HANDLE, DispatchProc, c_void_p),
    "SimConnect_RequestResponseTimes": (HANDLE, DWORD, POINTER(c_float)),
    "SimConnect_RetrieveString": (POINTER(Receive), DWORD, c_void_p, POINTER(c_char_p), POINTER(DWORD)),
    "SimConnect_InsertString": (c_void_p, DWORD, POINTER(c_void_p), POINTER(DWORD), c_char_p),
    "SimConnect_RequestNotificationGroup": (HANDLE, DWORD, DWORD, DWORD),
    "SimConnect_EnumerateControllers": (HANDLE,),
    "SimConnect_MapInputEventToClientEvent_EX1": (HANDLE, DWORD, c_char_p, DWORD, DWORD, DWORD, DWORD, c_int32),
    "SimConnect_SetInputGroupPriority": (HANDLE, DWORD, DWORD),
    "SimConnect_RemoveInputEvent": (HANDLE, DWORD, c_char_p),
    "SimConnect_ClearInputGroup": (HANDLE, DWORD),
    "SimConnect_SetInputGroupState": (HANDLE, DWORD, DWORD),
    "SimConnect_RequestReservedKey": (HANDLE, DWORD, c_char_p, c_char_p, c_char_p),
    "SimConnect_RequestDataOnSimObjectType": (HANDLE, DWORD, DWORD, DWORD, DWORD),
    "SimConnect_AICreateParkedATCAircraft": (HANDLE, c_char_p, c_char_p, c_char_p, DWORD),
    "SimConnect_AICreateEnrouteATCAircraft": (HANDLE, c_char_p, c_char_p, c_int32, c_char_p, c_double, c_int32, DWORD),
    "SimConnect_AICreateNonATCAircraft": (HANDLE, c_char_p, c_char_p, NativeInitialPosition, DWORD),
    "SimConnect_AICreateSimulatedObject": (HANDLE, c_char_p, NativeInitialPosition, DWORD),
    "SimConnect_AIReleaseControl": (HANDLE, DWORD, DWORD),
    "SimConnect_AIRemoveObject": (HANDLE, DWORD, DWORD),
    "SimConnect_AISetAircraftFlightPlan": (HANDLE, DWORD, c_char_p, DWORD),
    "SimConnect_FlightLoad": (HANDLE, c_char_p),
    "SimConnect_FlightSave": (HANDLE, c_char_p, c_char_p, c_char_p, DWORD),
    "SimConnect_FlightPlanLoad": (HANDLE, c_char_p),
    "SimConnect_ExecuteAction": (HANDLE, DWORD, c_char_p, DWORD, c_void_p),
    "SimConnect_SubscribeToFacilities": (HANDLE, DWORD, DWORD),
    "SimConnect_UnsubscribeToFacilities": (HANDLE, DWORD),
    "SimConnect_AddToFacilityDefinition": (HANDLE, DWORD, c_char_p),
    "SimConnect_AddFacilityDataDefinitionFilter": (HANDLE, DWORD, c_char_p, DWORD, c_void_p),
    "SimConnect_ClearAllFacilityDataDefinitionFilters": (HANDLE, DWORD),
    "SimConnect_RequestFacilityData": (HANDLE, DWORD, DWORD, c_char_p, c_char_p),
    "SimConnect_RequestFacilityData_EX1": (HANDLE, DWORD, DWORD, c_char_p, c_char_p, c_char),
    "SimConnect_RequestFacilitiesList_EX1": (HANDLE, DWORD, DWORD),
    "SimConnect_RequestFacilitiesList": (HANDLE, DWORD, DWORD),
    "SimConnect_SubscribeToFacilities_EX1": (HANDLE, DWORD, DWORD, DWORD),
    "SimConnect_UnsubscribeToFacilities_EX1": (HANDLE, DWORD, c_bool, c_bool),
    "SimConnect_RequestJetwayData": (HANDLE, c_char_p, DWORD, POINTER(c_int32)),
    "SimConnect_GetNextDispatch": (HANDLE, POINTER(POINTER(Receive)), POINTER(DWORD)),
    "SimConnect_GetLastSentPacketID": (HANDLE, POINTER(DWORD)),
    "SimConnect_AddToDataDefinition": (HANDLE, DWORD, c_char_p, c_char_p, DWORD, c_float, DWORD),
    "SimConnect_ClearDataDefinition": (HANDLE, DWORD),
    "SimConnect_RequestDataOnSimObject": (
        HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD,
    ),
    "SimConnect_SetDataOnSimObject": (HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD, c_void_p),
    "SimConnect_SubscribeToSystemEvent": (HANDLE, DWORD, c_char_p),
    "SimConnect_UnsubscribeFromSystemEvent": (HANDLE, DWORD),
    "SimConnect_SetSystemEventState": (HANDLE, DWORD, DWORD),
    "SimConnect_RequestSystemState": (HANDLE, DWORD, c_char_p),
    "SimConnect_MapClientEventToSimEvent": (HANDLE, DWORD, c_char_p),
    "SimConnect_AddClientEventToNotificationGroup": (HANDLE, DWORD, DWORD, c_int32),
    "SimConnect_RemoveClientEvent": (HANDLE, DWORD, DWORD),
    "SimConnect_SetNotificationGroupPriority": (HANDLE, DWORD, DWORD),
    "SimConnect_ClearNotificationGroup": (HANDLE, DWORD),
    "SimConnect_TransmitClientEvent": (HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD),
    "SimConnect_TransmitClientEvent_EX1": (
        HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD,
    ),
    "SimConnect_EnumerateInputEvents": (HANDLE, DWORD),
    "SimConnect_EnumerateInputEventParams": (HANDLE, UINT64),
    "SimConnect_GetInputEvent": (HANDLE, DWORD, UINT64),
    "SimConnect_SetInputEvent": (HANDLE, UINT64, DWORD, c_void_p),
    "SimConnect_SubscribeInputEvent": (HANDLE, UINT64),
    "SimConnect_UnsubscribeInputEvent": (HANDLE, UINT64),
    "SimConnect_MapClientDataNameToID": (HANDLE, c_char_p, DWORD),
    "SimConnect_CreateClientData": (HANDLE, DWORD, DWORD, DWORD),
    "SimConnect_AddToClientDataDefinition": (HANDLE, DWORD, DWORD, DWORD, c_float, DWORD),
    "SimConnect_ClearClientDataDefinition": (HANDLE, DWORD),
    "SimConnect_RequestClientData": (HANDLE,) + (DWORD,) * 8,
    "SimConnect_SetClientData": (HANDLE,) + (DWORD,) * 5 + (c_void_p,),
}


# Exports without which the adapter cannot hold a telemetry session. Every other
# export is optional: the loader records it as missing and the provider reports
# the dependent capability as unsupported instead of refusing to load.
REQUIRED_EXPORTS = frozenset({
    "SimConnect_Open", "SimConnect_Close", "SimConnect_GetNextDispatch", "SimConnect_CallDispatch",
    "SimConnect_GetLastSentPacketID", "SimConnect_AddToDataDefinition", "SimConnect_ClearDataDefinition",
    "SimConnect_RequestDataOnSimObject", "SimConnect_SubscribeToSystemEvent",
    "SimConnect_UnsubscribeFromSystemEvent", "SimConnect_SetSystemEventState", "SimConnect_RequestSystemState",
})
