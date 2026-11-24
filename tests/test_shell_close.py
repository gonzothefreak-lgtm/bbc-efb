"""Closing the EFB: Cancel or Save & Exit while a flight is open, truthful failures."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from concurrent.futures import Future
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication, QWidget

from bbc_efb import shell as shell_module
from bbc_efb.footer import ActionResult, LoggingState
from bbc_efb.sections import LOGBOOK, SectionSpec, default_sections
from bbc_efb.shell import CANCEL, CLOSE_REASON, EXIT_ANYWAY, SAVE_AND_EXIT, STAY, Shell

from tests.test_shell_support import FakeLogging, qt_app, services


class CloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def make(self, logging_, sections=None, **overrides):
        shell = Shell(services(logging=logging_, **overrides), sections=sections)
        self.asked = []
        self.failures = []
        self.addCleanup(self._dispose, shell)
        shell.show()
        QApplication.processEvents()
        return shell

    def _dispose(self, shell):
        shell._close_approved = True
        shell.close()
        shell.deleteLater()
        QApplication.processEvents()

    def answer_close(self, shell, choice):
        shell._ask_close_while_logging = lambda status: self.asked.append(status) or choice

    def answer_failure(self, shell, choice):
        shell._report_save_failure = lambda message: self.failures.append(message) or choice

    def test_close_without_open_flight_needs_no_dialog(self):
        for state in (LoggingState.READY, LoggingState.LOGGED, LoggingState.UNAVAILABLE, LoggingState.SAVE_FAILED):
            with self.subTest(state=state):
                logging_ = FakeLogging(state)
                shell = self.make(logging_)
                shell._ask_close_while_logging = lambda status: self.fail("must not ask")
                self.assertTrue(shell.close())
                self.assertEqual(logging_.called("save_and_exit"), [])
                self.assertFalse(shell.status_timer.isActive())

    def test_cancel_keeps_window_open_and_logging(self):
        for state in (LoggingState.ACTIVE, LoggingState.PAUSED, LoggingState.INTERRUPTED):
            with self.subTest(state=state):
                logging_ = FakeLogging(state, can_stop=True)
                shell = self.make(logging_)
                self.answer_close(shell, CANCEL)
                self.assertFalse(shell.close())
                self.assertTrue(shell.isVisible())
                self.assertEqual(len(self.asked), 1)
                self.assertEqual(logging_.called("save_and_exit"), [])
                self.assertEqual(logging_.called("stop"), [])
                self.assertTrue(shell.status_timer.isActive())

    def test_save_and_exit_saves_once_as_incomplete_and_closes(self):
        logging_ = FakeLogging(LoggingState.ACTIVE, can_stop=True)
        shell = self.make(logging_)
        self.answer_close(shell, SAVE_AND_EXIT)
        self.answer_failure(shell, STAY)
        self.assertTrue(shell.close())
        self.assertEqual(logging_.called("save_and_exit"), [("save_and_exit", CLOSE_REASON)])
        self.assertEqual(self.failures, [])
        self.assertFalse(shell.isVisible())
        # A second close after approval does not ask or save again.
        self.assertTrue(shell.close())
        self.assertEqual(len(logging_.called("save_and_exit")), 1)
        self.assertEqual(len(self.asked), 1)

    def test_save_failure_is_reported_and_window_stays_open(self):
        logging_ = FakeLogging(LoggingState.ACTIVE, can_stop=True)
        logging_.save_result = ActionResult(False, "The data folder is not writable.")
        shell = self.make(logging_)
        self.answer_close(shell, SAVE_AND_EXIT)
        self.answer_failure(shell, STAY)
        self.assertFalse(shell.close())
        self.assertTrue(shell.isVisible())
        self.assertEqual(self.failures, ["The data folder is not writable."])
        self.assertTrue(shell.status_timer.isActive())

    def test_save_failure_exit_anyway_closes(self):
        logging_ = FakeLogging(LoggingState.ACTIVE, can_stop=True)
        logging_.save_result = ActionResult(False, "disk full")
        shell = self.make(logging_)
        self.answer_close(shell, SAVE_AND_EXIT)
        self.answer_failure(shell, EXIT_ANYWAY)
        self.assertTrue(shell.close())
        self.assertEqual(self.failures, ["disk full"])

    def test_save_exception_is_reported(self):
        logging_ = FakeLogging(LoggingState.PAUSED, can_stop=True)
        logging_.save_result = PermissionError("Access is denied")
        shell = self.make(logging_)
        self.answer_close(shell, SAVE_AND_EXIT)
        self.answer_failure(shell, STAY)
        self.assertFalse(shell.close())
        self.assertEqual(self.failures, ["Access is denied"])

    def test_save_without_reason_still_says_so(self):
        logging_ = FakeLogging(LoggingState.ACTIVE)
        logging_.save_result = ActionResult(False, "")
        shell = self.make(logging_)
        self.answer_close(shell, SAVE_AND_EXIT)
        self.answer_failure(shell, STAY)
        self.assertFalse(shell.close())
        self.assertEqual(self.failures, ["no reason was given"])

    def test_save_future_success_and_timeout(self):
        logging_ = FakeLogging(LoggingState.ACTIVE)
        done = Future()
        done.set_result(ActionResult(True, "saved incomplete"))
        logging_.save_result = done
        shell = self.make(logging_)
        self.answer_close(shell, SAVE_AND_EXIT)
        self.answer_failure(shell, STAY)
        self.assertTrue(shell.close())

        logging_ = FakeLogging(LoggingState.ACTIVE)
        logging_.save_result = Future()  # never completes
        shell = self.make(logging_)
        self.answer_close(shell, SAVE_AND_EXIT)
        self.answer_failure(shell, STAY)
        with mock.patch.object(shell_module, "SAVE_TIMEOUT_S", 0.01):
            self.assertFalse(shell.close())
        self.assertIn("did not finish", self.failures[-1])

    def test_unsaved_logbook_edit_blocks_close_before_logging_question(self):
        class Guarded(QWidget):
            allow = False

            def can_close(self):
                return self.allow

        specs = tuple(spec for spec in default_sections() if spec.key != LOGBOOK) + (
            SectionSpec(LOGBOOK, "&Logbook", lambda services: Guarded()),)
        logging_ = FakeLogging(LoggingState.ACTIVE)
        shell = self.make(logging_, sections=specs)
        shell._ask_close_while_logging = lambda status: self.fail("must not ask yet")
        guarded = shell.show_section(LOGBOOK)
        self.assertFalse(shell.close())
        guarded.allow = True
        self.answer_close(shell, CANCEL)
        self.assertFalse(shell.close())
        self.assertEqual(len(self.asked), 1)

    def test_dialog_defaults_are_safe(self):
        """The real dialogs default to Cancel / Keep logging / Return to the EFB."""
        logging_ = FakeLogging(LoggingState.ACTIVE)
        shell = self.make(logging_)
        seen = []

        def fake_choose(icon, title, text, buttons, default, escape, informative=""):
            seen.append((title, default, escape, [key for key, _, _ in buttons], informative))
            return escape

        shell._choose = fake_choose
        self.assertEqual(shell._ask_close_while_logging(logging_.current), CANCEL)
        self.assertFalse(shell._confirm_stop(logging_.current))
        self.assertEqual(shell._report_save_failure("x"), STAY)
        (close_title, close_default, close_escape, close_keys, close_info), stop, failure = seen
        self.assertEqual((close_default, close_escape), (CANCEL, CANCEL))
        self.assertEqual(set(close_keys), {CANCEL, SAVE_AND_EXIT})
        self.assertIn("incomplete", close_info)
        self.assertIn("not marked as a completed flight", close_info)
        self.assertEqual((stop[1], stop[2]), ("keep", "keep"))
        self.assertEqual((failure[1], failure[2]), (STAY, STAY))
        self.assertIn("not in the Logbook", failure[4])


if __name__ == "__main__":
    unittest.main()
