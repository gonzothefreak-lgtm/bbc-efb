# BBC Integrated EFB and Black Box Core

BBC EFB is a Windows companion app for Microsoft Flight Simulator 2020. Black Box Core exposes simulator data and commands through a shared consumer API. The EFB and Logbook are under development. The first-build scope and current verification evidence are linked below.

## Working with this repository

Read the [Core consumer API guide](sources/05_CORE_AND_SIMCONNECT.md#consumer-api-developer-guide) for the available calls, the [function dictionary](sources/10_SIMCONNECT_FUNCTION_DICTIONARY.md) for SimConnect coverage, and the [Core offline test report](reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_remaining_sdk_batch_offline_2026-11-24/REPORT.md) for verification and prerequisites.

Launch the offline shell from the workspace root with `python -m bbc_efb` using the project Python runtime with PySide6 available. Dispatch opens first. Choose a local data folder and open Logbook to enter and edit manual flights. For an approved live check, the optional `--simconnect-dll PATH` argument takes the absolute path to a local SimConnect DLL and enables **Connect** in Connection Diagnostics. The DLL loads when the pilot selects **Connect**. Automatic Flight Logging is opt-in under **Flight Logging & Privacy** when a simulator connection and Logbook folder are configured. The parked MSFS 2020 connection check passed. Automatic recording and recovery have offline verification, with a live flight and the packaged Windows build still pending. [Live EFB verification](sources/07_STATUS_AND_EVIDENCE.md#parked-efb-read-only-check) · [Recorder recovery verification](sources/07_STATUS_AND_EVIDENCE.md#automatic-flight-logging-and-recovery)

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
