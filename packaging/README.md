# Building the BBC EFB portable Windows delivery

This folder turns the repository into the portable delivery described in [Architecture](../sources/02_ARCHITECTURE.md). The delivery is a portable ZIP of the whole EFB, with launch instructions, a changelog, the limitations and the actual test status. A full-source ZIP beside it makes the build reproducible.

> **Approvals.** Running the build installs the dependencies pinned in `requirements-build.txt` and produces a build. Both need the owner's approval under [AGENTS.md](../AGENTS.md). PyInstaller as the freezer and the dependency pins are **proposals** until the owner approves them. Distributing the result beyond internal review needs a separate decision. See the owner proposals below.

## What you need

- Windows 10 or 11, 64-bit.
- 64-bit CPython 3.10 or later from python.org, with the `py` launcher. 3.12 is the tested version.
- Windows PowerShell 5.1 or PowerShell 7.
- Internet access to PyPI for the pinned packages, or a folder of the same wheels (see `-WheelDir`).
- Git (optional). It records the source commit in `TEST_STATUS.txt` and lists the files for the source ZIP.
- No simulator, MSFS SDK or `SimConnect.dll` is needed to build.

## Build steps

1. Commit your changes. The build refuses a working tree with uncommitted changes unless you pass `-AllowDirty`. With `-AllowDirty` it records that the build includes uncommitted changes.
2. Write the changelog. Copy `packaging/CHANGELOG_TEMPLATE.md` to `CHANGELOG.md` in the repository root. Add an entry for the version in `bbc_efb/__init__.py`, replace every `TEMPLATE-PLACEHOLDER` line and delete the instruction comment.
3. Check the version. `VERSION` in `bbc_efb/__init__.py` is the only version source: `pyproject.toml` reads it, and `packaging/version_info.txt` must match it (the four numbers and both version strings, for example `0.1.0a1` and `(0, 1, 0, 1)`). The spec refuses to build otherwise.
4. From a PowerShell prompt in the repository folder, run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\packaging\build_portable.ps1
   ```

   `-ExecutionPolicy Bypass` applies to this one process only. It doesn't change the machine's policy.

5. Collect the delivery from `dist\BBC-EFB-<version>\`.

### What the script does

| Step | Detail | Stops the build when |
|---|---|---|
| Checks | Windows x64, 64-bit CPython 3.10 or later, `bbc_efb/__main__.py` defines `main()`, version resource, changelog, Git state. The spec also needs `bbc_core`, `bbc_efb` and `bbc_recorder`, and an `__init__.py` in every code folder (the same rule `pyproject.toml` uses for installs) | Any check fails |
| Environment | Recreates `build\package-venv`, installs `requirements-build.txt` with `--no-deps --only-binary=:all:`, then runs `pip check` | Install fails or dependencies conflict |
| Tests | Runs `python -B tests/test_repo_offline_guard.py` with that environment (`QT_QPA_PLATFORM=offscreen`, no bytecode written). Writes `build\package-work\TEST_STATUS.txt` and `TEST_OUTPUT.txt` | Any test fails or errors, nothing ran, or the offline guard blocked anything. **Nothing is packaged.** |
| Freeze | `python -m PyInstaller packaging\bbc-efb.spec` (onedir, windowed, no UPX) into `build\pyinstaller-dist\BBC-EFB` | The spec finds test code, `SimConnect.dll`, logs or probe outputs in the build, or PyInstaller fails |
| Launch check (optional) | `-LaunchCheck` starts `BBC-EFB.exe` and checks it is still running after `-LaunchCheckSeconds` (default 10) | The program exits early |
| Package | Writes the portable and source ZIPs, each with a `MANIFEST.sha256` inside | — |
| Verify | Re-opens both ZIPs and checks every entry: CRCs, manifest checksums, required files, `TEST_STATUS.txt` says PASSED, and none of the forbidden content listed below | Anything fails. The delivery folder is deleted so a bad ZIP can't be shared by mistake. |
| Checksums | `SHA256SUMS.txt` for both ZIPs and `TEST_STATUS.txt` | — |

### Options

| Option | Effect |
|---|---|
| `-PythonVersion 3.11` | Python version passed to the `py` launcher (default `3.12`) |
| `-PythonExe C:\Python312\python.exe` | Use this interpreter instead of the `py` launcher |
| `-Changelog path\to\CHANGELOG.md` | Use a changelog other than `CHANGELOG.md` in the root |
| `-WheelDir path\to\wheels` | Install offline from pre-downloaded wheels (`pip --no-index --find-links`) |
| `-ReuseVenv` | Keep `build\package-venv` (still reinstalls the pins and runs `pip check`) |
| `-AllowDirty` | Build from a working tree with uncommitted changes (recorded) |
| `-ExcludeSimulated` | Leave `bbc_core.simulated` out of the frozen app. Refused while production code imports it. Today `bbc_efb/app.py` imports it for `--simulated`. |
| `-LaunchCheck`, `-LaunchCheckSeconds N` | Start the frozen program once |

To prepare `-WheelDir` on a connected machine, run `py -3.12 -m pip download --only-binary=:all: --no-deps --platform win_amd64 -r packaging\requirements-build.txt -d wheels`.

## The delivery

```
dist\BBC-EFB-<version>\
  BBC-EFB-<version>-win64-portable.zip   the program and its documents
  BBC-EFB-<version>-source.zip           full source, tests and build scripts
  TEST_STATUS.txt                        same file as inside the portable ZIP
  SHA256SUMS.txt
```

Portable ZIP layout:

```
BBC-EFB-<version>/
  BBC-EFB/BBC-EFB.exe, BBC-EFB/_internal/...   PyInstaller onedir output
  README.md               from packaging/PORTABLE_README.md
  LIMITATIONS.md          from packaging/LIMITATIONS.md
  CHANGELOG.md            the checked changelog
  TEST_STATUS.txt         result, counts, commit, Python, pip freeze, launch check
  TEST_OUTPUT.txt         full test runner output
  THIRD_PARTY_NOTICES.md  from packaging/THIRD_PARTY_NOTICES.md
  BUNDLED_MODULES.txt     the application modules frozen into the program
  MANIFEST.sha256         checksum of every entry
```

### Never in the portable ZIP

These rules are checked by `package_tools.py`, both in the spec (for PyInstaller's module, binary and data lists) and on the finished ZIP:

- `tests/`, `tests_wip/`, test modules (`test_*.py`), fixtures and `test_artifacts/` evidence folders.
- Logs (`*.log`, rotated `*.log.N`, `logs/`), `unittest.txt`, JSONL journals and recordings, `in-progress/` folders.
- Probe outputs (`core_live_readonly_*`, anything named `*probe*` or `*preflight*`).
- Python bytecode caches (`__pycache__/`, `*.pyc`, `*.pyo`) from the development tree. PyInstaller keeps the application's compiled code inside `BBC-EFB.exe`.
- `SimConnect.dll`.
- Virtual environments, tool caches, credential and key files, and nested archives (except PyInstaller's own `base_library.zip`).

The source ZIP lists files with `git ls-files --cached --others --exclude-standard`, so `.gitignore` applies. If Git isn't available it walks the folder instead. It leaves out `tests_wip/`, logs, bytecode, build output, native binaries and nested archives such as the historical `bbc-efb-source-*.zip`. Each file left out is printed with the reason. Committed test evidence under `reference/` is kept, because it is the project's history.

## SimConnect.dll in the frozen build

`SimConnect.dll` is never bundled, because redistribution permission hasn't been established (see `reference/EXTERNAL_DEPENDENCIES_NOT_BUNDLED.md`). The frozen EFB calls `bbc_core.simconnect_sdk.find_simconnect_dll()`, which tries these locations in order:

1. An explicit path: `--simconnect-dll PATH`, or the `simconnect.dll_path` preference set in Settings.
2. PyInstaller's bundle folder (`sys._MEIPASS`, the `_internal` folder). The build leaves this empty.
3. The program folder, next to `BBC-EFB.exe`. Pilots can copy their own SDK DLL here.
4. `%MSFS_SDK%\SimConnect SDK\lib\SimConnect.dll`. The MSFS SDK installer sets `MSFS_SDK`. The default install path is `C:\MSFS SDK\`.

If the owner later approves redistribution, the change is deliberate and in two places: add the DLL to `binaries` in `bbc-efb.spec`, and remove the `simconnect.dll` rule from `portable_problem()` in `package_tools.py`. Record the approval, the DLL's SDK version and its SHA-256 in `reference/EXTERNAL_DEPENDENCIES_NOT_BUNDLED.md`.

## Checking the packaging offline

The rules can be checked on any platform without PowerShell or PyInstaller:

```
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t . -p "test_packaging_*.py"
python -I packaging/package_tools.py check-version-info
python -I packaging/package_tools.py check-entry-point
python -m pip wheel --no-deps --wheel-dir build/wheel .
python -I packaging/package_tools.py check-wheel build/wheel/bbc_efb-<version>-py3-none-any.whl
```

`check-wheel` confirms that a wheel built from `pyproject.toml` holds every package in the tree (`bbc_core`, `bbc_core.ipc`, `bbc_efb`, `bbc_efb.logbook_views`, `bbc_efb.modules`, `bbc_recorder`), every production module and the `bbc_efb.VERSION` version, and no tests. CI runs it on every job. Two packaging tests start Git or Python as a subprocess; the guarded runner blocks subprocesses, so those two skip under it and run under the plain `unittest` command.

The tests run the spec against stand-ins for PyInstaller's `Analysis`, `PYZ`, `EXE` and `COLLECT`, check the rules against real and synthetic ZIPs, and check that the script and spec refer only to files that exist. The spec and script themselves have **not yet been run on Windows**. The first real build is the remaining check.

`packaging/` deliberately has no `__init__.py`. `packaging` is also the name of a PyPI library that pip, setuptools and PyInstaller import. The build therefore runs pip and PyInstaller from `build\package-work`, never from the repository root, and `package_tools.py` is always loaded by its file path.

## Owner proposals

These are defaults chosen so the build can run. Each needs an owner decision.

1. **Freezer.** PyInstaller 6.11.1, onedir (a folder, not a single self-extracting exe), windowed, no UPX. Onedir starts faster, avoids temporary-folder extraction that antivirus tools often flag, and keeps Qt replaceable as the LGPL requires.
2. **Dependency pins.** Use the exact versions in `requirements-build.txt`, installed with `--no-deps`. Add `--hash` pins on the build machine next.
3. **Two ZIPs per delivery.** The portable ZIP holds the program and documents, with no tests or development evidence, as the readiness checklist requires. The full-source ZIP, with tests, holds the "whole buildable EFB". The alternative is one ZIP with a `source/` folder, but that would put `tests/` inside the program package.
4. **Simulated provider.** It ships as code because `--simulated` uses it, and its output is always labelled SIMULATED. The readiness checklist asks for deployed files "free of simulated providers". If that also covers code, remove the `--simulated` import path and build with `-ExcludeSimulated`.
5. **Clean tree required.** A delivery must match a Git commit unless `-AllowDirty` is passed, and then `TEST_STATUS.txt` says so.
6. **Changelog required.** No delivery without a filled-in changelog entry for the current version.
7. **Version resource.** `CompanyName` "BBC EFB project", and `LegalCopyright` left as "to be confirmed by the project owner". No icon yet: add `packaging/bbc-efb.ico` and the spec uses it automatically.
8. **Code signing.** None yet. Signing would remove SmartScreen warnings but needs a certificate.
9. **Licence texts.** Add the LGPL-3.0 and GPL-3.0 texts under `licenses\` in the ZIP before any distribution beyond internal review.
