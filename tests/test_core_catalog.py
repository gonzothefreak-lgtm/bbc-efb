"""Catalog coverage for the Recorder, pause decoding, SDK name rules and enum formatting."""

import json
from enum import Enum
from unittest import TestCase

import bbc_core
from bbc_core import (
    ClientDataArea, Core, CoreRequestError, Event, Kind, SimVar, ValueType,
)
from bbc_core import catalog, client_data, contract, facilities, provider, sdk_simvar, simulator_ops
from bbc_core.catalog import READS
from bbc_core.contract import PauseState, StrEnum
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simulated import SimulatedProvider
from bbc_core.simulator_ops import SimulatorOperation, flight_path, operation_arguments

from test_core_support import FakeClock

RECORDER_IDS = (
    "aircraft.altitude_msl", "aircraft.altitude_agl", "aircraft.indicated_airspeed", "aircraft.vertical_speed",
    "aircraft.heading_true", "aircraft.heading_magnetic", "aircraft.pitch", "aircraft.bank",
    "aircraft.parking_brake", "aircraft.engine1.combustion", "aircraft.engine4.combustion",
    "aircraft.fuel_total_quantity", "aircraft.gear.handle_down", "aircraft.gear.center_on_ground",
    "aircraft.flaps.handle_index", "aircraft.touchdown.normal_velocity", "sim.rate", "sim.simulation_time",
    "sim.zulu_time", "sim.absolute_time", "aircraft.atc_id", "aircraft.atc_model", "aircraft.atc_type",
    "aircraft.acceleration_body_x", "aircraft.acceleration_body_y", "aircraft.acceleration_body_z",
    "radio.com1.spacing_mode",
)


class _StubSdk:
    simulated = True


class RecorderCatalogTests(TestCase):
    def test_recorder_reads_use_documented_names_and_both_providers_accept_them(self):
        live = SimConnectProvider(_StubSdk())
        simulated = SimulatedProvider()
        for capability_id in RECORDER_IDS:
            entry = READS[capability_id]
            self.assertIs(entry.capability.kind, Kind.VALUE, capability_id)
            self.assertIn("not yet validated", entry.capability.description, capability_id)
            self.assertTrue(live.supports_signal(entry.signal), capability_id)
            self.assertTrue(simulated.supports_signal(entry.signal), capability_id)
        self.assertEqual(READS["aircraft.vertical_speed"].signal.unit, "feet per minute")
        self.assertEqual(READS["aircraft.touchdown.normal_velocity"].signal.name, "PLANE TOUCHDOWN NORMAL VELOCITY")
        self.assertEqual(READS["aircraft.engine2.combustion"].signal.name, "GENERAL ENG COMBUSTION:2")
        self.assertIn("earlier landing", READS["aircraft.touchdown.normal_velocity"].capability.description)

    def test_named_and_sdk_addresses_share_one_signal(self):
        named = READS["aircraft.vertical_speed"].signal
        clock = FakeClock()
        core = Core(SimulatedProvider(clock=clock))
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        consumer.subscribe("aircraft.vertical_speed")
        consumer.subscribe_sdk_simvar(SimVar("vertical speed", "Feet per minute", ValueType.NUMBER))
        core.connect()
        core.pump()
        self.assertEqual([item.signal for item in core._provider.active_demand], [named])


class PauseStateTests(TestCase):
    def test_pause_flags_decode_to_a_typed_int(self):
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer()
        consumer.subscribe("sim.pause_state")
        core.connect()
        core.pump()
        for flags in (1, 4, 8, 12, 0, 64):
            provider.publish("sim.pause_state", flags)
        core.pump()
        values = [u.value for u in consumer.poll() if isinstance(u, Event)]
        self.assertEqual(values, [1, 4, 8, 12, 0, 64])
        self.assertTrue(all(isinstance(value, PauseState) for value in values))
        full, active, sim, both, none, unknown = values
        self.assertEqual((full.full_pause, full.active_pause, full.sim_pause), (True, False, False))
        self.assertEqual((active.full_pause, active.active_pause, active.paused), (False, True, True))
        self.assertTrue(sim.sim_pause and both.active_pause and both.sim_pause)
        self.assertFalse(none.paused)
        self.assertEqual(int(unknown), 64)
        self.assertEqual((f"{both}", str(both), json.dumps(both)), ("12", "12", "12"))


class SdkNameTests(TestCase):
    def test_zero_based_gear_and_contact_point_indices_are_accepted(self):
        for name in ("GEAR IS ON GROUND", "CONTACT POINT COMPRESSION", "WHEEL RPM"):
            self.assertEqual(SimVar(name, "number", ValueType.NUMBER, index=0).sdk_name, f"{name}:0")
        with self.assertRaises(CoreRequestError):
            SimVar("GENERAL ENG COMBUSTION", "bool", ValueType.BOOLEAN, index=0)

    def test_dotted_custom_events_are_kept_exactly(self):
        clock = FakeClock()
        core = Core(SimulatedProvider(clock=clock))
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer(control_groups=("client_events",))
        core.connect()
        self.assertEqual(sdk_simvar.client_event_name("A32NX.FCU_HDG_INC"), "A32NX.FCU_HDG_INC")
        self.assertEqual(sdk_simvar.client_event_name("parking_brakes"), "PARKING_BRAKES")
        for bad in ("K:PARKING_BRAKES", "A.B.C", ".X", "X."):
            with self.assertRaises(CoreRequestError):
                sdk_simvar.client_event_name(bad)
        self.assertEqual(consumer.submit_client_event("Custom.Event").state.value, "accepted")

    def test_flight_plan_paths_accept_an_optional_extension(self):
        self.assertEqual(flight_path(r"C:\Plans\route", ".pln"), rb"C:\Plans\route.pln")
        self.assertEqual(flight_path(r"C:\Plans\route.PLN", ".pln", send_extension=False), rb"C:\Plans\route")
        self.assertEqual(operation_arguments(SimulatorOperation.LOAD_FLIGHT_PLAN, (r"D:\a\b",)), (rb"D:\a\b.pln",))
        for bad in (r"C:\Plans\route.flt", r"route.pln", r"C:\Plans\..\x.pln", r"C:\Plans\.pln"):
            with self.assertRaises(CoreRequestError):
                flight_path(bad, ".pln")

    def test_client_data_names_keep_their_published_case_by_default(self):
        """A76: the owner's spelling is the default; upper-casing is an explicit legacy option."""
        exact = ClientDataArea("PMDG_NG3_Data", 8)
        self.assertEqual(exact.name, "PMDG_NG3_Data")
        self.assertEqual(ClientDataArea("  PMDG_NG3_Data ", 8).name, "PMDG_NG3_Data")
        self.assertEqual(ClientDataArea("external", 8, preserve_case=False).name, "EXTERNAL")
        self.assertEqual(ClientDataArea("PMDG_NG3_Data", 8, preserve_case=True), exact)

        class ClientDataSimulated(SimulatedProvider):
            def request_client_data_value(self, signal):
                pass

        core = Core(ClientDataSimulated())
        core.register_client_data(exact)
        core.register_client_data(ClientDataArea("PMDG_NG3_Data", 8, preserve_case=True))
        with self.assertRaises(CoreRequestError):
            core.register_client_data(ClientDataArea("pmdg_ng3_data", 4, preserve_case=True))


class FormattingTests(TestCase):
    def test_every_core_text_enum_formats_as_its_value(self):
        modules = (contract, provider, catalog, client_data, facilities, sdk_simvar, simulator_ops)
        enums = [item for module in modules for item in vars(module).values()
                 if isinstance(item, type) and issubclass(item, Enum) and issubclass(item, str)
                 and item is not StrEnum]
        self.assertGreater(len(enums), 15)
        for enum in enums:
            self.assertTrue(issubclass(enum, StrEnum), enum.__name__)
            for member in enum:
                self.assertEqual((str(member), f"{member}", format(member, "")), (member.value,) * 3)
                self.assertIs(enum(f"{member}"), member)

    def test_public_package_exports_the_new_types(self):
        for name in ("PauseState", "Snapshot", "SnapshotPeriod", "SignalError", "SubscriptionEnded", "StrEnum"):
            self.assertIn(name, bbc_core.__all__)
            self.assertTrue(hasattr(bbc_core, name))
