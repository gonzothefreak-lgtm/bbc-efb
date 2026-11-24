# Third-party and SDK inventory — referenced, not bundled

| Item | Source / provenance | Version / SHA-256 / known local location | Status |
|---|---|---|---|
| `simconnect_h-0.7.2-py3-none-any.whl` | Owner-supplied third-party `simconnect-H` wheel, also in previous 77-file transfer | v0.7.2; `ddd3216a4d8303e35c6e8cf2b469cdace443f0b9562c2f6052190023c53c6c7e`; earlier `project sources/02_ALL_OTHER_AVAILABLE_FILES/` | **NOT BUNDLED**; first evaluation only, not selected; Code/Windows import report preserved |
| `simconnect_h-0.7.2.tar.gz` | Owner-supplied upstream third-party source archive | v0.7.2; `56bd7fb24de7c2db42746a827ae92184059a462d7f67b2a9ae8f212c32dcd994`; same prior location | **NOT BUNDLED**; upstream reportedly missing two loader imports |
| Microsoft MSFS SimConnect SDK `SimConnect.dll` | Owner-local x64 MSFS SDK install; **no redistribution authority established** | SDK 0.24.6.0; SHA-256 `CFFB74B62B596A897B0FF101BAC3D66EA10236C540C75864658B451FFD1E6513`; `C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll` | **NOT BUNDLED**; separate 12/12 export load-only probe, no SimConnect function calls |
| PySide6 / Qt Widgets | Approved UI framework **direction**, package/version not selected | Windows 10/11; package version, licence, deployment method and measured overhead open | **NOT installed or selected by this package** |
| Windows CPython | Original Codex tests reported using 64-bit CPython 3.12.14 in the Codex app environment | Runtime path from original test reports; may not persist after update | Package does not ship interpreter or requirements environment |

There is no approval for third-party binary redistribution, modified wrapper distribution, fixed SDK binding or real simulator testing merely because this inventory exists. Review provenance, licensing, environment and explicit owner permissions before copying or running these items. Original commands from Codex remain in preserved reports.
