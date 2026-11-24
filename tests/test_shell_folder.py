"""One store per app (C1): DataFolderControl, logging that follows the folder, Flight Logging that
follows the folder, and the shell's handling of the Logbook's ``data_folder_requested``."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from bbc_efb import app as app_module
from bbc_efb import logging_setup
from bbc_efb.app import AppOptions, CoreSetup, FlightLoggingManager, StartupReport
from bbc_efb.footer import ActionResult, LoggingState, UnavailableFlightLogging
from bbc_efb.sections import LOGBOOK, DataFolderControl, LogbookSection, SectionSpec, ShellServices
from bbc_efb.shell import UNSAVED_EDITS_REASON, Shell
from bbc_efb.storage_settings import open_data_folder

from tests.test_shell_support import FakeLogging, isolate_logging, qt_app, services as make_services


class FolderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.per_user = isolate_logging(self)

    def open(self, name):
        store = open_data_folder(self.root / name, create=True)
        self.addCleanup(store.close)
        return store


class DataFolderControlTests(FolderCase):
    def test_choosing_the_open_folder_again_reuses_the_store(self):
        store = self.open("one")
        services = ShellServices(logging=FakeLogging(), store=store)
        control = DataFolderControl(services, open_folder=mock.Mock(side_effect=AssertionError("opened twice")))
        seen = []
        control.listeners.append(seen.append)
        self.assertIs(control.change(self.root / "one"), store)
        self.assertIs(services.store, store)
        self.assertEqual(seen, [store])  # views still refresh

    def test_vetoes_and_open_flights_stop_a_switch(self):
        store = self.open("one")
        logging_ = FakeLogging()
        services = ShellServices(logging=logging_, store=store)
        control = DataFolderControl(services)
        control.vetoes.append(lambda: "Unsaved edits.")
        with self.assertRaisesRegex(OSError, "Unsaved edits"):
            control.change(self.root / "two")
        with self.assertRaisesRegex(OSError, "Unsaved edits"):
            control.release()
        self.assertIs(services.store, store)
        self.assertFalse((self.root / "two").exists())
        control.vetoes.clear()
        logging_.set(LoggingState.ACTIVE)
        with self.assertRaisesRegex(OSError, "Stop Flight Logging"):
            control.change(self.root / "two")

    def test_logs_follow_the_folder_and_leave_it_on_release(self):
        logging_setup.configure_logging(None, "INFO")
        store = self.open("one")
        services = ShellServices(logging=FakeLogging(), store=store)
        control = DataFolderControl(services)
        new = control.change(self.root / "two")
        self.addCleanup(new.close)
        self.assertEqual(logging_setup.current_configuration().log_file, self.root / "two" / "logs" / "bbc-efb.log")
        self.assertEqual(services.logging_config.log_file, self.root / "two" / "logs" / "bbc-efb.log")
        self.assertTrue(store.read_only or not store.writable)  # the old store was closed
        logging.getLogger("bbc_efb.test").warning("written into the new folder")
        control.release()
        self.assertIsNone(services.store)
        self.assertEqual(logging_setup.open_files_inside(self.root / "two"), ())
        self.assertEqual(logging_setup.current_configuration().log_file, self.per_user / "bbc-efb.log")
        # A restore into the folder that was in use can now rename it (Windows refuses with open files).
        (self.root / "two").rename(self.root / "two.pre-restore")
        text = (self.root / "two.pre-restore" / "logs" / "bbc-efb.log").read_text(encoding="utf-8")
        self.assertIn("written into the new folder", text)


class FakeService:
    """A Recorder service with the C3 constructor; records what the app gave it."""

    instances: list["FakeService"] = []

    def __init__(self, api, sink, data_dir, rules=None, clock=None):
        self.api, self.sink, self.data_dir = api, sink, data_dir
        self.closed = 0
        self.recovered = 0
        FakeService.instances.append(self)

    def tick(self):
        pass

    def recover_unfinished(self):
        self.recovered += 1
        return []

    def status(self):
        return None

    def close(self):
        self.closed += 1


class FakeHost:
    def __init__(self):
        self.ticks = []
        self.released = []
        self.registered = []

    def register_consumer(self, *, name=None, control_groups=(), diagnostics=False):
        self.registered.append((name, tuple(control_groups), diagnostics))
        return f"api-{len(self.registered)}"

    def release(self, name):
        self.released.append(name)
        return True

    def add_tick(self, callback):
        self.ticks.append(callback)
        return lambda: self.ticks.remove(callback)


class FlightLoggingManagerTests(FolderCase):
    def setUp(self):
        super().setUp()
        FakeService.instances = []
        patcher = mock.patch.object(app_module, "_optional", side_effect=self._optional)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _optional(module, *names, _real=app_module._optional):
        if module == "bbc_recorder.service" and names[0] == "RecorderService":
            return FakeService
        return _real(module, *names)

    def test_follows_the_data_folder_through_the_control(self):
        host = FakeHost()
        services = ShellServices()
        report = StartupReport()
        manager = FlightLoggingManager(CoreSetup(host=host), AppOptions(), report,
                                       store_provider=lambda: services.store)
        services.logging = manager
        manager.attach(None)
        self.assertEqual(manager.status().state, LoggingState.UNAVAILABLE)
        self.assertEqual(FakeService.instances, [])
        control = DataFolderControl(services)
        control.listeners.append(manager.attach)
        first = control.change(self.root / "one")
        self.addCleanup(first.close)
        service = FakeService.instances[-1]
        self.assertEqual(service.data_dir, Path(first.root))
        self.assertEqual(service.recovered, 1)
        self.assertEqual(host.registered, [("flight-recorder", (), False)])  # read-only handle
        self.assertEqual(host.ticks, [service.tick])
        self.assertIs(service.sink.__class__.__name__, "LogbookSink")
        second = control.change(self.root / "two")
        self.addCleanup(second.close)
        self.assertEqual(service.closed, 1)
        self.assertEqual(len(host.ticks), 1)
        self.assertEqual(FakeService.instances[-1].data_dir, Path(second.root))
        self.assertIn("flight-recorder", host.released)
        control.release()
        self.assertEqual(FakeService.instances[-1].closed, 1)
        self.assertEqual(host.ticks, [])
        self.assertIsInstance(manager.current, UnavailableFlightLogging)

    def test_read_only_folder_never_logs(self):
        class ReadOnly:
            read_only = True
            root = "/somewhere"

        manager = FlightLoggingManager(CoreSetup(host=FakeHost()), AppOptions(), StartupReport(),
                                       store_provider=lambda: None)
        manager.attach(ReadOnly())
        self.assertIn("read-only", manager.status().detail)
        self.assertEqual(FakeService.instances, [])


class StartupLoggingTests(FolderCase):
    def test_start_up_problems_reach_the_per_user_log_and_the_data_folder_log(self):
        with mock.patch.object(app_module, "_folder_settings", side_effect=OSError("APPDATA is read-only")), \
                mock.patch.object(app_module, "_load_preferences", return_value=None), \
                mock.patch.object(app_module, "find_dll", return_value=None):
            services = app_module.build_services(AppOptions(data_dir=self.root / "efb"))
        self.addCleanup(app_module.shutdown_services, services)
        for handler in logging.getLogger().handlers:
            handler.flush()
        early = (self.per_user / "bbc-efb.log").read_text(encoding="utf-8")
        self.assertIn("Settings storage is not available: APPDATA is read-only", early)
        data_log = (self.root / "efb" / "logs" / "bbc-efb.log").read_text(encoding="utf-8")
        self.assertIn("Start-up (before the data folder was known): Settings storage is not available", data_log)
        self.assertIn("without a simulator connection", data_log)
        self.assertEqual(services.logging_config.log_file, self.root / "efb" / "logs" / "bbc-efb.log")
        self.assertIsInstance(services.data_folder, DataFolderControl)

    def test_a_replacement_provider_needs_simulated(self):
        with self.assertRaises(ValueError):
            app_module.create_core(AppOptions(), StartupReport(), provider_factory=lambda: object())


class FakeLogbook(QWidget):
    """A Logbook with the C1 surface: set_store, can_switch_store, data_folder_requested."""

    data_folder_requested = Signal()

    def __init__(self):
        super().__init__()
        self.store = None
        self.stores = []
        self.statuses = []
        self.dirty = False
        self.allow = True
        self.reloads = 0

    def set_store(self, store):
        self.store = store
        self.stores.append(store)
        return True

    def reload(self, force=False):
        self.reloads += 1

    def can_switch_store(self):
        return not self.dirty or self.allow

    def can_leave_editor(self):
        return self.can_switch_store()

    def set_recorder_status(self, status):
        self.statuses.append(status)


class ShellDataFolderTests(FolderCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def make(self, logbook):
        services = make_services()

        def build(_services):
            if _services.store is not None:
                logbook.set_store(_services.store)
            return LogbookSection(logbook)

        from bbc_efb.sections import default_sections

        specs = tuple(SectionSpec(spec.key, spec.title, build, spec.shortcut, spec.tab, spec.description)
                      if spec.key == LOGBOOK else spec for spec in default_sections())
        shell = Shell(services, sections=specs)
        self.addCleanup(self._dispose, shell)
        return shell

    @staticmethod
    def _dispose(shell):
        shell._close_approved = True
        shell.close()
        shell.deleteLater()
        QApplication.processEvents()

    def test_logbook_request_switches_the_app_store(self):
        logbook = FakeLogbook()
        shell = self.make(logbook)
        shell.show_section(LOGBOOK)
        self.assertTrue(logbook.statuses)  # the Black Box page gets the live status at once
        shell._pick_folder = lambda: self.root / "chosen"
        logbook.data_folder_requested.emit()
        store = shell.services.store
        self.assertIsNotNone(store)
        self.addCleanup(store.close)
        self.assertEqual(Path(store.root), self.root / "chosen")
        self.assertIs(logbook.store, store)  # the Logbook shows the app's store, not one of its own
        # Asking again for the same folder reuses it instead of tripping over our own writer lock.
        warnings = []
        shell._warn = lambda title, text: warnings.append(text)
        logbook.data_folder_requested.emit()
        self.assertIs(shell.services.store, store)
        self.assertEqual(warnings, [])

    def test_unsaved_logbook_edits_veto_the_switch(self):
        logbook = FakeLogbook()
        shell = self.make(logbook)
        shell.show_section(LOGBOOK)
        logbook.dirty, logbook.allow = True, False
        warnings = []
        shell._warn = lambda title, text: warnings.append(text)
        shell._pick_folder = lambda: self.root / "chosen"
        self.assertIsNone(shell.request_data_folder())
        self.assertIsNone(shell.services.store)
        self.assertEqual(warnings, [UNSAVED_EDITS_REASON])
        self.assertFalse((self.root / "chosen").exists())

    def test_cancelled_pick_changes_nothing(self):
        shell = self.make(FakeLogbook())
        shell._pick_folder = lambda: None
        self.assertIsNone(shell.request_data_folder())
        self.assertIsNone(shell.services.store)

    def test_recorder_status_reaches_footer_and_logbook_and_saved_flights_reload(self):
        from bbc_efb.footer import LoggingStatus

        logbook = FakeLogbook()
        shell = self.make(logbook)
        shell.show_section(LOGBOOK)
        active = LoggingStatus(LoggingState.ACTIVE, "Airborne.", simulated=True)
        shell.on_recorder_status(active)
        self.assertIn("Logging Active (SIMULATED)", shell.footer.flight_logging.text)
        self.assertIs(logbook.statuses[-1], active)
        reloads = logbook.reloads
        shell.on_recorder_status(LoggingStatus(LoggingState.LOGGED))
        self.assertEqual(logbook.reloads, reloads + 1)
        self.assertNotIn("SimConnect: Connected", shell.footer.simconnect.text)  # never merged

    def test_notices_are_shown_once(self):
        services = make_services()
        services.notices.append("Recovered 1 flight.")
        shell = Shell(services)
        self.addCleanup(self._dispose, shell)
        self.assertEqual(shell.notice_banner.notices, ["Recovered 1 flight."])
        self.assertEqual(services.take_notices(), ())


class ModuleWiringTests(FolderCase):
    def test_module_host_is_off_by_default_and_needs_a_writable_folder(self):
        store = self.open("one")

        class Bridge:
            def api(self, name, control_groups=(), diagnostics=False):
                raise AssertionError("no module may be loaded while the switch is off")

            def release(self, name):
                return False

        bridge = Bridge()
        report = StartupReport()
        host = app_module.create_module_host(store, bridge, report)
        if host is None:
            self.skipTest("bbc_efb.modules is not in this build")
        self.assertFalse(host.experimental_enabled)
        self.assertEqual(host.loaded(), ())
        self.assertIsNone(app_module.create_module_host(None, bridge, report))
        self.assertIsNone(app_module.create_module_host(store, None, report))


class ShutdownTests(unittest.TestCase):
    def test_modules_close_before_core_stops(self):
        order = []

        class Part:
            def __init__(self, name):
                self.name = name

            def stop(self):
                order.append(self.name)

            def close(self):
                order.append(self.name)

        class Logging(FakeLogging):
            def shutdown(self):
                order.append("recorder")

        services = ShellServices(logging=Logging(), bridge=Part("bridge"), host=Part("host"),
                                 module_host=Part("modules"))
        app_module.shutdown_services(services)
        self.assertEqual(order, ["recorder", "modules", "bridge", "host"])

    def test_save_result_from_the_manager(self):
        class Current:
            def save_and_exit(self, reason):
                return ActionResult(True, reason)

        manager = FlightLoggingManager(CoreSetup(), AppOptions(), StartupReport(), store_provider=lambda: None)
        manager.current = Current()
        self.assertEqual(manager.save_and_exit("bye"), ActionResult(True, "bye"))

    def test_settings_automatic_switch_reaches_the_recorder(self):
        calls = []

        class Current(UnavailableFlightLogging):
            def set_automatic(self, enabled):
                calls.append(enabled)

        manager = FlightLoggingManager(CoreSetup(), AppOptions(), StartupReport(), store_provider=lambda: None)
        manager.current = Current()
        shell_services = ShellServices(logging=manager)
        qt_app()
        shell = Shell(shell_services)
        self.addCleanup(shell.deleteLater)
        shell.set_automatic_logging(False)
        self.assertEqual(calls, [False])


if __name__ == "__main__":
    unittest.main()
