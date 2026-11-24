"""Tests for bbc_efb/preferences.py (Qt-free)."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from bbc_efb import preferences as prefs_module
from bbc_efb.preferences import (
    LAYOUT_KEYS, PREFERENCES_FORMAT, SPECS, Preferences, PreferencesError, PreferencesSaveError,
    default_preferences_path,
)

FIXED = datetime(2026, 11, 24, 10, 5, 18, 123000, tzinfo=timezone.utc)


def clock():
    return FIXED


class PreferencesCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.path = self.folder / "config" / "preferences.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.path.write_text(text, encoding="utf-8")

    def load(self):
        return Preferences.load(self.path, clock=clock)

    # Defaults and round trip ------------------------------------------------

    def test_missing_file_gives_defaults_without_problems(self):
        prefs = self.load()
        for key, spec in SPECS.items():
            self.assertEqual(prefs.get(key), spec.default, key)
            self.assertFalse(prefs.is_set(key))
        self.assertIsNone(prefs.problem)
        self.assertIsNone(prefs.read_only_reason)
        self.assertTrue(prefs.writable)
        self.assertFalse(self.path.exists())

    def test_set_save_and_reload(self):
        prefs = self.load()
        self.assertTrue(prefs.set("logbook.history_view", "cards"))
        self.assertFalse(prefs.set("logbook.history_view", "cards"))
        prefs.set("flight_logging.automatic", False)
        prefs.set("flight_logging.journal_retention_days", 90)
        prefs.set_panel_visible("logbook.dashboard_panels", "charts", False)
        self.assertTrue(prefs.dirty)
        self.assertTrue(prefs.save())
        self.assertFalse(prefs.dirty)
        self.assertFalse(prefs.save(), "nothing new to write")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["format"], PREFERENCES_FORMAT)
        self.assertEqual(payload["saved_at"], "2026-11-24T10:05:18.123+00:00")
        self.assertEqual(payload["logbook"]["history_view"], "cards")
        again = self.load()
        self.assertEqual(again.get("logbook.history_view"), "cards")
        self.assertFalse(again.get("flight_logging.automatic"))
        self.assertEqual(again.get("flight_logging.journal_retention_days"), 90)
        self.assertFalse(again.panel_visible("logbook.dashboard_panels", "charts"))
        self.assertTrue(again.panel_visible("logbook.dashboard_panels", "recent_flights"))
        self.assertTrue(again.panel_visible("logbook.dashboard_panels", "never_mentioned"))

    def test_get_returns_copies(self):
        prefs = self.load()
        panels = prefs.get("logbook.dashboard_panels")
        panels["charts"] = False
        self.assertTrue(prefs.get("logbook.dashboard_panels")["charts"])
        self.assertTrue(SPECS["logbook.dashboard_panels"].default["charts"])

    def test_default_path_is_outside_data_folder(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.folder)}), \
                mock.patch.object(prefs_module.sys, "platform", "linux"):
            self.assertEqual(default_preferences_path(), self.folder / "bbc-efb" / "preferences.json")
        with mock.patch.dict(os.environ, {"APPDATA": str(self.folder)}), \
                mock.patch.object(prefs_module.sys, "platform", "win32"):
            self.assertEqual(default_preferences_path(), self.folder / "BBC EFB" / "preferences.json")

    # Validation -------------------------------------------------------------

    def test_invalid_values_are_refused_on_set(self):
        prefs = self.load()
        bad = [
            ("logbook.history_view", "grid"), ("flight_logging.automatic", 1),
            ("flight_logging.journal_retention_days", 0), ("flight_logging.journal_retention_days", True),
            ("flight_logging.journal_retention_days", 1.5), ("logbook.dashboard_panels", {"Bad Name": True}),
            ("logbook.dashboard_panels", {"charts": "yes"}), ("logbook.dashboard_panels", ["charts"]),
            ("window.geometry", "not base64!"), ("simconnect.dll_path", "a\x00b"),
        ]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                with self.assertRaises(PreferencesError) as caught:
                    prefs.set(key, value)
                self.assertIn(SPECS[key].label, str(caught.exception))
        self.assertFalse(prefs.dirty)

    def test_hidden_history_columns_preference(self):
        prefs = self.load()
        self.assertEqual(prefs.get("logbook.hidden_columns"),
                         ["airborne", "landing", "off_block", "on_block", "registration", "takeoff"])
        self.assertIn("logbook.hidden_columns", LAYOUT_KEYS)
        self.assertTrue(prefs.set("logbook.hidden_columns", ["source", "callsign", "source"]))
        self.assertEqual(prefs.get("logbook.hidden_columns"), ["callsign", "source"])
        for bad in (["date"], ["nope"], "source", [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(PreferencesError):
                    prefs.set("logbook.hidden_columns", bad)
        prefs.save()
        self.assertEqual(self.load().get("logbook.hidden_columns"), ["callsign", "source"])

    def test_unknown_key_is_refused(self):
        prefs = self.load()
        with self.assertRaises(PreferencesError):
            prefs.get("nope")
        with self.assertRaises(PreferencesError):
            prefs.set("nope", 1)
        with self.assertRaises(PreferencesError):
            prefs.reset(["nope"])

    def test_empty_text_and_retention_none(self):
        prefs = self.load()
        prefs.set("simconnect.dll_path", "")
        self.assertIsNone(prefs.get("simconnect.dll_path"))
        prefs.set("flight_logging.journal_retention_days", None)
        self.assertIsNone(prefs.get("flight_logging.journal_retention_days"))

    def test_invalid_stored_value_is_kept_but_not_used(self):
        self.write({"format": 1, "logbook": {"history_view": "hologram", "density": "compact"}})
        prefs = self.load()
        self.assertEqual(prefs.get("logbook.history_view"), "table")
        self.assertEqual(prefs.get("logbook.density"), "compact")
        self.assertEqual(prefs.invalid_keys(), ("logbook.history_view",))
        prefs.set("logbook.density", "comfortable")
        prefs.save()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["logbook"]["history_view"], "hologram", "invalid value preserved, not discarded")

    # Unknown keys and versions ----------------------------------------------

    def test_unknown_keys_are_preserved(self):
        self.write({
            "format": 1, "future_section": {"x": [1, 2, {"y": None}]},
            "logbook": {"history_view": "combined", "future_logbook_key": "keep me"},
            "inflight": {"panels": {"modules_launcher": True, "weather.radar": False}},
        })
        prefs = self.load()
        self.assertFalse(prefs.panel_visible("inflight.panels", "weather.radar"))
        prefs.set("logbook.density", "compact")
        prefs.save()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["future_section"], {"x": [1, 2, {"y": None}]})
        self.assertEqual(payload["logbook"]["future_logbook_key"], "keep me")
        self.assertEqual(payload["logbook"]["history_view"], "combined")
        self.assertEqual(payload["inflight"]["panels"]["weather.radar"], False)
        self.assertEqual(prefs.raw()["future_section"], {"x": [1, 2, {"y": None}]})

    def test_newer_format_is_read_only(self):
        original = {"format": 99, "logbook": {"history_view": "cards"}, "new": True}
        self.write(original)
        before = self.path.read_bytes()
        prefs = self.load()
        self.assertEqual(prefs.get("logbook.history_view"), "cards")
        self.assertIn("newer BBC EFB", prefs.read_only_reason)
        self.assertFalse(prefs.writable)
        prefs.set("logbook.history_view", "table")
        self.assertEqual(prefs.get("logbook.history_view"), "table", "session-only change still applies")
        with self.assertRaises(PreferencesSaveError):
            prefs.save()
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_format_marker_is_read_only(self):
        self.write({"format": "one"})
        prefs = self.load()
        self.assertIsNotNone(prefs.problem)
        self.assertIsNotNone(prefs.read_only_reason)

    def test_non_object_json_is_not_overwritten(self):
        self.write("[1, 2, 3]")
        prefs = self.load()
        self.assertIn("JSON object", prefs.problem)
        with self.assertRaises(PreferencesSaveError):
            prefs.save()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_damaged_file_is_kept_aside(self):
        self.write("{ not json")
        prefs = self.load()
        self.assertIn("could not be read", prefs.problem)
        kept = self.path.with_name("preferences.json.damaged-20261124T100518Z")
        self.assertTrue(kept.exists())
        self.assertEqual(kept.read_text(encoding="utf-8"), "{ not json")
        self.assertFalse(self.path.exists())
        self.assertTrue(prefs.writable)
        prefs.set("logbook.density", "compact")
        prefs.save()
        self.assertEqual(self.load().get("logbook.density"), "compact")

    def test_oversized_file_is_treated_as_damaged(self):
        self.write("{}")
        with mock.patch.object(prefs_module, "MAX_PREFERENCES_BYTES", 1):
            prefs = self.load()
        self.assertIn("larger than", prefs.problem)

    def test_invalid_utf8_is_damaged(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe{}")
        prefs = self.load()
        self.assertIsNotNone(prefs.problem)

    # Atomic write -----------------------------------------------------------

    def test_failed_replace_leaves_old_file_and_no_temporary(self):
        prefs = self.load()
        prefs.set("logbook.density", "compact")
        prefs.save()
        before = self.path.read_bytes()
        prefs.set("logbook.density", "comfortable")
        with mock.patch.object(prefs_module.os, "replace", side_effect=PermissionError(13, "Access denied")):
            with self.assertRaises(PreferencesSaveError) as caught:
                prefs.save()
        self.assertIn("previous file is unchanged", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([item.name for item in self.path.parent.iterdir()], ["preferences.json"])
        self.assertTrue(prefs.dirty, "the change is still pending")

    def test_windows_sharing_violation_is_retried(self):
        prefs = self.load()
        prefs.set("logbook.density", "compact")
        real_replace = os.replace
        calls = []

        def flaky(source, target):
            calls.append(1)
            if len(calls) < 3:
                error = PermissionError(13, "sharing violation")
                error.winerror = 32
                raise error
            return real_replace(source, target)

        with mock.patch.object(prefs_module.os, "replace", side_effect=flaky), \
                mock.patch.object(prefs_module.time, "sleep"):
            prefs.save()
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.load().get("logbook.density"), "compact")

    def test_unlinked_preferences_cannot_save(self):
        prefs = Preferences(None)
        with self.assertRaises(PreferencesSaveError):
            prefs.save()

    # Listeners, reset, adopt ------------------------------------------------

    def test_listeners_and_unsubscribe(self):
        prefs = self.load()
        seen = []
        unsubscribe = prefs.subscribe(lambda key, value: seen.append((key, value)))
        prefs.subscribe(lambda key, value: 1 / 0)  # a broken listener does not block others
        with self.assertLogs("bbc_efb.preferences", "ERROR"):
            prefs.set("logbook.history_view", "combined")
        self.assertEqual(seen, [("logbook.history_view", "combined")])
        unsubscribe()
        unsubscribe()
        with self.assertLogs("bbc_efb.preferences", "ERROR"):
            prefs.set("logbook.history_view", "table")
        self.assertEqual(len(seen), 1)

    def test_reset_layout_keeps_other_and_unknown_keys(self):
        self.write({"format": 1, "flight_logging": {"automatic": False},
                    "logbook": {"history_view": "cards", "mystery": 1}, "window": {"maximized": True}})
        prefs = self.load()
        changed = prefs.reset(LAYOUT_KEYS)
        self.assertEqual(set(changed), {"logbook.history_view", "window.maximized"})
        self.assertEqual(prefs.get("logbook.history_view"), "table")
        self.assertFalse(prefs.get("flight_logging.automatic"), "not a layout key")
        prefs.save()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["logbook"], {"mystery": 1})

    def test_adopt_replaces_and_saves(self):
        prefs = self.load()
        prefs.set("logbook.density", "compact")
        prefs.save()
        seen = []
        prefs.subscribe(lambda key, value: seen.append(key))
        incoming = json.dumps({"format": 1, "logbook": {"history_view": "cards"}, "x": 1}).encode()
        changed = prefs.adopt(incoming)
        self.assertEqual(set(changed), {"logbook.history_view", "logbook.density"})
        self.assertEqual(set(seen), set(changed))
        again = self.load()
        self.assertEqual(again.get("logbook.history_view"), "cards")
        self.assertEqual(again.get("logbook.density"), "comfortable")
        self.assertEqual(again.raw()["x"], 1)
        for bad in (b"{", b"[]", json.dumps({"format": 7}).encode()):
            with self.assertRaises(PreferencesError):
                prefs.adopt(bad)

    def test_concurrent_sets_are_all_kept(self):
        prefs = self.load()
        names = [f"panel{i}" for i in range(40)]

        def work(name):
            for _ in range(5):
                prefs.set_panel_visible("inflight.panels", name, False)

        threads = [threading.Thread(target=work, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        panels = prefs.get("inflight.panels")
        self.assertTrue(all(panels[name] is False for name in names))
        prefs.save()
        self.assertEqual(len(self.load().get("inflight.panels")), len(names) + 1)


if __name__ == "__main__":
    unittest.main()
