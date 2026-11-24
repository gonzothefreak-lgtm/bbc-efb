# R3 extended offline coverage matrix

| Concern | Evidence | Outcome |
|---|---|---|
| A. Transport loss after startup, cache invalidation, late lifecycle, fresh demand replay | test_quit_after_startup_invalidates_data_and_replays_demand | PASS |
| A. Startup dispatch fails before readiness | test_dispatch_start_failure_does_not_publish_ready | FAIL. A CONNECTED transition was emitted before the worker start failed. |
| B. Old numeric callback, timestamps, readiness, Recorder consumer delivery | test_old_numeric_callbacks_cannot_change_new_session | PASS. Tests the broker consumer path, without an actual Flight Recorder. |
| C. Partial native registration before ID returned, known staged rollback, orphan rejection, reuse block | test_partial_registration_leaves_unknown_id_unverified | PASS |
| C. Failed wrapper retirement | test_failed_retirement_does_not_leave_configuration_ready | PASS |
| D. Shared demand, replacement, additions, removals and retired callbacks | test_replacement_preserves_shared_demand_and_rejects_retired_callback | PASS |
| D. Interrupted staging | Original test_interrupted_staging_retires_known_id_once_and_blocks_reuse and test_partial_registration_leaves_unknown_id_unverified | PASS |
| E. Healthy, disconnected, repeated clean shutdown | test_repeated_clean_shutdown_closes_native_once | PASS |
| E. Shutdown requested while connecting | test_shutdown_requested_while_connect_is_pending | PASS. Shutdown serialises behind simulated OPEN. |
| E. Partially configured and subsequent uncertain shutdown | test_partial_registration_leaves_unknown_id_unverified | PASS. Repeated aborts did not issue a second native close. |
| E. Previous hung shutdown | test_repeated_hung_shutdown_preserves_uncertainty_diagnostics | FAIL. A repeated call erased the uncertainty text. Reuse remained blocked and close ran once. |
| F. Numeric count 127/128/129, numeric bucket 31/32/33 | test_numeric_total_127_128_129 and test_numeric_bucket_31_32_33 | PASS |
| F. String count 23/24/25 mixed with 103 numeric fields | test_string_total_23_24_25_with_numeric_demand | PASS. Over-limit string demand stayed within the overall 128-field budget. |
| G. Transport loss in dispatch during reconciliation join | test_quit_on_dispatch_thread_while_reconcile_joins_pump | PASS. No new registration occurred after the loss. |
| G. Pending dispatch while removing demand and during native close | test_removal_waits_for_pending_dispatch_callback and test_shutdown_invalidates_pending_dispatch_callback | PASS |
| G. Registration inside native call with real dispatch overlap | Fake requires the pump to be stopped. Tests exercise pending dispatch during the stop/join, plus synchronous staging interruption. | NOT TESTED. An actual callback during native registration after successful join would require a different source or native behaviour. |
| Native lock ordering, restore timer races, DLL shutdown or request retirement | Simulated wrapper has no native state or I/O locks, and no real worker or DLL. | UNVERIFIED |
