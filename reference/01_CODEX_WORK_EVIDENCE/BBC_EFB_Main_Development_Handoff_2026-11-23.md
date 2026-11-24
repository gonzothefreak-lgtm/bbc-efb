# BBC Integrated EFB — Main Development handoff

**23 November 2026 · Codex workspace review and Black Box Core development**

## Decision for Main Development

The owner's goal is a runnable concept of the whole EFB. The current workspace contains design masters and tested Core component source. There is no complete EFB shell or implemented Flight Recorder available to package. Discuss whether to authorise an internal full-shell concept with a working manual Logbook first, or require automatic live Flight Logging in the first runnable build. The approved full C0.1 functional target remains in force.

## Authority and approved direction

The ChatGPT Project Sources remain authoritative. The transfer contained nine local master copies. All 77 manifest-listed files matched their sizes and SHA-256 hashes. Historical copies and checkpoints in the transfer are reference material. Later approvals within current masters govern earlier draft sections. The transfer and Project Source masters were left unchanged.

- Python with PySide6 / Qt Widgets on Windows 10/11. NEW C0.1 is a single modular process. Separation of EFB, Black Box Core and Flight Recorder is targeted from C0.2 onward.
- Every authorised application build includes Dispatch, In-Flight, Logbook and accessible shared Settings. Start in Dispatch. Keep the In-Flight Modules launcher usable during resizing and retain independent SimConnect and Flight Logging indicators in the permanent footer. Clearly label unfinished services.
- Use pilot-selected writable local storage. Deliver a portable ZIP with the complete buildable source and Windows instructions.
- The approved C0.1 Logbook target covers Q1–Q14: Dashboard, History in Table/Cards/Combined views, in-EFB Flight Details, a unified editor with correction provenance, Recorder Management and Statistics. Manual entry requires aircraft type, departure, destination and date. Basic saved layout preferences belong in C0.1; advanced layout customisation is deferred to C0.2.
- Core supplies typed, shared, consumer-driven observations and continuity state. The Recorder owns flight event rules, timing, local persistence and recovery. Automatic Flight Logging with manual override is a C0.1 target. Exact start/end, pause, touchdown, interrupted-flight and persistence rules still need agreement and validation. Show unavailable data honestly.

## Work completed in Codex

1. Verified transfer integrity and classified current masters, historical revisions, handoffs, assets, Core drafts and the two v0.7.2 distributions.
2. Compared the earlier adapter drafts with the supplied wrapper source. Identified ctypes datatype conversion, cached-string snapshot freshness, reconnect/event replay, cleanup and shutdown gaps. Independently reviewed the owner-supplied R2 tree.
3. Created R3 as a separate complete Core candidate. Added generation invalidation, fresh-session demand replay, bounded streams, separate numeric/string handling, guarded staging and uncertainty reporting. The first eight R3 offline cases passed. Extended R3 testing ran 23 cases and found two failures.
4. Created R3.1 as a separate complete candidate. Fixed readiness publication before dispatch startup, preserved uncertain shutdown status on repeated teardown, and prevented rapid reconnect from erasing unread disconnect notices. Public Core interfaces remain unchanged. **31/31 guarded simulated tests passed** on Windows CPython 3.12.14 x64, with zero failures, errors or skips.
5. Imported the actual v0.7.2 wheel without installation or a DLL. Checked adapter call signatures, datatype mappings and closed-client registration/retirement. Separately loaded the x64 MSFS SDK 0.24.6.0 DLL and resolved all 12 wrapper-declared exports without calling SimConnect.
6. Under the owner's live smoke-test approval, ran the loader preflight. The supplied wrapper failed before any native connection. No live simulator session, EFB build or executable was produced.

## Live blocker and verification needed

The supplied wheel and source distribution omit imports for `find_simconnect_dll` and `is_untrusted_simconnect_dll` in `simconnect_native/client.py`. With an explicit path, `SimConnect.load_dll()` raises `NameError` at line 215 on the second helper. The standalone SDK DLL load passed. The wrapper is an evaluation dependency. Codex left the distribution unchanged.

Obtain an owner-approved corrected evaluation distribution, or separately approve a local corrected evaluation copy. Repeat wrapper preload, then exercise a bounded read-only parked-aircraft MSFS 2020 smoke test before relying on the Core in the EFB. Verify connection, representative telemetry, disconnect and teardown. That smoke test cannot establish automatic flight detection, touchdown accuracy, native lock behaviour or simulator overhead. v0.7.2 still hides some unsubscribe and close outcomes and the high-level event callback excludes filename-bearing events.

## Recommended sequencing for discussion

Start the full-shell internal concept and durable manual Logbook workflow while the wrapper and Recorder questions are resolved. Use the first review to verify navigation, resize, both status indicators, and entering, saving, reopening and editing a manual flight. In parallel, resolve the loader defect and agree on Recorder semantics. Integrate genuine live logging after the Core smoke test and validate it through the complete shell. Obtain explicit application implementation and build approval before proceeding.

## Bundle guide

- `development reviews/BBC_EFB_Black_Box_Core_R3.1_Source_Candidate_2026-11-23/` is the latest local candidate, with source, tests, README and changelog.
- The sibling R3 candidate preserves the preceding implementation and extended tests for reproduction.
- `test_artifacts/` contains the first/extended R3 reports, R3.1 offline coverage and output, DLL-free Windows validation, DLL load-only proof, and live preflight traceback with commands.
- The bundle contains Codex work products. Existing Project Sources, historical owner-provided inputs, the external wrapper distributions and the SDK/DLL are referenced rather than duplicated. R3.1 has no production, baseline, wrapper-selection or release approval.
