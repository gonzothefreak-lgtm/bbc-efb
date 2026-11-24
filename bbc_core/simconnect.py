"""First direct MSFS 2020 provider for Core. All SDK calls have one serial owner."""

from __future__ import annotations

from collections import deque
from ctypes import create_string_buffer, sizeof
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from math import isclose, isfinite
from struct import Struct, error as StructError
from time import monotonic, sleep
from typing import Protocol
from uuid import UUID

from .aircraft_inputs import INPUT_WRITE
from .catalog import COM1_ACTIVE, COM1_STANDBY, OPERATIONS, SYSTEM_EVENTS, SYSTEM_STATES, CommandTransport
from .client_data import CLIENT_DATA_WRITE
from .contract import FrameSample, MissionAction, SimObjectChange, Source, ValueType
from .facilities import FacilityState
from .provider import (
    Command, CommandOutcome, CommandRejected, Demand, Disconnect, IngressGap,
    FacilityReply, Outcome, ProviderError, Reading, Signal,
)
from .sdk_simvar import CLIENT_EVENT_SEND, SIMVAR_WRITE, SimVarFormat
from .simconnect_abi import (
    ClientDataPeriod, DataType, Period, Receive, ReceiveCustomActionPrefix, ReceiveEvent,
    ReceiveEventFilename, ReceiveEventFrame, ReceiveEventObject, ReceiveException, ReceiveId,
    ReceiveOpen, ReceiveRequest, ReceiveSimObjectDataPrefix, ReceiveSystemState,
)
from .simconnect_client_data import SimConnectClientData
from .simconnect_advanced import OPERATIONS as ADVANCED_OPERATIONS, SimConnectAdvanced
from .simconnect_client_events import SimConnectClientEvents
from .simconnect_facilities import SimConnectFacilities
from .simconnect_input_events import SimConnectInputEvents
from .simulator_ops import SimulatorRead, SimulatorOperation


_USER_AIRCRAFT = 0
_UNUSED = 0xFFFFFFFF
_DEFAULT_GROUP_PRIORITY = 2_000_000_000
_GROUP_IS_PRIORITY = 0x10
_SET_STANDBY = "COM_STBY_RADIO_SET_HZ"
_SWAP = "COM_STBY_RADIO_SWAP"
_SYSTEM_STATE_NAMES = {
    "SIM": b"Sim", "DIALOGMODE": b"DialogMode", "AIRCRAFTLOADED": b"AircraftLoaded",
    "FLIGHTLOADED": b"FlightLoaded", "FLIGHTPLAN": b"FlightPlan",
}
_SYSTEM_EVENT_NAMES = {
    "PAUSE_EX1": b"Pause_EX1", "AIRCRAFTLOADED": b"AircraftLoaded", "FLIGHTLOADED": b"FlightLoaded",
}
_SYSTEM_STATE_TIMEOUT = 3.0
_DATATYPES = {
    ValueType.BOOLEAN: (DataType.INT32, Struct("<i")),
    ValueType.INTEGER: (DataType.INT32, Struct("<i")),
    ValueType.NUMBER: (DataType.FLOAT64, Struct("<d")),
    ValueType.TEXT: (DataType.STRING256, Struct("<256s")),
}
_SIMVAR_FORMATS = {
    SimVarFormat.INT64: (DataType.INT64, Struct("<q")),
    SimVarFormat.FLOAT32: (DataType.FLOAT32, Struct("<f")),
    SimVarFormat.STRING8: (DataType.STRING8, Struct("<8s")),
    SimVarFormat.STRING32: (DataType.STRING32, Struct("<32s")),
    SimVarFormat.STRING64: (DataType.STRING64, Struct("<64s")),
    SimVarFormat.STRING128: (DataType.STRING128, Struct("<128s")),
    SimVarFormat.STRING260: (DataType.STRING260, Struct("<260s")),
}
_RADIO_PAIR = Struct("<ii")


class _Sdk(Protocol):
    simulated: bool

    def open(self, application: bytes): ...
    def close(self, handle) -> None: ...
    def next_packet(self, handle) -> bytes | None: ...
    def call(self, function: str, *args) -> None: ...
    def last_packet_id(self, handle) -> int: ...


@dataclass(frozen=True, slots=True)
class _Registration:
    demand: Demand
    id: int


@dataclass(slots=True)
class _PendingCommand:
    command: Command
    sent_packet: int
    readback: _Readback | None
    deadline: float
    next_read: float
    request_id: int | None = None
    readback_packet: int | None = None
    mapped_event_id: int | None = None
    response_id: int | None = None


@dataclass(frozen=True, slots=True)
class _Readback:
    definition: int
    decoder: Struct
    expected: tuple
    description: str
    client_data_id: int | None = None

    def matches(self, values):
        for expected, actual in zip(self.expected, values, strict=True):
            if expected is None:
                continue
            if type(expected) is float:
                if not isclose(expected, actual, rel_tol=0, abs_tol=1e-6):
                    return False
            elif type(expected) is bool:
                if expected != bool(actual):
                    return False
            elif isinstance(expected, bytes) and self.client_data_id is None:
                if expected != actual.split(b"\0", 1)[0]:
                    return False
            elif expected != actual:
                return False
        return True


class SimConnectProvider:
    """Adapt an explicitly loaded SDK or an injected fake to the Core contract."""

    minimum_interval_ms = 1000

    def __init__(self, sdk: _Sdk, *, open_timeout=3.0,
                 baseline_timeout=0.5, readback_timeout=1.5, legacy_facility_additions=False):
        for timeout in (open_timeout, baseline_timeout, readback_timeout):
            if not 0 < timeout <= 15:
                raise ValueError("SimConnect timeouts must be positive and at most 15 seconds.")
        self.source = Source(
            "SimConnect adapter with fake SDK" if sdk.simulated else "MSFS 2020 SimConnect",
            simulated=sdk.simulated,
        )
        self._sdk = sdk
        self._open_timeout = open_timeout
        self._baseline_timeout = baseline_timeout
        self._readback_timeout = readback_timeout
        self._legacy_facility_additions = legacy_facility_additions
        self._handle = None
        self._session = None
        self._ready = False
        self._reset()

    def _reset(self):
        self._ids = count(1)
        self._registrations: dict[Signal, _Registration] = {}
        self._by_id: dict[int, _Registration] = {}
        self._failed: set[int] = set()
        self._sent_registrations: dict[int, tuple[int, str]] = {}
        self._simvar_definitions: set[int] = set()
        self._simvar_requests: set[int] = set()
        self._system_event_ids: dict[Signal, int] = {}
        self._disabled_events: set[int] = set()
        self._lifecycle_events: set[int] = set()
        self._state_requests: dict[int, tuple[Signal, float]] = {}
        self._updates = deque()
        self._dropped = 0
        self._event_ids: dict[str, int] = {}
        self._event_errors: dict[str, str] = {}
        self._event_map_packets: dict[int, tuple[str, int]] = {}
        self._write_definitions: dict[tuple[Signal, ...], int] = {}
        self._write_definition_packets: dict[int, int] = {}
        self._write_definition_errors: dict[int, str] = {}
        self._before_request = None
        self._before_packet = None
        self._before_definition = None
        self._before_reading = None
        self._before_error = None
        self._pending: _PendingCommand | None = None
        self._inputs: SimConnectInputEvents | None = None
        self._client_data: SimConnectClientData | None = None
        self._client_events: SimConnectClientEvents | None = None
        self._advanced: SimConnectAdvanced | None = None
        self._facilities: SimConnectFacilities | None = None

    def supports_signal(self, signal):
        if signal.hardware_input is not None:
            return signal.event and signal.value_type is ValueType.HARDWARE_INPUT
        if signal.client_data_region is not None:
            return signal.value_type is ValueType.BYTES
        if signal.input_id is not None:
            return signal.value_type in (ValueType.NUMBER, ValueType.TEXT)
        if signal.client_event:
            return signal.event and signal.value_type is ValueType.CLIENT_EVENT
        if signal.event:
            return SYSTEM_EVENTS.get(signal.name) == signal
        if signal.system_state:
            return SYSTEM_STATES.get(signal.name) == signal
        if signal.value_type not in _DATATYPES:
            return False
        if signal.local_simvar:
            return (signal.name.startswith("L:") and signal.value_type is ValueType.NUMBER
                    and signal.simvar_format is None and bool(signal.unit))
        _, separator, index = signal.name.partition(":")
        return (
            signal.name.isascii()
            and (not separator or index.isdecimal())
            and (signal.unit is None if signal.value_type is ValueType.TEXT else
                 bool(signal.unit and signal.unit.isascii()))
        )

    def supports_command(self, operation):
        return (operation in OPERATIONS or operation in ADVANCED_OPERATIONS
                or operation in (CLIENT_DATA_WRITE, SIMVAR_WRITE, CLIENT_EVENT_SEND))

    def connect(self, session_id):
        if self._handle is not None:
            raise ProviderError("A previous SimConnect handle has not been closed.")
        self._reset()
        self._session = session_id
        self._handle = self._sdk.open(b"BBC Integrated EFB")
        deadline = monotonic() + self._open_timeout
        while monotonic() < deadline:
            packet = self._next()
            if packet is None:
                sleep(0.005)
            elif packet[0] == ReceiveId.OPEN:
                self._struct(ReceiveOpen, packet[1])
                self._inputs = SimConnectInputEvents(
                    session_id, self._call, lambda: self._sdk.last_packet_id(self._handle),
                    self._new_id, self._offer, self._retain_aircraft_lifecycle,
                )
                self._client_data = SimConnectClientData(
                    session_id, self._call, lambda: self._sdk.last_packet_id(self._handle),
                    self._new_id, self._offer,
                )
                self._client_events = SimConnectClientEvents(
                    self._call, lambda: self._sdk.last_packet_id(self._handle), self._new_id,
                    self._map_event, self._reading,
                )
                self._facilities = SimConnectFacilities(
                    session_id, self._call, lambda: self._sdk.last_packet_id(self._handle),
                    self._new_id, self._offer, legacy_facility_additions=self._legacy_facility_additions,
                )
                self._advanced = SimConnectAdvanced(
                    session_id, self._call, lambda: self._sdk.last_packet_id(self._handle),
                    self._new_id, self._offer, self._reading, self._simvar_encoding,
                )
                self._ready = True
                return
            elif packet[0] == ReceiveId.QUIT:
                raise ProviderError("MSFS quit before confirming SimConnect OPEN.")
            elif packet[0] == ReceiveId.EXCEPTION:
                raise ProviderError(self._exception_detail(self._struct(ReceiveException, packet[1])))
        raise ProviderError("Timed out waiting for SimConnect OPEN.")

    def disconnect(self):
        self._ready = False
        if self._handle is not None:
            # Preserve the handle on a failed close so Core cannot reopen over it.
            self._sdk.close(self._handle)
            self._handle = None
        self._session = None
        self._reset()

    def replace_demand(self, demand):
        if not self._ready:
            return
        self._client_events.replace({
            item.signal: None for item in demand if item.signal.client_event and item.event_enabled
        })
        self._advanced.replace_inputs({
            item.signal: None for item in demand if item.signal.hardware_input and item.event_enabled
        })
        desired = {item.signal: item for item in demand if not item.signal.client_event and not item.signal.hardware_input}
        for signal, registration in tuple(self._registrations.items()):
            if signal.event and signal in desired:
                self._set_system_event_enabled(registration.id, desired[signal].event_enabled)
            elif desired.get(signal) != registration.demand:
                self._retire(registration)
        for signal, item in desired.items():
            if signal not in self._registrations:
                self._register(item)

    def request_input_catalog(self):
        if self._ready:
            self._inputs.request_catalog()

    def request_input_parameters(self, input_id):
        if self._ready:
            self._inputs.request_parameters(input_id)

    def request_input_value(self, input_id):
        if self._ready:
            self._inputs.request_value(input_id)

    def request_client_data_value(self, signal):
        if self._ready:
            self._client_data.request_value(signal)

    def request_facility(self, id, query):
        if self._ready:
            if isinstance(query, SimulatorRead) and query.kind == "dialog":
                try:
                    self._client_events.request_dialog_notifications()
                except ProviderError as error:
                    reply = FacilityReply(self._session, id, query, FacilityState.UNAVAILABLE, detail=str(error))
                else:
                    reply = FacilityReply(self._session, id, query, FacilityState.READY,
                                          detail="Request dispatched. Events arrive separately.")
                self._offer(reply)
            elif isinstance(query, SimulatorRead):
                self._advanced.request(id, query)
            else:
                self._facilities.request(id, query)

    def response_times(self):
        if not self._ready:
            raise ProviderError("A connected SimConnect session is required for diagnostics.")
        request = getattr(self._sdk, "response_times", None)
        if request is None:
            raise ProviderError("The SDK transport does not expose response timings.")
        return request(self._handle)

    def replace_facility_watches(self, watches):
        if self._ready:
            self._facilities.cache.replace(watches)

    def request_system_state(self, signal):
        if not self._ready or signal not in self._registrations:
            return
        if any(active == signal for active, _ in self._state_requests.values()):
            return
        id = self._new_id()
        try:
            self._call_registration(id, "SimConnect_RequestSystemState", id, _SYSTEM_STATE_NAMES[signal.name])
        except ProviderError as error:
            self._reading(signal, None, error=str(error))
            return
        self._state_requests[id] = (signal, monotonic() + _SYSTEM_STATE_TIMEOUT)

    def _retain_aircraft_lifecycle(self):
        for name in ("AIRCRAFTLOADED", "FLIGHTLOADED"):
            id = self._ensure_system_event(SYSTEM_EVENTS[name])
            if id in self._failed:
                raise ProviderError(f"Cannot track the required {name} lifecycle event.")
            self._lifecycle_events.add(id)
            self._set_system_event_enabled(id, True)
        return self._lifecycle_events

    def _set_system_event_enabled(self, id, enabled):
        enabled = enabled or id in self._lifecycle_events
        currently_enabled = id not in self._disabled_events
        if id not in self._failed and enabled != currently_enabled:
            self._call_registration(id, "SimConnect_SetSystemEventState", id, int(enabled))
            if enabled:
                self._disabled_events.remove(id)
            else:
                self._disabled_events.add(id)

    def _ensure_system_event(self, signal):
        id = self._system_event_ids.get(signal)
        if id is None:
            id = self._new_id()
            name = _SYSTEM_EVENT_NAMES.get(signal.name, signal.name.encode("ascii"))
            self._call_registration(id, "SimConnect_SubscribeToSystemEvent", id, name)
            self._system_event_ids[signal] = id
        return id

    def _register(self, demand):
        signal = demand.signal
        if not self.supports_signal(signal):
            raise ProviderError("Unsupported SimConnect signal.")
        if signal.event:
            try:
                id = self._ensure_system_event(signal)
            except ProviderError as error:
                self._reading(signal, None, error=str(error))
                id = self._new_id()
                self._failed.add(id)
        else:
            id = self._new_id()
        registration = _Registration(demand, id)
        if signal.input_id is not None:
            self._inputs.subscribe(signal)
        elif signal.client_data_region is not None:
            self._client_data.subscribe(signal)
        elif not (signal.event or signal.system_state):
            try:
                self._define(id, signal, registration_id=id)
                self._simvar_definitions.add(id)
                interval = (demand.interval_ms - 1) // 1000
                self._call_registration(
                    id, "SimConnect_RequestDataOnSimObject",
                    id, id, _USER_AIRCRAFT, Period.SECOND, 0, 0, interval, 0,
                )
                self._simvar_requests.add(id)
            except ProviderError as error:
                self._failed.add(id)
                self._reading(signal, None, error=str(error))
        self._registrations[signal] = registration
        self._by_id[id] = registration
        if signal.event:
            self._set_system_event_enabled(id, demand.event_enabled)
        elif signal.system_state:
            self.request_system_state(signal)

    def _retire(self, registration):
        signal, id = registration.demand.signal, registration.id
        if signal.input_id is not None:
            self._inputs.unsubscribe(signal)
        elif signal.client_data_region is not None:
            if self._pending is not None and self._pending.command.client_data_region == signal.client_data_region:
                self._complete_command(Outcome.UNCERTAIN, "The Client Data subscription ended before verification.")
            self._client_data.unsubscribe(signal)
        elif signal.event:
            if id not in self._lifecycle_events and self._system_event_ids.pop(signal, None) is not None:
                self._call("SimConnect_UnsubscribeFromSystemEvent", id)
                self._disabled_events.discard(id)
        elif signal.system_state:
            for request, (active, _) in tuple(self._state_requests.items()):
                if active == signal:
                    del self._state_requests[request]
        else:
            if id in self._simvar_requests:
                self._call(
                    "SimConnect_RequestDataOnSimObject", id, id, _USER_AIRCRAFT, Period.NEVER, 0, 0, 0, 0,
                )
                self._simvar_requests.remove(id)
            if id in self._simvar_definitions:
                self._call("SimConnect_ClearDataDefinition", id)
                self._simvar_definitions.remove(id)
        del self._registrations[signal]
        del self._by_id[id]
        self._failed.discard(id)

    def _define(self, id, signal, *, registration_id=None):
        datatype, _ = self._simvar_encoding(signal)
        args = (
            id, signal.name.encode("ascii"), signal.unit.encode("ascii") if signal.unit else None,
            datatype, 0.0, _UNUSED,
        )
        if registration_id is None:
            self._call("SimConnect_AddToDataDefinition", *args)
        else:
            self._call_registration(registration_id, "SimConnect_AddToDataDefinition", *args)

    @staticmethod
    def _simvar_encoding(signal):
        if signal.simvar_format is not None:
            return _SIMVAR_FORMATS[signal.simvar_format]
        return _DATATYPES[signal.value_type]

    def send(self, command):
        if not self._ready or command.session_id != self._session:
            raise CommandRejected("There is no matching open SimConnect session.")
        if self._pending is not None:
            raise CommandRejected("A command is already awaiting an outcome.")
        mapping = OPERATIONS.get(command.operation)
        response_id = None
        try:
            if command.operation == INPUT_WRITE and command.input_id is not None:
                self._drain_before_session_scoped_write()
                payload = self._inputs.prepare_write(command.input_id, command.arguments)
                buffer = create_string_buffer(payload, len(payload)) if payload else None
                function = "SimConnect_SetInputEvent"
                arguments = (command.input_id.hash, len(payload), buffer)
                readback = None
            elif command.operation == CLIENT_DATA_WRITE and command.client_data_region is not None:
                self._drain_before_session_scoped_write()
                function, arguments, readback = self._prepare_client_data_write(command)
            elif command.operation == SIMVAR_WRITE and command.sdk_signal is not None:
                function, arguments, readback = self._prepare_simvar_write(command.sdk_signal, command.arguments)
            elif command.operation == CLIENT_EVENT_SEND and command.sdk_event is not None:
                function, arguments, readback = self._prepare_event(command.sdk_event, command.arguments)
            elif command.operation in ADVANCED_OPERATIONS:
                operation = ADVANCED_OPERATIONS[command.operation]
                if operation.name.startswith("CREATE_"):
                    for name in ("FLIGHTLOADED", "OBJECTREMOVED"):
                        event_id = self._ensure_system_event(SYSTEM_EVENTS[name])
                        self._lifecycle_events.add(event_id)
                        self._set_system_event_enabled(event_id, True)
                function, arguments, response_id = self._advanced.prepare(command)
                readback = None
            elif mapping is not None:
                function, arguments, readback = self._prepare_command(mapping, command)
            else:
                raise CommandRejected("Unsupported SimConnect command.")
        except CommandRejected:
            raise
        except ProviderError as error:
            if self._ready:
                self._ready = False
                self._offer(Disconnect(self._session, f"SimConnect command preparation failed: {error}"))
            raise CommandRejected(str(error)) from error
        self._call(function, *arguments)
        sent_packet = self._sdk.last_packet_id(self._handle)
        now = monotonic()
        timeout = max(self._readback_timeout, 4.0) if response_id is not None else self._readback_timeout
        self._pending = _PendingCommand(
            command, sent_packet, readback, now + timeout, now,
            mapped_event_id=arguments[1] if function.startswith("SimConnect_TransmitClientEvent") else None,
            response_id=response_id,
        )

    def _prepare_client_data_write(self, command):
        if len(command.arguments) != 1:
            raise CommandRejected("A Client Data write takes one binary payload.")
        region = command.client_data_region
        value, = command.arguments
        binding, flags, payload = self._client_data.prepare_write(region, value)
        return (
            "SimConnect_SetClientData",
            (binding.area.id, binding.definition, flags, 0, len(payload),
             create_string_buffer(payload, len(payload))),
            _Readback(binding.definition, Struct(f"<{region.size}s"), (value,),
                      f"Client Data {region.area.name}", binding.area.id),
        )

    def _drain_before_session_scoped_write(self):
        # Check queued aircraft changes, setup failures and quit packets even
        # when Core's last drain stopped at its batch limit.
        for _ in range(256):
            packet = self._next()
            if packet is None:
                return
            self._process(*packet)
            if not self._ready:
                raise CommandRejected("The simulator session closed before the write.")
        raise CommandRejected("SimConnect receive backlog prevented the write.")

    def _prepare_command(self, mapping, command):
        if mapping.transport is CommandTransport.SIMVAR:
            return self._prepare_simvar_write(mapping.signal, command.arguments)
        if mapping.transport is CommandTransport.EXTENDED_EVENT:
            if (len(command.arguments) != len(mapping.capability.parameters)
                    or not 1 <= len(command.arguments) <= 5 or any(
                type(value) is not int or not 0 <= value <= _UNUSED
                for value in command.arguments
            )):
                raise CommandRejected("Supply the documented DWORD arguments for this event.")
            return self._prepare_event(mapping.operation, command.arguments)
        readback = self._prepare_radio(command)
        event_id = self._map_event(mapping.operation)
        data = command.arguments[0] if command.arguments else 0
        return (
            "SimConnect_TransmitClientEvent",
            (_USER_AIRCRAFT, event_id, data, _DEFAULT_GROUP_PRIORITY, _GROUP_IS_PRIORITY),
            readback,
        )

    def _prepare_event(self, event, arguments):
        event_id = self._map_event(event)
        if len(arguments) <= 1:
            return (
                "SimConnect_TransmitClientEvent",
                (_USER_AIRCRAFT, event_id, arguments[0] if arguments else 0,
                 _DEFAULT_GROUP_PRIORITY, _GROUP_IS_PRIORITY),
                None,
            )
        words = arguments + (0,) * (5 - len(arguments))
        return (
            "SimConnect_TransmitClientEvent_EX1",
            (_USER_AIRCRAFT, event_id, _DEFAULT_GROUP_PRIORITY, _GROUP_IS_PRIORITY, *words),
            None,
        )

    def _prepare_simvar_write(self, signal, arguments):
        if len(arguments) != 1:
            raise CommandRejected("A writable SimVar takes one value.")
        value = arguments[0]
        _, encoder = self._simvar_encoding(signal)
        try:
            if signal.value_type is ValueType.TEXT:
                value = value.encode("utf-8")
                if len(value) >= encoder.size or b"\0" in value:
                    raise CommandRejected("Text exceeds the SDK string field or contains a NUL.")
            packed = encoder.pack(value)
            if signal.value_type is ValueType.NUMBER:
                value, = encoder.unpack(packed)
                if not isfinite(value):
                    raise CommandRejected("Value cannot be represented as a finite SDK number.")
        except (StructError, OverflowError, UnicodeError) as error:
            raise CommandRejected("Value cannot be represented in the SDK field.") from error
        try:
            definition = self._write_definition((signal,))
        except ProviderError as error:
            raise CommandRejected(str(error)) from error
        data = create_string_buffer(packed, len(packed))
        return (
            "SimConnect_SetDataOnSimObject",
            (definition, _USER_AIRCRAFT, 0, 1, len(packed), data),
            _Readback(definition, encoder, (value,), signal.name),
        )

    def _map_event(self, operation):
        if operation in self._event_errors:
            raise CommandRejected(self._event_errors[operation])
        event_id = self._event_ids.get(operation)
        if event_id is None:
            event_id = self._new_id()
            try:
                self._call("SimConnect_MapClientEventToSimEvent", event_id, operation.encode("ascii"))
            except ProviderError as error:
                raise CommandRejected(str(error)) from error
            self._event_ids[operation] = event_id
            self._event_map_packets[self._sdk.last_packet_id(self._handle)] = (operation, event_id)
        return event_id

    def _write_definition(self, signals):
        definition = self._write_definitions.get(signals)
        if definition in self._write_definition_errors:
            raise CommandRejected(self._write_definition_errors[definition])
        if definition is None:
            definition = self._new_id()
            for signal in signals:
                self._define(definition, signal)
                self._write_definition_packets[self._sdk.last_packet_id(self._handle)] = definition
            self._write_definitions[signals] = definition
        return definition

    def _prepare_radio(self, command):
        if command.operation == _SET_STANDBY:
            if len(command.arguments) != 1:
                raise CommandRejected("Supply one COM1 standby frequency in Hz.")
            frequency = command.arguments[0]
            if type(frequency) is not int or not self._in_com_band(frequency):
                raise CommandRejected("COM1 requires an integer frequency in its VHF band.")
        elif command.operation != _SWAP or command.arguments:
            raise CommandRejected("Unsupported COM1 command or arguments.")
        definition = self._write_definition((COM1_ACTIVE, COM1_STANDBY))
        active, standby = self._read_radio_baseline(definition)
        if command.operation == _SET_STANDBY:
            return _Readback(definition, _RADIO_PAIR, (None, frequency), "COM1 frequencies")
        return _Readback(definition, _RADIO_PAIR, (standby, active), "COM1 frequencies")

    def _read_radio_baseline(self, definition):
        self._before_request, self._before_packet = self._request_readback(definition)
        self._before_definition = definition
        self._before_reading = None
        self._before_error = None
        deadline = monotonic() + self._baseline_timeout
        try:
            while self._ready and monotonic() < deadline:
                packet = self._next()
                if packet is None:
                    sleep(0.005)
                    continue
                self._process(*packet)
                if self._before_error is not None:
                    raise CommandRejected(self._before_error)
                if self._before_reading is not None:
                    if all(map(self._in_com_band, self._before_reading)):
                        return self._before_reading
                    break
        finally:
            self._before_request = None
            self._before_packet = None
            self._before_definition = None
            self._before_reading = None
            self._before_error = None
        raise CommandRejected("No valid fresh COM1 baseline was available for the command.")

    @staticmethod
    def _in_com_band(value):
        return 118_000_000 <= value < 137_000_000

    def _request_readback(self, definition, client_data_id=None):
        id = self._new_id()
        if client_data_id is None:
            self._call(
                "SimConnect_RequestDataOnSimObject", id, definition,
                _USER_AIRCRAFT, Period.ONCE, 0, 0, 0, 0,
            )
        else:
            self._call(
                "SimConnect_RequestClientData", client_data_id, id, definition,
                ClientDataPeriod.ONCE, 0, 0, 0, 0,
            )
        return id, self._sdk.last_packet_id(self._handle)

    def drain(self, limit):
        transport_empty = False
        if self._ready:
            for _ in range(limit):
                packet = self._next()
                if packet is None:
                    transport_empty = True
                    break
                self._process(*packet)
                if not self._ready:
                    break
        if self._ready:
            self._inputs.expire()
            self._client_data.expire()
            self._advanced.expire()
            if transport_empty:
                self._client_events.activate_after_drain()
            self._facilities.progress(transport_empty=transport_empty)
            for request, (signal, deadline) in tuple(self._state_requests.items()):
                if monotonic() >= deadline:
                    del self._state_requests[request]
                    self._reading(signal, None, error="SimConnect system state request timed out.")
            self._advance_readback()
        batch = []
        if self._dropped:
            batch.append(IngressGap(self._session, self._dropped))
            self._dropped = 0
        while self._updates and len(batch) < limit:
            batch.append(self._updates.popleft())
        return tuple(batch)

    def _advance_readback(self):
        pending = self._pending
        if pending is None:
            return
        now = monotonic()
        readback = pending.readback
        if now >= pending.deadline:
            if pending.command.operation in ADVANCED_OPERATIONS:
                self._complete_command(Outcome.UNVERIFIED, self._advanced.command_timeout(pending.command))
            elif readback is None:
                self._complete_command(
                    Outcome.UNVERIFIED, "The command was sent. This capability has no configured readback.",
                )
            else:
                self._complete_command(Outcome.UNCERTAIN, f"{readback.description} did not confirm the command.")
        elif readback is not None and pending.request_id is None and now >= pending.next_read:
            try:
                pending.request_id, pending.readback_packet = self._request_readback(
                    readback.definition, readback.client_data_id,
                )
            except ProviderError as error:
                self._complete_command(Outcome.UNCERTAIN, f"Could not request {readback.description}: {error}")

    def _process(self, kind, packet):
        if kind == ReceiveId.QUIT:
            self._ready = False
            self._offer(Disconnect(self._session, "MSFS reported SimConnect QUIT."))
        elif kind == ReceiveId.EXCEPTION:
            self._receive_exception(self._struct(ReceiveException, packet))
        elif kind in (ReceiveId.ASSIGNED_OBJECT_ID, ReceiveId.ACTION_CALLBACK, ReceiveId.RESERVED_KEY):
            reply = self._advanced.command_reply(self._pending, kind, packet)
            if reply is not None:
                self._complete_command(*reply)
        elif kind == ReceiveId.CONTROLLERS_LIST:
            self._advanced.receive_controllers(packet)
        elif kind == ReceiveId.EVENT and self._advanced.receive_input(packet):
            return
        elif kind in (ReceiveId.EVENT, ReceiveId.EVENT_EX1) and self._client_events.receive(kind, packet):
            return
        elif kind in (ReceiveId.SIMOBJECT_DATA, ReceiveId.SIMOBJECT_DATA_BYTYPE) and self._advanced.receive_object(kind, packet):
            return
        elif kind == ReceiveId.SIMOBJECT_DATA:
            self._receive_data(packet)
        elif kind == ReceiveId.CLIENT_DATA:
            prefix = self._struct(ReceiveSimObjectDataPrefix, packet)
            if not self._receive_readback(prefix, packet, client_data=True):
                self._client_data.process(prefix, packet)
        elif kind == ReceiveId.SYSTEM_STATE:
            self._receive_system_state(packet)
        elif kind in SimConnectFacilities.RECEIVE_IDS:
            self._facilities.receive(kind, packet)
        elif kind in (ReceiveId.EVENT, ReceiveId.EVENT_FILENAME, ReceiveId.EVENT_FRAME,
                      ReceiveId.EVENT_OBJECT, ReceiveId.EVENT_WEATHER_MODE, ReceiveId.CUSTOM_ACTION):
            self._receive_system_event(kind, packet)
        else:
            self._inputs.process(kind, packet)

    def _receive_exception(self, error):
        detail = self._exception_detail(error)
        pending = self._pending
        if pending is not None and error.send_id == pending.sent_packet:
            self._complete_command(Outcome.REJECTED, detail)
            return
        mapped_event = self._event_map_packets.pop(error.send_id, None)
        if mapped_event is not None:
            event, event_id = mapped_event
            self._event_errors[event] = detail
            self._client_events.mapping_failed(event_id, detail)
            if pending is not None and pending.mapped_event_id == event_id:
                self._complete_command(Outcome.REJECTED, detail)
            return
        definition = self._write_definition_packets.get(error.send_id)
        if definition is not None:
            self._write_definition_errors[definition] = detail
            if definition == self._before_definition:
                self._before_error = detail
            if pending is not None and pending.readback is not None and definition == pending.readback.definition:
                self._complete_command(Outcome.UNCERTAIN, detail)
            return
        if error.send_id == self._before_packet:
            self._before_error = detail
            return
        if pending is not None and error.send_id == pending.readback_packet:
            self._complete_command(Outcome.UNCERTAIN, detail)
            return
        if self._inputs.receive_exception(error.send_id, detail):
            return
        if self._client_data.receive_exception(error.send_id, detail):
            return
        if self._client_events.receive_exception(error.send_id, detail):
            return
        if self._advanced.receive_exception(error.send_id, detail):
            return
        if self._facilities.receive_exception(error.send_id, detail):
            return
        id, operation = self._sent_registrations.get(error.send_id, (None, None))
        if id in self._lifecycle_events:
            raise ProviderError(detail)
        state = self._state_requests.pop(id, None)
        if state is not None:
            self._reading(state[0], None, error=detail)
            return
        registration = self._by_id.get(id)
        if id is not None and registration is None:
            return
        if registration is not None:
            self._failed.add(id)
            signal = registration.demand.signal
            if operation == "SimConnect_SubscribeToSystemEvent":
                self._system_event_ids.pop(signal, None)
                self._disabled_events.discard(id)
            elif operation == "SimConnect_AddToDataDefinition":
                self._simvar_definitions.discard(id)
                self._simvar_requests.discard(id)
            elif operation == "SimConnect_RequestDataOnSimObject":
                self._simvar_requests.discard(id)
            self._reading(signal, None, error=detail)
            return
        raise ProviderError(detail)

    def _receive_system_event(self, kind, packet):
        event = self._struct(ReceiveEvent, packet)
        if event.event_id == self._system_event_ids.get(SYSTEM_EVENTS["FLIGHTLOADED"]):
            self._advanced.flight_changed()
        if event.event_id == self._system_event_ids.get(SYSTEM_EVENTS["OBJECTREMOVED"]):
            self._advanced.owned_object_removed(event.data)
        if self._inputs.aircraft_changed(event.event_id):
            if self._pending is not None and self._pending.command.input_id is not None:
                self._complete_command(Outcome.UNCERTAIN, "Aircraft or flight changed after dispatch.")
        registration = self._by_id.get(event.event_id)
        if (registration is None or event.event_id in self._failed or event.event_id in self._disabled_events
                or not registration.demand.signal.event):
            return
        signal = registration.demand.signal
        try:
            value = self._system_event_value(signal, kind, event, packet)
        except ProviderError as error:
            self._reading(signal, None, error=str(error))
        else:
            self._reading(signal, value)

    def _system_event_value(self, signal, kind, event, packet):
        if signal.value_type is ValueType.TEXT and kind == ReceiveId.EVENT_FILENAME:
            return self._struct(ReceiveEventFilename, packet).filename.decode("utf-8", errors="replace")
        if signal.value_type is ValueType.FRAME and kind == ReceiveId.EVENT_FRAME:
            frame = self._struct(ReceiveEventFrame, packet)
            return FrameSample(frame.frame_rate, frame.simulation_rate)
        if signal.value_type is ValueType.OBJECT and kind == ReceiveId.EVENT_OBJECT:
            change = self._struct(ReceiveEventObject, packet)
            return SimObjectChange(event.data, change.object_type)
        if signal.value_type is ValueType.MISSION_ACTION and kind == ReceiveId.CUSTOM_ACTION:
            action = self._struct(ReceiveCustomActionPrefix, packet)
            payload, terminator, _ = packet[sizeof(ReceiveCustomActionPrefix):].partition(b"\0")
            if not terminator:
                raise ProviderError("Missing terminator in SimConnect mission action payload.")
            return MissionAction(UUID(bytes_le=bytes(action.instance_id)), bool(action.wait_for_completion),
                                 payload.decode("utf-8", errors="replace"))
        if signal.name == "WEATHERMODECHANGED" and kind == ReceiveId.EVENT_WEATHER_MODE:
            return event.data
        if kind == ReceiveId.EVENT and signal.value_type in (ValueType.INTEGER, ValueType.BOOLEAN):
            if signal.value_type is ValueType.BOOLEAN and event.data not in (0, 1):
                raise ProviderError("Invalid SimConnect Boolean event state.")
            return event.data
        raise ProviderError(f"Unexpected SimConnect packet type for {signal.name}.")

    def _receive_system_state(self, packet):
        prefix = self._struct(ReceiveRequest, packet)
        request = self._state_requests.pop(prefix.request_id, None)
        if request is None:
            return
        signal, _ = request
        try:
            state = self._struct(ReceiveSystemState, packet)
        except ProviderError as error:
            self._reading(signal, None, error=str(error))
            return
        if signal.value_type is ValueType.TEXT:
            self._reading(signal, state.text.decode("utf-8", errors="replace"))
        elif state.integer in (0, 1):
            self._reading(signal, bool(state.integer))
        else:
            self._reading(signal, None, error="Invalid SimConnect Boolean system state.")

    @staticmethod
    def _exception_detail(error):
        return f"SimConnect exception {error.exception} for packet {error.send_id}, index {error.index}."

    def _receive_data(self, packet):
        prefix = self._struct(ReceiveSimObjectDataPrefix, packet)
        request = prefix.request_id
        if request == self._before_request:
            try:
                self._before_reading = self._payload(prefix, packet, self._before_definition, 2, _RADIO_PAIR)
            except ProviderError as error:
                self._before_error = str(error)
            return
        if self._receive_readback(prefix, packet):
            return
        registration = self._by_id.get(request)
        if registration is None or request in self._failed or registration.demand.signal.event:
            return
        signal = registration.demand.signal
        _, decoder = self._simvar_encoding(signal)
        value, = self._payload(prefix, packet, registration.id, 1, decoder)
        if isinstance(value, bytes):
            value = value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        self._reading(signal, value)

    def _receive_readback(self, prefix, packet, *, client_data=False):
        pending = self._pending
        if pending is None or prefix.request_id != pending.request_id:
            return False
        readback = pending.readback
        if client_data != (readback.client_data_id is not None):
            self._complete_command(Outcome.UNCERTAIN, "Unexpected SimConnect readback packet type.")
            return True
        try:
            if client_data:
                values = (self._client_data.payload(prefix, packet, readback.definition, readback.decoder.size),)
            else:
                values = self._payload(prefix, packet, readback.definition, len(readback.expected), readback.decoder)
        except ProviderError as error:
            self._complete_command(Outcome.UNCERTAIN, str(error))
            return True
        if readback.matches(values):
            self._complete_command(Outcome.CONFIRMED, f"Observed the expected {readback.description}.")
        else:
            pending.request_id = None
            pending.next_read = monotonic() + 0.1
        return True

    @staticmethod
    def _payload(prefix, packet, definition, count, decoder):
        offset = sizeof(ReceiveSimObjectDataPrefix)
        if (prefix.definition_id != definition or prefix.definition_count != count
                or prefix.flags or len(packet) < offset + decoder.size):
            raise ProviderError("Invalid SimConnect data definition or payload.")
        return decoder.unpack_from(packet, offset)

    def _complete_command(self, outcome, detail, data=None):
        command = self._pending.command
        self._pending = None
        self._offer(CommandOutcome(self._session, command.id, outcome, detail, data))

    def _reading(self, signal, value, *, error=None):
        self._offer(Reading(
            self._session, signal, value, datetime.now(timezone.utc), monotonic(), error=error,
        ))

    def _offer(self, update):
        if len(self._updates) >= 512:
            self._updates.popleft()
            self._dropped += 1
        self._updates.append(update)

    def _new_id(self):
        id = next(self._ids)
        if id >= _UNUSED:
            raise ProviderError("SimConnect request ID space exhausted.")
        return id

    def _call(self, name, *args):
        self._sdk.call(name, self._handle, *args)

    def _call_registration(self, id, name, *args):
        self._call(name, *args)
        self._sent_registrations[self._sdk.last_packet_id(self._handle)] = (id, name)
        if len(self._sent_registrations) > 512:
            del self._sent_registrations[next(iter(self._sent_registrations))]

    def _next(self):
        packet = self._sdk.next_packet(self._handle)
        if packet is None:
            return None
        header = self._struct(Receive, packet)
        if not sizeof(Receive) <= header.size <= len(packet):
            raise ProviderError("Invalid SimConnect packet byte count.")
        if header.id == 0:
            return None
        return header.id, packet[:header.size]

    @staticmethod
    def _struct(cls, packet):
        if len(packet) < sizeof(cls):
            raise ProviderError(f"Truncated SimConnect {cls.__name__} packet.")
        return cls.from_buffer_copy(packet)
