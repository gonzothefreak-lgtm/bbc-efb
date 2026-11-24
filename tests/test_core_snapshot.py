"""Grouped snapshots through Core, and a SimulatedProvider realistic enough for Recorder work."""

from datetime import datetime, timezone
from unittest import TestCase

from bbc_core import Core, CoreRequestError, Observation, Quality, SimVar, ValueType
from bbc_core.contract import Gap, PauseState, SignalError, Snapshot, SnapshotPeriod, simulator_utc
from bbc_core.provider import Reading, SnapshotFrame
from bbc_core.simulated import SimulatedProvider

from test_core_support import FakeClock, drive

VS = SimVar("VERTICAL SPEED", "feet per minute", ValueType.NUMBER)
ON_GROUND = SimVar("SIM ON GROUND", "bool", ValueType.BOOLEAN)
NOSE_GEAR = SimVar("GEAR IS ON GROUND", "bool", ValueType.BOOLEAN, index=0)
TAIL = SimVar("ATC ID", None, ValueType.TEXT)
UNSCRIPTED = SimVar("PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", ValueType.NUMBER)


class SnapshotTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, wall_clock=self.clock.wall,
                                          absolute_time_s=63_900_000_000.0)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.recorder = self.core.register_consumer(queue_size=4096)
        for variable, value in ((VS, -120.0), (ON_GROUND, True), (NOSE_GEAR, 1), (TAIL, "VH-BBC")):
            self.provider.set_value(variable, value)

    def test_one_delivery_holds_every_field_and_the_simulator_clocks(self):
        subscription = self.recorder.subscribe_snapshot((VS, ON_GROUND, NOSE_GEAR, TAIL, UNSCRIPTED))
        self.assertEqual(subscription.interval_ms, 1000)
        self.core.connect()
        (updates,) = drive(self.core, self.clock, 10, consumers=(self.recorder,))
        snapshots = [u for u in updates if isinstance(u, Snapshot)]
        # The first pump happens 0.1 s in and sends the initial sample; then one per second.
        self.assertEqual(len(snapshots), 10)
        first = snapshots[0]
        self.assertEqual(first.names, ("VERTICAL SPEED", "SIM ON GROUND", "GEAR IS ON GROUND:0", "ATC ID",
                                       "PLANE TOUCHDOWN NORMAL VELOCITY"))
        self.assertEqual(first.values, (-120.0, True, True, "VH-BBC", None))
        self.assertTrue(first.source.simulated)
        self.assertEqual(first.sample_time, simulator_utc(first.absolute_time_s))
        times = [s.simulation_time_s for s in snapshots]
        self.assertEqual(times, sorted(times))
        self.assertAlmostEqual(times[-1] - times[0], 9.0, places=6)
        self.assertEqual(self.recorder.latest_snapshot(subscription.id), snapshots[-1])
        with self.assertRaises(CoreRequestError):
            self.recorder.latest(subscription.id)

    def test_identical_snapshot_requests_share_one_provider_demand(self):
        other = self.core.register_consumer()
        a = self.recorder.subscribe_snapshot((VS, ON_GROUND))
        b = other.subscribe_snapshot((VS, ON_GROUND))
        self.recorder.subscribe_snapshot((ON_GROUND, VS))
        self.core.connect()
        self.core.pump()
        self.assertEqual(a.capability.id, b.capability.id)
        self.assertEqual(len(self.provider.active_demand), 2)

    def test_changed_only_and_frame_period_snapshots(self):
        changed = self.recorder.subscribe_snapshot((VS,), changed_only=True)
        frames = self.core.register_consumer(queue_size=4096)
        per_frame = frames.subscribe_snapshot((ON_GROUND,), period=SnapshotPeriod.SIM_FRAME, every=3)
        self.assertIsNone(per_frame.interval_ms)
        self.core.connect()
        self.clock.advance(3.05)
        self.provider.set_value(VS, 400.0)
        updates, frame_updates = drive(self.core, self.clock, 3, consumers=(self.recorder, frames))
        values = [u.values[0] for u in updates if isinstance(u, Snapshot) and u.subscription_id == changed.id]
        self.assertEqual(values, [400.0])
        # Every third frame at the simulated 30 Hz is 10 snapshots a second.
        self.assertIn(len([u for u in frame_updates if isinstance(u, Snapshot)]), range(29, 32))

    def test_snapshots_are_never_throttled_and_a_lost_burst_is_reported(self):
        small = self.core.register_consumer(queue_size=3)
        small.subscribe_snapshot((VS,))
        self.core.connect()
        drive(self.core, self.clock, 6, consumers=())
        updates = small.poll()
        gaps = [u for u in updates if isinstance(u, Gap)]
        self.assertEqual(gaps[0].dropped_updates, 3)
        self.assertEqual(sum(isinstance(u, Snapshot) for u in updates), 3)

    def test_invalid_fields_and_malformed_frames_are_reported_honestly(self):
        with self.assertRaises(CoreRequestError):
            self.recorder.subscribe_snapshot(())
        with self.assertRaises(CoreRequestError):
            self.recorder.subscribe_snapshot((VS, VS))
        with self.assertRaises(CoreRequestError):
            self.recorder.subscribe_snapshot((VS,), every=0)
        subscription = self.recorder.subscribe_snapshot((VS, ON_GROUND))
        session = self.core.connect().session_id
        self.core.pump()
        self.recorder.poll()
        signal = next(item.signal for item in self.provider.active_demand if item.signal.snapshot is not None)
        self.clock.advance(0.5)
        self.provider._offer(Reading(session, signal, SnapshotFrame((1.0,)), datetime.now(timezone.utc),
                                     self.clock.now))
        self.provider._offer(Reading(session, signal, SnapshotFrame(("text", 2)), datetime.now(timezone.utc),
                                     self.clock.now))
        self.core.pump()
        error, partial = self.recorder.poll()
        self.assertIsInstance(error, SignalError)
        self.assertEqual(error.subscription_id, subscription.id)
        self.assertEqual(partial.values, (None, True))
        self.assertIsNone(partial.simulation_time_s)
        self.core.disconnect()
        self.assertIsNone(self.recorder.latest_snapshot(subscription.id))


class SimulatedRealismTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_unchanged_values_stay_current_because_they_are_re_sent_on_cadence(self):
        provider = SimulatedProvider(clock=self.clock)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        on_ground = consumer.subscribe("aircraft.on_ground")
        core.connect()
        drive(core, self.clock, 30, step=1.0, consumers=(consumer,))
        self.assertIs(consumer.latest(on_ground.id).quality, Quality.CURRENT)

    def test_on_change_mode_remains_available(self):
        provider = SimulatedProvider(clock=self.clock, periodic=False)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        on_ground = consumer.subscribe("aircraft.on_ground")
        core.connect()
        drive(core, self.clock, 5, step=1.0, consumers=(consumer,))
        self.assertIs(consumer.latest(on_ground.id).quality, Quality.STALE)

    def test_any_sdk_simvar_and_catalog_states_are_scriptable(self):
        provider = SimulatedProvider(clock=self.clock)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        vs = consumer.subscribe_sdk_simvar(VS)
        running = consumer.subscribe("sim.running")
        clock_value = consumer.subscribe("sim.simulation_time")
        core.connect()
        core.pump()
        self.assertIs(consumer.latest(vs.id).quality, Quality.UNAVAILABLE)
        provider.set_value(VS, 650.0)
        drive(core, self.clock, 1.0, consumers=())
        self.assertEqual(consumer.latest(vs.id).value, 650.0)
        self.assertIs(consumer.latest(running.id).value, True)
        self.assertGreater(consumer.latest(clock_value.id).value, 0.5)

    def test_jitter_is_reproducible_and_bounded(self):
        stamps = []
        for _ in range(2):
            clock = FakeClock()
            provider = SimulatedProvider(clock=clock, wall_clock=clock.wall, jitter_ms=40, seed=5)
            core = Core(provider)
            consumer = core.register_consumer(queue_size=4096)
            consumer.subscribe("aircraft.ground_speed")
            core.connect()
            (updates,) = drive(core, clock, 20, step=0.05, consumers=(consumer,))
            core.disconnect()
            stamps.append([u.received_at for u in updates if isinstance(u, Observation)])
        self.assertEqual(stamps[0], stamps[1])
        spacing = [(b - a).total_seconds() for a, b in zip(stamps[0], stamps[0][1:])]
        self.assertEqual(len(stamps[0]), 20)
        self.assertTrue(all(0.9 < gap < 1.1 for gap in spacing))
        self.assertTrue(any(abs(gap - 1.0) > 0.005 for gap in spacing))

    def test_pause_and_rate_drive_simulation_time(self):
        provider = SimulatedProvider(clock=self.clock)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer(queue_size=4096)
        pause = consumer.subscribe("sim.pause_state")
        snapshot = consumer.subscribe_snapshot((ON_GROUND,))
        core.connect()
        core.pump()
        provider.set_value(ON_GROUND, True)
        drive(core, self.clock, 2, consumers=())
        start = consumer.latest_snapshot(snapshot.id).simulation_time_s
        provider.set_pause(PauseState.ACTIVE)
        drive(core, self.clock, 5, consumers=())
        paused = consumer.latest_snapshot(snapshot.id).simulation_time_s
        provider.set_pause(PauseState.NONE)
        provider.set_simulation_rate(4)
        drive(core, self.clock, 2, consumers=())
        latest = consumer.latest_snapshot(snapshot.id)
        self.assertAlmostEqual(paused, start + (paused - start), places=6)
        self.assertLess(paused - start, 1.01)
        self.assertGreater(latest.simulation_time_s - paused, 6.0)
        self.assertEqual(latest.simulation_rate, 4.0)
        events = [u.value for u in consumer.poll(limit=4096) if getattr(u, "capability_id", "") == pause.capability.id]
        self.assertEqual([int(e) for e in events], [4, 0])
        self.assertTrue(events[0].active_pause)
        self.assertFalse(events[0].full_pause)
