"""Demand-driven SimConnect facility cache notifications.

A watch that fails (a timeout, a malformed page, a rejected subscription) is
reported UNAVAILABLE and resubscribed with capped backoff under the same watch
ID, so consumers keep their subscription and receive a fresh SNAPSHOT later.

A rejected unsubscribe never ends the session. The simulator may still hold that
subscription, so the kind gets a quiet period (capped exponential backoff per
rejection) before it is subscribed again. A resubscription risks a duplicate
server-side subscription; the per-watch request IDs already filter its packets.
A successful initial snapshot resets the backoff.

Removals that arrive before the initial snapshot are held. Removals that name a
facility the snapshot itself lists are dropped, because the consumer has just
been told it is present; the rest are published after the snapshot (A109).
"""

from __future__ import annotations

from ctypes import sizeof
from dataclasses import dataclass, field
from functools import partial
import logging
from time import monotonic

from .facilities import FacilityChange, FacilityKind
from .provider import FacilityCacheReply, ProviderError
from .simconnect_abi import ReceiveId, ReceiveListPrefix, ReceiveRequest
from .simconnect_facility_lists import LIST_FORMATS, MINIMAL_RECORD, ListPages, facility_identity, minimal_identity
from .simconnect_ledger import PacketLedger


_LOG = logging.getLogger(__name__)
_REPLY_TIMEOUT = 8.0          # Inactivity limit: each accepted page extends it.
_REPLY_HARD_TIMEOUT = 90.0    # Upper bound for one multipart batch.
_RETRY_BASE_S = 2.0
_RETRY_MAX_S = 60.0
_TAINT_BASE_S = 2.0            # Quiet period after a rejected unsubscribe, doubled per rejection.
_TAINT_MAX_S = 60.0


@dataclass(slots=True)
class _Channel:
    pages: ListPages = field(default_factory=ListPages)
    deadline: float | None = None
    hard_deadline: float | None = None
    kind: ReceiveId | None = None

    def start(self, now):
        self.deadline = now + _REPLY_TIMEOUT
        self.hard_deadline = now + _REPLY_HARD_TIMEOUT

    def extend(self, now):
        if self.hard_deadline is None:
            self.start(now)
        else:
            self.deadline = min(self.hard_deadline, now + _REPLY_TIMEOUT)

    def expired(self, now):
        return self.deadline is not None and now >= self.deadline


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
    failures: int = 0
    retry_at: float | None = None


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
        self._sent = PacketLedger()
        # Kind -> (detail, quiet_until) after a rejected unsubscribe; see the module docstring.
        self._tainted: dict[FacilityKind, tuple[str, float]] = {}
        self._taint_counts: dict[FacilityKind, int] = {}

    def _taint(self, kind, detail):
        count = self._taint_counts.get(kind, 0)
        self._taint_counts[kind] = count + 1
        until = monotonic() + min(_TAINT_MAX_S, _TAINT_BASE_S * 2 ** min(count, 5))
        self._tainted[kind] = (detail, until)

    def _quiet(self, kind, now):
        """Return (detail, until) while ``kind`` is in its quiet period, else None."""
        taint = self._tainted.get(kind)
        if taint is not None and now >= taint[1]:
            del self._tainted[kind]
            return None
        return taint

    def replace(self, watches):
        for kind, watch in tuple(self._watches.items()):
            if watch.id != watches.get(kind):
                self._retire(watch)
                del self._watches[kind]
        for kind, watch_id in watches.items():
            if kind not in self._watches:
                self._subscribe(kind, watch_id)

    def _subscribe(self, kind, watch_id, failures=0):
        now = monotonic()
        channel = _Channel()
        channel.start(now)
        watch = _Watch(watch_id, kind, self._new_id(), self._new_id() if not self._legacy else 0,
                       channel, failures=failures)
        self._watches[kind] = watch
        quiet = self._quiet(kind, now)
        if quiet is not None:
            watch.added = _Channel()
            watch.retry_at = quiet[1]
            self._publish(watch, FacilityChange.UNAVAILABLE,
                          detail=f"Facility cache paused after a rejected unsubscribe; retrying in "
                                 f"{max(0.0, quiet[1] - now):.0f} s: {quiet[0]}")
            return
        try:
            if self._legacy:
                self._call("SimConnect_SubscribeToFacilities", LIST_FORMATS[kind].sdk_type, watch.added_id)
            else:
                self._call("SimConnect_SubscribeToFacilities_EX1", LIST_FORMATS[kind].sdk_type,
                           watch.added_id, watch.removed_id)
        except ProviderError as error:
            self._fail(watch, str(error))
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
            watch.registered = False
            try:
                if self._legacy:
                    self._call("SimConnect_UnsubscribeToFacilities", LIST_FORMATS[watch.kind].sdk_type)
                else:
                    self._call("SimConnect_UnsubscribeToFacilities_EX1", LIST_FORMATS[watch.kind].sdk_type,
                               True, True)
            except ProviderError as error:
                self._taint(watch.kind, str(error))
                _LOG.warning("Facility cache unsubscribe failed: %s", error)
            else:
                self._track(watch, retiring=True)
        watch.added, watch.removed = _Channel(), _Channel()
        watch.early_removals.clear()

    def _track(self, watch, *, retiring):
        self._sent[self._last_packet()] = watch, retiring

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
            if kind == ReceiveId.FACILITY_MINIMAL_LIST:
                wire, decode = MINIMAL_RECORD, partial(minimal_identity, layout.icao_type)
            else:
                wire, decode = layout.wire, layout.decode
            prefix = ReceiveListPrefix.from_buffer_copy(packet)
            before = len(channel.pages.pages)
            records = channel.pages.receive(prefix, packet[sizeof(ReceiveListPrefix):], wire, decode,
                                            "facility cache", tolerant=True)
            if records is not None:
                detail = channel.pages.omitted_detail()
                channel.pages = ListPages()
                channel.deadline = channel.hard_deadline = channel.kind = None
                self._complete(watch, entering, kind, records, detail)
            elif len(channel.pages.pages) != before:
                channel.extend(monotonic())  # Progress: extend the inactivity deadline.
        except ProviderError as error:
            self._fail(watch, str(error))
        return True

    def _complete(self, watch, entering, kind, records, detail=""):
        if entering:
            change = FacilityChange.SNAPSHOT if watch.initial else FacilityChange.ADDED
            self._publish(watch, change, records, detail=detail)
            if watch.initial:
                watch.failures = 0
                watch.retry_at = None
                self._taint_counts.pop(watch.kind, None)
            watch.initial = False
            if watch.early_removals:
                removals = tuple(watch.early_removals)
                watch.early_removals.clear()
                if change is FacilityChange.SNAPSHOT:
                    # The snapshot is the newer statement for a facility it lists.
                    listed = {(item.icao, item.region) for item in records}
                    kept = tuple(item for item in removals if (item.icao, item.region) not in listed)
                    if len(kept) != len(removals):
                        _LOG.info("Dropped %d early facility removal(s) that the initial snapshot lists.",
                                  len(removals) - len(kept))
                    removals = kept
                if removals:
                    self._publish(watch, FacilityChange.REMOVED, removals)
        else:
            identities = records if kind == ReceiveId.FACILITY_MINIMAL_LIST else tuple(map(facility_identity, records))
            if watch.initial:
                if len(watch.early_removals) + len(identities) > 65_536:
                    raise ProviderError("Too many facility removals before the initial snapshot.")
                watch.early_removals.extend(identities)
            else:
                self._publish(watch, FacilityChange.REMOVED, identities, detail=detail)

    def receive_exception(self, send_id, detail):
        operation = self._sent.pop(send_id, None)
        if operation is None:
            return False
        watch, retiring = operation
        if retiring:
            # Non-fatal. Late packets for the retired request IDs are already unrouted.
            self._taint(watch.kind, detail)
            _LOG.warning("SimConnect rejected a facility cache unsubscribe: %s", detail)
            current = self._watches.get(watch.kind)
            if current is not None and current is not watch:
                self._fail(current, f"An earlier facility cache unsubscribe was rejected: {detail}")
            return True
        if self._watches.get(watch.kind) is watch and watch.registered:
            watch.registered = False  # SDK rejected the registration.
            self._fail(watch, detail)
        return True

    def expire(self):
        now = monotonic()
        for kind, watch in tuple(self._watches.items()):
            if watch.registered:
                if watch.added.expired(now) or watch.removed.expired(now):
                    self._fail(watch, "Simulator facility cache reply timed out.")
            elif watch.retry_at is not None and now >= watch.retry_at:
                del self._watches[kind]
                self._subscribe(kind, watch.id, watch.failures)

    def _fail(self, watch, reason):
        self._retire(watch)
        watch.failures += 1
        watch.initial = True
        now = monotonic()
        watch.retry_at = now + min(_RETRY_MAX_S, _RETRY_BASE_S * 2 ** min(watch.failures - 1, 5))
        quiet = self._quiet(watch.kind, now)
        if quiet is not None:
            watch.retry_at = max(watch.retry_at, quiet[1])
        self._publish(watch, FacilityChange.UNAVAILABLE, detail=reason)

    def _publish(self, watch, change, entries=(), *, detail=""):
        self._offer(FacilityCacheReply(self._session, watch.id, watch.kind, change, entries, detail))
