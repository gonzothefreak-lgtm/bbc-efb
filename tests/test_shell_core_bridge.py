"""CoreBridge additions for the shell (C4): Flight Logging status polling and Connection Diagnostics."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import unittest

from bbc_core.contract import Connection, ConnectionState, Source
from bbc_efb.core_bridge import CoreBridge

from tests.test_shell_support import qt_app


@dataclass(frozen=True)
class Status:
    state: str
    connection: Connection
    detail: str
    ui_active: bool = True
    failures: int = 0
    simulator_unavailable: bool = False
    last_error: str = ""
    updated_at: float = 0.0

    def retry_in(self):
        return 5.0 if self.failures else None


@dataclass(frozen=True)
class Stats:
    pumps: int = 12
    slow_pumps: int = 1
    max_pump_s: float = 0.0123
    crashes: int = 0


class FakeHost:
    def __init__(self, status):
        self._status = status
        self.ui = []

    def status(self):
        return self._status

    def stats(self):
        return Stats()

    def add_status_listener(self, fn):
        return lambda: None

    def add_ui_listener(self, fn):
        return lambda: None

    def set_ui_active(self, active):
        self.ui.append(active)


def disconnected():
    return Connection(ConnectionState.DISCONNECTED, Source("MSFS 2020 SimConnect", False), None,
                      datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc), "SimConnect_Open failed (E_FAIL).")


class CoreBridgeShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def bridge(self, status=None):
        host = FakeHost(status or Status("waiting", disconnected(), "MSFS is not running.", failures=2,
                                         simulator_unavailable=True, last_error="E_FAIL"))
        bridge = CoreBridge(host, autostart=False)
        self.addCleanup(bridge.close)
        return bridge, host

    def test_recorder_status_is_emitted_only_when_it_changes(self):
        bridge, _ = self.bridge()
        seen = []
        bridge.recorder_status_changed.connect(seen.append)
        current = {"value": "armed"}
        bridge.set_recorder_source(lambda: current["value"])
        self.assertEqual(bridge.poll_recorder(), "armed")
        bridge.poll_recorder()
        current["value"] = "recording"
        bridge.poll_recorder()
        self.assertEqual(seen, ["armed", "recording"])
        self.assertEqual(bridge.recorder_status, "recording")

    def test_a_failing_source_is_reported_not_raised(self):
        bridge, _ = self.bridge()
        seen = []
        bridge.recorder_status_changed.connect(seen.append)

        def broken():
            raise RuntimeError("recorder lock")

        bridge.set_recorder_source(broken)
        with self.assertLogs("bbc_efb.core_bridge", "ERROR"):
            self.assertIsNone(bridge.poll_recorder())
        self.assertEqual(seen, [])
        bridge.set_recorder_source(None)
        self.assertIsNone(bridge.poll_recorder())

    def test_polling_slows_while_the_ui_is_inactive_and_stops_on_close(self):
        bridge, _ = self.bridge()
        bridge.set_recorder_source(lambda: "armed")
        self.assertEqual(bridge._recorder_timer.interval(), CoreBridge.RECORDER_INTERVAL_MS)
        bridge._on_ui_active(False)
        self.assertEqual(bridge._recorder_timer.interval(), CoreBridge.RECORDER_INACTIVE_INTERVAL_MS)
        bridge.close()
        self.assertFalse(bridge._recorder_timer.isActive())
        self.assertIsNone(bridge.poll_recorder())

    def test_diagnostics_describe_the_host_without_touching_the_simulator(self):
        bridge, host = self.bridge()
        facts = bridge.diagnostics()
        self.assertEqual(facts["state"], "disconnected")
        self.assertIn("E_FAIL", facts["detail"])
        self.assertEqual(facts["last_errors"], ("E_FAIL",))
        notes = " ".join(facts["notes"])
        self.assertIn("Next connection attempt in 5 s", notes)
        self.assertIn("Failed connection attempts in a row: 2", notes)
        self.assertIn("MSFS is not running", notes)
        self.assertIn("longest pump: 12.3 ms", notes)
        host._status = replace(host._status, failures=0, last_error="")
        self.assertEqual(bridge.diagnostics()["last_errors"], ())

    def test_settings_can_read_the_diagnostics(self):
        from bbc_efb.sections import ShellServices, default_diagnostics
        from bbc_efb.settings import coerce_diagnostics

        bridge, _ = self.bridge()
        services = ShellServices(bridge=bridge, startup_notes=("SimConnect.dll found at C:/x.",))
        diagnostics = coerce_diagnostics(default_diagnostics(services)())
        self.assertEqual(diagnostics.state, "disconnected")
        self.assertIn("SimConnect.dll found at C:/x.", diagnostics.notes)
        self.assertFalse(diagnostics.dll_found)
        self.assertEqual(diagnostics.last_errors, ("E_FAIL",))


if __name__ == "__main__":
    unittest.main()
