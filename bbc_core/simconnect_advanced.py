"""Advanced SDK requests, hardware mappings and object operations."""

from __future__ import annotations

from ctypes import create_string_buffer, sizeof
from dataclasses import dataclass, field
import logging
from math import isfinite
from re import sub
from struct import Struct
from time import monotonic
from uuid import uuid4

from .contract import ValueType
from .facilities import FacilityState
from .provider import CommandRejected, FacilityReply, Outcome, ProviderError, Signal
from .sdk_simvar import SimVar
from .simconnect_abi import (
    DataType, NativeInitialPosition, Period, ReceiveActionCallback, ReceiveAssignedObject,
    ReceiveEvent, ReceiveId, ReceiveListPrefix, ReceiveRequest, ReceiveReservedKey, ReceiveSimObjectDataPrefix,
)
from .simconnect_facility_lists import ListPages
from .simconnect_ledger import PacketLedger
from .simulator_ops import (
    ControllerInfo, HardwareSample, ObjectFormat, ObjectKind, ObjectSample, SimObjectRef, SimulatorOperation,
)


OPERATIONS = {f"sdk.simulator.{item.value}": item for item in SimulatorOperation}
_STRUCTURES = {
    ObjectFormat.POSITION: (DataType.INITPOSITION, Struct("<6dII")),
    ObjectFormat.MARKER: (DataType.MARKERSTATE, Struct("<64sI")),
    ObjectFormat.WAYPOINT: (DataType.WAYPOINT, Struct("<3dI2d")),
    ObjectFormat.LATLONALT: (DataType.LATLONALT, Struct("<3d")),
    ObjectFormat.XYZ: (DataType.XYZ, Struct("<3d")),
    ObjectFormat.VARIABLE_TEXT: (DataType.STRINGV, None),
}
_OBJECT_TYPES = {kind: index for index, kind in enumerate(ObjectKind)}
_CREATE = {
    SimulatorOperation.CREATE_PARKED_AIRCRAFT: "SimConnect_AICreateParkedATCAircraft",
    SimulatorOperation.CREATE_ENROUTE_AIRCRAFT: "SimConnect_AICreateEnrouteATCAircraft",
    SimulatorOperation.CREATE_NON_ATC_AIRCRAFT: "SimConnect_AICreateNonATCAircraft",
    SimulatorOperation.CREATE_SIMULATED_OBJECT: "SimConnect_AICreateSimulatedObject",
}
_OBJECT_COMMANDS = {
    SimulatorOperation.RELEASE_AI: "SimConnect_AIReleaseControl",
    SimulatorOperation.REMOVE_OBJECT: "SimConnect_AIRemoveObject",
    SimulatorOperation.SET_AI_FLIGHT_PLAN: "SimConnect_AISetAircraftFlightPlan",
}
_FLIGHT_COMMANDS = {
    SimulatorOperation.LOAD_FLIGHT: "SimConnect_FlightLoad",
    SimulatorOperation.SAVE_FLIGHT: "SimConnect_FlightSave",
    SimulatorOperation.LOAD_FLIGHT_PLAN: "SimConnect_FlightPlanLoad",
}
OPERATION_EXPORTS = {
    **{operation: (function,) for operation, function in _CREATE.items()},
    **{operation: (function,) for operation, function in _OBJECT_COMMANDS.items()},
    **{operation: (function,) for operation, function in _FLIGHT_COMMANDS.items()},
    SimulatorOperation.RESERVE_KEY: ("SimConnect_RequestReservedKey", "SimConnect_MapClientEventToSimEvent"),
    SimulatorOperation.EXECUTE_ACTION: ("SimConnect_ExecuteAction",),
    SimulatorOperation.WRITE_OBJECT_DATA: ("SimConnect_SetDataOnSimObject", "SimConnect_AddToDataDefinition"),
}
_SILENT = {SimulatorOperation.SAVE_FLIGHT, SimulatorOperation.LOAD_FLIGHT_PLAN, SimulatorOperation.RESERVE_KEY}
_CONTROLLER = Struct("<256sIII4H")
_UNUSED = 0xFFFFFFFF
_TIMEOUT = 8.0
_LATE_CREATE_WINDOW_S = 600.0
_HIGHEST_PRIORITY = 1  # SIMCONNECT_GROUP_PRIORITY_HIGHEST
_LOG = logging.getLogger(__name__)


def reserved_key_event_name(key):
    """The client event name that delivers presses of a reserved key.

    For a reservation returned as ``"Tab+Q"`` a module calls
    ``subscribe_client_event("RESERVED_KEY_TAB_Q")``.
    """
    return "RESERVED_KEY_" + sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")


def _text(raw):
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def _controller(name, device, product, composite, *version):
    return ControllerInfo(_text(name), device, product, composite, version)


@dataclass(slots=True)
class _Read:
    id: str
    query: object
    deadline: float
    definition: int = 0
    decoder: Struct | None = None
    total: int | None = None
    records: dict[int, ObjectSample] = field(default_factory=dict)


class SimConnectAdvanced:
    def __init__(self, session, call, last_packet, new_id, offer, reading, encoding, *, private_event=None):
        self._session = session
        self._call, self._last_packet, self._new_id = call, last_packet, new_id
        self._offer, self._reading, self._encoding = offer, reading, encoding
        self._private_event = private_event
        self._requests: dict[int, _Read] = {}
        self._definitions = {}
        self._definition_errors = {}
        self._sent = PacketLedger()
        self._controllers: _Read | None = None
        self._controller_pages = ListPages()
        self._controller_error = ""
        self._controller_reply_id = None
        self._group = None
        self._notify_group = None
        self._bindings = {}
        self._keys = {}
        self._failed_inputs = set()
        self._owned = {}
        self._reservation_unknown = False
        self._reservations: dict[str, int] = {}  # RESERVE_KEY command ID -> private event ID
        self._late_creates = PacketLedger(_LATE_CREATE_WINDOW_S)  # create callback ID -> consumer ID

    def _track(self, category, item):
        self._sent[self._last_packet()] = category, item

    def _define(self, field, *, for_command=False):
        if field in self._definition_errors:
            # A consumer-supplied field the simulator refused is the consumer's
            # problem: reject the command rather than end the shared session.
            raise (CommandRejected if for_command else ProviderError)(self._definition_errors[field])
        if isinstance(field, SimVar):
            name, unit = field.sdk_name, field.unit
            datatype, decoder = self._encoding(Signal(name, unit, field.value_type, simvar_format=field.format))
        else:
            name, unit = field.name, None
            datatype, decoder = _STRUCTURES[field.format]
        if field not in self._definitions:
            definition = self._new_id()
            self._call("SimConnect_AddToDataDefinition", definition, name.encode("ascii"),
                       unit.encode("ascii") if unit else None, datatype, 0.0, _UNUSED)
            self._track("definition", field)
            self._definitions[field] = definition
        return self._definitions[field], decoder

    def request(self, id, query):
        read = _Read(id, query, monotonic() + _TIMEOUT)
        try:
            if query.kind == "controllers":
                if self._controller_error:
                    raise ProviderError(self._controller_error)
                if self._controllers is not None:
                    raise ProviderError("An earlier controller query is still outstanding.")
                self._call("SimConnect_EnumerateControllers")
                self._track("controllers", id)
                self._controllers = read
                return
            if query.object is not None and (query.object.session_id != self._session
                                            or type(query.object.object_id) is not int
                                            or not 0 <= query.object.object_id < _UNUSED):
                raise ProviderError("Use a valid object reference from the current SimConnect session.")
            read.definition, read.decoder = self._define(query.field)
            request = self._new_id()
            if query.kind == "nearby":
                self._call("SimConnect_RequestDataOnSimObjectType", request, read.definition,
                           query.radius, _OBJECT_TYPES[query.object_kind])
            else:
                self._call("SimConnect_RequestDataOnSimObject", request, read.definition,
                           query.object.object_id if query.object is not None else 0, Period.ONCE, 0, 0, 0, 0)
            self._track("read", request)
            self._requests[request] = read
        except ProviderError as error:
            self._publish(read, detail=str(error))

    def _publish(self, read, data=None, *, detail=""):
        self._offer(FacilityReply(self._session, read.id, read.query,
                                  FacilityState.UNAVAILABLE if detail else FacilityState.READY, data, detail))

    def receive_object(self, kind, packet):
        if len(packet) < sizeof(ReceiveRequest):
            return False
        request = ReceiveRequest.from_buffer_copy(packet).request_id
        read = self._requests.get(request)
        if read is None:
            return False
        try:
            if len(packet) < sizeof(ReceiveSimObjectDataPrefix):
                raise ProviderError("Truncated SimConnect object reply.")
            prefix = ReceiveSimObjectDataPrefix.from_buffer_copy(packet)
            expected = ReceiveId.SIMOBJECT_DATA_BYTYPE if read.query.kind == "nearby" else ReceiveId.SIMOBJECT_DATA
            if kind != expected or prefix.definition_id != read.definition or prefix.flags:
                raise ProviderError("Invalid SimConnect object reply.")
            payload = packet[sizeof(ReceiveSimObjectDataPrefix):]
            if (kind == ReceiveId.SIMOBJECT_DATA_BYTYPE and read.total is None
                    and not (prefix.out_of or prefix.entry_number or prefix.definition_count or payload)):
                self._publish(self._requests.pop(request), ())
                return True
            if prefix.definition_count not in (1, (len(payload) + 7) // 8) or prefix.object_id == _UNUSED:
                raise ProviderError("Invalid SimConnect object data definition count.")
            sample = ObjectSample(SimObjectRef(self._session, prefix.object_id), self._decode(read, payload))
            if read.query.kind != "nearby":
                if read.query.object is not None and sample.object.object_id != read.query.object.object_id:
                    raise ProviderError("The simulator returned a different object than requested.")
                self._publish(self._requests.pop(request), sample)
            else:
                self._object_page(request, read, prefix, sample)
        except ProviderError as error:
            self._publish(self._requests.pop(request), detail=str(error))
        return True

    @staticmethod
    def _decode(read, payload):
        if read.decoder is None:
            if b"\0" not in payload:
                raise ProviderError("Unterminated SimConnect variable-length string.")
            return _text(payload)
        if len(payload) != read.decoder.size:
            raise ProviderError("Invalid SimConnect object payload size.")
        fields = read.decoder.unpack(payload)
        if any(type(value) is float and not isfinite(value) for value in fields):
            raise ProviderError("Invalid non-finite SimConnect object value.")
        if isinstance(read.query.field, SimVar):
            value, = fields
            if read.query.field.value_type is ValueType.BOOLEAN:
                return bool(value)
            return _text(value) if isinstance(value, bytes) else value
        return tuple(_text(value) if isinstance(value, bytes) else value for value in fields)

    def _object_page(self, request, read, prefix, sample):
        if (not 1 <= prefix.entry_number <= prefix.out_of <= 65_536
                or read.total is not None and read.total != prefix.out_of):
            raise ProviderError("Invalid SimConnect nearby object list index.")
        read.total = prefix.out_of
        if read.records.setdefault(prefix.entry_number, sample) != sample:
            raise ProviderError("Conflicting SimConnect object list records.")
        if len(read.records) == read.total:
            self._publish(self._requests.pop(request), tuple(read.records[i] for i in range(1, read.total + 1)))

    def receive_controllers(self, packet):
        read = self._controllers
        if read is None:
            return
        try:
            if len(packet) < sizeof(ReceiveListPrefix):
                raise ProviderError("Truncated simulator controller list.")
            prefix = ReceiveListPrefix.from_buffer_copy(packet)
            if self._controller_reply_id is not None and prefix.request_id != self._controller_reply_id:
                raise ProviderError("Mismatched simulator controller list pages.")
            self._controller_reply_id = prefix.request_id
            data = self._controller_pages.receive(prefix, packet[sizeof(ReceiveListPrefix):],
                                                  _CONTROLLER, _controller, "controller list")
            if data is not None:
                self._finish_controllers(data)
        except ProviderError as error:
            self._finish_controllers(detail=str(error), ambiguous=True)

    def _finish_controllers(self, data=None, *, detail="", ambiguous=False):
        if ambiguous:
            self._controller_error = f"{detail} Start a fresh SimConnect session before querying controllers again."
        self._publish(self._controllers, data, detail=detail)
        self._controllers = None
        self._controller_pages = ListPages()
        self._controller_reply_id = None

    def _cleanup(self, function, *arguments):
        """Send a cleanup call. A failure is logged and never ends the session.

        Once the key mapping is removed locally, any packet the simulator still
        delivers for it matches nothing and is ignored.
        """
        try:
            self._call(function, *arguments)
        except ProviderError as error:
            _LOG.warning("SimConnect input cleanup %s failed: %s", function, error)
        else:
            self._track("input_cleanup", None)

    def replace_inputs(self, desired):
        self._failed_inputs.intersection_update(desired)
        removed = set(self._bindings).difference(desired)
        if removed and len(removed) == len(self._bindings):
            self._cleanup("SimConnect_SetInputGroupState", self._group, 0)
            self._cleanup("SimConnect_ClearInputGroup", self._group)
            if self._notify_group is not None:
                self._cleanup("SimConnect_ClearNotificationGroup", self._notify_group)
            self._bindings.clear()
            self._keys.clear()
        else:
            for signal in removed:
                ids = self._bindings.pop(signal)
                for id in ids:
                    self._keys.pop(id, None)
                    if id != _UNUSED and self._notify_group is not None:
                        self._cleanup("SimConnect_RemoveClientEvent", self._notify_group, id)
                self._cleanup("SimConnect_RemoveInputEvent", self._group, signal.hardware_input.sdk_name.encode("ascii"))
        for signal in desired:
            if signal not in self._bindings and signal not in self._failed_inputs:
                self._bind(signal)

    def _bind(self, signal):
        control = signal.hardware_input
        if self._group is None:
            self._group = self._new_id()
        if self._notify_group is None:
            # Private client events reach the client only through a notification
            # group, as in the SDK's input and joystick samples.
            self._notify_group = self._new_id()
        down = self._new_id()
        up = self._new_id() if control.control == "button" else _UNUSED
        first = not self._bindings
        try:
            for id in (down, up):
                if id != _UNUSED:
                    self._call("SimConnect_MapClientEventToSimEvent", id, b"")
                    self._track("input_setup", (signal, down))
                    self._call("SimConnect_AddClientEventToNotificationGroup", self._notify_group, id, 0)
                    self._track("input_setup", (signal, down))
            self._call("SimConnect_MapInputEventToClientEvent_EX1", self._group,
                       control.sdk_name.encode("ascii"), down, int(up != _UNUSED), up, 0, 0)
            self._track("input_map", (signal, down))
            if first:
                self._call("SimConnect_SetNotificationGroupPriority", self._notify_group, _HIGHEST_PRIORITY)
                self._track("input_group", None)
                self._call("SimConnect_SetInputGroupPriority", self._group, 2_000_000_000)
                self._track("input_group", None)
                self._call("SimConnect_SetInputGroupState", self._group, 1)
                self._track("input_group", None)
        except ProviderError as error:
            self._fail_input(signal, str(error))
            return
        self._bindings[signal] = (down, up)
        self._keys[down] = signal, True if up != _UNUSED else None
        if up != _UNUSED:
            self._keys[up] = signal, False

    def receive_input(self, packet):
        if len(packet) != sizeof(ReceiveEvent):
            return False
        event = ReceiveEvent.from_buffer_copy(packet)
        mapping = self._keys.get(event.event_id)
        if mapping is None:
            return False
        signal, pressed = mapping
        if (event.group_id in (self._notify_group, self._group, _UNUSED)
                and signal not in self._failed_inputs):
            value = event.data - (1 << 32) if event.data & (1 << 31) else event.data
            self._reading(signal, HardwareSample(signal.hardware_input, value, pressed))
        return True

    def _fail_input(self, signal, detail):
        self._failed_inputs.add(signal)
        self._reading(signal, None, error=detail)

    def receive_exception(self, send_id, detail):
        tracked = self._sent.pop(send_id, None)
        if tracked is None:
            return False
        operation, key = tracked
        if operation == "input_cleanup":
            _LOG.warning("SimConnect rejected a controller input cleanup call: %s", detail)
        elif operation == "input_group":
            # Without group priority or state no mapping can deliver input.
            # Report every mapping as failed; the session continues.
            for signal in tuple(self._bindings):
                if signal not in self._failed_inputs:
                    self._fail_input(signal, f"The simulator refused the controller input group: {detail}")
        elif operation in ("input_setup", "input_map"):
            signal, down = key
            if self._bindings.get(signal, (None,))[0] == down:
                if operation == "input_map":
                    for id in self._bindings.pop(signal):
                        self._keys.pop(id, None)
                    if not self._bindings:
                        self._cleanup("SimConnect_SetInputGroupState", self._group, 0)
                if signal not in self._failed_inputs:
                    self._fail_input(signal, detail)
        elif operation == "definition":
            self._definition_errors[key] = detail
            for request, read in tuple(self._requests.items()):
                if read.query.field == key:
                    self._publish(self._requests.pop(request), detail=detail)
        elif operation in ("reservation_map", "late_remove"):
            _LOG.warning("SimConnect rejected %s: %s", operation.replace("_", " "), detail)
        elif operation == "controllers":
            if self._controllers is not None and self._controllers.id == key:
                self._finish_controllers(detail=detail, ambiguous=self._controller_reply_id is not None)
        elif key in self._requests:
            self._publish(self._requests.pop(key), detail=detail)
        return True

    def expire(self):
        now = monotonic()
        for request, read in tuple(self._requests.items()):
            if now >= read.deadline:
                self._publish(self._requests.pop(request), detail="SimConnect object query timed out.")
        if self._controllers is not None and now >= self._controllers.deadline:
            self._finish_controllers(detail="SimConnect controller query timed out.", ambiguous=True)

    def prepare(self, command):
        operation = OPERATIONS[command.operation]
        args = command.arguments
        callback = None
        if operation in _CREATE:
            callback = self._new_id()
            if operation in (SimulatorOperation.CREATE_NON_ATC_AIRCRAFT, SimulatorOperation.CREATE_SIMULATED_OBJECT):
                args = (*args[:-1], NativeInitialPosition.from_buffer_copy(args[-1]))
            return _CREATE[operation], (*args, callback), callback
        if operation in _FLIGHT_COMMANDS:
            return _FLIGHT_COMMANDS[operation], (*args, 0) if operation is SimulatorOperation.SAVE_FLIGHT else args, None
        if operation is SimulatorOperation.RESERVE_KEY:
            if self._reservation_unknown:
                raise CommandRejected("Start a fresh SimConnect session before making another ambiguous key reservation.")
            event_id = self._new_id()
            self._call("SimConnect_MapClientEventToSimEvent", event_id, b"")
            self._track("reservation_map", None)
            self._reservations[command_key(command)] = event_id
            return "SimConnect_RequestReservedKey", (event_id, *args), _UNUSED
        if operation is SimulatorOperation.EXECUTE_ACTION:
            callback = self._new_id()
            name, payload = args
            return "SimConnect_ExecuteAction", (callback, name, len(payload), self._buffer(payload)), callback
        target = args[0]
        if self._owned.get(target.object_id) != (command.consumer_id, target):
            raise CommandRejected("This consumer does not own the object in the active simulator session.")
        if operation is SimulatorOperation.WRITE_OBJECT_DATA:
            field, payload = args[1:]
            _, decoder = _STRUCTURES[field.format]
            if decoder is None or len(payload) != decoder.size:
                raise CommandRejected("The packed structured data does not fit its fixed SDK format.")
            definition, _ = self._define(field, for_command=True)
            return "SimConnect_SetDataOnSimObject", (definition, target.object_id, 0, 1, len(payload),
                                                    self._buffer(payload)), None
        return _OBJECT_COMMANDS[operation], (target.object_id, *args[1:], self._new_id()), None

    @staticmethod
    def _buffer(payload):
        return create_string_buffer(payload, len(payload)) if payload else None

    def command_reply(self, pending, kind, packet):
        if pending is None or pending.command.operation not in OPERATIONS or pending.response_id is None:
            return None
        operation = OPERATIONS[pending.command.operation]
        if operation in _CREATE and kind == ReceiveId.ASSIGNED_OBJECT_ID:
            if len(packet) < sizeof(ReceiveAssignedObject):
                return None
            reply = ReceiveAssignedObject.from_buffer_copy(packet)
            if reply.request_id != pending.response_id or reply.object_id in (0, _UNUSED):
                return None
            target = SimObjectRef(self._session, reply.object_id, uuid4().hex)
            self._owned[target.object_id] = pending.command.consumer_id, target
            return Outcome.CONFIRMED, "SimConnect assigned an object ID.", target
        if operation is SimulatorOperation.EXECUTE_ACTION and kind == ReceiveId.ACTION_CALLBACK:
            if len(packet) >= sizeof(ReceiveActionCallback):
                reply = ReceiveActionCallback.from_buffer_copy(packet)
                if reply.request_id == pending.response_id and reply.action == pending.command.arguments[0]:
                    return Outcome.UNVERIFIED, "SimConnect returned the action callback. Its effect is unverified.", None
        if operation is SimulatorOperation.RESERVE_KEY and kind == ReceiveId.RESERVED_KEY:
            if len(packet) >= sizeof(ReceiveReservedKey):
                reply = ReceiveReservedKey.from_buffer_copy(packet)
                if not reply.choice:
                    self._reservations.pop(command_key(pending.command), None)
                    return Outcome.REJECTED, "SimConnect returned no reserved key.", None
                if reply.choice in pending.command.arguments and reply.key:
                    key = _text(reply.key)
                    event_id = self._reservations.pop(command_key(pending.command), None)
                    detail = "SimConnect returned a reserved key."
                    if event_id is not None and self._private_event is not None:
                        name = reserved_key_event_name(key)
                        self._private_event(name, event_id)
                        detail += f" Subscribe to client event {name} to receive its presses."
                    return Outcome.CONFIRMED, detail, key
        return None

    def late_assignment(self, packet):
        """Handle an ASSIGNED_OBJECT_ID that arrived after its create command finished.

        The consumer was told the create was unverified and holds no reference,
        so Core adopts the object and removes it rather than leave an orphan.
        """
        if len(packet) < sizeof(ReceiveAssignedObject):
            return
        reply = ReceiveAssignedObject.from_buffer_copy(packet)
        consumer = self._late_creates.pop(reply.request_id, None)
        if consumer is None or reply.object_id in (0, _UNUSED):
            return
        _LOG.warning("Removing simulator object %s created after its command timed out.", reply.object_id)
        try:
            self._call("SimConnect_AIRemoveObject", reply.object_id, self._new_id())
        except ProviderError as error:
            _LOG.warning("Could not remove late simulator object %s: %s", reply.object_id, error)
        else:
            self._track("late_remove", reply.object_id)

    def command_timeout(self, pending):
        command = pending.command
        operation = OPERATIONS[command.operation]
        self._reservations.pop(command_key(command), None)
        if operation in _CREATE and pending.response_id is not None:
            self._late_creates[pending.response_id] = command.consumer_id
        if operation is SimulatorOperation.RESERVE_KEY:
            self._reservation_unknown = True
        if operation is SimulatorOperation.REMOVE_OBJECT:
            self._owned.pop(command.arguments[0].object_id, None)
        if operation in _SILENT:
            return "Dispatched. MSFS 2020 may give no response and the effect is unverified."
        return "Dispatched. No confirmed simulator result was received."

    def owned_object_removed(self, object_id):
        self._owned.pop(object_id, None)

    def flight_changed(self):
        self._owned.clear()
        self._late_creates.clear()


def command_key(command):
    return command.id
