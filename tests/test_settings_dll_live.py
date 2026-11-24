"""Settings wording for a SimConnect.dll choice that applies without a restart, and module settings from a newer EFB."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import tempfile
import unittest
from pathlib import Path

from bbc_efb.settings import coerce_diagnostics
from tests.test_settings_ui import FakeModuleHost, SettingsCase


class LiveDllTextTests(unittest.TestCase):
    def setUp(self):
        from tests.test_shell_support import qt_app

        qt_app()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.in_use = self.base / "old" / "SimConnect.dll"
        self.chosen = self.base / "new" / "SimConnect.dll"
        for path in (self.in_use, self.chosen):
            path.parent.mkdir()
            path.write_bytes(b"MZ")

    def make(self, diag):
        from bbc_efb.preferences import Preferences
        from bbc_efb.settings import SettingsPage, run_inline
        from tests.test_settings_ui import FakeDialogs, FakeFolder

        data = self.base / "Data"
        data.mkdir(exist_ok=True)
        self.dialogs = FakeDialogs()
        page = SettingsPage(preferences=Preferences.load(self.base / "preferences.json"), data_folder=FakeFolder(data),
                            diagnostics=lambda: diag, dialogs=self.dialogs, run_task=run_inline,
                            app_version="0.1.0-test")
        self.addCleanup(page.deleteLater)
        return page

    def diag(self, state):
        return {"state": state, "dll_path": str(self.in_use), "dll_found": True, "dll_live": True,
                "source": "msfs2020"}

    def test_coerce_keeps_the_live_flag_and_pending_switch(self):
        info = coerce_diagnostics({"dll_live": True, "pending_provider": "SimConnect.dll at X"})
        self.assertTrue(info.dll_live)
        self.assertEqual(info.pending_provider, "SimConnect.dll at X")
        self.assertFalse(coerce_diagnostics({}).dll_live)

    def test_new_choice_while_connected_says_when_it_applies_and_no_restart(self):
        page = self.make(self.diag("connected"))
        self.dialogs.open_files.append(self.chosen)
        page.choose_dll()
        text = page.dll_choice_label.text()
        self.assertNotIn("Restart", text)
        self.assertIn("after the current session ends", text)
        self.assertIn(f"Until then it uses {self.in_use}", text)

    def test_new_choice_while_disconnected_applies_at_the_next_attempt(self):
        page = self.make(self.diag("disconnected"))
        self.dialogs.open_files.append(self.chosen)
        page.choose_dll()
        text = page.dll_choice_label.text()
        self.assertIn("at the next connection attempt", text)
        self.assertNotIn("after the current session ends", text)
        self.assertNotIn("Restart", text)

    def test_clearing_the_choice_searches_again_without_restart(self):
        page = self.make(self.diag("disconnected"))
        page.clear_dll()
        text = page.dll_choice_label.text()
        self.assertIn("searches again at the next connection attempt", text)
        self.assertNotIn("Restart", text)

    def test_without_a_running_host_a_restart_is_still_asked_for(self):
        page = self.make({"state": "disconnected", "dll_found": False, "dll_live": False, "source": "msfs2020"})
        self.dialogs.open_files.append(self.chosen)
        page.choose_dll()
        self.assertIn("Restart BBC EFB", page.dll_choice_label.text())


class NewerModuleSettingsTests(SettingsCase):
    def test_read_only_module_settings_explain_why_even_without_a_problem_text(self):
        host = FakeModuleHost()
        host.experimental_enabled = True
        host.settings_read_only = True
        host.settings_problem = ""
        page = self.make(modules=host)
        page.show_page("modules")
        self.assertIn("written by a newer EFB", page.modules_message.text())
        self.assertFalse(page.modules_switch.isEnabled())

    def test_message_is_not_repeated_when_the_host_already_gives_it(self):
        from bbc_efb.modules.loader import NEWER_STATE_MESSAGE

        host = FakeModuleHost()
        host.settings_read_only = True
        host.settings_problem = NEWER_STATE_MESSAGE
        page = self.make(modules=host)
        page.show_page("modules")
        self.assertEqual(page.modules_message.text().count(NEWER_STATE_MESSAGE), 1)


if __name__ == "__main__":
    unittest.main()
