# First Core offline verification

**24 November 2026** · Windows CPython 3.12.14 x64

**Result:** 14 passed, zero failures, zero errors, zero skips. The test process exited 0. The only provider used was the simulated provider. The audit guard reported zero violations.

- Eleven Core cases covered shared demand, consumer isolation, stale values, queue gaps, reconnect and old-session rejection, diagnostic access, simulated COM1 tune and swap with readback, command access and validation, uncertain writes, and cleanup failures.
- Three x64 ABI cases checked six packed receive layouts, the scalar widths and initial enum values, and argument declarations for 13 functions. Expectations were derived from the installed SDK 0.24.6.0 `SimConnect.h`. The checks evaluated Python declarations in memory. The SimConnect DLL was never loaded, and no native exports or live packets were exercised.
- No application code changes were needed. The native adapter, production packaging, radio channel validation and simulator behaviour remain outside this result.

## Exact invocation

Run from the workspace root using PowerShell.

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

----------------------------------------------------------------------
Ran 14 tests in 0.004s

OK
Offline guard violations: 0
```
