"""Delivery cadence, freshness labelling, queue lanes and continuity across gaps and sessions."""

from datetime import datetime, timezone
from random import Random
from unittest import TestCase

from bbc_core import (
    Connection, ConnectionState, Core, Event, Gap, InputCatalog, InputCatalogState, Observation, Quality,
    SimVar, ValueType,
)
from bbc_core.catalog import READS
from bbc_core.contract import AircraftInput, CommandResult, InputId, SignalError, SubscriptionEnded
from bbc_core.provider import Reading
from bbc_core.simulated import SimulatedProvider

from test_core_support import FakeClock, InputSimulatedProvider


GROUND_SPEED = READS["aircraft.ground_speed"].signal


class ManualProvider(SimulatedProvider):
    """Non-periodic simulated provider whose readings a test stamps by hand."""

    def __init__(self, clock, **options):
        super().__init__(clock=clock, periodic=False, **options)

    def reading(self, signal, value, at, error=None):
        self._offer(Reading(self._session_id, signal, value, datetime.now(timezone.utc), at, error=error))


class CadenceTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = ManualProvider(self.clock)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.consumer = self.core.register_consumer(queue_size=4096)

    def observations(self, subscription):
        return [u for u in self.consumer.poll(limit=4096)
                if isinstance(u, Observation) and u.capability_id == subscription.capability.id]

    def feed(self, times):
        for at in times:
            self.clock.now = at
            self.provider.reading(GROUND_SPEED, 10.0, at)
            self.core.pump()

    def test_default_interval_delivers_every_jittered_one_hz_sample(self):
        subscription = self.consumer.subscribe("aircraft.ground_speed", interval_ms=1000)
        self.core.connect()
        self.core.pump()
        self.consumer.poll()
        rng = Random(7)
        self.feed([1001 + i + rng.uniform(-0.01, 0.01) for i in range(60)])
        self.assertEqual(len(self.observations(subscription)), 60)

    def test_simulated_cadence_with_seeded_jitter_reaches_the_consumer_intact(self):
        for minimum in (100, 1000):
            with self.subTest(minimum_interval_ms=minimum):
                clock = FakeClock()
                provider = SimulatedProvider(clock=clock, minimum_interval_ms=minimum,
                                             jitter_ms=minimum * 0.2, seed=minimum)
                core = Core(provider)
                consumer = core.register_consumer(queue_size=4096)
                subscription = consumer.subscribe("aircraft.ground_speed", interval_ms=minimum)
                core.connect()
                received = []
                for _ in range(round(30_000 / minimum)):
                    clock.advance(minimum / 1000)
                    core.pump()
                    received.extend(u for u in consumer.poll(limit=4096) if isinstance(u, Observation))
                core.disconnect()
                self.assertEqual(subscription.interval_ms, minimum)
                self.assertIn(len(received), range(round(30_000 / minimum) - 1, round(30_000 / minimum) + 1))
                self.assertTrue(all(u.quality is Quality.CURRENT for u in received))

    def test_alternating_early_and_late_receipts_are_not_thinned(self):
        subscription = self.consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        self.consumer.poll()
        times, at = [], 1001.0
        for i in range(40):
            at += 0.96 if i % 2 else 1.045
            times.append(at)
        self.feed(times)
        self.assertEqual(len(self.observations(subscription)), 40)

    def test_slower_consumer_on_faster_shared_demand_gets_its_own_grid(self):
        fast = self.consumer.subscribe("aircraft.ground_speed", interval_ms=1000)
        slow_consumer = self.core.register_consumer(queue_size=4096)
        slow = slow_consumer.subscribe("aircraft.ground_speed", interval_ms=2000)
        self.core.connect()
        self.core.pump()
        self.consumer.poll()
        slow_consumer.poll()
        rng = Random(11)
        self.feed([1001 + i + rng.uniform(-0.05, 0.05) for i in range(60)])
        self.assertEqual(len(self.observations(fast)), 60)
        slow_count = sum(isinstance(u, Observation) and u.capability_id == slow.capability.id
                         for u in slow_consumer.poll(limit=4096))
        self.assertIn(slow_count, range(29, 32))

    def test_queued_samples_are_labelled_at_receipt_and_quiet_feed_reports_one_stale(self):
        provider = ManualProvider(self.clock, minimum_interval_ms=100)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        subscription = consumer.subscribe("aircraft.ground_speed", interval_ms=200)
        core.connect()
        core.pump()
        consumer.poll()
        for i in range(10):
            self.clock.now = 1000.2 + i * 0.2
            provider.reading(GROUND_SPEED, float(i), self.clock.now)
            core.pump()
        self.clock.advance(1.0)
        batch = [u for u in consumer.poll() if isinstance(u, Observation)]
        self.assertEqual([u.quality for u in batch[:10]], [Quality.CURRENT] * 10)
        self.assertEqual([u.quality for u in batch[10:]], [Quality.STALE])
        self.assertEqual(batch[-1].value, 9.0)
        self.assertEqual(consumer.poll(), ())
        self.assertIs(consumer.latest(subscription.id).quality, Quality.STALE)

    def test_stale_report_waits_for_queued_samples_and_is_not_starved_by_a_full_batch(self):
        consumer = self.core.register_consumer(queue_size=4096)
        quiet = consumer.subscribe("aircraft.title", max_age_ms=500)
        busy = consumer.subscribe("sim.pause_state")
        self.core.connect()
        self.core.pump()
        consumer.poll(limit=4096)
        self.clock.advance(2)
        for flag in range(50):
            self.provider.publish("sim.pause_state", flag % 2)
        self.core.pump()
        first = consumer.poll(limit=8)
        self.assertEqual(len(first), 8)
        stale = [u for u in first if isinstance(u, Observation) and u.capability_id == quiet.capability.id]
        self.assertEqual([u.quality for u in stale], [Quality.STALE])
        rest = consumer.poll(limit=4096)
        self.assertFalse(any(isinstance(u, Observation) for u in rest))
        self.assertTrue(busy.id)


class LaneTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, minimum_interval_ms=100)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)

    def test_sample_burst_cannot_evict_command_results_or_connection_changes(self):
        radio = self.core.register_consumer(control_groups=("radios",), queue_size=4)
        radio.subscribe("sim.pause_state")
        self.core.connect()
        self.core.pump()
        command = radio.submit_command("radio.com1.swap")
        self.core.pump()
        self.core.pump()
        for flag in range(20):
            self.provider.publish("sim.pause_state", flag % 2)
        self.core.pump()
        self.provider.lose_connection()
        self.core.pump()
        updates = radio.poll(limit=4096)
        results = [u.state.value for u in updates if isinstance(u, CommandResult) and u.id == command.id]
        self.assertEqual(results, ["accepted", "sent", "confirmed"])
        states = [u.state for u in updates if isinstance(u, Connection)]
        self.assertEqual(states[-1], ConnectionState.DISCONNECTED)
        gaps = [u for u in updates if type(u) is Gap]
        self.assertEqual([(g.dropped_updates, g.reason) for g in gaps], [(16, "Consumer update queue lost data.")])
        events = [u for u in updates if isinstance(u, Event)]
        self.assertEqual(len(events), 4)

    def test_updates_keep_production_order_across_lanes(self):
        consumer = self.core.register_consumer()
        consumer.subscribe("sim.pause_state")
        self.core.connect()
        self.core.pump()
        consumer.poll()
        self.provider.publish("sim.pause_state", 1)
        self.core.pump()
        self.provider.drop_updates(2)
        self.core.pump()
        self.provider.publish("sim.pause_state", 0)
        self.core.pump()
        kinds = [type(u).__name__ for u in consumer.poll()]
        self.assertEqual(kinds, ["Event", "Gap", "Event"])

    def test_provider_event_error_is_a_signal_error_not_queue_loss(self):
        consumer = self.core.register_consumer()
        subscription = consumer.subscribe("sim.pause_state")
        self.core.connect()
        self.core.pump()
        consumer.poll()
        self.provider._emit_reading(READS["sim.pause_state"].signal, None, self.clock.now, error="registration failed")
        self.provider.publish("sim.pause_state", "not flags")
        self.core.pump()
        errors = consumer.poll()
        self.assertEqual(len(errors), 2)
        for error in errors:
            self.assertIsInstance(error, SignalError)
            # C7: a provider error is not data loss, so it is never a Gap.
            self.assertNotIsInstance(error, Gap)
            self.assertFalse(error.is_data_loss)
            self.assertEqual((error.subscription_id, error.capability_id), (subscription.id, "sim.pause_state"))
        self.assertEqual(errors[0].reason, "registration failed")


class ContinuityTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, periodic=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.consumer = self.core.register_consumer()

    def test_ingress_gap_keeps_samples_received_before_it_and_marks_the_loss_in_order(self):
        subscription = self.consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        self.clock.advance(1)
        self.provider.publish("aircraft.ground_speed", 55)
        self.core.pump()
        self.provider.drop_updates(3)
        self.core.pump()
        updates = [u for u in self.consumer.poll() if not isinstance(u, Connection)]
        self.assertEqual([type(u).__name__ for u in updates], ["Observation", "Observation", "Gap"])
        self.assertEqual(updates[1].value, 55.0)
        self.assertEqual((updates[2].dropped_updates, updates[2].reason), (3, "Provider update queue lost data."))
        self.assertIs(self.consumer.latest(subscription.id).quality, Quality.UNAVAILABLE)

    def test_ingress_gap_requests_fresh_one_shot_states(self):
        running = self.consumer.subscribe("sim.running")
        self.core.connect()
        self.core.pump()
        self.assertIs(self.consumer.latest(running.id).value, True)
        self.provider.drop_updates(1)
        self.core.pump()
        self.assertIs(self.consumer.latest(running.id).quality, Quality.UNAVAILABLE)
        self.core.pump()
        self.assertIs(self.consumer.latest(running.id).quality, Quality.CURRENT)

    def test_session_end_delivers_queued_samples_before_the_connection_change(self):
        self.consumer.subscribe("aircraft.ground_speed")
        session = self.core.connect().session_id
        self.core.pump()
        self.clock.advance(1)
        self.provider.publish("aircraft.ground_speed", 3)
        self.core.pump()
        self.core.disconnect()
        updates = [u for u in self.consumer.poll() if not (isinstance(u, Connection) and u.state is not
                                                           ConnectionState.DISCONNECTED)]
        self.assertEqual([type(u).__name__ for u in updates], ["Observation", "Observation", "Connection"])
        self.assertEqual({u.session_id for u in updates[:2]}, {session})


class InputContinuityTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = InputSimulatedProvider(clock=self.clock, periodic=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.consumer = self.core.register_consumer()
        self.session = self.core.connect().session_id

    def catalog(self, generation, state, inputs=()):
        return InputCatalog(self.session, generation, state, inputs)

    def test_ready_catalog_of_the_generation_a_gap_interrupted_is_accepted(self):
        self.provider.offer(self.catalog(1, InputCatalogState.LOADING))
        self.core.pump()
        self.provider.drop_updates(1)
        self.core.pump()
        self.assertIs(self.consumer.aircraft_inputs().state, InputCatalogState.UNAVAILABLE)
        ready = self.catalog(1, InputCatalogState.READY, (
            AircraftInput(InputId(self.session, 1, 42), "LIGHT_SWITCH", ValueType.NUMBER),))
        self.provider.offer(ready)
        self.core.pump()
        self.assertIs(self.consumer.aircraft_inputs().state, InputCatalogState.READY)
        self.provider.offer(self.catalog(1, InputCatalogState.LOADING))
        self.core.pump()
        self.assertIs(self.consumer.aircraft_inputs().state, InputCatalogState.READY)

    def test_input_subscription_end_is_explicit_and_latest_explains_it(self):
        ref = InputId(self.session, 1, 42)
        self.provider.offer(self.catalog(1, InputCatalogState.READY, (AircraftInput(ref, "LIGHT", ValueType.NUMBER),)))
        self.core.pump()
        subscription = self.consumer.subscribe_input(ref)
        self.consumer.poll()
        self.provider.offer(self.catalog(2, InputCatalogState.LOADING))
        self.core.pump()
        ended = [u for u in self.consumer.poll() if isinstance(u, SubscriptionEnded)]
        self.assertEqual([u.subscription_id for u in ended], [subscription.id])
        ended_value = self.consumer.latest(subscription.id)
        self.assertIs(ended_value.quality, Quality.UNAVAILABLE)
        self.assertIn("aircraft inputs changed", ended_value.reason)
        self.assertEqual(ended_value.capability_id, subscription.capability.id)
        self.consumer.unsubscribe(subscription.id)
        with self.assertRaisesRegex(Exception, "active value subscription"):
            self.consumer.latest(subscription.id)


class IndexTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, periodic=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)

    def test_demand_is_cached_until_a_subscription_changes(self):
        consumer = self.core.register_consumer()
        consumer.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        applied = self.core._applied_demand
        self.core.pump()
        self.assertIs(self.core._desired_demand(), applied)
        extra = consumer.subscribe("aircraft.title")
        self.assertIsNot(self.core._desired_demand(), applied)
        consumer.unsubscribe(extra.id)
        self.assertEqual(self.core._desired_demand(), applied)

    def test_readings_reach_only_matching_subscriptions_and_unsubscribe_releases_values(self):
        consumers = [self.core.register_consumer() for _ in range(8)]
        subscriptions = [c.subscribe_sdk_simvar(SimVar(f"FAKE SIGNAL {i}", "number", ValueType.NUMBER))
                         for i, c in enumerate(consumers)]
        shared = [c.subscribe("aircraft.ground_speed") for c in consumers]
        self.core.connect()
        for i in range(8):
            self.provider.set_value(SimVar(f"FAKE SIGNAL {i}", "number", ValueType.NUMBER), float(i))
        self.core.pump()
        for consumer in consumers:
            consumer.poll()
        self.provider.publish(SimVar("FAKE SIGNAL 3", "number", ValueType.NUMBER), 33.0)
        self.core.pump()
        received = [len([u for u in c.poll() if isinstance(u, Observation)]) for c in consumers]
        self.assertEqual(received, [0, 0, 0, 1, 0, 0, 0, 0])
        self.assertEqual(consumers[3].latest(subscriptions[3].id).value, 33.0)
        for consumer, subscription in zip(consumers, shared):
            consumer.unsubscribe(subscription.id)
        self.assertNotIn(GROUND_SPEED, self.core._by_signal)
        # The reading is kept until the next pump retires the demand, for a quick resubscriber.
        self.core.pump()
        self.assertNotIn(GROUND_SPEED, self.core._readings)
