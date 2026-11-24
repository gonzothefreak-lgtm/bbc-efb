"""Discovery, validation, grants, persistence and load/unload of drop-in modules.

Sample modules are written into a temporary data folder. Each test uses fresh
package names so imports never collide across tests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from uuid import uuid4

from bbc_core.contract import CommandState, CoreRequestError
from bbc_core.core import Core
from bbc_core.simulated import SimulatedProvider
from bbc_efb.modules import (
    ModuleHost, ModuleManifest, ModulePolicy, ModuleStateStore, ModuleStatus, PartialGrant, ProblemCode,
    ScopedAPI, core_api_provider,
)

logging.getLogger("bbc_efb.modules").setLevel(logging.CRITICAL)


def unique(prefix="pkg"):
    return f"{prefix}_{uuid4().hex[:10]}"


class FakeAPI:
    def __init__(self, name, groups, capabilities=("radio.com1.active_frequency",)):
        self.name = name
        self.groups = groups
        self.closed = 0
        self._secret = "core internals"
        self._capabilities = capabilities

    def discover(self):
        class Cap:
            def __init__(self, cid):
                self.capability = type("C", (), {"id": cid})()
                self.provider_supported = True
        return type("D", (), {"capabilities": tuple(Cap(c) for c in self._capabilities)})()

    def poll(self, limit=128):
        return ()

    def close(self):
        self.closed += 1


class FakeProvider:
    def __init__(self, fail=None):
        self.apis: list[FakeAPI] = []
        self.fail = fail

    def __call__(self, name, groups):
        if self.fail:
            raise CoreRequestError(self.fail)
        api = FakeAPI(name, groups)
        self.apis.append(api)
        return api


FACTORY = '''
import json, pathlib
EVENTS = []
class Module:
    def __init__(self, context):
        self.context = context
        self.ticks = 0
        EVENTS.append(("start", context.manifest.id, sorted(context.granted_groups)))
    def tick(self):
        self.ticks += 1
    def close(self):
        EVENTS.append(("close", self.context.manifest.id))
def create(context):
    return Module(context)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.modules = self.data / "modules"
        self.modules.mkdir()
        self.provider = FakeProvider()
        self.path_before = list(sys.path)
        self.addCleanup(self._check_path)

    def _check_path(self):
        self.assertEqual(sys.path, self.path_before, "module folders must be removed from sys.path")

    def host(self, **kwargs):
        kwargs.setdefault("api_version", "0.1-draft")
        return ModuleHost(self.data, kwargs.pop("provider", self.provider), **kwargs)

    def enabled_host(self, **kwargs):
        host = self.host(**kwargs)
        host.set_experimental_enabled(True)
        return host

    def write_module(self, module_id=None, *, package=None, code=FACTORY, folder=None, entry_attr="create",
                     manifest=None, **fields):
        module_id = module_id or unique("mod")
        package = package or unique()
        folder = self.modules / (folder or module_id)
        (folder / package).mkdir(parents=True)
        (folder / package / "__init__.py").write_text(textwrap.dedent(code), encoding="utf-8")
        payload = {
            "manifest_format": 1, "id": module_id, "name": f"Sample {module_id}", "version": "1.0.0",
            "api_version": ">=0.1,<0.2", "entry": f"{package}:{entry_attr}", "control_groups": [],
            "description": "A test module.", "author": "Tests",
        }
        payload.update(fields)
        if manifest is not None:
            payload = manifest
        (folder / "module.json").write_text(json.dumps(payload), encoding="utf-8")
        return module_id, package


class FeatureFlagTests(Base):
    def test_off_by_default_and_nothing_scanned(self):
        module_id, package = self.write_module()
        host = self.host()
        self.assertFalse(host.experimental_enabled)
        self.assertEqual(host.scan(), ())
        host.load_enabled()
        self.assertNotIn(package, sys.modules)
        self.assertEqual(self.provider.apis, [])
        self.assertIn("off", host.feature_message())

    def test_switch_persists_and_creates_folder(self):
        self.modules.rmdir()
        host = self.host()
        host.set_experimental_enabled(True)
        self.assertTrue(self.modules.is_dir())
        self.assertTrue(self.host().experimental_enabled)
        state = json.loads((self.data / "module-state.json").read_text())
        self.assertIs(state["experimental_enabled"], True)

    def test_turning_off_unloads_dropins(self):
        module_id, package = self.write_module()
        host = self.enabled_host()
        host.scan()
        host.set_enabled(module_id, True)
        host.load_enabled()
        self.assertEqual(host.loaded(), (module_id,))
        host.set_experimental_enabled(False)
        self.assertEqual(host.loaded(), ())
        self.assertNotIn(package, sys.modules)
        self.assertEqual(self.provider.apis[0].closed, 1)
        self.assertEqual(host.records(), ())


class DiscoveryTests(Base):
    def test_valid_module_is_disabled_until_enabled(self):
        module_id, _ = self.write_module()
        host = self.enabled_host()
        (record,) = host.scan()
        self.assertEqual(record.key, module_id)
        self.assertIs(record.status, ModuleStatus.DISABLED)
        self.assertTrue(any(w.code is ProblemCode.DRAFT_API for w in record.warnings))
        self.assertEqual(record.summary, "Off")

    def test_bad_manifest_reported(self):
        folder = self.modules / "broken"
        folder.mkdir()
        (folder / "module.json").write_text("{", encoding="utf-8")
        (self.modules / "nomanifest").mkdir()
        host = self.enabled_host()
        records = {r.key: r for r in host.scan()}
        self.assertEqual(set(records), {"folder:broken", "folder:nomanifest"})
        for record in records.values():
            self.assertIs(record.status, ModuleStatus.INVALID)
            self.assertIs(record.problems[0].code, ProblemCode.BAD_MANIFEST)
            self.assertIsNone(record.id)
        self.assertIn("no module.json", records["folder:nomanifest"].summary)

    def test_hidden_files_and_zip_ignored(self):
        (self.modules / ".hidden").mkdir()
        (self.modules / "_parked").mkdir()
        (self.modules / "readme.txt").write_text("hi")
        (self.modules / "thing.zip").write_bytes(b"PK")
        host = self.enabled_host()
        self.assertEqual(host.scan(), ())
        (ignored,) = host.ignored()
        self.assertEqual(ignored[0].name, "thing.zip")
        self.assertIn("Unzip", ignored[1])

    def test_api_mismatch(self):
        module_id, _ = self.write_module(api_version=">=1.0")
        (record,) = self.enabled_host().scan()
        self.assertIs(record.status, ModuleStatus.INCOMPATIBLE)
        self.assertIs(record.problems[0].code, ProblemCode.API_VERSION_MISMATCH)
        self.assertIn("this EFB provides 0.1-draft", record.problems[0].message)

    def test_unknown_control_group(self):
        self.write_module(control_groups=["radios", "teleport"])
        (record,) = self.enabled_host().scan()
        self.assertIs(record.status, ModuleStatus.INCOMPATIBLE)
        self.assertIs(record.problems[0].code, ProblemCode.UNKNOWN_CONTROL_GROUP)
        self.assertIn("teleport", record.problems[0].message)

    def test_entry_package_must_be_in_folder(self):
        module_id = unique("mod")
        self.write_module(module_id, entry_attr="create")
        manifest_path = self.modules / module_id / "module.json"
        payload = json.loads(manifest_path.read_text())
        payload["entry"] = "elsewhere:create"
        manifest_path.write_text(json.dumps(payload))
        (record,) = self.enabled_host().scan()
        self.assertIs(record.status, ModuleStatus.INVALID)
        self.assertIn("no \"elsewhere\" folder", record.problems[0].message)

    def test_stdlib_and_installed_names_rejected(self):
        self.write_module(package="json")
        self.write_module(package="unittest")
        self.write_module(package="bbc_core")
        host = self.enabled_host()
        records = host.scan()
        self.assertEqual(len(records), 3)
        for record in records:
            self.assertIs(record.status, ModuleStatus.INVALID)
            self.assertIs(record.problems[0].code, ProblemCode.NAME_CONFLICT)

    def test_duplicate_ids_both_blocked(self):
        self.write_module("same.id", folder="one")
        self.write_module("same.id", folder="two")
        records = self.enabled_host().scan()
        self.assertEqual({r.key for r in records}, {"same.id@one", "same.id@two"})
        for record in records:
            self.assertIs(record.problems[0].code, ProblemCode.DUPLICATE_ID)
            self.assertIn("one, two", record.problems[0].message)

    def test_shared_package_name_blocked(self):
        package = unique()
        self.write_module(package=package)
        self.write_module(package=package)
        for record in self.enabled_host().scan():
            self.assertIs(record.problems[0].code, ProblemCode.NAME_CONFLICT)

    def test_folder_name_mismatch_is_a_note(self):
        module_id, _ = self.write_module(folder="different")
        (record,) = self.enabled_host().scan()
        self.assertIs(record.status, ModuleStatus.DISABLED)
        self.assertTrue(any("different" in w.message for w in record.warnings))

    def test_missing_required_module(self):
        module_id, _ = self.write_module(requires={"modules": ["not.installed"]})
        (record,) = self.enabled_host().scan()
        self.assertIs(record.status, ModuleStatus.MISSING_PREREQUISITE)
        self.assertIn("not.installed", record.problems[0].message)

    def test_dependency_resolves_after_install(self):
        base_id = unique("base")
        dependent_id, _ = self.write_module(requires={"modules": [base_id]})
        host = self.enabled_host()
        host.scan()
        self.assertIs(host.record(dependent_id).status, ModuleStatus.MISSING_PREREQUISITE)
        self.write_module(base_id)
        host.scan()
        self.assertIs(host.record(dependent_id).status, ModuleStatus.DISABLED)
        self.assertEqual(host.record(dependent_id).problems, ())

    def test_max_modules(self):
        for _ in range(3):
            self.write_module()
        host = self.enabled_host(policy=ModulePolicy(max_modules=2))
        self.assertEqual(len(host.scan()), 2)
        self.assertIn("More than 2", host.ignored()[0][1])


class GrantAndStateTests(Base):
    def test_grant_persists_and_requires_request(self):
        module_id, _ = self.write_module(control_groups=["radios", "flight_files"])
        host = self.enabled_host()
        host.scan()
        record = host.record(module_id)
        self.assertEqual(record.missing_groups, ("radios", "flight_files"))
        self.assertTrue(any(w.code is ProblemCode.NOT_GRANTED for w in record.warnings))
        with self.assertRaises(ValueError):
            host.grant(module_id, ["electrical"])
        record = host.grant(module_id, ["radios"])
        self.assertEqual(record.granted_groups, ("radios",))
        again = self.host()
        again.scan()
        self.assertEqual(again.record(module_id).granted_groups, ("radios",))
        again.revoke(module_id, ["radios"])
        self.assertEqual(again.record(module_id).granted_groups, ())

    def test_version_change_voids_grants(self):
        module_id, package = self.write_module(control_groups=["radios"])
        host = self.enabled_host()
        host.scan()
        host.grant(module_id, ["radios"])
        manifest_path = self.modules / module_id / "module.json"
        payload = json.loads(manifest_path.read_text())
        payload["version"] = "1.1.0"
        manifest_path.write_text(json.dumps(payload))
        host.scan()
        self.assertEqual(host.record(module_id).granted_groups, ())
        keep = self.host(policy=ModulePolicy(regrant_on_version_change=False))
        keep.scan()
        self.assertEqual(keep.record(module_id).granted_groups, ("radios",))

    def test_enabled_state_persists(self):
        module_id, _ = self.write_module()
        host = self.enabled_host()
        host.scan()
        host.set_enabled(module_id, True)
        again = self.host()
        again.scan()
        self.assertIs(again.record(module_id).status, ModuleStatus.READY)
        self.assertTrue(again.record(module_id).enabled)

    def test_corrupt_state_file_kept_aside(self):
        state = self.data / "module-state.json"
        state.write_text("{garbage", encoding="utf-8")
        host = self.host()
        self.assertFalse(host.experimental_enabled)
        self.assertIn("could not be read", host.settings_problem)
        host.set_experimental_enabled(True)
        self.assertEqual((self.data / "module-state.json.corrupt").read_text(), "{garbage")
        self.assertTrue(json.loads(state.read_text())["experimental_enabled"])

    def test_newer_state_format_not_trusted(self):
        state = self.data / "module-state.json"
        state.write_text(json.dumps({"format": 99, "experimental_enabled": True}))
        store = ModuleStateStore(state)
        self.assertFalse(store.load().experimental_enabled)
        self.assertIn("newer EFB", store.load_problem)

    def test_unknown_key_errors(self):
        host = self.enabled_host()
        host.scan()
        with self.assertRaises(KeyError):
            host.set_enabled("nope", True)


class LoadTests(Base):
    def loaded_host(self, module_id, **kwargs):
        host = self.enabled_host(**kwargs)
        host.scan()
        host.set_enabled(module_id, True)
        return host

    def test_load_tick_unload(self):
        module_id, package = self.write_module(control_groups=["radios"])
        host = self.loaded_host(module_id)
        host.grant(module_id, ["radios"])
        host.load_enabled()
        record = host.record(module_id)
        self.assertIs(record.status, ModuleStatus.LOADED, record.problems)
        self.assertEqual(record.granted_groups, ("radios",))
        self.assertEqual(self.provider.apis[0].groups, ("radios",))
        self.assertEqual(self.provider.apis[0].name, module_id)
        folder = str((self.modules / module_id).resolve())
        self.assertIn(folder, sys.path)
        self.assertEqual(sys.path.index(folder), len(sys.path) - 1, "module folders are appended, never prepended")
        instance = host.instance(module_id)
        self.assertIsInstance(instance.context.api, ScopedAPI)
        self.assertEqual(instance.context.data_dir, self.data / "module_data" / module_id)
        self.assertFalse(instance.context.data_dir.exists())
        instance.context.ensure_data_dir()
        self.assertTrue(instance.context.data_dir.is_dir())
        host.tick()
        host.tick()
        self.assertEqual(instance.ticks, 2)
        events = sys.modules[package].EVENTS
        host.unload(module_id)
        self.assertEqual(events, [("start", module_id, ["radios"]), ("close", module_id)])
        self.assertEqual(self.provider.apis[0].closed, 1)
        self.assertNotIn(package, sys.modules)
        self.assertIs(host.record(module_id).status, ModuleStatus.READY)
        self.assertIsNone(host.instance(module_id))

    def test_partial_grant_loads_with_granted_only(self):
        module_id, _ = self.write_module(control_groups=["radios", "electrical"])
        host = self.loaded_host(module_id)
        host.grant(module_id, ["radios"])
        host.load(module_id)
        context = host.instance(module_id).context
        self.assertEqual(context.granted_groups, frozenset({"radios"}))
        self.assertEqual(context.denied_groups, frozenset({"electrical"}))
        self.assertEqual(self.provider.apis[0].groups, ("radios",))
        host.close()

    def test_partial_grant_refuse_policy(self):
        module_id, package = self.write_module(control_groups=["radios"])
        host = self.loaded_host(module_id, policy=ModulePolicy(partial_grants=PartialGrant.REFUSE))
        record = host.load(module_id)
        self.assertIs(record.status, ModuleStatus.READY)
        self.assertIs(record.problems[0].code, ProblemCode.NOT_GRANTED)
        self.assertNotIn(package, sys.modules)
        self.assertEqual(self.provider.apis, [])
        self.assertEqual(host.grant(module_id, ["radios"]).problems, ())
        self.assertIs(host.load(module_id).status, ModuleStatus.LOADED)
        host.close()

    def test_load_requires_enabled(self):
        module_id, package = self.write_module()
        host = self.enabled_host()
        host.scan()
        record = host.load(module_id)
        self.assertIs(record.status, ModuleStatus.DISABLED)
        self.assertIn("Turn the module on", record.problems[0].message)
        self.assertNotIn(package, sys.modules)

    def test_import_error(self):
        module_id, package = self.write_module(code="raise RuntimeError('boom at import')\n")
        host = self.loaded_host(module_id)
        record = host.load(module_id)
        self.assertIs(record.status, ModuleStatus.FAILED)
        self.assertIs(record.problems[0].code, ProblemCode.IMPORT_ERROR)
        self.assertIn("boom at import", record.problems[0].message)
        self.assertNotIn(package, sys.modules)
        self.assertEqual(self.provider.apis[0].closed, 1)

    def test_syntax_error(self):
        module_id, _ = self.write_module(code="def broken(:\n")
        record = self.loaded_host(module_id).load(module_id)
        self.assertIs(record.problems[0].code, ProblemCode.IMPORT_ERROR)
        self.assertIn("SyntaxError", record.problems[0].message)

    def test_system_exit_at_import_is_contained(self):
        module_id, _ = self.write_module(code="import sys\nsys.exit(3)\n")
        record = self.loaded_host(module_id).load(module_id)
        self.assertIs(record.status, ModuleStatus.FAILED)
        self.assertIn("SystemExit", record.problems[0].message)

    def test_missing_third_party_package_is_prerequisite(self):
        missing = unique("missinglib")
        module_id, _ = self.write_module(code=f"import {missing}\n")
        record = self.loaded_host(module_id).load(module_id)
        self.assertIs(record.status, ModuleStatus.MISSING_PREREQUISITE)
        self.assertIn(missing, record.problems[0].message)

    def test_declared_python_package_checked_before_import(self):
        missing = unique("missinglib")
        module_id, package = self.write_module(requires={"python_packages": [missing]})
        record = self.loaded_host(module_id).load(module_id)
        self.assertIs(record.status, ModuleStatus.MISSING_PREREQUISITE)
        self.assertNotIn(package, sys.modules)

    def test_vendored_python_package_is_found(self):
        vendored = unique("vendored")
        module_id, _ = self.write_module(requires={"python_packages": [vendored]},
                                         code=f"import {vendored}\n" + FACTORY)
        (self.modules / module_id / f"{vendored}.py").write_text("VALUE = 1\n")
        host = self.loaded_host(module_id)
        self.assertIs(host.load(module_id).status, ModuleStatus.LOADED)
        host.close()
        sys.modules.pop(vendored, None)

    def test_missing_capability(self):
        module_id, package = self.write_module(requires={"capabilities": ["aircraft.warp_drive"]})
        record = self.loaded_host(module_id).load(module_id)
        self.assertIs(record.status, ModuleStatus.MISSING_PREREQUISITE)
        self.assertIn("aircraft.warp_drive", record.problems[0].message)
        self.assertNotIn(package, sys.modules)
        self.assertEqual(self.provider.apis[0].closed, 1)

    def test_command_capability_hidden_by_missing_grant_is_not_missing_support(self):
        class GroupAwareProvider(FakeProvider):
            def __call__(self, name, groups):
                api = FakeAPI(name, groups, ("radio.com1.active_frequency",
                                             *(("radio.com1.swap",) if "radios" in groups else ())))
                self.apis.append(api)
                return api

        provider = GroupAwareProvider()
        module_id, _ = self.write_module(control_groups=["radios"],
                                         requires={"capabilities": ["radio.com1.swap"]})
        host = self.enabled_host(provider=provider)
        host.scan()
        host.set_enabled(module_id, True)
        record = host.load(module_id)
        self.assertIs(record.status, ModuleStatus.LOADED, record.problems)
        self.assertEqual([api.groups for api in provider.apis], [(), ("radios",)])
        self.assertEqual(provider.apis[1].closed, 1, "the probe handle is closed at once")
        self.assertEqual(provider.apis[0].closed, 0)
        host.close()

    def test_factory_missing_or_not_callable(self):
        module_id, _ = self.write_module(entry_attr="nope")
        record = self.loaded_host(module_id).load(module_id)
        self.assertIs(record.problems[0].code, ProblemCode.BAD_MANIFEST)
        self.assertIn("has no \"nope\"", record.problems[0].message)
        other, _ = self.write_module(code="create = 5\n")
        host = self.enabled_host()
        host.scan()
        host.set_enabled(other, True)
        self.assertIn("not callable", host.load(other).problems[0].message)

    def test_factory_error(self):
        module_id, package = self.write_module(code="def create(context):\n    raise ValueError('bad start')\n")
        record = self.loaded_host(module_id).load(module_id)
        self.assertIs(record.status, ModuleStatus.FAILED)
        self.assertIs(record.problems[0].code, ProblemCode.FACTORY_ERROR)
        self.assertIn("bad start", record.problems[0].message)
        self.assertEqual(self.provider.apis[0].closed, 1)
        self.assertNotIn(package, sys.modules)

    def test_core_refusal(self):
        module_id, _ = self.write_module()
        host = self.enabled_host(provider=FakeProvider(fail="Core consumer limit reached."))
        host.scan()
        host.set_enabled(module_id, True)
        record = host.load(module_id)
        self.assertIs(record.problems[0].code, ProblemCode.CORE_REFUSED)
        self.assertIn("consumer limit", record.problems[0].message)

    def test_close_error_is_reported_but_handle_closed(self):
        code = "class M:\n    def close(self):\n        raise OSError('disk')\ndef create(c):\n    return M()\n"
        module_id, package = self.write_module(code=code)
        host = self.loaded_host(module_id)
        host.load(module_id)
        record = host.unload(module_id)
        self.assertTrue(any(w.code is ProblemCode.CLOSE_ERROR for w in record.warnings))
        self.assertEqual(self.provider.apis[0].closed, 1)
        self.assertNotIn(package, sys.modules)

    def test_repeated_tick_errors_stop_module(self):
        code = ("class M:\n    def tick(self):\n        raise RuntimeError('tick broke')\n"
                "def create(c):\n    return M()\n")
        module_id, _ = self.write_module(code=code)
        host = self.loaded_host(module_id, policy=ModulePolicy(tick_failure_limit=2))
        host.load(module_id)
        host.tick()
        self.assertIs(host.record(module_id).status, ModuleStatus.LOADED)
        host.tick()
        record = host.record(module_id)
        self.assertIs(record.status, ModuleStatus.FAILED)
        self.assertIs(record.problems[0].code, ProblemCode.RUNTIME_ERROR)
        self.assertIn("tick broke", record.problems[0].message)
        self.assertEqual(self.provider.apis[0].closed, 1)

    def test_factory_may_return_none(self):
        module_id, _ = self.write_module(code="def create(c):\n    return None\n")
        host = self.loaded_host(module_id)
        self.assertIs(host.load(module_id).status, ModuleStatus.LOADED)
        host.tick()
        host.close()
        self.assertEqual(host.loaded(), ())

    def test_dependencies_load_first_and_unload_last(self):
        base_id, base_pkg = self.write_module(unique("base"))
        top_id, top_pkg = self.write_module(unique("top"), requires={"modules": [base_id]})
        host = self.enabled_host()
        host.scan()
        host.set_enabled(base_id, True)
        host.set_enabled(top_id, True)
        host.load(top_id)
        self.assertEqual(host.loaded(), (base_id, top_id))
        base_events = sys.modules[base_pkg].EVENTS
        top_events = sys.modules[top_pkg].EVENTS
        host.unload(base_id)
        self.assertEqual(host.loaded(), ())
        self.assertEqual(top_events[-1], ("close", top_id))
        self.assertEqual(base_events[-1], ("close", base_id))

    def test_dependency_must_be_enabled(self):
        base_id, _ = self.write_module(unique("base"))
        top_id, _ = self.write_module(unique("top"), requires={"modules": [base_id]})
        host = self.loaded_host(top_id)
        record = host.load(top_id)
        self.assertIs(record.status, ModuleStatus.MISSING_PREREQUISITE)
        self.assertIn("Turn it on first", record.problems[0].message)

    def test_dependency_cycle(self):
        a, b = unique("a"), unique("b")
        self.write_module(a, requires={"modules": [b]})
        self.write_module(b, requires={"modules": [a]})
        host = self.enabled_host()
        host.scan()
        host.set_enabled(a, True)
        host.set_enabled(b, True)
        host.load_enabled()
        self.assertEqual(host.loaded(), ())
        statuses = {host.record(a).status, host.record(b).status}
        self.assertEqual(statuses, {ModuleStatus.MISSING_PREREQUISITE})

    def test_disable_unloads(self):
        module_id, package = self.write_module()
        host = self.loaded_host(module_id)
        host.load(module_id)
        record = host.set_enabled(module_id, False)
        self.assertIs(record.status, ModuleStatus.DISABLED)
        self.assertNotIn(package, sys.modules)

    def test_reload_picks_up_new_code(self):
        module_id, package = self.write_module(code="VALUE = 1\ndef create(c):\n    return VALUE\n")
        host = self.loaded_host(module_id)
        host.load(module_id)
        self.assertEqual(host.instance(module_id), 1)
        (self.modules / module_id / package / "__init__.py").write_text("VALUE = 2\ndef create(c):\n    return VALUE\n")
        manifest_path = self.modules / module_id / "module.json"
        payload = json.loads(manifest_path.read_text())
        payload["version"] = "1.0.1"
        manifest_path.write_text(json.dumps(payload))
        host.scan()
        self.assertTrue(any("changed on disk" in w.message for w in host.record(module_id).warnings))
        self.assertEqual(host.record(module_id).manifest.version.numbers, (1, 0, 0))
        record = host.reload(module_id)
        self.assertIs(record.status, ModuleStatus.LOADED)
        self.assertEqual(host.instance(module_id), 2)
        self.assertEqual(record.manifest.version.numbers, (1, 0, 1))
        host.close()

    def test_removed_folder_keeps_running_until_unloaded(self):
        import shutil
        module_id, package = self.write_module()
        host = self.loaded_host(module_id)
        host.load(module_id)
        shutil.rmtree(self.modules / module_id)
        host.scan()
        record = host.record(module_id)
        self.assertIs(record.status, ModuleStatus.LOADED)
        self.assertTrue(any("removed" in w.message for w in record.warnings))
        host.unload(module_id)
        host.scan()
        self.assertIsNone(host.record(module_id))

    def test_close_is_idempotent(self):
        module_id, _ = self.write_module()
        host = self.loaded_host(module_id)
        host.load_enabled()
        host.close()
        host.close()
        self.assertEqual(self.provider.apis[0].closed, 1)


class ScopedApiTests(unittest.TestCase):
    def test_forwards_public_and_blocks_private(self):
        api = FakeAPI("x", ())
        scoped = ScopedAPI(api, frozenset())
        self.assertEqual(scoped.poll(), ())
        with self.assertRaises(AttributeError):
            scoped._secret
        with self.assertRaises(AttributeError):
            scoped.anything = 1
        scoped.close()
        scoped.close()
        self.assertEqual(api.closed, 1)
        self.assertTrue(scoped.closed)
        with self.assertRaises(RuntimeError):
            scoped.poll()


class BuiltinTests(Base):
    def manifest(self, groups=("radios",), **kwargs):
        return ModuleManifest.build(id=kwargs.pop("id", "builtin.radio"), name="Radio", version="1.0",
                                    api_version="0.1", entry="builtin:radio", control_groups=groups, **kwargs)

    def test_builtin_auto_grant_without_feature_flag(self):
        seen = []
        host = self.host()
        record = host.register_builtin(self.manifest(), lambda context: seen.append(context) or "radio")
        self.assertIs(record.status, ModuleStatus.READY)
        self.assertTrue(record.builtin)
        self.assertEqual(record.granted_groups, ("radios",))
        self.assertFalse(any(w.code is ProblemCode.NOT_GRANTED for w in record.warnings))
        host.load_enabled()
        self.assertEqual(host.instance("builtin.radio"), "radio")
        self.assertEqual(self.provider.apis[0].groups, ("radios",))
        self.assertTrue(seen[0].builtin)
        self.assertIsNone(seen[0].module_dir)
        host.close()
        self.assertEqual(self.provider.apis[0].closed, 1)

    def test_builtin_can_be_disabled(self):
        host = self.host()
        host.register_builtin(self.manifest(), lambda context: None)
        host.set_enabled("builtin.radio", False)
        host.load_enabled()
        self.assertEqual(host.loaded(), ())
        fresh = self.host()
        self.assertIs(fresh.register_builtin(self.manifest(), lambda context: None).status, ModuleStatus.DISABLED)

    def test_builtin_not_auto_granted_when_policy_off(self):
        host = self.host(policy=ModulePolicy(auto_grant_builtin=False))
        record = host.register_builtin(self.manifest(), lambda context: None)
        self.assertEqual(record.granted_groups, ())

    def test_dropin_cannot_take_builtin_id(self):
        self.write_module("builtin.radio")
        host = self.enabled_host()
        host.register_builtin(self.manifest(), lambda context: None)
        records = {r.key: r for r in host.scan()}
        dropin = next(r for r in records.values() if not r.builtin)
        self.assertIs(dropin.problems[0].code, ProblemCode.DUPLICATE_ID)
        self.assertIs(records["builtin.radio"].status, ModuleStatus.READY)

    def test_duplicate_builtin_and_bad_args(self):
        host = self.host()
        host.register_builtin(self.manifest(), lambda context: None)
        with self.assertRaises(ValueError):
            host.register_builtin(self.manifest(), lambda context: None)
        with self.assertRaises(TypeError):
            host.register_builtin({"id": "x"}, lambda context: None)


class CoreIntegrationTests(Base):
    """The real in-process Core with the simulated provider: one read and one write, no Core edits."""

    CODE = '''
from bbc_core.contract import CoreRequestError
class Module:
    def __init__(self, context):
        self.api = context.api
        self.sub = self.api.subscribe("radio.com1.active_frequency", interval_ms=1000)
        self.results = []
    def swap(self):
        try:
            self.results.append(self.api.submit_command("radio.com1.swap"))
        except CoreRequestError as error:
            self.results.append(str(error))
    def close(self):
        self.api.unsubscribe(self.sub.id)
def create(context):
    return Module(context)
'''

    def setUp(self):
        super().setUp()
        self.core = Core(SimulatedProvider())
        self.core.connect()
        self.addCleanup(self.core.disconnect)

    def run_module(self, grant):
        module_id, _ = self.write_module(code=self.CODE, control_groups=["radios"],
                                         requires={"capabilities": ["radio.com1.active_frequency",
                                                                    "radio.com1.swap"] if grant else
                                                   ["radio.com1.active_frequency"]})
        host = self.enabled_host(provider=core_api_provider(self.core), api_version=None)
        host.scan()
        host.set_enabled(module_id, True)
        if grant:
            host.grant(module_id, ["radios"])
        record = host.load(module_id)
        self.addCleanup(host.close)
        self.assertIs(record.status, ModuleStatus.LOADED, record.problems)
        module = host.instance(module_id)
        self.core.pump()
        module.swap()
        return host, module

    def test_granted_module_reads_and_writes(self):
        host, module = self.run_module(grant=True)
        self.assertIn(module.results[0].state, (CommandState.ACCEPTED, CommandState.SENT, CommandState.CONFIRMED))
        self.core.pump()
        self.assertIsNotNone(module.api.latest(module.sub.id))
        host.close()
        with self.assertRaises(RuntimeError):
            module.api.poll()

    def test_ungranted_module_is_rejected_by_core(self):
        host, module = self.run_module(grant=False)
        result = module.results[0]
        self.assertIs(result.state, CommandState.REJECTED)
        self.assertIn("control group", result.detail)
        host.close()

    def test_core_unknown_group_refusal_reported(self):
        """If the host offers a group Core does not know, Core's refusal reaches the pilot verbatim."""
        from bbc_efb.modules import ControlGroup
        module_id, _ = self.write_module(control_groups=["made_up"])
        host = self.enabled_host(provider=core_api_provider(self.core), api_version=None,
                                 control_groups={"made_up": ControlGroup("made_up", "Not a Core group.")})
        host.scan()
        host.set_enabled(module_id, True)
        host.grant(module_id, ["made_up"])
        record = host.load(module_id)
        self.assertIs(record.problems[0].code, ProblemCode.CORE_REFUSED)
        self.assertIn("Unknown control group", record.problems[0].message)


if __name__ == "__main__":
    unittest.main()
