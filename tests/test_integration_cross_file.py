"""Regression tests for the integration pass that joined the workstreams.

* A31/A81: native ctypes and Windows faults at the SDK boundary become ProviderError,
  so they take the normal error path instead of escaping as raw exceptions.
* A96: Core reports the interval the provider can really deliver (see also
  test_integration_snapshot_session).
* A103: Client Data SECOND and VISUAL_FRAME streams can ask for changed-only delivery.
* A112: Core builds a facility cache snapshot tuple once per change, not per call.
* A142: the tests folder is a package, so it also runs from the repository root.
"""

from pathlib import Path
from unittest import TestCase

from bbc_core import ClientDataArea, ConnectionState, Core, CoreRequestError, FacilityKind
from bbc_core.client_data import ClientDataMode
from bbc_core.core import _FacilityWatch
from bbc_core.facilities import AirportReference
from bbc_core.provider import FacilityCacheReply, ProviderError
from bbc_core.facilities import FacilityChange
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import ClientDataPeriod, ReceiveId, ReceiveSimObjectDataPrefix
from bbc_core.simconnect_sdk import SimConnectSdk
from bbc_core.simulated import SimulatedProvider
from test_adapter_support import FakeDll, FakeClock
from test_client_data_offline import ClientDataSdk
from test_simconnect_provider_offline import packet


ROOT = Path(__file__).resolve().parent.parent


def loaded_sdk():
    sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll", callback_dispatch=False)
    dll = FakeDll()
    sdk._functions = dict(dll.functions)
    return sdk, dll


class NativeFaultTests(TestCase):
    def test_a_ctypes_argument_error_becomes_a_provider_error(self):
        sdk, _ = loaded_sdk()
        with self.assertLogs("bbc_core.simconnect_sdk", "ERROR"):
            with self.assertRaisesRegex(ProviderError, "SimConnect_ClearDataDefinition was called with an invalid"):
                sdk.call("SimConnect_ClearDataDefinition", None, "not a DWORD")

    def test_callback_dispatch_is_the_default_transport(self):
        """A16/A17: the live-proven transport is the default until pull is re-run live."""
        self.assertTrue(SimConnectSdk(Path.cwd() / "SimConnect.dll").callback_dispatch)
        self.assertFalse(SimConnectSdk(Path.cwd() / "SimConnect.dll", callback_dispatch=False).callback_dispatch)

    def test_a_native_os_error_becomes_a_provider_error(self):
        sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll", callback_dispatch=False)

        def access_violation(*_args):
            raise OSError("exception: access violation reading 0x0000000000000000")
        sdk._functions = {"SimConnect_GetNextDispatch": access_violation, "SimConnect_Close": access_violation}
        with self.assertLogs("bbc_core.simconnect_sdk", "ERROR"):
            with self.assertRaisesRegex(ProviderError, "failed inside SimConnect.dll"):
                sdk.next_packet(None)
            with self.assertRaisesRegex(ProviderError, "SimConnect_Close failed inside SimConnect.dll"):
                sdk.call("SimConnect_Close", None)


class ClientDataChangedOnlyTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100.0)  # Every bbc_core clock and simconnect.sleep (A139).
        self.sdk = ClientDataSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.module = self.core.register_consumer()
        self.area = ClientDataArea("EXTERNAL", 8)
        self.region = self.area.region(2, 4)
        self.core.register_client_data(self.area)

    def stream_requests(self):
        return [
            args for args, _ in self.sdk.matching_calls("SimConnect_RequestClientData")
            if args[3] == ClientDataPeriod.SECOND
        ]

    def test_changed_only_sets_the_changed_flag_and_accepts_flagged_packets(self):
        subscription = self.module.subscribe_client_data(self.region, mode=ClientDataMode.SECOND, changed_only=True)
        self.assertTrue(subscription.capability.id.endswith(":second:changed"))
        self.core.connect()
        self.core.pump()
        (stream,) = self.stream_requests()
        self.assertEqual(stream[4], 1)  # SIMCONNECT_CLIENT_DATA_REQUEST_FLAG_CHANGED
        self.sdk.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.CLIENT_DATA, b"WXYZ",
            request_id=stream[1], definition_id=stream[2], definition_count=1, flags=1,
        ))
        self.core.pump()
        self.assertEqual(self.module.latest(subscription.id).value, b"WXYZ")
        self.assertIs(self.module.connection().state, ConnectionState.CONNECTED)

    def test_default_streams_keep_no_flags_and_are_separate_from_changed_only(self):
        every = self.module.subscribe_client_data(self.region, mode=ClientDataMode.SECOND)
        changed = self.module.subscribe_client_data(self.region, mode=ClientDataMode.SECOND, changed_only=True)
        self.assertNotEqual(every.capability.id, changed.capability.id)
        self.core.connect()
        self.core.pump()
        self.assertEqual(sorted(args[4] for args in self.stream_requests()), [0, 1])

    def test_preserve_case_maps_the_exact_published_name(self):
        """A76: SimConnect gets the owner's spelling when the host asks to keep it."""
        area = ClientDataArea("External.Data", 8, preserve_case=True)
        self.sdk.areas[b"External.Data"] = bytearray(b"ab1234cd")
        self.core.register_client_data(area)
        subscription = self.module.subscribe_client_data(area.region(2, 4))
        self.core.connect()
        self.core.pump()
        names = [args[0] for args, _ in self.sdk.matching_calls("SimConnect_MapClientDataNameToID")]
        self.assertEqual(names, [b"External.Data"])
        self.assertEqual(self.module.latest(subscription.id).value, b"1234")

    def test_changed_only_is_refused_where_it_has_no_meaning(self):
        for mode in (ClientDataMode.ONCE, ClientDataMode.ON_SET):
            with self.assertRaisesRegex(CoreRequestError, "SECOND and VISUAL_FRAME"):
                self.module.subscribe_client_data(self.region, mode=mode, changed_only=True)
        with self.assertRaisesRegex(CoreRequestError, "True or False"):
            self.module.subscribe_client_data(self.region, mode=ClientDataMode.SECOND, changed_only=1)


class FacilitySnapshotCacheTests(TestCase):
    def test_snapshot_tuple_is_built_once_per_change(self):
        core = Core(SimulatedProvider())
        watch = _FacilityWatch("watch-1")
        core._facility_watches[FacilityKind.AIRPORT] = watch
        first = AirportReference("YSSY", "YM", -33.9, 151.2, 6.0)
        second = AirportReference("YSCB", "YM", -35.3, 149.2, 575.0)

        def reply(change, *entries):
            core._receive_facility_cache(FacilityCacheReply("s", "watch-1", FacilityKind.AIRPORT, change, entries))

        reply(FacilityChange.SNAPSHOT, first)
        one = core._cache_snapshot("sub", FacilityKind.AIRPORT, watch).entries
        self.assertIs(core._cache_snapshot("sub2", FacilityKind.AIRPORT, watch).entries, one)
        reply(FacilityChange.ADDED, second)
        two = core._cache_snapshot("sub", FacilityKind.AIRPORT, watch).entries
        self.assertEqual(two, (first, second))
        reply(FacilityChange.REMOVED, first)
        self.assertEqual(core._cache_snapshot("sub", FacilityKind.AIRPORT, watch).entries, (second,))
        reply(FacilityChange.UNAVAILABLE)
        self.assertEqual(core._cache_snapshot("sub", FacilityKind.AIRPORT, watch).entries, ())


class TestPackageTests(TestCase):
    def test_tests_folder_is_a_package_that_resolves_sibling_helpers(self):
        init = ROOT / "tests" / "__init__.py"
        self.assertTrue(init.is_file())
        self.assertIn("sys.path", init.read_text(encoding="utf-8"))


class OlderPythonDataclassTests(TestCase):
    """Python 3.11 rejects a dataclass default whose type is unhashable, and 3.10 rejects
    dict, list and set defaults. ``mappingproxy`` became hashable only in 3.12, so a
    ``default=MappingProxyType(...)`` passed on 3.12 and broke bbc_efb.storage on 3.11."""

    MODULES = (
        "bbc_core.contract", "bbc_core.provider", "bbc_core.core", "bbc_core.catalog", "bbc_core.client_data",
        "bbc_core.facilities", "bbc_core.simulator_ops", "bbc_core.simulated", "bbc_core.simconnect",
        "bbc_core.simconnect_client_data", "bbc_core.simconnect_facilities", "bbc_core.simconnect_input_events",
        "bbc_efb.storage", "bbc_efb.storage_settings", "bbc_efb.logging_setup",
    )

    def test_no_dataclass_default_is_mutable_or_unhashable(self):
        import dataclasses
        import importlib
        from types import MappingProxyType
        problems = []
        for name in self.MODULES:
            module = importlib.import_module(name)
            for value in vars(module).values():
                if not (isinstance(value, type) and dataclasses.is_dataclass(value)) or value.__module__ != name:
                    continue
                for item in dataclasses.fields(value):
                    default = item.default
                    if default is dataclasses.MISSING:
                        continue
                    if isinstance(default, (dict, list, set, MappingProxyType)) or type(default).__hash__ is None:
                        problems.append(f"{name}.{value.__name__}.{item.name}")
        self.assertEqual(problems, [])

