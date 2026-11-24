"""Round-4 adapter regressions.

Lifecycle ids shared with the input helper (ObjectRemoved after a CREATE_*),
asynchronous refusal of a single SimVar write definition, retries of system-event
registrations (current Demand, lifecycle id kept), FLOAT64 COM subscriptions
(A75), the remaining untested session-integrity paths (A36), the optional SDK
protocol and unused imports (A89).
"""

import ast
from pathlib import Path
from struct import pack
from unittest import TestCase

from bbc_core import CommandState, ConnectionState, Core, InputCatalogState, SimVar, ValueType
from bbc_core.aircraft_inputs import INPUT_WRITE
from bbc_core.catalog import COM1_ACTIVE, SYSTEM_EVENTS
from bbc_core.contract import InputCatalog, InputId
from bbc_core.provider import Command, CommandRejected, Demand, Disconnect, ProviderError
from bbc_core.simconnect import SimConnectProvider, _OptionalSdk
from bbc_core.simconnect_abi import (
    DataType, NativeInitialPosition, ReceiveEventObject, ReceiveException, ReceiveId,
)
from bbc_core.simconnect_ledger import PacketLedger
from bbc_core.simconnect_sdk import SimConnectSdk
from test_adapter_support import FakeClock
from test_aircraft_inputs_offline import INPUT_HASH, FakeInputSdk
from test_sdk_simvar_offline import GenericSdk
from test_simconnect_provider_offline import FakeSdk, packet


ROOT = Path(__file__).resolve().parents[1]


def create_command(id="c1"):
    return Command(id, "s", "sdk.simulator.create_simulated_object",
                   (b"Some Object", bytes(NativeInitialPosition())), consumer_id="module")


class ProviderTestCase(FakeClock, TestCase):
    sdk_class = FakeSdk

    def setUp(self):
        self.start_clock(100.0)
        self.sdk = self.sdk_class()
        self.provider = SimConnectProvider(self.sdk)
        self.provider.connect("s")
        self.addCleanup(self.provider.disconnect)
        self.provider.drain(64)

    def subscriptions(self, name):
        return [args[0] for args, _ in self.sdk.matching_calls("SimConnect_SubscribeToSystemEvent")
                if args[1] == name]


class ObjectRemovedTests(ProviderTestCase):
    sdk_class = FakeInputSdk

    def catalog_states(self):
        return [update.state for update in self.provider.drain(64) if isinstance(update, InputCatalog)]

    def test_unrelated_object_removed_after_a_create_leaves_the_catalog_ready(self):
        self.provider.request_input_catalog()
        request = self.sdk.matching_calls("SimConnect_EnumerateInputEvents")[-1][0][0]
        self.sdk.page(request, 0, 1, (b"TestControl", INPUT_HASH, DataType.FLOAT64))
        self.assertIn(InputCatalogState.READY, self.catalog_states())
        self.provider.send(create_command())
        removed = self.provider._system_event_ids[SYSTEM_EVENTS["OBJECTREMOVED"]]
        self.assertNotIn(removed, self.provider._inputs._lifecycle_ids)
        self.sdk.incoming.append(packet(ReceiveEventObject, ReceiveId.EVENT_OBJECT,
                                        event_id=removed, data=999, object_type=1))
        self.assertEqual(self.catalog_states(), [])
        self.assertTrue(self.provider._inputs._catalog)

    def test_create_before_discovery_does_not_make_object_removed_an_aircraft_change(self):
        self.provider.send(create_command())
        self.provider.request_input_catalog()
        self.catalog_states()
        removed = self.provider._system_event_ids[SYSTEM_EVENTS["OBJECTREMOVED"]]
        self.assertFalse(self.provider._inputs.aircraft_changed(removed))
        loaded = self.provider._system_event_ids[SYSTEM_EVENTS["AIRCRAFTLOADED"]]
        self.assertTrue(self.provider._inputs.aircraft_changed(loaded))


class AsyncRefusalSdk(GenericSdk):
    """MSFS reports an unknown SimVar name as an EXCEPTION for AddToDataDefinition."""

    def call(self, name, handle, *args):
        super().call(name, handle, *args)
        if name == "SimConnect_AddToDataDefinition" and args[1] == b"TYPO SIMVAR":
            self.exception(self.packet_id)
        if name == "SimConnect_SetDataOnSimObject" and self.definitions[args[0]][0][0] == b"TYPO SIMVAR":
            self.exception(self.packet_id)


class AsyncSimVarRefusalTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100.0)
        self.sdk = AsyncRefusalSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.pilot = self.core.register_consumer(control_groups=("simvar_writes",))
        self.core.connect()

    def complete(self, command):
        for _ in range(3):
            self.core.pump()
        return self.pilot.command_result(command.id)

    def test_refused_single_field_definition_rejects_and_later_writes_still_work(self):
        typo = SimVar("Typo SimVar", "number", ValueType.NUMBER)
        result = self.complete(self.pilot.submit_simvar(typo, 1.0))
        self.assertIs(result.state, CommandState.REJECTED)
        self.assertIn("refused the SimVar definition", result.detail)
        light = SimVar("Light Potentiometer", "Percent", ValueType.NUMBER, 2)
        self.sdk.values[b"LIGHT POTENTIOMETER:2"] = pack("<d", 25.0)
        self.assertIs(self.complete(self.pilot.submit_simvar(light, 10.0)).state, CommandState.CONFIRMED)
        # The refusal is cached, so the same name is rejected before sending.
        sets = len(self.sdk.matching_calls("SimConnect_SetDataOnSimObject"))
        self.assertIs(self.complete(self.pilot.submit_simvar(typo, 2.0)).state, CommandState.REJECTED)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_SetDataOnSimObject")), sets)
        self.assertIs(self.pilot.connection().state, ConnectionState.CONNECTED)


class EventRegistrationRetryTests(ProviderTestCase):
    def refuse_last_subscription(self):
        self.sdk.exception(self.sdk.matching_calls("SimConnect_SubscribeToSystemEvent")[-1][1])
        self.provider.drain(64)

    def test_retry_applies_the_latest_event_enabled(self):
        crashed = SYSTEM_EVENTS["CRASHED"]
        self.provider.replace_demand((Demand(crashed, None, event_enabled=False),))
        self.refuse_last_subscription()
        self.assertIn(self.provider._registrations[crashed].id, self.provider._failed)
        self.provider.replace_demand((Demand(crashed, None, event_enabled=True),))
        self.assertTrue(self.provider._registrations[crashed].demand.event_enabled)
        self.now += 5
        self.provider.drain(64)
        registration = self.provider._registrations[crashed]
        self.assertNotIn(registration.id, self.provider._failed)
        self.assertNotIn(registration.id, self.provider._disabled_events)
        self.assertIs(self.provider._by_id[registration.id], registration)
        states = [args for args, _ in self.sdk.matching_calls("SimConnect_SetSystemEventState")]
        self.assertNotIn((registration.id, 0), states)

    def test_retry_reuses_the_retained_lifecycle_subscription(self):
        loaded = SYSTEM_EVENTS["AIRCRAFTLOADED"]
        self.provider.replace_demand((Demand(loaded, None),))
        self.refuse_last_subscription()
        self.provider._retain_aircraft_lifecycle()
        lifecycle = self.provider._system_event_ids[loaded]
        self.now += 5
        self.provider.drain(64)
        self.assertEqual(len(self.subscriptions(b"AircraftLoaded")), 2)  # refused + lifecycle
        self.assertEqual(self.sdk.matching_calls("SimConnect_UnsubscribeFromSystemEvent"), [])
        self.assertEqual(self.provider._registrations[loaded].id, lifecycle)
        self.assertEqual(self.provider._system_event_ids[loaded], lifecycle)

    def test_dropping_a_demand_that_shares_a_lifecycle_id_keeps_it_mapped(self):
        flight = SYSTEM_EVENTS["FLIGHTLOADED"]
        self.provider.replace_demand((Demand(flight, None),))
        self.refuse_last_subscription()
        self.provider._retain_aircraft_lifecycle()
        lifecycle = self.provider._system_event_ids[flight]
        self.now += 5
        self.provider.drain(64)
        self.provider.replace_demand(())
        self.assertEqual(self.provider._system_event_ids.get(flight), lifecycle)
        self.assertEqual(self.sdk.matching_calls("SimConnect_UnsubscribeFromSystemEvent"), [])
        self.assertEqual(len(self.subscriptions(b"FlightLoaded")), 2)


class FrequencySubscriptionTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100.0)
        self.sdk = FakeSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.reader = self.core.register_consumer()
        self.core.connect()

    def test_com_frequency_subscriptions_read_float64_and_round_to_whole_hz(self):
        active = self.reader.subscribe("radio.com1.active_frequency")
        self.core.pump()
        (define, _), = [call for call in self.sdk.matching_calls("SimConnect_AddToDataDefinition")
                        if call[0][1] == b"COM ACTIVE FREQUENCY:1"]
        self.assertIs(define[3], DataType.FLOAT64)
        self.sdk.data(define[0], pack("<d", 128_009_999.75))
        self.core.pump()
        value = self.reader.latest(active.id).value
        self.assertEqual(value, 128_010_000)
        self.assertIs(type(value), int)

    def test_snapshot_com_field_is_rounded(self):
        self.assertEqual(SimConnectProvider._snapshot_value(COM1_ACTIVE, 121_499_999.6), 121_500_000)
        self.assertIsNone(SimConnectProvider._snapshot_value(COM1_ACTIVE, float("nan")))


class SessionIntegrityPathTests(ProviderTestCase):
    """A36 paths 4, 6, 7, 8 and 10."""

    def test_send_preparation_provider_error_ends_the_session(self):
        def refuse(name, handle, *args, original=self.sdk.call):
            if name == "SimConnect_SubscribeToSystemEvent" and args[1] == b"ObjectRemoved":
                raise ProviderError("boom")
            original(name, handle, *args)
        self.sdk.call = refuse
        with self.assertRaises(CommandRejected):
            self.provider.send(create_command())
        self.assertFalse(self.provider._ready)
        disconnects = [update for _, update in self.provider._control if isinstance(update, Disconnect)]
        self.assertEqual(len(disconnects), 1)
        self.assertIn("preparation failed", disconnects[0].reason)

    def test_session_scoped_write_drain_refuses_after_quit_or_backlog(self):
        command = Command("w", "s", INPUT_WRITE, (1.0,), input_id=InputId("s", 1, 5))
        self.sdk.quit()
        with self.assertRaisesRegex(CommandRejected, "session closed"):
            self.provider.send(command)
        self.assertEqual(self.sdk.matching_calls("SimConnect_SetInputEvent"), [])

    def test_session_scoped_write_drain_refuses_a_backlog(self):
        for _ in range(300):
            self.sdk.event(9999, 0)
        command = Command("w", "s", INPUT_WRITE, (1.0,), input_id=InputId("s", 1, 5))
        with self.assertRaisesRegex(CommandRejected, "backlog"):
            self.provider.send(command)
        self.assertTrue(self.provider._ready)
        self.assertEqual(self.sdk.matching_calls("SimConnect_SetInputEvent"), [])

    def test_invalid_packet_byte_count_raises(self):
        self.sdk.incoming.append(b"\x08\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00")
        with self.assertRaisesRegex(ProviderError, "byte count"):
            self.provider.drain(64)

    def test_packet_ledger_prunes_by_age(self):
        ledger = PacketLedger(window_s=10.0)
        ledger[1] = "old"
        self.now += 5
        ledger[2] = "newer"
        self.now += 6
        ledger[3] = "newest"
        self.assertNotIn(1, ledger)
        self.assertEqual((ledger.get(2), ledger.get(3)), ("newer", "newest"))
        self.assertEqual(len(ledger), 2)
        with self.assertRaises(ValueError):
            PacketLedger(window_s=0)


class RadioDefinitionExceptionTests(ProviderTestCase):
    """A36 path 6: exceptions on the COM1 baseline definition, baseline packet and readback."""

    def radio(self, frequency=121_800_000):
        return Command("r", "s", "COM_STBY_RADIO_SET_HZ", (frequency,))

    def test_baseline_packet_exception_rejects_before_transmission(self):
        original = self.sdk.call

        def refuse(name, handle, *args):
            original(name, handle, *args)
            if name == "SimConnect_RequestDataOnSimObject" and len(self.sdk.definitions[args[1]]) == 2:
                self.sdk.incoming.clear()
                self.sdk.exception(self.sdk.packet_id)
        self.sdk.call = refuse
        with self.assertRaises(CommandRejected):
            self.provider.send(self.radio())
        self.assertEqual(self.sdk.matching_calls("SimConnect_TransmitClientEvent"), [])
        self.assertTrue(self.provider._ready)

    def test_baseline_definition_exception_rejects_before_transmission(self):
        original = self.sdk.call

        def refuse(name, handle, *args):
            original(name, handle, *args)
            if name == "SimConnect_AddToDataDefinition" and args[1] == b"COM ACTIVE FREQUENCY:1":
                self.sdk.exception(self.sdk.packet_id)
            if name == "SimConnect_RequestDataOnSimObject" and len(self.sdk.definitions[args[1]]) == 2:
                self.sdk.incoming.clear()
                self.sdk.exception(self.sdk.packet_id - 3)  # the COM ACTIVE definition packet
        self.sdk.call = refuse
        with self.assertRaises(CommandRejected):
            self.provider.send(self.radio())
        self.assertEqual(self.sdk.matching_calls("SimConnect_TransmitClientEvent"), [])

    def test_late_multi_field_definition_exception_is_uncertain(self):
        self.provider.send(self.radio())
        pending = self.provider._pending
        packet_id = next(packet for packet, definition in self.provider._write_definition_packets._entries.items()
                         if definition[1] == pending.readback.definition)
        self.sdk.incoming.clear()
        self.sdk.exception(packet_id)
        updates = self.provider.drain(64)
        self.assertIsNone(self.provider._pending)
        outcomes = [update for update in updates if getattr(update, "command_id", None) == "r"]
        self.assertEqual(outcomes[-1].outcome.value, "uncertain")


class OpenExceptionTests(FakeClock, TestCase):
    """A36 path 9: an EXCEPTION during OPEN fails the connect and leaves no handle."""

    def test_exception_during_open(self):
        self.start_clock(100.0)
        sdk = FakeSdk()
        sdk.start = [packet(ReceiveException, ReceiveId.EXCEPTION, exception=7, send_id=1, index=0)]
        core = Core(SimConnectProvider(sdk))
        self.addCleanup(core.disconnect)
        status = core.connect()
        self.assertIs(status.state, ConnectionState.ERROR)
        self.assertIn("exception 7", status.detail)
        self.assertIsNone(sdk.handle)


class SdkProtocolAndImportTests(TestCase):
    """A89: the optional SDK protocol matches SimConnectSdk; no unused imports in the adapter."""

    def test_simconnect_sdk_implements_every_optional_feature(self):
        sdk = SimConnectSdk(Path.cwd() / "SimConnect.dll")  # Nothing is loaded until open().
        self.assertIsInstance(sdk, _OptionalSdk)
        self.assertNotIsInstance(FakeSdk(), _OptionalSdk)

    def test_adapter_modules_have_no_unused_imports_and_one_annotation_mode(self):
        for path in sorted((ROOT / "bbc_core").glob("simconnect*.py")):
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                self.assertIn("from __future__ import annotations", source)
                imported = {}
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                            continue
                        for alias in node.names:
                            name = (alias.asname or alias.name).split(".")[0]
                            imported[name] = node.lineno
                used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                used |= {node.value.id for node in ast.walk(tree)
                         if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)}
                exported = set()
                for node in tree.body:
                    if isinstance(node, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
                        exported = {element.value for element in node.value.elts}
                # Names used only inside string annotations count as used.
                text_names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        text_names.update(part for part in node.value.replace("|", " ").split())
                unused = sorted(name for name in imported
                                if name not in used and name not in exported and name not in text_names)
                self.assertEqual(unused, [], f"unused imports in {path.name}")
