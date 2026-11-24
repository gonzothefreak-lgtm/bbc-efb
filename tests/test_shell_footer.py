"""Footer: independent SimConnect and Flight Logging statuses, controls, Recorder adapter."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from concurrent.futures import Future
from datetime import datetime, timezone
import unittest

from PySide6.QtWidgets import QApplication

from bbc_core.contract import ConnectionState
from bbc_efb.footer import (
    ActionResult, Footer, LoggingState, LoggingStatus, RecorderServiceAdapter, UnavailableFlightLogging,
    describe_connection, resolve_action,
)
from bbc_efb.sections import DIAGNOSTICS_PAGE, PRIVACY_PAGE, SETTINGS
from bbc_efb.shell import Shell
from bbc_recorder.model import Phase

from tests.test_shell_support import FakeBridge, FakeLogging, connection, qt_app, services


class FooterWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def test_statuses_are_independent(self):
        footer = Footer()
        footer.set_logging_status(LoggingStatus(LoggingState.ACTIVE))
        logging_text = footer.flight_logging.text
        for state in ConnectionState:
            footer.set_connection(connection(state))
            self.assertEqual(footer.flight_logging.text, logging_text)
        simconnect_text = footer.simconnect.text
        for state in LoggingState:
            footer.set_logging_status(LoggingStatus(state))
            self.assertEqual(footer.simconnect.text, simconnect_text)

    def test_connection_wording(self):
        self.assertEqual(describe_connection(connection()).footer_text, "SimConnect: Connected (MSFS 2020)")
        self.assertEqual(describe_connection(connection(ConnectionState.CONNECTING)).text, "Connecting…")
        self.assertEqual(describe_connection(connection(ConnectionState.DISCONNECTED)).text, "Not connected")
        error = describe_connection(connection(ConnectionState.ERROR, detail="SimConnect quit"))
        self.assertEqual((error.text, error.detail, error.tone), ("Error", "SimConnect quit", "error"))
        none = describe_connection(None)
        self.assertEqual(none.text, "Not connected")
        self.assertIn("not reported", none.detail)

    def test_simulated_connection_is_always_labelled(self):
        for state in ConnectionState:
            view = describe_connection(connection(state, simulated=True))
            self.assertIn("SIMULATED", view.text)
            self.assertIn("SIMULATED", view.source_text)
            self.assertNotIn("MSFS 2020)", view.text)

    def test_tooltip_and_accessibility_carry_detail(self):
        footer = Footer()
        footer.set_connection(connection(ConnectionState.DISCONNECTED, detail="SimConnect.dll was not found."))
        self.assertIn("SimConnect.dll was not found.", footer.simconnect.button.toolTip())
        self.assertIn("Connection Diagnostics", footer.simconnect.button.accessibleDescription())
        self.assertEqual(footer.simconnect.button.accessibleName(), "SimConnect: Not connected")

    def test_logging_labels(self):
        self.assertEqual(LoggingStatus(LoggingState.READY).label, "Ready")
        self.assertEqual(LoggingStatus(LoggingState.ACTIVE).label, "Logging Active")
        self.assertEqual(LoggingStatus(LoggingState.INTERRUPTED).label, "Logging Interrupted")
        self.assertEqual(LoggingStatus(LoggingState.LOGGED).label, "Flight Logged")
        self.assertEqual(LoggingStatus(LoggingState.ACTIVE, simulated=True).label, "Logging Active (SIMULATED)")
        self.assertTrue(LoggingStatus(LoggingState.PAUSED).flight_open)
        self.assertTrue(LoggingStatus(LoggingState.INTERRUPTED).flight_open)
        self.assertFalse(LoggingStatus(LoggingState.LOGGED).flight_open)
        self.assertFalse(LoggingStatus(LoggingState.UNAVAILABLE).flight_open)

    def test_logging_menu_shows_details_and_allowed_controls(self):
        footer = Footer()
        footer.set_logging_status(LoggingStatus(
            LoggingState.ACTIVE, "Logging since block off.", (("Aircraft", "Unknown"), ("From", "YSSY")),
            can_stop=True))
        menu = footer.build_logging_menu()
        actions = footer.menu_actions
        self.assertFalse(actions["start"].isEnabled())
        self.assertTrue(actions["stop"].isEnabled())
        info = menu.actions()[0].defaultWidget().text()
        self.assertIn("Aircraft: Unknown", info)
        self.assertIn("From: YSSY", info)
        self.assertIn("Logging since block off.", info)
        footer.set_logging_status(LoggingStatus(LoggingState.READY, can_start=True))
        footer.build_logging_menu()
        self.assertTrue(footer.menu_actions["start"].isEnabled())
        self.assertFalse(footer.menu_actions["stop"].isEnabled())

    def test_open_logging_menu_is_non_blocking(self):
        footer = Footer()
        footer.open_logging_menu()
        self.assertIsNotNone(footer.menu)
        footer.menu.close()

    def test_clock_uses_injected_utc_and_timer_toggles(self):
        footer = Footer(clock=lambda: datetime(2026, 11, 24, 16, 42, tzinfo=timezone.utc))
        self.assertEqual(footer.clock_label.text(), "16:42Z")
        footer.set_active(True)
        self.assertTrue(footer.clock_timer.isActive())
        footer.set_active(False)
        self.assertFalse(footer.clock_timer.isActive())


class ShellFooterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def make(self, **overrides):
        shell = Shell(services(**overrides))

        def dispose():
            shell._close_approved = True
            shell.close()
            shell.deleteLater()
            QApplication.processEvents()

        self.addCleanup(dispose)
        return shell

    def test_simconnect_click_opens_connection_diagnostics(self):
        shell = self.make()
        shell.footer.simconnect.button.click()
        self.assertEqual(shell.current_section, SETTINGS)
        self.assertEqual(shell.sections[SETTINGS].current_page, DIAGNOSTICS_PAGE)

    def test_privacy_link_opens_privacy_page(self):
        shell = self.make()
        shell.footer.privacy_requested.emit()
        self.assertEqual(shell.sections[SETTINGS].current_page, PRIVACY_PAGE)

    def test_bridge_connection_updates_footer_and_settings_only(self):
        bridge = FakeBridge()
        logging_ = FakeLogging(LoggingState.ACTIVE, can_stop=True)
        shell = self.make(bridge=bridge, logging=logging_)
        settings = shell.open_settings()
        logging_text = shell.footer.flight_logging.text
        bridge.connection_changed.emit(connection(ConnectionState.ERROR, detail="SimConnect quit"))
        self.assertEqual(shell.footer.simconnect.text, "SimConnect: Error")
        self.assertEqual(settings.connection_rows.values["Detail"].text(), "SimConnect quit")
        self.assertEqual(shell.footer.flight_logging.text, logging_text)
        self.assertEqual(logging_text, "Flight Logging: Logging Active")

    def test_logging_status_polled_from_recorder(self):
        logging_ = FakeLogging(LoggingState.READY)
        shell = self.make(logging=logging_, initial_connection=connection())
        simconnect_text = shell.footer.simconnect.text
        logging_.set(LoggingState.INTERRUPTED, detail="No data for 12 s")
        shell.status_timer.timeout.emit()
        self.assertEqual(shell.footer.flight_logging.text, "Flight Logging: Logging Interrupted")
        self.assertEqual(shell.footer.simconnect.text, simconnect_text)

    def test_status_failure_is_reported_not_raised(self):
        class Broken(FakeLogging):
            def status(self):
                raise RuntimeError("recorder lock timeout")

        shell = self.make(logging=Broken())
        self.assertIn("Not available", shell.footer.flight_logging.text)
        self.assertIn("recorder lock timeout", shell.footer.logging_status.detail)

    def test_manual_stop_requires_confirmation(self):
        logging_ = FakeLogging(LoggingState.ACTIVE, can_stop=True)
        shell = self.make(logging=logging_)
        asked = []
        shell._confirm_stop = lambda status: asked.append(status) or False
        self.assertIsNone(shell.request_stop_logging())
        self.assertEqual(len(asked), 1)
        self.assertEqual(logging_.called("stop"), [])
        shell._confirm_stop = lambda status: True
        shell.footer.stop_requested.emit()
        self.assertEqual(len(logging_.called("stop")), 1)
        self.assertEqual(shell.footer.flight_logging.text, "Flight Logging: Flight Logged")

    def test_stop_without_open_flight_does_nothing(self):
        logging_ = FakeLogging(LoggingState.READY)
        shell = self.make(logging=logging_)
        shell._confirm_stop = lambda status: self.fail("must not ask")
        self.assertIsNone(shell.request_stop_logging())

    def test_failed_stop_and_start_are_reported(self):
        logging_ = FakeLogging(LoggingState.ACTIVE, can_stop=True)
        logging_.stop_result = ActionResult(False, "Journal write failed")
        shell = self.make(logging=logging_)
        warnings = []
        shell._warn = lambda title, text: warnings.append((title, text))
        shell._confirm_stop = lambda status: True
        outcome = shell.request_stop_logging()
        self.assertFalse(outcome.ok)
        self.assertIn("Journal write failed", warnings[-1][1])
        self.assertIn("still open", warnings[-1][1])
        logging_.start_result = ActionResult(False, "No aircraft loaded")
        shell.start_logging()
        self.assertIn("No aircraft loaded", warnings[-1][1])


class ResolveActionTests(unittest.TestCase):
    def test_values(self):
        self.assertTrue(resolve_action(None).ok)
        self.assertTrue(resolve_action(True).ok)
        self.assertFalse(resolve_action(False).ok)
        self.assertEqual(resolve_action(ActionResult(False, "x")), ActionResult(False, "x"))

        class Outcome:
            success = False
            error = "disk full"

        self.assertEqual(resolve_action(Outcome()), ActionResult(False, "disk full"))

    def test_future_success_failure_and_timeout(self):
        done = Future()
        done.set_result(ActionResult(True, "saved"))
        self.assertEqual(resolve_action(done), ActionResult(True, "saved"))
        failed = Future()
        failed.set_exception(OSError("disk full"))
        self.assertEqual(resolve_action(failed), ActionResult(False, "disk full"))
        pending = Future()
        outcome = resolve_action(pending, timeout=0.01)
        self.assertFalse(outcome.ok)
        self.assertIn("did not finish", outcome.message)


class RecorderAdapterTests(unittest.TestCase):
    class Service:
        def __init__(self, phase=Phase.IDLE):
            self.phase = phase
            self.paused = False
            self.gap_open = False
            self.simulated = False
            self.save_error = None
            self.current_flight = None
            self.calls = []

        def manual_start(self):
            self.calls.append("start")

        def manual_stop(self):
            self.calls.append("stop")
            return ActionResult(True, "stopped")

        def finalise(self, reason):
            self.calls.append(("finalise", reason))
            raise OSError("The data folder is not writable")

        def close(self):
            self.calls.append("close")

    def test_phase_mapping(self):
        service = self.Service()
        adapter = RecorderServiceAdapter(service)
        status = adapter.status()
        self.assertEqual(status.state, LoggingState.READY)
        self.assertTrue(status.can_start)
        self.assertFalse(status.can_stop)
        service.phase = Phase.AIRBORNE
        service.current_flight = {"aircraft": None, "aircraft_type": "B738", "departure": "YSSY",
                                  "arrival": None}
        status = adapter.status()
        self.assertEqual(status.state, LoggingState.ACTIVE)
        self.assertTrue(status.can_stop)
        self.assertFalse(status.can_start)
        rows = dict(status.flight)
        self.assertEqual(rows["Phase"], "Airborne")
        self.assertEqual(rows["Aircraft"], "B738")
        self.assertEqual(rows["From"], "YSSY")
        self.assertEqual(rows["To"], "Unknown")
        service.paused = True
        self.assertEqual(adapter.status().state, LoggingState.PAUSED)
        service.gap_open = True
        self.assertEqual(adapter.status().state, LoggingState.INTERRUPTED)
        service.phase = "complete"
        self.assertEqual(adapter.status().state, LoggingState.LOGGED)
        service.save_error = "disk full"
        status = adapter.status()
        self.assertEqual(status.state, LoggingState.SAVE_FAILED)
        self.assertIn("disk full", status.detail)

    def test_simulated_and_missing_state(self):
        service = self.Service(Phase.TAXI_OUT)
        service.simulated = True
        self.assertIn("SIMULATED", RecorderServiceAdapter(service).status().label)

        class Empty:
            pass

        status = RecorderServiceAdapter(Empty()).status()
        self.assertEqual(status.state, LoggingState.UNAVAILABLE)

    def test_logging_status_method_wins(self):
        class Direct:
            def logging_status(self):
                return LoggingStatus(LoggingState.PAUSED, "paused by pilot")

        self.assertEqual(RecorderServiceAdapter(Direct()).status().detail, "paused by pilot")

    def test_status_exception_becomes_unavailable(self):
        class Broken:
            @property
            def phase(self):
                raise RuntimeError("boom")

        status = RecorderServiceAdapter(Broken()).status()
        self.assertEqual(status.state, LoggingState.UNAVAILABLE)
        self.assertIn("boom", status.detail)

    def test_calls(self):
        service = self.Service(Phase.AIRBORNE)
        adapter = RecorderServiceAdapter(service)
        self.assertTrue(resolve_action(adapter.start()).ok)
        self.assertEqual(resolve_action(adapter.stop()), ActionResult(True, "stopped"))
        saved = resolve_action(adapter.save_and_exit("closed"))
        self.assertFalse(saved.ok)
        self.assertIn("not writable", saved.message)
        self.assertIn(("finalise", "closed"), service.calls)
        adapter.shutdown()
        self.assertEqual(service.calls[-1], "close")

    def test_missing_methods_are_reported(self):
        class Bare:
            phase = Phase.AIRBORNE

        adapter = RecorderServiceAdapter(Bare())
        self.assertFalse(adapter.status().can_stop)
        self.assertFalse(resolve_action(adapter.stop()).ok)
        self.assertFalse(resolve_action(adapter.save_and_exit("x")).ok)

    def test_stop_passes_confirmation_when_accepted(self):
        class Service:
            phase = Phase.AIRBORNE

            def __init__(self):
                self.kwargs = None

            def manual_stop(self, *, confirmed=False):
                self.kwargs = {"confirmed": confirmed}
                return ActionResult(confirmed, "stopped" if confirmed else "needs confirmation")

        service = Service()
        self.assertTrue(resolve_action(RecorderServiceAdapter(service).stop()).ok)
        self.assertEqual(service.kwargs, {"confirmed": True})

    def test_shutdown_is_save_and_exit_when_no_other_name(self):
        class Service:
            phase = Phase.AIRBORNE

            def __init__(self):
                self.reasons = []

            def shutdown(self, reason="default"):
                self.reasons.append(reason)
                return ActionResult(True, "The flight was saved as interrupted.")

        service = Service()
        adapter = RecorderServiceAdapter(service)
        self.assertTrue(resolve_action(adapter.save_and_exit("closed by pilot")).ok)
        self.assertEqual(service.reasons, ["closed by pilot"])

    def test_unavailable(self):
        unavailable = UnavailableFlightLogging("No data folder")
        self.assertEqual(unavailable.status().detail, "No data folder")
        self.assertFalse(unavailable.status().flight_open)
        self.assertFalse(unavailable.start().ok)
        self.assertTrue(unavailable.save_and_exit("x").ok)


class C3StatusTests(unittest.TestCase):
    """The round-3 RecorderStatus contract (C3) and Save & Exit through save_and_close."""

    def status(self, state, **values):
        from dataclasses import make_dataclass

        defaults = dict(phase=None, detail="detail text", flight_id=None, started_utc=None, aircraft=None,
                        departure=None, block_time_s=None, airborne_time_s=None, simulated=False)
        defaults.update(values)
        Status = make_dataclass("RecorderStatus", ["state", *defaults], frozen=True)
        return Status(state, *defaults.values())

    def adapter(self, current, **kwargs):
        class Service:
            def status(self):
                return holder["status"]

        holder = {"status": current}
        adapter = RecorderServiceAdapter(Service(), **kwargs)
        return adapter, holder

    def test_states_flight_rows_and_controls(self):
        started = datetime(2026, 11, 24, 21, 31, tzinfo=timezone.utc)
        adapter, holder = self.adapter(self.status(
            "recording", phase=Phase.AIRBORNE, flight_id="f1", started_utc=started, aircraft="A20N",
            departure="YSSY", block_time_s=1260.0, airborne_time_s=None, simulated=True))
        result = adapter.status()
        self.assertEqual(result.state, LoggingState.ACTIVE)
        self.assertTrue(result.flight_open)
        self.assertTrue(result.can_stop)
        self.assertFalse(result.can_start)
        self.assertTrue(result.simulated)
        self.assertIn("SIMULATED", result.label)
        rows = dict(result.flight)
        self.assertEqual(rows["Aircraft"], "A20N")
        self.assertEqual(rows["From"], "YSSY")
        self.assertEqual(rows["Block time"], "0h 21m")
        self.assertEqual(rows["Airborne time"], "Unknown")
        self.assertIn("21:31:00Z", rows["Started"])
        for state, expected, open_ in (("armed", LoggingState.READY, False),
                                       ("inactive", LoggingState.INACTIVE, False),
                                       ("unavailable", LoggingState.UNAVAILABLE, False),
                                       ("paused", LoggingState.PAUSED, True),
                                       ("gap", LoggingState.INTERRUPTED, True),
                                       ("saving", LoggingState.ACTIVE, True),
                                       ("error", LoggingState.ERROR, False)):
            holder["status"] = self.status(state, flight_id="f1" if open_ else None)
            result = adapter.status()
            self.assertEqual((result.state, result.flight_open), (expected, open_), state)
        holder["status"] = self.status("armed")
        self.assertTrue(adapter.status().can_start)
        holder["status"] = self.status("saving", flight_id="f1")
        self.assertFalse(adapter.status().can_stop)
        self.assertIn("Saving", adapter.status().label)

    def test_last_flight_saved_and_failed_saves(self):
        from types import SimpleNamespace

        adapter, holder = self.adapter(self.status(
            "armed", last_flight=SimpleNamespace(saved=True, outcome="complete"), flight_open=False,
            can_start=True, can_stop=False, label="Ready", pending_saves=0))
        self.assertEqual(adapter.status().state, LoggingState.LOGGED)
        holder["status"] = self.status("error", last_flight=SimpleNamespace(saved=False, outcome="complete"),
                                       flight_open=False, can_start=False, can_stop=False, label="Error",
                                       pending_saves=1)
        self.assertEqual(adapter.status().state, LoggingState.SAVE_FAILED)

    def test_save_and_close_is_used_with_the_timeout_and_reports_failures(self):
        class Service:
            def __init__(self, result):
                self.result = result
                self.calls = []

            def status(self):
                return None

            def save_and_close(self, timeout_s=10.0):
                self.calls.append(timeout_s)
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

        from types import SimpleNamespace

        ok = Service(SimpleNamespace(ok=True, detail="saved as interrupted"))
        result = RecorderServiceAdapter(ok).save_and_exit("closing", timeout_s=3.0)
        self.assertEqual(result, ActionResult(True, "saved as interrupted"))
        self.assertEqual(ok.calls, [3.0])
        failed = Service(SimpleNamespace(ok=False, detail="Saving did not finish within 3 s."))
        self.assertEqual(RecorderServiceAdapter(failed).save_and_exit("closing"),
                         ActionResult(False, "Saving did not finish within 3 s."))
        broken = Service(OSError("disk full"))
        self.assertEqual(RecorderServiceAdapter(broken).save_and_exit("closing"), ActionResult(False, "disk full"))

    def test_older_shutdown_runs_on_the_core_thread_when_given(self):
        class Service:
            def __init__(self):
                self.reasons = []

            def status(self):
                return None

            def shutdown(self, reason):
                self.reasons.append(reason)
                return ActionResult(True, "saved")

        submitted = []

        def run_on_core(fn):
            submitted.append(fn)
            future = Future()
            future.set_result(fn(object()))
            return future

        service = Service()
        result = RecorderServiceAdapter(service, run_on_core=run_on_core).save_and_exit("closing")
        self.assertIsInstance(result, Future)
        self.assertEqual(result.result(), ActionResult(True, "saved"))
        self.assertEqual(service.reasons, ["closing"])
        self.assertEqual(len(submitted), 1)

        def stopped(fn):
            raise RuntimeError("host stopped")

        service = Service()
        self.assertEqual(RecorderServiceAdapter(service, run_on_core=stopped).save_and_exit("closing"),
                         ActionResult(True, "saved"))

    def test_after_shutdown_steps_run_even_when_close_fails(self):
        class Service:
            def status(self):
                return None

            def close(self):
                raise OSError("journal busy")

        order = []
        adapter = RecorderServiceAdapter(Service(), before_shutdown=[lambda: order.append("untick")])
        adapter.after_shutdown.append(lambda: order.append("release consumer"))
        with self.assertRaises(OSError):
            adapter.shutdown()
        self.assertEqual(order, ["untick", "release consumer"])


if __name__ == "__main__":
    unittest.main()
