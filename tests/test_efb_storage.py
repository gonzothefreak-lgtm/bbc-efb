"""Offline tests for the Logbook storage layer (bbc_efb/storage.py)."""

import errno
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from bbc_efb import storage
from bbc_efb.storage import (
    DataFolderMissing, DataFolderNotWritable, FlightFields, FlightFileBusy, FlightStore, Gap,
    RecordConflict, RecordMissing, RecordReadOnly, RecorderEvidence, StoreLocked, StoreReadOnly,
)


def manual(**changes):
    values = dict(flight_date="2024-01-01", aircraft="A320", departure="EGLL", arrival="LFPG")
    values.update(changes)
    return FlightFields(**values)


def transient_permission_error(winerror=32):
    error = PermissionError(13, "The process cannot access the file")
    error.winerror = winerror
    return error


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        self.store = FlightStore(self.root)

    def tearDown(self):
        self.store.close()
        self._folder.cleanup()

    def path(self, flight):
        return self.root / "flights" / f"{flight.id}.json"

    def rewrite(self, flight, change):
        path = self.path(flight)
        payload = json.loads(path.read_text(encoding="utf-8"))
        change(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class RoundTripTests(StoreCase):
    def test_new_manual_flight_is_schema_2_and_normalised(self):
        flight = self.store.save(manual(aircraft=" a320 ", departure="egll", notes="  hi "))
        payload = json.loads(self.path(flight).read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], 2)
        self.assertEqual(payload["source"], "manual")
        self.assertEqual(payload["revisions"][0]["by"], "pilot")
        self.assertEqual(flight.fields.aircraft, "A320")
        self.assertEqual(flight.fields.departure, "EGLL")
        self.assertEqual(flight.fields.notes, "hi")
        self.assertEqual(self.store.get(flight.id), flight)

    def test_edit_appends_revision_and_noop_save_writes_nothing(self):
        flight = self.store.save(manual())
        edited = self.store.save(manual(minutes=60), flight)
        self.assertEqual(len(edited.revisions), 2)
        before = self.path(flight).read_bytes()
        same = self.store.save(manual(minutes=60), edited)
        self.assertEqual(len(same.revisions), 2)
        self.assertEqual(self.path(flight).read_bytes(), before)

    def test_payload_unknown_is_distinct_from_zero(self):
        flight = self.store.save(manual(crew=0, passengers=None, baggage_kg=0))
        again = self.store.get(flight.id).fields
        self.assertEqual(again.crew, 0)
        self.assertIsNone(again.passengers)
        self.assertEqual(again.baggage_kg, 0)
        self.assertIsNone(again.freight_kg)
        for bad in (-1, True, 1.5, 10_000_000):
            with self.assertRaises(ValueError):
                self.store.save(manual(freight_kg=bad))

    def test_saved_at_has_milliseconds_so_quick_edits_stay_ordered(self):
        flight = self.store.save(manual())
        edited = self.store.save(manual(minutes=1), flight)
        self.assertLessEqual(edited.revisions[0].saved_at_utc, edited.revisions[1].saved_at_utc)
        self.assertRegex(edited.revisions[1].saved_at, r"\.\d{3}\+00:00$")


class PreservationTests(StoreCase):
    def test_unchanged_values_and_old_revisions_are_written_back_exactly(self):
        flight = self.store.save(manual(notes="hi"))

        def hand_edit(payload):
            payload["revisions"][0]["fields"].update(departure="egll ", notes="  keep spaces ")
        path = self.rewrite(flight, hand_edit)
        stored = self.store.get(flight.id)
        self.assertEqual(stored.fields.departure, "egll ")
        edited = self.store.save(
            FlightFields(**{**stored.fields.as_json(), "minutes": 60}), stored)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["revisions"][0]["fields"]["departure"], "egll ")
        self.assertEqual(payload["revisions"][0]["fields"]["notes"], "  keep spaces ")
        self.assertEqual(edited.fields.departure, "egll ")
        self.assertEqual(edited.changed_fields(1), ("minutes",))

    def test_case_only_external_edit_is_a_conflict(self):
        flight = self.store.save(manual())
        self.rewrite(flight, lambda payload: payload["revisions"][0]["fields"].update(departure="Egll"))
        with self.assertRaises(RecordConflict):
            self.store.save(manual(minutes=5), flight)

    def test_stale_flight_conflicts_and_fresh_copy_saves(self):
        flight = self.store.save(manual())
        self.store.save(manual(minutes=5), flight)
        with self.assertRaises(RecordConflict):
            self.store.save(manual(minutes=9), flight)
        fresh = self.store.get(flight.id)
        self.assertEqual(self.store.save(manual(minutes=9), fresh).fields.minutes, 9)

    def test_unknown_keys_are_kept_at_every_level(self):
        flight = self.store.save(manual())

        def add_keys(payload):
            payload["future_block"] = {"a": 1}
            payload["revisions"][0]["signature"] = "xyz"
            payload["revisions"][0]["fields"]["fuel_kg"] = 4200
        path = self.rewrite(flight, add_keys)
        stored = self.store.get(flight.id)
        self.assertEqual(stored.extra["future_block"], {"a": 1})
        self.assertEqual(stored.revisions[0].extra["fuel_kg"], 4200)
        self.store.save(manual(minutes=30), stored)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["future_block"], {"a": 1})
        self.assertEqual(payload["revisions"][0]["signature"], "xyz")
        self.assertEqual(payload["revisions"][1]["fields"]["fuel_kg"], 4200)

    def test_newer_schema_is_shown_read_only_and_never_rewritten(self):
        flight = self.store.save(manual())
        path = self.rewrite(flight, lambda payload: payload.update(schema=3, hologram=True))
        before = path.read_bytes()
        flights, unreadable = self.store.listing()
        self.assertEqual(unreadable, [])
        self.assertTrue(flights[0].read_only)
        self.assertIn("newer BBC EFB", flights[0].read_only_reason)
        with self.assertRaises(RecordReadOnly):
            self.store.save(manual(minutes=3), flights[0])
        self.assertEqual(path.read_bytes(), before)

    def test_old_revision_outside_new_limits_still_loads(self):
        flight = self.store.save(manual())
        self.rewrite(flight, lambda payload: payload["revisions"][0]["fields"].update(minutes=12000, aircraft="X" * 300))
        stored = self.store.get(flight.id)
        self.assertEqual(stored.fields.minutes, 12000)
        self.assertTrue(stored.problems)
        edited = self.store.save(FlightFields(**{**stored.fields.as_json(), "callsign": "BAW1"}), stored)
        self.assertEqual(edited.fields.aircraft, "X" * 300)
        self.assertEqual(edited.fields.minutes, 12000)

    def test_new_or_changed_value_over_limit_is_rejected_not_truncated(self):
        with self.assertRaises(ValueError):
            self.store.save(manual(aircraft="A" * 257))
        with self.assertRaises(ValueError):  # ß upper-cases to SS: measured after normalising
            self.store.save(manual(callsign="ß" * 20))
        self.assertEqual(self.store.save(manual(aircraft="a" * 256)).fields.aircraft, "A" * 256)

    def test_lone_surrogate_is_rejected_before_any_file_is_written(self):
        with self.assertRaises(ValueError):
            self.store.save(manual(notes="\ud800"))
        self.assertEqual([p.name for p in (self.root / "flights").iterdir()], [])


class MigrationTests(StoreCase):
    def write_v1(self, **fields):
        flight_id = str(uuid4())
        payload = {
            "schema": 1, "source": "manual", "id": flight_id,
            "revisions": [{"saved_at": "2024-01-01T10:00:00+00:00", "fields": {
                "flight_date": "2024-01-01", "aircraft": "a320", "departure": "EGLL",
                "arrival": "LFPG", "callsign": "", "registration": "", "minutes": None,
                "notes": "", **fields,
            }}],
        }
        path = self.root / "flights" / f"{flight_id}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path, payload

    def test_v1_is_read_without_rewriting_and_migrated_on_save(self):
        path, original = self.write_v1()
        before = path.read_bytes()
        flights, unreadable = self.store.listing()
        self.assertEqual((len(flights), unreadable), (1, []))
        self.assertEqual(flights[0].schema, 1)
        self.assertEqual(flights[0].fields.aircraft, "a320")  # not normalised on read
        self.assertEqual(path.read_bytes(), before)
        edited = self.store.save(FlightFields(**{**flights[0].fields.as_json(), "minutes": 75}), flights[0])
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], 2)
        self.assertEqual(payload["migrated_from"]["schema"], 1)
        self.assertEqual(payload["revisions"][0], original["revisions"][0])
        backup = self.root / payload["migrated_from"]["original_copy"]
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(edited.migrated_from_schema, 1)
        self.assertEqual(edited.fields.aircraft, "a320")
        self.assertEqual(edited.provenance("minutes").corrections[0].value, 75)

    def test_v1_with_unknown_field_key_is_readable(self):
        self.write_v1(crew_note="two")
        flights, unreadable = self.store.listing()
        self.assertEqual((len(flights), unreadable), (1, []))
        self.assertEqual(flights[0].revisions[0].extra["crew_note"], "two")


class UnreadableTests(StoreCase):
    def test_bad_saved_at_is_reported_and_listing_survives_same_date_sort(self):
        good = self.store.save(manual())
        for value in (123, None, [1], "garbage", "2024-01-01T10:00:00"):
            bad = self.store.save(manual())
            self.rewrite(bad, lambda payload, value=value: payload["revisions"][0].update(saved_at=value))
        with self.assertLogs("bbc_efb.storage", "WARNING"):
            flights, unreadable = self.store.listing()
        self.assertEqual([flight.id for flight in flights], [good.id])
        self.assertEqual(len(unreadable), 5)
        self.assertTrue(all(item.kind == "unreadable" and "saved_at" in item.reason for item in unreadable))

    def test_corrupt_deep_and_bom_files(self):
        records = self.root / "flights"
        (records / f"{uuid4()}.json").write_text("{not json", encoding="utf-8")
        (records / f"{uuid4()}.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        good = self.store.save(manual())
        path = self.path(good)
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())  # Notepad's UTF-8 BOM
        with self.assertLogs("bbc_efb.storage", "WARNING"):
            flights, unreadable = self.store.listing()
        self.assertEqual([flight.id for flight in flights], [good.id])
        self.assertEqual(len(unreadable), 2)
        self.assertTrue(any("JSON" in item.reason for item in unreadable))
        self.assertTrue(any("deeply" in item.reason for item in unreadable))

    def test_uppercase_file_name_is_readable_and_saved_in_place(self):
        flight = self.store.save(manual())
        path = self.path(flight)
        upper = path.with_name(flight.id.upper() + ".json")
        path.rename(upper)
        flights, unreadable = self.store.listing()
        self.assertEqual(([f.id for f in flights], unreadable), ([flight.id], []))
        self.store.save(manual(minutes=4), flights[0])
        self.assertEqual(len(list((self.root / "flights").glob("*.json"))), 1)
        self.assertEqual(self.store.get(flight.id.upper()).fields.minutes, 4)

    def test_other_spellings_and_conflict_copies_are_misnamed_not_corrupt(self):
        flight = self.store.save(manual())
        content = self.path(flight).read_text(encoding="utf-8")
        records = self.root / "flights"
        (records / f"{flight.id.replace('-', '')}.json").write_text(content, encoding="utf-8")
        (records / f"{flight.id}-DESKTOP-ABC.json").write_text(content, encoding="utf-8")
        flights, unreadable = self.store.listing()
        self.assertEqual(len(flights), 1)
        self.assertEqual(sorted(item.kind for item in unreadable), ["misnamed", "misnamed"])
        self.assertTrue(any("Rename it to" in item.reason for item in unreadable))

    def test_missing_flight_raises_record_missing(self):
        flight = self.store.save(manual())
        self.path(flight).unlink()
        with self.assertRaises(RecordMissing):
            self.store.save(manual(minutes=1), flight)


class CacheTests(StoreCase):
    def test_listing_does_not_reread_unchanged_files(self):
        for day in range(1, 4):
            self.store.save(manual(flight_date=f"2024-01-0{day}"))
        self.store.listing()
        with mock.patch.object(FlightStore, "_read", wraps=self.store._read) as reads:
            flights, _ = self.store.listing()
            self.assertEqual(reads.call_count, 0)
            self.store.save(manual(minutes=1), flights[0])
            self.store.listing()
            self.assertEqual(reads.call_count, 1)  # the conflict check reads the file once
        target = self.path(flights[1])
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["revisions"][0]["fields"]["notes"] = "changed elsewhere"
        target.write_text(json.dumps(payload), encoding="utf-8")
        flights, _ = self.store.listing()
        self.assertIn("changed elsewhere", [flight.fields.notes for flight in flights])

    def test_removed_file_disappears_from_listing(self):
        flight = self.store.save(manual())
        self.store.listing()
        self.path(flight).unlink()
        self.assertEqual(self.store.listing(), ([], []))


class LockTests(StoreCase):
    def test_second_writer_is_refused_with_a_clear_message(self):
        with self.assertRaises(StoreLocked) as raised:
            FlightStore(self.root)
        self.assertIn("Another BBC EFB", str(raised.exception))

    def test_read_only_store_can_list_but_not_save(self):
        flight = self.store.save(manual())
        reader = FlightStore(self.root, read_only=True)
        self.assertEqual([f.id for f in reader.listing()[0]], [flight.id])
        with self.assertRaises(StoreReadOnly):
            reader.save(manual())

    def test_lock_is_released_on_close(self):
        self.store.close()
        with self.assertRaises(StoreReadOnly):
            self.store.save(manual())
        with FlightStore(self.root) as other:
            self.assertTrue(other.writable)
        self.store = FlightStore(self.root)

    def test_two_threads_saving_the_same_base_cannot_both_win(self):
        base = self.store.save(manual())
        results, barrier = [], threading.Barrier(2)

        def worker(minutes):
            barrier.wait()
            try:
                results.append(self.store.save(manual(minutes=minutes), base).fields.minutes)
            except RecordConflict:
                results.append("conflict")
        threads = [threading.Thread(target=worker, args=(m,)) for m in (10, 20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(map(str, results))[-1], "conflict")
        self.assertEqual(len(self.store.get(base.id).revisions), 2)


class LockErrnoTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def test_folder_without_lock_support_is_not_called_locked(self):
        failure = OSError(errno.ENOTSUP, "Operation not supported")
        with mock.patch.object(storage, "_lock", side_effect=failure):
            with self.assertRaises(DataFolderNotWritable) as raised:
                FlightStore(self.root)
        self.assertNotIsInstance(raised.exception, StoreLocked)
        self.assertIn("file locking", str(raised.exception))
        self.assertNotIn("Another BBC EFB", str(raised.exception))

    def test_no_locks_available_is_not_called_locked(self):
        with mock.patch.object(storage, "_lock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            with self.assertRaises(DataFolderNotWritable):
                FlightStore(self.root)

    def test_real_contention_errnos_are_store_locked(self):
        for code in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
            with self.subTest(code=code), mock.patch.object(storage, "_lock", side_effect=OSError(code, "busy")):
                with self.assertRaises(StoreLocked):
                    FlightStore(self.root)


class CaseSpellingTests(StoreCase):
    def test_name_rank_prefers_the_exact_lower_case_spelling(self):
        names = ["ABC.json", "abc.json", "Abc.json"]
        self.assertEqual(sorted(names, key=storage._name_rank)[0], "abc.json")
        self.assertEqual(min(["ABC.json", "Abc.json"], key=storage._name_rank),
                         sorted(["Abc.json", "ABC.json"], key=storage._name_rank)[0])

    def test_listing_and_find_pick_the_same_file_on_a_case_sensitive_folder(self):
        flight = self.store.save(manual())
        records = self.root / "flights"
        upper = records / f"{flight.id.upper()}.json"
        payload = json.loads(self.path(flight).read_text(encoding="utf-8"))
        payload["revisions"][0]["fields"]["notes"] = "upper copy"
        upper.write_text(json.dumps(payload), encoding="utf-8")
        if len(list(records.glob("*.json"))) != 2:
            self.skipTest("This file system is not case-sensitive (macOS and Windows defaults).")
        flights, unreadable = self.store.listing()
        self.assertEqual([f.fields.notes for f in flights], [""])
        self.assertEqual([(item.name, item.kind) for item in unreadable], [(upper.name, "duplicate")])
        self.assertEqual(self.store.get(flight.id).fields.notes, "")
        self.store.save(manual(minutes=3), flights[0])  # no RecordConflict: same file
        self.assertEqual(self.store.get(flight.id).fields.minutes, 3)

    def test_find_scan_uses_the_listing_order(self):
        flight_id = str(uuid4())
        names = [f"{flight_id.upper()}.json", f"{flight_id[:4].upper()}{flight_id[4:]}.json"]
        entries = [mock.Mock(name=n) for n in names]
        for entry, name in zip(entries, names):
            entry.name = name
            entry.is_file.return_value = True
        with mock.patch.object(storage.os, "scandir", return_value=entries), \
                mock.patch.object(Path, "is_file", return_value=False):
            found = self.store._find(flight_id)
        self.assertEqual(found.name, min(names, key=storage._name_rank))


class ListingConcurrencyTests(StoreCase):
    def test_listing_does_not_wait_for_a_writer(self):
        self.store.save(manual())
        holding, release, done = threading.Event(), threading.Event(), []

        def writer():
            with self.store._mutex:  # as a write retrying a busy file would
                holding.set()
                release.wait(5)
        thread = threading.Thread(target=writer)
        thread.start()
        holding.wait(5)
        lister = threading.Thread(target=lambda: done.append(self.store.listing()))
        lister.start()
        lister.join(1.0)
        finished = not lister.is_alive()
        release.set()
        thread.join()
        lister.join()
        self.assertTrue(finished, "listing() waited for the write lock")
        self.assertEqual(len(done[0][0]), 1)


class FolderTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.base = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()

    def test_missing_folder_is_reported_not_created(self):
        with self.assertRaises(DataFolderMissing):
            FlightStore(self.base / "gone")
        self.assertFalse((self.base / "gone").exists())

    def test_create_makes_parents(self):
        with FlightStore(self.base / "BBC EFB" / "data", create=True) as store:
            self.assertTrue(store.records.is_dir())
            self.assertEqual(list(store.records.iterdir()), [])  # probe files removed

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions only, and not as root")
    def test_unwritable_folder_is_reported(self):
        folder = self.base / "locked"
        folder.mkdir()
        folder.chmod(0o500)
        try:
            with self.assertRaises(DataFolderNotWritable):
                FlightStore(folder)
        finally:
            folder.chmod(0o700)

    def test_old_leftover_temp_files_are_swept_on_open(self):
        records = self.base / "flights"
        records.mkdir()
        old = records / ".flight-crash.tmp"
        new = records / ".flight-busy.tmp"
        old.write_text("x")
        new.write_text("x")
        stale = time.time() - 3600
        os.utime(old, (stale, stale))
        with FlightStore(self.base) as store:
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertEqual(store.listing(), ([], []))


class ReplaceRetryTests(StoreCase):
    def test_transient_sharing_violation_is_retried(self):
        flight = self.store.save(manual())
        real = os.replace
        calls = []

        def flaky(source, target):
            calls.append(target)
            if len(calls) == 1:
                raise transient_permission_error(32)
            return real(source, target)
        with mock.patch.object(storage.os, "replace", side_effect=flaky), \
                mock.patch.object(storage, "_sleep") as sleep:
            edited = self.store.save(manual(minutes=7), flight)
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once()
        self.assertEqual(self.store.get(flight.id).fields.minutes, 7)
        self.assertEqual(edited.fields.minutes, 7)
        self.assertEqual(list((self.root / "flights").glob(".flight-*.tmp")), [])

    def test_persistent_sharing_violation_gives_plain_message_and_keeps_file(self):
        flight = self.store.save(manual())
        before = self.path(flight).read_bytes()
        with mock.patch.object(storage.os, "replace", side_effect=transient_permission_error(5)), \
                mock.patch.object(storage, "_sleep") as sleep:
            with self.assertRaises(FlightFileBusy) as raised:
                self.store.save(manual(minutes=7), flight)
        self.assertEqual(sleep.call_count, len(storage._RETRY_DELAYS_S))
        self.assertLess(sum(storage._RETRY_DELAYS_S), 1.5)
        self.assertIn("Nothing was changed", str(raised.exception))
        self.assertEqual(self.path(flight).read_bytes(), before)
        self.assertEqual(list((self.root / "flights").glob(".flight-*.tmp")), [])

    def test_other_permission_errors_are_not_retried(self):
        flight = self.store.save(manual())
        with mock.patch.object(storage.os, "replace", side_effect=PermissionError(13, "denied")), \
                mock.patch.object(storage, "_sleep") as sleep:
            with self.assertRaises(PermissionError):
                self.store.save(manual(minutes=7), flight)
        sleep.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "needs Windows sharing semantics")
    def test_real_windows_sharing_violation_clears_within_retry(self):
        import _winapi
        flight = self.store.save(manual())
        handle = _winapi.CreateFile(
            str(self.path(flight)), _winapi.GENERIC_READ, 0x1 | 0x2, 0, _winapi.OPEN_EXISTING, 0, 0)
        timer = threading.Timer(0.1, _winapi.CloseHandle, (handle,))
        timer.start()
        try:
            self.assertEqual(self.store.save(manual(minutes=8), flight).fields.minutes, 8)
        finally:
            timer.join()


class RecorderApiTests(StoreCase):
    def evidence(self, **changes):
        values = dict(
            journal="recorder/archive/flight.jsonl", summary="Normal flight", time_basis="simulator",
            simulated=False, off_block="2024-01-01T10:00:00+00:00", on_block="2024-01-01T11:30:00+00:00",
            block_s=5400.0, airborne_s=4500.0, touchdown_rate_fpm=None,
            gaps=(Gap("Simulator disconnected", "2024-01-01T10:40:00+00:00", "2024-01-01T10:42:00+00:00", "disconnect"),),
        )
        values.update(changes)
        return RecorderEvidence(**values)

    def test_recorded_flight_keeps_observed_values_and_evidence(self):
        title = "Airbus A320neo FlyByWire Simulations A32NX Development"
        flight = self.store.create_recorded(
            FlightFields("2024-01-01", title, "EGLL", ""), self.evidence(),
            origins={"aircraft": "observed", "departure": "derived"},
        )
        stored = self.store.get(flight.id)
        self.assertEqual(stored.source, "recorder")
        self.assertEqual(stored.fields.aircraft, title)  # not upper-cased
        self.assertEqual(stored.provenance("aircraft").origin, "observed")
        self.assertEqual(stored.provenance("departure").origin, "derived")
        self.assertEqual(stored.provenance("callsign").origin, "recorder")
        self.assertIsNone(stored.evidence.touchdown_rate_fpm)
        self.assertEqual(stored.evidence.gaps[0].kind, "disconnect")
        self.assertFalse(stored.simulated)

    def test_correction_keeps_original_with_time_and_reason(self):
        flight = self.store.create_recorded(FlightFields("2024-01-01", "A20N", "EGLL", ""), self.evidence())
        with self.assertRaises(ValueError):
            self.store.correct(flight, {"arrival": "LFPG"}, reason=" ")
        corrected = self.store.correct(flight, {"arrival": "lfpg"}, reason="Recorder lost the arrival")
        provenance = corrected.provenance("arrival")
        self.assertEqual((provenance.original, provenance.value), ("", "LFPG"))
        self.assertEqual(provenance.corrections[0].reason, "Recorder lost the arrival")
        self.assertEqual(provenance.corrections[0].by, "pilot")
        self.assertEqual(corrected.corrected_fields(), ("arrival",))
        with self.assertRaises(ValueError):
            self.store.correct(corrected, {"altitude": 1}, reason="x")

    def test_evidence_is_checked(self):
        fields = FlightFields("2024-01-01", "A20N", "EGLL", "LFPG")
        for bad in (dict(journal="/abs/path.jsonl"), dict(journal="..\\up.jsonl"), dict(time_basis="wall"),
                    dict(simulated=1), dict(block_s=-1.0), dict(touchdown_rate_fpm=float("nan")),
                    dict(off_block="10:00"), dict(gaps=(Gap(""),))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.create_recorded(fields, self.evidence(**bad))
        self.assertEqual(self.store.listing(), ([], []))

    def test_gap_acknowledgement_is_appended(self):
        flight = self.store.create_recorded(FlightFields("2024-01-01", "A20N", "EGLL", "LFPG"), self.evidence(simulated=True))
        self.assertTrue(flight.simulated)
        with self.assertRaises(ValueError):
            self.store.acknowledge_gap(flight, 3)
        acknowledged = self.store.acknowledge_gap(flight, 0, note="Known disconnect")
        self.assertEqual([item.gap for item in acknowledged.acknowledgements], [0])
        self.assertEqual(acknowledged.revisions, flight.revisions)
        self.assertEqual(self.store.acknowledge_gap(acknowledged, 0), acknowledged)

    def test_imported_flight(self):
        flight = self.store.create_imported(
            FlightFields("2023-05-01", "c172", "EGKA", "EGKA", minutes=60), importer="CSV",
            reference="logbook.csv line 4", extra={"tach": 1234.5},
        )
        stored = self.store.get(flight.id)
        self.assertEqual(stored.source, "imported")
        self.assertEqual(stored.import_details.reference, "logbook.csv line 4")
        self.assertEqual(stored.extra["tach"], 1234.5)
        self.assertEqual(stored.fields.aircraft, "c172")
        self.assertEqual(stored.provenance("aircraft").origin, "imported")


if __name__ == "__main__":
    unittest.main()
