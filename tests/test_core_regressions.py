"""Regressions for Core and the simulated provider found in the verification round."""

from unittest import TestCase

from bbc_core import ConnectionState, Core, Event, Observation, Quality
from bbc_core.catalog import READS
from bbc_core.contract import Connection, SignalError
from bbc_core.simulated import FlightTape, SimulatedProvider

from test_core_support import FakeClock


class ResubscribeBetweenPumpsTests(TestCase):
    """Unsubscribing and subscribing the same signal again before a pump must not lose it."""

    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.api = self.core.register_consumer()
        self.core.connect()

    def pump_for(self, seconds):
        for _ in range(int(seconds // 2)):
            self.clock.advance(2)
            self.core.pump()

    def test_system_state_resubscribed_before_a_pump_is_refreshed(self):
        first = self.api.subscribe("sim.loaded_aircraft_file")
        self.core.pump()
        self.assertIs(self.api.latest(first.id).quality, Quality.CURRENT)
        self.api.unsubscribe(first.id)
        second = self.api.subscribe("sim.loaded_aircraft_file")
        self.pump_for(10)
        observation = self.api.latest(second.id)
        self.assertIs(observation.quality, Quality.CURRENT)
        self.assertTrue(observation.value.endswith("aircraft.cfg"))

    def test_handover_between_modules_keeps_the_value(self):
        a = self.core.register_consumer()
        b = self.core.register_consumer()
        old = a.subscribe("sim.loaded_aircraft_file")
        self.core.pump()
        a.unsubscribe(old.id)
        new = b.subscribe("sim.loaded_aircraft_file")
        self.pump_for(4)
        self.assertIs(b.latest(new.id).quality, Quality.CURRENT)

    def test_reading_is_released_once_the_demand_is_retired(self):
        signal = READS["sim.loaded_aircraft_file"].signal
        subscription = self.api.subscribe("sim.loaded_aircraft_file")
        self.core.pump()
        self.api.unsubscribe(subscription.id)
        self.core.pump()
        self.assertNotIn(signal, self.core._readings)
        self.assertFalse(self.core._retiring)
        # A later subscriber starts fresh and still gets the value.
        again = self.api.subscribe("sim.loaded_aircraft_file")
        self.pump_for(4)
        self.assertIs(self.api.latest(again.id).quality, Quality.CURRENT)

    def test_session_end_clears_retiring_signals(self):
        subscription = self.api.subscribe("sim.loaded_aircraft_file")
        self.core.pump()
        self.api.unsubscribe(subscription.id)
        self.core.disconnect()
        self.assertFalse(self.core._retiring)
        self.assertFalse(self.core._readings)


class PollReserveTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_unreportable_stale_candidates_do_not_shorten_batches(self):
        provider = SimulatedProvider(clock=self.clock)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        api = core.register_consumer()
        core.connect()
        api.subscribe("aircraft.ground_speed")
        api.subscribe("aircraft.latitude")
        core.pump()
        for _ in range(20):
            self.clock.advance(1)
            core.pump()
        self.clock.advance(10)
        sizes = []
        while True:
            batch = api.poll(limit=8)
            sizes.append(len(batch))
            if len(batch) < 8:
                break
        # A short batch means drained: nothing may be left behind.
        self.assertEqual(len(core._consumers[api._consumer_id].data), 0)
        self.assertTrue(all(size == 8 for size in sizes[:-1]))

    def test_limit_one_still_delivers_control_and_data_then_the_stale_report(self):
        provider = SimulatedProvider(clock=self.clock, periodic=False)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        api = core.register_consumer()
        core.connect()
        subscription = api.subscribe("aircraft.ground_speed")
        core.pump()
        core.pump()
        provider.publish("aircraft.ground_speed", 5.0)
        core.pump()
        self.clock.advance(10)
        received = []
        for _ in range(10):
            batch = api.poll(limit=1)
            if not batch:
                break
            self.assertEqual(len(batch), 1)
            received.extend(batch)
        self.assertTrue(any(isinstance(item, Connection) for item in received))
        observations = [item for item in received if isinstance(item, Observation)]
        self.assertEqual([item.value for item in observations], [0.0, 5.0, 5.0])
        self.assertIs(observations[-1].quality, Quality.STALE)
        self.assertEqual(api.poll(limit=1), ())
        self.assertEqual(api.latest(subscription.id).value, 5.0)

    def test_undecodable_stale_candidate_is_not_rearmed_into_the_past(self):
        provider = SimulatedProvider(clock=self.clock, periodic=False)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        api = core.register_consumer()
        core.connect()
        api.subscribe("aircraft.ground_speed")
        core.pump()
        api.poll()
        provider.publish("aircraft.ground_speed", "not a number")
        core.pump()
        api.poll()
        self.clock.advance(10)
        consumer = core._consumers[api._consumer_id]
        self.assertEqual(api.poll(limit=8), ())
        self.assertFalse(consumer.stale_heap)
        # Data arriving afterwards still fills a whole batch.
        provider.publish("aircraft.ground_speed", 1.0)
        core.pump()
        batch = api.poll(limit=8)
        self.assertEqual([item.value for item in batch if isinstance(item, Observation)], [1.0])


class SimulatedProviderRegressionTests(TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.provider = SimulatedProvider(clock=self.clock)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.api = self.core.register_consumer()
        self.core.connect()

    def test_tape_event_row_without_a_value_replays_as_an_event(self):
        subscription = self.api.subscribe("sim.aircraft_crashed")
        self.core.pump()
        self.api.poll()
        self.provider.play(FlightTape.from_jsonl(['{"t": 1, "id": "sim.aircraft_crashed"}']))
        self.clock.advance(2)
        self.core.pump()
        updates = [u for u in self.api.poll() if isinstance(u, (Event, SignalError))]
        self.assertEqual(len(updates), 1)
        self.assertIsInstance(updates[0], Event)
        self.assertEqual(updates[0].capability_id, subscription.capability.id)
        self.assertEqual(updates[0].value, 0)

    def test_transport_loss_with_a_waiting_state_refresh_is_a_disconnect(self):
        subscription = self.api.subscribe("sim.dialog_mode")
        self.core.pump()
        self.api.request_system_state(subscription.id)
        self.provider.lose_connection()
        self.core.pump()
        connection = self.api.connection()
        self.assertIs(connection.state, ConnectionState.DISCONNECTED)
        self.assertEqual(connection.detail, "Simulated transport loss.")


class ModuleHygieneTests(TestCase):
    def test_core_contract_modules_use_postponed_annotations(self):
        # A89: one annotation mode across the Core contract modules.
        import __future__
        import importlib
        for name in ("contract", "provider", "sdk_simvar", "core", "catalog", "client_data", "simulated"):
            module = importlib.import_module(f"bbc_core.{name}")
            self.assertIs(getattr(module, "annotations", None), __future__.annotations, name)

    def test_every_bbc_core_module_uses_postponed_annotations(self):
        # A89 (integration): one annotation mode across the whole bbc_core package.
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "bbc_core"
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                        and any(alias.name == "annotations" for alias in node.names) for node in tree.body)
            self.assertTrue(found, str(path.relative_to(root.parent)))

    def test_offline_adapter_suites_share_the_fake_clock(self):
        # A139: a frozen clock with real sleep can spin the baseline or connect loops.
        from test_adapter_support import FakeClock as SharedClock
        from test_aircraft_inputs_offline import AircraftInputOfflineTests
        from test_client_data_offline import ClientDataOfflineTests
        from test_core_offline import CoreOfflineTests
        from test_integration_cross_file import ClientDataChangedOnlyTests
        from test_sdk_simvar_offline import SdkSimVarOfflineTests
        for case in (AircraftInputOfflineTests, ClientDataOfflineTests, CoreOfflineTests,
                     ClientDataChangedOnlyTests, SdkSimVarOfflineTests):
            self.assertTrue(issubclass(case, SharedClock), case.__name__)


class ClientEventSpellingTests(TestCase):
    def test_custom_event_signal_keeps_its_spelling_and_standard_events_are_upper_case(self):
        from bbc_core.contract import ValueType
        from bbc_core.provider import Signal
        from bbc_core.sdk_simvar import client_event_name
        custom = Signal(client_event_name("Vendor.MyEvent"), None, ValueType.CLIENT_EVENT, event=True,
                        client_event=True)
        standard = Signal(client_event_name("parking_brakes"), None, ValueType.CLIENT_EVENT, event=True,
                          client_event=True)
        self.assertEqual(custom.name, "Vendor.MyEvent")
        self.assertEqual(standard.name, "PARKING_BRAKES")
        self.assertEqual(Signal(" ground velocity ", "Knots", ValueType.NUMBER).name, "GROUND VELOCITY")
