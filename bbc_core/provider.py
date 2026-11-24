"""Internal provider contract. Core calls it from one serial owner."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from .client_data import ClientDataMode, ClientDataRegion
from .contract import InputCatalog, InputId, InputParameters, Scalar, Source, ValueType
from .facilities import (
    AirportReference, FacilityChange, FacilityData, FacilityIdentity, FacilityKind, FacilityQuery,
    FacilityState, NavaidReference,
)
from .sdk_simvar import SimVarFormat
from .simulator_ops import ControllerInput, SimulatorRead


@dataclass(frozen=True, slots=True)
class Signal:
    name: str
    unit: str | None
    value_type: ValueType
    event: bool = False
    input_id: InputId | None = None
    client_data_region: ClientDataRegion | None = None
    system_state: bool = False
    simvar_format: SimVarFormat | None = None
    local_simvar: bool = False
    client_event: bool = False
    hardware_input: ControllerInput | None = None
    client_data_mode: ClientDataMode = ClientDataMode.ON_SET

    def __post_init__(self):
        # SimConnect matches variable names and units without case sensitivity.
        object.__setattr__(self, "name", self.name.strip().upper())
        if self.unit is not None:
            object.__setattr__(self, "unit", self.unit.strip().lower())


@dataclass(frozen=True, slots=True)
class Demand:
    signal: Signal
    interval_ms: int | None
    event_enabled: bool = True


@dataclass(frozen=True, slots=True)
class Reading:
    session_id: str
    signal: Signal
    value: object
    received_at: datetime
    received_monotonic: float
    sample_time: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Command:
    id: str
    session_id: str
    operation: str
    arguments: tuple
    input_id: InputId | None = None
    client_data_region: ClientDataRegion | None = None
    sdk_signal: Signal | None = None
    sdk_event: str | None = None
    consumer_id: str = ""


class Outcome(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    session_id: str
    command_id: str
    outcome: Outcome
    detail: str
    data: object = None


@dataclass(frozen=True, slots=True)
class Disconnect:
    session_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class IngressGap:
    session_id: str
    dropped_updates: int


@dataclass(frozen=True, slots=True)
class FacilityReply:
    session_id: str
    id: str
    query: FacilityQuery | SimulatorRead
    state: FacilityState
    data: object = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FacilityCacheReply:
    session_id: str
    watch_id: str
    kind: FacilityKind
    change: FacilityChange
    entries: tuple[AirportReference | NavaidReference | FacilityIdentity, ...] = ()
    detail: str = ""


ProviderUpdate = (
    Reading | CommandOutcome | Disconnect | IngressGap | InputCatalog | InputParameters | FacilityReply
    | FacilityCacheReply
)


class ProviderError(RuntimeError):
    """An adapter operation failed. A send error may leave its outcome unknown."""


class CommandRejected(ProviderError):
    """The adapter refused a command before attempting to send it."""


class Provider(Protocol):
    """Lifecycle and I/O run serially. Support queries read metadata only."""

    source: Source
    minimum_interval_ms: int

    def supports_signal(self, signal: Signal) -> bool: ...
    def supports_command(self, operation: str) -> bool: ...
    def connect(self, session_id: str) -> None: ...
    def replace_demand(self, demand: tuple[Demand, ...]) -> None: ...
    def send(self, command: Command) -> None: ...
    def drain(self, limit: int) -> tuple[ProviderUpdate, ...]: ...
    def disconnect(self) -> None: ...


@runtime_checkable
class AircraftInputProvider(Protocol):
    def request_input_catalog(self) -> None: ...
    def request_input_parameters(self, input_id: InputId) -> None: ...
    def request_input_value(self, input_id: InputId) -> None: ...


@runtime_checkable
class ClientDataProvider(Protocol):
    def request_client_data_value(self, signal: Signal) -> None: ...


@runtime_checkable
class SystemStateProvider(Protocol):
    def request_system_state(self, signal: Signal) -> None: ...


@runtime_checkable
class FacilityProvider(Protocol):
    def request_facility(self, id: str, query: FacilityQuery | SimulatorRead) -> None: ...


@runtime_checkable
class FacilityCacheProvider(Protocol):
    def replace_facility_watches(self, watches: dict[FacilityKind, str]) -> None: ...
