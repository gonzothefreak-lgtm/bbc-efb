"""Shared helpers for the Core regression tests. Contains no tests of its own."""

from datetime import datetime, timedelta, timezone

from bbc_core.facilities import FacilityState
from bbc_core.provider import FacilityReply
from bbc_core.simulated import SimulatedProvider


class FakeClock:
    """A monotonic clock and its matching UTC wall clock, advanced explicitly."""

    def __init__(self, start=1000.0, wall=datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)):
        self.now = float(start)
        self._start = float(start)
        self._wall = wall

    def __call__(self):
        return self.now

    def wall(self):
        return self._wall + timedelta(seconds=self.now - self._start)

    def advance(self, seconds):
        self.now += seconds
        return self.now


class FacilitySimulatedProvider(SimulatedProvider):
    """A simulated provider that also accepts facility reads and answers them on request."""

    def __init__(self, **options):
        super().__init__(**options)
        self.facility_requests = []

    def request_facility(self, id, query):
        self.facility_requests.append((id, query))

    def answer_facility(self, index=0, state=FacilityState.READY, data=None):
        id, query = self.facility_requests[index]
        self._offer(FacilityReply(self._session_id, id, query, state, data))


class InputSimulatedProvider(SimulatedProvider):
    """A simulated provider that lets a test publish aircraft input catalogs directly."""

    def __init__(self, **options):
        super().__init__(**options)
        self.catalog_requests = 0

    def supports_signal(self, signal):
        return signal.input_id is not None or super().supports_signal(signal)

    def replace_demand(self, demand):
        super().replace_demand(tuple(item for item in demand if item.signal.input_id is None))
        self.active_demand = demand

    def request_input_catalog(self):
        self.catalog_requests += 1

    def request_input_parameters(self, input_id):
        pass

    def request_input_value(self, input_id):
        pass

    def offer(self, update):
        self._offer(update)


def drive(core, clock, seconds, step=0.1, consumers=()):
    """Advance ``clock`` in ``step`` increments, pumping Core and collecting each consumer's updates."""
    collected = {id(consumer): [] for consumer in consumers}
    steps = round(seconds / step)
    for _ in range(steps):
        clock.advance(step)
        core.pump()
        for consumer in consumers:
            collected[id(consumer)].extend(consumer.poll(limit=4096))
    return [collected[id(consumer)] for consumer in consumers]
