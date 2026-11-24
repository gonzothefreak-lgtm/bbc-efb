"""Fields the Recorder could not be sure of: confirming them as correct (A68).

Covers the storage API (``FlightStore.confirm``), the view model (field checks, suspected
diversions, the History marker and filter) and the Flight Details / Logbook actions.
Run with QT_QPA_PLATFORM=offscreen.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTableWidget, QWidget  # noqa: E402

from bbc_efb.logbook import Logbook  # noqa: E402
from bbc_efb.logbook_views import filters as f  # noqa: E402
from bbc_efb.logbook_views import model as m  # noqa: E402
from bbc_efb.logbook_views.details import FlightDetailsOverlay  # noqa: E402
from bbc_efb.logbook_views.history import COLUMNS, FlightTableModel  # noqa: E402
from bbc_efb.logbook_views.recorder_management import StaleListing, StoreActions  # noqa: E402
from bbc_efb.storage import (  # noqa: E402
    FlightFields, FlightStore, RecordConflict, RecordReadOnly, RecorderEvidence, parse_record,
)

try:
    from tests.test_logviews_support import evidence, process, recorded
except ImportError:  # discovered without the package prefix
    from test_logviews_support import evidence, process, recorded  # type: ignore[no-redef]

APP = QApplication.instance() or QApplication([])

STATUS = {
    "aircraft": {"status": "needs_confirmation", "reason": "Only the aircraft title was available",
                 "confidence": "low"},
    "departure": {"status": "known", "confidence": "high"},
}
SUSPECTED = {
    "verdict": "diversion_suspected", "planned_destination": "LFPG",
    "reason": "Possible diversion: the aircraft arrived at LFPO, not the planned destination LFPG.",
    "needs_pilot_confirmation": True,
}


def texts(widget):
    return "\n".join(item.text() for item in widget.findChildren(QLabel))


def button(widget, text):
    found = [b for b in widget.findChildren(QPushButton) if b.text() == text]
    if not found:
        raise AssertionError(f"No button {text!r}")
    return found[0]


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

    def recorded_flight(self, extra=None, arrival="LFPO"):
        ev = RecorderEvidence("recorder/journals/a.jsonl", "Tape", "simulator", False,
                              extra=extra if extra is not None else {"field_status": STATUS,
                                                                     "needs_confirmation": ["aircraft"]})
        return self.store.create_recorded(FlightFields("2026-11-20", "Kodiak 100 Wheels Asobo", "EGLL", arrival), ev)

    def path(self, flight):
        return self.root / "flights" / f"{flight.id}.json"

    def make(self):
        self.logbook = Logbook(self.store, journal_in_background=False)
        self.logbook._warn = lambda title, text: self.warnings.append((title, text))
        self.logbook._ask = lambda *args, **kw: False
        self.logbook.resize(1280, 860)
        self.logbook.show()
        return self.logbook


class StorageConfirmTests(StoreCase):
    def test_confirm_writes_a_revision_without_changing_values(self):
        flight = self.recorded_flight()
        before = json.loads(self.path(flight).read_text(encoding="utf-8"))
        confirmed = self.store.confirm(flight, ["aircraft"])
        self.assertEqual(len(confirmed.revisions), 2)
        latest = confirmed.revisions[-1]
        self.assertEqual((latest.by, latest.confirmed), ("pilot", ("aircraft",)))
        self.assertEqual(latest.fields, flight.fields)
        self.assertIn("Aircraft type", latest.reason)
        self.assertEqual(confirmed.changed_fields(1), ())
        prov = confirmed.provenance("aircraft")
        self.assertTrue(prov.confirmed)
        self.assertFalse(prov.corrected)
        self.assertEqual([c.value for c in prov.confirmations], ["Kodiak 100 Wheels Asobo"])
        self.assertEqual(confirmed.confirmed_fields(), ("aircraft",))
        after = json.loads(self.path(flight).read_text(encoding="utf-8"))
        self.assertEqual(after["revisions"][0], before["revisions"][0])  # history untouched
        self.assertEqual(after["revisions"][1]["confirmed"], ["aircraft"])
        self.assertEqual(after["recorder"], before["recorder"])            # evidence untouched

    def test_confirming_again_writes_nothing(self):
        flight = self.store.confirm(self.recorded_flight(), ("aircraft",))
        digest = hashlib.sha256(self.path(flight).read_bytes()).hexdigest()
        again = self.store.confirm(flight, "aircraft")
        self.assertEqual(len(again.revisions), 2)
        self.assertEqual(hashlib.sha256(self.path(flight).read_bytes()).hexdigest(), digest)

    def test_a_later_change_clears_the_confirmation(self):
        flight = self.store.confirm(self.recorded_flight(), ["aircraft"])
        changed = self.store.correct(flight, {"aircraft": "K100"}, reason="Type code")
        self.assertFalse(changed.provenance("aircraft").confirmed)
        self.assertEqual(changed.confirmed_fields(), ())
        reconfirmed = self.store.confirm(changed, ["aircraft"])
        self.assertTrue(reconfirmed.provenance("aircraft").confirmed)
        self.assertEqual(len(reconfirmed.provenance("aircraft").confirmations), 2)

    def test_stale_flight_is_refused(self):
        flight = self.recorded_flight()
        self.store.correct(flight, {"callsign": "BAW1"}, reason="x")
        with self.assertRaises(RecordConflict):
            self.store.confirm(flight, ["aircraft"])

    def test_bad_requests_are_refused(self):
        flight = self.recorded_flight()
        for names in ((), ["wingspan"]):
            with self.assertRaises(ValueError):
                self.store.confirm(flight, names)
        with self.assertRaises(ValueError):
            self.store.confirm(flight, ["arrival"], diversion={"planned_arrival": "LFPG"})
        with self.assertRaises(ValueError):
            self.store.confirm(flight, ["arrival"], diversion={"confirmed": True, "when": 5})
        self.assertEqual(len(self.store.get(flight.id).revisions), 1)

    def test_newer_format_is_read_only(self):
        flight = self.recorded_flight()
        payload = json.loads(self.path(flight).read_text(encoding="utf-8"))
        payload["schema"] = 3
        self.path(flight).write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(RecordReadOnly):
            self.store.confirm(self.store.get(flight.id), ["aircraft"])

    def test_diversion_answer_is_kept_and_carried(self):
        flight = self.recorded_flight()
        answered = self.store.confirm(flight, ["arrival"], diversion={"confirmed": True, "planned_arrival": "LFPG"})
        self.assertEqual(dict(answered.revisions[-1].extra)["diversion"], {"confirmed": True, "planned_arrival": "LFPG"})
        edited = self.store.correct(answered, {"notes": "Weather"}, reason="Notes")
        self.assertEqual(dict(edited.revisions[-1].extra)["diversion"]["confirmed"], True)
        # An answer is written even when the field was already confirmed.
        self.assertEqual(len(answered.revisions), 2)

    def test_only_the_pilot_confirms(self):
        flight = self.recorded_flight()
        payload = json.loads(self.path(flight).read_text(encoding="utf-8"))
        payload["revisions"][0]["confirmed"] = ["aircraft"]
        parsed = parse_record(payload)
        self.assertFalse(parsed.provenance("aircraft").confirmed)  # a Recorder revision cannot confirm
        self.assertFalse(m.flight_view(payload).provenance["aircraft"].confirmed)

    def test_malformed_confirmed_list_is_kept_and_ignored(self):
        flight = self.recorded_flight()
        payload = json.loads(self.path(flight).read_text(encoding="utf-8"))
        payload["revisions"].append(dict(payload["revisions"][0], by="pilot", confirmed="aircraft",
                                         saved_at="2026-11-21T10:00:00.000+00:00"))
        self.path(flight).write_text(json.dumps(payload), encoding="utf-8")
        loaded = self.store.get(flight.id)
        self.assertFalse(loaded.provenance("aircraft").confirmed)
        self.assertTrue(any("confirmed fields" in problem for problem in loaded.problems))
        saved = self.store.confirm(loaded, ["aircraft"])
        written = json.loads(self.path(flight).read_text(encoding="utf-8"))
        self.assertEqual(written["revisions"][1]["confirmed"], "aircraft")  # kept exactly as stored
        self.assertTrue(saved.provenance("aircraft").confirmed)


class ModelTests(unittest.TestCase):
    def confirmed_record(self, ev, names, by="pilot", diversion=None):
        record = recorded(ev=ev)
        fields = dict(record["revisions"][0]["fields"])
        if diversion is not None:
            fields["diversion"] = diversion
        record["revisions"].append({"saved_at": "2026-11-21T10:00:00.000+00:00", "by": by, "reason": "",
                                    "confirmed": list(names), "fields": fields})
        return record

    def test_confirmation_clears_the_check(self):
        ev = evidence(field_status=STATUS)
        self.assertEqual([c.name for c in m.flight_view(recorded(ev=ev)).field_checks], ["aircraft"])
        view = m.flight_view(self.confirmed_record(ev, ["aircraft"]))
        self.assertEqual(view.field_checks, ())
        self.assertFalse(view.needs_confirmation)
        self.assertEqual(view.confirmed_fields, ("aircraft",))
        self.assertEqual(view.revisions[1].confirmed, ("aircraft",))
        self.assertEqual(m.flight_view(self.confirmed_record(ev, ["aircraft"], by="recorder")).confirmed_fields, ())

    def test_needs_confirmation_list_alone_is_shown(self):
        view = m.flight_view(recorded(ev=evidence(needs_confirmation=["registration"])))
        self.assertEqual([(c.name, c.status) for c in view.field_checks], [("registration", "needs_confirmation")])

    def test_suspected_diversion_needs_an_answer(self):
        ev = evidence(arrival_assessment=SUSPECTED)
        view = m.flight_view(recorded(ev=ev, arrival="LFPO"))
        self.assertEqual([c.name for c in view.field_checks], ["arrival"])
        self.assertIn("Possible diversion", view.field_checks[0].text)
        self.assertEqual(view.planned_destination, "LFPG")
        self.assertFalse(view.diverted)

        answered = m.flight_view(self.confirmed_record(ev, ["arrival"], diversion={"confirmed": True,
                                                                                    "planned_arrival": "LFPG"}))
        self.assertEqual(answered.field_checks, ())
        self.assertTrue(answered.diverted)
        self.assertEqual(answered.planned_destination, "")
        rejected = m.flight_view(self.confirmed_record(ev, ["arrival"], diversion={"confirmed": False}))
        self.assertEqual((rejected.field_checks, rejected.diverted), ((), False))

    def test_history_marker_and_filter(self):
        needs = m.flight_view(recorded(ev=evidence(field_status=STATUS)))
        confirmed = m.flight_view(self.confirmed_record(evidence(field_status=STATUS), ["aircraft"]))
        plain = m.flight_view(recorded())
        self.assertEqual(needs.status, "recorded")  # the record status itself is unchanged
        self.assertEqual(needs.review_label, "Needs confirmation · Recorded")
        self.assertEqual(plain.review_label, "Recorded")
        views = [needs, confirmed, plain]
        self.assertEqual(f.apply(views, f.FilterState(confirmation="needs")), [needs])
        self.assertEqual(f.apply(views, f.FilterState(confirmation="confirmed")), [confirmed])
        self.assertEqual(len(f.apply(views, f.FilterState())), 3)
        self.assertIn("needs confirmation", f.FilterState(confirmation="needs").describe())

        table = FlightTableModel()
        table.set_flights(views)
        status = next(i for i, column in enumerate(COLUMNS) if column.key == "status")
        self.assertEqual(table.data(table.index(0, status)), "Needs confirmation · Recorded")
        self.assertIn("Aircraft type", table.data(table.index(0, status), Qt.ItemDataRole.ToolTipRole))
        self.assertIsNotNone(table.data(table.index(0, status), Qt.ItemDataRole.ForegroundRole))

    def test_filter_bar_round_trips_confirmation(self):
        bar = f.FilterBar()
        seen = []
        bar.changed.connect(seen.append)
        bar.confirmation.setCurrentIndex(bar.confirmation.findData("needs"))
        self.assertEqual(seen[-1].confirmation, "needs")
        bar.reset()
        self.assertEqual(bar.state().confirmation, "any")
        bar.set_state(f.FilterState(confirmation="confirmed"))
        self.assertEqual(bar.confirmation.currentData(), "confirmed")


class DetailsTests(StoreCase):
    def overlay(self, view, can_edit=True):
        host = QWidget()
        host.resize(1100, 800)
        host.show()
        self.addCleanup(host.deleteLater)
        overlay = FlightDetailsOverlay(host, can_edit=can_edit)
        overlay.open_flight(view)
        return overlay

    def test_confirm_button_emits_a_request(self):
        view = m.flight_view(recorded(ev=evidence(field_status=STATUS)))
        overlay = self.overlay(view)
        requests, edits = [], []
        overlay.confirm_requested.connect(lambda *args: requests.append(args))
        overlay.edit_requested.connect(edits.append)
        button(overlay.tabs.widget(0), "Confirm as correct").click()
        self.assertEqual(requests, [(view.id, ("aircraft",), None)])
        button(overlay.tabs.widget(0), "Correct…").click()
        self.assertEqual(edits, [view.id])

    def test_buttons_follow_edit_permission(self):
        view = m.flight_view(recorded(ev=evidence(field_status=STATUS)))
        overlay = self.overlay(view, can_edit=False)
        confirm = button(overlay.tabs.widget(0), "Confirm as correct")
        self.assertFalse(confirm.isEnabled())
        overlay.set_can_edit(True)
        self.assertTrue(confirm.isEnabled())

    def test_suspected_diversion_offers_both_answers(self):
        view = m.flight_view(recorded(ev=evidence(arrival_assessment=SUSPECTED), arrival="LFPO"))
        overlay = self.overlay(view)
        requests = []
        overlay.confirm_requested.connect(lambda *args: requests.append(args))
        button(overlay.tabs.widget(0), "Confirm diversion").click()
        button(overlay.tabs.widget(0), "Not a diversion").click()
        self.assertEqual([(r[1], r[2]["confirmed"], r[2]["planned_arrival"]) for r in requests],
                         [(("arrival",), True, "LFPG"), (("arrival",), False, "LFPG")])

    def test_history_tab_shows_the_confirmation(self):
        flight = self.store.confirm(self.recorded_flight(), ["aircraft"])
        overlay = self.overlay(m.flight_view(flight))
        overlay.select_tab("history")
        tables = {t.accessibleName() or t.objectName(): t for t in overlay.tabs.currentWidget().findChildren(QTableWidget)}
        cells = "\n".join(t.item(r, c).text() for t in tables.values()
                          for r in range(t.rowCount()) for c in range(t.columnCount()) if t.item(r, c))
        self.assertIn("Confirmed as correct: Aircraft type", cells)
        self.assertIn("confirmed by pilot", texts(overlay.tabs.widget(0)))


class LogbookTests(StoreCase):
    def test_confirm_from_details_updates_the_flight(self):
        flight = self.recorded_flight()
        logbook = self.make()
        logbook.open_details(flight.id)
        details = logbook.workspace.details
        self.assertTrue(details.view.needs_confirmation)
        button(details.tabs.widget(0), "Confirm as correct").click()
        self.assertEqual(self.warnings, [])
        stored = self.store.get(flight.id)
        self.assertEqual(stored.confirmed_fields(), ("aircraft",))
        self.assertEqual(stored.fields, flight.fields)
        self.assertFalse(details.view.needs_confirmation)
        self.assertNotIn("Fields need confirmation",
                         [item.title for item in logbook.workspace.recorder.items])

    def test_flight_changed_since_shown_is_refused(self):
        flight = self.recorded_flight()
        logbook = self.make()
        logbook.open_details(flight.id)
        seen = logbook.workspace.details.view
        self.store.correct(self.store.get(flight.id), {"callsign": "BAW1"}, reason="Elsewhere")
        with self.assertRaises(StaleListing):
            StoreActions(self.store).confirm_fields(flight.id, ["aircraft"], seen=seen)
        self.assertFalse(logbook.confirm_fields(flight.id, ("aircraft",)))
        self.assertEqual(self.warnings[-1][0], "Flight changed")
        self.assertEqual(self.store.get(flight.id).confirmed_fields(), ())

    def test_read_only_folder_cannot_confirm(self):
        flight = self.recorded_flight()
        self.store.close()
        self.store = FlightStore(self.root, read_only=True)
        logbook = self.make()
        self.assertFalse(logbook.confirm_fields(flight.id, ("aircraft",)))
        self.assertEqual(len(self.store.get(flight.id).revisions), 1)


if __name__ == "__main__":
    unittest.main()
