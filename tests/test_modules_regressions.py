"""Regression tests for drop-in module host fixes found in review.

* a module tick() raising SystemExit is contained like any other tick error;
* the host tells listeners when the set of running modules changes (so the shell can start
  its module timer when a module is turned on from Settings);
* module-state.json written by a newer EFB is kept unchanged and never overwritten;
* a deeply nested module.json is reported for its own folder and never aborts the scan;
* a second folder with a running module's id does not relabel it as removed, and scan
  notes never stick after the cause is gone.
"""

from __future__ import annotations

import json
import shutil
import unittest

from bbc_efb.modules import (
    ModuleHost, ModulePolicy, ModuleSettingsReadOnly, ModuleStateStore, ModuleStatus, ProblemCode, read_manifest,
)
from tests.test_modules_loader import Base

SYSTEM_EXIT_TICK = (
    "import sys\n"
    "class M:\n"
    "    def tick(self):\n"
    "        sys.exit(4)\n"
    "def create(c):\n"
    "    return M()\n"
)


class TickSystemExitTests(Base):
    def test_system_exit_from_tick_is_contained_counted_and_stops_the_module(self):
        module_id, _ = self.write_module(code=SYSTEM_EXIT_TICK)
        host = self.enabled_host(policy=ModulePolicy(tick_failure_limit=2))
        host.scan()
        host.set_enabled(module_id, True)
        host.load(module_id)
        try:
            host.tick()  # must not raise SystemExit
        except SystemExit:  # pragma: no cover - the bug
            self.fail("SystemExit from a module tick escaped ModuleHost.tick()")
        self.assertIs(host.record(module_id).status, ModuleStatus.LOADED)
        host.tick()
        record = host.record(module_id)
        self.assertIs(record.status, ModuleStatus.FAILED)
        self.assertIs(record.problems[0].code, ProblemCode.RUNTIME_ERROR)
        self.assertIn("SystemExit", record.problems[0].message)
        self.assertEqual(host.loaded(), ())
        self.assertEqual(self.provider.apis[0].closed, 1)


class ChangeListenerTests(Base):
    def test_listener_runs_when_a_module_starts_or_stops(self):
        module_id, _ = self.write_module()
        host = self.enabled_host()
        host.scan()
        calls = []
        host.add_listener(lambda: calls.append(host.loaded()))
        host.set_enabled(module_id, True)
        self.assertEqual(calls, [], "enabling alone starts nothing")
        host.load(module_id)
        self.assertEqual(calls, [(module_id,)])
        host.load(module_id)  # already running: no change, no call
        self.assertEqual(len(calls), 1)
        host.reload(module_id)  # stops and starts again: same running set, one public call
        self.assertEqual(len(calls), 1)
        host.unload(module_id)
        self.assertEqual(calls[-1], ())
        host.load_enabled()
        self.assertEqual(calls[-1], (module_id,))
        host.set_experimental_enabled(False)
        self.assertEqual(calls[-1], ())

    def test_listener_runs_when_a_failing_tick_stops_a_module(self):
        code = "class M:\n    def tick(self):\n        raise RuntimeError('x')\ndef create(c):\n    return M()\n"
        module_id, _ = self.write_module(code=code)
        host = self.enabled_host(policy=ModulePolicy(tick_failure_limit=1))
        host.scan()
        host.set_enabled(module_id, True)
        host.load(module_id)
        calls = []
        host.add_listener(lambda: calls.append(host.loaded()))
        host.tick()
        self.assertEqual(calls, [()])

    def test_failing_listener_is_logged_and_does_not_undo_the_change(self):
        module_id, _ = self.write_module()
        host = self.enabled_host()
        host.scan()
        host.set_enabled(module_id, True)

        def broken():
            raise RuntimeError("listener broke")

        seen = []
        host.add_listener(broken)
        host.add_listener(lambda: seen.append(True))
        host.load(module_id)
        self.assertEqual(host.loaded(), (module_id,))
        self.assertEqual(seen, [True])
        host.remove_listener(broken)
        with self.assertRaises(TypeError):
            host.add_listener("not callable")
        host.close()


class NewerStateFormatTests(Base):
    def write_newer_state(self):
        state = self.data / "module-state.json"
        text = json.dumps({"format": 2, "experimental_enabled": True,
                           "modules": {"x": {"enabled": True, "granted_groups": ["radios"]}}})
        state.write_text(text, encoding="utf-8")
        return state, text

    def test_newer_state_is_kept_unchanged_and_changes_are_refused(self):
        state, text = self.write_newer_state()
        host = self.host()
        self.assertFalse(host.experimental_enabled)
        self.assertTrue(host.settings_read_only)
        self.assertIn("newer EFB", host.settings_problem)
        self.assertIn("cannot be changed", host.settings_problem)
        self.assertNotIn("without changing them", host.settings_problem)
        with self.assertRaises(ModuleSettingsReadOnly):
            host.set_experimental_enabled(True)
        self.assertFalse(host.experimental_enabled, "a refused change leaves the in-memory state alone")
        self.assertEqual(state.read_text(encoding="utf-8"), text)
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["module-state.json", "modules"])

    def test_store_save_refuses_a_newer_file(self):
        state, text = self.write_newer_state()
        store = ModuleStateStore(state)
        store.load()
        self.assertTrue(store.read_only)
        with self.assertRaises(ModuleSettingsReadOnly):
            store.save(store.load())
        self.assertEqual(state.read_text(encoding="utf-8"), text)

    def test_second_corrupt_file_never_overwrites_the_first_backup(self):
        state = self.data / "module-state.json"
        state.write_text("{first", encoding="utf-8")
        self.host().set_experimental_enabled(True)
        state.write_text("{second", encoding="utf-8")
        self.host().set_experimental_enabled(True)
        backups = sorted(p for p in self.data.iterdir() if p.name.endswith(".corrupt"))
        self.assertEqual(len(backups), 2, backups)
        self.assertEqual((self.data / "module-state.json.corrupt").read_text(), "{first")
        self.assertEqual({p.read_text() for p in backups}, {"{first", "{second"})


class NestedManifestTests(Base):
    def test_deeply_nested_manifest_is_a_problem_for_its_folder_only(self):
        good_id, _ = self.write_module()
        bad = self.modules / "nested"
        bad.mkdir()
        (bad / "module.json").write_text("[" * 30000 + "]" * 30000, encoding="utf-8")
        result = read_manifest(bad)
        self.assertIsNone(result.manifest)
        self.assertIs(result.problems[0].code, ProblemCode.BAD_MANIFEST)
        host = self.enabled_host()
        records = {record.key: record for record in host.scan()}
        self.assertIs(records["folder:nested"].status, ModuleStatus.INVALID)
        self.assertIn("nested too deeply", records["folder:nested"].problems[0].message)
        self.assertIs(records[good_id].status, ModuleStatus.DISABLED)

    def test_unexpected_manifest_error_never_aborts_the_scan(self):
        from unittest import mock
        good_id, _ = self.write_module()
        real = read_manifest

        def flaky(folder):
            if folder.name == good_id:
                raise RuntimeError("reader broke")
            return real(folder)

        other_id, _ = self.write_module()
        host = self.enabled_host()
        with mock.patch("bbc_efb.modules.loader.read_manifest", flaky):
            records = {record.key: record for record in host.scan()}
        self.assertIn("reader broke", records[f"folder:{good_id}"].problems[0].message)
        self.assertIs(records[other_id].status, ModuleStatus.DISABLED)


class DuplicateWhileRunningTests(Base):
    def test_copy_of_running_module_is_not_reported_as_removed_and_notes_do_not_stick(self):
        module_id, _ = self.write_module("radio")
        host = self.enabled_host()
        host.scan()
        host.set_enabled(module_id, True)
        host.load(module_id)
        shutil.copytree(self.modules / "radio", self.modules / "radio-backup")
        records = {record.key: record for record in host.scan()}
        running = records["radio"]
        self.assertIs(running.status, ModuleStatus.LOADED)
        messages = [w.message for w in running.warnings]
        self.assertFalse(any("was removed" in m for m in messages), messages)
        self.assertTrue(any("radio-backup" in m for m in messages), messages)
        self.assertIs(records["radio@radio-backup"].status, ModuleStatus.INVALID)

        shutil.rmtree(self.modules / "radio-backup")
        host.scan()
        record = host.record("radio")
        self.assertIs(record.status, ModuleStatus.LOADED)
        self.assertEqual([w.message for w in record.warnings
                          if "removed" in w.message or "radio-backup" in w.message], [])
        self.assertEqual([r.key for r in host.records() if r.key.startswith("radio")], ["radio"])
        host.close()

    def test_removed_note_is_cleared_when_the_folder_comes_back(self):
        module_id, _ = self.write_module("radio")
        host = self.enabled_host()
        host.scan()
        host.set_enabled(module_id, True)
        host.load(module_id)
        moved = self.data / "radio-away"
        shutil.move(str(self.modules / "radio"), str(moved))
        host.scan()
        self.assertTrue(any("was removed" in w.message for w in host.record("radio").warnings))
        shutil.move(str(moved), str(self.modules / "radio"))
        host.scan()
        self.assertFalse(any("was removed" in w.message for w in host.record("radio").warnings))
        host.close()


if __name__ == "__main__":
    unittest.main()
