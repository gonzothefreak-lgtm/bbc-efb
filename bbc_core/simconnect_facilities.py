"""Session-owned, one-shot SimConnect airport and facility reads."""

from collections import deque
from ctypes import c_int32, create_string_buffer, sizeof
from dataclasses import dataclass, field
from math import isfinite
from struct import Struct
from time import monotonic

from .facilities import (
    AirportDetails, AirportFilter, AirportFilterField, AirportJetway, AirportReference, FacilityKind,
    FacilityQuery, FacilityState, JetwayState,
    NavaidDetails, NavaidKind, ParkingPosition, Runway,
)
from .provider import FacilityReply, ProviderError
from .simconnect_abi import FacilityDataType, ReceiveFacilityDataPrefix, ReceiveId, ReceiveListPrefix, ReceiveRequest
from .simconnect_facility_cache import SimConnectFacilityCache
from .simconnect_facility_lists import (
    LIST_FORMATS, LIST_RECEIVE_IDS, ListPages, airport_reference as _airport_reference,
    facility_text as _text, valid_coordinates as _coordinates,
)


_TIMEOUT = 8.0
_MAX_OUTSTANDING = 8
_MAX_CHILDREN = 8_192
_AIRPORT = Struct("<8s8s64sdddiii")
_JETWAY = Struct("<8sidddfffii12dII")
_NAVAIDS = {
    NavaidKind.VOR: (
        FacilityDataType.VOR, b"V", Struct("<dddiIf64sf"),
        "VOR_LATITUDE VOR_LONGITUDE VOR_ALTITUDE TYPE FREQUENCY MAGVAR NAME NAV_RANGE",
    ),
    NavaidKind.NDB: (
        FacilityDataType.NDB, b"N", Struct("<dddiIf64sfi"),
        "LATITUDE LONGITUDE ALTITUDE TYPE FREQUENCY MAGVAR NAME RANGE IS_TERMINAL_NDB",
    ),
    NavaidKind.WAYPOINT: (
        FacilityDataType.WAYPOINT, b"W", Struct("<dddif8s8si"),
        "LATITUDE LONGITUDE ALTITUDE TYPE MAGVAR ICAO REGION IS_TERMINAL_WPT",
    ),
}
_CHILDREN = {
    FacilityDataType.RUNWAY: (
        "RUNWAY", "LATITUDE LONGITUDE HEADING LENGTH WIDTH SURFACE PRIMARY_NUMBER PRIMARY_DESIGNATOR "
        "SECONDARY_NUMBER SECONDARY_DESIGNATOR", Struct("<ddfffiiiii"), Runway,
    ),
    FacilityDataType.TAXI_PARKING: (
        "TAXI_PARKING", "TYPE NAME SUFFIX NUMBER HEADING RADIUS BIAS_X BIAS_Z",
        Struct("<iiiIffff"), ParkingPosition,
    ),
    FacilityDataType.JETWAY: (
        "JETWAY", "PARKING_GATE PARKING_SUFFIX PARKING_SPOT", Struct("<iii"), AirportJetway,
    ),
}
_FILTER_FIELDS = {
    AirportFilterField.AIRPORT_NAME: (FacilityDataType.AIRPORT, "name"),
    AirportFilterField.RUNWAY_PRIMARY_NUMBER: (FacilityDataType.RUNWAY, "primary_number"),
    AirportFilterField.RUNWAY_SECONDARY_NUMBER: (FacilityDataType.RUNWAY, "secondary_number"),
    AirportFilterField.RUNWAY_SURFACE: (FacilityDataType.RUNWAY, "surface"),
    AirportFilterField.RUNWAY_LENGTH: (FacilityDataType.RUNWAY, "length_m"),
    AirportFilterField.PARKING_TYPE: (FacilityDataType.TAXI_PARKING, "kind"),
    AirportFilterField.PARKING_NAME: (FacilityDataType.TAXI_PARKING, "name"),
    AirportFilterField.PARKING_NUMBER: (FacilityDataType.TAXI_PARKING, "number"),
    AirportFilterField.PARKING_RADIUS: (FacilityDataType.TAXI_PARKING, "radius_m"),
    AirportFilterField.JETWAY_PARKING_GATE: (FacilityDataType.JETWAY, "parking_gate"),
    AirportFilterField.JETWAY_PARKING_SPOT: (FacilityDataType.JETWAY, "parking_spot"),
}


def _airport_fields():
    yield "OPEN AIRPORT"
    yield from "ICAO REGION NAME64 LATITUDE LONGITUDE ALTITUDE N_RUNWAYS N_TAXI_PARKINGS N_JETWAYS".split()
    for name, fields, _, _ in _CHILDREN.values():
        yield f"OPEN {name}"
        yield from fields.split()
        yield f"CLOSE {name}"
    yield "CLOSE AIRPORT"


def _jetway_record(query, icao, index, lat, lon, alt, pitch, bank, heading, status, door, *details):
    airport = _text(icao)
    if (airport != query.icao or index < 0 or query.parking_indices and index not in query.parking_indices):
        raise ProviderError("The jetway reply did not match the requested airport or parking indices.")
    if not _coordinates(lat, lon) or not all(isfinite(x) for x in (alt, pitch, bank, heading, *details[:12])):
        raise ProviderError("The simulator returned an invalid jetway position.")
    return JetwayState(airport, index, lat, lon, alt, pitch, bank, heading, status, door,
                       tuple(details[:3]), tuple(details[3:6]), tuple(details[6:9]), tuple(details[9:12]),
                       details[12], details[13])


@dataclass(slots=True)
class _AirportResponse:
    filters: tuple[AirportFilter, ...]
    reference: AirportReference | None = None
    name: str = ""
    root_id: int | None = None
    counts: tuple[int, ...] = ()
    children: dict = field(default_factory=lambda: {kind: {} for kind in _CHILDREN})

    def receive(self, query, prefix, payload):
        if prefix.facility_type == FacilityDataType.AIRPORT:
            self._root(query, prefix, payload)
            return
        definition = _CHILDREN.get(prefix.facility_type)
        if definition is None:
            raise ProviderError("Unexpected simulator airport record type.")
        _, _, decoder, record_type = definition
        if (prefix.is_list_item != 1 or not 0 <= prefix.item_index < prefix.list_size <= _MAX_CHILDREN
                or len(payload) != decoder.size):
            raise ProviderError("Invalid simulator airport child index or record size.")
        record = record_type(prefix.item_index, *decoder.unpack(payload))
        for rule in self.filters:
            node, attribute = _FILTER_FIELDS[rule.field]
            if node == prefix.facility_type and getattr(record, attribute) != rule.value:
                raise ProviderError("The simulator returned a record outside the requested airport filter.")
        if isinstance(record, Runway) and (
            not _coordinates(record.latitude_deg, record.longitude_deg)
            or not all(isfinite(value) and value >= 0 for value in (record.length_m, record.width_m))
            or not isfinite(record.heading_deg)
        ):
            raise ProviderError("Invalid simulator runway position or dimensions.")
        if isinstance(record, ParkingPosition) and (
            not all(isfinite(value) for value in (record.heading_deg, record.radius_m, record.bias_x_m, record.bias_z_m))
            or record.radius_m < 0
        ):
            raise ProviderError("Invalid simulator parking position.")
        entry = (prefix.parent_id, prefix.list_size, record)
        stored = self.children[prefix.facility_type]
        if stored.setdefault(prefix.item_index, entry) != entry:
            raise ProviderError("The simulator returned conflicting airport records.")

    def _root(self, query, prefix, payload):
        if prefix.is_list_item or prefix.parent_id or len(payload) != _AIRPORT.size:
            raise ProviderError("Invalid simulator airport record.")
        icao, region, name, latitude, longitude, altitude, *counts = _AIRPORT.unpack(payload)
        reference = _airport_reference(_text(icao), _text(region), latitude, longitude, altitude)
        if reference.icao != query.icao or (query.region and reference.region != query.region):
            raise ProviderError("The simulator returned a different airport than requested.")
        if any(not 0 <= count <= _MAX_CHILDREN for count in counts):
            raise ProviderError("The simulator returned an invalid airport child count.")
        root = (reference, _text(name), prefix.unique_id, tuple(counts))
        if any(rule.field is AirportFilterField.AIRPORT_NAME and rule.value != root[1] for rule in self.filters):
            raise ProviderError("The simulator returned an airport outside the requested name filter.")
        if self.reference is not None and root != (self.reference, self.name, self.root_id, self.counts):
            raise ProviderError("The simulator returned conflicting airport records.")
        self.reference, self.name, self.root_id, self.counts = root

    def complete(self):
        if self.reference is None:
            if any(self.children.values()):
                raise ProviderError("The simulator returned airport children without an airport record.")
            return None
        lists = []
        filtered_nodes = {_FILTER_FIELDS[rule.field][0] for rule in self.filters}
        for count, (node, children) in zip(self.counts, self.children.items(), strict=True):
            sizes = {size for _, size, _ in children.values()}
            wrong_parent = any(parent != self.root_id for parent, _, _ in children.values())
            if node in filtered_nodes:
                # SDK replies may retain the source list size and original item indices.
                valid_count = len(sizes) <= 1 and all(
                    size <= count and (size == count or size == len(children)) for size in sizes
                )
            else:
                valid_count = len(children) == count and all(size == count for size in sizes)
            if wrong_parent or not valid_count:
                raise ProviderError("The simulator returned incomplete or mismatched airport records.")
            lists.append(tuple(children[index][2] for index in sorted(children)))
        return AirportDetails(self.reference, self.name, *lists)


@dataclass(slots=True)
class _NavaidResponse:
    records: dict[int, NavaidDetails] = field(default_factory=dict)

    def receive(self, query, prefix, payload):
        kind = query.navaid_kind
        expected, _, decoder, _ = _NAVAIDS[kind]
        if (prefix.facility_type != expected or prefix.parent_id or prefix.is_list_item
                or len(payload) != decoder.size):
            raise ProviderError("Invalid simulator navaid record.")
        lat, lon, alt, code, *fields = decoder.unpack(payload)
        if not _coordinates(lat, lon) or not isfinite(alt):
            raise ProviderError("The simulator returned an invalid navaid position.")
        if kind is NavaidKind.WAYPOINT:
            magvar, ident, region, terminal = fields
            ident, region = _text(ident), _text(region)
            if ident != query.icao or (query.region and query.region != region):
                raise ProviderError("The simulator returned a different waypoint than requested.")
            record = NavaidDetails(kind, ident, region, lat, lon, alt, code, magvar, terminal=bool(terminal))
        else:
            frequency, magvar, name, reach, *flag = fields
            if not isfinite(reach) or reach < 0:
                raise ProviderError("The simulator returned an invalid navaid range.")
            record = NavaidDetails(kind, query.icao, None, lat, lon, alt, code, magvar,
                                  _text(name), frequency, reach, bool(flag[0]) if flag else None)
        if not isfinite(magvar):
            raise ProviderError("The simulator returned an invalid magnetic variation.")
        if len(self.records) >= _MAX_CHILDREN and prefix.unique_id not in self.records:
            raise ProviderError("The simulator returned too many navaid records.")
        if self.records.setdefault(prefix.unique_id, record) != record:
            raise ProviderError("The simulator returned conflicting navaid records.")

    def complete(self):
        return tuple(self.records.values()) or None


@dataclass(slots=True)
class _Request:
    id: str
    query: FacilityQuery
    deadline: float
    response: _AirportResponse | _NavaidResponse | ListPages


@dataclass(slots=True)
class _JetwayRequest:
    item: _Request
    send_id: int
    response_id: int | None = None


@dataclass(slots=True)
class _FilteredDefinition:
    id: int
    request: int | None = None
    used: bool = False
    usable: bool = True


class SimConnectFacilities:
    RECEIVE_IDS = LIST_RECEIVE_IDS | {ReceiveId.FACILITY_DATA, ReceiveId.FACILITY_DATA_END,
                                     ReceiveId.FACILITY_MINIMAL_LIST, ReceiveId.JETWAY_DATA}

    def __init__(self, session, call, last_packet, new_id, offer, *, legacy_facility_additions=False):
        self._session = session
        self._call = call
        self._last_packet = last_packet
        self._new_id = new_id
        self._offer = offer
        self._definitions = {}
        self._filtered_definitions: list[_FilteredDefinition] = []
        self._filtered_requests: dict[int, _FilteredDefinition] = {}
        self._filter_packets: dict[int, tuple[_FilteredDefinition, int, bool]] = {}
        self._definition_errors = {}
        self._definition_packets = {}
        self._sent: dict[int, int] = {}
        self._requests: dict[int, _Request] = {}
        self._jetway_queue: deque[_Request] = deque()
        self._jetway: _JetwayRequest | None = None
        self._jetway_packets: dict[int, None] = {}
        self._jetway_error = ""
        self.cache = SimConnectFacilityCache(session, call, last_packet, new_id, offer, legacy=legacy_facility_additions)

    def request(self, id, query):
        if query.icao is None or query.parking_indices is not None:
            response = ListPages()
        elif query.navaid_kind is not None:
            response = _NavaidResponse()
        else:
            response = _AirportResponse(query.airport_filters)
        item = _Request(id, query, monotonic() + _TIMEOUT, response)
        if len(self._requests) + len(self._jetway_queue) + bool(self._jetway) >= _MAX_OUTSTANDING:
            self._publish(item, FacilityState.UNAVAILABLE, detail="Too many active simulator facility requests.")
            return
        if query.parking_indices is not None:
            if self._jetway_error:
                self._publish(item, FacilityState.UNAVAILABLE, detail=self._jetway_error)
            else:
                self._jetway_queue.append(item)
            return
        request = self._new_id()
        prepared = sent = False
        try:
            if query.icao is None:
                kind = FacilityKind.AIRPORT if query.navaid_kind is None else FacilityKind(query.navaid_kind.value)
                function = "SimConnect_RequestFacilitiesList" if query.worldwide_airports else "SimConnect_RequestFacilitiesList_EX1"
                self._call(function, LIST_FORMATS[kind].sdk_type, request)
            else:
                definition = (self._prepare_filters(request, query.airport_filters) if query.airport_filters
                              else self._ensure_definition(query.navaid_kind))
                prepared = True
                args = (definition, request, query.icao.encode("ascii"), query.region.encode("ascii"))
                if query.navaid_kind is None:
                    self._call("SimConnect_RequestFacilityData", *args)
                else:
                    self._call("SimConnect_RequestFacilityData_EX1", *args, _NAVAIDS[query.navaid_kind][1])
            sent = True
            self._sent[self._last_packet()] = request
        except ProviderError as error:
            if not sent:
                self._release_filtered(request, usable=prepared)
            self._publish(item, FacilityState.UNAVAILABLE, detail=str(error))
            return
        if len(self._sent) > 512:
            del self._sent[next(iter(self._sent))]
        self._requests[request] = item

    def _prepare_filters(self, request, filters):
        slot = next((item for item in self._filtered_definitions if item.usable and item.request is None), None)
        new_slot = slot is None
        if new_slot:
            if len(self._filtered_definitions) >= _MAX_OUTSTANDING:
                raise ProviderError("No airport filter definition is available in this session.")
            slot = _FilteredDefinition(self._new_id())
            self._filtered_definitions.append(slot)
        slot.request = request
        self._filtered_requests[request] = slot
        if new_slot:
            for field in _airport_fields():
                self._call("SimConnect_AddToFacilityDefinition", slot.id, field.encode("ascii"))
                self._track_filter(slot, request, fatal=True)
        if slot.used:
            self._call("SimConnect_ClearAllFacilityDataDefinitionFilters", slot.id)
            self._track_filter(slot, request, fatal=True)
        slot.used = True
        for rule in filters:
            payload = rule.sdk_payload()
            self._call("SimConnect_AddFacilityDataDefinitionFilter", slot.id, rule.field.value.encode("ascii"),
                       len(payload), create_string_buffer(payload, len(payload)))
            self._track_filter(slot, request, fatal=False)
        return slot.id

    def _track_filter(self, slot, request, *, fatal):
        self._filter_packets[self._last_packet()] = (slot, request, fatal)
        if len(self._filter_packets) > 4096:
            del self._filter_packets[next(iter(self._filter_packets))]

    def _release_filtered(self, request, *, usable=True):
        slot = self._filtered_requests.pop(request, None)
        if slot is not None:
            slot.request = None
            slot.usable = slot.usable and usable

    def _ensure_definition(self, kind):
        if kind in self._definition_errors:
            raise ProviderError(self._definition_errors[kind])
        if kind not in self._definitions:
            definition = self._new_id()
            name = kind.name if kind is not None else None
            fields = (f"OPEN {name}", *_NAVAIDS[kind][3].split(), f"CLOSE {name}") if name else _airport_fields()
            try:
                for field in fields:
                    self._call("SimConnect_AddToFacilityDefinition", definition, field.encode("ascii"))
                    self._definition_packets[self._last_packet()] = kind
            except ProviderError as error:
                self._definition_errors[kind] = str(error)
                raise
            self._definitions[kind] = definition
        return self._definitions[kind]

    def receive(self, kind, packet):
        if kind == ReceiveId.JETWAY_DATA:
            self._receive_jetway(packet)
            return
        if self.cache.receive(kind, packet):
            return
        if len(packet) < sizeof(ReceiveRequest):
            for request in tuple(self._requests):
                self._finish(request, FacilityState.UNAVAILABLE, detail="Truncated facility reply without a request ID.")
            return
        request = ReceiveRequest.from_buffer_copy(packet).request_id
        if kind == ReceiveId.FACILITY_DATA_END:
            self._release_filtered(request)
        item = self._requests.get(request)
        if item is None:
            return
        try:
            if isinstance(item.response, ListPages):
                navaid = item.query.navaid_kind
                layout = LIST_FORMATS[FacilityKind(navaid.value) if navaid is not None else FacilityKind.AIRPORT]
                if kind != layout.receive_id or len(packet) < sizeof(ReceiveListPrefix):
                    raise ProviderError("Unexpected or truncated simulator facility list page.")
                label = "airport list" if navaid is None else f"{navaid.value} list"
                data = item.response.receive(
                    ReceiveListPrefix.from_buffer_copy(packet), packet[sizeof(ReceiveListPrefix):],
                    layout.wire, layout.decode, label,
                )
                if data is not None:
                    self._finish(request, FacilityState.READY, data)
            elif kind == ReceiveId.FACILITY_DATA:
                if len(packet) < sizeof(ReceiveFacilityDataPrefix):
                    raise ProviderError("Truncated simulator facility record.")
                item.response.receive(
                    item.query, ReceiveFacilityDataPrefix.from_buffer_copy(packet), packet[sizeof(ReceiveFacilityDataPrefix):],
                )
            elif kind == ReceiveId.FACILITY_DATA_END:
                data = item.response.complete()
                if data is None:
                    self._finish(request, FacilityState.NO_DATA, detail="The simulator returned no matching facility.")
                else:
                    self._finish(request, FacilityState.READY, data)
            else:
                raise ProviderError("Unexpected response type for the simulator facility request.")
        except ProviderError as error:
            self._finish(request, FacilityState.UNAVAILABLE, detail=str(error))

    def receive_exception(self, send_id, detail):
        if self.cache.receive_exception(send_id, detail):
            return True
        filter_call = self._filter_packets.pop(send_id, None)
        if filter_call is not None:
            slot, request, fatal = filter_call
            if fatal:
                slot.usable = False
                request = slot.request if slot.request is not None else request
            if request in self._requests:
                self._finish(request, FacilityState.UNAVAILABLE, detail=detail)
            return True
        if send_id in self._definition_packets:
            kind = self._definition_packets[send_id]
            self._definition_errors[kind] = detail
            for request, item in tuple(self._requests.items()):
                if item.query.icao is not None and item.query.navaid_kind is kind:
                    self._finish(request, FacilityState.UNAVAILABLE, detail=detail)
            return True
        if send_id in self._jetway_packets:
            if self._jetway is not None and self._jetway.send_id == send_id:
                if self._jetway.response_id is not None:
                    self._stop_jetways(detail)
                else:
                    self._publish(self._jetway.item, FacilityState.UNAVAILABLE, detail=detail)
                    self._jetway = None
            return True
        request = self._sent.pop(send_id, None)
        if request is None:
            return False
        if request in self._requests:
            self._finish(request, FacilityState.UNAVAILABLE, detail=detail)
        self._release_filtered(request)
        return True

    def progress(self, *, transport_empty):
        self.cache.expire()
        now = monotonic()
        for request, item in tuple(self._requests.items()):
            if now >= item.deadline:
                self._finish(request, FacilityState.UNAVAILABLE, detail="Simulator facility request timed out.")
        if self._jetway is not None and now >= self._jetway.item.deadline:
            self._stop_jetways("Simulator jetway request timed out.")
        while self._jetway_queue and now >= self._jetway_queue[0].deadline:
            self._publish(self._jetway_queue.popleft(), FacilityState.UNAVAILABLE,
                          detail="The jetway request expired before it could be sent.")
        if transport_empty and self._jetway is None and self._jetway_queue:
            self._send_jetway(self._jetway_queue.popleft())

    def _send_jetway(self, item):
        # The SDK takes no caller-supplied request ID for this operation.
        # Dispatch one at a time and only after draining already queued replies.
        indices = item.query.parking_indices
        buffer = (c_int32 * len(indices))(*indices) if indices else None
        try:
            self._call("SimConnect_RequestJetwayData", item.query.icao.encode("ascii"), len(indices), buffer)
        except ProviderError as error:
            self._publish(item, FacilityState.UNAVAILABLE, detail=str(error))
            return
        try:
            sent = self._last_packet()
        except ProviderError as error:
            self._publish(item, FacilityState.UNAVAILABLE, detail=str(error))
            self._stop_jetways(str(error))
            return
        self._jetway = _JetwayRequest(item, sent)
        self._jetway_packets[sent] = None
        if len(self._jetway_packets) > 512:
            del self._jetway_packets[next(iter(self._jetway_packets))]

    def _receive_jetway(self, packet):
        active = self._jetway
        if active is None:
            return
        try:
            if len(packet) < sizeof(ReceiveListPrefix):
                raise ProviderError("Truncated simulator jetway list page.")
            prefix = ReceiveListPrefix.from_buffer_copy(packet)
            if active.response_id is not None and active.response_id != prefix.request_id:
                raise ProviderError("Mismatched simulator jetway list pages.")
            active.response_id = prefix.request_id
            item = active.item
            data = item.response.receive(
                prefix, packet[sizeof(ReceiveListPrefix):], _JETWAY,
                lambda *fields: _jetway_record(item.query, *fields), "jetway list",
            )
        except ProviderError as error:
            self._stop_jetways(str(error))
            return
        if data is not None:
            self._publish(item, FacilityState.READY, data)
            self._jetway = None

    def _stop_jetways(self, reason):
        # After an incomplete reply a late, empty packet cannot be attributed
        # to a future request on this handle. Only a fresh session resets this.
        self._jetway_error = f"{reason} Jetway queries require a fresh SimConnect session."
        if self._jetway is not None:
            self._publish(self._jetway.item, FacilityState.UNAVAILABLE, detail=self._jetway_error)
            self._jetway = None
        while self._jetway_queue:
            self._publish(self._jetway_queue.popleft(), FacilityState.UNAVAILABLE, detail=self._jetway_error)

    def _finish(self, request, state, data=None, detail=""):
        self._publish(self._requests.pop(request), state, data, detail)

    def _publish(self, item, state, data=None, detail=""):
        self._offer(FacilityReply(self._session, item.id, item.query, state, data, detail))
