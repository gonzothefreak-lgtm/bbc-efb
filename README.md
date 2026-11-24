# BBC Integrated EFB and Black Box Core

BBC EFB is a Windows companion app for Microsoft Flight Simulator 2020. Black Box Core exposes simulator data and commands through a shared consumer API. The EFB and Logbook are under development. The first-build scope and current verification evidence are linked below.

## Working with this repository

Read the [Core consumer API guide](sources/05_CORE_AND_SIMCONNECT.md#consumer-api-developer-guide) for the available calls, the [function dictionary](sources/10_SIMCONNECT_FUNCTION_DICTIONARY.md) for SimConnect coverage, and the [latest offline test report](reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_remaining_sdk_batch_offline_2026-11-24/REPORT.md) for the verification procedure and prerequisites. The workspace currently contains Core, offline tests and EFB drafts. A runnable Windows EFB package is still pending.

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
