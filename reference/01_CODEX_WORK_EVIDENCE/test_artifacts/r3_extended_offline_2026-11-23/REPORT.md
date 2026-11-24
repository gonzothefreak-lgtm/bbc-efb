# R3 extended offline validation — 23 November 2026

**NEEDS CORRECTION — Two confirmed Core defects require source corrections.**

Both runs used the same Windows bundled CPython 3.12.14 (64-bit). Preflight parsed the new module and reviewed its imports, fake wrapper, two-second Event and Thread deadlines, and bounded fixture counts. No external packages were needed. The fake allocates IDs before registration can raise, following the order in v0.7.2 subscribe.py. A child audit hook denied real wrapper imports, DLL loads, network calls and process launches. The internal watchdog was 12 seconds and an independent PowerShell parent enforced a 15-second kill deadline per process. Neither deadline fired. Both runs had zero audit violations. No real SimConnect connection, DLL load or MSFS launch occurred.

| Run | Executed | Pass | Fail | Error | Skipped | unittest time | Process exit |
|---|---:|---:|---:|---:|---:|---:|---:|
| Additions only | 15 | 13 | 2 | 0 | 0 | 0.019 s | 1 |
| Combined | 23 | 21 | 2 | 0 | 0 | 0.020 s | 1 |

All eight original tests passed in the combined run. There were 38 case executions across the two runs, covering 23 distinct tests. The new source was tests/test_adapter_r3_extended.py. Hash comparison verified all 10 pre-existing Python and first-run report/output files checked before and after were unchanged. The original run files remain intact. See COVERAGE.md for scenario outcomes and unittest.txt for both complete outputs, case timings, and tracebacks.

## Confirmed Core defects

1. **False connected transition on dispatcher startup failure.** With empty demand, _apply_demand marks subscription readiness and publishes CONNECTED before calling start_background_dispatch. When the fake start raises, the broker transition history is CONNECTING → CONNECTED → ERROR → ERROR. The test captured all publications even when the broker later cleared its queue. Expected no CONNECTED publication. See src/bbc_core/adapter_simconnect_h.py lines 505–524 and the failing test at tests/test_adapter_r3_extended.py line 279. Hold public readiness until the pump starts successfully and recheck the session generation before publishing it. Preserve appropriate callback ordering on startup.
2. **Repeated shutdown loses prior uncertainty.** After a simulated hung close, a second disconnect performs no additional native close and still blocks connect. It replaces the shutdown status “uncertain; host restart required” with “no native session”. See src/bbc_core/adapter_simconnect_h.py lines 626–646 and 649–679, and the failing test at tests/test_adapter_r3_extended.py line 270. Preserve the prior fault and uncertainty diagnostics across a no-handle disconnect.

These failures are adapter state-management defects, demonstrated with a simulated wrapper. No real wrapper defect was executed. Native lock-order and restore-timer interactions stay unverified. Source review still shows v0.7.2 retire uses object ID zero and can conceal stop/clear failures, and native Close HRESULT remains unavailable. No simulated thread timed out. The fake does not establish deadlock freedom, native cleanup, packet age, delivery cadence or simulator stability.

**Next gate:** Correct the two adapter defects under separate approval, then rerun the 23-case suite before moving to Windows/native-library validation.

## Exact invocation

From the workspace root in PowerShell:

```powershell
$py='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$r=(Get-Item -LiteralPath 'development reviews/BBC_EFB_Black_Box_Core_R3_Source_Candidate_2026-11-23').FullName
$runner=@'
import ctypes, faulthandler, pathlib, struct, sys, unittest, unittest.mock

blocked = []
def guard(event, args):
    if (event == "import" and args[0].split(".")[0] == "simconnect_native") or event == "ctypes.dlopen" or event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec")) or event in ("os.system", "os.startfile", "_winapi.CreateProcess"):
        blocked.append(event)
        raise RuntimeError("Offline guard blocked " + event)

sys.addaudithook(guard)
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
print("CPython", sys.version.split()[0], str(struct.calcsize("P") * 8) + "-bit", flush=True)
faulthandler.dump_traceback_later(12, exit=True)
try:
    run = unittest.main(module=None, argv=["unittest", "discover", "-s", str(root / "tests"), "-p", sys.argv[2], "-v", "--durations", "0"], exit=False)
    print("Offline guard violations:", len(blocked), flush=True)
finally:
    faulthandler.cancel_dump_traceback_later()
raise SystemExit(0 if run.result.wasSuccessful() and not blocked else 1)
'@
foreach ($pattern in 'test_adapter_r3_extended.py','test_*.py') {
 $info=[Diagnostics.ProcessStartInfo]::new($py)
 $info.WorkingDirectory=$r
 $info.UseShellExecute=$false
 $info.CreateNoWindow=$true
 $info.RedirectStandardOutput=$true
 $info.RedirectStandardError=$true
 foreach ($argument in '-I','-B','-c',$runner,$r,$pattern) { $info.ArgumentList.Add($argument) }
 $child=[Diagnostics.Process]::Start($info)
 try {
  $stdout=$child.StandardOutput.ReadToEndAsync()
  $stderr=$child.StandardError.ReadToEndAsync()
  if (!$child.WaitForExit(15000)) {
   $child.Kill($true)
   if (!$child.WaitForExit(2000)) { throw 'Unable to reap timed-out test process' }
   "External deadline exceeded: $pattern"
  }
  "=== $pattern (exit $($child.ExitCode)) ==="
  $stdout.GetAwaiter().GetResult()
  $stderr.GetAwaiter().GetResult()
 } finally { $child.Dispose() }
}
```
