"""First direct MSFS 2020 provider for Core. All SDK calls have one serial owner.

Failure scope
-------------
One shared SimConnect session serves every module, so the provider keeps a
failure local whenever it can be attributed:

* QUIT, a broken transport (an HRESULT failure, an undecodable packet header or
  exception packet) and a rejected required lifecycle registration end the
  session. Everything else is local.
* Every SDK call is recorded in an age-pruned packet ledger. An exception names
  its packet, so it is routed to the owner (a signal, a command, a facility
  request, a helper). The owner degrades: an error Reading, an UNAVAILABLE
  facility reply, a rejected or uncertain command. A late exception for a packet
  whose owner has finished, or one nobody claims, is logged and ignored.
* Failed registrations are retried with capped exponential backoff.
* Write safety stays fail-closed: a command whose confirmation cannot be
  established is reported UNCERTAIN, which Core uses to block further writes.

Update queue
------------
Control updates (command outcomes, facility and catalog replies, error
readings, discrete system, client and hardware-input events, Disconnect) are
never evicted. Telemetry readings share a large
bounded buffer whose overflow is reported as an IngressGap. ``drain(limit)``
stops reading SimConnect once ``limit`` updates are waiting, so backlog stays in
SimConnect's own queue instead of being dropped here.

Snapshots
---------
One multi-datum definition and request serve each SnapshotSpec. Without
``changed_only`` the simulator clocks (SIMULATION TIME, ABSOLUTE TIME, SIMULATION
RATE) are appended and ``sample_time`` is ABSOLUTE TIME via
``contract.simulator_utc``. With ``changed_only`` the definition holds only the
requested fields and uses SIMCONNECT_DATA_REQUEST_FLAG_CHANGED, so SimConnect
really sends only when a field changed; such frames report no clocks (None).
Fields SimConnect refuses are excluded and remembered for the life of the demand,
across rebuilds and backoff retries.

COM1
----
Frequencies are read as FLOAT64 Hz and rounded to integer Hz (contract C6). A set
command reads COM SPACING MODE:1 with its baseline when the simulator provides it,
and refuses a channel the current spacing cannot tune before anything is sent.
"""

from __future__ import annotations

from collections import deque
from ctypes import create_string_buffer, sizeof
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
import logging
from math import isclose, isfinite
from struct import Struct, error as StructError
from time import monotonic, perf_counter, sleep
from typing import Protocol, runtime_checkable
from uuid import UUID

from .aircraft_inputs import INPUT_WRITE
# com_channel_problem: one COM channel rule (contract C6), shared with Core and SimulatedProvider,
# so all three give the same rejection detail.
from .catalog import (
    FREQUENCY_HZ_SIGNALS, OPERATIONS, SYSTEM_EVENTS, SYSTEM_STATES, CommandTransport, com_channel_problem,
)
from .client_data import CLIENT_DATA_WRITE
from .contract import FrameSample, MissionAction, SimObjectChange, SnapshotPeriod, Source, ValueType, simulator_utc
from .facilities import FacilityState
from .provider import (
    Command, CommandOutcome, CommandRejected, Demand, Disconnect, IngressGap,
    FacilityReply, Outcome, ProviderError, Reading, Signal, SnapshotFrame,
)
from .sdk_simvar import CLIENT_EVENT_SEND, SIMVAR_WRITE, SimVarFormat
from .simconnect_abi import (
    DATA_REQUEST_FLAG_CHANGED, ClientDataPeriod, DataType, Period, Receive, ReceiveCustomActionPrefix,
    ReceiveEvent, ReceiveEventFilename, ReceiveEventFrame, ReceiveEventObject, ReceiveException, ReceiveId,
    ReceiveOpen, ReceiveRequest, ReceiveSimObjectDataPrefix, ReceiveSystemState,
)
from .simconnect_client_data import SimConnectClientData
from .simconnect_advanced import OPERATIONS as ADVANCED_OPERATIONS, OPERATION_EXPORTS, SimConnectAdvanced
from .simconnect_client_events import SimConnectClientEvents
from .simconnect_facilities import FACILITY_WORST_CASE_SECONDS, SimConnectFacilities
from .simconnect_input_events import SimConnectInputEvents
from .simconnect_ledger import PacketLedger
from .simulator_ops import SimulatorRead


_LOG = logging.getLogger(__name__)
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
# Canonical SDK spelling for every documented system event. Signal names are
# upper-cased for matching, but the SDK documents these names in mixed case.
_SYSTEM_EVENT_NAMES = {name.upper(): name.encode("ascii") for name in (
    "1sec", "4sec", "6Hz", "AircraftLoaded", "Crashed", "CrashReset", "CustomMissionActionExecuted",
    "FlightLoaded", "FlightPlanActivated", "FlightPlanDeactivated", "FlightSaved", "Frame",
    "ObjectAdded", "ObjectRemoved", "Pause", "Pause_EX1", "Paused", "PauseFrame", "PositionChanged",
    "Sim", "SimStart", "SimStop", "Sound", "Unpaused", "View", "WeatherModeChanged",
)}
_SYSTEM_STATE_TIMEOUT = 3.0
_RESPONSE_TIMEOUT = 4.0  # Commands that wait for a simulator reply (create, reserve key, ...).
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
# COM frequencies are read as FLOAT64 Hz and rounded to the nearest integer Hz
# when decoded (contract C6), never truncated through INT32. MSFS may hold the
# value as float32 internally (an 8 Hz step near 136 MHz), so readback compares
# within _COM_RADIO_INT_TOL, which stays far below the 5 kHz channel-name grid.
_COM1_PAIR = (
    Signal("COM ACTIVE FREQUENCY:1", "Hz", ValueType.NUMBER),
    Signal("COM STANDBY FREQUENCY:1", "Hz", ValueType.NUMBER),
)
_FREQUENCY_ENCODING = (DataType.FLOAT64, Struct("<d"))
_COM1_SPACING = (Signal("COM SPACING MODE:1", "enum", ValueType.INTEGER),)
_RADIO_PAIR = Struct("<dd")
_SPACING = Struct("<i")
# A value MSFS stores as float32 (or converts through float32) reads back within
# about one float32 step. Two steps of relative tolerance confirm such a write
# while a different value is still never confirmed.
_FLOAT_READBACK_REL_TOL = 2.0 ** -22
_FLOAT_READBACK_ABS_TOL = 1e-6
_COM_BAND = (118_000_000, 137_000_000)
_COM_RADIO_INT_TOL = 100  # Hz, see _COM1_PAIR.
_SPACING_PENDING = object()
_SNAPSHOT_PERIODS = {
    SnapshotPeriod.SECOND: Period.SECOND,
    SnapshotPeriod.SIM_FRAME: Period.SIM_FRAME,
    SnapshotPeriod.VISUAL_FRAME: Period.VISUAL_FRAME,
}
# Clock fields appended after the requested fields of a snapshot definition. A
# changed_only snapshot leaves them out: SIMULATION TIME and ABSOLUTE TIME change
# on every frame, so with them in the definition SIMCONNECT_DATA_REQUEST_FLAG_CHANGED
# would send the snapshot every period. The TAGGED flag does not help either,
# because SimConnect still sends a (clock-only) packet whenever any datum changed.
# A changed_only frame therefore reports the clocks and sample_time as None, and
# consumers use the host receipt time.
_SNAPSHOT_CLOCKS = (
    Signal("SIMULATION TIME", "seconds", ValueType.NUMBER),
    Signal("ABSOLUTE TIME", "seconds", ValueType.NUMBER),
    Signal("SIMULATION RATE", "number", ValueType.NUMBER),
)
_MAX_SNAPSHOT_FIELDS = 512
_DATA_CAP = 8192
_RETRY_BASE_S = 2.0
_RETRY_MAX_S = 60.0
_BASELINE_PACKET_BUDGET = 256
_DRAIN_TIME_BUDGET_S = 0.02
_DRAIN_MIN_PACKETS = 64
_RESERVED_KEY_PREFIX = "RESERVED_KEY_"


class _Sdk(Protocol):
    simulated: bool

    def open(self, application: bytes): ...
    def close(self, handle) -> None: ...
    def next_packet(self, handle) -> bytes | None: ...
    def call(self, function: str, *args) -> None: ...
    def last_packet_id(self, handle) -> int: ...


@runtime_checkable
class _OptionalSdk(Protocol):
    """Optional SDK features the provider looks up with getattr.

    A transport may omit any of them: without ``has`` every export is assumed
    present, without ``wait``/``event_handle`` the provider sleeps, without
    ``wake`` waking is a no-op, without ``take_dropped`` no overflow count is
    reported, and without ``response_times`` diagnostics are refused.
    ``SimConnectSdk`` implements all of them (tests/test_adapter_round4.py).
    """

    @property
    def event_handle(self) -> object | None: ...
    def has(self, name: str) -> bool: ...
    def wait(self, timeout: float) -> bool: ...
    def wake(self) -> None: ...
    def take_dropped(self) -> int: ...
    def response_times(self, handle) -> object: ...


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
    observed: tuple | None = None


@dataclass(frozen=True, slots=True)
class _Readback:
    definition: int
    decoder: Struct
    expected: tuple
    description: str
    client_data_id: int | None = None
    rel_tol: float = 0.0
    int_tol: int = 0
    round_hz: bool = False   # FLOAT64 Hz values rounded to integer Hz before comparing.

    def matches(self, values):
        for expected, actual in zip(self.expected, values, strict=True):
            if expected is None:
                continue
            if isinstance(expected, frozenset):
                if not any(abs(actual - option) <= self.int_tol for option in expected):
                    return False
            elif type(expected) is float:
                if not isclose(expected, actual, rel_tol=self.rel_tol, abs_tol=_FLOAT_READBACK_ABS_TOL):
                    return False
            elif type(expected) is bool:
                if expected != bool(actual):
                    return False
            elif isinstance(expected, bytes) and self.client_data_id is None:
                if expected != actual.split(b"\0", 1)[0]:
                    return False
            elif type(expected) is int and self.int_tol:
                if abs(expected - actual) > self.int_tol:
                    return False
            elif expected != actual:
                return False
        return True


@dataclass(slots=True)
class _SnapshotState:
    """One multi-datum definition and request serving one SnapshotSpec."""

    signal: Signal
    registration_id: int
    definition: int
    request: int
    positions: tuple[int, ...]        # Positions (fields then clocks) present in the definition.
    decoder: Struct
    flags: int
    excluded: set[int] = field(default_factory=set)
    requested: bool = False


def _round_hz(values):
    """Round FLOAT64 Hz readings to integer Hz. A non-finite value is invalid."""
    if not all(isfinite(value) for value in values):
        raise ProviderError("SimConnect returned a non-finite COM frequency.")
    return tuple(round(value) for value in values)


def _text_value(value):
    return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def _com_readback_options(frequency, baseline):
    """Values that confirm a COM SET_HZ request, allowing MSFS channel snapping.

    MSFS may store the request snapped to the 25 kHz grid, the 8.33 kHz carrier
    grid or the 5 kHz 8.33 channel-name grid. The exact request always confirms.
    A snapped value equal to the previous standby frequency would not show that
    anything changed, so it is left out and only the exact value confirms.
    """
    low = _COM_BAND[0]
    offset = frequency - low
    options = {
        frequency,
        low + round(offset / 25_000) * 25_000,
        low + round(round(offset * 3 / 25_000) * 25_000 / 3),
        low + round(offset / 5_000) * 5_000,
    }
    return frozenset(
        value for value in options
        if value == frequency or (_COM_BAND[0] <= value < _COM_BAND[1] and abs(value - baseline) > _COM_RADIO_INT_TOL)
    )


class SimConnectProvider:
    """Adapt an explicitly loaded SDK or an injected fake to the Core contract."""

    minimum_interval_ms = 1000
    # Native SimConnect periods for value subscriptions are whole seconds. Core
    # should round a requested interval up to this granularity when it reports
    # the accepted interval to the consumer.
    interval_granularity_ms = 1000

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
        # Core sizes its own backstop deadlines from these (see bbc_core/provider.py).
        # A sent command reports its outcome within max_command_timeout_seconds; a
        # facility request is answered or failed within facility_timeout_seconds.
        self.max_command_timeout_seconds = max(readback_timeout, _RESPONSE_TIMEOUT)
        self.facility_timeout_seconds = FACILITY_WORST_CASE_SECONDS
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
        self._retry_attempts: dict[Signal, int] = {}
        self._retry_due: dict[Signal, float] = {}
        self._ledger = PacketLedger()
        self._last_sent = None
        self._sent_registrations = PacketLedger()
        self._simvar_definitions: set[int] = set()
        self._simvar_requests: set[int] = set()
        self._snapshots: dict[Signal, _SnapshotState] = {}
        self._snapshot_requests: dict[int, _SnapshotState] = {}
        self._snapshot_field_packets = PacketLedger()
        # Field positions SimConnect refused, kept per snapshot signal across rebuilds
        # and backoff retries until the demand ends, so a refused field is not re-added.
        self._snapshot_exclusions: dict[Signal, set[int]] = {}
        self._snapshot_rebuilds: set[Signal] = set()
        self._system_event_ids: dict[Signal, int] = {}
        self._disabled_events: set[int] = set()
        self._lifecycle_events: set[int] = set()
        self._state_requests: dict[int, tuple[Signal, float]] = {}
        self._control: deque = deque()
        self._data: deque = deque()
        self._sequence = count()
        self._dropped = 0
        self._event_ids: dict[str, int] = {}
        self._event_errors: dict[str, str] = {}
        self._event_map_packets = PacketLedger()
        self._write_definitions: dict[tuple[Signal, ...], int] = {}
        self._write_definition_packets = PacketLedger()
        self._write_definition_errors: dict[int, str] = {}
        self._before_request = None
        self._before_packet = None
        self._before_definition = None
        self._before_reading = None
        self._before_error = None
        self._spacing_definition = None
        self._spacing_request = None
        self._spacing_packet = None
        self._spacing_value = _SPACING_PENDING
        self._pending: _PendingCommand | None = None
        self._inputs: SimConnectInputEvents | None = None
        self._client_data: SimConnectClientData | None = None
        self._client_events: SimConnectClientEvents | None = None
        self._advanced: SimConnectAdvanced | None = None
        self._facilities: SimConnectFacilities | None = None

    # Capability support -------------------------------------------------------

    def _has(self, *exports):
        has = getattr(self._sdk, "has", None)
        return has is None or all(has(name) for name in exports)

    def supports_signal(self, signal):
        if signal.snapshot is not None or signal.value_type is ValueType.SNAPSHOT:
            return self._supports_snapshot(signal)
        if signal.hardware_input is not None:
            return (signal.event and signal.value_type is ValueType.HARDWARE_INPUT
                    and self._has("SimConnect_MapInputEventToClientEvent_EX1"))
        if signal.client_data_region is not None:
            return signal.value_type is ValueType.BYTES and self._has("SimConnect_RequestClientData")
        if signal.input_id is not None:
            return (signal.value_type in (ValueType.NUMBER, ValueType.TEXT)
                    and self._has("SimConnect_SubscribeInputEvent", "SimConnect_GetInputEvent"))
        if signal.client_event:
            return (signal.event and signal.value_type is ValueType.CLIENT_EVENT
                    and self._has("SimConnect_AddClientEventToNotificationGroup"))
        if signal.event:
            return SYSTEM_EVENTS.get(signal.name) == signal
        if signal.system_state:
            return SYSTEM_STATES.get(signal.name) == signal
        return self._supports_simvar(signal)

    @staticmethod
    def _supports_simvar(signal):
        if (signal.value_type not in _DATATYPES or signal.event or signal.system_state
                or signal.input_id is not None or signal.client_data_region is not None
                or signal.client_event or signal.hardware_input is not None or signal.snapshot is not None):
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

    def _supports_snapshot(self, signal):
        spec = signal.snapshot
        return (
            spec is not None and signal.value_type is ValueType.SNAPSHOT
            and isinstance(spec.fields, tuple) and 1 <= len(spec.fields) <= _MAX_SNAPSHOT_FIELDS
            and all(isinstance(item, Signal) and self._supports_simvar(item) for item in spec.fields)
            and spec.period in _SNAPSHOT_PERIODS
            and type(spec.every) is int and 1 <= spec.every <= 0xFFFFFFFF
            and isinstance(spec.changed_only, bool)
        )

    def supports_command(self, operation):
        if operation in ADVANCED_OPERATIONS:
            return self._has(*OPERATION_EXPORTS.get(ADVANCED_OPERATIONS[operation], ()))
        if operation == CLIENT_DATA_WRITE:
            return self._has("SimConnect_SetClientData", "SimConnect_RequestClientData")
        if operation == INPUT_WRITE:
            return self._has("SimConnect_SetInputEvent")
        if operation == SIMVAR_WRITE:
            return self._has("SimConnect_SetDataOnSimObject")
        if operation == CLIENT_EVENT_SEND:
            return self._has("SimConnect_MapClientEventToSimEvent", "SimConnect_TransmitClientEvent")
        mapping = OPERATIONS.get(operation)
        if mapping is None:
            return False
        if mapping.transport is CommandTransport.SIMVAR:
            return self._has("SimConnect_SetDataOnSimObject")
        if mapping.transport is CommandTransport.EXTENDED_EVENT:
            return self._has("SimConnect_MapClientEventToSimEvent", "SimConnect_TransmitClientEvent_EX1")
        return self._has("SimConnect_MapClientEventToSimEvent", "SimConnect_TransmitClientEvent")

    # Session ------------------------------------------------------------------

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
                self._idle(0.005)
            elif packet[0] == ReceiveId.OPEN:
                self._struct(ReceiveOpen, packet[1])
                last_packet = lambda: self._last_sent  # noqa: E731 - cached by _call
                self._inputs = SimConnectInputEvents(
                    session_id, self._call, last_packet, self._new_id, self._offer, self._retain_aircraft_lifecycle,
                )
                self._client_data = SimConnectClientData(
                    session_id, self._call, last_packet, self._new_id, self._offer,
                )
                self._client_events = SimConnectClientEvents(
                    self._call, last_packet, self._new_id, self._map_event, self._reading,
                )
                self._facilities = SimConnectFacilities(
                    session_id, self._call, last_packet, self._new_id, self._offer,
                    legacy_facility_additions=self._legacy_facility_additions,
                )
                self._advanced = SimConnectAdvanced(
                    session_id, self._call, last_packet, self._new_id, self._offer, self._reading,
                    self._simvar_encoding, private_event=self._client_events.register_private,
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

    def wait(self, timeout):
        """Block a host worker until SimConnect signals data or ``timeout`` passes.

        Uses the SDK's Win32 wake event when it has one (SimConnectSdk with
        ``wake_event=True``); otherwise sleeps. Safe to call without Core's
        provider lock because it never touches the SimConnect handle.
        """
        waiter = getattr(self._sdk, "wait", None)
        if waiter is None:
            sleep(max(0.0, timeout))
            return False
        return waiter(timeout)

    def wake(self):
        """Wake a worker blocked in ``wait()``, for example after queuing a command.

        Signals the SDK's wake event when there is one; otherwise does nothing.
        Like ``wait()``, it never touches the SimConnect handle.
        """
        waker = getattr(self._sdk, "wake", None)
        if waker is not None:
            waker()

    def _idle(self, seconds):
        if getattr(self._sdk, "event_handle", None) is not None:
            self.wait(seconds)  # Optional SDK member: wait() falls back to sleep without it.
        else:
            sleep(seconds)

    # Demand -------------------------------------------------------------------

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
                # Keep the current Demand so a backoff retry of a failed
                # subscription re-applies the latest event_enabled, not a stale one.
                kept = _Registration(desired[signal], registration.id)
                self._registrations[signal] = kept
                if self._by_id.get(registration.id) is registration:
                    self._by_id[registration.id] = kept
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
        # Returns its own frozenset of only the aircraft and flight ids. The shared
        # _lifecycle_events set also gains OBJECTREMOVED for advanced objects, and an
        # unrelated AI object leaving the world is not an aircraft change.
        ids = []
        for name in ("AIRCRAFTLOADED", "FLIGHTLOADED"):
            id = self._ensure_system_event(SYSTEM_EVENTS[name])
            if id in self._failed:
                raise ProviderError(f"Cannot track the required {name} lifecycle event.")
            self._lifecycle_events.add(id)
            self._set_system_event_enabled(id, True)
            ids.append(id)
        return frozenset(ids)

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
        failed_now = False
        if signal.event:
            try:
                id = self._ensure_system_event(signal)
            except ProviderError as error:
                self._reading(signal, None, error=str(error))
                id = self._new_id()
                failed_now = True
        else:
            id = self._new_id()
        registration = _Registration(demand, id)
        self._registrations[signal] = registration
        self._by_id[id] = registration
        if signal.input_id is not None:
            self._inputs.subscribe(signal)
        elif signal.client_data_region is not None:
            self._client_data.subscribe(signal)
        elif signal.snapshot is not None:
            try:
                self._define_snapshot(registration, self._snapshot_exclusions.get(signal, set()))
            except ProviderError as error:
                self._forget_total_refusal(signal)
                failed_now = True
                self._reading(signal, None, error=str(error))
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
                failed_now = True
                self._reading(signal, None, error=str(error))
        if failed_now:
            self._mark_failed(registration)
        if signal.event:
            self._set_system_event_enabled(id, demand.event_enabled)
        elif signal.system_state:
            self.request_system_state(signal)

    def _mark_failed(self, registration):
        self._failed.add(registration.id)
        signal = registration.demand.signal
        if registration.id in self._lifecycle_events:
            return
        attempt = self._retry_attempts.get(signal, 0)
        self._retry_attempts[signal] = attempt + 1
        self._retry_due[signal] = monotonic() + min(_RETRY_MAX_S, _RETRY_BASE_S * 2 ** min(attempt, 6))

    def _retry_failed_registrations(self):
        now = monotonic()
        for signal, due in tuple(self._retry_due.items()):
            registration = self._registrations.get(signal)
            if registration is None or registration.id not in self._failed:
                del self._retry_due[signal]
                continue
            if now < due:
                continue
            del self._retry_due[signal]
            _LOG.info("Retrying the failed SimConnect registration for %s.", signal.name)
            self._retire(registration, retrying=True)
            self._register(registration.demand)
            if not self._ready:
                return

    def _retry_now(self):
        now = monotonic()
        for signal in self._retry_due:
            self._retry_due[signal] = now

    def _retire(self, registration, *, retrying=False):
        signal, id = registration.demand.signal, registration.id
        if not retrying:
            self._retry_attempts.pop(signal, None)
            self._retry_due.pop(signal, None)
            self._snapshot_exclusions.pop(signal, None)
            self._snapshot_rebuilds.discard(signal)
        if signal.input_id is not None:
            self._inputs.unsubscribe(signal)
        elif signal.client_data_region is not None:
            if self._pending is not None and self._pending.command.client_data_region == signal.client_data_region:
                self._complete_command(Outcome.UNCERTAIN, "The Client Data subscription ended before verification.")
            self._client_data.unsubscribe(signal)
        elif signal.snapshot is not None:
            state = self._snapshots.pop(signal, None)
            if state is not None:
                self._clear_snapshot(state)
        elif signal.event:
            # Only tear down the subscription this registration owns. The mapping may
            # instead hold a lifecycle id retained after this registration failed;
            # that subscription stays live and mapped, and _register reuses it.
            if id not in self._lifecycle_events and self._system_event_ids.get(signal) == id:
                del self._system_event_ids[signal]
                self._teardown("SimConnect_UnsubscribeFromSystemEvent", id)
            if id not in self._lifecycle_events:
                self._disabled_events.discard(id)
        elif signal.system_state:
            for request, (active, _) in tuple(self._state_requests.items()):
                if active == signal:
                    del self._state_requests[request]
        else:
            if id in self._simvar_requests:
                self._simvar_requests.remove(id)
                self._teardown(
                    "SimConnect_RequestDataOnSimObject", id, id, _USER_AIRCRAFT, Period.NEVER, 0, 0, 0, 0,
                )
            if id in self._simvar_definitions:
                self._simvar_definitions.remove(id)
                self._teardown("SimConnect_ClearDataDefinition", id)
        del self._registrations[signal]
        del self._by_id[id]
        self._failed.discard(id)

    def _teardown(self, name, *args):
        """Send a cleanup call. A failure, now or as a late exception, is only logged."""
        try:
            packet = self._call(name, *args)
        except ProviderError as error:
            _LOG.warning("SimConnect cleanup call %s failed: %s", name, error)
        else:
            self._ledger[packet] = f"cleanup {name}"

    def _define(self, id, signal, *, registration_id=None, datum_id=_UNUSED):
        datatype, _ = self._simvar_encoding(signal)
        args = (
            id, signal.name.encode("ascii"), signal.unit.encode("ascii") if signal.unit else None,
            datatype, 0.0, datum_id,
        )
        if registration_id is None:
            return self._call("SimConnect_AddToDataDefinition", *args)
        return self._call_registration(registration_id, "SimConnect_AddToDataDefinition", *args)

    @staticmethod
    def _simvar_encoding(signal):
        if signal.simvar_format is not None:
            return _SIMVAR_FORMATS[signal.simvar_format]
        if signal in FREQUENCY_HZ_SIGNALS:
            # COM frequencies are read as FLOAT64 Hz and rounded to whole Hz on
            # receipt (contract C6); INT32 would truncate e.g. 128009999.99.
            return _FREQUENCY_ENCODING
        return _DATATYPES[signal.value_type]

    # Snapshots ----------------------------------------------------------------

    def _snapshot_field(self, spec, position):
        return spec.fields[position] if position < len(spec.fields) else _SNAPSHOT_CLOCKS[position - len(spec.fields)]

    @staticmethod
    def _snapshot_clocks(spec):
        """Clock fields of a snapshot definition (none for changed_only, see _SNAPSHOT_CLOCKS)."""
        return () if spec.changed_only else _SNAPSHOT_CLOCKS

    def _define_snapshot(self, registration, excluded):
        """Build one multi-datum definition and one request for a SnapshotSpec."""
        signal = registration.demand.signal
        spec = signal.snapshot
        total = len(spec.fields) + len(self._snapshot_clocks(spec))
        positions = tuple(position for position in range(total) if position not in excluded)
        if not any(position < len(spec.fields) for position in positions):
            raise ProviderError("SimConnect rejected every field of the snapshot.")
        decoder = Struct("<" + "".join(
            self._simvar_encoding(self._snapshot_field(spec, position))[1].format.lstrip("<")
            for position in positions
        ))
        flags = DATA_REQUEST_FLAG_CHANGED if spec.changed_only else 0
        state = _SnapshotState(signal, registration.id, self._new_id(), self._new_id(), positions, decoder,
                               flags, set(excluded))
        self._snapshots[signal] = state
        self._snapshot_requests[state.request] = state
        try:
            for datum, position in enumerate(positions):
                packet = self._define(state.definition, self._snapshot_field(spec, position), datum_id=datum)
                self._snapshot_field_packets[packet] = (state, position)
            packet = self._call(
                "SimConnect_RequestDataOnSimObject",
                state.request, state.definition, _USER_AIRCRAFT, _SNAPSHOT_PERIODS[spec.period], flags, 0,
                spec.every - 1, 0,
            )
        except ProviderError:
            self._snapshots.pop(signal, None)
            self._clear_snapshot(state)
            raise
        self._snapshot_field_packets[packet] = (state, None)
        state.requested = True
        return state

    def _clear_snapshot(self, state):
        self._snapshot_requests.pop(state.request, None)
        if state.requested:
            state.requested = False
            self._teardown("SimConnect_RequestDataOnSimObject", state.request,
                           state.definition, _USER_AIRCRAFT, Period.NEVER, 0, 0, 0, 0)
        self._teardown("SimConnect_ClearDataDefinition", state.definition)

    def _snapshot_field_rejected(self, state, position, detail):
        """Record a field SimConnect refused; the snapshot is rebuilt without it.

        ``position`` None means SimConnect refused the request itself; the
        snapshot then reports an error and is retried with backoff.

        Field refusals are collected and the definition is rebuilt once at the
        end of the drain, so several refused fields cost one rebuild. A refusal
        that names an earlier definition of the same registration (it was
        rebuilt before this exception arrived) still excludes the field, because
        each rebuild re-added it.
        """
        signal = state.signal
        registration = self._registrations.get(signal)
        if registration is None or registration.id != state.registration_id or registration.id in self._failed:
            _LOG.warning("Ignored a late SimConnect exception for a retired snapshot: %s", detail)
            return
        if position is None:
            if self._snapshots.get(signal) is not state:
                _LOG.warning("Ignored a late SimConnect exception for a replaced snapshot request: %s", detail)
                return
            state.requested = False
            self._mark_failed(registration)
            self._reading(signal, None, error=detail)
            return
        name = self._snapshot_field(signal.snapshot, position).name
        _LOG.warning("SimConnect refused snapshot field %s: %s", name, detail)
        self._snapshot_exclusions.setdefault(signal, set()).add(position)
        self._snapshot_rebuilds.add(signal)

    def _forget_total_refusal(self, signal):
        """When every field was refused, let the backoff retry try them all again.

        A partial refusal is remembered; a total one more likely means the
        aircraft or simulator was not ready, and remembering it would make every
        retry fail without asking SimConnect.
        """
        excluded = self._snapshot_exclusions.get(signal)
        if excluded is not None and all(position in excluded for position in range(len(signal.snapshot.fields))):
            del self._snapshot_exclusions[signal]

    def _rebuild_snapshots(self):
        for signal in tuple(self._snapshot_rebuilds):
            self._snapshot_rebuilds.discard(signal)
            registration = self._registrations.get(signal)
            if registration is None or registration.id in self._failed:
                continue
            excluded = self._snapshot_exclusions.get(signal, set())
            state = self._snapshots.get(signal)
            if state is not None and excluded <= state.excluded:
                continue  # Already built without every refused field.
            if state is not None:
                self._snapshots.pop(signal)
                self._clear_snapshot(state)
            try:
                self._define_snapshot(registration, excluded)
            except ProviderError as error:
                self._snapshots.pop(signal, None)
                self._forget_total_refusal(signal)
                self._mark_failed(registration)
                self._reading(signal, None, error=f"{error} Refused fields: {len(excluded)}.")
            if not self._ready:
                return

    def _receive_snapshot(self, state, prefix, packet):
        signal = state.signal
        registration = self._registrations.get(signal)
        if registration is None or registration.id in self._failed:
            return
        spec = signal.snapshot
        offset = sizeof(ReceiveSimObjectDataPrefix)
        decoder = state.decoder
        if (prefix.definition_id != state.definition or prefix.flags & ~state.flags & 0xFFFFFFFF
                or prefix.definition_count not in (len(state.positions), (decoder.size + 7) // 8)
                or len(packet) < offset + decoder.size):
            self._reading(signal, None, error="Invalid SimConnect snapshot definition or payload.")
            return
        raw = decoder.unpack_from(packet, offset)
        values = [None] * (len(spec.fields) + len(_SNAPSHOT_CLOCKS))
        for position, value in zip(state.positions, raw):
            values[position] = self._snapshot_value(self._snapshot_field(spec, position), value)
        simulation_time, absolute_time, rate = values[len(spec.fields):]
        sample_time = simulator_utc(absolute_time)
        frame = SnapshotFrame(tuple(values[:len(spec.fields)]), simulation_time, absolute_time, rate)
        self._retry_attempts.pop(signal, None)
        self._offer(Reading(
            self._session, signal, frame, datetime.now(timezone.utc), monotonic(), sample_time=sample_time,
        ))

    @staticmethod
    def _snapshot_value(signal, value):
        if isinstance(value, bytes):
            return _text_value(value)
        if type(value) is float and not isfinite(value):
            return None
        if signal.value_type is ValueType.BOOLEAN:
            return bool(value)
        if signal in FREQUENCY_HZ_SIGNALS:
            return round(value)
        return value

    # Commands -----------------------------------------------------------------

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
        sent_packet = self._call(function, *arguments)
        self._ledger[sent_packet] = f"command {command.operation} ({command.id})"
        now = monotonic()
        timeout = max(self._readback_timeout, _RESPONSE_TIMEOUT) if response_id is not None else self._readback_timeout
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
        if not self._has("SimConnect_TransmitClientEvent_EX1"):
            raise CommandRejected("The selected SimConnect.dll cannot send events with several arguments.")
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
        rel_tol = _FLOAT_READBACK_REL_TOL if encoder.format in ("<d", "<f") else 0.0
        return (
            "SimConnect_SetDataOnSimObject",
            (definition, _USER_AIRCRAFT, 0, 1, len(packed), data),
            _Readback(definition, encoder, (value,), signal.name, rel_tol=rel_tol),
        )

    def _map_event(self, operation):
        if operation in self._event_errors:
            raise CommandRejected(self._event_errors[operation])
        event_id = self._event_ids.get(operation)
        if event_id is None:
            event_id = self._new_id()
            try:
                packet = self._call("SimConnect_MapClientEventToSimEvent", event_id, operation.encode("ascii"))
            except ProviderError as error:
                raise CommandRejected(str(error)) from error
            self._event_ids[operation] = event_id
            self._event_map_packets[packet] = (operation, event_id)
        return event_id

    def _single_field_write_definition(self, definition):
        return any(len(signals) == 1 and known == definition for signals, known in self._write_definitions.items())

    def _write_definition(self, signals):
        definition = self._write_definitions.get(signals)
        if definition in self._write_definition_errors:
            raise CommandRejected(self._write_definition_errors[definition])
        if definition is None:
            definition = self._new_id()
            for signal in signals:
                packet = self._define(definition, signal)
                self._write_definition_packets[packet] = definition
            self._write_definitions[signals] = definition
        return definition

    def _prepare_radio(self, command):
        if command.operation == _SET_STANDBY:
            if len(command.arguments) != 1:
                raise CommandRejected("Supply one COM1 standby frequency in Hz.")
            frequency = command.arguments[0]
            problem = com_channel_problem(frequency)
            if problem is not None:
                raise CommandRejected(problem)
        elif command.operation != _SWAP or command.arguments:
            raise CommandRejected("Unsupported COM1 command or arguments.")
        definition = self._write_definition(_COM1_PAIR)
        active, standby, spacing = self._read_radio_baseline(definition)
        if command.operation == _SET_STANDBY:
            # Same channel rule as Core and SimulatedProvider (contract C6): a radio
            # known to use 25 kHz spacing refuses an 8.33 kHz channel before sending.
            problem = com_channel_problem(frequency, spacing)
            if problem is not None:
                raise CommandRejected(problem)
            return _Readback(definition, _RADIO_PAIR, (None, _com_readback_options(frequency, standby)),
                             "COM1 frequencies", int_tol=_COM_RADIO_INT_TOL, round_hz=True)
        return _Readback(definition, _RADIO_PAIR, (standby, active), "COM1 frequencies",
                         int_tol=_COM_RADIO_INT_TOL, round_hz=True)

    def _radio_spacing_definition(self):
        """The COM SPACING MODE:1 definition, or None once SimConnect refused it."""
        try:
            definition = self._write_definition(_COM1_SPACING)
        except CommandRejected:
            return None
        self._spacing_definition = definition
        return definition

    def _read_radio_baseline(self, definition):
        """Read the COM1 pair and, when available, its channel spacing (None if unknown)."""
        spacing_definition = self._radio_spacing_definition()
        self._before_request, self._before_packet = self._request_readback(definition)
        self._before_definition = definition
        self._before_reading = None
        self._before_error = None
        self._spacing_value = None
        if spacing_definition is not None:
            try:
                self._spacing_request, self._spacing_packet = self._request_readback(spacing_definition)
            except ProviderError as error:
                _LOG.info("COM1 spacing is unknown for this command: %s", error)
            else:
                self._spacing_value = _SPACING_PENDING
        deadline = monotonic() + self._baseline_timeout
        budget = _BASELINE_PACKET_BUDGET
        pair = None
        try:
            while self._ready and monotonic() < deadline:
                if pair is None and self._before_reading is not None:
                    if not all(map(self._in_com_band, self._before_reading)):
                        break
                    pair = self._before_reading
                if pair is not None and self._spacing_value is not _SPACING_PENDING:
                    return (*pair, self._spacing_value)
                if not budget:
                    raise CommandRejected("SimConnect receive backlog prevented the COM1 baseline read.")
                packet = self._next()
                if packet is None:
                    self._idle(0.005)
                    continue
                budget -= 1
                self._process(*packet)
                if self._before_error is not None:
                    raise CommandRejected(self._before_error)
            if pair is None and self._before_reading is not None and all(map(self._in_com_band, self._before_reading)):
                pair = self._before_reading
            if pair is not None:
                # The pair arrived but the spacing did not: proceed with it unknown.
                return (*pair, None)
        finally:
            if self._before_packet is not None:
                self._ledger[self._before_packet] = "COM1 baseline read"
            if self._spacing_packet is not None:
                self._ledger[self._spacing_packet] = "COM1 spacing read"
            self._before_request = None
            self._before_packet = None
            self._before_definition = None
            self._before_reading = None
            self._before_error = None
            self._spacing_request = None
            self._spacing_packet = None
            self._spacing_value = _SPACING_PENDING
        raise CommandRejected("No valid fresh COM1 baseline was available for the command.")

    @staticmethod
    def _in_com_band(value):
        return _COM_BAND[0] <= value < _COM_BAND[1]

    def _request_readback(self, definition, client_data_id=None):
        id = self._new_id()
        if client_data_id is None:
            packet = self._call(
                "SimConnect_RequestDataOnSimObject", id, definition,
                _USER_AIRCRAFT, Period.ONCE, 0, 0, 0, 0,
            )
        else:
            packet = self._call(
                "SimConnect_RequestClientData", client_data_id, id, definition,
                ClientDataPeriod.ONCE, 0, 0, 0, 0,
            )
        return id, packet

    # Drain --------------------------------------------------------------------

    def drain(self, limit):
        transport_empty = False
        if self._ready:
            started = perf_counter()
            for processed in range(limit):
                if self._queued() >= limit:
                    break  # Back-pressure: leave the rest in SimConnect's queue.
                if processed >= _DRAIN_MIN_PACKETS and perf_counter() - started > _DRAIN_TIME_BUDGET_S:
                    break  # Bound one pump's work; the rest is read on the next pump.
                packet = self._next()
                if packet is None:
                    transport_empty = True
                    break
                self._process(*packet)
                if not self._ready:
                    break
        take_dropped = getattr(self._sdk, "take_dropped", None)
        if take_dropped is not None:
            self._dropped += take_dropped()
        if self._ready:
            self._rebuild_snapshots()
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
            self._retry_failed_registrations()
        if self._ready:
            self._advance_readback()
        batch = []
        if self._dropped:
            batch.append(IngressGap(self._session, self._dropped))
            self._dropped = 0
        control, data = self._control, self._data
        while (control or data) and len(batch) < limit:
            if not data or (control and control[0][0] < data[0][0]):
                batch.append(control.popleft()[1])
            else:
                batch.append(data.popleft()[1])
        return tuple(batch)

    def _queued(self):
        return len(self._control) + len(self._data)

    def _advance_readback(self):
        pending = self._pending
        if pending is None:
            return
        now = monotonic()
        readback = pending.readback
        if now >= pending.deadline:
            if pending.command.operation in ADVANCED_OPERATIONS:
                self._complete_command(Outcome.UNVERIFIED, self._advanced.command_timeout(pending))
            elif readback is None:
                self._complete_command(
                    Outcome.UNVERIFIED, "The command was sent. This capability has no configured readback.",
                )
            else:
                observed = "" if pending.observed is None else f" Last readback: {self._describe(pending.observed)}."
                self._complete_command(Outcome.UNCERTAIN, f"{readback.description} did not confirm the command.{observed}")
        elif readback is not None and pending.request_id is None and now >= pending.next_read:
            try:
                pending.request_id, pending.readback_packet = self._request_readback(
                    readback.definition, readback.client_data_id,
                )
            except ProviderError as error:
                self._complete_command(Outcome.UNCERTAIN, f"Could not request {readback.description}: {error}")

    @staticmethod
    def _describe(values):
        return ", ".join(repr(value) if isinstance(value, bytes) else str(value) for value in values)

    # Packets ------------------------------------------------------------------

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
            elif kind == ReceiveId.ASSIGNED_OBJECT_ID:
                self._advanced.late_assignment(packet)
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
            prefix = self._prefix(ReceiveSimObjectDataPrefix, packet, "Client Data")
            if prefix is not None and not self._receive_readback(prefix, packet, client_data=True):
                self._client_data.process(prefix, packet)
        elif kind == ReceiveId.SYSTEM_STATE:
            self._receive_system_state(packet)
        elif kind in SimConnectFacilities.RECEIVE_IDS:
            self._facilities.receive(kind, packet)
        elif kind in (ReceiveId.EVENT, ReceiveId.EVENT_FILENAME, ReceiveId.EVENT_FRAME,
                      ReceiveId.EVENT_OBJECT, ReceiveId.EVENT_WEATHER_MODE, ReceiveId.CUSTOM_ACTION):
            self._receive_system_event(kind, packet)
        elif not self._inputs.process(kind, packet):
            _LOG.debug("Ignored SimConnect packet type %s.", kind)

    def _prefix(self, cls, packet, label):
        """Decode a routing prefix. An unattributable short packet is logged and dropped."""
        if len(packet) < sizeof(cls):
            _LOG.warning("Dropped a truncated SimConnect %s packet of %d bytes.", label, len(packet))
            return None
        return cls.from_buffer_copy(packet)

    def _receive_exception(self, error):
        detail = self._exception_detail(error)
        send_id = error.send_id
        pending = self._pending
        if pending is not None and send_id == pending.sent_packet:
            self._complete_command(Outcome.REJECTED, detail)
            return
        mapped_event = self._event_map_packets.pop(send_id, None)
        if mapped_event is not None:
            event, event_id = mapped_event
            self._event_errors[event] = detail
            self._client_events.mapping_failed(event_id, detail)
            if pending is not None and pending.mapped_event_id == event_id:
                self._complete_command(Outcome.REJECTED, detail)
            return
        definition = self._write_definition_packets.get(send_id)
        if definition is not None:
            self._write_definition_errors[definition] = detail
            if definition == self._spacing_definition:
                # COM SPACING MODE is optional: the spacing is then unknown.
                _LOG.info("SimConnect refused COM SPACING MODE:1; COM1 spacing is unknown: %s", detail)
                if self._spacing_value is _SPACING_PENDING:
                    self._spacing_value = None
                return
            if definition == self._before_definition:
                self._before_error = detail
            if pending is not None and pending.readback is not None and definition == pending.readback.definition:
                if self._single_field_write_definition(definition):
                    # The only field was refused, so the definition is empty and
                    # SetDataOnSimObject cannot have written anything.
                    self._complete_command(
                        Outcome.REJECTED, f"SimConnect refused the SimVar definition: {detail}")
                else:
                    self._complete_command(Outcome.UNCERTAIN, detail)
            return
        if send_id == self._before_packet:
            self._before_error = detail
            return
        if self._spacing_packet is not None and send_id == self._spacing_packet:
            if self._spacing_value is _SPACING_PENDING:
                self._spacing_value = None
            return
        if pending is not None and send_id == pending.readback_packet:
            self._complete_command(Outcome.UNCERTAIN, detail)
            return
        snapshot_field = self._snapshot_field_packets.pop(send_id, None)
        if snapshot_field is not None:
            self._snapshot_field_rejected(*snapshot_field, detail)
            return
        if self._inputs.receive_exception(send_id, detail):
            return
        if self._client_data.receive_exception(send_id, detail, error.exception):
            return
        if self._client_events.receive_exception(send_id, detail):
            return
        if self._advanced.receive_exception(send_id, detail):
            return
        if self._facilities.receive_exception(send_id, detail):
            return
        id, operation = self._sent_registrations.get(send_id, (None, None))
        if id is not None and id in self._lifecycle_events:
            raise ProviderError(f"A required simulator lifecycle registration failed: {detail}")
        state = self._state_requests.pop(id, None)
        if state is not None:
            self._reading(state[0], None, error=detail)
            return
        registration = self._by_id.get(id)
        if registration is not None:
            signal = registration.demand.signal
            if operation == "SimConnect_SubscribeToSystemEvent":
                self._system_event_ids.pop(signal, None)
                self._disabled_events.discard(id)
            elif operation == "SimConnect_RequestDataOnSimObject":
                # The request was refused, so it is not held; the definition still is,
                # and the retry's retire clears it. After a refused AddToDataDefinition
                # both ids stay recorded: the request was already sent, so the retire
                # sends Period.NEVER and ClearDataDefinition (rejections are tolerated).
                self._simvar_requests.discard(id)
            if id not in self._failed:
                self._mark_failed(registration)
            self._reading(signal, None, error=detail)
            return
        owner = self._ledger.get(send_id)
        if owner is None:
            _LOG.warning("Ignored an unattributed SimConnect exception: %s", detail)
        else:
            _LOG.warning("Ignored a late SimConnect exception for %s: %s", owner, detail)

    def _receive_system_event(self, kind, packet):
        event = self._prefix(ReceiveEvent, packet, "event")
        if event is None:
            return
        if event.event_id == self._system_event_ids.get(SYSTEM_EVENTS["FLIGHTLOADED"]):
            self._advanced.flight_changed()
        if event.event_id == self._system_event_ids.get(SYSTEM_EVENTS["OBJECTREMOVED"]):
            self._advanced.owned_object_removed(event.data)
        if event.event_id in (self._system_event_ids.get(SYSTEM_EVENTS["FLIGHTLOADED"]),
                              self._system_event_ids.get(SYSTEM_EVENTS["AIRCRAFTLOADED"])):
            self._retry_now()  # A new aircraft or flight is a good moment to retry failed registrations.
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
            self._retry_attempts.pop(signal, None)
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
        prefix = self._prefix(ReceiveRequest, packet, "system state")
        if prefix is None:
            return
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
        prefix = self._prefix(ReceiveSimObjectDataPrefix, packet, "object data")
        if prefix is None:
            return
        request = prefix.request_id
        if request == self._before_request:
            try:
                self._before_reading = _round_hz(
                    self._payload(prefix, packet, self._before_definition, 2, _RADIO_PAIR))
            except ProviderError as error:
                self._before_error = str(error)
            return
        if self._spacing_request is not None and request == self._spacing_request:
            self._spacing_request = None
            try:
                spacing, = self._payload(prefix, packet, self._spacing_definition, 1, _SPACING)
            except ProviderError as error:
                _LOG.info("COM1 spacing reply was invalid: %s", error)
                spacing = None
            self._spacing_value = spacing if spacing in (0, 1) else None
            return
        if self._receive_readback(prefix, packet):
            return
        snapshot = self._snapshot_requests.get(request)
        if snapshot is not None:
            self._receive_snapshot(snapshot, prefix, packet)
            return
        registration = self._by_id.get(request)
        if registration is None or request in self._failed or registration.demand.signal.event:
            return
        signal = registration.demand.signal
        if signal.snapshot is not None:
            return
        _, decoder = self._simvar_encoding(signal)
        try:
            value, = self._payload(prefix, packet, registration.id, 1, decoder)
            if signal in FREQUENCY_HZ_SIGNALS:
                value, = _round_hz((value,))
        except ProviderError as error:
            # Contain a malformed reply to its own signal; the session continues.
            self._reading(signal, None, error=str(error))
            return
        if isinstance(value, bytes):
            value = _text_value(value)
        self._retry_attempts.pop(signal, None)
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
                if readback.round_hz:
                    values = _round_hz(values)
        except ProviderError as error:
            self._complete_command(Outcome.UNCERTAIN, str(error))
            return True
        pending.observed = values
        if readback.matches(values):
            self._complete_command(Outcome.CONFIRMED, self._confirmation(pending, values))
        else:
            if pending.readback_packet is not None:
                self._ledger[pending.readback_packet] = f"readback for command {pending.command.id}"
            pending.request_id = None
            pending.next_read = monotonic() + 0.1
        return True

    @staticmethod
    def _confirmation(pending, values):
        readback = pending.readback
        detail = f"Observed the expected {readback.description}."
        command = pending.command
        if command.operation == _SET_STANDBY and command.arguments:
            requested, stored = command.arguments[0], values[1]
            if abs(stored - requested) > readback.int_tol:
                detail += (f" The simulator stored {stored} Hz, the channel nearest the requested "
                           f"{requested} Hz.")
        return detail

    @staticmethod
    def _payload(prefix, packet, definition, count, decoder):
        offset = sizeof(ReceiveSimObjectDataPrefix)
        # dwDefineCount is the datum count in practice; the SDK text describes
        # it as the count of 8-byte elements. Accept either reading.
        if (prefix.definition_id != definition or prefix.definition_count not in (count, (decoder.size + 7) // 8)
                or prefix.flags or len(packet) < offset + decoder.size):
            raise ProviderError("Invalid SimConnect data definition or payload.")
        return decoder.unpack_from(packet, offset)

    def _complete_command(self, outcome, detail, data=None):
        pending = self._pending
        self._pending = None
        self._ledger[pending.sent_packet] = f"finished command {pending.command.operation} ({pending.command.id})"
        if pending.readback_packet is not None:
            self._ledger[pending.readback_packet] = f"readback for finished command {pending.command.id}"
        self._offer(CommandOutcome(self._session, pending.command.id, outcome, detail, data))

    def _reading(self, signal, value, *, error=None):
        self._offer(Reading(
            self._session, signal, value, datetime.now(timezone.utc), monotonic(), error=error,
        ))

    def _offer(self, update):
        item = (next(self._sequence), update)
        # Only periodic telemetry (SimVar, snapshot and Client Data values) is
        # evictable. Discrete system events, client events and hardware input
        # samples are one-off and go to the control lane with everything else.
        if isinstance(update, Reading) and update.error is None and not update.signal.event:
            if len(self._data) >= _DATA_CAP:
                self._data.popleft()
                self._dropped += 1
            self._data.append(item)
        else:
            # Outcomes, replies, catalogs, error readings and Disconnect: never evicted.
            self._control.append(item)

    def _new_id(self):
        id = next(self._ids)
        if id >= _UNUSED:
            raise ProviderError("SimConnect request ID space exhausted.")
        return id

    def _call(self, name, *args):
        """Send one SDK call and attribute its packet. Returns the packet ID."""
        self._sdk.call(name, self._handle, *args)
        packet = self._sdk.last_packet_id(self._handle)
        self._last_sent = packet
        self._ledger[packet] = name
        return packet

    def _call_registration(self, id, name, *args):
        packet = self._call(name, *args)
        self._sent_registrations[packet] = (id, name)
        return packet

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
