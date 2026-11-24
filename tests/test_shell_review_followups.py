"""Regression tests for review follow-ups in the app shell, data-folder control, logging and Settings.

* A failed Save & Exit followed by "Return to BBC EFB" starts Flight Logging again (it used
  to leave the Recorder closed for the rest of the session).
* A data-folder switch saves the old folder's pending flight into the OLD folder.
* The open-flight check runs again after the veto dialog, right before the swap.
* A retired log handler never reopens the old data folder's log file.
* Settings says the SimConnect.dll choice needs a restart.
"""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bbc_efb import logging_setup
from bbc_efb.footer import ActionResult, LoggingState
from bbc_efb.sections import DataFolderControl
from tests import test_shell_end_to_end as e2e
from tests.test_shell_support import FakeLogging, qt_app, services


@unittest.skipUnless(e2e._has(*e2e.STACK), "needs the Core host, Core bridge and Recorder")
class RecorderFollowUpTests(e2e.SimulatedFlightEndToEndTests):
    # Only the tests below; the parent's own tests run in their own module.
    test_full_simulated_flight_reaches_the_logbook_history = None
    test_save_and_exit_during_a_flight_saves_it_as_interrupted = None

    def test_return_after_failed_save_and_exit_restarts_flight_logging(self):
        from bbc_efb.shell import SAVE_AND_EXIT, STAY

        self.fly(self.airborne_at + 10)
        self.assertTrue(self.services.logging.status().flight_open)
        old_service = self.services.logging.service
        store_type = type(self.services.store)
        asked, reported = [], []
        self.shell._ask_close_while_logging = lambda current: asked.append("ask") or SAVE_AND_EXIT
        failing = mock.patch.object(store_type, "create_recorded", side_effect=OSError("disk full (test)"))

        def report(message):
            reported.append(message)
            failing.stop()  # the disk problem is fixed while the pilot reads the dialog
            return STAY

        self.shell._report_save_failure = report
        failing.start()
        closed = self.shell.close()
        self.assertFalse(closed)
        self.assertEqual(len(reported), 1)
        self.assertTrue(self.shell.isVisible())
        # A fresh Recorder runs for the same folder; the old one stays closed.
        self.assertTrue(old_service.closed)
        new_service = self.services.logging.service
        self.assertIsNotNone(new_service)
        self.assertIsNot(new_service, old_service)
        self.assertFalse(new_service.closed)
        status = self.services.logging.status()
        self.assertNotEqual(status.state, LoggingState.UNAVAILABLE, status)
        # The flight that failed to save was retried by the new Recorder (the store works again).
        flights = self.recorder_flights()
        self.assertEqual(len(flights), 1, flights)
        self.assertIsNone(flights[0].evidence.on_block)

    def test_return_while_the_store_still_fails_keeps_the_journal_and_says_so(self):
        from bbc_efb.shell import SAVE_AND_EXIT, STAY

        self.fly(self.airborne_at + 10)
        store_type = type(self.services.store)
        self.shell._ask_close_while_logging = lambda current: SAVE_AND_EXIT
        self.shell._report_save_failure = lambda message: STAY
        with mock.patch.object(store_type, "create_recorded", side_effect=OSError("disk full (test)")):
            self.assertFalse(self.shell.close())
        self.assertNotEqual(self.services.logging.status().state, LoggingState.UNAVAILABLE)
        self.assertIn("could not be saved", self.shell.notice_banner.label.text())
        self.assertEqual(self.recorder_flights(), [])
        journals = list((self.data / "recorder" / "in-progress").glob("*.jsonl"))
        self.assertEqual(len(journals), 1, journals)

    def test_folder_switch_saves_a_pending_flight_into_its_own_folder(self):
        from bbc_efb.footer import resolve_action

        self.fly(self.airborne_at + 10)
        old_store = self.services.store
        store_type = type(old_store)
        with mock.patch.object(store_type, "create_recorded", side_effect=OSError("disk full (test)")):
            outcome = resolve_action(self.services.logging.stop())
            self.assertTrue(self.wait(lambda: not self.services.logging.status().flight_open, 5), outcome)
            for _ in range(20):
                self.services.bridge.poll_recorder()
                self.wait(lambda: False, 0.02)
        status = self.services.logging.status()
        self.assertFalse(status.flight_open, status)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(self._shutdown)  # runs first: stop the new folder's Recorder before removing it
        new_folder = Path(tmp.name) / "other"
        new_store = self.shell.change_data_folder(new_folder)
        self.assertIsNotNone(new_store)
        self.assertIsNot(new_store, old_store)
        new_flights, _ = new_store.listing()
        self.assertEqual([flight for flight in new_flights if flight.source == "recorder"], [])
        # The retried save went to the folder that holds the journal.
        old_files = sorted((self.data / "flights").rglob("*.json")) if (self.data / "flights").is_dir() else []
        self.assertEqual(len(old_files), 1, old_files)


class FakeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.read_only = False
        self.closed = False

    def close(self):
        self.closed = True


class FolderSwitchOrderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.logging = FakeLogging(LoggingState.READY)
        self.old = FakeStore(self.base / "A")
        self.services = services(logging=self.logging, store=self.old)
        self.opened = []

        def open_folder(folder, create):
            store = FakeStore(folder)
            self.opened.append(store)
            return store

        self.control = DataFolderControl(self.services, open_folder=open_folder)
        self.notified = []
        self.control.listeners.append(self.notified.append)

        def veto_dialog():
            # The modal "Discard unsaved flight changes?" box is up; the Core thread opens a flight.
            self.logging.set(LoggingState.ACTIVE)
            return None  # the pilot pressed Discard

        self.control.vetoes.append(veto_dialog)

    def test_change_refuses_when_a_flight_opened_during_the_veto_dialog(self):
        with self.assertRaises(OSError) as raised:
            self.control.change(self.base / "B")
        self.assertIn("flight is being logged", str(raised.exception))
        self.assertEqual(self.notified, [])
        self.assertIs(self.services.store, self.old)
        self.assertFalse(self.old.closed)
        self.assertEqual(len(self.opened), 1)
        self.assertTrue(self.opened[0].closed)  # the folder opened for the switch is let go again

    def test_release_refuses_when_a_flight_opened_during_the_veto_dialog(self):
        with self.assertRaises(OSError):
            self.control.release()
        self.assertEqual(self.notified, [])
        self.assertIs(self.services.store, self.old)
        self.assertFalse(self.old.closed)

    def test_change_goes_ahead_when_no_flight_opened(self):
        self.control.vetoes.clear()
        store = self.control.change(self.base / "B")
        self.assertIs(self.services.store, store)
        self.assertEqual(self.notified, [store])
        self.assertTrue(self.old.closed)


class BoundSinkTests(unittest.TestCase):
    """The Recorder's sink saves into the store that Recorder was made for."""

    def test_manager_binds_each_recorder_to_its_own_store(self):
        from bbc_efb import app as app_module

        captured = []
        holder = {"store": "B"}

        def fake_create(core, store, data_dir, options, report, *, current_store=None, **kwargs):
            captured.append(current_store)
            return FakeLogging()

        manager = app_module.FlightLoggingManager(app_module.CoreSetup(), app_module.AppOptions(),
                                                  app_module.StartupReport(),
                                                  store_provider=lambda: holder["store"])
        store_a = FakeStore("/tmp/A")
        with mock.patch.object(app_module, "create_flight_logging", side_effect=fake_create):
            manager.attach(store_a)
        holder["store"] = FakeStore("/tmp/B")
        self.assertIs(captured[0](), store_a)


class ResumeAfterFailedExitTests(unittest.TestCase):
    def test_resume_only_restarts_a_closed_recorder(self):
        from bbc_efb import app as app_module

        class Service:
            closed = False

        class Adapter(FakeLogging):
            def __init__(self):
                super().__init__()
                self.service = Service()

        made = []

        def fake_create(*args, **kwargs):
            made.append(Adapter())
            return made[-1]

        manager = app_module.FlightLoggingManager(app_module.CoreSetup(), app_module.AppOptions(),
                                                  app_module.StartupReport(), store_provider=lambda: None)
        store = FakeStore("/tmp/A")
        with mock.patch.object(app_module, "create_flight_logging", side_effect=fake_create):
            manager.attach(store)
            self.assertFalse(manager.resume_after_failed_exit())
            self.assertEqual(len(made), 1)
            made[0].service.closed = True
            self.assertTrue(manager.resume_after_failed_exit())
        self.assertEqual(len(made), 2)
        self.assertEqual(made[0].shutdowns, 1)
        self.assertIs(manager.current, made[1])
        self.assertIs(manager.store, store)

    def test_shell_restarts_logging_when_the_pilot_returns(self):
        qt_app()
        from bbc_efb.shell import SAVE_AND_EXIT, STAY, Shell

        class ClosingLogging(FakeLogging):
            """Closes itself on Save & Exit, like the real Recorder, and can be resumed."""

            def __init__(self):
                super().__init__(LoggingState.ACTIVE)
                self.resumed = 0

            def save_and_exit(self, reason):
                super().save_and_exit(reason)
                self.set(LoggingState.UNAVAILABLE)
                return ActionResult(False, "disk full (test)")

            def resume_after_failed_exit(self):
                self.resumed += 1
                self.set(LoggingState.READY)
                return True

        flight_logging = ClosingLogging()
        shell = Shell(services(logging=flight_logging))
        self.addCleanup(shell.deleteLater)
        shell._ask_close_while_logging = lambda status: SAVE_AND_EXIT
        shell._report_save_failure = lambda message: STAY
        self.assertFalse(shell.close())
        self.assertEqual(flight_logging.resumed, 1)
        self.assertEqual(shell.refresh_logging_status().state, LoggingState.READY)
        shell._close_approved = True
        shell.close()


class RetiredLogHandlerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        logging_setup.reset_logging()
        self.addCleanup(logging_setup.reset_logging)
        patcher = mock.patch.object(logging_setup, "user_log_directory", return_value=self.base / "user")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_stale_emit_does_not_reopen_the_old_folder_log(self):
        folder_a, folder_b = self.base / "A", self.base / "B"
        logging_setup.configure_logging(folder_a)
        old = [h for h in logging_setup._state.handlers if getattr(h, "baseFilename", None)]
        self.assertEqual(len(old), 1)
        old_handler = old[0]
        logging_setup.configure_logging(folder_b)
        self.assertIsNone(old_handler.stream)
        size = (folder_a / "logs" / logging_setup.LOG_FILE_NAME).stat().st_size
        # A thread that already held the old handler emits after the switch.
        record = logging.LogRecord("bbc_core", logging.INFO, __file__, 1, "tick", None, None)
        old_handler.handle(record)
        self.assertIsNone(old_handler.stream)  # not reopened
        self.assertEqual((folder_a / "logs" / logging_setup.LOG_FILE_NAME).stat().st_size, size)
        self.assertEqual(logging_setup.open_files_inside(folder_a), ())
        logging.getLogger("bbc_core").warning("after the switch")
        new_log = (folder_b / "logs" / logging_setup.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("after the switch", new_log)

    def test_reset_retires_the_file_handler(self):
        logging_setup.configure_logging(self.base / "A")
        handler = [h for h in logging_setup._state.handlers if getattr(h, "baseFilename", None)][0]
        logging_setup.reset_logging()
        handler.handle(logging.LogRecord("x", logging.INFO, __file__, 1, "late", None, None))
        self.assertIsNone(handler.stream)


class DllRestartTextTests(unittest.TestCase):
    def setUp(self):
        qt_app()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def make(self, diag):
        from bbc_efb.preferences import Preferences
        from bbc_efb.settings import SettingsPage, run_inline
        from tests.test_settings_ui import FakeDialogs, FakeFolder

        data = self.base / "Data"
        data.mkdir(exist_ok=True)
        self.prefs = Preferences.load(self.base / "config" / "preferences.json")
        self.dialogs = FakeDialogs()
        page = SettingsPage(preferences=self.prefs, data_folder=FakeFolder(data), diagnostics=lambda: diag,
                            dialogs=self.dialogs, run_task=run_inline, app_version="0.1.0-test")
        self.addCleanup(page.deleteLater)
        return page

    def test_choosing_a_dll_after_none_was_found_says_restart(self):
        dll = self.base / "SimConnect.dll"
        dll.write_bytes(b"MZ")
        page = self.make({"state": "disconnected", "dll_found": False, "source": "msfs2020"})
        self.dialogs.open_files.append(dll)
        page.choose_dll()
        text = page.dll_choice_label.text()
        self.assertIn(str(dll), text)
        self.assertIn("Restart BBC EFB", text)
        self.assertIn("no SimConnect.dll", text)

    def test_choosing_the_dll_in_use_needs_no_restart(self):
        dll = self.base / "SimConnect.dll"
        dll.write_bytes(b"MZ")
        page = self.make({"state": "connected", "dll_path": str(dll), "dll_found": True, "source": "msfs2020"})
        self.dialogs.open_files.append(dll)
        page.choose_dll()
        self.assertNotIn("Restart", page.dll_choice_label.text())

    def test_clearing_the_choice_says_restart_and_the_card_no_longer_promises_the_next_connection(self):
        page = self.make({"state": "disconnected", "dll_found": False, "source": "msfs2020"})
        page.clear_dll()
        self.assertIn("Restart BBC EFB", page.dll_choice_label.text())
        from PySide6.QtWidgets import QLabel

        texts = " ".join(label.text() for label in page.findChildren(QLabel))
        self.assertNotIn("used at the next connection", texts)
        self.assertIn("restart BBC EFB after choosing it", texts)


class RememberFailureNoticeTests(unittest.TestCase):
    """Integration follow-up: a folder choice that cannot be remembered is told to the pilot."""

    def test_failed_remember_leaves_a_notice_for_the_banner(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)

        class FailingSettings:
            def remember(self, path):
                raise OSError(13, "settings.json is read-only (test)")

        svc = services(logging=FakeLogging(LoggingState.READY), store=FakeStore(base / "A"),
                       folder_settings=FailingSettings())
        control = DataFolderControl(svc, open_folder=lambda folder, create: FakeStore(folder))
        with mock.patch.object(DataFolderControl, "_relocate_logging"):
            store = control.change(base / "B")
        self.assertIs(svc.store, store)  # the switch itself still happens
        notices = svc.take_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("could not remember it", notices[0])


if __name__ == "__main__":
    unittest.main()
