"""Demand-driven SimConnect facility cache notifications."""

from ctypes import sizeof
from dataclasses import dataclass, field
from functools import partial
from time import monotonic

from .facilities import FacilityChange, FacilityKind
from .provider import FacilityCacheReply, ProviderError
from .simconnect_abi import ReceiveId, ReceiveListPrefix, ReceiveRequest
from .simconnect_facility_lists import LIST_FORMATS, MINIMAL_RECORD, ListPages, facility_identity, minimal_identity


_REPLY_TIMEOUT = 8.0


@dataclass(slots=True)
class _Channel:
    pages: ListPages = field(default_factory=ListPages)
    deadline: float | None = None
    kind: ReceiveId | None = None


@dataclass(slots=True)
class _Watch:
    id: str
    kind: FacilityKind
    added_id: int
    removed_id: int
    added: _Channel
    removed: _Channel = field(default_factory=_Channel)
    initial: bool = True
    registered: bool = False
    early_removals: list = field(default_factory=list)


class SimConnectFacilityCache:
    def __init__(self, session, call, last_packet, new_id, offer, *, legacy=False):
        self._session = session
        self._call = call
        self._last_packet = last_packet
        self._new_id = new_id
        self._offer = offer
        self._legacy = legacy
        self._watches: dict[FacilityKind, _Watch] = {}
        self._by_request: dict[int, tuple[_Watch, bool]] = {}
        self._sent: dict[int, tuple[_Watch, bool]] = {}

    def replace(self, watches):
        for kind, watch in tuple(self._watches.items()):
            if watch.id != watches.get(kind):
                self._retire(watch)
                del self._watches[kind]
        for kind, watch_id in watches.items():
            if kind not in self._watches:
                self._subscribe(kind, watch_id)

    def _subscribe(self, kind, watch_id):
        watch = _Watch(watch_id, kind, self._new_id(), self._new_id() if not self._legacy else 0,
                       _Channel(deadline=monotonic() + _REPLY_TIMEOUT))
        self._watches[kind] = watch
        try:
            if self._legacy:
                self._call("SimConnect_SubscribeToFacilities", LIST_FORMATS[kind].sdk_type, watch.added_id)
            else:
                self._call("SimConnect_SubscribeToFacilities_EX1", LIST_FORMATS[kind].sdk_type,
                           watch.added_id, watch.removed_id)
        except ProviderError as error:
            self._publish(watch, FacilityChange.UNAVAILABLE, detail=str(error))
            return
        self._track(watch, retiring=False)
        watch.registered = True
        self._by_request[watch.added_id] = watch, True
        if watch.removed_id:
            self._by_request[watch.removed_id] = watch, False

    def _retire(self, watch):
        self._by_request.pop(watch.added_id, None)
        self._by_request.pop(watch.removed_id, None)
        if watch.registered:
            if self._legacy:
                self._call("SimConnect_UnsubscribeToFacilities", LIST_FORMATS[watch.kind].sdk_type)
            else:
                self._call("SimConnect_UnsubscribeToFacilities_EX1", LIST_FORMATS[watch.kind].sdk_type, True, True)
            self._track(watch, retiring=True)
            watch.registered = False
        watch.added, watch.removed = _Channel(), _Channel()
        watch.early_removals.clear()

    def _track(self, watch, *, retiring):
        self._sent[self._last_packet()] = watch, retiring
        if len(self._sent) > 512:
            del self._sent[next(iter(self._sent))]

    def receive(self, kind, packet):
        if len(packet) < sizeof(ReceiveRequest):
            return False
        matched = self._by_request.get(ReceiveRequest.from_buffer_copy(packet).request_id)
        if matched is None:
            return False
        watch, entering = matched
        channel = watch.added if entering else watch.removed
        layout = LIST_FORMATS[watch.kind]
        try:
            if len(packet) < sizeof(ReceiveListPrefix):
                raise ProviderError("Truncated simulator facility cache page.")
            if (kind != layout.receive_id and (entering or kind != ReceiveId.FACILITY_MINIMAL_LIST)
                    or channel.kind is not None and channel.kind != kind):
                raise ProviderError("Unexpected simulator facility cache packet type.")
            channel.kind = kind
            if channel.deadline is None:
                channel.deadline = monotonic() + _REPLY_TIMEOUT
            if kind == ReceiveId.FACILITY_MINIMAL_LIST:
                wire, decode = MINIMAL_RECORD, partial(minimal_identity, layout.icao_type)
            else:
                wire, decode = layout.wire, layout.decode
            prefix = ReceiveListPrefix.from_buffer_copy(packet)
            records = channel.pages.receive(prefix, packet[sizeof(ReceiveListPrefix):], wire, decode, "facility cache")
            if records is not None:
                channel.pages = ListPages()
                channel.deadline = channel.kind = None
                self._complete(watch, entering, kind, records)
        except ProviderError as error:
            self._fail(watch, str(error))
        return True

    def _complete(self, watch, entering, kind, records):
        if entering:
            change = FacilityChange.SNAPSHOT if watch.initial else FacilityChange.ADDED
            self._publish(watch, change, records)
            watch.initial = False
            if watch.early_removals:
                self._publish(watch, FacilityChange.REMOVED, tuple(watch.early_removals))
                watch.early_removals.clear()
        else:
            identities = records if kind == ReceiveId.FACILITY_MINIMAL_LIST else tuple(map(facility_identity, records))
            if watch.initial:
                if len(watch.early_removals) + len(identities) > 65_536:
                    raise ProviderError("Too many facility removals before the initial snapshot.")
                watch.early_removals.extend(identities)
            else:
                self._publish(watch, FacilityChange.REMOVED, identities)

    def receive_exception(self, send_id, detail):
        operation = self._sent.pop(send_id, None)
        if operation is None:
            return False
        watch, retiring = operation
        if retiring:
            raise ProviderError(detail)
        if self._watches.get(watch.kind) is watch and watch.registered:
            watch.registered = False  # SDK rejected the registration.
            self._fail(watch, detail)
        return True

    def expire(self):
        now = monotonic()
        for watch in self._watches.values():
            if watch.registered and any(
                channel.deadline is not None and now >= channel.deadline for channel in (watch.added, watch.removed)
            ):
                self._fail(watch, "Simulator facility cache reply timed out.")

    def _fail(self, watch, reason):
        self._retire(watch)
        self._publish(watch, FacilityChange.UNAVAILABLE, detail=reason)

    def _publish(self, watch, change, entries=(), *, detail=""):
        self._offer(FacilityCacheReply(self._session, watch.id, watch.kind, change, entries, detail))
