"""A reader (in-process thread and a separate process) while the writer is appending."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bbc_recorder.journal import IssueKind, Journal, JournalLayout, JournalLockedError, is_locked, read_journal
from bbc_recorder.model import EventKind, Phase, RecorderEvent
from bbc_recorder.recovery import find_unfinished, finalize_interrupted

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]

# The guarded offline runner (tests/test_repo_offline_guard.py) blocks subprocesses and sets
# BBC_OFFLINE_GUARD=1; these cross-process checks then skip and still run under plain unittest.
_GUARDED = os.environ.get("BBC_OFFLINE_GUARD") == "1"
_SKIP_REASON = "starts a subprocess, which the offline guard blocks"


def event(i: int) -> RecorderEvent:
    return RecorderEvent(EventKind.SAMPLE, Phase.AIRBORNE, T0 + timedelta(seconds=i), float(i),
                         None, {"i": i, "pad": "x" * (i % 300)})


class ConcurrentReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.layout = JournalLayout(Path(self._tmp.name))

    def test_reader_thread_sees_consistent_prefixes(self) -> None:
        j = Journal.create(self.layout, "C1", app_version="x")
        self.addCleanup(j.abandon)
        total = 3000
        stop = threading.Event()
        failures: list[str] = []
        reads = {"n": 0, "torn": 0}

        def reader() -> None:
            last = 0
            while not stop.is_set():
                r = read_journal(j.path, keep_events=True)
                reads["n"] += 1
                ids = [e.data["i"] for e in r.events]
                if ids != list(range(len(ids))):
                    failures.append(f"non-prefix read: {ids[:5]}…")
                if len(ids) < last:
                    failures.append("went backwards")
                last = len(ids)
                bad = [i for i in r.issues if i.kind is not IssueKind.TORN_TAIL]
                if bad:
                    failures.append(f"mid-file issue while writing: {bad[0]}")
                if r.torn_tail_bytes:
                    reads["torn"] += 1

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(total):
                j.append(event(i))
        finally:
            stop.set()
            thread.join()
        self.assertEqual(failures, [])
        self.assertGreater(reads["n"], 0)
        j.close()
        r = read_journal(j.path)
        self.assertTrue(r.clean)
        self.assertEqual(r.event_count, total)

    @unittest.skipIf(_GUARDED, _SKIP_REASON)
    def test_reader_in_another_process_and_lock_across_processes(self) -> None:
        j = Journal.create(self.layout, "C2", app_version="x")
        self.addCleanup(j.abandon)
        for i in range(50):
            j.append(event(i))
        script = (
            "import sys, json\n"
            "from bbc_recorder.journal import read_journal, is_locked, Journal, JournalLockedError\n"
            "from bbc_recorder.recovery import find_unfinished\n"
            "p, lock, data = sys.argv[1], sys.argv[2], sys.argv[3]\n"
            "r = read_journal(p)\n"
            "try:\n"
            "    Journal.resume(data, 'C2'); took = True\n"
            "except JournalLockedError:\n"
            "    took = False\n"
            "print(json.dumps({'n': r.event_count, 'locked': is_locked(lock), 'took': took,"
            " 'unfinished': len(find_unfinished(data)), 'in_use': len(find_unfinished(data, include_in_use=True))}))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", script, str(j.path), str(self.layout.lock_path("C2")), str(self.layout.data_dir)],
            capture_output=True, text=True, cwd=ROOT, timeout=60,
            env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual(report, {"n": 50, "locked": True, "took": False, "unfinished": 0, "in_use": 1})

    @unittest.skipIf(_GUARDED, _SKIP_REASON)
    def test_lock_released_when_writer_process_dies(self) -> None:
        script = (
            "import os, sys\n"
            "from bbc_recorder.journal import Journal\n"
            "from bbc_recorder.model import *\n"
            "from datetime import datetime, timezone\n"
            "j = Journal.create(sys.argv[1], 'C3', app_version='x')\n"
            "j.append(RecorderEvent(EventKind.SAMPLE, Phase.AIRBORNE, datetime.now(timezone.utc)))\n"
            "os._exit(9)  # simulated crash: no footer, no cleanup\n"
        )
        out = subprocess.run([sys.executable, "-c", script, str(self.layout.data_dir)],
                             capture_output=True, text=True, cwd=ROOT, timeout=60,
                             env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                                  "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
        self.assertEqual(out.returncode, 9, out.stderr)
        self.assertTrue(self.layout.lock_path("C3").exists())  # stale lock file left behind
        self.assertFalse(is_locked(self.layout.lock_path("C3")))  # but nobody holds it
        found = find_unfinished(self.layout)
        self.assertEqual([f.flight_id for f in found], ["C3"])
        self.assertEqual(found[0].event_count, 1)
        finalize_interrupted(found[0], data_dir=self.layout)
        self.assertFalse(self.layout.lock_path("C3").exists())

    def test_recovery_and_writer_race_in_process(self) -> None:
        j = Journal.create(self.layout, "C4", app_version="x")
        self.addCleanup(j.abandon)
        with self.assertRaises(JournalLockedError):
            finalize_interrupted("C4", data_dir=self.layout)
        j.append(event(0))  # writer unaffected
        self.assertEqual(read_journal(j.path).event_count, 1)


if __name__ == "__main__":
    unittest.main()
