# Flight Recorder rules (proposal)

Status: **PROPOSAL for owner review.** `sources/04` and `sources/08` leave the Recorder rules open: flight start and end, block and airborne clocks, pause and Active Pause, sim rate, touchdown and bounce, circuits and multiple sectors, interruption and recovery. This package implements a sensible default for each one. Every threshold and policy is a field of `RecorderRules` (`bbc_recorder/rules.py`), so the owner can change a rule without changing code. Each flight carries `RULES_VERSION` and the full rule values. They are in the `FLIGHT_STARTED` event and in `build_flight_record()`, so a reader can always tell which rules produced a derived value.

The Recorder is a pure state machine. It has no Qt, no SimConnect and no Core import: it duck-types Core objects. It reads no clock of its own and returns events instead of writing files. The same tape always produces the same events.

## 1. Inputs

| Input | Source | Notes |
|---|---|---|
| `Sample` | `sample_from_snapshot(Snapshot)` | One coherent snapshot. Uses `SIMULATION TIME`, `ABSOLUTE TIME`, sim rate and the SimVars in `RECORDER_FIELDS`. `None` always means unknown, never zero. |
| `Lifecycle` | `lifecycle_from_update(Event / Connection / Gap)` | Covers `sim.pause_state` (Pause_EX1 flags), `sim.aircraft_crashed` / `sim.crashed`, `sim.crash_reset`, `sim.aircraft_loaded`, `sim.flight_loaded`, `sim.position_changed`, `sim.started` / `sim.stopped`, Connection DISCONNECTED / ERROR / CONNECTED, and Core `Gap`. |
| Manual | `manual_start()`, `manual_stop()`, `resume_automatic()` | These are the Start / Stop Logging controls. The UI asks for Stop confirmation; the Recorder does not. |

Subscribe to `sim.pause_state` only, not to `sim.paused` or `sim.pause_changed` as well, so that pause is not counted twice.

## 2. The Recorder timeline and its clocks

- **Credited time** advances by the `SIMULATION TIME` delta between two samples. That clock follows the sim rate and stops in pause. A 4x flight therefore credits simulated time, and the real-time figure is kept separately (`*_host_s`).
- **Host fallback.** When a sample has no simulation time, the Recorder credits the host delta multiplied by the reported sim rate, or by 1 when no rate is reported. Each clock is labelled `simulator`, `host_fallback` or `mixed`.
- **Distrust rule.** A simulation-time delta is not credited when it goes backwards by more than 0.5 s, for example after a reload or a restarted simulator. It is also not credited when it runs ahead of host time × rate × `max_sim_time_lead` + `max_sim_time_slack_s`. The flight gets a warning in either case. When the rate is unknown, the Recorder allows up to 16x.
- **Debounce windows** are measured on credited time. At 4x a 5 s confirmation takes 1.25 real seconds, and confirmation stops while the simulator is paused.
- **Block clock** runs from the block-off sample to the block-on sample. **Airborne clock** is the sum of the segments from each takeoff to each touchdown. A bounce's brief air time is part of the landing and is not counted as airborne. When block-off was not observed, for example when the flight started in the air, `block_s` is `None` and has a note. The clock is never estimated.
- **PROPOSAL: pause.** With `pause_policy=ALL`, any Pause_EX1 flag stops credited time: full pause, Active Pause and sim pause. The Recorder applies this by subtracting the paused share of host time, so it still works if SIMULATION TIME keeps running in Active Pause. `FULL_AND_SIM_ONLY` credits Active Pause time instead. Each flight records `paused_host_s`, `active_paused_host_s` and every pause episode.
- A long period without samples while paused is **not** a gap.

## 3. Phases and events

`IDLE → PREFLIGHT → TAXI_OUT → TAKEOFF_ROLL → AIRBORNE → LANDING_ROLL → TAXI_IN → COMPLETE`. A flight can also end as `ABANDONED`, or as discarded, which returns the Recorder to `IDLE`.

| Transition | Default rule (field) | Event time |
|---|---|---|
| arm (IDLE→PREFLIGHT) | the first sample with a known on-ground state (`auto_start`). A first sample that is already airborne starts in AIRBORNE when `start_in_air` is on, and the flight is marked `started_in_air`. | that sample |
| block-off | on the ground, speed above 3 kt for 5 s, parking brake not set (`block_off_*`) | **first** moving sample |
| takeoff roll | above 40 kt for 2 s (`takeoff_roll_*`) | first fast sample |
| rejected takeoff | back below 30 kt on the ground during the roll. The Recorder returns to TAXI_OUT and records it in `rejected_takeoffs`. | that sample |
| takeoff | airborne for 2 s **or** at least 50 ft AGL (`takeoff_confirm_*`). A brief wheel hop does not count. | lift-off sample |
| touchdown | the first on-ground sample while AIRBORNE. The aircraft enters LANDING_ROLL. | contact sample |
| bounce | airborne again after contact and back on the ground within 5 s while staying at or below 100 ft AGL (`bounce_max_s`, `touch_and_go_agl_ft`) | re-contact sample |
| touch-and-go | airborne after contact for 5 s or more, or above 100 ft AGL. The Recorder records a `TAKEOFF` with `touch_and_go=true`. | lift-off sample |
| end of landing roll | below 30 kt for 2 s | — |
| stop-and-go / circuit | a full-stop landing, then above 40 kt again before block-on. The aircraft takes off again within the **same flight**. | — |
| go-around | while airborne, the aircraft was above 500 ft AGL, descended below it, then climbed 300 ft without ground contact. Recorded in the summary (`go_arounds`). It is not an event, because the event vocabulary in `model.py` has no kind for it. | lowest point |
| block-on | stationary below 1 kt with `block_on_policy` evidence for 5 s. When the only evidence is the parking brake, the wait is 30 s. | first stationary sample |

**PROPOSAL: block-on policy.** The default is `ENGINES_OFF`: all known engines have stopped. A parking brake alone is accepted only when engine state is unknown, after 30 s. This is because pilots often hold short or wait on a taxiway with the brake set. `PARKING_BRAKE`, `ENGINES_OFF_OR_BRAKE` and `ENGINES_OFF_AND_BRAKE` are available as alternatives. Block-on before takeoff (return to stand) needs an engine shutdown **observed after block-off**, so a tug pushback with engines off never closes the flight.

**PROPOSAL: sectors.** Each block-off to block-on is one flight. After a normal block-on, the next sample arms a new flight in PREFLIGHT (`auto_rearm_after_end`). A flight that never reaches block-off, takeoff or touchdown is `loggable=false`. When it ends, it is "discarded" and nothing should be saved. Circuits, touch-and-goes and stop-and-gos stay within one flight. Each touchdown is classified as `full_stop`, `touch_and_go` or `unconfirmed`. `landings` counts touchdowns, not bounces.

## 4. Touchdown rate (provenance always labelled)

1. `touchdown_simvar`: `PLANE TOUCHDOWN NORMAL VELOCITY`, requested in the catalog unit (feet per second) and converted to feet per minute, changed compared with the value seen while airborne. MSFS latches this value, so it is valid even at 1 Hz. The Recorder waits up to `touchdown_simvar_wait_s` (1 s) for the value to update after contact. The event is still stamped with the contact sample.
2. `vertical_speed_high_rate`: used only when the last airborne `VERTICAL SPEED` sample is at most 0.25 s (`touchdown_vs_max_age_s`) before contact, and no gap came in between. In practice this means per-frame data.
3. Otherwise the rate is `unavailable` and carries a reason. **The Recorder never infers a rate from 1 Hz data.**

Sign convention: `rate_fpm` is negative for descent. The SimVar's magnitude is used.

## 5. Interruptions

- **Disconnect.** The Recorder emits `GAP` `status=started` with the reason. The flight stays open. The first sample after reconnection closes the gap with `GAP` `status=ended`. That event records host length, simulator delta, and credited and uncredited seconds. **PROPOSAL:** a gap's time is credited only when SIMULATION TIME is continuous and the gap lasts no more than `credit_gap_max_s` (60 s). Distance across a gap is kept separately as `distance_bridged_nm`.
- **Silent feed.** More than `gap_after_s` (5 s) of unpaused host time with no samples produces `GAP` `status=detected`.
- **Core `Gap`.** Produces `GAP` `status=reported` with `dropped_updates`. The next touchdown cannot use the vertical-speed rate.
- **Position jumps.** The Recorder emits `POSITION_JUMP` for slew mode (`IS SLEW ACTIVE`, one event per slew episode), for a simulator `PositionChanged`, for a clock discontinuity with movement, or for movement faster than (fastest ground speed × 1.5 + 150 kt) × dt + 0.2 nm. Jump distance is excluded (`distance_excluded_nm`). Debounces reset. A jump never creates a takeoff or a landing: the phase follows the new ground state and the flight gets a warning.
- **PROPOSAL: crash.** The Recorder emits `CRASHED`, ends the flight as `abandoned`, and holds automatic logging until `CrashReset`, a flight or aircraft load, or Start Logging.
- **PROPOSAL: aircraft change, flight loaded, sim stop.** These events close the open flight. The outcome is `complete` (with block-on "not observed") when the aircraft has landed and is in LANDING_ROLL or TAXI_IN. Otherwise it is `abandoned`, or `discarded` when the flight is not loggable. A repeated AircraftLoaded for the same aircraft is ignored. An aircraft change at the gate after block-on discards only the newly armed, empty flight.
- **PROPOSAL: after SimStop.** Once the simulator reports `SimStop` (the user left the flight for the menu or a load), no flight is armed automatically until `SimStart`. Samples that keep arriving from the menu or a loading screen therefore never open a flight. Start Logging still works. When the EFB starts mid-flight without having seen either event, arming works as usual.
- **Manual Stop.** Uses the same outcome rule, then holds automatic logging until Start Logging, `resume_automatic()` or a new aircraft or flight. Without this hold, the next sample would re-arm a flight immediately.

## 6. Adaptive sampling hint

`recorder.sampling_hint` returns a `SamplingHint(period, every, high_rate, reason)`. `period` is a `SnapshotPeriod` value. The hint is `sim_frame` during the takeoff and landing rolls, while a touchdown or bounce is being resolved, when below 1000 ft AGL, and when taxiing at 30 kt or more. It returns to `second` above 1500 ft AGL (hysteresis), when parked and when slow. The caller compares the hint with what it last applied and re-subscribes only on a change. That keeps simulator impact minimal.

## 7. Outputs

- `RecorderEvent`s, from `model.py`. Each one is stamped with the host time, simulation time and simulator UTC of the **evidence sample**, not the time it was emitted. `data` is JSON-safe and carries `flight_id`.
- `summary()` returns a `FlightSummary` for the current or the last flight, with `to_json()`. It contains block and airborne seconds (simulator, host and source label), pause seconds, uncredited gap seconds, time at each sim rate, distance (measured, bridged and excluded), takeoffs, touchdowns with rate provenance and bounces, landings, touch-and-goes, go-arounds, rejected takeoffs, gaps, position jumps, pauses, warnings and the rules version.
- `FLIGHT_ENDED.data.summary` holds the final summary. `build_flight_record()` gives a storage-neutral dict for `FlightSink.save_recorded_flight()`.

## 8. Integration (round 3: `bbc_recorder.service.RecorderService`, contract C3)

The service does all of this; the app only constructs it and ticks it.

- **Construction:** `RecorderService(api, sink, data_dir, rules=None, clock=None)`. `api` is a read-only Core `ConsumerAPI` (no control groups). `sink` is `bbc_efb.recorder_sink.LogbookSink(store_provider)` (contract C2) over the app's single `FlightStore`. Journals live under `data_dir/recorder/`.
- **Threads:** `tick()` runs on the Core thread after each pump (`CoreHost.add_tick`). `status()` is safe from any thread. `manual_start()`, `manual_stop(confirmed)`, `save_and_close(timeout_s)` and `close()` may be called from the UI thread; they never call Core (Core requests are queued for the next tick). Logbook saves run on a background thread, so the Core thread never waits on disk retries.
- **Feeding:** recorder snapshot (`snapshot_simvars()`, the catalog's names and units), a separate identity snapshot (`attribution.identity_simvars()`, every 5 s) and the lifecycle events (`sim.pause_state` only for pause). Each item from `poll()` goes to `FlightRecorder.process`, except a `SignalError` (one provider signal failed: journalled as a NOTE, not a gap; checked before `Gap` because Core is splitting it from `Gap`, contract C7).
- **Sampling:** the service follows `sampling_hint` by subscribing the new cadence first and releasing the old one after the new one delivers; samples from the overlap are fed once (strictly newer simulator time only). A cadence that never delivers within 10 s is dropped and the old one kept.
- **Journal:** every `RecorderEvent`, plus a SAMPLE track point (the `Sample` the state machine saw) every second, or every 0.2 s at high rate, plus ATTRIBUTION events (aircraft identity at flight start and on change; departure at block-off and arrival at block-on from nearest-airport facility queries) and NOTE events.
- **End of flight:** the journal stays open until the arrival lookup answers (at most 12 s), is closed with the state machine's outcome, and `recovery.finalize_closed` builds the record from the journal (`bbc_recorder.records.build_recorded_flight`), saves it and archives the journal. Discarded flights go to `recorder/discarded/` and never reach the Logbook.
- **Save & Exit, very long gaps (30 min):** `FlightRecorder.interrupt_flight()` drops the open flight without an end; the journal is left without a footer and `recovery.finalize_interrupted` adds the interruption gap and saves it as *interrupted* (no invented end time). Startup: `recover_unfinished()` does the same for journals an earlier run left behind and files empty ones under `discarded/`.
- **Diversions:** the arrival is the airport the aircraft actually reached; `set_planned_destination()` lets the arrival be compared with a plan, and a different arrival is only ever *suspected* (`attribution.assess_arrival`), never confirmed by the Recorder.
- A flight from the simulated provider carries `simulated=true` and must never be presented as a native recording.
