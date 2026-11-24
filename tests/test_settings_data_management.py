"""Tests for bbc_efb/data_management.py (Qt-free): folder checks, Export All Data, Backup and Restore."""

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from bbc_efb import data_management as dm

FIXED = datetime(2026, 11, 24, 10, 5, 18, 123000, tzinfo=timezone.utc)
ID_V2 = "6f1c6a8e-3f4e-4c4e-9a52-1d2b9d1d2c11"
ID_V1 = "0b7f9a51-5b9c-4c7e-8a33-7d0a1e6f4b22"
ID_REC = "9a1b2c3d-0000-4000-8000-000000000001"


def clock():
    return FIXED


def v2_record():
    first = {"flight_date": "2026-11-24", "aircraft": "A320", "departure": "EGLL", "arrival": "LFPG",
             "callsign": "BAW12", "registration": "", "minutes": 95, "notes": "=SUM(A1)", "crew": 2,
             "passengers": None, "baggage_kg": None, "freight_kg": 0}
    second = dict(first, arrival="LFPO", notes="=SUM(A1) diverted")
    return {
        "schema": 2, "id": ID_V2, "source": "manual",
        "revisions": [
            {"saved_at": "2026-11-24T10:00:00.000+00:00", "by": "pilot", "fields": first},
            {"saved_at": "2026-11-24T11:00:00.000+00:00", "by": "pilot", "reason": "typo", "fields": second},
        ],
        "extra": {"keep": True},
    }


def v1_record():
    return {"schema": 1, "id": ID_V1, "source": "manual", "revisions": [
        {"saved_at": "2024-01-01T00:00:00+00:00", "fields": {
            "flight_date": "2024-01-01", "aircraft": "C172", "departure": "KSEA", "arrival": "KPAE",
            "callsign": "", "registration": "N123", "minutes": 42, "notes": "Résumé ✈"}}]}


def recorder_record():
    fields = {"flight_date": "2026-11-23", "aircraft": "B738", "departure": "EHAM", "arrival": "",
              "callsign": "", "registration": "", "minutes": None, "notes": "", "crew": None,
              "passengers": None, "baggage_kg": None, "freight_kg": None}
    return {"schema": 2, "id": ID_REC, "source": "recorder",
            "recorder": {"journal": "recorder/journals/x.jsonl", "simulated": True, "gaps": []},
            "revisions": [{"saved_at": "2026-11-23T09:00:00.000+00:00", "by": "recorder", "fields": fields}]}


def make_data_folder(root: Path):
    flights = root / "flights"
    flights.mkdir(parents=True)
    (flights / f"{ID_V2}.json").write_text(json.dumps(v2_record(), indent=2), encoding="utf-8")
    (flights / f"{ID_V1}.json").write_text(json.dumps(v1_record(), ensure_ascii=False), encoding="utf-8")
    (flights / f"{ID_REC}.json").write_text(json.dumps(recorder_record()), encoding="utf-8")
    (flights / "broken.json").write_text("{ nope", encoding="utf-8")
    (flights / ".flight-abc.tmp").write_text("partial", encoding="utf-8")
    (root / ".bbc-efb-writer.lock").write_text("pid 1", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"\x00\x01")
    journals = root / "recorder" / "journals"
    journals.mkdir(parents=True)
    (journals / "x.jsonl").write_bytes(b'{"kind":"flight_started"}\n' * 50)
    (root / "migration" / "schema-1").mkdir(parents=True)
    (root / "migration" / "schema-1" / f"{ID_V1}.json").write_bytes(b'{"schema":1}')
    (root / "logs").mkdir()
    (root / "logs" / "bbc-efb.log").write_text("log line\n", encoding="utf-8")
    (root / ".hidden-staging").mkdir()
    (root / ".hidden-staging" / "junk.json").write_text("{}", encoding="utf-8")
    (root / "binary.bin").write_bytes(os.urandom(300_000))


def tree(root: Path, *, skip=("logs",)):
    """{relative posix path: bytes} for comparable data files."""
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            continue
        first = rel.split("/")[0]
        if first in skip or first.startswith(".") or path.name.endswith(".tmp") or path.name.casefold() in dm.OS_CLUTTER:
            continue
        result[rel] = path.read_bytes()
    return result


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.data = self.base / "Data"
        make_data_folder(self.data)
        self.out = self.base / "out"
        self.out.mkdir()

    def tearDown(self):
        for path in self.base.rglob("*"):
            try:
                path.chmod(0o755)
            except OSError:
                pass
        self._tmp.cleanup()

    def backup(self, **kwargs):
        kwargs.setdefault("app_version", "0.1.0-test")
        kwargs.setdefault("clock", clock)
        return dm.create_backup(self.data, self.out / "b.zip", **kwargs)


# Folder checks ---------------------------------------------------------------

class FolderCheckTests(Base):
    def test_creates_nested_folder_and_probe_leaves_nothing(self):
        target = self.base / "new" / "deeper" / "BBC EFB Data"
        result = dm.check_data_folder(target, create=True)
        self.assertTrue(result.usable, result.problem)
        self.assertTrue(result.created)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_missing_folder_without_create(self):
        result = dm.check_data_folder(self.base / "absent")
        self.assertFalse(result.usable)
        self.assertIn("does not exist", result.problem)
        self.assertFalse((self.base / "absent").exists())

    def test_file_and_empty_input(self):
        file = self.base / "file.txt"
        file.write_text("x")
        self.assertIn("file, not a folder", dm.check_data_folder(file, create=True).problem)
        self.assertIn("No folder", dm.check_data_folder("", create=True).problem)
        self.assertIn("No folder", dm.check_data_folder(None).problem)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "POSIX permissions")
    def test_unwritable_folder_is_refused_and_created_parents_removed(self):
        locked = self.base / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        result = dm.check_data_folder(locked)
        self.assertFalse(result.usable)
        self.assertIn("cannot write", result.problem)
        self.assertIsNotNone(dm.probe_writable(locked))
        with mock.patch.object(dm, "probe_writable", return_value="BBC EFB cannot write to this folder (x)."):
            target = self.base / "made" / "here"
            result = dm.check_data_folder(target, create=True)
        self.assertFalse(result.usable)
        self.assertFalse((self.base / "made").exists(), "folders created for a failed check are removed")

    def test_probe_detects_read_back_mismatch(self):
        real_open = open

        class Liar:
            def __init__(self, path, mode):
                self.handle = real_open(path, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, data):
                self.handle.write(b"other")

            def flush(self):
                self.handle.flush()

            def fileno(self):
                return self.handle.fileno()

        with mock.patch("builtins.open", side_effect=lambda path, mode="r", *a, **k: Liar(path, mode)):
            problem = dm.probe_writable(self.out)
        self.assertIn("read back differently", problem)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_existing_logbook_and_other_files_are_noted(self):
        result = dm.check_data_folder(self.data)
        self.assertTrue(result.usable)
        self.assertEqual(result.existing_flights, 4)
        self.assertTrue(any("already holds 4" in note for note in result.notes))
        self.assertTrue(any("other item" in note for note in result.notes))

    def test_onedrive_drive_root_and_network_notes(self):
        notes = dm.folder_notes(self.base / "OneDrive - Org" / "Logbook")
        self.assertTrue(any("OneDrive" in note for note in notes))
        with mock.patch.dict(os.environ, {"OneDrive": str(self.base / "Sync")}):
            self.assertTrue(any("OneDrive" in note for note in dm.folder_notes(self.base / "Sync" / "x")))
        root = Path(Path.cwd().anchor)
        self.assertTrue(any("top of a drive" in note for note in dm.folder_notes(root)))
        self.assertTrue(any("temporary" in note for note in dm.folder_notes(Path(tempfile.gettempdir()) / "x")))

    def test_summary(self):
        summary = dm.summarize_data_folder(self.data)
        self.assertTrue(summary.exists)
        self.assertEqual(summary.flight_files, 4)
        self.assertIn("4 flight file(s)", summary.describe())
        missing = dm.summarize_data_folder(self.base / "absent")
        self.assertFalse(missing.exists)
        self.assertIn("not", missing.describe())


# Records -------------------------------------------------------------------------

class RecordSummaryTests(unittest.TestCase):
    def test_v2_current_values_and_corrections(self):
        row = dm.summarize_record("f.json", json.dumps(v2_record()).encode())
        self.assertEqual(row.status, "ok")
        self.assertEqual(row.fields["arrival"], "LFPO")
        self.assertEqual(row.corrected, ("arrival", "notes"))
        self.assertEqual(row.revisions, 2)
        self.assertIsNone(row.fields["passengers"])
        self.assertEqual(row.first_saved_at, "2026-11-24T10:00:00.000+00:00")

    def test_v1_and_newer_and_broken(self):
        row = dm.summarize_record("a", json.dumps(v1_record()).encode())
        self.assertEqual((row.schema, row.source, row.fields["crew"]), (1, "manual", None))
        newer = dict(v2_record(), schema=3)
        self.assertIn("newer", dm.summarize_record("n", json.dumps(newer).encode()).status)
        cases = [b"{", b"[]", b'{"revisions": []}', b'{"revisions": [{"fields": 3}]}', b"\xff", None]
        for data in cases:
            with self.subTest(data=data):
                self.assertTrue(dm.summarize_record("x", data).status.startswith("unreadable"))


# Export -----------------------------------------------------------------------------

class ExportTests(Base):
    def export(self, **kwargs):
        kwargs.setdefault("app_version", "0.1.0-test")
        kwargs.setdefault("clock", clock)
        return dm.export_all_data(self.data, self.out / "export.zip", **kwargs)

    def test_export_contents(self):
        before = tree(self.data, skip=())
        result = self.export()
        self.assertEqual(result.path, self.out / "export.zip")
        self.assertEqual(result.flights, 3)
        self.assertEqual(len(result.unreadable), 1)
        self.assertIn("could not be read", result.describe())
        with zipfile.ZipFile(result.path) as archive:
            names = set(archive.namelist())
            self.assertIn("README.txt", names)
            self.assertIn("flights_summary.csv", names)
            self.assertIn("export_info.json", names)
            self.assertNotIn("manifest.json", names, "an export is not a backup")
            self.assertIn(f"data/flights/{ID_V2}.json", names)
            self.assertIn("data/recorder/journals/x.jsonl", names)
            self.assertIn("data/migration/schema-1/" + ID_V1 + ".json", names)
            self.assertIn("data/binary.bin", names)
            for forbidden in ("data/.bbc-efb-writer.lock", "data/flights/.flight-abc.tmp", "data/.DS_Store",
                              "data/logs/bbc-efb.log", "data/.hidden-staging/junk.json"):
                self.assertNotIn(forbidden, names)
            for rel, content in tree(self.data).items():
                self.assertEqual(archive.read("data/" + rel), content, rel)
            readme = archive.read("README.txt").decode("utf-8")
            self.assertIn("NOT a backup", readme)
            self.assertIn("EMPTY cell means the value is unknown", readme)
            info = json.loads(archive.read("export_info.json"))
            self.assertEqual(info["kind"], dm.EXPORT_KIND)
            self.assertEqual(info["created_at"], "2026-11-24T10:05:18.123+00:00")
            self.assertFalse(info["logs_included"])
            raw = archive.read("flights_summary.csv")
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        self.assertEqual(tuple(rows[0].keys()), dm.CSV_HEADER)
        by_id = {row["id"]: row for row in rows}
        v2 = by_id[ID_V2]
        self.assertEqual(v2["arrival"], "LFPO")
        self.assertEqual(v2["passengers"], "", "unknown stays empty")
        self.assertEqual(v2["freight_kg"], "0", "a real zero stays 0")
        self.assertEqual(v2["notes"], "'=SUM(A1) diverted", "formula guard")
        self.assertEqual(v2["corrected_fields"], "arrival notes")
        self.assertEqual(by_id[ID_V1]["notes"], "Résumé ✈")
        self.assertEqual(by_id[ID_V1]["schema"], "1")
        self.assertEqual(by_id[ID_REC]["simulated"], "true")
        self.assertEqual(by_id[ID_REC]["minutes"], "")
        broken = [row for row in rows if row["file"].endswith("broken.json")][0]
        self.assertTrue(broken["status"].startswith("unreadable"))
        self.assertEqual(tree(self.data, skip=()), before, "export only reads")

    def test_logs_can_be_included(self):
        result = self.export(include_logs=True)
        with zipfile.ZipFile(result.path) as archive:
            self.assertIn("data/logs/bbc-efb.log", archive.namelist())

    def test_destination_rules(self):
        with self.assertRaises(dm.DestinationInsideDataFolder):
            dm.export_all_data(self.data, self.data / "inside.zip", app_version="x")
        with self.assertRaises(dm.DestinationInsideDataFolder):
            dm.create_backup(self.data, self.data / "flights" / "inside.zip", app_version="x")
        (self.out / "export.zip").write_bytes(b"keep")
        with self.assertRaises(dm.DestinationExists):
            self.export()
        self.assertEqual((self.out / "export.zip").read_bytes(), b"keep")
        self.export(overwrite=True)
        self.assertTrue(zipfile.is_zipfile(self.out / "export.zip"))
        result = dm.export_all_data(self.data, self.out / "noext", app_version="x")
        self.assertEqual(result.path.name, "noext.zip")
        with self.assertRaises(dm.DataManagementError):
            dm.export_all_data(self.base / "absent", self.out / "e.zip", app_version="x")

    def test_failed_write_leaves_nothing(self):
        with mock.patch.object(zipfile.ZipFile, "writestr", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(dm.DataManagementError) as caught:
                self.export()
        self.assertIn("No space left", str(caught.exception))
        self.assertEqual(list(self.out.iterdir()), [], "no partial ZIP left behind")

    def test_unreadable_file_is_reported_not_hidden(self):
        real = dm._copy_into_zip

        def flaky(archive, name, source, moment, size):
            if name.endswith("binary.bin"):
                raise PermissionError(13, "Permission denied")
            return real(archive, name, source, moment, size)

        with mock.patch.object(dm, "_copy_into_zip", side_effect=flaky):
            result = self.export()
        self.assertEqual(result.skipped, (("binary.bin", "Permission denied"),))
        self.assertIn("could not be copied", result.describe())

    def test_progress_is_reported(self):
        seen = []
        self.export(progress=lambda done, total: seen.append((done, total)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], seen[-1][1])


# Backup -------------------------------------------------------------------------------

class BackupTests(Base):
    def test_manifest(self):
        config = self.base / "preferences.json"
        config.write_text(json.dumps({"format": 1}), encoding="utf-8")
        result = self.backup(record_schema=2, config_files={"preferences.json": config})
        self.assertEqual(result.flights, 4)
        self.assertEqual(result.schema_versions["flight_records_found"], {"1": 1, "2": 2, "unreadable": 1})
        self.assertEqual(result.schema_versions["config_formats"], {"preferences.json": 1})
        self.assertIn("Backup saved", result.describe())
        with zipfile.ZipFile(result.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["kind"], dm.BACKUP_KIND)
            self.assertEqual(manifest["manifest_format"], dm.MANIFEST_FORMAT)
            self.assertEqual(manifest["app_version"], "0.1.0-test")
            self.assertEqual(manifest["created_at"], "2026-11-24T10:05:18.123+00:00")
            self.assertEqual(manifest["source_folder_name"], "Data")
            self.assertNotIn(str(self.base), json.dumps(manifest), "no full paths in the manifest")
            self.assertEqual(manifest["schema_versions"]["flight_record_written_by_app"], 2)
            listed = {item["path"]: item for item in manifest["files"]}
            self.assertEqual(set(listed), set(archive.namelist()) - {"manifest.json"})
            self.assertIn("config/preferences.json", listed)
            for name, item in listed.items():
                content = archive.read(name)
                self.assertEqual(item["size"], len(content))
                self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(manifest["file_count"], len(listed))
            self.assertEqual(manifest["total_bytes"], sum(item["size"] for item in listed.values()))
        self.assertFalse(any(name.startswith("data/logs/") for name in listed))

    def test_bad_config_names_and_missing_config(self):
        with self.assertRaises(ValueError):
            self.backup(config_files={"../x.json": self.base / "x"})
        result = self.backup(config_files={"preferences.json": self.base / "missing.json"})
        self.assertEqual(result.skipped[0][0], "config/preferences.json")
        self.assertIn("WARNING", result.describe())
        inspection = dm.inspect_backup(result.path)
        self.assertTrue(inspection.ok)
        self.assertTrue(any("not in it" in warning for warning in inspection.warnings))

    def test_inspect_good_backup(self):
        result = self.backup()
        inspection = dm.inspect_backup(result.path)
        self.assertTrue(inspection.ok, inspection.problems)
        self.assertTrue(inspection.checksums_verified)
        self.assertEqual(inspection.flight_count, 4)
        self.assertEqual(inspection.app_version, "0.1.0-test")
        self.assertIn("Every file matched its checksum", inspection.describe())
        self.assertTrue(any("could not be read" in warning for warning in inspection.warnings))
        quick = dm.inspect_backup(result.path, verify_checksums=False)
        self.assertTrue(quick.ok)
        self.assertFalse(quick.checksums_verified)


def rewrite_zip(source: Path, target: Path, *, manifest=None, members=None, extra=None, drop=()):
    """Copy a backup ZIP, optionally replacing the manifest or member contents."""
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as copy:
        for info in original.infolist():
            if info.filename in drop:
                continue
            data = original.read(info.filename)
            if info.filename == "manifest.json" and manifest is not None:
                data = json.dumps(manifest(json.loads(data))).encode()
            elif members and info.filename in members:
                data = members[info.filename]
            copy.writestr(info.filename, data)
        for name, data in (extra or {}).items():
            copy.writestr(name, data)
    return target


class InspectProblemTests(Base):
    def setUp(self):
        super().setUp()
        self.good = self.backup().path
        self.name = f"data/flights/{ID_V2}.json"

    def problems(self, **kwargs):
        target = rewrite_zip(self.good, self.out / "bad.zip", **kwargs)
        inspection = dm.inspect_backup(target)
        self.assertFalse(inspection.ok)
        self.assertIn("cannot be restored", inspection.describe())
        return " | ".join(inspection.problems)

    def test_tampered_content(self):
        original = zipfile.ZipFile(self.good).read(self.name)
        tampered = original.replace(b"LFPO", b"KJFK")
        self.assertIn("does not match its checksum", self.problems(members={self.name: tampered}))

    def test_size_mismatch(self):
        self.assertIn("different size", self.problems(members={self.name: b"{}"}))

    def test_missing_and_unlisted(self):
        self.assertIn("missing from the ZIP", self.problems(drop=(self.name,)))
        self.assertIn("not in the manifest", self.problems(extra={"data/flights/sneaky.json": b"{}"}))
        self.assertIn("not in the manifest", self.problems(extra={"elsewhere.txt": b"x"}))

    def test_manifest_problems(self):
        self.assertIn("not describe a BBC EFB backup", self.problems(manifest=lambda m: dict(m, kind="other")))
        self.assertIn("newer BBC EFB", self.problems(manifest=lambda m: dict(m, manifest_format=2)))
        self.assertIn("no valid format", self.problems(manifest=lambda m: dict(m, manifest_format="1")))
        self.assertIn("no file list", self.problems(manifest=lambda m: dict(m, files={})))
        self.assertIn("not a JSON object", self.problems(manifest=lambda m: [m]))

        def entry(**change):
            def edit(manifest):
                manifest["files"][0] = dict(manifest["files"][0], **change)
                return manifest
            return edit

        self.assertIn("no valid SHA-256", self.problems(manifest=entry(sha256="abc")))
        self.assertIn("no valid size", self.problems(manifest=entry(size=-1)))
        self.assertIn("Unsafe file entry", self.problems(manifest=entry(path="data/../../evil.txt")))
        self.assertIn("Unsafe file entry", self.problems(manifest=entry(path="/etc/passwd")))
        self.assertIn("outside the data/", self.problems(manifest=entry(path="other/thing")))

        def duplicate(manifest):
            first = dict(manifest["files"][0])
            first["path"] = first["path"].upper().replace("DATA/", "data/")
            manifest["files"].append(first)
            return manifest

        self.assertIn("listed twice", self.problems(manifest=duplicate))

    def test_export_zip_is_not_a_backup(self):
        export = dm.export_all_data(self.data, self.out / "export.zip", app_version="x")
        inspection = dm.inspect_backup(export.path)
        self.assertFalse(inspection.ok)
        self.assertIn("Export All Data", inspection.problems[0])

    def test_not_a_zip_or_missing(self):
        junk = self.out / "junk.zip"
        junk.write_bytes(b"not a zip")
        self.assertIn("not a readable ZIP", dm.inspect_backup(junk).problems[0])
        self.assertIn("does not exist", dm.inspect_backup(self.out / "nope.zip").problems[0])
        bad_manifest = self.out / "badmanifest.zip"
        with zipfile.ZipFile(bad_manifest, "w") as archive:
            archive.writestr("manifest.json", b"{")
        self.assertIn("cannot be read", dm.inspect_backup(bad_manifest).problems[0])

    def test_file_and_folder_clash(self):
        def clash(manifest):
            content = b"x"
            manifest["files"].append({"path": "data/flights", "size": 1,
                                      "sha256": hashlib.sha256(content).hexdigest()})
            return manifest
        target = rewrite_zip(self.good, self.out / "clash.zip", manifest=clash, extra={"data/flights": b"x"})
        self.assertIn("both as a file and as a folder", " ".join(dm.inspect_backup(target).problems))


class UnsafeNameTests(unittest.TestCase):
    def test_names(self):
        bad = ["", "/abs", "data/../x", "data/./x", "data//x", "data\\x", "C:/x", "data/con.txt", "data/NUL",
               "data/x.", "data/x ", "data/a\x00b", "data/a?b", "data/a\nb", "data/LPT1.json", "x" * 401]
        for name in bad:
            with self.subTest(name=name):
                self.assertIsNotNone(dm.unsafe_member_reason(name))
        for name in ("data/flights/a.json", "data/recorder/journals/x.jsonl", "config/preferences.json",
                     "data/console.json", "data/.hidden"):
            with self.subTest(name=name):
                self.assertIsNone(dm.unsafe_member_reason(name))


# Restore ---------------------------------------------------------------------------------

class RestoreTests(Base):
    def setUp(self):
        super().setUp()
        config = self.base / "preferences.json"
        config.write_text(json.dumps({"format": 1, "logbook": {"history_view": "cards"}}), encoding="utf-8")
        self.config_bytes = config.read_bytes()
        self.archive = self.backup(config_files={"preferences.json": config}).path
        self.expected = tree(self.data)

    def leftovers(self, parent: Path):
        return [path.name for path in parent.iterdir() if ".restore-" in path.name]

    def test_restore_into_missing_folder_round_trip(self):
        target = self.base / "restored" / "Data"
        result = dm.restore_backup(self.archive, target, clock=clock)
        self.assertEqual(tree(target), self.expected)
        self.assertEqual(result.flights, 4)
        self.assertIsNone(result.previous_data)
        self.assertEqual(result.config["preferences.json"], self.config_bytes)
        self.assertFalse((target / "config").exists(), "config files never go into the data folder")
        self.assertFalse((target / "preferences.json").exists())
        self.assertEqual(self.leftovers(target.parent), [])
        self.assertIn("Restored 4 flight(s)", result.describe())

    def test_restore_into_empty_folder_keeps_os_clutter(self):
        target = self.base / "empty"
        target.mkdir()
        (target / "desktop.ini").write_text("[.ShellClassInfo]")
        dm.restore_backup(self.archive, target)
        self.assertEqual(tree(target), self.expected)
        self.assertEqual((target / "desktop.ini").read_text(), "[.ShellClassInfo]")
        self.assertEqual(self.leftovers(self.base), [])

    def test_non_empty_folder_needs_confirmation(self):
        target = self.base / "busy"
        target.mkdir()
        (target / "mine.txt").write_text("precious")
        with self.assertRaises(dm.TargetNotEmpty) as caught:
            dm.restore_backup(self.archive, target)
        self.assertEqual(caught.exception.entries, ("mine.txt",))
        self.assertIn("renamed, not deleted", str(caught.exception))
        self.assertEqual([p.name for p in target.iterdir()], ["mine.txt"])
        self.assertEqual(self.leftovers(self.base), [])

    def test_replace_existing_keeps_previous_contents(self):
        target = self.base / "busy"
        target.mkdir()
        (target / "mine.txt").write_text("precious")
        result = dm.restore_backup(self.archive, target, replace_existing=True, clock=clock)
        self.assertEqual(tree(target), self.expected)
        self.assertEqual(result.previous_data, self.base / "busy.pre-restore-20261124T100518Z")
        self.assertEqual((result.previous_data / "mine.txt").read_text(), "precious")
        self.assertIn("previous contents were kept", result.describe())
        # A second restore at the same instant does not collide with the kept copy.
        again = dm.restore_backup(self.archive, target, replace_existing=True, clock=clock)
        self.assertEqual(again.previous_data.name, "busy.pre-restore-20261124T100518Z-2")
        self.assertEqual(tree(again.previous_data), self.expected)

    def test_replace_the_data_folder_itself(self):
        before = tree(self.data, skip=())
        (self.data / "flights" / f"{ID_V2}.json").write_text("{}")
        result = dm.restore_backup(self.archive, self.data, replace_existing=True, clock=clock)
        self.assertEqual(tree(self.data), self.expected)
        self.assertNotEqual(tree(result.previous_data, skip=()), before)
        self.assertEqual((result.previous_data / "flights" / f"{ID_V2}.json").read_text(), "{}")

    def test_tampered_backup_changes_nothing(self):
        bad = rewrite_zip(self.archive, self.out / "bad.zip",
                          members={f"data/flights/{ID_V2}.json": zipfile.ZipFile(self.archive).read(
                              f"data/flights/{ID_V2}.json").replace(b"LFPO", b"KJFK")})
        target = self.base / "t"
        with self.assertRaises(dm.BackupInvalid) as caught:
            dm.restore_backup(bad, target)
        self.assertIn("checksum", str(caught.exception))
        self.assertFalse(target.exists())
        self.assertEqual(self.leftovers(self.base), [])

    def test_failure_while_putting_in_place_rolls_back(self):
        target = self.base / "busy"
        target.mkdir()
        (target / "mine.txt").write_text("precious")
        real_rename = os.rename
        calls = []

        def failing(source, destination):
            calls.append((Path(source).name, Path(destination).name))
            if len(calls) == 2:
                raise PermissionError(13, "Access is denied")
            return real_rename(source, destination)

        with mock.patch.object(dm.os, "rename", side_effect=failing):
            with self.assertRaises(dm.DataManagementError) as caught:
                dm.restore_backup(self.archive, target, replace_existing=True)
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual([p.name for p in target.iterdir()], ["mine.txt"])
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["Data", "busy", "out", "preferences.json"])

    def test_set_aside_failure_reports_plainly(self):
        target = self.base / "busy"
        target.mkdir()
        (target / "mine.txt").write_text("precious")
        with mock.patch.object(dm.os, "rename", side_effect=PermissionError(13, "in use")):
            with self.assertRaises(dm.DataManagementError) as caught:
                dm.restore_backup(self.archive, target, replace_existing=True)
        self.assertIn("could not be set aside", str(caught.exception))
        self.assertEqual(self.leftovers(self.base), [])

    def test_extraction_failure_cleans_staging(self):
        target = self.base / "t"
        real = dm._read_member
        count = []

        def failing(archive, info, expected, sink=None):
            if sink is not None:
                count.append(1)
            if len(count) == 3:
                raise OSError(28, "No space left on device")
            return real(archive, info, expected, sink)

        with mock.patch.object(dm, "_read_member", side_effect=failing):
            with self.assertRaises(dm.DataManagementError) as caught:
                dm.restore_backup(self.archive, target)
        self.assertIn("No space left", str(caught.exception))
        self.assertFalse(target.exists())
        self.assertEqual(self.leftovers(self.base), [])

    def test_not_enough_space(self):
        usage = shutil._ntuple_diskusage(10, 10, 0) if hasattr(shutil, "_ntuple_diskusage") else None
        fake = usage or type("U", (), {"free": 0})()
        with mock.patch.object(dm.shutil, "disk_usage", return_value=fake):
            with self.assertRaises(dm.DataManagementError) as caught:
                dm.restore_backup(self.archive, self.base / "t")
        self.assertIn("not enough free space", str(caught.exception))

    def test_target_is_a_file_or_drive_root(self):
        file = self.base / "file"
        file.write_text("x")
        with self.assertRaises(dm.DataManagementError):
            dm.restore_backup(self.archive, file)

    def test_backup_inside_target_cannot_replace_it(self):
        target = self.base / "holder"
        target.mkdir()
        inside = target / "b.zip"
        shutil.copy(self.archive, inside)
        with self.assertRaises(dm.DataManagementError) as caught:
            dm.restore_backup(inside, target, replace_existing=True)
        self.assertIn("inside the folder", str(caught.exception))

    def test_folder_entries(self):
        self.assertEqual(dm.folder_entries(self.base / "nope"), ())
        self.assertIn("flights", dm.folder_entries(self.data))
        self.assertNotIn(".DS_Store", dm.folder_entries(self.data))


class NamesTests(unittest.TestCase):
    def test_default_names_have_no_colons(self):
        self.assertEqual(dm.default_export_name(clock), "BBC EFB Export 2026-11-24 1005Z.zip")
        self.assertEqual(dm.default_backup_name(clock), "BBC EFB Backup 2026-11-24 1005Z.zip")
        with self.assertRaises(ValueError):
            dm.default_backup_name(lambda: datetime(2026, 1, 1))

    def test_human_size(self):
        self.assertEqual(dm.human_size(10), "10 bytes")
        self.assertEqual(dm.human_size(2048), "2.0 KB")
        self.assertEqual(dm.human_size(5 * 1024 * 1024), "5.0 MB")


if __name__ == "__main__":
    unittest.main()
