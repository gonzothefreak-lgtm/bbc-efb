"""Aircraft Input Event traffic owned by the direct SimConnect provider."""

from __future__ import annotations

from ctypes import sizeof
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from struct import unpack_from
from time import monotonic

from .aircraft_inputs import encode_input_arguments
from .contract import (
    AircraftInput, CoreRequestError, InputCatalog, InputCatalogState, InputId, InputParameters,
    ValueType,
)
from .provider import CommandRejected, ProviderError, Reading, Signal
from .simconnect_abi import (
    DataType, InputEventDescriptor, ReceiveGetInputPrefix, ReceiveId, ReceiveInputParameters,
    ReceiveListPrefix, ReceiveSubscribeInputPrefix,
)
from .simconnect_ledger import PacketLedger


_LOG = logging.getLogger(__name__)
# SIMCONNECT_INPUT_EVENT_DESCRIPTOR.eType is a SIMCONNECT_DATATYPE.
_DESCRIPTOR_TYPES = {
    DataType.INT32: ValueType.NUMBER, DataType.INT64: ValueType.NUMBER,
    DataType.FLOAT32: ValueType.NUMBER, DataType.FLOAT64: ValueType.NUMBER,
    **{kind: ValueType.TEXT for kind in (
        DataType.STRING8, DataType.STRING32, DataType.STRING64, DataType.STRING128,
        DataType.STRING256, DataType.STRING260, DataType.STRINGV,
    )},
}
# GET_INPUT_EVENT and SUBSCRIBE_INPUT_EVENT carry a SIMCONNECT_INPUT_EVENT_TYPE.
_EVENT_VALUE_TYPES = {0: ValueType.NUMBER, 1: ValueType.TEXT}
_TIMEOUT = 3.0
_GET_ATTEMPTS = 3
_LATE_READS = 64


@dataclass(slots=True)
class _Enumeration:
    request_id: int
    deadline: float
    total_pages: int | None = None
    pages: dict[int, tuple[AircraftInput, ...]] = field(default_factory=dict)
    item_count: int = 0


class SimConnectInputEvents:
    """Scope hashes and cached metadata to a simulator session and aircraft."""

    def __init__(self, session_id, call, last_packet, new_id, offer, retain_lifecycle):
        self._session_id = session_id
        self._call = call
        self._last_packet = last_packet
        self._new_id = new_id
        self._offer = offer
        self._retain_lifecycle = retain_lifecycle
        self._generation = 0
        self._lifecycle_ids = set()
        self._enumeration = None
        self._catalog: dict[int, AircraftInput] = {}
        self._parameters: dict[InputId, tuple[str, ...]] = {}
        self._parameter_requests: dict[InputId, float] = {}
        self._retired_parameters: set[int] = set()
        self._reads: dict[int, tuple[InputId, float, int]] = {}
        self._late_reads: dict[int, InputId] = {}
        self._active: dict[int, Signal] = {}
        self._subscribed: set[int] = set()
        self._primed: set[InputId] = set()
        # Inputs whose initial GetInputEvent failed every attempt. The next valid
        # SUBSCRIBE_INPUT_EVENT value primes them instead, so the subscription is
        # not silent for the rest of the aircraft session (A113).
        self._get_exhausted: set[InputId] = set()
        self._sent = PacketLedger()
        self._unknown_types: set[int] = set()

    def request_catalog(self):
        """Start (or restart) catalog discovery.

        A request while an enumeration is in progress restarts it under a new
        generation, so "Discover again" after a gap always yields a fresh catalog;
        pages of the abandoned enumeration no longer match its request ID.
        """
        if not self._lifecycle_ids:
            self._lifecycle_ids = self._retain_lifecycle()
        self._invalidate(InputCatalogState.LOADING, "Discovering inputs for the loaded aircraft.")
        request = self._new_id()
        self._enumeration = _Enumeration(request, monotonic() + _TIMEOUT)
        try:
            self._tracked("catalog", request, "SimConnect_EnumerateInputEvents", request)
        except ProviderError as error:
            self._catalog_failed(str(error))

    def aircraft_changed(self, event_id):
        if event_id not in self._lifecycle_ids:
            return False
        self._invalidate(InputCatalogState.UNAVAILABLE, "Aircraft or flight changed. Discover its inputs again.")
        return True

    def _invalidate(self, state, detail):
        for signal in tuple(self._active.values()):
            # After an aircraft change the old hashes may no longer exist. The
            # unsubscribe is tracked and its rejection tolerated.
            self.unsubscribe(signal)
        self._generation += 1
        self._enumeration = None
        self._catalog.clear()
        self._parameters.clear()
        self._retired_parameters.update(ref.hash for ref in self._parameter_requests)
        self._parameter_requests.clear()
        self._reads.clear()
        self._late_reads.clear()
        self._primed.clear()
        self._get_exhausted.clear()
        self._subscribed.clear()
        self._publish_catalog(state, detail)

    def _publish_catalog(self, state, detail=""):
        self._offer(InputCatalog(
            self._session_id, self._generation, state, tuple(self._catalog.values()), detail,
        ))

    def _catalog_failed(self, detail):
        self._enumeration = None
        self._publish_catalog(InputCatalogState.FAILED, detail)

    def _descriptor(self, input_id):
        item = self._catalog.get(input_id.hash)
        return item if item is not None and item.id == input_id else None

    def subscribe(self, signal):
        ref = signal.input_id
        if self._descriptor(ref) is None or ref.hash in self._active:
            return
        self._active[ref.hash] = signal
        try:
            self._tracked("subscribe", ref, "SimConnect_SubscribeInputEvent", ref.hash)
        except ProviderError as error:
            self._reading(ref, None, error=str(error))
            del self._active[ref.hash]
            return
        self._subscribed.add(ref.hash)  # Optimistic; undone if the simulator rejects it.
        self.request_value(ref)

    def unsubscribe(self, signal):
        ref = signal.input_id
        if self._active.get(ref.hash) != signal:
            return
        del self._active[ref.hash]
        self._primed.discard(ref)
        self._get_exhausted.discard(ref)
        for request, (input_id, *_) in tuple(self._reads.items()):
            if input_id == ref:
                del self._reads[request]
        for request, input_id in tuple(self._late_reads.items()):
            if input_id == ref:
                del self._late_reads[request]
        if ref.hash in self._subscribed:
            self._subscribed.discard(ref.hash)
            try:
                self._tracked("unsubscribe", ref, "SimConnect_UnsubscribeInputEvent", ref.hash)
            except ProviderError as error:
                _LOG.warning("SimConnect_UnsubscribeInputEvent failed: %s", error)

    def request_value(self, input_id, attempt=0):
        signal = self._active.get(input_id.hash)
        if signal is None or signal.input_id != input_id:
            return
        if any(ref == input_id for ref, *_ in self._reads.values()):
            return
        request = self._new_id()
        try:
            self._tracked("get", request, "SimConnect_GetInputEvent", request, input_id.hash)
        except ProviderError as error:
            self._reading(input_id, None, error=str(error))
            if input_id not in self._primed:
                self._get_exhausted.add(input_id)
            return
        self._reads[request] = (input_id, monotonic() + _TIMEOUT * 2 ** attempt, attempt)

    def request_parameters(self, input_id):
        if self._descriptor(input_id) is None or input_id in self._parameter_requests:
            return
        if input_id.hash in self._retired_parameters:
            self._offer(InputParameters(
                input_id, None, "An earlier parameter request is unresolved. Reconnect if it does not clear.",
            ))
            return
        if len(self._retired_parameters) + len(self._parameter_requests) >= 4096:
            self._offer(InputParameters(input_id, None, "Reconnect to clear unresolved parameter requests."))
            return
        self._parameters.pop(input_id, None)
        try:
            self._tracked("parameters", input_id, "SimConnect_EnumerateInputEventParams", input_id.hash)
        except ProviderError as error:
            self._offer(InputParameters(input_id, None, str(error)))
            return
        self._parameter_requests[input_id] = monotonic() + _TIMEOUT

    def prepare_write(self, input_id, arguments):
        if self._descriptor(input_id) is None:
            raise CommandRejected("The input is no longer available on this aircraft.")
        types = self._parameters.get(input_id)
        if types is None:
            raise CommandRejected("Request current aircraft input parameters before writing.")
        try:
            return encode_input_arguments(types, arguments)
        except CoreRequestError as error:
            raise CommandRejected(str(error)) from error

    def _tracked(self, operation, key, function, *arguments):
        self._call(function, *arguments)
        self._sent[self._last_packet()] = (operation, key)

    def _retry_get(self, ref, attempt, reason):
        if attempt + 1 < _GET_ATTEMPTS and self._active.get(ref.hash) is not None:
            self._reading(ref, None, error=f"{reason} Retrying.")
            self.request_value(ref, attempt + 1)
        elif ref in self._primed:
            self._reading(ref, None, error=reason)
        else:
            self._get_exhausted.add(ref)
            self._reading(ref, None, error=f"{reason} Waiting for the next change from the simulator.")

    def receive_exception(self, send_id, detail):
        tracked = self._sent.pop(send_id, None)
        if tracked is None:
            return False
        action, key = tracked
        if action == "catalog" and self._enumeration is not None and self._enumeration.request_id == key:
            self._catalog_failed(detail)
        elif action == "parameters":
            self._retired_parameters.discard(key.hash)
            if self._parameter_requests.pop(key, None) is not None:
                self._offer(InputParameters(key, None, detail))
        elif action == "get":
            request = self._reads.pop(key, None)
            if request is not None:
                self._retry_get(request[0], request[2], detail)
        elif action == "subscribe":
            self._reading(key, None, error=detail)  # Before removal: _reading looks up _active.
            self._subscribed.discard(key.hash)
            signal = self._active.get(key.hash)
            if signal is not None and signal.input_id == key:
                del self._active[key.hash]
                self._primed.discard(key)
                self._get_exhausted.discard(key)
                for request, (input_id, *_) in tuple(self._reads.items()):
                    if input_id == key:
                        del self._reads[request]
        elif action == "unsubscribe":
            # Typically a hash of an aircraft that is no longer loaded. Harmless.
            _LOG.info("SimConnect rejected an Input Event unsubscribe: %s", detail)
        return True

    def expire(self):
        now = monotonic()
        if self._enumeration is not None and now >= self._enumeration.deadline:
            self._catalog_failed("Timed out waiting for all aircraft input catalog pages.")
        for ref, deadline in tuple(self._parameter_requests.items()):
            if now >= deadline:
                del self._parameter_requests[ref]
                self._retired_parameters.add(ref.hash)
                self._offer(InputParameters(ref, None, "Aircraft input parameter request timed out."))
        for request, (ref, deadline, attempt) in tuple(self._reads.items()):
            if now >= deadline:
                del self._reads[request]
                # A slow gauge may still answer: keep the request ID briefly.
                self._late_reads[request] = ref
                while len(self._late_reads) > _LATE_READS:
                    del self._late_reads[next(iter(self._late_reads))]
                self._retry_get(ref, attempt, "Aircraft input value request timed out.")

    def process(self, kind, packet):
        if kind == ReceiveId.ENUMERATE_INPUT_EVENTS:
            self._receive_page(packet)
        elif kind == ReceiveId.ENUMERATE_INPUT_EVENT_PARAMS:
            self._receive_parameters(packet)
        elif kind == ReceiveId.GET_INPUT_EVENT:
            response = self._struct(ReceiveGetInputPrefix, packet)
            request = self._reads.pop(response.request_id, None)
            ref = request[0] if request is not None else self._late_reads.pop(response.request_id, None)
            if ref is not None:
                if request is None:
                    # A late reply to a timed-out read supersedes its retry.
                    for pending, (input_id, *_) in tuple(self._reads.items()):
                        if input_id == ref:
                            del self._reads[pending]
                self._receive_value(ref, response.type, packet[sizeof(ReceiveGetInputPrefix):], prime=True)
        elif kind == ReceiveId.SUBSCRIBE_INPUT_EVENT:
            response = self._struct(ReceiveSubscribeInputPrefix, packet)
            signal = self._active.get(response.hash)
            if signal is None:
                return True
            ref = signal.input_id
            payload = packet[sizeof(ReceiveSubscribeInputPrefix):]
            if ref in self._primed:
                self._receive_value(ref, response.type, payload)
            elif ref in self._get_exhausted and not any(input_id == ref for input_id, *_ in self._reads.values()):
                # The initial read never succeeded; a valid change value primes instead.
                self._receive_value(ref, response.type, payload, prime=True)
        else:
            return False
        return True

    def _receive_page(self, packet):
        page = self._struct(ReceiveListPrefix, packet)
        request = self._enumeration
        if request is None or page.request_id != request.request_id:
            return
        total = page.out_of or 1
        offset, width = sizeof(ReceiveListPrefix), sizeof(InputEventDescriptor)
        if (total > 512 or page.entry_number >= total
                or (page.out_of == 0 and page.array_size)
                or (request.total_pages is not None and request.total_pages != total)
                or page.array_size * width > len(packet) - offset):
            self._catalog_failed("Invalid aircraft input catalog page.")
            return
        request.total_pages = total
        if page.entry_number in request.pages:
            return
        request.item_count += page.array_size
        if request.item_count > 16_384:
            self._catalog_failed("Aircraft input catalog exceeds the supported size.")
            return
        items = []
        for item in range(page.array_size):
            descriptor = InputEventDescriptor.from_buffer_copy(packet, offset + item * width)
            value_type = _DESCRIPTOR_TYPES.get(descriptor.type)
            if value_type is None and descriptor.type not in self._unknown_types:
                self._unknown_types.add(descriptor.type)
                _LOG.warning("Aircraft input descriptor reports unknown SIMCONNECT_DATATYPE %s.", descriptor.type)
            if descriptor.name and descriptor.hash:
                items.append(AircraftInput(
                    InputId(self._session_id, self._generation, descriptor.hash),
                    descriptor.name.decode("utf-8", errors="replace"), value_type,
                ))
        request.pages[page.entry_number] = tuple(items)
        if len(request.pages) != total:
            return
        catalog = {}
        for number in range(total):
            for item in request.pages[number]:
                previous = catalog.get(item.id.hash)
                if previous is not None and previous != item:
                    self._catalog_failed("Conflicting descriptions for an aircraft input hash.")
                    return
                catalog[item.id.hash] = item
        self._catalog = catalog
        self._enumeration = None
        self._publish_catalog(InputCatalogState.READY)

    def _receive_parameters(self, packet):
        response = self._struct(ReceiveInputParameters, packet)
        if response.hash in self._retired_parameters:
            self._retired_parameters.remove(response.hash)
            return
        entry = self._catalog.get(response.hash)
        if entry is None or self._parameter_requests.pop(entry.id, None) is None:
            return
        try:
            # The SDK uses semicolon-separated type names and may start with a delimiter.
            names = response.value.decode("ascii").strip(";").split(";") if response.value else ()
            types = tuple(name.strip() for name in names)
        except UnicodeError:
            self._offer(InputParameters(entry.id, None, "Invalid aircraft input parameter description."))
            return
        self._parameters[entry.id] = types
        self._offer(InputParameters(entry.id, types))

    def _receive_value(self, ref, data_type, payload, *, prime=False):
        entry = self._descriptor(ref)
        if entry is None or self._active.get(ref.hash) is None:
            return
        packet_type = _EVENT_VALUE_TYPES.get(data_type)
        if packet_type is None or (entry.value_type is not None and packet_type is not entry.value_type):
            self._reading(ref, None, error="Aircraft input returned a different value type.")
        elif data_type == 0 and len(payload) >= 8:
            self._reading(ref, unpack_from("<d", payload)[0])
            if prime:
                self._primed.add(ref)
                self._get_exhausted.discard(ref)
        elif data_type == 1 and b"\0" in payload[:256]:
            self._reading(ref, payload[:256].split(b"\0", 1)[0].decode("utf-8", errors="replace"))
            if prime:
                self._primed.add(ref)
                self._get_exhausted.discard(ref)
        else:
            self._reading(ref, None, error="Malformed aircraft input value.")

    def _reading(self, input_id, value, *, error=None):
        signal = self._active.get(input_id.hash)
        if signal is not None and signal.input_id == input_id:
            self._offer(Reading(
                self._session_id, signal, value, datetime.now(timezone.utc), monotonic(), error=error,
            ))

    @staticmethod
    def _struct(cls, packet):
        if len(packet) < sizeof(cls):
            raise ProviderError(f"Truncated SimConnect {cls.__name__} packet.")
        return cls.from_buffer_copy(packet)
