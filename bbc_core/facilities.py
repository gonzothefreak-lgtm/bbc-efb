"""Read-only simulator facility requests and typed airport data."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from re import fullmatch
from struct import Struct

from .contract import CoreRequestError, Source


class FacilityState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    NO_DATA = "no_data"
    UNAVAILABLE = "unavailable"


class NavaidKind(str, Enum):
    VOR = "vor"
    NDB = "ndb"
    WAYPOINT = "waypoint"


class FacilityKind(str, Enum):
    AIRPORT = "airport"
    VOR = "vor"
    NDB = "ndb"
    WAYPOINT = "waypoint"


class FacilityChange(str, Enum):
    SNAPSHOT = "snapshot"
    ADDED = "added"
    REMOVED = "removed"
    UNAVAILABLE = "unavailable"


class AirportFilterField(str, Enum):
    AIRPORT_NAME = "AIRPORT:NAME64"
    RUNWAY_PRIMARY_NUMBER = "AIRPORT:RUNWAY:PRIMARY_NUMBER"
    RUNWAY_SECONDARY_NUMBER = "AIRPORT:RUNWAY:SECONDARY_NUMBER"
    RUNWAY_SURFACE = "AIRPORT:RUNWAY:SURFACE"
    RUNWAY_LENGTH = "AIRPORT:RUNWAY:LENGTH"
    PARKING_TYPE = "AIRPORT:TAXI_PARKING:TYPE"
    PARKING_NAME = "AIRPORT:TAXI_PARKING:NAME"
    PARKING_NUMBER = "AIRPORT:TAXI_PARKING:NUMBER"
    PARKING_RADIUS = "AIRPORT:TAXI_PARKING:RADIUS"
    JETWAY_PARKING_GATE = "AIRPORT:JETWAY:PARKING_GATE"
    JETWAY_PARKING_SPOT = "AIRPORT:JETWAY:PARKING_SPOT"


@dataclass(frozen=True, slots=True)
class AirportFilter:
    field: AirportFilterField
    value: int | float | str

    def __post_init__(self):
        if not isinstance(self.field, AirportFilterField):
            raise CoreRequestError("Select an AirportFilterField.")
        if self.field is AirportFilterField.AIRPORT_NAME:
            if (not isinstance(self.value, str) or not self.value.isascii() or not self.value.isprintable()
                    or not 1 <= len(self.value) < 64 or self.value != self.value.strip()):
                raise CoreRequestError("Use an exact airport name of one to 63 printable ASCII characters.")
            return
        if self.field in (AirportFilterField.RUNWAY_LENGTH, AirportFilterField.PARKING_RADIUS):
            if type(self.value) not in (int, float) or not 0 <= self.value <= 3.4e38:
                raise CoreRequestError("Use a finite non-negative distance in metres.")
            value, = Struct("<f").unpack(Struct("<f").pack(self.value))
            if not isfinite(value):
                raise CoreRequestError("The filter distance is outside the SDK range.")
            object.__setattr__(self, "value", value)
            return
        maximum = 0xFFFFFFFF if self.field is AirportFilterField.PARKING_NUMBER else 0x7FFFFFFF
        if type(self.value) is not int or not 0 <= self.value <= maximum:
            raise CoreRequestError("Airport filters require a non-negative integer that fits the SDK field.")

    def sdk_payload(self):
        if isinstance(self.value, str):
            return Struct("<64s").pack(self.value.encode("ascii"))
        if isinstance(self.value, float):
            return Struct("<f").pack(self.value)
        return self.value.to_bytes(4, "little")


@dataclass(frozen=True, slots=True)
class AirportReference:
    icao: str
    region: str
    latitude_deg: float
    longitude_deg: float
    altitude_m: float


@dataclass(frozen=True, slots=True)
class Runway:
    index: int
    latitude_deg: float
    longitude_deg: float
    heading_deg: float
    length_m: float
    width_m: float
    surface: int
    primary_number: int
    primary_designator: int
    secondary_number: int
    secondary_designator: int


@dataclass(frozen=True, slots=True)
class ParkingPosition:
    index: int
    kind: int
    name: int
    suffix: int
    number: int
    heading_deg: float
    radius_m: float
    bias_x_m: float
    bias_z_m: float


@dataclass(frozen=True, slots=True)
class AirportJetway:
    index: int
    parking_gate: int
    parking_suffix: int
    parking_spot: int


@dataclass(frozen=True, slots=True)
class AirportDetails:
    airport: AirportReference
    name: str
    runways: tuple[Runway, ...]
    parking: tuple[ParkingPosition, ...]
    jetways: tuple[AirportJetway, ...]


@dataclass(frozen=True, slots=True)
class NavaidDetails:
    kind: NavaidKind
    icao: str
    region: str | None
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    type_code: int
    magnetic_variation_deg: float
    name: str | None = None
    frequency_raw: int | None = None
    range_m: float | None = None
    terminal: bool | None = None


@dataclass(frozen=True, slots=True)
class NavaidReference:
    kind: NavaidKind
    icao: str
    region: str
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    magnetic_variation_deg: float
    frequency_hz: int | None = None
    vor_flags: int | None = None
    localizer_deg: float | None = None
    glideslope_latitude_deg: float | None = None
    glideslope_longitude_deg: float | None = None
    glideslope_altitude_m: float | None = None
    glideslope_angle_deg: float | None = None


@dataclass(frozen=True, slots=True)
class FacilityIdentity:
    icao: str
    region: str
    latitude_deg: float
    longitude_deg: float
    associated_airport: str = ""


@dataclass(frozen=True, slots=True)
class FacilityCacheSubscription:
    id: str
    kind: FacilityKind


@dataclass(frozen=True, slots=True)
class FacilityCacheUpdate:
    subscription_id: str
    kind: FacilityKind
    change: FacilityChange
    source: Source
    session_id: str | None
    received_at: datetime
    entries: tuple[AirportReference | NavaidReference | FacilityIdentity, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class JetwayState:
    airport_icao: str
    parking_index: int
    latitude_deg: float
    longitude_deg: float
    altitude_ft: float
    pitch_deg: float
    bank_deg: float
    heading_deg: float
    status: int
    door_index: int
    exit_door_relative_xyz: tuple[float, float, float]
    main_handle_xyz: tuple[float, float, float]
    secondary_handle_xyz: tuple[float, float, float]
    wheel_ground_lock_xyz: tuple[float, float, float]
    jetway_object_id: int
    attached_object_id: int

    @property
    def fully_attached(self) -> bool:
        return self.status == 7


FacilityData = AirportDetails | tuple[AirportReference, ...] | tuple[NavaidReference, ...] | tuple[NavaidDetails, ...] | tuple[JetwayState, ...]


@dataclass(frozen=True, slots=True)
class FacilityResult:
    id: str
    state: FacilityState
    source: Source
    session_id: str | None
    received_at: datetime | None = None
    data: FacilityData | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FacilityQuery:
    icao: str | None = None
    region: str = ""
    navaid_kind: NavaidKind | None = None
    parking_indices: tuple[int, ...] | None = None
    worldwide_airports: bool = False
    airport_filters: tuple[AirportFilter, ...] = ()

    @classmethod
    def airport(cls, icao: str, region: str, filters: tuple[AirportFilter, ...] = ()):
        if not isinstance(filters, tuple) or any(not isinstance(item, AirportFilter) for item in filters):
            raise CoreRequestError("Supply a tuple of AirportFilter values.")
        unique = {}
        for item in filters:
            if unique.setdefault(item.field, item) != item:
                raise CoreRequestError("Supply one value per airport filter field.")
        normalized = tuple(sorted(unique.values(), key=lambda item: item.field.value))
        return cls(*_identifier(icao, region), airport_filters=normalized)

    @classmethod
    def navaid(cls, kind: NavaidKind, icao: str, region: str):
        if not isinstance(kind, NavaidKind):
            raise CoreRequestError("Select NavaidKind.VOR, NavaidKind.NDB or NavaidKind.WAYPOINT.")
        return cls(*_identifier(icao, region), navaid_kind=kind)

    @classmethod
    def jetways(cls, icao: str, indices: tuple[int, ...]):
        if not isinstance(indices, tuple) or any(type(i) is not int or not 0 <= i <= 0x7FFFFFFF for i in indices):
            raise CoreRequestError("Supply a tuple of non-negative SDK parking indices.")
        return cls(*_identifier(icao, ""), parking_indices=tuple(sorted(set(indices))))


def _identifier(icao, region):
    if not isinstance(icao, str) or not fullmatch(r"[A-Za-z0-9]{1,7}", icao.strip()):
        raise CoreRequestError("Supply a facility identifier of one to seven ASCII letters or digits.")
    if not isinstance(region, str) or not fullmatch(r"[A-Za-z0-9]{0,2}", region.strip()):
        raise CoreRequestError("Supply an optional simulator region code of at most two letters or digits.")
    return icao.strip().upper(), region.strip().upper()
