"""The shell with the real Settings page, Core host, Core bridge and Recorder service.

Uses Black Box Core's simulated provider (development only) in a temporary data
folder. Nothing touches MSFS or SimConnect.
"""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QApplication

from bbc_efb import app as app_module
from bbc_efb.app import AppOptions
from bbc_efb.footer import LoggingState, RecorderServiceAdapter
from bbc_efb.sections import DATA_PAGE, PRIVACY_PAGE, SETTINGS, DataFolderControl, ShellServices
from bbc_efb.shell import Shell

from tests.test_shell_support import FakeLogging, connection, isolate_logging, qt_app


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    QApplication.processEvents()
    return predicate()


@unittest.skipUnless(_has("bbc_efb.settings"), "bbc_efb.settings is not in this build")
class RealSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def make(self, **values):
        values.setdefault("logging", FakeLogging())
        values.setdefault("initial_connection", connection())
        shell = Shell(ShellServices(**values))

        def dispose():
            shell._close_approved = True
            shell.close()
            shell.deleteLater()
            QApplication.processEvents()

        self.addCleanup(dispose)
        return shell

    def test_footer_opens_real_connection_diagnostics(self):
        shell = self.make()
        settings = shell.open_connection_diagnostics()
        from bbc_efb.settings import SettingsPage

        self.assertIsInstance(settings, SettingsPage)
        self.assertEqual(settings.current_page, "simconnect")
        shell.open_settings(PRIVACY_PAGE)
        self.assertEqual(settings.current_page, "flight_logging")
        shell.open_settings(DATA_PAGE)
        self.assertEqual(settings.current_page, "data_management")
        self.assertEqual(shell.current_section, SETTINGS)
        diagnostics = settings.refresh_diagnostics()
        self.assertEqual(diagnostics.state, "connected")

    def test_data_folder_change_switches_store_and_refuses_while_logging(self):
        with tempfile.TemporaryDirectory() as folder:
            logging_ = FakeLogging(LoggingState.READY)
            services = ShellServices(logging=logging_, initial_connection=connection())
            control = DataFolderControl(services)
            services.data_folder = control
            shell = self.make(**{name: getattr(services, name) for name in ("logging", "data_folder")})
            shell_services = shell.services
            changed = []
            control.services = shell_services
            control.listeners.append(changed.append)
            store = control.change(Path(folder) / "efb")
            self.addCleanup(store.close)
            self.assertIs(shell_services.store, store)
            self.assertEqual(control.current(), Path(store.root))
            self.assertEqual(changed, [store])
            logging_.set(LoggingState.ACTIVE)
            with self.assertRaises(OSError) as raised:
                control.change(Path(folder) / "other")
            self.assertIn("Stop Flight Logging", str(raised.exception))
            self.assertIs(shell_services.store, store)


@unittest.skipUnless(_has("bbc_core.host") and _has("bbc_efb.core_bridge") and _has("bbc_recorder.service"),
                     "Core host, bridge or Recorder service is not in this build")
class SimulatedStackTests(unittest.TestCase):
    """build_services(--simulated) end to end: real CoreHost thread, CoreBridge and Recorder."""

    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def test_simulated_stack_connects_labels_and_shuts_down(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        isolate_logging(self)
        with mock.patch.object(app_module, "_folder_settings", return_value=None), \
                mock.patch.object(app_module, "_load_preferences", return_value=None):
            services = app_module.build_services(AppOptions(data_dir=Path(tmp.name) / "efb", simulated=True))
        shut = []
        self.addCleanup(lambda: shut or app_module.shutdown_services(services))
        self.assertIsNotNone(services.host)
        self.assertIsNotNone(services.bridge)
        self.assertIsInstance(services.logging.current, RecorderServiceAdapter)
        shell = Shell(services)
        shell.show()
        self.assertTrue(wait_until(lambda: "Connected" in shell.footer.simconnect.text),
                        shell.footer.simconnect.text)
        self.assertIn("SIMULATED", shell.footer.simconnect.text)
        self.assertIn("SIMULATED", shell.footer.flight_logging.text)
        status = services.logging.status()
        self.assertIn(status.state, set(LoggingState))
        # Minimise: Core keeps running, the host is told the UI is inactive.
        shell.setWindowState(shell.windowState() | __import__("PySide6.QtCore").QtCore.Qt.WindowState.WindowMinimized)
        self.assertTrue(wait_until(lambda: services.host.ui_active is False))
        self.assertTrue(services.host.running)
        shell.setWindowState(__import__("PySide6.QtCore").QtCore.Qt.WindowState.WindowNoState)
        self.assertTrue(wait_until(lambda: services.host.ui_active is True))
        if status.flight_open:
            shell._ask_close_while_logging = lambda status: "save"
            shell._report_save_failure = lambda message: "exit"
        self.assertTrue(shell.close())
        app_module.shutdown_services(services)
        shut.append(True)
        self.assertFalse(services.host.running)
        shell.deleteLater()
        QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
