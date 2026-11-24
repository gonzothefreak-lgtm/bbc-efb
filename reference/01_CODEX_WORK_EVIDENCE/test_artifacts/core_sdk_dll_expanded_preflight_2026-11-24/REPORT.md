# Expanded SimConnect load-only verification

24 November 2026, Windows CPython 3.12.14 x64, installed MSFS 2020 SDK 0.24.6.0.

**PASS.** The static check found 91 matching header declarations, x64 PE export names and dictionary rows. The current adapter references and declares the same 67 symbols shown as implemented in the dictionary. All linked reference pages were present in the installed SDK. The official API reference was also consulted. [Microsoft SimConnect API reference](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/SimConnect_API_Reference.htm)

The production loader resolved **67/67 exports** from the same SHA-256-verified DLL used for the earlier 13-export check. It assigned the adapter's declared Python argument and result types. The load-only probe permitted a single load from the verified SDK path and symbol lookups for that DLL. It recorded zero guard violations, and no SimConnect function was invoked.

This verifies export resolution and application of the Python declarations. Live function behavior, ABI calling behaviour, actual simulator effects, and aircraft and add-on compatibility remain unverified. [Microsoft connection reference](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/General/SimConnect_Open.htm)

## Static SDK inventory check

Run from the workspace root in PowerShell.

```powershell
$bbcPython='C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
$bbcInventory=@'
from pathlib import Path
from struct import unpack_from
import ast
import hashlib
import json
import re

sdk = Path(r"C:\MSFS SDK")
simconnect = sdk / "SimConnect SDK"
header = simconnect / "include" / "SimConnect.h"
dll = simconnect / "lib" / "SimConnect.dll"
source = header.read_text(encoding="utf-8-sig")
declarations = {}
for match in re.finditer(r"(?m)^SIMCONNECTAPI\s+(SimConnect_\w+)\s*\((.*)\);\s*$", source):
    name, parameters = match.groups()
    assert name not in declarations, name
    declarations[name] = [source.count("\n", 0, match.start()) + 1, parameters]
assert len(declarations) == len(re.findall(r"(?m)^SIMCONNECTAPI\s+SimConnect_", source))

docs = sdk / "Documentation" / "html" / "Programming_Tools" / "SimConnect"
pages = {path.stem: path.relative_to(docs).as_posix()
         for path in (docs / "API_Reference").rglob("SimConnect_*.htm")}
index = (docs / "SimConnect_API_Reference.htm").read_text(encoding="utf-8-sig")
indexed = set(re.findall(r"\bSimConnect_[A-Za-z0-9_]+\b", re.sub(r"<[^>]+>", " ", index)))
indexed = sorted(name for name in indexed if name in declarations)

image = dll.read_bytes()
pe = unpack_from("<I", image, 0x3C)[0]
assert image[:2] == b"MZ" and image[pe:pe+4] == b"PE\0\0"
machine, section_count = unpack_from("<HH", image, pe + 4)
optional_size = unpack_from("<H", image, pe + 20)[0]
optional = pe + 24
assert machine == 0x8664 and unpack_from("<H", image, optional)[0] == 0x20B
assert unpack_from("<I", image, optional + 108)[0] > 0
sections = [unpack_from("<IIII", image, optional + optional_size + number * 40 + 8)
            for number in range(section_count)]
def offset(rva, size):
    for _, start, raw_size, position in sections:
        displacement = rva - start
        if 0 <= displacement and displacement + size <= raw_size:
            address = position + displacement
            assert address + size <= len(image)
            return address
    raise ValueError(f"RVA outside file-backed sections: {rva:#x}")

export_rva = unpack_from("<I", image, optional + 112)[0]
export = offset(export_rva, 40)
name_count = unpack_from("<I", image, export + 24)[0]
name_table = offset(unpack_from("<I", image, export + 32)[0], name_count * 4)
exports = set()
for n in range(name_count):
    name_rva = unpack_from("<I", image, name_table + n * 4)[0]
    position = offset(name_rva, 1)
    end = image.index(0, position, min(position + 1024, len(image)))
    name = image[position:end].decode("ascii")
    if name.startswith("SimConnect_"):
        exports.add(name)
assert len(exports) <= name_count

tree = ast.parse(Path("bbc_core/simconnect_abi.py").read_text(encoding="utf-8"))
signatures = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "SIGNATURES"
                          for target in node.targets))
bindings = [node.value for node in signatures.keys]
used = set()
for module in Path("bbc_core").glob("*.py"):
    if module.name == "simconnect_abi.py":
        continue
    parsed = ast.parse(module.read_text(encoding="utf-8"))
    used.update(node.value for node in ast.walk(parsed)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value in bindings)
assert used <= set(bindings) <= set(declarations)
from collections import Counter

dictionary = Path("sources/10_SIMCONNECT_FUNCTION_DICTIONARY.md").read_text(encoding="utf-8")
rows = {}
for line in dictionary.splitlines():
    match = re.fullmatch(
        r"\| (?:\[(SimConnect_\w+)\]\(([^)]+)\)|`(SimConnect_\w+)`) \| .+? "
        r"\| (Core path|Offline path|Experimental|Host helper|Deprecated|Unsupported|Header only) "
        r"\| \[(\d+)\]\(<C:/MSFS SDK/SimConnect SDK/include/SimConnect\.h:\d+>\) \|",
        line,
    )
    if not match:
        continue
    linked_name, link, plain_name, status, line_number = match.groups()
    name = linked_name or plain_name
    assert name not in rows, name
    assert int(line_number) == declarations[name][0]
    if link:
        relative = link.split("/Programming_Tools/SimConnect/", 1)[1]
        assert (docs / relative).is_file(), name
    rows[name] = status

assert len(rows) == len(declarations) == len(exports) == 91
assert set(rows) == set(declarations) == exports
implemented = {name for name, state in rows.items()
               if state in ("Core path", "Offline path", "Experimental", "Host helper")}
assert implemented == used == set(bindings), (sorted(implemented - used), sorted(used - implemented))
assert len(implemented) == 67
print(f"PASS: {len(implemented)} adapter bindings match the implemented dictionary rows and adapter references")
counts = Counter(rows.values())
assert counts == {"Core path": 13, "Offline path": 47, "Experimental": 3, "Host helper": 4,
                  "Deprecated": 22, "Unsupported": 1, "Header only": 1}
print(f"SDK version: {(sdk / 'version.txt').read_text().strip()}")
print(f"Header SHA-256: {hashlib.sha256(header.read_bytes()).hexdigest()}")
print(f"DLL SHA-256: {hashlib.sha256(image).hexdigest()}")
print(f"PASS: {len(rows)} unique rows, {len(declarations)} declarations, {len(exports)} x64 exports")
print("PASS: all header line numbers and installed SDK documentation links match")
print("Status: " + ", ".join(f"{key} {counts[key]}" for key in ("Core path", "Offline path", "Experimental", "Host helper", "Deprecated", "Unsupported", "Header only")))
print(f"Installed documentation index matches by exact spelling: {len(indexed)} of {len(rows)}")
print("DLL loads: 0; SimConnect calls: 0; simulator connections: 0")
'@
& $bbcPython -I -B -c $bbcInventory
exit $LASTEXITCODE
```

```text
PASS: 67 adapter bindings match the implemented dictionary rows and adapter references
SDK version: 0.24.6.0
Header SHA-256: bc041ea0d1e41b34de9730ab7e42cbf7fee1bbffe38e79fac7c3b2128e9138a6
DLL SHA-256: cffb74b62b596a897b0ff101bac3d66ea10236c540c75864658b451ffd1e6513
PASS: 91 unique rows, 91 declarations, 91 x64 exports
PASS: all header line numbers and installed SDK documentation links match
Status: Core path 13, Offline path 47, Experimental 3, Host helper 4, Deprecated 22, Unsupported 1, Header only 1
Installed documentation index matches by exact spelling: 87 of 91
DLL loads: 0; SimConnect calls: 0; simulator connections: 0
```

## Guarded DLL load-only check

Run from the workspace root in PowerShell.

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
        if args[1] in SIGNATURES and str(args[0]._name).casefold() == str(dll).casefold():
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
    assert len(SIGNATURES) == 67
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

```text
Windows CPython 3.12.14 x64
SDK DLL SHA-256: cffb74b62b596a897b0ff101bac3d66ea10236c540c75864658b451ffd1e6513
PASS: one selected DLL loaded and 67/67 exports resolved
PASS: Python argument and return declarations assigned
SimConnect functions invoked: 0; guard violations: 0
```
