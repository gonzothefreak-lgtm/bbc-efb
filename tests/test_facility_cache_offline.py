"""Exercise facility list and cache lifecycles through the public Core API."""

from struct import pack
from unittest import TestCase

from bbc_core import Core, FacilityCacheUpdate, FacilityChange, FacilityKind, FacilityState, NavaidKind
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import ReceiveId, ReceiveListPrefix, ReceiveRequest
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk, packet


class FacilityCacheOfflineTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100)  # Shared clock for every bbc_core module, sleep included (A139).
        self.sdk = FakeSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.first = self.core.register_consumer()

    def page(self, kind, request, *records, index=0, total=1):
        self.sdk.incoming.append(packet(
            ReceiveListPrefix, kind, b"".join(records), request_id=request,
            array_size=len(records), entry_number=index, out_of=total,
        ))

    @staticmethod
    def airport(icao, lat=-33.9):
        return pack("<6s3sddd", icao, b"AS", lat, 151.2, 8.5)

    @staticmethod
    def watch_events(consumer):
        return [item for item in consumer.poll() if isinstance(item, FacilityCacheUpdate)]

    def subscriptions(self):
        return self.sdk.matching_calls("SimConnect_SubscribeToFacilities_EX1")

    def retirements(self):
        return self.sdk.matching_calls("SimConnect_UnsubscribeToFacilities_EX1")

    def test_worldwide_and_nearby_airports_use_distinct_queries(self):
        self.core.connect()
        worldwide = self.first.request_worldwide_airports()
        nearby = self.first.request_nearby_airports()
        self.core.pump()
        ((global_args, _),) = self.sdk.matching_calls("SimConnect_RequestFacilitiesList")
        ((near_args, _),) = self.sdk.matching_calls("SimConnect_RequestFacilitiesList_EX1")
        self.assertNotEqual(global_args[1], near_args[1])
        self.assertEqual((global_args[0], near_args[0]), (0, 0))
        self.page(ReceiveId.AIRPORT_LIST, global_args[1], self.airport(b"EGLL"), index=1, total=2)
        self.page(ReceiveId.AIRPORT_LIST, near_args[1], self.airport(b"YSSY"))
        self.core.pump()
        self.assertIs(self.first.facility_result(worldwide.id).state, FacilityState.PENDING)
        self.assertEqual(self.first.facility_result(nearby.id).data[0].icao, "YSSY")
        self.page(ReceiveId.AIRPORT_LIST, global_args[1], self.airport(b"YSSY"), index=0, total=2)
        self.core.pump()
        self.assertEqual([item.icao for item in self.first.facility_result(worldwide.id).data], ["YSSY", "EGLL"])

    def test_nearby_waypoints_ndbs_and_vors_decode_their_native_layouts(self):
        self.core.connect()
        pending = {kind: self.first.request_nearby_navaids(kind) for kind in NavaidKind}
        self.core.pump()
        requests = {args[0]: args[1] for args, _ in self.sdk.matching_calls("SimConnect_RequestFacilitiesList_EX1")}
        base = self.airport(b"TEST")
        self.page(ReceiveId.WAYPOINT_LIST, requests[1], base + pack("<f", 2.5))
        self.page(ReceiveId.NDB_LIST, requests[2], base + pack("<fI", -1.0, 385000))
        self.page(ReceiveId.VOR_LIST, requests[3], base + pack("<fIIfdddf", 3.5, 113500000, 7, 92, -34, 151, 5, 3))
        self.core.pump()
        waypoint, ndb, vor = (
            self.first.facility_result(pending[kind].id).data[0]
            for kind in (NavaidKind.WAYPOINT, NavaidKind.NDB, NavaidKind.VOR)
        )
        self.assertEqual((waypoint.kind, waypoint.magnetic_variation_deg, waypoint.frequency_hz),
                         (NavaidKind.WAYPOINT, 2.5, None))
        self.assertEqual(ndb.frequency_hz, 385000)
        self.assertEqual((vor.vor_flags, vor.localizer_deg, vor.glideslope_angle_deg), (7, 92, 3))

    def test_cache_shares_native_subscription_and_replays_current_snapshot_for_late_join(self):
        self.core.connect()
        a = self.first.subscribe_facility_cache(FacilityKind.AIRPORT)
        self.core.pump()
        (((kind, added, removed), _),) = self.subscriptions()
        self.assertEqual(kind, 0)
        self.page(ReceiveId.AIRPORT_LIST, added, self.airport(b"YSSY"), index=0, total=2)
        self.core.pump()
        self.assertIsNone(self.first.facility_cache_snapshot(a.id))
        self.page(ReceiveId.AIRPORT_LIST, added, self.airport(b"YSBK"), index=1, total=2)
        self.page(ReceiveId.AIRPORT_LIST, added, self.airport(b"YSCB", lat=-35.3))
        minimal = pack("<c6s3s5sddd", b"A", b"YSBK", b"AS", b"", -33.9, 151.2, 28)
        self.page(ReceiveId.FACILITY_MINIMAL_LIST, removed, minimal)
        self.core.pump()
        self.assertEqual([item.change for item in self.watch_events(self.first)],
                         [FacilityChange.SNAPSHOT, FacilityChange.ADDED, FacilityChange.REMOVED])
        peer = self.core.register_consumer()
        b = peer.subscribe_facility_cache(FacilityKind.AIRPORT)
        self.assertEqual([record.icao for record in self.watch_events(peer)[0].entries], ["YSSY", "YSCB"])
        self.core.pump()
        self.assertEqual(len(self.subscriptions()), 1)
        self.first.unsubscribe(a.id)
        self.core.pump()
        self.assertFalse(self.retirements())
        peer.unsubscribe(b.id)
        self.core.pump()
        (((kind, adds, removals), _),) = self.retirements()
        self.assertEqual((kind, adds, removals), (0, True, True))

    def test_early_removals_follow_initial_snapshot_and_full_removal_is_supported(self):
        self.core.connect()
        watch = self.first.subscribe_facility_cache(FacilityKind.WAYPOINT)
        self.core.pump()
        (((kind, added, removed), _),) = self.subscriptions()
        self.assertEqual(kind, 1)
        gone = self.airport(b"GONE") + pack("<f", 1.5)
        point = self.airport(b"POINT") + pack("<f", 1.5)
        self.page(ReceiveId.WAYPOINT_LIST, removed, gone)
        self.core.pump()
        self.assertFalse(self.watch_events(self.first))
        self.page(ReceiveId.WAYPOINT_LIST, added, point, gone)
        self.core.pump()
        # GONE is in the snapshot, which is newer than its early removal (A109): it stays.
        self.assertEqual([reply.change for reply in self.watch_events(self.first)], [FacilityChange.SNAPSHOT])
        self.assertEqual([item.icao for item in self.first.facility_cache_snapshot(watch.id).entries],
                         ["POINT", "GONE"])
        self.page(ReceiveId.WAYPOINT_LIST, removed, point, gone)
        self.core.pump()
        self.assertEqual([reply.change for reply in self.watch_events(self.first)], [FacilityChange.REMOVED])
        self.assertEqual(self.first.facility_cache_snapshot(watch.id).entries, ())

    def test_early_removals_of_unlisted_facilities_follow_the_snapshot(self):
        self.core.connect()
        watch = self.first.subscribe_facility_cache(FacilityKind.WAYPOINT)
        self.core.pump()
        (((_, added, removed), _),) = self.subscriptions()
        self.page(ReceiveId.WAYPOINT_LIST, removed, self.airport(b"GONE") + pack("<f", 1.5))
        self.page(ReceiveId.WAYPOINT_LIST, added, self.airport(b"POINT") + pack("<f", 1.5))
        self.core.pump()
        events = self.watch_events(self.first)
        self.assertEqual([reply.change for reply in events], [FacilityChange.SNAPSHOT, FacilityChange.REMOVED])
        self.assertEqual([item.icao for item in events[1].entries], ["GONE"])
        self.assertEqual([item.icao for item in self.first.facility_cache_snapshot(watch.id).entries], ["POINT"])

    def test_malformed_watch_and_partial_timeout_do_not_affect_one_shot_requests(self):
        self.core.connect()
        airport = self.first.subscribe_facility_cache(FacilityKind.AIRPORT)
        vor = self.first.subscribe_facility_cache(FacilityKind.VOR)
        one_shot = self.first.request_nearby_airports()
        self.core.pump()
        ((a, _), (v, _)) = self.subscriptions()
        self.sdk.incoming.append(packet(ReceiveRequest, ReceiveId.AIRPORT_LIST, request_id=a[1]))
        self.page(ReceiveId.VOR_LIST, v[1], index=0, total=2)
        self.core.pump()
        self.assertIn("Truncated", self.first.facility_cache_snapshot(airport.id).detail)
        query = self.sdk.matching_calls("SimConnect_RequestFacilitiesList_EX1")[0][0][1]
        self.page(ReceiveId.AIRPORT_LIST, query, self.airport(b"YSSY"))
        self.now += 9
        self.core.pump()
        self.assertIn("timed out", self.first.facility_cache_snapshot(vor.id).detail)
        self.assertIs(self.first.facility_result(one_shot.id).state, FacilityState.READY)
        self.assertEqual(len(self.retirements()), 2)

    def test_reconnect_reacquires_only_active_subscriptions(self):
        self.core.connect()
        watch = self.first.subscribe_facility_cache(FacilityKind.AIRPORT)
        other = self.first.subscribe_facility_cache(FacilityKind.NDB)
        self.core.pump()
        added = self.subscriptions()[0][0][1]
        self.page(ReceiveId.AIRPORT_LIST, added, self.airport(b"YSSY"))
        self.core.pump()
        before = self.first.facility_cache_snapshot(watch.id)
        self.core.disconnect()
        self.assertIs(self.first.facility_cache_snapshot(watch.id).change, FacilityChange.UNAVAILABLE)
        self.first.unsubscribe(other.id)
        self.core.connect()
        self.core.pump()
        self.assertEqual([args[0] for args, _ in self.subscriptions()], [0, 2, 0])
        self.page(ReceiveId.AIRPORT_LIST, self.subscriptions()[-1][0][1], self.airport(b"YSCB"))
        self.core.pump()
        after = self.first.facility_cache_snapshot(watch.id)
        self.assertNotEqual(before.session_id, after.session_id)
        self.assertEqual([item.icao for item in after.entries], ["YSCB"])

    def test_last_unsubscribe_before_pump_creates_no_sdk_subscription(self):
        self.core.connect()
        watch = self.first.subscribe_facility_cache(FacilityKind.AIRPORT)
        self.first.unsubscribe(watch.id)
        self.core.pump()
        self.assertFalse(self.subscriptions())

    def test_replacement_watch_ignores_retired_request_ids_before_its_own_snapshot(self):
        self.core.connect()
        initial = self.first.subscribe_facility_cache(FacilityKind.AIRPORT)
        self.core.pump()
        old_id = self.subscriptions()[0][0][1]
        self.page(ReceiveId.AIRPORT_LIST, old_id, self.airport(b"YSSY"))
        self.core.pump()
        self.first.unsubscribe(initial.id)
        replacement = self.first.subscribe_facility_cache(FacilityKind.AIRPORT)
        self.page(ReceiveId.AIRPORT_LIST, old_id, self.airport(b"YSSY"))
        self.core.pump()
        self.assertEqual((len(self.subscriptions()), len(self.retirements())), (2, 1))
        self.assertFalse(self.watch_events(self.first))
        self.assertIsNone(self.first.facility_cache_snapshot(replacement.id))
        new_id = self.subscriptions()[-1][0][1]
        self.page(ReceiveId.AIRPORT_LIST, new_id, self.airport(b"YSCB"))
        self.core.pump()
        snapshot, = self.watch_events(self.first)
        self.assertEqual(snapshot.subscription_id, replacement.id)
        self.assertEqual(snapshot.entries[0].icao, "YSCB")

    def test_sdk_watch_rejection_is_correlated_to_its_own_kind(self):
        self.core.connect()
        airport = self.first.subscribe_facility_cache(FacilityKind.AIRPORT)
        ndb = self.first.subscribe_facility_cache(FacilityKind.NDB)
        self.core.pump()
        ((_, send), (args, _)) = self.subscriptions()
        self.sdk.exception(send)
        self.page(ReceiveId.NDB_LIST, args[1])
        self.core.pump()
        self.assertIs(self.first.facility_cache_snapshot(airport.id).change, FacilityChange.UNAVAILABLE)
        self.assertIs(self.first.facility_cache_snapshot(ndb.id).change, FacilityChange.SNAPSHOT)
        self.assertFalse(self.retirements())
