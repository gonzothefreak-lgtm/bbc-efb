"""A SimConnect.dll chosen in Settings is used from the next connect attempt, without a restart.

Also covers the related honesty fixes: the Core bridge names a waiting switch, a folder
choice that cannot be remembered at start-up reaches the pilot's notice banner, and a
remember failure other than OSError is still reported.
"""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bbc_core.host import CoreHost, HostConfig, HostState
from bbc_efb import app as app_module
from bbc_efb.app import AppOptions, SimConnectDllSwitch, StartupReport, create_core
from bbc_efb.core_bridge import CoreBridge
from bbc_efb.preferences import Preferences
from bbc_efb.sections import DataFolderControl, ShellServices, _dll_facts

from tests.test_host_core import FAST, ThreadRecordingProvider, wait_until
from tests.test_shell_core_bridge import FakeHost, Status, disconnected
from tests.test_shell_review_followups import FakeStore
from tests.test_shell_support import FakeLogging, qt_app, services


class Tagged(ThreadRecordingProvider):
    def __init__(self, dll):
        super().__init__(minimum_interval_ms=100)
        self.dll = dll


class RecordingHost:
    def __init__(self):
        self.requests = []

    def replace_provider_factory(self, factory, *, description=""):
        self.requests.append((factory, description))


class DllSwitchUnitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.old = self.base / "old" / "SimConnect.dll"
        self.new = self.base / "new" / "SimConnect.dll"
        for path in (self.old, self.new):
            path.parent.mkdir()
            path.write_bytes(b"MZ")
        self.prefs = Preferences.load(self.base / "preferences.json")
        self.host = RecordingHost()
        self.switch = SimConnectDllSwitch(AppOptions(), self.prefs, self.host, self.old.resolve())

    def factories(self):
        return mock.patch.object(app_module, "_simconnect_provider_factory", side_effect=lambda dll: lambda: Tagged(dll))

    def test_same_file_asks_for_nothing(self):
        self.prefs.set("simconnect.dll_path", str(self.old))
        self.switch.check()
        self.assertEqual(self.host.requests, [])

    def test_new_choice_is_handed_over_once_and_reported_when_built(self):
        changed = []
        self.switch.on_change = changed.append
        self.prefs.set("simconnect.dll_path", str(self.new))
        with self.factories():
            self.switch.check()
            self.switch.check()  # the same choice is not handed over twice
        self.assertEqual(len(self.host.requests), 1)
        factory, description = self.host.requests[0]
        self.assertIn(str(self.new.resolve()), description)
        self.assertEqual(changed, [])  # nothing is in use until the host builds it
        provider = factory()
        self.assertEqual(provider.dll, self.new.resolve())
        self.assertEqual(changed, [self.new.resolve()])
        self.assertEqual(self.switch.in_use, self.new.resolve())

    def test_going_back_to_the_file_in_use_withdraws_the_request(self):
        self.prefs.set("simconnect.dll_path", str(self.new))
        with self.factories():
            self.switch.check()
        self.prefs.set("simconnect.dll_path", str(self.old))
        self.switch.check()
        self.assertEqual(self.host.requests[-1], (None, ""))
        self.assertIsNone(self.switch.requested)

    def test_nothing_found_keeps_the_current_file(self):
        with mock.patch.object(app_module, "find_dll", return_value=None):
            self.switch.check()
        self.assertEqual(self.host.requests, [])

    def test_unreadable_choice_is_logged_not_raised(self):
        with mock.patch.object(app_module, "find_dll", side_effect=RuntimeError("broken")), \
                self.assertLogs("bbc_efb.app", level="ERROR"):
            self.switch.check()
        self.assertEqual(self.host.requests, [])


class DllSwitchWithRealHostTests(unittest.TestCase):
    """create_core wires the switch into a real CoreHost; a new choice applies at the next attempt."""

    def test_choice_made_while_running_is_used_after_reconnect(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        old, new = base / "a" / "SimConnect.dll", base / "b" / "SimConnect.dll"
        for path in (old, new):
            path.parent.mkdir()
            path.write_bytes(b"MZ")
        prefs = Preferences.load(base / "preferences.json")
        prefs.set("simconnect.dll_path", str(old))
        built = []

        def factory_for(dll):
            def build():
                provider = Tagged(dll)
                built.append(provider)
                return provider
            return build

        def host_with_fast_config(provider_factory, provider):
            return CoreHost(provider, provider_factory=provider_factory, config=HostConfig(**FAST))

        # Both stay patched for the whole test: the switch builds the new provider later, on the Core thread.
        for patcher in (
                mock.patch.object(app_module, "_simconnect_provider_factory", side_effect=factory_for),
                mock.patch.object(app_module, "_optional", side_effect=lambda module, name: (
                    host_with_fast_config if name == "CoreHost" else None if name == "CoreBridge"
                    else getattr(__import__(module, fromlist=[name]), name, None)))):
            patcher.start()
            self.addCleanup(patcher.stop)
        setup = create_core(AppOptions(), StartupReport(), prefs)
        host = setup.host
        self.addCleanup(host.stop)
        self.assertIsNotNone(setup.dll_switch)
        self.assertEqual(setup.dll_path, old.resolve())
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertEqual(host.provider.dll, old.resolve())

        changed = []
        setup.dll_switch.on_change = changed.append
        prefs.set("simconnect.dll_path", str(new))
        # A connected session is not interrupted by the new choice.
        self.assertFalse(wait_until(lambda: host.provider.dll != old.resolve(), timeout=0.3))
        host.disconnect().result(2)
        host.connect_now().result(2)
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED
                                   and host.provider.dll == new.resolve()))
        self.assertEqual(changed, [new.resolve()])
        self.assertEqual([provider.dll for provider in built], [old.resolve(), new.resolve()])
        self.assertIsNone(host.pending_provider)

    def test_simulated_runs_have_no_switch(self):
        with mock.patch.object(app_module, "_optional", side_effect=lambda module, name: (
                (lambda provider_factory, provider: CoreHost(provider, provider_factory=provider_factory,
                                                             config=HostConfig(**FAST)))
                if name == "CoreHost" else None)):
            setup = create_core(AppOptions(simulated=True), StartupReport())
        self.addCleanup(setup.host.stop)
        self.assertIsNone(setup.dll_switch)


class DiagnosticsFactsTests(unittest.TestCase):
    def test_dll_facts_say_whether_a_new_choice_applies_without_restart(self):
        svc = ShellServices(dll_path=Path("C:/SDK/SimConnect.dll"))
        self.assertFalse(_dll_facts(svc)["dll_live"])
        svc.dll_switch = object()
        self.assertTrue(_dll_facts(svc)["dll_live"])

    def test_bridge_names_a_waiting_switch(self):
        qt_app()
        host = FakeHost(Status("connected", disconnected(), "Connected."))
        host.pending_provider = "SimConnect.dll at D:\\New\\SimConnect.dll"
        bridge = CoreBridge(host, autostart=False)
        self.addCleanup(bridge.close)
        facts = bridge.diagnostics()
        self.assertEqual(facts["pending_provider"], host.pending_provider)
        self.assertTrue(any("Waiting to switch to SimConnect.dll at D:\\New" in note for note in facts["notes"]))


class StartupRememberFailureTests(unittest.TestCase):
    def test_folder_not_remembered_at_start_up_reaches_the_banner(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        report = StartupReport()

        class Failing:
            def remember(self, path):
                raise OSError(13, "settings.json is read-only (test)")

        store = FakeStore(Path(tmp.name))
        self.assertIs(app_module._adopt_chosen_folder(store, Failing(), report), store)
        self.assertEqual(len(report.pilot_notices), 1)
        self.assertIn("could not remember it", report.pilot_notices[0])
        self.assertIn(report.pilot_notices[0], report.notes)

    def test_non_oserror_from_remember_is_still_told_to_the_pilot(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)

        class Broken:
            def remember(self, path):
                raise ValueError("settings.json is damaged")

        svc = services(logging=FakeLogging(), store=FakeStore(base / "A"), folder_settings=Broken())
        control = DataFolderControl(svc, open_folder=lambda folder, create: FakeStore(folder))
        with mock.patch.object(DataFolderControl, "_relocate_logging"):
            control.change(base / "B")
        notices = svc.take_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("settings.json is damaged", notices[0])


if __name__ == "__main__":
    unittest.main()
