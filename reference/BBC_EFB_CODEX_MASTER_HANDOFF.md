# BBC Integrated EFB — Codex Master Migration Handoff

**Checkpoint:** 23 November 2026  
**Status:** **OWNER-ORDERED MIGRATION FREEZE — preparation only.** No new implementation, tests, new dependency selection, production Core baseline or application build is authorised by this document.  
**First file to read in the new Codex workspace.** This handoff is a guide, **not** a replacement Project Source master or active `AGENTS.md`.

## 1. Product, goals and honest current status

**BBC Integrated Workstation and EFB for VATSIM** is a Windows 10/11 Electronic Flight Bag (EFB) and aviation workstation for flight simulation/VATSIM. It aims to reduce jumping among separate planning, flying, communications and logbook tools. The owner is a real-world pilot and decides aviation behaviour, UX and all major implementation/scope choices. Performance of Microsoft Flight Simulator (especially frame-time spikes), flight-log correctness, and robust offline operation outrank decorative functionality.

The three permanent primary Sections are **Dispatch**, **In-Flight**, **Logbook**; Settings is shared. The intended **NEW C0.1** is a complete modular application with the **full owner-approved Q1–Q14 Logbook functional experience** and **genuine local read-only MSFS 2020 automatic Flight Logging with manual override**. Full advanced drag/dock/layout customisation is deferred to C0.2+. An **internal earlier concept** with real manual Logbook persistence was discussed but **NOT approved**. The owner wants a runnable complete-EFB concept and will decide the first-build sequencing before implementation.

**What exists at migration:** nine current documentation masters, concept artwork, supporting handoffs, earlier component source snapshots, and **Black Box Core R3.1 as a complete source candidate**. Codex reported **31/31 guarded simulated offline tests passed** using Windows CPython 3.12.14 x64. The actual `simconnect-H` v0.7.2 wheel imported on Windows and a separate x64 SDK DLL loaded/resolved all 12 wrapper-declared exports without calling SimConnect. A later wrapper loader preflight raised `NameError` before session opening. **There is NO complete PySide6 EFB shell, NO implemented Flight Recorder, NO live MSFS 2020 session, NO verified automatic log, NO complete runnable NEW C0.1 application, and NO tested packaged EXE.** Earlier OLD project C0.1–C0.3 Tkinter concepts do not constitute current code.

**Authority transition:** The owner confirmed uploading eight updated masters on 23 Nov 2026; the unchanged founding prompt is ninth. They remain the reference authority until the owner explicitly completes a new Codex-source-authority cutover. Do not infer authority merely from the directory name. R3.1 is not an approved production baseline.

## 2. Source-of-truth order and exact reading map

Read these **byte-for-byte original files** under `00_CURRENT_PROJECT_SOURCE_MASTERS/`. They are the owner-confirmed migration masters, not newly rewritten requirements. Read later dated addenda of each master before older historical paragraphs. Only the owner may approve conflicts or changes.

| Order | Master, version/date | Primary passages and authority |
|---|---|---|
| 1 | `BBC_EFB_Project_Development_Record.txt` **v1.10**, 23 Nov 2026 | Overall architecture, approvals and history; **§30 migration freeze/current Codex evidence**; §29 23 Nov architecture and full C0.1 target; §§16–17 full shell and release/performance policy; §20 Core/Recorder boundaries. Supersedes v1.9. |
| 2 | `BBC_EFB_Development_Chat_Register.txt` **v1.10**, 23 Nov | Chat responsibilities, handoffs and provenance; **final migration register** makes former chats historical coordination records. Supersedes v1.9. |
| 3 | `BBC_EFB_NEW_C0.1_Scope_and_Readiness_Plan.txt` **v0.3**, 23 Nov | **§8 owner-approved C0.1 architecture/full Logbook**, §9 Core source/evaluation evidence (older snapshot), **§11 migration, updated R3.1 evidence and UNAPPROVED first-build A/B**. Supersedes v0.2. |
| 4 | `BBC_EFB_UI_Design_Standards.docx` **v1.2**, 23 Nov | Illustrated shared shell, footer, UI terms, resizing; **§11** PySide6 + responsive/scope decisions; **§12** migration/untested UI. Supersedes v1.1. |
| 5 | `BBC_EFB_Logbook_Design_Record.docx` **v1.2**, 23 Nov | Illustrated **Q1–Q14** decision register, details and sample visuals; current migration addendum. Supersedes v1.1. |
| 6 | `BBC_EFB_SimConnect_Integration_Development_Record.txt` **v0.8**, 23 Nov | SimConnect Q1–Q15 (Q11 withdrawn), Core architecture and R3.1/loader technical status; final v0.8 migration addendum. Supersedes v0.7. |
| 7 | `BBC_EFB_SimConnect_Data_Requirements_Register.txt` **v0.4**, 23 Nov | SC-001–016 + candidate F/I/T/Touchdown/Payload data. Final v0.4 evidence caveat: **no live data mapping validated**. Supersedes v0.3. |
| 8 | `BBC_EFB_Aircraft_Plugin_Development_Framework.txt` **v0.5**, 23 Nov | External plugin security and PMDG 737 research history; **§16 latest migration parking**; not a C0.1 plugin requirement. Supersedes v0.4. |
| 9 | `VATSIM_Integrated_Pilot_Workstation_Project_Master_Prompt.txt` **original, unchanged** | Founding vision and older assumptions; newer approved masters and owner decisions control when they differ. No dated new revision verified. |

**Original instructions:** `PROJECT_INSTRUCTIONS_ORIGINAL.md` is the 22 Nov condensed instruction text visible to this chat and byte-identical to a local file previously labelled 'Proposed'; its parity with the live Project Settings screen was **not** independently checked. `SPECIALIST_INSTRUCTIONS_ORIGINAL.md` and `CHAT_DECISIONS_AVAILABLE.md` preserve limited, identified excerpts, **not complete transcripts**. Do **not** autogenerate or activate `AGENTS.md` from them; review with the owner first. Any Codex-adapted rules belong in a later owner-reviewed document, not these originals.

**Supporting originals:** `02_SPECIALIST_HANDOFFS/` contains four 22–23 Nov source DOCX handoffs (Logbook Q1–Q7, Q8–Q14, telemetry research, Core C0.1 research). They supplement, **never overrule**, the nine masters. `03_HISTORICAL_CORE_SOURCE/` contains four preserved pre-R3 source ZIPs; historical only. `04_REFERENCE_ASSETS/` contains owner UI reference PNG, Club logo ZIP and architecture concept. Images show illustrative/sample data, not tested UI. 

**Evidence:** `01_CODEX_WORK_EVIDENCE/` is an **unaltered extraction** of the owner-supplied 23 Nov Codex ZIP. Its latest `development reviews/BBC_EFB_Black_Box_Core_R3.1_Source_Candidate_2026-11-23/` is a **candidate**, not approved production. Test reports are in `test_artifacts/`; original exact commands, output and traceback stay there. This package has been verified by size and SHA-256 during assembly; see `MANIFEST.json`.

## 3. Approved whole-EFB architecture and scope boundaries

**Approved directions:** Windows 10/11; **Python + PySide6/Qt Widgets** UI direction (exact version/packaging/licensing and performance still to validate); **NEW C0.1 = one modular process** (separate responsibility-based packages for shell, Core, Flight Recorder, storage, tests/assets). **C0.2+** intends separate EFB/Core/Recorder processes, with IPC/supervision not selected and cost/benefit still to verify. Shared source-independent/versioned Core consumer contracts; one shared, demand-driven read-only **local** MSFS 2020 SimConnect integration, bounded queues, telemetry freshness/gaps and safe shutdown. Core must not be tied to Qt or the Recorder. MSFS 2024 requires separate validation.

**Every authorised application build**, even an internal concept, contains Dispatch, In-Flight, Logbook, Settings access and **two independent persistent footer indicators**: SimConnect *connection* and Flight Logging *Recorder state*. Dispatch launches first. Loading UI Sections on demand is intended; when minimised, suspend avoidable visual updates but retain essential Recorder/Core active consumers. The In-Flight **right-hand Modules launcher must remain usable on narrow resize**. The approved styling is dark, slim, borderless, with custom window controls and hybrid adaptive reflow/minimum widths/scrolling. Never display mock telemetry, fake integrations, sample aircraft stats or imaginary flight records as live.

**Storage and distribution:** On first launch pilot chooses a writable local data folder (Documents, dedicated C: folder or custom); component-owned writes, shared safe file operations and single-writer record integrity. Prefer accessible JSON/JSONL/TXT/CSV; no mandatory cloud/SQLite. Export All Data and restorable backup are **different operations**. C0.1 distribution aim is a **portable ZIP containing the complete independently buildable EFB source tree**, build/run instructions, dependencies, changes, limitations and real test status. No installer selected. Nothing about this handoff authorises making it.

**Under evaluation / proposed:** `simconnect-H` v0.7.2 is the first library evaluated, **not** chosen permanently; official SDK/direct binding remains fallback if evidence justifies it; FSUIPC7 optional alternative neither approved nor rejected. Controlled simulator write/commands (e.g. COM radios) are a possible future guarded extension; C0.1 simulator operation is read-only. Optional connection profiles, services, LAN/API hosting, external modules and home cockpit support remain later questions; never start listeners or remote monitoring without approval. Performance targets (idle CPU <1%, typical average <3%, RAM <300MB, no measurable MSFS FPS/frame-time penalty) are **provisional and unproven**, not contractual test passes.

**Deferred / parked:** PMDG research only as earlier initial aircraft-specific feasibility direction; no plugin/SDK/user-script loader selected, no first C0.1 plugin requirement, no enabled broadcast settings. Fenix/iniBuilds later research. Advanced full panel dragging, docking, layout presets, and multi-process architecture are C0.2+ direction, not first shell gates. vPilot/SimBrief/VATSIM/Hoppie/weather/chart panels are potential integrations with actual provider/API/legal validation, not licence to embed/control other applications.

## 4. Approved UI and Logbook — Q1–Q14 in their original illustrated sources

See `00_CURRENT_PROJECT_SOURCE_MASTERS/BBC_EFB_Logbook_Design_Record.docx` **pp.1–31**, including cover decision index and illustrated Q1–Q14 pages; and `BBC_EFB_UI_Design_Standards.docx` **pp.1–7**. Historical illustrations still show subsequently removed Aircraft/Reports sidebar pages and mock values; newer text governs.

- **Q1:** Logbook Pages **Dashboard**, **Flight History**, **Black Box Recorder Management**, **Statistics**; no separate Aircraft or Reports page.
- **Q2:** Combined graphical Dashboard: flight/time/distance/landing metrics, aircraft-type side-profile cards, compact graphs, recent flights and quick links. Derive aggregates only from actual persisted records; display unknown metrics as unavailable.
- **Q3:** Flight History **Table, Cards and Combined**, persistent display preference, common filters/search and a large **in-EFB resizable Flight Details overlay** that retains selection and browsing context; never a separate Windows pop-out. Flight Details permanent **side-on aircraft-profile header** above Overview, Black Box, Operations, Notes and Record History sections/tabs as applicable (Q11).
- **Q4:** Recorder Management primarily an attention queue for **native recording gaps**; review, accept/ignore gap without destroying evidence, and delete as applicable. Do not flag ordinary missing native telemetry for imported/manual flights.
- **Q5:** Statistics combined overview: aircraft, operations, freight and landing statistics where data exists; do not infer unsupported freight/landing information.
- **Q6–Q7:** Aircraft and Reports sidebar Pages removed. **Main Settings > Data Management > Export All Data** is central and is **not** the restorable backup facility.
- **Q8:** Shared `Customise Layout` / `Done` design for Logbook and In-Flight; per-Section preferences. **C0.1 only basic saved visibility/display/layout preferences; C0.2+ full drag/reorder/dock/preset system.**
- **Q9:** Unified Flight Editor for manual and automatic records. **Required manual fields: aircraft type, From, To and flight date**; other fields optional. Retain original values, current edits, provenance/history and missingness. No diversion shown unless pilot confirms it.
- **Q10:** Optional pilot-entered crew, passenger, baggage, freight and payload data with honest totals; MSFS payload stations are not automatically commercial load categories. Detailed cargo assignment/manifests are deferred.
- **Q11:** Selected in-EFB Flight Details layout uses **persistent full side-on aircraft profile header** above tabs. Shared aircraft artwork library must be type-based; operator/livery cosmetic only; use generic correctly labelled silhouette if unavailable.
- **Q12:** Owner-approved **hybrid adaptive** resize: reflow where useful, protect minimum readable panel widths and scroll if needed; retain launcher and footer. Windows behavior untested.
- **Q13:** **Automatic Flight Logging with manual override**, Stop confirmation, privacy explanation in Main Settings. Actual event-detection and recording rules **not yet approved**. Closing during active logging: **Cancel / Save & Exit**, preserve incomplete record truthfully.
- **Q14:** SimConnect connection and Flight Logging status are **different** persistent side-by-side footer items, with different owners and interactions. SimConnect click opens `Settings > SimConnect > Connection Diagnostics`; Flight Logging status permits current-flight details/appropriate controls when implemented.

Show appropriate blank-state text when no flights exist; do not seed fake flights into real storage. Accessibility, display scaling, borderless Windows behaviours, minimum widths, chart library choice, actual asset licences and packaging still require verification.

## 5. Black Box Core versus Flight Recorder; unresolved semantics

**Core = shared provider gateway:** generic typed/unit-bearing/indexed SimVar and ordinary supported event requests; one shared consumer-driven connection, broker, freshness/source/session continuity, bounded delivery, health and reconnect/shutdown uncertainty. Any UI module may consume supported Core data directly. Current R3.1 `src/bbc_core/` implements **candidate** interfaces (`model.py`, `interfaces.py`, `broker.py`, `adapter_simconnect_h.py`, `codec.py`); read its `docs/INTERFACES.md`. The adapter uses numeric batches/string path separation and guards against stale session callbacks; test evidence is simulated unless separately labelled.

**Flight Recorder = independent Core consumer, Logbook-owned:** implements flight phase/event decisions, log state, timestamps, durable partial/completed records, provenance and audit, edit/correction history, pause/reconnect/interruption recovery and pilot UX. No Recorder implementation exists. A Recorder failure must not crash the Core or other UI consumers; a disconnected or stale simulator must not silently create false flight facts. Keep independent connection/logging status, no invented landing rates or ICAOs.

**Owner decisions still open:** exact flight start/finish (manual/automatic, on/off block), takeoff/touchdown criteria, single flight/circuits/multi-sector logic, bounce/touch-and-go, normal Pause versus Active Pause, wall/simulator/airborne/block clocks and sim-rate changes, flight plan changes and aircraft reload/slew, reconnect/stale data, corrupted/incomplete/aborted record recovery, durable write/schema/atomicity/import handling, truthful landing-rate sampling and missing-value treatment. See Logbook master and Data Requirements Register v0.4. Do **not** treat P1/P2/P3 research variables as owner-approved live mappings.

## 6. Original code and asset inventory (verified contents; not functionality)

| Relative path in this package | Contents and status | Requirements / dependencies |
|---|---|---|
| `01_CODEX_WORK_EVIDENCE/development reviews/BBC_EFB_Black_Box_Core_R3.1_Source_Candidate_2026-11-23/` | Latest **complete Core source candidate** (`src/bbc_core/`, tests, README, changelog, docs, `pyproject.toml`). **Not** integrated into full EFB or approved baseline. | Python ≥3.10 per Core metadata; Codex tested with Windows CPython 3.12.14 x64; optional `simconnect-H` v0.7.2 evaluation dependency. |
| `01_CODEX_WORK_EVIDENCE/development reviews/BBC_EFB_Black_Box_Core_R3_Source_Candidate_2026-11-23/` | Superseded R3 complete source/testing reference. **Do not merge files blindly into R3.1.** | historical debugging only |
| `03_HISTORICAL_CORE_SOURCE/*.zip` | Foundation, initial adapter, corrected draft and R2. Source evidence, **not** selectable active baselines. | Source-review only |
| `01_CODEX_WORK_EVIDENCE/test_artifacts/` | Original test reports, coverage matrices, raw output, exact commands and tracebacks; results are **reports from Codex Windows**, not tests rerun by this migration. | no new testing authorised |
| `02_SPECIALIST_HANDOFFS/*.docx` | Original Logbook and Core research/design handoffs. | original illustrated DOCX masters supersede standalone Logbook handoffs |
| `04_REFERENCE_ASSETS/` | Owner-designated UI image, Club logo package, architecture reference. | licensing/branding selection needs review; mock UI values are NOT live |
| `00_CURRENT_PROJECT_SOURCE_MASTERS/` | Nine full master documents, original filenames/bytes. | requirements authority until owner Codex cutover |

**Explicitly absent:** a current-project PySide6 application shell, Dispatch/In-Flight/Logbook executable, implemented Flight Recorder, tested manual record persistence UI, integrated GUI settings/indicators, whole-EFB source/release package, real flight data, reliable landing-rate algorithm, measured MSFS performance and live functionality. Do not reverse-engineer an application from historical UI screenshots or call R3.1 the whole EFB.

**External packages NOT bundled** here to prevent unclear third-party redistribution: `simconnect_h-0.7.2-py3-none-any.whl` (SHA-256 `ddd3216a4d8303e35c6e8cf2b469cdace443f0b9562c2f6052190023c53c6c7e`), `simconnect_h-0.7.2.tar.gz` (`56bd7fb24de7c2db42746a827ae92184059a462d7f67b2a9ae8f212c32dcd994`), both present in the earlier owner-provided transfer under `project sources/02_ALL_OTHER_AVAILABLE_FILES/`. The separate x64 MSFS SDK **0.24.6.0** `SimConnect.dll` was reported at `C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll`, SHA-256 `CFFB74B62B596A897B0FF101BAC3D66EA10236C540C75864658B451FFD1E6513`. **Neither wheel nor SDK DLL is selected/approved for release.** SDK licensing/redistribution check still pending. Retrieve/copy them locally **only after review of permissions/needs**, not as an automatic project dependency. Other historical assets/documents exist in the preserved old workspace; this package does not pretend to include all chat exports or historical 77-file transfer contents.

## 7. What was actually tested (Codex reports preserved verbatim)

Read the original reports under `01_CODEX_WORK_EVIDENCE/test_artifacts/` **before** drawing conclusions:

| Stage | Evidence file | Reported outcome | Limitation |
|---|---|---|---|
| Static/source review | R3.1 `README.md`, `CHANGELOG.md`, `docs/` | Source-integrity and suspected defects corrected to R3.1 candidate | Static review cannot prove native behaviour |
| R3 initial guarded simulated offline | `r3_offline_2026-11-23/REPORT.md`, `unittest.txt` | **8/8 passed** | fake wrapper; no DLL or MSFS |
| R3 extended | `r3_extended_offline_2026-11-23/REPORT.md`, `COVERAGE.md` | **21 passed / 2 failed** out of 23 | two defects prompted targeted R3.1 work |
| R3.1 extended guarded simulated | `r3_1_offline_2026-11-23/REPORT.md`, `COVERAGE.md`, `unittest.txt` | **31/31 passed**, 0 failures/errors/skips on Windows CPython 3.12.14 x64 | fake wrapper; bounded tests not proof of native concurrency, all failure paths or frame times |
| Real wheel API on Windows, **no DLL** | `r3_1_windows_wrapper_2026-11-23/REPORT.md` | **PASS**, wheel imported in isolated Python; datatype/closed-client signatures checked | no native DLL/session/telemetry |
| Standalone x64 SDK DLL load-only | `r3_1_dll_load_2026-11-23/REPORT.md` | **PASS**, SHA-256-matched SDK DLL loaded, 12 declared exports resolved | **0 SimConnect functions called**, no open session |
| Wrapper native preload preflight | `r3_1_live_preflight/REPORT.md` | **BLOCKED**: supplied v0.7.2 `simconnect_native/client.py` omitted imports `find_simconnect_dll` and `is_untrusted_simconnect_dll`; with explicit DLL path `NameError` on latter at line **215**. | MSFS **not running**, wrapper never opened native session; no live smoke test |
| Actual MSFS 2020/2024 and whole EFB | no evidence | **NOT TESTED / NOT BUILT** | no claim of performance, landing accuracy, auto logging or executable |

**Not a requirement change:** the Codex report updates *implementation/test status*, not the approved UI, Recorder, C0.1 or migration-hold requirements. `BBC_EFB_Main_Development_Handoff_2026-11-23.md` is copied inside the evidence folder and byte-identical to the separately uploaded version. The current v1.10/v0.8/v0.4/v0.3 masters already record the 31/31 result and native blocker; no newer unrecorded test claim was found in this transferred ZIP.

**Important wrapper uncertainty:** v0.7.2's native unsubscribe/close outcomes may be hidden; restored subscriptions/event payload and lock timing remain unproven; high-level sampling is not a verified subsecond touchdown solution. An SDK DLL load is not a working SimConnect integration. An external fixed wrapper or owner-approved local evaluation copy is needed before native preflight; never silently patch the upstream package or assert library selection.

## 8. Decisions/blockers — owner-owned gates, not automatic work tickets

| Decision / blocker | What it blocks | Owner/technical action after migration |
|---|---|---|
| **A versus B: first runnable concept sequence — UNDECIDED** | Whole-EFB application implementation scope | Owner reviews **A**: complete shell + real manual Logbook (labels for inactive live services), then integrate logging; versus **B**: require live automatic logging for first runnable concept. **Both keep full Q1–Q14 C0.1 target**. Get scope choice and separate explicit implementation approval. |
| `simconnect-H` v0.7.2 missing loader imports | Wrapper native preload, then MSFS 2020 smoke testing | Codex researches owner-approved corrected evaluation copy or upstream release; owner approves exact change/dependency and bounded separate tests. |
| Recorder flight/timing/event/persistence semantics open | Credible automatic Flight Logging and acceptance tests | Owner with Recorder/Logbook design sets events, clocks, pauses, touchdown, interruptions and local storage policies. |
| Essential SimVar cadence/mapping/landing metrics unvalidated | Recorder technical feasibility; cannot promise landing rate | After separate owner-approved native read-only test, capture actual MSFS 2020 source/units/quality and performance. |
| UI framework version/licensing, packaging, resizing and storage implementation unverified | Windows concept acceptance/release | After build approval verify PySide6 Windows 10/11 behaviours, chosen folder, durable writes, portable packaging and CPU/RAM/GPU. |
| Core R3.1 and old draft snapshots not approved production baseline | Integration into full EFB | Owner reviews source/test risk and chooses explicit integration candidate after evidence; do not blend partial snapshots. |
| Codex workspace/process authority cutover | What files/policies should control future work | First verify new transfer and original instructions together. Owner confirms source authority/cutover, workspace and any future `AGENTS.md`. |

**Conflict/watch list:** Some early source sections still say no current component code, no Windows tests or UI toolkit undecided; these are historical context **superseded** only on the particular points by later owner-approved v1.9/v1.10, v0.8 and UI v1.2 addenda. The general original Project Instructions say to evaluate toolkits; subsequent approval selects PySide6/Qt Widgets **as direction subject to validation**. These are dated context differences, **not** silent changes. PMDG earlier research focus does not override the later parked plugin decision. If an actual conflict remains, bring it to the owner; don't reconcile it in code or a new master.

## 9. Safe new Codex workspace setup and read-only acceptance

**Proposed clean layout** (not a command to create directories or modify the old workspace):

```text
BBC_EFB_New_Codex_Workspace/
  BBC_EFB_CODEX_MASTER_HANDOFF.md           # START HERE
  PROJECT_INSTRUCTIONS_ORIGINAL.md          # verbatim visible ChatGPT instruction text
  SPECIALIST_INSTRUCTIONS_ORIGINAL.md       # partial/labelled originals
  CHAT_DECISIONS_AVAILABLE.md
  00_CURRENT_PROJECT_SOURCE_MASTERS/        # 9 owner-confirmed master copies
  01_CODEX_WORK_EVIDENCE/                   # R3.1/R3 sources and original reports
  02_SPECIALIST_HANDOFFS/                   # original DOCX design/research
  03_HISTORICAL_CORE_SOURCE/                # historical candidates, not baselines
  04_REFERENCE_ASSETS/                      # images and logo ZIP
  05_PREVIOUS_TRANSFER_REFERENCE/           # earlier file inventory reference
  README_TRANSFER.md
  MISSING_MATERIAL_CHECKLIST.md
  EXTERNAL_DEPENDENCIES_NOT_BUNDLED.md
  MANIFEST.json
  MANIFEST.sha256
  # NO AGENTS.md yet
```

**Plain-English migration procedure:**

1. **Keep the existing Codex workspace intact** as a fallback. Back it up (including local untracked work) **before any cleanup**. Do not merge or delete source trees yet.
2. In your separate clean Codex project directory, extract `BBC_EFB_Codex_Migration.zip`. The root must contain this handoff. No app execution required.
3. Check all **nine** files in `00_CURRENT_PROJECT_SOURCE_MASTERS/` (eight updated plus unchanged founding prompt), including opening both illustrated `.docx` documents. Leave the originals unmodified.
4. Read `README_TRANSFER.md` and `MANIFEST.json`, verify files/hashes. Compare new R3.1 files and reports **read-only** with existing workspace; do not assume a directory exists merely because an old conversation mentioned it.
5. Review `PROJECT_INSTRUCTIONS_ORIGINAL.md` and the limitation notes in `SPECIALIST_INSTRUCTIONS_ORIGINAL.md` with the owner. **Do not write/activate `AGENTS.md` until the owner approves a Codex-specific rule set**. Do not transplant specialist one-off task permissions as standing approval.
6. Check `MISSING_MATERIAL_CHECKLIST.md` for any exact items the owner wishes to supply (actual Settings instruction parity, full chat exports, third-party packages, SDK provenance). **Do not import unreviewed binaries automatically.**
7. Discuss first full-shell concept Option A/B with owner. After the owner explicitly selects concrete scope, seek separate coding, testing and build authorisations. Until then **preparation only**.

**Transfer acceptance, not code test:** all payload files in ZIP should match `MANIFEST.json` SHA-256 and size. This package construction validates archive contents; it does **not** re-run Codex tests or validate Windows/MSFS functionality. An owner or fresh Codex session should confirm archive integrity in the new environment before trusting its contents.

## 10. Evidence limitations, access and outstanding materials

**Directly inspected for this transfer:** nine available master files (and verified matching eight-file updated master ZIP), 22 Nov instruction text file, supporting handoffs/assets, **all 45 Codex ZIP entries**, R3/R3.1 source paths, relevant Codex reports/traceback and the source transfer manifest. **Not directly accessible:** the authenticated ChatGPT Project settings UI, any complete Main/Core/Logbook chat transcript exports, the existing PC's current filesystem, the physical SDK DLL, a running MSFS session or Codex's runtime processes. The existing Codex report is preserved unchanged as evidence **reported from the user's machine**; no tests were independently repeated here.

**Manual retrieval instructions** and the exact expected destination paths are in `MISSING_MATERIAL_CHECKLIST.md`. The old ChatGPT Project URL was not an authentication bridge for Codex; this new workspace should be fully understandable from these files. If the Project Settings instructions vary from `PROJECT_INSTRUCTIONS_ORIGINAL.md`, owner-provided verbatim Settings export takes precedence, without mutating this preserved file until reviewed.

---

**FINAL STATUS: MIGRATION/PRESERVATION ONLY.** First whole-EFB build, actual SimConnect/Flight Recorder operation, library choice, UI implementation, SDK distribution and architecture changes await the owner's separate decisions.
