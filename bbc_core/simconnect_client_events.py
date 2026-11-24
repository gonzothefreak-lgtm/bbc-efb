"""Shared, non-masking SimConnect client event reception."""

from __future__ import annotations

from ctypes import sizeof
import logging

from .contract import ClientEventData
from .provider import ProviderError, Signal
from .simconnect_abi import ReceiveEvent, ReceiveEventEx1, ReceiveEventHeader, ReceiveId
from .simconnect_ledger import PacketLedger


_LOG = logging.getLogger(__name__)
_PRIVATE_PREFIX = "RESERVED_KEY_"


_DEFAULT_PRIORITY = 2_000_000_000
_UNKNOWN_GROUP = 0xFFFFFFFF


class SimConnectClientEvents:
    def __init__(self, call, last_packet, new_id, map_event, reading):
        self._call = call
        self._last_packet = last_packet
        self._new_id = new_id
        self._map_event = map_event
        self._reading = reading
        self._group = None
        self._generation = 0
        self._desired: dict[Signal, None] = {}
        self._members: dict[Signal, int] = {}
        self._by_id: dict[int, Signal] = {}
        self._failed: set[Signal] = set()
        self._add_packets: dict[Signal, int] = {}
        self._sent = PacketLedger()
        # Private (unnamed) client events, such as a reserved key, by public name.
        self._private: dict[str, int] = {}

    def register_private(self, name, event_id):
        """Make a private client event subscribable as ``subscribe_client_event(name)``."""
        self._private[name] = event_id
        for signal in tuple(self._failed):
            if signal.name == name:
                self._failed.discard(signal)

    def replace(self, desired):
        self._desired = desired
        self._failed.intersection_update(desired)
        removed = set(self._members).difference(desired)
        if removed and len(removed) == len(self._members):
            self._clear()
        else:
            for signal in tuple(self._members):
                if signal in removed:
                    event_id = self._members.pop(signal)
                    self._by_id.pop(event_id, None)
                    self._add_packets.pop(signal, None)
                    try:
                        self._call("SimConnect_RemoveClientEvent", self._group, event_id)
                    except ProviderError as error:
                        self._cleanup_failed(f"RemoveClientEvent failed: {error}")
                    else:
                        self._track("remove")

    def activate_after_drain(self):
        """Discard queued packets before activating any newly requested mapping."""
        for signal in self._desired:
            if signal not in self._members and signal not in self._failed:
                self._add(signal)

    def request_dialog_notifications(self):
        if not self._members:
            raise ProviderError("An active SimConnect client event subscription is required.")
        self._call("SimConnect_RequestNotificationGroup", self._group, 0, 0)
        self._track("dialog")

    def _add(self, signal):
        if len(self._members) >= 1000:
            self._fail(signal, "The SimConnect notification group holds 1,000 client events.")
            return
        if self._group is None:
            self._group = self._new_id()
        try:
            if signal.name.startswith(_PRIVATE_PREFIX):
                event_id = self._private.get(signal.name)
                if event_id is None:
                    raise ProviderError("No key reservation in this SimConnect session uses this event name.")
            else:
                event_id = self._map_event(signal.name)
            self._call("SimConnect_AddClientEventToNotificationGroup", self._group, event_id, 0)
        except ProviderError as error:
            self._fail(signal, str(error))
            return
        self._add_packets[signal] = self._track("add", signal)
        self._members[signal] = event_id
        self._by_id[event_id] = signal
        if len(self._members) == 1:
            try:
                self._call("SimConnect_SetNotificationGroupPriority", self._group, _DEFAULT_PRIORITY)
            except ProviderError as error:
                self._fail_group(str(error))
            else:
                self._track("priority")

    def _clear(self):
        try:
            self._call("SimConnect_ClearNotificationGroup", self._group)
        except ProviderError as error:
            self._cleanup_failed(f"ClearNotificationGroup failed: {error}")
        else:
            self._track("clear")
        self._members.clear()
        self._by_id.clear()
        self._add_packets.clear()
        self._generation += 1

    def _track(self, operation, signal=None):
        packet = self._last_packet()
        self._sent[packet] = operation, signal, self._generation
        return packet

    def _cleanup_failed(self, detail):
        """A remove or clear was refused. Keep the session; stop trusting the group.

        Events from the old group still arrive with its group ID, which no
        longer matches, so ``receive`` drops them. New members go to a fresh
        group on the next activation.
        """
        _LOG.warning("SimConnect client event cleanup was refused; using a fresh notification group. %s", detail)
        self._members.clear()
        self._by_id.clear()
        self._add_packets.clear()
        self._generation += 1
        self._group = None  # Wanted members are re-added to a new group after the next drain.

    def receive(self, kind, packet):
        if len(packet) < sizeof(ReceiveEventHeader):
            return False
        prefix = ReceiveEventHeader.from_buffer_copy(packet)
        signal = self._by_id.get(prefix.event_id)
        if signal is None:
            return False
        if prefix.group_id not in (self._group, _UNKNOWN_GROUP):
            return True
        packet_type = ReceiveEventEx1 if kind == ReceiveId.EVENT_EX1 else ReceiveEvent
        if len(packet) != sizeof(packet_type):
            self._reading(signal, None, error="Invalid SimConnect client event packet size.")
            return True
        event = packet_type.from_buffer_copy(packet)
        words = (event.data,)
        if kind == ReceiveId.EVENT_EX1:
            words += tuple(event.remaining_words)
        self._reading(signal, ClientEventData(words))
        return True

    def mapping_failed(self, event_id, detail):
        signal = self._by_id.pop(event_id, None)
        if signal is None:
            return
        if self._members.get(signal) == event_id:
            del self._members[signal]
        # A later exception for the group add then matches nothing and is absorbed.
        self._add_packets.pop(signal, None)
        if signal not in self._failed:
            self._fail(signal, detail)

    def receive_exception(self, send_id, detail):
        item = self._sent.pop(send_id, None)
        if item is None:
            return False
        operation, signal, generation = item
        if operation in ("remove", "clear"):
            # The server may still deliver the unwanted event; the group is retired
            # so those packets are dropped, and the session continues.
            self._cleanup_failed(detail)
            return True
        if generation == self._generation:
            if operation == "priority":
                self._fail_group(detail)
            elif operation == "dialog":
                for signal in self._by_id.values():
                    self._reading(signal, None, error=detail)
            elif self._add_packets.get(signal) == send_id:
                del self._add_packets[signal]
                self._by_id.pop(self._members.pop(signal), None)
                if signal not in self._failed:
                    self._fail(signal, detail)
        return True

    def _fail(self, signal, detail):
        self._failed.add(signal)
        self._reading(signal, None, error=detail)

    def _fail_group(self, detail):
        affected = tuple(self._members)
        if affected:
            self._clear()
        for signal in affected:
            self._fail(signal, detail)
