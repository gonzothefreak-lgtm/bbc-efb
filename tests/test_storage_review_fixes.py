"""Regression tests for review fixes in storage, data management, preferences and
data-folder settings (transient read errors, BOM files, listener locking, panel maps,
NaN preferences, settings.json write retry)."""

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from bbc_efb import data_management as dm
from bbc_efb import preferences as preferences_module
from bbc_efb import storage
from bbc_efb.preferences import Preferences, PreferencesError
from bbc_efb.storage import FlightFields, FlightStore
from bbc_efb.storage_settings import DataFolderSettings


def manual():
    return FlightFields(flight_date="2024-01-01", aircraft="A320", departure="EGLL", arrival="LFPG")


class _Folder(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)

    def tearDown(self):
        self._folder.cleanup()


class TransientReadErrorTests(_Folder):
    def _flaky_read(self, error_factory, times=1):
        original = Path.read_bytes
        state = {"left": times}

        def flaky(path):
            if path.name.endswith(".json") and path.parent.name == "flights" and state["left"] > 0:
                state["left"] -= 1
                raise error_factory()
            return original(path)
        return mock.patch.object(Path, "read_bytes", flaky)

    def test_oserror_in_listing_is_not_cached(self):
        store = FlightStore(self.root)
        try:
            flight = store.save(manual())
            store._cache.clear()
            with self._flaky_read(lambda: PermissionError(13, "in use")), \
                    self.assertLogs("bbc_efb.storage", "WARNING"):
                flights, unreadable = store.listing()
            self.assertEqual((len(flights), len(unreadable)), (0, 1))
            self.assertIn("right now", unreadable[0].reason)
            flights, unreadable = store.listing()  # the other program let go
            self.assertEqual([item.id for item in flights], [flight.id])
            self.assertEqual(unreadable, [])
        finally:
            store.close()

    def test_windows_sharing_violation_is_retried_once(self):
        def sharing_violation():
            error = PermissionError(13, "The process cannot access the file")
            error.winerror = 32
            return error
        store = FlightStore(self.root)
        try:
            flight = store.save(manual())
            store._cache.clear()
            with self._flaky_read(sharing_violation), mock.patch.object(storage, "_sleep") as slept:
                flights, unreadable = store.listing()
            self.assertEqual([item.id for item in flights], [flight.id])
            self.assertEqual(unreadable, [])
            slept.assert_called_once()
        finally:
            store.close()

    def test_parse_failure_is_still_cached(self):
        store = FlightStore(self.root)
        try:
            bad = self.root / "flights" / "00000000-0000-4000-8000-000000000001.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertLogs("bbc_efb.storage", "WARNING"):
                store.listing()
            with mock.patch.object(FlightStore, "_read", side_effect=AssertionError("re-read")):
                flights, unreadable = store.listing()
            self.assertEqual(len(unreadable), 1)
        finally:
            store.close()


class BomFlightExportTests(_Folder):
    def test_bom_flight_is_counted_as_readable_in_export_and_backup(self):
        data = self.root / "data"
        out = self.root / "out"
        out.mkdir()
        store = FlightStore(data, create=True)
        flight = store.save(manual())
        path = data / "flights" / f"{flight.id}.json"
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        flights, unreadable = store.listing()
        self.assertEqual((len(flights), len(unreadable)), (1, 0))
        store.close()
        summary = dm.summarize_record(path.name, path.read_bytes())
        self.assertFalse(summary.status.startswith("unreadable"), summary.status)
        self.assertEqual(dm._record_schema(path.read_bytes()), "2")
        result = dm.export_all_data(data, out / "export.zip", app_version="test")
        self.assertFalse(result.unreadable)
        backup = dm.create_backup(data, out / "backup.zip", app_version="test", record_schema=2)
        self.assertEqual(backup.schema_versions["flight_records_found"], {"2": 1})
        warnings = " ".join(dm.inspect_backup(out / "backup.zip").warnings)
        self.assertNotIn("could not be read", warnings)


class PreferencesFixTests(_Folder):
    def test_set_panel_visible_notifies_after_releasing_the_lock(self):
        prefs = Preferences.load(self.root / "preferences.json")
        seen = {}

        def listener(key, value):
            worker = threading.Thread(target=lambda: seen.setdefault("density", prefs.get("logbook.density")))
            worker.start()
            worker.join(2.0)
            seen["blocked"] = worker.is_alive()
            seen["value"] = value
        prefs.subscribe(listener)
        self.assertTrue(prefs.set_panel_visible("logbook.dashboard_panels", "charts", False))
        self.assertFalse(seen["blocked"])
        self.assertEqual(seen["value"]["charts"], False)

    def test_toggling_one_panel_keeps_every_other_stored_entry(self):
        path = self.root / "preferences.json"
        stored = {"summary": False, "weather.radar": False, "Bad Name": True}
        path.write_text(json.dumps({"format": 1, "logbook": {"dashboard_panels": stored}}), encoding="utf-8")
        prefs = Preferences.load(path)
        self.assertFalse(prefs.panel_visible("logbook.dashboard_panels", "summary"))
        self.assertFalse(prefs.panel_visible("logbook.dashboard_panels", "weather.radar"))
        self.assertEqual(prefs.invalid_keys(), ("logbook.dashboard_panels",))
        prefs.set_panel_visible("logbook.dashboard_panels", "charts", False)
        prefs.save()
        written = json.loads(path.read_text(encoding="utf-8"))["logbook"]["dashboard_panels"]
        self.assertEqual(written, {**stored, "charts": False})
        self.assertFalse(prefs.set_panel_visible("logbook.dashboard_panels", "charts", False))
        with self.assertRaises(PreferencesError):
            prefs.set_panel_visible("logbook.dashboard_panels", "Bad Name", False)
        with self.assertRaises(PreferencesError):
            prefs.set_panel_visible("logbook.density", "charts", False)

    def test_nan_in_preferences_is_treated_as_damaged(self):
        path = self.root / "preferences.json"
        path.write_text('{"format": 1, "plugin": {"zoom": NaN}}', encoding="utf-8")
        prefs = Preferences.load(path)
        self.assertIsNotNone(prefs.problem)
        self.assertTrue(prefs.writable)
        self.assertTrue(any(name.startswith("preferences.json.damaged-") for name in os.listdir(self.root)))
        prefs.set("logbook.density", "compact")
        self.assertTrue(prefs.save())
        with self.assertRaises(PreferencesError):
            prefs.adopt(b'{"format": 1, "x": Infinity}')


class DataFolderSettingsFixTests(_Folder):
    def test_remember_retries_a_windows_sharing_violation(self):
        settings = DataFolderSettings(self.root / "cfg" / "settings.json")
        settings.remember(self.root / "old")
        original = os.replace
        calls = {"n": 0}

        def flaky(source, target):
            calls["n"] += 1
            if calls["n"] == 1:
                error = PermissionError(13, "in use")
                error.winerror = 32
                raise error
            return original(source, target)
        with mock.patch.object(preferences_module.os, "replace", flaky), \
                mock.patch.object(preferences_module.time, "sleep"):
            settings.remember(self.root / "new")
        self.assertEqual(settings.load(), self.root / "new")
        self.assertEqual(calls["n"], 2)

    def test_damaged_settings_file_is_kept_aside(self):
        path = self.root / "settings.json"
        path.write_text("{broken", encoding="utf-8")
        settings = DataFolderSettings(path)
        settings.remember(self.root / "data")
        self.assertEqual(settings.load(), self.root / "data")
        kept = [name for name in os.listdir(self.root) if name.startswith("settings.json.damaged-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual((self.root / kept[0]).read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
