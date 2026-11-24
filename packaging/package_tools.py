"""Packaging helpers for the BBC EFB portable Windows build.

This file is shared by ``packaging/bbc-efb.spec`` (PyInstaller) and
``packaging/build_portable.ps1``. It uses the standard library only, never
imports the application, and never loads native libraries, so the same rules can
be checked offline on any platform.

It is deliberately *not* importable as ``packaging.package_tools``: the folder
has no ``__init__.py`` because ``packaging`` is also the name of a PyPI package
that pip, setuptools and PyInstaller import. Load it by path::

    python -I packaging/package_tools.py <command> ...

or, from Python, with ``importlib.util.spec_from_file_location``.

Commands (each exits 0 on success, 1 on a refusal, 2 on bad arguments):

    version            print the application version from bbc_efb/__init__.py
    check-interpreter  check this Python is 64-bit CPython 3.10 or later
    check-version-info check packaging/version_info.txt against that version
    check-entry-point  check bbc_efb/__main__.py defines main()
    check-changelog    check a changelog names the version and is not the template
    run-tests          run the guarded offline suite and save its output
    test-status        write TEST_STATUS.txt from a saved test run
    assemble-portable  write the portable ZIP from the PyInstaller output
    source-zip         write the full-source ZIP from the working tree
    verify-portable    list problems in a portable ZIP (exit 1 if any)
    verify-source      list problems in a source ZIP (exit 1 if any)
    check-wheel        check a built wheel holds every application package
    sha256sums         write a SHA256SUMS.txt for the given files
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import struct
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Sequence

APP_NAME = "BBC-EFB"
EXE_NAME = APP_NAME + ".exe"
APP_PACKAGE_PREFIX = "bbc_"
# Top-level packages every build and every wheel must contain. Subpackages are
# found from the tree (every folder with an __init__.py), see app_package_names().
REQUIRED_PACKAGES = ("bbc_core", "bbc_efb", "bbc_recorder")
# The simulated provider ships as code inside bbc_core unless the build asks
# for it to be left out (see LIMITATIONS.md and the owner proposals).
SIMULATED_MODULES = ("bbc_core.simulated",)
TEST_COMMAND = ("-B", "tests/test_repo_offline_guard.py")
TESTED_PYTHON = (3, 12)
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
CHANGELOG_TEMPLATE_MARKER = "TEMPLATE-PLACEHOLDER"
MANIFEST_NAME = "MANIFEST.sha256"

# Files placed at the top of the portable ZIP, beside the application folder.
PORTABLE_DOCS = (
    "README.md", "LIMITATIONS.md", "CHANGELOG.md", "TEST_STATUS.txt", "TEST_OUTPUT.txt",
    "THIRD_PARTY_NOTICES.md", "BUNDLED_MODULES.txt",
)
REQUIRED_SOURCE_FILES = (
    "pyproject.toml", "README.md", "bbc_efb/__init__.py", "bbc_core/__init__.py",
    "packaging/bbc-efb.spec", "packaging/build_portable.ps1", "packaging/package_tools.py",
    "packaging/requirements-build.txt", "tests/test_repo_offline_guard.py",
)

# Standard library and tooling modules the frozen EFB never needs. An entry is
# dropped automatically when production code imports it.
BASE_EXCLUDES = (
    "tests", "tests_wip", "test", "pytest", "_pytest",
    "tkinter", "_tkinter", "turtle", "turtledemo", "idlelib", "lib2to3", "pydoc_data",
    "ensurepip", "venv", "distutils", "setuptools", "pkg_resources", "pip",
    "PyInstaller",
)

# Qt for Python modules that are large and unused by the EFB today. A module is
# kept when production code imports it, or when a kept module depends on it.
QT_OPTIONAL_MODULES = (
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
    "QtAxContainer", "QtBluetooth", "QtCharts", "QtDataVisualization", "QtDesigner",
    "QtGraphs", "QtGraphsWidgets", "QtHelp", "QtHttpServer", "QtLocation",
    "QtMultimedia", "QtMultimediaWidgets", "QtNetworkAuth", "QtNfc", "QtPdf", "QtPdfWidgets",
    "QtPositioning", "QtQml", "QtQuick", "QtQuick3D", "QtQuickControls2", "QtQuickTest",
    "QtQuickWidgets", "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus",
    "QtSerialPort", "QtSpatialAudio", "QtSql", "QtStateMachine", "QtTest", "QtTextToSpeech",
    "QtUiTools", "QtWebChannel", "QtWebEngineCore", "QtWebEngineQuick", "QtWebEngineWidgets",
    "QtWebSockets", "QtWebView",
)
# Best-effort Python-level dependencies between optional Qt modules. Kept
# modules pull their dependencies back out of the exclude list.
QT_DEPENDENCIES = {
    "Qt3DAnimation": ("Qt3DCore", "Qt3DRender"),
    "Qt3DExtras": ("Qt3DCore", "Qt3DRender", "Qt3DInput", "Qt3DLogic"),
    "Qt3DInput": ("Qt3DCore",),
    "Qt3DLogic": ("Qt3DCore",),
    "Qt3DRender": ("Qt3DCore",),
    "QtGraphs": ("QtQuick", "QtQml", "QtQuick3D"),
    "QtGraphsWidgets": ("QtGraphs", "QtQuickWidgets", "QtQuick", "QtQml"),
    "QtLocation": ("QtPositioning", "QtQuick", "QtQml"),
    "QtMultimediaWidgets": ("QtMultimedia",),
    "QtPdfWidgets": ("QtPdf",),
    "QtQuick": ("QtQml",),
    "QtQuick3D": ("QtQuick", "QtQml"),
    "QtQuickControls2": ("QtQuick", "QtQml"),
    "QtQuickTest": ("QtQuick", "QtQml", "QtTest"),
    "QtQuickWidgets": ("QtQuick", "QtQml"),
    "QtRemoteObjects": ("QtQml",),
    "QtScxml": ("QtQml",),
    "QtWebEngineCore": ("QtWebChannel", "QtPositioning", "QtQuick", "QtQml"),
    "QtWebEngineQuick": ("QtWebEngineCore", "QtQuick", "QtQml"),
    "QtWebEngineWidgets": ("QtWebEngineCore", "QtQuickWidgets", "QtWebChannel", "QtPrintSupport"),
    "QtWebView": ("QtQuick", "QtQml"),
}

# Folder names inside a production package whose files ship as data.
DATA_FOLDERS = ("assets", "resources")

_ARCHIVE_SUFFIXES = (".zip", ".whl", ".7z", ".rar", ".tar", ".gz", ".tgz", ".msi")
_SECRET_NAMES = frozenset({".env", ".netrc", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"})
_SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12")
_ROTATED_LOG = re.compile(r"\.log\.\d+$", re.IGNORECASE)
_VERSION_LINE = re.compile(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*$", re.MULTILINE)
_SEMVERISH = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?P<rest>[0-9A-Za-z.+-]*)$")


# ---------------------------------------------------------------------------
# Version


def read_app_version(root: Path) -> str:
    """Return ``bbc_efb.VERSION`` without importing the application."""

    init = Path(root) / "bbc_efb" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(init))
    except SyntaxError as exc:  # pragma: no cover - the test suite would fail first
        raise ValueError(f"{init} does not parse: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    match = _VERSION_LINE.search(text)
    if match:
        return match.group(1)
    raise ValueError(f"{init} has no VERSION = '...' string")


def windows_version_tuple(version: str) -> tuple[int, int, int, int]:
    """Map the app version to the four numbers of a Windows version resource.

    ``0.1.0-concept-a.1`` becomes ``(0, 1, 0, 1)``: major, minor and patch, then
    the trailing pre-release number (0 when there is none).
    """

    match = _SEMVERISH.match(version.strip())
    if not match:
        raise ValueError(f"version {version!r} does not start with MAJOR.MINOR.PATCH")
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    tail = re.search(r"(\d+)$", match.group("rest"))
    build = int(tail.group(1)) if tail else 0
    parts = (major, minor, patch, build)
    if any(part > 0xFFFF for part in parts):
        raise ValueError(f"version {version!r} has a part above 65535")
    return parts


def safe_version_label(version: str) -> str:
    """Return the version as a file-name-safe label."""

    label = re.sub(r"[^0-9A-Za-z.+-]", "-", version.strip())
    if not label or label in (".", "..") or label.startswith("."):
        raise ValueError(f"version {version!r} cannot be used in a file name")
    return label


def read_version_info(path: Path) -> dict:
    """Parse a PyInstaller VSVersionInfo file without evaluating it."""

    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    found: dict = {"strings": {}}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name == "FixedFileInfo":
            for keyword in node.keywords:
                if keyword.arg in ("filevers", "prodvers"):
                    found[keyword.arg] = tuple(ast.literal_eval(keyword.value))
        elif name == "StringStruct" and len(node.args) == 2:
            key, value = (ast.literal_eval(arg) for arg in node.args)
            found["strings"][key] = value
    return found


def check_version_info(path: Path, version: str) -> list[str]:
    """Return problems between version_info.txt and the app version."""

    problems: list[str] = []
    try:
        info = read_version_info(path)
    except (OSError, SyntaxError, ValueError) as exc:
        return [f"{path} cannot be read as a version resource: {exc}"]
    expected = windows_version_tuple(version)
    for key in ("filevers", "prodvers"):
        if info.get(key) != expected:
            problems.append(f"{path.name} {key} is {info.get(key)} but bbc_efb.VERSION {version!r} needs {expected}")
    for key in ("FileVersion", "ProductVersion"):
        if info["strings"].get(key) != version:
            problems.append(f"{path.name} {key} is {info['strings'].get(key)!r} but bbc_efb.VERSION is {version!r}")
    if info["strings"].get("OriginalFilename") != EXE_NAME:
        problems.append(f"{path.name} OriginalFilename should be {EXE_NAME!r}")
    return problems


def check_entry_point(root: Path) -> list[str]:
    """Return problems with ``bbc_efb/__main__.py`` (the packaged entry point)."""

    main = Path(root) / "bbc_efb" / "__main__.py"
    if not main.is_file():
        return [f"{main.relative_to(root).as_posix()} is missing. The EFB entry point "
                "bbc_efb.__main__:main must exist before the build can be packaged."]
    try:
        tree = ast.parse(main.read_text(encoding="utf-8"), filename=str(main))
    except SyntaxError as exc:
        return [f"bbc_efb/__main__.py does not parse: {exc}"]
    if "main" not in {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}:
        return ["bbc_efb/__main__.py has no top-level main() function"]
    return []


def check_changelog(path: Path, version: str) -> list[str]:
    """Return problems that make a changelog unfit to ship with ``version``."""

    path = Path(path)
    if not path.is_file():
        return [f"No changelog at {path}. Copy packaging/CHANGELOG_TEMPLATE.md to CHANGELOG.md "
                "in the repository root and describe this build."]
    text = path.read_text(encoding="utf-8")
    problems = []
    if CHANGELOG_TEMPLATE_MARKER in text:
        problems.append(f"{path.name} still contains {CHANGELOG_TEMPLATE_MARKER} lines from the template")
    if version not in text:
        problems.append(f"{path.name} has no entry for version {version}")
    return problems


# ---------------------------------------------------------------------------
# Production source discovery


def discover_app_packages(root: Path) -> list[str]:
    """Return the top-level ``bbc_*`` packages that hold Python code."""

    packages = []
    for child in sorted(Path(root).iterdir()):
        if child.is_dir() and child.name.startswith(APP_PACKAGE_PREFIX) and child.name.isidentifier():
            if any(child.rglob("*.py")):
                packages.append(child.name)
    return packages


def _is_test_part(part: str) -> bool:
    lowered = part.casefold()
    return (lowered in ("tests", "tests_wip", "test", "testing", "fixtures", "__pycache__")
            or lowered.startswith("test_") or lowered.endswith("_test") or lowered.endswith("_tests"))


def is_test_module(name: str) -> bool:
    """True for a dotted module name that belongs to a test suite or fixture."""

    parts = name.split(".")
    return any(_is_test_part(part) for part in parts)


def iter_production_files(root: Path, packages: Sequence[str]) -> Iterator[Path]:
    """Yield production ``.py`` files of the given packages (no tests, no caches)."""

    root = Path(root)
    for package in packages:
        for path in sorted((root / package).rglob("*.py")):
            relative = path.relative_to(root)
            if any(_is_test_part(part) for part in relative.with_suffix("").parts):
                continue
            yield path


def production_modules(root: Path, packages: Sequence[str], *, exclude_simulated: bool = False) -> list[str]:
    """Return dotted names of every production module, for PyInstaller hiddenimports.

    Every module is listed, not only those reachable by static imports, so code
    loaded by name at run time is still present in the frozen build.
    """

    modules = []
    for path in iter_production_files(root, packages):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        name = ".".join(parts)
        if exclude_simulated and any(name == s or name.startswith(s + ".") for s in SIMULATED_MODULES):
            continue
        modules.append(name)
    return sorted(set(modules))


def missing_package_inits(root: Path, packages: Sequence[str]) -> list[str]:
    """Return production package folders without ``__init__.py`` (namespace packages)."""

    missing = []
    root = Path(root)
    for package in packages:
        for folder in [root / package, *sorted(p for p in (root / package).rglob("*") if p.is_dir())]:
            relative = folder.relative_to(root)
            if any(_is_test_part(part) for part in relative.parts) or folder.name in DATA_FOLDERS:
                continue
            if any(parent.name in DATA_FOLDERS for parent in folder.relative_to(root).parents):
                continue
            if any(folder.glob("*.py")) and not (folder / "__init__.py").is_file():
                missing.append(relative.as_posix())
    return missing


def imported_modules(paths: Iterable[Path]) -> set[str]:
    """Return every absolute module name imported by the given files."""

    names: set[str] = set()
    for path in paths:
        try:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*")
    return names


def used_qt_modules(imports: Iterable[str]) -> set[str]:
    """Return the Qt for Python module names (``QtCore`` etc.) in ``imports``."""

    used = set()
    for name in imports:
        parts = name.split(".")
        if parts[0] == "PySide6" and len(parts) > 1 and parts[1].startswith("Qt"):
            used.add(parts[1])
    return used


def qt_excludes(used: Iterable[str]) -> list[str]:
    """Return ``PySide6.<module>`` excludes for optional Qt modules nobody uses."""

    keep = set(used)
    pending = list(keep)
    while pending:
        for dependency in QT_DEPENDENCIES.get(pending.pop(), ()):
            if dependency not in keep:
                keep.add(dependency)
                pending.append(dependency)
    return [f"PySide6.{name}" for name in QT_OPTIONAL_MODULES if name not in keep]


def build_excludes(imports: Iterable[str], *, exclude_simulated: bool = False) -> list[str]:
    """Return the PyInstaller ``excludes`` list for the given production imports."""

    imports = set(imports)
    top_level = {name.split(".")[0] for name in imports}
    excludes = [name for name in BASE_EXCLUDES if name not in top_level]
    excludes += qt_excludes(used_qt_modules(imports))
    if exclude_simulated:
        excludes += list(SIMULATED_MODULES)
    return excludes


def simulated_importers(root: Path, packages: Sequence[str]) -> list[str]:
    """Return production files that import the simulated provider."""

    importers = []
    for path in iter_production_files(root, packages):
        relative = path.relative_to(root).with_suffix("")
        name = ".".join(relative.parts)
        if any(name == s for s in SIMULATED_MODULES):
            continue
        found = imported_modules([path])
        text = path.read_text(encoding="utf-8")
        relative_use = re.search(r"^\s*from\s+\.+\s*(import\s+[^\n]*\bsimulated\b|simulated\b)", text, re.MULTILINE)
        if any(m == s or m.startswith(s + ".") for m in found for s in SIMULATED_MODULES) or relative_use:
            importers.append(relative.as_posix() + ".py")
    return importers


def app_package_names(root: Path, packages: Sequence[str] | None = None) -> list[str]:
    """Return the dotted names of every production package (folders with ``__init__.py``).

    This is what an installed copy must contain: for example ``bbc_core``,
    ``bbc_core.ipc``, ``bbc_efb.logbook_views`` and ``bbc_recorder``.
    """

    root = Path(root)
    names = []
    for package in (discover_app_packages(root) if packages is None else packages):
        for init in sorted((root / package).rglob("__init__.py")):
            relative = init.parent.relative_to(root)
            if any(_is_test_part(part) or part in DATA_FOLDERS for part in relative.parts):
                continue
            names.append(".".join(relative.parts))
    return sorted(set(names))


def wheel_problems(wheel: Path, root: Path, version: str) -> list[str]:
    """Return problems with a built wheel: missing packages, wrong version, test code.

    The wheel must contain the ``__init__.py`` of every package that
    :func:`app_package_names` finds in the tree and every production module,
    carry ``Version: <bbc_efb.VERSION>`` in its metadata, and hold no tests.
    """

    wheel, root = Path(wheel), Path(root)
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            metadata = [n for n in names if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
            meta_text = archive.read(metadata[0]).decode("utf-8", errors="replace") if len(metadata) == 1 else ""
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"{wheel.name} is not a readable wheel: {exc}"]
    problems = []
    packages = discover_app_packages(root)
    for required in REQUIRED_PACKAGES:
        if required not in packages:
            problems.append(f"the {required} package is missing from {root}")
    for package in app_package_names(root, packages):
        if package.replace(".", "/") + "/__init__.py" not in names:
            problems.append(f"{wheel.name} is missing the {package} package")
    for path in iter_production_files(root, packages):
        member = path.relative_to(root).as_posix()
        if member not in names:
            problems.append(f"{wheel.name} is missing {member}")
    if not metadata:
        problems.append(f"{wheel.name} has no .dist-info/METADATA")
    elif f"\nVersion: {version}\n" not in "\n" + meta_text:
        problems.append(f"{wheel.name} metadata does not say Version: {version} (bbc_efb.VERSION)")
    for name in sorted(names):
        top = name.split("/", 1)[0].casefold()
        test_code = name.endswith(".py") and is_test_module(name[:-3].replace("/", "."))
        if top in ("tests", "tests_wip", "packaging", "reference") or test_code:
            problems.append(f"{wheel.name} contains test or development file {name}")
    return problems


def package_data_files(root: Path, packages: Sequence[str]) -> list[tuple[str, str]]:
    """Return PyInstaller ``datas`` pairs for files under ``assets``/``resources`` folders."""

    root = Path(root)
    datas = []
    for package in packages:
        for path in sorted((root / package).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if not any(part in DATA_FOLDERS for part in relative.parts[1:-1]):
                continue
            if portable_problem(relative.as_posix()) or path.suffix in (".py", ".pyc", ".pyo"):
                continue
            datas.append((str(path), relative.parent.as_posix()))
    return datas


# ---------------------------------------------------------------------------
# Content rules


def _common_problem(path: str) -> str | None:
    pure = PurePosixPath(path)
    if "\\" in path:
        return "uses a backslash separator"
    if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", path):
        return "is not a safe relative path"
    name = pure.name.casefold()
    if name in _SECRET_NAMES or name.startswith(".env.") or name.endswith(_SECRET_SUFFIXES):
        return "looks like a credential or key file"
    if name.endswith((".pyc", ".pyo")) or "__pycache__" in (p.casefold() for p in pure.parts):
        return "is a Python bytecode cache from the development tree"
    if name.endswith(".log") or _ROTATED_LOG.search(name) or name in ("fault.log", "unittest.txt"):
        return "is a log or test runner output"
    lowered_parts = [p.casefold() for p in pure.parts]
    if any(p.startswith("core_live_readonly_") for p in lowered_parts):
        return "is raw live simulator probe output"
    if any(p in (".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache") for p in lowered_parts):
        return "belongs to a development environment or tool cache"
    if "tests_wip" in lowered_parts:
        return "is an unfinished work-in-progress test"
    return None


def portable_problem(path: str) -> str | None:
    """Return why ``path`` must not appear in the portable ZIP, or None."""

    problem = _common_problem(path)
    if problem:
        return problem
    pure = PurePosixPath(path)
    lowered_parts = [p.casefold() for p in pure.parts]
    name = lowered_parts[-1] if lowered_parts else ""
    if name == "simconnect.dll":
        return "is SimConnect.dll, which has no approved redistribution"
    if any(p in ("tests", "test", "fixtures", "test_artifacts", "logs", "in-progress",
                 "01_codex_work_evidence", "review-evidence") for p in lowered_parts[:-1]):
        return "is inside a test, fixture, evidence or log folder"
    if name.startswith("test_") and name.endswith(".py"):
        return "is a test module"
    if any("probe" in p or "preflight" in p for p in lowered_parts):
        return "looks like probe output"
    if name.endswith(".jsonl"):
        return "is a JSONL journal or recording, which is pilot or test data"
    if name.endswith(_ARCHIVE_SUFFIXES) and name != "base_library.zip":
        return "is a nested archive that cannot be inspected"
    if name.endswith((".py", ".pyw")) and any(p.startswith(APP_PACKAGE_PREFIX) for p in lowered_parts):
        # PyInstaller keeps application code inside the executable archive.
        return "is loose application source inside the app folder"
    return None


def source_problem(path: str) -> str | None:
    """Return why ``path`` must not appear in the source ZIP, or None."""

    problem = _common_problem(path)
    if problem:
        return problem
    lowered_parts = [p.casefold() for p in PurePosixPath(path).parts]
    name = lowered_parts[-1] if lowered_parts else ""
    if lowered_parts and lowered_parts[0] in ("build", "dist"):
        return "is build output"
    if any(p == "development reviews" for p in lowered_parts):
        return "is an excluded development review folder"
    if name.endswith((".dll", ".exe", ".pyd")):
        return "is a native binary"
    if name.endswith(_ARCHIVE_SUFFIXES) and not name.endswith((".docx", ".xlsx", ".pptx")):
        return "is a nested archive"
    return None


def check_analysis_tocs(pure: Iterable, binaries: Iterable, datas: Iterable,
                        *, exclude_simulated: bool = False) -> list[str]:
    """Check PyInstaller TOCs (``a.pure``, ``a.binaries``, ``a.datas``) for forbidden content."""

    problems = []
    for entry in pure:
        name = str(entry[0])
        top = name.split(".")[0]
        if top in ("tests", "tests_wip") or (top.startswith(APP_PACKAGE_PREFIX) and is_test_module(name)):
            problems.append(f"module {name} is test code")
        if exclude_simulated and any(name == s or name.startswith(s + ".") for s in SIMULATED_MODULES):
            problems.append(f"module {name} is the simulated provider, which this build excludes")
    for kind, toc in (("binary", binaries), ("data file", datas)):
        for entry in toc:
            destination = str(entry[0]).replace("\\", "/")
            problem = portable_problem(destination)
            if problem:
                problems.append(f"{kind} {destination} {problem}")
    return problems


def bundled_modules_report(pure: Iterable, version: str, *, exclude_simulated: bool) -> str:
    """Return the text of BUNDLED_MODULES.txt for the frozen build."""

    names = sorted({str(entry[0]) for entry in pure})
    app = [n for n in names if n.split(".")[0].startswith(APP_PACKAGE_PREFIX)]
    lines = [
        f"BBC EFB {version}: Python modules frozen into {EXE_NAME}",
        f"Application modules: {len(app)}. Other modules (standard library and PySide6): {len(names) - len(app)}.",
        "Simulated provider: " + ("excluded from this build" if exclude_simulated else
                                  "included as code (bbc_core.simulated); it is never started unless the EFB selects it"),
        "SimConnect.dll: not bundled. The EFB uses the pilot's own MSFS SDK copy.",
        "",
        "Application modules:",
    ]
    lines += [f"  {name}" for name in app]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Test run and status


@dataclass(frozen=True, slots=True)
class TestSummary:
    """Counts parsed from unittest output. Unknown counts stay None."""

    exit_code: int
    ran: int | None = None
    skipped: int | None = None
    failures: int | None = None
    errors: int | None = None
    expected_failures: int | None = None
    unexpected_successes: int | None = None
    guard_violations: int | None = None
    runtime: str | None = None
    outcome_line: str | None = None

    @property
    def passed(self) -> bool:
        """True only when the run demonstrably passed with zero guard violations."""

        return (self.exit_code == 0 and bool(self.ran) and self.outcome_line is not None
                and self.outcome_line.startswith("OK") and self.guard_violations == 0
                and not self.failures and not self.errors and not self.unexpected_successes)

    @property
    def status(self) -> str:
        return STATUS_PASSED if self.passed else STATUS_FAILED

    def describe(self) -> str:
        def show(value):
            return "unknown" if value is None else str(value)
        return (f"ran {show(self.ran)}, skipped {show(self.skipped)}, failures {show(self.failures)}, "
                f"errors {show(self.errors)}, expected failures {show(self.expected_failures)}, "
                f"unexpected successes {show(self.unexpected_successes)}, "
                f"offline guard violations {show(self.guard_violations)}")


def parse_unittest_output(text: str, exit_code: int) -> TestSummary:
    """Parse the summary that ``unittest`` and the offline guard print."""

    ran = runtime = outcome = None
    counts = {"skipped": None, "failures": None, "errors": None,
              "expected failures": None, "unexpected successes": None}
    guard = None
    for match in re.finditer(r"^Ran (\d+) tests? in ([0-9.]+s)\s*$", text, re.MULTILINE):
        ran, runtime = int(match.group(1)), match.group(2)
    outcomes = list(re.finditer(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", text, re.MULTILINE))
    if outcomes:
        last = outcomes[-1]
        outcome = last.group(0).strip()
        for key in counts:
            counts[key] = 0
        for part in (last.group(2) or "").split(","):
            if "=" in part:
                key, _, value = part.strip().partition("=")
                key = key.replace("_", " ")
                if key in counts and value.isdigit():
                    counts[key] = int(value)
    for match in re.finditer(r"^Offline guard violations: (\d+)\s*$", text, re.MULTILINE):
        guard = int(match.group(1))
    return TestSummary(
        exit_code=exit_code, ran=ran, skipped=counts["skipped"], failures=counts["failures"],
        errors=counts["errors"], expected_failures=counts["expected failures"],
        unexpected_successes=counts["unexpected successes"], guard_violations=guard,
        runtime=runtime, outcome_line=outcome,
    )


def run_offline_tests(root: Path, python: str, output: Path, *, timeout_s: float = 1800.0) -> TestSummary:
    """Run the guarded offline suite with ``python`` and save its combined output."""

    env = dict(os.environ)
    env.update({"QT_QPA_PLATFORM": "offscreen", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    env.pop("PYTHONPATH", None)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [python, *TEST_COMMAND]
    try:
        completed = subprocess.run(command, cwd=str(root), env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=timeout_s, check=False)
        text = completed.stdout.decode("utf-8", errors="replace")
        code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or b"").decode("utf-8", errors="replace") + f"\nTimed out after {timeout_s:.0f} s\n"
        code = 124
    text = text.replace("\r\n", "\n")
    output.write_text(f"$ python {' '.join(TEST_COMMAND)}\n" + text, encoding="utf-8")
    return parse_unittest_output(text, code)


def render_test_status(summary: TestSummary, *, version: str, commit: str | None, dirty: bool | None,
                       python_version: str, platform_name: str, dependencies: str,
                       launch_check: str, built_at: _dt.datetime | None = None) -> str:
    """Return the text of TEST_STATUS.txt. Unknown values are written as unknown."""

    built_at = built_at or _dt.datetime.now(_dt.timezone.utc)
    if commit:
        tree = {True: "yes, the build includes uncommitted changes", False: "no", None: "unknown"}[dirty]
        commit_line = f"{commit} (uncommitted changes: {tree})"
    else:
        commit_line = "unknown (Git was not available or this is not a Git working tree)"
    lines = [
        f"BBC EFB {version} test status",
        "",
        f"Built (UTC):        {built_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Source commit:      {commit_line}",
        f"Build machine:      {platform_name}",
        f"Python:             {python_version}",
        "",
        f"Offline suite:      python {' '.join(TEST_COMMAND)}",
        "                    (QT_QPA_PLATFORM=offscreen; no simulator, SimConnect.dll or network;",
        "                    the guard blocks native DLL loads, sockets and subprocesses)",
        f"Result:             {summary.status}",
        f"Counts:             {summary.describe()}",
        f"Runner summary:     {summary.outcome_line or 'not found in the output'}"
        + (f" in {summary.runtime}" if summary.runtime else ""),
        f"Exit code:          {summary.exit_code}",
        "",
        f"Frozen app launch:  {launch_check}",
        "Live MSFS 2020:     NOT VERIFIED by this build. No simulator connection was made.",
        "                    See LIMITATIONS.md for everything that is still unverified.",
        "",
        "Full runner output: TEST_OUTPUT.txt",
        "",
        "Installed build dependencies (pip freeze):",
    ]
    lines += [f"  {line}" for line in dependencies.strip().splitlines() if line.strip()] or ["  unknown"]
    return "\n".join(lines) + "\n"


def read_test_status(text: str) -> str | None:
    match = re.search(r"^Result:\s+(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# ZIP writing and verification


def _zip_info(name: str, source: Path | None = None) -> zipfile.ZipInfo:
    # Fixed timestamps keep the archive reproducible; content hashes identify it.
    info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    executable = source is not None and source.suffix.casefold() in (".exe", ".dll", ".pyd")
    info.external_attr = (0o755 if executable else 0o644) << 16
    return info


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_zip(zip_path: Path, entries: Sequence[tuple[str, Path]], *, manifest: bool = True) -> None:
    """Write ``entries`` (archive name, source file) to ``zip_path``.

    The archive is written to a temporary name and renamed, so a failed build
    never leaves a partial ZIP that looks finished. With ``manifest`` a
    ``MANIFEST.sha256`` listing every entry is added under the common root.
    """

    zip_path = Path(zip_path)
    names = [name for name, _ in entries]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError("duplicate archive entries: " + ", ".join(duplicates))
    temporary = zip_path.with_name(zip_path.name + ".partial")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, source in sorted(entries):
                source = Path(source)
                with open(source, "rb") as handle, archive.open(_zip_info(name, source), "w") as target:
                    digest = hashlib.sha256()
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                        target.write(block)
                lines.append(f"{digest.hexdigest()}  {name}")
            if manifest and names:
                root = names[0].split("/", 1)[0]
                archive.writestr(_zip_info(f"{root}/{MANIFEST_NAME}"), "\n".join(lines) + "\n")
        os.replace(temporary, zip_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def iter_files(folder: Path) -> Iterator[Path]:
    for path in sorted(Path(folder).rglob("*")):
        if path.is_file():
            yield path


def portable_entries(app_dir: Path, docs: dict[str, Path], root_name: str) -> list[tuple[str, Path]]:
    """Map the PyInstaller onedir folder and documents to portable ZIP entries."""

    app_dir = Path(app_dir)
    entries = [(f"{root_name}/{APP_NAME}/{path.relative_to(app_dir).as_posix()}", path) for path in iter_files(app_dir)]
    entries += [(f"{root_name}/{name}", Path(path)) for name, path in docs.items()]
    return entries


def _verify_manifest(archive: zipfile.ZipFile, root: str) -> list[str]:
    name = f"{root}/{MANIFEST_NAME}"
    try:
        text = archive.read(name).decode("utf-8")
    except KeyError:
        return [f"{MANIFEST_NAME} is missing"]
    problems = []
    listed = {}
    for line in text.splitlines():
        digest, _, entry = line.partition("  ")
        listed[entry] = digest
    actual = {info.filename for info in archive.infolist() if not info.is_dir()} - {name}
    for entry in sorted(actual - set(listed)):
        problems.append(f"{entry} is not listed in {MANIFEST_NAME}")
    for entry in sorted(set(listed) - actual):
        problems.append(f"{MANIFEST_NAME} lists {entry}, which is not in the archive")
    for entry in sorted(actual & set(listed)):
        digest = hashlib.sha256(archive.read(entry)).hexdigest()
        if digest != listed[entry]:
            problems.append(f"{entry} does not match its {MANIFEST_NAME} checksum")
    return problems


def _verify_archive(zip_path: Path, root: str, rule, required: Sequence[str]) -> tuple[list[str], zipfile.ZipFile | None]:
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"{zip_path} is not a readable ZIP: {exc}"], None
    problems = []
    bad = archive.testzip()
    if bad:
        problems.append(f"{bad} fails its CRC check")
    names = [info.filename for info in archive.infolist()]
    if len(names) != len(set(names)):
        problems.append("the archive has duplicate entries")
    for name in names:
        if not name.startswith(root + "/"):
            problems.append(f"{name} is outside the {root}/ folder")
            continue
        reason = rule(name[len(root) + 1:].rstrip("/")) if name != root + "/" else None
        if reason:
            problems.append(f"{name} {reason}")
    present = set(names)
    for item in required:
        if f"{root}/{item}" not in present:
            problems.append(f"{root}/{item} is missing")
    problems += _verify_manifest(archive, root)
    return problems, archive


def verify_portable_zip(zip_path: Path, version: str) -> list[str]:
    """Return every reason the portable ZIP is unfit to deliver (empty when fine)."""

    root = f"{APP_NAME}-{safe_version_label(version)}"
    required = [f"{APP_NAME}/{EXE_NAME}", *PORTABLE_DOCS]
    problems, archive = _verify_archive(Path(zip_path), root, portable_problem, required)
    if archive is None:
        return problems
    with archive:
        names = archive.namelist()
        if not any(n.startswith(f"{root}/{APP_NAME}/_internal/") for n in names):
            problems.append(f"{root}/{APP_NAME}/_internal/ (the PyInstaller runtime folder) is missing")
        try:
            status = read_test_status(archive.read(f"{root}/TEST_STATUS.txt").decode("utf-8"))
        except KeyError:
            status = None
        if status is not None and status != STATUS_PASSED:
            problems.append(f"TEST_STATUS.txt says {status}, so this build must not be delivered")
        try:
            changelog = archive.read(f"{root}/CHANGELOG.md").decode("utf-8")
            if CHANGELOG_TEMPLATE_MARKER in changelog or version not in changelog:
                problems.append("CHANGELOG.md is the unfilled template or has no entry for this version")
        except KeyError:
            pass
    return problems


def verify_source_zip(zip_path: Path, version: str) -> list[str]:
    """Return every reason the source ZIP is unfit to deliver (empty when fine)."""

    root = f"{APP_NAME}-{safe_version_label(version)}-source"
    problems, archive = _verify_archive(Path(zip_path), root, source_problem, REQUIRED_SOURCE_FILES)
    if archive is not None:
        archive.close()
    return problems


# ---------------------------------------------------------------------------
# Source listing


def parse_git_ls_files(output: bytes) -> list[str]:
    """Split ``git ls-files -z`` output into POSIX relative paths."""

    return sorted({item.decode("utf-8") for item in output.split(b"\0") if item})


def git_source_files(root: Path) -> list[str] | None:
    """Return tracked and untracked-but-not-ignored files, or None without Git."""

    try:
        completed = subprocess.run(
            ["git", "-c", "core.quotepath=off", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_git_ls_files(completed.stdout)


def walk_source_files(root: Path) -> list[str]:
    """Fallback listing when Git is unavailable: every file, minus obvious junk."""

    root = Path(root)
    skip_dirs = {".git", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache",
                 ".mypy_cache", ".ruff_cache", "tests_wip"}
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs and not d.startswith(".concept-deps-"))
        for filename in sorted(filenames):
            files.append((Path(dirpath) / filename).relative_to(root).as_posix())
    return files


@dataclass(frozen=True, slots=True)
class SourceSelection:
    included: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def select_source_files(root: Path, candidates: Iterable[str]) -> SourceSelection:
    """Split candidate paths into those that ship and those left out (with reasons)."""

    included, excluded = [], []
    for relative in sorted(set(candidates)):
        if relative == ".git" or relative.startswith(".git/"):
            continue
        reason = source_problem(relative)
        if reason is None and not (Path(root) / relative).is_file():
            reason = "is listed by Git but missing from the working tree"
        if reason:
            excluded.append((relative, reason))
        else:
            included.append(relative)
    return SourceSelection(tuple(included), tuple(excluded))


def git_state(root: Path) -> tuple[str | None, bool | None]:
    """Return (commit, dirty) for the working tree; unknown values are None."""

    def run(*args):
        try:
            completed = subprocess.run(["git", *args], cwd=str(root), stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, check=False, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return completed.stdout.decode("utf-8", errors="replace") if completed.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    if not commit:
        return None, None
    status = run("status", "--porcelain", "--untracked-files=normal")
    return commit.strip(), (None if status is None else bool(status.strip()))


# ---------------------------------------------------------------------------
# Command line


def check_interpreter(implementation: str, bits: int, version: tuple) -> tuple[list[str], list[str]]:
    """Return (problems, warnings) for the interpreter that will build the EFB."""

    problems, warnings = [], []
    if implementation != "CPython":
        problems.append(f"{implementation} is not supported; use 64-bit CPython from python.org")
    if bits != 64:
        problems.append(f"this Python is {bits}-bit; MSFS 2020 and SimConnect.dll are 64-bit only")
    if tuple(version[:2]) < (3, 10):
        problems.append(f"Python {version[0]}.{version[1]} is too old; the code needs 3.10 or later")
    elif tuple(version[:2]) != TESTED_PYTHON:
        warnings.append(f"Python {version[0]}.{version[1]} is supported but not the tested "
                        f"{TESTED_PYTHON[0]}.{TESTED_PYTHON[1]}; TEST_STATUS.txt records the version used")
    return problems, warnings


def pip_freeze(python: str) -> str:
    """Return ``pip freeze --all`` for ``python``, or an explanation when it fails."""

    try:
        completed = subprocess.run([python, "-m", "pip", "freeze", "--all", "--disable-pip-version-check"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unknown (pip freeze could not run: {exc})"
    text = completed.stdout.decode("utf-8", errors="replace")
    return text if completed.returncode == 0 else f"unknown (pip freeze exited {completed.returncode})"


def _python_description() -> str:
    return f"CPython {platform.python_version()} {struct.calcsize('P') * 8}-bit"


def _platform_description() -> str:
    return f"{platform.system()} {platform.release()} ({platform.version()}), {platform.machine()}"


def _print_problems(problems: Sequence[str], heading: str) -> int:
    if not problems:
        return 0
    print(heading, file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="package_tools.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, *arguments):
        sub = commands.add_parser(name)
        for spec in arguments:
            flags, options = spec
            sub.add_argument(*flags, **options)
        return sub

    root_arg = (("--root",), {"type": Path, "default": Path(__file__).resolve().parent.parent})
    version_arg = (("--version",), {"dest": "app_version"})
    add("version", root_arg)
    add("check-interpreter")
    add("check-version-info", root_arg)
    add("check-entry-point", root_arg)
    add("check-changelog", root_arg, (("path",), {"type": Path}), version_arg)
    add("run-tests", root_arg, (("--python",), {"default": sys.executable}), (("--output",), {"type": Path, "required": True}),
        (("--summary",), {"type": Path}))
    add("test-status", root_arg, (("--test-output",), {"type": Path, "required": True}),
        (("--exit-code",), {"type": int, "required": True}), (("--freeze",), {"type": Path}),
        (("--pip-freeze",), {"action": "store_true"}),
        (("--launch-check",), {"default": "not run"}), (("--out",), {"type": Path, "required": True}),
        (("--python-description",), {}), version_arg)
    add("assemble-portable", root_arg, (("--app-dir",), {"type": Path, "required": True}),
        (("--out",), {"type": Path, "required": True}), (("--readme",), {"type": Path, "required": True}),
        (("--limitations",), {"type": Path, "required": True}), (("--changelog",), {"type": Path, "required": True}),
        (("--test-status",), {"type": Path, "required": True}), (("--test-output",), {"type": Path, "required": True}),
        (("--notices",), {"type": Path, "required": True}), (("--modules",), {"type": Path, "required": True}),
        version_arg)
    add("source-zip", root_arg, (("--out",), {"type": Path, "required": True}), version_arg,
        (("--no-git",), {"action": "store_true"}))
    add("verify-portable", root_arg, (("zip",), {"type": Path}), version_arg)
    add("verify-source", root_arg, (("zip",), {"type": Path}), version_arg)
    add("sha256sums", (("--out",), {"type": Path, "required": True}), (("files",), {"type": Path, "nargs": "+"}))
    add("check-wheel", root_arg, (("wheel",), {"type": Path}), version_arg)
    args = parser.parse_args(argv)

    root = Path(getattr(args, "root", ".")).resolve()
    version = getattr(args, "app_version", None)
    if version is None and args.command not in ("sha256sums", "check-interpreter"):
        version = read_app_version(root)

    if args.command == "version":
        print(version)
        return 0
    if args.command == "check-interpreter":
        problems, warnings = check_interpreter(platform.python_implementation(), struct.calcsize("P") * 8,
                                               tuple(sys.version_info[:3]))
        for warning in warnings:
            print(f"Note: {warning}")
        if not problems:
            print(f"Using {_python_description()} at {sys.executable}")
        return _print_problems(problems, "This Python cannot build the BBC EFB:")
    if args.command == "check-version-info":
        return _print_problems(check_version_info(root / "packaging" / "version_info.txt", version),
                               "packaging/version_info.txt does not match the application version:")
    if args.command == "check-entry-point":
        return _print_problems(check_entry_point(root), "The EFB entry point is not ready:")
    if args.command == "check-changelog":
        return _print_problems(check_changelog(args.path, version), "The changelog cannot ship with this build:")
    if args.command == "run-tests":
        summary = run_offline_tests(root, args.python, args.output)
        if args.summary:
            args.summary.write_text(json.dumps({"status": summary.status, "exit_code": summary.exit_code,
                                                "counts": summary.describe()}) + "\n", encoding="utf-8")
        print(f"Offline suite {summary.status}: {summary.describe()}")
        return 0 if summary.passed else 1
    if args.command == "test-status":
        text = args.test_output.read_text(encoding="utf-8")
        summary = parse_unittest_output(text, args.exit_code)
        commit, dirty = git_state(root)
        if args.pip_freeze:
            freeze = pip_freeze(sys.executable)
        elif args.freeze and args.freeze.is_file():
            freeze = args.freeze.read_text(encoding="utf-8-sig")
        else:
            freeze = ""
        args.out.write_text(render_test_status(
            summary, version=version, commit=commit, dirty=dirty,
            python_version=args.python_description or _python_description(),
            platform_name=_platform_description(), dependencies=freeze, launch_check=args.launch_check,
        ), encoding="utf-8")
        print(f"Wrote {args.out} ({summary.status})")
        return 0 if summary.passed else 1
    if args.command == "assemble-portable":
        root_name = f"{APP_NAME}-{safe_version_label(version)}"
        if not (args.app_dir / EXE_NAME).is_file():
            return _print_problems([f"{args.app_dir / EXE_NAME} was not built"], "Cannot assemble the portable ZIP:")
        docs = {"README.md": args.readme, "LIMITATIONS.md": args.limitations, "CHANGELOG.md": args.changelog,
                "TEST_STATUS.txt": args.test_status, "TEST_OUTPUT.txt": args.test_output,
                "THIRD_PARTY_NOTICES.md": args.notices, "BUNDLED_MODULES.txt": args.modules}
        missing = [f"{name} source {path} does not exist" for name, path in docs.items() if not Path(path).is_file()]
        if missing:
            return _print_problems(missing, "Cannot assemble the portable ZIP:")
        entries = portable_entries(args.app_dir, docs, root_name)
        refused = [f"{name} {reason}" for name, _ in entries
                   if (reason := portable_problem(name[len(root_name) + 1:]))]
        if refused:
            return _print_problems(refused, "The PyInstaller output contains files that must not ship:")
        write_zip(args.out, entries)
        print(f"Wrote {args.out} ({len(entries)} files)")
        return 0
    if args.command == "source-zip":
        candidates = None if args.no_git else git_source_files(root)
        method = "git ls-files (tracked and untracked, not ignored)"
        if candidates is None:
            candidates, method = walk_source_files(root), "folder walk (Git unavailable)"
        selection = select_source_files(root, candidates)
        for relative, reason in selection.excluded:
            print(f"Left out of the source ZIP: {relative} ({reason})")
        root_name = f"{APP_NAME}-{safe_version_label(version)}-source"
        write_zip(args.out, [(f"{root_name}/{relative}", root / relative) for relative in selection.included])
        print(f"Wrote {args.out} ({len(selection.included)} files, listed by {method})")
        return 0
    if args.command == "verify-portable":
        return _print_problems(verify_portable_zip(args.zip, version), f"{args.zip.name} failed verification:")
    if args.command == "verify-source":
        return _print_problems(verify_source_zip(args.zip, version), f"{args.zip.name} failed verification:")
    if args.command == "check-wheel":
        problems = wheel_problems(args.wheel, root, version)
        if not problems:
            print(f"{args.wheel.name} holds {', '.join(app_package_names(root))} at version {version}")
        return _print_problems(problems, f"{args.wheel.name} is incomplete:")
    if args.command == "sha256sums":
        lines = [f"{sha256_file(path)}  {path.name}" for path in args.files]
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 0
    parser.error(f"unknown command {args.command}")  # pragma: no cover
    return 2


if __name__ == "__main__":
    sys.exit(main())
