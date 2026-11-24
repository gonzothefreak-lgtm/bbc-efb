"""Offline checks for packaging/package_tools.py (the shared packaging rules)."""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOLS_PATH = ROOT / "packaging" / "package_tools.py"


def load_tools():
    name = "bbc_package_tools_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tools = load_tools()

# Set by tests/test_repo_offline_guard.py while its audit hook blocks subprocesses.
GUARDED = os.environ.get("BBC_OFFLINE_GUARD") == "1"

PASSING_OUTPUT = """CPython 3.12.4 64-bit on win32
test_a (tests.test_x.A.test_a) ... ok
----------------------------------------------------------------------
Ran 412 tests in 31.250s

OK (skipped=3)
Offline guard violations: 0
"""


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run_cli(*args) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tools.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class VersionTests(unittest.TestCase):
    def test_reads_the_real_application_version_without_importing_it(self):
        version = tools.read_app_version(ROOT)
        self.assertRegex(version, r"^\d+\.\d+\.\d+")

    def test_windows_version_tuple(self):
        cases = {
            "0.1.0-concept-a.1": (0, 1, 0, 1),
            "1.2.3": (1, 2, 3, 0),
            "1.2.3rc4": (1, 2, 3, 4),
            "2.0.10-beta.12": (2, 0, 10, 12),
            "0.1.0a1": (0, 1, 0, 1),
        }
        for version, expected in cases.items():
            with self.subTest(version=version):
                self.assertEqual(tools.windows_version_tuple(version), expected)
        for bad in ("1.2", "v1.2.3", "1.2.3.4.x y", "70000.0.0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tools.windows_version_tuple(bad)

    def test_safe_version_label(self):
        self.assertEqual(tools.safe_version_label("0.1.0-concept-a.1"), "0.1.0-concept-a.1")
        self.assertEqual(tools.safe_version_label("1.0 beta/2"), "1.0-beta-2")
        for bad in ("", "..", ".hidden"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tools.safe_version_label(bad)

    def test_shipped_version_info_matches_bbc_efb_version(self):
        version = tools.read_app_version(ROOT)
        self.assertEqual(tools.check_version_info(ROOT / "packaging" / "version_info.txt", version), [])

    def test_version_info_mismatch_is_reported(self):
        problems = tools.check_version_info(ROOT / "packaging" / "version_info.txt", "9.8.7-x.6")
        joined = "\n".join(problems)
        for key in ("filevers", "prodvers", "FileVersion", "ProductVersion"):
            self.assertIn(key, joined)

    def test_unreadable_version_info_is_a_problem_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp) / "version_info.txt", "VSVersionInfo(")
            self.assertTrue(tools.check_version_info(path, "1.0.0"))
            self.assertTrue(tools.check_version_info(Path(tmp) / "missing.txt", "1.0.0"))

    def test_version_info_is_a_pyinstaller_expression(self):
        import ast
        text = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
        tree = ast.parse(text, mode="eval")
        self.assertEqual(tree.body.func.id, "VSVersionInfo")
        self.assertTrue(text.isascii())


class EntryPointAndChangelogTests(unittest.TestCase):
    def test_entry_point_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIn("missing", tools.check_entry_point(root)[0])
            write(root / "bbc_efb" / "__main__.py", "def other():\n    pass\n")
            self.assertIn("no top-level main()", tools.check_entry_point(root)[0])
            write(root / "bbc_efb" / "__main__.py", "def main(:\n")
            self.assertIn("does not parse", tools.check_entry_point(root)[0])
            write(root / "bbc_efb" / "__main__.py", "def main(argv=None):\n    return 0\n")
            self.assertEqual(tools.check_entry_point(root), [])

    def test_real_entry_point(self):
        self.assertTrue((ROOT / "bbc_efb" / "__main__.py").is_file())
        self.assertEqual(tools.check_entry_point(ROOT), [])

    def test_changelog_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CHANGELOG.md"
            self.assertIn("CHANGELOG_TEMPLATE.md", tools.check_changelog(path, "1.0.0")[0])
            write(path, "## 1.0.0\n- TEMPLATE-PLACEHOLDER\n")
            self.assertIn("TEMPLATE-PLACEHOLDER", tools.check_changelog(path, "1.0.0")[0])
            write(path, "## 0.9.0\n- Older entry\n")
            self.assertIn("no entry for version 1.0.0", tools.check_changelog(path, "1.0.0")[0])
            write(path, "## 1.0.0 - 2026-11-24\n- Real entry\n")
            self.assertEqual(tools.check_changelog(path, "1.0.0"), [])

    def test_the_template_itself_is_refused(self):
        version = tools.read_app_version(ROOT)
        problems = tools.check_changelog(ROOT / "packaging" / "CHANGELOG_TEMPLATE.md", version)
        self.assertTrue(any("TEMPLATE-PLACEHOLDER" in p for p in problems))


class SourceDiscoveryTests(unittest.TestCase):
    def make_tree(self, root: Path):
        write(root / "bbc_core" / "__init__.py")
        write(root / "bbc_core" / "core.py", "import json\n")
        write(root / "bbc_core" / "simulated.py", "class SimulatedProvider: pass\n")
        write(root / "bbc_core" / "ipc" / "__init__.py")
        write(root / "bbc_core" / "ipc" / "server.py")
        write(root / "bbc_core" / "tests" / "test_core.py")
        write(root / "bbc_core" / "__pycache__" / "core.cpython-312.py")
        write(root / "bbc_efb" / "__init__.py", "VERSION = '1.0.0'\n")
        write(root / "bbc_efb" / "__main__.py", "def main():\n    return 0\n")
        write(root / "bbc_efb" / "app.py",
              "from PySide6.QtWidgets import QApplication\nfrom PySide6 import QtCharts\nimport unittest\n")
        write(root / "bbc_efb" / "views" / "chart.py")  # namespace package
        write(root / "bbc_efb" / "assets" / "icon.svg", "<svg/>")
        write(root / "bbc_efb" / "assets" / "fonts" / "Inter.ttf", "font")
        write(root / "bbc_efb" / "assets" / "debug.log", "noise")
        write(root / "bbc_efb" / "fixtures" / "flight.json", "{}")
        write(root / "bbc_recorder" / "model.py")
        write(root / "bbc_recorder" / "test_rules.py")
        write(root / "bbc_notes" / "README.md")  # no Python: not a package
        write(root / "tests" / "test_x.py")
        write(root / "other" / "x.py")
        return root

    def test_packages_modules_and_namespace_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_tree(Path(tmp))
            packages = tools.discover_app_packages(root)
            self.assertEqual(packages, ["bbc_core", "bbc_efb", "bbc_recorder"])
            modules = tools.production_modules(root, packages)
            self.assertEqual(modules, [
                "bbc_core", "bbc_core.core", "bbc_core.ipc", "bbc_core.ipc.server", "bbc_core.simulated",
                "bbc_efb", "bbc_efb.__main__", "bbc_efb.app", "bbc_efb.views.chart", "bbc_recorder.model",
            ])
            self.assertFalse(any(tools.is_test_module(m) for m in modules))
            without = tools.production_modules(root, packages, exclude_simulated=True)
            self.assertNotIn("bbc_core.simulated", without)
            self.assertEqual(tools.missing_package_inits(root, packages), ["bbc_efb/views", "bbc_recorder"])

    def test_imports_drive_qt_and_base_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_tree(Path(tmp))
            imports = tools.imported_modules(tools.iter_production_files(root, tools.discover_app_packages(root)))
            self.assertEqual(tools.used_qt_modules(imports), {"QtWidgets", "QtCharts"})
            excludes = tools.build_excludes(imports)
            self.assertNotIn("PySide6.QtCharts", excludes)
            self.assertIn("PySide6.QtWebEngineCore", excludes)
            self.assertIn("tests", excludes)
            self.assertIn("tests_wip", excludes)
            self.assertNotIn("bbc_core.simulated", excludes)
            self.assertIn("bbc_core.simulated", tools.build_excludes(imports, exclude_simulated=True))

    def test_base_exclude_is_dropped_when_production_code_imports_it(self):
        excludes = tools.build_excludes({"tkinter.ttk", "PySide6.QtCore"})
        self.assertNotIn("tkinter", excludes)
        self.assertIn("turtle", excludes)

    def test_qt_dependencies_are_kept_transitively(self):
        excludes = tools.qt_excludes({"QtWebEngineWidgets"})
        for kept in ("QtWebEngineWidgets", "QtWebEngineCore", "QtWebChannel", "QtQuick", "QtQml",
                     "QtPositioning", "QtQuickWidgets"):
            self.assertNotIn(f"PySide6.{kept}", excludes)
        self.assertIn("PySide6.Qt3DCore", excludes)
        for core in ("QtCore", "QtGui", "QtWidgets", "QtSvg", "QtNetwork"):
            self.assertNotIn(f"PySide6.{core}", tools.qt_excludes(set()))

    def test_simulated_importers_find_absolute_and_relative_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_tree(Path(tmp))
            packages = tools.discover_app_packages(root)
            self.assertEqual(tools.simulated_importers(root, packages), [])
            write(root / "bbc_efb" / "app.py", "def f():\n    from bbc_core.simulated import SimulatedProvider\n")
            write(root / "bbc_core" / "host.py", "from .simulated import SimulatedProvider\n")
            write(root / "bbc_core" / "other.py", "from . import simulated\n")
            self.assertEqual(sorted(tools.simulated_importers(root, packages)),
                             ["bbc_core/host.py", "bbc_core/other.py", "bbc_efb/app.py"])

    def test_package_data_comes_only_from_asset_folders_and_skips_forbidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_tree(Path(tmp))
            datas = tools.package_data_files(root, tools.discover_app_packages(root))
            self.assertEqual(sorted(dest for _, dest in datas), ["bbc_efb/assets", "bbc_efb/assets/fonts"])
            self.assertFalse(any(src.endswith(".log") for src, _ in datas))

    def test_real_tree_has_core_efb_and_recorder_and_no_test_modules(self):
        packages = tools.discover_app_packages(ROOT)
        for required in tools.REQUIRED_PACKAGES:
            self.assertIn(required, packages)
        self.assertEqual(tools.missing_package_inits(ROOT, packages), [])
        names = tools.app_package_names(ROOT, packages)
        for package in ("bbc_core", "bbc_core.ipc", "bbc_efb", "bbc_efb.logbook_views", "bbc_efb.modules",
                        "bbc_recorder"):
            self.assertIn(package, names)
        modules = tools.production_modules(ROOT, packages)
        self.assertIn("bbc_core.core", modules)
        self.assertIn("bbc_efb.storage", modules)
        self.assertFalse([m for m in modules if tools.is_test_module(m)])
        imports = tools.imported_modules(tools.iter_production_files(ROOT, packages))
        self.assertTrue({"QtCore", "QtWidgets"} <= tools.used_qt_modules(imports))


class ContentRuleTests(unittest.TestCase):
    PORTABLE_OK = (
        "BBC-EFB/BBC-EFB.exe", "BBC-EFB/_internal/base_library.zip", "BBC-EFB/_internal/python312.dll",
        "BBC-EFB/_internal/PySide6/Qt6Widgets.dll", "BBC-EFB/_internal/PySide6/plugins/platforms/qwindows.dll",
        "BBC-EFB/_internal/PySide6/Qt6Test.dll", "README.md", "TEST_STATUS.txt", "TEST_OUTPUT.txt",
        "BBC-EFB/_internal/bbc_efb/assets/icon.svg",
    )
    PORTABLE_BAD = {
        "tests/test_core_offline.py": "test",
        "BBC-EFB/_internal/tests_wip/test_x.py": "work-in-progress",
        "BBC-EFB/_internal/bbc_core/__pycache__/core.cpython-312.pyc": "bytecode",
        "BBC-EFB/_internal/bbc_core/core.pyc": "bytecode",
        "BBC-EFB/logs/bbc-efb.log": "log",
        "BBC-EFB/bbc-efb.log.3": "log",
        "BBC-EFB/fault.log": "log",
        "reference/x/unittest.txt": "test runner output",
        "BBC-EFB/SimConnect.dll": "SimConnect.dll",
        "BBC-EFB/_internal/simconnect.DLL": "SimConnect.dll",
        "BBC-EFB/core_live_readonly_2026-11-24/raw.txt": "probe",
        "BBC-EFB/_internal/sdk_preflight.json": "probe",
        "BBC-EFB/in-progress/abc.jsonl": "folder",
        "BBC-EFB/_internal/flight_tape.jsonl": "journal",
        "BBC-EFB/_internal/fixtures/flight.json": "fixture",
        "BBC-EFB/_internal/test_artifacts/REPORT.md": "evidence",
        "BBC-EFB/_internal/extra.zip": "nested archive",
        "BBC-EFB/_internal/wheel.whl": "nested archive",
        "BBC-EFB/.env": "credential",
        "BBC-EFB/_internal/cert.pfx": "credential",
        "BBC-EFB/_internal/bbc_core/core.py": "loose application source",
        "BBC-EFB/.venv/pyvenv.cfg": "development environment",
        "/abs/path": "safe relative",
        "../escape.txt": "safe relative",
        "C:/Windows/x.dll": "safe relative",
        "BBC-EFB\\BBC-EFB.exe": "backslash",
    }

    def test_portable_rules(self):
        for path in self.PORTABLE_OK:
            with self.subTest(path=path):
                self.assertIsNone(tools.portable_problem(path))
        for path, word in self.PORTABLE_BAD.items():
            with self.subTest(path=path):
                problem = tools.portable_problem(path)
                self.assertIsNotNone(problem)
                self.assertIn(word.split()[0].lower(), problem.lower())

    def test_source_rules_keep_tests_and_evidence_but_not_junk(self):
        for path in ("tests/test_core_offline.py", "reference/01_CODEX_WORK_EVIDENCE/test_artifacts/x/REPORT.md",
                     "sources/BBC_EFB_UI_Design_Standards.docx", "packaging/bbc-efb.spec", ".github/workflows/ci.yml",
                     "reference/04_REFERENCE_ASSETS/Flight History.png", ".gitignore"):
            with self.subTest(path=path):
                self.assertIsNone(tools.source_problem(path))
        for path in ("tests_wip/test_packaging_tools.py", "bbc-efb-source-53d2091.zip", "build/x.txt", "dist/a.zip",
                     "bbc_core/__pycache__/core.cpython-312.pyc", "bbc-efb.log", "SimConnect.dll", "x/BBC-EFB.exe",
                     "reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_live_readonly_2026-11-24/raw.txt",
                     "reference/01_CODEX_WORK_EVIDENCE/test_artifacts/x/unittest.txt",
                     "reference/01_CODEX_WORK_EVIDENCE/development reviews/a.md", ".venv/bin/python",
                     "credentials.json", ".env.local"):
            with self.subTest(path=path):
                self.assertIsNotNone(tools.source_problem(path))

    def test_analysis_toc_check(self):
        pure = [("bbc_core.core", "/r/bbc_core/core.py", "PYMODULE"), ("json", "/py/json/__init__.py", "PYMODULE"),
                ("unittest", "/py/unittest/__init__.py", "PYMODULE")]
        binaries = [("PySide6/Qt6Core.dll", "/s/Qt6Core.dll", "BINARY")]
        datas = [("bbc_efb/assets/icon.svg", "/r/icon.svg", "DATA"), ("base_library.zip", "/w/b.zip", "DATA")]
        self.assertEqual(tools.check_analysis_tocs(pure, binaries, datas), [])
        bad = tools.check_analysis_tocs(
            pure + [("tests.test_core", "/r/tests/test_core.py", "PYMODULE"),
                    ("bbc_recorder.tests.test_rules", "/r/x.py", "PYMODULE"),
                    ("bbc_core.simulated", "/r/bbc_core/simulated.py", "PYMODULE")],
            binaries + [("SimConnect.dll", "C:/MSFS SDK/SimConnect.dll", "BINARY")],
            datas + [("logs\\bbc-efb.log", "/r/logs/bbc-efb.log", "DATA")],
            exclude_simulated=True)
        joined = "\n".join(bad)
        self.assertEqual(len(bad), 5, joined)
        for word in ("tests.test_core", "bbc_recorder.tests", "simulated", "SimConnect.dll", "logs/bbc-efb.log"):
            self.assertIn(word, joined)

    def test_bundled_modules_report(self):
        pure = [("bbc_efb.app", "", ""), ("bbc_core", "", ""), ("json", "", ""), ("bbc_efb.app", "", "")]
        text = tools.bundled_modules_report(pure, "1.0.0", exclude_simulated=False)
        self.assertIn("Application modules: 2. Other modules (standard library and PySide6): 1.", text)
        self.assertIn("  bbc_efb.app\n", text)
        self.assertIn("included as code", text)
        self.assertIn("SimConnect.dll: not bundled", text)
        self.assertIn("excluded", tools.bundled_modules_report(pure, "1.0.0", exclude_simulated=True))


class TestOutputParsingTests(unittest.TestCase):
    def test_passing_run(self):
        summary = tools.parse_unittest_output(PASSING_OUTPUT, 0)
        self.assertTrue(summary.passed)
        self.assertEqual((summary.ran, summary.skipped, summary.failures, summary.errors, summary.guard_violations),
                         (412, 3, 0, 0, 0))
        self.assertEqual(summary.runtime, "31.250s")
        self.assertEqual(summary.status, "PASSED")

    def test_failing_run(self):
        text = "Ran 10 tests in 1.0s\n\nFAILED (failures=2, errors=1, skipped=4)\nOffline guard violations: 0\n"
        summary = tools.parse_unittest_output(text, 1)
        self.assertFalse(summary.passed)
        self.assertEqual((summary.failures, summary.errors, summary.skipped), (2, 1, 4))

    def test_never_passes_without_evidence(self):
        cases = {
            "exit code non-zero": (PASSING_OUTPUT, 1),
            "guard violation": (PASSING_OUTPUT.replace("violations: 0", "violations: 2"), 0),
            "no guard line": (PASSING_OUTPUT.replace("Offline guard violations: 0\n", ""), 0),
            "zero tests": ("Ran 0 tests in 0.000s\n\nOK\nOffline guard violations: 0\n", 0),
            "empty output": ("", 0),
            "unexpected success": ("Ran 3 tests in 0.1s\n\nOK (unexpected successes=1)\nOffline guard violations: 0\n", 0),
        }
        for label, (text, code) in cases.items():
            with self.subTest(label):
                self.assertFalse(tools.parse_unittest_output(text, code).passed)

    def test_unknown_counts_stay_unknown(self):
        summary = tools.parse_unittest_output("Traceback: import failed\n", 1)
        self.assertIsNone(summary.ran)
        self.assertIn("ran unknown", summary.describe())
        self.assertIn("guard violations unknown", summary.describe())

    def test_expected_failures_count(self):
        summary = tools.parse_unittest_output(
            "Ran 5 tests in 0.1s\n\nOK (skipped=1, expected failures=2)\nOffline guard violations: 0\n", 0)
        self.assertTrue(summary.passed)
        self.assertEqual(summary.expected_failures, 2)

    def test_status_text(self):
        summary = tools.parse_unittest_output(PASSING_OUTPUT, 0)
        text = tools.render_test_status(
            summary, version="1.0.0", commit="abc123", dirty=True, python_version="CPython 3.12.4 64-bit",
            platform_name="Windows 10", dependencies="PySide6==6.8.3\npyinstaller==6.11.1\n",
            launch_check="not run", built_at=dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc))
        self.assertIn("Result:             PASSED", text)
        self.assertIn("2026-11-24T12:00:00Z", text)
        self.assertIn("abc123 (uncommitted changes: yes", text)
        self.assertIn("NOT VERIFIED", text)
        self.assertIn("  PySide6==6.8.3", text)
        self.assertEqual(tools.read_test_status(text), "PASSED")
        unknown = tools.render_test_status(
            tools.parse_unittest_output("", 1), version="1.0.0", commit=None, dirty=None, python_version="?",
            platform_name="?", dependencies="", launch_check="not run")
        self.assertIn("Result:             FAILED", unknown)
        self.assertIn("Source commit:      unknown", unknown)
        self.assertIn("  unknown", unknown)

    def test_interpreter_check(self):
        self.assertEqual(tools.check_interpreter("CPython", 64, (3, 12, 4)), ([], []))
        problems, warnings = tools.check_interpreter("CPython", 64, (3, 10, 11))
        self.assertEqual(problems, [])
        self.assertTrue(warnings)
        self.assertEqual(len(tools.check_interpreter("PyPy", 32, (3, 9, 0))[0]), 3)


def make_app_dir(base: Path) -> Path:
    app = base / "pyinstaller-dist" / "BBC-EFB"
    write(app / "BBC-EFB.exe", "MZ fake exe")
    write(app / "_internal" / "python312.dll", "fake")
    write(app / "_internal" / "PySide6" / "Qt6Core.dll", "fake")
    with zipfile.ZipFile(app / "_internal" / "base_library.zip", "w") as archive:
        archive.writestr("os.pyc", b"\0")
    return app


def make_docs(base: Path, version: str) -> dict:
    docs = base / "docs"
    return {
        "--readme": write(docs / "README.md", "# Readme"),
        "--limitations": write(docs / "LIMITATIONS.md", "Live MSFS unverified"),
        "--changelog": write(docs / "CHANGELOG.md", f"## {version}\n- Entry\n"),
        "--test-status": write(docs / "TEST_STATUS.txt", "Result:             PASSED\n"),
        "--test-output": write(docs / "TEST_OUTPUT.txt", PASSING_OUTPUT),
        "--notices": write(docs / "NOTICES.md", "notices"),
        "--modules": write(docs / "BUNDLED_MODULES.txt", "modules"),
    }


class ZipTests(unittest.TestCase):
    VERSION = "1.2.3-test.4"

    def assemble(self, base: Path, app: Path, docs: dict) -> tuple[int, Path, str]:
        out = base / "dist" / "portable.zip"
        args = ["assemble-portable", "--root", ROOT, "--version", self.VERSION, "--app-dir", app, "--out", out]
        for flag, path in docs.items():
            args += [flag, path]
        code, _, err = run_cli(*args)
        return code, out, err

    def test_assemble_and_verify_portable_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            code, out, err = self.assemble(base, make_app_dir(base), make_docs(base, self.VERSION))
            self.assertEqual(code, 0, err)
            self.assertEqual(tools.verify_portable_zip(out, self.VERSION), [])
            self.assertEqual(run_cli("verify-portable", out, "--version", self.VERSION, "--root", ROOT)[0], 0)
            names = zipfile.ZipFile(out).namelist()
            root = "BBC-EFB-1.2.3-test.4/"
            self.assertIn(root + "BBC-EFB/BBC-EFB.exe", names)
            self.assertIn(root + "MANIFEST.sha256", names)
            for doc in tools.PORTABLE_DOCS:
                self.assertIn(root + doc, names)
            self.assertFalse(Path(str(out) + ".partial").exists())
            first = out.read_bytes()
            out.unlink()
            self.assertEqual(self.assemble(base, base / "pyinstaller-dist" / "BBC-EFB", make_docs(base, self.VERSION))[0], 0)
            self.assertEqual(out.read_bytes(), first, "archives should be reproducible")

    def test_assemble_refuses_forbidden_pyinstaller_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            app = make_app_dir(base)
            write(app / "SimConnect.dll", "copied by hand")
            write(app / "_internal" / "tests" / "test_core.py")
            code, out, err = self.assemble(base, app, make_docs(base, self.VERSION))
            self.assertEqual(code, 1)
            self.assertIn("SimConnect.dll", err)
            self.assertIn("tests/test_core.py", err)
            self.assertFalse(out.exists())

    def test_assemble_refuses_missing_exe_or_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            app = make_app_dir(base)
            docs = make_docs(base, self.VERSION)
            docs["--notices"] = base / "nope.md"
            self.assertEqual(self.assemble(base, app, docs)[0], 1)
            (app / "BBC-EFB.exe").unlink()
            self.assertEqual(self.assemble(base, app, make_docs(base, self.VERSION))[0], 1)

    def build_zip(self, path: Path, entries: dict, *, manifest=True, root="BBC-EFB-1.2.3-test.4"):
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i, (name, content) in enumerate(entries.items()):
                source = write(Path(tmp) / f"f{i}", content)
                files.append((f"{root}/{name}" if root else name, source))
            tools.write_zip(path, files, manifest=manifest)

    def good_entries(self):
        return {
            "BBC-EFB/BBC-EFB.exe": "MZ", "BBC-EFB/_internal/python312.dll": "x", "README.md": "r",
            "LIMITATIONS.md": "l", "CHANGELOG.md": f"## {self.VERSION}\n", "TEST_STATUS.txt": "Result:   PASSED\n",
            "TEST_OUTPUT.txt": "o", "THIRD_PARTY_NOTICES.md": "n", "BUNDLED_MODULES.txt": "m",
        }

    def test_verify_portable_finds_each_problem(self):
        cases = {
            "tests folder": ({"tests/test_x.py": "x"}, "inside a test"),
            "wip tests": ({"BBC-EFB/_internal/tests_wip/a.txt": "x"}, "work-in-progress"),
            "bytecode": ({"BBC-EFB/_internal/__pycache__/a.cpython-312.pyc": "x"}, "bytecode"),
            "log": ({"BBC-EFB/bbc-efb.log": "x"}, "log"),
            "probe": ({"BBC-EFB/core_live_readonly_1/raw.txt": "x"}, "probe"),
            "dll": ({"BBC-EFB/SimConnect.dll": "x"}, "SimConnect.dll"),
            "failed status": ({"TEST_STATUS.txt": "Result:   FAILED\n"}, "says FAILED"),
            "template changelog": ({"CHANGELOG.md": "TEMPLATE-PLACEHOLDER"}, "unfilled template"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, (extra, expected) in cases.items():
                with self.subTest(label):
                    path = Path(tmp) / f"{label}.zip"
                    entries = self.good_entries()
                    entries.update(extra)
                    self.build_zip(path, entries)
                    problems = tools.verify_portable_zip(path, self.VERSION)
                    self.assertTrue(any(expected in p for p in problems), problems)
            path = Path(tmp) / "good.zip"
            self.build_zip(path, self.good_entries())
            problems = tools.verify_portable_zip(path, self.VERSION)
            self.assertEqual(problems, [])

    def test_verify_portable_structure_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.zip"
            entries = self.good_entries()
            del entries["LIMITATIONS.md"]
            del entries["BBC-EFB/BBC-EFB.exe"]
            self.build_zip(missing, entries)
            joined = "\n".join(tools.verify_portable_zip(missing, self.VERSION))
            self.assertIn("LIMITATIONS.md is missing", joined)
            self.assertIn("BBC-EFB.exe is missing", joined)

            outside = Path(tmp) / "outside.zip"
            self.build_zip(outside, {"BBC-EFB-other/README.md": "x"}, root="")
            self.assertTrue(any("outside" in p for p in tools.verify_portable_zip(outside, self.VERSION)))

            unmanifested = Path(tmp) / "nomanifest.zip"
            self.build_zip(unmanifested, self.good_entries(), manifest=False)
            self.assertIn("MANIFEST.sha256 is missing", tools.verify_portable_zip(unmanifested, self.VERSION))

            tampered = Path(tmp) / "tampered.zip"
            self.build_zip(tampered, self.good_entries())
            with zipfile.ZipFile(tampered, "a") as archive:
                archive.writestr("BBC-EFB-1.2.3-test.4/EXTRA.md", "sneaked in")
            self.assertTrue(any("not listed in MANIFEST" in p for p in tools.verify_portable_zip(tampered, self.VERSION)))

            corrupt = Path(tmp) / "corrupt.zip"
            corrupt.write_bytes(b"not a zip")
            self.assertIn("not a readable ZIP", tools.verify_portable_zip(corrupt, self.VERSION)[0])

    def test_manifest_detects_changed_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.zip"
            self.build_zip(path, self.good_entries())
            rebuilt = Path(tmp) / "b.zip"
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(rebuilt, "w") as target:
                for info in source.infolist():
                    data = source.read(info)
                    if info.filename.endswith("README.md"):
                        data = b"changed"
                    target.writestr(info.filename, data)
            self.assertTrue(any("does not match" in p for p in tools.verify_portable_zip(rebuilt, self.VERSION)))

    def test_write_zip_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write(Path(tmp) / "a", "a")
            with self.assertRaises(ValueError):
                tools.write_zip(Path(tmp) / "x.zip", [("r/a", source), ("r/a", source)])

    def test_sha256sums_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = write(Path(tmp) / "a.zip", "hello")
            out = Path(tmp) / "SHA256SUMS.txt"
            self.assertEqual(run_cli("sha256sums", "--out", out, a)[0], 0)
            self.assertEqual(out.read_text(encoding="utf-8"),
                             "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824  a.zip\n")


class SourceZipTests(unittest.TestCase):
    VERSION = "1.2.3"

    def make_source_tree(self, root: Path):
        for relative in tools.REQUIRED_SOURCE_FILES:
            write(root / relative, f"# {relative}\n")
        write(root / "bbc_efb" / "__init__.py", f"VERSION = '{self.VERSION}'\n")
        write(root / "tests_wip" / "test_new.py")
        write(root / "bbc_core" / "__pycache__" / "core.cpython-312.pyc")
        write(root / "build" / "package-venv" / "pyvenv.cfg")
        write(root / "dist" / "old.zip")
        write(root / "old-snapshot.zip")
        write(root / "logs" / "bbc-efb.log")
        write(root / ".git" / "HEAD")
        write(root / ".venv" / "pyvenv.cfg")
        write(root / "reference" / "evidence" / "REPORT.md")

    def test_walk_listing_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_source_tree(root)
            listed = tools.walk_source_files(root)
            for skipped in ("tests_wip/test_new.py", ".git/HEAD", ".venv/pyvenv.cfg", "build/package-venv/pyvenv.cfg"):
                self.assertNotIn(skipped, listed)
            selection = tools.select_source_files(root, listed + ["deleted/by_git.py", ".git/config"])
            excluded = dict(selection.excluded)
            self.assertIn("old-snapshot.zip", excluded)
            self.assertIn("logs/bbc-efb.log", excluded)
            self.assertIn("deleted/by_git.py", excluded)
            self.assertNotIn(".git/config", selection.included)
            self.assertIn("reference/evidence/REPORT.md", selection.included)
            self.assertIn("tests/test_repo_offline_guard.py", selection.included)

    def test_source_zip_command_and_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self.make_source_tree(root)
            out = Path(tmp) / "source.zip"
            code, stdout, err = run_cli("source-zip", "--root", root, "--out", out, "--no-git")
            self.assertEqual(code, 0, err)
            self.assertIn("Left out of the source ZIP: old-snapshot.zip", stdout)
            self.assertEqual(tools.verify_source_zip(out, self.VERSION), [])
            names = zipfile.ZipFile(out).namelist()
            self.assertIn("BBC-EFB-1.2.3-source/tests/test_repo_offline_guard.py", names)
            self.assertFalse([n for n in names if "tests_wip" in n or n.endswith((".pyc", ".log"))])

    def test_verify_source_reports_missing_and_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write(Path(tmp) / "f", "x")
            path = Path(tmp) / "bad.zip"
            tools.write_zip(path, [("BBC-EFB-1.2.3-source/tests_wip/test_a.py", source),
                                   ("BBC-EFB-1.2.3-source/README.md", source)])
            joined = "\n".join(tools.verify_source_zip(path, self.VERSION))
            self.assertIn("work-in-progress", joined)
            self.assertIn("pyproject.toml is missing", joined)

    def test_parse_git_ls_files(self):
        output = "b.py\0a dir/é.md\0b.py\0".encode("utf-8")
        self.assertEqual(tools.parse_git_ls_files(output), ["a dir/é.md", "b.py"])

    @unittest.skipIf(GUARDED, "runs git as a subprocess, which the offline guard blocks")
    def test_real_repository_listing(self):
        listed = tools.git_source_files(ROOT)
        if listed is None:
            self.skipTest("Git is not available")
        selection = tools.select_source_files(ROOT, listed)
        included = set(selection.included)
        for required in tools.REQUIRED_SOURCE_FILES:
            if (ROOT / required).is_file():
                self.assertIn(required, included)
        self.assertFalse([p for p in included if p.startswith("tests_wip/")])
        self.assertFalse([p for p in included if p.endswith((".zip", ".pyc", ".log", ".dll", ".exe"))])


class WheelTests(unittest.TestCase):
    """check-wheel: a wheel built from pyproject.toml must hold every package."""

    VERSION = "1.2.3a4"

    def make_tree(self, root: Path) -> Path:
        write(root / "bbc_efb" / "__init__.py", f"VERSION = '{self.VERSION}'\n")
        write(root / "bbc_efb" / "__main__.py", "def main(argv=None):\n    return 0\n")
        write(root / "bbc_efb" / "logbook_views" / "__init__.py", "")
        write(root / "bbc_efb" / "logbook_views" / "model.py", "")
        write(root / "bbc_efb" / "STORAGE_SCHEMA.md", "# notes\n")
        write(root / "bbc_core" / "__init__.py", "")
        write(root / "bbc_core" / "ipc" / "__init__.py", "")
        write(root / "bbc_recorder" / "__init__.py", "")
        write(root / "bbc_recorder" / "service.py", "")
        write(root / "tests" / "test_x.py", "")
        return root

    def wheel(self, folder: Path, root: Path, *, skip=(), extra=(), version=None) -> Path:
        path = folder / f"bbc_efb-{self.VERSION}-py3-none-any.whl"
        members = [p.relative_to(root).as_posix() for p in tools.iter_production_files(root, tools.discover_app_packages(root))]
        with zipfile.ZipFile(path, "w") as archive:
            for member in members:
                if member not in skip:
                    archive.writestr(member, "")
            for member in extra:
                archive.writestr(member, "")
            archive.writestr(f"bbc_efb-{self.VERSION}.dist-info/METADATA",
                             f"Metadata-Version: 2.1\nName: bbc-efb\nVersion: {version or self.VERSION}\n\n")
        return path

    def test_complete_wheel_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_tree(Path(tmp) / "src")
            wheel = self.wheel(Path(tmp), root)
            self.assertEqual(tools.wheel_problems(wheel, root, self.VERSION), [])
            code, out, _ = run_cli("check-wheel", wheel, "--root", root)
            self.assertEqual(code, 0)
            self.assertIn("bbc_core.ipc", out)
            self.assertIn("bbc_recorder", out)

    def test_wheel_from_the_old_two_package_list_is_refused(self):
        # The bug the review found: packages = ["bbc_core", "bbc_efb"] shipped only
        # the top-level folders, so bbc_recorder, bbc_core.ipc and the views were lost.
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_tree(Path(tmp) / "src")
            skip = ("bbc_core/ipc/__init__.py", "bbc_efb/logbook_views/__init__.py", "bbc_efb/logbook_views/model.py",
                    "bbc_recorder/__init__.py", "bbc_recorder/service.py")
            wheel = self.wheel(Path(tmp), root, skip=skip)
            joined = "\n".join(tools.wheel_problems(wheel, root, self.VERSION))
            for package in ("bbc_core.ipc", "bbc_efb.logbook_views", "bbc_recorder"):
                self.assertIn(f"missing the {package} package", joined)
            self.assertIn("missing bbc_recorder/service.py", joined)
            self.assertEqual(run_cli("check-wheel", wheel, "--root", root)[0], 1)

    def test_wrong_version_tests_and_missing_required_package_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_tree(Path(tmp) / "src")
            wheel = self.wheel(Path(tmp), root, version="0.0.1", extra=("tests/test_x.py",))
            joined = "\n".join(tools.wheel_problems(wheel, root, self.VERSION))
            self.assertIn("Version: 1.2.3a4", joined)
            self.assertIn("tests/test_x.py", joined)
            (root / "bbc_recorder" / "service.py").unlink()
            (root / "bbc_recorder" / "__init__.py").unlink()
            (root / "bbc_recorder").rmdir()
            self.assertIn("bbc_recorder package is missing", "\n".join(tools.wheel_problems(wheel, root, self.VERSION)))
            broken = Path(tmp) / "broken.whl"
            broken.write_bytes(b"not a zip")
            self.assertIn("not a readable wheel", tools.wheel_problems(broken, root, self.VERSION)[0])

    def test_app_package_names_skip_tests_caches_and_data_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_tree(Path(tmp) / "src")
            write(root / "bbc_core" / "__pycache__" / "__init__.py", "")
            write(root / "bbc_core" / "tests" / "__init__.py", "")
            write(root / "bbc_efb" / "assets" / "__init__.py", "")
            self.assertEqual(tools.app_package_names(root),
                             ["bbc_core", "bbc_core.ipc", "bbc_efb", "bbc_efb.logbook_views", "bbc_recorder"])


class CommandLineTests(unittest.TestCase):
    def test_version_and_checks(self):
        code, out, _ = run_cli("version", "--root", ROOT)
        self.assertEqual((code, out.strip()), (0, tools.read_app_version(ROOT)))
        self.assertEqual(run_cli("check-version-info", "--root", ROOT)[0], 0)
        self.assertEqual(run_cli("check-interpreter")[0], 0)

    def test_check_changelog_refuses_template(self):
        code, _, err = run_cli("check-changelog", ROOT / "packaging" / "CHANGELOG_TEMPLATE.md", "--root", ROOT)
        self.assertEqual(code, 1)
        self.assertIn("TEMPLATE-PLACEHOLDER", err)

    def test_test_status_command_writes_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = write(Path(tmp) / "TEST_OUTPUT.txt", "Ran 2 tests in 0.1s\n\nFAILED (failures=1)\n")
            freeze = Path(tmp) / "freeze.txt"
            freeze.write_bytes("\ufeffPySide6==6.8.3\n".encode("utf-8"))
            status = Path(tmp) / "TEST_STATUS.txt"
            # git_state() would start Git; the offline guard blocks subprocesses.
            with mock.patch.object(tools, "git_state", return_value=(None, None)):
                code, _, _ = run_cli("test-status", "--root", Path(tmp), "--version", "1.0.0", "--test-output", output,
                                     "--exit-code", 1, "--freeze", freeze, "--out", status)
            self.assertEqual(code, 1)
            text = status.read_text(encoding="utf-8")
            self.assertIn("Result:             FAILED", text)
            self.assertIn("  PySide6==6.8.3", text)
            self.assertIn("Source commit:      unknown", text)

    @unittest.skipIf(GUARDED, "starts Python as a subprocess, which the offline guard blocks")
    def test_cli_can_be_run_as_a_script_in_isolated_mode(self):
        import subprocess
        completed = subprocess.run([sys.executable, "-I", str(TOOLS_PATH), "version", "--root", str(ROOT)],
                                   capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), tools.read_app_version(ROOT))


if __name__ == "__main__":
    unittest.main()
