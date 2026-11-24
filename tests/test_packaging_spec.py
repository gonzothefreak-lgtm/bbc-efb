"""Offline checks for packaging/bbc-efb.spec.

PyInstaller isn't installed in the development environment and only builds on
Windows, so the spec is executed here against stand-ins for PyInstaller's
Analysis, PYZ, EXE and COLLECT. The stand-ins record their arguments, which lets
the tests check what the spec would hand to PyInstaller.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "bbc-efb.spec"


def load_tools():
    name = "bbc_package_tools_spec_tests"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SPEC.parent / "package_tools.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class Recorder:
    """Stand-ins for the PyInstaller spec globals."""

    def __init__(self, extra_pure=(), extra_binaries=(), extra_datas=()):
        self.calls = {}
        self.extra_pure = list(extra_pure)
        self.extra_binaries = list(extra_binaries)
        self.extra_datas = list(extra_datas)

    def analysis(self, scripts, **kwargs):
        self.calls["Analysis"] = (scripts, kwargs)
        pure = [(name, f"/fake/{name.replace('.', '/')}.py", "PYMODULE") for name in kwargs["hiddenimports"]]
        pure += [("json", "/py/json/__init__.py", "PYMODULE")] + self.extra_pure
        datas = [(dest + "/" + Path(src).name, src, "DATA") for src, dest in kwargs["datas"]] + self.extra_datas

        class Result:
            pass

        result = Result()
        result.pure, result.binaries, result.datas = pure, list(self.extra_binaries), datas
        result.scripts = [("launch_bbc_efb", scripts[0], "PYSOURCE")]
        return result

    def record(self, name):
        def call(*args, **kwargs):
            self.calls[name] = (args, kwargs)
            return name
        return call

    def namespace(self):
        return {"SPECPATH": str(SPEC.parent), "Analysis": self.analysis, "PYZ": self.record("PYZ"),
                "EXE": self.record("EXE"), "COLLECT": self.record("COLLECT"), "__name__": "__main__",
                "__file__": str(SPEC)}


def entry_point_ready():
    main = ROOT / "bbc_efb" / "__main__.py"
    if not main.is_file():
        return False
    tree = ast.parse(main.read_text(encoding="utf-8"))
    return "main" in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def run_spec(recorder: Recorder, report_dir: Path, **env):
    environment = {"BBC_EFB_ALLOW_NON_WINDOWS": "1", "BBC_EFB_REPORT_DIR": str(report_dir),
                   "BBC_EFB_EXCLUDE_SIMULATED": ""}
    environment.update(env)
    code = compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec")
    with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
        exec(code, recorder.namespace())


class SpecSourceTests(unittest.TestCase):
    def test_spec_parses_as_python_3_10(self):
        ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC), feature_version=(3, 10))

    def test_spec_is_ascii_and_never_names_a_dll_to_bundle(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        tree = ast.parse(text)
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertFalse([s for s in strings if s.casefold().endswith(".dll")])

    def test_paths_named_by_the_spec_exist(self):
        text = SPEC.read_text(encoding="utf-8")
        tree = ast.parse(text)
        names = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        for required in ("package_tools.py", "launch_bbc_efb.py", "version_info.txt"):
            self.assertIn(required, names)
            self.assertTrue((SPEC.parent / required).is_file(), required)

    def test_launcher_calls_the_declared_entry_point(self):
        launcher = (SPEC.parent / "launch_bbc_efb.py").read_text(encoding="utf-8")
        ast.parse(launcher, feature_version=(3, 10))
        self.assertIn("from bbc_efb.__main__ import main", launcher)
        self.assertIn("freeze_support()", launcher)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('bbc-efb = "bbc_efb.__main__:main"', pyproject)


class SpecExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report = Path(self.tmp.name) / "report"

    def test_entry_point_is_integrated(self):
        self.assertTrue(entry_point_ready(), "bbc_efb/__main__.py must define main()")

    def test_spec_hands_pyinstaller_a_clean_onedir_windowed_build(self):
        recorder = Recorder()
        run_spec(recorder, self.report)
        scripts, analysis = recorder.calls["Analysis"]
        self.assertEqual(scripts, [str(SPEC.parent / "launch_bbc_efb.py")])
        self.assertEqual(analysis["pathex"], [str(ROOT)])
        self.assertEqual(analysis["binaries"], [])
        hidden = analysis["hiddenimports"]
        for module in ("bbc_efb", "bbc_efb.__main__", "bbc_core.core", "bbc_core.simulated"):
            self.assertIn(module, hidden)
        for module in ("bbc_recorder", "bbc_recorder.service", "bbc_core.ipc", "bbc_core.host",
                       "bbc_efb.logbook_views", "bbc_efb.modules"):
            self.assertIn(module, hidden)
        self.assertEqual(analysis["datas"], [], "no package data is read at run time today")
        self.assertFalse([m for m in hidden if m.split(".")[0] in ("tests", "tests_wip") or ".test" in m])
        for excluded in ("tests", "tests_wip", "tkinter", "PyInstaller", "PySide6.QtWebEngineCore"):
            self.assertIn(excluded, analysis["excludes"])
        for kept in ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
            self.assertNotIn(kept, analysis["excludes"])
        self.assertNotIn("bbc_core.simulated", analysis["excludes"])

        exe_args, exe = recorder.calls["EXE"]
        self.assertEqual(exe_args[0], "PYZ")
        self.assertTrue(exe["exclude_binaries"], "onedir builds keep binaries out of the exe")
        self.assertFalse(exe["console"])
        self.assertFalse(exe["upx"])
        self.assertFalse(exe["debug"])
        self.assertEqual(exe["name"], "BBC-EFB")
        self.assertEqual(Path(exe["version"]), SPEC.parent / "version_info.txt")
        icon = SPEC.parent / "bbc-efb.ico"
        self.assertEqual(exe["icon"], str(icon) if icon.is_file() else None)
        _, collect = recorder.calls["COLLECT"]
        self.assertEqual(collect["name"], "BBC-EFB")
        self.assertFalse(collect["upx"])

        report = (self.report / "BUNDLED_MODULES.txt").read_text(encoding="utf-8")
        self.assertIn("bbc_core.core", report)
        self.assertIn("SimConnect.dll: not bundled", report)

    def test_spec_refuses_test_code_and_simconnect_dll(self):
        cases = {
            "test module": dict(extra_pure=[("tests.test_core_offline", "/r/tests/test_core_offline.py", "PYMODULE")]),
            "wip test": dict(extra_pure=[("tests_wip.test_x", "/r/tests_wip/test_x.py", "PYMODULE")]),
            "dll": dict(extra_binaries=[("SimConnect.dll", r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll", "BINARY")]),
            "log data": dict(extra_datas=[("logs/bbc-efb.log", "/r/logs/bbc-efb.log", "DATA")]),
            "probe data": dict(extra_datas=[("core_live_readonly_x/raw.txt", "/r/raw.txt", "DATA")]),
        }
        for label, extras in cases.items():
            with self.subTest(label):
                recorder = Recorder(**extras)
                with self.assertRaises(SystemExit) as caught:
                    run_spec(recorder, self.report)
                self.assertIn("must not ship", str(caught.exception))
                self.assertNotIn("EXE", recorder.calls)

    def test_exclude_simulated_is_refused_while_production_code_imports_it(self):
        tools = load_tools()
        importers = tools.simulated_importers(ROOT, tools.discover_app_packages(ROOT))
        recorder = Recorder()
        if importers:
            with self.assertRaises(SystemExit) as caught:
                run_spec(recorder, self.report, BBC_EFB_EXCLUDE_SIMULATED="1")
            self.assertIn("simulated provider", str(caught.exception))
            self.assertNotIn("Analysis", recorder.calls)
        else:
            run_spec(recorder, self.report, BBC_EFB_EXCLUDE_SIMULATED="1")
            _, analysis = recorder.calls["Analysis"]
            self.assertIn("bbc_core.simulated", analysis["excludes"])
            self.assertNotIn("bbc_core.simulated", analysis["hiddenimports"])

    @unittest.skipIf(sys.platform == "win32", "the platform refusal only applies off Windows")
    def test_spec_refuses_to_build_off_windows_by_default(self):
        recorder = Recorder()
        with self.assertRaises(SystemExit) as caught:
            run_spec(recorder, self.report, BBC_EFB_ALLOW_NON_WINDOWS="")
        self.assertIn("Windows", str(caught.exception))
        self.assertEqual(recorder.calls, {})


class SpecGuardTests(unittest.TestCase):
    """Run the spec from a copied packaging folder inside a small synthetic tree."""

    def make_tree(self, root: Path, *, version="1.0.0", info_version="1.0.0", main=True, core=True,
                  recorder=True, namespace=False):
        packaging = root / "packaging"
        packaging.mkdir(parents=True)
        for name in ("bbc-efb.spec", "package_tools.py", "launch_bbc_efb.py"):
            shutil.copy2(SPEC.parent / name, packaging / name)
        info = (SPEC.parent / "version_info.txt").read_text(encoding="utf-8")
        real = load_tools().read_app_version(ROOT)
        numbers = ", ".join(str(n) for n in load_tools().windows_version_tuple(info_version))
        real_numbers = ", ".join(str(n) for n in load_tools().windows_version_tuple(real))
        info = info.replace(real, info_version).replace(real_numbers, numbers)
        (packaging / "version_info.txt").write_text(info, encoding="utf-8")
        (root / "bbc_efb").mkdir()
        (root / "bbc_efb" / "__init__.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")
        if main:
            (root / "bbc_efb" / "__main__.py").write_text(
                "from PySide6.QtWidgets import QApplication\n\ndef main(argv=None):\n    return 0\n", encoding="utf-8")
        if core:
            (root / "bbc_core").mkdir()
            (root / "bbc_core" / "__init__.py").write_text("", encoding="utf-8")
            (root / "bbc_core" / "simulated.py").write_text("", encoding="utf-8")
        if recorder:
            (root / "bbc_recorder").mkdir()
            (root / "bbc_recorder" / "__init__.py").write_text("", encoding="utf-8")
        if namespace:
            (root / "bbc_efb" / "views").mkdir()
            (root / "bbc_efb" / "views" / "model.py").write_text("", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("", encoding="utf-8")
        return packaging / "bbc-efb.spec"

    def run_copied_spec(self, spec_path: Path, recorder: Recorder, **env):
        namespace = recorder.namespace()
        namespace["SPECPATH"] = str(spec_path.parent)
        environment = {"BBC_EFB_ALLOW_NON_WINDOWS": "1", "BBC_EFB_EXCLUDE_SIMULATED": "",
                       "BBC_EFB_REPORT_DIR": str(spec_path.parent.parent / "build" / "report")}
        environment.update(env)
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(spec_path.read_text(encoding="utf-8"), str(spec_path), "exec"), namespace)

    def test_synthetic_tree_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = self.make_tree(Path(tmp))
            recorder = Recorder()
            self.run_copied_spec(spec, recorder)
            _, analysis = recorder.calls["Analysis"]
            self.assertEqual(sorted(analysis["hiddenimports"]),
                             ["bbc_core", "bbc_core.simulated", "bbc_efb", "bbc_efb.__main__", "bbc_recorder"])
            self.assertIn("COLLECT", recorder.calls)
            self.assertTrue((Path(tmp) / "build" / "report" / "BUNDLED_MODULES.txt").is_file())

    def test_synthetic_tree_can_exclude_the_simulated_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = self.make_tree(Path(tmp))
            recorder = Recorder()
            self.run_copied_spec(spec, recorder, BBC_EFB_EXCLUDE_SIMULATED="1")
            _, analysis = recorder.calls["Analysis"]
            self.assertNotIn("bbc_core.simulated", analysis["hiddenimports"])
            self.assertIn("bbc_core.simulated", analysis["excludes"])

    def test_version_mismatch_and_missing_pieces_stop_before_analysis(self):
        cases = {
            "version": (dict(version="1.0.1"), "version_info.txt"),
            "entry point": (dict(main=False), "__main__.py is missing"),
            "core": (dict(core=False), "bbc_core package is missing"),
            "recorder": (dict(recorder=False), "bbc_recorder package is missing"),
            "namespace package": (dict(namespace=True), "bbc_efb/views has code but no __init__.py"),
        }
        for label, (options, expected) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                spec = self.make_tree(Path(tmp), **options)
                recorder = Recorder()
                with self.assertRaises(SystemExit) as caught:
                    self.run_copied_spec(spec, recorder)
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("Analysis", recorder.calls)


if __name__ == "__main__":
    unittest.main()
