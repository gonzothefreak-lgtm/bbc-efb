"""Check ctypes declarations against C prototypes rather than against a copy of themselves.

Finding A72: the older signature test compared SIGNATURES with a hand-written
copy of SIGNATURES. Here every argument is classified by its C type text, as
written in the MSFS 2020 SDK 0.24.6.0 SimConnect.h, and by its ctypes type. A
width or kind mismatch (for example a DWORD where the header has a UINT64, or a
4-byte BOOL where it has a 1-byte bool) fails. When the SDK header is installed
(MSFS_SDK or C:\\MSFS SDK), a second test parses it directly.

Findings A98/A99: packed structures declare the MSVC layout explicitly.
"""

import ctypes
from ctypes import POINTER, c_bool, c_char, c_char_p, c_double, c_float, c_int32, c_uint32, c_uint64, c_void_p
import os
from pathlib import Path
import importlib.util
import re
import warnings
from unittest import TestCase, skipUnless

from bbc_core import simconnect_abi as abi


# Parameter lists copied from SimConnect.h (MSFS 2020 SDK 0.24.6.0), defaults removed.
HEADER_PROTOTYPES = {
    "SimConnect_Open": "HANDLE * phSimConnect, LPCSTR szName, HWND hWnd, DWORD UserEventWin32, HANDLE hEventHandle, DWORD ConfigIndex",
    "SimConnect_Close": "HANDLE hSimConnect",
    "SimConnect_CallDispatch": "HANDLE hSimConnect, DispatchProc pfcnDispatch, void * pContext",
    "SimConnect_RequestResponseTimes": "HANDLE hSimConnect, DWORD nCount, float * fElapsedSeconds",
    "SimConnect_RetrieveString": "SIMCONNECT_RECV * pData, DWORD cbData, void * pStringV, char ** pszString, DWORD * pcbString",
    "SimConnect_InsertString": "char * pDest, DWORD cbDest, void ** ppEnd, DWORD * pcbStringV, const char * pSource",
    "SimConnect_RequestNotificationGroup": "HANDLE hSimConnect, SIMCONNECT_NOTIFICATION_GROUP_ID GroupID, DWORD dwReserved, DWORD Flags",
    "SimConnect_EnumerateControllers": "HANDLE hSimConnect",
    "SimConnect_MapInputEventToClientEvent_EX1": "HANDLE hSimConnect, SIMCONNECT_INPUT_GROUP_ID GroupID, const char * szInputDefinition, SIMCONNECT_CLIENT_EVENT_ID DownEventID, DWORD DownValue, SIMCONNECT_CLIENT_EVENT_ID UpEventID, DWORD UpValue, BOOL bMaskable",
    "SimConnect_SetInputGroupPriority": "HANDLE hSimConnect, SIMCONNECT_INPUT_GROUP_ID GroupID, DWORD uPriority",
    "SimConnect_RemoveInputEvent": "HANDLE hSimConnect, SIMCONNECT_INPUT_GROUP_ID GroupID, const char * szInputDefinition",
    "SimConnect_ClearInputGroup": "HANDLE hSimConnect, SIMCONNECT_INPUT_GROUP_ID GroupID",
    "SimConnect_SetInputGroupState": "HANDLE hSimConnect, SIMCONNECT_INPUT_GROUP_ID GroupID, DWORD dwState",
    "SimConnect_RequestReservedKey": "HANDLE hSimConnect, SIMCONNECT_CLIENT_EVENT_ID EventID, const char * szKeyChoice1, const char * szKeyChoice2, const char * szKeyChoice3",
    "SimConnect_RequestDataOnSimObjectType": "HANDLE hSimConnect, SIMCONNECT_DATA_REQUEST_ID RequestID, SIMCONNECT_DATA_DEFINITION_ID DefineID, DWORD dwRadiusMeters, SIMCONNECT_SIMOBJECT_TYPE type",
    "SimConnect_AICreateParkedATCAircraft": "HANDLE hSimConnect, const char * szContainerTitle, const char * szTailNumber, const char * szAirportID, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_AICreateEnrouteATCAircraft": "HANDLE hSimConnect, const char * szContainerTitle, const char * szTailNumber, int iFlightNumber, const char * szFlightPlanPath, double dFlightPlanPosition, BOOL bTouchAndGo, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_AICreateNonATCAircraft": "HANDLE hSimConnect, const char * szContainerTitle, const char * szTailNumber, SIMCONNECT_DATA_INITPOSITION InitPos, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_AICreateSimulatedObject": "HANDLE hSimConnect, const char * szContainerTitle, SIMCONNECT_DATA_INITPOSITION InitPos, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_AIReleaseControl": "HANDLE hSimConnect, SIMCONNECT_OBJECT_ID ObjectID, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_AIRemoveObject": "HANDLE hSimConnect, SIMCONNECT_OBJECT_ID ObjectID, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_AISetAircraftFlightPlan": "HANDLE hSimConnect, SIMCONNECT_OBJECT_ID ObjectID, const char * szFlightPlanPath, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_FlightLoad": "HANDLE hSimConnect, const char * szFileName",
    "SimConnect_FlightSave": "HANDLE hSimConnect, const char * szFileName, const char * szTitle, const char * szDescription, DWORD Flags",
    "SimConnect_FlightPlanLoad": "HANDLE hSimConnect, const char * szFileName",
    "SimConnect_ExecuteAction": "HANDLE hSimConnect, DWORD cbRequestID, const char * szActionID, DWORD cbUnitSize, void * pParamValues",
    "SimConnect_SubscribeToFacilities": "HANDLE hSimConnect, SIMCONNECT_FACILITY_LIST_TYPE type, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_UnsubscribeToFacilities": "HANDLE hSimConnect, SIMCONNECT_FACILITY_LIST_TYPE type",
    "SimConnect_AddToFacilityDefinition": "HANDLE hSimConnect, SIMCONNECT_DATA_DEFINITION_ID DefineID, const char * FieldName",
    "SimConnect_AddFacilityDataDefinitionFilter": "HANDLE hSimConnect, SIMCONNECT_DATA_DEFINITION_ID DefineID, const char * szFilterPath, DWORD cbUnitSize, void * pFilterData",
    "SimConnect_ClearAllFacilityDataDefinitionFilters": "HANDLE hSimConnect, SIMCONNECT_DATA_DEFINITION_ID DefineID",
    "SimConnect_RequestFacilityData": "HANDLE hSimConnect, SIMCONNECT_DATA_DEFINITION_ID DefineID, SIMCONNECT_DATA_REQUEST_ID RequestID, const char * ICAO, const char * Region",
    "SimConnect_RequestFacilityData_EX1": "HANDLE hSimConnect, SIMCONNECT_DATA_DEFINITION_ID DefineID, SIMCONNECT_DATA_REQUEST_ID RequestID, const char * ICAO, const char * Region, char Type",
    "SimConnect_RequestFacilitiesList_EX1": "HANDLE hSimConnect, SIMCONNECT_FACILITY_LIST_TYPE type, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_RequestFacilitiesList": "HANDLE hSimConnect, SIMCONNECT_FACILITY_LIST_TYPE type, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_SubscribeToFacilities_EX1": "HANDLE hSimConnect, SIMCONNECT_FACILITY_LIST_TYPE type, SIMCONNECT_DATA_REQUEST_ID newElemInRangeRequestID, SIMCONNECT_DATA_REQUEST_ID oldElemOutRangeRequestID",
    "SimConnect_UnsubscribeToFacilities_EX1": "HANDLE hSimConnect, SIMCONNECT_FACILITY_LIST_TYPE type, bool bUnsubscribeNewInRange, bool bUnsubscribeOldOutRange",
    "SimConnect_RequestJetwayData": "HANDLE hSimConnect, const char * airportIcao, DWORD arrayCount, int * indexes",
    "SimConnect_GetNextDispatch": "HANDLE hSimConnect, SIMCONNECT_RECV ** ppData, DWORD * pcbData",
    "SimConnect_GetLastSentPacketID": "HANDLE hSimConnect, DWORD * pdwError",
    "SimConnect_AddToDataDefinition": "HANDLE hSimConnect, SIMCONNECT_DATA_DEFINITION_ID DefineID, const char * DatumName, const char * UnitsName, SIMCONNECT_DATATYPE DatumType, float fEpsilon, DWORD DatumID",
    "SimConnect_ClearDataDefinition": "HANDLE hSimConnect, SIMCONNECT_DATA_DEFINITION_ID DefineID",
    "SimConnect_RequestDataOnSimObject": "HANDLE hSimConnect, SIMCONNECT_DATA_REQUEST_ID RequestID, SIMCONNECT_DATA_DEFINITION_ID DefineID, SIMCONNECT_OBJECT_ID ObjectID, SIMCONNECT_PERIOD Period, SIMCONNECT_DATA_REQUEST_FLAG Flags, DWORD origin, DWORD interval, DWORD limit",
    "SimConnect_SetDataOnSimObject": "HANDLE hSimConnect, SIMCONNECT_DATA_DEFINITION_ID DefineID, SIMCONNECT_OBJECT_ID ObjectID, SIMCONNECT_DATA_SET_FLAG Flags, DWORD ArrayCount, DWORD cbUnitSize, void * pDataSet",
    "SimConnect_SubscribeToSystemEvent": "HANDLE hSimConnect, SIMCONNECT_CLIENT_EVENT_ID EventID, const char * SystemEventName",
    "SimConnect_UnsubscribeFromSystemEvent": "HANDLE hSimConnect, SIMCONNECT_CLIENT_EVENT_ID EventID",
    "SimConnect_SetSystemEventState": "HANDLE hSimConnect, SIMCONNECT_CLIENT_EVENT_ID EventID, SIMCONNECT_STATE dwState",
    "SimConnect_RequestSystemState": "HANDLE hSimConnect, SIMCONNECT_DATA_REQUEST_ID RequestID, const char * szState",
    "SimConnect_MapClientEventToSimEvent": "HANDLE hSimConnect, SIMCONNECT_CLIENT_EVENT_ID EventID, const char * EventName",
    "SimConnect_AddClientEventToNotificationGroup": "HANDLE hSimConnect, SIMCONNECT_NOTIFICATION_GROUP_ID GroupID, SIMCONNECT_CLIENT_EVENT_ID EventID, BOOL bMaskable",
    "SimConnect_RemoveClientEvent": "HANDLE hSimConnect, SIMCONNECT_NOTIFICATION_GROUP_ID GroupID, SIMCONNECT_CLIENT_EVENT_ID EventID",
    "SimConnect_SetNotificationGroupPriority": "HANDLE hSimConnect, SIMCONNECT_NOTIFICATION_GROUP_ID GroupID, DWORD uPriority",
    "SimConnect_ClearNotificationGroup": "HANDLE hSimConnect, SIMCONNECT_NOTIFICATION_GROUP_ID GroupID",
    "SimConnect_TransmitClientEvent": "HANDLE hSimConnect, SIMCONNECT_OBJECT_ID ObjectID, SIMCONNECT_CLIENT_EVENT_ID EventID, DWORD dwData, SIMCONNECT_NOTIFICATION_GROUP_ID GroupID, SIMCONNECT_EVENT_FLAG Flags",
    "SimConnect_TransmitClientEvent_EX1": "HANDLE hSimConnect, SIMCONNECT_OBJECT_ID ObjectID, SIMCONNECT_CLIENT_EVENT_ID EventID, SIMCONNECT_NOTIFICATION_GROUP_ID GroupID, SIMCONNECT_EVENT_FLAG Flags, DWORD dwData0, DWORD dwData1, DWORD dwData2, DWORD dwData3, DWORD dwData4",
    "SimConnect_EnumerateInputEvents": "HANDLE hSimConnect, SIMCONNECT_DATA_REQUEST_ID RequestID",
    "SimConnect_EnumerateInputEventParams": "HANDLE hSimConnect, UINT64 Hash",
    "SimConnect_GetInputEvent": "HANDLE hSimConnect, SIMCONNECT_DATA_REQUEST_ID RequestID, UINT64 Hash",
    "SimConnect_SetInputEvent": "HANDLE hSimConnect, UINT64 Hash, DWORD cbUnitSize, void * Value",
    "SimConnect_SubscribeInputEvent": "HANDLE hSimConnect, UINT64 Hash",
    "SimConnect_UnsubscribeInputEvent": "HANDLE hSimConnect, UINT64 Hash",
    "SimConnect_MapClientDataNameToID": "HANDLE hSimConnect, const char * szClientDataName, SIMCONNECT_CLIENT_DATA_ID ClientDataID",
    "SimConnect_CreateClientData": "HANDLE hSimConnect, SIMCONNECT_CLIENT_DATA_ID ClientDataID, DWORD dwSize, SIMCONNECT_CREATE_CLIENT_DATA_FLAG Flags",
    "SimConnect_AddToClientDataDefinition": "HANDLE hSimConnect, SIMCONNECT_CLIENT_DATA_DEFINITION_ID DefineID, DWORD dwOffset, DWORD dwSizeOrType, float fEpsilon, DWORD DatumID",
    "SimConnect_ClearClientDataDefinition": "HANDLE hSimConnect, SIMCONNECT_CLIENT_DATA_DEFINITION_ID DefineID",
    "SimConnect_RequestClientData": "HANDLE hSimConnect, SIMCONNECT_CLIENT_DATA_ID ClientDataID, SIMCONNECT_DATA_REQUEST_ID RequestID, SIMCONNECT_CLIENT_DATA_DEFINITION_ID DefineID, SIMCONNECT_CLIENT_DATA_PERIOD Period, SIMCONNECT_CLIENT_DATA_REQUEST_FLAG Flags, DWORD origin, DWORD interval, DWORD limit",
    "SimConnect_SetClientData": "HANDLE hSimConnect, SIMCONNECT_CLIENT_DATA_ID ClientDataID, SIMCONNECT_CLIENT_DATA_DEFINITION_ID DefineID, SIMCONNECT_CLIENT_DATA_SET_FLAG Flags, DWORD dwReserved, DWORD cbUnitSize, void * pDataSet",
}


def c_kind(parameter):
    """Classify one C parameter (type and name, optional default) by ABI kind."""
    declaration = parameter.split("=", 1)[0].strip()
    declaration = re.sub(r"\s*\*\s*", " * ", declaration)
    words = declaration.split()
    type_words = words[:-1] if len(words) > 1 else words
    text = " ".join(type_words)
    stars = text.count("*")
    base = text.replace("*", "").replace("const", "").strip()
    if stars:
        return "str" if stars == 1 and "const" in text and base == "char" else "ptr"
    if base in ("HANDLE", "HWND"):
        return "ptr"
    if base == "LPCSTR":
        return "str"
    if base == "DispatchProc":
        return "proc"
    if base == "SIMCONNECT_DATA_INITPOSITION":
        return "struct"
    return {"UINT64": "u8", "DWORD": "u4", "int": "i4", "BOOL": "i4", "float": "f4", "double": "f8",
            "bool": "b1", "char": "c1"}.get(base, "u4" if base.startswith("SIMCONNECT_") else f"unknown:{base}")


def ctypes_kind(argtype):
    if argtype is c_char_p:
        return "str"
    if argtype is c_void_p or (isinstance(argtype, type) and issubclass(argtype, ctypes._Pointer)):
        return "ptr"
    if argtype is abi.DispatchProc:
        return "proc"
    if argtype is abi.NativeInitialPosition:
        return "struct"
    return {c_uint64: "u8", c_uint32: "u4", c_int32: "i4", c_float: "f4", c_double: "f8",
            c_bool: "b1", c_char: "c1"}.get(argtype, f"unknown:{argtype!r}")


def header_kinds(parameters):
    return [c_kind(item) for item in parameters.split(",") if item.strip()]


class AbiPrototypeTests(TestCase):
    def test_signatures_match_c_prototypes_by_kind_and_width(self):
        self.assertEqual(set(abi.SIGNATURES), set(HEADER_PROTOTYPES))
        for name, argtypes in abi.SIGNATURES.items():
            with self.subTest(name=name):
                self.assertEqual([ctypes_kind(item) for item in argtypes], header_kinds(HEADER_PROTOTYPES[name]))

    def test_classifier_detects_width_errors(self):
        self.assertNotEqual(ctypes_kind(abi.DWORD), c_kind("UINT64 Hash"))
        self.assertNotEqual(ctypes_kind(c_int32), c_kind("bool bFlag"))
        self.assertEqual(c_kind("const char * Name = \"\""), "str")

    @skipUnless(any(path.is_file() for path in (
        Path(os.environ.get("MSFS_SDK", "/nonexistent")) / "SimConnect SDK" / "include" / "SimConnect.h",
        Path(r"C:\MSFS SDK") / "SimConnect SDK" / "include" / "SimConnect.h",
    )), "The MSFS SDK header is not installed on this host.")
    def test_signatures_match_the_installed_header(self):
        for root in (os.environ.get("MSFS_SDK"), r"C:\MSFS SDK"):
            header = Path(root or "/nonexistent") / "SimConnect SDK" / "include" / "SimConnect.h"
            if header.is_file():
                break
        text = header.read_text(encoding="latin-1")
        found = dict(re.findall(r"SIMCONNECTAPI\s+(SimConnect_\w+)\s*\(([^)]*)\)", text))
        for name, argtypes in abi.SIGNATURES.items():
            with self.subTest(name=name):
                self.assertIn(name, found)
                self.assertEqual([ctypes_kind(item) for item in argtypes], header_kinds(found[name]))


class AbiLayoutTests(TestCase):
    def test_packed_structures_declare_the_msvc_layout(self):
        for structure in (abi.Receive, abi.ReceiveSimObjectDataPrefix, abi.NativeInitialPosition,
                          abi.InputEventDescriptor):
            with self.subTest(structure=structure.__name__):
                self.assertEqual(getattr(structure, "_layout_", None), "ms")
                self.assertEqual(getattr(structure, "_pack_", None), 1)

    def test_import_emits_no_deprecation_warning(self):
        # Run the module's code again in-process, as a separate copy, with
        # DeprecationWarning raised as an error. A subprocess would be simpler, but the
        # guarded CI runner (tests/test_repo_offline_guard.py) blocks subprocesses.
        spec = importlib.util.spec_from_file_location("_abi_deprecation_check", abi.__file__)
        module = importlib.util.module_from_spec(spec)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            spec.loader.exec_module(module)
        self.assertEqual(module.SIGNATURES.keys(), abi.SIGNATURES.keys())
