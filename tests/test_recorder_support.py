"""Shared helpers for the Recorder tests: real Core + SimulatedProvider driven by flight tapes.

``CoreTapeRun`` plays a ``bbc_recorder.tape`` scenario through ``bbc_core.simulated`` and the
real ``Core`` on a fake clock, ticks a real ``RecorderService`` after every pump (as the Core
host does) and saves into a temporary ``FlightStore`` through ``bbc_efb.recorder_sink``.
Nothing here touches a network, a simulator or the real data folder.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bbc_core import Core
from bbc_core.facilities import AirportReference, FacilityState
from bbc_core.provider import FacilityReply
from bbc_core.sdk_simvar import SimVar
from bbc_core.simulated import SimulatedProvider
from bbc_efb.recorder_sink import LogbookSink
from bbc_efb.storage import FlightStore
from bbc_recorder.service import RecorderConfig, RecorderService
from bbc_recorder.tape import core_sessions


class FakeClock:
    """A monotonic clock and its matching UTC wall clock, advanced explicitly."""

    def __init__(self, start=1000.0, wall=datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)):
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


class TapeProvider(SimulatedProvider):
    """SimulatedProvider that answers nearby-airport queries from a fixed airport list."""

    def __init__(self, airports=(), *, answer=True, **options):
        super().__init__(**options)
        self.airports = tuple(airports)
        self.answer = answer
        self.facility_requests = []

    def set_session_absolute_time(self, absolute_time_s):
        self._initial_absolute = absolute_time_s

    def request_facility(self, id, query):
        self.facility_requests.append((id, query))
        if not self.answer:
            return
        data = tuple(AirportReference(item["icao"], item.get("region", ""), float(item["lat"]), float(item["lon"]),
                                      float(item.get("alt_m", 0.0))) for item in self.airports)
        self._offer(FacilityReply(self._session_id, id, query, FacilityState.READY, data))


class CoreTapeRun:
    """One scenario: Core + SimulatedProvider + RecorderService + FlightStore in a temp folder."""

    def __init__(self, tape, *, frame_rate_hz=10.0, config=None, data_dir=None, store=True, answer_airports=True,
                 sink=None, rules=None):
        self.tape = tape
        self.temp = None
        if data_dir is None:
            self.temp = tempfile.mkdtemp(prefix="bbc-recorder-test-")
            data_dir = Path(self.temp)
        self.data_dir = Path(data_dir)
        self.clock = FakeClock(wall=tape.header.start_host_utc)
        self.provider = TapeProvider(tape.header.airports, answer=answer_airports, clock=self.clock,
                                     wall_clock=self.clock.wall, frame_rate_hz=frame_rate_hz)
        self.core = Core(self.provider)
        self.api = self.core.register_consumer(queue_size=4096)
        self.store = FlightStore(self.data_dir, create=True) if store else None
        self.sink = sink if sink is not None else LogbookSink(lambda: self.store)
        self.service = RecorderService(self.api, self.sink, self.data_dir, rules, self.clock.wall,
                                       monotonic=self.clock,
                                       config=config or RecorderConfig(background_saves=False))
        self.sessions = core_sessions(tape)
        self.t = 0.0

    def close(self):
        try:
            self.service.close()
        finally:
            try:
                self.core.disconnect()
            except Exception:  # noqa: BLE001
                pass
            if self.store is not None:
                self.store.close()
            if self.temp is not None:
                shutil.rmtree(self.temp, ignore_errors=True)

    def run(self, *, until=None, step=0.1, settle_s=20.0, hooks=None):
        """Play the tape until ``until`` (tape seconds; default: the end plus ``settle_s``)."""
        end = (self.tape.duration_s + settle_s) if until is None else until
        hooks = sorted((hooks or {}).items())
        pending = list(self.sessions)
        while self.t < end - 1e-9:
            if pending and pending[0].start_t <= self.t + 1e-9:
                session = pending.pop(0)
                self.provider.set_session_absolute_time(session.absolute_time_s)
                # The simulator already shows the aircraft when the session opens: start from the
                # tape's first values, not the provider's defaults.
                first = next((entry.at_s for entry in session.tape.entries if isinstance(entry.target, SimVar)), 0.0)
                for entry in session.tape.entries:
                    if entry.at_s > first:
                        break
                    if isinstance(entry.target, SimVar):
                        self.provider.set_value(entry.target, entry.value)
                self.core.connect()
                if session.rate != 1.0:
                    self.provider.set_simulation_rate(session.rate)
                self.provider.play(session.tape, start=self.clock())
            self.clock.advance(step)
            self.t += step
            self.core.pump()
            self.service.tick()
            while hooks and hooks[0][0] <= self.t + 1e-9:
                hooks.pop(0)[1]()
        return self

    def flights(self):
        if self.store is None:
            return []
        flights, _unreadable = self.store.listing()
        return flights
