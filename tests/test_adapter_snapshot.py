"""Provider side of grouped snapshots (findings A14, A94, A95)."""

from datetime import datetime, timedelta, timezone
from struct import pack
from unittest import TestCase

from bbc_core import ConnectionState, Core, SimVar, ValueType
from bbc_core.contract import Snapshot, SnapshotPeriod
from bbc_core.provider import Demand, Reading, Signal, SnapshotFrame, SnapshotSpec
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import DATA_REQUEST_FLAG_CHANGED, DataType, Period, ReceiveId, ReceiveSimObjectDataPrefix
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk, packet


LATITUDE = Signal("PLANE LATITUDE", "degrees", ValueType.NUMBER)
ON_GROUND = Signal("SIM ON GROUND", "bool", ValueType.BOOLEAN)
TITLE = Signal("TITLE", None, ValueType.TEXT)
ABSOLUTE = 63_871_000_000.0  # Seconds since 0001-01-01 on the simulator clock.


def snapshot_signal(*fields, period=SnapshotPeriod.SECOND, every=1, changed_only=False):
    return Signal("SNAPSHOT", None, ValueType.SNAPSHOT, snapshot=SnapshotSpec(fields, period, every, changed_only))


class RefusingSdk(FakeSdk):
    refuse = set()

    def call(self, name, handle, *args):
        super().call(name, handle, *args)
        if name == "SimConnect_AddToDataDefinition" and args[1] in self.refuse:
            self.exception(self.packet_id)


class SnapshotProviderTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock()
        self.sdk = RefusingSdk()
        self.provider = SimConnectProvider(self.sdk)
        self.provider.connect("session")
        self.addCleanup(self.provider.disconnect)

    def demand(self, signal):
        self.provider.replace_demand((Demand(signal, None),))

    def deliver(self, request, definition, payload, *, flags=0, count=None):
        self.sdk.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, payload, request_id=request,
            definition_id=definition, flags=flags, definition_count=count,
        ))

    def test_one_definition_one_request_and_one_coherent_reading(self):
        signal = snapshot_signal(LATITUDE, ON_GROUND, TITLE, period=SnapshotPeriod.SIM_FRAME, every=3)
        self.assertTrue(self.provider.supports_signal(signal))
        self.demand(signal)
        definitions = self.sdk.matching_calls("SimConnect_AddToDataDefinition")
        self.assertEqual([args[1] for args, _ in definitions], [
            b"PLANE LATITUDE", b"SIM ON GROUND", b"TITLE", b"SIMULATION TIME", b"ABSOLUTE TIME", b"SIMULATION RATE",
        ])
        self.assertEqual(len({args[0] for args, _ in definitions}), 1)
        self.assertEqual([args[3] for args, _ in definitions], [
            DataType.FLOAT64, DataType.INT32, DataType.STRING256, DataType.FLOAT64, DataType.FLOAT64, DataType.FLOAT64,
        ])
        ((request, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.assertEqual(request[1], definitions[0][0][0])
        self.assertEqual(request[2:7], (0, Period.SIM_FRAME, 0, 0, 2))
        payload = pack("<di256sddd", -33.9, 1, b"Test aircraft", 12.5, ABSOLUTE, 4.0)
        self.deliver(request[0], request[1], payload, count=6)
        (reading,) = [item for item in self.provider.drain(16) if isinstance(item, Reading)]
        self.assertIsNone(reading.error)
        self.assertEqual(reading.value, SnapshotFrame((-33.9, True, "Test aircraft"), 12.5, ABSOLUTE, 4.0))
        self.assertEqual(reading.sample_time, datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ABSOLUTE))

    def test_changed_only_definition_has_no_clock_datums(self):
        # SIMULATION TIME changes every frame; in a CHANGED definition it would make
        # SimConnect send the snapshot every period, defeating changed_only.
        signal = snapshot_signal(LATITUDE, ON_GROUND, period=SnapshotPeriod.SIM_FRAME, changed_only=True)
        self.demand(signal)
        names = [args[1] for args, _ in self.sdk.matching_calls("SimConnect_AddToDataDefinition")]
        self.assertEqual(names, [b"PLANE LATITUDE", b"SIM ON GROUND"])
        for clock in (b"SIMULATION TIME", b"ABSOLUTE TIME", b"SIMULATION RATE"):
            self.assertNotIn(clock, names)
        ((request, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.assertEqual(request[3:5], (Period.SIM_FRAME, DATA_REQUEST_FLAG_CHANGED))
        self.deliver(request[0], request[1], pack("<di", 51.5, 0), flags=DATA_REQUEST_FLAG_CHANGED, count=2)
        (reading,) = [item for item in self.provider.drain(16) if isinstance(item, Reading)]
        self.assertIsNone(reading.error)
        # No simulator clocks are claimed; consumers use the host receipt time.
        self.assertEqual(reading.value, SnapshotFrame((51.5, False), None, None, None))
        self.assertIsNone(reading.sample_time)

    def test_refused_field_is_rebuilt_out_and_reported_as_none(self):
        self.sdk.refuse = {b"SIM ON GROUND"}
        self.demand(snapshot_signal(LATITUDE, ON_GROUND))
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            self.provider.drain(16)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_ClearDataDefinition")), 1)
        request, _ = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")[-1]
        self.assertEqual(request[3], Period.SECOND)
        self.deliver(request[0], request[1], pack("<dddd", 51.5, 1.0, ABSOLUTE, 1.0), count=4)
        (reading,) = [item for item in self.provider.drain(16) if isinstance(item, Reading)]
        self.assertEqual(reading.value.values, (51.5, None))

    def test_malformed_snapshot_packet_is_local_and_never_ends_the_session(self):
        signal = snapshot_signal(LATITUDE)
        self.demand(signal)
        ((request, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.deliver(request[0], request[1], b"\0" * 4, count=4)
        (reading,) = [item for item in self.provider.drain(16) if isinstance(item, Reading)]
        self.assertIn("snapshot", reading.error)
        self.assertTrue(self.provider._ready)

    def test_unsupported_fields_and_options_are_refused(self):
        event = Signal("SIMSTART", None, ValueType.INTEGER, event=True)
        self.assertFalse(self.provider.supports_signal(snapshot_signal(event)))
        self.assertFalse(self.provider.supports_signal(snapshot_signal()))
        self.assertFalse(self.provider.supports_signal(snapshot_signal(LATITUDE, every=0)))

    def test_retire_cancels_and_clears_the_definition(self):
        signal = snapshot_signal(LATITUDE)
        self.demand(signal)
        self.provider.replace_demand(())
        retired, _ = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")[-1]
        self.assertEqual(retired[3], Period.NEVER)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_ClearDataDefinition")), 1)


class SnapshotThroughCoreTests(FakeClock, TestCase):
    def test_consumer_snapshot_round_trip(self):
        self.start_clock()
        sdk = FakeSdk()
        core = Core(SimConnectProvider(sdk))
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer(diagnostics=True)
        fields = (SimVar("PLANE ALTITUDE", "feet", ValueType.NUMBER), SimVar("SIM ON GROUND", "bool", ValueType.BOOLEAN))
        subscription = consumer.subscribe_snapshot(fields, period=SnapshotPeriod.VISUAL_FRAME)
        core.connect()
        core.pump()
        ((request, _),) = sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.assertEqual(request[3], Period.VISUAL_FRAME)
        sdk.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, pack("<diddd", 1200.0, 0, 5.0, ABSOLUTE, 1.0),
            request_id=request[0], definition_id=request[1], definition_count=5,
        ))
        core.pump()
        snapshot = consumer.latest_snapshot(subscription.id)
        self.assertIsInstance(snapshot, Snapshot)
        self.assertEqual(snapshot.values[0], 1200.0)
        self.assertEqual(snapshot.simulation_time_s, 5.0)
        self.assertIs(consumer.connection().state, ConnectionState.CONNECTED)
