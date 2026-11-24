"""Offline checks of the experimental standalone-Core wire protocol (bbc_core.ipc.protocol)."""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
import os
import stat
import sys
import tempfile
import types
import typing
import unittest
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from uuid import UUID

from bbc_core.client_data import ClientDataArea, ClientDataMode
from bbc_core.contract import (
    API_VERSION, CommandResult, CommandState, Connection, ConnectionState, CoreRequestError, Event, Gap,
    MissionAction, Observation, PauseState, Quality, SignalError, Snapshot, SnapshotPeriod, Source, SubscriptionEnded,
)
from bbc_core.core import ConsumerAPI
from bbc_core.facilities import (
    AirportDetails, AirportJetway, AirportReference, FacilityResult, FacilityState, JetwayState, ParkingPosition,
    Runway,
)
from bbc_core.ipc import protocol as wire
from bbc_core.ipc.client import RemoteConsumerAPI
from bbc_core.sdk_simvar import SimVar, SimVarFormat
from bbc_core.contract import ValueType

UTC = timezone.utc
SOURCE = Source("Test source", simulated=True)
NOW = datetime(2026, 11, 24, 10, 30, 15, 123456, tzinfo=UTC)


def round_trip(value, *, validate=False):
    # Through real JSON bytes, as on the wire.
    return wire.decode(json.loads(wire.dumps({"type": "x", "v": wire.encode(value)}))["v"], validate=validate)


class RoundTripTests(unittest.TestCase):
    def test_snapshot_with_clocks_bytes_and_unknowns(self):
        snapshot = Snapshot(
            "sub-1", ("PLANE ALTITUDE", "TITLE", "SIM ON GROUND", "RAW"), (1234.5, "C172", True, None),
            12.5, 63931112776.0, 1.0, SOURCE, "session", NOW, NOW + timedelta(milliseconds=3),
        )
        result = round_trip(snapshot)
        self.assertEqual(result, snapshot)
        self.assertIs(type(result), Snapshot)
        self.assertEqual(result.received_at.tzinfo, UTC)
        self.assertIsNone(result.values[3], "an unknown value must stay unknown")

    def test_observation_with_bytes_and_enum(self):
        observation = Observation("client_data:X:0:4", b"\x00\x01\xfe\xff", None, Quality.CURRENT, SOURCE, "s", NOW)
        result = round_trip(observation)
        self.assertEqual(result, observation)
        self.assertIs(type(result.value), bytes)
        self.assertIs(result.quality, Quality.CURRENT)

    def test_pause_flags_keep_type_and_unknown_bits(self):
        event = Event("sim.pause_state", PauseState(PauseState.ACTIVE | 0x40), SOURCE, "s", NOW)
        result = round_trip(event)
        self.assertIsInstance(result.value, PauseState)
        self.assertEqual(int(result.value), 0x44)
        self.assertTrue(result.value.active_pause)

    def test_uuid_inside_mission_action(self):
        action = MissionAction(UUID("12345678-1234-5678-1234-567812345678"), True, "payload")
        result = round_trip(Event("sim.mission", action, SOURCE, "s", NOW))
        self.assertEqual(result.value, action)
        self.assertIsInstance(result.value.instance_id, UUID)

    def test_nested_facility_result(self):
        details = AirportDetails(
            AirportReference("EGLL", "EG", 51.47, -0.4543, 25.0), "Heathrow",
            (Runway(0, 51.47, -0.45, 90.0, 3900.0, 50.0, 4, 9, 1, 27, 2),),
            (ParkingPosition(0, 1, 2, 0, 101, 180.0, 30.0, 0.0, 0.0),),
            (AirportJetway(0, 101, 0, 1),),
        )
        result = FacilityResult("f1", FacilityState.READY, SOURCE, "s", NOW, details)
        self.assertEqual(round_trip(result), result)
        jetway = JetwayState("EGLL", 3, 1.0, 2.0, 3.0, 0.0, 0.0, 90.0, 7, 0, (1.0, 2.0, 3.0), (0.0, 0.0, 0.0),
                             (0.0, 0.0, 0.0), (0.5, 0.5, 0.5), 11, 12)
        self.assertEqual(round_trip(FacilityResult("f2", FacilityState.READY, SOURCE, "s", NOW, (jetway,))).data,
                         (jetway,))

    def test_signal_error_keeps_its_own_type(self):
        error = SignalError(2, "The provider reported an error.", "aircraft.title", "sub-9")
        result = round_trip(error)
        self.assertIs(type(result), SignalError)
        self.assertEqual(result, error)
        # C7: a provider error is not data loss; the wire must not turn one into the other.
        self.assertEqual(isinstance(result, Gap), issubclass(SignalError, Gap))
        self.assertIs(type(round_trip(Gap(3, "lost"))), Gap)

    def test_pause_flags_inside_an_observation(self):
        observation = Observation("sim.pause_state", PauseState.SIM | PauseState.FULL, None, Quality.CURRENT,
                                  SOURCE, "s", NOW)
        result = round_trip(observation)
        self.assertIs(type(result.value), PauseState)
        self.assertTrue(result.value.full_pause and result.value.sim_pause)
        self.assertEqual(result.value, 9, "pause flags still compare equal to the plain integer")
        self.assertIs(round_trip(PauseState.NONE), PauseState.NONE)

    def test_subscription_ended_round_trips(self):
        ended = SubscriptionEnded("sub-3", "input.abc", "The aircraft inputs changed.")
        self.assertEqual(round_trip(ended), ended)
        self.assertIs(type(round_trip(ended)), SubscriptionEnded)

    def test_dicts_lists_sets_and_tuples_keep_their_shapes(self):
        data = {1: [1, 2], "a": (1, (2, 3)), (1, 2): frozenset({"x", "y"}), "s": {3}}
        result = round_trip(CommandResult("c", "x", CommandState.CONFIRMED, SOURCE, "s", "", data)).data
        self.assertEqual(result, data)
        self.assertIs(type(result[1]), list)
        self.assertIs(type(result["a"]), tuple)
        self.assertIs(type(result[(1, 2)]), frozenset)
        self.assertIs(type(result["s"]), set)

    def test_special_numbers_and_plain_time_values(self):
        values = round_trip((math.nan, math.inf, -math.inf, 0.1, 2 ** 70, -0.0))
        self.assertTrue(math.isnan(values[0]))
        self.assertEqual(values[1:], (math.inf, -math.inf, 0.1, 2 ** 70, -0.0))
        naive = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(round_trip((naive, date(2026, 1, 2), timedelta(days=-1, seconds=5, microseconds=7))),
                         (naive, date(2026, 1, 2), timedelta(days=-1, seconds=5, microseconds=7)))
        self.assertIsNone(round_trip(naive).tzinfo, "a naive time must not gain a time zone")

    def test_request_types_are_rebuilt_through_validation(self):
        variable = SimVar("plane altitude", "Feet", ValueType.NUMBER, format=SimVarFormat.FLOAT64)
        self.assertEqual(round_trip(variable, validate=True), variable)
        area = ClientDataArea("PMDG_NG3_Data", 16, preserve_case=True)
        self.assertEqual(round_trip(area.region(4, 8), validate=True).area.name, "PMDG_NG3_Data")
        self.assertIs(round_trip(ClientDataMode.SECOND, validate=True), ClientDataMode.SECOND)
        self.assertIs(round_trip(SnapshotPeriod.SIM_FRAME), SnapshotPeriod.SIM_FRAME)

    def test_validation_errors_are_the_in_process_errors(self):
        bad = {"$t": "SimVar", "f": {"name": "bad name!", "unit": "feet", "value_type": {"$e": "ValueType", "v": "number"},
                                     "index": None, "format": None}}
        with self.assertRaises(CoreRequestError) as caught:
            wire.decode(bad)
        with self.assertRaises(CoreRequestError) as direct:
            SimVar("bad name!", "feet", ValueType.NUMBER)
        self.assertEqual(str(caught.exception), str(direct.exception))
        bad_area = {"$t": "ClientDataArea", "f": {"name": "X", "size": 0}}
        with self.assertRaises(ValueError):
            wire.decode(bad_area)

    def test_answers_are_restored_without_revalidation(self):
        encoded = wire.encode(Connection(ConnectionState.CONNECTED, SOURCE, "s", NOW))
        del encoded["f"]["detail"]  # an older peer may omit a field that has a default
        self.assertEqual(wire.decode(encoded, validate=False).detail, "")
        del encoded["f"]["state"]
        with self.assertRaises(wire.WireError):
            wire.decode(encoded, validate=False)


def _sample(annotation, depth=0):
    """A representative value for a contract field annotation (for the every-type round trip)."""
    if depth > 8:
        raise AssertionError(f"Cannot build a sample for {annotation!r}")
    if annotation is type(None):
        return None
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        options = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        return _sample(options[0], depth + 1)
    if origin is tuple:
        args = typing.get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            return (_sample(args[0], depth + 1), None) if type(None) in typing.get_args(args[0]) else (
                _sample(args[0], depth + 1),)
        return tuple(_sample(arg, depth + 1) for arg in args)
    if origin in (frozenset, set):
        return origin({_sample(typing.get_args(annotation)[0], depth + 1)})
    if origin is dict:
        key, value = typing.get_args(annotation)
        return {_sample(key, depth + 1): _sample(value, depth + 1)}
    simple = {bool: True, int: 7, float: 1.25, str: "text", bytes: b"\x00\xff", datetime: NOW, UUID: UUID(int=5),
              object: ("any", 1)}
    if annotation in simple:
        return simple[annotation]
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return list(annotation)[-1]
    if dataclasses.is_dataclass(annotation):
        hints = typing.get_type_hints(annotation)
        return annotation(**{field.name: _sample(hints[field.name], depth + 1)
                             for field in dataclasses.fields(annotation) if field.init})
    raise AssertionError(f"Cannot build a sample for {annotation!r}")


class EveryUpdateTypeTests(unittest.TestCase):
    """Every type poll() can return crosses the wire unchanged, including types added later."""

    def poll_types(self):
        from bbc_core import contract
        from bbc_core.facilities import FacilityCacheUpdate
        from bbc_core.simulator_ops import SimulatorResult

        found = list(typing.get_args(contract.Update)) + [FacilityResult, FacilityCacheUpdate, SimulatorResult]
        for name in ("Snapshot", "SignalError", "SubscriptionEnded", "Gap", "Connection"):
            self.assertIn(getattr(contract, name), found, f"{name} should be in the Update union")
        return found

    def test_every_update_type_round_trips(self):
        for cls in self.poll_types():
            with self.subTest(update=cls.__name__):
                self.assertIs(wire.WIRE_TYPES.get(cls.__name__), cls, "every update type must be registered")
                value = _sample(cls)
                result = round_trip(value)
                self.assertIs(type(result), cls)
                self.assertEqual(result, value)
                for field in dataclasses.fields(cls):
                    self.assertIs(type(getattr(result, field.name)), type(getattr(value, field.name)), field.name)

    def test_every_registered_dataclass_round_trips(self):
        for name, cls in sorted(wire.WIRE_TYPES.items()):
            if not dataclasses.is_dataclass(cls):
                continue
            with self.subTest(type=name):
                try:
                    value = _sample(cls)
                except (ValueError, TypeError):  # request types validate in their constructors
                    continue
                self.assertEqual(round_trip(value), value)

    def test_every_enum_member_round_trips(self):
        for name, cls in sorted(wire.WIRE_TYPES.items()):
            if isinstance(cls, type) and issubclass(cls, Enum):
                for member in cls:
                    with self.subTest(member=f"{name}.{member.name}"):
                        self.assertIs(round_trip(member), member)


class RejectionTests(unittest.TestCase):
    def test_unregistered_types_are_refused_on_encode(self):
        @dataclasses.dataclass(frozen=True)
        class Snapshot:  # same name as a contract type, different class
            x: int = 1

        class Colour(Enum):
            RED = 1

        for value in (object(), Snapshot(), Colour.RED, Path("/tmp"), 1 + 2j):
            with self.subTest(value=value), self.assertRaises(wire.WireError):
                wire.encode(value)

    def test_malformed_values_are_refused_on_decode(self):
        cases = (
            {"$t": "NotAType", "f": {}},
            {"$t": "Gap", "f": {"dropped_updates": 1, "reason": "x", "extra": 1}},
            {"$t": "FacilityQuery", "f": {}},
            {"plain": "object"},
            {"$e": "Quality", "v": "brilliant"},
            {"$e": "Gap", "v": 1},
            {"$t": "Quality", "f": {}},
            {"$b": "***"},
            {"$dt": 5},
            {"$f": "huge"},
            {"$uuid": "nope"},
            {"$map": [[1]]},
            {"$td": [1, 2]},
            {"$dt": "2026-01-01", "extra": 1},
            math.nan,
        )
        for data in cases:
            with self.subTest(data=data), self.assertRaises(wire.WireError):
                wire.decode(data)

    def test_deep_nesting_is_refused(self):
        deep = []
        for _ in range(wire.MAX_DEPTH + 5):
            deep = [deep]
        with self.assertRaises(wire.WireError):
            wire.decode(deep)
        value = ()
        for _ in range(wire.MAX_DEPTH + 5):
            value = (value,)
        with self.assertRaises(wire.WireError):
            wire.encode(value)

    def test_messages_must_be_typed_json_objects(self):
        for data in (b"\xff\xfe", b"[1,2]", b'{"no_type": 1}', b'{"type": 5}', b"not json", b"[" * 100000):
            with self.subTest(data=data[:12]), self.assertRaises(wire.WireError):
                wire.loads(data)
        self.assertEqual(wire.loads(wire.dumps({"type": "ping", "id": 1})), {"type": "ping", "id": 1})
        with self.assertRaises(wire.WireError):
            wire.dumps({"type": "x", "v": math.nan})


class VersionTests(unittest.TestCase):
    def test_api_version_rule(self):
        cases = {
            ("0.1-draft", "0.1-draft"): True,
            ("0.1", "0.1.3"): True,
            ("0.1.3", "0.1"): False,
            ("0.1-draft", "0.1"): False,
            ("0.1", "0.2"): False,
            ("1.0", "1.4"): True,
            ("1.4", "1.0"): False,
            ("1.0", "2.0"): False,
            ("garbage", "0.1"): False,
        }
        for (client, server), expected in cases.items():
            with self.subTest(client=client, server=server):
                ok, reason = wire.api_versions_compatible(client, server)
                self.assertIs(ok, expected)
                self.assertEqual(bool(reason), not expected)
        self.assertTrue(wire.api_versions_compatible(API_VERSION, API_VERSION)[0])
        self.assertIn("0.2", wire.api_versions_compatible("0.2", "0.1")[1])

    def test_protocol_negotiation_picks_highest_common(self):
        self.assertEqual(wire.choose_protocol_version([1, 2, 3], (1, 2)), 2)
        self.assertIsNone(wire.choose_protocol_version([99], (1,)))
        self.assertIsNone(wire.choose_protocol_version("1", (1,)))
        self.assertIsNone(wire.choose_protocol_version(["1"], (1,)))
        self.assertIsNone(wire.choose_protocol_version([True], (1,)), "a Boolean is not a version number")

    def test_schema_fingerprint_tracks_field_names(self):
        self.assertEqual(wire.schema_fingerprint(), wire._fingerprint(wire.WIRE_TYPES))

        @dataclasses.dataclass(frozen=True)
        class Gap:
            dropped_updates: int
            reason: str
            new_field: int = 0

        changed = dict(wire.WIRE_TYPES, Gap=Gap)
        self.assertNotEqual(wire._fingerprint(changed), wire.schema_fingerprint())

    def test_registry_covers_every_update_type_and_no_internal_type(self):
        for name in ("Connection", "Observation", "Event", "Gap", "SignalError", "CommandResult", "InputCatalog",
                     "InputParameters", "Snapshot", "SubscriptionEnded", "FacilityResult", "FacilityCacheUpdate",
                     "SimulatorResult", "Discovery", "Subscription", "SimVar", "PauseState", "SnapshotPeriod"):
            self.assertIn(name, wire.WIRE_TYPES)
        self.assertNotIn("FacilityQuery", wire.WIRE_TYPES)
        self.assertNotIn("SimulatorRead", wire.WIRE_TYPES)
        self.assertNotIn("StrEnum", wire.WIRE_TYPES)


class MethodTableTests(unittest.TestCase):
    def test_wire_methods_match_consumer_api(self):
        public = {name for name, value in vars(ConsumerAPI).items() if callable(value) and not name.startswith("_")}
        self.assertLessEqual(wire.CONSUMER_METHODS, public)
        self.assertEqual(public - wire.CONSUMER_METHODS, {"close"},
                         "a new ConsumerAPI method needs a wire decision (add it to CONSUMER_METHODS)")

    def test_remote_api_mirrors_parameter_names(self):
        for name in sorted(wire.CONSUMER_METHODS):
            with self.subTest(method=name):
                local = inspect.signature(getattr(ConsumerAPI, name))
                remote = inspect.signature(getattr(RemoteConsumerAPI, name))
                self.assertEqual([(p.name, p.kind) for p in local.parameters.values()],
                                 [(p.name, p.kind) for p in remote.parameters.values()])


class ErrorAndProofTests(unittest.TestCase):
    def test_error_mapping(self):
        for error, cls in ((CoreRequestError("bad"), CoreRequestError), (ValueError("v"), ValueError),
                           (TypeError("t"), TypeError), (KeyError("k"), wire.RemoteCoreError),
                           (wire.WireError("w"), CoreRequestError)):
            with self.subTest(error=error):
                rebuilt = wire.decode_error(json.loads(json.dumps(wire.encode_error(error))))
                self.assertIs(type(rebuilt), cls)
        self.assertEqual(str(wire.decode_error(wire.encode_error(CoreRequestError("Same text.")))), "Same text.")
        self.assertIsInstance(wire.decode_error("junk"), wire.RemoteCoreError)

    def test_proofs_depend_on_key_label_and_parts(self):
        key = os.urandom(32)
        base = wire.proof(key, "bbc-core-client", b"a", b"b")
        self.assertTrue(wire.proof_matches(base, wire.proof(key, "bbc-core-client", b"a", b"b")))
        self.assertFalse(wire.proof_matches(base, wire.proof(key, "bbc-core-server", b"a", b"b")))
        self.assertFalse(wire.proof_matches(base, wire.proof(os.urandom(32), "bbc-core-client", b"a", b"b")))
        self.assertFalse(wire.proof_matches(base, wire.proof(key, "bbc-core-client", b"ab", b"")))
        self.assertFalse(wire.proof_matches(base, None))
        self.assertFalse(wire.proof_matches(base, "é"))

    def test_short_keys_are_refused(self):
        with self.assertRaises(ValueError):
            wire.check_authkey(b"short")
        with self.assertRaises(ValueError):
            wire.check_authkey("a" * 40)


class AddressAndKeyTests(unittest.TestCase):
    def test_default_addresses_are_local_only(self):
        pipe = wire.default_address("AF_PIPE")
        self.assertTrue(pipe.startswith("\\\\.\\pipe\\bbc-core-"), pipe)
        self.assertTrue(pipe.endswith(f"-p{wire.PROTOCOL_VERSION}"))
        with self.assertRaises(ValueError):
            wire.default_address("AF_INET")
        if sys.platform != "win32":
            path = Path(wire.default_address("AF_UNIX"))
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_authkey_file_is_created_private_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sub" / "core-ipc.key"
            key = wire.load_or_create_authkey(path)
            self.assertEqual(len(key), wire.MIN_AUTHKEY_BYTES)
            self.assertEqual(wire.load_or_create_authkey(path), key)
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                path.chmod(0o644)
                with self.assertRaises(PermissionError):
                    wire.load_or_create_authkey(path)
                path.chmod(0o600)
            path.write_bytes(b"tiny")
            with self.assertRaises(ValueError):
                wire.load_or_create_authkey(path)


if __name__ == "__main__":
    unittest.main()
