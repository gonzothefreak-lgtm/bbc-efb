"""Journal writer: header, append, fsync policy, footer, locks, failure handling, archive."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bbc_recorder import journal as jmod
from bbc_recorder.journal import (
    FsyncPolicy, IssueKind, Journal, JournalClosedError, JournalError, JournalLayout,
    JournalLockedError, JournalWriteError, Outcome, archive_journal, compress_archived,
    find_archived, is_locked, list_archived, new_flight_id, read_journal, validate_flight_id,
)
from bbc_recorder.model import EventKind, Phase, RecorderEvent

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def event(i: int, kind: EventKind = EventKind.SAMPLE, phase: Phase = Phase.PREFLIGHT,
          **data: object) -> RecorderEvent:
    return RecorderEvent(kind, phase, T0 + timedelta(seconds=i), float(i),
                         T0 + timedelta(seconds=i), {"i": i, **data})


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class WriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.layout = JournalLayout(self.data)

    def create(self, flight_id: str = "F1", **kw: object) -> Journal:
        kw.setdefault("app_version", "0.1.0a1")
        kw.setdefault("now", lambda: T0)
        journal = Journal.create(self.layout, flight_id, **kw)  # type: ignore[arg-type]
        self.addCleanup(journal.abandon)
        return journal

    def test_header_line_and_location(self) -> None:
        j = self.create(started_host_time=T0, started_sim_time_s=12.5, aircraft={"title": "C172"},
                        source={"name": "simulated", "simulated": True}, rules_version="r1",
                        extra={"note": "x"})
        self.assertEqual(j.path, self.data / "recorder" / "in-progress" / "F1.jsonl")
        self.assertTrue(j.path.exists())
        self.assertTrue(self.layout.lock_path("F1").exists())
        first = j.path.read_bytes().split(b"\n")[0]
        header = json.loads(first)
        self.assertEqual(header["type"], "header")
        self.assertEqual(header["seq"], 0)
        self.assertEqual(header["schema"], jmod.JOURNAL_SCHEMA)
        self.assertEqual(header["app_version"], "0.1.0a1")
        self.assertEqual(header["started_host_time"], T0.isoformat())
        self.assertEqual(header["started_sim_time_s"], 12.5)
        self.assertIsNone(header["started_sim_utc"])  # unknown stays unknown
        self.assertEqual(header["aircraft"], {"title": "C172"})
        self.assertEqual(header["source"]["simulated"], True)
        self.assertEqual(header["extra"], {"note": "x"})
        self.assertTrue(first.endswith(b'"}'))
        self.assertRegex(first.decode(), r',"crc":"[0-9a-f]{8}"\}$')
        self.assertEqual(j.fsync_count, 1)  # header is synced

    def test_unknown_header_values_are_null_not_invented(self) -> None:
        j = self.create()
        header = read_journal(j.path).header
        assert header is not None
        for key in ("started_host_time", "started_sim_time_s", "started_sim_utc", "aircraft",
                    "source", "rules_version"):
            self.assertIsNone(header[key], key)

    def test_each_line_reaches_os_immediately(self) -> None:
        j = self.create()
        for i in range(3):
            seq = j.append(event(i))
            self.assertEqual(seq, i + 1)
            # a separate reader sees it straight away (no user-space buffering)
            result = read_journal(j.path)
            self.assertEqual(result.event_count, i + 1)
            self.assertEqual(result.events[-1], event(i))
        self.assertEqual(j.event_count, 3)
        self.assertEqual(j.last_seq, 3)

    def test_events_round_trip_exactly(self) -> None:
        j = self.create()
        originals = [event(i, phase=Phase.TAXI_OUT, name="Zürich ✈ 東京", nested={"a": [1, 2.5, None]})
                     for i in range(10)]
        for e in originals:
            j.append(e)
        j.close(ended_host_time=T0 + timedelta(hours=1), final_phase=Phase.COMPLETE)
        result = read_journal(j.path)
        self.assertEqual(list(result.events), originals)
        self.assertTrue(result.clean)
        self.assertTrue(result.closed)
        self.assertEqual(result.footer["outcome"], "complete")
        self.assertEqual(result.footer["event_count"], 10)
        self.assertEqual(result.footer["final_phase"], "complete")

    def test_fsync_interval_policy(self) -> None:
        clock = FakeClock()
        j = self.create(policy=FsyncPolicy(interval_s=2.0), clock=clock)
        base = j.fsync_count
        j.append(event(0))
        self.assertEqual(j.fsync_count, base)
        self.assertTrue(j.unsynced)
        clock.t += 1.0
        j.append(event(1))
        self.assertEqual(j.fsync_count, base)
        clock.t += 1.0
        j.append(event(2))  # 2 s since last sync
        self.assertEqual(j.fsync_count, base + 1)
        self.assertFalse(j.unsynced)
        clock.t += 0.5
        j.append(event(3))
        self.assertFalse(j.tick())  # interval not reached
        clock.t += 1.6
        self.assertTrue(j.tick())
        self.assertFalse(j.tick())  # nothing pending
        self.assertEqual(j.fsync_count, base + 2)

    def test_fsync_on_phase_change_and_important_kinds(self) -> None:
        clock = FakeClock()
        j = self.create(policy=FsyncPolicy(interval_s=None), clock=clock)
        base = j.fsync_count
        j.append(event(0, phase=Phase.PREFLIGHT))
        j.append(event(1, phase=Phase.PREFLIGHT))
        self.assertEqual(j.fsync_count, base)
        j.append(event(2, phase=Phase.TAXI_OUT))  # phase change
        self.assertEqual(j.fsync_count, base + 1)
        j.append(event(3, EventKind.TOUCHDOWN, phase=Phase.TAXI_OUT))
        self.assertEqual(j.fsync_count, base + 2)
        j.append(event(4, phase=Phase.TAXI_OUT))
        self.assertEqual(j.fsync_count, base + 2)
        j.close()
        self.assertEqual(j.fsync_count, base + 3)

    def test_real_fsync_calls_are_counted(self) -> None:
        real = os.fsync
        calls: list[int] = []
        with mock.patch.object(jmod.os, "fsync", side_effect=lambda fd: (calls.append(fd), real(fd))):
            j = self.create(policy=FsyncPolicy(interval_s=None))
            j.append(event(0))
            j.append(event(1, EventKind.PHASE_CHANGED, Phase.TAXI_OUT))
            j.close()
        # header, phase change, close; plus the new directory entry on POSIX
        self.assertEqual(len(calls), 3 + (0 if jmod._IS_WINDOWS else 1))

    def test_close_releases_lock_and_refuses_more(self) -> None:
        j = self.create()
        lock = self.layout.lock_path("F1")
        self.assertTrue(is_locked(lock))
        j.append(event(0))
        path = j.close(Outcome.ABANDONED, detail="sim quit")
        self.assertEqual(path, j.path)
        self.assertTrue(j.closed)
        self.assertFalse(lock.exists())
        self.assertFalse(is_locked(lock))
        with self.assertRaises(JournalClosedError):
            j.append(event(1))
        with self.assertRaises(JournalClosedError):
            j.close()
        footer = read_journal(path).footer
        self.assertEqual(footer["outcome"], "abandoned")
        self.assertEqual(footer["detail"], "sim quit")
        self.assertIsNone(footer["ended_host_time"])  # not supplied, so not invented

    def test_context_manager_never_writes_footer(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.create() as j:
                j.append(event(0))
                raise RuntimeError("boom")
        result = read_journal(j.path)
        self.assertIsNone(result.footer)
        self.assertEqual(result.event_count, 1)
        self.assertFalse(is_locked(self.layout.lock_path("F1")))

    def test_second_writer_is_refused(self) -> None:
        j = self.create()
        with self.assertRaises(JournalLockedError):
            Journal.create(self.layout, "F1", app_version="x")
        with self.assertRaises(JournalLockedError):
            Journal.resume(self.layout, "F1")
        j.abandon()
        with self.assertRaises(JournalError):  # exists: never overwritten
            Journal.create(self.layout, "F1", app_version="x")
        self.assertEqual(read_journal(j.path).header["app_version"], "0.1.0a1")

    def test_flight_id_validation(self) -> None:
        for bad in ("", "../x", "a/b", "con", "NUL", "a b", "x" * 81, "-lead", "ä"):
            with self.assertRaises(JournalError, msg=bad):
                validate_flight_id(bad)
        validate_flight_id("20261124T101500Z-abc_1")
        fid = new_flight_id(T0)
        self.assertTrue(fid.startswith("20261124T100000Z-"))
        validate_flight_id(fid)
        self.assertNotEqual(new_flight_id(T0), fid)

    def test_non_finite_numbers_are_stored_as_unknown(self) -> None:
        j = self.create()
        j.append(event(0, alt=float("nan"), vs=float("inf"), ok=1.5))
        line = j.path.read_bytes().split(b"\n")[1]
        json.loads(line)  # strict JSON
        self.assertIn(b'"sanitized"', line)
        e = read_journal(j.path).events[0]
        self.assertIsNone(e.data["alt"])
        self.assertIsNone(e.data["vs"])
        self.assertEqual(e.data["ok"], 1.5)

    def test_lone_surrogate_and_odd_types(self) -> None:
        j = self.create()
        j.append(event(0, text="bad\udc80", when=T0, tags={"b", "a"}, path=Path("x/y"),
                       enum_value=EventKind.TAKEOFF, raw=b"\x01\x02"))
        e = read_journal(j.path).events[0]
        self.assertEqual(e.data["text"], "bad\udc80")
        self.assertEqual(e.data["when"], T0.isoformat())
        self.assertEqual(e.data["tags"], ["a", "b"])
        self.assertEqual(e.data["path"], "x/y")
        self.assertEqual(e.data["enum_value"], "takeoff")
        self.assertEqual(e.data["raw"], "0102")
        with self.assertRaises(TypeError):
            j.append(event(1, obj=object()))
        self.assertEqual(j.event_count, 1)  # the refused event consumed nothing
        j.append(event(2))
        self.assertTrue(read_journal(j.path).clean)

    def test_oversized_line_is_refused(self) -> None:
        j = self.create()
        with self.assertRaises(JournalError):
            j.append(event(0, blob="x" * (jmod.MAX_LINE_BYTES + 10)))
        self.assertEqual(j.event_count, 0)
        with self.assertRaises(TypeError):
            j.append({"kind": "sample"})  # type: ignore[arg-type]

    def test_partial_write_failure_is_contained_to_one_line(self) -> None:
        j = self.create()
        j.append(event(0))
        real_write = os.write
        state = {"n": 0}

        def flaky(fd: int, data: bytes) -> int:
            state["n"] += 1
            if state["n"] == 1:
                return real_write(fd, bytes(data[:20]))  # short write
            raise OSError(28, "No space left on device")

        with mock.patch.object(jmod.os, "write", side_effect=flaky):
            with self.assertRaises(JournalWriteError) as caught:
                j.append(event(1))
        self.assertIn("could not be written", str(caught.exception))
        self.assertEqual(j.event_count, 1)
        j.append(event(1))  # retried after the disk recovered
        j.append(event(2))
        j.close()
        result = read_journal(j.path)
        self.assertEqual([e.data["i"] for e in result.events], [0, 1, 2])
        self.assertEqual(result.issue_count, 1)
        self.assertIn(result.issues[0].kind, (IssueKind.CHECKSUM, IssueKind.BAD_UTF8))
        self.assertTrue(result.closed)

    def test_failed_write_with_no_bytes_leaves_no_trace(self) -> None:
        j = self.create()
        with mock.patch.object(jmod.os, "write", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(JournalWriteError):
                j.append(event(0))
        j.append(event(0))
        j.close()
        self.assertTrue(read_journal(j.path).clean)

    def test_fsync_failure_is_reported(self) -> None:
        j = self.create(policy=FsyncPolicy(interval_s=None))
        with mock.patch.object(jmod.os, "fsync", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(JournalWriteError):
                j.append(event(0, EventKind.TAKEOFF))
        self.assertEqual(read_journal(j.path).event_count, 1)  # the line itself was written

    def test_resume_after_torn_tail_appends_on_a_new_line(self) -> None:
        j = self.create()
        for i in range(3):
            j.append(event(i))
        j.abandon()
        data = j.path.read_bytes()
        j.path.write_bytes(data[:-15])  # tear the last line
        r = Journal.resume(self.layout, "F1")
        self.addCleanup(r.abandon)
        self.assertEqual(r.last_seq, 2)  # torn seq 3 is not counted
        self.assertEqual(r.event_count, 2)
        self.assertEqual(r.last_event, event(1))
        r.append(event(3))
        r.close()
        result = read_journal(j.path)
        self.assertEqual([e.data["i"] for e in result.events], [0, 1, 3])
        self.assertEqual(result.issue_count, 1)
        self.assertEqual(result.issues[0].kind, IssueKind.CHECKSUM)
        self.assertTrue(j.path.read_bytes().startswith(data[:-15]))  # never rewritten
        with self.assertRaises(JournalClosedError):
            Journal.resume(self.layout, "F1")

    def test_threaded_appends_keep_lines_whole(self) -> None:
        import threading
        j = self.create()
        def worker(base: int) -> None:
            for i in range(200):
                j.append(event(base + i))
        threads = [threading.Thread(target=worker, args=(k * 1000,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        j.close()
        result = read_journal(j.path)
        self.assertTrue(result.clean)
        self.assertEqual(result.event_count, 800)


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.layout = JournalLayout(Path(self._tmp.name))

    def closed(self, flight_id: str, start: datetime = T0) -> Path:
        j = Journal.create(self.layout, flight_id, app_version="x", started_host_time=start)
        j.append(event(0))
        return j.close()

    def test_archive_moves_closed_journal_by_month(self) -> None:
        self.closed("A1")
        target = archive_journal(self.layout, "A1")
        self.assertEqual(target, self.layout.archive_dir / "2026-11" / "A1.jsonl")
        self.assertFalse(self.layout.journal_path("A1").exists())
        self.assertEqual(read_journal(target).event_count, 1)
        self.assertEqual(find_archived(self.layout, "A1"), target)
        self.assertEqual(list_archived(self.layout), [target])

    def test_archive_refuses_open_or_unfinished(self) -> None:
        j = Journal.create(self.layout, "A2", app_version="x")
        with self.assertRaises(JournalLockedError):  # no rename while open
            archive_journal(self.layout, "A2")
        j.abandon()
        with self.assertRaises(JournalError):
            archive_journal(self.layout, "A2")
        self.assertTrue(self.layout.journal_path("A2").exists())

    def test_archive_never_overwrites(self) -> None:
        self.closed("A3")
        first = archive_journal(self.layout, "A3")
        # same id again with different content (e.g. restored backup)
        self.closed("A3", start=T0 + timedelta(days=1))
        second = archive_journal(self.layout, "A3")
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, "A3-2.jsonl")
        self.assertTrue(first.exists() and second.exists())

    def test_identical_copy_is_not_duplicated(self) -> None:
        path = self.closed("A4")
        content = path.read_bytes()
        target = archive_journal(self.layout, "A4")
        path.write_bytes(content)  # same file reappears (e.g. crash after copy)
        self.assertEqual(archive_journal(self.layout, "A4"), target)
        self.assertFalse(path.exists())
        self.assertEqual(len(list_archived(self.layout)), 1)

    def test_compress_archived_verifies_and_reads(self) -> None:
        self.closed("A5")
        target = archive_journal(self.layout, "A5")
        original = target.read_bytes()
        self.assertEqual(compress_archived(self.layout, older_than_days=1), [])  # too new
        future = datetime.now(timezone.utc) + timedelta(days=10)
        done = compress_archived(self.layout, older_than_days=1, now=future)
        self.assertEqual(done, [target.with_name("A5.jsonl.gz")])
        self.assertFalse(target.exists())
        with gzip.open(done[0], "rb") as f:
            self.assertEqual(f.read(), original)
        result = read_journal(done[0])
        self.assertTrue(result.compressed and result.closed and result.clean)
        self.assertEqual(find_archived(self.layout, "A5"), done[0])

    def test_move_retries_transient_permission_error(self) -> None:
        self.closed("A6")
        real = os.replace
        calls = {"n": 0}

        def flaky(a: object, b: object) -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(13, "sharing violation")
            real(a, b)

        with mock.patch.object(jmod.os, "replace", side_effect=flaky):
            target = archive_journal(self.layout, "A6")
        self.assertTrue(target.exists())
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
