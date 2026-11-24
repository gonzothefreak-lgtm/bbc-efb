"""End to end: Core + SimConnectProvider + FakeSdk with snapshots and local errors.

These tests drive the real Core and the real SimConnect adapter together through the
fake SDK used by the adapter tests. They check what a Recorder relies on:

* one ``subscribe_snapshot`` request becomes one multi-datum SimConnect definition and
  one request, shared by every consumer asking for the same fields;
* the snapshot's ``sample_time`` comes from the simulator's ABSOLUTE TIME, not from the
  host clock;
* ``changed_only`` sets SIMCONNECT_DATA_REQUEST_FLAG_CHANGED and leaves the simulator
  clocks out of that definition (they change every frame), so its sample_time is None;
* a SimConnect exception for one signal (or for a packet nobody claims) stays with that
  signal: the shared session stays CONNECTED and other data keeps flowing;
* a refused registration is retried with a fresh definition, and the old one is cleared.
"""

from datetime import datetime, timedelta, timezone
from struct import pack
from unittest import TestCase

from bbc_core import ConnectionState, Core, Quality, SimVar, Snapshot, ValueType
from bbc_core.contract import SnapshotPeriod
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import DATA_REQUEST_FLAG_CHANGED, DataType, Period, ReceiveId, ReceiveSimObjectDataPrefix
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk, packet


ABSOLUTE = 63_871_000_000.0  # Simulator UTC, seconds since 0001-01-01.
ALTITUDE = SimVar("PLANE ALTITUDE", "feet", ValueType.NUMBER)
ON_GROUND = SimVar("SIM ON GROUND", "bool", ValueType.BOOLEAN)
HEADING = ("PLANE HEADING DEGREES TRUE", "degrees", ValueType.NUMBER)


def simulator_time(seconds):
    return datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)


class SnapshotSessionTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock()
        self.sdk = FakeSdk()
        self.provider = SimConnectProvider(self.sdk)
        self.core = Core(self.provider)
        self.addCleanup(self.core.disconnect)
        self.recorder = self.core.register_consumer(diagnostics=True)
        self.snapshot = self.recorder.subscribe_snapshot(
            (ALTITUDE, ON_GROUND), period=SnapshotPeriod.SECOND,
        )
        self.heading = self.recorder.subscribe_simvar(*HEADING)
        self.core.connect()
        self.core.pump()
        self.assertIs(self.recorder.connection().state, ConnectionState.CONNECTED)

    # Helpers ---------------------------------------------------------------------------

    def snapshot_request(self):
        requests = [
            (args, packet_id) for args, packet_id in self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
            if len(self.sdk.definitions.get(args[1], ())) > 1
        ]
        self.assertEqual(len(requests), 1, "expected exactly one snapshot request")
        return requests[0]

    def heading_calls(self, name):
        return [
            (args, packet_id) for args, packet_id in self.sdk.matching_calls(name)
            if name != "SimConnect_AddToDataDefinition" or args[1] == HEADING[0].encode("ascii")
        ]

    def heading_request(self):
        definitions = {args[0] for args, _ in self.heading_calls("SimConnect_AddToDataDefinition")}
        requests = [
            (args, packet_id) for args, packet_id in self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
            if args[1] in definitions and args[3] == Period.SECOND
        ]
        return requests[-1]

    def deliver_snapshot(self, altitude, on_ground, simulation_time, absolute):
        (request, _) = self.snapshot_request()
        self.sdk.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA,
            pack("<diddd", altitude, on_ground, simulation_time, absolute, 1.0),
            request_id=request[0], definition_id=request[1], definition_count=5,
        ))

    def deliver_heading(self, value):
        (request, _) = self.heading_request()
        self.sdk.data(request[0], pack("<d", value), definition=request[1])

    def polled_snapshots(self, consumer=None):
        return [item for item in (consumer or self.recorder).poll(limit=256) if isinstance(item, Snapshot)]

    # Tests -----------------------------------------------------------------------------

    def test_one_multi_datum_definition_with_the_simulator_clocks(self):
        (request, _) = self.snapshot_request()
        fields = self.sdk.definitions[request[1]]
        self.assertEqual([name for name, _, _ in fields], [
            b"PLANE ALTITUDE", b"SIM ON GROUND", b"SIMULATION TIME", b"ABSOLUTE TIME", b"SIMULATION RATE",
        ])
        self.assertEqual([datatype for _, _, datatype in fields], [
            DataType.FLOAT64, DataType.INT32, DataType.FLOAT64, DataType.FLOAT64, DataType.FLOAT64,
        ])
        self.assertEqual(request[3], Period.SECOND)
        self.assertEqual(request[4], 0)
        self.assertEqual(request[6], 0)  # every=1: no skipped periods.

    def test_changed_only_has_the_changed_flag_and_no_clock_fields(self):
        """The clocks change every frame, so a CHANGED definition must not contain them."""
        self.recorder.unsubscribe(self.snapshot.id)
        changed = self.recorder.subscribe_snapshot((ALTITUDE, ON_GROUND), changed_only=True)
        self.core.pump()
        requests = [
            args for args, _ in self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
            if args[4] == DATA_REQUEST_FLAG_CHANGED
        ]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        names = [name for name, _, _ in self.sdk.definitions[request[1]]]
        self.assertEqual(names, [b"PLANE ALTITUDE", b"SIM ON GROUND"])
        self.sdk.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, pack("<di", 900.0, 1),
            request_id=request[0], definition_id=request[1], definition_count=2,
            flags=DATA_REQUEST_FLAG_CHANGED,
        ))
        self.core.pump()
        (snapshot,) = self.polled_snapshots()
        self.assertEqual((snapshot.subscription_id, snapshot.values), (changed.id, (900.0, True)))
        # Honest labelling: no simulator clock was read, so none is reported.
        self.assertEqual((snapshot.simulation_time_s, snapshot.absolute_time_s, snapshot.sample_time),
                         (None, None, None))

    def test_identical_snapshot_specs_share_one_definition(self):
        other = self.core.register_consumer()
        shared = other.subscribe_snapshot((ALTITUDE, ON_GROUND), period=SnapshotPeriod.SECOND)
        self.core.pump()
        self.snapshot_request()  # Still exactly one.
        self.deliver_snapshot(3500.0, 0, 42.0, ABSOLUTE)
        self.core.pump()
        self.assertEqual(len(self.polled_snapshots()), 1)
        (snapshot,) = self.polled_snapshots(other)
        self.assertEqual(snapshot.subscription_id, shared.id)
        self.assertEqual(snapshot.values, (3500.0, False))

    def test_sample_time_is_the_simulator_clock_not_the_host_clock(self):
        # The simulator UTC is deliberately decades away from the host clock, and it
        # crosses UTC midnight between the two deliveries.
        midnight = (simulator_time(ABSOLUTE).replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=1) - simulator_time(0)).total_seconds()
        self.deliver_snapshot(1200.0, 1, 10.0, midnight - 0.5)
        self.now += 1.0
        self.deliver_snapshot(1210.0, 0, 11.0, midnight + 0.5)
        self.core.pump()
        first, second = self.polled_snapshots()
        self.assertEqual(first.sample_time, simulator_time(midnight - 0.5))
        self.assertEqual(second.sample_time, simulator_time(midnight + 0.5))
        self.assertEqual(second.sample_time.date() - first.sample_time.date(), timedelta(days=1))
        self.assertNotEqual(first.sample_time.year, second.received_at.year)
        self.assertEqual((first.values, first.simulation_time_s, first.simulation_rate), ((1200.0, True), 10.0, 1.0))
        self.assertEqual(self.recorder.latest_snapshot(self.snapshot.id), second)

    def test_a_per_signal_exception_does_not_end_the_shared_session(self):
        self.deliver_heading(90.0)
        self.core.pump()
        self.assertEqual(self.recorder.latest(self.heading.id).value, 90.0)
        _, packet_id = self.heading_request()
        self.sdk.exception(packet_id)  # SimConnect refuses the heading request.
        self.deliver_snapshot(2000.0, 0, 20.0, ABSOLUTE)
        self.core.pump()
        self.assertIs(self.recorder.connection().state, ConnectionState.CONNECTED)
        heading = self.recorder.latest(self.heading.id)
        self.assertIs(heading.quality, Quality.UNAVAILABLE)
        self.assertTrue(heading.reason)
        (snapshot,) = self.polled_snapshots()
        self.assertEqual(snapshot.values, (2000.0, False))
        # Data for other signals keeps flowing in the same session.
        self.deliver_snapshot(2100.0, 0, 21.0, ABSOLUTE + 1)
        self.core.pump()
        self.assertEqual(self.recorder.latest_snapshot(self.snapshot.id).values, (2100.0, False))

    def test_refused_registration_is_retried_with_a_fresh_definition(self):
        (old_request, packet_id) = self.heading_request()
        self.sdk.exception(packet_id)
        self.core.pump()
        self.now += 2.5  # First retry is due after 2 s.
        self.core.pump()
        self.assertIn(old_request[1], [args[0] for args, _ in self.sdk.matching_calls("SimConnect_ClearDataDefinition")])
        (new_request, _) = self.heading_request()
        self.assertNotEqual(new_request[1], old_request[1])
        self.deliver_heading(181.0)
        self.core.pump()
        heading = self.recorder.latest(self.heading.id)
        self.assertIs(heading.quality, Quality.CURRENT)
        self.assertEqual(heading.value, 181.0)
        self.assertIs(self.recorder.connection().state, ConnectionState.CONNECTED)

    def test_an_exception_nobody_claims_is_logged_and_the_session_stays_open(self):
        self.sdk.exception(9_999_999)
        with self.assertLogs("bbc_core.simconnect", "WARNING"):
            self.core.pump()
        self.deliver_snapshot(500.0, 1, 5.0, ABSOLUTE)
        self.core.pump()
        self.assertIs(self.recorder.connection().state, ConnectionState.CONNECTED)
        self.assertEqual(len(self.polled_snapshots()), 1)

    def test_a_malformed_snapshot_packet_is_reported_on_that_subscription_only(self):
        (request, _) = self.snapshot_request()
        self.sdk.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.SIMOBJECT_DATA, b"\0" * 4,
            request_id=request[0], definition_id=request[1], definition_count=5,
        ))
        self.deliver_heading(45.0)
        self.core.pump()
        self.assertIs(self.recorder.connection().state, ConnectionState.CONNECTED)
        self.assertEqual(self.recorder.latest(self.heading.id).value, 45.0)
        self.assertEqual(self.polled_snapshots(), [])

    def test_quit_still_ends_the_session(self):
        self.sdk.quit()
        self.core.pump()
        self.assertIsNot(self.recorder.connection().state, ConnectionState.CONNECTED)


class ProviderLimitsTests(FakeClock, TestCase):
    def test_core_sizes_its_backstops_from_the_adapter(self):
        self.start_clock()
        provider = SimConnectProvider(FakeSdk(), readback_timeout=6.0)
        self.assertEqual(provider.max_command_timeout_seconds, 6.0)
        self.assertEqual(SimConnectProvider(FakeSdk()).max_command_timeout_seconds, 4.0)
        # The worldwide airport list may legitimately take 120 s; Core must not fail it first.
        self.assertGreaterEqual(provider.facility_timeout_seconds, 120.0)
        core = Core(provider)
        self.assertGreater(core._facility_timeout, provider.facility_timeout_seconds)
        self.assertGreater(core._confirm_timeout, provider.max_command_timeout_seconds)

    def test_reported_interval_is_the_native_whole_second_period(self):
        self.start_clock()
        sdk = FakeSdk()
        core = Core(SimConnectProvider(sdk))
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer(diagnostics=True)
        subscription = consumer.subscribe_simvar(*HEADING, interval_ms=1500)
        self.assertEqual(subscription.interval_ms, 2000)
        core.connect()
        core.pump()
        ((request, _),) = [
            item for item in sdk.matching_calls("SimConnect_RequestDataOnSimObject") if item[0][3] == Period.SECOND
        ]
        self.assertEqual(request[6], 1)  # Native interval: skip one period, so every 2 s.
