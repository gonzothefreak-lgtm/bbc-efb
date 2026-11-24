"""Regression tests for confirmed Logbook UI bugs (editor overflow, Black Box selection and
stale listings, Flight Details refresh, journal parsing, touchdown sort, relative dates,
Recorder fields that need confirmation). Run with QT_QPA_PLATFORM=offscreen."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from bbc_efb.logbook import Logbook  # noqa: E402
from bbc_efb.logbook_views import filters as f  # noqa: E402
from bbc_efb.logbook_views import model as m  # noqa: E402
from bbc_efb.logbook_views.details import FlightDetailsOverlay, journal_loader_for  # noqa: E402
from bbc_efb.logbook_views.history import COLUMNS  # noqa: E402
from bbc_efb.logbook_views.recorder_management import (  # noqa: E402
    StaleListing, StoreActions, attention_items_for,
)
from bbc_efb.storage import FlightFields, FlightStore, Gap, RecorderEvidence  # noqa: E402

try:
    from tests.test_logviews_support import evidence, imported, process, recorded
except ImportError:  # discovered without the package prefix
    from test_logviews_support import evidence, imported, process, recorded  # type: ignore[no-redef]

APP = QApplication.instance() or QApplication([])


def texts(widget):
    return "\n".join(item.text() for item in widget.findChildren(QLabel))


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        self.store = FlightStore(self.root)
        self.warnings = []

    def tearDown(self):
        if hasattr(self, "logbook"):
            self.logbook.close()
            self.logbook.deleteLater()
            process()
        self.store.close()
        self._folder.cleanup()

    def make(self, **kwargs):
        kwargs.setdefault("journal_in_background", False)
        self.logbook = Logbook(self.store, **kwargs)
        self.logbook._warn = lambda title, text: self.warnings.append((title, text))
        self.logbook._ask = lambda *args, **kw: False
        self.logbook.resize(1280, 860)
        self.logbook.show()
        return self.logbook

    def hand_edit(self, flight, change):
        path = self.root / "flights" / f"{flight.id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        change(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def recorded_flight(self, gaps=(), date_text="2024-01-05", arrival="LFPG", extra=None):
        ev = RecorderEvidence("recorder/journals/a.jsonl", "Tape", "simulator", False, gaps=tuple(gaps),
                              **({"extra": extra} if extra else {}))
        return self.store.create_recorded(FlightFields(date_text, "A20N", "EGLL", arrival), ev)


class EditorOverflowTests(StoreCase):
    """A stored number outside the QSpinBox int32 range must not half-load the editor."""

    def test_number_beyond_int32_is_kept_and_nothing_is_wiped(self):
        flight = self.store.save(FlightFields("2024-01-01", "A320", "EGLL", "LFPG", crew=2, passengers=150,
                                              notes="important notes"))
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(
            minutes=3_000_000_000, baggage_kg=-(2**31) - 5))
        logbook = self.make()
        self.assertTrue(logbook.start_editor(logbook.flights[0]))
        editor = logbook.editor
        self.assertEqual(editor.minutes.value(), -1)  # shown as Unknown, not clamped or overflowed
        self.assertIn("shown as Unknown", editor.notice.text())
        values = editor.values()
        self.assertEqual((values.minutes, values.baggage_kg), (3_000_000_000, -(2**31) - 5))
        self.assertEqual((values.crew, values.passengers, values.notes), (2, 150, "important notes"))
        self.assertFalse(editor.dirty)
        editor.text_fields["callsign"].setText("BBC1")
        self.assertTrue(logbook.save_flight())
        saved = self.store.get(flight.id).fields
        self.assertEqual((saved.minutes, saved.crew, saved.passengers, saved.notes, saved.callsign),
                         (3_000_000_000, 2, 150, "important notes", "BBC1"))

    def test_show_value_refuses_a_value_beyond_int32(self):
        logbook = self.make()
        logbook.start_editor(None)
        self.assertFalse(logbook.editor.show_value("minutes", 2**31))
        self.assertTrue(logbook.editor.show_value("minutes", 120))

    def test_a_failed_load_never_leaves_a_saveable_editor(self):
        flight = self.store.save(FlightFields("2024-01-01", "A320", "EGLL", "LFPG", notes="keep"))
        logbook = self.make()
        real_open = logbook.editor.open

        def broken(target, pending=None):
            if target is not None:
                real_open(None)
                raise OverflowError("simulated load failure")
            return real_open(target, pending)
        logbook.editor.open = broken
        with self.assertLogs("bbc_efb.logbook", "ERROR"):
            self.assertFalse(logbook.start_editor(logbook.flights[0]))
        self.assertIsNone(logbook.editing)
        self.assertIsNot(logbook.stack.currentWidget(), logbook.pages["Editor"])
        self.assertFalse(logbook.editor.save_button.isEnabled())
        self.assertTrue(any("could not be opened" in title for title, _ in self.warnings))
        self.assertEqual(len(self.store.get(flight.id).revisions), 1)


class BlackBoxTests(StoreCase):
    def test_accepting_gaps_does_not_move_the_selection_to_another_flight(self):
        self.recorded_flight(gaps=(Gap("Simulator disconnected"),), date_text="2024-01-01", arrival="LF2G")
        newer = self.recorded_flight(gaps=(Gap("Stale feed"),), date_text="2024-02-01", arrival="LF1G")
        logbook = self.make()
        logbook.navigate("black_box")
        page = logbook.black_box
        row = next(i for i, item in enumerate(page.items) if item.flight_id == newer.id and item.kind == "gaps")
        page.select_row(row)
        page.ask_note = lambda item: "ok"
        self.assertTrue(page.accept_selected())
        self.assertIsNone(page.selected_item())
        self.assertFalse(page.accept_button.isEnabled())
        self.assertTrue(any(item.kind == "gaps" for item in page.items))  # the other flight is still queued

    def test_gap_accept_is_refused_when_the_file_changed_after_listing(self):
        flight = self.recorded_flight(gaps=(Gap("Simulator disconnected"), Gap("Stale feed")))
        logbook = self.make()
        logbook.navigate("black_box")
        page = logbook.black_box
        row = next(i for i, item in enumerate(page.items) if item.kind == "gaps")
        page.select_row(row)

        def swap(payload):
            payload["recorder"]["gaps"][0]["reason"] = "Different A"
            payload["recorder"]["gaps"][1]["reason"] = "Different B"
        self.hand_edit(flight, swap)
        errors = []
        page.show_error = lambda title, text: errors.append((title, text))
        page.ask_note = lambda item: "ok"
        self.assertFalse(page.accept_selected())
        self.assertEqual(self.store.get(flight.id).acknowledgements, ())
        self.assertTrue(errors and "changed on disk" in errors[0][1])
        # The page was refreshed and now lists the new gaps for review.
        item = next(item for item in page.items if item.kind == "gaps")
        self.assertIn("Different A", item.detail)

    def test_store_actions_chain_each_acknowledgement(self):
        flight = self.recorded_flight(gaps=(Gap("Simulator disconnected"), Gap("Stale feed")))
        seen = m.flight_view(flight)
        StoreActions(self.store).acknowledge_gaps(flight.id, (0, 1), "seen", seen=seen)
        self.assertEqual([a.gap for a in self.store.get(flight.id).acknowledgements], [0, 1])
        with self.assertRaises(StaleListing):  # the listing is now out of date
            StoreActions(self.store).acknowledge_gaps(flight.id, (0,), "again", seen=seen)


class DetailsRefreshTests(unittest.TestCase):
    def test_flight_coming_back_unchanged_clears_the_gone_note(self):
        host = QWidget()
        host.resize(1100, 800)
        host.show()
        self.addCleanup(host.deleteLater)
        overlay = FlightDetailsOverlay(host)
        view = m.flight_view(recorded())
        overlay.open_flight(view)
        self.assertTrue(overlay.edit_button.isEnabled())
        overlay.refresh_from(lambda _id: None)
        self.assertFalse(overlay.read_only_note.isHidden())
        self.assertFalse(overlay.edit_button.isEnabled())
        overlay.refresh_from(lambda _id: view)
        self.assertTrue(overlay.read_only_note.isHidden())
        self.assertTrue(overlay.edit_button.isEnabled())


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "recorder" / "archive").mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def write(self, lines):
        (self.root / "recorder" / "archive" / "x.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def line(self, kind, sim):
        return json.dumps({"kind": kind, "phase": "airborne", "host_time": "2026-11-20T08:00:00+00:00",
                           "sim_time_s": sim, "sim_utc": None, "data": {"rate_fpm": -150}})

    def test_deeply_nested_line_is_a_torn_line_not_a_lost_journal(self):
        self.write([self.line("takeoff", 1.0), "[" * 100_000, self.line("touchdown", 9.0)])
        contents = m.load_journal(self.root, "recorder/archive/x.jsonl")
        self.assertEqual(contents.torn_lines, 1)
        self.assertEqual(contents.problem, "")
        self.assertEqual([e.kind for e in contents.events], ["takeoff", "touchdown"])

    def test_loader_caches_until_the_file_changes(self):
        self.write([self.line("takeoff", 1.0)])
        loader = journal_loader_for(self.root)
        view = m.flight_view(recorded())
        first = loader(view)
        self.assertIs(loader(view), first)  # not parsed again on a rebuild
        time.sleep(0.01)
        self.write([self.line("takeoff", 1.0), self.line("touchdown", 9.0)])
        self.assertEqual(len(loader(view).events), 2)

    def test_background_load_keeps_the_ui_thread_free_and_fills_the_tab(self):
        self.write([self.line("takeoff", 1.0), self.line("touchdown", 9.0)])
        host = QWidget()
        host.resize(1100, 800)
        host.show()
        self.addCleanup(host.deleteLater)
        overlay = FlightDetailsOverlay(host, journal_loader=journal_loader_for(self.root), load_in_background=True)
        ready = []
        overlay.journal_ready.connect(ready.append)
        view = m.flight_view(recorded())
        overlay.open_flight(view, "black_box")
        self.assertIn("Loading the recording", texts(overlay.tabs.currentWidget()))
        deadline = time.monotonic() + 5
        while not ready and time.monotonic() < deadline:
            process(10)
        self.assertEqual(ready, [view.id])
        self.assertEqual(len(overlay.journal().events), 2)
        self.assertNotIn("Loading the recording", texts(overlay.tabs.currentWidget()))


class TouchdownSortTests(unittest.TestCase):
    def test_rates_sort_by_magnitude_whatever_their_sign(self):
        column = next(c for c in COLUMNS if c.key == "landing_rate")
        views = [m.flight_view(recorded(ev=evidence(touchdown_rate_fpm=-800.0), aircraft="rec firm")),
                 m.flight_view(recorded(ev=evidence(touchdown_rate_fpm=-100.0), aircraft="rec soft")),
                 m.flight_view(imported(extra={"touchdown_rate_fpm": 150.0}, aircraft="imp medium")),
                 m.flight_view(imported(extra={"touchdown_rate_fpm": 600.0}, aircraft="imp firm")),
                 m.flight_view(recorded(ev=evidence(touchdown_rate_fpm=None), aircraft="none"))]
        ordered = [v.aircraft for v in sorted(views, key=column.sort)]
        self.assertEqual(ordered, ["none", "rec soft", "imp medium", "imp firm", "rec firm"])


class RelativeDateTests(unittest.TestCase):
    def test_relative_preset_follows_the_calendar(self):
        today = [date(2026, 11, 24)]
        bar = f.FilterBar(today=lambda: today[0])
        bar.date_preset.setCurrentIndex(bar.date_preset.findData("30d"))
        self.assertEqual(bar.state().date_to, date(2026, 11, 24))
        seen = []
        bar.changed.connect(seen.append)
        self.assertFalse(bar.refresh_relative_dates())  # same day: nothing to do
        today[0] = date(2026, 11, 25)
        self.assertTrue(bar.refresh_relative_dates())
        self.assertEqual(seen[-1].date_to, date(2026, 11, 25))
        flight = m.flight_view(recorded(flight_date="2026-11-25"))
        self.assertTrue(f.compile_filter(bar.state())(flight))

    def test_custom_range_is_never_moved(self):
        bar = f.FilterBar(today=lambda: date(2026, 11, 24))
        bar.set_state(f.FilterState(date_from=date(2026, 1, 1), date_to=date(2026, 2, 1)))
        self.assertFalse(bar.refresh_relative_dates())
        self.assertEqual(bar.state().date_to, date(2026, 2, 1))


class FieldConfirmationTests(StoreCase):
    STATUS = {
        "aircraft": {"status": "needs_confirmation", "reason": "Only the aircraft title was available",
                     "confidence": "low"},
        "arrival": {"status": "ambiguous", "reason": "Two airports fit", "candidate": "LFPO"},
        "departure": {"status": "known", "confidence": "high"},
    }

    def test_view_lists_fields_needing_confirmation(self):
        view = m.flight_view(recorded(ev=evidence(field_status=self.STATUS)))
        self.assertEqual([c.name for c in view.field_checks], ["aircraft", "arrival"])
        self.assertIn("Candidate: LFPO", view.field_checks[1].text)
        self.assertEqual(m.flight_view(recorded()).field_checks, ())

    def test_pilot_correction_clears_the_check(self):
        record = recorded(ev=evidence(field_status=self.STATUS))
        record["revisions"].append({"saved_at": "2026-11-21T10:00:00.000+00:00", "by": "pilot", "reason": "",
                                    "fields": dict(record["revisions"][0]["fields"], aircraft="B38M")})
        self.assertEqual([c.name for c in m.flight_view(record).field_checks], ["arrival"])

    def test_details_queue_and_editor_say_needs_confirmation(self):
        view = m.flight_view(recorded(ev=evidence(field_status=self.STATUS)))
        self.assertIn("confirm_fields", [item.kind for item in attention_items_for(view)])
        host = QWidget()
        host.resize(1100, 800)
        host.show()
        self.addCleanup(host.deleteLater)
        overlay = FlightDetailsOverlay(host)
        overlay.open_flight(view)
        overview = texts(overlay.tabs.widget(0))
        self.assertIn("Needs confirmation", overview)
        self.assertIn("needs confirmation", overview)  # origin note on the field itself

        flight = self.recorded_flight(extra={"field_status": self.STATUS})
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        self.assertIn("Needs confirmation", logbook.editor.notice.text())
        self.assertEqual(flight.id, logbook.editing.id)


if __name__ == "__main__":
    unittest.main()
