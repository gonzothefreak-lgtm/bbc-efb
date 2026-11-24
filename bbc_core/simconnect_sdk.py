"""Local x64 SimConnect DLL boundary. Loading is explicit and belongs to the host.

Dispatch transports
-------------------
``SimConnectSdk(path)`` uses callback dispatch (``SimConnect_CallDispatch``) by
default, because it is the transport that passed both live read-only probes. One
call can deliver everything SimConnect has queued, so packets are buffered in a
byte-bounded queue; past the bound only periodic data is dropped, and the
provider reports the loss as a gap.

``callback_dispatch=False`` selects pull dispatch (``SimConnect_GetNextDispatch``).
An empty receive queue is the normal idle state: the SDK reports it as ``E_FAIL``
with no packet, and ``next_packet`` returns ``None``. Backlog stays in SimConnect's
own queue, so the provider's ``drain(limit)`` bounds the work per pump. The live
pull failure recorded on 24 November 2026 matches the old handling of that
``E_FAIL``, which is fixed, but pull mode has not been re-run live, so it stays
opt-in until it has. Only corrupt packets end the session in either mode.

Waking a worker
---------------
With ``wake_event=True`` the SDK creates a Win32 auto-reset event (kernel32
``CreateEventW``) and passes it to ``SimConnect_Open``. SimConnect signals it when
a message is queued, so a host worker can call ``wait(timeout)`` between pumps
instead of polling. ``wake()`` signals the same event, for example when the host
queues a command. Both touch only the event handle, never the SimConnect handle,
so a worker may call them without holding Core's provider lock.
"""

from __future__ import annotations

from collections import deque
from ctypes import POINTER, addressof, byref, c_char_p, c_float, c_void_p, cast, create_string_buffer, sizeof, string_at
from pathlib import Path, PureWindowsPath
import ctypes
import logging
import os
import sys
import time

from .provider import ProviderError
from .simconnect_abi import (
    DWORD, E_FAIL, HANDLE, HRESULT, REQUIRED_EXPORTS, DispatchProc, Receive, ReceiveId, SIGNATURES,
)


_LOG = logging.getLogger(__name__)
_MAX_PACKET = 1024 * 1024
_MAX_CALLBACK_BYTES = 64 * 1024 * 1024
# Periodic telemetry can be dropped (and reported as an ingress gap) when the
# callback buffer is full. Every other packet is kept, because losing a QUIT,
# exception, command reply or one page of a multipart reply would corrupt state.
_DROPPABLE = frozenset({ReceiveId.SIMOBJECT_DATA, ReceiveId.EVENT_FRAME, ReceiveId.CLIENT_DATA})
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
SDK_ENVIRONMENT_VARIABLE = "MSFS_SDK"


def simconnect_dll_candidates(explicit=None):
    """Yield candidate SimConnect.dll paths in priority order.

    1. ``explicit``, when the host or Settings supplies a path.
    2. The PyInstaller bundle folder (``sys._MEIPASS``) of a frozen build.
    3. The application folder: the frozen executable's folder, or the folder of
       the script that started Python.
    4. The MSFS SDK named by the ``MSFS_SDK`` environment variable, which the SDK
       installer sets (``<MSFS_SDK>/SimConnect SDK/lib/SimConnect.dll``).
    """
    seen = set()

    def unique(path):
        key = os.path.normcase(str(path))
        if key in seen:
            return None
        seen.add(key)
        return path

    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "SimConnect.dll")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "SimConnect.dll")
    elif sys.argv and sys.argv[0]:
        candidates.append(Path(sys.argv[0]).resolve().parent / "SimConnect.dll")
    sdk = os.environ.get(SDK_ENVIRONMENT_VARIABLE)
    if sdk:
        candidates.append(Path(sdk) / "SimConnect SDK" / "lib" / "SimConnect.dll")
    for candidate in candidates:
        candidate = unique(candidate)
        if candidate is not None:
            yield candidate


def find_simconnect_dll(explicit=None):
    """Return the first existing absolute SimConnect.dll candidate, or None.

    The host should show the chosen path (or the absence of one) in Settings and
    the SimConnect status so the pilot can see where the DLL came from.
    """
    for candidate in simconnect_dll_candidates(explicit):
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _is_dll_path(path):
    # Accept Windows-style paths on every host so offline tests are portable.
    if sys.platform == "win32":
        return path.is_absolute() and path.name.casefold() == "simconnect.dll"
    windows = PureWindowsPath(str(path))
    return ((path.is_absolute() and path.name.casefold() == "simconnect.dll")
            or (windows.is_absolute() and windows.name.casefold() == "simconnect.dll"))


class SimConnectSdk:
    """The provider accepts an injected SDK instead for offline verification."""

    simulated = False

    def __init__(self, dll_path, *, callback_dispatch=True, wake_event=False, kernel32=None):
        path = Path(dll_path)
        if not _is_dll_path(path):
            raise ProviderError("Supply an absolute path to the local SimConnect.dll.")
        self._path = path
        self._functions = None
        self._library = None
        self._callback_dispatch = callback_dispatch
        self._callback_packets = deque()
        self._callback_bytes = 0
        self._callback_invalid = None
        self._callback_dropped = 0
        self._dispatch_callback = DispatchProc(self._receive_callback)
        self._use_event = wake_event
        self._kernel32 = kernel32
        self._event = None
        self.missing_exports = frozenset()

    @property
    def callback_dispatch(self):
        return self._callback_dispatch

    @property
    def path(self):
        return self._path

    def load(self):
        if self._functions is not None:
            return
        if sys.platform != "win32" or sizeof(HANDLE) != 8:
            raise ProviderError("SimConnect requires a 64-bit Windows host.")
        try:
            library = ctypes.WinDLL(str(self._path.resolve(strict=True)))
        except (OSError, RuntimeError) as error:
            raise ProviderError(f"Could not load the selected SimConnect SDK: {error}") from error
        self._bind(library)

    def _bind(self, library):
        """Bind each declared export. Missing optional exports degrade, not fail."""
        functions, missing = {}, set()
        for name, arguments in SIGNATURES.items():
            function = getattr(library, name, None)
            if function is None:
                if name in REQUIRED_EXPORTS:
                    raise ProviderError(
                        f"The selected SimConnect.dll lacks the required export {name}. "
                        "Use the DLL from the MSFS 2020 SDK 0.24.x."
                    )
                missing.add(name)
                continue
            function.argtypes = arguments
            function.restype = HRESULT
            functions[name] = function
        if missing:
            _LOG.warning("SimConnect.dll at %s lacks optional exports: %s", self._path, ", ".join(sorted(missing)))
        self._library = library
        self._functions = functions
        self.missing_exports = frozenset(missing)

    def has(self, name):
        """True when the loaded DLL provides ``name`` (or no DLL is loaded yet)."""
        return self._functions is None or name in self._functions

    def _function(self, name):
        if self._functions is None:
            raise ProviderError("Load the selected SimConnect DLL before using the provider.")
        function = self._functions.get(name)
        if function is None:
            raise ProviderError(f"{name} is not available in the selected SimConnect.dll.")
        return function

    @staticmethod
    def _native(name, function, *args):
        """Run one native call; a ctypes or Windows fault becomes a ProviderError."""
        try:
            return function(*args)
        except ctypes.ArgumentError as error:
            _LOG.error("%s was given an argument ctypes could not convert: %s", name, error)
            raise ProviderError(f"{name} was called with an invalid argument: {error}") from error
        except OSError as error:
            # On Windows ctypes reports a native access violation as OSError.
            _LOG.error("%s raised a native error: %s", name, error)
            raise ProviderError(f"{name} failed inside SimConnect.dll: {error}") from error

    def call(self, name, *args):
        result = self._native(name, self._function(name), *args)
        if result < 0:
            _LOG.debug("%s failed with HRESULT 0x%08X", name, result & 0xFFFFFFFF)
            raise ProviderError(f"{name} failed with HRESULT 0x{result & 0xFFFFFFFF:08X}.")

    # Win32 event support ------------------------------------------------

    def _kernel(self):
        if self._kernel32 is None:
            if sys.platform != "win32":
                raise ProviderError("A Win32 wake event requires a Windows host.")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateEventW.argtypes = (c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
            kernel32.CreateEventW.restype = HANDLE
            kernel32.WaitForSingleObject.argtypes = (HANDLE, DWORD)
            kernel32.WaitForSingleObject.restype = DWORD
            kernel32.SetEvent.argtypes = (HANDLE,)
            kernel32.SetEvent.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = (HANDLE,)
            kernel32.CloseHandle.restype = ctypes.c_int
            self._kernel32 = kernel32
        return self._kernel32

    def _ensure_event(self):
        if not self._use_event:
            return None
        if self._event is None:
            # Auto-reset, initially non-signalled, unnamed.
            event = self._kernel().CreateEventW(None, False, False, None)
            if not event:
                raise ProviderError("Could not create the Win32 event for SimConnect.")
            self._event = HANDLE(event) if not isinstance(event, HANDLE) else event
        return self._event

    @property
    def event_handle(self):
        """The Win32 event passed to SimConnect_Open, or None."""
        return self._event

    def wait(self, timeout):
        """Block until SimConnect signals queued data, ``wake()`` is called or ``timeout`` passes.

        Returns True when the event was signalled. Without a wake event this sleeps
        for ``timeout`` seconds and returns False, so callers can use it either way.
        """
        timeout = max(0.0, float(timeout))
        if self._event is None:
            time.sleep(timeout)
            return False
        milliseconds = min(int(timeout * 1000), _INFINITE - 1)
        return self._kernel().WaitForSingleObject(self._event, milliseconds) == _WAIT_OBJECT_0

    def wake(self):
        """Signal the wake event so a waiting worker runs its next pump now."""
        if self._event is not None:
            self._kernel().SetEvent(self._event)

    def release_event(self):
        """Close the wake event. Call only after the worker has stopped waiting."""
        if self._event is not None:
            event, self._event = self._event, None
            self._kernel().CloseHandle(event)

    # Session --------------------------------------------------------------

    def open(self, application):
        handle = HANDLE()
        event = self._ensure_event()
        # SIMCONNECT_OPEN_CONFIGINDEX_LOCAL bypasses SimConnect.cfg.
        self.call("SimConnect_Open", byref(handle), application, None, 0, event, 0xFFFFFFFF)
        if not handle.value:
            raise ProviderError("SimConnect_Open returned an empty handle.")
        return handle

    def close(self, handle):
        self.call("SimConnect_Close", handle)
        self._reset_callback_buffer()

    def _reset_callback_buffer(self):
        self._callback_packets.clear()
        self._callback_bytes = 0
        self._callback_invalid = None
        self._callback_dropped = 0

    def next_packet(self, handle):
        """Return one copied packet, or None when the receive queue is empty."""
        if self._callback_dispatch:
            if not self._callback_packets:
                self._call_dispatch(handle)
            if not self._callback_packets:
                return None
            packet = self._callback_packets.popleft()
            self._callback_bytes -= len(packet)
            return packet
        function = self._function("SimConnect_GetNextDispatch")
        pointer, size = POINTER(Receive)(), DWORD()
        result = self._native("SimConnect_GetNextDispatch", function, handle, byref(pointer), byref(size))
        if not pointer and (result == E_FAIL or result >= 0):
            return None  # Empty receive queue: the normal idle state.
        if result < 0:
            raise ProviderError(f"SimConnect_GetNextDispatch failed with HRESULT 0x{result & 0xFFFFFFFF:08X}.")
        if not sizeof(Receive) <= size.value <= _MAX_PACKET:
            raise ProviderError("SimConnect returned an invalid packet byte count.")
        # The SDK reuses this memory on a later call. Copy it while we own it.
        return string_at(pointer, size.value)

    def take_dropped(self):
        """Return and reset the count of periodic packets dropped by the callback buffer."""
        dropped, self._callback_dropped = self._callback_dropped, 0
        return dropped

    def _receive_callback(self, pointer, size, _context):
        if self._callback_invalid:
            return
        if not pointer or not sizeof(Receive) <= size <= _MAX_PACKET:
            self._callback_invalid = "SimConnect delivered an invalid callback packet."
            return
        if self._callback_bytes + size > _MAX_CALLBACK_BYTES:
            kind = cast(pointer, POINTER(Receive))[0].id
            if kind in _DROPPABLE:
                self._callback_dropped += 1
                return
            if self._callback_bytes + size > 2 * _MAX_CALLBACK_BYTES:
                self._callback_invalid = "SimConnect callback burst exceeded the memory bound."
                return
        self._callback_packets.append(string_at(pointer, size))
        self._callback_bytes += size

    def _call_dispatch(self, handle):
        self.call("SimConnect_CallDispatch", handle, self._dispatch_callback, None)
        if self._callback_invalid:
            reason = self._callback_invalid
            self._reset_callback_buffer()
            raise ProviderError(reason)

    def response_times(self, handle):
        values = (c_float * 5)()
        self.call("SimConnect_RequestResponseTimes", handle, len(values), values)
        return tuple(values)

    def retrieve_string(self, packet, offset):
        if not sizeof(Receive) <= offset < len(packet) <= _MAX_PACKET:
            raise ProviderError("Invalid SimConnect string offset.")
        buffer = create_string_buffer(packet, len(packet))
        start = addressof(buffer)
        result, size = c_char_p(), DWORD()
        self.call("SimConnect_RetrieveString", cast(buffer, POINTER(Receive)), len(packet),
                  start + offset, byref(result), byref(size))
        pointer = cast(result, c_void_p).value
        if (pointer is None or not start + offset <= pointer < start + len(packet)
                or size.value > start + len(packet) - pointer):
            raise ProviderError("SDK string does not fit in its SimConnect packet.")
        return string_at(pointer, size.value).split(b"\0", 1)[0]

    def insert_string(self, value: bytes):
        if not isinstance(value, bytes) or b"\0" in value or len(value) > 65_535:
            raise ProviderError("Supply one bounded, NUL-free SDK string.")
        buffer = create_string_buffer((len(value) + 4) & ~3)
        start, end, count = addressof(buffer), c_void_p(), DWORD()
        self.call("SimConnect_InsertString", start, len(buffer), byref(end), byref(count), value)
        if end.value is None or not start < end.value <= start + len(buffer) or count.value > len(buffer):
            raise ProviderError("SDK returned a string outside its destination buffer.")
        return bytes(buffer)[:end.value - start]

    def last_packet_id(self, handle):
        packet_id = DWORD()
        self.call("SimConnect_GetLastSentPacketID", handle, byref(packet_id))
        return packet_id.value
