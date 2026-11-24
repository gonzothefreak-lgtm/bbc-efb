"""Black Box Recorder Management: attention queue rules and evidence-preserving review."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from tests.test_logviews_support import app, evidence, imported, manual, recorded
except ImportError:  # discovered without the package prefix
    from test_logviews_support import app, evidence, imported, manual, recorded  # type: ignore[no-redef]

from bbc_efb.logbook_views.model import flight_view, flight_views
from bbc_efb.logbook_views.recorder_management import (
    GapAcknowledger, RecorderManagementPage, StoreActions, attention_items_for, attention_queue,
)


class FakeActions:
    def __init__(self, fail_at=None, can_delete=False):
        self.calls = []
        self.deleted = []
        self.fail_at = fail_at
        self.can_delete = can_delete

    def acknowledge_gap(self, flight_id, gap_index, note):
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise PermissionError("The Logbook folder is read-only.")
        self.calls.append((flight_id, gap_index, note))

    def delete_recording(self, flight_id):
        self.deleted.append(flight_id)


class QueueTests(unittest.TestCase):
    def test_manual_and_imported_never_flagged_for_missing_telemetry(self):
        for record in (manual(), manual(minutes=None, aircraft="?"), imported(), imported(minutes=None)):
            self.assertEqual(attention_items_for(flight_view(record)), [])

    def test_imported_with_real_problem_is_flagged(self):
        items = attention_items_for(flight_view(imported(flight_date="31/02/2026")))
        self.assertEqual([i.kind for i in items], ["import_review"])
        self.assertIn("not a valid date", items[0].detail)

    def test_recorder_cases(self):
        gaps = evidence(gaps=[{"reason": "Simulator disconnected"}, {"reason": "Stale feed"},
                              {"reason": "Simulator disconnected"}])
        item = attention_items_for(flight_view(recorded(ev=gaps, acks=((1, ""),))))[0]
        self.assertEqual(item.kind, "gaps")
        self.assertEqual(item.gap_indexes, (0, 2))
        self.assertTrue(item.can_accept_gaps)
        self.assertIn("2 gaps not reviewed: Simulator disconnected", item.detail)
        self.assertEqual(attention_items_for(flight_view(recorded(ev=gaps, acks=((0, ""), (1, ""), (2, ""))))), [])
        self.assertEqual(attention_items_for(flight_view(recorded())), [])
        interrupted = attention_items_for(flight_view(recorded(ev=evidence(status="interrupted"))))
        self.assertEqual([i.kind for i in interrupted], ["interrupted"])
        self.assertFalse(interrupted[0].can_accept_gaps)
        missing = recorded()
        del missing["recorder"]
        self.assertEqual([i.kind for i in attention_items_for(flight_view(missing))], ["evidence_missing"])

    def test_queue_order_and_unreadable_files(self):
        views, _ = flight_views([
            recorded(flight_date="2026-01-01", ev=evidence(gaps=[{"reason": "a"}])),
            recorded(flight_date="2026-03-01", ev=evidence(gaps=[{"reason": "b"}])),
            imported(flight_date="bad"),
        ])
        unreadable = [SimpleNamespace(name="x.json", kind="unreadable", reason="Corrupt JSON"),
                      SimpleNamespace(name="y-DESKTOP.json", kind="misnamed", reason="Name is not the id")]
        queue = attention_queue(views, unreadable)
        self.assertEqual([i.kind for i in queue], ["unreadable", "unreadable", "gaps", "gaps", "import_review"])
        self.assertIn("(misnamed)", queue[1].detail)
        self.assertEqual(queue[2].detail, "1 gap not reviewed: b")  # newest flight first within a kind


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def page_with(self, records, actions=None, unreadable=()):
        page = RecorderManagementPage(actions=actions)
        views, _ = flight_views(records)
        page.set_flights(views, unreadable)
        page.show_error = lambda title, text: self.errors.append((title, text))
        self.errors = []
        self.addCleanup(page.deleteLater)
        return page

    def test_empty_queue(self):
        page = self.page_with([manual(), imported(), recorded()])
        self.assertEqual(page.items, [])
        self.assertTrue(page.empty.isVisibleTo(page))
        self.assertIn("0 items need attention", page.counts.text())
        self.assertIn("1 Recorder flight", page.counts.text())

    def test_accept_gaps_flow(self):
        actions = FakeActions()
        record = recorded(ev=evidence(gaps=[{"reason": "a"}, {"reason": "b"}]))
        page = self.page_with([record, manual()], actions)
        changed = []
        page.records_changed.connect(lambda: changed.append(True))
        self.assertFalse(page.accept_button.isEnabled())
        page.select_row(0)
        self.assertTrue(page.accept_button.isEnabled())
        page.ask_note = lambda item: None  # pilot cancels
        self.assertFalse(page.accept_selected())
        self.assertEqual(actions.calls, [])
        page.ask_note = lambda item: "  known outage "
        self.assertTrue(page.accept_selected())
        self.assertEqual(actions.calls, [(record["id"], 0, "known outage"), (record["id"], 1, "known outage")])
        self.assertEqual(changed, [True])

    def test_accept_failure_is_reported(self):
        actions = FakeActions(fail_at=1)
        page = self.page_with([recorded(ev=evidence(gaps=[{"reason": "a"}, {"reason": "b"}]))], actions)
        page.select_row(0)
        page.ask_note = lambda item: ""
        changed = []
        page.records_changed.connect(lambda: changed.append(True))
        self.assertFalse(page.accept_selected())
        self.assertEqual(len(self.errors), 1)
        self.assertIn("1 of 2 gaps were accepted", self.errors[0][1])
        self.assertEqual(changed, [True])  # the partial acknowledgement is reloaded honestly

    def test_without_actions_accept_is_unavailable(self):
        page = self.page_with([recorded(ev=evidence(gaps=[{"reason": "a"}]))], None)
        page.select_row(0)
        self.assertFalse(page.accept_button.isEnabled())
        self.assertIn("read-only", page.accept_button.toolTip())
        self.assertFalse(page.accept_selected())

    def test_delete_only_when_supported_and_confirmed(self):
        record = recorded(ev=evidence(status="interrupted"))
        page = self.page_with([record], FakeActions())
        page.select_row(0)
        self.assertFalse(page.delete_button.isEnabled())
        self.assertFalse(page.delete_selected())
        actions = FakeActions(can_delete=True)
        page = self.page_with([record], actions)
        page.select_row(0)
        self.assertTrue(page.delete_button.isEnabled())
        page.confirm_delete = lambda item: False
        self.assertFalse(page.delete_selected())
        page.confirm_delete = lambda item: True
        self.assertTrue(page.delete_selected())
        self.assertEqual(actions.deleted, [record["id"]])

    def test_review_and_links(self):
        record = recorded(ev=evidence(gaps=[{"reason": "a"}]))
        page = self.page_with([record], None, [SimpleNamespace(name="bad.json", kind="unreadable", reason="Corrupt")])
        reviews, diag = [], []
        page.review_requested.connect(reviews.append)
        page.diagnostics_requested.connect(lambda: diag.append(True))
        page.select_row(0)  # unreadable file: nothing to review
        self.assertFalse(page.review_button.isEnabled())
        page.select_row(1)
        page.review_button.click()
        self.assertEqual(reviews, [record["id"]])
        page.set_flight_logging_status("Flight Logging: Ready")
        self.assertEqual(page.logging_status.text(), "Flight Logging: Ready")
        page.set_flight_logging_status("")
        self.assertIn("footer", page.logging_status.text())

    def test_selection_survives_refresh(self):
        records = [recorded(flight_date=f"2026-01-0{i + 1}", ev=evidence(gaps=[{"reason": str(i)}])) for i in range(3)]
        page = self.page_with(records, FakeActions())
        page.select_row(1)
        chosen = page.selected_item()
        views, _ = flight_views(records)
        page.set_flights(views)
        self.assertEqual(page.selected_item().flight_id, chosen.flight_id)


class StoreActionsTests(unittest.TestCase):
    def test_protocol_and_fake_store(self):
        calls = []
        store = SimpleNamespace(get=lambda fid: ("flight", fid),
                                acknowledge_gap=lambda flight, gap, note="", by="pilot": calls.append((flight, gap, note)))
        actions = StoreActions(store)
        self.assertIsInstance(actions, GapAcknowledger)
        actions.acknowledge_gap("abc", 2, "seen")
        self.assertEqual(calls, [(("flight", "abc"), 2, "seen")])
        self.assertFalse(actions.can_delete)
        with self.assertRaises(PermissionError):
            actions.delete_recording("abc")

    def test_with_real_flight_store(self):
        try:
            from bbc_efb import storage
            store_cls, evidence_cls, gap_cls = storage.FlightStore, storage.RecorderEvidence, storage.Gap
            fields_cls = storage.FlightFields
        except Exception as error:  # pragma: no cover - storage being edited concurrently
            self.skipTest(f"storage API not available: {error}")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                store = store_cls(tmp, create=True)
                flight = store.create_recorded(
                    fields_cls("2026-11-20", "B738", "YSSY", "YBBN"),
                    evidence_cls("recorder/archive/x.jsonl", "Test", "simulator", False,
                                 gaps=(gap_cls("Simulator disconnected"),)))
            except Exception as error:  # pragma: no cover
                self.skipTest(f"storage API differs: {error}")
            try:
                view = flight_view(flight)
                self.assertEqual(view.status, "gaps")
                StoreActions(store).acknowledge_gap(view.id, 0, "known")
                after = flight_view(store.get(view.id))
                self.assertEqual(after.status, "reviewed")
                self.assertEqual(len(after.gaps), 1)  # evidence kept
                self.assertEqual(after.gaps[0].note, "known")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
