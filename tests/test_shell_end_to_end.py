"""End to end, offscreen: the app with --simulated, driven by a SimulatedProvider FlightTape.

Path under test (nothing faked between the provider and the Logbook):

    SimulatedProvider (FlightTape) -> Core -> CoreHost thread -> RecorderService (ticked on the
    Core thread) -> LogbookSink -> the app's one FlightStore -> Logbook -> History model

The provider, Core and the Recorder share a virtual clock that the test advances, so a
ten-minute circuit replays in a few seconds. The tape is synthetic and simulated, and every
status and record is checked to say so. Nothing touches MSFS or SimConnect.
"""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import importlib
import importlib.util
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QApplication

from tests.test_shell_support import isolate_logging, qt_app


def _has(*modules: str) -> bool:
    try:
        return all(importlib.util.find_spec(module) is not None for module in modules)
    except ModuleNotFoundError:
        return False


STACK = ("bbc_core.host", "bbc_efb.core_bridge", "bbc_recorder.service", "bbc_recorder.state_machine",
         "bbc_recorder.journal")
_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
SIM_START = datetime(2026, 11, 24, 21, 30, tzinfo=timezone.utc)


class VirtualClock:
    """Monotonic seconds plus the matching UTC wall time, advanced only by the test."""

    def __init__(self, wall_start: datetime):
        self._lock = threading.Lock()
        self._t = 1000.0
        self._wall_start = wall_start

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def wall(self) -> datetime:
        return self._wall_start + timedelta(seconds=self() - 1000.0)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds


def recorder_fields():
    """Every SimVar the Recorder may subscribe to, from whichever Recorder modules this build has."""
    found = {}
    sources = (("bbc_recorder.rules", "snapshot_simvars"), ("bbc_recorder.attribution", "identity_simvars"),
               ("bbc_recorder.service", "state_machine_fields"))
    for module_name, name in sources:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        provide = getattr(module, name, None)
        if callable(provide):
            for variable in provide():
                found.setdefault((variable.sdk_name, variable.unit), variable)
    for variable in getattr(importlib.import_module("bbc_recorder.service"), "DEFAULT_FIELDS", ()):
        found.setdefault((variable.sdk_name, variable.unit), variable)
    return tuple(found.values())


def circuit_tape():
    """A synthetic YSSY circuit as a Core FlightTape: (tape, airborne time, end time).

    Simple kinematics only: every value is made up for the test and the provider labels it
    simulated. Frames are 1 s apart, 0.2 s near the ground, like the Recorder's own tapes.
    """
    import math

    from bbc_core.simulated import FlightTape, TapeEntry

    fields = recorder_fields()
    state = {"t": 0.0, "lat": -33.9421, "lon": 151.1742, "agl": 0.0, "gs": 0.0, "vs": 0.0, "hdg": 160.0,
             "on_ground": True, "brake": True, "engines": False, "td_fps": -6.5}
    entries, last = [], {}

    def values():
        return {
            "SIM ON GROUND": state["on_ground"], "GROUND VELOCITY": state["gs"],
            "AIRSPEED INDICATED": max(0.0, state["gs"] - 3), "AIRSPEED TRUE": state["gs"],
            "VERTICAL SPEED": state["vs"], "PLANE ALT ABOVE GROUND": state["agl"],
            "PLANE ALT ABOVE GROUND MINUS CG": max(0.0, state["agl"] - 6),
            "PLANE ALTITUDE": 21.0 + state["agl"], "PLANE LATITUDE": round(state["lat"], 7),
            "PLANE LONGITUDE": round(state["lon"], 7), "PLANE HEADING DEGREES TRUE": state["hdg"],
            "BRAKE PARKING POSITION": state["brake"], "IS SLEW ACTIVE": False,
            "PLANE TOUCHDOWN NORMAL VELOCITY": state["td_fps"], "NUMBER OF ENGINES": 2,
            "GENERAL ENG COMBUSTION:1": state["engines"], "GENERAL ENG COMBUSTION:2": state["engines"],
            "GENERAL ENG COMBUSTION:3": False, "GENERAL ENG COMBUSTION:4": False,
            "TITLE": "Generic Twin Jet (synthetic test data)", "ATC ID": "VH-TPE", "ATC TYPE": "GTJ",
            "ATC MODEL": "GTJ", "ATC AIRLINE": "", "ATC FLIGHT NUMBER": "",
        }

    def frame():
        current = values()
        for variable in fields:
            value = current.get(variable.sdk_name)
            if value is None:
                continue
            if variable.sdk_name == "PLANE TOUCHDOWN NORMAL VELOCITY" and variable.unit == "feet per minute":
                value = value * 60.0
            key = (variable.sdk_name, variable.unit)
            if last.get(key, object()) != value:
                last[key] = value
                entries.append(TapeEntry(round(state["t"], 3), variable, value))

    def run(seconds, step=None):
        remaining = seconds
        while remaining > 1e-9:
            near_ground = state["agl"] < 1500
            dt = min(remaining, 0.2 if near_ground else 1.0)
            remaining -= dt
            state["t"] += dt
            if step is not None:
                step(dt)
            distance_nm = state["gs"] * dt / 3600.0
            state["lat"] += distance_nm / 60.0 * math.cos(math.radians(state["hdg"]))
            state["lon"] += distance_nm / 60.0 * math.sin(math.radians(state["hdg"])) / math.cos(
                math.radians(state["lat"]))
            frame()

    frame()
    run(15)
    state["engines"] = True
    run(30)
    state["brake"] = False
    run(3)
    state["gs"] = 14.0
    run(60)
    state["gs"] = 0.0
    run(5)

    def roll(dt):
        state["gs"] = min(150.0, state["gs"] + 4.5 * dt)

    run(34, roll)
    state.update(on_ground=False, vs=1500.0)
    airborne_at = state["t"]

    def climb(dt):
        state["agl"] = min(1000.0, state["agl"] + state["vs"] / 60.0 * dt)
        if state["agl"] >= 1000.0:
            state["vs"] = 0.0

    run(60, climb)
    state["vs"] = -700.0

    def descend(dt):
        state["agl"] = max(0.0, state["agl"] + state["vs"] / 60.0 * dt)
        if state["agl"] < 30:
            state["vs"] = -180.0

    while state["agl"] > 0.0:
        run(0.2, descend)
    state.update(on_ground=True, vs=0.0, td_fps=-3.0)

    def brake(dt):
        state["gs"] = max(12.0, state["gs"] - 5.0 * dt)

    run(30, brake)
    run(40)
    state["gs"] = 0.0
    run(5)
    state["brake"] = True
    run(10)
    state["engines"] = False
    run(20)
    return FlightTape(entries), airborne_at, state["t"]


@unittest.skipUnless(_has(*STACK), "Core host, bridge or Recorder is not in this build")
class SimulatedFlightEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()
        cls.tape, cls.airborne_at, cls.end_at = circuit_tape()

    def setUp(self):
        import logging

        from bbc_core.host import HostConfig
        from bbc_core.simulated import SimulatedProvider
        from bbc_efb import app as app_module
        from bbc_efb.app import AppOptions

        logging.getLogger("bbc_recorder").setLevel(logging.ERROR)
        self.addCleanup(logging.getLogger("bbc_recorder").setLevel, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.per_user_logs = isolate_logging(self)
        self.data = Path(tmp.name) / "efb"
        self.clock = VirtualClock(datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc))
        self.provider = SimulatedProvider(clock=self.clock, wall_clock=self.clock.wall, frame_rate_hz=5,
                                          absolute_time_s=(SIM_START - _EPOCH).total_seconds())
        with mock.patch.object(app_module, "_folder_settings", return_value=None), \
                mock.patch.object(app_module, "_load_preferences", return_value=None):
            self.services = app_module.build_services(
                AppOptions(data_dir=self.data, simulated=True),
                provider_factory=lambda: self.provider,
                host_config=HostConfig(pump_interval_s=0.005, backoff_s=(0.05,)),
                recorder_overrides={"clock": self.clock.wall, "monotonic": self.clock})
        self.shut_down = False
        self.addCleanup(self._shutdown)
        from bbc_efb.shell import Shell

        self.shell = Shell(self.services)
        self.shell.show()
        self.addCleanup(self._dispose)
        self.assertTrue(self.wait(lambda: self.services.host.status().connected), "simulated host did not connect")

    def _dispose(self):
        self.shell._close_approved = True
        self.shell.close()
        self.shell.deleteLater()
        QApplication.processEvents()

    def _shutdown(self):
        if not self.shut_down:
            from bbc_efb import app as app_module

            app_module.shutdown_services(self.services)
            self.shut_down = True

    @staticmethod
    def wait(predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def fly(self, until: float, step: float = 1.0):
        """Play the tape until ``until`` tape seconds, letting Core and the Recorder catch up each step."""
        host = self.services.host
        self.services.host.call(lambda core: self.provider.play(self.tape))
        elapsed = 0.0
        seen = []
        while elapsed < until:
            self.clock.advance(step)
            elapsed += step
            pumps = host.stats().pumps
            deadline = time.monotonic() + 2.0
            while host.stats().pumps < pumps + 2 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.services.bridge.poll_recorder()
            QApplication.processEvents()
            label = self.shell.footer.flight_logging.text
            if not seen or seen[-1] != label:
                seen.append(label)
        return seen

    def recorder_flights(self):
        flights, unreadable = self.services.store.listing()
        self.assertEqual(unreadable, [])
        return [flight for flight in flights if flight.source == "recorder"]

    def test_full_simulated_flight_reaches_the_logbook_history(self):
        from bbc_efb.logbook_views.history import FlightTableModel
        from bbc_efb.logbook_views.model import ROLE_FLIGHT, flight_views
        from bbc_efb.sections import LOGBOOK

        logbook_section = self.shell.show_section(LOGBOOK)
        self.shell.show_section("dispatch")  # Flight Logging does not depend on the open Section
        labels = self.fly(self.end_at + 10)
        self.assertTrue(any("Logging" in label and "SIMULATED" in label for label in labels), labels)
        self.assertTrue(any("Flight Logged" in label for label in labels), labels)
        self.assertIn("SIMULATED", self.shell.footer.simconnect.text)

        flights = self.recorder_flights()
        self.assertEqual(len(flights), 1, flights)
        flight = flights[0]
        evidence = flight.evidence
        self.assertIsNotNone(evidence)
        self.assertTrue(evidence.simulated)
        self.assertIn(evidence.time_basis, ("simulator", "host"))
        for name in ("off_block", "takeoff", "landing", "on_block"):
            self.assertIsNotNone(getattr(evidence, name), name)
        self.assertGreater(evidence.airborne_s, 0)
        self.assertGreater(evidence.block_s, evidence.airborne_s)
        self.assertTrue((self.data / evidence.journal).is_file(), evidence.journal)
        self.assertEqual(flight.fields.registration, "VH-TPE")
        self.assertEqual(flight.fields.flight_date, "2026-11-24")

        # The Logbook re-read the app's store when the flight was logged (no manual refresh).
        logbook = logbook_section.logbook
        self.assertIs(logbook.store, self.services.store)
        self.assertIn(flight.id, [item.id for item in logbook.flights])
        models = logbook.findChildren(FlightTableModel)
        if models:
            model = models[0]
        else:  # the Logbook does not embed the History views yet; build the same model from its flights
            model = FlightTableModel()
            views, failures = flight_views(logbook.flights)
            self.assertEqual(failures, [])
            model.set_flights(views)
        rows = [model.data(model.index(row, 0), ROLE_FLIGHT) for row in range(model.rowCount())]
        recorded = [view for view in rows if view.id == flight.id]
        self.assertEqual(len(recorded), 1)
        view = recorded[0]
        self.assertEqual(view.source, "recorder")
        self.assertTrue(view.has_evidence)
        self.assertTrue(view.simulated)
        self.assertEqual(view.journal, evidence.journal)

        # Logging continued in a log file inside the data folder.
        log_file = self.data / "logs" / "bbc-efb.log"
        self.assertTrue(log_file.is_file())

    def test_save_and_exit_during_a_flight_saves_it_as_interrupted(self):
        from bbc_efb.footer import LoggingState
        from bbc_efb.shell import SAVE_AND_EXIT

        self.fly(self.airborne_at + 10)
        status = self.services.logging.status()
        self.assertTrue(status.flight_open, status)
        self.assertEqual(status.state, LoggingState.ACTIVE)
        self.assertEqual(self.recorder_flights(), [])
        asked = []
        self.shell._ask_close_while_logging = lambda current: asked.append(current) or SAVE_AND_EXIT
        self.shell._report_save_failure = lambda message: self.fail(f"Save & Exit failed: {message}")
        self.assertTrue(self.shell.close())
        self.assertEqual(len(asked), 1)
        flights = self.recorder_flights()
        self.assertEqual(len(flights), 1)
        evidence = flights[0].evidence
        self.assertTrue(evidence.simulated)
        self.assertIsNone(evidence.on_block)  # never marked as a completed flight
        # What was observed before Save & Exit stays as evidence, on the simulator clock.
        self.assertIsNotNone(evidence.off_block)
        self.assertIsNotNone(evidence.takeoff)
        self.assertIsNone(evidence.landing)
        self.assertEqual(evidence.time_basis, "simulator")
        self.assertIsNone(flights[0].fields.minutes)  # no block time is invented for an interrupted flight
        record_status = (evidence.extra.get("status") or evidence.extra.get("outcome") or "")
        text = f"{record_status} {evidence.summary}".lower()
        self.assertIn("interrupted", text)
        self._shutdown()
        self.assertFalse(self.services.host.running)
        # Nothing is left for start-up recovery: the journal was archived after the save.
        in_progress = self.data / "recorder" / "in-progress"
        leftovers = list(in_progress.glob("*.jsonl")) if in_progress.is_dir() else []
        self.assertEqual(leftovers, [])


@unittest.skipUnless(_has(*STACK, "bbc_recorder.recovery"), "Core host, bridge or Recorder is not in this build")
class StartupRecoveryTests(unittest.TestCase):
    """A journal left open by a crash is saved as interrupted at start-up, and the pilot is told."""

    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def crashed_journal(self, data: Path) -> str:
        from bbc_recorder.journal import Journal, JournalLayout, new_flight_id
        from bbc_recorder.model import EventKind, Phase, RecorderEvent

        start = datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)
        flight_id = new_flight_id(start)
        journal = Journal.create(JournalLayout(data), flight_id, app_version="test", started_host_time=start,
                                 aircraft={"title": "Generic Twin Jet (synthetic test data)", "atc_id": "VH-TPE"},
                                 source={"name": "BBC Core simulated provider", "simulated": True})
        for offset, kind, phase in ((0, EventKind.FLIGHT_STARTED, Phase.PREFLIGHT),
                                    (60, EventKind.BLOCK_OFF, Phase.TAXI_OUT),
                                    (300, EventKind.TAKEOFF, Phase.AIRBORNE),
                                    (360, EventKind.SAMPLE, Phase.AIRBORNE)):
            moment = start + timedelta(seconds=offset)
            journal.append(RecorderEvent(kind, phase, moment, 100.0 + offset, moment, {}))
        journal.abandon()  # the process "dies": no footer, the lock is released
        return flight_id

    def test_unfinished_journal_is_saved_as_interrupted_with_a_pilot_notice(self):
        from bbc_efb import app as app_module
        from bbc_efb.app import AppOptions
        from bbc_efb.shell import Shell
        from bbc_recorder.recovery import find_unfinished

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        isolate_logging(self)
        data = Path(tmp.name) / "efb"
        data.mkdir()
        flight_id = self.crashed_journal(data)
        self.assertEqual(len(find_unfinished(data)), 1)
        provider = __import__("bbc_core.simulated", fromlist=["SimulatedProvider"]).SimulatedProvider()
        with mock.patch.object(app_module, "_folder_settings", return_value=None), \
                mock.patch.object(app_module, "_load_preferences", return_value=None):
            services = app_module.build_services(AppOptions(data_dir=data, simulated=True),
                                                 provider_factory=lambda: provider)
        self.addCleanup(app_module.shutdown_services, services)
        flights, _ = services.store.listing()
        recovered = [flight for flight in flights if flight.source == "recorder"]
        self.assertEqual(len(recovered), 1)
        evidence = recovered[0].evidence
        self.assertEqual(evidence.extra.get("status"), "interrupted")
        self.assertIn(flight_id, str(dict(evidence.extra)))
        self.assertEqual(find_unfinished(data), [])
        self.assertTrue(any("saved it to the Logbook as interrupted" in note for note in services.startup_notes))
        shell = Shell(services)
        self.addCleanup(shell.deleteLater)
        self.addCleanup(lambda: setattr(shell, "_close_approved", True))
        banner = shell.notice_banner
        self.assertFalse(banner.isHidden())
        self.assertEqual(len(banner.notices), 1)
        self.assertIn("as interrupted", banner.notices[0])
        self.assertIn("Review it in the Logbook", banner.notices[0])
        banner.open_button.click()
        self.assertEqual(shell.current_section, "logbook")
        banner.dismiss_button.click()
        self.assertTrue(banner.isHidden())
        self.assertEqual(services.notices, [])  # shown once


if __name__ == "__main__":
    unittest.main()
