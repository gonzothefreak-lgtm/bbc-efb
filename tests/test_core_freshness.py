"""Freshness for late subscribers, lifecycle invalidation timing and deterministic demand order.

Review round 3: the stale-heap late-subscriber regression, A33 (a late system-state
subscriber must not see an old value as CURRENT), the lifecycle invalidation stamp, and
``_signal_key`` covering ``client_data_changed_only``.
"""

from unittest import TestCase

from bbc_core import ClientDataArea, Core, Observation, Quality
from bbc_core.catalog import READS, SYSTEM_EVENTS, SYSTEM_STATES
from bbc_core.client_data import ClientDataMode
from bbc_core.core import _signal_key
from bbc_core.provider import Demand, Signal
from bbc_core.contract import ValueType
from bbc_core.simulated import SimulatedProvider

from test_core_support import FakeClock


def qualities(updates):
    return [u.quality for u in updates if isinstance(u, Observation)]


class LateSubscriberStaleTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, periodic=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)

    def test_a_late_subscriber_is_told_when_a_quiet_feed_goes_stale(self):
        early = self.core.register_consumer()
        early_sub = early.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        early.poll()
        self.clock.advance(1)
        self.core.pump()
        late = self.core.register_consumer()
        late_sub = late.subscribe("aircraft.ground_speed")
        self.core.pump()
        late.poll()
        self.clock.advance(10)  # Well past 3 x 1000 ms with no new sample.
        self.core.pump()
        self.assertEqual(qualities(early.poll()), [Quality.STALE])
        self.assertEqual(qualities(late.poll()), [Quality.STALE])
        self.assertIs(late.latest(late_sub.id).quality, Quality.STALE)
        self.assertIs(early.latest(early_sub.id).quality, Quality.STALE)
        # Reported once, not on every poll.
        self.clock.advance(1)
        self.assertEqual(qualities(late.poll()), [])

    def test_a_late_subscriber_to_a_fresh_feed_is_not_reported_stale(self):
        early = self.core.register_consumer()
        early.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        late = self.core.register_consumer()
        late.subscribe("aircraft.ground_speed")
        for _ in range(5):
            self.clock.advance(1)
            self.provider.publish("aircraft.ground_speed", 10.0)
            self.core.pump()
        self.assertNotIn(Quality.STALE, qualities(late.poll()))


class LateSystemStateTests(TestCase):
    """A33: an inherited system-state value is STALE until the simulator answers again."""

    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, periodic=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.first = self.core.register_consumer()
        self.first_sub = self.first.subscribe("sim.dialog_mode")
        self.core.connect()
        self.core.pump()
        self.first.poll()

    def test_the_inherited_value_is_stale_until_the_refresh_reply(self):
        self.provider.set_value(SYSTEM_STATES["DIALOGMODE"], True)  # Changed with no event.
        self.clock.advance(3600)
        late = self.core.register_consumer()
        late_sub = late.subscribe("sim.dialog_mode")
        waiting = late.latest(late_sub.id)
        self.assertIs(waiting.quality, Quality.STALE)
        self.assertIs(waiting.value, False)
        self.assertIn("Waiting for a fresh reply", waiting.reason)
        # The earlier subscriber's own view is unchanged.
        self.assertIs(self.first.latest(self.first_sub.id).quality, Quality.CURRENT)
        self.core.pump()  # Sends the refresh and receives the reply.
        fresh = late.latest(late_sub.id)
        self.assertIs(fresh.quality, Quality.CURRENT)
        self.assertIs(fresh.value, True)
        self.assertIsNone(fresh.reason)

    def test_a_late_subscriber_without_a_previous_reply_waits_as_unavailable(self):
        self.core.disconnect()
        self.core.connect()
        late = self.core.register_consumer()
        late_sub = late.subscribe("sim.dialog_mode")
        self.assertIs(late.latest(late_sub.id).quality, Quality.UNAVAILABLE)
        self.core.pump()
        self.assertIs(late.latest(late_sub.id).quality, Quality.CURRENT)

    def test_dialog_mode_is_documented_as_refreshed_only_on_request(self):
        description = READS["sim.dialog_mode"].capability.description
        self.assertIn("only on request", description)


class LifecycleStampTests(TestCase):
    def test_a_fresh_reply_in_the_same_batch_as_the_event_is_kept(self):
        """The invalidation uses the event's receipt time, so the reply after it is accepted."""
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock, periodic=False)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        subscription = consumer.subscribe("sim.loaded_aircraft_file")
        core.connect()
        core.pump()
        core.pump()
        consumer.poll()
        self.assertNotEqual(consumer.latest(subscription.id).value, "new.cfg")
        provider.set_value(SYSTEM_STATES["AIRCRAFTLOADED"], "new.cfg")
        provider.emit_event(SYSTEM_EVENTS["AIRCRAFTLOADED"], "new.cfg")
        provider.request_system_state(SYSTEM_STATES["AIRCRAFTLOADED"])
        clock.advance(0.05)  # Core processes the batch after both arrived.
        core.pump()
        observation = consumer.latest(subscription.id)
        self.assertIs(observation.quality, Quality.CURRENT)
        self.assertEqual(observation.value, "new.cfg")

    def test_an_event_still_invalidates_an_older_reply(self):
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock, periodic=False)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        subscription = consumer.subscribe("sim.loaded_aircraft_file")
        core.connect()
        core.pump()
        clock.advance(1)
        provider.emit_event(SYSTEM_EVENTS["AIRCRAFTLOADED"], "other.cfg")
        sent = []
        original = provider.request_system_state
        provider.request_system_state = lambda signal: (sent.append(signal), original(signal))
        core.pump()
        self.assertIs(consumer.latest(subscription.id).quality, Quality.UNAVAILABLE)
        core.pump()
        self.assertEqual(sent, [SYSTEM_STATES["AIRCRAFTLOADED"]])
        self.assertIs(consumer.latest(subscription.id).quality, Quality.CURRENT)


class SignalKeyTests(TestCase):
    def test_changed_only_client_data_signals_have_distinct_keys(self):
        region = ClientDataArea("PMDG_NG3_Data", 8).region()
        every = Signal(region.area.name, None, ValueType.BYTES, client_data_region=region,
                       client_data_mode=ClientDataMode.SECOND)
        changed = Signal(region.area.name, None, ValueType.BYTES, client_data_region=region,
                         client_data_mode=ClientDataMode.SECOND, client_data_changed_only=True)
        self.assertNotEqual(_signal_key(every), _signal_key(changed))
        ordered = sorted((changed, every), key=_signal_key)
        self.assertEqual(sorted((every, changed), key=_signal_key), ordered)
        self.assertEqual(tuple(Demand(s, None) for s in ordered), tuple(Demand(s, None) for s in sorted(
            (every, changed), key=_signal_key)))
