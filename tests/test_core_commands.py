"""Command deadlines, unexpected send failures, per-module quotas and simulated command parity."""

from unittest import TestCase
from unittest.mock import patch

from bbc_core import CommandState, Core, SimVar, ValueType
from bbc_core.catalog import valid_com_channel
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simulated import SimulatedProvider

from test_core_support import FakeClock


class DeadlineTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def core(self, **options):
        provider = SimulatedProvider(clock=self.clock, periodic=False, **options)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        radio = core.register_consumer(control_groups=("radios",))
        core.connect()
        core.pump()
        return provider, core, radio

    def test_queue_time_does_not_count_against_a_sent_commands_confirmation(self):
        # Each command takes the 1.5 s readback window, like the live adapter's no-readback path.
        provider, core, radio = self.core(command_delay_s=1.5)
        ids = [radio.submit_command("radio.com1.swap").id for _ in range(4)]
        for _ in range(80):
            self.clock.advance(0.1)
            core.pump()
        self.assertEqual([radio.command_result(i).state for i in ids], [CommandState.CONFIRMED] * 4)
        self.assertIs(radio.submit_command("radio.com1.swap").state, CommandState.ACCEPTED)

    def test_command_left_waiting_in_the_queue_is_not_sent_and_does_not_block_writes(self):
        provider, core, radio = self.core(confirm_commands=False, max_command_timeout_seconds=30.0)
        first = radio.submit_command("radio.com1.swap")
        core.pump()
        queued = radio.submit_command("radio.com1.set_standby", 121_500_000)
        self.clock.advance(6)
        core.pump()
        result = radio.command_result(queued.id)
        self.assertIs(result.state, CommandState.NOT_SENT)
        self.assertIn("waited too long in the queue", result.detail)
        self.assertIs(radio.command_result(first.id).state, CommandState.SENT)
        provider.complete_command()
        core.pump()
        self.assertIs(radio.command_result(first.id).state, CommandState.CONFIRMED)
        self.assertIs(radio.submit_command("radio.com1.swap").state, CommandState.ACCEPTED)

    def test_silent_provider_past_its_own_timeout_blocks_writes_fail_closed(self):
        provider, core, radio = self.core(confirm_commands=False, max_command_timeout_seconds=2.0)
        sent = radio.submit_command("radio.com1.swap")
        core.pump()
        self.clock.advance(2.9)
        core.pump()
        self.assertIs(radio.command_result(sent.id).state, CommandState.SENT)
        self.clock.advance(0.2)
        with self.assertLogs("bbc_core.core", "WARNING"):
            core.pump()
        self.assertIs(radio.command_result(sent.id).state, CommandState.UNCERTAIN)
        self.assertIs(radio.submit_command("radio.com1.swap").state, CommandState.REJECTED)

    def test_unexpected_send_exception_is_uncertain_never_resent_and_keeps_the_session(self):
        provider, core, radio = self.core()
        speed = radio.subscribe("aircraft.ground_speed")
        command = radio.submit_command("radio.com1.swap")
        with patch.object(provider, "send", side_effect=OSError("exception: access violation")) as send, \
                self.assertLogs("bbc_core.core", "ERROR"):
            core.pump()
            core.pump()
        self.assertEqual(send.call_count, 1)
        result = radio.command_result(command.id)
        self.assertIs(result.state, CommandState.UNCERTAIN)
        self.assertIn("access violation", result.detail)
        self.assertIs(radio.submit_command("radio.com1.swap").state, CommandState.REJECTED)
        self.assertIs(radio.connection().state.value, "connected")
        self.clock.advance(1)
        provider.publish("aircraft.ground_speed", 12)
        core.pump()
        self.assertEqual(radio.latest(speed.id).value, 12.0)


class QuotaTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, periodic=False, confirm_commands=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.busy = self.core.register_consumer(control_groups=("radios",))
        self.panel = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()

    def test_one_module_cannot_fill_the_shared_command_queue(self):
        results = [self.busy.submit_command("radio.com1.swap") for _ in range(20)]
        rejected = [r for r in results if r.state is CommandState.REJECTED]
        self.assertEqual(len(rejected), 4)
        self.assertIn("too many commands", rejected[0].detail)
        self.assertIs(self.panel.submit_command("radio.com1.swap").state, CommandState.ACCEPTED)

    def test_newer_absolute_setter_supersedes_a_queued_one_but_toggles_are_kept(self):
        self.busy.submit_command("radio.com1.swap")
        self.core.pump()
        tunes = [self.busy.submit_command("radio.com1.set_standby", hz)
                 for hz in (121_500_000, 121_525_000, 121_550_000)]
        swaps = [self.busy.submit_command("radio.com1.swap") for _ in range(2)]
        self.assertEqual([self.busy.command_result(t.id).state for t in tunes],
                         [CommandState.NOT_SENT, CommandState.NOT_SENT, CommandState.ACCEPTED])
        self.assertIn("Superseded", self.busy.command_result(tunes[0].id).detail)
        self.assertEqual([self.busy.command_result(s.id).state for s in swaps], [CommandState.ACCEPTED] * 2)
        other = self.panel.submit_command("radio.com1.set_standby", 122_800_000)
        self.assertIs(self.panel.command_result(other.id).state, CommandState.ACCEPTED)


class ClosedConsumerTests(TestCase):
    def test_closing_a_module_withdraws_its_queued_commands_but_not_the_sent_one(self):
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock, periodic=False, confirm_commands=False)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        module = core.register_consumer(control_groups=("radios",))
        core.connect()
        module.submit_command("radio.com1.swap")
        core.pump()
        module.submit_command("radio.com1.set_standby", 122_800_000)
        module.close()
        sends = []
        original = provider.send
        provider.send = lambda command: (sends.append(command), original(command))
        provider.complete_command()
        core.pump()
        core.pump()
        self.assertEqual(sends, [])
        self.assertEqual(core._pending, {})


class ParityTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock, periodic=False)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.radio = self.core.register_consumer(control_groups=("radios", "simvar_writes", "client_events"))
        self.core.connect()

    def test_com_frequencies_must_be_on_a_vhf_channel_before_dispatch(self):
        for hz in (3, 99_000_000, 118_001_000, 118_020_000, 137_000_000):
            self.assertIs(self.radio.submit_command("radio.com1.set_standby", hz).state, CommandState.REJECTED)
        for hz in (118_000_000, 118_005_000, 136_990_000, 123_450_000):
            self.assertTrue(valid_com_channel(hz))
        self.assertFalse(valid_com_channel(118_005_000, spacing=0))

    def test_simulated_25_khz_radio_rejects_an_8_33_channel_without_blocking_writes(self):
        """A62/C6: the provider refuses before sending; only this one command fails."""
        standby = self.radio.subscribe("radio.com1.standby_frequency")
        tune = self.radio.submit_command("radio.com1.set_standby", 118_005_000)
        self.assertIs(tune.state, CommandState.ACCEPTED)  # Core does not know the spacing yet.
        self.core.pump()
        self.core.pump()
        result = self.radio.command_result(tune.id)
        self.assertIs(result.state, CommandState.REJECTED)
        self.assertIn("25 kHz channel spacing", result.detail)
        self.assertEqual(self.radio.latest(standby.id).value, 121_500_000)
        swap = self.radio.submit_command("radio.com1.swap")
        self.assertIs(swap.state, CommandState.ACCEPTED)
        self.core.pump()
        self.core.pump()
        self.assertIs(self.radio.command_result(swap.id).state, CommandState.CONFIRMED)

    def test_simulated_generic_writes_and_events_follow_the_live_outcomes(self):
        light = SimVar("LIGHT POTENTIOMETER", "percent", ValueType.NUMBER, index=2)
        reading = self.radio.subscribe_sdk_simvar(light)
        write = self.radio.submit_simvar(light, 60.0)
        self.core.pump()
        self.core.pump()
        self.assertIs(self.radio.command_result(write.id).state, CommandState.CONFIRMED)
        self.assertEqual(self.radio.latest(reading.id).value, 60.0)
        event = self.radio.submit_client_event("A32NX.FCU_HDG_INC")
        self.core.pump()
        self.core.pump()
        self.assertIs(self.radio.command_result(event.id).state, CommandState.UNVERIFIED)

    def test_simulated_default_cadence_matches_the_live_adapter(self):
        self.assertEqual(self.provider.minimum_interval_ms, 1000)
        subscription = self.radio.subscribe("aircraft.ground_speed", interval_ms=200)
        self.assertEqual(subscription.interval_ms, 1000)
        # C5: whole-second granularity like the live adapter, so 1500 ms is served as 2000 ms.
        self.assertEqual(self.provider.interval_granularity_ms, SimConnectProvider.interval_granularity_ms)
        self.assertEqual(self.radio.subscribe("aircraft.title", interval_ms=1500).interval_ms, 2000)
