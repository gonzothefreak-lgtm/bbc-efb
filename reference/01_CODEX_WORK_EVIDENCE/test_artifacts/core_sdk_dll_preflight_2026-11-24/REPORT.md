# Direct SimConnect SDK DLL load-only preflight

**24 November 2026** · Windows CPython 3.12.14 x64 · MSFS SDK 0.24.6.0

**PASS.** The approved DLL at `C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll` matched the preserved SHA-256. The new adapter's production loader resolved all 13 declared exports and assigned its Python argument and return types. The guard allowed one load of the verified path and the declared symbol lookups only. There were zero guard violations.

No SimConnect function was invoked. This verifies loading and symbol availability for the installed DLL. Simulator connection, runtime calling behaviour, live telemetry, writes and teardown remain unverified.

## Exact invocation

Run from the workspace root using PowerShell.

```powershell
$bbcPython='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$bbcProbe=@'
import ctypes, faulthandler, hashlib, pathlib, struct, sys

root = pathlib.Path.cwd()
sys.path.insert(0, str(root))
from bbc_core.simconnect_abi import HRESULT, SIGNATURES
from bbc_core.simconnect_sdk import SimConnectSdk

dll = pathlib.Path(r"C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll").resolve(strict=True)
expected_hash = "cffb74b62b596a897b0ff101bac3d66ea10236c540c75864658b451ffd1e6513"
with dll.open("rb") as handle:
    digest = hashlib.file_digest(handle, "sha256").hexdigest()
if digest != expected_hash:
    raise SystemExit(f"Unexpected SDK DLL SHA-256: {digest}")

loads = []
symbols = []
blocked = []
def guard(event, args):
    if event == "ctypes.dlopen":
        if args[0] is not None and str(args[0]).casefold() == str(dll).casefold() and not loads:
            loads.append(args[0])
            return
    elif event == "ctypes.dlsym":
        if args[1] in SIGNATURES:
            symbols.append(args[1])
            return
    elif not (event == "ctypes.call_function"
              or event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec"))
              or event in ("os.system", "os.startfile", "_winapi.CreateProcess")):
        return
    blocked.append(event)
    raise RuntimeError("Load-only guard blocked " + event)

class LoadOnlySdk(SimConnectSdk):
    def call(self, name, *args):
        blocked.append(name)
        raise RuntimeError("Load-only probe attempted a SimConnect call: " + name)

sys.addaudithook(guard)
faulthandler.dump_traceback_later(15, exit=True)
try:
    sdk = LoadOnlySdk(dll)
    sdk.load()
    assert set(sdk._functions) == set(SIGNATURES)
    assert len(loads) == 1 and symbols == list(SIGNATURES) and not blocked
    for name, prototype in SIGNATURES.items():
        function = sdk._functions[name]
        assert function.argtypes == prototype and function.restype is HRESULT, name
    print(f"Windows CPython {sys.version.split()[0]} x{struct.calcsize('P') * 8}")
    print(f"SDK DLL SHA-256: {digest}")
    print(f"PASS: one selected DLL loaded and {len(symbols)}/{len(SIGNATURES)} exports resolved")
    print("PASS: Python argument and return declarations assigned")
    print(f"SimConnect functions invoked: 0; guard violations: {len(blocked)}")
finally:
    faulthandler.cancel_dump_traceback_later()
'@
& $bbcPython -I -B -c $bbcProbe
exit $LASTEXITCODE
```

## Output

```text
Windows CPython 3.12.14 x64
SDK DLL SHA-256: cffb74b62b596a897b0ff101bac3d66ea10236c540c75864658b451ffd1e6513
PASS: one selected DLL loaded and 13/13 exports resolved
PASS: Python argument and return declarations assigned
SimConnect functions invoked: 0; guard violations: 0
```
