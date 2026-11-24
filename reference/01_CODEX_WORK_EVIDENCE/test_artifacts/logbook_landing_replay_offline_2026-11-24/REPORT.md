# Landing replay offline probe

24 November 2026. Two focused checks passed on the workspace Windows Python runtime. The probe used an in-memory SDK and synthetic samples. The guard recorded zero native library loads, network attempts or subprocess launches.

The current direct SimConnect provider accepted a 20 ms subscription request for all four candidate signals at a minimum interval of 1,000 ms. It issued `Period.SECOND` requests and delivered the injected on-ground state, indicated vertical speed, last-touchdown normal velocity and G-force samples as distinct simulated values. The fake SDK accepts the supplied definitions. This does not verify native field units, sign, availability or packet timing.

The second check swept all whole-millisecond sample phases relative to a synthetic 350 ms airborne bounce interval and a separate 60 ms high-load window. A hit means at least one ideal sample fell in the window. It does not establish a valid bounce classification or peak measurement.

| Ideal sample interval | Bounce window hit | High-load window hit |
|---|---:|---:|
| 1,000 ms | 350/1,000 (35%) | 60/1,000 (6%) |
| 100 ms | 100/100 (100%) | 60/100 (60%) |
| 50 ms | 50/50 (100%) | 50/50 (100%) |

The current periodic path is too coarse to rely on for brief events. Evaluate a bounded faster path through Core. Verify timestamp continuity, actual cadence, contact correlation, peak-G under-sampling and simulator frame-time impact before setting grading requirements. Ideal sample windows do not establish a production cadence. The interactive concept uses authored long-float and bounce examples. It is not an implemented detector or scoring engine.

Run from the workspace root with the project's Python runtime:

```powershell
python -I -B reference/01_CODEX_WORK_EVIDENCE/test_artifacts/logbook_landing_replay_offline_2026-11-24/probe.py
```
