"""Tolerant journal reader: torn lines, partial UTF-8, zero fill, damage, missing lines."""

from __future__ import annotations

import json
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bbc_recorder import journal as jmod
from bbc_recorder.journal import (
    IssueKind, Journal, JournalLayout, iter_journal_events, read_journal,
)
from bbc_recorder.model import EventKind, Phase, RecorderEvent

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def event(i: int, **data: object) -> RecorderEvent:
    return RecorderEvent(EventKind.SAMPLE, Phase.AIRBORNE, T0 + timedelta(seconds=i), float(i),
                         None, {"i": i, **data})


def line(obj: dict) -> bytes:
    return jmod._encode_line(obj)


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.layout = JournalLayout(Path(self._tmp.name))

    def write(self, n: int = 5, close: bool = False, **data: object) -> Path:
        j = Journal.create(self.layout, "R1", app_version="x", started_host_time=T0)
        for i in range(n):
            j.append(event(i, **data))
        if close:
            j.close()
        else:
            j.abandon()
        return j.path

    def lines(self, path: Path) -> list[bytes]:
        return path.read_bytes().splitlines(keepends=True)

    def test_clean_journal(self) -> None:
        path = self.write(5, close=True)
        r = read_journal(path)
        self.assertTrue(r.clean)
        self.assertTrue(r.closed)
        self.assertEqual(r.event_count, 5)
        self.assertEqual(r.last_seq, 6)
        self.assertEqual(r.good_bytes, r.size_bytes)
        self.assertEqual(r.first_event, event(0))
        self.assertEqual(r.last_event, event(4))
        self.assertEqual(r.counts_by_kind, {"sample": 5})
        self.assertEqual(r.notable_events, ())
        self.assertIn("5 events, closed", r.summary())

    def test_torn_final_line_is_skipped_and_reported(self) -> None:
        path = self.write(5)
        data = path.read_bytes()
        last_len = len(self.lines(path)[-1])
        for cut in (2, 5, last_len // 2, last_len - 2):
            path.write_bytes(data[:-cut])
            r = read_journal(path)
            self.assertEqual(r.event_count, 4, cut)
            self.assertEqual(r.torn_tail_bytes, last_len - cut, cut)
            self.assertEqual(r.good_bytes, len(data) - last_len)
            self.assertFalse(r.ends_with_newline)
            self.assertEqual([i.kind for i in r.issues], [IssueKind.TORN_TAIL])
            self.assertIn("incomplete", r.summary())

    def test_final_line_missing_only_newline_is_accepted(self) -> None:
        path = self.write(3)
        path.write_bytes(path.read_bytes()[:-1])
        r = read_journal(path)
        self.assertEqual(r.event_count, 3)
        self.assertTrue(r.clean)
        self.assertFalse(r.ends_with_newline)

    def test_partial_utf8_in_final_line(self) -> None:
        path = self.write(3, place="Zürich ✈ 東京")
        data = path.read_bytes()
        last = self.lines(path)[-1]
        start = len(data) - len(last)
        # cut inside each multi-byte character of the last line
        cuts = [k for k in range(len(last)) if 0x80 <= last[k] < 0xC0]
        self.assertTrue(cuts)
        for k in cuts:
            path.write_bytes(data[: start + k])
            r = read_journal(path)
            self.assertEqual(r.event_count, 2)
            self.assertEqual(r.issues[0].kind, IssueKind.TORN_TAIL)
            self.assertIn("UTF-8", r.issues[0].detail)
        path.write_bytes(data)
        self.assertEqual(read_journal(path).events[2].data["place"], "Zürich ✈ 東京")

    def test_invalid_utf8_mid_file(self) -> None:
        path = self.write(3)
        ls = self.lines(path)
        ls.insert(2, b"\xff\xfe garbage\n")
        path.write_bytes(b"".join(ls))
        r = read_journal(path)
        self.assertEqual(r.event_count, 3)
        self.assertEqual([i.kind for i in r.issues], [IssueKind.BAD_UTF8])
        self.assertEqual(r.issues[0].line, 3)

    def test_zero_filled_tail_after_power_cut(self) -> None:
        path = self.write(4)
        data = path.read_bytes()
        last = len(self.lines(path)[-1])
        path.write_bytes(data[:-last] + b"\x00" * last)  # NTFS: size updated, data lost
        r = read_journal(path)
        self.assertEqual(r.event_count, 3)
        self.assertEqual(r.issues[0].kind, IssueKind.TORN_TAIL)
        self.assertIn("zero bytes", r.issues[0].detail)
        # zero run followed by newline and good data (after a resumed write)
        path.write_bytes(data[:-last] + b"\x00" * 40 + b"\n" + data[-last:])
        r = read_journal(path)
        self.assertEqual(r.event_count, 4)
        self.assertEqual([i.kind for i in r.issues], [IssueKind.ZERO_FILL])

    def test_damaged_middle_line_detected_by_checksum(self) -> None:
        path = self.write(5)
        ls = self.lines(path)
        target = bytearray(ls[3])
        pos = target.index(b'"i":') + 4
        target[pos] = ord("7") if target[pos] != ord("7") else ord("8")  # still valid JSON
        json.loads(bytes(target))
        ls[3] = bytes(target)
        path.write_bytes(b"".join(ls))
        r = read_journal(path)
        self.assertEqual(r.event_count, 4)
        self.assertEqual([i.kind for i in r.issues], [IssueKind.CHECKSUM, IssueKind.MISSING_LINES])
        self.assertNotIn(7, [e.data["i"] for e in r.events])

    def test_hand_edited_line_without_checksum(self) -> None:
        path = self.write(2)
        ls = self.lines(path)
        ls.append(b'{"seq":3,"type":"event","event":{}}\n')
        path.write_bytes(b"".join(ls))
        r = read_journal(path)
        self.assertEqual(r.event_count, 2)
        self.assertEqual(r.issues[0].kind, IssueKind.CHECKSUM)

    def test_missing_lines_reported(self) -> None:
        path = self.write(6)
        ls = self.lines(path)
        del ls[2:4]
        path.write_bytes(b"".join(ls))
        r = read_journal(path)
        self.assertEqual(r.event_count, 4)
        self.assertEqual([i.kind for i in r.issues], [IssueKind.MISSING_LINES])
        self.assertIn("2 lines are missing", r.issues[0].detail)

    def test_duplicate_and_out_of_order(self) -> None:
        path = self.write(3)
        ls = self.lines(path)
        ls.append(ls[2])
        path.write_bytes(b"".join(ls))
        r = read_journal(path)
        self.assertEqual(r.event_count, 3)
        self.assertEqual([i.kind for i in r.issues], [IssueKind.OUT_OF_ORDER])

    def test_blank_and_crlf_lines_are_harmless(self) -> None:
        path = self.write(3)
        ls = self.lines(path)
        ls = [ls[0], b"\n", ls[1].replace(b"\n", b"\r\n"), b"   \n", ls[2], ls[3]]
        path.write_bytes(b"".join(ls))
        r = read_journal(path)
        self.assertEqual(r.event_count, 3)
        self.assertTrue(r.clean)

    def test_too_long_line(self) -> None:
        path = self.write(2)
        ls = self.lines(path)
        ls.insert(2, b"x" * 5000 + b"\n")
        path.write_bytes(b"".join(ls))
        r = read_journal(path, line_limit=1024)
        self.assertEqual(r.event_count, 2)
        self.assertEqual([i.kind for i in r.issues], [IssueKind.TOO_LONG])
        path.write_bytes(b"".join(ls[:2]) + b"y" * 5000)  # overlong and unterminated
        r = read_journal(path, line_limit=1024)
        self.assertEqual(r.event_count, 1)
        self.assertEqual(r.torn_tail_bytes, 5000)

    def test_data_after_footer_and_unknown_type(self) -> None:
        path = self.write(2, close=True)
        with open(path, "ab") as f:
            f.write(line({"seq": 99, "type": "event", "event": event(9).to_json()}))
        r = read_journal(path)
        self.assertEqual(r.event_count, 2)
        self.assertEqual([i.kind for i in r.issues], [IssueKind.AFTER_FOOTER])

        path = self.write_unknown()
        r = read_journal(path)
        self.assertEqual(r.event_count, 2)
        self.assertEqual([i.kind for i in r.issues], [IssueKind.UNKNOWN_TYPE])

    def write_unknown(self) -> Path:
        path = self.layout.in_progress_dir / "U1.jsonl"
        header = {"seq": 0, "type": "header", "schema": jmod.JOURNAL_SCHEMA, "version": 1}
        path.write_bytes(line(header)
                         + line({"seq": 1, "type": "event", "event": event(0).to_json()})
                         + line({"seq": 2, "type": "marker", "note": "future"})
                         + line({"seq": 3, "type": "event", "event": event(1).to_json()}))
        return path

    def test_bad_event_payload(self) -> None:
        path = self.layout.in_progress_dir / "B1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {"seq": 0, "type": "header", "schema": jmod.JOURNAL_SCHEMA, "version": 1}
        path.write_bytes(line(header)
                         + line({"seq": 1, "type": "event", "event": {"kind": "warp_drive",
                                 "phase": "airborne", "host_time": T0.isoformat()}})
                         + line({"seq": 2, "type": "event", "event": event(0).to_json()}))
        r = read_journal(path)
        self.assertEqual(r.event_count, 1)
        self.assertEqual(r.issues[0].kind, IssueKind.CORRUPT)

    def test_newer_schema_and_missing_header(self) -> None:
        path = self.layout.in_progress_dir / "N1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(line({"seq": 0, "type": "header", "schema": jmod.JOURNAL_SCHEMA, "version": 9})
                         + line({"seq": 1, "type": "event", "event": event(0).to_json()}))
        r = read_journal(path)
        self.assertEqual(r.issues[0].kind, IssueKind.NEWER_SCHEMA)
        self.assertEqual(r.event_count, 1)

        path.write_bytes(line({"seq": 1, "type": "event", "event": event(0).to_json()}))
        r = read_journal(path)
        self.assertIsNone(r.header)
        self.assertEqual(r.event_count, 1)
        self.assertIn(IssueKind.NO_HEADER, [i.kind for i in r.issues])

    def test_empty_and_header_only(self) -> None:
        path = self.layout.in_progress_dir / "E1.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        r = read_journal(path)
        self.assertEqual((r.event_count, r.issue_count, r.last_seq, r.header), (0, 0, -1, None))
        path = self.write(0)
        r = read_journal(path)
        self.assertIsNotNone(r.header)
        self.assertEqual(r.event_count, 0)
        self.assertIsNone(r.last_event)

    def test_issue_list_is_bounded(self) -> None:
        path = self.write(1)
        with open(path, "ab") as f:
            for _ in range(jmod.MAX_REPORTED_ISSUES + 50):
                f.write(b"junk\n")
        r = read_journal(path)
        self.assertEqual(len(r.issues), jmod.MAX_REPORTED_ISSUES)
        self.assertEqual(r.issue_count, jmod.MAX_REPORTED_ISSUES + 50)

    def test_keep_events_false_and_callback(self) -> None:
        path = self.write(10)
        seen: list[int] = []
        r = read_journal(path, keep_events=False, on_event=lambda seq, e: seen.append(seq))
        self.assertEqual(r.events, ())
        self.assertEqual(r.event_count, 10)
        self.assertEqual(seen, list(range(1, 11)))
        self.assertEqual(r.last_event, event(9))
        self.assertEqual([e.data["i"] for e in iter_journal_events(path)], list(range(10)))

    def test_crc_is_over_line_bytes(self) -> None:
        raw = line({"seq": 1, "type": "x", "v": "ä"})
        body, crc = raw.rstrip(b"\n").rsplit(b',"crc":"', 1)
        self.assertEqual(int(crc[:8], 16), zlib.crc32(body + b"}"))


if __name__ == "__main__":
    unittest.main()
