"""Record-based summaries, Dashboard, Statistics page and the Logbook workspace."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import os
import time
import unittest
from datetime import date

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from tests.test_logviews_support import TODAY, app, evidence, imported, manual, many, process, recorded
except ImportError:  # discovered without the package prefix
    from test_logviews_support import TODAY, app, evidence, imported, manual, many, process, recorded  # type: ignore[no-redef]

from PySide6.QtCore import Qt

from bbc_efb.logbook_views.dashboard import DashboardPage, LogbookWorkspace
from bbc_efb.logbook_views.filters import FilterState
from bbc_efb.logbook_views.model import flight_views
from bbc_efb.logbook_views.statistics import StatisticsPage, rate_distribution, summarise


def views_of(records):
    views, failures = flight_views(records)
    assert not failures, failures
    return views


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            manual(aircraft="B738", minutes=60, flight_date="2026-11-01", extra={"operation": "airline"}),
            manual(aircraft="B738", minutes=None, flight_date="2026-10-01"),
            recorded(aircraft="A320", minutes=None, ev=evidence(block_s=7200.0, airborne_s=6000.0, touchdown_rate_fpm=-250.0,
                                                               distance_nm=300.0), flight_date="2026-11-10"),
            recorded(aircraft="A320", ev=evidence(touchdown_rate_fpm=None, landing=None, distance_nm=None,
                                                  gaps=[{"reason": "x"}]), flight_date="2026-09-15"),
            recorded(aircraft="C172", ev=evidence(simulated=True, touchdown_rate_fpm=-50.0), flight_date="2026-11-11"),
            imported(aircraft="", minutes=30, flight_date="2025-01-01", freight_kg=1000, extra={"touchdown_rate_fpm": 120}),
            imported(aircraft="E190", minutes=45, flight_date="nope", extra={"operation": "freight"}),
        ]
        self.views = views_of(self.records)

    def test_totals_count_unknowns(self):
        s = summarise(self.views, today=TODAY)
        self.assertEqual(s.flights, 6)
        self.assertEqual(s.simulated_excluded, 1)
        self.assertEqual(dict(s.by_source), {"manual": 2, "recorder": 2, "imported": 2})
        # 60 min + 7200 s block + 90 min (recorder minutes) + 30 + 45 min; one manual unknown
        self.assertEqual(s.time.known, 5)
        self.assertEqual(s.time.unknown, 1)
        self.assertAlmostEqual(s.time.value, 3600 + 7200 + 5400 + 1800 + 2700)
        self.assertEqual((s.distance_nm.value, s.distance_nm.known, s.distance_nm.unknown), (300.0, 1, 5))
        self.assertEqual((s.landings.value, s.landings.known), (1, 1))  # only the measured landing
        self.assertEqual((s.block.known, s.block.unknown), (2, 0))

    def test_touchdown_rates_only_measured(self):
        s = summarise(self.views, today=TODAY)
        self.assertEqual(s.rates.measured, 2)            # recorder -250 and imported 120
        self.assertEqual(s.rates.imported, 1)
        self.assertEqual(s.rates.unmeasured, 1)          # the recorder flight without a rate
        self.assertEqual(s.rates.best_fpm, 120)
        self.assertEqual(s.rates.worst_fpm, 250)
        self.assertEqual(s.rates.average_fpm, 185)
        with_sim = summarise(self.views, include_simulated=True, today=TODAY)
        self.assertEqual(with_sim.rates.best_fpm, 50)
        self.assertEqual(with_sim.flights, 7)

    def test_distribution_bands(self):
        bands = dict(rate_distribution([0, 99.9, 100, 260, 999]))
        self.assertEqual(bands["< 100"], 2)
        self.assertEqual(bands["100–150"], 1)
        self.assertEqual(bands["250–300"], 1)
        self.assertEqual(bands["≥ 400"], 1)

    def test_profiles_operations_airports_monthly(self):
        s = summarise(self.views, today=TODAY)
        self.assertEqual([p.key for p in s.aircraft], ["A320", "B738", "E190"])
        b738 = next(p for p in s.aircraft if p.key == "B738")
        self.assertEqual((b738.flights, b738.time.known, b738.time.unknown), (2, 1, 1))
        self.assertEqual(b738.last_flown, date(2026, 11, 1))
        self.assertEqual(s.aircraft_unknown, 1)
        self.assertEqual(dict(s.operations), {"Airline": 1, "Freight": 1})
        self.assertEqual(s.operations_unset, 4)
        self.assertEqual(s.airports[0], ("YBBN", 6))
        self.assertEqual(len(s.monthly), 12)
        self.assertEqual(s.monthly[-1][0], "2026-11")
        self.assertEqual(s.monthly[-1][2], 2)
        self.assertEqual(s.undated, 1)
        self.assertEqual(s.freight.freight_kg.value, 1000)
        self.assertEqual(s.freight.freight_kg.unknown, 5)
        self.assertEqual(s.with_gaps, 1)
        self.assertEqual(s.needing_review, 2)  # the gap flight and the imported record with a bad date

    def test_recent_newest_first(self):
        s = summarise(self.views, today=TODAY, recent=3)
        self.assertEqual([v.flight_date for v in s.recent], [date(2026, 11, 11), date(2026, 11, 10), date(2026, 11, 1)])

    def test_empty_logbook(self):
        s = summarise([], today=TODAY)
        self.assertEqual(s.flights, 0)
        self.assertIsNone(s.rates.average_fpm)
        self.assertEqual(s.rates.distribution, ())

    def test_2000_flight_summary_is_fast(self):
        views = views_of(many(2000))
        started = time.perf_counter()
        s = summarise(views, today=TODAY)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, f"summary took {elapsed:.3f}s")
        self.assertEqual(s.flights + s.simulated_excluded, 2000)
        self.assertEqual(s.time.known + s.time.unknown, s.flights)


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def test_recent_flights_open_with_return_and_keypad_enter(self):
        """A120: the Dashboard's Recent flights table opens the selected flight from the keyboard."""
        from PySide6.QtTest import QTest

        page = DashboardPage(today=lambda: TODAY)
        page.resize(1400, 1000)
        page.show()
        self.addCleanup(page.deleteLater)
        page.set_flights(views_of([manual(aircraft="B738", minutes=60), manual(aircraft="DH8D", minutes=30)]))
        opened = []
        page.flight_opened.connect(opened.append)
        page.recent_table.setFocus()
        page.recent_table.setCurrentIndex(page.recent_model.index(1, 0))
        QTest.keyClick(page.recent_table, Qt.Key.Key_Return)
        QTest.keyClick(page.recent_table, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
        process()
        self.assertEqual(len(opened), 2)
        self.assertEqual(opened[0], opened[1])
        self.assertEqual(page.recent_table.accessibleName(), "Recent flights")

    def test_dashboard_metrics_are_honest(self):
        page = DashboardPage(today=lambda: TODAY)
        page.resize(1400, 1000)
        page.set_flights(views_of([manual(minutes=None), manual(minutes=90, aircraft="A320"),
                                   recorded(ev=evidence(touchdown_rate_fpm=None))]))
        m = page.metrics
        self.assertEqual(m["flights"].value.text(), "3")
        self.assertEqual(m["time"].note.text(), "1 flight without a known time")
        self.assertEqual(m["distance"].value.text(), "Not recorded")
        self.assertEqual(m["rate"].value.text(), "Not measured")
        self.assertIn("1 without a measured rate", m["rate"].note.text())
        self.assertEqual(m["landings"].value.text(), "1")
        self.assertIn("2 flights without a landing count", m["landings"].note.text())
        self.assertIn("No measured touchdown rates", page.rate_chart.accessibleDescription())
        page.grab()

    def test_dashboard_aircraft_cards_and_signals(self):
        page = DashboardPage(today=lambda: TODAY)
        page.resize(1400, 1000)
        page.show()
        records = [manual(aircraft="B738", minutes=60) for _ in range(3)] + [manual(aircraft="DH8D", minutes=30)]
        page.set_flights(views_of(records))
        visible = [c for c in page.aircraft_cards if c.isVisibleTo(page)]
        self.assertEqual([c.type_key for c in visible], ["B738", "DH8D"])
        self.assertIn("3 flights | 3.0 h", visible[0].facts.text())
        selected, navigate, opened = [], [], []
        page.aircraft_selected.connect(selected.append)
        page.navigate_requested.connect(navigate.append)
        page.flight_opened.connect(opened.append)
        visible[1].click()
        page.link_buttons["statistics"].click()
        page.all_aircraft.click()
        self.assertEqual(selected, ["DH8D"])
        self.assertEqual(navigate, ["statistics", "statistics"])
        page.recent_table.doubleClicked.emit(page.recent_model.index(0, 0))
        self.assertEqual(len(opened), 1)
        page.grab()
        page.deleteLater()

    def test_dashboard_excludes_simulated_with_note(self):
        page = DashboardPage(today=lambda: TODAY)
        page.set_flights(views_of([recorded(ev=evidence(simulated=True)), manual()]))
        self.assertEqual(page.metrics["flights"].value.text(), "1")
        self.assertIn("1 simulated-data flight not included", page.data_note.text())

    def test_statistics_page_renders(self):
        page = StatisticsPage(today=lambda: TODAY)
        page.resize(1400, 1200)
        page.set_flights(views_of(many(300)))
        self.assertTrue(page.monthly.bars)
        self.assertTrue(page.by_aircraft_time.segments)
        self.assertIn("simulated-data flight", page.excluded_note.text())
        flights_before = page.summary.flights
        page.include_simulated.setChecked(True)
        self.assertGreater(page.summary.flights, flights_before)
        self.assertEqual(page.excluded_note.text(), "")
        page.grab()

    def test_statistics_empty(self):
        page = StatisticsPage(today=lambda: TODAY)
        page.set_flights([])
        self.assertEqual(page.metrics["flights"].value.text(), "0")
        self.assertEqual(page.metrics["rate_avg"].value.text(), "Not measured")
        page.grab()


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def make(self, **kwargs):
        workspace = LogbookWorkspace(today=lambda: TODAY, **kwargs)
        workspace.resize(1500, 950)
        workspace.show()
        process()
        self.addCleanup(workspace.deleteLater)
        return workspace

    def test_navigation_and_lazy_refresh(self):
        ws = self.make()
        self.assertEqual(ws.current_page, "dashboard")
        ws.set_flights(many(200))
        self.assertEqual(ws.dashboard.summary.flights + ws.dashboard.summary.simulated_excluded, 200)
        self.assertIn("statistics", ws._dirty)            # hidden pages wait until shown
        self.assertEqual(ws.statistics.summary.flights, 0)
        ws.navigate("statistics")
        self.assertNotIn("statistics", ws._dirty)
        self.assertGreater(ws.statistics.summary.flights, 0)
        self.assertEqual(ws.history.model.rowCount(), 200)  # History is always current
        with self.assertRaises(ValueError):
            ws.navigate("aircraft")                        # Q6: no Aircraft page
        self.assertNotIn("reports", ws.pages)              # Q7: no Reports page

    def test_aircraft_card_filters_history_by_type(self):
        ws = self.make()
        ws.set_flights([manual(aircraft="B738"), manual(aircraft="b738"), manual(aircraft="A320")])
        ws.dashboard.aircraft_selected.emit("B738")
        self.assertEqual(ws.current_page, "history")
        self.assertEqual(ws.history.filter_state().aircraft, frozenset({"B738"}))
        self.assertEqual(ws.history.proxy.rowCount(), 2)

    def test_open_flight_uses_shared_overlay(self):
        ws = self.make()
        record = recorded(ev=evidence(gaps=[{"reason": "x"}]))
        ws.set_flights([record, manual()])
        self.assertTrue(ws.open_flight(record["id"], "black_box"))
        self.assertTrue(ws.details.is_open)
        self.assertEqual(ws.details.current_tab, "black_box")
        self.assertEqual(ws.details.geometry(), ws.rect())
        self.assertFalse(ws.open_flight("missing"))

    def test_recorder_review_opens_black_box(self):
        ws = self.make()
        record = recorded(ev=evidence(gaps=[{"reason": "x"}]))
        ws.set_flights([record])
        ws.navigate("black_box")
        ws.recorder.select_row(0)
        ws.recorder.review_button.click()
        self.assertTrue(ws.details.is_open)
        self.assertEqual(ws.details.current_tab, "black_box")

    def test_unconvertible_records_are_reported_not_hidden(self):
        ws = self.make()
        ws.set_flights([manual(), {"id": "broken"}])
        ws.navigate("black_box")
        self.assertEqual(len(ws.conversion_failures), 1)
        self.assertTrue(any(item.kind == "unreadable" and item.file_name == "broken" for item in ws.recorder.items))

    def test_preferences_round_trip(self):
        ws = self.make(preferences={"view_mode": "cards", "hidden_columns": ["status"]})
        self.assertEqual(ws.history.view_mode, "cards")
        seen = []
        ws.preferences_changed.connect(seen.append)
        ws.history.set_view_mode("table")
        self.assertEqual(seen[-1], {"view_mode": "table", "hidden_columns": ["status"]})

    def test_ctrl_f_focuses_search(self):
        from PySide6.QtTest import QTest
        ws = self.make()
        ws.activateWindow()
        ws.dashboard.setFocus()
        QTest.keyClick(ws, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        process()
        self.assertEqual(ws.current_page, "history")

    def test_history_filter_survives_page_switches(self):
        ws = self.make()
        ws.set_flights(many(50))
        ws.navigate("history")
        ws.history.set_filter(FilterState(text="bbc001"))
        shown = ws.history.proxy.rowCount()
        ws.navigate("dashboard")
        ws.set_flights(many(50))
        ws.navigate("history")
        self.assertEqual(ws.history.filter_state().text, "bbc001")
        self.assertEqual(ws.history.proxy.rowCount(), shown)


if __name__ == "__main__":
    unittest.main()
