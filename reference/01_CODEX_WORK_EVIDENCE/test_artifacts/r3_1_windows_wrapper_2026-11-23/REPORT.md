# R3.1 Windows wrapper validation

**PASS for the DLL-free Windows gate.** The supplied simconnect-H v0.7.2 wheel imported directly on Windows CPython 3.12.14 (64-bit) without installation. The probe checked all four real datatype conversions through R3.1, bound the adapter's connection, event and worker method calls to the actual wrapper signatures, and exercised numeric/string registration, retirement and close on a client with no native handle. No errors, timeouts or guard violations occurred.

No native DLL was loaded, no real connection was attempted and no simulator was launched. The probe used an 8-second child watchdog and an independent 12-second parent deadline. No Core source changed. No SimConnect.dll was found in the supplied project-source or candidate directories. Native load, dispatch, cleanup, event delivery and MSFS remain unverified.

## Exact command

```powershell
$py='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$wheel=(Get-Item -LiteralPath 'project sources/02_ALL_OTHER_AVAILABLE_FILES/simconnect_h-0.7.2-py3-none-any.whl').FullName
$src=(Get-Item -LiteralPath 'development reviews/BBC_EFB_Black_Box_Core_R3.1_Source_Candidate_2026-11-23').FullName
$probe=@'
import ctypes, faulthandler, importlib.metadata, inspect, pathlib, struct, sys
events = []
def guard(event, args):
    if event == "ctypes.dlopen" or event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec")) or event in ("os.system", "os.startfile", "_winapi.CreateProcess"):
        events.append(event)
        raise RuntimeError("Validation guard blocked " + event)
sys.addaudithook(guard)
faulthandler.dump_traceback_later(8, exit=True)
wheel, source = map(pathlib.Path, sys.argv[1:3])
sys.path[:0] = [str(wheel), str(source / "src")]
import simconnect_native as native
from bbc_core.adapter_simconnect_h import SimConnectHProvider
from bbc_core.model import SimVar, ValueType
assert str(wheel).casefold() in str(native.__file__).casefold()
assert importlib.metadata.version("simconnect-H") == "0.7.2"
assert sys.platform == "win32" and struct.calcsize("P") == 8
provider = SimConnectHProvider()
for kind, expected in [(ValueType.FLOAT64, 4), (ValueType.INT32, 1), (ValueType.INT64, 2), (ValueType.STRING, 11)]:
    assert provider._datatype(SimVar("TEST", "number", kind), native) == expected
client = native.SimConnect(auto_reconnect=False)
def callback(*args):
    raise AssertionError("Unexpected callback from closed client")
inspect.signature(client.connect).bind("BBC EFB", open_timeout=5.0, config_indices=(0,), start_dispatch=False, auto_reconnect=False)
inspect.signature(client.subscribe_system_event).bind("Pause", callback)
inspect.signature(client.start_background_dispatch).bind()
inspect.signature(client.stop_background_dispatch).bind()
client.set_dispatch_cb(callback)
assert client.dll is None and not client.is_open and not client.dispatch_thread_alive
numeric = client.subscribe_many({"v0": native.DataField("PLANE ALTITUDE", "feet", 4)}, callback, period=native.SIMCONNECT_PERIOD_SECOND)
string = client.subscribe_string("TITLE", callback, period=native.SIMCONNECT_PERIOD_SECOND, immediate_first=False)
assert type(numeric) is int and type(string) is int and numeric != string
assert client.unsubscribe(numeric) is True and client.unsubscribe(string) is True
assert client.unsubscribe(numeric) is False
client.close()
assert client.dll is None and not client.is_open and not client.dispatch_thread_alive and not client.native_hung
assert not events, events
faulthandler.cancel_dump_traceback_later()
print("PASS: Windows", sys.version.split()[0], "64-bit, supplied simconnect-H 0.7.2 wheel")
print("PASS: real ctypes datatypes, adapter call signatures, closed-client registration/retirement and close")
print("DLL loads, real connections and simulator launches: 0; guard violations:", len(events))
'@
$info=[Diagnostics.ProcessStartInfo]::new($py)
$info.WorkingDirectory=$src
$info.UseShellExecute=$false
$info.RedirectStandardOutput=$true
$info.RedirectStandardError=$true
foreach($arg in '-I','-B','-c',$probe,$wheel,$src) { $info.ArgumentList.Add($arg) }
$child=[Diagnostics.Process]::Start($info)
try {
  $stdout=$child.StandardOutput.ReadToEndAsync()
  $stderr=$child.StandardError.ReadToEndAsync()
  if(!$child.WaitForExit(12000)) {
    $child.Kill($true)
    if(!$child.WaitForExit(2000)) { throw 'Unable to reap probe' }
    throw 'Windows wrapper probe exceeded deadline'
  }
  $stdout.GetAwaiter().GetResult()
  $stderr.GetAwaiter().GetResult()
  exit $child.ExitCode
} finally { $child.Dispose() }
```

## Output

```text
PASS: Windows 3.12.14 64-bit, supplied simconnect-H 0.7.2 wheel
PASS: real ctypes datatypes, adapter call signatures, closed-client registration/retirement and close
DLL loads, real connections and simulator launches: 0; guard violations: 0
```
