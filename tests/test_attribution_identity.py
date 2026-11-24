"""Aircraft identity, payload and provenance tests for bbc_recorder.attribution."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bbc_recorder import attribution as a

T0 = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def readings(values, **kw):
    kw.setdefault("observed_at", T0)
    kw.setdefault("session_id", "s1")
    kw.setdefault("source_name", "msfs")
    kw.setdefault("simulated", False)
    return a.readings_from_values(values, **kw)


A320 = {
    "TITLE": "Airbus A320 Neo Asobo",
    "ATC TYPE": "TT:ATCCOM.ATC_NAME AIRBUS.0.text",
    "ATC MODEL": "TT:ATCCOM.AC_MODEL A320.0.text",
    "ATC ID": "F-HBXA",
    "ATC AIRLINE": "BAW",
    "ATC FLIGHT NUMBER": "123",
}


class TextNormalisation(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(a.clean_text("  A320\x00garbage"), "A320")
        self.assertEqual(a.clean_text("Air\tbus   A320 \n"), "Air bus A320")
        self.assertEqual(a.clean_text(b"B738\x00\xff"), "B738")
        self.assertIsNone(a.clean_text(b"\xff\xfe"))
        self.assertIsNone(a.clean_text("   "))
        self.assertIsNone(a.clean_text("\x00abc"))
        self.assertIsNone(a.clean_text(None))
        self.assertIsNone(a.clean_text(42))

    def test_localisation_keys(self):
        self.assertEqual(a.unwrap_localisation_key("TT:ATCCOM.AC_MODEL A320.0.text"), ("A320", "AC_MODEL"))
        self.assertEqual(a.unwrap_localisation_key("TT:ATCCOM.AC_MODEL_B738.0.text"), ("B738", "AC_MODEL"))
        self.assertEqual(a.unwrap_localisation_key("ATCCOM.ATC_NAME DIAMOND.0.TEXT"), ("DIAMOND", "ATC_NAME"))
        self.assertEqual(a.unwrap_localisation_key("Boeing"), ("Boeing", None))

    def test_type_designator(self):
        for raw, expected in [
            ("TT:ATCCOM.AC_MODEL A320.0.text", "A320"), ("TT:ATCCOM.AC_MODEL_B748.0.text", "B748"),
            ("B738", "B738"), (" a20n ", "A20N"), ("C172", "C172"), ("DH8D", "DH8D"),
            ("ZZZZ", None), ("Kodiak 100", None), ("C172 Skyhawk", None), ("", None), (None, None),
            ("1234", None), ("A", None), ("A3200", None),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(a.type_designator(raw), expected)

    def test_airport_ident(self):
        self.assertEqual(a.normalise_airport_ident(" egll "), "EGLL")
        self.assertEqual(a.normalise_airport_ident("K2W6"), "K2W6")
        self.assertIsNone(a.normalise_airport_ident("EG-LL"))
        self.assertIsNone(a.normalise_airport_ident("TOOLONGID"))
        self.assertIsNone(a.normalise_airport_ident(None))


class AircraftType(unittest.TestCase):
    def test_full_identity_from_localisation_keys(self):
        result = a.aircraft_type(readings(A320))
        self.assertEqual(result.designator.value, "A320")
        self.assertEqual(result.designator.status, a.Status.KNOWN)
        self.assertEqual(result.designator.confidence, a.Confidence.MEDIUM)
        self.assertEqual(result.designator.provenance.origin, a.Origin.DERIVED)
        self.assertEqual(result.designator.provenance.method, "atc_model.localisation_key")
        self.assertEqual(result.designator.provenance.fields, ("ATC MODEL",))
        self.assertEqual(result.manufacturer.value, "Airbus")
        self.assertEqual(result.display.value, "Airbus A320")
        self.assertEqual(result.group_key, "A320")
        self.assertIn("livery", result.title.reason)

    def test_plain_model_is_observed(self):
        result = a.aircraft_type(readings({"ATC MODEL": "B738", "ATC TYPE": "Boeing", "TITLE": "x"}))
        self.assertEqual(result.designator.provenance.origin, a.Origin.OBSERVED)
        self.assertEqual(result.display.value, "Boeing B738")
        self.assertEqual(result.manufacturer.provenance.origin, a.Origin.OBSERVED)

    def test_manufacturer_not_repeated(self):
        result = a.aircraft_type(readings({"ATC MODEL": "C172", "ATC TYPE": "C172"}))
        self.assertEqual(result.display.value, "C172")

    def test_title_fallback_needs_confirmation(self):
        result = a.aircraft_type(readings({"ATC MODEL": "Kodiak 100", "TITLE": "Kodiak 100 Wheels Asobo"}))
        self.assertIsNone(result.designator.value)
        self.assertEqual(result.designator.status, a.Status.UNKNOWN)
        self.assertIn("Kodiak 100", result.designator.reason)
        self.assertIsNone(result.group_key)
        self.assertEqual(result.display.value, "Kodiak 100 Wheels Asobo")
        self.assertEqual(result.display.status, a.Status.NEEDS_CONFIRMATION)
        self.assertEqual(result.display.confidence, a.Confidence.LOW)
        self.assertIn("confirm", result.display.reason)

    def test_atc_type_fallback(self):
        result = a.aircraft_type(readings({"ATC TYPE": "TT:ATCCOM.ATC_NAME CESSNA.0.text"}))
        self.assertEqual(result.display.value, "Cessna")
        self.assertEqual(result.display.status, a.Status.NEEDS_CONFIRMATION)

    def test_nothing_known(self):
        result = a.aircraft_type({})
        for item in (result.designator, result.manufacturer, result.title, result.display):
            self.assertIsNone(item.value)
            self.assertEqual(item.status, a.Status.UNKNOWN)
            self.assertEqual(item.confidence, a.Confidence.NONE)
        self.assertIn("not requested or not received", result.designator.reason)

    def test_empty_strings_are_unknown_with_provenance(self):
        result = a.aircraft_type(readings({"ATC MODEL": "", "TITLE": "\x00", "ATC TYPE": "  "}))
        self.assertEqual(result.designator.status, a.Status.UNKNOWN)
        self.assertEqual(result.designator.provenance.origin, a.Origin.UNAVAILABLE)
        self.assertEqual(result.designator.provenance.raw, (("ATC MODEL", ""),))
        self.assertEqual(result.display.status, a.Status.UNKNOWN)

    def test_title_alias(self):
        policy = a.IdentityPolicy(title_aliases={"Kodiak 100 Wheels Asobo": "kodi"})
        result = a.aircraft_type(readings({"ATC MODEL": "Kodiak", "TITLE": "KODIAK 100 wheels asobo"}), policy)
        self.assertEqual(result.designator.value, "KODI")
        self.assertEqual(result.designator.provenance.method, "configured_title_alias")
        self.assertEqual(result.display.status, a.Status.KNOWN)
        self.assertEqual(hash(policy), hash(a.IdentityPolicy(title_aliases=(("kodiak 100 wheels asobo", "KODI"),))))

    def test_model_beats_alias(self):
        policy = a.IdentityPolicy(title_aliases={"Airbus A320 Neo Asobo": "A20N"})
        self.assertEqual(a.aircraft_type(readings(A320), policy).designator.value, "A320")

    def test_invalid_alias_rejected(self):
        with self.assertRaises(ValueError):
            a.IdentityPolicy(title_aliases={"title": "not a type"})
        with self.assertRaises(ValueError):
            a.IdentityPolicy(title_aliases={"": "A320"})

    def test_livery_change_keeps_group(self):
        before = a.identify_aircraft(readings(A320))
        after = a.identify_aircraft(readings({**A320, "TITLE": "Airbus A320 Neo British Airways"}))
        self.assertEqual(a.identity_changes(before, after), ("title",))
        self.assertEqual(before.type.group_key, after.type.group_key)
        changed = a.identify_aircraft(readings({**A320, "ATC MODEL": "B738", "ATC ID": "G-ABCD"}))
        self.assertEqual(a.identity_changes(before, changed), ("type", "registration"))
        self.assertEqual(len(a.identity_changes(None, before)), 4)


class Registration(unittest.TestCase):
    def test_valid(self):
        reg = a.registration(readings({"ATC ID": "G-EUPT"}))
        self.assertEqual((reg.value, reg.status, reg.confidence), ("G-EUPT", a.Status.KNOWN, a.Confidence.MEDIUM))
        self.assertEqual(reg.provenance.origin, a.Origin.OBSERVED)
        self.assertIn("ATC ID", reg.reason)

    def test_normalised_is_derived(self):
        reg = a.registration(readings({"ATC ID": " n 123ab "}))
        self.assertEqual(reg.value, "N123AB")
        self.assertEqual(reg.provenance.origin, a.Origin.DERIVED)
        self.assertEqual(reg.provenance.raw, (("ATC ID", " n 123ab "),))

    def test_odd_value_needs_confirmation(self):
        reg = a.registration(readings({"ATC ID": "My plane!"}))
        self.assertEqual(reg.value, "My plane!")
        self.assertEqual(reg.status, a.Status.NEEDS_CONFIRMATION)
        self.assertTrue(reg.needs_pilot)

    def test_missing_and_empty(self):
        self.assertEqual(a.registration({}).status, a.Status.UNKNOWN)
        empty = a.registration(readings({"ATC ID": ""}))
        self.assertIsNone(empty.value)
        self.assertEqual(empty.provenance.origin, a.Origin.UNAVAILABLE)


class Callsign(unittest.TestCase):
    def test_icao_compact(self):
        result = a.callsign(readings({"ATC AIRLINE": "BAW", "ATC FLIGHT NUMBER": "123"}))
        self.assertEqual(result.callsign.value, "BAW123")
        self.assertEqual(result.callsign.status, a.Status.KNOWN)
        self.assertIn("not a VATSIM", result.callsign.reason)
        self.assertEqual(result.callsign.provenance.fields, ("ATC AIRLINE", "ATC FLIGHT NUMBER"))
        self.assertEqual(result.callsign.provenance.origin, a.Origin.DERIVED)

    def test_spoken(self):
        result = a.callsign(readings({"ATC AIRLINE": "Speedbird", "ATC FLIGHT NUMBER": "12a"}))
        self.assertEqual(result.callsign.value, "Speedbird 12A")
        no_compact = a.callsign(readings({"ATC AIRLINE": "BAW", "ATC FLIGHT NUMBER": "1"}),
                                a.IdentityPolicy(compact_icao_callsign=False))
        self.assertEqual(no_compact.callsign.value, "BAW 1")

    def test_partial_is_unknown(self):
        only_number = a.callsign(readings({"ATC FLIGHT NUMBER": "123", "ATC AIRLINE": ""}))
        self.assertIsNone(only_number.callsign.value)
        self.assertEqual(only_number.flight_number.value, "123")
        self.assertIn("no airline", only_number.callsign.reason)
        only_airline = a.callsign(readings({"ATC AIRLINE": "Speedbird"}))
        self.assertIsNone(only_airline.callsign.value)
        self.assertIn("no flight number", only_airline.callsign.reason)
        self.assertEqual(a.callsign({}).callsign.status, a.Status.UNKNOWN)

    def test_odd_number(self):
        result = a.callsign(readings({"ATC AIRLINE": "BAW", "ATC FLIGHT NUMBER": "12-34"}))
        self.assertEqual(result.callsign.value, "BAW 12-34")
        self.assertEqual(result.callsign.status, a.Status.NEEDS_CONFIRMATION)

    def test_registration_fallback_off_by_default(self):
        values = readings({"ATC ID": "G-ABCD"})
        self.assertIsNone(a.identify_aircraft(values).callsign.callsign.value)
        policy = a.IdentityPolicy(registration_as_callsign=True)
        sign = a.identify_aircraft(values, policy).callsign.callsign
        self.assertEqual(sign.value, "G-ABCD")
        self.assertEqual(sign.status, a.Status.NEEDS_CONFIRMATION)
        self.assertEqual(sign.provenance.method, "registration_as_callsign")


class Provenance(unittest.TestCase):
    def test_snapshot_conversion(self):
        snapshot = SimpleNamespace(
            subscription_id="sub", names=("TITLE", "ATC ID", "PAYLOAD STATION WEIGHT:1"),
            values=("A320", None, 170.0), simulation_time_s=42.5, absolute_time_s=6.4e10, simulation_rate=1.0,
            source=SimpleNamespace(name="simulated", simulated=True), session_id="sess-9",
            received_at=T0, sample_time=T0 - timedelta(seconds=1),
        )
        values = a.readings_from_snapshot(snapshot)
        self.assertEqual(set(values), {"TITLE", "ATC ID", "PAYLOAD STATION WEIGHT:1"})
        title = values["TITLE"]
        self.assertEqual((title.observed_at, title.session_id, title.sim_time_s), (T0, "sess-9", 42.5))
        self.assertTrue(title.simulated)
        reg = a.registration(values)
        self.assertEqual(reg.status, a.Status.UNKNOWN)  # undecodable stays unknown
        typed = a.aircraft_type(values)
        self.assertTrue(typed.title.provenance.simulated)
        self.assertEqual(typed.title.provenance.sim_utc, T0 - timedelta(seconds=1))

    def test_newest_reading_and_simulated_flag(self):
        older = a.FieldReading("ATC AIRLINE", "BAW", T0, "s1", "msfs", False)
        newer = a.FieldReading("ATC FLIGHT NUMBER", "1", T0 + timedelta(seconds=5), "s1", "sim", True)
        sign = a.callsign({"ATC AIRLINE": older, "ATC FLIGHT NUMBER": newer}).callsign
        self.assertEqual(sign.provenance.observed_at, T0 + timedelta(seconds=5))
        self.assertTrue(sign.provenance.simulated)

    def test_merge_readings_prefers_newer(self):
        first = readings({"TITLE": "new"}, observed_at=T0 + timedelta(seconds=1))
        second = readings({"TITLE": "old", "ATC ID": "X"}, observed_at=T0)
        merged = a.merge_readings(first, second)
        self.assertEqual(merged["TITLE"].value, "new")
        self.assertEqual(merged["ATC ID"].value, "X")

    def test_json(self):
        identity = a.identify_aircraft(readings(A320))
        text = json.dumps(identity.to_json())
        self.assertIn("rules_version", text)
        fields = identity.record_fields()
        self.assertEqual(fields["aircraft"]["value"], "Airbus A320")
        self.assertEqual(fields["registration"]["provenance"], "observed")
        self.assertEqual(fields["callsign"]["evidence"]["fields"], ["ATC AIRLINE", "ATC FLIGHT NUMBER"])
        self.assertEqual(fields["aircraft_type"]["original"], "A320")
        self.assertEqual(a.unknown("x").to_record_field()["provenance"], "unavailable")
        json.dumps(fields)

    def test_lower_confidence(self):
        self.assertEqual(a.lower_confidence(a.Confidence.HIGH), a.Confidence.MEDIUM)
        self.assertEqual(a.lower_confidence(a.Confidence.LOW), a.Confidence.LOW)
        self.assertEqual(a.lower_confidence(a.Confidence.HIGH, 5), a.Confidence.LOW)
        self.assertEqual(a.lower_confidence(a.Confidence.NONE), a.Confidence.NONE)
        self.assertEqual(f"{a.Status.KNOWN}", "known")


class Payload(unittest.TestCase):
    def test_unsupported(self):
        result = a.payload(readings({"PAYLOAD STATION COUNT": 2}), supported=False)
        self.assertEqual(result.station_total_kg.status, a.Status.UNSUPPORTED)
        self.assertEqual(result.stations, ())

    def test_missing_count(self):
        self.assertEqual(a.payload({}).station_total_kg.status, a.Status.UNKNOWN)
        bad = a.payload(readings({"PAYLOAD STATION COUNT": None}))
        self.assertEqual(bad.station_total_kg.provenance.origin, a.Origin.UNAVAILABLE)
        self.assertEqual(a.payload(readings({"PAYLOAD STATION COUNT": 1.5})).station_total_kg.status, a.Status.UNKNOWN)
        self.assertEqual(a.payload(readings({"PAYLOAD STATION COUNT": True})).station_total_kg.status,
                         a.Status.UNKNOWN)

    def test_zero_stations(self):
        result = a.payload(readings({"PAYLOAD STATION COUNT": 0}))
        self.assertEqual((result.station_total_kg.value, result.station_total_kg.status), (0.0, a.Status.KNOWN))

    def test_complete_sum(self):
        result = a.payload(readings({
            "PAYLOAD STATION COUNT": 2, "PAYLOAD STATION WEIGHT:1": 170.0, "PAYLOAD STATION NAME:1": "Pilot",
            "PAYLOAD STATION WEIGHT:2": 30, "PAYLOAD STATION NAME:2": "Baggage\x00",
        }))
        self.assertEqual([s.name for s in result.stations], ["Pilot", "Baggage"])
        self.assertAlmostEqual(result.station_total_kg.value, 200 * a.LB_TO_KG)
        self.assertEqual(result.station_total_kg.status, a.Status.KNOWN)
        self.assertIn("not crew", result.station_total_kg.reason)
        self.assertAlmostEqual(result.stations[0].weight_kg, 170 * a.LB_TO_KG)
        # A station named "Pilot" does not make the crew count known.
        for item in (result.crew, result.passengers, result.baggage_kg, result.freight_kg):
            self.assertIsNone(item.value)
            self.assertEqual(item.status, a.Status.UNKNOWN)
        self.assertIn("PAYLOAD STATION WEIGHT:2", result.station_total_kg.provenance.fields)
        json.dumps(result.to_json())

    def test_incomplete_sum(self):
        result = a.payload(readings({
            "PAYLOAD STATION COUNT": 3, "PAYLOAD STATION WEIGHT:1": 100.0, "PAYLOAD STATION WEIGHT:2": -5.0,
        }))
        self.assertEqual(result.station_total_kg.status, a.Status.INCOMPLETE)
        self.assertAlmostEqual(result.station_total_kg.value, 100 * a.LB_TO_KG)
        self.assertIn("1 of 3", result.station_total_kg.reason)
        self.assertIsNone(result.stations[1].weight_lb)

    def test_no_weights(self):
        result = a.payload(readings({"PAYLOAD STATION COUNT": 2}))
        self.assertEqual(result.station_total_kg.status, a.Status.UNKNOWN)

    def test_station_cap(self):
        values = {"PAYLOAD STATION COUNT": 20}
        values.update({f"PAYLOAD STATION WEIGHT:{i}": 10.0 for i in range(1, 16)})
        result = a.payload(readings(values))
        self.assertEqual(len(result.stations), 15)
        self.assertEqual(result.station_total_kg.status, a.Status.INCOMPLETE)


class SimVarBuilders(unittest.TestCase):
    def test_builders(self):
        names = [v.sdk_name for v in a.identity_simvars()]
        self.assertEqual(names, list(a.IDENTITY_FIELDS))
        self.assertEqual([v.sdk_name for v in a.position_simvars()],
                         ["PLANE LATITUDE", "PLANE LONGITUDE", "SIM ON GROUND"])
        payload = a.payload_simvars(20)
        self.assertEqual(len(payload), 1 + 2 * 15)
        self.assertEqual(payload[2].sdk_name, "PAYLOAD STATION WEIGHT:1")
        self.assertEqual(payload[2].unit, "pounds")
        self.assertEqual(len(a.payload_simvars()), 1)
        with self.assertRaises(ValueError):
            a.payload_simvars(-1)


if __name__ == "__main__":
    unittest.main()
