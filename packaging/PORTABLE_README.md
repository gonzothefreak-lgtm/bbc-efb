# BBC EFB portable build

This is an **internal concept build** of the BBC Integrated EFB for Microsoft Flight Simulator 2020 on Windows 10/11 (64-bit). It runs from this folder and doesn't need an installer or administrator rights.

Before you use it, read `LIMITATIONS.md`. The build was tested offline only and has not been checked against a live simulator. `TEST_STATUS.txt` records the tests that ran and their results.

## What is in this ZIP

| Item | What it is |
|---|---|
| `BBC-EFB/BBC-EFB.exe` | The EFB. Keep it with its `_internal` folder. |
| `TEST_STATUS.txt` | Tests run before packaging, with their result and the exact dependency versions |
| `TEST_OUTPUT.txt` | Full test runner output |
| `LIMITATIONS.md` | What is unverified or not included |
| `CHANGELOG.md` | What changed in this build |
| `THIRD_PARTY_NOTICES.md` | Components included and their licences |
| `BUNDLED_MODULES.txt` | The application modules frozen into the program |
| `MANIFEST.sha256` | SHA-256 checksum for every file in the ZIP |

The full source, with its tests and build scripts, is delivered separately as `BBC-EFB-<version>-source.zip`.

## Start the EFB

1. Extract the whole ZIP to a folder you can write to, for example `C:\Users\<you>\BBC-EFB`. Don't run it from inside the ZIP. Avoid `C:\Program Files`.
2. Double-click `BBC-EFB\BBC-EFB.exe`.
3. On first launch, choose a local folder for your Logbook data. The EFB stores your flights there as plain JSON files that you own.

Windows SmartScreen may warn about an unrecognised app, because the build isn't code-signed. Choose **More info**, then **Run anyway**, but only if the ZIP came from the project owner and its checksum matches the `SHA256SUMS.txt` delivered with it. To check it, run `certutil -hashfile <zip> SHA256` in a Command Prompt.

## Connect to MSFS 2020 (SimConnect.dll)

The EFB talks to the simulator through Microsoft's `SimConnect.dll`. **The DLL isn't included**, because no permission to redistribute it has been confirmed. Use your own copy from the MSFS 2020 SDK. The EFB searches these locations in order and uses the first DLL it finds:

1. The path given with `--simconnect-dll "C:\path\to\SimConnect.dll"`, or chosen in Settings.
2. The `BBC-EFB` folder, next to `BBC-EFB.exe`. You can copy your own SDK DLL here.
3. The MSFS SDK named by the `MSFS_SDK` environment variable, which the SDK installer sets: `%MSFS_SDK%\SimConnect SDK\lib\SimConnect.dll`. By default the SDK installs to `C:\MSFS SDK\SimConnect SDK\lib\SimConnect.dll`.

If no DLL is found, the EFB still opens. The SimConnect status in the footer then says it isn't connected and why. Nothing is made up to fill the gap.

To get the SDK, enable **Developer Mode** in MSFS 2020 (Options, General Options, Developers), then use the **Help, SDK Installer** menu in the developer toolbar.

## Command-line options

Run these from a Command Prompt in the `BBC-EFB` folder. A windowed program prints nothing, so `--help` and `--version` don't show any output here. Use the source build to see them.

| Option | Effect |
|---|---|
| `--data-dir DIR` | Use this Logbook data folder for this run only |
| `--simconnect-dll PATH` | Use this SimConnect.dll |
| `--log-level DEBUG` | Write more detail to the log file |
| `--simulated` | Development only. Uses simulated data, labelled SIMULATED everywhere. Never use it for real flights. |

## Logs and problems

The EFB writes a rotating log to `logs` in your data folder. Until a data folder is chosen, or if it can't be written, the log goes to `%LOCALAPPDATA%\BBC EFB\logs`. Send `bbc-efb.log` and `fault.log` with any problem report, and check them for anything personal first.

## Remove it

Delete the extracted folder. Your Logbook data folder is separate and stays untouched. Delete it only if you no longer want your flights. Two items remain in your user profile: the settings and preferences files in `%APPDATA%\BBC EFB`, and the fallback log folder in `%LOCALAPPDATA%\BBC EFB`.
