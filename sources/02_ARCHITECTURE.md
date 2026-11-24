# Approved architecture

| Area | Direction |
|---|---|
| UI | Python and PySide6 / Qt Widgets on Windows 10/11. Exact dependency version, licensing and deployment need validation. |
| NEW C0.1 | One modular process with distinct shell, Core, Recorder, and storage responsibilities. |
| C0.2 onward | Target separate EFB, Core and Recorder processes. IPC, supervision, restart behaviour and performance remain open. |
| Core | Owns provider adapters, translation, shared demand-driven telemetry, simulator commands and the versioned consumer API. The first Core milestone includes reads and writes through one local MSFS 2020 session. Independent of Qt and Recorder semantics. |
| Recorder | Owns flight events, logging state, record persistence and interruption recovery. |
| Storage | First launch asks the pilot to select a writable local data folder. Prefer inspectable local formats such as JSON/JSONL/TXT/CSV. Keep component ownership and writer responsibilities explicit. Schema, atomic writes, migration and recovery details need design and tests. |
| Delivery | Portable ZIP with the whole buildable EFB, dependency and launch/build instructions, changelog, limitations and actual test status. Installer and redistribution need separate review. |

Launch in Dispatch; load other Sections on demand while essential Core/Recorder consumers remain independent of navigation and minimise state. Suspend avoidable UI work when minimised. Preserve versioned logical boundaries for the later process split. No unapproved listeners, cloud requirement or simulator writes.

Develop Core and its translation interfaces independently of the EFB and Recorder. In the first whole-EFB build, the EFB calls the consumer API in-process and Core owns the single shared MSFS 2020 SimConnect session. After the first-build owner review, move Core to a standalone process and carry the consumer API over a versioned local interface. The choices of process hosting, IPC, access controls and supervision remain open. See [Decisions and readiness](08_DECISIONS_AND_READINESS.md).
