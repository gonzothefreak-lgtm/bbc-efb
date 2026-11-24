# Audit fixes: status of findings A1 to A142

This maps every finding in the offline audit (`REVIEW-NOTES.md`, `review-evidence/audit.json`, ids A1 to A142) to its final status after the fix rounds, and where it was fixed. Everything here was checked offline on macOS: no Windows run, no MSFS session, no CI run. "Resolved" means resolved and tested offline. It does not mean validated in the simulator.

## Final integration check (2026-11-25)

| Check | Result |
|---|---|
| Full suite, Python 3.12 (PySide6 6.8.3, offscreen) | 1646 tests, OK, 4 skipped (Windows-only or SDK-header tests) |
| Full suite, Python 3.11 (no PySide6: Qt modules skip) | 1276 tests, OK, 27 skipped |
| `ast.parse(..., feature_version=(3, 10))` over every .py file | 175 files, no errors |
| App smoke: `python -m bbc_efb --simulated --data-dir <tmp>`, HOME/APPDATA in temp dirs, ~8 s, window closed | Exit code 0; no ERROR, CRITICAL or Traceback in the console, `bbc-efb.log` or `fault.log` (both fault logs empty) |
| End-to-end recorded flight: `tests.test_shell_end_to_end`, `tests.test_service_acceptance`, `tests.test_core_flight_tape` | 16 tests, OK |

Test command: `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t . -p "test_*.py"`.

Status counts: resolved 102, partial 1, needs native validation 32, owner decision 7.

Status meanings:

- **resolved**: the defect is fixed and covered by a test offline.
- **partial**: part of the defect is fixed; the rest is listed in the last column.
- **needs native validation**: the code fix is done and tested offline, but the behaviour depends on Windows, MSFS or SimConnect.dll and has not been run there.
- **owner decision**: the work is done as far as it can go without a decision by the owner, or waits on one (see the owner decisions section).

## Findings

| Id | Finding | Status | Fixed in | Tests | Remaining |
|---|---|---|---|---|---|
| A1 | No Core worker thread or Qt signal bridge, and SimConnect_Open passes no Win32 event so the worker cannot block effic... | needs native validation | bbc_core/host.py (CoreHost); bbc_efb/core_bridge.py; bbc_core/simconnect_sdk.py (wake event); bbc_core/simconnect.py wait/wake | test_host_core, test_host_bridge, test_core_threading | SimConnect signalling the event, idle CPU and window responsiveness on Windows/MSFS. |
| A2 | Storage schema 1 cannot represent captured or imported flights, provenance, corrections, payload or Recorder evidence | resolved | bbc_efb/storage.py (schema 2, provenance, recorder evidence); bbc_efb/STORAGE_SCHEMA.md; logbook_views/details.py, model.py | test_efb_storage, test_recorder_records, test_logviews_* | Owner acceptance of the schema (with the Recorder rules). |
| A3 | No application entry point or EFB shell (Dispatch, In-Flight, Logbook, Settings, frameless window) | needs native validation | bbc_efb/__main__.py, app.py, shell.py, sections.py; pyproject gui-script | test_shell_* | Windows launch, frameless move/resize, Snap, clean close. |
| A4 | No permanent, independent SimConnect and Flight Logging footer statuses | resolved | bbc_efb/footer.py; shell.py (request_stop_logging, _confirm_stop) | test_shell_footer, test_shell_integration | Visual check on Windows (with A3). |
| A5 | Flight Recorder does not exist (phases, clocks, pause, touchdown, gaps) | owner decision | bbc_recorder/ (state_machine, rules, service, journal, recovery, records) | test_recorder_*, test_service_*, test_core_flight_tape | Owner approval of bbc_recorder/RULES.md; live MSFS recorded flight. |
| A6 | No crash-safe Recorder journal or interruption recovery | needs native validation | bbc_recorder/journal.py, recovery.py, service.recover_unfinished; app.py; journal lost-line accounting (this round) | test_journal_powerloss, test_journal_recovery, test_journal_concurrency, test_recorder_integrity_fixes | Windows durability and msvcrt locking; owner decisions on recovery UX (P-R1) and retention. |
| A7 | No pyproject or requirements, no declared Python version, unpinned PySide6, no entry point; tests were run on the Cod... | resolved | pyproject.toml; bbc_efb/__init__.py VERSION; packaging/requirements-build.txt; .github/workflows/ci.yml; bbc_core/ipc/protocol.py | test_repo_packaging, test_repo_docs; suite on 3.11 and 3.12 | Version and API-version rule are proposals; CI and Python 3.10 never run. |
| A8 | No logging anywhere; failures in the field leave no trace, and windowed builds lose stderr | resolved | bbc_efb/logging_setup.py (incl. retired handler); Core/host/adapter logging | test_repo_logging_setup, test_shell_review_followups | Check log.txt/fault.log in a windowed PyInstaller build. |
| A9 | No connection supervision, retry or 'simulator not running' handling | needs native validation | bbc_core/host.py supervision and describe_connect_failure | test_host_core | Real 'not running' strings, reconnect and aircraft reload in MSFS; retry schedule is a proposal. |
| A10 | Command deadline counts queue time, so a short burst of writes blocks all writes for the session | resolved | bbc_core/core.py (_Pending.sent_deadline, provider-derived deadline) | test_core_commands | None offline. |
| A11 | Interval throttling drops about 1/3 of samples when provider timing jitters (default 1 Hz case) | resolved | bbc_core/core.py _due | test_core_delivery | None. |
| A12 | Per-subscription interval throttle drops about half of 1 Hz samples under normal receive jitter | resolved | bbc_core/core.py _due | test_core_delivery | None offline. |
| A13 | Per-consumer interval throttle drops about half of 1 Hz samples under normal timing jitter | resolved | bbc_core/core.py _due/_receive_reading | test_core_delivery | Live cadence check only. |
| A14 | Readings are per-signal and stamped only with host receipt time, so Recorder samples are neither coherent nor tied to... | resolved | bbc_core/contract.py Snapshot; core.subscribe_snapshot; simconnect.py snapshots; bbc_recorder/service.py | test_adapter_snapshot, test_core_snapshot, test_integration_snapshot_session | Live field and clock values. |
| A15 | Input Event unsubscribes are untracked, and an aircraft change can end the session | resolved | bbc_core/simconnect_input_events.py | test_adapter_helpers InputEventTests | None offline. |
| A16 | Pull transport raises ProviderError on GetNextDispatch's normal 'queue empty' E_FAIL, so the default SDK path cannot... | needs native validation | bbc_core/simconnect_sdk.py next_packet (E_FAIL idle) | test_adapter_sdk_transport PullDispatchTests | Pull mode re-run live; stays opt-in. |
| A17 | Pull dispatch treats an empty SimConnect queue (E_FAIL) as a fatal error; FakeSdk hides this by returning a NULL packet | needs native validation | bbc_core/simconnect_sdk.py; tests/test_adapter_support.py FakeDll | test_adapter_sdk_transport | Live pull-mode idle behaviour. |
| A18 | Pull dispatch probably treats an empty SimConnect queue (E_FAIL) as fatal, which matches the failed live pull run | needs native validation | bbc_core/simconnect_sdk.py | test_adapter_sdk_transport | Live pull-mode re-run. |
| A19 | Pull dispatch treats the normal empty-queue E_FAIL from SimConnect_GetNextDispatch as fatal, so every live pull sessi... | needs native validation | bbc_core/simconnect_sdk.py; simconnect.py connect | test_adapter_sdk_transport | Live pull-mode validation. |
| A20 | Recorder-relevant telemetry is missing from the catalog, and SimulatedProvider rejects every SDK SimVar | resolved | bbc_core/catalog.py; bbc_core/simulated.py | test_core_snapshot, test_core_delivery | None. |
| A21 | Logbook draft covers only part of Q1-Q14 (no Cards/Combined views, filters, overlay, tabs or real Statistics) | resolved | bbc_efb/logbook_views/ (dashboard, history, details, recorder_management, statistics, filters) | test_logviews_*, test_logviews_regressions | Owner visual acceptance; PROPOSAL view keys; Delete pending retention policy. |
| A22 | No storage writer lock or Recorder/UI write ownership, even though the first build adds a second writer | resolved | bbc_efb/storage.py (writer lock, _mutex, RecordConflict) | test_efb_storage LockTests | Windows msvcrt / OneDrive / network-folder spot check. |
| A23 | No first-launch data-folder selection or Settings, and FlightStore cannot create a new folder | resolved | bbc_efb/storage.py; storage_settings.py; settings.py; app.py | test_settings_data_management, test_settings_ui, test_shell_folder, test_storage_review_fixes | Native dialog and OneDrive detection. |
| A24 | No packaging, dependency manifest or SimConnect.dll discovery for the portable ZIP | needs native validation | pyproject.toml; packaging/ (spec, build_portable.ps1, ...); simconnect_sdk.find_simconnect_dll | test_packaging_spec, test_packaging_tools, test_packaging_script, test_repo_packaging | Portable build never run on Windows; simulated-provider exclusion and freezer are owner decisions. |
| A25 | Export All Data and restorable backup are not implemented | resolved | bbc_efb/data_management.py; settings.py (incl. BOM handling this round) | test_settings_data_management, test_storage_review_fixes | Windows check of copying a live journal and renaming the in-use folder. |
| A26 | Close-while-logging (Cancel / Save & Exit) and minimised behaviour are not implemented | resolved | bbc_efb/shell.py closeEvent + _resume_logging_after_failed_exit; app.py FlightLoggingManager.resume_after_failed_exit; bbc_recorder/service.py save_and_close | test_shell_close, test_shell_review_followups | None: 'Return to BBC EFB' now restarts Flight Logging and retries the kept journal; dialog text corrected. |
| A27 | FakeSdk models an empty queue as a NULL (id 0) packet, which hides the E_FAIL contract from every offline test | resolved | tests/test_simconnect_provider_offline.py FakeSdk; simconnect_sdk.py | test_adapter_sdk_transport | Live idle GetNextDispatch result. |
| A28 | Pause state arrives as raw integer flags with no Active Pause decoding | needs native validation | bbc_core/contract.py PauseState; catalog.py; bbc_recorder pause_policy | test_core_catalog, test_core_snapshot, test_service_tape | Live Pause_EX1 bits; owner approval of pause_policy; catalog wording for sim.paused/unpaused vs Active Pause. |
| A29 | Queue overflow drops control messages (Connection, CommandResult, InputCatalog, FacilityResult) the same as samples;... | resolved | bbc_core/core.py _Consumer two lanes | test_core_delivery LaneTests, test_core_performance ControlLaneOverflowTests | None. |
| A30 | After a failed provider.disconnect, connect() silently returns ERROR forever instead of retrying cleanup | resolved | bbc_core/core.py connect/_end_session | test_core_failures | None. |
| A31 | Core only catches ProviderError; an OSError or ctypes.ArgumentError from the DLL leaves Core stuck in CONNECTING and... | resolved | bbc_core/simconnect_sdk.py _native; core.py connect/pump | test_core_failures, test_integration_cross_file, test_adapter_round3 | None offline. |
| A32 | No threading or blocking-time tests even though Core is built for a multi-threaded host | resolved | bbc_core/host.py; core.py locking | test_core_threading, test_host_core | Design note: 0.5 s COM baseline wait blocks the Core worker (not the UI). |
| A33 | One-shot system state values (aircraft file, flight file, Sim, DialogMode, FlightPlan) never go stale and are never r... | needs native validation | bbc_core/core.py _add_subscription, _LIFECYCLE_STATES | test_core_failures SystemStateFreshnessTests, test_core_freshness, test_core_regressions ResubscribeBetweenPumpsTests | Live lifecycle events on aircraft/flight change. |
| A34 | Queued observations are labelled STALE by poll time, so timely samples are mislabelled | resolved | bbc_core/core.py _delivered | test_core_delivery | None. |
| A35 | A non-ProviderError from provider.send leaves the command 'dispatching' forever, then it is re-sent or mislabelled NO... | resolved | bbc_core/core.py _dispatch_next | test_core_commands | None. |
| A36 | Core and provider failure paths with no test coverage (stdlib trace) | resolved | tests only (coverage) | test_adapter_round4 (paths 4, 6, 7, 8, 9, 10), test_core_performance ControlLaneOverflowTests, test_core_delivery ContinuityTests; test_adapter_failures (10 provider failure paths found by a stdlib trace pass: connect over an open handle, diagnostics without a session, request-ID exhaustion, refused state, event, dialog, mapping and readback requests, malformed readback, failed cleanup) | None: every listed uncovered path now has a targeted test. |
| A37 | IngressGap clears latest values of on-change/one-shot signals and never re-requests them | resolved | bbc_core/core.py _receive_gap | test_core_delivery test_ingress_gap_requests_fresh_one_shot_states; test_client_data_offline test_ingress_gap_rereads_an_on_set_region_once (fails if ON_SET is dropped from the refresh rule) | None offline. |
| A38 | Aircraft input catalog can stay UNAVAILABLE after an IngressGap during enumeration, and a re-request does nothing | resolved | bbc_core/core.py; simconnect_input_events.py request_catalog | test_core_delivery, test_adapter_round3 CatalogRestartTests | None offline. |
| A39 | Core._receive_reading scans all consumers x all subscriptions for every reading (quadratic; 89 ms per 1 Hz burst at 1... | resolved | bbc_core/core.py _by_signal index | test_core_threading | None. |
| A40 | Session end and IngressGap delete valid, already-received samples the consumer has not polled yet | resolved | bbc_core/core.py _reset_delivery | test_core_delivery, test_core_offline | None. |
| A41 | SimVar rejects index 0, so zero-based gear and contact-point variables cannot be addressed | resolved | bbc_core/sdk_simvar.py zero_based_index | test_core_catalog | Curated prefix list; extend if new zero-based families appear. |
| A42 | _retire issues untracked SDK calls, so any exception they cause is fatal | resolved | bbc_core/simconnect.py _retire/_teardown; simconnect_ledger.py | test_adapter_provider FailureScopeTests, test_adapter_round4 | None offline. |
| A43 | pump(limit>512) overflows SimConnectProvider's 512-entry update queue and triggers a full IngressGap cascade | resolved | bbc_core/simconnect.py drain back-pressure | test_adapter_provider QueueTests | None. |
| A44 | Exceptions that arrive after a command, readback or baseline has completed are unmatched and end the session | resolved | bbc_core/simconnect.py ledger records | test_adapter_provider | None offline. |
| A45 | One undecodable SIMOBJECT_DATA packet for one signal ends the whole SimConnect session | resolved | bbc_core/simconnect.py _receive_data/_payload | test_simconnect_provider_offline | Native dwDefineCount for TEXT SimVars. |
| A46 | Bounded update queue silently drops the oldest item, including CommandOutcome and FacilityReply, and the baseline loo... | resolved | bbc_core/simconnect.py _offer lanes; _BASELINE_PACKET_BUDGET | test_adapter_provider | Control lane unbounded by design. |
| A47 | A bad ObjectField in a WRITE_OBJECT_DATA command disconnects the whole SimConnect session | resolved | bbc_core/simconnect_advanced.py _define | test_adapter_helpers AdvancedTests | None offline. |
| A48 | An ASSIGNED_OBJECT_ID that arrives after the 4 s command timeout leaves an orphaned AI object nobody can remove | resolved | bbc_core/simconnect_advanced.py late_assignment; simconnect.py _process | test_adapter_helpers | Consumer told only via log; real spawn latency unverified. |
| A49 | Unsubscribing a Client Data region whose stream or definition failed ends the session | resolved | bbc_core/simconnect_client_data.py | test_adapter_helpers ClientDataTests | None offline. |
| A50 | A CreateClientData failure such as ALREADY_CREATED on reconnect permanently poisons an area that is usable | resolved | bbc_core/simconnect_client_data.py; simconnect.py | test_adapter_helpers ClientDataTests | None offline. |
| A51 | Jetway deadline starts at enqueue, not at send, and one timeout disables all jetway queries for the session | resolved | bbc_core/simconnect_facilities.py jetway queue/quiet period | test_adapter_facilities JetwayTests, test_facilities_offline | Native: does MSFS send JETWAY_DATA for airports without jetways. |
| A52 | Fixed 8 s absolute deadline for every facility request, including worldwide lists and large airports | resolved | bbc_core/simconnect_facilities.py; simconnect_facility_cache.py | test_adapter_facilities ListTests | Native worldwide-list timing. |
| A53 | A SimConnect exception on a facility-cache unsubscribe ends the entire SimConnect session | resolved | bbc_core/simconnect_facility_cache.py | test_adapter_facilities CacheTests | None. |
| A54 | A facility-cache watch that fails once is never resubscribed for the rest of the session | resolved | bbc_core/simconnect_facility_cache.py; core.py _receive_facility_cache | test_adapter_facilities CacheTests | Native churn when no initial list. |
| A55 | One out-of-range or empty-ident record discards an entire facility list and kills the cache watch | resolved | bbc_core/simconnect_facility_lists.py tolerant decode | test_adapter_facilities | None. |
| A56 | Input Event descriptor eType is decoded with the wrong enum (SIMCONNECT_INPUT_EVENT_TYPE instead of SIMCONNECT_DATATYPE) | needs native validation | bbc_core/simconnect_input_events.py _DESCRIPTOR_TYPES | test_adapter_helpers, test_aircraft_inputs_offline | Live eType values of real descriptors. |
| A57 | Callback transport ends the session when one CallDispatch delivers more than 512 packets, discarding all of them | resolved | bbc_core/simconnect_sdk.py byte-bounded deque; simconnect.py | test_adapter_sdk_transport CallbackDispatchTests | None offline. |
| A58 | Callback dispatch mode turns any backlog over 512 packets into a session failure | resolved | same as A57 | test_adapter_sdk_transport | None offline. |
| A59 | Callback dispatch tears down the whole session when more than 512 packets arrive in one CallDispatch | resolved | same as A57 | test_adapter_sdk_transport | None offline. |
| A60 | Simulated provider and fake clocks are too ideal to support Recorder-grade tests, and no Recorder fixtures exist | needs native validation | bbc_core/simulated.py FlightTape; bbc_recorder/tape.py | test_core_flight_tape, test_service_acceptance, test_service_tape | Capture real MSFS 1 Hz and visual-frame tapes on Windows as fixtures. |
| A61 | SimulatedProvider emits only on change, so every unchanged simulated value is labelled STALE after 3 s under Core's d... | resolved | bbc_core/simulated.py periodic re-send | test_core_snapshot | None offline. |
| A62 | SimulatedProvider confirms commands the real provider rejects, and runs 10x faster | resolved | bbc_core/simulated.py send; catalog.com_channel_problem | test_core_parity | None offline. |
| A63 | Editor silently truncates or clamps loaded values, so saving an unrelated change alters them | resolved | bbc_efb/logbook.py FlightEditor (incl. int32 range this round) | test_efb_logbook EditorFidelityTests, test_logviews_regressions | None. |
| A64 | After RecordConflict the UI keeps the stale Flight, so the pilot cannot recover without restarting | resolved | bbc_efb/logbook.py _handle_conflict | test_efb_logbook | None. |
| A65 | Records with any extra field are rejected as unreadable; no forward-compatible schema handling | resolved | bbc_efb/storage.py parse_record/_parse_fields | test_efb_storage | None. |
| A66 | Non-string or mismatched saved_at in any record crashes listing() (uncaught TypeError in sort) and Details view | resolved | bbc_efb/storage.py _parse_instant/_sort_key | test_efb_storage | None. |
| A67 | No tests for the EFB storage layer: Windows file behaviour, concurrency and crash leftovers | needs native validation | bbc_efb/storage.py _replace_with_retry, temp sweep | test_efb_storage (Windows test skipped here) | Run test_efb_storage on Windows NTFS with OneDrive/Defender. |
| A68 | Recorder-owned airport, aircraft and registration attribution has no defined path (From/To from position, title not t... | owner decision | bbc_recorder/attribution.py, records.py (needs_confirmation list); bbc_efb/recorder_sink.py; logbook_views/model.py FieldCheck, details.py, recorder_management.py; logbook.py editor notice; bbc_efb/storage.py FlightStore.confirm, FieldProvenance.confirmed, Flight.confirmed_fields (pilot 'confirmed' revisions, `diversion` answer); logbook_views/model.py needs_confirmation, review_label, suspected_diversion; history.py, cards.py (Needs confirmation marker); filters.py (Field confirmation filter); details.py (Confirm as correct, Correct…, Confirm diversion, Not a diversion); recorder_management.py StoreActions.confirm_fields; logbook.py confirm_fields | test_attribution_*, test_recorder_integrity_fixes, test_logviews_regressions; test_field_confirmation (21 tests) | Owner decision 7: whether the aircraft field may be filled from the low-confidence TITLE fallback. Such a field is now marked 'Needs confirmation' and the pilot can confirm or correct it. |
| A69 | No standalone-Core IPC design or versioned wire format | needs native validation | bbc_core/ipc/ (protocol, server incl. PIPE_REJECT_REMOTE_CLIENTS, client); docs/STANDALONE_CORE_IPC.md | test_ipc_protocol, test_ipc_server, test_ipc_hardening | Named pipe never run on Windows; PROPOSAL rules need owner decision. |
| A70 | No module discovery or loading model for drop-in modules (second release) | owner decision | bbc_efb/modules/manifest.py, loader.py; docs/MODULE_AUTHORING.md | test_modules_* | Trust/isolation model; modules still in-process and ConsumerAPI._host reachable. |
| A71 | No tests for bbc_efb, and no offline Recorder 'flight tape' acceptance harness | resolved | tests/ (bbc_efb suites, flight-tape harness) | test_service_acceptance, test_core_flight_tape, test_recorder_flights, test_shell_end_to_end | Live flights are the separate acceptance step. |
| A72 | The 'header-derived' signature test is tautological and never reads the SDK header | needs native validation | tests/test_adapter_abi.py (classified ABI vs header prototypes) | test_adapter_abi (header test skipped here) | Run against an installed 0.24.6.0 SDK header. |
| A73 | The real native transport (GetNextDispatch, Close, GetLastSentPacketID, callback overflow, load) is never executed by... | resolved | tests/test_adapter_support.py FakeDll; test_adapter_sdk_transport.py | test_adapter_sdk_transport, test_adapter_round3 | ctypes.WinDLL load and real DLL behaviour on Windows. |
| A74 | The only SimConnectSdk transport test fails on non-Windows hosts | resolved | bbc_core/simconnect_sdk.py _is_dll_path | test_simulator_batch_offline, test_adapter_sdk_transport | None. |
| A75 | The COM1 write path ignores channel spacing, and Hz is read as a truncating INT32 | needs native validation | bbc_core/simconnect.py (COM frequencies registered as FLOAT64 and rounded to Hz, plain and snapshot); catalog.com_channel_problem | test_adapter_round4 FrequencySubscriptionTests, test_adapter_provider ComChannelTests | Native check of frequency representation and both spacings. |
| A76 | Client Data area names are upper-cased, and the fake mirrors that assumption | resolved | bbc_core/client_data.py; core.register_client_data | test_client_data_offline | Native case rule unverified (harmless). |
| A77 | (str, Enum) members print as 'Class.MEMBER' in str() and f-strings on 3.11+, a trap for the planned JSON/JSONL Recorder | resolved | bbc_core/contract.py StrEnum and equivalents | test_core_catalog | None. |
| A78 | connect(): a non-ProviderError from provider.connect leaves Core stuck in CONNECTING | resolved | bbc_core/core.py connect | test_core_failures | None. |
| A79 | _desired_demand() rebuilds, sorts and compares the entire demand on every pump (350-700 us idle at 1,000 subscriptions) | resolved | bbc_core/core.py demand cache | test_core_delivery, test_core_threading | None. |
| A80 | Unexpected exception in pump strands facility reads (marked dispatched but never sent) and loses queued requests | resolved | bbc_core/core.py pump/_end_session/_expire_facilities | test_core_failures | None. |
| A81 | Core.pump and connect only catch ProviderError; other provider exceptions escape and leave half-registered state | resolved | bbc_core/simconnect_sdk.py _native; simconnect.py _register | test_adapter_round3 NativeErrorTests, RegistrationCleanupTests | Real SEH mapping on Windows. |
| A82 | Facility read limit is global (8) with no per-consumer quota and no Core-side timeout | resolved | bbc_core/core.py _MAX_FACILITY_PER_CONSUMER, Core-side deadline | test_core_failures | Optional: reserved slots for small queries. |
| A83 | _poll scans every subscription for staleness on each call, and the scan is starved when the queue fills the batch | resolved | bbc_core/core.py _poll stale heap and reserve (refined this round) | test_core_delivery, test_core_regressions PollReserveTests | None. |
| A84 | Each unsubscribe rebuilds the consumer queue and the global readings map (0.4 ms each; 53 ms to tear down 128 subscri... | resolved | bbc_core/core.py _index_remove | test_core_delivery IndexTests | None. |
| A85 | Command queue cap (64) is global with no per-consumer quota, so one module can block every other module's writes | resolved | bbc_core/core.py per-consumer command cap, supersession | test_core_commands QuotaTests | None. |
| A86 | Input subscriptions are deleted silently on catalog change; later latest() raises CoreRequestError | resolved | bbc_core/contract.py SubscriptionEnded; core.py tombstones | test_core_delivery | None. |
| A87 | Per-reading fan-out and per-pump demand rebuild scan every subscription of every consumer | resolved | bbc_core/core.py _by_signal/_demand_generation | test_core_threading, test_core_delivery | None. |
| A88 | Provider event errors are reported as consumer data-loss Gaps | resolved | bbc_core/contract.py SignalError; core.py | test_core_parity, test_client_events_offline, test_recorder_reconcile | None. |
| A89 | Low type-hint coverage, mixed annotation modes and dead imports; mypy/pyright cannot guard the ABI boundary | partial | bbc_core/simconnect.py, simconnect_advanced.py (unused imports removed, _OptionalSdk protocol); from __future__ annotations in every bbc_core module; simulator_ops.ObjectSample annotation; .github/workflows/ci.yml advisory ruff/mypy job; tests/test_core_static.py (stdlib unused-import check with every package held to zero, get_type_hints over all bbc_core dataclasses, _Sdk/_OptionalSdk conformance); unused imports removed from bbc_recorder/attribution.py and bbc_efb/logbook.py; simconnect.py _idle goes through self.wait() | test_adapter_round4 (AST hygiene), test_core_regressions ModuleHygieneTests, test_repo_packaging WorkflowTests; test_core_static | ruff and mypy are not installed here and the advisory CI job has never run; annotation coverage in bbc_core is still low; three existing lines in simconnect.py exceed the 120-character ruff limit. |
| A90 | client_event_name rejects dotted custom events used by add-on aircraft | resolved | bbc_core/sdk_simvar.py client_event_name; provider.py Signal no longer upper-cases client events | test_core_catalog, test_core_commands, test_client_events_offline, test_core_regressions ClientEventSpellingTests | Whether MSFS custom-event lookup is case-sensitive (now irrelevant: one spelling). |
| A91 | Readback float tolerance abs_tol=1e-6 marks valid writes UNCERTAIN for float32-backed or unit-converted SimVars | resolved | bbc_core/simconnect.py _FLOAT_READBACK_REL_TOL | test_adapter_provider ReadbackToleranceTests | None in code. |
| A92 | Failed registrations are never retried while their demand is unchanged | resolved | bbc_core/simconnect.py _retry_failed_registrations (current Demand used this round) | test_adapter_provider RetryTests, test_adapter_round3, test_adapter_round4 EventRegistrationRetryTests | None. |
| A93 | Most system-event names are sent upper-cased, while states and three events get canonical spelling | resolved | bbc_core/simconnect.py _SYSTEM_EVENT_NAMES | test_adapter_provider test_system_events_use_canonical_sdk_spelling, SystemEventNameTableTests (completeness) | None. |
| A94 | One data definition and one RequestDataOnSimObject per SimVar: N packets per second instead of one, and no same-frame... | owner decision | bbc_core/simconnect.py snapshots (grouped path); bbc_recorder uses snapshots | test_adapter_snapshot | Plain subscriptions are still one request per SimVar: group them, or record that bulk readers must use snapshots. Measure frame cost on Windows. |
| A95 | Single-datum definition and request per SimVar: cost grows with signal count, and the design cannot scale to high-rat... | needs native validation | bbc_core/simconnect.py SIM_FRAME/VISUAL_FRAME snapshots; bbc_recorder/service.py | test_adapter_snapshot, recorder tests | Pump keeping up at 30-60 Hz and sim frame-time impact. |
| A96 | Accepted interval returned to consumers does not match the native SimConnect period | resolved | bbc_core/simconnect.py interval_granularity_ms; core.py rounding | test_core_commands, test_adapter_provider | None. |
| A97 | COM1 SET_HZ readback is exact-match, but band validation ignores channel spacing | needs native validation | bbc_core/simconnect.py _prepare_radio, _com_readback_options | test_adapter_provider ComChannelTests, test_adapter_round3 | Live COM STANDBY value after COM_STBY_RADIO_SET_HZ for 25/8.33 kHz. |
| A98 | ctypes _pack_ without _layout_ emits DeprecationWarning, which becomes an error in Python 3.19 on the macOS test host | resolved | bbc_core/simconnect_abi.py _layout_ = 'ms' | test_adapter_abi, test_simconnect_abi_offline | None. |
| A99 | ctypes Structures use _pack_ without _layout_; deprecated since 3.14 and an error from 3.19 on non-Windows | resolved | same as A98 | test_adapter_abi | None. |
| A100 | Hardware-input client events are never added to a notification group | needs native validation | bbc_core/simconnect_advanced.py notification group | test_adapter_helpers | Live hardware input delivery. |
| A101 | RESERVE_KEY returns a key whose presses can never be received | needs native validation | bbc_core/simconnect_advanced.py; simconnect_client_events.py private events | test_adapter_helpers | Live reserved-key EVENT packets. |
| A102 | Client Data payloads must be exactly the region size, so padded packets would be rejected | resolved | bbc_core/simconnect_client_data.py payload | test_adapter_helpers | None. |
| A103 | Periodic Client Data modes (SECOND, VISUAL_FRAME) send full payloads every period without the CHANGED flag | owner decision | bbc_core/core.py subscribe_client_data(changed_only=True); simconnect_client_data.py | test_integration_cross_file | Default is still Flags=0 for VISUAL_FRAME: default CHANGED, add a rate cap, or accept the full stream. |
| A104 | mapping_failed() leaves the signal in _members, so the group fills up and a RemoveClientEvent is sent for an unmapped... | resolved | bbc_core/simconnect_client_events.py | test_adapter_helpers ClientEventTests | None. |
| A105 | VOR/NDB detail replies are not checked against the requested ident or region, and region is reported as None | owner decision | bbc_core/simconnect_facilities.py _NAVAID_IDENTITY_NOTE (honest label) | test_facilities_offline | Require a region for VOR/NDB in Core.request_navaid, or confirm the schema live. |
| A106 | A truncated facility packet fails every outstanding facility request | resolved | bbc_core/simconnect_facilities.py receive | test_adapter_facilities | None. |
| A107 | Filtered airport definition slots leak when FACILITY_DATA_END never arrives; after 8, filtered queries fail for the s... | resolved | bbc_core/simconnect_facilities.py filter quarantine | test_adapter_facilities | Edge: fatal/unused-tier slots never reclaimed. |
| A108 | The airport detail definition is all-or-nothing: one unsupported field disables every airport detail query for the se... | needs native validation | bbc_core/simconnect_facilities.py jetway tier fallback | test_adapter_facilities | Live per-field exception; jetways=() vs unknown labelling. |
| A109 | Early removals are replayed after the initial snapshot even when a later add re-listed the facility | resolved | bbc_core/simconnect_facility_cache.py _complete | test_facility_cache_offline | None offline. |
| A110 | Strict list payload length check may reject valid packets if the SDK pads dwSize | needs native validation | bbc_core/simconnect_facility_lists.py padding | test_adapter_facilities | Capture real list packets; padded empty placeholder not accepted. |
| A111 | Worldwide list decoding runs all at once inside pump() on the caller's thread | resolved | bbc_core/host.py worker; simconnect.py drain time budget | test_adapter_provider, test_host_core | Frame-time on a slow Windows PC unmeasured. |
| A112 | Worldwide airport list and facility-cache snapshot decode synchronously inside one pump (about 30-40 ms for 40k airpo... | resolved | bbc_core/core.py _FacilityWatch entries cache | test_integration_cross_file | None offline. |
| A113 | A failed or timed-out initial GetInputEvent silently discards all later subscription updates | resolved | bbc_core/simconnect_input_events.py _retry_get | test_adapter_round3, test_adapter_helpers | None offline. |
| A114 | No frozen-build path for locating SimConnect.dll; the host must supply an absolute path | needs native validation | bbc_core/simconnect_sdk.py find_simconnect_dll; app.py; sections.py; settings.py (restart wording this round); a new SimConnect.dll choice is used at the next connection attempt without a restart: bbc_core/host.py replace_provider_factory, set_before_connect; app.py SimConnectDllSwitch; settings.py and core_bridge.py wording | test_adapter_sdk_transport, test_shell_review_followups DllRestartTextTests; test_host_provider_swap, test_shell_dll_switch, test_settings_dll_live | PyInstaller discovery on Windows; loading a second SimConnect.dll in one process on Windows; a restart is still needed when no DLL was found at start-up (owner decision 23); DLL redistribution is an owner decision. |
| A115 | DLL load is all-or-nothing over about 75 exports; one missing export blocks even basic telemetry | resolved | bbc_core/simconnect_sdk.py _bind; simconnect.py _has | test_adapter_sdk_transport | Optional: test with an older SimConnect.dll. |
| A116 | Callback dispatch caps one CallDispatch at 512 packets, which a worldwide list can exceed and which then ends the ses... | resolved | bbc_core/simconnect_sdk.py byte-bounded buffer | test_adapter_sdk_transport | None offline. |
| A117 | SimulatedProvider does linear scans per publish and per support check | resolved | bbc_core/simulated.py set lookups | full suite | No dedicated perf test. |
| A118 | Flight-plan paths for AI and plan loading must include '.pln', although the SDK says the extension is optional or omi... | needs native validation | bbc_core/simulator_ops.py flight_path, SEND_FLIGHT_FILE_EXTENSION | test_core_catalog, test_simulator_batch_offline | Native probe decides the extension form. |
| A119 | Editor field length limits silently truncate stored values on edit | resolved | bbc_efb/logbook.py FlightEditor maxLength; storage TEXT_LIMITS | test_efb_logbook | None. |
| A120 | Recent flights table only opens on double-click; there is no keyboard activation | resolved | bbc_efb/logbook_views/dashboard.py EnterActivates | test_logviews_statistics test_recent_flights_open_with_return_and_keypad_enter (added this round) | None. |
| A121 | Full listing and table rebuild on the GUI thread on every save and every search keystroke | resolved | bbc_efb/logbook_views/history.py model/proxy; filters.py debounce; storage listing cache | test_logviews_history | Real Windows timing unmeasured. |
| A122 | refresh(store) does not reset selected/editing/detail state when the data folder changes | resolved | bbc_efb/logbook.py set_store/_rebase_editor | test_efb_logbook | None. |
| A123 | 'Time entered' totals omit flights with unknown duration without saying so (Q10 honest-labelling requirement) | resolved | bbc_efb/logbook_views/statistics.py, dashboard.py; logbook.py _warning_text | test_logviews_statistics | None. |
| A124 | History selection jumps to the last-opened flight when the search text changes, so Open flight can open a different f... | resolved | bbc_efb/logbook_views/history.py selection model | test_logviews_history | None. |
| A125 | FlightStore cannot create its data folder on first run (no parents=True) | resolved | bbc_efb/storage.py FlightStore(create=False); app.py; settings.py | test_efb_storage | None. |
| A126 | Filename case or alternative UUID spellings make a valid record unreadable | resolved | bbc_efb/storage.py _canonical_id/_name_rank | test_efb_storage | None. |
| A127 | A record with any field this version doesn't know becomes unreadable (FlightFields(**fields)); there is no forward-co... | resolved | bbc_efb/storage.py _parse_fields extras; read-only newer schema | test_efb_storage | None. |
| A128 | Every save rewrites all earlier revisions in normalised form, destroying stored originals (conflicts with Q9 'preserv... | resolved | bbc_efb/storage.py _payload_for_write/_merge; migration copy | test_efb_storage | None. |
| A129 | os.replace on Windows fails with PermissionError when another process has the target open, and save has no retry | needs native validation | bbc_efb/storage.py _replace_with_retry, FlightFileBusy; listing retry (this round) | test_efb_storage, test_storage_review_fixes (real Windows test skipped) | Run on Windows with OneDrive/Defender; msvcrt lock. |
| A130 | os.replace in FlightStore.save has no retry for Windows sharing violations (antivirus, OneDrive, Explorer preview) | needs native validation | same as A129; bbc_efb/recorder_sink.py on the save thread | test_efb_storage | Windows 11 OneDrive and Explorer preview pane. |
| A131 | Global '*' colour rule makes disabled inputs look enabled and may leave popup views (calendar, combo lists) as light... | needs native validation | bbc_efb/theme.py palette and stylesheet | test_efb_theme_widgets | Native Windows 11 style rendering and Text size setting. |
| A132 | publish.ps1 secret regex gives false positives on ordinary prose and is case-insensitive | resolved | publish.ps1 secret rules | test_repo_publish | Optional: run under PowerShell 5.1/7. |
| A133 | publish.ps1 secret scan skips binary and UTF-16 files, including the tracked 21 MB source zip and the .docx masters | needs native validation | publish.ps1 Get-ContentProblems (UTF-16, ZIP entries, refusals) | test_repo_publish (Python mirror) | publish.ps1 never run; owner decides on untracking the snapshot zip. |
| A134 | publish.ps1 stages everything before validating, mishandles single-item -match, and parses quoted paths | needs native validation | publish.ps1 temporary index, per-path match, -z records | test_repo_publish (static/mirror) | Run on PowerShell 5.1 and 7.3+. |
| A135 | publish.ps1 re-scans every commit in history on every publish, so cost grows with commits times tree size | needs native validation | publish.ps1 unpublished-commit scan | test_repo_publish | Run on Windows against a real remote. |
| A136 | publish.ps1 does not check commit author or committer identity; history already includes a personal email | needs native validation | publish.ps1 identity checks | test_repo_publish | Run on Windows; history rewrite of 0c5fb13 is an owner decision. |
| A137 | Live evidence behind ticked checklist items is missing from the repository | owner decision | tests/test_repo_offline_guard.py; sources/07, 08, 10 evidence labels | test_repo_offline_guard, test_repo_dictionary EvidenceLinkTests | Commit redacted live reports and a live probe script, or not. |
| A138 | Function dictionary has stale or contradictory status text | resolved | sources/10_SIMCONNECT_FUNCTION_DICTIONARY.md | test_repo_dictionary, test_repo_docs | None. |
| A139 | Test clocks are patched per module, inconsistently; frozen time with unpatched sleep can hang instead of failing | resolved | tests/test_adapter_support.py FakeClock; test_sdk_simvar_offline, test_aircraft_inputs_offline, test_client_data_offline, test_core_offline, test_integration_cross_file moved to it; test_adapter_sdk_transport and test_core_parity moved to it (sleep patched too) | test_core_regressions test_offline_adapter_suites_share_the_fake_clock; test_core_static (every test class that runs a SimConnectProvider calls start_clock; start_clock patches sleep) | None. |
| A140 | No tests for the lifecycle failure paths above | resolved | tests/test_adapter_facilities.py lifecycle tests | test_adapter_facilities, test_core_failures | None. |
| A141 | No performance regression tests; the test fake hides queue costs | resolved | tests/test_core_performance.py scaling guards count executed Python lines with sys.settrace instead of measuring wall-clock time, so results do not depend on the machine | test_core_performance (readings 1000 vs 4000: 3.98x against a 6x limit; 2 vs 8 shared consumers: 2.5x; idle pump with 10 vs 1000 subscriptions: 1.0x against 1.1x; an injected queue-rescanning offer fails the guard) | None. |
| A142 | Offline test portability and test runner gaps | resolved | tests/__init__.py; simconnect_sdk.py _is_dll_path | test_repo_offline_guard, test_repo_docs, test_adapter_sdk_transport | None. |

## Defects found in the follow-up review and fixed

The fix rounds after the first resolution pass also fixed defects that were not in the A-list. Each has a regression test.

| Area | Defect | Fixed in | Test |
|---|---|---|---|
| Core | Resubscribing a signal before the next pump left it UNAVAILABLE | bbc_core/core.py (_retiring set, _retire_signals) | tests/test_core_regressions.py ResubscribeBetweenPumpsTests |
| Core | poll() reserved batch slots for stale reports it could not send (nothing delivered with limit=1) | bbc_core/core.py _poll | test_core_regressions PollReserveTests |
| Core | Custom client events were upper-cased on subscribe but not on transmit | bbc_core/provider.py Signal | test_client_events_offline, test_core_regressions |
| Core | A FlightTape event row with no value replayed as a SignalError | bbc_core/simulated.py _apply_tape | test_core_regressions |
| Core | Simulated transport loss reported as ERROR while a state refresh was waiting | bbc_core/simulated.py request_system_state | test_core_regressions |
| Adapter | ObjectRemoved after a CREATE_* command wiped the aircraft input catalog | bbc_core/simconnect.py _retain_aircraft_lifecycle | tests/test_adapter_round4.py ObjectRemovedTests |
| Adapter | A single-field SimVar write refused asynchronously stayed UNCERTAIN and blocked writes | bbc_core/simconnect.py _receive_exception | test_adapter_round4 AsyncSimVarRefusalTests |
| Adapter | Retrying a failed system-event registration used a stale Demand | bbc_core/simconnect.py replace_demand | test_adapter_round4 EventRegistrationRetryTests |
| Adapter | _retire could unsubscribe or unmap a shared lifecycle id | bbc_core/simconnect.py _retire/_register | test_adapter_round4 |
| Recorder | A failed journal write left the record marked clean | bbc_recorder/journal.py, service.py, recovery.py, records.py; bbc_efb/recorder_sink.py | tests/test_recorder_integrity_fixes.py |
| Recorder | The notable_events cap could drop milestones (and lose a flight in recovery) | bbc_recorder/journal.py, service.py, recovery.py | test_recorder_integrity_fixes |
| Recorder | A crash during the arrival wait recovered as interrupted with an invented gap | bbc_recorder/recovery.py | test_recorder_integrity_fixes |
| Recorder | Overlap de-duplication ignored samples after a simulator time reset | bbc_recorder/service.py _on_snapshot | test_recorder_integrity_fixes |
| Storage | listing() cached transient OSErrors for the whole session | bbc_efb/storage.py listing/_read_for_listing | tests/test_storage_review_fixes.py |
| Storage | BOM flight files counted as unreadable in Export and Backup | bbc_efb/data_management.py | test_storage_review_fixes |
| Storage | set_panel_visible ran listeners under the lock and dropped other stored entries | bbc_efb/preferences.py | test_storage_review_fixes |
| Storage | NaN/Infinity preferences loaded but could never be saved | bbc_efb/preferences.py | test_storage_review_fixes |
| Storage | DataFolderSettings.remember had no retry and overwrote a damaged or unreadable settings.json | bbc_efb/storage_settings.py | test_storage_review_fixes |
| Logbook | Editor overflow for numbers outside the int32 spin range wiped fields on save | bbc_efb/logbook.py | tests/test_logviews_regressions.py |
| Logbook | Black Box selection jumped to another flight after accepting gaps | logbook_views/recorder_management.py | test_logviews_regressions |
| Logbook | Gap acknowledgement skipped the conflict check | logbook_views/recorder_management.py StoreActions.acknowledge_gaps | test_logviews_regressions |
| Logbook | Flight Details stuck on "no longer in the Logbook folder" | logbook_views/details.py | test_logviews_regressions |
| Logbook | A deeply nested journal line raised RecursionError | logbook_views/model.py load_journal | test_logviews_regressions |
| Logbook | Black Box journal parsed on the UI thread | logbook_views/details.py, dashboard.py; logbook.py | test_logviews_regressions |
| Logbook | Touchdown-rate column sorted by sign, not magnitude | logbook_views/history.py | test_logviews_regressions |
| Logbook | Relative date filters froze at the day they were chosen | logbook_views/filters.py, history.py | test_logviews_regressions |
| Shell | A folder switch saved the old folder's pending flight into the new folder | bbc_efb/app.py _bound_store | tests/test_shell_review_followups.py |
| Shell | A flight opening during the veto dialog could be cut by the folder switch | bbc_efb/sections.py DataFolderControl | test_shell_review_followups |
| Shell | A retired log handler could reopen the old folder's log file | bbc_efb/logging_setup.py | test_shell_review_followups |
| Shell | Settings promised the chosen SimConnect.dll is used at the next connection (it needs a restart) | bbc_efb/settings.py | test_shell_review_followups |
| Modules | A module tick() raising SystemExit closed the EFB | bbc_efb/modules/loader.py; bbc_efb/shell.py tick_modules | tests/test_modules_regressions.py, tests/test_shell_module_listener.py |
| Modules | A module turned on from Settings never ticked until minimise and restore | bbc_efb/modules/loader.py add_listener; bbc_efb/shell.py _watch_module_host | test_modules_regressions, test_shell_module_listener |
| Modules | module-state.json from a newer EFB was renamed and overwritten | bbc_efb/modules/loader.py; bbc_efb/settings.py (switches disabled) | test_modules_regressions, test_settings_ui |
| Modules | A deeply nested module.json disabled the module host | bbc_efb/modules/manifest.py | test_modules_regressions |
| Modules | A copied folder relabelled a running module as "folder was removed" | bbc_efb/modules/loader.py | test_modules_regressions |
| IPC | An interrupted first start could leave an empty key file | bbc_core/ipc/protocol.py load_or_create_authkey | tests/test_ipc_hardening.py |
| IPC | Named-pipe listener accepted remote SMB clients | bbc_core/ipc/server.py open_listener | test_ipc_hardening (stand-ins only) |

### Changes in the final integration pass

| Change | Files | Test |
|---|---|---|
| The Shell follows the current module host (and the new one after a folder switch) through `add_listener`, queued onto the UI thread; `tick_modules` also contains `SystemExit` | bbc_efb/shell.py | tests/test_shell_module_listener.py |
| The Recorder sink refuses to create a second copy when an unreadable Logbook file names the same Recorder flight, or when a flight file cannot be read right now; the journal is kept and the save retried | bbc_efb/recorder_sink.py find_existing | tests/test_service_sink.py (3 tests) |
| The Settings panel toggle changes one entry through `Preferences.set_panel_visible`, so stored entries this build rejects are kept | bbc_efb/settings.py _panel_changed/_save_prefs | tests/test_settings_ui.py test_invalid_stored_panel_entry_survives_a_toggle |
| Module switches and grants are disabled when module settings were written by a newer EFB | bbc_efb/settings.py | test_settings_ui test_settings_from_a_newer_efb_are_shown_and_cannot_be_changed |
| A folder choice that cannot be remembered for the next start now leaves a notice for the shell's banner, not only a log line | bbc_efb/sections.py _remember | test_shell_review_followups RememberFailureNoticeTests |
| `from __future__ import annotations` in the last five bbc_core modules; `ObjectSample.value` annotated `builtins.object`, because postponed evaluation would resolve `object` to the class's own field | bbc_core/simulator_ops.py, facilities.py, aircraft_inputs.py, `__init__.py`, ipc/`__init__.py` | test_core_regressions test_every_bbc_core_module_uses_postponed_annotations; test_ipc_protocol |
| Docs updated for `StoreActions.acknowledge_gaps` and `PIPE_REJECT_REMOTE_CLIENTS` | bbc_efb/logbook_views/LOGBOOK_VIEWS.md; docs/STANDALONE_CORE_IPC.md | (docs) |
| Flaky timing tests made deterministic without weakening them: the provider flood test pumps until the SDK is empty (the drain has a 20 ms real-time budget); the Core scaling guard pauses GC and takes the best of 5 (an injected O(N^2) still fails); the background-save status test hands off to the reader after each tick | tests/test_adapter_provider.py, test_core_performance.py, test_service_unit.py | (same) |
| A120 and A93 regression tests committed (previously scratch-only) | tests/test_logviews_statistics.py, tests/test_adapter_provider.py | (same) |

### Changes in the closing pass

| Change | Files | Test |
|---|---|---|
| A68: the pilot can see and resolve every field the Recorder marks as needing confirmation. `FlightStore.confirm` writes a pilot revision that repeats the stored values and lists them in `confirmed` (conflict-checked by digest, refused on read-only records, nothing written when already confirmed). A field counts as confirmed only when the pilot confirmed it after its last change; confirmations in Recorder or import revisions are ignored. A pilot's answer to a suspected diversion is stored as `diversion` and carried forward | bbc_efb/storage.py; bbc_efb/STORAGE_SCHEMA.md | tests/test_field_confirmation.py |
| A68: History leads with a "Needs confirmation" marker (warning colour, reasons in the tooltip, sorted first), and a "Field confirmation" filter (Any / Needs confirmation / Confirmed by the pilot); `record_status()` is unchanged | logbook_views/model.py, history.py, cards.py, filters.py | test_field_confirmation |
| A68: Flight Details offers "Confirm as correct" and "Correct…" per field, and "Confirm diversion" / "Not a diversion" for a suspected diversion, with the same permission as Edit flight. The save goes through `StoreActions.confirm_fields(seen=view)`, which raises `StaleListing` and writes nothing if the flight changed since it was shown. Record History shows confirmations and a "Pilot confirmed" column | logbook_views/details.py, dashboard.py, recorder_management.py; bbc_efb/logbook.py; logbook_views/LOGBOOK_VIEWS.md | test_field_confirmation |
| `LogbookSink.find_existing` also checks misnamed files (for example sync-tool copies) and duplicates, not only unreadable files; when one holds the same Recorder flight or cannot be read, the journal is kept and no duplicate is created | bbc_efb/recorder_sink.py | tests/test_service_sink.py (2 new tests) |
| A114: a SimConnect.dll chosen in Settings is used at the next connection attempt, never during a connected session. A replacement that cannot be built shows an honest status and is retried with backoff; the old DLL is not quietly used instead. Settings, the choice label and diagnostics say when the new file applies | bbc_core/host.py; bbc_efb/app.py, settings.py, core_bridge.py | tests/test_host_provider_swap.py, tests/test_shell_dll_switch.py, tests/test_settings_dll_live.py, test_shell_review_followups |
| A failure to remember the data folder at start-up now reaches the notice banner (`StartupReport.tell_pilot`), and `DataFolderControl._remember` reports non-OSError failures too | bbc_efb/app.py, sections.py | test_shell_dll_switch StartupRememberFailureTests |
| Settings shows the read-only message whenever module settings were written by a newer EFB, even with no other problem text | bbc_efb/settings.py | test_settings_dll_live NewerModuleSettingsTests |
| A89: stdlib static checks (unused imports in every package, `get_type_hints` over all bbc_core dataclasses, SDK protocol conformance); the last three unused imports removed; `_idle` no longer calls the optional `sdk.wait` directly | tests/test_core_static.py; bbc_core/simconnect.py; bbc_recorder/attribution.py; bbc_efb/logbook.py; pyproject.toml comment | test_core_static |
| A36, A37, A139, A141: provider failure-path tests, the Client Data ON_SET refresh test, the remaining provider tests on the shared FakeClock, and work-counting scaling guards | tests/test_adapter_failures.py, test_client_data_offline.py, test_adapter_sdk_transport.py, test_core_parity.py, test_core_performance.py | (same) |
| The guide's `PIPE_REJECT_REMOTE_CLIENTS` and default-DACL statements are guarded by a test | docs/STANDALONE_CORE_IPC.md | tests/test_ipc_hardening.py PipeSecurityDocumentationTests |

## Still needs native Windows / MSFS validation

None of these can be exercised on this Mac. Each item says what to check and where the code is.

**Core and SimConnect transport**

- A1: SimConnect signals the Win32 event passed to SimConnect_Open; the Core worker wakes promptly with low idle CPU; the window stays responsive during connect and the COM baseline. `bbc_core/simconnect_sdk.py` (CreateEventW, wait/wake), `bbc_core/host.py`.
- A9: real "MSFS not running" and delayed-launch failure strings match `describe_connect_failure`; reconnect and aircraft reload. `bbc_core/host.py`.
- A16 to A19, A27: pull dispatch (`callback_dispatch=False`) idle result is E_FAIL with a NULL pointer, and the recorded first-receive failure is gone. Pull mode stays opt-in until then. `bbc_core/simconnect_sdk.py` next_packet.
- A73, A81, A115: `ctypes.WinDLL` load, SEH-to-OSError mapping, older SimConnect.dll with missing optional exports. `bbc_core/simconnect_sdk.py`.
- A72: `test_adapter_abi.test_signatures_match_the_installed_header` against the 0.24.6.0 SDK header (`MSFS_SDK` set). `tests/test_adapter_abi.py`.
- A57 to A59, A116: callback buffer under a real long stall. `bbc_core/simconnect_sdk.py`.
- A111, A95, A94: frame-time cost on a slow PC; pump keeping up with SIM_FRAME / VISUAL_FRAME snapshots at 30-60 Hz; per-SimVar request cost of plain subscriptions. `bbc_core/simconnect.py`.
- A13, A12, A14: live 1 Hz receive cadence and jitter, snapshot field and clock values (SIMULATION TIME, ABSOLUTE TIME, SIMULATION RATE).
- A28: Pause_EX1 bit values and Active Pause behaviour. `bbc_core/contract.py` PauseState, `bbc_core/catalog.py`.
- A33: AircraftLoaded, FlightLoaded, FlightPlanActivated and Sim events fire on aircraft or flight change and refreshed system-state replies arrive. `bbc_core/core.py` _LIFECYCLE_STATES.
- A45: real dwDefineCount for TEXT SimVars. `bbc_core/simconnect.py` _payload.
- A48: AI object spawn latency (late ASSIGNED_OBJECT_ID). `bbc_core/simconnect_advanced.py`.
- A50: MSFS returns ALREADY_CREATED for a Client Data area on reconnect. `bbc_core/simconnect_client_data.py`.
- A51, A52, A54, A108, A109, A110: facility behaviour: JETWAY_DATA for airports without jetways, worldwide-list duration, cache churn at the menu, per-field exception for refused jetway fields, removals before the initial list, real list-page padding. `bbc_core/simconnect_facilities.py`, `simconnect_facility_cache.py`, `simconnect_facility_lists.py`.
- A56: descriptor eType values of real aircraft input events. `bbc_core/simconnect_input_events.py`.
- A75, A97: how COM ACTIVE/STANDBY frequencies are represented, and which grid COM STANDBY reports after COM_STBY_RADIO_SET_HZ for 25 kHz and 8.33 kHz. `bbc_core/simconnect.py`, `bbc_core/catalog.py` com_channel_problem.
- A91: which SimVars MSFS stores as float32 (readback tolerance). `bbc_core/simconnect.py`.
- A100, A101: hardware input and reserved-key presses arrive through the notification group. `bbc_core/simconnect_advanced.py`, `simconnect_client_events.py`.
- A105: whether the MSFS 2020 VOR/NDB facility schema really lacks ICAO/REGION. `bbc_core/simconnect_facilities.py`.
- A113: a slow WASM gauge answering GetInputEvent late. `bbc_core/simconnect_input_events.py`.
- A118: which flight-file form (with or without .pln/.flt) SET_AI_FLIGHT_PLAN, CREATE_ENROUTE_AIRCRAFT and LOAD_FLIGHT_PLAN accept. `bbc_core/simulator_ops.py` SEND_FLIGHT_FILE_EXTENSION.
- A76, A90: SimConnect name case rules for Client Data areas and custom events.
- A60: capture real 1 Hz and visual-frame tapes with `bbc_recorder/tape.py` and add them as regression fixtures (every tape in the repo is synthetic).

**Recorder and storage on Windows**

- A5: an automatically recorded flight end to end in MSFS, after the owner approves the rules.
- A6: journal durability (FlushFileBuffers), msvcrt sidecar lock, recovery at start-up. `bbc_recorder/journal.py`, `recovery.py`.
- A22, A67, A129, A130: run `tests/test_efb_storage.py` on Windows NTFS (the skipped `test_real_windows_sharing_violation_clears_within_retry`), with OneDrive-synced Documents, Defender and the Explorer preview pane; msvcrt writer lock across processes. `bbc_efb/storage.py`.
- A25: copying a journal the Recorder is appending to, and renaming the in-use folder during Restore. `bbc_efb/data_management.py`.
- A23: native folder dialog and OneDrive redirection detection. `bbc_efb/settings.py`, `storage_settings.py`.
- Storage follow-up: the listing retry on WinError 5/32/33 (`bbc_efb/storage.py` _read_for_listing) and the settings.json retry (`storage_settings.py`).

**EFB shell, UI and packaging**

- A3, A4: Windows launch, frameless move and resize, Snap, dual footer, clean close. `bbc_efb/shell.py`.
- A131: dark palette, disabled inputs and calendar/combo popups under the native Windows 11 style, with the Windows Text size changed. `bbc_efb/theme.py`.
- A8: `bbc-efb.log` and `fault.log` written by a windowed PyInstaller build. `bbc_efb/logging_setup.py`.
- A24, A114: portable build on Windows (`packaging/build_portable.ps1`, `packaging/bbc-efb.spec`), clean ZIP launch, `_MEIPASS`/exe-folder/MSFS_SDK DLL discovery, test fixtures and demo code excluded.
- A114: switching SimConnect.dll without a restart: loading a second SimConnect.dll in the same process, and the switch at the next connection attempt against MSFS. `bbc_core/host.py` replace_provider_factory, `bbc_efb/app.py` SimConnectDllSwitch.
- A68: the field confirmation actions, History marker and filter on Windows, with a real recorded flight that has a TITLE-fallback aircraft or a suspected diversion. `bbc_efb/logbook_views/details.py`, `history.py`.
- A21: owner visual acceptance of the Logbook views on Windows.
- A7: CI workflow (`.github/workflows/ci.yml`) has never run; Python 3.10 untested; the advisory ruff/mypy job (A89) has never run.

**Publishing and IPC**

- A132 to A136: `publish.ps1` has never run. Run it on Windows PowerShell 5.1 and 7.3+ against a scratch repo (UTF-16 secret, secret inside a .docx, a .tgz, non-ASCII paths, `$PSNativeCommandUseErrorActionPreference=$true`), and check fetch, rev-list --not and the identity checks.
- A69: the named-pipe transport, `PIPE_REJECT_REMOTE_CLIENTS` and the authkey link-into-place on a Windows volume. `bbc_core/ipc/server.py`, `protocol.py`.

## Owner decisions proposed

None of these is approved by being listed. Each names where the proposal or open item is recorded.

| # | Decision | Where |
|---|---|---|
| 1 | Approve the Recorder rules: start and end, clocks, pause and Active Pause policy, sim rate, touchdown and bounce, gaps, recovery (A5, A28) | `bbc_recorder/RULES.md` (marked PROPOSAL); `sources/08_DECISIONS_AND_READINESS.md` line 60 |
| 2 | Recorder service rules P-S1 to P-S9, and no automatic arming after SimStop until SimStart | `sources/08_DECISIONS_AND_READINESS.md` lines 75-76; `bbc_recorder/RULES.md` |
| 3 | Start-up recovery saves interrupted flights without asking (current) versus asking the pilot first (P-R1); Resume not offered (A6) | `sources/08_DECISIONS_AND_READINESS.md` line 77; `bbc_recorder/recovery.py`, `bbc_recorder/service.py` recover_unfinished |
| 4 | Journal retention: `flight_logging.journal_retention_days` is saved but nothing deletes journals; one discarded journal per session accumulates (A6) | `sources/08_DECISIONS_AND_READINESS.md` line 78; `bbc_recorder/journal.py` |
| 5 | Accept storage schema 2 (A2) | `bbc_efb/STORAGE_SCHEMA.md`; `bbc_efb/storage.py` |
| 6 | Logbook view keys marked PROPOSAL (operation, distance, touchdowns, recorder_state), and a retention policy before Delete is enabled (A21) | `bbc_efb/logbook_views/LOGBOOK_VIEWS.md` |
| 7 | Whether the aircraft field may be filled from the low-confidence TITLE fallback (A68). The "confirm as correct" pilot revision now exists (`FlightStore.confirm`), so such a field is marked and confirmable either way | `bbc_recorder/records.py`, `bbc_recorder/attribution.py` |
| 8 | Version `0.1.0a1`, automatic `bbc_*` package discovery, CI matrix, and the API version compatibility rule (A7) | `sources/08_DECISIONS_AND_READINESS.md` lines 67-69; `pyproject.toml`; `bbc_core/ipc/protocol.py` parse_api_version |
| 9 | First Windows runs of the guarded suite, `publish.ps1` and `packaging/build_portable.ps1` | `sources/08_DECISIONS_AND_READINESS.md` line 70 |
| 10 | Packaging: freezer, dependency pins, two ZIPs, excluding the simulated provider from deployed builds (sources/08 requires it; the build includes it by default), licence texts, code signing, SimConnect.dll redistribution (A24, A114) | `sources/08_DECISIONS_AND_READINESS.md` line 74; `packaging/README.md`, `packaging/LIMITATIONS.md`, `packaging/bbc-efb.spec` |
| 11 | CoreHost reconnect schedule 1/2/5/10/30 s (A9) | `bbc_core/host.py` module docstring |
| 12 | Drop-in modules: trust and isolation model (in-process trusted code versus out-of-process over bbc_core.ipc, signing or allow-list) and the manifest format; ConsumerAPI `_host` is still reachable in-process (A70) | `docs/MODULE_AUTHORING.md` (PROPOSALS); `bbc_efb/modules/loader.py`, `manifest.py`; `bbc_core/core.py` ConsumerAPI |
| 13 | Standalone Core IPC: compatibility, grant and delivery rules, and Windows pipe hardening (owner-only DACL still missing) (A69) | `docs/STANDALONE_CORE_IPC.md` section 11 |
| 14 | Plain subscriptions: group them into shared multi-datum requests with FLAG_CHANGED, or record that they stay per-SimVar and bulk readers must use snapshots (A94) | `bbc_core/simconnect.py` _register; `bbc_core/core.py` subscribe_snapshot |
| 15 | Client Data VISUAL_FRAME default: CHANGED by default, a per-consumer rate or size cap, or keep the full stream (A103) | `bbc_core/core.py` subscribe_client_data; `bbc_core/simconnect_client_data.py` |
| 16 | VOR/NDB detail queries: require a non-empty region in `Core.request_navaid` (breaks current empty-region lookups) or keep the honest "ident unconfirmed" label (A105) | `bbc_core/core.py` request_navaid; `bbc_core/simconnect_facilities.py` _NAVAID_IDENTITY_NOTE |
| 17 | Untrack `bbc-efb-source-53d2091.zip` (A133) | `sources/08_DECISIONS_AND_READINESS.md` line 71 |
| 18 | Rewrite history to remove the personal email on commit 0c5fb13, and GitHub email-privacy settings (A136) | `sources/08_DECISIONS_AND_READINESS.md` line 72 |
| 19 | Commit redacted live read-only reports and a live probe script (A137) | `sources/08_DECISIONS_AND_READINESS.md` line 73 |
| 20 | Whether the offline guard may allow AF_UNIX socket files so the IPC tests run under it | `sources/08_DECISIONS_AND_READINESS.md` line 80; `tests/test_repo_offline_guard.py` |
| 21 | Client Data area names keep their declared case by default (A76) | `sources/08_DECISIONS_AND_READINESS.md` line 79; `bbc_core/client_data.py` |
| 22 | Make the CI static-checks job (ruff 0.6.9, mypy 1.11.2) blocking after a first clean run (A89) | `.github/workflows/ci.yml`; `pyproject.toml` [tool.ruff], [tool.mypy] |
| 23 | When BBC EFB started without finding SimConnect.dll there is no Core host, so a DLL chosen later still needs a restart (Settings says so). Creating the host later would mean rebuilding the bridge, Recorder and modules. A choice made while a host exists is now applied at the next connection attempt (A114) | `bbc_efb/app.py` create_core, SimConnectDllSwitch; `bbc_core/host.py` replace_provider_factory; `bbc_efb/settings.py` |
| 24 | Pull dispatch stays opt-in and callback dispatch the default until a live run (A16 to A19) | `bbc_core/simconnect_sdk.py` docstring; `sources/10_SIMCONNECT_FUNCTION_DICTIONARY.md` |
