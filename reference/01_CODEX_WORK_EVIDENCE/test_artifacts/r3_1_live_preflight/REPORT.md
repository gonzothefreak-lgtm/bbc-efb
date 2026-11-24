# R3.1 live smoke-test preflight

**BLOCKED by the supplied simconnect-H v0.7.2 distribution.** MSFS 2020 was not running. The preflight did not start it or attempt a connection.

The standalone SDK DLL probe had passed. The actual v0.7.2 SimConnect.load_dll call with the same absolute DLL path raised NameError before attempting the native load. simconnect_native/client.py does not import find_simconnect_dll or is_untrusted_simconnect_dll from simconnect_native/dll.py. An explicit DLL path bypasses the first missing name, but line 215 still fails on the second. The source distribution has the same missing imports. R3.1 calls the wrapper's standard connect path (adapter_simconnect_h.py line 282), which reaches the same loader.

No distribution or Core files were modified. The next validation input is an owner-approved corrected wrapper distribution. Repeat the wrapper preload check before starting the parked-aircraft, read-only smoke test. Native dataflow and teardown remain unverified.

## Exact reproduction

```powershell
$py='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$wheel=(Get-Item -LiteralPath 'project sources/02_ALL_OTHER_AVAILABLE_FILES/simconnect_h-0.7.2-py3-none-any.whl').FullName
$dll='C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll'
& $py -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from simconnect_native import SimConnect; c=SimConnect(auto_reconnect=False); c.load_dll(sys.argv[2]); print(c.dll is not None, c.is_open)' $wheel $dll
exit $LASTEXITCODE
```

## Output

```text
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "C:\Users\peter\OneDrive\Desktop\gpt files\Workspace - BBC EFB and Black Box Core\project sources\02_ALL_OTHER_AVAILABLE_FILES\simconnect_h-0.7.2-py3-none-any.whl\simconnect_native\client.py", line 215, in load_dll
NameError: name 'is_untrusted_simconnect_dll' is not defined
```
