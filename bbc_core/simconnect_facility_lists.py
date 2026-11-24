"""Packed facility lists and their shared multipart assembly."""

from dataclasses import dataclass, field
from functools import partial
from math import isfinite
from struct import Struct

from .facilities import AirportReference, FacilityIdentity, FacilityKind, NavaidKind, NavaidReference
from .provider import ProviderError
from .simconnect_abi import FacilityListType, ReceiveId


_MAX_LIST_ENTRIES = 65_536
MINIMAL_RECORD = Struct("<c6s3s5sddd")


def facility_text(raw):
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def valid_coordinates(latitude, longitude):
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def airport_reference(icao, region, lat, lon, alt):
    if not icao or not valid_coordinates(lat, lon) or not isfinite(alt):
        raise ProviderError("The simulator returned an invalid facility position or identifier.")
    return AirportReference(icao, region, lat, lon, alt)


def _airport(icao, region, lat, lon, alt):
    return airport_reference(facility_text(icao), facility_text(region), lat, lon, alt)


def _navaid(kind, icao, region, lat, lon, alt, magvar, frequency=None, *vor):
    airport = _airport(icao, region, lat, lon, alt)
    if not isfinite(magvar):
        raise ProviderError("The simulator returned an invalid magnetic variation.")
    details = {}
    if vor:
        flags, localizer, glide_lat, glide_lon, glide_alt, glide_angle = vor
        details["vor_flags"] = flags
        if flags & 0x02:
            if not isfinite(localizer):
                raise ProviderError("The simulator returned an invalid localizer heading.")
            details["localizer_deg"] = localizer
        if flags & 0x04:
            if not valid_coordinates(glide_lat, glide_lon) or not all(isfinite(x) for x in (glide_alt, glide_angle)):
                raise ProviderError("The simulator returned an invalid glideslope.")
            details.update(glideslope_latitude_deg=glide_lat, glideslope_longitude_deg=glide_lon,
                           glideslope_altitude_m=glide_alt, glideslope_angle_deg=glide_angle)
    return NavaidReference(kind, airport.icao, airport.region, lat, lon, alt, magvar, frequency, **details)


def facility_identity(record):
    return FacilityIdentity(record.icao, record.region, record.latitude_deg, record.longitude_deg)


def minimal_identity(expected, type_code, ident, region, associated_airport, lat, lon, _alt):
    if type_code != expected or not facility_text(ident) or not valid_coordinates(lat, lon):
        raise ProviderError("The simulator returned an invalid facility removal record.")
    return FacilityIdentity(facility_text(ident), facility_text(region), lat, lon, facility_text(associated_airport))


@dataclass(frozen=True, slots=True)
class FacilityListFormat:
    sdk_type: FacilityListType
    receive_id: ReceiveId
    wire: Struct
    decode: object
    icao_type: bytes


LIST_FORMATS = {
    FacilityKind.AIRPORT: FacilityListFormat(FacilityListType.AIRPORT, ReceiveId.AIRPORT_LIST,
                                            Struct("<6s3sddd"), _airport, b"A"),
    FacilityKind.WAYPOINT: FacilityListFormat(FacilityListType.WAYPOINT, ReceiveId.WAYPOINT_LIST,
                                              Struct("<6s3sdddf"), partial(_navaid, NavaidKind.WAYPOINT), b"W"),
    FacilityKind.NDB: FacilityListFormat(FacilityListType.NDB, ReceiveId.NDB_LIST,
                                         Struct("<6s3sdddfI"), partial(_navaid, NavaidKind.NDB), b"N"),
    FacilityKind.VOR: FacilityListFormat(FacilityListType.VOR, ReceiveId.VOR_LIST,
                                         Struct("<6s3sdddfIIfdddf"), partial(_navaid, NavaidKind.VOR), b"V"),
}
LIST_RECEIVE_IDS = frozenset(layout.receive_id for layout in LIST_FORMATS.values())


@dataclass(slots=True)
class ListPages:
    total: int | None = None
    count: int = 0
    pages: dict[int, tuple] = field(default_factory=dict)

    def receive(self, prefix, payload, wire, decode, label):
        if self.total is None and not (prefix.entry_number or prefix.out_of or prefix.array_size or payload):
            return ()
        if (not 0 <= prefix.entry_number < prefix.out_of <= _MAX_LIST_ENTRIES
                or prefix.array_size > _MAX_LIST_ENTRIES
                or len(payload) != prefix.array_size * wire.size
                or self.total is not None and prefix.out_of != self.total):
            raise ProviderError(f"Invalid simulator {label} page.")
        records = tuple(decode(*fields) for fields in wire.iter_unpack(payload))
        previous = self.pages.get(prefix.entry_number)
        if previous is not None:
            if previous != records:
                raise ProviderError(f"The simulator returned conflicting {label} pages.")
            return None
        if self.count + len(records) > _MAX_LIST_ENTRIES:
            raise ProviderError(f"The simulator {label} exceeded the supported response size.")
        self.total = prefix.out_of
        self.count += len(records)
        self.pages[prefix.entry_number] = records
        if len(self.pages) == self.total:
            return tuple(record for index in range(self.total) for record in self.pages[index])
        return None
