# Known limitations of this build

This is an internal concept build for owner review. It is not a release. Everything below is either unverified or deliberately left out. `TEST_STATUS.txt` records what was actually tested for this exact build.

## Not verified against a live simulator

The build was tested **offline only**. The guarded test suite blocks native DLL loads, sockets and subprocesses, and uses simulated and fake SimConnect providers. No part of this build has been run against MSFS 2020. Until a separately approved live validation is done, the following are **unverified**:

- Connecting to MSFS 2020, disconnecting, reconnecting after the simulator restarts, and clean teardown on exit.
- The real telemetry values, units, altitude datum and update cadence, including stale-value handling.
- Simulator writes and readback, including COM radio frequency changes in both 25 kHz and 8.33 kHz spacing.
- Aircraft input discovery and Client Data with third-party aircraft. PMDG input discovery returned zero entries in an earlier read-only probe.
- The three experimental simulator calls: flight save, flight plan load and reserved Tab combinations.
- The impact on simulator frame times, CPU, memory, GPU and disk with the EFB active, idle and minimised.

Earlier read-only probes (Asobo 747-8i and PMDG 737-800BCF) checked basic telemetry with an older adapter. They don't verify this build.

## Automatic Flight Logging

- The Recorder's rules are **proposals awaiting owner confirmation**. They cover flight start and end, block and airborne clocks, pause and Active Pause, sim rate, touchdown and bounce detection, disconnect gaps and interruption recovery.
- The Recorder has only been tested with recorded and synthetic flight data. A complete automatically recorded flight in MSFS hasn't been verified yet.
- Automatically logged flights should be checked by hand until the live validation is done.

## Packaging and distribution

- **SimConnect.dll isn't included.** Redistribution permission hasn't been established. Pilots use their own MSFS SDK copy. See `README.md`.
- **The program isn't code-signed.** Windows SmartScreen and some antivirus products may warn about it or block it.
- **No installer, no automatic updates and no Start menu entry.** This is a portable folder only.
- **Windows 10/11 x64 only.** 32-bit Windows, Windows on ARM and other operating systems aren't supported.
- **The bundled third-party licences haven't had a final review.** Qt for Python (PySide6) is used under the LGPL-3.0. The licence obligations for any distribution beyond internal review need an owner decision. See `THIRD_PARTY_NOTICES.md`.
- **The simulated provider is included as code.** It runs only with the `--simulated` development option, and everything it produces is labelled SIMULATED. Builds made with `-ExcludeSimulated` leave it out.
- **Build dependencies are pinned by version only, not by hash.** Hash pinning is proposed in `packaging/requirements-build.txt`.
- **PyInstaller is the proposed freezer.** It hasn't been formally selected by the owner.

## Not in this build

- Drop-in EFB modules. These are scheduled after the first-build review.
- The standalone Core process and its local interface. Core runs inside the EFB process in this build.
- Online services and VATSIM features. The EFB needs no network connection.

## How to report a problem

Note the version from `CHANGELOG.md` and the source commit from `TEST_STATUS.txt`. Describe what you did, what you expected and what happened. Attach `bbc-efb.log` and `fault.log`, after checking them for anything personal.
