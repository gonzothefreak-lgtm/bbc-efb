"""Power-loss simulation: the journal truncated (or zero-filled) at random byte offsets.

Property: whatever the cut, the reader never raises, returns exactly the events whose
lines were complete, reports any partial tail, and recovery then closes the flight as
interrupted without inventing an end time and without losing a surviving event.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bbc_recorder.journal import IssueKind, Journal, JournalLayout, read_journal
from bbc_recorder.model import EventKind, Phase, RecorderEvent
from bbc_recorder.recovery import RecoveryState, finalize_interrupted, find_unfinished

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
PHASES = [Phase.PREFLIGHT, Phase.TAXI_OUT, Phase.TAKEOFF_ROLL, Phase.AIRBORNE,
          Phase.LANDING_ROLL, Phase.TAXI_IN]


def flight_events(n: int) -> list[RecorderEvent]:
    out = []
    for i in range(n):
        phase = PHASES[min(i * len(PHASES) // n, len(PHASES) - 1)]
        kind = EventKind.PHASE_CHANGED if i and phase != out[-1].phase else EventKind.SAMPLE
        out.append(RecorderEvent(kind, phase, T0 + timedelta(seconds=i), 100.0 + i,
                                 T0 + timedelta(seconds=i),
                                 {"i": i, "lat": 51.47 + i / 1e4, "label": "Zürich ✈" if i % 7 == 0 else "EGLL"}))
    return out


class PowerLossTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name) / "source"
        layout = JournalLayout(base)
        cls.events = flight_events(120)
        j = Journal.create(layout, "P1", app_version="x", started_host_time=T0)
        for e in cls.events:
            j.append(e)
        j.abandon()
        cls.data = j.path.read_bytes()
        # byte offset at which each event line ends
        ends, pos = [], 0
        for raw in cls.data.splitlines(keepends=True):
            pos += len(raw)
            ends.append(pos)
        cls.header_end = ends[0]
        cls.event_ends = ends[1:]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def expected_count(self, cut: int) -> int:
        # a line counts if complete, or complete except for its final newline
        return sum(1 for end in self.event_ends if end <= cut + 1)

    def fresh(self, name: str, content: bytes) -> tuple[Path, Path]:
        root = Path(self._tmp.name) / name
        if root.exists():
            shutil.rmtree(root)
        layout = JournalLayout(root)
        layout.in_progress_dir.mkdir(parents=True)
        path = layout.journal_path("P1")
        path.write_bytes(content)
        return root, path

    def test_random_truncation_points(self) -> None:
        rng = random.Random(20261124)
        cuts = sorted({rng.randrange(0, len(self.data) + 1) for _ in range(400)}
                      | {0, 1, self.header_end - 1, self.header_end, len(self.data)}
                      | {e for e in self.event_ends[:5]} | {e - 1 for e in self.event_ends[:5]})
        for cut in cuts:
            with self.subTest(cut=cut):
                _, path = self.fresh("cut", self.data[:cut])
                r = read_journal(path)
                n = self.expected_count(cut)
                self.assertEqual(r.event_count, n)
                self.assertEqual(list(r.events), self.events[:n])
                at_boundary = cut in self.event_ends or cut == self.header_end or cut == 0 \
                    or cut + 1 in self.event_ends or cut + 1 == self.header_end
                if at_boundary:
                    self.assertEqual(r.torn_tail_bytes, 0)
                    self.assertTrue(r.clean or r.issues[0].kind is IssueKind.NO_HEADER)
                else:
                    self.assertGreater(r.torn_tail_bytes, 0)
                    self.assertEqual(r.issues[-1].kind, IssueKind.TORN_TAIL)
                self.assertEqual(r.good_bytes + r.torn_tail_bytes, cut)

    def test_zero_filled_tail_at_random_points(self) -> None:
        rng = random.Random(7)
        for _ in range(80):
            cut = rng.randrange(self.header_end, len(self.data))
            lost = rng.randrange(1, 600)
            with self.subTest(cut=cut, lost=lost):
                _, path = self.fresh("zero", self.data[:cut] + b"\x00" * lost)
                r = read_journal(path)
                n = sum(1 for end in self.event_ends if end <= cut)
                self.assertEqual(list(r.events), self.events[:n])
                self.assertFalse(r.closed)

    def test_recovery_after_random_truncation(self) -> None:
        rng = random.Random(42)
        now = T0 + timedelta(hours=3)
        for _ in range(40):
            cut = rng.randrange(self.header_end, len(self.data) + 1)
            with self.subTest(cut=cut):
                root, path = self.fresh("recover", self.data[:cut])
                found = find_unfinished(root)
                self.assertEqual(len(found), 1)
                flight = found[0]
                n = self.expected_count(cut)
                self.assertEqual(flight.event_count, n)
                self.assertEqual(flight.state, RecoveryState.INTERRUPTED if n else RecoveryState.EMPTY)
                if n:
                    self.assertEqual(flight.last_good_host_time, self.events[n - 1].host_time)
                result = finalize_interrupted(flight, data_dir=root, now=now)
                self.assertEqual(find_unfinished(root), [])
                archived = read_journal(result.archived_path)
                self.assertTrue(archived.closed)
                self.assertEqual(archived.footer["outcome"], "interrupted")
                self.assertIsNone(archived.footer["ended_host_time"])
                self.assertIsNone(archived.footer["ended_sim_time_s"])
                self.assertEqual(list(archived.events[:n]), self.events[:n])
                gap = archived.events[-1]
                self.assertEqual(gap.kind, EventKind.GAP)
                self.assertEqual(gap.data["to_host_time"], now.isoformat())
                self.assertIsNone(gap.data["to_sim_time_s"])
                self.assertEqual(result.record["ended"]["host_time"], None)
                # the original surviving bytes are untouched
                self.assertTrue(result.archived_path.read_bytes().startswith(self.data[:cut]))


if __name__ == "__main__":
    unittest.main()
