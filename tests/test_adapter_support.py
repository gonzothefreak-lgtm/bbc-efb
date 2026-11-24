"""Shared helpers for the adapter regression tests (no tests of its own).

``FakeClock`` patches ``monotonic`` in every loaded ``bbc_core`` module that
imports it, and makes ``bbc_core.simconnect.sleep`` advance the same clock, so
no busy-wait can spin forever on frozen time (finding A139).

``FakeDll`` builds an in-memory SimConnect "DLL" whose exports are ctypes
CFUNCTYPE objects built from ``SIGNATURES``. A real ``SimConnectSdk`` then runs
its own HRESULT handling, ctypes marshalling, packet copying and packet-ID
bookkeeping against it (finding A73).
"""

from collections import deque
import ctypes
from ctypes import POINTER, c_int32, cast, create_string_buffer, sizeof
import importlib
import sys
from unittest.mock import patch

from bbc_core.simconnect_abi import E_FAIL, Receive, ReceiveId, ReceiveOpen, SIGNATURES


def raw_packet(cls, kind, payload=b"", **fields):
    message = cls()
    message.id = kind
    message.version = 1
    message.size = sizeof(cls) + len(payload)
    for name, value in fields.items():
        setattr(message, name, value)
    return bytes(message) + payload


class FakeClock:
    """Mixin for TestCase: ``self.now`` drives every Core and adapter clock."""

    def start_clock(self, now=100.0):
        self.now = now
        import bbc_core  # noqa: F401 - make sure the package is loaded
        for name in ("core", "simconnect", "simconnect_advanced", "simconnect_client_data",
                     "simconnect_client_events", "simconnect_facilities", "simconnect_facility_cache",
                     "simconnect_input_events", "simconnect_ledger"):
            importlib.import_module(f"bbc_core.{name}")
        for name, module in tuple(sys.modules.items()):
            if name.startswith("bbc_core.") and hasattr(module, "monotonic"):
                clock = patch(f"{name}.monotonic", side_effect=lambda: self.now)
                clock.start()
                self.addCleanup(clock.stop)
        sleeper = patch("bbc_core.simconnect.sleep", side_effect=self.advance)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def advance(self, seconds):
        self.now += seconds


class FakeDll:
    """In-memory exports with the declared ctypes prototypes."""

    def __init__(self):
        self.queue = deque()
        self.calls = []
        self.packet_id = 0
        self.callback_packets = []
        self.open_args = None
        self.hresults = {}
        self._buffer = None
        self._prototypes = {}
        self.functions = {}
        for name, argtypes in SIGNATURES.items():
            prototype = ctypes.CFUNCTYPE(c_int32, *argtypes)
            implementation = getattr(self, "_" + name.removeprefix("SimConnect_"), None)
            function = prototype(implementation or self._recorder(name))
            self._prototypes[name] = function  # keep the thunk alive
            self.functions[name] = function

    def _recorder(self, name):
        def record(*args):
            self.packet_id += 1
            self.calls.append((name, args[1:]))
            return self.hresults.get(name, 0)
        return record

    def library(self, *, missing=()):
        """An object that looks like a loaded ctypes library for SimConnectSdk._bind."""
        namespace = type("Library", (), {})()
        for name in self.functions:
            if name not in missing:
                setattr(namespace, name, ctypes.CFUNCTYPE(c_int32)(lambda: 0))
        return namespace

    def push(self, packet):
        self.queue.append(packet)

    def _Open(self, handle, name, window, message, event, config):
        self.open_args = (name, window, message, event, config)
        handle[0] = 0x1234
        self.queue.append(raw_packet(ReceiveOpen, ReceiveId.OPEN))
        return 0

    def _Close(self, handle):
        self.calls.append(("SimConnect_Close", ()))
        return 0

    def _GetNextDispatch(self, handle, pointer, size):
        if not self.queue:
            return E_FAIL
        self._buffer = create_string_buffer(self.queue.popleft())
        pointer[0] = cast(self._buffer, POINTER(Receive))
        size[0] = len(self._buffer) - 1
        return 0

    def _GetLastSentPacketID(self, handle, packet):
        packet[0] = self.packet_id
        return 0

    def _CallDispatch(self, handle, callback, context):
        buffers = [create_string_buffer(item) for item in self.callback_packets]
        self.callback_packets = []
        for buffer in buffers:
            callback(cast(buffer, POINTER(Receive)), len(buffer) - 1, context)
        return 0
