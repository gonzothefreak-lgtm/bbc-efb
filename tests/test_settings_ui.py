"""Offscreen tests for bbc_efb/settings.py (SettingsPage and choose_data_folder)."""

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bbc_efb import data_management as dm  # noqa: E402
from bbc_efb import settings as settings_module  # noqa: E402
from bbc_efb.preferences import Preferences  # noqa: E402
from bbc_efb.settings import (  # noqa: E402
    ConnectionDiagnostics, SettingsPage, choose_data_folder, coerce_diagnostics, format_response_times, run_inline,
)

APP = QApplication.instance() or QApplication([])


class FakeDialogs:
    """Scripted answers; every call is recorded."""

    def __init__(self):
        self.folders = []
        self.save_files = []
        self.open_files = []
        self.confirms = []
        self.prompts = []
        self.calls = []

    def _next(self, queue, kind, *args):
        self.calls.append((kind, *args))
        if not queue:
            raise AssertionError(f"Unexpected dialog {kind}: {args}")
        return queue.pop(0)

    def choose_folder(self, title, start=None):
        return self._next(self.folders, "folder", title)

    def choose_save_file(self, title, start, name_filter):
        return self._next(self.save_files, "save", title)

    def choose_open_file(self, title, start, name_filter):
        return self._next(self.open_files, "open", title)

    def confirm(self, title, text, accept="OK", reject="Cancel", default_accept=False):
        return self._next(self.confirms, "confirm", title, text)

    def first_folder_prompt(self, text, suggested):
        return self._next(self.prompts, "prompt", text)

    def inform(self, title, text):
        self.calls.append(("inform", title, text))

    def warn(self, title, text):
        self.calls.append(("warn", title, text))

    def kinds(self):
        return [call[0] for call in self.calls]

    def texts(self, kind):
        return [" ".join(str(part) for part in call[1:]) for call in self.calls if call[0] == kind]


class FakeFolder:
    def __init__(self, path=None, *, fail=None):
        self.path = path
        self.changes = []
        self.released = 0
        self.fail = fail

    def current(self):
        return self.path

    def change(self, folder):
        if self.fail:
            raise OSError(self.fail)
        self.changes.append(Path(folder))
        self.path = Path(folder)

    def release(self):
        self.released += 1


def make_flight(folder: Path, name="a", arrival="LFPG"):
    flights = folder / "flights"
    flights.mkdir(parents=True, exist_ok=True)
    record = {"schema": 2, "id": name, "source": "manual", "revisions": [{
        "saved_at": "2026-11-24T10:00:00.000+00:00", "by": "pilot",
        "fields": {"flight_date": "2026-11-24", "aircraft": "A320", "departure": "EGLL", "arrival": arrival}}]}
    (flights / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


def spin(condition, timeout_ms=3000):
    loop_until = datetime.now().timestamp() + timeout_ms / 1000
    while not condition() and datetime.now().timestamp() < loop_until:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
    return condition()


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        patcher = mock.patch.object(dm, "_temporary_root", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data = self.base / "Data"
        make_flight(self.data)
        self.prefs_path = self.base / "config" / "preferences.json"
        self.prefs = Preferences.load(self.prefs_path)
        self.folder = FakeFolder(self.data)
        self.dialogs = FakeDialogs()
        self.diag = {"state": "connected", "detail": "Connected to MSFS 2020.", "source": "msfs2020",
                     "session_id": "s-42", "since": datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc),
                     "dll_path": str(self.base / "SimConnect.dll"), "dll_found": True,
                     "last_errors": [(datetime(2026, 11, 24, 9, 1, tzinfo=timezone.utc), "Exception 7")]}
        self.measured = (0.012, 0.001, 0.002, 0.008, 0.001)
        self.page = None

    def tearDown(self):
        if self.page is not None:
            self.page.close()
            self.page.deleteLater()
            QCoreApplication.processEvents()
        self._tmp.cleanup()

    def make(self, **kwargs):
        kwargs.setdefault("preferences", self.prefs)
        kwargs.setdefault("data_folder", self.folder)
        kwargs.setdefault("diagnostics", lambda: self.diag)
        kwargs.setdefault("measure_response_times", lambda: self.measured)
        kwargs.setdefault("dialogs", self.dialogs)
        kwargs.setdefault("run_task", run_inline)
        kwargs.setdefault("app_version", "0.1.0-test")
        self.page = SettingsPage(**kwargs)
        self.page.resize(900, 700)
        return self.page

    def saved(self):
        return json.loads(self.prefs_path.read_text(encoding="utf-8"))


class NavigationTests(SettingsCase):
    def test_pages_and_footer_destination(self):
        page = self.make()
        self.assertEqual(page.navigation.count(), 5)
        self.assertEqual([page.navigation.item(i).text() for i in range(5)],
                         ["SimConnect", "Flight Logging & Privacy", "Data Management", "Layout",
                          "Modules (Experimental)"])
        seen = []
        page.page_changed.connect(seen.append)
        page.show_page("layout")
        self.assertEqual(page.current_page, "layout")
        page.open_connection_diagnostics()
        self.assertEqual(page.current_page, "simconnect")
        page.open_privacy()
        self.assertEqual(seen, ["layout", "simconnect", "flight_logging"])
        with self.assertRaises(KeyError):
            page.show_page("nope")

    def test_last_page_is_remembered(self):
        self.make().show_page("data_management")
        self.assertEqual(self.saved()["settings"]["last_page"], "data_management")
        self.page.close()
        again = SettingsPage(Preferences.load(self.prefs_path), self.folder, dialogs=self.dialogs,
                             run_task=run_inline)
        self.assertEqual(again.current_page, "data_management")
        again.deleteLater()

    def test_works_with_no_dependencies(self):
        page = self.make(preferences=None, data_folder=None, diagnostics=None, measure_response_times=None)
        self.assertEqual(page.diag_fields["state"].text(), "Unknown (Black Box Core is not running)")
        self.assertIn("not connected to Settings", page.diag_fields["detail"].text())
        self.assertFalse(page.measure_button.isEnabled())
        self.assertFalse(page.automatic_check.isEnabled())
        self.assertFalse(page.export_button.isEnabled())
        self.assertFalse(page.change_folder_button.isEnabled())
        self.assertIn("No data folder", page.folder_label.text())
        self.assertIn("No data folder is chosen", page.privacy_location.text())

    def test_plain_callable_folder_is_read_only(self):
        page = self.make(data_folder=lambda: self.data)
        self.assertEqual(page.folder_label.text(), str(self.data))
        self.assertFalse(page.change_folder_button.isEnabled())
        self.assertTrue(page.export_button.isEnabled())


class DiagnosticsTests(SettingsCase):
    def test_fields_are_shown(self):
        page = self.make()
        fields = page.diag_fields
        self.assertEqual(fields["state"].text(), "Connected")
        self.assertEqual(fields["detail"].text(), "Connected to MSFS 2020.")
        self.assertEqual(fields["session"].text(), "s-42")
        self.assertEqual(fields["since"].text(), "2026-11-24 09:00:00 UTC")
        self.assertEqual(fields["source"].text(), "MSFS 2020 (SimConnect)")
        self.assertIn("SimConnect.dll (found)", fields["dll"].text())
        self.assertIn("2026-11-24 09:01:00 UTC  Exception 7", page.errors_view.toPlainText())

    def test_unknown_values_are_labelled(self):
        self.diag = {"state": "disconnected"}
        page = self.make()
        self.assertEqual(page.diag_fields["state"].text(), "Not connected")
        self.assertEqual(page.diag_fields["session"].text(), "No session")
        self.assertEqual(page.diag_fields["dll"].text(), "Not available")
        self.assertEqual(page.diag_fields["since"].text(), "Not available")
        self.assertEqual(page.errors_view.toPlainText(), "No errors reported.")
        self.assertFalse(page.measure_button.isEnabled(), "only while connected")
        self.diag = {"state": "error", "dll_found": False, "dll_candidates": ["C:/MSFS SDK/SimConnect SDK/lib"]}
        page.refresh_diagnostics()
        self.assertIn("was not found", page.diag_fields["dll"].text())
        self.assertIn("C:/MSFS SDK", page.diag_fields["dll"].text())

    def test_provider_failure_is_shown(self):
        def broken():
            raise RuntimeError("host stopped")
        with self.assertLogs("bbc_efb.settings", "WARNING"):
            page = self.make(diagnostics=broken)
        self.assertIn("host stopped", page.diag_fields["detail"].text())
        self.assertEqual(page.diag_fields["state"].text(), "Unknown (Black Box Core is not running)")

    def test_measure_response_times(self):
        page = self.make()
        page.measure()
        text = page.response_label.text()
        self.assertIn("Total round trip: 12.0 ms", text)
        self.assertIn("Simulator answer until received here: 1.0 ms", text)
        self.assertIn("Measured", text)
        page.refresh_diagnostics()
        self.assertIn("12.0 ms", page.response_label.text(), "kept after refresh")

    def test_measure_failure(self):
        def failing():
            raise RuntimeError("The selected provider does not support response timing.")
        page = self.make(measure_response_times=failing)
        page.measure()
        self.assertIn("could not be measured", page.response_label.text())
        self.assertTrue(page.measure_button.isEnabled())
        page = self.make(measure_response_times=lambda: ())
        page.measure()
        self.assertIn("no response times", page.response_label.text())

    def test_measure_in_worker_thread(self):
        threads = []

        def measure():
            threads.append(threading.current_thread())
            return self.measured

        page = self.make(measure_response_times=measure, run_task=None)
        page.measure()
        self.assertTrue(spin(lambda: "12.0 ms" in page.response_label.text()))
        self.assertIsNot(threads[0], threading.main_thread())

    def test_refresh_timer_runs_only_while_visible_on_simconnect(self):
        page = self.make()
        self.assertFalse(page.diagnostics_refreshing)
        page.show()
        QCoreApplication.processEvents()
        self.assertTrue(page.diagnostics_refreshing)
        page.show_page("layout")
        self.assertFalse(page.diagnostics_refreshing)
        page.show_page("simconnect")
        self.assertTrue(page.diagnostics_refreshing)
        page.hide()
        self.assertFalse(page.diagnostics_refreshing)

    def test_connection_changed_slot_refreshes(self):
        page = self.make()
        self.diag = dict(self.diag, state="connecting")
        page.on_connection_changed(object())
        self.assertEqual(page.diag_fields["state"].text(), "Connecting")

    def test_choose_and_clear_dll(self):
        dll = self.base / "SimConnect.dll"
        dll.write_bytes(b"MZ")
        wrong = self.base / "other.dll"
        wrong.write_bytes(b"MZ")
        page = self.make()
        self.dialogs.open_files += [wrong, dll]
        page.choose_dll()
        self.assertIn("warn", self.dialogs.kinds())
        self.assertIsNone(self.prefs.get("simconnect.dll_path"))
        page.choose_dll()
        self.assertEqual(self.saved()["simconnect"]["dll_path"], str(dll))
        self.assertIn(str(dll), page.dll_choice_label.text())
        page.clear_dll()
        self.assertIsNone(self.saved()["simconnect"]["dll_path"])


class CoerceTests(unittest.TestCase):
    def test_contract_connection(self):
        from bbc_core.contract import Connection, ConnectionState, Source
        moment = datetime(2026, 11, 24, tzinfo=timezone.utc)
        info = coerce_diagnostics(Connection(ConnectionState.CONNECTED, Source("msfs2020", False), "abc", moment, "ok"))
        self.assertEqual(info.state, "connected")
        self.assertEqual(info.session_id, "abc")
        self.assertEqual(info.since, moment)
        self.assertEqual(info.detail, "ok")
        self.assertEqual(info.source, "msfs2020")
        simulated = coerce_diagnostics(Connection(ConnectionState.ERROR, Source("simulated-x", True), None, moment))
        self.assertEqual((simulated.state, simulated.source, simulated.session_id), ("error", "simulated", None))

    def test_nested_connection_and_extras(self):
        class Holder:
            connection = {"state": "connected", "session_id": "z"}
            response_times = (0.5,)
            last_errors = "only one"
            dll_path = "/does/not/exist/SimConnect.dll"
        info = coerce_diagnostics(Holder())
        self.assertEqual(info.state, "connected")
        self.assertEqual(info.session_id, "z")
        self.assertEqual(info.response_times_s, (0.5,))
        self.assertEqual(info.last_errors, ("only one",))
        self.assertFalse(info.dll_found)
        self.assertEqual(coerce_diagnostics(None), ConnectionDiagnostics())

    def test_format_response_times(self):
        self.assertEqual(format_response_times(None), ())
        rows = format_response_times([0.001, "x", -1, 0.002, 0.003, 0.004])
        self.assertEqual(rows[0], ("Total round trip", "1.0 ms"))
        self.assertEqual(rows[1][1], "Not available")
        self.assertEqual(rows[2][1], "Not available")
        self.assertEqual(rows[5][0], "Value 6")


class FlightLoggingTests(SettingsCase):
    def test_automatic_logging_toggle_is_saved_and_signalled(self):
        page = self.make()
        signalled = []
        page.automatic_logging_changed.connect(signalled.append)
        self.assertTrue(page.automatic_check.isChecked())
        page.automatic_check.setChecked(False)
        self.assertFalse(self.saved()["flight_logging"]["automatic"])
        self.assertEqual(signalled, [False])

    def test_retention(self):
        page = self.make()
        self.assertEqual(page.retention_spin.text(), "Until I delete them")
        page.retention_spin.setValue(30)
        self.assertEqual(self.saved()["flight_logging"]["journal_retention_days"], 30)
        page.retention_spin.setValue(0)
        self.assertIsNone(self.saved()["flight_logging"]["journal_retention_days"])

    def test_privacy_information(self):
        page = self.make()
        location = page.privacy_location.text()
        self.assertIn(str(self.data), location)
        self.assertIn(str(self.data / "flights"), location)
        self.assertIn("stays on this computer", location)
        self.assertIn(str(self.prefs_path.parent), location)
        texts = " ".join(label.text() for label in page.findChildren(settings_module.QLabel))
        self.assertIn("sends nothing over the internet", texts)
        self.assertIn("Planned inventory", texts)
        self.assertIn("No audio, video", texts)
        self.assertIn("never deletes flights automatically", texts)
        self.assertIn("no Delete Flight command", texts)

    def test_recorder_can_supply_exact_inventory(self):
        page = self.make(recorded_items=("Only this.",))
        texts = " ".join(label.text() for label in page.findChildren(settings_module.QLabel))
        self.assertNotIn("Planned inventory", texts)
        self.assertIn("Only this.", page.recorded_label.text())

    def test_read_only_preferences_are_explained(self):
        self.prefs_path.parent.mkdir(parents=True)
        self.prefs_path.write_text(json.dumps({"format": 9}), encoding="utf-8")
        self.prefs = Preferences.load(self.prefs_path)
        page = self.make()
        self.assertIn("newer BBC EFB", page.layout_status.text())
        page.automatic_check.setChecked(False)
        self.assertFalse(self.prefs.get("flight_logging.automatic"), "applies for this session")
        self.assertEqual(json.loads(self.prefs_path.read_text())["format"], 9, "file untouched")

    def test_save_failure_is_reported(self):
        page = self.make()
        with mock.patch.object(self.prefs, "save", side_effect=OSError("disk full")):
            page.automatic_check.setChecked(False)
        self.assertIn("could not be saved", page.logging_status.text())
        self.assertFalse(page.logging_status.isHidden())


class LayoutTests(SettingsCase):
    def test_layout_preferences_saved(self):
        page = self.make()
        page.show_page("layout")
        page.history_view_combo.setCurrentIndex(page.history_view_combo.findData("combined"))
        page.logbook_density_combo.setCurrentIndex(page.logbook_density_combo.findData("compact"))
        page.inflight_density_combo.setCurrentIndex(page.inflight_density_combo.findData("compact"))
        page.panel_checks[("logbook.dashboard_panels", "charts")].setChecked(False)
        page.launcher_check.setChecked(True)
        page.maximized_check.setChecked(True)
        saved = self.saved()
        self.assertEqual(saved["logbook"]["history_view"], "combined")
        self.assertEqual(saved["logbook"]["density"], "compact")
        self.assertEqual(saved["inflight"]["density"], "compact")
        self.assertFalse(saved["logbook"]["dashboard_panels"]["charts"])
        self.assertTrue(saved["logbook"]["dashboard_panels"]["recent_flights"])
        self.assertTrue(saved["inflight"]["launcher_collapsed"])
        self.assertTrue(saved["window"]["maximized"])

    def test_external_change_updates_controls(self):
        page = self.make()
        self.prefs.set("logbook.history_view", "cards")
        self.assertTrue(spin(lambda: page.history_view_combo.currentData() == "cards"))

    def test_reset_requires_confirmation(self):
        page = self.make()
        page.maximized_check.setChecked(True)
        page.automatic_check.setChecked(False)
        self.dialogs.confirms += [False, True]
        page.reset_layout()
        self.assertTrue(self.saved()["window"]["maximized"])
        page.reset_layout()
        self.assertNotIn("maximized", self.saved().get("window", {}))
        self.assertFalse(page.maximized_check.isChecked())
        self.assertFalse(self.saved()["flight_logging"]["automatic"], "reset is layout only")

    def test_unknown_panels_survive_ui_edits(self):
        self.prefs.set("logbook.dashboard_panels", {"charts": True, "future.widget": False})
        page = self.make()
        page.panel_checks[("logbook.dashboard_panels", "charts")].setChecked(False)
        self.assertEqual(self.saved()["logbook"]["dashboard_panels"], {"charts": False, "future.widget": False})


    def test_invalid_stored_panel_entry_survives_a_toggle(self):
        """Integration follow-up: the toggle changes one entry, so entries this build rejects are kept."""
        self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
        self.prefs_path.write_text(json.dumps({"format": 1, "logbook": {"dashboard_panels": {
            "charts": True, "recent_flights": "maybe", "future.widget": False}}}), encoding="utf-8")
        self.prefs = Preferences.load(self.prefs_path)
        page = self.make()
        page.panel_checks[("logbook.dashboard_panels", "charts")].setChecked(False)
        self.assertEqual(self.saved()["logbook"]["dashboard_panels"],
                         {"charts": False, "recent_flights": "maybe", "future.widget": False})

class DataManagementUiTests(SettingsCase):
    def test_folder_summary(self):
        page = self.make()
        page.show_page("data_management")
        self.assertEqual(page.folder_label.text(), str(self.data))
        self.assertIn("1 flight file(s)", page.folder_summary.text())
        self.assertTrue(page.export_button.isEnabled())

    def test_export_all_data(self):
        page = self.make()
        destination = self.base / "out" / "My Export"
        self.dialogs.save_files.append(destination)
        page.export_all()
        result = destination.with_name("My Export.zip")
        self.assertTrue(zipfile.is_zipfile(result))
        self.assertIn("Exported 1 flight(s)", page.data_status.text())
        self.assertIn("inform", self.dialogs.kinds())
        # Existing file: declined, then accepted.
        self.dialogs.save_files += [result, result]
        self.dialogs.confirms += [False, True]
        before = result.stat().st_mtime_ns
        page.export_all()
        self.assertEqual(result.stat().st_mtime_ns, before)
        page.export_all()
        self.assertFalse(page.busy)

    def test_export_inside_data_folder_is_refused(self):
        page = self.make()
        self.dialogs.save_files.append(self.data / "x.zip")
        page.export_all()
        self.assertIn("Export failed", page.data_status.text())
        self.assertIn("outside the data folder", self.dialogs.texts("warn")[0])

    def test_backup_includes_preferences_and_restore_round_trip(self):
        self.prefs.set("logbook.history_view", "cards")
        self.prefs.save()
        page = self.make()
        backup = self.base / "out" / "b.zip"
        self.dialogs.save_files.append(backup)
        page.create_backup()
        self.assertIn("Backup saved", page.data_status.text())
        with zipfile.ZipFile(backup) as archive:
            self.assertIn("config/preferences.json", archive.namelist())
        # Change preferences, then restore into an empty folder, switch to it and take the backup prefs.
        self.prefs.set("logbook.history_view", "table")
        self.prefs.save()
        target = self.base / "Restored"
        target.mkdir()
        self.dialogs.open_files.append(backup)
        self.dialogs.confirms += [True, True, True]  # restore?, switch folder?, adopt prefs?
        self.dialogs.folders.append(target)
        page.restore_backup()
        self.assertTrue((target / "flights" / "a.json").is_file())
        self.assertEqual(self.folder.changes, [target])
        self.assertEqual(self.prefs.get("logbook.history_view"), "cards")
        self.assertIn("Restored 1 flight(s)", page.data_status.text())
        self.assertEqual(page.folder_label.text(), str(target))

    def test_restore_rejects_bad_backup(self):
        page = self.make()
        junk = self.base / "junk.zip"
        junk.write_bytes(b"nope")
        self.dialogs.open_files.append(junk)
        page.restore_backup()
        self.assertIn("cannot be restored", page.data_status.text())
        self.assertEqual(self.dialogs.kinds(), ["open", "warn"])

    def test_restore_into_current_folder_sets_old_data_aside(self):
        backup = dm.create_backup(self.data, self.base / "b.zip", app_version="x").path
        make_flight(self.data, "b")
        page = self.make()
        self.dialogs.open_files.append(backup)
        self.dialogs.confirms += [True, False, True]  # restore?, (not empty) declined
        self.dialogs.folders.append(self.data)
        page.restore_backup()
        self.assertTrue((self.data / "flights" / "b.json").exists(), "declined: nothing changed")
        self.assertEqual(self.folder.released, 0)
        self.dialogs.open_files.append(backup)
        self.dialogs.folders.append(self.data)
        self.dialogs.confirms += [True]
        page.restore_backup()
        self.assertEqual(self.folder.released, 1)
        self.assertEqual(self.folder.changes, [self.data], "reopened after restore")
        self.assertFalse((self.data / "flights" / "b.json").exists())
        kept = [p for p in self.base.iterdir() if p.name.startswith("Data.pre-restore-")]
        self.assertEqual(len(kept), 1)
        self.assertTrue((kept[0] / "flights" / "b.json").exists())

    def test_restore_into_current_folder_without_release_is_refused(self):
        class NoRelease:
            def __init__(self, path):
                self.path = path

            def current(self):
                return self.path

            def change(self, folder):
                self.path = folder

        folder = NoRelease(self.data)
        backup = dm.create_backup(self.data, self.base / "b.zip", app_version="x").path
        page = self.make(data_folder=folder)
        self.dialogs.open_files.append(backup)
        self.dialogs.confirms += [True]
        self.dialogs.folders.append(self.data)
        page.restore_backup()
        self.assertEqual(self.dialogs.confirms, [], "refused before asking to set data aside")
        self.assertIn("cannot be replaced while BBC EFB has it open", self.dialogs.texts("warn")[0])

    def test_change_folder(self):
        page = self.make()
        new = self.base / "New Data"
        self.dialogs.folders.append(new)
        emitted = []
        page.data_folder_changed.connect(emitted.append)
        page.change_folder()
        self.assertEqual(self.folder.changes, [new])
        self.assertTrue(new.is_dir())
        self.assertEqual(emitted, [new])
        self.assertIn("Data folder changed", page.data_status.text())
        # Same folder again: no change.
        self.dialogs.folders.append(new)
        page.change_folder()
        self.assertIn("already the data folder", page.data_status.text())

    def test_change_folder_failure_is_reported(self):
        self.folder.fail = "Another BBC EFB already uses this folder."
        page = self.make()
        self.dialogs.folders.append(self.base / "Other")
        page.change_folder()
        self.assertIn("already uses", self.dialogs.texts("warn")[0])
        self.assertEqual(page.folder_label.text(), str(self.data))

    def test_background_task_runner(self):
        page = self.make(run_task=None)
        destination = self.base / "out" / "bg.zip"
        self.dialogs.save_files.append(destination)
        page.export_all()
        self.assertTrue(page.busy)
        self.assertFalse(page.export_button.isEnabled())
        self.assertTrue(spin(lambda: not page.busy))
        self.assertTrue(destination.exists())
        self.assertTrue(page.export_button.isEnabled())


class ChooseDataFolderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        patcher = mock.patch.object(dm, "_temporary_root", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dialogs = FakeDialogs()

    def tearDown(self):
        self._tmp.cleanup()

    def test_suggested_folder_is_created(self):
        suggested = self.base / "Documents" / "BBC EFB Data"
        self.dialogs.prompts.append("suggested")
        self.assertEqual(choose_data_folder(dialogs=self.dialogs, suggested=suggested), suggested)
        self.assertTrue(suggested.is_dir())
        self.assertIn(str(suggested), self.dialogs.texts("prompt")[0])

    def test_cancel(self):
        self.dialogs.prompts.append(None)
        self.assertIsNone(choose_data_folder(dialogs=self.dialogs, suggested=self.base / "x"))
        self.assertFalse((self.base / "x").exists())

    def test_choose_cancel_then_unusable_then_good(self):
        bad = self.base / "file.txt"
        bad.write_text("x")
        good = self.base / "Good"
        self.dialogs.prompts += ["choose", "choose", "choose"]
        self.dialogs.folders += [None, bad, good]
        self.assertEqual(choose_data_folder(dialogs=self.dialogs, suggested=self.base / "s"), good)
        self.assertEqual(len(self.dialogs.texts("warn")), 1)
        self.assertIn("file, not a folder", self.dialogs.texts("warn")[0])

    def test_notes_need_confirmation_and_declined_folder_is_removed(self):
        risky = self.base / "OneDrive" / "Logbook"
        self.dialogs.prompts += ["choose", "choose"]
        self.dialogs.folders += [risky, risky]
        self.dialogs.confirms += [False, True]
        self.assertEqual(choose_data_folder(dialogs=self.dialogs, suggested=self.base / "s"), risky)
        self.assertIn("OneDrive", self.dialogs.texts("confirm")[0])

    def test_declined_new_folder_is_not_left_behind(self):
        risky = self.base / "OneDrive" / "Logbook"
        self.dialogs.prompts += ["choose", None]
        self.dialogs.folders += [risky]
        self.dialogs.confirms += [False]
        self.assertIsNone(choose_data_folder(dialogs=self.dialogs, suggested=self.base / "s"))
        self.assertFalse(risky.exists())

    def test_settings_variant_has_no_prompt(self):
        target = self.base / "T"
        self.dialogs.folders.append(target)
        self.assertEqual(choose_data_folder(dialogs=self.dialogs, first_launch=False), target)
        self.dialogs.folders.append(None)
        self.assertIsNone(choose_data_folder(dialogs=self.dialogs, first_launch=False))
        self.assertNotIn("prompt", self.dialogs.kinds())

    def test_default_suggestion_uses_documents(self):
        self.assertEqual(settings_module.suggested_data_folder().name, "BBC EFB Data")


class FakeModuleHost:
    """The parts of ``bbc_efb.modules.ModuleHost`` the Modules page uses."""

    def __init__(self):
        from types import SimpleNamespace

        self.experimental_enabled = False
        self.calls = []
        self.settings_problem = ""
        self._records = {
            "radio": SimpleNamespace(key="radio", name="Radio panel", builtin=False, enabled=False,
                                     summary="Off", status="disabled", warnings=(),
                                     requested_groups=("radios", "simvar_writes"), granted_groups=(),
                                     missing_groups=("radios", "simvar_writes")),
        }

    def feature_message(self):
        return "Drop-in modules are on." if self.experimental_enabled else "Drop-in modules are off."

    def set_experimental_enabled(self, value):
        self.calls.append(("switch", value))
        self.experimental_enabled = value

    def scan(self):
        self.calls.append(("scan",))
        return tuple(self._records.values())

    def load_enabled(self):
        self.calls.append(("load_enabled",))

    def records(self):
        return tuple(self._records.values())

    def record(self, key):
        return self._records.get(key)

    def ignored(self):
        return ((Path("/data/modules/broken.zip"), "Zip files are not supported yet."),)

    def set_enabled(self, key, value):
        self.calls.append(("enabled", key, value))
        self._records[key].enabled = value

    def load(self, key):
        self.calls.append(("load", key))

    def unload(self, key):
        self.calls.append(("unload", key))

    def grant(self, key, groups):
        self.calls.append(("grant", key, tuple(groups)))
        self._records[key].missing_groups = ()

    def reload(self, key):
        self.calls.append(("reload", key))


class ModulesPageTests(SettingsCase):
    def test_without_a_data_folder_the_page_says_so(self):
        page = self.make(modules=lambda: None)
        page.show_page("modules")
        self.assertFalse(page.modules_switch.isEnabled())
        self.assertIn("need an open", page.modules_message.text())

    def test_switch_needs_the_trust_notice_and_is_off_by_default(self):
        host = FakeModuleHost()
        page = self.make(modules=lambda: host)
        page.show_page("modules")
        self.assertFalse(page.modules_switch.isChecked())
        self.assertIn("Zip files are not supported", page.modules_ignored.text())
        self.assertIn("Radio panel", page.module_rows["radio"]["frame"].findChildren(
            settings_module.QLabel)[0].text())
        self.dialogs.confirms.append(False)
        page.modules_switch.setChecked(True)
        self.assertEqual(host.calls, [])
        self.assertFalse(page.modules_switch.isChecked())
        kind, title, text = self.dialogs.calls[-1]
        self.assertEqual(kind, "confirm")
        self.assertIn("same access to your computer", text)
        self.dialogs.confirms.append(True)
        page.modules_switch.setChecked(True)
        self.assertEqual(host.calls, [("switch", True), ("scan",), ("load_enabled",)])
        self.assertTrue(page.modules_switch.isChecked())
        page.modules_switch.setChecked(False)
        self.assertEqual(host.calls[-1], ("switch", False))

    def test_grants_show_warnings_and_modules_switch_on_and_off(self):
        host = FakeModuleHost()
        host.experimental_enabled = True
        page = self.make(modules=host)
        page.show_page("modules")
        self.dialogs.confirms.append(True)
        self.assertTrue(page.grant_module("radio"))
        _, _, text = self.dialogs.calls[-1]
        self.assertIn("simvar_writes", text)
        self.assertIn("WARNING: Broad access", text)
        self.assertIn(("grant", "radio", ("radios", "simvar_writes")), host.calls)
        page.module_rows["radio"]["enabled"].setChecked(True)
        self.assertIn(("enabled", "radio", True), host.calls)
        self.assertEqual(host.calls[-1], ("load", "radio"))
        page.set_module_enabled("radio", False)
        self.assertEqual(host.calls[-1], ("unload", "radio"))


    def test_settings_from_a_newer_efb_are_shown_and_cannot_be_changed(self):
        """Integration follow-up: a read-only module-state.json disables the switches up front."""
        host = FakeModuleHost()
        host.experimental_enabled = True
        host.settings_read_only = True
        host.settings_problem = "Module settings were written by a newer EFB."
        page = self.make(modules=host)
        page.show_page("modules")
        self.assertIn("newer EFB", page.modules_message.text())
        self.assertFalse(page.modules_switch.isEnabled())
        self.assertFalse(page.module_rows["radio"]["enabled"].isEnabled())
        self.assertFalse(page.module_rows["radio"]["grant"].isEnabled())

if __name__ == "__main__":
    unittest.main()
