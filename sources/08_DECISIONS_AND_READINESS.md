# Decisions and readiness

## Approved internal concept

On 23 November 2026 the owner confirmed Codex Source authority and initially selected the manual-first Option A internal concept. On 24 November the owner clarified that the first whole-EFB build targets the fully functioning Q1–Q14 Logbook, including genuine automatic Flight Logging from read-only MSFS 2020 telemetry, manual override and manual entry. The whole shell launches in Dispatch, includes In-Flight and shared Settings, and keeps SimConnect and Flight Logging statuses permanently separate. Unfinished integrations are labelled inactive. The C0.2 deferral for advanced layout customisation still applies. This scope decision supersedes the manual-only first-build plan and does not authorise implementation or live checks under the current review task.

On 24 November 2026 the owner clarified that Black Box Core runs in-process for the first whole-EFB build with the working Logbook. Stop after that build for owner review. The standalone process comes in the following phase. This supersedes the earlier entry that placed the process split before EFB integration.

The owner chose a fresh design baseline for SimConnect and Core, with translation inside Core. The exact R3.1 source, interface documentation and reports are available locally as reference. The owner approved a direct Python binding to the Microsoft x64 SDK as the design direction on 23 November 2026. Coding, offline tests, DLL loading and live validation require separate approval.

The owner requires full operational SimConnect coverage for deployment. The first internal EFB build has the shell, in-process Core and working Logbook. Additional live MSFS validation requires separate approval. Drop-in modules are scheduled for the second release. The public API package and module-loading model remain open.

The first implementation of the [Core Consumer API](05_CORE_AND_SIMCONNECT.md#proposed-api-contract) and the approved starter direct adapter passed 28 offline cases. The owner then approved the load-only preflight. The new loader resolved all 13 declared exports with no SimConnect function calls. Live validation stays separate. [Offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_native_adapter_offline_2026-11-24/REPORT.md) · [DLL report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_sdk_dll_preflight_2026-11-24/REPORT.md)

The owner agreed that trusted internal diagnostics can explore read-only SimConnect variables through Core and propose additions to the data dictionary. Normal modules use documented capability IDs. Candidate research and runtime validation stay distinct.

The owner confirmed full SimConnect read and write capability for the first Core milestone and clarified that deployment must cover every supported function family. The goal is a stable module API that can accommodate non-SimConnect providers later. The owner approved automatic assignment of related write capabilities to built-in modules when the EFB registers them. General SDK access through the API, user-built module and external app access, and simulator validation still need implementation and acceptance scope.

| Open owner decision | What it gates |
|---|---|
| Decide which Core coverage is required for the first internal EFB review | Timing of complete operational SDK coverage relative to the whole-EFB and Logbook review. The second release owns drop-in modules. |
| Approve live validation and release scope separately | Simulator connections, live reads and commands, and further work beyond the internal concept. |
| Agree Recorder event, time and persistence rules | Automatic logging, truthful recovery and flight-level acceptance. |

## Acceptance work

- First whole-EFB build: exercise the in-process Core consumer API from the EFB and verify the full approved Logbook workflow. Save and reopen both manual and automatically logged flights, verify History, editing, correction provenance and record-based summaries, and verify manual override, gap reporting and interruption recovery. Stop for owner review after the build.
- Whole shell: Windows launch, resize, maintain right launcher/dual footer, Settings, and clean close or truthful Save & Exit.
- Local data: select writable folder, create/edit/save/reopen manual record, retain provenance, and distinguish export/backup.
- Core integration: offline checks for shared subscriptions, freshness, gaps and command outcomes. Separately authorised live validation covers real telemetry and units, controlled writes with readback where available, loss/reconnect and teardown. Distinguish simulated results from native evidence.
- Recorder: separately verify one normal flight and defined edge handling. Record pause, Active Pause, rate, abnormal shutdown, touchdown and gaps honestly.
- Measure simulator-only versus EFB active, idle and minimised, including frame-time spikes, CPU, RAM, GPU and I/O. Historical budget figures are provisional.
- Portable concept delivery includes full source and accurate test results, approved dependency handling and clear limits.

## Delivery checklist

### Core and API

- [x] Inventory all 91 functions in the installed MSFS 2020 SDK. Confirm the original 13 adapter symbols with a load-only preflight. [Function dictionary](10_SIMCONNECT_FUNCTION_DICTIONARY.md)
- [x] Exercise shared telemetry, representative radio, SimVar and extended event writes, aircraft Input Events, and contiguous Client Data reads and writes offline. [Client Data checkpoint](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_client_data_offline_2026-11-24/REPORT.md)
- [x] Exercise the five documented system state requests, system notifications and shared event delivery controls offline. [Session checkpoint](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_session_state_offline_2026-11-24/REPORT.md)
- [x] Exercise general typed reads and writes for user-aircraft SimVars and send SDK client events outside the starter catalog. [General SimVar and client event checkpoint](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_sdk_simvar_and_client_events_offline_2026-11-24/REPORT.md)
- [x] Exercise one-shot airport details with runways, parking and static jetways, plus nearby airport lists. [Airport facilities checkpoint](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_airport_facilities_offline_2026-11-24/REPORT.md)
- [x] Exercise VOR, NDB, waypoint and spawned jetway state queries. [Navaid and jetway checkpoint](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_navaids_and_jetways_offline_2026-11-24/REPORT.md)
- [x] Exercise worldwide airport and nearby navaid lists and shared facility cache updates. [Facility list and cache checkpoint](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_facility_lists_and_cache_offline_2026-11-24/REPORT.md)
- [x] Exercise typed integer airport detail filters and isolated definition reuse. [Airport filters checkpoint](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_airport_filters_offline_2026-11-24/REPORT.md)
- [x] Exercise standard and extended client event reception, shared non-masking delivery and cleanup. [Client event checkpoint](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_client_event_reception_offline_2026-11-24/REPORT.md)
- [x] Complete the approved offline SDK batch and account for all 91 functions. The inventory shows 67 implemented paths, including three owner-approved experimental calls, 22 deprecated entries, one unsupported entry and one header-only entry. The guarded suite passed 115 cases. [Function dictionary](10_SIMCONNECT_FUNCTION_DICTIONARY.md) · [Batch report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_remaining_sdk_batch_offline_2026-11-24/REPORT.md)
- [ ] Finalise the module-facing API and dictionaries after native verification. Record aircraft and add-on support, Client Data schemas and reconnect behaviour, and observed failure or silence for the three experimental functions. The owner approved public access to simulator flight save, flight plan load and reserved Tab combinations with a prominent warning. [Experimental calls](05_CORE_AND_SIMCONNECT.md#experimental-simulator-calls)
- [x] Resolve all 67 adapter symbols in the approved load-only preflight. Zero SimConnect functions were called. [Expanded preflight report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_sdk_dll_expanded_preflight_2026-11-24/REPORT.md)
- [x] Run the approved live read-only default aircraft probe. The Asobo 747-8i passed using callback dispatch. Desktop pull dispatch failed on its first packet retrieval. [Default aircraft report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_live_readonly_2026-11-24/DEFAULT_RETRY.md)
- [x] Repeat the approved callback-based read-only check on the PMDG 737-800BCF. Basic telemetry and COM1 readings passed. Standard aircraft input discovery returned zero entries. [Comparison report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_live_readonly_2026-11-24/PMDG_READONLY.md)
- [ ] With separate approval, validate the release candidate's runtime behavior in MSFS. Cover connection and teardown, reconnect and stale values, actual data units and cadence, controlled writes and readback (including both COM spacings), third-party Client Data schemas, and simulator frame-time and resource impact.

### First internal EFB build

- [x] Confirm first-build scope. Include the full approved Logbook and genuine automatic Flight Logging. Retain the one-off parked read-only SimConnect demo as an initial connection check, then separately verify an automatically recorded flight. Get separate approval before running live checks.
- [x] Implement and verify the offline shell with Dispatch on launch, In-Flight examples, Logbook and Settings navigation, reflow and independent permanent statuses. [Offline shell verification](07_STATUS_AND_EVIDENCE.md#offline-efb-shell)
- [x] Connect the footer and Connection Diagnostics to the in-process Core consumer API and verify state changes offline. Flight Logging remains independently Inactive. [Core connection status verification](07_STATUS_AND_EVIDENCE.md#offline-efbcore-connection-status)
- [x] Wire the callback-based SimConnect adapter into a single in-process Core session host. Verify connect, error, retry, dispatch and shutdown offline. [Session wiring verification](07_STATUS_AND_EVIDENCE.md#efb-session-host)
- [x] Verify the EFB's shared session, connection status and teardown in a parked, read-only MSFS 2020 check. [Live EFB evidence](07_STATUS_AND_EVIDENCE.md#parked-efb-read-only-check)
- [ ] Verify automatic simulator identification in the footer and wire the Recorder's states and controls. First build scope excludes drop-in modules.
- [x] Implement the manual yearly JSON store and verify save, reopen, edit, correction history and interrupted year moves offline. [Storage verification](07_STATUS_AND_EVIDENCE.md#manual-logbook-storage)
- [x] Verify the local manual Logbook in the offline shell. Select a data folder, save and reopen a flight, find it in History, edit it, and check the displayed correction history and recalculated summaries. [Offline shell verification](07_STATUS_AND_EVIDENCE.md#offline-efb-shell)
- [ ] Confirm automatic Recorder rules with the owner, including flight start/end, clocks, pause and Active Pause, sim rate, touchdown, disconnect gaps and recovery. Verify a normal flight and interruption/reopen cases before accepting automatic Flight Logging. Live validation requires separate approval. [Logbook and Recorder scope](04_LOGBOOK_AND_RECORDER.md)
- [ ] Package and verify the first Windows build under the applicable approvals. Check launch, resize, close, offline status wording and data persistence. Verify that deployed application code and files are free of temporary demo code, simulated providers, test fixtures, development logs and probe outputs. Preserve development and test evidence separately. Stop for owner review after that build.

### Second release after owner review

- [ ] Define and approve the standalone Core host, versioned local interface, connection lifecycle and client access. Verify process startup and shutdown, client disconnect and restart, and SimConnect recovery with an independent client before switching the EFB to standalone Core.
- [ ] Publish module packaging and installation instructions. Prove that a developer using the docs can build a module which the EFB discovers automatically and which performs a supported read and write with no Core edits or separate SimConnect setup.

The owner asked to review what remains and the API developer documentation before moving on to the EFB and Recorder. [Consumer API developer guide](05_CORE_AND_SIMCONNECT.md#consumer-api-developer-guide) · [Expanded write test report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_expanded_writes_offline_2026-11-24/REPORT.md)

Main Development owns shell and shared integration, Core owns data feasibility, Logbook/Recorder owns flight semantics, and the owner approves product decisions. Prefer one shared decision record over repetitive specialist handoffs.
