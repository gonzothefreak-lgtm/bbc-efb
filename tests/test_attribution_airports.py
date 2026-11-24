"""Airport matching, diversion assessment and facility helper tests for bbc_recorder.attribution."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bbc_core.contract import CoreRequestError, Source
from bbc_core.facilities import AirportDetails, AirportReference, FacilityResult, FacilityState, Runway
from bbc_recorder import attribution as a

T0 = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
SRC = Source("msfs", False)
DEG_M = a.EARTH_RADIUS_M * 3.141592653589793 / 180  # metres per degree of latitude


def pos(lat, lon, on_ground=True, **kw):
    kw.setdefault("observed_at", T0)
    kw.setdefault("session_id", "s1")
    return a.Position(lat, lon, on_ground, **kw)


def ap(icao, lat, lon, region="XX"):
    return AirportReference(icao, region, lat, lon, 50.0)


def result(state, data=None, *, rid="req-1", received_at=T0, session="s1", detail=""):
    return FacilityResult(rid, state, SRC, session, received_at, data, detail)


class Geometry(unittest.TestCase):
    def test_haversine(self):
        self.assertAlmostEqual(a.haversine_m(0, 0, 1, 0), DEG_M, delta=0.01)
        self.assertAlmostEqual(a.haversine_m(10, 179.9, 10, -179.9), a.haversine_m(10, 0, 10, 0.2), delta=0.01)
        self.assertEqual(a.haversine_m(5, 5, 5, 5), 0.0)

    def test_position_validation(self):
        with self.assertRaises(ValueError):
            a.Position(91, 0)
        with self.assertRaises(ValueError):
            a.Position(0, float("nan"))
        with self.assertRaises(ValueError):
            a.Position("1", 0)
        self.assertIs(a.Position(0, 0, 1).on_ground, True)

    def test_position_from_readings(self):
        values = a.readings_from_values({"PLANE LATITUDE": 51.47, "PLANE LONGITUDE": -0.46, "SIM ON GROUND": 1},
                                        observed_at=T0, session_id="s1", simulated=True)
        p = a.Position.from_readings(values)
        self.assertEqual((p.latitude_deg, p.longitude_deg, p.on_ground, p.session_id), (51.47, -0.46, True, "s1"))
        self.assertTrue(p.simulated)
        self.assertEqual(p.fields, ("PLANE LATITUDE", "PLANE LONGITUDE", "SIM ON GROUND"))
        missing_ground = a.Position.from_readings(a.readings_from_values({"PLANE LATITUDE": 1, "PLANE LONGITUDE": 2}))
        self.assertIsNone(missing_ground.on_ground)
        self.assertIsNone(a.Position.from_readings(a.readings_from_values({"PLANE LATITUDE": 1})))
        self.assertIsNone(a.Position.from_readings(a.readings_from_values(
            {"PLANE LATITUDE": None, "PLANE LONGITUDE": 2})))
        self.assertIsNone(a.Position.from_readings(a.readings_from_values(
            {"PLANE LATITUDE": 1.6, "PLANE LONGITUDE": 400})))  # radians requested by mistake or junk

    def test_runway_surface_distance(self):
        rwy = Runway(0, 10.0, 10.0, 90.0, 3000.0, 45.0, 0, 9, 0, 27, 0)
        self.assertEqual(a.runway_surface_distance_m(pos(10.0, 10.01), rwy), 0.0)   # ~1.1 km east of centre
        beside = a.runway_surface_distance_m(pos(10.001, 10.0), rwy)             # ~111 m north of centreline
        self.assertAlmostEqual(beside, 111.2 - 22.5, delta=1.0)
        beyond = a.runway_surface_distance_m(pos(10.0, 10.0 + 2000 / (DEG_M * 0.98481)), rwy)
        self.assertAlmostEqual(beyond, 500.0, delta=2.0)
        self.assertIsNone(a.runway_surface_distance_m(pos(10, 10), SimpleNamespace(latitude_deg=10)))
        bad = Runway(0, 10.0, 10.0, 90.0, 0.0, 45.0, 0, 9, 0, 27, 0)
        self.assertIsNone(a.runway_surface_distance_m(pos(10, 10), bad))


class MatchAirport(unittest.TestCase):
    def test_known_high(self):
        m = a.match_airport(pos(51.4706, -0.4619), [ap("EGLL", 51.4775, -0.4614), ap("EGKK", 51.1481, -0.1903)],
                            role="departure", request_id="r1", received_at=T0)
        self.assertEqual((m.icao, m.airport.status, m.airport.confidence),
                         ("EGLL", a.Status.KNOWN, a.Confidence.HIGH))
        self.assertEqual(m.role, "departure")
        self.assertEqual([c.icao for c in m.candidates], ["EGLL"])
        prov = m.airport.provenance
        self.assertEqual(prov.method, "facility.nearby_airports")
        self.assertEqual(prov.request_id, "r1")
        self.assertEqual(prov.fields, a.POSITION_FIELDS)
        self.assertEqual(prov.origin, a.Origin.DERIVED)
        self.assertIn("EGLL", m.airport.reason)
        json.dumps(m.to_json())

    def test_confidence_by_distance(self):
        policy = a.AirportMatchPolicy()
        medium = a.match_airport(pos(0, 0), [ap("MED1", 2500 / DEG_M, 0)], policy=policy)
        self.assertEqual(medium.airport.confidence, a.Confidence.MEDIUM)
        low = a.match_airport(pos(0, 0), [ap("LOW1", 5000 / DEG_M, 0)], policy=policy)
        self.assertEqual((low.icao, low.airport.confidence), ("LOW1", a.Confidence.LOW))

    def test_nothing_within_threshold(self):
        m = a.match_airport(pos(0, 0), [ap("FAR1", 0.1, 0)])
        self.assertIsNone(m.icao)
        self.assertEqual(m.airport.status, a.Status.UNKNOWN)
        self.assertIn("FAR1", m.airport.reason)
        self.assertIn("11.1 km", m.airport.reason)
        self.assertEqual(m.candidates, ())
        self.assertEqual(m.airport.provenance.origin, a.Origin.UNAVAILABLE)
        empty = a.match_airport(pos(0, 0), [])
        self.assertIn("no nearby airports", empty.airport.reason)

    def test_threshold_is_configurable(self):
        policy = a.AirportMatchPolicy(max_distance_m=15000, high_confidence_m=1500)
        self.assertEqual(a.match_airport(pos(0, 0), [ap("FAR1", 0.1, 0)], policy=policy).icao, "FAR1")

    def test_airborne_and_no_position(self):
        air = a.match_airport(pos(0, 0, False), [ap("AAA1", 0, 0)])
        self.assertIsNone(air.icao)
        self.assertIn("airborne", air.airport.reason)
        allowed = a.match_airport(pos(0, 0, False), [ap("AAA1", 0, 0)],
                                  policy=a.AirportMatchPolicy(require_on_ground=False))
        self.assertEqual(allowed.icao, "AAA1")
        self.assertIsNone(a.match_airport(None, [ap("AAA1", 0, 0)]).icao)

    def test_unknown_ground_and_stale_list_downgrade(self):
        unknown_ground = a.match_airport(pos(0, 0, None), [ap("AAA1", 0, 0)])
        self.assertEqual(unknown_ground.airport.confidence, a.Confidence.MEDIUM)
        self.assertIn("ground state was unknown", unknown_ground.airport.reason)
        stale = a.match_airport(pos(0, 0), [ap("AAA1", 0, 0)], received_at=T0 - timedelta(minutes=5))
        self.assertEqual(stale.airport.confidence, a.Confidence.MEDIUM)
        self.assertIn("300 s", stale.airport.reason)
        both = a.match_airport(pos(0, 0, None), [ap("AAA1", 0, 0)], received_at=T0 + timedelta(minutes=5))
        self.assertEqual(both.airport.confidence, a.Confidence.LOW)

    def test_session_mismatch(self):
        m = a.match_airport(pos(0, 0), [ap("AAA1", 0, 0)], result_session_id="other")
        self.assertIsNone(m.icao)
        self.assertIn("different simulator session", m.airport.reason)

    def test_ambiguous(self):
        m = a.match_airport(pos(0, 0.001), [ap("AAA1", 0, 0), ap("BBB2", 0, 0.005), ap("CCC3", 1, 1)])
        self.assertEqual((m.icao, m.airport.status, m.airport.confidence), ("AAA1", a.Status.AMBIGUOUS, a.Confidence.LOW))
        self.assertEqual([c.icao for c in m.candidates], ["AAA1", "BBB2"])
        self.assertTrue(m.airport.needs_pilot)
        self.assertIn("BBB2", m.airport.reason)
        not_ambiguous = a.match_airport(pos(0, 0.001), [ap("AAA1", 0, 0), ap("BBB2", 0, 0.005)],
                                        policy=a.AirportMatchPolicy(ambiguity_margin_m=100))
        self.assertEqual(not_ambiguous.airport.status, a.Status.KNOWN)

    def test_runway_details_refine(self):
        rwy = Runway(0, 10.0, 10.0, 90.0, 3000.0, 45.0, 0, 9, 0, 27, 0)
        big = AirportDetails(ap("BIG1", 10.03, 10.0), "Big", (rwy,), (), ())
        near = ap("NEAR", 10.0, 10.04)  # ~3.3 km east of the aircraft
        position = pos(10.0, 10.01)
        plain = a.match_airport(position, [big.airport, near])
        self.assertEqual(plain.airport.status, a.Status.AMBIGUOUS)
        refined = a.match_airport(position, [big.airport, near], details={"BIG1": big})
        self.assertEqual((refined.icao, refined.airport.status, refined.airport.confidence),
                         ("BIG1", a.Status.KNOWN, a.Confidence.HIGH))
        self.assertIn("on a runway of BIG1", refined.airport.reason)
        self.assertEqual(refined.candidates[0].runway_distance_m, 0.0)

    def test_bad_entries_duplicates_and_limit(self):
        entries = [SimpleNamespace(icao="", latitude_deg=0, longitude_deg=0),
                   SimpleNamespace(icao="BAD!", latitude_deg=0, longitude_deg=0),
                   SimpleNamespace(icao="NOPOS", latitude_deg=None, longitude_deg=0),
                   ap("dup1", 0.01, 0), ap("DUP1", 0.001, 0)]
        entries += [ap(f"A{i}", 0.002 * i, 0.002) for i in range(1, 9)]
        m = a.match_airport(pos(0, 0), entries, policy=a.AirportMatchPolicy(max_candidates=3))
        self.assertEqual(len(m.candidates), 3)
        self.assertEqual(m.candidates[0].icao, "DUP1")
        self.assertAlmostEqual(m.candidates[0].distance_m, 0.001 * DEG_M, delta=0.5)
        self.assertNotIn("BAD!", [c.icao for c in a.rank_airports(pos(0, 0), entries)])

    def test_policy_validation(self):
        with self.assertRaises(ValueError):
            a.AirportMatchPolicy(max_distance_m=-1)
        with self.assertRaises(ValueError):
            a.AirportMatchPolicy(max_distance_m=1000, high_confidence_m=2000)
        with self.assertRaises(ValueError):
            a.AirportMatchPolicy(max_candidates=0)
        with self.assertRaises(ValueError):
            a.AirportMatchPolicy(ambiguity_margin_m=float("inf"))


class FacilityResults(unittest.TestCase):
    def test_states(self):
        p = pos(0, 0)
        self.assertIsNone(a.match_from_facility_result(p, result(FacilityState.PENDING)))
        ready = a.match_from_facility_result(p, result(FacilityState.READY, (ap("AAA1", 0, 0),)), role="arrival")
        self.assertEqual((ready.icao, ready.role, ready.airport.provenance.request_id), ("AAA1", "arrival", "req-1"))
        self.assertEqual(ready.airport.provenance.facility_received_at, T0)
        empty = a.match_from_facility_result(p, result(FacilityState.READY, ()))
        self.assertIn("no nearby airports", empty.airport.reason)
        none = a.match_from_facility_result(p, result(FacilityState.READY, None))
        self.assertIsNone(none.icao)
        no_data = a.match_from_facility_result(p, result(FacilityState.NO_DATA))
        self.assertIn("no airport record", no_data.airport.reason)
        unavailable = a.match_from_facility_result(p, result(FacilityState.UNAVAILABLE, detail="Not connected."))
        self.assertIn("(Not connected.)", unavailable.airport.reason)
        self.assertEqual(unavailable.airport.status, a.Status.UNKNOWN)
        other = a.match_from_facility_result(p, result(FacilityState.READY, (ap("AAA1", 0, 0),), session="s2"))
        self.assertIsNone(other.icao)

    def test_airport_details_reply(self):
        rwy = Runway(0, 10.0, 10.0, 90.0, 3000.0, 45.0, 0, 9, 0, 27, 0)
        details = AirportDetails(ap("BIG1", 10.03, 10.0), "Big", (rwy,), (), ())
        m = a.match_from_facility_result(pos(10.0, 10.01), result(FacilityState.READY, details))
        self.assertEqual((m.icao, m.airport.confidence), ("BIG1", a.Confidence.HIGH))

    def test_duck_typed_string_state(self):
        duck = SimpleNamespace(id="x", state="ready", session_id=None, received_at=None,
                               data=[SimpleNamespace(icao="aaa1", region=None, latitude_deg=0, longitude_deg=0)])
        self.assertEqual(a.match_from_facility_result(pos(0, 0), duck).icao, "AAA1")


def arrival_at(icao_list, position):
    return a.match_airport(position, icao_list, role="arrival")


class ArrivalAssessment(unittest.TestCase):
    def setUp(self):
        self.p = pos(0, 0)
        self.at_aaa = arrival_at([ap("AAA1", 0, 0)], self.p)

    def test_no_plan(self):
        r = a.assess_arrival(self.at_aaa, None)
        self.assertEqual(r.verdict, a.ArrivalVerdict.NO_PLAN)
        self.assertFalse(r.needs_pilot_confirmation)
        self.assertEqual(r.logbook_arrival().value, "AAA1")
        bad = a.assess_arrival(self.at_aaa, "not an icao")
        self.assertEqual(bad.verdict, a.ArrivalVerdict.NO_PLAN)
        self.assertIn("not a valid identifier", bad.reason)
        unknown = a.assess_arrival(arrival_at([], self.p), "")
        self.assertTrue(unknown.needs_pilot_confirmation)

    def test_matches_plan(self):
        r = a.assess_arrival(self.at_aaa, "aaa1")
        self.assertEqual(r.verdict, a.ArrivalVerdict.MATCHES_PLAN)
        self.assertFalse(r.resolved_by_plan or r.needs_pilot_confirmation)
        field = r.logbook_arrival()
        self.assertEqual((field.value, field.status, field.confidence), ("AAA1", a.Status.KNOWN, a.Confidence.HIGH))

    def test_diversion_suspected_never_confirmed(self):
        r = a.assess_arrival(self.at_aaa, "BBB2", alternates=["aaa1"], departure_icao="CCC3")
        self.assertEqual(r.verdict, a.ArrivalVerdict.DIVERSION_SUSPECTED)
        self.assertEqual(str(r.verdict), "diversion_suspected")
        self.assertTrue(r.needs_pilot_confirmation)
        self.assertFalse(r.diversion_confirmed)
        self.assertEqual(r.matched_alternate, "AAA1")
        self.assertFalse(r.returned_to_departure)
        self.assertIn("planned alternate", r.reason)
        self.assertIn("Please confirm", r.reason)
        field = r.logbook_arrival()
        self.assertEqual((field.value, field.status), ("AAA1", a.Status.NEEDS_CONFIRMATION))
        json.dumps(r.to_json())

    def test_return_to_departure(self):
        r = a.assess_arrival(self.at_aaa, "BBB2", departure_icao="AAA1")
        self.assertEqual(r.verdict, a.ArrivalVerdict.DIVERSION_SUSPECTED)
        self.assertTrue(r.returned_to_departure)
        self.assertIn("departure airport", r.reason)

    def test_ambiguous_resolved_by_plan(self):
        actual = arrival_at([ap("AAA1", 0, 0), ap("BBB2", 0, 0.005)], pos(0, 0.001))
        r = a.assess_arrival(actual, "BBB2")
        self.assertEqual(r.verdict, a.ArrivalVerdict.MATCHES_PLAN)
        self.assertTrue(r.resolved_by_plan)
        self.assertEqual(r.logbook_arrival().value, "BBB2")
        nearest = a.assess_arrival(actual, "AAA1")
        self.assertEqual(nearest.verdict, a.ArrivalVerdict.MATCHES_PLAN)
        self.assertTrue(nearest.resolved_by_plan)

    def test_planned_position_resolves(self):
        # The plan's airport is 600 m further than the nearest airport (within the 750 m margin).
        actual = arrival_at([ap("AAA1", 0, 0.001)], self.p)
        r = a.assess_arrival(actual, "PLAN", planned_position=(0, 700 / DEG_M))
        self.assertEqual(r.verdict, a.ArrivalVerdict.MATCHES_PLAN)
        self.assertTrue(r.resolved_by_plan)
        far = a.assess_arrival(actual, "PLAN", planned_position=(0, 3000 / DEG_M))
        self.assertEqual(far.verdict, a.ArrivalVerdict.DIVERSION_SUSPECTED)
        bad = a.assess_arrival(actual, "PLAN", planned_position=(float("nan"), 0))
        self.assertEqual(bad.verdict, a.ArrivalVerdict.DIVERSION_SUSPECTED)

    def test_unknown_arrival(self):
        nothing = arrival_at([], self.p)
        r = a.assess_arrival(nothing, "BBB2")
        self.assertEqual(r.verdict, a.ArrivalVerdict.ARRIVAL_UNKNOWN)
        self.assertTrue(r.needs_pilot_confirmation)
        self.assertIsNone(r.logbook_arrival().value)
        near_plan = a.assess_arrival(nothing, "BBB2", planned_position=(0.01, 0))
        self.assertEqual(near_plan.verdict, a.ArrivalVerdict.MATCHES_PLAN)
        self.assertIn("1.1 km", near_plan.reason)
        self.assertEqual(near_plan.logbook_arrival().confidence, a.Confidence.MEDIUM)

    def test_pilot_decisions(self):
        suspected = a.assess_arrival(self.at_aaa, "BBB2")
        confirmed = a.record_pilot_diversion_decision(suspected, confirmed=True, decided_at=T0)
        self.assertEqual(confirmed.verdict, a.ArrivalVerdict.DIVERSION_CONFIRMED)
        self.assertTrue(confirmed.diversion_confirmed)
        self.assertFalse(confirmed.needs_pilot_confirmation)
        self.assertEqual(confirmed.logbook_arrival().value, "AAA1")
        self.assertEqual(confirmed.pilot_decided_at, T0)
        rejected = a.record_pilot_diversion_decision(suspected, confirmed=False, decided_at=T0)
        self.assertEqual((rejected.verdict, rejected.pilot_arrival), (a.ArrivalVerdict.DIVERSION_REJECTED, "BBB2"))
        corrected = a.record_pilot_diversion_decision(suspected, confirmed=False, decided_at=T0, arrival_icao="aaa1")
        self.assertEqual(corrected.logbook_arrival().value, "AAA1")
        with self.assertRaises(ValueError):
            a.record_pilot_diversion_decision(a.assess_arrival(self.at_aaa, "AAA1"), confirmed=True, decided_at=T0)
        with self.assertRaises(ValueError):
            a.record_pilot_diversion_decision(suspected, confirmed=True, decided_at="now")
        with self.assertRaises(ValueError):
            a.record_pilot_diversion_decision(suspected, confirmed=True, decided_at=T0, arrival_icao="BBB2")
        unknown = a.assess_arrival(arrival_at([], self.p), "BBB2")
        with self.assertRaises(ValueError):
            a.record_pilot_diversion_decision(unknown, confirmed=True, decided_at=T0)
        entered = a.record_pilot_diversion_decision(unknown, confirmed=True, decided_at=T0, arrival_icao="ZZZ9")
        self.assertEqual(entered.pilot_arrival, "ZZZ9")


class FakeApi:
    def __init__(self, first, later=None, raises=None):
        self.first = first
        self.later = later
        self.raises = raises
        self.requests = 0
        self.pulls = 0

    def request_nearby_airports(self):
        self.requests += 1
        if self.raises:
            raise self.raises
        return self.first

    def facility_result(self, request_id):
        self.pulls += 1
        return self.later


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Lookup(unittest.TestCase):
    def test_protocol(self):
        self.assertIsInstance(FakeApi(None), a.FacilitySource)

    def test_immediate_ready(self):
        api = FakeApi(result(FacilityState.READY, (ap("AAA1", 0, 0),)))
        lookup = a.NearbyAirportLookup(api)
        lookup.begin("departure", pos(0, 0))
        self.assertEqual(lookup.pending, ())
        done = lookup.check()
        self.assertEqual([(d.role, d.icao) for d in done], [("departure", "AAA1")])
        self.assertEqual(lookup.check(), ())

    def test_offer_from_poll(self):
        api = FakeApi(result(FacilityState.PENDING))
        lookup = a.NearbyAirportLookup(api)
        dep = lookup.begin("departure", pos(0, 0))
        arr = lookup.begin("arrival", pos(0, 0))  # identical queries share one Core request id
        self.assertEqual(set(lookup.pending), {dep, arr})
        self.assertEqual(lookup.offer(SimpleNamespace(state="connected")), ())  # unrelated Connection-like update
        self.assertEqual(lookup.offer(result(FacilityState.READY, (), rid="other")), ())
        done = lookup.offer(result(FacilityState.READY, (ap("AAA1", 0, 0),)))
        self.assertEqual(sorted(d.role for d in done), ["arrival", "departure"])
        self.assertEqual(lookup.pending, ())

    def test_check_pulls_and_times_out(self):
        clock = Clock()
        api = FakeApi(result(FacilityState.PENDING), later=result(FacilityState.PENDING))
        lookup = a.NearbyAirportLookup(api, timeout_s=5, clock=clock)
        lookup.begin("arrival", pos(0, 0))
        self.assertEqual(lookup.check(), ())
        clock.now += 5
        done = lookup.check()
        self.assertEqual(len(done), 1)
        self.assertIsNone(done[0].icao)
        self.assertIn("did not answer", done[0].airport.reason)
        self.assertEqual(api.pulls, 2)

    def test_check_pull_completes(self):
        api = FakeApi(result(FacilityState.PENDING), later=result(FacilityState.READY, (ap("AAA1", 0, 0),)))
        lookup = a.NearbyAirportLookup(api)
        lookup.begin("arrival", pos(0, 0))
        self.assertEqual([d.icao for d in lookup.check()], ["AAA1"])

    def test_request_error_and_no_position(self):
        api = FakeApi(None, raises=CoreRequestError("Not connected to the simulator."))
        lookup = a.NearbyAirportLookup(api)
        lookup.begin("departure", pos(0, 0))
        lookup.begin("arrival", None)
        done = lookup.check()
        self.assertIn("Not connected", done[0].airport.reason)
        self.assertIn("position is unknown", done[1].airport.reason)
        self.assertEqual(api.requests, 1)

    def test_cancel_and_validation(self):
        lookup = a.NearbyAirportLookup(FakeApi(result(FacilityState.PENDING)))
        token = lookup.begin("departure", pos(0, 0))
        lookup.cancel(token)
        self.assertEqual(lookup.offer(result(FacilityState.READY, ())), ())
        with self.assertRaises(ValueError):
            a.NearbyAirportLookup(FakeApi(None), timeout_s=0)

    def test_unavailable_result(self):
        lookup = a.NearbyAirportLookup(FakeApi(result(FacilityState.PENDING)))
        lookup.begin("departure", pos(0, 0))
        done = lookup.offer(result(FacilityState.UNAVAILABLE, detail="Request timed out."))
        self.assertIn("Request timed out", done[0].airport.reason)


def run_coroutine(coroutine):
    """Drive a coroutine whose awaits never suspend (the injected sleep only moves a fake clock).

    asyncio.run() would create an event loop, and its self-pipe is a socket, which the guarded
    offline runner (tests/test_repo_offline_guard.py) blocks.
    """
    try:
        while True:
            coroutine.send(None)
    except StopIteration as stop:
        return stop.value


class AsyncResolve(unittest.TestCase):
    def test_resolves(self):
        clock = Clock()
        replies = [None, result(FacilityState.PENDING), result(FacilityState.READY, (ap("AAA1", 0, 0),))]

        class Api(FakeApi):
            def facility_result(self, request_id):
                return replies.pop(0) if replies else None

        async def sleep(seconds):
            clock.now += seconds

        found = run_coroutine(a.resolve_airport(Api(result(FacilityState.PENDING)), pos(0, 0), role="departure",
                                              sleep=sleep, clock=clock))
        self.assertEqual((found.role, found.icao), ("departure", "AAA1"))

    def test_times_out(self):
        clock = Clock()

        async def sleep(seconds):
            clock.now += seconds

        found = run_coroutine(a.resolve_airport(FakeApi(result(FacilityState.PENDING)), pos(0, 0), timeout_s=1,
                                              interval_s=0.5, sleep=sleep, clock=clock))
        self.assertIsNone(found.icao)
        self.assertIn("within 1 s", found.airport.reason)

    def test_immediate_and_validation(self):
        found = run_coroutine(a.resolve_airport(FakeApi(result(FacilityState.READY, (ap("AAA1", 0, 0),))), pos(0, 0)))
        self.assertEqual(found.icao, "AAA1")
        with self.assertRaises(ValueError):
            run_coroutine(a.resolve_airport(FakeApi(None), pos(0, 0), interval_s=0))


if __name__ == "__main__":
    unittest.main()
