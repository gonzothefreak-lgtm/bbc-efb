"""Exercise named Client Data areas against an in-memory SDK."""

from ctypes import string_at
from struct import unpack_from
from unittest import TestCase
from unittest.mock import patch

from bbc_core import ClientDataArea, CommandState, ConnectionState, Core, CoreRequestError, Quality
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import ClientDataPeriod, ReceiveId, ReceiveSimObjectDataPrefix
from test_simconnect_provider_offline import FakeSdk, packet


class ClientDataSdk(FakeSdk):
    def __init__(self):
        super().__init__()
        self.areas = {b"EXTERNAL": bytearray(b"ab1234cd")}
        self.names = {}
        self.regions = {}
        self.streams = {}
        self.supply_reads = True
        self.reject_create = False
        self.writes = []

    def close(self, handle):
        super().close(handle)
        self.names.clear()
        self.regions.clear()
        self.streams.clear()

    def call(self, name, handle, *args):
        super().call(name, handle, *args)
        if name == "SimConnect_MapClientDataNameToID":
            area, identifier = args
            self.names[identifier] = area
        elif name == "SimConnect_CreateClientData":
            identifier, size, _ = args
            if self.reject_create:
                self.exception(self.packet_id)
            else:
                self.areas[self.names[identifier]] = bytearray(size)
        elif name == "SimConnect_AddToClientDataDefinition":
            definition, offset, size, _, datum = args
            self.regions[definition] = offset, size, datum
        elif name == "SimConnect_ClearClientDataDefinition":
            del self.regions[args[0]]
        elif name == "SimConnect_RequestClientData":
            identifier, request, definition, period, *_ = args
            if period == ClientDataPeriod.NEVER:
                self.streams.pop(request, None)
            elif period == ClientDataPeriod.ON_SET:
                self.streams[request] = identifier, definition
            elif self.supply_reads:
                self.deliver(identifier, request, definition)
        elif name == "SimConnect_SetClientData":
            identifier, definition, flags, reserved, length, buffer = args
            offset, size, datum = self.regions[definition]
            payload = string_at(buffer, length)
            self.writes.append(payload)
            if (flags, reserved, length, unpack_from("<I", payload)[0]) != (1, 0, size + 4, datum):
                raise AssertionError("Malformed tagged Client Data write.")
            if self.reject_write:
                self.exception(self.packet_id)
            elif self.apply_write:
                self.areas[self.names[identifier]][offset:offset + size] = payload[4:]

    def deliver(self, identifier, request, definition, payload=None):
        if payload is None:
            area = self.areas.get(self.names[identifier])
            if area is None:
                self.exception(self.packet_id)
                return
            offset, size, _ = self.regions[definition]
            payload = bytes(area[offset:offset + size])
        self.incoming.append(packet(
            ReceiveSimObjectDataPrefix, ReceiveId.CLIENT_DATA, payload,
            request_id=request, definition_id=definition, definition_count=1,
        ))


class ClientDataOfflineTests(TestCase):
    def setUp(self):
        self.now = 100.0
        for module in ("core", "simconnect", "simconnect_client_data"):
            clock = patch(f"bbc_core.{module}.monotonic", side_effect=lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)
        self.sdk = ClientDataSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.module = self.core.register_consumer(control_groups=("client_data",))
        self.area = ClientDataArea("external", 8, writable=True)
        self.region = self.area.region(2, 4)
        self.core.register_client_data(self.area)

    def test_shared_subscription_initial_read_refresh_and_final_retirement(self):
        self.assertEqual(self.module.client_data_areas(), (self.area,))
        self.assertEqual(self.sdk.calls, [])
        second = self.core.register_consumer()
        first_sub = self.module.subscribe_client_data(self.region)
        second_sub = second.subscribe_client_data(self.region)
        self.core.connect()
        self.core.pump()
        self.assertEqual(second.latest(second_sub.id).value, b"1234")
        self.assertFalse(self.sdk.matching_calls("SimConnect_CreateClientData"))
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_MapClientDataNameToID")), 1)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_AddToClientDataDefinition")), 1)
        self.sdk.areas[b"EXTERNAL"][2:6] = b"ABCD"
        ((request, (identifier, definition)),) = self.sdk.streams.items()
        self.sdk.deliver(identifier, request, definition)
        self.core.pump()
        self.assertEqual(self.module.latest(first_sub.id).value, b"ABCD")
        self.sdk.areas[b"EXTERNAL"][2:6] = b"WXYZ"
        second.request_client_data(second_sub.id)
        self.module.request_client_data(first_sub.id)
        self.core.pump()
        reads = self.sdk.matching_calls("SimConnect_RequestClientData")
        self.assertEqual([args[3] for args, _ in reads], [
            ClientDataPeriod.ON_SET, ClientDataPeriod.ONCE, ClientDataPeriod.ONCE,
        ])
        self.assertEqual(second.latest(second_sub.id).value, b"WXYZ")
        second.close()
        self.core.pump()
        self.assertTrue(self.sdk.streams)
        self.module.close()
        self.core.pump()
        self.assertFalse(self.sdk.streams)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_ClearClientDataDefinition")), 1)

    def test_create_once_for_two_regions_and_keep_sdk_area_after_disconnect(self):
        owned = ClientDataArea("owned", 8, create=True, writable=True)
        self.core.register_client_data(owned)
        first = self.module.subscribe_client_data(owned.region(0, 4))
        self.module.subscribe_client_data(owned.region(4, 4))
        self.core.connect()
        self.core.pump()
        ((create, _),) = self.sdk.matching_calls("SimConnect_CreateClientData")
        self.assertEqual(create[1:], (8, 1))
        self.assertEqual(self.module.latest(first.id).value, bytes(4))
        self.core.disconnect()
        self.assertIn(b"OWNED", self.sdk.areas)

    def test_permissions_and_bounds_prevent_writes(self):
        with self.assertRaises(ValueError):
            self.area.region(7, 2)
        with self.assertRaises(CoreRequestError):
            self.core.register_client_data(ClientDataArea("EXTERNAL", 7))
        with self.assertRaises(CoreRequestError):
            self.module.subscribe_client_data(ClientDataArea("UNKNOWN", 8).region())
        spectator = self.core.register_consumer()
        readonly = ClientDataArea("readonly", 4)
        self.core.register_client_data(readonly)
        self.core.connect()
        for consumer, region, payload in (
            (spectator, self.region, b"test"),
            (self.module, readonly.region(), b"test"),
            (self.module, self.region, b"short"),
            (self.module, self.region, "test"),
        ):
            self.assertEqual(consumer.submit_client_data(region, payload).state, CommandState.REJECTED)
        write = self.module.submit_client_data(self.region, b"test")
        self.core.pump()
        self.assertEqual(self.module.command_result(write.id).state, CommandState.REJECTED)
        self.assertFalse(self.sdk.writes)

    def test_tagged_write_preserves_binary_bytes_and_adjacent_bytes(self):
        self.module.subscribe_client_data(self.region)
        self.core.connect()
        self.core.pump()
        write = self.module.submit_client_data(self.region, b"X\0Y\0")
        for _ in range(3):
            self.core.pump()
        self.assertEqual(self.module.command_result(write.id).state, CommandState.CONFIRMED)
        self.assertEqual(self.sdk.areas[b"EXTERNAL"], bytearray(b"abX\0Y\0cd"))
        self.assertEqual(len(self.sdk.writes), 1)

    def test_mismatched_readback_times_out_without_write_replay(self):
        self.module.subscribe_client_data(self.region)
        self.core.connect()
        self.core.pump()
        self.sdk.apply_write = False
        write = self.module.submit_client_data(self.region, b"test")
        for _ in range(4):
            self.core.pump()
            self.now += 0.2
        self.now += 2
        self.core.pump()
        self.assertEqual(self.module.command_result(write.id).state, CommandState.UNCERTAIN)
        self.assertEqual(len(self.sdk.writes), 1)

    def test_failed_create_isolated_and_sdk_write_exception_reported(self):
        owned = ClientDataArea("owned", 8, create=True, writable=True)
        self.core.register_client_data(owned)
        bad = self.module.subscribe_client_data(owned.region())
        good = self.module.subscribe_client_data(self.region)
        self.sdk.reject_create = True
        self.core.connect()
        self.core.pump()
        self.assertIs(self.module.latest(bad.id).quality, Quality.UNAVAILABLE)
        self.assertIn("exception", self.module.latest(bad.id).reason)
        self.assertEqual(self.module.latest(good.id).value, b"1234")
        self.sdk.reject_write = True
        write = self.module.submit_client_data(self.region, b"test")
        self.core.pump()
        self.core.pump()
        self.assertEqual(self.module.command_result(write.id).state, CommandState.REJECTED)
        self.assertIs(self.module.connection().state, ConnectionState.CONNECTED)

    def test_read_timeout_and_truncated_packet_survive_and_allow_explicit_refresh(self):
        self.sdk.supply_reads = False
        sub = self.module.subscribe_client_data(self.region)
        self.core.connect()
        self.core.pump()
        self.now += 4
        self.core.pump()
        self.assertIn("timed out", self.module.latest(sub.id).reason)
        ((request, (identifier, definition)),) = self.sdk.streams.items()
        self.sdk.deliver(identifier, request, definition, payload=b"a")
        self.core.pump()
        self.assertIn("payload size", self.module.latest(sub.id).reason)
        self.sdk.supply_reads = True
        self.module.request_client_data(sub.id)
        self.core.pump()
        self.assertEqual(self.module.latest(sub.id).value, b"1234")

    def test_reconnect_invalidates_baseline_and_remaps_once_for_new_handle(self):
        self.module.subscribe_client_data(self.region)
        first = self.core.connect()
        self.core.pump()
        self.core.disconnect()
        self.sdk.supply_reads = False
        second = self.core.connect()
        self.core.pump()
        self.assertNotEqual(first.session_id, second.session_id)
        write = self.module.submit_client_data(self.region, b"test")
        self.core.pump()
        self.assertEqual(self.module.command_result(write.id).state, CommandState.REJECTED)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_MapClientDataNameToID")), 2)
        self.assertFalse(self.sdk.writes)
