"""Flight-tape acceptance tests for the Flight Recorder state machine (bbc_recorder)."""

from __future__ import annotations

import itertools
import json
import math
import unittest
from datetime import datetime, timedelta, timezone

from bbc_recorder import (
    EventKind, FlightRecorder, Lifecycle, LifecycleKind, PausePolicy, Phase, RecorderEvent,
    RecorderRules, Sample,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


class Tape:
    """Synthetic flight tape: a tiny kinematic model that feeds the Recorder like Core would.

    The tape honours the Recorder's sampling hint (1 Hz normally, 30 Hz when it asks for
    per-frame data) unless ``adaptive=False``. While ``paused`` the simulator clock and the
    aircraft freeze but host time keeps running.
    """

    def __init__(self, rules: RecorderRules | None = None, *, rate: float = 1.0, adaptive: bool = True,
                 with_sim_time: bool = True, with_tdv: bool = True, high_hz: float = 30.0) -> None:
        ids = itertools.count(1)
        self.rec = FlightRecorder(rules, id_factory=lambda: f"F{next(ids)}", now=lambda: self.host)
        self.host = T0
        self.sim = 5000.0
        self.rate = rate
        self.adaptive = adaptive
        self.with_sim_time = with_sim_time
        self.with_tdv = with_tdv
        self.high_hz = high_hz
        self.paused = False
        self.sim_runs_in_pause = False
        self.send = True
        self.st = dict(on_ground=True, gs=0.0, ias=0.0, vs=0.0, agl=0.0, elev=80.0, lat=51.4700, lon=-0.4543,
                       hdg=270.0, brake=True, engines=(False, False), tdv=95.0, slew=False, aircraft=None)
        self.events: list[RecorderEvent] = []
        self.hints: list[bool] = []
        self.steps = 0

    # ------------------------------------------------------------------ driving
    @property
    def dt(self) -> float:
        if self.adaptive and self.rec.sampling_hint.high_rate:
            return 1.0 / self.high_hz
        return 1.0

    def step(self, dt: float | None = None, **changes) -> list[RecorderEvent]:
        self.st.update(changes)
        self.steps += 1
        if self.steps > 400_000:
            raise RuntimeError("tape ran away")
        dt = self.dt if dt is None else dt
        self.host += timedelta(seconds=dt)
        advance = 0.0 if (self.paused and not self.sim_runs_in_pause) else dt * self.rate
        self.sim += advance
        st = self.st
        if not (self.paused and not self.sim_runs_in_pause) and not st["slew"]:
            dnm = st["gs"] * advance / 3600
            st["lat"] += dnm * math.cos(math.radians(st["hdg"])) / 60
            st["lon"] += dnm * math.sin(math.radians(st["hdg"])) / (60 * math.cos(math.radians(st["lat"])))
            if not st["on_ground"]:
                st["agl"] = max(0.0, st["agl"] + st["vs"] * advance / 60)
        if not self.send:
            return []
        sample = Sample(
            host_time=self.host,
            sim_time_s=self.sim if self.with_sim_time else None,
            sim_utc=(T0 + timedelta(seconds=self.sim)) if self.with_sim_time else None,
            sim_rate=self.rate,
            on_ground=st["on_ground"], ground_speed_kt=st["gs"], ias_kt=st["ias"], vs_fpm=st["vs"],
            alt_agl_ft=0.0 if st["on_ground"] else st["agl"], alt_msl_ft=st["elev"] + (0 if st["on_ground"] else st["agl"]),
            lat=st["lat"], lon=st["lon"], heading_deg=st["hdg"], parking_brake=st["brake"],
            engines_running=st["engines"], touchdown_velocity_fpm=st["tdv"] if self.with_tdv else None,
            slew_active=st["slew"], aircraft=st["aircraft"], simulated=True, session_id="S1",
        )
        out = self.rec.feed(sample)
        self.events.extend(out)
        self.hints.append(self.rec.sampling_hint.high_rate)
        return out

    def run(self, seconds: float, **changes) -> None:
        """Hold a state for ``seconds`` of host time."""
        end = self.host + timedelta(seconds=seconds)
        self.st.update(changes)
        while self.host < end - timedelta(microseconds=1):
            self.step(min(self.dt, (end - self.host).total_seconds()))

    def life(self, kind: LifecycleKind, value=None, detail: str = "") -> list[RecorderEvent]:
        out = self.rec.on_lifecycle(Lifecycle(kind, self.host, value, detail))
        self.events.extend(out)
        return out

    def wait(self, seconds: float) -> None:
        """Host time passes with no samples (sim clock follows unless paused)."""
        self.host += timedelta(seconds=seconds)
        if not self.paused or self.sim_runs_in_pause:
            self.sim += seconds * self.rate

    # ------------------------------------------------------------------ scripts
    def preflight(self, seconds: float = 10) -> None:
        self.run(seconds)
        self.run(20, engines=(True, True))

    def taxi_out(self) -> None:
        self.run(30, brake=False, gs=4.0)           # pushback
        self.run(5, gs=0.0)
        self.run(120, gs=15.0)                       # taxi
        self.run(10, gs=0.0)                         # line up

    def takeoff(self, vr: float = 140.0) -> None:
        speed = 0.0
        while speed < vr:
            speed = min(vr, speed + 5.0)
            self.step(1.0, gs=speed, ias=speed)
        self.run(1, on_ground=False, vs=1500.0, agl=5.0)

    def climb_to(self, agl: float, vs: float = 1500.0, gs: float = 180.0) -> None:
        self.st.update(vs=vs, gs=gs, ias=gs)
        while self.st["agl"] < agl:
            self.step()
        self.st["vs"] = 0.0

    def cruise(self, seconds: float, gs: float = 250.0) -> None:
        self.run(seconds, gs=gs, ias=gs, vs=0.0)

    def descend_to(self, agl: float, vs: float = -1500.0, gs: float = 160.0) -> None:
        self.st.update(vs=vs, gs=gs, ias=gs)
        while self.st["agl"] + vs * self.dt * self.rate / 60 > agl:
            self.step()
        self.st["vs"] = 0.0

    def land(self, rate_fpm: float = 180.0, *, tdv: float | None = None, gs: float = 130.0) -> None:
        """Descend from wherever the tape is to ground contact; the touchdown SimVar latches ``tdv``."""
        self.st.update(vs=-rate_fpm, gs=gs, ias=gs)
        while self.st["agl"] - rate_fpm * self.dt * self.rate / 60 > 0:
            self.step()
        self.step(on_ground=True, agl=0.0, vs=-rate_fpm * 0.2, tdv=rate_fpm if tdv is None else tdv)

    def rollout(self, to: float = 20.0) -> None:
        speed = self.st["gs"]
        while speed > to:
            speed = max(to, speed - 4.0)
            self.step(None, gs=speed, ias=speed, vs=0.0)
        self.run(5, gs=to)

    def taxi_in_and_park(self) -> None:
        self.run(60, gs=15.0)
        self.run(5, gs=0.0, brake=True)
        self.run(30, engines=(False, False))

    def full_flight(self, cruise_s: float = 600, rate_fpm: float = 180.0) -> None:
        self.preflight()
        self.taxi_out()
        self.takeoff()
        self.climb_to(3000)
        self.cruise(cruise_s)
        self.descend_to(1200)
        self.land(rate_fpm)
        self.rollout()
        self.taxi_in_and_park()

    # ------------------------------------------------------------------ assertions
    def kinds(self) -> list[EventKind]:
        return [e.kind for e in self.events]

    def of(self, kind: EventKind) -> list[RecorderEvent]:
        return [e for e in self.events if e.kind is kind]

    def phases(self) -> list[str]:
        return [e.data["to"] for e in self.events if e.kind is EventKind.PHASE_CHANGED]


class NormalFlightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tape = Tape()
        self.tape.full_flight()
        self.summary = self.tape.rec.completed_flights[-1]

    def test_phase_sequence(self) -> None:
        self.assertEqual(self.tape.phases(), ["preflight", "taxi_out", "takeoff_roll", "airborne",
                                              "landing_roll", "taxi_in", "complete", "preflight"])

    def test_key_events_once_each(self) -> None:
        for kind in (EventKind.FLIGHT_STARTED, EventKind.BLOCK_OFF, EventKind.TAKEOFF, EventKind.TOUCHDOWN,
                     EventKind.BLOCK_ON, EventKind.FLIGHT_ENDED):
            expected = 2 if kind is EventKind.FLIGHT_STARTED else 1  # next flight arms after block-on
            self.assertEqual(len(self.tape.of(kind)), expected, kind)
        self.assertEqual(self.tape.of(EventKind.FLIGHT_ENDED)[0].data["outcome"], "complete")
        self.assertNotIn(EventKind.GAP, self.tape.kinds())
        self.assertNotIn(EventKind.POSITION_JUMP, self.tape.kinds())
        self.assertNotIn(EventKind.BOUNCE, self.tape.kinds())

    def test_touchdown_rate_from_simvar(self) -> None:
        td = self.tape.of(EventKind.TOUCHDOWN)[0]
        self.assertEqual(td.data["rate_fpm"], -180.0)
        self.assertEqual(td.data["rate_source"], "touchdown_simvar")
        self.assertEqual(td.phase, Phase.LANDING_ROLL)
        self.assertEqual(self.summary.landings, 1)
        self.assertEqual(self.summary.touchdowns[0]["kind"], "full_stop")

    def test_block_off_is_start_of_movement(self) -> None:
        off = self.tape.of(EventKind.BLOCK_OFF)[0]
        start = self.tape.of(EventKind.FLIGHT_STARTED)[0]
        # preflight 10 s + engines 20 s, first moving sample one second later
        self.assertAlmostEqual(off.sim_time_s - start.sim_time_s, 30.0, delta=1.01)
        self.assertEqual(self.summary.block_off["basis"], "moving with parking brake released")

    def test_clocks_on_simulator_time(self) -> None:
        s = self.summary
        self.assertEqual(s.block_source, "simulator")
        self.assertEqual(s.airborne_source, "simulator")
        self.assertGreater(s.block_s, s.airborne_s)
        self.assertGreater(s.airborne_s, 600)
        on = self.tape.of(EventKind.BLOCK_ON)[0]
        off = self.tape.of(EventKind.BLOCK_OFF)[0]
        self.assertAlmostEqual(s.block_s, on.sim_time_s - off.sim_time_s, delta=0.01)
        to = self.tape.of(EventKind.TAKEOFF)[0]
        td = self.tape.of(EventKind.TOUCHDOWN)[0]
        self.assertAlmostEqual(s.airborne_s, td.sim_time_s - to.sim_time_s, delta=0.01)
        self.assertAlmostEqual(s.block_host_s, s.block_s, delta=0.01)  # 1x rate, no pause
        self.assertEqual(s.paused_host_s, 0.0)
        self.assertIsNone(s.block_note)

    def test_distance(self) -> None:
        # 600 s at 250 kt alone is ~41.7 nm
        self.assertGreater(self.summary.distance_nm, 45)
        self.assertLess(self.summary.distance_nm, 80)
        self.assertEqual(self.summary.distance_excluded_nm, 0.0)

    def test_adaptive_sampling(self) -> None:
        self.assertTrue(any(self.tape.hints))
        # high rate while rolling and near the ground; low again in cruise and after parking
        self.assertFalse(self.tape.hints[-1])
        cruise_index = next(i for i, e in enumerate(self.tape.events) if e.kind is EventKind.TAKEOFF)
        self.assertIn(True, self.tape.hints)
        self.assertGreater(len(self.tape.hints), cruise_index)

    def test_events_are_json_safe_and_round_trip(self) -> None:
        for event in self.tape.events:
            text = json.dumps(event.to_json())
            back = RecorderEvent.from_json(json.loads(text))
            self.assertEqual(back.kind, event.kind)
            self.assertEqual(back.sim_time_s, event.sim_time_s)
        json.dumps(self.summary.to_json())

    def test_deterministic(self) -> None:
        again = Tape()
        again.full_flight()
        self.assertEqual([e.to_json() for e in again.events], [e.to_json() for e in self.tape.events])

    def test_next_flight_armed_but_not_loggable(self) -> None:
        self.assertEqual(self.tape.rec.phase, Phase.PREFLIGHT)
        self.assertFalse(self.tape.rec.summary().loggable)
        self.assertNotEqual(self.tape.rec.flight_id, self.summary.flight_id)


class LandingVariantsTest(unittest.TestCase):
    def airborne_tape(self, **kw) -> Tape:
        tape = Tape(**kw)
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(2500)
        tape.cruise(60)
        tape.descend_to(1200)
        return tape

    def test_bounce(self) -> None:
        tape = self.airborne_tape()
        tape.land(420.0)
        self.assertTrue(tape.rec.sampling_hint.high_rate)
        for _ in range(15):  # 0.5 s back in the air at 30 Hz
            tape.step(on_ground=False, agl=3.0, vs=120.0, gs=125.0)
        tape.step(on_ground=True, agl=0.0, vs=-40.0, tdv=150.0)
        tape.rollout()
        tape.taxi_in_and_park()
        s = tape.rec.completed_flights[-1]
        self.assertEqual(s.landings, 1)
        self.assertEqual(s.bounces, 1)
        self.assertEqual(len(tape.of(EventKind.TOUCHDOWN)), 1)
        bounce = tape.of(EventKind.BOUNCE)[0]
        self.assertEqual(bounce.data["rate_fpm"], -150.0)
        self.assertEqual(bounce.data["rate_source"], "touchdown_simvar")
        self.assertEqual(s.touchdowns[0]["rate_fpm"], -420.0)
        self.assertEqual(len(s.touchdowns[0]["bounces"]), 1)
        self.assertEqual(len(tape.of(EventKind.TAKEOFF)), 1)

    def test_touch_and_go_circuit(self) -> None:
        tape = self.airborne_tape()
        tape.land(200.0)
        tape.run(4, gs=110.0, ias=110.0, vs=0.0)
        tape.step(on_ground=False, agl=5.0, vs=1200.0, gs=120.0)
        tape.climb_to(1500, vs=1200.0)
        tape.cruise(120, gs=110)
        tape.descend_to(1200)
        tape.land(150.0)
        tape.rollout()
        tape.taxi_in_and_park()
        s = tape.rec.completed_flights[-1]
        self.assertEqual(s.outcome, "complete")
        self.assertEqual(s.landings, 2)
        self.assertEqual(s.touch_and_goes, 1)
        self.assertEqual([t["kind"] for t in s.touchdowns], ["touch_and_go", "full_stop"])
        self.assertEqual([t["touch_and_go"] for t in s.takeoffs], [False, True])
        self.assertEqual([t.data["rate_fpm"] for t in tape.of(EventKind.TOUCHDOWN)], [-200.0, -150.0])
        self.assertEqual(s.bounces, 0)
        self.assertEqual(s.go_arounds, ())
        # airborne time excludes the 4 s on the runway
        td = tape.of(EventKind.TOUCHDOWN)
        to = tape.of(EventKind.TAKEOFF)
        expected = (td[0].sim_time_s - to[0].sim_time_s) + (td[1].sim_time_s - to[1].sim_time_s)
        self.assertAlmostEqual(s.airborne_s, expected, delta=0.01)

    def test_go_around(self) -> None:
        tape = self.airborne_tape()
        tape.descend_to(200, vs=-700.0)
        tape.climb_to(1600, vs=1500.0)
        self.assertEqual(len(tape.rec.summary().go_arounds), 1)
        self.assertEqual(tape.rec.summary().landings, 0)
        tape.cruise(120, gs=150)
        tape.descend_to(1200)
        tape.land(160.0)
        tape.rollout()
        tape.taxi_in_and_park()
        s = tape.rec.completed_flights[-1]
        self.assertEqual(len(s.go_arounds), 1)
        self.assertLess(s.go_arounds[0]["min_agl_ft"], 250)
        self.assertEqual(s.landings, 1)
        self.assertEqual(len(tape.of(EventKind.TOUCHDOWN)), 1)
        self.assertEqual(s.touch_and_goes, 0)

    def test_no_go_around_counted_after_takeoff(self) -> None:
        tape = self.airborne_tape()
        self.assertEqual(tape.rec.summary().go_arounds, ())

    def test_touchdown_rate_from_high_rate_vs_when_no_simvar(self) -> None:
        tape = self.airborne_tape(with_tdv=False)
        tape.land(240.0)
        self.assertEqual(tape.of(EventKind.TOUCHDOWN), [])  # still waiting for the SimVar
        tape.run(1.5, vs=0.0)
        td = tape.of(EventKind.TOUCHDOWN)[0]
        self.assertEqual(td.data["rate_source"], "vertical_speed_high_rate")
        self.assertEqual(td.data["rate_fpm"], -240.0)

    def test_touchdown_rate_never_from_1hz_vertical_speed(self) -> None:
        tape = self.airborne_tape(with_tdv=False, adaptive=False)
        tape.land(240.0)
        tape.rollout()
        td = tape.of(EventKind.TOUCHDOWN)[0]
        self.assertIsNone(td.data["rate_fpm"])
        self.assertEqual(td.data["rate_source"], "unavailable")
        self.assertIn("high-rate", td.data["rate_note"])

    def test_touchdown_simvar_works_at_1hz(self) -> None:
        tape = self.airborne_tape(adaptive=False)
        tape.land(310.0)
        tape.rollout()
        td = tape.of(EventKind.TOUCHDOWN)[0]
        self.assertEqual(td.data["rate_source"], "touchdown_simvar")
        self.assertEqual(td.data["rate_fpm"], -310.0)

    def test_touchdown_simvar_arriving_a_frame_late(self) -> None:
        tape = self.airborne_tape()
        tape.land(200.0, tdv=95.0)  # simvar still holds the old value at contact
        self.assertEqual(tape.of(EventKind.TOUCHDOWN), [])
        tape.step(tdv=260.0)
        td = tape.of(EventKind.TOUCHDOWN)[0]
        self.assertEqual(td.data["rate_fpm"], -260.0)
        self.assertEqual(td.data["rate_source"], "touchdown_simvar")
        # event is stamped with the contact sample, not the later one
        contact = [e for e in tape.events if e.kind is EventKind.PHASE_CHANGED and e.data["to"] == "landing_roll"][0]
        self.assertEqual(td.sim_time_s, contact.sim_time_s)


class PauseAndRateTest(unittest.TestCase):
    def climbing(self, **kw) -> Tape:
        tape = Tape(**kw)
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(5000)
        return tape

    def finish(self, tape: Tape) -> None:
        tape.descend_to(1200)
        tape.land(180)
        tape.rollout()
        tape.taxi_in_and_park()

    def test_full_pause_mid_air(self) -> None:
        tape = self.climbing()
        tape.cruise(100)
        before = tape.rec.summary().airborne_s
        tape.paused = True
        tape.step()
        tape.life(LifecycleKind.PAUSE_FLAGS, 1)
        tape.run(300)
        tape.life(LifecycleKind.PAUSE_FLAGS, 0)
        tape.paused = False
        tape.cruise(100)
        s = tape.rec.summary()
        self.assertAlmostEqual(s.airborne_s - before, 100.0, delta=1.1)  # paused 300 s not credited
        self.assertAlmostEqual(s.paused_host_s, 300.0, delta=1.1)
        self.assertEqual(len(tape.of(EventKind.PAUSED)), 1)
        self.assertEqual(len(tape.of(EventKind.RESUMED)), 1)
        self.assertTrue(tape.of(EventKind.PAUSED)[0].data["full"])
        self.assertNotIn(EventKind.GAP, tape.kinds())
        self.finish(tape)
        self.assertEqual(tape.rec.completed_flights[-1].pauses[0]["host_s"], s.pauses[0]["host_s"])

    def test_pause_without_samples_is_not_a_gap(self) -> None:
        tape = self.climbing()
        tape.cruise(30)
        before = tape.rec.summary().airborne_s
        tape.paused = True
        tape.life(LifecycleKind.PAUSE_FLAGS, 1)
        tape.wait(600)
        tape.life(LifecycleKind.PAUSE_FLAGS, 0)
        tape.paused = False
        tape.cruise(10)
        s = tape.rec.summary()
        self.assertNotIn(EventKind.GAP, tape.kinds())
        self.assertAlmostEqual(s.airborne_s - before, 10.0, delta=1.1)
        self.assertAlmostEqual(s.paused_host_s, 600.0, delta=0.01)

    def test_active_pause_not_credited_by_default(self) -> None:
        tape = self.climbing()
        tape.cruise(30)
        before = tape.rec.summary().airborne_s
        tape.paused = True
        tape.sim_runs_in_pause = True  # worst case: SIMULATION TIME keeps running in Active Pause
        tape.life(LifecycleKind.PAUSE_FLAGS, 4)
        tape.run(120, gs=0.0)
        tape.life(LifecycleKind.PAUSE_FLAGS, 0)
        tape.paused = False
        tape.cruise(30)
        s = tape.rec.summary()
        self.assertAlmostEqual(s.airborne_s - before, 30.0, delta=1.1)
        self.assertAlmostEqual(s.active_paused_host_s, 120.0, delta=1.1)
        paused = tape.of(EventKind.PAUSED)[0]
        self.assertTrue(paused.data["active"])
        self.assertTrue(paused.data["clocks_stopped"])

    def test_active_pause_credited_when_configured(self) -> None:
        tape = self.climbing(rules=RecorderRules(pause_policy=PausePolicy.FULL_AND_SIM_ONLY))
        tape.cruise(30)
        before = tape.rec.summary().airborne_s
        tape.paused = True
        tape.sim_runs_in_pause = True
        tape.life(LifecycleKind.PAUSE_FLAGS, 4)
        tape.run(120, gs=0.0)
        tape.life(LifecycleKind.PAUSE_FLAGS, 0)
        tape.paused = False
        tape.cruise(30)
        s = tape.rec.summary()
        self.assertAlmostEqual(s.airborne_s - before, 150.0, delta=1.1)
        self.assertFalse(tape.of(EventKind.PAUSED)[0].data["clocks_stopped"])

    def test_four_times_sim_rate(self) -> None:
        tape = self.climbing()
        tape.cruise(10)
        before = tape.rec.summary()
        tape.rate = 4.0
        tape.cruise(300)
        tape.rate = 1.0
        tape.cruise(10)
        s = tape.rec.summary()
        self.assertAlmostEqual(s.airborne_s - before.airborne_s, 1210.0, delta=4.1)
        self.assertAlmostEqual(s.airborne_host_s - before.airborne_host_s, 310.0, delta=1.1)
        self.assertEqual(s.max_sim_rate, 4.0)
        self.assertAlmostEqual(s.time_at_rate_s["4"], 1200.0, delta=4.1)
        rates = [(e.data["from_rate"], e.data["to_rate"]) for e in tape.of(EventKind.SIM_RATE_CHANGED)]
        self.assertEqual(rates, [(1.0, 4.0), (4.0, 1.0)])
        self.assertEqual(s.airborne_source, "simulator")
        self.assertNotIn(EventKind.POSITION_JUMP, tape.kinds())  # 4x movement is not a teleport
        self.assertNotIn(EventKind.GAP, tape.kinds())

    def test_host_fallback_is_rate_aware_and_labelled(self) -> None:
        tape = Tape(rate=4.0, with_sim_time=False)
        tape.full_flight(cruise_s=150)
        s = tape.rec.completed_flights[-1]
        self.assertEqual(s.block_source, "host_fallback")
        self.assertEqual(s.airborne_source, "host_fallback")
        self.assertAlmostEqual(s.block_s, 4 * s.block_host_s, delta=0.5)
        self.assertIsNone(tape.of(EventKind.TAKEOFF)[0].sim_time_s)


class InterruptionTest(unittest.TestCase):
    def cruising(self, **kw) -> Tape:
        tape = Tape(**kw)
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(3000)
        tape.cruise(60)
        return tape

    def test_short_disconnect_is_credited_when_sim_time_continuous(self) -> None:
        tape = self.cruising()
        before = tape.rec.summary()
        tape.life(LifecycleKind.DISCONNECTED, "disconnected", "MSFS closed the SimConnect link")
        tape.send = False
        tape.run(30)
        tape.send = True
        tape.life(LifecycleKind.CONNECTED, "connected")
        tape.cruise(10)
        s = tape.rec.summary()
        gaps = tape.of(EventKind.GAP)
        self.assertEqual([g.data["status"] for g in gaps], ["started", "ended"])
        self.assertEqual(gaps[0].data["reason"], "MSFS closed the SimConnect link")
        self.assertEqual(len(s.gaps), 1)
        self.assertTrue(s.gaps[0]["sim_time_continuous"])
        self.assertAlmostEqual(s.gaps[0]["credited_s"], 31.0, delta=1.01)
        self.assertAlmostEqual(s.airborne_s - before.airborne_s, 41.0, delta=1.1)
        self.assertEqual(tape.rec.phase, Phase.AIRBORNE)
        self.assertGreater(s.distance_bridged_nm, 1.5)

    def test_long_disconnect_mid_flight_not_credited(self) -> None:
        tape = self.cruising()
        before = tape.rec.summary()
        tape.life(LifecycleKind.DISCONNECTED, "disconnected", "simulator disconnected")
        tape.send = False
        tape.run(300)
        tape.send = True
        tape.cruise(10)
        tape.descend_to(1200)
        tape.land(200)
        tape.rollout()
        tape.taxi_in_and_park()
        s = tape.rec.completed_flights[-1]
        self.assertEqual(s.outcome, "complete")
        self.assertEqual(len(s.gaps), 1)
        self.assertEqual(s.gaps[0]["credited_s"], 0.0)
        self.assertAlmostEqual(s.gaps[0]["uncredited_s"], 301.0, delta=1.01)
        self.assertAlmostEqual(s.uncredited_gap_s, 301.0, delta=1.01)
        self.assertEqual(s.landings, 1)
        self.assertGreater(s.airborne_s, before.airborne_s)

    def test_silent_feed_detected_as_gap(self) -> None:
        tape = self.cruising()
        tape.send = False
        tape.run(20)
        tape.send = True
        tape.cruise(5)
        gaps = tape.of(EventKind.GAP)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].data["status"], "detected")
        self.assertIn("no telemetry", gaps[0].data["reason"])

    def test_disconnect_then_simulator_restart(self) -> None:
        tape = self.cruising()
        tape.life(LifecycleKind.DISCONNECTED, "disconnected", "simulator disconnected")
        tape.send = False
        tape.run(120)
        tape.send = True
        tape.sim = 10.0  # MSFS restarted: SIMULATION TIME starts over
        tape.st.update(lat=40.0, lon=-74.0, on_ground=True, gs=0.0, agl=0.0)
        tape.step()
        s = tape.rec.summary()
        self.assertEqual(s.gaps[0]["credited_s"], 0.0)
        self.assertFalse(s.gaps[0]["sim_time_continuous"])
        self.assertEqual(len(tape.of(EventKind.POSITION_JUMP)), 1)
        self.assertEqual(tape.of(EventKind.TOUCHDOWN), [])
        self.assertEqual(tape.rec.phase, Phase.TAXI_IN)
        self.assertTrue(any("no landing" in w for w in s.warnings))

    def test_core_gap_blocks_vs_based_touchdown_rate(self) -> None:
        tape = self.cruising(with_tdv=False)
        tape.descend_to(1200)
        tape.st.update(vs=-200.0, gs=130.0)
        while tape.st["agl"] > 8:
            tape.step()
        tape.life(LifecycleKind.GAP, 12, "queue overflow")
        tape.step(on_ground=True, agl=0.0)
        tape.rollout()
        td = tape.of(EventKind.TOUCHDOWN)[0]
        self.assertEqual(td.data["rate_source"], "unavailable")
        self.assertTrue(td.data["during_gap"])
        self.assertEqual(tape.rec.summary().gaps[0]["dropped_updates"], 12)

    def test_crash(self) -> None:
        tape = self.cruising()
        tape.life(LifecycleKind.CRASHED, 1)
        ended = tape.of(EventKind.FLIGHT_ENDED)[0]
        self.assertEqual(ended.data["outcome"], "abandoned")
        self.assertEqual(ended.data["reason"], "aircraft crashed")
        self.assertTrue(ended.data["loggable"])
        self.assertEqual(tape.rec.phase, Phase.ABANDONED)
        self.assertIn(EventKind.CRASHED, tape.kinds())
        # no automatic re-arm while the crash scene plays
        tape.run(10, on_ground=True, gs=0.0)
        self.assertFalse(tape.rec.recording)
        self.assertEqual(tape.rec.suspended_reason, "crash")
        tape.life(LifecycleKind.CRASH_RESET, 1)
        tape.run(2)
        self.assertTrue(tape.rec.recording)
        self.assertEqual(tape.rec.phase, Phase.PREFLIGHT)
        s = tape.rec.completed_flights[-1]
        self.assertIsNone(s.block_on)
        self.assertEqual(s.block_note, "Block-on was not observed.")
        self.assertEqual(s.touchdowns, ())

    def test_slew_jump_in_flight(self) -> None:
        tape = self.cruising()
        before = tape.rec.summary().distance_nm
        tape.step(slew=True)
        for _ in range(10):
            tape.st["lat"] += 0.05
            tape.step()
        tape.step(slew=False)
        tape.cruise(10)
        s = tape.rec.summary()
        jumps = tape.of(EventKind.POSITION_JUMP)
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0].data["reason"], "slew mode")
        self.assertGreater(s.distance_excluded_nm, 25)
        self.assertLess(s.distance_nm - before, 2 * 250 * 13 / 3600 + 0.5)
        self.assertEqual(tape.rec.phase, Phase.AIRBORNE)

    def test_teleport_reported_by_simulator(self) -> None:
        tape = Tape()
        tape.preflight()
        tape.life(LifecycleKind.POSITION_CHANGED, 1)
        tape.st.update(lat=48.0, lon=2.0)
        tape.step()
        jumps = tape.of(EventKind.POSITION_JUMP)
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0].data["reason"], "simulator reported a position change")
        self.assertEqual(tape.rec.phase, Phase.PREFLIGHT)

    def test_implausible_jump_without_any_flag(self) -> None:
        tape = self.cruising()
        tape.st["lat"] += 2.0
        tape.step()
        jumps = tape.of(EventKind.POSITION_JUMP)
        self.assertEqual(jumps[0].data["reason"], "implausible movement between samples")

    def test_aircraft_change_at_gate(self) -> None:
        tape = Tape()
        tape.life(LifecycleKind.AIRCRAFT_LOADED, "SimObjects/Airplanes/C172/aircraft.cfg")
        tape.full_flight(cruise_s=120)
        first = tape.rec.completed_flights[-1]
        self.assertEqual(first.outcome, "complete")
        self.assertEqual(first.aircraft, "SimObjects/Airplanes/C172/aircraft.cfg")
        tape.life(LifecycleKind.AIRCRAFT_LOADED, "SimObjects/Airplanes/A320/aircraft.cfg")
        changed = tape.of(EventKind.AIRCRAFT_CHANGED)
        self.assertEqual(len(changed), 1)
        ended = tape.of(EventKind.FLIGHT_ENDED)[-1]
        self.assertEqual(ended.data["outcome"], "discarded")
        self.assertFalse(ended.data["loggable"])
        tape.run(3)
        self.assertEqual(tape.rec.summary().aircraft, "SimObjects/Airplanes/A320/aircraft.cfg")
        self.assertEqual(tape.rec.phase, Phase.PREFLIGHT)
        # the completed flight is untouched
        self.assertEqual(tape.rec.completed_flights[0].outcome, "complete")

    def test_aircraft_change_during_taxi_abandons(self) -> None:
        tape = Tape()
        tape.life(LifecycleKind.AIRCRAFT_LOADED, "C172")
        tape.preflight()
        tape.taxi_out()
        tape.life(LifecycleKind.AIRCRAFT_LOADED, "C172")  # repeat of the same aircraft is ignored
        self.assertEqual(tape.of(EventKind.AIRCRAFT_CHANGED), [])
        tape.life(LifecycleKind.AIRCRAFT_LOADED, "A320")
        self.assertEqual(tape.of(EventKind.FLIGHT_ENDED)[-1].data["outcome"], "abandoned")

    def test_sim_stop_after_landing_keeps_the_landing(self) -> None:
        tape = self.cruising()
        tape.descend_to(1200)
        tape.land(150)
        tape.rollout()
        tape.run(20, gs=15)
        tape.life(LifecycleKind.SIM_STOPPED, 1)
        s = tape.rec.completed_flights[-1]
        self.assertEqual(s.outcome, "complete")
        self.assertIsNone(s.block_on)
        self.assertEqual(s.block_note, "Block-on was not observed.")
        self.assertEqual(s.landings, 1)

    def test_flight_loaded_mid_air_abandons(self) -> None:
        tape = self.cruising()
        tape.life(LifecycleKind.FLIGHT_LOADED, "flights/new.flt")
        self.assertEqual(tape.rec.completed_flights[-1].outcome, "abandoned")


class GroundOperationsTest(unittest.TestCase):
    def test_rejected_takeoff_and_return_to_stand(self) -> None:
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        for speed in range(5, 85, 5):
            tape.step(1.0, gs=float(speed), ias=float(speed))
        self.assertEqual(tape.rec.phase, Phase.TAKEOFF_ROLL)
        for speed in range(80, 0, -8):
            tape.step(None, gs=float(speed), ias=float(speed))
        self.assertEqual(tape.rec.phase, Phase.TAXI_OUT)
        tape.run(60, gs=15.0)
        tape.run(5, gs=0.0, brake=True)
        tape.run(10, engines=(False, False))
        s = tape.rec.completed_flights[-1]
        self.assertEqual(s.outcome, "complete")
        self.assertEqual(len(s.rejected_takeoffs), 1)
        self.assertEqual(s.takeoffs, ())
        self.assertEqual(s.landings, 0)
        self.assertIsNone(s.airborne_s)
        self.assertIsNotNone(s.block_s)
        self.assertTrue(any("without taking off" in w for w in s.warnings))
        reasons = [e.data["reason"] for e in tape.of(EventKind.PHASE_CHANGED)]
        self.assertIn("rejected takeoff", reasons)

    def test_tug_pushback_with_engines_off_is_not_block_on(self) -> None:
        tape = Tape()
        tape.run(10)
        tape.run(40, brake=False, gs=3.5)        # tug push, engines off
        tape.run(30, gs=0.0, brake=True)          # push complete, brake set, engines still off
        self.assertEqual(tape.rec.phase, Phase.TAXI_OUT)
        self.assertEqual(tape.of(EventKind.BLOCK_ON), [])

    def test_holding_with_parking_brake_after_landing_is_not_block_on(self) -> None:
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(1500)
        tape.descend_to(1200)
        tape.land(150)
        tape.rollout()
        tape.run(120, gs=0.0, brake=True)  # engines still running
        self.assertEqual(tape.rec.phase, Phase.TAXI_IN)
        tape.run(10, engines=(False, False))
        self.assertEqual(tape.rec.completed_flights[-1].outcome, "complete")

    def test_multiple_sectors(self) -> None:
        tape = Tape()
        tape.full_flight(cruise_s=120)
        tape.full_flight(cruise_s=120)
        flights = tape.rec.completed_flights
        self.assertEqual([f.outcome for f in flights], ["complete", "complete"])
        self.assertNotEqual(flights[0].flight_id, flights[1].flight_id)
        self.assertEqual([f.landings for f in flights], [1, 1])
        self.assertGreater(flights[1].block_off["sim_time_s"], flights[0].block_on["sim_time_s"])

    def test_stop_and_go_circuit_same_flight(self) -> None:
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(1500)
        tape.descend_to(1200)
        tape.land(170)
        tape.rollout()
        tape.run(20, gs=12)
        self.assertEqual(tape.rec.phase, Phase.TAXI_IN)
        tape.takeoff()
        tape.climb_to(1500)
        tape.descend_to(1200)
        tape.land(140)
        tape.rollout()
        tape.taxi_in_and_park()
        s = tape.rec.completed_flights[-1]
        self.assertEqual(len(s.takeoffs), 2)
        self.assertEqual(s.landings, 2)
        self.assertEqual([t["kind"] for t in s.touchdowns], ["full_stop", "full_stop"])


class StartAndManualTest(unittest.TestCase):
    def test_started_in_the_air(self) -> None:
        tape = Tape()
        tape.st.update(on_ground=False, agl=2000.0, gs=150.0, ias=150.0, brake=False, engines=(True, True))
        tape.cruise(60, gs=150)
        tape.descend_to(1200)
        tape.land(220)
        tape.rollout()
        tape.taxi_in_and_park()
        s = tape.rec.completed_flights[-1]
        self.assertTrue(s.started_in_air)
        self.assertIsNone(s.block_off)
        self.assertIsNone(s.block_s)
        self.assertEqual(s.block_source, "unavailable")
        self.assertEqual(s.block_note, "Block-off was not observed.")
        self.assertIn("Started in the air", s.airborne_note)
        self.assertEqual(s.landings, 1)
        self.assertEqual(s.outcome, "complete")

    def test_auto_start_off_and_manual_start_stop(self) -> None:
        tape = Tape(RecorderRules(auto_start=False))
        tape.preflight()
        self.assertFalse(tape.rec.recording)
        events = tape.rec.manual_start(tape.host, "pilot pressed Start")
        tape.events.extend(events)
        self.assertEqual([e.kind for e in events][:2], [EventKind.MANUAL_START, EventKind.FLIGHT_STARTED])
        self.assertEqual(tape.rec.summary().trigger, "manual")
        self.assertEqual(tape.rec.manual_start(tape.host), [])  # already recording
        tape.taxi_out()
        tape.takeoff()
        tape.climb_to(1500)
        stop = tape.rec.manual_stop(tape.host, "pilot confirmed Stop")
        self.assertEqual(stop[0].kind, EventKind.MANUAL_STOP)
        ended = [e for e in stop if e.kind is EventKind.FLIGHT_ENDED][0]
        self.assertEqual(ended.data["outcome"], "abandoned")
        self.assertEqual(ended.data["reason"], "stopped by pilot")
        self.assertEqual(tape.rec.manual_stop(tape.host), [])

    def test_manual_stop_holds_automatic_logging(self) -> None:
        tape = Tape()
        tape.preflight()
        tape.taxi_out()
        tape.events.extend(tape.rec.manual_stop(tape.host))
        tape.run(30)
        self.assertFalse(tape.rec.recording)
        self.assertEqual(tape.rec.suspended_reason, "manual stop")
        tape.rec.resume_automatic()
        tape.run(2)
        self.assertTrue(tape.rec.recording)

    def test_manual_start_with_no_recent_sample_waits_for_data(self) -> None:
        tape = Tape(RecorderRules(auto_start=False))
        events = tape.rec.manual_start(T0, "before connection")
        self.assertEqual(events[1].kind, EventKind.FLIGHT_STARTED)
        tape.st.update(on_ground=False, agl=3000.0, gs=200.0)
        tape.cruise(5, gs=200)
        self.assertEqual(tape.rec.phase, Phase.AIRBORNE)
        self.assertTrue(tape.rec.summary().started_in_air)

    def test_start_in_air_disabled(self) -> None:
        tape = Tape(RecorderRules(start_in_air=False))
        tape.st.update(on_ground=False, agl=3000.0, gs=200.0)
        tape.cruise(5, gs=200)
        self.assertFalse(tape.rec.recording)

    def test_pause_before_flight_start_counts_only_inside_flight(self) -> None:
        tape = Tape(RecorderRules(auto_start=False))
        tape.run(5)
        tape.paused = True
        tape.life(LifecycleKind.PAUSE_FLAGS, 1)
        tape.run(100)
        tape.events.extend(tape.rec.manual_start(tape.host))
        tape.run(20)
        tape.life(LifecycleKind.PAUSE_FLAGS, 0)
        self.assertAlmostEqual(tape.rec.summary().paused_host_s, 20.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
