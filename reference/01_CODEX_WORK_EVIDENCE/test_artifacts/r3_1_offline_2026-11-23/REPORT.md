# R3.1 offline acceptance — 23 November 2026

**PASS within the simulated scope.** Windows CPython 3.12.14, 64-bit. Existing standard-library dependencies only.

| Run | Passed | Failed | Errors | Skips | Time |
|---|---:|---:|---:|---:|---:|
| Known defects, pre-fix | 0 | 2 | 0 | 0 | 0.001 s |
| Additional reproductions, pre-fix | 0 | 2 | 0 | 0 | 0.001 s |
| Targeted regression after corrections | 4 | 0 | 0 | 0 | 0.001 s |
| Final extended module | 23 | 0 | 0 | 0 | 0.020 s |
| Final full discovery | 31 | 0 | 0 | 0 | 0.021 s |

Both final processes exited 0. Isolation disabled bytecode writes. A 12-second Python watchdog and independent 15-second PowerShell deadline bounded each process. No deadline fired. The audit guard recorded zero violations for real wrapper imports, DLL loads, network calls, or external process launches. No native DLL or MSFS was used. No packages were installed and no builds were run.

## Verified corrections

- Adapter demand readiness follows successful dispatch startup and a current-session check. Aborted worker startup and QUIT during startup produce no CONNECTED notice.
- Repeated shutdown preserves native termination uncertainty and the reuse block without repeating native close.
- The broker retains unread connection notices during reconnection while invalidating old queued data. The bounded queue still reports overflow.

The provider and broker signatures and approved architecture are unchanged. Eight extra tests were added to the extended module. Previous candidates and test reports were preserved.

## Remaining limitations and gate

simconnect-H v0.7.2 remains under evaluation. It hides native retirement and Close outcomes. The adapter cannot verify retirement on an assigned object or force a stuck native call to return. Actual restore lock interactions, native packet age and ordering, event support and cadence, DLL provenance, and simulator frame-time impact remain unverified. Filename-bearing events remain outside the ordinary dispatcher path. The next gate is separately authorised Windows/native-library validation. Live MSFS testing stays separate.

## Exact invocation

Run from the workspace root in PowerShell:

```powershell
$py='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$r=(Get-Item -LiteralPath 'development reviews/BBC_EFB_Black_Box_Core_R3.1_Source_Candidate_2026-11-23').FullName
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

Targeted runs used the same runner restricted to test_adapter_r3_extended.py. The first pre-fix run added -k test_dispatch_start_failure and -k test_repeated_hung_shutdown. The second used -k test_quit_while_dispatch_starts and -k test_fast_reconnect. The targeted retest used all four filters.
