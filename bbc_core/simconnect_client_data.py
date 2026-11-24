"""Session-scoped SimConnect Client Data bindings and subscriptions."""

from ctypes import sizeof
from dataclasses import dataclass
from datetime import datetime, timezone
from struct import pack
from time import monotonic

from .client_data import ClientDataArea, ClientDataMode
from .provider import CommandRejected, ProviderError, Reading, Signal
from .simconnect_abi import ClientDataPeriod, ReceiveSimObjectDataPrefix


DATUM_ID = 1
_TAGGED = 1
_CREATOR_ONLY = 1
_READ_TIMEOUT = 3.0


@dataclass(slots=True)
class _Area:
    declaration: ClientDataArea
    id: int
    error: str | None = None


@dataclass(slots=True)
class ClientDataBinding:
    signal: Signal
    area: _Area
    definition: int
    started: bool = False
    streaming: bool = False
    primed: bool = False
    error: str | None = None

    @property
    def region(self):
        return self.signal.client_data_region


class SimConnectClientData:
    def __init__(self, session_id, call, last_packet, new_id, offer):
        self._session_id = session_id
        self._call = call
        self._last_packet = last_packet
        self._new_id = new_id
        self._offer = offer
        self._areas: dict[str, _Area] = {}
        self._active: dict[Signal, ClientDataBinding] = {}
        self._streams: dict[int, ClientDataBinding] = {}
        self._reads: dict[int, tuple[ClientDataBinding, float]] = {}
        self._sent: dict[int, tuple[str, object]] = {}

    def subscribe(self, signal):
        region = signal.client_data_region
        if signal in self._active:
            return
        area = self._areas.get(region.area.name)
        if area is None:
            area = _Area(region.area, self._new_id())
            self._areas[region.area.name] = area
            try:
                self._tracked(
                    "area", area, "SimConnect_MapClientDataNameToID",
                    region.area.name.encode("ascii"), area.id,
                )
                if region.area.create:
                    flags = _CREATOR_ONLY if region.area.creator_only else 0
                    self._tracked(
                        "area", area, "SimConnect_CreateClientData", area.id, region.area.size, flags,
                    )
            except ProviderError as error:
                area.error = str(error)
        binding = ClientDataBinding(signal, area, self._new_id())
        self._active[signal] = binding
        if area.error:
            self._unavailable(binding, area.error)
            return
        try:
            self._tracked(
                "definition", binding, "SimConnect_AddToClientDataDefinition",
                binding.definition, region.offset, region.size, 0.0, DATUM_ID,
            )
            binding.started = True
            if signal.client_data_mode is not ClientDataMode.ONCE:
                period = ClientDataPeriod[signal.client_data_mode.name]
                self._request(binding, binding.definition, period, "stream", binding)
                binding.streaming = True
                self._streams[binding.definition] = binding
        except ProviderError as error:
            self._unavailable(binding, str(error))
            return
        self.request_value(signal)

    def unsubscribe(self, signal):
        binding = self._active.pop(signal, None)
        if binding is None:
            return
        self._streams.pop(binding.definition, None)
        for request, (item, _) in tuple(self._reads.items()):
            if item is binding:
                del self._reads[request]
        if binding.streaming:
            self._request(binding, binding.definition, ClientDataPeriod.NEVER, "retire", binding)
        if binding.started:
            self._tracked("retire", binding, "SimConnect_ClearClientDataDefinition", binding.definition)

    def request_value(self, signal):
        binding = self._active.get(signal)
        if binding is None or binding.error or binding.area.error:
            return
        if any(item is binding for item, _ in self._reads.values()):
            return
        request = self._new_id()
        try:
            self._request(binding, request, ClientDataPeriod.ONCE, "read", request)
        except ProviderError as error:
            self._reading(binding, None, error=str(error))
            return
        self._reads[request] = (binding, monotonic() + _READ_TIMEOUT)

    def prepare_write(self, region, value):
        binding = next((item for item in self._active.values()
                        if item.region == region and item.primed and not item.error and not item.area.error), None)
        if not region.area.writable:
            raise CommandRejected("This Client Data area is read-only in Core.")
        if not isinstance(value, bytes) or len(value) != region.size:
            raise CommandRejected("The Client Data payload does not match the region size.")
        if binding is None or not binding.primed or binding.error or binding.area.error:
            raise CommandRejected("Read the declared Client Data region in this session before writing.")
        # Always use a tagged write. A region can be smaller than the area
        # allocated by the simulator, especially when another client owns it.
        return binding, _TAGGED, pack("<I", DATUM_ID) + value

    def process(self, prefix, packet):
        request = self._reads.pop(prefix.request_id, None)
        binding = request[0] if request else self._streams.get(prefix.request_id)
        if binding is None or binding.error or binding.area.error:
            return
        try:
            value = self.payload(prefix, packet, binding.definition, binding.region.size)
        except ProviderError as error:
            binding.primed = False
            self._reading(binding, None, error=str(error))
            return
        if request is not None:
            binding.primed = True
        self._reading(binding, value)

    @staticmethod
    def payload(prefix, packet, definition, size):
        offset = sizeof(ReceiveSimObjectDataPrefix)
        if (prefix.definition_id != definition or prefix.definition_count != 1
                or prefix.flags or len(packet) != offset + size):
            raise ProviderError("Invalid Client Data definition or payload size.")
        return packet[offset:]

    def receive_exception(self, send_id, detail):
        tracked = self._sent.pop(send_id, None)
        if tracked is None:
            return False
        operation, key = tracked
        if operation == "retire":
            raise ProviderError(detail)
        if operation == "area":
            key.error = detail
            for binding in self._active.values():
                if binding.area is key:
                    self._unavailable(binding, detail)
        elif operation == "read":
            request = self._reads.pop(key, None)
            if request is not None:
                request[0].primed = False
                self._reading(request[0], None, error=detail)
        elif self._active.get(key.signal) is key:
            self._unavailable(key, detail)
        return True

    def expire(self):
        now = monotonic()
        for request, (binding, deadline) in tuple(self._reads.items()):
            if now >= deadline:
                del self._reads[request]
                binding.primed = False
                self._reading(binding, None, error="Client Data read timed out.")

    def _request(self, binding, id, period, operation, key):
        self._tracked(
            operation, key, "SimConnect_RequestClientData", binding.area.id, id,
            binding.definition, period, 0, 0, 0, 0,
        )

    def _unavailable(self, binding, reason):
        binding.primed = False
        binding.error = reason
        self._reading(binding, None, error=reason)

    def _reading(self, binding, value, *, error=None):
        self._offer(Reading(
            self._session_id, binding.signal, value, datetime.now(timezone.utc), monotonic(), error=error,
        ))

    def _tracked(self, operation, key, function, *arguments):
        self._call(function, *arguments)
        self._sent[self._last_packet()] = (operation, key)
        if len(self._sent) > 512:
            del self._sent[next(iter(self._sent))]
