"""Check the yearly manual Logbook store without Qt or the simulator."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from bbc_efb.storage import Flight, FlightFields, FlightStore, RecordConflict, Revision


class LogbookStorageOfflineTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = FlightStore(self.root)
        self.original = FlightFields("2026-12-31", "A320", "YMML", "YSSY", minutes=80)

    def test_manual_workflow_uses_one_year_file_and_keeps_corrections(self):
        first = self.store.save(self.original)
        second = self.store.save(replace(self.original, aircraft="Q400"))
        reopened = FlightStore(self.root)
        self.assertEqual(reopened.get(first.id), first)
        corrected = reopened.save(replace(first.fields, minutes=84), first)

        self.assertEqual(reopened.save(corrected.fields, corrected), corrected)
        self.assertEqual([path.name for path in self.store.records.glob("*.json")], ["flights-2026.json"])
        flights, unreadable = FlightStore(self.root).listing()
        self.assertEqual({flight.id for flight in flights}, {first.id, second.id})
        self.assertEqual(unreadable, [])
        self.assertEqual([revision.fields.minutes for revision in corrected.revisions], [80, 84])
        self.assertEqual(corrected.id, first.id)
        self.assertEqual((corrected.source, corrected.status), ("manual", "complete"))

    def test_invalid_required_fields_do_not_create_a_record(self):
        with self.assertRaisesRegex(ValueError, "required"):
            self.store.save(replace(self.original, arrival=" "))
        self.assertEqual(self.store.listing(), ([], []))

    def test_legacy_record_opens_and_its_original_remains_after_edit(self):
        legacy = Flight(str(uuid4()), (Revision("2026-01-01T01:02:03+00:00", self.original),))
        content = json.dumps({"schema": 1, "id": legacy.id, "source": "manual",
                              "revisions": [asdict(revision) for revision in legacy.revisions]})
        path = self.store.records / f"{legacy.id}.json"
        path.write_text(content, encoding="utf-8")
        self.assertEqual(self.store.listing(), ([legacy], []))
        edited = self.store.save(replace(legacy.fields, notes="Legacy flight edited"), legacy)
        self.assertEqual(self.store.listing(), ([edited], []))
        self.assertEqual(edited.revisions[0], legacy.revisions[0])
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_failed_year_replacement_leaves_previous_revision_readable(self):
        first = self.store.save(self.original)
        with patch("bbc_efb.storage.os.replace", side_effect=OSError("write interrupted")):
            with self.assertRaisesRegex(OSError, "write interrupted"):
                self.store.save(replace(first.fields, minutes=84), first)
        self.assertEqual(FlightStore(self.root).get(first.id), first)
        self.assertEqual(list(self.store.records.glob("*.tmp")), [])

    def test_year_move_recovery_after_each_interrupted_write(self):
        for blocked_stage in ("2027", "2026", "journal cleanup"):
            with self.subTest(blocked_stage=blocked_stage), TemporaryDirectory() as folder:
                root = Path(folder)
                store = FlightStore(root)
                first = store.save(self.original)
                other = store.save(replace(self.original, aircraft="Q400"))
                destination = store.save(replace(self.original, flight_date="2027-01-01", aircraft="B738"))
                write_year = store._write_year
                unlink = Path.unlink

                def interrupted(year, flights):
                    if year == blocked_stage:
                        raise OSError("power lost")
                    write_year(year, flights)

                def interrupted_unlink(path, *args, **kwargs):
                    if blocked_stage == "journal cleanup" and path.name == ".year-move.json":
                        raise OSError("power lost")
                    return unlink(path, *args, **kwargs)

                with (patch.object(store, "_write_year", side_effect=interrupted),
                      patch.object(Path, "unlink", interrupted_unlink)):
                    with self.assertRaisesRegex(OSError, "power lost"):
                        store.save(replace(self.original, flight_date="2027-01-01"), first)
                reopened = FlightStore(root)
                flights, unreadable = reopened.listing()
                self.assertEqual(unreadable, [])
                self.assertEqual(len(flights), 3)
                self.assertEqual({flight.id for flight in flights}, {first.id, other.id, destination.id})
                moved = reopened.get(first.id)
                self.assertEqual([r.fields.flight_date for r in moved.revisions], ["2026-12-31", "2027-01-01"])
                self.assertFalse((reopened.records / ".year-move.json").exists())
                self.assertEqual(reopened.get(other.id), other)
                self.assertEqual(reopened.get(destination.id), destination)

    def test_stale_editor_cannot_overwrite_a_record_moved_to_another_year(self):
        first = self.store.save(self.original)
        edited = self.store.save(replace(self.original, flight_date="2027-01-01"), first)
        with self.assertRaises(RecordConflict):
            FlightStore(self.root).save(replace(self.original, minutes=84), first)
        self.assertEqual(self.store.listing(), ([edited], []))

    def test_second_store_cannot_write_during_an_update(self):
        other = FlightStore(self.root)
        with self.store._locked():
            with self.assertRaisesRegex(OSError, "busy"):
                other.save(self.original)
        self.assertEqual(other.save(self.original).fields, self.original)

    def test_damaged_year_is_reported_and_left_untouched(self):
        first = self.store.save(self.original)
        damaged = self.store.records / "flights-2027.json"
        damaged.write_text("{", encoding="utf-8")
        self.assertEqual(self.store.listing(), ([first], [damaged.name]))
        with self.assertRaisesRegex(ValueError, damaged.name):
            self.store.save(replace(self.original, flight_date="2027-01-01"))
        self.assertEqual(damaged.read_text(encoding="utf-8"), "{")

    def test_reopen_and_edit_an_annual_file_with_5000_flights(self):
        revision = Revision(datetime.now(timezone.utc).isoformat(), self.original)
        records = [Flight(str(uuid4()), (revision,)) for _ in range(5000)]
        (self.store.records / "flights-2026.json").write_text(json.dumps({
            "schema": 1, "year": "2026", "flights": [asdict(flight) for flight in records],
        }), encoding="utf-8")
        reopened = FlightStore(self.root)
        flight = reopened.get(records[-1].id)
        edited = reopened.save(replace(self.original, minutes=84), flight)
        flights, unreadable = reopened.listing()
        self.assertEqual((len(flights), unreadable), (5000, []))
        self.assertEqual(reopened.get(flight.id), edited)
