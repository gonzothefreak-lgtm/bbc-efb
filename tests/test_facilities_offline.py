"""Verify read-only airport queries against an in-memory SimConnect SDK."""

from ctypes import string_at
from struct import pack
from unittest import TestCase
from unittest.mock import patch

from bbc_core import (
    AirportFilter, AirportFilterField, ConnectionState, Core, CoreRequestError, FacilityResult,
    FacilityState, NavaidKind,
)
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import ReceiveFacilityDataPrefix, ReceiveId, ReceiveListPrefix, ReceiveRequest
from bbc_core.simulated import SimulatedProvider
from test_simconnect_provider_offline import FakeSdk, packet


class FacilitiesOfflineTests(TestCase):
    def setUp(self):
        self.now = 100
        clock = patch("bbc_core.simconnect_facilities.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.sdk = FakeSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.dispatch = self.core.register_consumer()

    def requests(self):
        return self.sdk.matching_calls("SimConnect_RequestFacilityData")

    def request(self, icao="YSSY", region=""):
        self.core.connect()
        result = self.dispatch.request_airport(icao, region=region)
        self.core.pump()
        return result, self.requests()[-1][0][1]

    def root(self, request, icao=b"YSSY", region=b"AS", counts=(0, 0, 0)):
        payload = pack("<8s8s64sdddiii", icao, region, b"Test Airport", -33.94, 151.17, 6.4, *counts)
        self.record(request, 0, payload, unique_id=42)

    def record(self, request, facility_type, payload, *, unique_id=0, **fields):
        self.sdk.incoming.append(packet(
            ReceiveFacilityDataPrefix, ReceiveId.FACILITY_DATA, payload,
            request_id=request, facility_type=facility_type, unique_id=unique_id, **fields,
        ))

    def child(self, request, type_id, payload, *, parent=42, index=0, size=1):
        self.record(request, type_id, payload, parent_id=parent, is_list_item=1, item_index=index, list_size=size)

    def end(self, request):
        self.sdk.incoming.append(packet(ReceiveRequest, ReceiveId.FACILITY_DATA_END, request_id=request))

    def list_page(self, request, index, total, *airports):
        payload = b"".join(pack("<6s3sddd", ident, b"AS", -33.94, 151.17, 6.4) for ident in airports)
        self.sdk.incoming.append(packet(
            ReceiveListPrefix, ReceiveId.AIRPORT_LIST, payload, request_id=request,
            array_size=len(airports), entry_number=index, out_of=total,
        ))

    def jetway_page(self, response_id, index, total, *records):
        self.sdk.incoming.append(packet(
            ReceiveListPrefix, ReceiveId.JETWAY_DATA, b"".join(records), request_id=response_id,
            array_size=len(records), entry_number=index, out_of=total,
        ))

    @staticmethod
    def jetway(airport=b"YSSY", parking=24, status=7, object_id=101):
        return (
            pack("<8si", airport, parking)
            + pack("<dddfffii", -33.94, 151.17, 15.75, 1.0, 2.0, 90.5, status, 2)
            + pack("<12dII", *range(12), object_id, 45)
        )

    def jetway_calls(self):
        return self.sdk.matching_calls("SimConnect_RequestJetwayData")

    def filtered_airport(self, *filters):
        self.core.connect()
        pending = self.dispatch.request_airport("YSSY", filters=filters)
        self.core.pump()
        return pending, self.requests()[-1][0][1]

    @staticmethod
    def parking(kind=9, number=24):
        return pack("<iiiIffff", kind, 10, 0, number, 90.0, 20.0, 15.0, 25.0)

    def test_detail_assembles_airport_runways_parking_and_static_jetways(self):
        result, request = self.request(" yssy ", "as")
        ((call, _),) = self.requests()
        self.assertEqual(call[2:], (b"YSSY", b"AS"))
        fields = [args[1] for args, _ in self.sdk.matching_calls("SimConnect_AddToFacilityDefinition")]
        self.assertEqual(fields[0], b"OPEN AIRPORT")
        self.assertEqual(fields[-1], b"CLOSE AIRPORT")
        for name in (b"RUNWAY", b"TAXI_PARKING", b"JETWAY"):
            self.assertLess(fields.index(b"OPEN " + name), fields.index(b"CLOSE " + name))
        self.child(request, 1, pack("<ddfffiiiii", -33.94, 151.17, 160.0, 2500.0, 45.0, 4, 16, 1, 34, 2))
        self.root(request, counts=(1, 1, 1))
        self.child(request, 15, pack("<iiiIffff", 9, 10, 0, 24, 90.0, 20.0, 15.0, 25.0))
        self.child(request, 18, pack("<iii", 10, 0, 24))
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(result.id).state, FacilityState.PENDING)
        self.end(request)
        self.core.pump()
        final = self.dispatch.facility_result(result.id)
        self.assertIs(final.state, FacilityState.READY)
        self.assertIs(final.source.simulated, True)
        self.assertEqual(final.data.airport.icao, "YSSY")
        self.assertEqual(final.data.runways[0].primary_number, 16)
        self.assertEqual(final.data.runways[0].length_m, 2500.0)
        self.assertEqual(final.data.parking[0].number, 24)
        self.assertEqual(final.data.parking[0].bias_z_m, 25.0)
        self.assertEqual(final.data.jetways[0].parking_spot, 24)
        self.assertEqual([update for update in self.dispatch.poll() if isinstance(update, FacilityResult)], [final])

    def test_nearby_list_assembles_pages_out_of_order_and_in_page_order(self):
        self.core.connect()
        pending = self.dispatch.request_nearby_airports()
        self.core.pump()
        (((facility_type, request), _),) = self.sdk.matching_calls("SimConnect_RequestFacilitiesList_EX1")
        self.assertEqual(facility_type, 0)
        self.list_page(request, 1, 2, b"YSCB")
        self.list_page(request, 1, 2, b"YSCB")
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(pending.id).state, FacilityState.PENDING)
        self.list_page(request, 0, 2, b"YSSY", b"YSBK")
        self.core.pump()
        result = self.dispatch.facility_result(pending.id)
        self.assertIs(result.state, FacilityState.READY)
        self.assertEqual([airport.icao for airport in result.data], ["YSSY", "YSBK", "YSCB"])

    def test_explicit_empty_responses_and_airport_without_jetways_are_distinct(self):
        empty, request = self.request("ZZZZ")
        self.end(request)
        no_jetways = self.dispatch.request_airport("YSSY")
        nearby = self.dispatch.request_nearby_airports()
        self.core.pump()
        request = self.requests()[-1][0][1]
        self.root(request)
        self.end(request)
        list_request = self.sdk.matching_calls("SimConnect_RequestFacilitiesList_EX1")[-1][0][1]
        self.list_page(list_request, 0, 1)
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(empty.id).state, FacilityState.NO_DATA)
        self.assertIsNone(self.dispatch.facility_result(empty.id).data)
        airport = self.dispatch.facility_result(no_jetways.id)
        self.assertIs(airport.state, FacilityState.READY)
        self.assertEqual(airport.data.jetways, ())
        result = self.dispatch.facility_result(nearby.id)
        self.assertIs(result.state, FacilityState.READY)
        self.assertEqual(result.data, ())

    def test_shares_equal_queries_and_does_not_expose_other_consumers_requests(self):
        self.core.connect()
        other = self.core.register_consumer()
        pending = self.dispatch.request_airport("YSSY")
        self.assertIsNone(other.facility_result(pending.id))
        self.core.pump()
        self.assertEqual(other.request_airport("yssy").id, pending.id)
        self.core.pump()
        ((args, _),) = self.requests()
        self.root(args[1])
        self.end(args[1])
        self.core.pump()
        self.assertEqual(other.facility_result(pending.id), self.dispatch.facility_result(pending.id))
        followup, _ = self.request()
        self.assertNotEqual(followup.id, pending.id)
        self.assertEqual(len(self.requests()), 2)
        definition_ids = {args[0] for args, _ in self.sdk.matching_calls("SimConnect_AddToFacilityDefinition")}
        self.assertEqual(len(definition_ids), 1)

    def test_missing_or_wrong_children_fail_instead_of_publishing_partial_airport(self):
        for parent in (None, 999):
            pending, request = self.request()
            self.root(request, counts=(0, 0, 1))
            if parent is not None:
                self.child(request, 18, pack("<iii", 10, 0, 24), parent=parent)
            self.end(request)
            self.core.pump()
            result = self.dispatch.facility_result(pending.id)
            self.assertIs(result.state, FacilityState.UNAVAILABLE)
            self.assertIn("incomplete or mismatched", result.detail)
            self.assertIsNone(result.data)

    def test_unrelated_requests_and_telemetry_survive_packet_and_sdk_errors(self):
        speed = self.dispatch.subscribe("aircraft.ground_speed")
        first, first_id = self.request()
        second, second_id = self.request("YSCB")
        third, _ = self.request("YBBN")
        self.record(first_id, 0, b"invalid")
        self.root(second_id, b"YSCB")
        self.end(second_id)
        self.sdk.exception(self.requests()[-1][1])
        ((args, _),) = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.sdk.data(args[0], pack("<d", 52.5))
        self.core.pump()
        self.assertIn("Invalid simulator airport record", self.dispatch.facility_result(first.id).detail)
        self.assertEqual(self.dispatch.facility_result(second.id).data.airport.icao, "YSCB")
        self.assertIn("SimConnect exception", self.dispatch.facility_result(third.id).detail)
        self.assertIs(self.dispatch.connection().state, ConnectionState.CONNECTED)
        self.assertEqual(self.dispatch.latest(speed.id).value, 52.5)

    def test_nearby_list_with_missing_or_conflicting_pages_never_publishes_partial_data(self):
        self.core.connect()
        for conflict in (False, True):
            pending = self.dispatch.request_nearby_airports()
            self.core.pump()
            request = self.sdk.matching_calls("SimConnect_RequestFacilitiesList_EX1")[-1][0][1]
            self.list_page(request, 0, 2, b"YSSY")
            if conflict:
                self.list_page(request, 0, 2, b"YSCB")
                self.core.pump()
            else:
                self.core.pump()
                self.now += 9
                self.core.pump()
            result = self.dispatch.facility_result(pending.id)
            self.assertIs(result.state, FacilityState.UNAVAILABLE)
            self.assertIsNone(result.data)
            self.assertIn("conflicting" if conflict else "timed out", result.detail)

    def test_timeout_and_disconnect_fail_pending_queries_and_ignore_late_replies(self):
        pending, old = self.request()
        self.now += 9
        self.core.pump()
        self.assertIn("timed out", self.dispatch.facility_result(pending.id).detail)
        current, request = self.request()
        self.root(old)
        self.end(old)
        self.root(request)
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(current.id).state, FacilityState.PENDING)
        self.sdk.quit()
        self.core.pump()
        failed = self.dispatch.facility_result(current.id)
        self.assertIs(failed.state, FacilityState.UNAVAILABLE)
        self.assertIn("QUIT", failed.detail)
        again, _ = self.request()
        self.assertNotEqual(again.session_id, failed.session_id)

    def test_closed_consumer_before_dispatch_and_unsupported_provider_do_not_query(self):
        with self.assertRaises(CoreRequestError):
            self.dispatch.request_airport("bad/id")
        self.assertIs(self.dispatch.request_nearby_airports().state, FacilityState.UNAVAILABLE)
        other_core = Core(SimulatedProvider())
        self.assertIs(other_core.register_consumer().request_airport("YSSY").state, FacilityState.UNAVAILABLE)
        self.core.connect()
        self.dispatch.request_airport("YSSY")
        self.dispatch.close()
        self.core.pump()
        self.assertFalse(self.requests())

    def test_navaid_types_get_separate_schemas_and_keep_raw_units_and_waypoint_region(self):
        self.core.connect()
        reads = {kind: self.dispatch.request_navaid(kind, "ABC", region="as") for kind in NavaidKind}
        self.core.pump()
        calls = {args[4]: args for args, _ in self.sdk.matching_calls("SimConnect_RequestFacilityData_EX1")}
        self.assertEqual(set(calls), {b"V", b"N", b"W"})
        self.assertEqual(len({args[0] for args in calls.values()}), 3)
        self.assertTrue(all(args[2:4] == (b"ABC", b"AS") for args in calls.values()))
        vor, ndb, waypoint = (calls[code][1] for code in (b"V", b"N", b"W"))
        self.record(vor, 19, pack("<dddiIf64sf", 10, 20, 21, 1, 113500000, -1.25, b"Test VOR", 10000))
        self.record(ndb, 20, pack("<dddiIf64sfi", 11, 21, 22, 2, 385000, 1.25, b"Test NDB", 2000, 1))
        self.record(waypoint, 21, pack("<dddif8s8si", 12, 22, 23, 1, 2.5, b"ABC", b"AS", 0))
        self.core.pump()
        self.assertTrue(all(self.dispatch.facility_result(item.id).state is FacilityState.PENDING for item in reads.values()))
        for request in (waypoint, ndb, vor):
            self.end(request)
        self.core.pump()
        vor_result = self.dispatch.facility_result(reads[NavaidKind.VOR].id).data[0]
        ndb_result = self.dispatch.facility_result(reads[NavaidKind.NDB].id).data[0]
        waypoint_result = self.dispatch.facility_result(reads[NavaidKind.WAYPOINT].id).data[0]
        self.assertEqual((vor_result.name, vor_result.frequency_raw, vor_result.region), ("Test VOR", 113500000, None))
        self.assertEqual((ndb_result.kind, ndb_result.range_m, ndb_result.terminal), (NavaidKind.NDB, 2000, True))
        self.assertEqual((waypoint_result.icao, waypoint_result.region, waypoint_result.terminal), ("ABC", "AS", False))

    def test_navaid_no_data_and_wrong_typed_record_isolate_concurrent_queries(self):
        self.core.connect()
        first = self.dispatch.request_navaid(NavaidKind.VOR, "ABC")
        second = self.dispatch.request_navaid(NavaidKind.NDB, "ABC")
        self.core.pump()
        ((vor, _), (ndb, _)) = self.sdk.matching_calls("SimConnect_RequestFacilityData_EX1")
        self.record(vor[1], 20, b"wrong type")
        self.end(ndb[1])
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(first.id).state, FacilityState.UNAVAILABLE)
        self.assertIs(self.dispatch.facility_result(second.id).state, FacilityState.NO_DATA)
        self.assertIs(self.dispatch.connection().state, ConnectionState.CONNECTED)

    def test_navaid_schema_rejection_does_not_invalidate_airports(self):
        self.core.connect()
        vor = self.dispatch.request_navaid(NavaidKind.VOR, "ABC")
        airport, request = self.request()
        send_id = next(send for args, send in self.sdk.matching_calls("SimConnect_AddToFacilityDefinition")
                       if args[1] == b"OPEN VOR")
        self.sdk.exception(send_id)
        self.root(request)
        self.end(request)
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(vor.id).state, FacilityState.UNAVAILABLE)
        self.assertIs(self.dispatch.facility_result(airport.id).state, FacilityState.READY)

    def test_jetway_uses_selected_indices_and_keeps_position_and_attachment(self):
        self.core.connect()
        first = self.dispatch.request_jetways("yssy", parking_indices=(24, 2, 24))
        peer = self.core.register_consumer()
        self.assertEqual(first.id, peer.request_jetways("YSSY", parking_indices=(2, 24)).id)
        self.core.pump()
        (((airport, count, indices), _),) = self.jetway_calls()
        self.assertEqual((airport, count, tuple(indices)), (b"YSSY", 2, (2, 24)))
        self.jetway_page(0, 1, 2, self.jetway(parking=24, status=7))
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(first.id).state, FacilityState.PENDING)
        self.jetway_page(0, 0, 2, self.jetway(parking=2, status=6, object_id=99))
        self.core.pump()
        final = self.dispatch.facility_result(first.id)
        self.assertIs(final.state, FacilityState.READY)
        self.assertEqual(final, peer.facility_result(first.id))
        waiting, attached = final.data
        self.assertFalse(waiting.fully_attached)
        self.assertTrue(attached.fully_attached)
        self.assertEqual((attached.parking_index, attached.altitude_ft, attached.heading_deg), (24, 15.75, 90.5))
        self.assertEqual(attached.wheel_ground_lock_xyz, (9.0, 10.0, 11.0))
        self.assertEqual((attached.jetway_object_id, attached.attached_object_id), (101, 45))

    def test_jetways_serialize_and_do_not_misroute_already_queued_empty_reply(self):
        self.core.connect()
        first = self.dispatch.request_jetways("YSSY")
        second = self.dispatch.request_jetways("YSCB")
        self.core.pump()
        self.assertEqual(len(self.jetway_calls()), 1)
        self.assertEqual(self.jetway_calls()[0][0], (b"YSSY", 0, None))
        self.jetway_page(0, 0, 1, self.jetway())
        self.jetway_page(0, 0, 1)
        self.core.pump(limit=1)
        self.assertEqual(len(self.jetway_calls()), 1)
        self.core.pump()
        self.assertEqual([args[0] for args, _ in self.jetway_calls()], [b"YSSY", b"YSCB"])
        self.assertIs(self.dispatch.facility_result(second.id).state, FacilityState.PENDING)
        self.jetway_page(0, 0, 1)
        self.core.pump()
        self.assertEqual(self.dispatch.facility_result(second.id).data, ())
        self.assertTrue(self.dispatch.facility_result(first.id).data[0].fully_attached)

    def test_jetway_timeout_fences_late_packets_until_a_new_session(self):
        self.core.connect()
        first = self.dispatch.request_jetways("YSSY")
        second = self.dispatch.request_jetways("YSCB")
        self.core.pump()
        self.now += 9
        self.core.pump()
        for item in (first, second):
            self.assertIn("fresh SimConnect session", self.dispatch.facility_result(item.id).detail)
        self.jetway_page(0, 0, 1)
        non_jetway, request = self.request()
        self.root(request)
        self.end(request)
        blocked = self.dispatch.request_jetways("YSSY")
        self.core.pump()
        self.assertEqual(len(self.jetway_calls()), 1)
        self.assertIs(self.dispatch.facility_result(non_jetway.id).state, FacilityState.READY)
        self.assertIs(self.dispatch.facility_result(blocked.id).state, FacilityState.UNAVAILABLE)
        self.core.disconnect()
        self.core.connect()
        again = self.dispatch.request_jetways("YSSY")
        self.core.pump()
        self.jetway_page(0, 0, 1)
        self.core.pump()
        self.assertEqual(len(self.jetway_calls()), 2)
        self.assertEqual(self.dispatch.facility_result(again.id).data, ())

    def test_jetway_sdk_rejection_is_correlated_and_wrong_airport_fences_results(self):
        self.core.connect()
        first = self.dispatch.request_jetways("YSSY")
        second = self.dispatch.request_jetways("YSCB")
        self.core.pump()
        self.sdk.exception(self.jetway_calls()[0][1])
        self.core.pump()
        self.assertIn("SimConnect exception", self.dispatch.facility_result(first.id).detail)
        self.assertEqual(len(self.jetway_calls()), 2)
        self.jetway_page(0, 0, 1, self.jetway(airport=b"YSSY"))
        self.core.pump()
        self.assertIn("airport or parking", self.dispatch.facility_result(second.id).detail)
        self.assertIs(self.dispatch.connection().state, ConnectionState.CONNECTED)

    def test_filtered_parking_accepts_sparse_indices_and_preserves_other_lists(self):
        filtered, request = self.filtered_airport(AirportFilter(AirportFilterField.PARKING_TYPE, 9))
        ((args, _),) = self.sdk.matching_calls("SimConnect_AddFacilityDataDefinitionFilter")
        self.assertEqual(args[:3], (self.requests()[0][0][0], b"AIRPORT:TAXI_PARKING:TYPE", 4))
        self.assertEqual(string_at(args[3], args[2]), pack("<i", 9))
        self.root(request, counts=(0, 5, 1))
        self.child(request, 15, self.parking(), index=3, size=5)
        self.child(request, 18, pack("<iii", 10, 0, 24))
        self.end(request)
        self.core.pump()
        data = self.dispatch.facility_result(filtered.id).data
        self.assertEqual([(spot.index, spot.number) for spot in data.parking], [(3, 24)])
        self.assertEqual(len(data.jetways), 1)

    def test_no_filter_matches_is_a_ready_airport_and_reuse_clears_old_rules(self):
        first, request = self.filtered_airport(AirportFilter(AirportFilterField.PARKING_TYPE, 10))
        self.root(request, counts=(0, 5, 0))
        self.end(request)
        self.core.pump()
        self.assertEqual(self.dispatch.facility_result(first.id).data.parking, ())
        second, request = self.filtered_airport(AirportFilter(AirportFilterField.PARKING_NUMBER, 0xFFFFFFFF))
        definitions = [args[0] for args, _ in self.requests()]
        self.assertEqual(definitions[0], definitions[1])
        clearing, = self.sdk.matching_calls("SimConnect_ClearAllFacilityDataDefinitionFilters")
        self.assertEqual(clearing[0], (definitions[0],))
        args, applied = self.sdk.matching_calls("SimConnect_AddFacilityDataDefinitionFilter")[-1]
        self.assertLess(clearing[1], applied)
        self.assertEqual(string_at(args[3], args[2]), b"\xff" * 4)
        self.root(request, counts=(0, 5, 0))
        self.child(request, 15, self.parking(number=0xFFFFFFFF), size=1)
        self.end(request)
        self.core.pump()
        self.assertEqual(self.dispatch.facility_result(second.id).data.parking[0].number, 0xFFFFFFFF)

    def test_concurrent_filtered_and_unfiltered_queries_have_independent_definitions(self):
        first, a = self.filtered_airport(AirportFilter(AirportFilterField.PARKING_TYPE, 9))
        second, b = self.filtered_airport(AirportFilter(AirportFilterField.PARKING_TYPE, 10))
        unfiltered, c = self.request()
        self.assertEqual(len({args[0] for args, _ in self.requests()}), 3)
        for request, kind in ((b, 10), (c, 1), (a, 9)):
            self.root(request, counts=(0, 1, 0))
            self.child(request, 15, self.parking(kind))
            self.end(request)
        self.core.pump()
        for pending, kind in ((first, 9), (second, 10), (unfiltered, 1)):
            self.assertEqual(self.dispatch.facility_result(pending.id).data.parking[0].kind, kind)

    def test_filter_rejection_and_unexpected_matches_do_not_publish_partial_data(self):
        rule = AirportFilter(AirportFilterField.PARKING_TYPE, 9)
        rejected, request = self.filtered_airport(rule)
        _, sent = self.sdk.matching_calls("SimConnect_AddFacilityDataDefinitionFilter")[0]
        self.sdk.exception(sent)
        self.end(request)
        self.core.pump()
        self.assertIn("SimConnect exception", self.dispatch.facility_result(rejected.id).detail)
        mismatched, request = self.filtered_airport(rule)
        self.root(request, counts=(0, 1, 0))
        self.child(request, 15, self.parking(kind=10))
        self.end(request)
        self.core.pump()
        status = self.dispatch.facility_result(mismatched.id)
        self.assertIs(status.state, FacilityState.UNAVAILABLE)
        self.assertIn("outside the requested airport filter", status.detail)

    def test_filtered_timeout_does_not_reuse_a_definition_until_a_late_end(self):
        rule = AirportFilter(AirportFilterField.PARKING_TYPE, 9)
        first, old_request = self.filtered_airport(rule)
        self.now += 9
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(first.id).state, FacilityState.UNAVAILABLE)
        second, new_request = self.filtered_airport(rule)
        self.assertNotEqual(self.requests()[0][0][0], self.requests()[1][0][0])
        self.end(old_request)
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(second.id).state, FacilityState.PENDING)
        self.end(new_request)
        self.core.pump()
        self.filtered_airport(rule)
        self.assertEqual(self.requests()[0][0][0], self.requests()[-1][0][0])

    def test_clear_rejection_isolated_from_the_unfiltered_airport_path(self):
        rule = AirportFilter(AirportFilterField.PARKING_TYPE, 9)
        _, request = self.filtered_airport(rule)
        self.end(request)
        self.core.pump()
        rejected, filtered_id = self.filtered_airport(rule)
        definition = self.requests()[-1][0][0]
        _, sent = self.sdk.matching_calls("SimConnect_ClearAllFacilityDataDefinitionFilters")[-1]
        full, full_id = self.request()
        self.sdk.exception(sent)
        self.root(full_id)
        self.end(full_id)
        self.end(filtered_id)
        self.core.pump()
        self.assertIs(self.dispatch.facility_result(rejected.id).state, FacilityState.UNAVAILABLE)
        self.assertIs(self.dispatch.facility_result(full.id).state, FacilityState.READY)
        self.filtered_airport(rule)
        self.assertNotEqual(self.requests()[-1][0][0], definition)

    def test_filter_sets_are_normalized_and_invalid_fields_never_reach_the_sdk(self):
        a = AirportFilter(AirportFilterField.PARKING_TYPE, 9)
        b = AirportFilter(AirportFilterField.RUNWAY_PRIMARY_NUMBER, 27)
        self.core.connect()
        one = self.dispatch.request_airport("YSSY", filters=(a, b, a))
        self.assertEqual(self.dispatch.request_airport("yssy", filters=(b, a)).id, one.id)
        with self.assertRaises(CoreRequestError):
            self.dispatch.request_airport("YSSY", filters=(a, AirportFilter(a.field, 11)))
        with self.assertRaises(CoreRequestError):
            AirportFilter(a.field, True)
        self.core.pump()
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_AddFacilityDataDefinitionFilter")), 2)
