# Development status and evidence

| Evidence | Result |
|---|---|
| R3 initial simulated suite | 8/8 passed. |
| R3 extended simulated suite | 21 passed and 2 failed out of 23. |
| R3.1 simulated suite | 31/31 passed on Windows CPython 3.12.14 x64, 0 failures/errors/skips. Fixed early CONNECTED, repeated teardown diagnostics and unread disconnect notices across fast reconnect. |
| Windows wrapper API | Actual supplied v0.7.2 wheel imported. Datatypes, signatures and closed-client paths verified without DLL. |
| R3.1 SDK DLL load-only | SDK 0.24.6.0 x64 DLL loaded and all 12 former wrapper-declared exports resolved. No SimConnect functions called. |
| R3.1 wrapper preload | BLOCKED. The wheel and source distribution omit `find_simconnect_dll` and `is_untrusted_simconnect_dll` imports in `simconnect_native/client.py`. Explicit DLL path still hits `NameError` on the latter at line 215. |
| Live simulator, Recorder, complete EFB | No live session, implemented Flight Recorder, current runnable EFB shell or packaged build demonstrated. |
| Fresh Core offline suite | 14/14 passed on Windows CPython 3.12.14 x64. Eleven simulated Core checks and three checks of the initial declarations against the installed SDK header. No SimConnect DLL was loaded. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_first_offline_2026-11-24/REPORT.md) |
| First direct adapter offline suite | 28/28 passed on Windows CPython 3.12.14 x64, including 14 injected-SDK adapter cases. No DLL or simulator was used. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_native_adapter_offline_2026-11-24/REPORT.md) |
| Direct adapter SDK DLL preflight | PASS. The new loader resolved 13/13 exports from the SHA-256-verified SDK 0.24.6.0 x64 DLL. Zero SimConnect function calls and zero guard violations. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_sdk_dll_preflight_2026-11-24/REPORT.md) |
| Expanded Core write paths | 32/32 passed on Windows x64, including four new guarded fake-SDK cases for typed SimVar writes and extended event commands. No DLL loading or simulator connection. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_expanded_writes_offline_2026-11-24/REPORT.md) |
| SimConnect function dictionary | Static PASS. 91 unique dictionary entries match 91 declarations and 91 x64 DLL export names read from disk. The inventory checkpoint recorded 13 adapter calls. [Dictionary](10_SIMCONNECT_FUNCTION_DICTIONARY.md) · [Verification](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_simconnect_dictionary_2026-11-24/REPORT.md) |
| Aircraft Input Events | 41/41 offline cases passed, including nine new fake-SDK cases. The six Input Event functions are implemented and checked offline. The expanded 19-export loader and live aircraft behaviour are unverified. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_aircraft_inputs_offline_2026-11-24/REPORT.md) |
| Client Data | 49/49 offline cases passed, including eight new fake-SDK cases for named areas, shared reads, tagged writes and exact readback. Zero guard violations. The current adapter declares 25 exports. Native load-only evidence still covers the earlier 13. No simulator session or application build was run. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_client_data_offline_2026-11-24/REPORT.md) |
| Simulator session state and events | 58/58 offline cases passed, including nine fake-SDK cases for one-shot state and typed notifications, shared delivery control, and aircraft input lifecycle. The adapter declares 27 exports. The earlier load-only run covered 13. Zero guard violations, no DLL load or MSFS session. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_session_state_offline_2026-11-24/REPORT.md) |
| General SimVar and client event API | 66/66 offline cases passed, including eight fake-SDK cases for SDK-defined fields and events outside the starter catalog, shared reads, typed writes and readback, and event dispatch. Reuses the existing 27 SDK declarations. Zero guard violations, no DLL load or MSFS session. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_sdk_simvar_and_client_events_offline_2026-11-24/REPORT.md) |
| Airport facilities and jetways | 75/75 offline cases passed, including nine fake-SDK cases for typed airport details, static jetways and nearby airport lists. The adapter declares 30 exports. Native preflight still covers the original 13. Zero guard violations, no DLL load or MSFS session. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_airport_facilities_offline_2026-11-24/REPORT.md) |
| Navaid and jetway state queries | 82/82 offline cases passed, including seven fake-SDK cases for VOR, NDB, waypoint and spawned jetway query paths. The adapter declares 32 exports. Native preflight still covers the original 13. Zero guard violations, no DLL load or MSFS session. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_navaids_and_jetways_offline_2026-11-24/REPORT.md) |
| Facility lists and cache updates | 91/91 offline cases passed, including nine fake-SDK cases for worldwide airport searches, nearby navaid lists and shared cache subscriptions. The adapter declares 35 exports. Native preflight still covers the original 13. Zero guard violations, no DLL load or MSFS session. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_facility_lists_and_cache_offline_2026-11-24/REPORT.md) |
| Airport detail filters | 98/98 offline cases passed, including seven fake-SDK cases covering filtered airport details, concurrent requests, setup and clear errors, and safe definition reuse. The adapter declares 37 exports. Prior native preflight covers 13. Zero guard violations, no DLL load or MSFS session. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_airport_filters_offline_2026-11-24/REPORT.md) |
| Client event reception | 106/106 offline cases passed, including eight fake-SDK cases for shared non-masking subscriptions, ordinary and extended event payloads, filtering, teardown and errors. The adapter declares 41 exports. Prior native preflight covers 13. Zero guard violations, no DLL load or MSFS session. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_client_event_reception_offline_2026-11-24/REPORT.md) |
| Remaining SDK batch | 115/115 guarded offline cases passed, including nine new batch cases. The adapter implements 67 SDK paths, including three experimental calls and four host-side helpers. The dictionary accounts for all 91 entries. Prior native preflight covers 13. Zero guard violations, no DLL load, MSFS session or EFB build. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_remaining_sdk_batch_offline_2026-11-24/REPORT.md) |
| Expanded SDK DLL load-only preflight | PASS. The production loader resolved all 67 declared exports from the verified SDK 0.24.6.0 x64 DLL. A separate static check matched all 91 dictionary rows to the header and DLL export table. No SimConnect functions were called. Zero guard violations. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_sdk_dll_expanded_preflight_2026-11-24/REPORT.md) |
| Live default airliner read-only attempt | Blocked at the first local `SimConnect_Open` call with HRESULT `0x80004005`. No session or telemetry established. No simulator writes attempted. Default-aircraft identity and the cause of the connection error remain unverified. The PMDG run is pending. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_live_readonly_2026-11-24/DEFAULT_ATTEMPT_01.md) |
| Live default airliner read-only retry | PASS with callback dispatch on a parked, cold-and-dark Asobo Boeing 747-8i. Core read standard telemetry, both COM1 frequencies and 539 advertised aircraft inputs, then disconnected cleanly. Desktop pull dispatch obtained a handle but failed on its first receive call. No simulator writes, no app transport changes. [Report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_live_readonly_2026-11-24/DEFAULT_RETRY.md) |
| Live PMDG read-only probe | PASS with callback dispatch on the PMDG 737-800BCF in the owner's reported turnaround state. Title, position, on-ground state, groundspeed and both COM1 frequencies were read. The input catalog completed with zero entries. Core disconnected cleanly, and no simulator writes were attempted. [Comparison report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_live_readonly_2026-11-24/PMDG_READONLY.md) |

R3.1 is the latest complete candidate from the prior Core design, with public provider/broker interfaces and tests. R3.1, R3 and older snapshots are preserved as references. The new Core under `bbc_core/` has a starter direct SimConnect adapter with fake-SDK and header-derived offline evidence and a successful load-only preflight. Live evidence and full read/write coverage remain pending.

The R3.1 tree and original Main Development handoff were recovered on 23 November 2026. All 44 payload files in `BBC_EFB_Codex_Work_Package_2026-11-23.zip` matched the archive manifest. The 16 R3.1 files also matched the corresponding files in the previous workspace, and the 11 archive report files matched the reports already here. The copied files were verified by SHA-256. The original ZIP remains in the previous workspace's `handoff/` directory. No tests were run as part of recovery. [Recovered candidate](<../reference/01_CODEX_WORK_EVIDENCE/development reviews/BBC_EFB_Black_Box_Core_R3.1_Source_Candidate_2026-11-23/README.md>) · [Original handoff](../reference/01_CODEX_WORK_EVIDENCE/BBC_EFB_Main_Development_Handoff_2026-11-23.md)

Native teardown, reconnect and restore locks, event delivery, data cadence and simulator impact remain unverified. The former evaluation wrapper can conceal native retirement and close failures. Its high-level event callback lacks filename-bearing packets. See preserved reports under `reference/01_CODEX_WORK_EVIDENCE/test_artifacts/` and third-party details under `reference/EXTERNAL_DEPENDENCIES_NOT_BUNDLED.md`. The historical suites were preserved as recorded.

## Manual Logbook storage

25 November 2026. Nine offline cases passed on the workspace Windows Python runtime. They covered yearly save, reopen and edits, retained revisions, required fields, old per-flight records, simulated write failure, three interruption points in a year move, stale edits, a competing writer, damaged data and an annual file containing 5,000 flights. The Logbook UI passed a syntax check. Qt was not launched, and no EFB package or simulator connection was used.

Run the focused suite from the workspace root with the project's Python runtime:

```powershell
python -B -m unittest discover -s tests -p 'test_logbook_storage_offline.py' -v
```

## Offline EFB shell

25 November 2026. The source entry point launched into Dispatch and entered the Qt event loop. Five Qt offscreen UI tests passed on Windows using the existing PySide6 Essentials 6.10.2 dependencies. They covered wide and narrow navigation, both In-Flight examples, the footer links, folder selection, manual save/edit/reopen through the Logbook UI, displayed correction history and recalculated totals, the saved time display preference, custom maximise/restore/close controls and discard confirmation. The nine storage checks also passed. Wide Dispatch and narrow In-Flight captures were inspected using the local Segoe UI font. Native window dragging, Windows display scaling, a packaged app and SimConnect remain unverified in this slice.

Run the UI suite from the workspace root using the project Python runtime. The first line applies when using the existing workspace-local PySide6 installation.

```powershell
$env:PYTHONPATH = (Resolve-Path -LiteralPath '.concept-deps-py312').Path
python -B -m unittest discover -s tests -p 'test_efb_shell_ui.py' -v
```

## Offline EFB–Core connection status

25 November 2026. Seven Qt offscreen checks passed, including a simulated connection, loss and reconnect, a provider error with the Core message visible in Connection Diagnostics, and consumer cleanup on window close. The simulated provider was labelled throughout. Flight Logging stayed Inactive and the manual Logbook saved and reopened a record while Core was disconnected. Nine storage checks also passed.

The window takes a host-registered, read-only Core consumer. It reads the connection snapshot every 200 ms when a consumer is attached and releases the handle on close. The host owns the Core session. Normal launch has no provider and stays Disconnected. Native session wiring, simulator version identification and the Recorder remain pending. This check used no native DLL, simulator session or packaged build.

Use the project Python runtime and the workspace-local PySide6 setup above.

```powershell
python -B -m unittest discover -s tests -p 'test_efb_shell_ui.py' -v
python -B -m unittest discover -s tests -p 'test_logbook_storage_offline.py' -q
```

## EFB session host

25 November 2026. The EFB launch path can create one in-process Core with the existing callback-based SimConnect adapter. With an explicit absolute DLL path, Connection Diagnostics offers Connect, then Retry connection after an error or simulator quit. Connecting triggers local DLL loading. One worker owns open, dispatch and close. It waits for pilot action while disconnected and joins on app exit. The ordinary launch stays offline. Simulator identity and automatic Flight Logging are still pending.

All 133 offline tests passed on the project runtime. An injected SDK exercised a failed open, navigation during a pending connection, retry, dispatch of QUIT, reconnect with a new session ID and cleanup of both handles on one worker. A separate check verified that the opt-in DLL load step precedes the first SDK call and that a load failure prevents that call. The launcher's `--help` output was checked. These runs used no native DLL or MSFS session.

Use the project Python runtime and the workspace-local PySide6 setup above.

```powershell
python -B -m unittest discover -s tests -p 'test_*.py' -q
python -B -m bbc_efb --help
```

### Parked EFB read-only check

25 November 2026. The EFB launcher opened one callback-based session from Connection Diagnostics in MSFS 2020. The SimConnect footer changed from Disconnected to Connected. A read-only Core consumer received current live aircraft title, ground speed and on-ground telemetry for the parked Asobo Boeing 747-8i. Ground speed was effectively zero and on-ground was true. Flight Logging stayed Inactive. Closing the EFB closed the SDK handle and returned Core to Disconnected. The SDK call guard reported one Open, one Close and zero blocked calls or simulator writes. This verified the app session on MSFS 2020. Automatic simulator-product identification in the footer and the Recorder are still pending.

The check ran the source launcher through Qt's offscreen platform using a temporary settings file and the verified local DLL. The first invocation could not find the previously used Python runtime in the simulator desktop context and made no connection. The successful run used the installed x64 Python 3.12.10 and the existing workspace-local PySide6 dependencies.

### Separate owner-requested dome light adjustment

After the read-only run had closed, the owner-requested adjustment was made in a separate Core session on the parked Asobo 747-8i. The generic potentiometer and cabin switch events were sent without a change in readback. The aircraft advertised `LIGHTING_CABIN_3` with one `FLOAT64` parameter. Core sent 100 through that aircraft input. Separate SimVar readback confirmed the dome light on at 100%, with the aircraft still on the ground and the parking brake set. The session closed cleanly. This result applies to the tested aircraft input.

### Owner-requested simulated after-landing cleanup

On the parked Asobo 747-8i, the generic `FLAPS_UP` event was sent without a change in readback. The advertised `HANDLING_Flaps` input moved the lever to UP. A later sample confirmed both trailing-edge flap angles at effectively zero. The speedbrake was already stowed and disarmed. Four aircraft-specific landing light inputs were set to OFF and verified independently. The taxi light was already on. The parking brake remained set.

The owner confirmed that the cockpit autobrake selector stayed at **3**. Core's `AUTO BRAKE SWITCH CB` reading stayed at 4, which the installed Boeing knob template maps to detent 3. The SDK accepted `SET_AUTOBRAKE_CONTROL` with value 1, but the selector moved to neither OFF nor DISARM. The aircraft's autobrake knob input returned an empty parameter list and was left unused. The autobrake change failed. All sessions were closed.

## First automatic Recorder slice

25 November 2026. An offline flight replay drove the actual Core consumer, Recorder, annual JSON store and EFB History. The simulated flight captured gate out, takeoff, touchdown and gate in, kept the real-world Logbook date separate from simulator timestamps, and saved an observed −180 fpm landing rate. Closing and reopening the EFB preserved the record. Editing the arrival airport preserved its ID and recorded landing rate.

The offline suite also checked pause exclusion, missing touchdown evidence, retry of a failed final write and refusal to save an interrupted capture as a completed flight. The initial replay exposed equal receipt timestamps for several frames. Comparing both receipt timestamp and simulator clock value fixed dropped samples in replay. The full suite passed 138 tests. The normal app launch still needs the Recorder lifecycle and controls. Durable interruption recovery and native in-flight validation are separate milestones.

Use the project Python runtime and the workspace-local PySide6 setup above.

```powershell
python -B -m unittest discover -s tests -p 'test_*.py' -q
```
