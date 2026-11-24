# Third-party components

The portable build contains the components below. `TEST_STATUS.txt` lists the exact installed versions. **The licence review for any distribution beyond internal owner review is still an open owner decision.**

| Component | Included as | Licence | Source |
|---|---|---|---|
| CPython runtime and standard library | `_internal\python3*.dll`, `base_library.zip` and extension modules | Python Software Foundation License Version 2 | https://www.python.org/ and https://docs.python.org/3/license.html |
| Qt for Python (PySide6, PySide6-Essentials) and Shiboken6 | `_internal\PySide6`, `_internal\shiboken6` | LGPL-3.0-only (also offered under GPL-2.0 / GPL-3.0 or a commercial licence) | https://pypi.org/project/PySide6/ and https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt 6 libraries shipped with PySide6 | `_internal\PySide6\Qt6*.dll`, `_internal\PySide6\plugins` | LGPL-3.0-only for the modules used; some Qt modules have other terms | https://www.qt.io/licensing/ and https://doc.qt.io/qt-6/licensing.html |
| PyInstaller bootloader | Built into `BBC-EFB.exe` | GPL-2.0-or-later with the PyInstaller bootloader exception, which allows distributing frozen programs under any licence | https://pyinstaller.org/en/stable/license.html |
| Microsoft Visual C++ runtime files that come with Python and Qt | `_internal\VCRUNTIME140*.dll`, `_internal\MSVCP140*.dll` (when present) | Microsoft Visual C++ redistributable terms | https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist |

## LGPL notes for Qt for Python

- The build is a PyInstaller **onedir** folder. Qt and PySide6 stay as separate, unmodified DLL and `.pyd` files in `_internal`, so they can be replaced with compatible versions. This is the relinking freedom the LGPL requires.
- Qt and PySide6 haven't been modified. Their source code is available from the links above for the exact versions listed in `TEST_STATUS.txt`.
- The full LGPL-3.0 and GPL-3.0 licence texts must be included before any distribution beyond internal review. Proposed approach: add them under `licenses\` in the portable ZIP. This is still an open owner decision.

## Not included

- **Microsoft SimConnect.dll** (MSFS 2020 SDK). Redistribution permission hasn't been established, so pilots use their own SDK copy.
- **simconnect-H** and other evaluated third-party SimConnect wrappers. Black Box Core uses its own `ctypes` binding.
