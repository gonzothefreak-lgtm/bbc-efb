"""Regression tests for the real SimConnectSdk transport, loader and wake event."""

from ctypes import POINTER, cast, create_string_buffer
import os
from pathlib import Path
from struct import pack
import sys
from unittest import TestCase
from unittest.mock import patch

from bbc_core import ConnectionState, Core, Gap
from bbc_core.provider import ProviderError
from bbc_core.simconnect import SimConnectProvider
from bbc_core import simconnect_sdk
from bbc_core.simconnect_abi import (
    E_FAIL, REQUIRED_EXPORTS, Receive, ReceiveEvent, ReceiveId, ReceiveSimObjectDataPrefix,
)
from bbc_core.simconnect_sdk import SimConnectSdk, find_simconnect_dll, simconnect_dll_candidates
from test_adapter_support import FakeClock, FakeDll, raw_packet


def loaded_sdk(**options):
    # These tests exercise pull dispatch unless they ask for callbacks; callback
    # dispatch is the SDK default because it is the live-proven transport.
    options.setdefault("callback_dispatch", False)
    sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll", **options)
    dll = FakeDll()
    sdk._functions = dict(dll.functions)
    return sdk, dll


class PullDispatchTests(FakeClock, TestCase):
    """Findings A16-A19 and A27: an empty pull queue is the normal idle state."""

    def setUp(self):
        self.start_clock()  # Every bbc_core clock and simconnect.sleep (A139).

    def test_empty_queue_results_return_none_and_real_failures_raise(self):
        sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll", callback_dispatch=False)
        sdk._functions = {"SimConnect_GetNextDispatch": lambda *_: E_FAIL}
        self.assertIsNone(sdk.next_packet(None))
        sdk._functions = {"SimConnect_GetNextDispatch": lambda *_: 0}  # S_OK with a NULL pointer
        self.assertIsNone(sdk.next_packet(None))
        sdk._functions = {"SimConnect_GetNextDispatch": lambda *_: -2147024890}  # E_HANDLE: a real failure
        with self.assertRaisesRegex(ProviderError, "0x80070006"):
            sdk.next_packet(None)

    def test_failure_with_a_packet_pointer_raises(self):
        buffer = create_string_buffer(raw_packet(Receive, ReceiveId.QUIT))

        def broken(_handle, pointer, size):
            cast(pointer, POINTER(POINTER(Receive)))[0] = cast(buffer, POINTER(Receive))
            return -2147418113  # E_UNEXPECTED
        sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll", callback_dispatch=False)
        sdk._functions = {"SimConnect_GetNextDispatch": broken}
        with self.assertRaises(ProviderError):
            sdk.next_packet(None)

    def test_real_sdk_marshals_packets_through_declared_prototypes(self):
        sdk, dll = loaded_sdk()
        dll.push(raw_packet(Receive, ReceiveId.QUIT))
        self.assertEqual(sdk.next_packet(None), raw_packet(Receive, ReceiveId.QUIT))
        self.assertIsNone(sdk.next_packet(None))
        sdk.call("SimConnect_ClearDataDefinition", None, 5)
        self.assertEqual(sdk.last_packet_id(None), 1)
        dll.hresults["SimConnect_ClearDataDefinition"] = E_FAIL
        with self.assertRaisesRegex(ProviderError, "0x80004005"):
            sdk.call("SimConnect_ClearDataDefinition", None, 5)

    def test_pull_dispatch_idle_queue_keeps_session(self):
        sdk, dll = loaded_sdk()
        core = Core(SimConnectProvider(sdk))
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        speed = consumer.subscribe("aircraft.ground_speed")
        self.assertIs(core.connect().state, ConnectionState.CONNECTED)
        for _ in range(10):
            core.pump()
        self.assertIs(consumer.connection().state, ConnectionState.CONNECTED)
        self.assertIsNone(dll.open_args[3])  # No wake event unless requested.
        self.assertEqual(dll.open_args[4], 0xFFFFFFFF)
        definitions = [args for name, args in dll.calls if name == "SimConnect_AddToDataDefinition"]
        self.assertEqual(definitions[0][1], b"GROUND VELOCITY")
        request = next(args for name, args in dll.calls if name == "SimConnect_RequestDataOnSimObject")
        dll.push(raw_packet(ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, pack("<d", 42.5),
                            request_id=request[0], definition_id=request[1], definition_count=1))
        core.pump()
        self.assertEqual(consumer.latest(speed.id).value, 42.5)
        self.assertIs(consumer.connection().state, ConnectionState.CONNECTED)


class CallbackDispatchTests(FakeClock, TestCase):
    """Findings A57-A59 and A116: volume is back-pressure, not corruption."""

    def setUp(self):
        self.start_clock()  # Every bbc_core clock and simconnect.sleep (A139).

    def test_large_burst_is_delivered_in_order(self):
        sdk, dll = loaded_sdk(callback_dispatch=True)
        burst = [raw_packet(ReceiveEvent, ReceiveId.EVENT, event_id=index, data=index) for index in range(700)]
        dll.callback_packets = list(burst)
        received = []
        while (packet := sdk.next_packet(None)) is not None:
            received.append(packet)
        self.assertEqual(received, burst)
        self.assertEqual(sdk.take_dropped(), 0)

    def test_byte_bound_drops_only_periodic_data_and_reports_a_gap(self):
        sdk, dll = loaded_sdk(callback_dispatch=True)
        data = raw_packet(ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, b"\0" * 8)
        quit_packet = raw_packet(Receive, ReceiveId.QUIT)
        dll.callback_packets = [data] * 5 + [quit_packet]
        with patch.object(simconnect_sdk, "_MAX_CALLBACK_BYTES", len(data) * 2):
            first = sdk.next_packet(None)
        received = [first]
        while (packet := sdk.next_packet(None)) is not None:
            received.append(packet)
        self.assertEqual(received, [data, data, quit_packet])
        self.assertEqual(sdk.take_dropped(), 3)
        self.assertEqual(sdk.take_dropped(), 0)

    def test_dropped_callback_packets_reach_core_as_an_ingress_gap_not_an_error(self):
        sdk, dll = loaded_sdk(callback_dispatch=True)
        original = dll._Open

        def open_with_callback(*args):
            result = original(*args)
            dll.callback_packets = list(dll.queue)
            dll.queue.clear()
            return result
        sdk._functions["SimConnect_Open"] = type(dll.functions["SimConnect_Open"])(open_with_callback)
        core = Core(SimConnectProvider(sdk))
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        self.assertIs(core.connect().state, ConnectionState.CONNECTED)
        data = raw_packet(ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, b"\0" * 8, request_id=999)
        dll.callback_packets = [data] * 4
        with patch.object(simconnect_sdk, "_MAX_CALLBACK_BYTES", len(data)):
            core.pump()
        self.assertIs(consumer.connection().state, ConnectionState.CONNECTED)
        self.assertTrue(any(isinstance(item, Gap) for item in consumer.poll()))

    def test_invalid_callback_packet_is_still_fatal(self):
        sdk, dll = loaded_sdk(callback_dispatch=True)
        dll.callback_packets = [b"\0" * 4]
        with self.assertRaisesRegex(ProviderError, "invalid callback packet"):
            sdk.next_packet(None)


class LoaderTests(TestCase):
    """Findings A114 and A115: DLL discovery and tolerant per-export loading."""

    def test_windows_style_path_is_accepted_on_any_host(self):
        # Finding A74: the constructor must not depend on the host's path flavour.
        SimConnectSdk(r"C:\SDK\SimConnect.dll")
        with self.assertRaises(ProviderError):
            SimConnectSdk("SimConnect.dll")
        with self.assertRaises(ProviderError):
            SimConnectSdk(Path.cwd() / "Other.dll")

    def test_missing_optional_export_degrades_and_required_export_fails(self):
        dll = FakeDll()
        sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll")
        with self.assertLogs("bbc_core.simconnect_sdk", "WARNING"):
            sdk._bind(dll.library(missing={"SimConnect_EnumerateControllers"}))
        self.assertEqual(sdk.missing_exports, {"SimConnect_EnumerateControllers"})
        self.assertFalse(sdk.has("SimConnect_EnumerateControllers"))
        self.assertTrue(sdk.has("SimConnect_Open"))
        with self.assertRaisesRegex(ProviderError, "not available"):
            sdk.call("SimConnect_EnumerateControllers", None)
        provider = SimConnectProvider(sdk)
        self.assertTrue(provider.supports_command("sdk.simulator.reserve_key"))
        with self.assertLogs("bbc_core.simconnect_sdk", "WARNING"):
            sdk._bind(dll.library(missing={"SimConnect_RequestReservedKey"}))
        self.assertFalse(provider.supports_command("sdk.simulator.reserve_key"))
        self.assertTrue(provider.supports_command("sdk.simulator.load_flight"))
        required = sorted(REQUIRED_EXPORTS)[0]
        with self.assertRaisesRegex(ProviderError, "required export"):
            sdk._bind(dll.library(missing={required}))

    def test_candidates_cover_bundle_app_folder_and_sdk_environment(self):
        root = Path.cwd()
        with patch.object(sys, "_MEIPASS", str(root / "bundle"), create=True), \
                patch.dict(os.environ, {"MSFS_SDK": str(root / "sdk")}):
            candidates = list(simconnect_dll_candidates(root / "explicit" / "SimConnect.dll"))
        self.assertEqual(candidates[0], root / "explicit" / "SimConnect.dll")
        self.assertEqual(candidates[1], root / "bundle" / "SimConnect.dll")
        self.assertEqual(candidates[-1], root / "sdk" / "SimConnect SDK" / "lib" / "SimConnect.dll")

    def test_find_returns_the_first_existing_candidate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "SimConnect SDK" / "lib"
            target.mkdir(parents=True)
            (target / "SimConnect.dll").write_bytes(b"")
            with patch.dict(os.environ, {"MSFS_SDK": folder}):
                found = find_simconnect_dll(Path(folder) / "missing" / "SimConnect.dll")
            self.assertEqual(found, (target / "SimConnect.dll").resolve())
            with patch.dict(os.environ, {"MSFS_SDK": str(Path(folder) / "none")}):
                self.assertIsNone(find_simconnect_dll(Path(folder) / "missing" / "SimConnect.dll"))


class FakeKernel32:
    def __init__(self):
        self.calls = []
        self.signalled = False

    def CreateEventW(self, *args):
        self.calls.append(("CreateEventW", args))
        return 0x55

    def WaitForSingleObject(self, handle, milliseconds):
        self.calls.append(("WaitForSingleObject", (handle, milliseconds)))
        signalled, self.signalled = self.signalled, False
        return 0 if signalled else 0x102  # WAIT_OBJECT_0 or WAIT_TIMEOUT

    def SetEvent(self, handle):
        self.calls.append(("SetEvent", (handle,)))
        self.signalled = True
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", (handle,)))
        return 1


class WakeEventTests(FakeClock, TestCase):
    """Finding A1 (adapter part): SimConnect_Open can signal a Win32 event."""

    def setUp(self):
        self.start_clock()  # Every bbc_core clock and simconnect.sleep (A139).

    def test_open_passes_event_and_wait_blocks_on_it(self):
        kernel = FakeKernel32()
        sdk, dll = loaded_sdk(wake_event=True, kernel32=kernel)
        provider = SimConnectProvider(sdk)
        provider.connect("session")
        self.addCleanup(provider.disconnect)
        self.assertEqual(dll.open_args[3], 0x55)
        self.assertFalse(provider.wait(0.25))
        self.assertIn(("WaitForSingleObject", (sdk.event_handle, 250)), kernel.calls)
        sdk.wake()
        self.assertTrue(provider.wait(1))
        provider.disconnect()
        sdk.release_event()
        self.assertEqual(kernel.calls[-1][0], "CloseHandle")
        self.assertIsNone(sdk.event_handle)

    def test_wait_without_event_sleeps(self):
        sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll")
        with patch.object(simconnect_sdk.time, "sleep") as sleeper:
            self.assertFalse(sdk.wait(0.01))
        sleeper.assert_called_once_with(0.01)
