"""The Shell follows the module host, so a module turned on from Settings ticks at once.

Integration follow-up: ``ModuleHost.add_listener`` existed, but nothing in the Shell used it,
so a module turned on in Settings did not tick until the window was minimised and restored.
"""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import threading
import unittest

from bbc_efb.shell import Shell
from tests.test_shell_support import qt_app, services


class FakeModuleHost:
    def __init__(self):
        self.listeners = []
        self.running: tuple[str, ...] = ()
        self.ticks = 0
        self.raise_exit = False

    def add_listener(self, callback):
        if callback not in self.listeners:
            self.listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def loaded(self):
        return self.running

    def tick(self):
        self.ticks += 1
        if self.raise_exit:
            raise SystemExit(3)

    def turn_on(self, module_id="demo"):
        self.running = (module_id,)
        for callback in list(self.listeners):
            callback()


class ShellModuleListenerTests(unittest.TestCase):
    def setUp(self):
        self.app = qt_app()
        self.host = FakeModuleHost()
        self.services = services(module_host=self.host)
        self.shell = Shell(self.services)
        self.addCleanup(self.shell.deleteLater)

    def test_module_turned_on_after_start_up_starts_the_timer(self):
        self.assertFalse(self.shell.module_timer.isActive())
        self.assertEqual(len(self.host.listeners), 1)
        self.host.turn_on()
        self.app.processEvents()
        self.assertTrue(self.shell.module_timer.isActive())
        self.shell.tick_modules()
        self.assertEqual(self.host.ticks, 1)

    def test_change_reported_from_another_thread_reaches_the_ui_thread(self):
        worker = threading.Thread(target=self.host.turn_on)
        worker.start()
        worker.join()
        for _ in range(5):
            self.app.processEvents()
        self.assertTrue(self.shell.module_timer.isActive())

    def test_folder_switch_follows_the_new_module_host(self):
        new_host = FakeModuleHost()
        self.services.module_host = new_host
        self.shell.on_store_changed(None)
        self.assertEqual(self.host.listeners, [])
        self.assertEqual(len(new_host.listeners), 1)
        new_host.turn_on()
        self.app.processEvents()
        self.assertTrue(self.shell.module_timer.isActive())

    def test_system_exit_from_a_module_tick_does_not_close_the_efb(self):
        self.host.raise_exit = True
        self.host.turn_on()
        with self.assertLogs("bbc_efb.shell", level="ERROR"):
            self.shell.tick_modules()  # must not raise SystemExit
        self.assertEqual(self.host.ticks, 1)


if __name__ == "__main__":
    unittest.main()
