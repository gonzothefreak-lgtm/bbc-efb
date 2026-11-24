"""Flight Details overlay: in-EFB, resizable, persistent header, honest tabs."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from tests.test_logviews_support import app, evidence, imported, manual, process, recorded
except ImportError:  # discovered without the package prefix
    from test_logviews_support import app, evidence, imported, manual, process, recorded  # type: ignore[no-redef]

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QTableWidget, QVBoxLayout, QWidget

from bbc_efb.logbook_views.charts import ProfileChart
from bbc_efb.logbook_views.details import MIN_PANEL, FlightDetailsOverlay, journal_loader_for
from bbc_efb.logbook_views.model import JournalContents, flight_view


def texts(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


def table_texts(widget):
    out = []
    for table in widget.findChildren(QTableWidget):
        for r in range(table.rowCount()):
            out.append(" | ".join(table.item(r, c).text() if table.item(r, c) else "" for c in range(table.columnCount())))
    return "\n".join(out)


class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1200, 800)
        layout = QVBoxLayout(self.host)
        self.field = QLineEdit()
        layout.addWidget(self.field)
        self.host.show()
        self.field.setFocus()
        process()
        self.loads = []
        self.overlay = FlightDetailsOverlay(self.host, journal_loader=self.loader)
        self.addCleanup(self.host.deleteLater)

    def loader(self, view):
        self.loads.append(view.id)
        return JournalContents(problem="test journal unavailable")

    def test_overlay_is_inside_the_host_not_a_window(self):
        self.overlay.open_flight(flight_view(manual()))
        process()
        self.assertFalse(self.overlay.isWindow())
        self.assertIs(self.overlay.parentWidget(), self.host)
        self.assertEqual(self.overlay.geometry(), self.host.rect())
        self.assertTrue(self.host.rect().contains(self.overlay.panel.geometry()))

    def test_manual_flight_tabs_and_header(self):
        view = flight_view(manual(callsign="BBC427", registration="N738BC", minutes=77, aircraft="B737-800BCF",
                                  freight_kg=18420, notes="Gusty", extra={"operation": "freight"}))
        self.overlay.open_flight(view)
        self.assertEqual(self.overlay.tab_keys(), ["overview", "operations", "notes", "history"])  # no Black Box
        header = texts(self.overlay.header)
        for part in ("YSSY → YBBN", "B737-800BCF", "N738BC", "BBC427", "Flight time 1:17",
                     "Touchdown rate not measured", "Manual entry", "family illustration"):
            self.assertIn(part, header)
        overview = texts(self.overlay.tabs.widget(0))
        self.assertIn("Event times are recorded only by Flight Logging", overview)
        self.assertIn("Freight", overview)
        self.overlay.select_tab("operations")
        ops = texts(self.overlay.tabs.currentWidget())
        self.assertIn("At least 18,420 kg (incomplete: baggage unknown)", ops)
        self.assertIn("No standard passenger or crew mass is assumed", ops)
        self.assertIn("Not entered", ops)
        self.overlay.select_tab("notes")
        self.assertEqual(self.overlay.tabs.currentWidget().findChild(QPlainTextEdit).toPlainText(), "Gusty")

    def test_header_persists_across_tabs(self):
        self.overlay.open_flight(flight_view(recorded(callsign="QFA1")))
        for key in self.overlay.tab_keys():
            self.overlay.select_tab(key)
            process()
            self.assertTrue(self.overlay.header.isVisible())
            self.assertIn("QFA1", self.overlay.header.aircraft.text())

    def test_unknown_values_are_labelled(self):
        self.overlay.open_flight(flight_view(manual(aircraft="Mystery Jet", registration="")))
        overview = texts(self.overlay.tabs.widget(0))
        self.assertIn("Not recorded", overview)      # registration, distance, landings
        self.assertIn("Not measured", overview)      # touchdown rate
        self.assertIn("Generic profile: no artwork for this type", texts(self.overlay.header))

    def test_recorder_black_box_tab_loads_journal_lazily(self):
        ev = evidence(gaps=[{"reason": "Simulator disconnected", "start": "2026-11-20T08:40:00+00:00",
                             "end": "2026-11-20T08:41:00+00:00"}])
        view = flight_view(recorded(ev=ev, acks=((0, "known outage"),)))
        self.overlay.open_flight(view)
        self.assertIn("black_box", self.overlay.tab_keys())
        self.assertEqual(self.loads, [])
        self.overlay.select_tab("black_box")
        self.assertEqual(self.loads, [view.id])
        black_box = self.overlay.tabs.currentWidget()
        content = texts(black_box)
        self.assertIn("test journal unavailable", content)
        tables = table_texts(black_box)
        self.assertIn("Simulator disconnected", tables)
        self.assertIn("known outage", tables)
        self.assertIn("Off-block", tables)  # timeline falls back to the saved evidence
        self.assertIn("-180 fpm", tables + content)
        self.overlay.select_tab("overview")
        self.overlay.select_tab("black_box")
        self.assertEqual(len(self.loads), 1)  # loaded once per opening

    def test_simulated_flight_is_flagged_everywhere(self):
        self.overlay.open_flight(flight_view(recorded(ev=evidence(simulated=True))), "black_box")
        self.assertIn("Simulated data", texts(self.overlay.header))
        self.assertIn("not from Microsoft Flight Simulator", texts(self.overlay.tabs.currentWidget()))

    def test_record_history_shows_originals_and_corrections(self):
        record = recorded(arrival="YBBN")
        record["revisions"].append({"saved_at": "2026-11-21T08:00:00.000+00:00", "by": "pilot",
                                    "reason": "Recorder lost the arrival",
                                    "fields": dict(record["revisions"][0]["fields"], arrival="YBCG")})
        self.overlay.open_flight(flight_view(record), "history")
        page = self.overlay.tabs.currentWidget()
        tables = table_texts(page)
        self.assertIn("To | Recorder | YBBN | YBCG | 1", tables)
        self.assertIn("Aircraft type | Observed by the Recorder", tables)
        self.assertIn("Recorder lost the arrival", tables)
        self.assertIn("To: YBBN → YBCG", tables)
        self.assertIn("Original values", tables)

    def test_imported_flight(self):
        self.overlay.open_flight(flight_view(imported()), "history")
        page_text = texts(self.overlay.tabs.currentWidget())
        self.assertIn("CSV", page_text)
        self.assertIn("log.csv line 4", page_text)
        self.assertNotIn("black_box", self.overlay.tab_keys())
        self.assertIn("does not include flight event times", texts(self.overlay.tabs.widget(0)))

    def test_confirmed_diversion_only(self):
        self.overlay.open_flight(flight_view(manual(extra={"diversion": {"confirmed": True, "planned_arrival": "YSSY",
                                                                          "reason": "Weather"}})))
        self.assertIn("(diverted)", self.overlay.header.route.text())
        self.assertIn("Weather", texts(self.overlay.tabs.widget(0)))
        self.overlay.open_flight(flight_view(manual(extra={"diversion": {"confirmed": False, "planned_arrival": "YSSY"}})))
        self.assertNotIn("diverted", self.overlay.header.route.text())
        self.assertNotIn("Planned destination", texts(self.overlay.tabs.widget(0)))

    def test_resize_clamps_and_maximise(self):
        self.overlay.open_flight(flight_view(manual()))
        self.overlay.resize_panel(100, 100)
        self.assertEqual(self.overlay.panel.size(), MIN_PANEL)
        self.overlay.resize_panel(5000, 5000)
        self.assertEqual(self.overlay.panel.width(), 1200 - 32)
        self.assertEqual(self.overlay.panel.height(), 800 - 32)
        self.overlay.resize_panel(800, 600)
        self.overlay.toggle_maximised()
        self.assertTrue(self.overlay.maximised)
        self.assertEqual(self.overlay.panel.geometry(), self.overlay.rect().adjusted(16, 16, -16, -16))
        self.overlay.toggle_maximised()
        self.assertEqual((self.overlay.panel.width(), self.overlay.panel.height()), (800, 600))
        self.host.resize(700, 500)  # host shrinks: panel follows, never larger than host
        process()
        self.assertTrue(self.host.rect().contains(self.overlay.panel.geometry()))

    def test_grip_drag_and_keyboard_resize(self):
        self.overlay.open_flight(flight_view(manual()))
        self.overlay.resize_panel(800, 600)
        grip = self.overlay.grip
        QTest.mousePress(grip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(5, 5))
        QTest.mouseMove(grip, QPoint(65, 45))
        QTest.mouseRelease(grip, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(65, 45))
        self.assertEqual((self.overlay.panel.width(), self.overlay.panel.height()), (860, 640))
        QTest.keyClick(self.overlay.tabs, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.overlay.panel.width(), 820)

    def test_escape_closes_and_restores_focus(self):
        closed = []
        self.overlay.closed.connect(closed.append)
        view = flight_view(manual())
        self.overlay.open_flight(view)
        process()
        QTest.keyClick(self.overlay.tabs, Qt.Key.Key_Escape)
        process()
        self.assertFalse(self.overlay.isVisible())
        self.assertEqual(closed, [view.id])
        self.assertIs(self.app.focusWidget(), self.field)

    def test_edit_button(self):
        edits = []
        self.overlay.edit_requested.connect(edits.append)
        view = flight_view(manual())
        self.overlay.open_flight(view)
        self.overlay.edit_button.click()
        self.assertEqual(edits, [view.id])
        record = manual()
        record["schema"] = 9
        self.overlay.open_flight(flight_view(record))
        self.assertFalse(self.overlay.edit_button.isEnabled())
        self.assertTrue(self.overlay.read_only_note.isVisibleTo(self.overlay))
        self.overlay.open_flight(view)
        self.overlay.set_can_edit(False, "Read-only folder")
        self.assertFalse(self.overlay.edit_button.isEnabled())
        self.assertEqual(self.overlay.edit_button.toolTip(), "Read-only folder")

    def test_backdrop_paints(self):
        self.overlay.open_flight(flight_view(recorded()))
        image = self.host.grab()
        self.assertFalse(image.isNull())


class JournalLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def test_black_box_with_real_journal_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "recorder" / "archive").mkdir(parents=True)
            lines = [{"kind": "takeoff", "phase": "takeoff_roll", "host_time": "2026-11-20T08:15:00+00:00",
                      "sim_time_s": 10.0, "sim_utc": "2026-11-20T08:15:00+00:00", "data": {}}]
            lines += [{"kind": "sample", "phase": "airborne", "host_time": "2026-11-20T08:16:00+00:00",
                       "sim_time_s": 10.0 + i, "sim_utc": None, "data": {"altitude_ft": 100.0 * i}} for i in range(60)]
            lines += [{"kind": "touchdown", "phase": "landing_roll", "host_time": "2026-11-20T09:20:00+00:00",
                       "sim_time_s": 80.0, "sim_utc": "2026-11-20T09:20:00+00:00", "data": {"rate_fpm": -142.0}}]
            (root / "recorder" / "archive" / "x.jsonl").write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
            host = QWidget()
            host.resize(1100, 800)
            host.show()
            overlay = FlightDetailsOverlay(host, journal_loader=journal_loader_for(root))
            overlay.open_flight(flight_view(recorded()), "black_box")
            process()
            page = overlay.tabs.currentWidget()
            chart = page.findChild(ProfileChart)
            self.assertEqual(len(chart.points), 60)
            self.assertEqual([m.label for m in chart.markers], ["Takeoff", "Touchdown"])
            self.assertIn("60 track points", chart.accessibleDescription())
            self.assertIn("-142 fpm", table_texts(page))
            self.assertIsNotNone(overlay.journal())
            host.grab()
            host.deleteLater()

    def test_loader_errors_never_escape(self):
        host = QWidget()
        overlay = FlightDetailsOverlay(host, journal_loader=lambda view: 1 / 0)
        overlay.open_flight(flight_view(recorded()), "black_box")
        self.assertIn("could not be read", texts(overlay.tabs.currentWidget()))
        host.deleteLater()


if __name__ == "__main__":
    unittest.main()
