"""Local x64 SimConnect DLL boundary. The host controls when loading occurs."""

from ctypes import POINTER, addressof, byref, c_char_p, c_float, c_void_p, cast, create_string_buffer, sizeof, string_at
from pathlib import Path
import ctypes
import sys

from .provider import ProviderError
from .simconnect_abi import DWORD, HANDLE, HRESULT, DispatchProc, Receive, SIGNATURES


class SimConnectSdk:
    """The provider accepts an injected SDK instead for offline verification."""

    simulated = False

    def __init__(self, dll_path, *, callback_dispatch=False, load_on_open=False):
        path = Path(dll_path)
        if not path.is_absolute() or path.name.casefold() != "simconnect.dll":
            raise ProviderError("Supply an absolute path to the local SimConnect.dll.")
        self._path = path
        self._functions = None
        self._library = None
        self._callback_dispatch = callback_dispatch
        self._load_on_open = load_on_open
        self._callback_packets = []
        self._callback_failed = False
        self._dispatch_callback = DispatchProc(self._receive_callback)

    def load(self):
        if self._functions is not None:
            return
        if sys.platform != "win32" or sizeof(HANDLE) != 8:
            raise ProviderError("SimConnect requires a 64-bit Windows host.")
        try:
            library = ctypes.WinDLL(str(self._path.resolve(strict=True)))
            functions = {}
            for name, arguments in SIGNATURES.items():
                function = getattr(library, name)
                function.argtypes = arguments
                function.restype = HRESULT
                functions[name] = function
        except (OSError, AttributeError, RuntimeError) as error:
            raise ProviderError(f"Could not load the selected SimConnect SDK: {error}") from error
        self._library = library
        self._functions = functions

    def call(self, name, *args):
        if self._functions is None:
            raise ProviderError("Load the selected SimConnect DLL before using the provider.")
        result = self._functions[name](*args)
        if result < 0:
            raise ProviderError(f"{name} failed with HRESULT 0x{result & 0xFFFFFFFF:08X}.")

    def open(self, application):
        if self._load_on_open:
            self.load()
        handle = HANDLE()
        # SIMCONNECT_OPEN_CONFIGINDEX_LOCAL bypasses SimConnect.cfg.
        self.call("SimConnect_Open", byref(handle), application, None, 0, None, 0xFFFFFFFF)
        if not handle.value:
            raise ProviderError("SimConnect_Open returned an empty handle.")
        return handle

    def close(self, handle):
        self.call("SimConnect_Close", handle)
        self._callback_packets.clear()
        self._callback_failed = False

    def next_packet(self, handle):
        if self._callback_dispatch:
            if not self._callback_packets:
                self._call_dispatch(handle)
            return self._callback_packets.pop(0) if self._callback_packets else None
        pointer, size = POINTER(Receive)(), DWORD()
        self.call("SimConnect_GetNextDispatch", handle, byref(pointer), byref(size))
        if not pointer or not sizeof(Receive) <= size.value <= 1024 * 1024:
            raise ProviderError("SimConnect returned an invalid packet pointer or byte count.")
        # The SDK reuses this memory on a later call. Copy it while we own it.
        return string_at(pointer, size.value)

    def _receive_callback(self, pointer, size, _context):
        if not pointer or not sizeof(Receive) <= size <= 1024 * 1024 or len(self._callback_packets) >= 512:
            self._callback_failed = True
        else:
            self._callback_packets.append(string_at(pointer, size))

    def _call_dispatch(self, handle):
        self.call("SimConnect_CallDispatch", handle, self._dispatch_callback, None)
        if self._callback_failed:
            self._callback_packets.clear()
            self._callback_failed = False
            raise ProviderError("Invalid or excessive SimConnect callback packets.")

    def response_times(self, handle):
        values = (c_float * 5)()
        self.call("SimConnect_RequestResponseTimes", handle, len(values), values)
        return tuple(values)

    def retrieve_string(self, packet, offset):
        if not sizeof(Receive) <= offset < len(packet) <= 1024 * 1024:
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
