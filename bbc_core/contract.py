"""Provider-independent messages and capabilities for the BBC Core API."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID


API_VERSION = "0.1-draft"
Scalar = bool | int | float | str | bytes


class Kind(str, Enum):
    VALUE = "value"
    EVENT = "event"
    COMMAND = "command"


class ValueType(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    TEXT = "text"
    BYTES = "bytes"
    FRAME = "frame"
    OBJECT = "object"
    MISSION_ACTION = "mission_action"
    CLIENT_EVENT = "client_event"
    HARDWARE_INPUT = "hardware_input"


class Quality(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class CommandState(str, Enum):
    ACCEPTED = "accepted"
    SENT = "sent"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    NOT_SENT = "not_sent"
    UNCERTAIN = "uncertain"
    UNVERIFIED = "unverified"


class InputCatalogState(str, Enum):
    UNAVAILABLE = "unavailable"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    simulated: bool


@dataclass(frozen=True, slots=True)
class CommandParameter:
    name: str
    value_type: ValueType
    unit: str | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    kind: Kind
    value_type: ValueType | None
    unit: str | None
    description: str
    control_group: str | None = None
    parameters: tuple[CommandParameter, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    capability: Capability
    provider_supported: bool
    session_active: bool


@dataclass(frozen=True, slots=True)
class Discovery:
    version: str
    source: Source
    capabilities: tuple[CapabilityStatus, ...]


@dataclass(frozen=True, slots=True)
class Connection:
    state: ConnectionState
    source: Source
    session_id: str | None
    occurred_at: datetime
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Consumer:
    id: str
    connection: Connection


@dataclass(frozen=True, slots=True)
class Subscription:
    id: str
    capability: Capability
    interval_ms: int | None


@dataclass(frozen=True, slots=True)
class Observation:
    capability_id: str
    value: Scalar | None
    unit: str | None
    quality: Quality
    source: Source
    session_id: str | None
    received_at: datetime | None
    sample_time: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FrameSample:
    frames_per_second: float
    simulation_rate: float


@dataclass(frozen=True, slots=True)
class SimObjectChange:
    object_id: int
    object_type: int


@dataclass(frozen=True, slots=True)
class MissionAction:
    instance_id: UUID
    wait_for_completion: bool
    payload: str


@dataclass(frozen=True, slots=True)
class ClientEventData:
    words: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Event:
    capability_id: str
    value: Scalar | FrameSample | SimObjectChange | MissionAction | ClientEventData
    source: Source
    session_id: str
    received_at: datetime
    sample_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class Gap:
    dropped_updates: int
    reason: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    id: str
    capability_id: str
    state: CommandState
    source: Source
    session_id: str | None
    detail: str = ""
    data: object = None


@dataclass(frozen=True, slots=True)
class InputId:
    session_id: str
    generation: int
    hash: int


@dataclass(frozen=True, slots=True)
class AircraftInput:
    id: InputId
    name: str
    value_type: ValueType | None


@dataclass(frozen=True, slots=True)
class InputCatalog:
    session_id: str | None
    generation: int
    state: InputCatalogState
    inputs: tuple[AircraftInput, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class InputParameters:
    id: InputId
    types: tuple[str, ...] | None
    detail: str = ""

    @property
    def session_id(self):
        return self.id.session_id


Update = Connection | Observation | Event | Gap | CommandResult | InputCatalog | InputParameters


class CoreRequestError(ValueError):
    """A consumer, capability or request is invalid for this API."""
