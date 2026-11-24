"""Public types and validation for advanced SimConnect operations."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import PureWindowsPath
from re import fullmatch
from struct import Struct

from .contract import CoreRequestError, Source, StrEnum
from .sdk_simvar import SimVar


class SimulatorOperation(StrEnum):
    RESERVE_KEY = "reserve_key"
    CREATE_PARKED_AIRCRAFT = "create_parked_aircraft"
    CREATE_ENROUTE_AIRCRAFT = "create_enroute_aircraft"
    CREATE_NON_ATC_AIRCRAFT = "create_non_atc_aircraft"
    CREATE_SIMULATED_OBJECT = "create_simulated_object"
    RELEASE_AI = "release_ai"
    REMOVE_OBJECT = "remove_object"
    SET_AI_FLIGHT_PLAN = "set_ai_flight_plan"
    LOAD_FLIGHT = "load_flight"
    SAVE_FLIGHT = "save_flight"
    LOAD_FLIGHT_PLAN = "load_flight_plan"
    EXECUTE_ACTION = "execute_action"
    WRITE_OBJECT_DATA = "write_object_data"


class ObjectKind(StrEnum):
    USER = "user"
    ALL = "all"
    AIRCRAFT = "aircraft"
    HELICOPTER = "helicopter"
    BOAT = "boat"
    GROUND = "ground"


class ObjectFormat(StrEnum):
    POSITION = "initial_position"
    LATLONALT = "latlonalt"
    WAYPOINT = "waypoint"
    MARKER = "marker"
    XYZ = "xyz"
    VARIABLE_TEXT = "variable_text"


class SimulatorReadState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class InitialPosition:
    latitude_deg: float
    longitude_deg: float
    altitude_ft: float
    pitch_deg: float = 0
    bank_deg: float = 0
    heading_deg: float = 0
    on_ground: bool = False
    airspeed_knots: int = 0

    def packed(self):
        fields = (self.latitude_deg, self.longitude_deg, self.altitude_ft,
                  self.pitch_deg, self.bank_deg, self.heading_deg)
        if (not all(_number(field) for field in fields) or not -90 <= self.latitude_deg <= 90
                or not -180 <= self.longitude_deg <= 180 or type(self.on_ground) is not bool
                or type(self.airspeed_knots) is not int or not -2 <= self.airspeed_knots <= 0xFFFFFFFF):
            raise CoreRequestError("Invalid SimConnect initial position.")
        return Struct("<6dII").pack(*fields, self.on_ground, self.airspeed_knots & 0xFFFFFFFF)


@dataclass(frozen=True, slots=True)
class SimObjectRef:
    session_id: str
    object_id: int
    ownership_token: str | None = None


@dataclass(frozen=True, slots=True)
class ObjectField:
    name: str
    format: ObjectFormat

    def __post_init__(self):
        if (not isinstance(self.name, str) or not fullmatch(r"[A-Za-z0-9][A-Za-z0-9_ :.-]{0,255}", self.name)
                or not isinstance(self.format, ObjectFormat)):
            raise CoreRequestError("Supply a documented structured SimVar name and format.")
        object.__setattr__(self, "name", self.name.strip().upper())


@dataclass(frozen=True, slots=True)
class ObjectSample:
    object: SimObjectRef
    # builtins.object, not "object": under postponed annotations get_type_hints() would
    # otherwise resolve the name to this class's own ``object`` field.
    value: builtins.object


@dataclass(frozen=True, slots=True)
class ControllerInfo:
    name: str
    device_id: int
    product_id: int
    composite_id: int
    version: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class ControllerInput:
    device: int
    control: str
    index: int | None = None

    def __post_init__(self):
        if type(self.device) is not int or not 0 <= self.device <= 0xFFFF:
            raise CoreRequestError("Supply a SimConnect controller number.")
        controls = {name.casefold(): name for name in
                    ("button", "POV", "slider", "XAxis", "YAxis", "ZAxis", "RxAxis", "RyAxis", "RzAxis")}
        name = controls.get(self.control.casefold()) if isinstance(self.control, str) else None
        if name in ("button", "POV", "slider"):
            valid = type(self.index) is int and 0 <= self.index <= 0xFFFF
        else:
            valid = bool(name) and self.index is None
        if not valid:
            raise CoreRequestError("Supply a documented controller control and its index if required.")
        object.__setattr__(self, "control", name)

    @property
    def sdk_name(self):
        suffix = "" if self.index is None else f":{self.index}"
        return f"joystick:{self.device}:{self.control}{suffix}"


@dataclass(frozen=True, slots=True)
class HardwareSample:
    control: ControllerInput
    value: int
    pressed: bool | None = None


@dataclass(frozen=True, slots=True)
class SimulatorRead:
    kind: str
    field: SimVar | ObjectField | None = None
    object: SimObjectRef | None = None
    object_kind: ObjectKind | None = None
    radius: int = 0


@dataclass(frozen=True, slots=True)
class SimulatorResult:
    id: str
    state: SimulatorReadState
    source: Source
    session_id: str | None
    data: object = None
    received_at: datetime | None = None
    detail: str = ""


OPERATION_GROUPS = {
    **dict.fromkeys((SimulatorOperation.CREATE_PARKED_AIRCRAFT, SimulatorOperation.CREATE_ENROUTE_AIRCRAFT,
                     SimulatorOperation.CREATE_NON_ATC_AIRCRAFT, SimulatorOperation.CREATE_SIMULATED_OBJECT,
                     SimulatorOperation.RELEASE_AI, SimulatorOperation.REMOVE_OBJECT,
                     SimulatorOperation.SET_AI_FLIGHT_PLAN, SimulatorOperation.WRITE_OBJECT_DATA), "ai_objects"),
    **dict.fromkeys((SimulatorOperation.LOAD_FLIGHT, SimulatorOperation.SAVE_FLIGHT,
                     SimulatorOperation.LOAD_FLIGHT_PLAN), "flight_files"),
    SimulatorOperation.RESERVE_KEY: "reserved_keys",
    SimulatorOperation.EXECUTE_ACTION: "simulator_actions",
}


def _number(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def encoded_text(value, label, *, limit=259, empty=False):
    if not isinstance(value, str) or "\0" in value or not (value or empty):
        raise CoreRequestError(f"Supply valid {label} text.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise CoreRequestError(f"Supply valid {label} text.") from error
    if len(encoded) > limit:
        raise CoreRequestError(f"The {label} is too long for SimConnect.")
    return encoded


# The SDK says the .FLT/.PLN extension "need not" be entered for flight and plan files.
# Whether MSFS 2020 also accepts it with the extension is not yet verified natively. Core
# accepts both spellings from modules and sends this form; flip it only after a native probe.
SEND_FLIGHT_FILE_EXTENSION = True


def flight_path(value, suffix, *, send_extension=None):
    """Validate an absolute local flight or plan path, with or without its extension."""
    encoded_text(value, "flight file path")
    path = PureWindowsPath(value)
    extension = path.suffix.casefold()
    if (not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":"
            or extension not in ("", suffix) or ".." in path.parts or path.name.startswith(".")
            or any(char in value[2:] for char in ':*?"<>|\r\n')):
        raise CoreRequestError(f"Supply an absolute local {suffix} path. The extension is optional.")
    keep = SEND_FLIGHT_FILE_EXTENSION if send_extension is None else send_extension
    if keep:
        text = value if extension else value + suffix
    else:
        text = value[:-len(path.suffix)] if extension else value
    return encoded_text(text, "flight file path")


def operation_arguments(operation, arguments):
    """Validate and encode the public operation's fixed signature."""
    sizes = {
        SimulatorOperation.RESERVE_KEY: (1, 3), SimulatorOperation.CREATE_PARKED_AIRCRAFT: (3, 3),
        SimulatorOperation.CREATE_ENROUTE_AIRCRAFT: (6, 6), SimulatorOperation.CREATE_NON_ATC_AIRCRAFT: (3, 3),
        SimulatorOperation.CREATE_SIMULATED_OBJECT: (2, 2), SimulatorOperation.RELEASE_AI: (1, 1),
        SimulatorOperation.REMOVE_OBJECT: (1, 1), SimulatorOperation.SET_AI_FLIGHT_PLAN: (2, 2),
        SimulatorOperation.LOAD_FLIGHT: (1, 1), SimulatorOperation.SAVE_FLIGHT: (3, 3),
        SimulatorOperation.LOAD_FLIGHT_PLAN: (1, 1), SimulatorOperation.EXECUTE_ACTION: (2, 2),
        SimulatorOperation.WRITE_OBJECT_DATA: (3, 3),
    }
    low, high = sizes[operation]
    if not low <= len(arguments) <= high:
        raise CoreRequestError("Supply the documented operation arguments.")
    if operation is SimulatorOperation.RESERVE_KEY:
        if any(not isinstance(key, str) or not fullmatch(r"[A-Za-z0-9]", key) for key in arguments):
            raise CoreRequestError("Supply one to three single-character Tab shortcut choices.")
        return tuple(key.encode("ascii") for key in arguments) + (b"",) * (3 - len(arguments))
    if operation is SimulatorOperation.LOAD_FLIGHT:
        return (flight_path(arguments[0], ".flt"),)
    if operation is SimulatorOperation.LOAD_FLIGHT_PLAN:
        return (flight_path(arguments[0], ".pln"),)
    if operation is SimulatorOperation.SAVE_FLIGHT:
        path, title, description = arguments
        return flight_path(path, ".flt"), encoded_text(title, "title", empty=True), encoded_text(description, "description", empty=True)
    if operation is SimulatorOperation.EXECUTE_ACTION:
        name, payload = arguments
        if not isinstance(payload, bytes) or len(payload) > 65536:
            raise CoreRequestError("Supply the action's documented packed byte payload.")
        return encoded_text(name, "action"), payload
    if operation in (SimulatorOperation.RELEASE_AI, SimulatorOperation.REMOVE_OBJECT,
                     SimulatorOperation.SET_AI_FLIGHT_PLAN, SimulatorOperation.WRITE_OBJECT_DATA):
        target = arguments[0]
        if not isinstance(target, SimObjectRef):
            raise CoreRequestError("Supply an object reference returned by Core.")
        if operation is SimulatorOperation.SET_AI_FLIGHT_PLAN:
            return target, flight_path(arguments[1], ".pln")
        if operation is SimulatorOperation.WRITE_OBJECT_DATA:
            field, payload = arguments[1:]
            if not isinstance(field, ObjectField) or not isinstance(payload, (bytes, InitialPosition)):
                raise CoreRequestError("Supply a structured field and its packed SDK payload.")
            return target, field, payload.packed() if isinstance(payload, InitialPosition) else payload
        return (target,)
    title = encoded_text(arguments[0], "simulator object title")
    if operation is SimulatorOperation.CREATE_SIMULATED_OBJECT:
        return title, _position(arguments[1])
    tail = encoded_text(arguments[1], "tail number", limit=11)
    if operation is SimulatorOperation.CREATE_NON_ATC_AIRCRAFT:
        return title, tail, _position(arguments[2])
    if operation is SimulatorOperation.CREATE_PARKED_AIRCRAFT:
        return title, tail, encoded_text(arguments[2], "airport identifier", limit=7)
    number, path, position, touch_and_go = arguments[2:]
    if (type(number) is not int or not 0 <= number <= 0x7FFFFFFF or not _number(position)
            or position < 0 or type(touch_and_go) is not bool):
        raise CoreRequestError("Invalid en-route flight number, plan position or touch-and-go setting.")
    return title, tail, number, flight_path(path, ".pln"), float(position), int(touch_and_go)


def _position(value):
    if not isinstance(value, InitialPosition):
        raise CoreRequestError("Supply an InitialPosition.")
    return value.packed()
