# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the BBC EFB portable Windows build (onedir, windowed).

Run it through packaging/build_portable.ps1, which creates a clean virtual
environment, installs the pinned dependencies, runs the offline tests and only
then calls PyInstaller. Running it by hand:

    python -m PyInstaller --noconfirm --clean packaging/bbc-efb.spec

Rules this spec enforces (it stops the build with a plain message otherwise):

* The entry point is bbc_efb.__main__:main, the same target as the bbc-efb
  launcher in pyproject.toml. packaging/launch_bbc_efb.py calls it so relative
  imports inside bbc_efb/__main__.py keep working when frozen.
* Every production module of every top-level bbc_* package (bbc_core,
  bbc_efb and bbc_recorder, with their subpackages such as bbc_core.ipc,
  bbc_efb.logbook_views and bbc_efb.modules) is frozen, so code loaded by name
  at run time is present. Each code folder must have an __init__.py, the same
  rule pyproject.toml applies to installs. Test suites, work-in-progress tests,
  fixtures, logs and probe outputs are never collected.
* SimConnect.dll is never bundled. Redistribution has not been approved. The
  frozen EFB finds the pilot's own copy (see packaging/README.md).
* The simulated provider (bbc_core.simulated) ships only as code. Set
  BBC_EFB_EXCLUDE_SIMULATED=1 to leave it out; the build then refuses if any
  production module imports it.
* packaging/version_info.txt must match bbc_efb.VERSION, which is also the
  version pyproject.toml reads.
* No package data is collected except files under assets/ or resources/
  folders inside a package (there are none today). The Markdown notes beside
  the code are developer documentation and are not read at run time.

Environment variables:

    BBC_EFB_EXCLUDE_SIMULATED=1   leave bbc_core.simulated out of the build
    BBC_EFB_REPORT_DIR=<folder>   where BUNDLED_MODULES.txt is written
                                  (default: build/pyinstaller-report)
    BBC_EFB_ALLOW_NON_WINDOWS=1   allow running the spec on another platform,
                                  only for offline checks of this file
"""

import importlib.util
import os
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 - provided by PyInstaller
ROOT = SPEC_DIR.parent


def _load_tools():
    # The packaging folder is deliberately not a Python package (it would shadow
    # the PyPI "packaging" module that PyInstaller itself imports).
    spec = importlib.util.spec_from_file_location("bbc_package_tools", SPEC_DIR / "package_tools.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _refuse(heading, problems):
    lines = [heading] + [f"  - {problem}" for problem in problems]
    raise SystemExit("\n".join(lines))


def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


tools = _load_tools()

if sys.platform != "win32" and not _flag("BBC_EFB_ALLOW_NON_WINDOWS"):
    _refuse("The BBC EFB portable build targets Windows 10/11 x64:",
            [f"this machine reports {sys.platform}. Build on Windows with packaging/build_portable.ps1."])

APP_VERSION = tools.read_app_version(ROOT)
VERSION_FILE = SPEC_DIR / "version_info.txt"
EXCLUDE_SIMULATED = _flag("BBC_EFB_EXCLUDE_SIMULATED")
REPORT_DIR = Path(os.environ.get("BBC_EFB_REPORT_DIR") or ROOT / "build" / "pyinstaller-report")

problems = tools.check_entry_point(ROOT) + tools.check_version_info(VERSION_FILE, APP_VERSION)
PACKAGES = tools.discover_app_packages(ROOT)
for required in tools.REQUIRED_PACKAGES:
    if required not in PACKAGES:
        problems.append(f"the {required} package is missing from {ROOT}")
# pyproject.toml turns namespace packages off, so a code folder without an
# __init__.py would be missing from an installed copy. Refuse it here too, so
# the frozen build and the wheel always contain the same packages.
problems += [f"{folder} has code but no __init__.py, so pyproject.toml would leave it out of an install"
             for folder in tools.missing_package_inits(ROOT, PACKAGES)]
if EXCLUDE_SIMULATED:
    problems += [f"{path} imports the simulated provider, so it cannot be excluded"
                 for path in tools.simulated_importers(ROOT, PACKAGES)]
if problems:
    _refuse("The BBC EFB build cannot start:", problems)

HIDDEN_IMPORTS = tools.production_modules(ROOT, PACKAGES, exclude_simulated=EXCLUDE_SIMULATED)
PRODUCTION_IMPORTS = tools.imported_modules(tools.iter_production_files(ROOT, PACKAGES))
EXCLUDES = tools.build_excludes(PRODUCTION_IMPORTS, exclude_simulated=EXCLUDE_SIMULATED)
DATAS = tools.package_data_files(ROOT, PACKAGES)
ICON = SPEC_DIR / "bbc-efb.ico"

a = Analysis(  # noqa: F821 - provided by PyInstaller
    [str(SPEC_DIR / "launch_bbc_efb.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

problems = tools.check_analysis_tocs(a.pure, a.binaries, a.datas, exclude_simulated=EXCLUDE_SIMULATED)
if problems:
    _refuse("The frozen build would contain files that must not ship:", problems)

REPORT_DIR.mkdir(parents=True, exist_ok=True)
(REPORT_DIR / "BUNDLED_MODULES.txt").write_text(
    tools.bundled_modules_report(a.pure, APP_VERSION, exclude_simulated=EXCLUDE_SIMULATED), encoding="utf-8")

pyz = PYZ(a.pure)  # noqa: F821 - provided by PyInstaller

exe = EXE(  # noqa: F821 - provided by PyInstaller
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=tools.APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-packed executables trigger antivirus false positives; size is not worth it.
    upx=False,
    console=False,
    # A windowed build has no console; keep PyInstaller's crash dialog so an
    # unexpected start-up failure is visible to the pilot.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(VERSION_FILE),
    icon=str(ICON) if ICON.is_file() else None,
)

coll = COLLECT(  # noqa: F821 - provided by PyInstaller
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=tools.APP_NAME,
)
