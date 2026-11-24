# SimConnect function dictionary verification

**24 November 2026** · Installed MSFS 2020 SDK 0.24.6.0 · x64 DLL

**PASS.** The static check compared the installed header, the DLL's PE export names read from disk, the installed Microsoft documentation, and the dictionary. Each of the 91 exported names appears once and links to the matching declaration line. All linked SDK pages exist in the local documentation. The 13 entries marked as Core paths match the current adapter's call sites and binding declarations. This check made no DLL loads, SDK calls or simulator connections.

The installed reference index contains 87 exact name matches. The dictionary accounts for the facility-list spelling discrepancy, the separately documented ExecuteAction, and two header and export entries with no matched MSFS 2020 reference page. Deprecation was checked against the [official API index](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/SimConnect_API_Reference.htm) and the individual [input mapping reference](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/InputEvents/SimConnect_MapInputEventToClientEvent.htm).

## Exact invocation

From the workspace root in PowerShell.

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
for module in ("simconnect.py", "simconnect_sdk.py"):
    parsed = ast.parse((Path("bbc_core") / module).read_text(encoding="utf-8"))
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
        r"\| (Core path|Pending|Deprecated|Header only) "
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
assert {name for name, status in rows.items() if status == "Core path"} == used == set(bindings)
counts = Counter(rows.values())
assert counts == {"Core path": 13, "Pending": 54, "Deprecated": 22, "Header only": 2}
print(f"SDK version: {(sdk / 'version.txt').read_text().strip()}")
print(f"Header SHA-256: {hashlib.sha256(header.read_bytes()).hexdigest()}")
print(f"DLL SHA-256: {hashlib.sha256(image).hexdigest()}")
print(f"PASS: {len(rows)} unique rows, {len(declarations)} declarations, {len(exports)} x64 exports")
print("PASS: all header line numbers and installed SDK documentation links match")
print("Status: " + ", ".join(f"{key} {counts[key]}" for key in ("Core path", "Pending", "Deprecated", "Header only")))
print(f"Installed documentation index matches by exact spelling: {len(indexed)} of {len(rows)}")
print("DLL loads: 0; SimConnect calls: 0; simulator connections: 0")
'@
& $bbcPython -I -B -c $bbcInventory
exit $LASTEXITCODE
```

## Output

```text
SDK version: 0.24.6.0
Header SHA-256: bc041ea0d1e41b34de9730ab7e42cbf7fee1bbffe38e79fac7c3b2128e9138a6
DLL SHA-256: cffb74b62b596a897b0ff101bac3d66ea10236c540c75864658b451ffd1e6513
PASS: 91 unique rows, 91 declarations, 91 x64 exports
PASS: all header line numbers and installed SDK documentation links match
Status: Core path 13, Pending 54, Deprecated 22, Header only 2
Installed documentation index matches by exact spelling: 87 of 91
DLL loads: 0; SimConnect calls: 0; simulator connections: 0
```
