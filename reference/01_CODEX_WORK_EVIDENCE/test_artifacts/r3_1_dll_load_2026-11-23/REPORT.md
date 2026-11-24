# SimConnect SDK load-only probe

**PASS.** Windows CPython 3.12.14 (64-bit) loaded the SHA-256-verified SDK 0.24.6.0 DLL using its absolute path. All 12 exports declared by simconnect-H v0.7.2 resolved. The probe called no SimConnect functions and made no simulator connection. No simulator was launched.

## Exact invocation

```powershell
$py='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$dll='C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll'
$expected='CFFB74B62B596A897B0FF101BAC3D66EA10236C540C75864658B451FFD1E6513'
$probe=@'
import ctypes, faulthandler, hashlib, pathlib, re, struct, sys, zipfile
dll, expected, wheel = sys.argv[1:]
assert struct.calcsize("P") == 8 and sys.platform == "win32"
assert hashlib.sha256(pathlib.Path(dll).read_bytes()).hexdigest().upper() == expected
with zipfile.ZipFile(wheel) as archive:
    source = archive.read("simconnect_native/client.py").decode()
exports = sorted(set(re.findall(r"\bd\.(SimConnect_\w+)\.(?:argtypes|restype)", source)))
assert {"SimConnect_Open", "SimConnect_Close", "SimConnect_CallDispatch"} <= set(exports)
def guard(event, args):
    if event == "ctypes.dlopen" and args[0] != dll:
        raise RuntimeError("Unexpected DLL load")
    if event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec")) or event in ("os.system", "os.startfile", "_winapi.CreateProcess"):
        raise RuntimeError("Unexpected network or process action")
sys.addaudithook(guard)
faulthandler.dump_traceback_later(8, exit=True)
library = ctypes.WinDLL(dll)
for name in exports:
    getattr(library, name)
faulthandler.cancel_dump_traceback_later()
print("PASS: exact SDK DLL loaded on Windows CPython", sys.version.split()[0], "(64-bit)")
print("SHA-256:", expected)
print("Resolved", len(exports), "wrapper exports:", ", ".join(exports))
print("SimConnect functions called: 0; sessions opened: 0; simulator launches: 0")
'@
$wheel=(Get-Item -LiteralPath 'project sources/02_ALL_OTHER_AVAILABLE_FILES/simconnect_h-0.7.2-py3-none-any.whl').FullName
$i=[Diagnostics.ProcessStartInfo]::new($py)
$i.WorkingDirectory=(Get-Location).Path
$i.UseShellExecute=$false
$i.RedirectStandardOutput=$true
$i.RedirectStandardError=$true
foreach($a in '-I','-B','-c',$probe,$dll,$expected,$wheel){$i.ArgumentList.Add($a)}
$p=[Diagnostics.Process]::Start($i)
try {
 $stdout=$p.StandardOutput.ReadToEndAsync()
 $stderr=$p.StandardError.ReadToEndAsync()
 if(!$p.WaitForExit(12000)){
  $p.Kill($true)
  if(!$p.WaitForExit(2000)){throw 'Unable to reap timed-out process'}
  throw 'Load-only probe timed out'
 }
 $stdout.GetAwaiter().GetResult()
 $stderr.GetAwaiter().GetResult()
 exit $p.ExitCode
} finally {$p.Dispose()}
```

## Output

```text
PASS: exact SDK DLL loaded on Windows CPython 3.12.14 (64-bit)
SHA-256: CFFB74B62B596A897B0FF101BAC3D66EA10236C540C75864658B451FFD1E6513
Resolved 12 wrapper exports: SimConnect_AddToDataDefinition, SimConnect_CallDispatch, SimConnect_ClearDataDefinition, SimConnect_Close, SimConnect_GetLastSentPacketID, SimConnect_MapClientEventToSimEvent, SimConnect_Open, SimConnect_RequestDataOnSimObject, SimConnect_RequestDataOnSimObjectType, SimConnect_SetDataOnSimObject, SimConnect_SubscribeToSystemEvent, SimConnect_TransmitClientEvent
SimConnect functions called: 0; sessions opened: 0; simulator launches: 0
```
