"""Provider-independent messages and capabilities for the BBC Core API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, IntFlag
from math import isfinite
from uuid import UUID


API_VERSION = "0.1-draft"
Scalar = bool | int | float | str | bytes


class StrEnum(str, Enum):
    """A text enum whose ``str()`` and ``format()`` give the value on every supported Python.

    ``enum.StrEnum`` only exists from Python 3.11, and a plain ``(str, Enum)`` prints as
    ``Class.MEMBER`` in f-strings on 3.12. Records and logs written with ``f"{member}"``
    therefore read back the same way whichever interpreter wrote them.
    """

    __str__ = str.__str__
    __format__ = str.__format__


class Kind(StrEnum):
    VALUE = "value"
    EVENT = "event"
    COMMAND = "command"


class ValueType(StrEnum):
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
    SNAPSHOT = "snapshot"


class Quality(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class CommandState(StrEnum):
    ACCEPTED = "accepted"
    SENT = "sent"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    NOT_SENT = "not_sent"
    UNCERTAIN = "uncertain"
    UNVERIFIED = "unverified"


class SnapshotPeriod(StrEnum):
    """How often a provider samples a snapshot: per second, per simulation frame or per visual frame."""
    SECOND = "second"
    SIM_FRAME = "sim_frame"
    VISUAL_FRAME = "visual_frame"


class PauseState(IntFlag):
    """Decoded ``Pause_EX1`` flags. The value stays an ``int``, so existing comparisons work.

    The SDK documents FULL (1, full pause), FULL_WITH_SOUND (2, legacy full pause with
    sound), ACTIVE (4, the Active Pause button) and SIM (8, the player's simulation is paused
    while traffic and multiplayer keep running). Unknown bits are kept, not discarded.
    """

    NONE = 0
    FULL = 1
    FULL_WITH_SOUND = 2
    ACTIVE = 4
    SIM = 8

    @property
    def full_pause(self) -> bool:
        return bool(self & (PauseState.FULL | PauseState.FULL_WITH_SOUND))

    @property
    def active_pause(self) -> bool:
        return bool(self & PauseState.ACTIVE)

    @property
    def sim_pause(self) -> bool:
        return bool(self & PauseState.SIM)

    @property
    def paused(self) -> bool:
        """True for any documented pause, including Active Pause."""
        return bool(self & (PauseState.FULL | PauseState.FULL_WITH_SOUND | PauseState.ACTIVE | PauseState.SIM))

    __str__ = int.__repr__
    __format__ = int.__format__


class InputCatalogState(StrEnum):
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
    """Updates were lost from a queue (the provider's or this consumer's): data loss.

    A provider error for one subscription is a separate type, :class:`SignalError`, and is
    never a ``Gap``. ``isinstance(update, Gap)`` therefore means data loss only.
    """

    dropped_updates: int
    reason: str

    @property
    def is_data_loss(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class SignalError:
    """The provider reported an error for one subscription instead of a value or event.

    This is not a :class:`Gap`: it means "this source failed", not "updates were lost from a
    queue". It has the same ``dropped_updates`` and ``reason`` fields so the two can be shown
    alike. ``dropped_updates`` is the number of events or samples the error replaced.
    """

    dropped_updates: int
    reason: str
    capability_id: str = ""
    subscription_id: str = ""

    @property
    def is_data_loss(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class SubscriptionEnded:
    """Core ended a subscription itself, for example after the aircraft inputs changed.

    The subscription ID is no longer active. Subscribe again to keep receiving the data.
    """

    subscription_id: str
    capability_id: str
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


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Values of several SimVars taken from one simulator delivery, with simulator clocks.

    ``values`` follows the order of ``names``. A value is None when the provider could not
    decode that field. ``simulation_time_s`` is SIMULATION TIME (seconds, advances with sim
    rate and stops in pause), ``absolute_time_s`` is ABSOLUTE TIME (seconds since
    0001-01-01T00:00 on the simulator's UTC clock) and ``sample_time`` is that instant as
    an aware UTC datetime. Host receipt time is ``received_at``. For a ``changed_only``
    subscription the four clock fields are None (the provider leaves the clock datums out).
    """

    subscription_id: str
    names: tuple[str, ...]
    values: tuple[Scalar | None, ...]
    simulation_time_s: float | None
    absolute_time_s: float | None
    simulation_rate: float | None
    source: Source
    session_id: str
    received_at: datetime
    sample_time: datetime | None = None


Update = (
    Connection | Observation | Event | Gap | SignalError | CommandResult | InputCatalog | InputParameters
    | Snapshot | SubscriptionEnded
)

_ABSOLUTE_TIME_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)


def simulator_utc(absolute_time_s: float | None) -> datetime | None:
    """Convert ABSOLUTE TIME (seconds since 0001-01-01 on the simulator's UTC clock) to a datetime.

    Returns None for a missing, non-finite or out-of-range value rather than guessing.
    """
    if type(absolute_time_s) not in (int, float):
        return None
    try:
        if not isfinite(absolute_time_s) or absolute_time_s < 0:
            return None
        return _ABSOLUTE_TIME_EPOCH + timedelta(seconds=absolute_time_s)
    except (OverflowError, ValueError):
        return None


class CoreRequestError(ValueError):
    """A consumer, capability or request is invalid for this API."""
