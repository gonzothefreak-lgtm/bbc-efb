# R3 simulated offline results — 23 November 2026

**PASS — Offline tests passed; remaining limitations documented.**

Local Windows, bundled CPython 3.12.14 (64-bit), standard-library unittest and ctypes, no installation. Both existing test modules were inspected. The adapter imports the wrapper inside connect. Every adapter test substitutes FakeSession before connect. The broker cases use in-memory data. This scope follows the current C0.1 plan for bounded, read-only telemetry and explicit connection and quality states.

The run used isolation, disabled bytecode writes, and applied a 15-second watchdog. Audit guards denied real wrapper imports, ctypes DLL loads, network calls and process launches. Zero guard violations. No real SimConnect connection, native DLL load or simulator launch occurred within the test process.

## Exact command

Executed from the workspace root in PowerShell.

```powershell
$py='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$r=(Get-Item -LiteralPath 'development reviews/BBC_EFB_Black_Box_Core_R3_Source_Candidate_2026-11-23').FullName
$runner=@'
import ctypes, faulthandler, pathlib, sys, unittest, unittest.mock

blocked = []
def guard(event, args):
    if (event == "import" and args[0].split(".")[0] == "simconnect_native") or event == "ctypes.dlopen" or event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec")) or event in ("os.system", "os.startfile", "_winapi.CreateProcess"):
        blocked.append(event)
        raise RuntimeError("Offline guard blocked " + event)

sys.addaudithook(guard)
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
faulthandler.dump_traceback_later(15, exit=True)
try:
    run = unittest.main(module=None, argv=["unittest", "discover", "-s", str(root / "tests"), "-p", "test_*.py", "-v", "--durations", "0"], exit=False)
    print("Offline guard violations:", len(blocked), flush=True)
finally:
    faulthandler.cancel_dump_traceback_later()
raise SystemExit(0 if run.result.wasSuccessful() and not blocked else 1)
'@
& $py -I -B -c $runner $r
exit $LASTEXITCODE
```

## Results

8 executed, 8 passed, 0 failed, 0 errors, 0 skipped. Suite time 0.002 s, exit code 0. Individual times appear in unittest.txt. No failure tracebacks.

| Test | Status | Expected behaviour demonstrated |
|---|---|---|
| test_cancelled_startup_hung_close_blocks_reuse | PASS | Startup QUIT and simulated hung teardown mark the provider unusable and reject reuse. |
| test_event_only_demand_replays_and_old_callback_is_ignored | PASS | Explicit new session after lifecycle invalidation re-registers Pause. Only the new callback delivers. Readiness is true and SimVar dataflow unconfirmed. |
| test_interrupted_staging_retires_known_id_once_and_blocks_reuse | PASS | Interrupted reconciliation raises, retires the known ID once and blocks reuse after uncertain teardown. |
| test_numeric_and_string_callbacks_do_not_refresh_each_other | PASS | Four ctypes types convert. First stream alone cannot establish dataflow. A string callback leaves the prior numeric timestamp unchanged. A live string stream cannot mask numeric silence at the health probe, which clears cached altitude. |
| test_startup_quit_never_publishes_connected | PASS | Startup QUIT raises and ends disconnected without a connected notice. |
| test_deduplicates_shared_simvar_and_uses_fastest_interval | PASS | Two consumers share one effective broker request at the fastest interval. Broker releases update and remove demand. |
| test_no_live_data_on_disconnection | PASS | The broker returns a live sample while connected and removes it on disconnection. |
| test_queue_overflow_is_never_silent | PASS | A 16-entry queue reports two dropped events after 18 are submitted. |

## NOT TESTED

- Independent open failure, QUIT after startup, late lifecycle notifications after QUIT, transport-loss recovery distinct from lifecycle replay, or prior-generation numeric callbacks. Prior-generation event rejection was verified.
- Partial registration that raises before returning an ID, normal native subscription retirement or replacement, failed retirement, aggregate limits, or adapter delivery to multiple consumers. Broker demand sharing was verified.
- Empty-demand readiness as a distinct assertion, filename-event rejection, and complete telemetry recovery after a stall. Event-only readiness and first-delivery gating were verified.
- Explicit clean and repeated shutdown assertions and callbacks during shutdown. A normal close was traversed in the event replay case. Cancelled-startup hung close was verified with a fake.
- Observation-queue overflow, per-consumer fanout, actual thread contention, and deadlock freedom. FakeSession models its worker using booleans. The staging interruption is synchronous.

## Defects and validation gate

No confirmed defects or hangs emerged from the executed cases. The simconnect-H v0.7.2 source review still applies: unsubscribe uses object ID zero and can hide native stop/clear failures, Close HRESULT is unavailable, and the ordinary event callback excludes filename-bearing events. Internal restore races, native hangs, packet age and cadence, and frame-time effects require further validation.

Proceed toward planning a separately authorised Windows/native-library validation. Address the critical offline coverage gaps before authorising a live simulator connection. No source or tests were edited.
