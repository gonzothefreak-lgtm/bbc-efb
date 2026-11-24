"""Internal provider contract. Core calls it from one serial owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from .client_data import ClientDataMode, ClientDataRegion
from .contract import InputCatalog, InputId, InputParameters, SnapshotPeriod, Source, StrEnum, ValueType
from .facilities import (
    AirportReference, FacilityChange, FacilityIdentity, FacilityKind, FacilityQuery,
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
    snapshot: "SnapshotSpec | None" = None
    # Client Data SECOND/VISUAL_FRAME streams only: deliver only when the region changed
    # (SIMCONNECT_CLIENT_DATA_REQUEST_FLAG_CHANGED).
    client_data_changed_only: bool = False
    # Signals are dictionary keys on every reading, so the hash is computed once.
    _hash: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self):
        # SimConnect matches variable names and units without case sensitivity. Client event
        # names are already validated by client_event_name(): standard events are upper-cased
        # there and custom VENDOR.EVENT names keep their owner's spelling, so that a subscription
        # maps exactly the name a transmit maps.
        name = self.name.strip()
        object.__setattr__(self, "name", name if self.client_event else name.upper())
        if self.unit is not None:
            object.__setattr__(self, "unit", self.unit.strip().lower())
        object.__setattr__(self, "_hash", hash((
            self.name, self.unit, self.value_type, self.event, self.input_id, self.client_data_region,
            self.system_state, self.simvar_format, self.local_simvar, self.client_event,
            self.hardware_input, self.client_data_mode, self.snapshot, self.client_data_changed_only,
        )))

    def __hash__(self) -> int:
        return self._hash


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    """A grouped read: every field comes from the same simulator delivery.

    Core builds the snapshot Signal as ``Signal("SNAPSHOT", None, ValueType.SNAPSHOT,
    snapshot=spec)``. ``fields`` are plain SimVar signals (not events, system states,
    inputs or Client Data). ``every`` delivers every Nth period (seconds for SECOND,
    frames otherwise). With ``changed_only`` the provider asks SimConnect to send only
    when a field changed. The provider appends its own clock fields and reports them in
    SnapshotFrame rather than in ``values``. A changed_only snapshot carries no simulator
    clocks (simulation_time_s, absolute_time_s, simulation_rate and sample_time are None),
    because a clock datum would change every frame; use ``received_at``.
    """

    fields: tuple["Signal", ...]
    period: SnapshotPeriod = SnapshotPeriod.SECOND
    every: int = 1
    changed_only: bool = False


@dataclass(frozen=True, slots=True)
class SnapshotFrame:
    """Provider payload of a snapshot Reading. ``values`` follow SnapshotSpec.fields."""

    values: tuple
    simulation_time_s: float | None = None
    absolute_time_s: float | None = None
    simulation_rate: float | None = None


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


class Outcome(StrEnum):
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
    """Lifecycle and I/O run serially. Support queries read metadata only.

    Optional attributes Core reads with ``getattr`` when present:

    - ``max_command_timeout_seconds``: the longest time the provider can take to report the
      outcome of a sent command (its own readback or response timeout). Core waits this long
      plus a grace period after sending before it treats the command as uncertain. Core
      assumes 4 seconds when the provider does not say.
    - ``facility_timeout_seconds``: the longest time the provider takes to answer or fail a
      facility or simulator read. Core fails a read itself if no answer arrives in this time
      plus a grace period. Core assumes 30 seconds when the provider does not say.
    - ``interval_granularity_ms``: the step the provider's native periods come in (SimConnect
      periods are whole seconds, so 1000). Core rounds a requested interval up to it and
      reports that interval to the consumer. Core assumes 1 ms when the provider does not say.
    - ``clock``: a callable returning the monotonic seconds used for ``received_monotonic``,
      when that is not ``time.monotonic`` (for example an injected replay clock).
    """

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
