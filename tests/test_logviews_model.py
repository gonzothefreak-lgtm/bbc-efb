"""View model: record mapping, honesty rules, provenance, journal reading (no Qt needed)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bbc_efb.logbook_views import model as m
try:
    from tests.test_logviews_support import evidence, imported, manual, recorded
except ImportError:  # discovered without the package prefix
    from test_logviews_support import evidence, imported, manual, recorded  # type: ignore[no-redef]


class AdapterTests(unittest.TestCase):
    def test_model_module_imports_without_qt(self):
        # Checked in-process (the offline CI guard refuses subprocesses): model.py and the
        # package __init__ import neither Qt nor storage at module level.
        package = Path(m.__file__).resolve().parent
        for name in ("model.py", "__init__.py", "live_status.py"):
            tree = ast.parse((package / name).read_text(encoding="utf-8"))
            imported_names = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    imported_names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported_names.add(("." * node.level) + (node.module or ""))
            for module in imported_names:
                self.assertFalse(module.startswith("PySide6"), f"{name} imports {module}")
                self.assertNotIn("storage", module, f"{name} imports {module}")

    def test_manual_record(self):
        view = m.flight_view(manual(flight_id="6F1C6A8E-3F4E-4C4E-9A52-1D2B9D1D2C11", minutes=95,
                                    callsign="BAW12", notes="Smooth"))
        self.assertEqual(view.id, "6f1c6a8e-3f4e-4c4e-9a52-1d2b9d1d2c11")
        self.assertEqual(view.source, "manual")
        self.assertEqual(view.flight_date, date(2026, 11, 20))
        self.assertEqual(view.flight_time_s, 95 * 60)
        self.assertEqual(view.flight_time_origin, "pilot")
        self.assertEqual(view.status, "manual")
        self.assertFalse(view.needs_attention)
        self.assertFalse(view.has_gaps)
        self.assertIsNone(view.touchdown_rate_fpm)
        self.assertIsNone(view.landings)
        self.assertIsNone(view.distance_nm)
        self.assertIn("baw12", view.search_text)
        self.assertIn("smooth", view.search_text)
        self.assertEqual(view.provenance["aircraft"].origin, "pilot")
        self.assertEqual(view.route, "YSSY \u2192 YBBN")

    def test_schema_one_reads_as_manual(self):
        record = manual()
        record["schema"] = 1
        del record["source"]
        del record["revisions"][0]["by"]
        view = m.flight_view(record)
        self.assertEqual(view.source, "manual")
        self.assertEqual(view.revisions[0].by, "pilot")

    def test_unknown_numbers_stay_unknown(self):
        view = m.flight_view(manual(crew=None, passengers=0))
        self.assertIsNone(view.crew)
        self.assertEqual(view.passengers, 0)
        self.assertIsNone(view.flight_time_s)
        self.assertEqual(m.format_duration(view.flight_time_s), "Unknown")

    def test_booleans_and_bad_types_are_not_numbers(self):
        view = m.flight_view(manual(minutes=True, crew="3"))
        self.assertIsNone(view.minutes)
        self.assertIsNone(view.crew)

    def test_invalid_date_is_a_problem_not_a_crash(self):
        view = m.flight_view(manual(flight_date="2026-13-40"))
        self.assertIsNone(view.flight_date)
        self.assertEqual(view.flight_date_text, "2026-13-40")
        self.assertTrue(any("not a valid date" in p for p in view.problems))
        self.assertEqual(m.format_date(view), "2026-13-40")

    def test_recorder_record(self):
        view = m.flight_view(recorded(callsign="QFA1"))
        self.assertEqual(view.status, "recorded")
        self.assertEqual(view.block_s, 5400.0)
        self.assertEqual(view.touchdown_rate_fpm, -180.0)
        self.assertEqual(view.touchdown_rate_origin, "recorder")
        self.assertEqual(view.landings, 1)  # a recorded landing time is a measured landing
        self.assertEqual(view.landings_origin, "recorder")
        self.assertEqual(view.provenance["aircraft"].origin, "observed")
        self.assertEqual(view.provenance["departure"].origin, "derived")
        self.assertEqual(view.provenance["arrival"].origin, "recorder")
        self.assertEqual(view.time_basis, "simulator")

    def test_flight_time_falls_back_to_block_time(self):
        view = m.flight_view(recorded(minutes=None))
        self.assertEqual(view.flight_time_s, 5400.0)
        self.assertEqual(view.flight_time_origin, "recorder")

    def test_simulated_recording_is_labelled(self):
        view = m.flight_view(recorded(ev=evidence(simulated=True)))
        self.assertTrue(view.simulated)
        self.assertEqual(view.status, "recorded_simulated")
        self.assertIn("simulated", view.source_label.lower())

    def test_gaps_and_acknowledgements(self):
        ev = evidence(gaps=[{"reason": "Disconnected", "start": "2026-11-20T08:30:00+00:00",
                             "end": "2026-11-20T08:32:30+00:00"}, {"reason": "Stale feed"}])
        view = m.flight_view(recorded(ev=ev, acks=((0, "known"),)))
        self.assertEqual(view.status, "gaps")
        self.assertTrue(view.needs_attention)
        self.assertEqual([g.index for g in view.unacknowledged_gaps], [1])
        self.assertTrue(view.gaps[0].acknowledged)
        self.assertEqual(view.gaps[0].note, "known")
        self.assertEqual(view.gaps[0].duration_s, 150.0)
        self.assertIsNone(view.gaps[1].duration_s)
        both = m.flight_view(recorded(ev=ev, acks=((0, ""), (1, ""))))
        self.assertEqual(both.status, "reviewed")
        self.assertFalse(both.needs_attention)
        self.assertEqual(len(both.gaps), 2)  # accepting never removes evidence

    def test_acknowledgement_for_missing_gap_is_ignored(self):
        view = m.flight_view(recorded(acks=((5, ""),)))
        self.assertEqual(view.status, "recorded")

    def test_interrupted_without_gap_needs_attention(self):
        view = m.flight_view(recorded(ev=evidence(status="interrupted")))
        self.assertTrue(view.interrupted)
        self.assertEqual(view.status, "interrupted")
        with_gap = m.flight_view(recorded(ev=evidence(status="interrupted", gaps=[{"reason": "App closed"}]),
                                          acks=((0, ""),)))
        self.assertEqual(with_gap.status, "reviewed")

    def test_recorder_without_evidence(self):
        record = recorded()
        del record["recorder"]
        view = m.flight_view(record)
        self.assertEqual(view.status, "evidence_missing")
        self.assertTrue(any("evidence" in p for p in view.problems))

    def test_imported_flags_only_real_problems(self):
        plain = m.flight_view(imported())
        self.assertEqual(plain.status, "imported")
        self.assertFalse(plain.needs_attention)
        self.assertEqual(plain.importer, "CSV")
        bad = m.flight_view(imported(flight_date="not a date"))
        self.assertEqual(bad.status, "import_review")

    def test_imported_rate_is_labelled_imported(self):
        view = m.flight_view(imported(extra={"touchdown_rate_fpm": -210}))
        self.assertEqual(view.touchdown_rate_fpm, -210.0)
        self.assertEqual(view.touchdown_rate_origin, "imported")

    def test_manual_extra_rate_is_never_used(self):
        view = m.flight_view(manual(extra={"touchdown_rate_fpm": -100}))
        self.assertIsNone(view.touchdown_rate_fpm)

    def test_extra_values_latest_revision_first(self):
        record = manual(extra={"operation": "charter", "distance_nm": 400, "landings": 3})
        record["revisions"][0]["fields"]["operation"] = "freight"
        view = m.flight_view(record)
        self.assertEqual(view.operation, "freight")
        self.assertEqual(view.operation_label, "Freight")
        self.assertEqual(view.distance_nm, 400.0)
        self.assertEqual(view.distance_origin, "pilot")
        self.assertEqual(view.landings, 3)
        self.assertEqual(view.landings_origin, "pilot")
        self.assertEqual(m.operation_label("VSOA ops"), "VSOA ops")

    def test_recorder_measured_values_win(self):
        record = recorded(ev=evidence(distance_nm=512.5, touchdowns=[
            {"time": "2026-11-20T09:20:00+00:00", "rate_fpm": -300, "bounce": False},
            {"time": "2026-11-20T09:20:03+00:00", "rate_fpm": -90, "bounce": True},
            {"time": "2026-11-20T10:20:03+00:00", "rate_fpm": -150},
        ]))
        record["extra"] = {"distance_nm": 1}
        view = m.flight_view(record)
        self.assertEqual(view.distance_nm, 512.5)
        self.assertEqual(view.distance_origin, "recorder")
        self.assertEqual(view.landings, 2)
        self.assertEqual(len(view.touchdowns), 3)
        self.assertTrue(view.touchdowns[1].bounce)

    def test_diversion_only_when_confirmed(self):
        unconfirmed = m.flight_view(manual(extra={"diversion": {"confirmed": False, "planned_arrival": "YSSY"}}))
        self.assertFalse(unconfirmed.diverted)
        confirmed = m.flight_view(manual(extra={"diversion": {"confirmed": True, "planned_arrival": "YSSY"}}))
        self.assertTrue(confirmed.diverted)

    def test_corrections_and_record_history(self):
        record = recorded(arrival="YBBN")
        record["revisions"].append({
            "saved_at": "2026-11-21T08:00:00.000+00:00", "by": "pilot", "reason": "Recorder lost the arrival",
            "fields": dict(record["revisions"][0]["fields"], arrival="YBCG", notes="fixed"),
        })
        record["revisions"].append({
            "saved_at": "2026-11-22T08:00:00.000+00:00", "by": "pilot", "reason": "",
            "fields": dict(record["revisions"][1]["fields"], arrival="YBCG"),
        })
        view = m.flight_view(record)
        prov = view.provenance["arrival"]
        self.assertEqual(prov.original, "YBBN")
        self.assertEqual(prov.value, "YBCG")
        self.assertEqual(len(prov.corrections), 1)
        self.assertEqual(prov.corrections[0].reason, "Recorder lost the arrival")
        self.assertEqual(set(view.corrected_fields), {"arrival", "notes"})
        self.assertEqual(view.revisions[1].by_label, "Pilot")
        self.assertEqual([c.name for c in view.revisions[1].changes], ["arrival", "notes"])
        self.assertEqual(view.revisions[2].changes, ())
        self.assertEqual(view.arrival, "YBCG")
        self.assertEqual(view.created_at, "2026-11-20T09:31:00.000+00:00")
        self.assertEqual(view.updated_at, "2026-11-22T08:00:00.000+00:00")

    def test_newer_schema_is_read_only(self):
        record = manual()
        record["schema"] = 3
        view = m.flight_view(record)
        self.assertTrue(view.read_only)
        self.assertIn("newer", view.read_only_reason)

    def test_unknown_source_is_shown_with_problem(self):
        record = manual()
        record["source"] = "telepathy"
        view = m.flight_view(record)
        self.assertEqual(view.source, "imported")
        self.assertTrue(view.problems)

    def test_not_a_record(self):
        for bad in ([], {"id": "x"}, {"id": "x", "revisions": []}, {"revisions": [{"fields": {}}]}):
            with self.assertRaises(m.ViewError):
                m.flight_view_from_record(bad)
        views, failures = m.flight_views([manual(), {"id": "y"}, object()])
        self.assertEqual(len(views), 1)
        self.assertEqual(len(failures), 2)

    def test_payload_labels(self):
        self.assertEqual(m.flight_view(manual()).payload.label, "Unknown")
        self.assertEqual(m.flight_view(manual(baggage_kg=500, freight_kg=1000)).payload.label, "1,500 kg (baggage + freight)")
        partial = m.flight_view(manual(freight_kg=18420)).payload
        self.assertFalse(partial.complete)
        self.assertEqual(partial.label, "At least 18,420 kg (incomplete: baggage unknown)")
        zero = m.flight_view(manual(baggage_kg=0, freight_kg=0)).payload
        self.assertEqual(zero.label, "0 kg (baggage + freight)")
        pilot = m.flight_view(manual(extra={"payload_total_kg": 20000})).payload
        self.assertEqual(pilot.label, "20,000 kg (pilot-entered total)")

    def test_format_helpers(self):
        self.assertEqual(m.format_duration(3900), "1:05")
        self.assertEqual(m.format_rate(-182.4), "-182 fpm")
        self.assertEqual(m.format_rate(None), "Not measured")
        self.assertEqual(m.format_time("2026-11-20T08:15:00+00:00"), "08:15")
        self.assertEqual(m.format_time("2026-11-20T08:15:00"), "Not recorded")  # no offset: unknown basis
        self.assertEqual(m.format_value(1200, "kg"), "1,200 kg")
        self.assertEqual(m.aircraft_key("  b738  bcf "), "B738 BCF")

    def test_evidence_timeline_sorted(self):
        ev = evidence(gaps=[{"reason": "Disconnected", "start": "2026-11-20T08:40:00+00:00"}])
        timeline = m.evidence_timeline(m.flight_view(recorded(ev=ev)))
        self.assertEqual([e.kind for e in timeline], ["block_off", "takeoff", "gap", "touchdown", "block_on"])


class StorageInteropTests(unittest.TestCase):
    """The adapter works with real storage objects (skipped if storage is mid-change)."""

    def setUp(self):
        try:
            from bbc_efb import storage
        except Exception as error:  # pragma: no cover - concurrent edits
            self.skipTest(f"storage not importable: {error}")
        if not hasattr(storage, "parse_record"):
            self.skipTest("storage.parse_record not available")
        self.storage = storage

    def test_parse_record_object(self):
        record = recorded(ev=evidence(gaps=[{"reason": "Disconnected"}]), acks=((0, "seen"),))
        flight = self.storage.parse_record(record, record["id"])
        view = m.flight_view(flight)
        self.assertEqual(view.source, "recorder")
        self.assertEqual(view.status, "reviewed")
        self.assertEqual(view.provenance["aircraft"].origin, "observed")

    def test_object_without_raw_payload(self):
        import dataclasses
        record = recorded(ev=evidence(gaps=[{"reason": "Disconnected"}]))
        record["revisions"][0]["fields"]["operation"] = "freight"
        flight = self.storage.parse_record(record, record["id"])
        stripped = dataclasses.replace(flight, raw=None)
        view = m.flight_view(stripped)
        self.assertEqual(view.status, "gaps")
        self.assertEqual(view.operation, "freight")
        self.assertEqual(view.touchdown_rate_fpm, -180.0)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "recorder" / "archive").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, lines, name="recorder/archive/f.jsonl"):
        path = self.root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return name

    def event(self, kind, sim, data=None, phase="airborne"):
        return json.dumps({"kind": kind, "phase": phase, "host_time": "2026-11-20T08:00:00+00:00",
                           "sim_time_s": sim, "sim_utc": f"2026-11-20T08:{int(sim // 60):02d}:00+00:00",
                           "data": data or {}})

    def test_reads_events_samples_and_torn_lines(self):
        lines = [self.event("flight_started", 0, phase="preflight")]
        lines += [self.event("sample", float(i), {"altitude_ft": i * 10}) for i in range(1, 50)]
        lines += [json.dumps({"seq": 9, "event": json.loads(self.event("touchdown", 50, {"rate_fpm": -210}))}),
                  self.event("bounce", 52, {"touchdown_rate_fpm": -80}), '{"kind": "takeo']
        contents = m.load_journal(self.root, self.write(lines))
        self.assertEqual([e.kind for e in contents.events], ["flight_started", "touchdown", "bounce"])
        self.assertEqual(contents.torn_lines, 1)
        self.assertEqual(len(contents.profile), 49)
        self.assertEqual(contents.profile[0].seconds, 0.0)
        self.assertEqual(contents.profile_origin_sim_s, 1.0)
        self.assertEqual([t.rate_fpm for t in contents.touchdowns], [-210.0, -80.0])
        self.assertTrue(contents.touchdowns[1].bounce)
        self.assertEqual(m.event_offset_s(contents.events[1], contents), 49.0)
        self.assertEqual(contents.problem, "")

    def test_profile_is_thinned(self):
        lines = [self.event("sample", float(i), {"altitude_ft": (i * 37) % 1000}) for i in range(20000)]
        contents = m.load_journal(self.root, self.write(lines), max_samples=500)
        self.assertLessEqual(len(contents.profile), 520)
        self.assertEqual(max(p.altitude_ft for p in contents.profile), 999)

    def test_unsafe_and_missing_paths(self):
        self.assertIn("not inside", m.load_journal(self.root, "../secret.jsonl").problem)
        self.assertIn("not inside", m.load_journal(self.root, "/etc/passwd").problem)
        self.assertIn("not inside", m.load_journal(self.root, "C:\\Windows\\x").problem)
        self.assertIn("not available", m.load_journal(self.root, "recorder/archive/missing.jsonl").problem)


if __name__ == "__main__":
    unittest.main()
