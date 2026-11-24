"""Simulated-versus-live parity and contract shapes from review round 3.

* C5: interval granularity and command timeout attributes on the simulated provider.
* C6: COM frequencies rounded to whole Hz, and one channel-spacing rule in Core and in
  the simulated provider (the live adapter uses the same catalog rule).
* C7: SignalError is not a Gap.
* FlightTape rejects bad action values when it is built, naming the entry.
* Changed-only snapshots report no simulator clocks, like the live adapter.
"""

import typing
from unittest import TestCase

from bbc_core import (
    CommandState, Core, Gap, Quality, SignalError, SimVar, Snapshot, ValueType, known_control_groups,
)
from bbc_core.catalog import (
    COM1_ACTIVE, COM1_SPACING, com_channel_problem, decode_frequency_hz, valid_com_channel,
)
from bbc_core.contract import Update
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simulated import FlightTape, SimulatedProvider, TapeAction

from test_adapter_support import FakeClock as AdapterClock
from test_core_support import FakeClock, drive
from test_simconnect_provider_offline import FakeSdk


class IntervalGranularityTests(AdapterClock, TestCase):
    def setUp(self):
        self.start_clock()  # The live adapter runs on the shared fake clock (A139).

    def test_simulated_defaults_match_the_live_adapter(self):
        simulated = SimulatedProvider()
        live = SimConnectProvider(FakeSdk())
        self.assertEqual(simulated.interval_granularity_ms, live.interval_granularity_ms)
        self.assertEqual(simulated.minimum_interval_ms, live.minimum_interval_ms)
        self.assertEqual(simulated.max_command_timeout_seconds, 4.0)
        for provider in (simulated, live):
            consumer = Core(provider).register_consumer()
            self.assertEqual(consumer.subscribe("aircraft.ground_speed", interval_ms=1500).interval_ms, 2000)

    def test_the_simulated_provider_serves_the_rounded_cadence(self):
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock, wall_clock=clock.wall)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        consumer.subscribe("aircraft.ground_speed", interval_ms=1500)
        core.connect()
        (updates,) = drive(core, clock, 10.05, step=0.05, consumers=(consumer,))
        times = [u.received_at for u in updates if hasattr(u, "quality")]
        self.assertEqual(len(times), 5)  # The initial value, then one every 2 s.
        gaps = {round((b - a).total_seconds(), 3) for a, b in zip(times, times[1:])}
        self.assertEqual(gaps, {2.0})

    def test_sub_second_tests_keep_their_cadence_and_bad_values_are_refused(self):
        fast = SimulatedProvider(minimum_interval_ms=100)
        self.assertEqual(fast.interval_granularity_ms, 100)
        self.assertEqual(Core(fast).register_consumer().subscribe(
            "aircraft.ground_speed", interval_ms=250).interval_ms, 300)
        self.assertEqual(SimulatedProvider(interval_granularity_ms=500).interval_granularity_ms, 500)
        for bad in (0, 1.5, True, 3_600_001):
            with self.assertRaises(ValueError):
                SimulatedProvider(interval_granularity_ms=bad)
        for bad in (0, -1, float("nan"), 601):
            with self.assertRaises(ValueError):
                SimulatedProvider(max_command_timeout_seconds=bad)


class ComFrequencyDecodeTests(TestCase):
    def test_frequencies_round_to_the_nearest_hz_never_truncate(self):
        self.assertEqual(decode_frequency_hz(128_009_999.9999), 128_010_000)
        self.assertEqual(decode_frequency_hz(128_009_999.99999999), 128_010_000)
        self.assertEqual(decode_frequency_hz(128_010_000.4), 128_010_000)
        self.assertEqual(decode_frequency_hz(128_009_999.4), 128_009_999)
        self.assertEqual(decode_frequency_hz(118_100_000), 118_100_000)
        for bad in (float("nan"), float("inf"), "128.010", None, True):
            with self.assertRaises(ValueError):
                decode_frequency_hz(bad)

    def test_core_reports_a_float64_reading_as_whole_hz(self):
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock, periodic=False)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        active = consumer.subscribe("radio.com1.active_frequency")
        core.connect()
        core.pump()
        provider.publish(COM1_ACTIVE, 128_009_999.99999999)
        core.pump()
        observation = consumer.latest(active.id)
        self.assertIs(observation.quality, Quality.CURRENT)
        self.assertEqual(observation.value, 128_010_000)
        self.assertIs(type(observation.value), int)
        provider.publish(COM1_ACTIVE, float("nan"))
        core.pump()
        self.assertIs(consumer.latest(active.id).quality, Quality.UNAVAILABLE)


class ComSpacingParityTests(AdapterClock, TestCase):
    """One rule (catalog.com_channel_problem) decides, wherever the spacing is known."""

    TABLE = (
        # (frequency Hz, spacing, valid)
        (118_000_000, 0, True), (118_025_000, 0, True), (118_005_000, 0, False), (122_805_000, 0, False),
        (118_005_000, 1, True), (122_805_000, 1, True), (118_020_000, 1, False), (118_020_000, 0, False),
        (118_005_000, None, True), (118_020_000, None, False), (137_000_000, 1, False), (118_001_000, 1, False),
    )

    def setUp(self):
        self.start_clock()  # The live adapter runs on the shared fake clock (A139).

    def simulated(self, spacing):
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock, periodic=False)
        if spacing is not None:
            provider.set_value(COM1_SPACING, spacing)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        radio = core.register_consumer(control_groups=("radios",))
        core.connect()
        core.pump()
        return provider, core, radio

    def test_the_catalog_rule_matches_the_table(self):
        for hz, spacing, valid in self.TABLE:
            with self.subTest(hz=hz, spacing=spacing):
                self.assertIs(valid_com_channel(hz, spacing), valid)
                self.assertIs(com_channel_problem(hz, spacing) is None, valid)

    def test_simulated_provider_rejects_before_sending_and_never_blocks_the_session(self):
        for hz, spacing, valid in self.TABLE:
            with self.subTest(hz=hz, spacing=spacing):
                provider, core, radio = self.simulated(0 if spacing is None else spacing)
                if spacing is None:
                    provider.set_value(COM1_SPACING, None)
                standby = radio.subscribe("radio.com1.standby_frequency")
                tune = radio.submit_command("radio.com1.set_standby", hz)
                core.pump()
                core.pump()
                state = radio.command_result(tune.id).state
                self.assertIs(state, CommandState.CONFIRMED if valid else CommandState.REJECTED)
                if valid:
                    self.assertEqual(radio.latest(standby.id).value, hz)
                swap = radio.submit_command("radio.com1.swap")
                self.assertIs(swap.state, CommandState.ACCEPTED)

    def test_core_checks_a_known_spacing_before_queueing(self):
        provider, core, radio = self.simulated(0)
        radio.subscribe("radio.com1.spacing_mode")
        core.pump()
        sent = []
        original = provider.send
        provider.send = lambda command: (sent.append(command), original(command))
        tune = radio.submit_command("radio.com1.set_standby", 118_005_000)
        self.assertIs(tune.state, CommandState.REJECTED)
        self.assertIn("25 kHz channel spacing", tune.detail)
        core.pump()
        self.assertEqual(sent, [])
        # After the pilot switches the radio to 8.33 kHz, the same channel is accepted.
        provider.publish(COM1_SPACING, 1)
        core.pump()
        tune = radio.submit_command("radio.com1.set_standby", 118_005_000)
        self.assertIs(tune.state, CommandState.ACCEPTED)
        core.pump()
        core.pump()
        self.assertIs(radio.command_result(tune.id).state, CommandState.CONFIRMED)

    def test_core_gives_the_same_answer_for_the_live_adapter_once_spacing_is_read(self):
        """With a spacing reading, Core rejects off-grid channels before either provider sends."""
        from struct import pack
        from bbc_core.simconnect_abi import Period

        for spacing in (0, 1):
            sdk = FakeSdk()
            core = Core(SimConnectProvider(sdk))
            self.addCleanup(core.disconnect)
            radio = core.register_consumer(control_groups=("radios",))
            subscription = radio.subscribe("radio.com1.spacing_mode")
            core.connect()
            core.pump()
            definitions = {definition for definition, fields in sdk.definitions.items()
                           if any(name == b"COM SPACING MODE:1" for name, *_ in fields)}
            (request,) = [args for args, _ in sdk.matching_calls("SimConnect_RequestDataOnSimObject")
                          if args[1] in definitions and args[3] == Period.SECOND]
            sdk.data(request[0], pack("<i", spacing), definition=request[1])
            core.pump()
            self.assertEqual(radio.latest(subscription.id).value, spacing)
            _, sim_core, sim_radio = self.simulated(spacing)
            sim_radio.subscribe("radio.com1.spacing_mode")
            sim_core.pump()
            for hz, table_spacing, valid in self.TABLE:
                if table_spacing != spacing:
                    continue
                with self.subTest(hz=hz, spacing=spacing):
                    live = radio.submit_command("radio.com1.set_standby", hz)
                    simulated = sim_radio.submit_command("radio.com1.set_standby", hz)
                    self.assertIs(live.state is CommandState.REJECTED, not valid)
                    self.assertEqual((live.state, live.detail), (simulated.state, simulated.detail))


class SignalErrorShapeTests(TestCase):
    def test_a_signal_error_is_not_data_loss(self):
        error = SignalError(1, "The provider reported an error.", "aircraft.title", "sub-1")
        self.assertNotIsInstance(error, Gap)
        self.assertFalse(error.is_data_loss)
        self.assertTrue(Gap(2, "Consumer update queue lost data.").is_data_loss)
        self.assertEqual((error.dropped_updates, error.reason), (1, "The provider reported an error."))
        self.assertIn(SignalError, typing.get_args(Update))


class ControlGroupTests(TestCase):
    def test_known_control_groups_are_exactly_what_registration_accepts(self):
        groups = known_control_groups()
        self.assertTrue({"radios", "aircraft_inputs", "client_data", "simvar_writes"} <= groups)
        core = Core(SimulatedProvider())
        core.register_consumer(control_groups=tuple(groups))
        with self.assertRaises(Exception):
            core.register_consumer(control_groups=("not-a-group",))


class FlightTapeValidationTests(TestCase):
    def test_bad_action_values_fail_when_the_tape_is_built(self):
        bad_rows = (
            (TapeAction.RATE, 0), (TapeAction.RATE, 129), (TapeAction.RATE, float("nan")), (TapeAction.RATE, "4"),
            (TapeAction.PAUSE, 1.5), (TapeAction.PAUSE, "4"), (TapeAction.PAUSE, -1), (TapeAction.PAUSE, True),
            (TapeAction.GAP, 0), (TapeAction.GAP, 2.5), (TapeAction.GAP, "3"), (TapeAction.DISCONNECT, 7),
        )
        for action, value in bad_rows:
            with self.subTest(action=action, value=value):
                with self.assertRaisesRegex(ValueError, "entry 1 is invalid"):
                    FlightTape([(0, "aircraft.on_ground", True), (1, action, value)])
        with self.assertRaisesRegex(ValueError, "entry 0 is invalid: unknown capability"):
            FlightTape([(0, "aircraft.no_such_thing", 1)])
        with self.assertRaisesRegex(ValueError, "line 1 is invalid"):
            FlightTape.from_jsonl(['{"t": 0, "action": "rate", "value": 500}'])

    def test_good_action_values_are_accepted(self):
        tape = FlightTape([(0, TapeAction.RATE, 4), (1, TapeAction.PAUSE, 4), (2, TapeAction.GAP, None),
                           (3, TapeAction.GAP, 3), (4, TapeAction.DISCONNECT, None), (5, TapeAction.DISCONNECT, "x")])
        self.assertEqual(len(tape.entries), 6)


class ChangedOnlySnapshotTests(TestCase):
    def test_changed_only_snapshots_report_no_simulator_clock(self):
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock, wall_clock=clock.wall)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        vs = SimVar("VERTICAL SPEED", "feet per minute", ValueType.NUMBER)
        provider.set_value(vs, 0.0)
        changed = consumer.subscribe_snapshot((vs,), changed_only=True)
        every = consumer.subscribe_snapshot((vs,))
        core.connect()
        (updates,) = drive(core, clock, 2, consumers=(consumer,))
        mine = [u for u in updates if isinstance(u, Snapshot) and u.subscription_id == changed.id]
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0].simulation_time_s, mine[0].absolute_time_s, mine[0].sample_time), (None, None, None))
        full = [u for u in updates if isinstance(u, Snapshot) and u.subscription_id == every.id]
        self.assertIsNotNone(full[0].sample_time)
