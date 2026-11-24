"""Checks for pyproject.toml, the single version source, the CI workflow and the Python floor."""

import ast
import os
import re
import sys
import unittest
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TOP_LEVEL_PACKAGES = ("bbc_core", "bbc_efb", "bbc_recorder")
# Every package an installed copy needs today. The test below also finds new
# subpackages from the tree, so this list is a floor, not the whole rule.
KNOWN_PACKAGES = ("bbc_core", "bbc_core.ipc", "bbc_efb", "bbc_efb.logbook_views", "bbc_efb.modules", "bbc_recorder")
MINIMUM_PYTHON = (3, 10)

# PEP 440 canonical public version (from the specification's appendix).
PEP440 = re.compile(
    r"^([1-9][0-9]*!)?(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*((a|b|rc)(0|[1-9][0-9]*))?"
    r"(\.post(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?$")

# Standard library names added after Python 3.10 that would break the declared floor.
NEWER_STDLIB = {
    ("tomllib", None), ("enum", "StrEnum"), ("datetime", "UTC"), ("typing", "Self"),
    ("typing", "LiteralString"), ("typing", "Never"), ("typing", "assert_never"),
    ("typing", "reveal_type"), ("typing", "override"), ("typing", "TypeAliasType"),
    ("asyncio", "TaskGroup"), ("asyncio", "timeout"),
}


def python_files():
    for package in TOP_LEVEL_PACKAGES + ("tests",):
        yield from sorted((ROOT / package).rglob("*.py"))


def production_files():
    for package in TOP_LEVEL_PACKAGES:
        yield from sorted((ROOT / package).rglob("*.py"))


def tree_packages():
    """Dotted names of every folder under the bbc_* packages that holds Python code."""

    found = {}
    for package in TOP_LEVEL_PACKAGES:
        for path in (ROOT / package).rglob("*.py"):
            folder = path.parent.relative_to(ROOT)
            if "__pycache__" in folder.parts:
                continue
            found[".".join(folder.parts)] = (ROOT / folder / "__init__.py").is_file()
    return found


def emulate_find(config):
    """Apply [tool.setuptools.packages.find] the way setuptools' PackageFinder does.

    With namespaces = false a folder is a package only when it has an
    __init__.py, the search only descends into packages, and include/exclude
    are fnmatch patterns over the dotted name.
    """

    include = config.get("include", ["*"])
    exclude = config.get("exclude", [])
    packages = []
    for where in config.get("where", ["."]):
        base = (ROOT / where).resolve()
        for dirpath, dirnames, _ in os.walk(base):
            current = Path(dirpath)
            keep = []
            for name in sorted(dirnames):
                full = current / name
                dotted = ".".join(full.relative_to(base).parts)
                looks_like_package = config.get("namespaces", True) or (full / "__init__.py").is_file()
                if "." in name or not looks_like_package:
                    continue
                if any(fnmatchcase(dotted, p) for p in include) and not any(fnmatchcase(dotted, p) for p in exclude):
                    packages.append(dotted)
                keep.append(name)
            dirnames[:] = keep
    return sorted(packages)


def load_pyproject():
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        return None
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def app_version():
    tree = ast.parse((ROOT / "bbc_efb" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("bbc_efb/__init__.py has no VERSION")


class VersionTests(unittest.TestCase):
    """bbc_efb.VERSION is the only version source (pyproject reads it)."""

    def test_version_is_pep_440_and_importable_without_qt(self):
        import bbc_efb
        self.assertRegex(bbc_efb.VERSION, PEP440)
        self.assertEqual(bbc_efb.VERSION, app_version())
        self.assertEqual(bbc_efb.__version__, bbc_efb.VERSION)

    def test_pyproject_has_no_second_copy_of_the_version(self):
        text = PYPROJECT.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^version\s*=\s*[\"']", "the version must come from bbc_efb.VERSION only")
        self.assertIn('version = { attr = "bbc_efb.VERSION" }', text)

    def test_windows_version_resource_and_changelog_template_follow_it(self):
        version = app_version()
        info = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
        self.assertIn(f"StringStruct('FileVersion', '{version}')", info)
        self.assertIn(f"StringStruct('ProductVersion', '{version}')", info)
        self.assertIn(f"## {version} - ", (ROOT / "packaging" / "CHANGELOG_TEMPLATE.md").read_text(encoding="utf-8"))


class PyprojectTests(unittest.TestCase):
    def setUp(self):
        self.data = load_pyproject()
        if self.data is None:
            self.skipTest("tomllib needs Python 3.11 or later")
        self.project = self.data["project"]
        self.setuptools = self.data["tool"]["setuptools"]

    def test_metadata_is_valid_and_declares_the_python_floor(self):
        self.assertEqual(self.project["name"], "bbc-efb")
        self.assertNotIn("version", self.project)
        self.assertEqual(self.project["dynamic"], ["version"])
        self.assertEqual(self.setuptools["dynamic"]["version"], {"attr": "bbc_efb.VERSION"})
        self.assertEqual(self.project["requires-python"], ">=3.10")
        self.assertEqual(self.data["build-system"]["build-backend"], "setuptools.build_meta")

    def test_core_has_no_dependencies_and_the_ui_extra_pins_pyside6_to_6_8(self):
        self.assertEqual(self.project["dependencies"], [])
        (requirement,) = self.project["optional-dependencies"]["ui"]
        match = re.fullmatch(r"PySide6>=6\.8\.(\d+),<6\.9", requirement)
        self.assertIsNotNone(match, requirement)

    def test_package_discovery_covers_every_package_in_the_tree(self):
        self.assertIsInstance(self.setuptools["packages"], dict, "an explicit list goes stale; use packages.find")
        find = self.setuptools["packages"]["find"]
        self.assertIs(find["namespaces"], False)
        found = emulate_find(find)
        for package in KNOWN_PACKAGES:
            self.assertIn(package, found)
        code_folders = tree_packages()
        self.assertEqual(sorted(name for name, has_init in code_folders.items() if not has_init), [],
                         "a code folder without __init__.py would be left out of an install")
        self.assertEqual(sorted(set(code_folders) - set(found)), [], "code folders missing from the install")
        self.assertFalse([p for p in found if p.split(".")[0] not in TOP_LEVEL_PACKAGES or "__pycache__" in p])

    def test_setuptools_finds_the_same_packages_and_version_when_available(self):
        try:
            from setuptools.config.pyprojecttoml import read_configuration
        except ImportError:
            self.skipTest("setuptools is not installed in this environment (CI checks a real wheel)")
        config = read_configuration(str(PYPROJECT))
        self.assertEqual(config["project"]["version"], app_version())
        packages = sorted(config["tool"]["setuptools"]["packages"])
        self.assertEqual(packages, emulate_find(self.setuptools["packages"]["find"]))
        for package in KNOWN_PACKAGES:
            self.assertIn(package, packages)

    def test_the_old_explicit_list_would_fail_this_check(self):
        # Regression guard for the review finding: packages = ["bbc_core", "bbc_efb"].
        old = {"bbc_core", "bbc_efb"}
        self.assertTrue(set(tree_packages()) - old, "the tree has subpackages the old list left out")

    def test_no_package_data_is_needed_at_run_time(self):
        self.assertIs(self.setuptools["include-package-data"], False)
        self.assertNotIn("package-data", self.setuptools)
        data_names = {p.name for package in TOP_LEVEL_PACKAGES for p in (ROOT / package).rglob("*")
                      if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts}
        problems = []
        for path in production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                          and node.body and isinstance(node.body[0], ast.Expr)
                          and isinstance(node.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    if any(n in ("importlib.resources", "pkgutil") for n in names) or (
                            isinstance(node, ast.ImportFrom) and node.module == "importlib"
                            and any(a.name == "resources" for a in node.names)):
                        problems.append(f"{path.relative_to(ROOT)}:{node.lineno} reads package resources")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    for name in data_names:
                        if re.search(r"(^|[/\\\\])" + re.escape(name) + r"$", node.value):
                            problems.append(f"{path.relative_to(ROOT)}:{node.lineno} opens {name}")
        self.assertEqual(problems, [], "add package-data to pyproject.toml (and the spec) before reading these")

    def test_entry_point_exists_and_defines_main(self):
        target = self.project["gui-scripts"]["bbc-efb"]
        self.assertEqual(target, "bbc_efb.__main__:main")
        main_module = ROOT / "bbc_efb" / "__main__.py"
        self.assertTrue(main_module.is_file())
        tree = ast.parse(main_module.read_text(encoding="utf-8"))
        self.assertIn("main", {node.name for node in tree.body if isinstance(node, ast.FunctionDef)})
        launcher = (ROOT / "packaging" / "launch_bbc_efb.py").read_text(encoding="utf-8")
        self.assertIn("from bbc_efb.__main__ import main", launcher)


class PythonFloorTests(unittest.TestCase):
    def test_sources_parse_with_python_3_10_grammar(self):
        files = list(python_files())
        self.assertTrue(any(p.parts[-2] == "bbc_recorder" for p in files))
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=MINIMUM_PYTHON)

    def test_sources_avoid_standard_library_names_newer_than_3_10(self):
        problems = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            guarded = {id(node) for handler in ast.walk(tree) if isinstance(handler, ast.Try)
                       for node in ast.walk(handler)
                       if any(isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError")
                              for h in handler.handlers)}
            for node in ast.walk(tree):
                if id(node) in guarded:
                    continue
                if isinstance(node, ast.Import):
                    found = {(alias.name, None) for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    found = {(node.module, alias.name) for alias in node.names}
                else:
                    continue
                for item in found & NEWER_STDLIB:
                    problems.append(f"{path.relative_to(ROOT)}:{node.lineno} {item[0]}{'.' + item[1] if item[1] else ''}")
        self.assertEqual(problems, [])

    def test_core_uses_the_standard_library_only(self):
        if sys.version_info < (3, 10):
            self.skipTest("sys.stdlib_module_names needs Python 3.10")
        problems = []
        for path in sorted((ROOT / "bbc_core").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    if top not in sys.stdlib_module_names and top != "bbc_core":
                        problems.append(f"{path.name}:{node.lineno} {name}")
        self.assertEqual(problems, [])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_matrix_covers_windows_and_linux_on_the_floor_and_tested_versions(self):
        self.assertIn("os: [windows-latest, ubuntu-latest]", self.text)
        self.assertIn('python-version: ["3.10", "3.11", "3.12"]', self.text)
        self.assertIn("QT_QPA_PLATFORM: offscreen", self.text)
        self.assertNotIn("\t", self.text, "YAML does not allow tab indentation")

    def test_full_suite_runs_with_the_repository_root_as_top_level(self):
        self.assertIn('python -m unittest discover -s tests -t . -p "test_*.py"', self.text)
        self.assertIn("python -B tests/test_repo_offline_guard.py", self.text)

    def test_a_real_wheel_is_built_checked_and_installed_outside_the_repository(self):
        build = self.text.index("pip wheel --no-deps")
        check = self.text.index("package_tools.py check-wheel")
        install = self.text.index('"${wheel}[ui]"')
        smoke = self.text.index("import bbc_recorder.service")
        self.assertLess(build, check)
        self.assertLess(check, install)
        self.assertLess(install, smoke)
        self.assertNotIn("pip install -e", self.text, "an editable install hides missing packages")
        smoke_step = self.text[self.text.rindex("- name:", 0, smoke):smoke]
        self.assertIn("working-directory: ${{ runner.temp }}", smoke_step)
        for package in KNOWN_PACKAGES:
            self.assertIn(package, self.text[smoke:])

    def test_static_checks_run_in_ci_with_pinned_tools(self):
        # A89: the ruff and mypy settings in pyproject.toml must be exercised by CI.
        job = self.text[self.text.index("  static-checks:"):]
        self.assertIn('"ruff==', job)
        self.assertIn('"mypy==', job)
        self.assertIn("ruff check bbc_core bbc_efb bbc_recorder", job)
        self.assertIn("run: mypy", job)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.ruff]", pyproject)
        self.assertIn("[tool.mypy]", pyproject)
        self.assertIn("static-checks", pyproject)


if __name__ == "__main__":
    unittest.main()
