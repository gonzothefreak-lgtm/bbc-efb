"""Shared, non-masking SimConnect client event reception."""

from ctypes import sizeof

from .contract import ClientEventData
from .provider import ProviderError, Signal
from .simconnect_abi import ReceiveEvent, ReceiveEventEx1, ReceiveEventHeader, ReceiveId


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
        self._sent: dict[int, tuple[str, Signal | None, int]] = {}

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
                    self._call("SimConnect_RemoveClientEvent", self._group, event_id)
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
        self._call("SimConnect_ClearNotificationGroup", self._group)
        self._track("clear")
        self._members.clear()
        self._by_id.clear()
        self._add_packets.clear()
        self._generation += 1

    def _track(self, operation, signal=None):
        packet = self._last_packet()
        self._sent[packet] = operation, signal, self._generation
        if len(self._sent) > 4096:
            del self._sent[next(iter(self._sent))]
        return packet

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
        if signal is not None:
            self._fail(signal, detail)

    def receive_exception(self, send_id, detail):
        item = self._sent.pop(send_id, None)
        if item is None:
            return False
        operation, signal, generation = item
        if operation in ("remove", "clear"):
            # The server may still be delivering an unwanted subscription.
            raise ProviderError(detail)
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
