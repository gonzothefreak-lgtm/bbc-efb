"""Static checks that need no third-party tool (findings A89 and A139).

ruff and mypy run in the advisory CI job; these checks give the same guarantees for the
parts that matter most and run everywhere the test suite runs:

* no unused imports in bbc_core, bbc_recorder or bbc_efb (ruff F401),
* every bbc_core data type's annotations resolve to real types under postponed
  annotations (the IPC layer and its tests rely on ``typing.get_type_hints``),
* the SimConnect provider uses its SDK only through the ``_Sdk`` and ``_OptionalSdk``
  protocols,
* every offline test that builds a ``SimConnectProvider`` runs on the shared fake clock.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
from pathlib import Path
import pkgutil
import types
import typing
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]

# Unused imports accepted in bbc_recorder and bbc_efb, as (path, name) entries. The list
# is empty: every package is held to zero. Should an entry ever be needed, the test also
# fails once it has been fixed, so the list can only shrink.
KNOWN_UNUSED: frozenset = frozenset()


def _annotation_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                             arguments.vararg, arguments.kwarg):
                if argument is not None and argument.annotation is not None:
                    yield argument.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def unused_imports(source, filename="<source>"):
    """Return sorted (name, line) pairs for imported names the module never uses.

    A name counts as used when it is read anywhere (including inside annotations and
    quoted annotations) or listed in ``__all__``. ``__future__`` imports, star imports and
    lines marked ``noqa`` are ignored.
    """
    tree = ast.parse(source, filename)
    lines = source.splitlines()
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.setdefault(alias.asname or alias.name.split(".")[0], node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for alias in node.names:
                if alias.name != "*":
                    imported.setdefault(alias.asname or alias.name, node.lineno)
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for annotation in _annotation_nodes(tree):
        for node in ast.walk(annotation):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                try:
                    quoted = ast.parse(node.value, mode="eval")
                except SyntaxError:
                    continue
                used.update(name.id for name in ast.walk(quoted) if isinstance(name, ast.Name))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            try:
                used.update(ast.literal_eval(node.value))
            except ValueError:
                pass
    return sorted((name, line) for name, line in imported.items()
                  if name not in used and "noqa" not in lines[line - 1])


def _package_findings(package):
    findings = set()
    for path in sorted((ROOT / package).rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        for name, _ in unused_imports(path.read_text(encoding="utf-8"), relative):
            findings.add((relative, name))
    return findings


class UnusedImportCheckerTests(TestCase):
    def test_the_checker_finds_unused_names_and_respects_real_uses(self):
        source = (
            "from __future__ import annotations\n"
            "import os\n"
            "import os.path as osp\n"
            "import json\n"
            "import xml.dom\n"
            "from typing import Any, Mapping, Sequence\n"
            "from collections import deque  # noqa: F401 - re-exported\n"
            "from enum import Enum\n"
            "__all__ = ['Enum']\n"
            "def f(value: Mapping[str, 'Sequence[int]']) -> None:\n"
            "    return os.getcwd(), xml.dom\n"
        )
        self.assertEqual(unused_imports(source), [("Any", 6), ("json", 4), ("osp", 3)])

    def test_bbc_core_has_no_unused_imports(self):
        self.assertEqual(_package_findings("bbc_core"), set())

    def test_recorder_and_efb_unused_imports_match_the_known_list(self):
        found = _package_findings("bbc_recorder") | _package_findings("bbc_efb")
        self.assertEqual(found - KNOWN_UNUSED, set(), "new unused imports")
        self.assertEqual(KNOWN_UNUSED - found, set(), "fixed: remove these from KNOWN_UNUSED")


def _bbc_core_dataclasses():
    import bbc_core
    for info in pkgutil.walk_packages(bbc_core.__path__, "bbc_core."):
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if isinstance(value, type) and value.__module__ == module.__name__ and dataclasses.is_dataclass(value):
                yield f"{info.name}.{name}", value


def _hint_problem(hint):
    """Why a resolved hint is not a type, or None when it is one."""
    if isinstance(hint, (types.MemberDescriptorType, types.GetSetDescriptorType, property, dataclasses.Field)):
        return f"resolved to a class attribute ({hint!r}); a field name shadows a type name"
    if typing.get_origin(hint) is not None:
        for argument in typing.get_args(hint):
            if isinstance(argument, (list, str, int)) or argument is Ellipsis:
                continue  # Callable parameter lists, Literal values
            problem = _hint_problem(argument)
            if problem:
                return problem
        return None
    if hint is None or isinstance(hint, (type, typing.TypeVar)) or hint is typing.Any:
        return None
    if callable(hint):
        return None  # Factory types such as threading.Lock
    return f"resolved to a value, not a type ({hint!r})"


class AnnotationResolutionTests(TestCase):
    def test_every_bbc_core_data_type_has_resolvable_type_hints(self):
        # A89: with "from __future__ import annotations" everywhere, an annotation is only
        # checked when something resolves it. The IPC every-type round trip does, so a name
        # that resolves wrongly (ObjectSample's "object" field once shadowed the builtin)
        # must fail here, not at run time in another process.
        checked = 0
        for name, cls in _bbc_core_dataclasses():
            with self.subTest(type=name):
                hints = typing.get_type_hints(cls)
                for field in dataclasses.fields(cls):
                    self.assertIn(field.name, hints)
                    self.assertIsNone(_hint_problem(hints[field.name]), f"{name}.{field.name}")
                checked += 1
        self.assertGreater(checked, 50)

    def test_the_resolution_check_catches_a_shadowed_name(self):
        import sys
        module = types.ModuleType("bbc_static_shadow_probe")
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__)
        exec(
            "from __future__ import annotations\n"
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class Sample:\n"
            "    object: int\n"
            "    value: object\n",
            module.__dict__,
        )
        hints = typing.get_type_hints(module.Sample)
        self.assertIsNotNone(_hint_problem(hints["value"]))
        self.assertIsNone(_hint_problem(int | None))


def _provider_sdk_uses():
    """Attribute names simconnect.py reads from its SDK directly and through getattr."""
    tree = ast.parse((ROOT / "bbc_core" / "simconnect.py").read_text(encoding="utf-8"))

    def is_sdk(node):
        return ((isinstance(node, ast.Attribute) and node.attr == "_sdk"
                 and isinstance(node.value, ast.Name) and node.value.id == "self")
                or (isinstance(node, ast.Name) and node.id == "sdk"))

    direct, optional = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and is_sdk(node.value):
            direct.add(node.attr)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
              and node.args and is_sdk(node.args[0])):
            name = node.args[1]
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                raise AssertionError(f"getattr on the SDK with a computed name at line {node.lineno}")
            optional.add(name.value)
    return direct, optional


def _protocol_members(protocol):
    return {name for name in vars(protocol) if not name.startswith("_")} | set(
        getattr(protocol, "__annotations__", {}))


class SdkProtocolTests(TestCase):
    def test_the_provider_uses_its_sdk_only_through_the_protocols(self):
        from bbc_core.simconnect import _OptionalSdk, _Sdk
        required, optional = _protocol_members(_Sdk), _protocol_members(_OptionalSdk)
        direct, looked_up = _provider_sdk_uses()
        self.assertTrue(direct)
        self.assertEqual(direct - required, set(), "direct SDK use missing from _Sdk")
        self.assertEqual(looked_up - optional, set(), "getattr SDK use missing from _OptionalSdk")
        self.assertEqual(required & optional, set(), "a member cannot be both required and optional")
        self.assertEqual(optional - looked_up, set(), "_OptionalSdk lists a member the provider never uses")

    def test_the_real_sdk_and_the_test_sdk_implement_the_required_protocol(self):
        from bbc_core.simconnect import _Sdk
        from bbc_core.simconnect_sdk import SimConnectSdk
        from test_simconnect_provider_offline import FakeSdk
        fake = FakeSdk()
        for sdk in (SimConnectSdk, fake):
            for name in _protocol_members(_Sdk):
                with self.subTest(sdk=type(sdk).__name__, member=name):
                    self.assertTrue(hasattr(sdk, name))


def _calls(function):
    """Which provider-related calls a test method makes: build, run and start_clock."""
    found = set()
    for call in ast.walk(function):
        if not isinstance(call, ast.Call):
            continue
        if isinstance(call.func, ast.Name) and call.func.id == "SimConnectProvider":
            found.add("build")
        elif isinstance(call.func, ast.Attribute) and call.func.attr in ("connect", "pump"):
            found.add("run")
        elif isinstance(call.func, ast.Attribute) and call.func.attr == "start_clock":
            found.add("start_clock")
    return found


class SharedClockTests(TestCase):
    """A139: a frozen clock with real sleep can spin the connect or baseline loops forever."""

    PATTERNS = ("test_core_*.py", "test_adapter_*.py", "test_simconnect_*.py", "test_facilit*.py",
                "test_simulator_*.py", "test_client_*.py", "test_sdk_simvar_offline.py",
                "test_aircraft_inputs_offline.py", "test_integration_*.py", "test_ipc_*.py")

    def test_every_class_that_runs_a_simconnect_provider_starts_the_shared_clock(self):
        files = sorted({path for pattern in self.PATTERNS for path in (ROOT / "tests").glob(pattern)})
        missing, classes = [], 0
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                # Building a provider without connecting it never reads the clock, so a class
                # needs the clock when one method builds and runs a provider, or when setUp
                # builds one and any method runs it.
                methods = {item.name: _calls(item) for item in node.body
                           if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
                set_up = methods.get("setUp", set())
                runs_any = any("run" in found for found in methods.values())
                builds = (any({"build", "run"} <= found for found in methods.values())
                          or ("build" in set_up and runs_any))
                starts = any("start_clock" in found for found in methods.values())
                classes += builds
                if builds and not starts:
                    missing.append(f"{path.name}:{node.name}")
        self.assertGreater(classes, 20)
        self.assertEqual(missing, [])

    def test_the_shared_clock_patches_sleep_as_well_as_monotonic(self):
        from bbc_core import simconnect
        from test_adapter_support import FakeClock

        class Probe(FakeClock, TestCase):
            def runTest(self):
                pass

        probe = Probe()
        probe.start_clock(50.0)
        try:
            simconnect.sleep(2.5)
            self.assertEqual(simconnect.monotonic(), 52.5)
        finally:
            probe.doCleanups()
