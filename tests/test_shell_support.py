"""Fakes and helpers shared by the shell tests (no test cases here)."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from concurrent.futures import Future
from datetime import datetime, timezone
import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from bbc_core.contract import Connection, ConnectionState, Source
from bbc_efb.footer import ActionResult, LoggingState, LoggingStatus
from bbc_efb.sections import ShellServices

# Expected failures are logged with tracebacks by design; keep them out of test output.
logging.getLogger("bbc_efb").addHandler(logging.NullHandler())

MISSING_SETTINGS = "bbc_efb._no_such_settings_module_for_tests"


def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def connection(state=ConnectionState.CONNECTED, *, simulated=False, detail="", name=None):
    source = Source(name or ("BBC Core simulated provider" if simulated else "MSFS 2020 SimConnect"), simulated)
    return Connection(state, source, "session-1" if state == ConnectionState.CONNECTED else None,
                      datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc), detail)


class FakeBridge(QObject):
    connection_changed = Signal(object)
    updates = Signal(object)

    def __init__(self):
        super().__init__()
        self.ui_active_calls: list[bool] = []
        self.stopped = 0

    def set_ui_active(self, active):
        self.ui_active_calls.append(active)

    def stop(self):
        self.stopped += 1


class FakeLogging:
    """A FlightLogging implementation the tests drive directly."""

    def __init__(self, state=LoggingState.READY, **kwargs):
        self.current = LoggingStatus(state, **kwargs)
        self.calls: list[tuple] = []
        self.save_result = ActionResult(True, "saved")
        self.stop_result = ActionResult(True)
        self.start_result = ActionResult(True)
        self.shutdowns = 0

    def set(self, state, **kwargs):
        self.current = LoggingStatus(state, **kwargs)

    def status(self):
        self.calls.append(("status",))
        return self.current

    def start(self):
        self.calls.append(("start",))
        return self.start_result

    def stop(self):
        self.calls.append(("stop",))
        if self.stop_result.ok:
            self.current = LoggingStatus(LoggingState.LOGGED)
        return self.stop_result

    def save_and_exit(self, reason):
        self.calls.append(("save_and_exit", reason))
        result = self.save_result
        if isinstance(result, BaseException):
            raise result
        return result

    def shutdown(self):
        self.shutdowns += 1

    def called(self, name):
        return [call for call in self.calls if call[0] == name]


def services(**overrides) -> ShellServices:
    values = dict(logging=FakeLogging(), settings_module=MISSING_SETTINGS,
                  initial_connection=connection(ConnectionState.DISCONNECTED, detail="SimConnect.dll was not found."))
    values.update(overrides)
    return ShellServices(**values)


def pending_future() -> Future:
    return Future()


def isolate_logging(testcase) -> "Path":
    """Point the per-user log folder at a temporary folder for one test and reset logging after it.

    ``build_services`` configures logging the way the app does; without this the test
    would write into the real per-user log folder.
    """
    import tempfile
    from pathlib import Path
    from unittest import mock

    from bbc_efb import logging_setup

    folder = tempfile.TemporaryDirectory()
    testcase.addCleanup(folder.cleanup)
    path = Path(folder.name) / "per-user-logs"
    patcher = mock.patch.object(logging_setup, "user_log_directory", return_value=path)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    testcase.addCleanup(logging_setup.reset_logging)
    return path
