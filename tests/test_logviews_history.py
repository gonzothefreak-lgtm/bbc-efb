"""Flight History: filters, model/proxy correctness, 2,000-flight performance, view modes,
coordinated selection and preserved browsing context."""

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
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from bbc_efb.logbook_views import filters as f
from bbc_efb.logbook_views.details import FlightDetailsOverlay
from bbc_efb.logbook_views.history import COLUMN_KEYS, DEFAULT_HIDDEN, FlightHistoryPage
from bbc_efb.logbook_views.model import ROLE_FLIGHT, ROLE_ID, flight_views


def views_of(records):
    views, failures = flight_views(records)
    assert not failures, failures
    return views


class FilterLogicTests(unittest.TestCase):
    def setUp(self):
        self.a = views_of([manual(callsign="QFA1", aircraft="B738", departure="YSSY", arrival="YMML",
                                  flight_date="2026-11-01", notes="Night approach", extra={"operation": "airline"})])[0]
        self.b = views_of([recorded(callsign="CPA2", aircraft="A333", departure="VHHH", arrival="YSSY",
                                    flight_date="2026-06-01",
                                    ev=evidence(gaps=[{"reason": "Disconnected"}]))])[0]
        self.c = views_of([recorded(callsign="QFA3", aircraft="b738", departure="YBBN", arrival="YBCS",
                                    flight_date="2025-01-01", ev=evidence(simulated=True, gaps=[{"reason": "x"}]),
                                    acks=((0, ""),))])[0]
        self.d = views_of([imported(callsign="", aircraft="C172", flight_date="bad", departure="YMAY", arrival="YSWG",
                                    extra={"diversion": {"confirmed": True, "planned_arrival": "YSCB"}})])[0]
        self.all = [self.a, self.b, self.c, self.d]

    def ids(self, state):
        return {v.id for v in f.apply(self.all, state)}

    def test_empty_filter_matches_everything(self):
        self.assertEqual(self.ids(f.FilterState()), {v.id for v in self.all})
        self.assertFalse(f.FilterState().active)

    def test_search_tokens_all_required(self):
        self.assertEqual(self.ids(f.FilterState(text="qfa")), {self.a.id, self.c.id})
        self.assertEqual(self.ids(f.FilterState(text="QFA ymml")), {self.a.id})
        self.assertEqual(self.ids(f.FilterState(text="night")), {self.a.id})
        self.assertEqual(self.ids(f.FilterState(text="november 2026")), {self.a.id})
        self.assertEqual(self.ids(f.FilterState(text="qfa zzz")), set())

    def test_date_range_hides_unknown_dates(self):
        state = f.FilterState(date_from=date(2026, 1, 1), date_to=date(2026, 12, 31))
        self.assertEqual(self.ids(state), {self.a.id, self.b.id})
        self.assertEqual(self.ids(f.FilterState(date_to=date(2025, 12, 31))), {self.c.id})

    def test_aircraft_is_type_identity(self):
        self.assertEqual(self.ids(f.FilterState(aircraft=frozenset({" b738 "}))), {self.a.id, self.c.id})

    def test_airport_from_to_and_confirmed_diversion(self):
        self.assertEqual(self.ids(f.FilterState(airport="yssy")), {self.a.id, self.b.id})
        self.assertEqual(self.ids(f.FilterState(airport="YSCB")), {self.d.id})

    def test_source_gaps_operation_simulated(self):
        self.assertEqual(self.ids(f.FilterState(sources=frozenset({"recorder"}))), {self.b.id, self.c.id})
        self.assertEqual(self.ids(f.FilterState(sources=frozenset({"manual", "imported"}))), {self.a.id, self.d.id})
        self.assertEqual(self.ids(f.FilterState(gaps="with")), {self.b.id, self.c.id})
        self.assertEqual(self.ids(f.FilterState(gaps="needs_review")), {self.b.id})
        self.assertEqual(self.ids(f.FilterState(gaps="without")), {self.a.id, self.d.id})
        self.assertEqual(self.ids(f.FilterState(operation="AIRLINE")), {self.a.id})
        self.assertEqual(self.ids(f.FilterState(include_simulated=False)), {self.a.id, self.b.id, self.d.id})

    def test_presets(self):
        today = date(2024, 3, 1)
        self.assertEqual(f.preset_range("30d", today), (date(2024, 2, 1), today))
        self.assertEqual(f.preset_range("year", today), (date(2024, 1, 1), today))
        self.assertEqual(f.preset_range("12m", date(2024, 2, 29)), (date(2023, 3, 1), date(2024, 2, 29)))
        self.assertEqual(f.preset_range("all", today), (None, None))

    def test_describe(self):
        text = f.FilterState(text="qfa", airport="yssy", gaps="with", sources=frozenset({"manual"})).describe()
        for part in ("qfa", "YSSY", "data gaps", "Manual"):
            self.assertIn(part, text)


class FilterBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def test_debounced_search_and_reset(self):
        bar = f.FilterBar(today=lambda: TODAY)
        seen = []
        bar.changed.connect(seen.append)
        bar.search.setText("qfa")
        self.assertEqual(seen, [])  # debounced
        process(f.SEARCH_DEBOUNCE_MS + 80)
        self.assertEqual(seen[-1].text, "qfa")
        bar.date_preset.setCurrentIndex(bar.date_preset.findData("90d"))
        self.assertEqual(seen[-1].date_from, date(2026, 8, 27))
        self.assertTrue(bar.reset_button.isEnabled())
        bar.reset()
        self.assertEqual(seen[-1], f.FilterState())
        self.assertFalse(bar.reset_button.isEnabled())

    def test_set_state_and_options(self):
        bar = f.FilterBar(today=lambda: TODAY)
        views = views_of([manual(aircraft="B738", extra={"operation": "freight"}), manual(aircraft="A320")])
        bar.set_options(views)
        self.assertEqual([bar.aircraft.itemData(i) for i in range(bar.aircraft.count())], ["", "A320", "B738"])
        bar.set_state(f.FilterState(aircraft=frozenset({"B738"}), sources=frozenset({"manual"}), gaps="with",
                                    date_from=date(2026, 1, 1), date_to=date(2026, 2, 1)))
        self.assertEqual(bar.aircraft.currentData(), "B738")
        self.assertEqual(bar.date_preset.currentData(), "custom")
        self.assertFalse(bar.date_from.isHidden())
        state = bar.state()
        self.assertEqual((state.date_from, state.date_to), (date(2026, 1, 1), date(2026, 2, 1)))
        bar.set_options(views[1:])  # active aircraft filter stays visible even if no longer listed
        self.assertEqual(bar.aircraft.currentData(), "B738")
        bar.set_state(f.FilterState(aircraft=frozenset({"ZZZZ"})))
        self.assertEqual(bar.aircraft.currentData(), "ZZZZ")


class HistoryPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()
        cls.records = many(2000)
        cls.views = views_of(cls.records)

    def make_page(self, **kwargs):
        host = QWidget()
        host.resize(1400, 900)
        page = FlightHistoryPage(host, today=lambda: TODAY, **kwargs)
        page.details = FlightDetailsOverlay(host)
        page.resize(1400, 900)
        host.show()
        process()
        self.addCleanup(host.deleteLater)
        return host, page

    def test_2000_flights_are_fast_and_filters_match_pure_logic(self):
        _host, page = self.make_page()
        started = time.perf_counter()
        page.set_flights(self.views)
        process()
        load_s = time.perf_counter() - started
        self.assertEqual(page.proxy.rowCount(), 2000)
        states = [
            f.FilterState(text="bbc01"),
            f.FilterState(aircraft=frozenset({"B738"})),
            f.FilterState(airport="YSSY", sources=frozenset({"recorder"})),
            f.FilterState(gaps="needs_review"),
            f.FilterState(date_from=date(2025, 1, 1), date_to=date(2025, 6, 30), operation="freight"),
            f.FilterState(include_simulated=False, gaps="with"),
        ]
        worst = 0.0
        for state in states:
            started = time.perf_counter()
            page.set_filter(state)
            elapsed = time.perf_counter() - started
            worst = max(worst, elapsed)
            expected = {v.id for v in f.apply(self.views, state)}
            self.assertEqual(set(page.proxy.visible_ids()), expected, state)
            self.assertIn(f"of 2,000 flights", page.summary.text())
        started = time.perf_counter()
        page.set_filter(f.FilterState())
        page.table.sortByColumn(COLUMN_KEYS.index("flight_time"), Qt.SortOrder.AscendingOrder)
        sort_s = time.perf_counter() - started
        # Generous ceilings for slow CI machines; typical figures are a few ms.
        self.assertLess(load_s, 2.0, f"load took {load_s:.3f}s")
        self.assertLess(worst, 0.5, f"slowest filter took {worst:.3f}s")
        self.assertLess(sort_s, 1.0, f"sort took {sort_s:.3f}s")
        page.set_view_mode("cards")
        started = time.perf_counter()
        page.cards.grab()
        page.cards.verticalScrollBar().setValue(page.cards.verticalScrollBar().maximum())
        page.cards.grab()
        paint_s = time.perf_counter() - started
        self.assertLess(paint_s, 2.0, f"card painting took {paint_s:.3f}s")

    def test_sorting_unknown_values_sort_lowest(self):
        _host, page = self.make_page()
        page.set_flights(views_of([manual(minutes=None, callsign="U"), manual(minutes=50, callsign="M"),
                                   manual(minutes=10, callsign="S")]))
        page.table.sortByColumn(COLUMN_KEYS.index("flight_time"), Qt.SortOrder.AscendingOrder)
        order = [page.proxy.flight_at(r).callsign for r in range(3)]
        self.assertEqual(order, ["U", "S", "M"])
        index = page.proxy.index(0, COLUMN_KEYS.index("flight_time"))
        self.assertEqual(index.data(), "—")
        self.assertEqual(index.data(Qt.ItemDataRole.ToolTipRole), "Unknown")

    def test_sorting_keeps_selection_and_survives_reload(self):
        _host, page = self.make_page()
        page.set_flights(self.views)
        chosen = page.proxy.flight_at(25).id
        page.select_flight(chosen)
        page.table.sortByColumn(COLUMN_KEYS.index("callsign"), Qt.SortOrder.AscendingOrder)
        self.assertEqual(page.selected_id(), chosen)
        callsigns = [page.proxy.flight_at(r).callsign for r in range(page.proxy.rowCount())]
        self.assertEqual(callsigns, sorted(callsigns, key=str.casefold))
        page.set_flights(list(reversed(self.views)))  # reload keeps the chosen sort and selection
        self.assertEqual([page.proxy.flight_at(r).callsign for r in range(5)], callsigns[:5])
        self.assertEqual(page.selected_id(), chosen)
        page.set_filter(f.FilterState(sources=frozenset({"manual"})))
        manual_calls = [page.proxy.flight_at(r).callsign for r in range(page.proxy.rowCount())]
        self.assertEqual(manual_calls, sorted(manual_calls, key=str.casefold))

    def test_date_sort_newest_first_by_default(self):
        _host, page = self.make_page()
        page.set_flights(views_of([manual(flight_date="2026-01-02", callsign="B"),
                                   manual(flight_date="2026-03-02", callsign="C"),
                                   manual(flight_date="2025-12-31", callsign="A")]))
        self.assertEqual([page.proxy.flight_at(r).callsign for r in range(3)], ["C", "B", "A"])

    def test_view_modes_share_model_and_selection(self):
        _host, page = self.make_page(view_mode="table")
        page.set_flights(self.views[:100])
        modes = []
        page.view_mode_changed.connect(modes.append)
        self.assertTrue(page.table.isVisible())
        self.assertFalse(page.cards.isVisible())
        page.set_view_mode("combined")
        self.assertTrue(page.table.isVisible() and page.cards.isVisible())
        self.assertIs(page.cards.model(), page.proxy)
        self.assertIs(page.cards.selectionModel(), page.table.selectionModel())
        target = page.proxy.flight_at(5).id
        page.cards.setCurrentIndex(page.proxy.index(5, 0))
        self.assertEqual(page.selected_id(), target)
        self.assertTrue(page.table.selectionModel().isRowSelected(5, page.table.rootIndex()))
        page.set_view_mode("cards")
        self.assertEqual(modes, ["combined", "cards"])
        self.assertEqual(page.selected_id(), target)
        with self.assertRaises(ValueError):
            page.set_view_mode("gallery")

    def test_hidden_columns(self):
        _host, page = self.make_page()
        self.assertEqual(page.hidden_columns, DEFAULT_HIDDEN)
        seen = []
        page.columns_changed.connect(seen.append)
        page.set_hidden_columns({"registration", "date", "nonsense"})
        self.assertEqual(page.hidden_columns, frozenset({"registration"}))  # date can never be hidden
        self.assertTrue(page.table.isColumnHidden(COLUMN_KEYS.index("registration")))
        self.assertFalse(page.table.isColumnHidden(COLUMN_KEYS.index("block")))
        self.assertEqual(seen, [frozenset({"registration"})])

    def test_opening_details_preserves_browsing_context(self):
        host, page = self.make_page()
        page.set_flights(self.views)
        page.set_filter(f.FilterState(sources=frozenset({"recorder"})))
        page.table.sortByColumn(COLUMN_KEYS.index("callsign"), Qt.SortOrder.AscendingOrder)
        page.select_flight(page.proxy.flight_at(300).id)
        page.table.verticalScrollBar().setValue(page.table.verticalScrollBar().maximum() // 2)
        page.cards.verticalScrollBar().setValue(page.cards.verticalScrollBar().maximum() // 3)
        page.table.setFocus()
        process()
        before = page.browsing_context()
        opened = []
        page.flight_opened.connect(opened.append)
        page.open_selected()
        process()
        self.assertTrue(page.details.is_open)
        self.assertEqual(opened, [before["selected"]])
        self.assertEqual(page.details.geometry(), host.rect())
        self.assertEqual(page.browsing_context(), before)
        QTest.keyClick(page.details.tabs, Qt.Key.Key_Escape)
        process()
        self.assertFalse(page.details.is_open)
        self.assertEqual(page.browsing_context(), before)
        self.assertTrue(page.table.hasFocus() or self.app.focusWidget() is page.table)

    def test_keyboard_activation_opens_details(self):
        _host, page = self.make_page(view_mode="table")
        page.set_flights(self.views[:20])
        page.table.setFocus()
        page.select_flight(page.proxy.flight_at(3).id)
        QTest.keyClick(page.table, Qt.Key.Key_Return)
        process()
        self.assertTrue(page.details.is_open)
        self.assertEqual(page.details.view.id, page.proxy.flight_at(3).id)

    def test_refresh_keeps_selection_and_updates_open_details(self):
        _host, page = self.make_page()
        records = [manual(callsign=f"F{i}", flight_date=f"2026-01-{i + 1:02d}") for i in range(10)]
        page.set_flights(views_of(records))
        target = records[4]["id"]
        page.open_flight(target)
        records[4]["revisions"].append({"saved_at": "2026-11-23T00:00:00.000+00:00", "by": "pilot",
                                        "reason": "typo", "fields": dict(records[4]["revisions"][0]["fields"], callsign="FIXED")})
        page.set_flights(views_of(records))
        self.assertEqual(page.selected_id(), target)
        self.assertEqual(page.details.view.callsign, "FIXED")
        page.set_flights(views_of(records[:4]))  # flight removed outside the EFB
        self.assertTrue(page.details.read_only_note.isVisibleTo(page.details))
        self.assertFalse(page.details.edit_button.isEnabled())

    def test_empty_states_and_counts(self):
        _host, page = self.make_page()
        page.set_flights([])
        self.assertIn("No flights in the Logbook", page.empty.text())
        page.set_flights(self.views[:10])
        self.assertEqual(page.summary.text(), "10 flights")
        page.set_filter(f.FilterState(text="no-such-flight"))
        self.assertTrue(page.empty.isVisibleTo(page))
        self.assertIn("Showing 0 of 10 flights", page.summary.text())

    def test_edit_buttons_respect_read_only(self):
        _host, page = self.make_page()
        record = manual()
        record["schema"] = 3
        page.set_flights(views_of([record, manual()]))
        edits = []
        page.edit_requested.connect(edits.append)
        page.select_flight(record["id"])
        self.assertFalse(page.edit_button.isEnabled())
        self.assertIn("newer", page.edit_button.toolTip())
        other = page.proxy.flight_at(0).id if page.proxy.flight_at(0).id != record["id"] else page.proxy.flight_at(1).id
        page.select_flight(other)
        self.assertTrue(page.edit_button.isEnabled())
        page.edit_button.click()
        self.assertEqual(edits, [other])
        page.set_can_edit(False, "The Logbook folder is read-only.")
        self.assertFalse(page.edit_button.isEnabled())
        self.assertFalse(page.new_button.isEnabled())

    def test_model_roles(self):
        _host, page = self.make_page()
        page.set_flights(self.views[:3])
        index = page.proxy.index(0, 0)
        self.assertIsNotNone(index.data(ROLE_FLIGHT))
        self.assertEqual(index.data(ROLE_ID), index.data(ROLE_FLIGHT).id)
        self.assertIn("Date:", index.data(Qt.ItemDataRole.AccessibleTextRole))


if __name__ == "__main__":
    unittest.main()
