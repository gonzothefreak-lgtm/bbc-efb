# Developing BBC EFB

This guide covers setting up a development copy, running the tests and finding your way around the code. Read [AGENTS.md](../AGENTS.md) for the approval rules first. Code, tests, dependency choices, builds, native DLL use and simulator connections each need the owner's approval.

## Requirements

- 64-bit CPython **3.10 or later**. **3.12** with **PySide6 6.8.3** is the tested combination. The code uses `dataclass(slots=True)`, `zip(strict=True)` and `X | None` annotations, so 3.9 won't work.
- Black Box Core (`bbc_core`) and the Flight Recorder (`bbc_recorder`) use the standard library only.
- The EFB user interface (`bbc_efb`) needs PySide6, which is the `ui` extra in `pyproject.toml`, pinned to the 6.8 series.
- A live simulator connection needs Windows, MSFS 2020 and the pilot's own `SimConnect.dll` from the MSFS SDK. Nothing in development or the offline tests needs them.

## Set up on Windows

From PowerShell in the repository folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[ui]"
```

If activation is blocked by the execution policy, run `Set-ExecutionPolicy -Scope Process Bypass` in that window first. It applies to that window only.

Run the EFB from source:

```powershell
python -m bbc_efb                  # or the installed launcher: bbc-efb
python -m bbc_efb --simulated      # development data only, labelled SIMULATED
python -m bbc_efb --data-dir "$env:TEMP\bbc-efb-dev" --log-level DEBUG
```

To connect to MSFS, the EFB looks for `SimConnect.dll` in this order: `--simconnect-dll PATH` or the Settings path, the program folder, then `%MSFS_SDK%\SimConnect SDK\lib\SimConnect.dll`. Connecting to the simulator is a **live test** and needs separate approval.

## Set up on macOS (or Linux)

macOS is fine for Core, Recorder and EFB development and for every offline test. It can't load `SimConnect.dll` or connect to MSFS. Windows-only tests skip themselves.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[ui]"
python -m bbc_efb --simulated
```

The EFB window opens normally on macOS. The SimConnect status says that no DLL was found, which is the honest result there. On Linux, Qt needs a few system libraries. The CI workflow (`.github/workflows/ci.yml`) lists them for Ubuntu.

## Run the tests

The offline suite needs no simulator, `SimConnect.dll` or network access.

```powershell
# Windows PowerShell
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest discover -s tests -t . -p "test_*.py"
python -B tests/test_repo_offline_guard.py     # the guarded run used by CI and the build
```

```bash
# macOS / Linux
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t . -p "test_*.py"
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python -B tests/test_repo_offline_guard.py
```

- **Offscreen Qt.** `QT_QPA_PLATFORM=offscreen` lets the widget tests create windows without a display. Use it on CI, over SSH, and whenever you don't want windows to flash up. Leave it unset to see the real window.
- **The offline guard.** `tests/test_repo_offline_guard.py` installs a Python audit hook before discovering the tests. The hook blocks native DLL loads, sockets and subprocesses. The runner prints `Offline guard violations: N` and fails when N isn't 0. It discovers the tests with the repository root as the top level, the same as `-t .`, and sets `BBC_OFFLINE_GUARD=1` while it runs. Any test that needs one of those resources belongs to a separately approved live or Windows-integration check, not this suite. The only exceptions are two packaging tests that start Git or Python as a subprocess: they skip under the guard and run under the plain command.
- **Bytecode.** `-B` or `PYTHONDONTWRITEBYTECODE=1` keeps `__pycache__` folders out of the tree. They are ignored by Git and refused by the packaging checks either way.
- **One file or one test.** Use `python -m unittest tests.test_efb_storage` or `python -m unittest tests.test_efb_storage.StorageTests.test_name`. To run one file under the guard, use `python -B tests/test_repo_offline_guard.py "test_efb_*.py"`.
- **Work-in-progress tests.** `tests_wip/` holds tests for parts still being integrated. Run them with `python -m unittest discover -s tests_wip -p "test_*.py"` (the folder has no `__init__.py`, so `-t .` does not work there). They aren't part of the guarded suite, aren't packaged and are left out of the source ZIP. Move a test into `tests/` once its code is integrated.
- **CI.** The GitHub Actions workflow is configured for Windows and Ubuntu with Python 3.10, 3.11 and 3.12. Each job builds a wheel, checks it with `python -I packaging/package_tools.py check-wheel`, installs it (not editable) and imports every package from outside the repository, then runs the plain and the guarded suite. It has not run on GitHub yet.
- **Packages and version.** `pyproject.toml` finds packages with `[tool.setuptools.packages.find]` and namespace packages off, so every code folder needs an `__init__.py`; `tests/test_repo_packaging.py` fails when one is missing or left out. The version comes only from `VERSION` in `bbc_efb/__init__.py` (PEP 440, for example `0.1.0a1`). When it changes, update `packaging/version_info.txt` and the changelog template too; the tests and the build spec check both.

## Project layout

| Path | What it holds |
|---|---|
| `bbc_core/` | **Black Box Core.** It owns the single SimConnect session (`ctypes`, standard library only) and the versioned in-process `ConsumerAPI` (`core.py`, `contract.py`). It also holds the starter catalog (`catalog.py`), general SimVars (`sdk_simvar.py`), facilities, aircraft inputs, Client Data and simulator operations. Core is independent of Qt and of the Recorder. |
| `bbc_core/simconnect_sdk.py`, `simconnect_abi.py` | The native boundary: DLL discovery (`find_simconnect_dll`), signatures and dispatch |
| `bbc_core/simulated.py` | The simulated provider for development and tests. Everything it produces is labelled simulated. |
| `bbc_core/host.py` | `CoreHost`: the worker thread that owns Core and the SDK. It connects, pumps, and reconnects with backoff. |
| `bbc_core/ipc/` | Groundwork for the later standalone Core process and its local interface (not used by the first build) |
| `bbc_recorder/` | **Flight Recorder.** A pure state machine with no Qt and no SimConnect. It is fed by Core snapshots and produces flight events (`model.py`, `state_machine.py`, `rules.py`). It also holds the crash-safe journal (`journal.py`), interruption recovery (`recovery.py`) and the service that ties them together (`service.py`). |
| `bbc_efb/` | **The EFB application** (PySide6). Entry point `__main__.py` (`python -m bbc_efb`, launcher `bbc-efb`), start-up `app.py`, window shell `shell.py` and `sections.py`, permanent footer statuses `footer.py`, and the Qt bridge to `CoreHost` in `core_bridge.py` |
| `bbc_efb/logbook.py`, `logbook_views/` | Logbook dashboard, history, flight details and charts |
| `bbc_efb/storage.py`, `STORAGE_SCHEMA.md` | Local JSON flight storage: schema, provenance and corrections, writer lock |
| `bbc_efb/storage_settings.py`, `preferences.py`, `data_management.py` | Data folder choice, preferences, export and backup |
| `bbc_efb/theme.py`, `widgets.py` | Visual design tokens and shared widgets |
| `bbc_efb/logging_setup.py` | Rotating log file and crash hooks |
| `bbc_efb/modules/` | Drop-in module discovery (scheduled after the first build) |
| `tests/` | The offline test suite, run by CI and by the build |
| `tests_wip/` | Work-in-progress tests, not packaged |
| `packaging/` | Portable Windows build: PyInstaller spec, `build_portable.ps1`, shared rules in `package_tools.py`, pinned build dependencies, and the documents shipped in the ZIP. See [packaging/README.md](../packaging/README.md). |
| `docs/` | Developer documentation (this file) |
| `sources/` | The owner's requirement and decision records (`01`-`10`) and the two illustrated Word design masters |
| `reference/` | Preserved history: original instructions, masters, test evidence and UI mock-ups (`04_REFERENCE_ASSETS/`). Read-only, don't rewrite it. |
| `pyproject.toml` | Package metadata, the Python floor, the `ui` extra and the `bbc-efb` launcher |
| `publish.ps1` | Checked publishing to the private GitHub repository (owner approval required) |

Some modules in this table are being integrated in parallel and may not all be present in every checkout yet.

### Boundaries to keep

- `bbc_core` never imports Qt, `bbc_efb` or `bbc_recorder`.
- `bbc_recorder` never imports Qt or anything SimConnect-specific. It takes snapshots and produces events, so it can be tested with recorded flight tapes.
- Only `CoreHost`'s thread touches the SimConnect handle. The UI receives updates through `CoreBridge` signals and never calls the provider directly.
- Unknown values stay unknown and are labelled as such. Simulated data is always labelled SIMULATED.

## Building the Windows package

See [packaging/README.md](../packaging/README.md). In short, on Windows, after committing and writing `CHANGELOG.md`:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_portable.ps1
```

The script creates its own environment, runs the guarded suite, and refuses to package if anything fails. It then freezes the EFB with PyInstaller and writes verified ZIPs to `dist\`. `packaging/` has no `__init__.py`, because a folder called `packaging` would otherwise shadow the PyPI `packaging` library that pip and PyInstaller use. Don't add one.

## Logs while developing

The EFB writes `bbc-efb.log` and `fault.log` to `logs` in the data folder. Until a data folder is chosen, they go to `%LOCALAPPDATA%\BBC EFB\logs` on Windows and `~/Library/Logs/BBC EFB` on macOS. Logs are ignored by Git and refused by the packaging checks. Don't commit raw simulator logs.
