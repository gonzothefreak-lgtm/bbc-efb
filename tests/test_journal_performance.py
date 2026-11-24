"""A 20-hour flight at 1 Hz (72,000 samples): write, read and recover within budget,
with bounded memory while writing and while scanning."""

from __future__ import annotations

import os
import tempfile
import time
import tracemalloc
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bbc_recorder.journal import FsyncPolicy, Journal, JournalLayout, read_journal
from bbc_recorder.model import EventKind, Phase, RecorderEvent
from bbc_recorder.recovery import RecoveryState, finalize_interrupted, find_unfinished

T0 = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
SECONDS = 20 * 3600
# Generous budgets so slow CI machines pass; typical figures are a fraction of these.
WRITE_BUDGET_S = float(os.environ.get("BBC_JOURNAL_WRITE_BUDGET_S", "60"))
READ_BUDGET_S = float(os.environ.get("BBC_JOURNAL_READ_BUDGET_S", "45"))


def sample(i: int) -> RecorderEvent:
    phase = Phase.AIRBORNE if 600 <= i < SECONDS - 600 else Phase.TAXI_OUT
    return RecorderEvent(
        EventKind.SAMPLE, phase, T0 + timedelta(seconds=i), float(i), T0 + timedelta(seconds=i),
        {"lat": 51.4775 + i * 1e-5, "lon": -0.4614 + i * 1e-5, "alt_ft": 35000.0,
         "gs_kt": 452.3, "hdg": 271.0, "vs_fpm": 0.0, "on_ground": False},
    )


class LargeFlightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.layout = JournalLayout(Path(cls._tmp.name))
        clock = {"t": 0.0}
        tracemalloc.start()
        started = time.perf_counter()
        # Simulated 1 Hz clock so the 2 s fsync interval behaves as in a real flight.
        j = Journal.create(cls.layout, "LONG", app_version="x", started_host_time=T0,
                           policy=FsyncPolicy(interval_s=2.0), clock=lambda: clock["t"])
        baseline = tracemalloc.get_traced_memory()[0]
        for i in range(SECONDS):
            clock["t"] = float(i)
            j.append(sample(i))
        cls.write_s = time.perf_counter() - started
        cls.write_peak = tracemalloc.get_traced_memory()[1] - baseline
        tracemalloc.stop()
        cls.fsyncs = j.fsync_count
        j.abandon()
        cls.path = j.path
        cls.size = j.path.stat().st_size

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_write_is_fast_bounded_and_syncs_at_the_set_cadence(self) -> None:
        print(f"\n[journal perf] wrote {SECONDS} events, {self.size / 1e6:.1f} MB in {self.write_s:.2f} s; "
              f"fsyncs={self.fsyncs}; peak extra memory {self.write_peak / 1e3:.0f} kB")
        self.assertLess(self.write_s, WRITE_BUDGET_S)
        self.assertLess(self.write_peak, 2_000_000)  # the writer keeps no history
        # ~one sync per 2 s plus 2 phase changes and the header
        self.assertLessEqual(self.fsyncs, SECONDS // 2 + 4)
        self.assertGreaterEqual(self.fsyncs, SECONDS // 2 - 1)

    def test_full_read(self) -> None:
        started = time.perf_counter()
        r = read_journal(self.path)
        elapsed = time.perf_counter() - started
        print(f"\n[journal perf] full read {elapsed:.2f} s")
        self.assertEqual(r.event_count, SECONDS)
        self.assertTrue(r.clean)
        self.assertEqual(r.events[-1], sample(SECONDS - 1))
        self.assertLess(elapsed, READ_BUDGET_S)

    def test_scan_without_events_is_memory_bounded(self) -> None:
        tracemalloc.start()
        started = time.perf_counter()
        [flight] = find_unfinished(self.layout, load_events=False)
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        print(f"\n[journal perf] recovery scan {elapsed:.2f} s, peak {peak / 1e3:.0f} kB")
        self.assertEqual(flight.state, RecoveryState.INTERRUPTED)
        self.assertEqual(flight.event_count, SECONDS)
        self.assertEqual(flight.last_good_host_time, sample(SECONDS - 1).host_time)
        self.assertLess(peak, 10_000_000)
        self.assertLess(elapsed, READ_BUDGET_S)

    def test_zz_finalize_large_flight(self) -> None:
        started = time.perf_counter()
        result = finalize_interrupted("LONG", data_dir=self.layout,
                                      now=T0 + timedelta(seconds=SECONDS + 3600))
        elapsed = time.perf_counter() - started
        print(f"\n[journal perf] finalise {elapsed:.2f} s")
        self.assertEqual(result.record["event_count"], SECONDS + 1)
        self.assertLess(elapsed, READ_BUDGET_S * 2)


if __name__ == "__main__":
    unittest.main()
