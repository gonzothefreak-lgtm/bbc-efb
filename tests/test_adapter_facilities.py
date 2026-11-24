"""Regression tests for facility requests, lists and cache watches."""

from struct import pack
from unittest import TestCase

from bbc_core import (
    AirportFilter, AirportFilterField, ConnectionState, Core, FacilityCacheUpdate, FacilityChange, FacilityKind,
    FacilityState,
)
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import ReceiveFacilityDataPrefix, ReceiveId, ReceiveListPrefix, ReceiveRequest
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk, packet


def airport_record(icao, lat=-33.9, lon=151.2):
    return pack("<6s3sddd", icao, b"AS", lat, lon, 8.5)


class RefusingFacilitySdk(FakeSdk):
    refuse = frozenset()

    def call(self, name, handle, *args):
        super().call(name, handle, *args)
        if name == "SimConnect_AddToFacilityDefinition" and args[1] in self.refuse:
            self.exception(self.packet_id)


class FacilityCase(FakeClock, TestCase):
    def setUp(self):
        self.start_clock()
        self.sdk = RefusingFacilitySdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.reader = self.core.register_consumer()
        self.core.connect()

    def page(self, kind, request, *records, index=0, total=1, padding=b""):
        self.sdk.incoming.append(packet(
            ReceiveListPrefix, kind, b"".join(records) + padding, request_id=request,
            array_size=len(records), entry_number=index, out_of=total,
        ))

    def record(self, request, facility_type, payload, *, unique_id=0, **fields):
        self.sdk.incoming.append(packet(
            ReceiveFacilityDataPrefix, ReceiveId.FACILITY_DATA, payload,
            request_id=request, facility_type=facility_type, unique_id=unique_id, **fields,
        ))

    def end(self, request):
        self.sdk.incoming.append(packet(ReceiveRequest, ReceiveId.FACILITY_DATA_END, request_id=request))

    def nearby(self):
        result = self.reader.request_nearby_airports()
        self.core.pump()
        return result, self.sdk.matching_calls("SimConnect_RequestFacilitiesList_EX1")[-1][0][1]


class ListTests(FacilityCase):
    """Findings A52, A55, A106 and A110."""

    def test_bad_record_is_omitted_and_labelled(self):
        result, request = self.nearby()
        self.page(ReceiveId.AIRPORT_LIST, request, airport_record(b"YSSY"), airport_record(b""),
                  airport_record(b"YBBN", lon=180.0001))
        with self.assertLogs("bbc_core.simconnect_facility_lists", "WARNING"):
            self.core.pump()
        reply = self.reader.facility_result(result.id)
        self.assertIs(reply.state, FacilityState.READY)
        self.assertEqual([item.icao for item in reply.data], ["YSSY"])
        self.assertIn("2 invalid simulator records omitted", reply.detail)

    def test_padded_page_is_accepted(self):
        result, request = self.nearby()
        self.page(ReceiveId.AIRPORT_LIST, request, airport_record(b"YSSY"), padding=b"\0\0\0")
        self.core.pump()
        self.assertEqual(len(self.reader.facility_result(result.id).data), 1)

    def test_slow_multipart_list_completes_while_pages_keep_arriving(self):
        result, request = self.nearby()
        for index in range(4):
            self.page(ReceiveId.AIRPORT_LIST, request, airport_record(b"Y%03d" % index), index=index, total=4)
            self.core.pump()
            self.advance(6)
            self.core.pump()
        reply = self.reader.facility_result(result.id)
        self.assertIs(reply.state, FacilityState.READY)
        self.assertEqual(len(reply.data), 4)

    def test_stalled_list_still_times_out(self):
        result, request = self.nearby()
        self.page(ReceiveId.AIRPORT_LIST, request, airport_record(b"YSSY"), index=0, total=2)
        self.core.pump()
        self.advance(9)
        self.core.pump()
        self.assertIn("timed out", self.reader.facility_result(result.id).detail)

    def test_truncated_packet_fails_no_outstanding_request(self):
        result, request = self.nearby()
        self.sdk.incoming.append(packet(ReceiveListPrefix, ReceiveId.AIRPORT_LIST)[:14])
        self.sdk.incoming[-1] = (14).to_bytes(4, "little") + self.sdk.incoming[-1][4:]
        with self.assertLogs("bbc_core.simconnect_facilities", "WARNING"):
            self.core.pump()
        self.assertIs(self.reader.facility_result(result.id).state, FacilityState.PENDING)
        self.page(ReceiveId.AIRPORT_LIST, request, airport_record(b"YSSY"))
        self.core.pump()
        self.assertIs(self.reader.facility_result(result.id).state, FacilityState.READY)


class AirportDefinitionTests(FacilityCase):
    """Findings A107 and A108."""

    def root(self, request, counts=(0, 0)):
        payload = pack("<8s8s64sddd" + "i" * len(counts), b"YSSY", b"AS", b"Test", -33.9, 151.2, 6.0, *counts)
        self.record(request, 0, payload, unique_id=42)

    def test_refused_jetway_fields_fall_back_to_a_definition_without_jetways(self):
        self.sdk.refuse = {b"N_JETWAYS"}
        result = self.reader.request_airport("YSSY")
        with self.assertLogs("bbc_core.simconnect_facilities", "WARNING"):
            self.core.pump()
        self.core.pump()
        requests = self.sdk.matching_calls("SimConnect_RequestFacilityData")
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0][0][0], requests[1][0][0])
        fields = [args[1] for args, _ in self.sdk.matching_calls("SimConnect_AddToFacilityDefinition")
                  if args[0] == requests[1][0][0]]
        self.assertNotIn(b"N_JETWAYS", fields)
        self.assertNotIn(b"OPEN JETWAY", fields)
        request = requests[1][0][1]
        self.root(request)
        self.end(request)
        self.core.pump()
        reply = self.reader.facility_result(result.id)
        self.assertIs(reply.state, FacilityState.READY)
        self.assertEqual(reply.data.jetways, ())
        self.assertIn("jetways are omitted", reply.detail)
        self.assertIs(self.reader.connection().state, ConnectionState.CONNECTED)

    def test_timed_out_filter_definition_is_quarantined_then_reused(self):
        rule = AirportFilter(AirportFilterField.PARKING_TYPE, 9)
        for _ in range(8):
            self.reader.request_airport("YSSY", filters=(rule,))
            self.core.pump()
            self.advance(21)
            self.core.pump()
        definitions = {args[0] for args, _ in self.sdk.matching_calls("SimConnect_RequestFacilityData")}
        self.assertGreater(len(definitions), 1)
        self.advance(61)
        later = self.reader.request_airport("YSSY", filters=(rule,))
        self.core.pump()
        self.assertIs(self.reader.facility_result(later.id).state, FacilityState.PENDING)
        last = self.sdk.matching_calls("SimConnect_RequestFacilityData")[-1][0][0]
        self.assertIn(last, definitions)


class JetwayTests(FacilityCase):
    """Finding A51."""

    @staticmethod
    def jetway(airport=b"YSSY"):
        return (pack("<8si", airport, 24) + pack("<dddfffii", -33.94, 151.17, 15.75, 1.0, 2.0, 90.5, 7, 2)
                + pack("<12dII", *range(12), 101, 45))

    def test_queued_request_gets_its_full_reply_window_from_send(self):
        first = self.reader.request_jetways("YSSY")
        second = self.reader.request_jetways("YSCB")
        self.core.pump()
        self.advance(7.5)
        self.page(ReceiveId.JETWAY_DATA, 0, self.jetway())
        self.core.pump()
        self.assertIs(self.reader.facility_result(first.id).state, FacilityState.READY)
        self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_RequestJetwayData")), 2)
        self.advance(6)
        self.core.pump()
        self.assertIs(self.reader.facility_result(second.id).state, FacilityState.PENDING)

    def test_partial_reply_timeout_still_fences_until_reconnect(self):
        first = self.reader.request_jetways("YSSY")
        self.core.pump()
        self.page(ReceiveId.JETWAY_DATA, 0, self.jetway(), index=0, total=2)
        self.core.pump()
        self.advance(9)
        self.core.pump()
        self.assertIn("fresh SimConnect session", self.reader.facility_result(first.id).detail)
        blocked = self.reader.request_jetways("YSSY")
        self.core.pump()
        self.assertIs(self.reader.facility_result(blocked.id).state, FacilityState.UNAVAILABLE)


class CacheTests(FacilityCase):
    """Findings A53, A54 and A55 for cache watches."""

    def updates(self):
        return [item for item in self.reader.poll() if isinstance(item, FacilityCacheUpdate)]

    def test_failed_watch_is_resubscribed_with_backoff(self):
        watch = self.reader.subscribe_facility_cache(FacilityKind.AIRPORT)
        self.core.pump()
        self.advance(9)
        self.core.pump()
        self.assertIs(self.reader.facility_cache_snapshot(watch.id).change, FacilityChange.UNAVAILABLE)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_SubscribeToFacilities_EX1")), 1)
        self.advance(2.5)
        self.core.pump()
        calls = self.sdk.matching_calls("SimConnect_SubscribeToFacilities_EX1")
        self.assertEqual(len(calls), 2)
        self.page(ReceiveId.AIRPORT_LIST, calls[-1][0][1], airport_record(b"YSSY"), airport_record(b""))
        self.core.pump()
        snapshot = self.reader.facility_cache_snapshot(watch.id)
        self.assertIs(snapshot.change, FacilityChange.SNAPSHOT)
        self.assertEqual([item.icao for item in snapshot.entries], ["YSSY"])

    def test_unsubscribe_rejection_pauses_the_kind_then_resubscribes(self):
        watch = self.reader.subscribe_facility_cache(FacilityKind.VOR)
        self.core.pump()
        self.reader.unsubscribe(watch.id)
        self.core.pump()
        (_, sent), = self.sdk.matching_calls("SimConnect_UnsubscribeToFacilities_EX1")
        self.sdk.exception(sent)
        with self.assertLogs("bbc_core.simconnect_facility_cache", "WARNING"):
            self.core.pump()
        self.assertIs(self.reader.connection().state, ConnectionState.CONNECTED)
        again = self.reader.subscribe_facility_cache(FacilityKind.VOR)
        self.core.pump()
        snapshot = self.reader.facility_cache_snapshot(again.id)
        self.assertIs(snapshot.change, FacilityChange.UNAVAILABLE)
        self.assertIn("rejected unsubscribe", snapshot.detail)
        subscribes = self.sdk.matching_calls("SimConnect_SubscribeToFacilities_EX1")
        self.assertEqual(len(subscribes), 1)
        # A quiet period, not a session-long taint: the kind is subscribed again later.
        self.advance(2.5)
        self.core.pump()
        subscribes = self.sdk.matching_calls("SimConnect_SubscribeToFacilities_EX1")
        self.assertEqual(len(subscribes), 2)
        self.page(ReceiveId.VOR_LIST, subscribes[-1][0][1])
        self.core.pump()
        self.assertIs(self.reader.facility_cache_snapshot(again.id).change, FacilityChange.SNAPSHOT)

    def test_repeated_unsubscribe_rejections_back_off(self):
        cache = self.core._provider._facilities.cache
        for expected in (2.0, 4.0, 8.0):
            started = self.now
            cache._taint(FacilityKind.VOR, "rejected")
            self.assertAlmostEqual(cache._tainted[FacilityKind.VOR][1] - started, expected)
        self.assertIsNone(cache._quiet(FacilityKind.VOR, self.now + 9))
