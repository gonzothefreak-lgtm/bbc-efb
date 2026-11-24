# BBC Integrated EFB and Black Box Core

BBC EFB is a Windows companion app for Microsoft Flight Simulator 2020. Black Box Core exposes simulator data and commands through a shared consumer API. The EFB and Logbook are under development. The first-build scope and current verification evidence are linked below.

## Working with this repository

Read the [Core consumer API guide](sources/05_CORE_AND_SIMCONNECT.md#consumer-api-developer-guide) for the available calls, the [function dictionary](sources/10_SIMCONNECT_FUNCTION_DICTIONARY.md) for SimConnect coverage, and the [latest offline test report](reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_remaining_sdk_batch_offline_2026-11-24/REPORT.md) for the verification procedure and prerequisites. The workspace contains Core, the Flight Recorder, the EFB shell and Logbook, and the offline tests. The EFB has an entry point (`python -m bbc_efb`) that the offline tests exercise with offscreen Qt. It has not been run against a live simulator, run on Windows, or built as a Windows package yet.

To publish reviewed workspace changes to the private GitHub repository from a Git-authenticated Windows PowerShell session, run `pwsh -File .\publish.ps1 -Message "Describe the change"`. The script checks staged files and local history, commits changes and pushes `main`. First-time Git authentication must be completed using your account's secure credential helper.

Start with [AGENTS.md](AGENTS.md) for workspace rules and [Project Charter](sources/01_PROJECT_CHARTER.md) for product scope. Read [Status and Evidence](sources/07_STATUS_AND_EVIDENCE.md) before proposing implementation. [Decisions and Readiness](sources/08_DECISIONS_AND_READINESS.md) records the next owner decisions.

On 23 November 2026 the owner confirmed Codex Source authority. On 24 November the owner clarified that the first internal Windows build targets the whole EFB shell and the fully functioning Logbook, including automatic Flight Logging. See [Decisions and Readiness](sources/08_DECISIONS_AND_READINESS.md). The two illustrated Word masters remain the detailed UI and Logbook references. The seven other masters, instructions and selected evidence are preserved under `reference/`.

| Source | Purpose |
|---|---|
| [Project Charter](sources/01_PROJECT_CHARTER.md) | Product, scope and priorities |
| [Architecture](sources/02_ARCHITECTURE.md) | Component boundaries and runtime |
| [EFB UI](sources/03_EFB_UI.md) | Shell, navigation, settings and window behaviour |
| [Logbook and Flight Recorder](sources/04_LOGBOOK_AND_RECORDER.md) | Q1-Q14 functional requirements and open recording rules |
| [Core and SimConnect](sources/05_CORE_AND_SIMCONNECT.md) | Telemetry, integration and validation |
| [Aircraft and Extensions](sources/06_AIRCRAFT_AND_EXTENSIONS.md) | Deferred research and feature boundaries |
| [Status and Evidence](sources/07_STATUS_AND_EVIDENCE.md) | Code inventory, test results and confirmed blocker |
| [Decisions and Readiness](sources/08_DECISIONS_AND_READINESS.md) | Ownership, open decisions and acceptance checks |
| [Source Register](sources/09_SOURCE_REGISTER.md) | Provenance and preserved reference map |
| [SimConnect Function Dictionary](sources/10_SIMCONNECT_FUNCTION_DICTIONARY.md) | Installed SDK exports, purpose and Core coverage |

## Developer setup

Use 64-bit CPython 3.10 or later. Python 3.12 with PySide6 6.8.3 is the tested combination. For the user interface, use Python 3.10 to 3.13: the `ui` extra stays on PySide6 6.8, which is not expected to have wheels for Python 3.14 or later (not checked offline). Black Box Core (`bbc_core`) needs only the standard library. The EFB user interface needs the `ui` extra, which keeps PySide6 on the 6.8 series. From a Windows PowerShell prompt in the repository folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[ui]"
```

Run the offline tests with the standard library runner. They need no simulator, SimConnect.dll or network access:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest discover -s tests -t . -p "test_*.py"
```

`python -B tests/test_repo_offline_guard.py` runs the same suite under the offline guard described in the test reports. The guard blocks native DLL loads, sockets and subprocesses, and the runner prints the guard violation count. It allows only the interpreter's own `ctypes` handle, which Linux and macOS need. The GitHub Actions workflow (`.github/workflows/ci.yml`) is configured to build and check a wheel, install it, and run both commands on Windows and Linux with Python 3.10, 3.11 and 3.12. It has not run on GitHub yet, and the guarded suite has not yet been run on Windows.

The package declares a `bbc-efb` launcher for the application entry point `bbc_efb.__main__:main` (the same as `python -m bbc_efb`). `pyproject.toml` finds every package automatically (`bbc_core`, `bbc_core.ipc`, `bbc_efb`, `bbc_efb.logbook_views`, `bbc_efb.modules` and `bbc_recorder`), and takes its version from `VERSION` in `bbc_efb/__init__.py`, the only version source. SimConnect.dll is never bundled or installed. The live provider uses the pilot's own MSFS 2020 SDK copy (see `reference/EXTERNAL_DEPENDENCIES_NOT_BUNDLED.md`). It uses callback dispatch (`SimConnect_CallDispatch`) by default, the transport that passed both live read-only probes. Pull dispatch (`SimConnect_GetNextDispatch`) stays available as an explicit option until it is re-run live.

The application writes a rotating diagnostic log through `bbc_efb.logging_setup.configure_logging()`. The log goes to `logs` in the pilot's data folder, or to `%LOCALAPPDATA%\BBC EFB\logs` until a data folder is chosen or when it cannot be written. Windowed builds have no console, so this file is the only record of connection errors and uncaught exceptions.

Logging starts in the per-user folder before the window opens, then moves to `<data folder>/logs` once the data folder is known, and the earlier start-up notes are copied there. A data folder opened read-only because another BBC EFB window is writing to it keeps the per-user log. Changing or releasing the data folder, or restoring a backup into it, moves the log out of the old folder first, so no log file is left open there.

Settings has a **Modules (Experimental)** page for drop-in modules. It is off by default, and turning it on requires accepting a trust notice; each module's control-group requests are shown before they are granted. Modules have not been reviewed for release.

Before publishing, `publish.ps1` refreshes `origin/main` and stages into a temporary index. It checks only commits and files that GitHub does not have yet. It decodes UTF-16 text and looks inside ZIP-based files such as `.zip` and `.docx`. It refuses compressed archives it cannot open and opaque binaries over 8 MB. Smaller images and PDFs are byte-scanned only, so their compressed contents are not inspected. It also requires GitHub noreply author and committer email addresses, and checks every identity before it commits. If a check fails, nothing is staged, committed or pushed. A failure at the push itself can leave the new local commit in place. PowerShell was not available where the script was last changed, so it has been checked statically only and has not been run since.
