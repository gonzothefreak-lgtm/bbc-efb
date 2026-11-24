# Aircraft Input Events offline verification

**24 November 2026** · Windows CPython 3.12.14 x64 · MSFS 2020 SDK 0.24.6.0 header

**Result:** 41 passed, zero failures, zero errors, zero skips. Exit code 0 and zero audit guard violations. The 32 existing Core checks passed with expanded ABI expectations, and nine new fake-SDK tests exercised the aircraft Input Event paths.

The new tests cover paginated discovery, parameter discovery, a correlated one-shot read, shared change subscriptions, release on last subscriber, 64-bit input hashes, numeric and string arguments, SDK rejection, and expiry and invalidation across aircraft changes. A queued aircraft-load packet is processed before an aircraft-scoped write is sent. Writes through `SetInputEvent` are reported as unverified after dispatch unless the SDK rejects them. This API provides no response confirming that the simulator applied the input.

The published `SetInputEvent` reference shows a `DWORD` hash in its syntax, while the installed header specifies `UINT64`. The published `EnumerateInputEventParams` page shows a different signature from the installed header, which identifies the query by `UINT64` hash. The binding follows the header and the mock checks use hashes above 32 bits. [SetInputEvent](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/InputEvents/SimConnect_SetInputEvent.htm) · [EnumerateInputEventParams](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/InputEvents/SimConnect_EnumerateInputEventParams.htm)

No DLL was loaded and no simulator connection was made. Live packet timing, aircraft lifecycle notification order, string encoding, real-aircraft parameter support and native write effects remain unverified. The prior load-only result covers 13 exports. The expanded adapter declares 19.

## Exact invocation

Run from the workspace root in PowerShell.

```powershell
$bbcPython='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$bbcRunner=@'
import ctypes, faulthandler, pathlib, struct, sys, unittest

blocked = []
def guard(event, args):
    if (event == "ctypes.dlopen"
            or event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec"))
            or event in ("os.system", "os.startfile", "_winapi.CreateProcess")
            or (event == "import" and args[0].split(".")[0] == "simconnect_native")):
        blocked.append(event)
        raise RuntimeError("Offline guard blocked " + event)

sys.addaudithook(guard)
root = pathlib.Path.cwd()
sys.path.insert(0, str(root))
print(f"CPython {sys.version.split()[0]} {struct.calcsize('P') * 8}-bit", flush=True)
faulthandler.dump_traceback_later(15, exit=True)
try:
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*_offline.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"Offline guard violations: {len(blocked)}", flush=True)
finally:
    faulthandler.cancel_dump_traceback_later()
raise SystemExit(0 if result.testsRun and result.wasSuccessful() and not blocked else 1)
'@
& $bbcPython -I -B -c $bbcRunner
exit $LASTEXITCODE
```

## Output

```text
CPython 3.12.14 64-bit
test_a_load_packet_behind_the_drain_limit_prevents_a_stale_input_write (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_a_load_packet_behind_the_drain_limit_prevents_a_stale_input_write) ... ok
test_aircraft_reload_invalidates_old_handles_and_unconfirmed_writes (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_aircraft_reload_invalidates_old_handles_and_unconfirmed_writes) ... ok
test_async_set_exception_reports_rejected_without_closing_session (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_async_set_exception_reports_rejected_without_closing_session) ... ok
test_delayed_hash_only_parameters_cannot_validate_a_replacement_aircraft (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_delayed_hash_only_parameters_cannot_validate_a_replacement_aircraft) ... ok
test_discovery_is_opt_in_and_assembles_multipart_pages (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_discovery_is_opt_in_and_assembles_multipart_pages) ... ok
test_incomplete_catalog_times_out_without_publishing_partial_results (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_incomplete_catalog_times_out_without_publishing_partial_results) ... ok
test_reads_fixed_text_and_rejects_a_malformed_value (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_reads_fixed_text_and_rejects_a_malformed_value) ... ok
test_shares_subscriptions_primes_with_get_and_releases_on_last_consumer (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_shares_subscriptions_primes_with_get_and_releases_on_last_consumer) ... ok
test_write_uses_current_schema_and_the_full_64_bit_hash (test_aircraft_inputs_offline.AircraftInputOfflineTests.test_write_uses_current_schema_and_the_full_64_bit_hash) ... ok
test_command_access_and_arguments_are_checked_without_provider_dispatch (test_core_offline.CoreOfflineTests.test_command_access_and_arguments_are_checked_without_provider_dispatch) ... ok
test_consumer_gap_is_explicit_and_does_not_affect_another_consumer (test_core_offline.CoreOfflineTests.test_consumer_gap_is_explicit_and_does_not_affect_another_consumer) ... ok
test_delayed_reading_from_previous_session_is_ignored (test_core_offline.CoreOfflineTests.test_delayed_reading_from_previous_session_is_ignored) ... ok
test_disconnect_discards_old_values_and_keeps_subscription_on_reconnect (test_core_offline.CoreOfflineTests.test_disconnect_discards_old_values_and_keeps_subscription_on_reconnect) ... ok
test_disconnect_marks_sent_command_uncertain_and_does_not_replay_it (test_core_offline.CoreOfflineTests.test_disconnect_marks_sent_command_uncertain_and_does_not_replay_it) ... ok
test_radio_tune_and_swap_report_accepted_sent_and_confirmed (test_core_offline.CoreOfflineTests.test_radio_tune_and_swap_report_accepted_sent_and_confirmed) ... ok
test_send_failure_blocks_writes_but_leaves_telemetry_usable (test_core_offline.CoreOfflineTests.test_send_failure_blocks_writes_but_leaves_telemetry_usable) ... ok
test_stale_read_retains_timestamp_and_emits_one_stale_update (test_core_offline.CoreOfflineTests.test_stale_read_retains_timestamp_and_emits_one_stale_update) ... ok
test_trusted_raw_diagnostics_share_the_named_subscription (test_core_offline.CoreOfflineTests.test_trusted_raw_diagnostics_share_the_named_subscription) ... ok
test_two_consumers_share_provider_demand_and_can_leave_independently (test_core_offline.CoreOfflineTests.test_two_consumers_share_provider_demand_and_can_leave_independently) ... ok
test_unresolved_cleanup_blocks_opening_a_new_session (test_core_offline.CoreOfflineTests.test_unresolved_cleanup_blocks_opening_a_new_session) ... ok
test_initial_signatures_match_the_header (test_simconnect_abi_offline.SimConnectAbiOfflineTests.test_initial_signatures_match_the_header) ... ok
test_packed_receive_structures_match_header_field_offsets (test_simconnect_abi_offline.SimConnectAbiOfflineTests.test_packed_receive_structures_match_header_field_offsets) ... ok
test_x64_windows_types_and_initial_enum_values (test_simconnect_abi_offline.SimConnectAbiOfflineTests.test_x64_windows_types_and_initial_enum_values) ... ok
test_correlated_native_exception_reports_rejected_write (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_correlated_native_exception_reports_rejected_write) ... ok
test_diagnostics_accepts_indexed_simvars_but_not_other_variable_namespaces (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_diagnostics_accepts_indexed_simvars_but_not_other_variable_namespaces) ... ok
test_extended_event_argument_schema_and_dispatch_without_readback (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_extended_event_argument_schema_and_dispatch_without_readback) ... ok
test_extended_event_correlates_an_async_rejection (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_extended_event_correlates_an_async_rejection) ... ok
test_invalid_signal_exception_does_not_break_another_read (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_invalid_signal_exception_does_not_break_another_read) ... ok
test_missing_swap_baseline_and_invalid_frequency_prevent_transmission (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_missing_swap_baseline_and_invalid_frequency_prevent_transmission) ... ok
test_numeric_fixed_width_text_and_pause_packets (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_numeric_fixed_width_text_and_pause_packets) ... ok
test_open_requires_confirmation_and_close_retires_handle (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_open_requires_confirmation_and_close_retires_handle) ... ok
test_open_uses_the_sdk_local_only_config_constant (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_open_uses_the_sdk_local_only_config_constant) ... ok
test_quit_marks_command_uncertain_and_failed_close_blocks_reconnect (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_quit_marks_command_uncertain_and_failed_close_blocks_reconnect) ... ok
test_readback_exception_leaves_telemetry_running (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_readback_exception_leaves_telemetry_running) ... ok
test_shared_subscription_retires_only_after_last_consumer (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_shared_subscription_retires_only_after_last_consumer) ... ok
test_startup_quit_and_open_timeout_leave_no_handle (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_startup_quit_and_open_timeout_leave_no_handle) ... ok
test_truncated_known_packet_ends_session_cleanly (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_truncated_known_packet_ends_session_cleanly) ... ok
test_tune_and_swap_use_one_shot_readback_without_a_telemetry_subscription (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_tune_and_swap_use_one_shot_readback_without_a_telemetry_subscription) ... ok
test_writable_simvar_rejects_invalid_values_and_reports_readback_mismatch (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_writable_simvar_rejects_invalid_values_and_reports_readback_mismatch) ... ok
test_writable_simvar_sends_one_typed_element_and_confirms_against_readback (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_writable_simvar_sends_one_typed_element_and_confirms_against_readback) ... ok
test_wrong_readback_times_out_without_resending_command (test_simconnect_provider_offline.SimConnectProviderOfflineTests.test_wrong_readback_times_out_without_resending_command) ... ok

----------------------------------------------------------------------
Ran 41 tests in 0.017s

OK
Offline guard violations: 0
```
