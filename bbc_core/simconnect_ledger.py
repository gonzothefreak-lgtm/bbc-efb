"""Age-pruned record of sent SimConnect packets and who owns them.

SimConnect reports a rejected request asynchronously, naming only the packet ID
returned by ``SimConnect_GetLastSentPacketID``. The exception can arrive late,
for example while the simulator is loading. A ledger keeps each packet's owner
for a fixed time window, so a late exception is still attributed. Entries are
pruned by age, not by count, so heavy traffic cannot push out a recent packet.
A large hard limit only protects memory if a caller sends without pause.
"""

from __future__ import annotations

from collections import OrderedDict
from time import monotonic


DEFAULT_WINDOW_S = 300.0
_HARD_LIMIT = 262_144


class PacketLedger:
    """A dict-like map of packet ID to owner, pruned by age on every write."""

    __slots__ = ("_entries", "_window")

    def __init__(self, window_s: float = DEFAULT_WINDOW_S):
        if not window_s > 0:
            raise ValueError("The packet ledger window must be positive.")
        self._window = window_s
        self._entries: OrderedDict[int, tuple[float, object]] = OrderedDict()

    def __setitem__(self, packet, owner):
        now = monotonic()
        self._entries.pop(packet, None)
        self._entries[packet] = (now, owner)
        self._prune(now)

    def __contains__(self, packet):
        return packet in self._entries

    def __len__(self):
        return len(self._entries)

    def get(self, packet, default=None):
        entry = self._entries.get(packet)
        return default if entry is None else entry[1]

    def pop(self, packet, default=None):
        entry = self._entries.pop(packet, None)
        return default if entry is None else entry[1]

    def values(self):
        return tuple(owner for _, owner in self._entries.values())

    def clear(self):
        self._entries.clear()

    def _prune(self, now):
        cutoff = now - self._window
        entries = self._entries
        while entries:
            packet, (sent_at, _) = next(iter(entries.items()))
            if sent_at >= cutoff and len(entries) <= _HARD_LIMIT:
                break
            del entries[packet]
