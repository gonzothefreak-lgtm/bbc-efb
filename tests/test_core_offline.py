"""Offline checks for the first Black Box Core implementation."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from bbc_core import (
    CommandState, Connection, ConnectionState, Core, CoreRequestError, Event,
    Gap, Observation, Quality, ValueType,
)
from bbc_core.catalog import READS
from bbc_core.provider import ProviderError, Reading
from bbc_core.simulated import SimulatedProvider


class CoreOfflineTests(TestCase):
    def setUp(self):
        self.now = 100.0
        for target in ("bbc_core.core.monotonic", "bbc_core.simulated.monotonic"):
            clock = patch(target, side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.provider = SimulatedProvider()
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)

    def test_two_consumers_share_provider_demand_and_can_leave_independently(self):
        inflight = self.core.register_consumer()
        diagnostics = self.core.register_consumer()
        self.core.connect()
        self.core.pump()
        self.assertEqual(self.provider.active_demand, ())

        display = inflight.subscribe("aircraft.ground_speed", interval_ms=500)
        diagnostic = diagnostics.subscribe("aircraft.ground_speed", interval_ms=200)
        self.core.pump()
        self.assertEqual(len(self.provider.active_demand), 1)
        self.assertEqual(self.provider.active_demand[0].interval_ms, 200)
        self.assertEqual(inflight.latest(display.id).value, 0.0)
        self.assertEqual(diagnostics.latest(diagnostic.id).value, 0.0)

        diagnostics.close()
        self.core.pump()
        self.assertEqual(self.provider.active_demand[0].interval_ms, 500)
        self.now += 0.5
        self.provider.publish("aircraft.ground_speed", 42)
        self.core.pump()
        update = inflight.latest(display.id)
        self.assertEqual((update.value, update.unit), (42.0, "knots"))
        self.assertTrue(update.source.simulated)
        inflight.unsubscribe(display.id)
        self.core.pump()
        self.assertEqual(self.provider.active_demand, ())

    def test_stale_read_retains_timestamp_and_emits_one_stale_update(self):
        consumer = self.core.register_consumer()
        subscription = consumer.subscribe("aircraft.ground_speed", max_age_ms=200)
        self.core.connect()
        self.core.pump()
        first = consumer.latest(subscription.id)
        self.assertEqual(first.quality, Quality.CURRENT)
        consumer.poll()

        self.now += 1
        stale = consumer.latest(subscription.id)
        self.assertEqual((stale.quality, stale.value), (Quality.STALE, first.value))
        self.assertEqual(stale.received_at, first.received_at)
        self.assertIsNone(stale.sample_time)
        self.assertEqual(consumer.poll(), (stale,))
        self.assertEqual(consumer.poll(), ())

        self.provider.publish("aircraft.ground_speed", 28)
        self.core.pump()
        self.assertEqual(consumer.latest(subscription.id).quality, Quality.CURRENT)

    def test_disconnect_discards_old_values_and_keeps_subscription_on_reconnect(self):
        consumer = self.core.register_consumer()
        subscription = consumer.subscribe("aircraft.ground_speed")
        session_one = self.core.connect().session_id
        self.core.pump()
        self.provider.lose_connection()
        self.core.pump()
        self.assertEqual(consumer.latest(subscription.id).quality, Quality.UNAVAILABLE)

        session_two = self.core.connect().session_id
        self.assertNotEqual(session_one, session_two)
        self.assertEqual(consumer.latest(subscription.id).quality, Quality.UNAVAILABLE)
        self.core.pump()
        self.assertEqual(consumer.latest(subscription.id).session_id, session_two)
        updates = consumer.poll()
        states = [update.state for update in updates if isinstance(update, Connection)]
        self.assertIn(ConnectionState.DISCONNECTED, states)
        self.assertEqual(
            [update.session_id for update in updates if isinstance(update, Observation)],
            [session_two],
        )

    def test_consumer_gap_is_explicit_and_does_not_affect_another_consumer(self):
        small = self.core.register_consumer(queue_size=1)
        regular = self.core.register_consumer()
        for consumer in small, regular:
            consumer.subscribe("sim.pause_state")
        self.core.connect()
        self.core.pump()
        small.poll()
        regular.poll()
        for flag in 1, 4, 0:
            self.provider.publish("sim.pause_state", flag)
        self.core.pump()

        gap, event = small.poll()
        self.assertIsInstance(gap, Gap)
        self.assertEqual(gap.dropped_updates, 2)
        self.assertIsInstance(event, Event)
        self.assertEqual(event.value, 0)
        self.assertEqual([update.value for update in regular.poll()], [1, 4, 0])

    def test_trusted_raw_diagnostics_share_the_named_subscription(self):
        module = self.core.register_consumer()
        diagnostics = self.core.register_consumer(diagnostics=True)
        named = module.subscribe("aircraft.on_ground")
        with self.assertRaises(CoreRequestError):
            module.subscribe_simvar("SIM ON GROUND", "bool", ValueType.BOOLEAN)
        raw = diagnostics.subscribe_simvar("sim on ground", "BOOL", ValueType.BOOLEAN)
        self.core.connect()
        self.core.pump()
        self.assertEqual(len(self.provider.active_demand), 1)
        self.assertIs(diagnostics.latest(raw.id).value, True)

        self.now += 1
        self.provider.publish("aircraft.on_ground", 0)
        self.core.pump()
        self.assertIs(module.latest(named.id).value, False)

    def test_delayed_reading_from_previous_session_is_ignored(self):
        consumer = self.core.register_consumer()
        subscription = consumer.subscribe("aircraft.ground_speed")
        old_session = self.core.connect().session_id
        self.core.pump()
        self.core.disconnect()
        self.core.connect()
        self.core.pump()
        original = consumer.latest(subscription.id)
        delayed = Reading(
            old_session, READS["aircraft.ground_speed"].signal, 300,
            datetime.now(timezone.utc), self.now + 1,
        )
        with patch.object(self.provider, "drain", return_value=(delayed,)):
            self.core.pump()
        self.assertEqual(consumer.latest(subscription.id), original)

    def test_radio_tune_and_swap_report_accepted_sent_and_confirmed(self):
        radio = self.core.register_consumer(control_groups=("radios",))
        active = radio.subscribe("radio.com1.active_frequency")
        standby = radio.subscribe("radio.com1.standby_frequency")
        self.core.connect()
        self.core.pump()
        original_active = radio.latest(active.id).value
        radio.poll()

        tune = radio.submit_command("radio.com1.set_standby", 123_450_000)
        self.assertEqual(tune.state, CommandState.ACCEPTED)
        self.core.pump()
        self.assertEqual(radio.command_result(tune.id).state, CommandState.SENT)
        self.core.pump()
        self.assertEqual(radio.command_result(tune.id).state, CommandState.CONFIRMED)
        self.assertEqual(radio.latest(standby.id).value, 123_450_000)

        swap = radio.submit_command("radio.com1.swap")
        self.core.pump()
        self.core.pump()
        self.assertEqual(radio.command_result(swap.id).state, CommandState.CONFIRMED)
        self.assertEqual(radio.latest(active.id).value, 123_450_000)
        self.assertEqual(radio.latest(standby.id).value, original_active)

    def test_command_access_and_arguments_are_checked_without_provider_dispatch(self):
        module = self.core.register_consumer()
        radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()
        names = {status.capability.id for status in module.discover().capabilities}
        self.assertNotIn("radio.com1.swap", names)
        denied = module.submit_command("radio.com1.swap")
        malformed = radio.submit_command("radio.com1.set_standby", True)
        with patch.object(self.provider, "send") as send:
            self.core.pump()
        send.assert_not_called()
        for result in denied, malformed:
            self.assertEqual(result.state, CommandState.REJECTED)

    def test_disconnect_marks_sent_command_uncertain_and_does_not_replay_it(self):
        self.provider = SimulatedProvider(confirm_commands=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()
        sent = radio.submit_command("radio.com1.swap")
        queued = radio.submit_command("radio.com1.set_standby", 123_450_000)
        self.core.pump()
        self.assertEqual(radio.command_result(sent.id).state, CommandState.SENT)

        self.provider.lose_connection()
        self.core.pump()
        self.assertEqual(radio.command_result(sent.id).state, CommandState.UNCERTAIN)
        self.assertEqual(radio.command_result(queued.id).state, CommandState.NOT_SENT)
        self.core.connect()
        with patch.object(self.provider, "send") as send:
            self.core.pump()
        send.assert_not_called()

    def test_send_failure_blocks_writes_but_leaves_telemetry_usable(self):
        radio = self.core.register_consumer(control_groups=("radios",))
        subscription = radio.subscribe("aircraft.ground_speed")
        self.core.connect()
        self.core.pump()
        sent = radio.submit_command("radio.com1.swap")
        with patch.object(self.provider, "send", side_effect=ProviderError("send failed")):
            self.core.pump()

        self.assertEqual(radio.command_result(sent.id).state, CommandState.UNCERTAIN)
        self.assertEqual(radio.submit_command("radio.com1.swap").state, CommandState.REJECTED)
        self.now += 1
        self.provider.publish("aircraft.ground_speed", 29)
        self.core.pump()
        self.assertEqual(radio.latest(subscription.id).value, 29.0)

    def test_unresolved_cleanup_blocks_opening_a_new_session(self):
        consumer = self.core.register_consumer()
        session = self.core.connect().session_id
        with patch.object(self.provider, "disconnect", side_effect=ProviderError("close failed")):
            self.core.disconnect()
            self.assertEqual(consumer.connection().state, ConnectionState.ERROR)
            self.assertIsNone(self.core.connect().session_id)
        self.core.disconnect()
        self.assertNotEqual(self.core.connect().session_id, session)
