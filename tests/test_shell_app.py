"""Start-up: command line, data folder, Core host choice (never silently simulated), shutdown."""

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

from bbc_core.contract import ConnectionState
from bbc_efb import app as app_module
from bbc_efb.__main__ import build_parser, parse_options
from bbc_efb.app import AppOptions, StartupReport, create_core, create_flight_logging, resolve_store
from bbc_efb.footer import LoggingState, RecorderServiceAdapter, UnavailableFlightLogging
from bbc_efb.sections import ShellServices

from tests.test_shell_support import FakeBridge, FakeLogging, isolate_logging, qt_app


def optional_without(*blocked_modules, extra=None):
    """Patch ``app._optional`` so the named modules look absent (or return ``extra`` entries).

    ``extra`` keys are ``"module"`` or ``"module:Name"`` (the first name looked up).
    """
    real = app_module._optional
    extra = extra or {}

    def fake(module, *names):
        specific = f"{module}:{names[0]}" if names else module
        if specific in extra:
            return extra[specific]
        if module in extra:
            return extra[module]
        if module in blocked_modules:
            return None
        return real(module, *names)

    return mock.patch.object(app_module, "_optional", side_effect=fake)


class CommandLineTests(unittest.TestCase):
    def test_defaults(self):
        options = parse_options([])
        self.assertEqual(options, AppOptions())
        self.assertFalse(options.simulated)

    def test_flags(self):
        options = parse_options(["--data-dir", "/tmp/x", "--simulated", "--log-level", "debug",
                                 "--simconnect-dll", "/tmp/SimConnect.dll"])
        self.assertEqual(options.data_dir, Path("/tmp/x"))
        self.assertTrue(options.simulated)
        self.assertEqual(options.log_level, "DEBUG")
        self.assertEqual(options.simconnect_dll, Path("/tmp/SimConnect.dll"))

    def test_bad_level_rejected(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            build_parser().parse_args(["--log-level", "loud"])

    def test_main_runs_app(self):
        from bbc_efb import __main__ as main_module

        with mock.patch("bbc_efb.app.run", return_value=0) as run:
            self.assertEqual(main_module.main(["--simulated"]), 0)
        self.assertTrue(run.call_args.args[0].simulated)


class CoreSetupTests(unittest.TestCase):
    def test_no_dll_runs_disconnected_and_never_simulated(self):
        report = StartupReport()
        with mock.patch.object(app_module, "find_dll", return_value=None), \
                mock.patch.object(app_module, "_simulated_provider_factory",
                                  side_effect=AssertionError("simulated provider must not be used")):
            setup = create_core(AppOptions(), report)
        self.assertIsNone(setup.host)
        self.assertIsNone(setup.bridge)
        connection = setup.initial_connection
        self.assertEqual(connection.state, ConnectionState.DISCONNECTED)
        self.assertFalse(connection.source.simulated)
        self.assertIn("without a simulator connection", connection.detail)
        self.assertTrue(report.notes)

    def test_simulated_only_with_flag_and_labelled(self):
        report = StartupReport()
        with optional_without("bbc_core.host"):
            setup = create_core(AppOptions(simulated=True), report)
        self.assertTrue(setup.initial_connection.source.simulated)
        self.assertIn("SIMULATED", report.notes[0])
        self.assertTrue(any("host is not part of this build" in note for note in report.notes))

    def test_host_and_bridge_constructed_and_started(self):
        created = {}

        class Host:
            def __init__(self, provider):
                created["factory"] = lambda: provider
                self.started = False

            def start(self):
                self.started = True

        class Bridge(FakeBridge):
            def __init__(self, host):
                super().__init__()
                self.host = host

        qt_app()
        report = StartupReport()
        with optional_without(extra={"bbc_core.host": Host, "bbc_efb.core_bridge": Bridge}):
            setup = create_core(AppOptions(simulated=True), report)
        self.assertIsInstance(setup.host, Host)
        self.assertTrue(setup.host.started)
        self.assertIs(setup.bridge.host, setup.host)
        provider = created["factory"]()
        self.assertTrue(provider.source.simulated)

    def test_found_dll_uses_simconnect_provider_factory(self):
        captured = {}

        class Host:
            def __init__(self, provider, *, config=None, core=None):
                captured["provider"] = provider
                captured["core"] = core

        dll = Path("C:/MSFS SDK/SimConnect SDK/lib/SimConnect.dll")
        sentinel = object()
        with mock.patch.object(app_module, "find_dll", return_value=dll), \
                mock.patch.object(app_module, "_simconnect_provider_factory", return_value=lambda: sentinel) as factory, \
                optional_without("bbc_efb.core_bridge", extra={"bbc_core.host": Host}):
            setup = create_core(AppOptions(), StartupReport())
        factory.assert_called_once_with(dll)
        self.assertIs(captured["provider"], sentinel)
        self.assertIsNone(captured["core"])
        self.assertEqual(setup.dll_path, dll)

    def test_host_failure_is_honest(self):
        class Host:
            def __init__(self, provider):
                raise OSError("SimConnect.dll is the wrong architecture")

        report = StartupReport()
        with mock.patch.object(app_module, "find_dll", return_value=Path("C:/x/SimConnect.dll")), \
                optional_without(extra={"bbc_core.host": Host}), \
                self.assertLogs("bbc_efb.app", level=logging.ERROR):
            setup = create_core(AppOptions(), report)
        self.assertIsNone(setup.host)
        self.assertIn("wrong architecture", setup.initial_connection.detail)
        self.assertFalse(setup.initial_connection.source.simulated)


class DataFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def settings(self):
        from bbc_efb.storage_settings import DataFolderSettings

        return DataFolderSettings(self.root / "config" / "settings.json")

    def test_command_line_folder_is_created_and_not_remembered(self):
        folder_settings = self.settings()
        report = StartupReport()
        store = resolve_store(AppOptions(data_dir=self.root / "data" / "nested"), folder_settings, report)
        self.assertIsNotNone(store)
        self.addCleanup(store.close)
        self.assertTrue((self.root / "data" / "nested").is_dir())
        self.assertIsNone(folder_settings.load())

    def test_missing_remembered_folder_is_reported_not_recreated(self):
        folder_settings = self.settings()
        (self.root / "gone").mkdir()
        folder_settings.remember(self.root / "gone")
        (self.root / "gone").rmdir()
        report = StartupReport()
        store = resolve_store(AppOptions(), folder_settings, report)
        self.assertIsNone(store)
        self.assertFalse((self.root / "gone").exists())
        self.assertTrue(any("not available" in note for note in report.notes))

    def test_first_launch_without_prompt(self):
        report = StartupReport()
        store = resolve_store(AppOptions(ask_for_data_folder=False), self.settings(), report)
        self.assertIsNone(store)
        self.assertIn("No data folder has been chosen yet.", report.notes)

    def test_first_launch_delegates_to_settings_helper(self):
        chosen = object()
        calls = []

        def helper(parent=None, *, first_launch=True):
            calls.append((parent, first_launch))
            return self.root / "chosen"

        folder_settings = self.settings()
        with optional_without(extra={"bbc_efb.settings": helper}):
            store = resolve_store(AppOptions(), folder_settings, StartupReport(), parent="window")
        self.addCleanup(store.close)
        self.assertEqual(Path(store.root), self.root / "chosen")
        self.assertEqual(calls, [("window", True)])
        self.assertEqual(folder_settings.load(), self.root / "chosen")

    def test_first_launch_cancelled(self):
        with optional_without(extra={"bbc_efb.settings": lambda parent=None: None}):
            report = StartupReport()
            self.assertIsNone(resolve_store(AppOptions(), self.settings(), report))
        self.assertTrue(any("No data folder was chosen" in note for note in report.notes))

    def test_second_window_opens_read_only(self):
        first = resolve_store(AppOptions(data_dir=self.root / "shared"), self.settings(), StartupReport())
        self.addCleanup(first.close)
        report = StartupReport()
        second = resolve_store(AppOptions(data_dir=self.root / "shared"), self.settings(), report)
        if second is None:
            self.skipTest("this platform's writer lock does not exclude a second store in one process")
        self.addCleanup(second.close)
        self.assertTrue(second.read_only)
        self.assertTrue(any("read-only" in note for note in report.notes))

    def test_first_launch_falls_back_to_basic_prompt(self):
        with optional_without("bbc_efb.settings"), \
                mock.patch.object(app_module, "basic_first_launch_prompt", return_value="store") as prompt:
            self.assertEqual(resolve_store(AppOptions(), self.settings(), StartupReport()), "store")
        prompt.assert_called_once()


class RecorderSetupTests(unittest.TestCase):
    def test_no_store_means_unavailable_with_reason(self):
        logging_ = create_flight_logging(app_module.CoreSetup(), None, None, AppOptions(), StartupReport())
        self.assertIsInstance(logging_, UnavailableFlightLogging)
        self.assertIn("data folder", logging_.status().detail)

    def test_read_only_store_means_unavailable(self):
        class Store:
            read_only = True

        logging_ = create_flight_logging(app_module.CoreSetup(), Store(), None, AppOptions(), StartupReport())
        self.assertIn("read-only", logging_.status().detail)

    def test_missing_recorder_module(self):
        with optional_without("bbc_recorder.service"):
            logging_ = create_flight_logging(app_module.CoreSetup(host=object()), object(), None, AppOptions(),
                                             StartupReport())
        self.assertEqual(logging_.status().state, LoggingState.UNAVAILABLE)
        self.assertIn("not available in this build", logging_.status().detail)

    def test_no_host_means_unavailable(self):
        logging_ = create_flight_logging(app_module.CoreSetup(), object(), None, AppOptions(), StartupReport())
        self.assertIn("Black Box Core is not running", logging_.status().detail)

    def test_recorder_service_constructed_ticked_and_adapted(self):
        class Host:
            def __init__(self):
                self.names = []
                self.listeners = []
                self.removed = 0

            def register_consumer(self, *, name=None, control_groups=(), diagnostics=False, queue_size=256):
                self.names.append(name)
                self.queue_sizes = getattr(self, "queue_sizes", []) + [queue_size]
                return "consumer-api"

            def add_pump_listener(self, fn):
                self.listeners.append(fn)

                def remove():
                    self.removed += 1
                return remove

        class RecorderService:  # the C3 constructor shape
            def __init__(self, api, sink, data_dir, rules=None, clock=None, *, app_version=None):
                self.args = (api, data_dir, sink)
                self.app_version = app_version
                self.phase = "idle"
                self.automatic = None
                self.closed = 0

            def tick(self):
                pass

            def set_automatic(self, enabled):
                self.automatic = enabled

            def shutdown(self, reason="x"):
                self.closed += 1

        class Preferences:
            def get(self, key):
                return {"flight_logging.automatic": False}[key]

        class Store:
            def __init__(self):
                self.saved = []

            def save_recorded_flight(self, record):
                self.saved.append(record)
                return "id-1"

        host = Host()
        store = Store()
        current = {"store": store}
        with optional_without("bbc_efb.recorder_sink",
                              extra={"bbc_recorder.service:RecorderService": RecorderService}):
            logging_ = create_flight_logging(app_module.CoreSetup(host=host), store, Path("/data"),
                                             AppOptions(), StartupReport(), current_store=lambda: current["store"],
                                             preferences=Preferences())
        self.assertIsInstance(logging_, RecorderServiceAdapter)
        api, data_dir, sink = logging_.service.args
        self.assertEqual(api, "consumer-api")
        self.assertEqual(host.names, ["flight-recorder"])
        self.assertEqual(host.queue_sizes, [app_module.RECORDER_QUEUE_SIZE])
        self.assertEqual(app_module.RECORDER_QUEUE_SIZE, 1024)
        self.assertEqual(data_dir, Path("/data"))
        self.assertEqual(logging_.service.app_version, app_module.VERSION)
        self.assertEqual(host.listeners, [logging_.service.tick])
        self.assertFalse(logging_.service.automatic)
        self.assertEqual(sink.save_recorded_flight({"a": 1}), "id-1")
        current["store"] = None
        with self.assertRaises(OSError):
            sink.save_recorded_flight({"a": 2})
        self.assertEqual(logging_.status().state, LoggingState.READY)
        logging_.shutdown()
        logging_.shutdown()
        self.assertEqual(host.removed, 1)
        self.assertEqual(logging_.service.closed, 1)

    def test_response_time_measurer_blocks_for_the_result(self):
        from concurrent.futures import Future

        class Engine:
            def response_times(self):
                return (0.1, 0.2, 0.3, 0.4, 0.5)

        class RunHost:
            def run(self, fn, timeout=None):
                self.timeout = timeout
                return fn(Engine())

        class CallHost:  # C4: call() returns a Future
            def call(self, fn):
                future = Future()
                future.set_result(fn(Engine()))
                return future

        run_host = RunHost()
        self.assertEqual(app_module.response_time_measurer(run_host)(), (0.1, 0.2, 0.3, 0.4, 0.5))
        self.assertEqual(run_host.timeout, app_module.RESPONSE_TIMES_TIMEOUT_S)
        self.assertEqual(app_module.response_time_measurer(CallHost())(), (0.1, 0.2, 0.3, 0.4, 0.5))
        self.assertIsNone(app_module.response_time_measurer(None))
        self.assertIsNone(app_module.response_time_measurer(object()))

    def test_sink_without_storage_support_says_so(self):
        sink = app_module.LogbookSink(lambda: object())
        with self.assertRaises(OSError) as raised:
            sink.save_recorded_flight({})
        self.assertIn("journal is kept", str(raised.exception))

    def test_recorder_failure_is_reported(self):
        class RecorderService:
            def __init__(self, api=None, *, sink=None):
                raise RuntimeError("journal folder locked")

        class Host:
            def api(self, name):
                return None

        report = StartupReport()
        with optional_without(extra={"bbc_recorder.service:RecorderService": RecorderService,
                                     "bbc_recorder.service:DataFolderJournals": None}), \
                self.assertLogs("bbc_efb.app", level=logging.ERROR):
            logging_ = create_flight_logging(app_module.CoreSetup(host=Host()), object(), None, AppOptions(),
                                             report)
        self.assertIn("journal folder locked", logging_.status().detail)


class ShutdownTests(unittest.TestCase):
    def test_everything_is_attempted_in_order(self):
        order = []

        class Part:
            def __init__(self, name, fail=False):
                self.name, self.fail = name, fail

            def stop(self):
                order.append(self.name)
                if self.fail:
                    raise RuntimeError("stop failed")

            def close(self):
                order.append(self.name + " closed")

        class Logging(FakeLogging):
            def shutdown(self):
                order.append("recorder")

        services = ShellServices(logging=Logging(), bridge=Part("bridge", fail=True), host=Part("host"),
                                 store=Part("store"))
        logbook_store = Part("logbook store")
        with self.assertLogs("bbc_efb.app", level=logging.ERROR):
            app_module.shutdown_services(services, logbook_store=logbook_store)
        self.assertEqual(order, ["recorder", "bridge", "host", "logbook store closed", "store closed"])

    def test_same_store_closed_once(self):
        closed = []

        class Store:
            def close(self):
                closed.append(1)

        store = Store()
        app_module.shutdown_services(ShellServices(store=store), logbook_store=store)
        self.assertEqual(closed, [1])


class BuildServicesTests(unittest.TestCase):
    def test_build_services_with_data_dir_and_no_dll(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        isolate_logging(self)
        with mock.patch.object(app_module, "find_dll", return_value=None), \
                mock.patch.object(app_module, "_folder_settings", return_value=None), \
                mock.patch.object(app_module, "_load_preferences", return_value=None), \
                optional_without("bbc_recorder.service"):
            services = app_module.build_services(AppOptions(data_dir=Path(tmp.name) / "efb"))
        self.addCleanup(app_module.shutdown_services, services)
        self.assertIsNotNone(services.store)
        self.assertEqual(services.data_dir, Path(tmp.name) / "efb")
        self.assertIsNone(services.host)
        self.assertFalse(services.simulated)
        self.assertEqual(services.initial_connection.state, ConnectionState.DISCONNECTED)
        self.assertTrue(any("without a simulator connection" in note for note in services.startup_notes))
        self.assertEqual(services.logging.status().state, LoggingState.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
