"""Offscreen tests for the Logbook section (bbc_efb/logbook.py). Run with QT_QPA_PLATFORM=offscreen."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from PySide6.QtCore import QDate  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from bbc_efb.logbook import STORED_DATE, Logbook  # noqa: E402
from bbc_efb.logbook_views.live_status import describe_recorder_status  # noqa: E402
from bbc_efb.storage import FlightFields, FlightStore, Gap, RecorderEvidence  # noqa: E402

try:
    from tests.test_logviews_support import many
except ImportError:  # discovered without the package prefix
    from test_logviews_support import many  # type: ignore[no-redef]

APP = QApplication.instance() or QApplication([])

LONG_TITLE = "Airbus A320neo FlyByWire Simulations A32NX Development"


def manual(**changes):
    values = dict(flight_date="2024-01-01", aircraft="A320", departure="EGLL", arrival="LFPG")
    values.update(changes)
    return FlightFields(**values)


def texts(widget):
    return "\n".join(item.text() for item in widget.findChildren(QLabel))


class FakePreferences:
    """Shape of bbc_efb.preferences.Preferences, without the hidden-columns key."""

    KNOWN = {"logbook.history_view"}

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.saved = 0
        self.listeners = []
        self.writable = True

    def get(self, key):
        if key not in self.KNOWN:
            raise ValueError(f"{key!r} is not a known preference.")
        return self.values.get(key, "table")

    def set(self, key, value):
        if key not in self.KNOWN:
            raise ValueError(f"{key!r} is not a known preference.")
        self.values[key] = value
        for listener in list(self.listeners):
            listener(key, value)
        return True

    def save(self):
        self.saved += 1
        return True

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None


class LogbookCase(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        self.store = FlightStore(self.root)
        self.warnings = []
        self.answers = []
        self.questions = []

    def tearDown(self):
        if hasattr(self, "logbook"):
            self.logbook.close()
            self.logbook.deleteLater()
        self.store.close()
        self._folder.cleanup()

    def make(self, store="default", **kwargs):
        self.logbook = Logbook(self.store if store == "default" else store, **kwargs)
        self.logbook._warn = lambda title, text: self.warnings.append((title, text))

        def ask(title, text, accept, reject, default_accept=True):
            self.questions.append((title, text, accept, reject))
            return self.answers.pop(0) if self.answers else False
        self.logbook._ask = ask
        self.logbook.resize(1280, 860)
        self.logbook.show()
        return self.logbook

    def path_of(self, flight):
        return self.root / "flights" / f"{flight.id}.json"

    def hand_edit(self, flight, change):
        path = self.path_of(flight)
        payload = json.loads(path.read_text(encoding="utf-8"))
        change(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")


class EditorFidelityTests(LogbookCase):
    def test_long_recorded_title_is_not_truncated_when_another_field_is_edited(self):
        evidence = RecorderEvidence("recorder/a.jsonl", "", "simulator", False)
        flight = self.store.create_recorded(FlightFields("2024-01-01", LONG_TITLE, "EGLL", "LFPG"), evidence)
        logbook = self.make()
        logbook.start_editor(flight)
        self.assertEqual(logbook.editor.text_fields["aircraft"].text(), LONG_TITLE)
        logbook.editor.text_fields["callsign"].setText("baw12")
        logbook.save_flight()
        saved = self.store.get(flight.id)
        self.assertEqual(saved.fields.aircraft, LONG_TITLE)
        self.assertEqual(saved.fields.callsign, "BAW12")
        self.assertEqual(saved.changed_fields(1), ("callsign",))

    def test_value_longer_than_the_limit_is_kept_with_a_warning(self):
        flight = self.store.save(manual())
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(departure="E" * 80))
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        self.assertEqual(logbook.editor.text_fields["departure"].text(), "E" * 80)
        self.assertTrue(logbook.editor.notice.isVisibleTo(logbook.editor))
        self.assertIn("longer than", logbook.editor.notice.text())
        self.assertEqual(logbook.editor.values(), logbook.editing.fields)

    def test_out_of_range_number_is_shown_exactly_not_clamped(self):
        flight = self.store.save(manual())

        def edit(payload):
            payload["revisions"][0]["fields"].update(flight_date="1850-07-04", minutes=12000)
        self.hand_edit(flight, edit)
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        self.assertEqual(logbook.editor.date.date(), QDate(1850, 7, 4))  # range reaches year 100
        self.assertEqual(logbook.editor.minutes.value(), 12000)
        self.assertEqual(logbook.editor.values().minutes, 12000)
        self.assertIn("outside the range", logbook.editor.notice.text())
        logbook.editor.notes.setPlainText("Checked")
        logbook.save_flight()
        saved = self.store.get(flight.id).fields
        self.assertEqual((saved.flight_date, saved.minutes, saved.notes), ("1850-07-04", 12000, "Checked"))

    def test_changing_an_out_of_range_number_to_9999_saves_9999(self):
        flight = self.store.save(manual())
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(minutes=12000))
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        logbook.editor.minutes.setValue(9999)
        self.assertEqual(logbook.editor.values().minutes, 9999)
        logbook.save_flight()
        self.assertEqual(self.store.get(flight.id).fields.minutes, 9999)

    def test_invalid_stored_date_is_kept_unless_a_new_date_is_picked(self):
        flight = self.store.save(manual())
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(flight_date="2024-02-30"))
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        self.assertIn("cannot be shown", logbook.editor.notice.text())
        self.assertEqual(logbook.editor.date.date(), STORED_DATE)
        self.assertIn("Stored: 2024-02-30", logbook.editor.date.text())
        self.assertEqual(logbook.editor.values().flight_date, "2024-02-30")
        logbook.editor.date.setDate(QDate(2024, 2, 28))
        self.assertEqual(logbook.editor.values().flight_date, "2024-02-28")

    def test_picking_today_replaces_an_unshowable_stored_date(self):
        flight = self.store.save(manual())
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(flight_date="2024-02-30"))
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        today = QDate.currentDate()
        logbook.editor.date.setDate(today)
        self.assertEqual(logbook.editor.values().flight_date, today.toString("yyyy-MM-dd"))
        logbook.save_flight()
        self.assertEqual(self.store.get(flight.id).fields.flight_date, today.toString("yyyy-MM-dd"))

    def test_negative_stored_number_can_be_made_unknown(self):
        flight = self.store.save(manual())
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(crew=-5))
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        self.assertEqual(logbook.editor.values().crew, -5)  # untouched: kept as stored
        spin = logbook.editor.number_fields["crew"]
        spin.setValue(2)
        spin.setValue(-1)
        self.assertIsNone(logbook.editor.values().crew)

    def test_hand_edited_lowercase_values_open_clean_and_save_nothing(self):
        flight = self.store.save(manual())
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(departure="egll "))
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        self.assertEqual(logbook.editor.values(), logbook.editor.original)
        self.assertTrue(logbook.can_leave_editor())
        logbook.save_flight()
        self.assertEqual(len(self.store.get(flight.id).revisions), 1)

    def test_show_value_reports_values_it_cannot_show(self):
        logbook = self.make()
        logbook.new_flight()
        editor = logbook.editor
        self.assertFalse(editor.show_value("flight_date", "2024-02-30"))
        self.assertTrue(editor.show_value("flight_date", "2024-02-28"))
        self.assertFalse(editor.show_value("crew", -3))
        self.assertTrue(editor.show_value("minutes", 12000))
        self.assertEqual(editor.minutes.value(), 12000)

    def test_payload_unknown_and_zero_round_trip(self):
        logbook = self.make()
        logbook.new_flight()
        editor = logbook.editor
        editor.text_fields["aircraft"].setText("c172")
        editor.text_fields["departure"].setText("egka")
        editor.text_fields["arrival"].setText("egka")
        editor.number_fields["crew"].setValue(1)
        editor.number_fields["passengers"].setValue(0)
        self.assertTrue(logbook.save_flight())
        fields = logbook.selected.fields
        self.assertEqual((fields.crew, fields.passengers, fields.baggage_kg), (1, 0, None))

    def test_reason_is_saved_with_the_edit(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(flight)
        logbook.editor.number_fields["minutes"].setValue(55)
        logbook.editor.reason.setText("Checked against the airline log")
        logbook.save_flight()
        self.assertEqual(self.store.get(flight.id).revisions[-1].reason, "Checked against the airline log")


class ConflictTests(LogbookCase):
    def test_conflict_reloads_rebases_pilot_edits_and_saves_both(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        logbook.editor.number_fields["minutes"].setValue(70)
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(callsign="EZY1"))
        self.answers.append(True)
        logbook.save_flight()
        self.assertEqual(len(self.questions), 1)
        self.assertIn("callsign", self.questions[0][1])
        saved = self.store.get(flight.id).fields
        self.assertEqual((saved.callsign, saved.minutes), ("EZY1", 70))
        self.assertEqual(logbook.selected.fields.minutes, 70)
        self.assertEqual(logbook.flights[0].fields.callsign, "EZY1")

    def test_keep_editing_after_conflict_then_save_works(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        logbook.editor.text_fields["registration"].setText("G-ABCD")
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(notes="other window"))
        self.answers.append(False)
        logbook.save_flight()
        self.assertEqual(logbook.editor.notes.toPlainText(), "other window")
        self.assertEqual(logbook.editor.text_fields["registration"].text(), "G-ABCD")
        logbook.save_flight()
        saved = self.store.get(flight.id).fields
        self.assertEqual((saved.notes, saved.registration), ("other window", "G-ABCD"))

    def test_record_turning_read_only_never_creates_a_duplicate(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        logbook.editor.number_fields["minutes"].setValue(42)
        self.hand_edit(flight, lambda p: p.update(schema=3))
        self.assertFalse(logbook.save_flight())
        self.assertFalse(logbook.save_flight())  # Save is off; a second try does nothing
        flights, _ = self.store.listing()
        self.assertEqual(len(flights), 1)
        self.assertEqual(logbook.editing.id, flight.id)
        self.assertFalse(logbook.editor.save_button.isEnabled())
        self.assertIn("newer BBC EFB", logbook.editor.notice.text())
        self.assertEqual(logbook.editor.minutes.value(), 42)  # the pilot's edit is still shown
        self.assertEqual(logbook.history.model.rowCount(), 1)

    def test_unreadable_record_during_save_holds_the_edits(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        logbook.editor.number_fields["minutes"].setValue(42)
        self.path_of(flight).write_text("{ not json", encoding="utf-8")
        self.assertFalse(logbook.save_flight())
        self.assertFalse(logbook.editor.save_button.isEnabled())
        self.assertIn("can no longer be read", logbook.editor.notice.text())
        self.assertEqual(len(list((self.root / "flights").glob("*.json"))), 1)

    def test_renamed_file_is_not_saved_as_a_new_flight(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        logbook.editor.number_fields["minutes"].setValue(42)
        copy = self.root / "flights" / f"{flight.id} (conflict copy).json"
        self.path_of(flight).rename(copy)
        self.assertFalse(logbook.save_flight())
        self.assertFalse(logbook.editor.save_button.isEnabled())
        self.assertIn("renamed", self.warnings[-1][1])
        self.assertEqual(sorted(p.name for p in (self.root / "flights").glob("*.json")), [copy.name])

    def test_conflict_lists_edits_that_could_not_be_applied(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        logbook.editor.text_fields["registration"].setText("G-ABCD")
        self.hand_edit(flight, lambda p: p["revisions"][0]["fields"].update(notes="other window"))
        stale = logbook.editor.original
        logbook.editor.original = FlightFields(**{**stale.as_json(), "flight_date": "1999-01-01"})
        logbook.editor._loaded["flight_date"] = (logbook.editor.date.date(), "2024-02-30", True)
        self.answers.append(False)
        logbook.save_flight()
        self.assertIn("NOT applied", self.questions[-1][1])
        self.assertIn("flight date", self.questions[-1][1])
        self.assertIn("NOT applied", logbook.editor.notice.text())

    def test_flight_removed_on_disk_keeps_edits_and_saves_as_new(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(logbook.flights[0])
        logbook.editor.number_fields["minutes"].setValue(15)
        self.path_of(flight).unlink()
        logbook.save_flight()
        self.assertIsNone(logbook.editing)
        self.assertIn("new flight", self.warnings[-1][1])
        logbook.save_flight()
        self.assertEqual([f.fields.minutes for f in self.store.listing()[0]], [15])


class WorkspaceTests(LogbookCase):
    def setUp(self):
        super().setUp()
        self.x = self.store.save(manual(flight_date="2024-01-03", aircraft="B738", departure="EGKK"))
        self.y = self.store.save(manual(flight_date="2024-01-02", aircraft="A320", departure="EGLL"))

    def test_pages_show_storage_records(self):
        logbook = self.make()
        self.assertEqual(logbook.workspace.current_page, "dashboard")
        self.assertEqual(logbook.history.model.rowCount(), 2)
        self.assertEqual(logbook.dashboard.metrics["flights"].value.text(), "2")
        for name in ("Flight History", "black_box", "Statistics", "Dashboard"):
            self.assertTrue(logbook.navigate(name))
        self.assertEqual(logbook.workspace.current_page, "dashboard")

    def test_open_details_reads_fresh_and_opens_the_overlay(self):
        logbook = self.make()
        stale = logbook.flights[0]
        self.hand_edit(stale, lambda p: p["revisions"][0]["fields"].update(notes="fresh"))
        self.assertTrue(logbook.open_details(stale, "notes"))
        self.assertEqual(logbook.selected.fields.notes, "fresh")
        self.assertTrue(logbook.details.is_open)
        self.assertEqual(logbook.details.view.notes, "fresh")
        self.assertEqual(logbook.details.current_tab, "notes")
        self.assertEqual(logbook.details.tab_keys(), ["overview", "operations", "notes", "history"])

    def test_edit_from_overlay_opens_the_editor_and_save_returns_to_details(self):
        logbook = self.make()
        logbook.open_details(self.x)
        logbook.details.edit_button.click()
        self.assertIs(logbook.stack.currentWidget(), logbook.pages["Editor"])
        self.assertEqual(logbook.editing.id, self.x.id)
        logbook.editor.number_fields["minutes"].setValue(33)
        self.assertTrue(logbook.save_flight())
        self.assertIs(logbook.stack.currentWidget(), logbook.workspace)
        self.assertTrue(logbook.details.is_open)
        self.assertEqual(logbook.details.view.minutes, 33)

    def test_leaving_a_dirty_editor_asks(self):
        logbook = self.make()
        logbook.start_editor(self.x)
        logbook.editor.text_fields["callsign"].setText("ABC")
        self.assertFalse(logbook.navigate("history"))
        self.assertIs(logbook.stack.currentWidget(), logbook.pages["Editor"])
        self.answers.append(True)
        self.assertTrue(logbook.navigate("history"))
        self.assertEqual(logbook.workspace.current_page, "history")

    def test_unchanged_listing_is_not_converted_again(self):
        logbook = self.make()
        self.assertFalse(logbook.reload())
        self.store.save(manual(aircraft="C172"))
        self.assertTrue(logbook.reload())
        self.assertEqual(logbook.history.model.rowCount(), 3)

    def test_unreadable_files_are_named_and_queued_in_black_box(self):
        (self.root / "flights" / "copy of flight.json").write_text("{}", encoding="utf-8")
        logbook = self.make()
        self.assertIn("copy of flight.json", logbook.warning.text())
        self.assertIn("not a flight id", logbook.warning.text())
        logbook.navigate("black_box")
        kinds = [item.kind for item in logbook.black_box.items]
        self.assertIn("unreadable", kinds)

    def test_read_only_store_disables_editing_and_gap_acceptance(self):
        reader = FlightStore(self.root, read_only=True)
        logbook = self.make(store=reader)
        self.assertFalse(logbook.history.new_button.isEnabled())
        self.assertIsNone(logbook.black_box.actions)
        self.assertIn("read-only", logbook.warning.text())
        self.assertFalse(logbook.start_editor(self.x))


class RecorderTests(LogbookCase):
    def recorded(self, gaps=(), simulated=False):
        evidence = RecorderEvidence("recorder/journals/a.jsonl", "Tape", "simulator", simulated, gaps=tuple(gaps))
        return self.store.create_recorded(FlightFields("2024-01-05", "A20N", "EGLL", "LFPG"), evidence)

    def test_gap_acknowledgement_goes_through_the_store(self):
        flight = self.recorded(gaps=(Gap("Simulator disconnected"), Gap("Paused")))
        logbook = self.make()
        logbook.navigate("black_box")
        page = logbook.black_box
        rows = [i for i, item in enumerate(page.items) if item.kind == "gaps"]
        self.assertEqual(len(rows), 1)
        page.select_row(rows[0])
        page.ask_note = lambda item: "Checked"
        self.assertTrue(page.accept_selected())
        saved = self.store.get(flight.id)
        self.assertEqual([(a.gap, a.note) for a in saved.acknowledgements], [(0, "Checked"), (1, "Checked")])
        self.assertEqual([item.kind for item in page.items if item.flight_id == flight.id], [])
        self.assertEqual(saved.evidence.gaps, flight.evidence.gaps)

    def test_correction_origin_names_who_corrected_it(self):
        flight = self.recorded()
        self.store.correct(flight, {"arrival": "LFPO"}, reason="Recorder re-check", by="recorder")
        logbook = self.make()
        logbook.open_details(flight)
        self.assertIn("corrected by Recorder", texts(logbook.details.panel))
        self.assertNotIn("corrected by pilot", texts(logbook.details.panel))

    def test_simulated_recorder_flight_is_labelled(self):
        flight = self.recorded(simulated=True)
        logbook = self.make()
        logbook.open_details(flight, "black_box")
        self.assertIn("Simulated data", texts(logbook.details.panel))
        self.assertIn("black_box", logbook.details.tab_keys())

    def test_running_recorder_is_never_called_unavailable(self):
        logbook = self.make()
        started = datetime(2024, 1, 5, 10, 0, tzinfo=timezone.utc)
        for state in ("inactive", "armed", "recording", "paused", "gap", "saving", "error"):
            status = SimpleNamespace(state=state, phase="airborne", detail="", flight_id="f-1",
                                     started_utc=started, aircraft="A20N", departure="EGLL",
                                     block_time_s=3600.0, airborne_time_s=1800.0, simulated=False)
            live = logbook.set_recorder_status(status)
            shown = texts(logbook.black_box).lower()
            self.assertNotIn("not available", shown, state)
            self.assertNotIn("unavailable", shown, state)
            self.assertNotIn("not available in this build", shown)
            self.assertEqual(live.state, state)
        self.assertIn("A20N", texts(logbook.black_box))
        self.assertIn("1h 00m", texts(logbook.black_box))

    def test_unavailable_status_shows_its_reason(self):
        logbook = self.make()
        logbook.set_recorder_status(SimpleNamespace(state="unavailable", detail="No data folder is open."))
        shown = texts(logbook.black_box)
        self.assertIn("not available", shown)
        self.assertIn("No data folder is open.", shown)

    def test_simulated_status_is_labelled(self):
        logbook = self.make()
        logbook.set_recorder_status(SimpleNamespace(state="recording", detail="", simulated=True))
        self.assertIn("SIMULATED", texts(logbook.black_box))

    def test_recorder_service_status_shape_is_read(self):
        flight = SimpleNamespace(flight_id="abc", phase_label="Taxi out", aircraft_title="Cessna 172",
                                 atc_id="G-ABCD", started_at=datetime(2024, 1, 5, 9, 0, tzinfo=timezone.utc),
                                 sim_started_utc=None, touchdowns=0, gaps=1, journal_ok=True)
        status = SimpleNamespace(state=SimpleNamespace(value="recording"), label="Logging active",
                                 detail="Capturing", reason=None, phase=None, flight=flight)
        live = describe_recorder_status(status)
        facts = dict(live.facts)
        self.assertEqual(facts["Aircraft"], "Cessna 172")
        self.assertEqual(facts["Phase"], "Taxi out")
        self.assertIn("computer clock", facts["Started"])
        self.assertTrue(live.flight_open)

    def test_flight_saved_by_the_recorder_appears_when_logging_ends(self):
        logbook = self.make()
        logbook.set_recorder_status(SimpleNamespace(state="recording", detail=""))
        self.recorded()
        self.assertEqual(logbook.history.model.rowCount(), 0)
        logbook.set_recorder_status(SimpleNamespace(state="armed", detail=""))
        self.assertEqual(logbook.history.model.rowCount(), 1)


class StoreSwitchTests(LogbookCase):
    def other_store(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = FlightStore(Path(folder.name))
        self.addCleanup(store.close)
        return store

    def test_logbook_never_opens_its_own_store(self):
        logbook = self.make(store=None)
        self.assertFalse(hasattr(logbook, "_owned_store"))
        requested = []
        logbook.data_folder_requested.connect(lambda: requested.append(True))
        self.assertTrue(logbook.choose_data_folder())
        self.assertEqual(requested, [True])
        self.assertIsNone(logbook.store)
        self.assertTrue(logbook.folder_card.isVisible())
        logbook.set_store(self.store)
        self.assertIs(logbook.store, self.store)
        self.assertFalse(logbook.folder_card.isVisible())
        logbook.set_store(None)
        self.assertTrue(self.store.writable)  # the Logbook does not close the app's store

    def test_choose_without_a_shell_says_where_to_go(self):
        logbook = self.make(store=None)
        self.assertFalse(logbook.choose_data_folder())
        self.assertIn("Settings", self.warnings[-1][0])

    def test_clean_switch_resets_selection_editor_and_details(self):
        self.store.save(manual())
        logbook = self.make()
        logbook.open_details(logbook.flights[0])
        other = self.other_store()
        logbook.set_store(other)
        self.assertEqual(self.questions, [])
        self.assertIsNone(logbook.selected)
        self.assertFalse(logbook.details.is_open)
        self.assertEqual(logbook.flights, [])
        logbook.set_store(None)
        self.assertFalse(logbook.history.new_button.isEnabled())

    def test_dirty_editor_asks_and_discard_closes_it(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(flight)
        logbook.editor.text_fields["callsign"].setText("ABC")
        self.answers.append(False)  # Discard
        logbook.set_store(self.other_store())
        self.assertEqual(len(self.questions), 1)
        self.assertIs(logbook.stack.currentWidget(), logbook.workspace)
        self.assertIsNone(logbook.editing)

    def test_kept_edits_of_another_folders_flight_cannot_be_saved(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(flight)
        logbook.editor.text_fields["callsign"].setText("ABC")
        other = self.other_store()
        self.answers.append(True)  # Keep my changes
        logbook.set_store(other)
        self.assertIs(logbook.stack.currentWidget(), logbook.pages["Editor"])
        self.assertEqual(logbook.editor.text_fields["callsign"].text(), "ABC")
        self.assertFalse(logbook.editor.save_button.isEnabled())
        self.assertFalse(logbook.save_flight())
        self.assertEqual(other.listing()[0], [])

    def test_kept_edits_continue_when_the_same_folder_is_reopened(self):
        flight = self.store.save(manual())
        logbook = self.make()
        logbook.start_editor(flight)
        logbook.editor.text_fields["callsign"].setText("ABC")
        self.store.close()
        self.store = FlightStore(self.root)
        self.answers.append(True)
        logbook.set_store(self.store)
        self.assertTrue(logbook.editor.save_button.isEnabled())
        self.assertTrue(logbook.save_flight())
        self.assertEqual(self.store.get(flight.id).fields.callsign, "ABC")
        self.assertEqual(len(self.store.listing()[0]), 1)

    def test_kept_new_flight_is_saved_in_the_new_folder(self):
        logbook = self.make()
        logbook.new_flight()
        for name, value in (("aircraft", "C172"), ("departure", "EGKA"), ("arrival", "EGKA")):
            logbook.editor.text_fields[name].setText(value)
        other = self.other_store()
        self.answers.append(True)
        logbook.set_store(other)
        self.assertTrue(logbook.save_flight())
        self.assertEqual(len(other.listing()[0]), 1)
        self.assertEqual(self.store.listing()[0], [])

    def test_can_switch_store_discards_once(self):
        logbook = self.make()
        logbook.new_flight()
        logbook.editor.text_fields["aircraft"].setText("C172")
        self.answers.append(True)
        self.assertTrue(logbook.can_switch_store())
        logbook.set_store(self.other_store())
        self.assertEqual(len(self.questions), 1)


class PreferenceTests(LogbookCase):
    def test_view_mode_is_loaded_saved_and_followed(self):
        prefs = FakePreferences({"logbook.history_view": "cards"})
        logbook = self.make(preferences=prefs)
        self.assertEqual(logbook.history.view_mode, "cards")
        logbook.history.set_view_mode("table")
        self.assertEqual(prefs.values["logbook.history_view"], "table")
        self.assertGreaterEqual(prefs.saved, 1)
        prefs.set("logbook.history_view", "combined")  # changed in Settings
        self.assertEqual(logbook.history.view_mode, "combined")

    def test_unknown_hidden_columns_key_is_session_only(self):
        prefs = FakePreferences()
        logbook = self.make(preferences=prefs)
        logbook.history.set_hidden_columns({"registration"})
        self.assertEqual(logbook._session_columns, ["registration"])

    def test_hidden_columns_are_kept_in_real_preferences(self):
        from bbc_efb.preferences import HISTORY_COLUMNS, HISTORY_DEFAULT_HIDDEN, Preferences
        from bbc_efb.logbook_views.history import COLUMN_KEYS, DEFAULT_HIDDEN
        self.assertEqual(HISTORY_COLUMNS, COLUMN_KEYS)
        self.assertEqual(frozenset(HISTORY_DEFAULT_HIDDEN), DEFAULT_HIDDEN)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            prefs = Preferences.load(path)
            logbook = self.make(preferences=prefs)
            self.assertEqual(logbook.history.hidden_columns, DEFAULT_HIDDEN)
            logbook.history.set_hidden_columns({"registration", "source"})
            self.assertEqual(prefs.get("logbook.hidden_columns"), ["registration", "source"])
            logbook.deleteLater()
            again = self.make(preferences=Preferences.load(path))
            self.assertEqual(again.history.hidden_columns, frozenset({"registration", "source"}))
            again.deleteLater()


class PerformanceTests(unittest.TestCase):
    def test_two_thousand_flights_load_and_reload_quickly(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with FlightStore(root) as store:
                for record in many(2000):
                    (root / "flights" / f"{record['id']}.json").write_text(json.dumps(record), encoding="utf-8")
                started = time.perf_counter()
                logbook = Logbook(store)
                first = time.perf_counter() - started
                self.assertEqual(logbook.history.model.rowCount(), 2000)
                started = time.perf_counter()
                for page in ("history", "statistics", "black_box", "dashboard"):
                    logbook.navigate(page)
                switching = time.perf_counter() - started
                started = time.perf_counter()
                self.assertFalse(logbook.reload())
                unchanged = time.perf_counter() - started
                logbook.deleteLater()
        self.assertLess(first, 4.0)
        self.assertLess(switching, 1.5)
        self.assertLess(unchanged, 0.25)


if __name__ == "__main__":
    unittest.main()
