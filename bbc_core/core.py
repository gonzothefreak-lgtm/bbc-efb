"""In-process Core host and the API handed to each consumer."""

from __future__ import annotations

import heapq
import logging
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import blake2b
from itertools import count
from threading import Lock, RLock
from time import monotonic
from uuid import uuid4

from .aircraft_inputs import INPUT_WRITE, encode_input_arguments
from .catalog import (
    COM1_SPACING, COMMANDS, FREQUENCY_HZ_SIGNALS, READS, SYSTEM_EVENTS, SYSTEM_STATES, ReadMapping,
    com_channel_problem, com_spacing, decode_event_value, decode_frequency_hz, decode_value,
)
from .client_data import CLIENT_DATA_WRITE, ClientDataArea, ClientDataMode, ClientDataRegion
from .contract import (
    API_VERSION, Capability, CapabilityStatus, CommandResult, CommandState, Connection,
    ConnectionState, CoreRequestError, Discovery, Event, Gap, InputCatalog, InputCatalogState,
    InputId, InputParameters, Kind, Observation, Quality, SignalError, Snapshot, SnapshotPeriod, Subscription,
    SubscriptionEnded, Update, ValueType, simulator_utc,
)
from .facilities import (
    AirportFilter, AirportReference, FacilityCacheSubscription, FacilityCacheUpdate, FacilityChange, FacilityKind,
    FacilityQuery, FacilityResult, FacilityState, NavaidKind, NavaidReference,
)
from .provider import (
    AircraftInputProvider, ClientDataProvider, Command, CommandOutcome, CommandRejected, Demand, Disconnect,
    FacilityCacheProvider, FacilityCacheReply, FacilityProvider, FacilityReply, IngressGap,
    Outcome, Provider, ProviderError, Reading, Signal, SnapshotFrame, SnapshotSpec,
    SystemStateProvider,
)
from .sdk_simvar import CLIENT_EVENT_SEND, SIMVAR_WRITE, SimVar, client_event_name, client_event_words
from .simulator_ops import (
    OPERATION_GROUPS, ControllerInput, ObjectField, ObjectKind, SimObjectRef, SimulatorOperation,
    SimulatorRead, SimulatorReadState, SimulatorResult, operation_arguments,
)

log = logging.getLogger(__name__)

_TERMINAL_OUTCOMES = {
    Outcome.CONFIRMED: CommandState.CONFIRMED,
    Outcome.REJECTED: CommandState.REJECTED,
    Outcome.UNCERTAIN: CommandState.UNCERTAIN,
    Outcome.UNVERIFIED: CommandState.UNVERIFIED,
}
_SDK_COMMANDS = {
    SIMVAR_WRITE: Capability(SIMVAR_WRITE, Kind.COMMAND, None, None,
                             "Write a documented SimVar with state readback.", "simvar_writes"),
    CLIENT_EVENT_SEND: Capability(CLIENT_EVENT_SEND, Kind.COMMAND, None, None,
                                  "Transmit a documented simulator client event.", "client_events"),
}
_ADVANCED_COMMANDS = {f"sdk.simulator.{kind.value}": kind for kind in SimulatorOperation}
_SDK_COMMANDS.update({
    key: Capability(key, Kind.COMMAND, None, None, kind.value.replace("_", " "), OPERATION_GROUPS[kind])
    for key, kind in _ADVANCED_COMMANDS.items()
})

_MAX_CONSUMERS = 64
_MAX_SUBSCRIPTIONS = 128
_MAX_PENDING_COMMANDS = 64
_MAX_PENDING_PER_CONSUMER = 16
_MAX_FACILITY_REQUESTS = 8
_MAX_FACILITY_PER_CONSUMER = 4
_MAX_FACILITY_RESULTS = 16
_MAX_ENDED_SUBSCRIPTIONS = 256
_MAX_SNAPSHOT_FIELDS = 64
_MIN_CONTROL_QUEUE = 64
# Used when a provider does not publish its own limits (see the Provider protocol).
_DEFAULT_PROVIDER_COMMAND_TIMEOUT = 4.0
_DEFAULT_PROVIDER_FACILITY_TIMEOUT = 30.0
_PROVIDER_GRACE_SECONDS = 1.0
_FACILITY_GRACE_SECONDS = 5.0

# A simulator lifecycle event that means the matching one-shot state may have changed.
_LIFECYCLE_STATES = {
    "AIRCRAFTLOADED": ("AIRCRAFTLOADED",),
    "FLIGHTLOADED": ("FLIGHTLOADED", "AIRCRAFTLOADED", "FLIGHTPLAN"),
    "FLIGHTPLANACTIVATED": ("FLIGHTPLAN",),
    "FLIGHTPLANDEACTIVATED": ("FLIGHTPLAN",),
    "SIM": ("SIM",),
}


def known_control_groups() -> frozenset[str]:
    """Every control group a host may grant with ``Core.register_consumer(control_groups=...)``."""
    groups = {entry.capability.control_group for entry in COMMANDS.values()}
    groups.update(entry.control_group for entry in _SDK_COMMANDS.values())
    groups.update(("aircraft_inputs", "client_data"))
    groups.discard(None)
    return frozenset(groups)


def _decode_reading(capability, signal, value):
    """Decode a provider value for ``capability``. COM frequencies are rounded to whole Hz."""
    if signal in FREQUENCY_HZ_SIGNALS:
        return decode_frequency_hz(value)
    return decode_value(capability.value_type, value)


def _sdk_simvar_signal(variable):
    if not isinstance(variable, SimVar):
        raise CoreRequestError("Supply a SimVar address.")
    return Signal(variable.sdk_name, variable.unit, variable.value_type, simvar_format=variable.format,
                  local_simvar=variable.name.startswith("L:"))


def _positive_integer(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise CoreRequestError(f"{label} must be an integer from 1 to {maximum}.")
    return value


def _signal_key(signal: Signal) -> tuple:
    """A total, deterministic order for demand, so identical demand compares equal."""
    region = signal.client_data_region
    input_id = signal.input_id
    spec = signal.snapshot
    return (
        signal.name, signal.unit or "", signal.value_type.value, signal.event, signal.system_state,
        signal.client_event, signal.local_simvar,
        signal.simvar_format.value if signal.simvar_format else "",
        (region.area.name, region.offset, region.size) if region else ("", 0, 0),
        signal.client_data_mode.value, signal.client_data_changed_only,
        (input_id.session_id, input_id.generation, input_id.hash) if input_id else ("", 0, 0),
        signal.hardware_input.sdk_name if signal.hardware_input else "",
        (spec.period.value, spec.every, spec.changed_only, tuple(_signal_key(item) for item in spec.fields))
        if spec else (),
    )


@dataclass(slots=True, eq=False)
class _Subscription:
    public: Subscription
    signal: Signal
    max_age_ms: int | None
    owner: _Consumer = field(repr=False)
    last_delivery: float | None = None
    next_due: float | None = None
    stale_reported_at: float | None = None
    in_stale_heap: bool = False
    # A late system-state subscriber: the reading it inherited, shown as STALE until replaced.
    awaiting_refresh: Reading | None = None
    event_enabled: bool = True
    queued: int = 0


@dataclass(slots=True, eq=False)
class _Consumer:
    """Per-module state. Samples and status updates use separate bounded lanes.

    The data lane holds observations, events and snapshots and drops its oldest sample when
    full. The control lane holds connection changes, command results, catalogs, facility
    results and provider gap notices, so a burst of samples can never push them out. Each
    lane reports its own losses. Updates are returned in the order Core produced them.
    """

    id: str
    groups: frozenset[str]
    diagnostics: bool
    queue_size: int
    subscriptions: dict[str, _Subscription] = field(default_factory=dict)
    data: deque = field(default_factory=deque)
    control: deque = field(default_factory=deque)
    results: dict[str, CommandResult] = field(default_factory=dict)
    facility_results: dict[str, FacilityResult | SimulatorResult] = field(default_factory=dict)
    facility_subscriptions: dict[str, FacilityKind] = field(default_factory=dict)
    ended: dict[str, tuple[str, Capability]] = field(default_factory=dict)
    stale_heap: list = field(default_factory=list)
    sequence: count = field(default_factory=count)
    dropped: int = 0
    control_dropped: int = 0

    @property
    def control_size(self) -> int:
        return max(_MIN_CONTROL_QUEUE, self.queue_size)

    def offer(self, update):
        item = (next(self.sequence), update)
        if isinstance(update, _QueuedReading):
            if len(self.data) >= self.queue_size:
                _, discarded = self.data.popleft()
                discarded.entry.queued -= 1
                self.dropped += 1
            update.entry.queued += 1
            self.data.append(item)
        else:
            if len(self.control) >= self.control_size:
                _, discarded = self.control.popleft()
                self.control_dropped += discarded.dropped_updates if isinstance(discarded, Gap) else 1
            self.control.append(item)

    def remove_data(self, predicate):
        if not self.data:
            return
        kept = deque()
        for item in self.data:
            if predicate(item[1]):
                item[1].entry.queued -= 1
            else:
                kept.append(item)
        self.data = kept

    def remove_control(self, predicate):
        if self.control:
            self.control = deque(item for item in self.control if not predicate(item[1]))


@dataclass(slots=True)
class _Pending:
    consumer_id: str
    request: Command
    result: CommandResult
    deadline: float                     # queue deadline: accepted -> dispatched
    dispatching: bool = False
    sent_deadline: float | None = None  # outcome deadline: dispatched -> provider outcome
    coalesce_key: tuple | None = None


@dataclass(slots=True)
class _FacilityRead:
    result: FacilityResult | SimulatorResult
    consumers: set[str] = field(default_factory=set)
    dispatched: bool = False
    deadline: float | None = None


@dataclass(slots=True)
class _FacilityWatch:
    id: str
    subscribers: dict[str, str] = field(default_factory=dict)
    records: dict[tuple, AirportReference | NavaidReference] | None = None
    error: str = ""
    received_at: datetime | None = None
    session_id: str | None = None
    # tuple(records.values()), built once per change: every subscriber's snapshot shares it.
    entries: tuple | None = None

    def snapshot_entries(self) -> tuple:
        if self.records is None:
            return ()
        if self.entries is None:
            self.entries = tuple(self.records.values())
        return self.entries


class ConsumerAPI:
    """Module entry point. The host grants access when it creates this handle."""

    def __init__(self, host: Core, consumer_id: str, initial_connection: Connection):
        self._host = host
        self._consumer_id = consumer_id
        self.initial_connection = initial_connection

    def discover(self) -> Discovery:
        return self._host._discover(self._consumer_id)

    def connection(self) -> Connection:
        return self._host._connection_for(self._consumer_id)

    def subscribe(self, capability_id: str, *, interval_ms: int | None = None,
                  max_age_ms: int | None = None) -> Subscription:
        return self._host._subscribe_named(self._consumer_id, capability_id, interval_ms, max_age_ms)

    def subscribe_simvar(self, name: str, unit: str | None, value_type: ValueType, *, interval_ms: int | None = None,
                         max_age_ms: int | None = None) -> Subscription:
        return self._host._subscribe_simvar(
            self._consumer_id, name, unit, value_type, interval_ms, max_age_ms,
        )

    def subscribe_sdk_simvar(self, variable: SimVar, *, interval_ms: int | None = None,
                             max_age_ms: int | None = None) -> Subscription:
        return self._host._subscribe_sdk_simvar(self._consumer_id, variable, interval_ms, max_age_ms)

    def subscribe_snapshot(self, fields: tuple[SimVar, ...], *, period: SnapshotPeriod = SnapshotPeriod.SECOND,
                           every: int = 1, changed_only: bool = False) -> Subscription:
        """Request several SimVars taken from the same simulator delivery, with simulator clocks.

        Each delivery arrives in ``poll()`` as one ``Snapshot``; none is skipped or merged.
        ``every`` counts seconds for ``SnapshotPeriod.SECOND`` and frames otherwise. With
        ``changed_only`` the provider sends only when a field changed; the simulator clocks
        change every frame, so such a snapshot does not read them and reports
        ``simulation_time_s``, ``absolute_time_s``, ``simulation_rate`` and ``sample_time`` as
        None. Read the newest one with ``latest_snapshot()``.
        """
        return self._host._subscribe_snapshot(self._consumer_id, fields, period, every, changed_only)

    def latest_snapshot(self, subscription_id: str) -> Snapshot | None:
        """The newest snapshot of the current session, or None while none has arrived."""
        return self._host._latest_snapshot(self._consumer_id, subscription_id)

    def latest(self, subscription_id: str) -> Observation:
        """The newest value with its quality.

        CURRENT is fresh data of this session. STALE keeps the last value but says why it may
        be out of date: the feed went quiet past the subscription's limit, or (for a system
        state this module subscribed to after another) the value was received before this
        subscription and a fresh reply is on its way. UNAVAILABLE has no value and a reason,
        including when Core ended the subscription itself (see ``SubscriptionEnded``).
        COM frequencies are whole Hz, rounded from the simulator's value.
        """
        return self._host._latest(self._consumer_id, subscription_id)

    def request_system_state(self, subscription_id: str) -> None:
        self._host._refresh_system_state(self._consumer_id, subscription_id)

    def set_event_enabled(self, subscription_id: str, enabled: bool) -> None:
        self._host._set_event_enabled(self._consumer_id, subscription_id, enabled)

    def poll(self, *, limit: int = 128) -> tuple[Update | FacilityResult | FacilityCacheUpdate | SimulatorResult, ...]:
        return self._host._poll(self._consumer_id, limit)

    def request_airport(self, icao: str, *, region: str = "", filters: tuple[AirportFilter, ...] = ()) -> FacilityResult:
        return self._host._request_facility(self._consumer_id, FacilityQuery.airport(icao, region, filters))

    def request_nearby_airports(self) -> FacilityResult:
        return self._host._request_facility(self._consumer_id, FacilityQuery())

    def request_worldwide_airports(self) -> FacilityResult:
        return self._host._request_facility(self._consumer_id, FacilityQuery(worldwide_airports=True))

    def request_nearby_navaids(self, kind: NavaidKind) -> FacilityResult:
        if not isinstance(kind, NavaidKind):
            raise CoreRequestError("Select a NavaidKind for the nearby list.")
        return self._host._request_facility(self._consumer_id, FacilityQuery(navaid_kind=kind))

    def request_navaid(self, kind: NavaidKind, icao: str, *, region: str = "") -> FacilityResult:
        return self._host._request_facility(self._consumer_id, FacilityQuery.navaid(kind, icao, region))

    def request_jetways(self, icao: str, *, parking_indices: tuple[int, ...] = ()) -> FacilityResult:
        return self._host._request_facility(self._consumer_id, FacilityQuery.jetways(icao, parking_indices))

    def facility_result(self, request_id: str) -> FacilityResult | None:
        result = self._host._facility_result(self._consumer_id, request_id)
        return result if isinstance(result, FacilityResult) else None

    def request_controllers(self) -> SimulatorResult:
        return self._host._request_facility(self._consumer_id, SimulatorRead("controllers"))

    def request_dialog_notifications(self) -> SimulatorResult:
        return self._host._request_dialog_notifications(self._consumer_id)

    def request_nearby_objects(self, kind: ObjectKind, radius_m: int, field: SimVar | ObjectField) -> SimulatorResult:
        if (not isinstance(kind, ObjectKind) or type(radius_m) is not int or not 0 <= radius_m <= 200_000
                or not isinstance(field, (SimVar, ObjectField))):
            raise CoreRequestError("Supply an object type, a radius from 0 to 200,000 metres and a typed field.")
        return self._host._request_facility(self._consumer_id, SimulatorRead("nearby", field, object_kind=kind, radius=radius_m))

    def request_object_data(self, field: SimVar | ObjectField, target: SimObjectRef | None = None) -> SimulatorResult:
        if not isinstance(field, (SimVar, ObjectField)) or (target is not None and not isinstance(target, SimObjectRef)):
            raise CoreRequestError("Supply a typed field and an optional current-session object reference.")
        return self._host._request_facility(self._consumer_id, SimulatorRead("object", field, target))

    def simulator_result(self, request_id: str) -> SimulatorResult | None:
        result = self._host._facility_result(self._consumer_id, request_id)
        return result if isinstance(result, SimulatorResult) else None

    def subscribe_facility_cache(self, kind: FacilityKind) -> FacilityCacheSubscription:
        return self._host._subscribe_facility_cache(self._consumer_id, kind)

    def facility_cache_snapshot(self, subscription_id: str) -> FacilityCacheUpdate | None:
        return self._host._facility_cache_snapshot(self._consumer_id, subscription_id)

    def submit_command(self, capability_id: str, argument=None) -> CommandResult:
        return self._host._submit_command(self._consumer_id, capability_id, argument)

    def submit_simvar(self, variable: SimVar, value) -> CommandResult:
        return self._host._submit_command(self._consumer_id, SIMVAR_WRITE, value, variable=variable)

    def submit_client_event(self, event: str, arguments=()) -> CommandResult:
        return self._host._submit_command(self._consumer_id, CLIENT_EVENT_SEND, arguments, event=event)

    def subscribe_client_event(self, event: str) -> Subscription:
        return self._host._subscribe_client_event(self._consumer_id, client_event_name(event))

    def submit_simulator_operation(self, operation: SimulatorOperation, *arguments) -> CommandResult:
        if not isinstance(operation, SimulatorOperation):
            raise CoreRequestError("Select a SimulatorOperation.")
        return self._host._submit_command(self._consumer_id, f"sdk.simulator.{operation.value}", arguments)

    def subscribe_controller(self, control: ControllerInput) -> Subscription:
        return self._host._subscribe_controller(self._consumer_id, control)

    def request_aircraft_inputs(self) -> None:
        self._host._queue_input_request(self._consumer_id, "catalog")

    def aircraft_inputs(self) -> InputCatalog:
        return self._host._aircraft_inputs(self._consumer_id)

    def request_input_parameters(self, input_id: InputId) -> None:
        self._host._queue_input_request(self._consumer_id, "parameters", input_id)

    def input_parameters(self, input_id: InputId) -> InputParameters | None:
        return self._host._input_parameters_for(self._consumer_id, input_id)

    def subscribe_input(self, input_id: InputId, *, max_age_ms: int | None = None) -> Subscription:
        return self._host._subscribe_input(self._consumer_id, input_id, max_age_ms)

    def request_input_value(self, subscription_id: str) -> None:
        self._host._refresh_input(self._consumer_id, subscription_id)

    def submit_input(self, input_id: InputId, arguments=()) -> CommandResult:
        return self._host._submit_command(
            self._consumer_id, INPUT_WRITE, arguments, input_id=input_id,
        )

    def client_data_areas(self) -> tuple[ClientDataArea, ...]:
        return self._host._client_data_areas_for(self._consumer_id)

    def subscribe_client_data(self, region: ClientDataRegion, *, mode: ClientDataMode = ClientDataMode.ON_SET,
                              max_age_ms: int | None = None, changed_only: bool = False) -> Subscription:
        """``changed_only`` (SECOND and VISUAL_FRAME only) asks the simulator to send the
        region only when its bytes changed, instead of every period."""
        return self._host._subscribe_client_data(self._consumer_id, region, mode, max_age_ms, changed_only)

    def request_client_data(self, subscription_id: str) -> None:
        self._host._refresh_client_data(self._consumer_id, subscription_id)

    def submit_client_data(self, region: ClientDataRegion, payload: bytes) -> CommandResult:
        return self._host._submit_command(
            self._consumer_id, CLIENT_DATA_WRITE, payload, client_data_region=region,
        )

    def command_result(self, command_id: str) -> CommandResult | None:
        return self._host._command_result(self._consumer_id, command_id)

    def unsubscribe(self, subscription_id: str) -> None:
        self._host._unsubscribe(self._consumer_id, subscription_id)

    def close(self) -> None:
        self._host._close_consumer(self._consumer_id)


class Core:
    """Own one provider session. The application explicitly drives its lifecycle.

    ``command_timeout_seconds`` is how long an accepted command may wait in the queue before
    it is dropped unsent. Once sent, Core waits for the provider's own outcome timeout
    (``max_command_timeout_seconds`` on the provider) plus a grace period.

    ``clock`` is the monotonic clock that readings' ``received_monotonic`` stamps use. It
    defaults to the provider's ``clock`` attribute when it has one (an injected simulated or
    replay clock), otherwise ``time.monotonic``.
    """

    def __init__(self, provider: Provider, *, command_timeout_seconds: float = 5, clock=None):
        if not 0 < command_timeout_seconds <= 60:
            raise ValueError("Command timeout must be positive and at most 60 seconds.")
        clock = clock if clock is not None else getattr(provider, "clock", None)
        if clock is not None and not callable(clock):
            raise ValueError("The clock must be a callable returning monotonic seconds.")
        self._clock = clock
        _positive_integer(provider.minimum_interval_ms, "Provider minimum interval", 3_600_000)
        self._provider = provider
        self._input_provider = provider if isinstance(provider, AircraftInputProvider) else None
        self._client_data_provider = provider if isinstance(provider, ClientDataProvider) else None
        self._system_state_provider = provider if isinstance(provider, SystemStateProvider) else None
        self._facility_provider = provider if isinstance(provider, FacilityProvider) else None
        self._facility_cache_provider = provider if isinstance(provider, FacilityCacheProvider) else None
        self._command_timeout_seconds = command_timeout_seconds
        granularity = getattr(provider, "interval_granularity_ms", 1)
        _positive_integer(granularity, "Provider interval granularity", 3_600_000)
        self._interval_granularity_ms = granularity
        self._confirm_timeout = self._provider_limit(
            "max_command_timeout_seconds", _DEFAULT_PROVIDER_COMMAND_TIMEOUT) + _PROVIDER_GRACE_SECONDS
        self._facility_timeout = self._provider_limit(
            "facility_timeout_seconds", _DEFAULT_PROVIDER_FACILITY_TIMEOUT) + _FACILITY_GRACE_SECONDS
        self._lock = RLock()
        self._provider_lock = Lock()
        self._consumers: dict[str, _Consumer] = {}
        self._by_signal: dict[Signal, list[_Subscription]] = {}
        self._readings: dict[Signal, Reading] = {}
        self._pending: dict[str, _Pending] = {}
        self._applied_demand: tuple[Demand, ...] = ()
        self._applied_signals: frozenset[Signal] = frozenset()
        # Signals whose last subscriber left while the provider still serves them. Their cached
        # reading and queued refreshes are kept until the next pump retires the demand, so a
        # module that subscribes again before then is treated as a late subscriber.
        self._retiring: set[Signal] = set()
        self._demand_generation = 0
        self._demand_cache: tuple[Demand, ...] = ()
        self._demand_cache_generation = -1
        self._demand_intervals: dict[Signal, int | None] = {}
        self._cleanup_required = False
        self._write_blocked: str | None = None
        self._input_catalog = InputCatalog(None, 0, InputCatalogState.UNAVAILABLE)
        self._discarded_input_generation = 0
        self._input_descriptors = {}
        self._input_parameters = {}
        self._input_requests = {}
        self._client_data_areas: dict[str, ClientDataArea] = {}
        self._client_refreshes: dict[Signal, None] = {}
        self._state_refreshes: set[Signal] = set()
        self._facility_reads: dict[FacilityQuery | SimulatorRead, _FacilityRead] = {}
        self._facility_watches: dict[FacilityKind, _FacilityWatch] = {}
        self._applied_facility_watches: dict[FacilityKind, str] = {}
        self._stale_sequence = count()
        self._lifecycle: dict[Signal, tuple[Signal, ...]] = {}
        for event_name, state_names in _LIFECYCLE_STATES.items():
            event = SYSTEM_EVENTS.get(event_name)
            states = tuple(SYSTEM_STATES[name] for name in state_names)
            if event is not None and provider.supports_signal(event):
                self._lifecycle[event] = states
        self._connection = Connection(
            ConnectionState.DISCONNECTED, provider.source, None, datetime.now(timezone.utc),
        )

    def _now(self) -> float:
        return self._clock() if self._clock is not None else monotonic()

    def _provider_limit(self, name, default):
        value = getattr(self._provider, name, default)
        if type(value) not in (int, float) or not 0 < value <= 600:
            raise ValueError(f"The provider's {name} must be a positive number of seconds up to 600.")
        return float(value)

    def register_consumer(self, *, control_groups=(), diagnostics=False, queue_size=256) -> ConsumerAPI:
        """Host-only registration. Consumers receive the returned API, not the host."""
        _positive_integer(queue_size, "Queue size", 4096)
        groups = frozenset(control_groups)
        if not groups <= known_control_groups():
            raise CoreRequestError("Unknown control group.")
        with self._lock:
            if len(self._consumers) >= _MAX_CONSUMERS:
                raise CoreRequestError("Core consumer limit reached.")
            consumer_id = uuid4().hex
            self._consumers[consumer_id] = _Consumer(consumer_id, groups, diagnostics, queue_size)
            return ConsumerAPI(self, consumer_id, self._connection)

    def register_client_data(self, area: ClientDataArea) -> None:
        """Host-only declaration. Does not create or alter a simulator area."""
        if not isinstance(area, ClientDataArea):
            raise CoreRequestError("Supply a ClientDataArea declaration.")
        if self._client_data_provider is None:
            raise CoreRequestError("The selected provider does not expose Client Data.")
        with self._lock:
            existing = self._client_data_areas.get(area.key)
            if existing is not None and existing != area:
                raise CoreRequestError("This Client Data name already has a different declaration.")
            self._client_data_areas[area.key] = area

    # -- Session lifecycle ---------------------------------------------------------------------

    def connect(self) -> Connection:
        """Open a provider session. Retries an unresolved cleanup from the previous session first."""
        with self._provider_lock:
            with self._lock:
                if self._connection.state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
                    return self._connection
            if self._cleanup_required:
                try:
                    self._provider.disconnect()
                except Exception as error:  # noqa: BLE001 - any provider failure keeps cleanup unresolved
                    log.warning("Provider cleanup is still unresolved: %r", error)
                    with self._lock:
                        self._publish_connection(
                            ConnectionState.ERROR, None,
                            f"Provider cleanup is still unresolved, so a new session cannot start: {error}",
                        )
                    return self._connection
                self._cleanup_required = False
            with self._lock:
                session = uuid4().hex
                self._publish_connection(ConnectionState.CONNECTING, session)
                self._cleanup_required = True
            try:
                self._provider.connect(session)
            except ProviderError as error:
                log.warning("Provider connect failed: %s", error)
                self._end_session(str(error), failed=True)
            except Exception as error:  # noqa: BLE001 - never leave Core stuck in CONNECTING
                log.exception("Provider connect failed unexpectedly")
                self._end_session(f"The provider failed unexpectedly while connecting: {error!r}", failed=True)
            except BaseException:
                self._end_session("Connecting was interrupted.", failed=True)
                raise
            else:
                with self._lock:
                    self._set_applied_demand(())
                    self._write_blocked = None
                    self._publish_connection(ConnectionState.CONNECTED, session)
            return self._connection

    def disconnect(self) -> Connection:
        with self._provider_lock:
            if self._cleanup_required:
                self._end_session("Disconnected by the host.")
            return self._connection

    def response_times(self) -> tuple[float, ...]:
        """Host diagnostics only. A call may block while the SDK measures latency."""
        with self._provider_lock:
            diagnostics = getattr(self._provider, "response_times", None)
            if diagnostics is None:
                raise CoreRequestError("The selected provider does not support response timing.")
            return diagnostics()

    def pump(self, *, limit: int = 256) -> None:
        """Reconcile demand, receive updates and dispatch one queued command."""
        _positive_integer(limit, "Batch size", 4096)
        with self._provider_lock:
            with self._lock:
                if self._connection.state is not ConnectionState.CONNECTED:
                    return
                demand = self._desired_demand()
                if self._retiring:
                    self._retire_signals()
                requests = tuple(self._input_requests)
                self._input_requests.clear()
                client_refreshes = tuple(self._client_refreshes)
                self._client_refreshes.clear()
                state_refreshes = tuple(self._state_refreshes)
                self._state_refreshes.clear()
                now = self._now()
                facility_reads = tuple(
                    (query, read) for query, read in self._facility_reads.items() if not read.dispatched
                )
                for _, read in facility_reads:
                    read.dispatched = True
                    read.deadline = now + self._facility_timeout
                watches = {kind: watch.id for kind, watch in self._facility_watches.items()}
            try:
                if demand is not self._applied_demand:
                    if demand != self._applied_demand:
                        self._provider.replace_demand(demand)
                    self._set_applied_demand(demand)
                for action, input_id in requests:
                    if action == "catalog":
                        self._input_provider.request_input_catalog()
                    elif action == "parameters":
                        self._input_provider.request_input_parameters(input_id)
                    else:
                        self._input_provider.request_input_value(input_id)
                for signal in client_refreshes:
                    self._client_data_provider.request_client_data_value(signal)
                for signal in state_refreshes:
                    self._system_state_provider.request_system_state(signal)
                for query, read in facility_reads:
                    self._facility_provider.request_facility(read.result.id, query)
                if watches != self._applied_facility_watches:
                    self._facility_cache_provider.replace_facility_watches(watches)
                    self._applied_facility_watches = watches
                updates = self._provider.drain(limit)
            except ProviderError as error:
                log.warning("Provider failed during pump: %s", error)
                self._end_session(str(error), failed=True)
                return
            except Exception as error:  # noqa: BLE001 - report the fault as a session error, never as CONNECTED
                log.exception("Provider failed unexpectedly during pump")
                self._end_session(f"The provider failed unexpectedly: {error!r}", failed=True)
                return
            except BaseException:
                self._end_session("Core was interrupted while talking to the provider.", failed=True)
                raise
            for update in updates:
                if getattr(update, "session_id", None) != self._connection.session_id:
                    continue
                if isinstance(update, Disconnect):
                    self._end_session(update.reason)
                    return
                with self._lock:
                    try:
                        self._receive(update)
                    except Exception:  # noqa: BLE001 - one bad update must not end the shared session
                        log.exception("Core skipped a provider update it could not process: %r", type(update).__name__)
            with self._lock:
                self._expire_facilities(self._now())
            self._dispatch_next()

    # -- Lookup helpers ------------------------------------------------------------------------

    def _consumer(self, consumer_id):
        try:
            return self._consumers[consumer_id]
        except KeyError as error:
            raise CoreRequestError("Consumer is closed or unknown.") from error

    def _discover(self, consumer_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            active = self._connection.state is ConnectionState.CONNECTED
            entries = [
                (entry.capability, self._provider.supports_signal(entry.signal))
                for entry in READS.values()
            ]
            entries.extend(
                (entry.capability, self._provider.supports_command(entry.operation))
                for entry in COMMANDS.values()
                if entry.capability.control_group in consumer.groups
            )
            entries.extend(
                (entry, self._provider.supports_command(entry.id))
                for entry in _SDK_COMMANDS.values() if entry.control_group in consumer.groups
            )
            return Discovery(API_VERSION, self._provider.source, tuple(
                CapabilityStatus(capability, supported, active)
                for capability, supported in entries
            ))

    def _connection_for(self, consumer_id):
        with self._lock:
            self._consumer(consumer_id)
            return self._connection

    # -- Facilities ----------------------------------------------------------------------------

    def _request_facility(self, consumer_id, query):
        with self._lock:
            consumer = self._consumer(consumer_id)
            read = self._facility_reads.get(query)
            if read is not None:
                read.consumers.add(consumer_id)
                return read.result
            if isinstance(query, SimulatorRead):
                result = SimulatorResult(uuid4().hex, SimulatorReadState.PENDING, self._provider.source,
                                         self._connection.session_id)
            else:
                result = FacilityResult(uuid4().hex, FacilityState.PENDING, self._provider.source,
                                        self._connection.session_id)
            reason = ""
            if self._facility_provider is None:
                reason = "The provider does not support facility queries."
            elif self._connection.state is not ConnectionState.CONNECTED:
                reason = "A connected simulator session is required."
            elif sum(consumer_id in item.consumers for item in self._facility_reads.values()) >= _MAX_FACILITY_PER_CONSUMER:
                reason = "This module already has too many facility queries outstanding."
            elif len(self._facility_reads) >= _MAX_FACILITY_REQUESTS:
                reason = "Too many facility queries are outstanding."
            if reason:
                state = SimulatorReadState.UNAVAILABLE if isinstance(query, SimulatorRead) else FacilityState.UNAVAILABLE
                result = replace(result, state=state,
                                 received_at=datetime.now(timezone.utc), detail=reason)
                self._remember_facility(consumer, result)
            else:
                self._facility_reads[query] = _FacilityRead(result, {consumer_id})
            return result

    def _request_dialog_notifications(self, consumer_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            if not any(item.signal.client_event and item.event_enabled for item in consumer.subscriptions.values()):
                raise CoreRequestError("An enabled client event subscription is required.")
            return self._request_facility(consumer_id, SimulatorRead("dialog"))

    def _facility_result(self, consumer_id, request_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            for read in self._facility_reads.values():
                if read.result.id == request_id and consumer_id in read.consumers:
                    return read.result
            return consumer.facility_results.get(request_id)

    @staticmethod
    def _remember_facility(consumer, result):
        consumer.facility_results[result.id] = result
        if len(consumer.facility_results) > _MAX_FACILITY_RESULTS:
            del consumer.facility_results[next(iter(consumer.facility_results))]
        consumer.offer(result)

    def _finish_facility(self, query, state, data=None, detail=""):
        read = self._facility_reads.pop(query)
        if isinstance(query, SimulatorRead):
            state = SimulatorReadState(state.value)
        result = replace(read.result, state=state, data=data, detail=detail,
                         received_at=datetime.now(timezone.utc))
        for consumer_id in read.consumers:
            consumer = self._consumers.get(consumer_id)
            if consumer is not None:
                self._remember_facility(consumer, result)

    def _fail_facilities(self, reason):
        for query in tuple(self._facility_reads):
            self._finish_facility(query, FacilityState.UNAVAILABLE, detail=reason)

    def _expire_facilities(self, now):
        for query, read in tuple(self._facility_reads.items()):
            if read.dispatched and read.deadline is not None and read.deadline <= now:
                log.warning("A facility or simulator read got no provider answer in time")
                self._finish_facility(query, FacilityState.UNAVAILABLE,
                                      detail="The provider gave no answer to this query in time.")

    def _subscribe_facility_cache(self, consumer_id, kind):
        if not isinstance(kind, FacilityKind):
            raise CoreRequestError("Select a FacilityKind for the cache subscription.")
        with self._lock:
            consumer = self._consumer(consumer_id)
            if self._facility_cache_provider is None:
                raise CoreRequestError("The provider does not support facility cache updates.")
            for identifier, registered in consumer.facility_subscriptions.items():
                if registered is kind:
                    return FacilityCacheSubscription(identifier, kind)
            if kind not in self._facility_watches:
                self._facility_watches[kind] = _FacilityWatch(uuid4().hex)
            watch = self._facility_watches[kind]
            subscription = FacilityCacheSubscription(uuid4().hex, kind)
            consumer.facility_subscriptions[subscription.id] = kind
            watch.subscribers[subscription.id] = consumer_id
            snapshot = self._cache_snapshot(subscription.id, kind, watch)
            if snapshot is not None:
                consumer.offer(snapshot)
            return subscription

    def _facility_cache_snapshot(self, consumer_id, subscription_id):
        with self._lock:
            kind = self._consumer(consumer_id).facility_subscriptions.get(subscription_id)
            if kind is None:
                raise CoreRequestError("An active facility cache subscription is required.")
            return self._cache_snapshot(subscription_id, kind, self._facility_watches[kind])

    def _cache_snapshot(self, subscription_id, kind, watch):
        if watch.received_at is None:
            return None
        change = FacilityChange.UNAVAILABLE if watch.error else FacilityChange.SNAPSHOT
        return FacilityCacheUpdate(
            subscription_id, kind, change, self._provider.source, watch.session_id,
            watch.received_at, watch.snapshot_entries(), watch.error,
        )

    def _remove_facility_subscription(self, consumer, subscription_id):
        kind = consumer.facility_subscriptions.pop(subscription_id, None)
        if kind is not None:
            watch = self._facility_watches[kind]
            del watch.subscribers[subscription_id]
            if not watch.subscribers:
                del self._facility_watches[kind]
            consumer.remove_control(
                lambda item: isinstance(item, FacilityCacheUpdate) and item.subscription_id == subscription_id
            )

    def _receive_facility_cache(self, update):
        watch = self._facility_watches.get(update.kind)
        if watch is None or watch.id != update.watch_id:
            return
        if update.change is FacilityChange.UNAVAILABLE:
            watch.records = None
            watch.error = update.detail
        elif update.change is FacilityChange.SNAPSHOT:
            watch.records = {self._facility_key(record): record for record in update.entries}
            watch.error = ""
        elif watch.records is None:
            return
        elif update.change is FacilityChange.ADDED:
            watch.records.update((self._facility_key(record), record) for record in update.entries)
        else:
            for record in update.entries:
                watch.records.pop(self._facility_key(record), None)
        watch.entries = None
        watch.received_at = datetime.now(timezone.utc)
        watch.session_id = update.session_id
        for subscription, consumer_id in watch.subscribers.items():
            self._consumers[consumer_id].offer(FacilityCacheUpdate(
                subscription, update.kind, update.change, self._provider.source, update.session_id,
                watch.received_at, update.entries, update.detail,
            ))

    @staticmethod
    def _facility_key(record):
        return record.icao, record.region, record.latitude_deg, record.longitude_deg

    def _invalidate_facility_watches(self, detail):
        for consumer in self._consumers.values():
            consumer.remove_control(lambda item: isinstance(item, FacilityCacheUpdate))
        for kind, watch in self._facility_watches.items():
            self._receive_facility_cache(FacilityCacheReply(
                self._connection.session_id, watch.id, kind, FacilityChange.UNAVAILABLE, detail=detail,
            ))
            watch.id = uuid4().hex

    # -- Subscriptions -------------------------------------------------------------------------

    def _subscribe_named(self, consumer_id, capability_id, interval_ms, max_age_ms):
        entry = READS.get(capability_id)
        if entry is None:
            raise CoreRequestError("Unknown readable capability or event.")
        with self._lock:
            return self._add_subscription(self._consumer(consumer_id), entry, interval_ms, max_age_ms)

    def _subscribe_client_event(self, consumer_id, name):
        entry = ReadMapping(
            Capability(f"sdk.client_event:{name}", Kind.EVENT, ValueType.CLIENT_EVENT, None, "SDK client event."),
            Signal(name, None, ValueType.CLIENT_EVENT, event=True, client_event=True),
        )
        with self._lock:
            return self._add_subscription(self._consumer(consumer_id), entry, None, None)

    def _subscribe_controller(self, consumer_id, control):
        if not isinstance(control, ControllerInput):
            raise CoreRequestError("Supply a ControllerInput.")
        signal = Signal(control.sdk_name, None, ValueType.HARDWARE_INPUT, event=True, hardware_input=control)
        entry = ReadMapping(Capability(f"sdk.input:{control.sdk_name}", Kind.EVENT, ValueType.HARDWARE_INPUT,
                                       None, "Mapped simulator controller input."), signal)
        with self._lock:
            return self._add_subscription(self._consumer(consumer_id), entry, None, None)

    def _subscribe_snapshot(self, consumer_id, fields, period, every, changed_only):
        if not isinstance(fields, tuple) or not 1 <= len(fields) <= _MAX_SNAPSHOT_FIELDS:
            raise CoreRequestError(f"Supply a tuple of 1 to {_MAX_SNAPSHOT_FIELDS} SimVar fields.")
        if not isinstance(period, SnapshotPeriod):
            raise CoreRequestError("Select a SnapshotPeriod.")
        _positive_integer(every, "Snapshot period count", 3600 if period is SnapshotPeriod.SECOND else 100_000)
        if type(changed_only) is not bool:
            raise CoreRequestError("The changed-only option must be Boolean.")
        signals = tuple(_sdk_simvar_signal(variable) for variable in fields)
        if len(set(signals)) != len(signals):
            raise CoreRequestError("Each snapshot field may appear only once.")
        spec = SnapshotSpec(signals, period, every, changed_only)
        signal = Signal("SNAPSHOT", None, ValueType.SNAPSHOT, snapshot=spec)
        names = tuple(variable.sdk_name for variable in fields)
        digest = blake2b(repr(_signal_key(signal)).encode("utf-8"), digest_size=8).hexdigest()
        entry = ReadMapping(Capability(
            f"sdk.snapshot:{digest}", Kind.VALUE, ValueType.SNAPSHOT, None,
            "Consumer-defined snapshot of " + ", ".join(names),
        ), signal)
        with self._lock:
            return self._add_subscription(self._consumer(consumer_id), entry, None, None)

    def _aircraft_inputs(self, consumer_id):
        with self._lock:
            self._consumer(consumer_id)
            return self._input_catalog

    def _input_parameters_for(self, consumer_id, input_id):
        with self._lock:
            self._consumer(consumer_id)
            self._input_descriptor(input_id)
            return self._input_parameters.get(input_id)

    def _input_descriptor(self, input_id):
        if not isinstance(input_id, InputId):
            raise CoreRequestError("Use the InputId returned by aircraft discovery.")
        entry = self._input_descriptors.get(input_id)
        if entry is None:
            raise CoreRequestError("Discover this input on the currently loaded aircraft first.")
        return entry

    def _queue_input_request(self, consumer_id, action, input_id=None):
        with self._lock:
            self._consumer(consumer_id)
            if self._input_provider is None:
                raise CoreRequestError("The selected provider does not expose aircraft Input Events.")
            if self._connection.state is not ConnectionState.CONNECTED:
                raise CoreRequestError("A connected simulator session is required.")
            if action != "catalog":
                self._input_descriptor(input_id)
            request = (action, input_id)
            if len(self._input_requests) >= 128 and request not in self._input_requests:
                raise CoreRequestError("Aircraft input request queue is full.")
            self._input_requests[request] = None
            if action == "parameters":
                self._input_parameters.pop(input_id, None)

    def _subscribe_input(self, consumer_id, input_id, max_age_ms):
        with self._lock:
            consumer = self._consumer(consumer_id)
            descriptor = self._input_descriptor(input_id)
            if descriptor.value_type is None:
                raise CoreRequestError("The provider returned an unsupported aircraft input value type.")
            signal = Signal(descriptor.name, None, descriptor.value_type, input_id=input_id)
            entry = ReadMapping(
                Capability(f"aircraft.input:{input_id.hash:016x}", Kind.VALUE,
                           descriptor.value_type, None, descriptor.name),
                signal,
            )
            return self._add_subscription(consumer, entry, None, max_age_ms)

    def _refresh_input(self, consumer_id, subscription_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            entry = consumer.subscriptions.get(subscription_id)
            if entry is None or entry.signal.input_id is None:
                ended = consumer.ended.get(subscription_id)
                raise CoreRequestError(ended[0] if ended else "An active aircraft input subscription is required.")
            self._queue_input_request(consumer_id, "value", entry.signal.input_id)

    def _client_data_areas_for(self, consumer_id):
        with self._lock:
            self._consumer(consumer_id)
            return tuple(self._client_data_areas.values())

    def _require_client_data_region(self, region):
        if (not isinstance(region, ClientDataRegion)
                or self._client_data_areas.get(region.area.key) != region.area):
            raise CoreRequestError("Use a region from a host-declared Client Data area.")

    def _subscribe_client_data(self, consumer_id, region, mode, max_age_ms, changed_only=False):
        with self._lock:
            consumer = self._consumer(consumer_id)
            self._require_client_data_region(region)
            if not isinstance(mode, ClientDataMode):
                raise CoreRequestError("Select a ClientDataMode.")
            if type(changed_only) is not bool:
                raise CoreRequestError("changed_only must be True or False.")
            if changed_only and mode not in (ClientDataMode.SECOND, ClientDataMode.VISUAL_FRAME):
                raise CoreRequestError("changed_only applies only to the SECOND and VISUAL_FRAME Client Data modes.")
            signal = Signal(region.area.name, None, ValueType.BYTES, client_data_region=region, client_data_mode=mode,
                            client_data_changed_only=changed_only)
            capability_id = region.capability_id if mode is ClientDataMode.ON_SET else f"{region.capability_id}:{mode.value}"
            if changed_only:
                capability_id += ":changed"
            entry = ReadMapping(
                Capability(capability_id, Kind.VALUE, ValueType.BYTES, None, region.area.name),
                signal,
            )
            return self._add_subscription(consumer, entry, None, max_age_ms)

    def _refresh_client_data(self, consumer_id, subscription_id):
        with self._lock:
            entry = self._consumer(consumer_id).subscriptions.get(subscription_id)
            if entry is None or entry.signal.client_data_region is None:
                raise CoreRequestError("An active Client Data subscription is required.")
            if self._connection.state is not ConnectionState.CONNECTED:
                raise CoreRequestError("A connected simulator session is required.")
            if len(self._client_refreshes) >= 128 and entry.signal not in self._client_refreshes:
                raise CoreRequestError("Client Data read request queue is full.")
            self._client_refreshes[entry.signal] = None

    def _refresh_system_state(self, consumer_id, subscription_id):
        with self._lock:
            entry = self._consumer(consumer_id).subscriptions.get(subscription_id)
            if entry is None or not entry.signal.system_state:
                raise CoreRequestError("An active system state subscription is required.")
            if self._connection.state is not ConnectionState.CONNECTED:
                raise CoreRequestError("A connected simulator session is required.")
            self._state_refreshes.add(entry.signal)

    def _set_event_enabled(self, consumer_id, subscription_id, enabled):
        if type(enabled) is not bool:
            raise CoreRequestError("Event delivery state must be Boolean.")
        with self._lock:
            consumer = self._consumer(consumer_id)
            entry = consumer.subscriptions.get(subscription_id)
            if entry is None or not entry.signal.event:
                raise CoreRequestError("An active event subscription is required.")
            if entry.event_enabled != enabled:
                entry.event_enabled = enabled
                self._touch_demand()
            if not enabled and entry.queued:
                consumer.remove_data(lambda item: item.entry is entry)

    def _subscribe_simvar(self, consumer_id, name, unit, value_type, interval_ms, max_age_ms):
        with self._lock:
            consumer = self._consumer(consumer_id)
            if not consumer.diagnostics:
                raise CoreRequestError("Raw SimVar requests require trusted diagnostics access.")
            if not isinstance(name, str) or not name.strip() or len(name) > 256 or "\0" in name:
                raise CoreRequestError("Supply a valid SDK variable name.")
            if not isinstance(value_type, ValueType) or value_type not in (
                ValueType.BOOLEAN, ValueType.INTEGER, ValueType.NUMBER, ValueType.TEXT,
            ):
                raise CoreRequestError("Supply a Core value type.")
            if value_type is ValueType.TEXT:
                if unit is not None:
                    raise CoreRequestError("A string SimVar has no SDK unit.")
            elif not isinstance(unit, str) or not unit.strip() or len(unit) > 80 or "\0" in unit:
                raise CoreRequestError("A numeric or Boolean SimVar requires an SDK unit.")
            signal = Signal(name, unit, value_type)
            entry = ReadMapping(
                Capability(
                    f"diagnostic.simvar:{signal.name}:{signal.unit}:{value_type.value}",
                    Kind.VALUE, value_type, signal.unit, "Unvalidated diagnostic SimVar.",
                ),
                signal,
            )
            return self._add_subscription(consumer, entry, interval_ms, max_age_ms)

    def _subscribe_sdk_simvar(self, consumer_id, variable, interval_ms, max_age_ms):
        signal = _sdk_simvar_signal(variable)
        encoding = variable.format.value if variable.format else variable.value_type.value
        entry = ReadMapping(Capability(
            f"sdk.simvar:{signal.name}:{signal.unit}:{encoding}",
            Kind.VALUE, signal.value_type, signal.unit, "Consumer-supplied SDK SimVar.",
        ), signal)
        with self._lock:
            return self._add_subscription(self._consumer(consumer_id), entry, interval_ms, max_age_ms)

    def _add_subscription(self, consumer, entry, interval_ms, max_age_ms):
        if len(consumer.subscriptions) >= _MAX_SUBSCRIPTIONS:
            raise CoreRequestError("Consumer subscription limit reached.")
        signal = entry.signal
        if not self._provider.supports_signal(signal):
            raise CoreRequestError("The provider does not support this capability.")
        if signal.system_state and interval_ms is not None:
            raise CoreRequestError("System state uses one-shot requests, without a polling interval.")
        if signal.snapshot is not None:
            spec = signal.snapshot
            interval_ms = spec.every * 1000 if spec.period is SnapshotPeriod.SECOND else None
            max_age_ms = None
        elif signal.input_id is not None or signal.client_data_region is not None or signal.system_state:
            if max_age_ms is not None:
                _positive_integer(max_age_ms, "Maximum age", 86_400_000)
        elif entry.capability.kind is Kind.EVENT:
            if interval_ms is not None or max_age_ms is not None:
                raise CoreRequestError("Events do not accept cadence or freshness options.")
        else:
            if interval_ms is not None:
                _positive_integer(interval_ms, "Interval", 3_600_000)
            interval_ms = max(interval_ms or 1000, self._provider.minimum_interval_ms)
            # Report the cadence the provider can really deliver: SimConnect periods are
            # whole seconds, so 1500 ms is served (and reported) as 2000 ms.
            granularity = self._interval_granularity_ms
            interval_ms = min(-(-interval_ms // granularity) * granularity, 3_600_000)
            if max_age_ms is None:
                max_age_ms = 3 * interval_ms
            else:
                _positive_integer(max_age_ms, "Maximum age", 86_400_000)
        # A signal whose last subscriber left before the provider was told is still served: the
        # provider will not resend a one-shot or on-change value, so treat it as a late subscriber.
        already_demanded = signal in self._by_signal or signal in self._retiring
        self._retiring.discard(signal)
        public = Subscription(uuid4().hex, entry.capability, interval_ms)
        subscription = _Subscription(public, signal, max_age_ms, consumer)
        consumer.subscriptions[public.id] = subscription
        self._index_add(subscription)
        reading = self._readings.get(signal)
        if (signal.system_state and already_demanded and self._system_state_provider is not None
                and self._connection.state is ConnectionState.CONNECTED):
            # A late subscriber asks for a fresh reply instead of inheriting an old one. Until
            # that reply arrives, the inherited value is shown as STALE, never as CURRENT.
            self._state_refreshes.add(signal)
            subscription.awaiting_refresh = reading
        if reading is not None and reading.error is None and max_age_ms is not None and signal.snapshot is None:
            # A feed that goes quiet must be reported to a late subscriber too.
            self._arm_stale(consumer, subscription, reading.received_monotonic + max_age_ms / 1000)
        return public

    def _index_add(self, entry):
        subscribers = self._by_signal.get(entry.signal)
        if subscribers is None:
            self._by_signal[entry.signal] = [entry]
        else:
            subscribers.append(entry)
        self._touch_demand()

    def _index_remove(self, entry):
        subscribers = self._by_signal.get(entry.signal)
        if subscribers is not None:
            for index, item in enumerate(subscribers):
                if item is entry:
                    del subscribers[index]
                    break
            if not subscribers:
                del self._by_signal[entry.signal]
                if entry.signal in self._applied_signals:
                    self._retiring.add(entry.signal)
                else:
                    self._forget_signal(entry.signal)
        self._touch_demand()

    def _forget_signal(self, signal):
        self._readings.pop(signal, None)
        self._client_refreshes.pop(signal, None)
        self._state_refreshes.discard(signal)

    def _retire_signals(self):
        # Called with the lock held, once the demand without these signals is about to be applied.
        for signal in self._retiring:
            if signal not in self._by_signal:
                self._forget_signal(signal)
        self._retiring.clear()

    def _set_applied_demand(self, demand):
        if demand is not self._applied_demand:
            self._applied_demand = demand
            self._applied_signals = frozenset(item.signal for item in demand)

    def _touch_demand(self):
        self._demand_generation += 1

    def _desired_demand(self):
        if self._demand_cache_generation == self._demand_generation:
            return self._demand_cache
        merged = {}
        for signal, subscribers in self._by_signal.items():
            intervals = [item.public.interval_ms for item in subscribers if item.public.interval_ms is not None]
            enabled = not signal.event or any(item.event_enabled for item in subscribers)
            merged[signal] = (min(intervals) if intervals else None, enabled)
        for event, states in self._lifecycle.items():
            # Core's own need: keep the lifecycle event that invalidates a subscribed state.
            if any(state in self._by_signal for state in states):
                interval, _ = merged.get(event, (None, True))
                merged[event] = (interval, True)
        demand = tuple(
            Demand(signal, interval, enabled)
            for signal, (interval, enabled) in sorted(merged.items(), key=lambda pair: _signal_key(pair[0]))
        )
        self._demand_cache = demand
        self._demand_cache_generation = self._demand_generation
        self._demand_intervals = {item.signal: item.interval_ms for item in demand}
        return demand

    def _subscription_for(self, consumer_id, subscription_id):
        consumer = self._consumer(consumer_id)
        entry = consumer.subscriptions.get(subscription_id)
        if entry is None and subscription_id in consumer.ended:
            raise CoreRequestError(consumer.ended[subscription_id][0])
        return entry

    def _latest(self, consumer_id, subscription_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            ended = consumer.ended.get(subscription_id)
            if ended is not None and subscription_id not in consumer.subscriptions:
                # Core ended this subscription itself (A86): say why, as an unavailable value.
                reason, capability = ended
                return Observation(
                    capability.id, None, capability.unit, Quality.UNAVAILABLE, self._provider.source,
                    self._connection.session_id, None, reason=reason,
                )
            entry = self._subscription_for(consumer_id, subscription_id)
            if entry is None or entry.public.capability.kind is not Kind.VALUE:
                raise CoreRequestError("An active value subscription is required.")
            if entry.signal.snapshot is not None:
                raise CoreRequestError("Use latest_snapshot() for a snapshot subscription.")
            capability = entry.public.capability
            reading = self._readings.get(entry.signal)
            if reading is None or self._connection.state is not ConnectionState.CONNECTED:
                reason = (
                    "Waiting for current-session data."
                    if self._connection.state is ConnectionState.CONNECTED
                    else "There is no connected provider session."
                )
                return Observation(
                    capability.id, None, capability.unit, Quality.UNAVAILABLE, self._provider.source,
                    self._connection.session_id, None, reason=reason,
                )
            return self._observation(entry, reading, self._now())

    def _latest_snapshot(self, consumer_id, subscription_id):
        with self._lock:
            entry = self._subscription_for(consumer_id, subscription_id)
            if entry is None or entry.signal.snapshot is None:
                raise CoreRequestError("An active snapshot subscription is required.")
            reading = self._readings.get(entry.signal)
            if (reading is None or reading.error is not None
                    or self._connection.state is not ConnectionState.CONNECTED):
                return None
            snapshot = self._snapshot(entry, reading)
            return snapshot if isinstance(snapshot, Snapshot) else None

    def _observation(self, entry, reading, now):
        capability = entry.public.capability
        try:
            if reading.error is not None:
                raise ValueError(reading.error)
            value = _decode_reading(capability, entry.signal, reading.value)
        except ValueError as error:
            return Observation(
                capability.id, None, capability.unit, Quality.UNAVAILABLE, self._provider.source,
                reading.session_id, reading.received_at, reading.sample_time, str(error),
            )
        reason = None
        if entry.awaiting_refresh is not None and entry.awaiting_refresh is reading:
            reason = "Received before this subscription. Waiting for a fresh reply from the simulator."
        elif entry.max_age_ms is not None and (now - reading.received_monotonic) * 1000 > entry.max_age_ms:
            reason = "Receipt age exceeds the subscription limit."
        return Observation(
            capability.id, value, capability.unit, Quality.STALE if reason else Quality.CURRENT,
            self._provider.source, reading.session_id, reading.received_at, reading.sample_time, reason,
        )

    def _snapshot(self, entry, reading):
        """Build the consumer's Snapshot, or a SignalError if the payload is malformed."""
        spec = entry.signal.snapshot
        frame = reading.value
        capability_id = entry.public.capability.id
        if not isinstance(frame, SnapshotFrame) or not isinstance(frame.values, tuple) \
                or len(frame.values) != len(spec.fields):
            return SignalError(1, "Provider supplied an invalid snapshot.", capability_id, entry.public.id)
        values = []
        for field_signal, value in zip(spec.fields, frame.values):
            try:
                values.append(None if value is None else decode_value(field_signal.value_type, value))
            except ValueError:
                values.append(None)
        sample_time = reading.sample_time if isinstance(reading.sample_time, datetime) else None
        if sample_time is None:
            sample_time = simulator_utc(frame.absolute_time_s)
        return Snapshot(
            entry.public.id, tuple(item.name for item in spec.fields), tuple(values),
            _finite(frame.simulation_time_s), _finite(frame.absolute_time_s), _finite(frame.simulation_rate),
            self._provider.source, reading.session_id, reading.received_at, sample_time,
        )

    def _poll(self, consumer_id, limit):
        _positive_integer(limit, "Batch size", 4096)
        with self._lock:
            consumer = self._consumer(consumer_id)
            now = self._now()
            batch = []
            if consumer.control_dropped:
                batch.append(Gap(
                    consumer.control_dropped,
                    "Consumer status queue lost updates. Read connection() and command_result() for the current state.",
                ))
                consumer.control_dropped = 0
            if consumer.dropped:
                batch.append(Gap(consumer.dropped, "Consumer update queue lost data."))
                consumer.dropped = 0
            stale = []
            for entry, reading in self._stale_candidates(consumer, now):
                observation = self._observation(entry, reading, now)
                if observation.quality is not Quality.STALE:
                    # Undecodable (UNAVAILABLE): nothing to report until a new reading arrives.
                    entry.stale_reported_at = reading.received_monotonic
                    continue
                stale.append((entry, reading, observation))
            # Only reports that can be sent now hold back room, and never the whole batch: a short
            # batch tells consumers that the queue is drained.
            ready = sum(1 for entry, _, _ in stale if entry.queued == 0)
            reserve = min(ready, limit // 4)
            data, control = consumer.data, consumer.control
            while (data or control) and len(batch) < limit - reserve:
                if control and (not data or control[0][0] < data[0][0]):
                    batch.append(control.popleft()[1])
                    continue
                update = data.popleft()[1]
                update.entry.queued -= 1
                batch.append(self._delivered(update))
            for entry, reading, observation in stale:
                if entry.queued == 0 and len(batch) < limit:
                    entry.stale_reported_at = reading.received_monotonic
                    batch.append(observation)
                    continue
                # Older samples of this subscription are still queued, or there was no room:
                # keep the original deadline so the next poll reports it.
                self._arm_stale(consumer, entry, reading.received_monotonic + entry.max_age_ms / 1000)
            return tuple(batch)

    def _delivered(self, update):
        if isinstance(update, _QueuedObservation):
            # A queued sample is judged at receipt. A quiet feed is reported by the stale scan.
            return self._observation(update.entry, update.reading, update.reading.received_monotonic)
        return update.update

    def _stale_candidates(self, consumer, now):
        heap = consumer.stale_heap
        due = []
        rearm = []
        while heap and heap[0][0] < now:
            _, _, entry = heapq.heappop(heap)
            entry.in_stale_heap = False
            if consumer.subscriptions.get(entry.public.id) is not entry or entry.max_age_ms is None:
                continue
            reading = self._readings.get(entry.signal)
            if reading is None or reading.error is not None or entry.stale_reported_at == reading.received_monotonic:
                continue
            if (now - reading.received_monotonic) * 1000 > entry.max_age_ms:
                due.append((entry, reading))
            else:
                rearm.append((entry, reading.received_monotonic + entry.max_age_ms / 1000))
        for entry, deadline in rearm:
            self._arm_stale(consumer, entry, deadline)
        return due

    def _arm_stale(self, consumer, entry, deadline):
        if not entry.in_stale_heap:
            entry.in_stale_heap = True
            heapq.heappush(consumer.stale_heap, (deadline, next(self._stale_sequence), entry))

    def _unsubscribe(self, consumer_id, subscription_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            consumer.ended.pop(subscription_id, None)
            self._remove_facility_subscription(consumer, subscription_id)
            existing = consumer.subscriptions.pop(subscription_id, None)
            if existing is not None:
                if existing.queued:
                    consumer.remove_data(lambda item: item.entry is existing)
                self._index_remove(existing)

    def _close_consumer(self, consumer_id):
        with self._lock:
            consumer = self._consumers.pop(consumer_id, None)
            if consumer is None:
                return
            for subscription in tuple(consumer.facility_subscriptions):
                self._remove_facility_subscription(consumer, subscription)
            for entry in consumer.subscriptions.values():
                self._index_remove(entry)
            consumer.subscriptions.clear()
            for item in tuple(self._pending.values()):
                if (item.consumer_id == consumer_id and not item.dispatching
                        and item.result.state is CommandState.ACCEPTED):
                    del self._pending[item.request.id]
            for query, read in tuple(self._facility_reads.items()):
                read.consumers.discard(consumer_id)
                # A dispatched read keeps its slot until the provider answers or Core's deadline passes.
                if not read.consumers and not read.dispatched:
                    del self._facility_reads[query]

    # -- Commands ------------------------------------------------------------------------------

    def _submit_command(self, consumer_id, capability_id, value, *, input_id=None,
                        client_data_region=None, variable=None, event=None):
        with self._lock:
            consumer = self._consumer(consumer_id)
            command_id = uuid4().hex
            session = self._connection.session_id
            result = CommandResult(
                command_id, capability_id, CommandState.ACCEPTED, self._provider.source, session,
            )
            sdk_signal = sdk_event = None
            coalesce_key = None
            try:
                if self._connection.state is not ConnectionState.CONNECTED or session is None:
                    raise CoreRequestError("A connected simulator session is required.")
                if self._write_blocked is not None:
                    raise CoreRequestError(self._write_blocked)
                if capability_id in _SDK_COMMANDS:
                    operation = capability_id
                    if _SDK_COMMANDS[operation].control_group not in consumer.groups:
                        raise CoreRequestError("The consumer lacks access to this control group.")
                    if not self._provider.supports_command(operation):
                        raise CoreRequestError("The provider does not support this SDK command.")
                    if operation in _ADVANCED_COMMANDS:
                        if not isinstance(value, tuple):
                            raise CoreRequestError("Supply the documented operation argument tuple.")
                        arguments = operation_arguments(_ADVANCED_COMMANDS[operation], value)
                    elif operation == SIMVAR_WRITE:
                        sdk_signal = _sdk_simvar_signal(variable)
                        if not self._provider.supports_signal(sdk_signal):
                            raise CoreRequestError("The provider does not support this SimVar.")
                        try:
                            if sdk_signal.value_type is ValueType.BOOLEAN and type(value) is not bool:
                                raise ValueError("Expected a Boolean.")
                            arguments = (decode_value(sdk_signal.value_type, value),)
                        except ValueError as error:
                            raise CoreRequestError(f"Invalid SimVar write value. {error}") from error
                        coalesce_key = (operation, sdk_signal)
                    else:
                        sdk_event, arguments = client_event_words(event, value)
                elif capability_id == CLIENT_DATA_WRITE:
                    self._require_client_data_region(client_data_region)
                    if not self._provider.supports_command(CLIENT_DATA_WRITE):
                        raise CoreRequestError("The provider does not support Client Data writes.")
                    if "client_data" not in consumer.groups or not client_data_region.area.writable:
                        raise CoreRequestError("The consumer or area does not permit Client Data writes.")
                    if not isinstance(value, bytes) or len(value) != client_data_region.size:
                        raise CoreRequestError("The Client Data payload must match the declared region size.")
                    operation, arguments = CLIENT_DATA_WRITE, (value,)
                    coalesce_key = (operation, client_data_region)
                elif input_id is None:
                    entry = COMMANDS.get(capability_id)
                    if entry is None or not self._provider.supports_command(entry.operation):
                        raise CoreRequestError("Unsupported simulator command.")
                    if entry.capability.control_group not in consumer.groups:
                        raise CoreRequestError("The consumer lacks access to this control group.")
                    operation, arguments = entry.operation, entry.arguments(value)
                    if operation == "COM_STBY_RADIO_SET_HZ":
                        # Reject an 8.33 kHz channel on a 25 kHz radio before sending, when the
                        # current session has reported COM1's spacing (C6).
                        problem = com_channel_problem(arguments[0], self._known_com_spacing())
                        if problem is not None:
                            raise CoreRequestError(problem)
                    if entry.coalesce:
                        coalesce_key = (operation,)
                else:
                    if "aircraft_inputs" not in consumer.groups:
                        raise CoreRequestError("The consumer lacks aircraft input write access.")
                    self._input_descriptor(input_id)
                    parameters = self._input_parameters.get(input_id)
                    if parameters is None or parameters.types is None:
                        raise CoreRequestError("Request and receive input parameters before writing.")
                    arguments = value if isinstance(value, tuple) else (value,)
                    encode_input_arguments(parameters.types, arguments)
                    operation = INPUT_WRITE
                superseded = self._supersedable(consumer_id, coalesce_key)
                mine = sum(item.consumer_id == consumer_id for item in self._pending.values()) - len(superseded)
                if mine >= _MAX_PENDING_PER_CONSUMER:
                    raise CoreRequestError("This module already has too many commands waiting.")
                if len(self._pending) - len(superseded) >= _MAX_PENDING_COMMANDS:
                    raise CoreRequestError("Command queue is full.")
            except CoreRequestError as error:
                rejected = replace(result, state=CommandState.REJECTED, detail=str(error))
                self._remember_result(consumer, rejected)
                return rejected
            for item in superseded:
                self._finish(item, CommandState.NOT_SENT,
                             "Superseded by a newer value from the same module before it was sent.")
            self._pending[command_id] = _Pending(
                consumer_id, Command(
                    command_id, session, operation, arguments, input_id, client_data_region,
                    sdk_signal, sdk_event, consumer_id,
                ),
                result, self._now() + self._command_timeout_seconds, coalesce_key=coalesce_key,
            )
            consumer.offer(result)
            return result

    def _known_com_spacing(self):
        """COM1 spacing from a current-session reading, or None when no module reads it."""
        reading = self._readings.get(COM1_SPACING)
        if reading is None or reading.error is not None or reading.session_id != self._connection.session_id:
            return None
        return com_spacing(reading.value)

    def _supersedable(self, consumer_id, key):
        """Queued, unsent absolute writes from the same module that a newer value replaces."""
        if key is None:
            return ()
        return tuple(
            item for item in self._pending.values()
            if item.consumer_id == consumer_id and item.coalesce_key == key and not item.dispatching
            and item.result.state is CommandState.ACCEPTED
        )

    def _command_result(self, consumer_id, command_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            item = self._pending.get(command_id)
            if item is not None and item.consumer_id == consumer_id:
                return item.result
            return consumer.results.get(command_id)

    def _dispatch_next(self):
        with self._lock:
            now = self._now()
            for item in tuple(self._pending.values()):
                if item.dispatching or item.result.state is CommandState.SENT:
                    if item.sent_deadline is not None and item.sent_deadline <= now:
                        log.warning("The provider gave no outcome for command %s in time", item.request.id)
                        self._block_commands("The provider gave no outcome for a sent command in time.")
                        return
                elif item.deadline <= now:
                    self._finish(item, CommandState.NOT_SENT,
                                 "The command waited too long in the queue and was not sent.")
            if any(item.dispatching or item.result.state is CommandState.SENT for item in self._pending.values()):
                return
            item = next(iter(self._pending.values()), None)
            if item is None:
                return
            item.dispatching = True
            item.sent_deadline = now + self._confirm_timeout
        try:
            self._provider.send(item.request)
        except CommandRejected as error:
            with self._lock:
                self._finish(item, CommandState.REJECTED, str(error))
            return
        except ProviderError as error:
            log.warning("Command send failed: %s", error)
            with self._lock:
                self._block_commands(str(error))
            return
        except Exception as error:  # noqa: BLE001 - the write may have reached the simulator
            log.exception("Command send failed unexpectedly")
            with self._lock:
                self._block_commands(f"The provider failed unexpectedly while sending a command: {error!r}")
            return
        except BaseException:
            with self._lock:
                self._block_commands("Command dispatch was interrupted.")
            raise
        with self._lock:
            if self._pending.get(item.request.id) is not item:
                return
            item.result = replace(item.result, state=CommandState.SENT)
            item.dispatching = False
            item.sent_deadline = self._now() + self._confirm_timeout
            consumer = self._consumers.get(item.consumer_id)
            if consumer is not None:
                consumer.offer(item.result)

    def _finish(self, item, state, detail, data=None):
        self._pending.pop(item.request.id, None)
        consumer = self._consumers.get(item.consumer_id)
        if consumer is not None:
            self._remember_result(consumer, replace(item.result, state=state, detail=detail, data=data))

    @staticmethod
    def _remember_result(consumer, result):
        consumer.results[result.id] = result
        if len(consumer.results) > 128:
            del consumer.results[next(iter(consumer.results))]
        consumer.offer(result)

    # -- Provider updates ----------------------------------------------------------------------

    def _receive(self, update):
        if isinstance(update, IngressGap):
            self._receive_gap(update)
        elif isinstance(update, CommandOutcome):
            item = self._pending.get(update.command_id)
            if item is not None and item.result.state is CommandState.SENT:
                self._finish(item, _TERMINAL_OUTCOMES[update.outcome], update.detail, update.data)
                if update.outcome is Outcome.UNCERTAIN:
                    self._block_commands(update.detail)
        elif isinstance(update, InputCatalog):
            # A complete catalog of the generation that a gap interrupted is still trustworthy.
            if (update.generation > self._discarded_input_generation
                    or (update.generation == self._discarded_input_generation
                        and update.state in (InputCatalogState.READY, InputCatalogState.FAILED))):
                self._receive_input_catalog(update)
        elif isinstance(update, InputParameters):
            if update.id in self._input_descriptors:
                self._input_parameters[update.id] = update
                for consumer in self._consumers.values():
                    consumer.offer(update)
        elif isinstance(update, FacilityReply):
            read = self._facility_reads.get(update.query)
            if read is not None and read.result.id == update.id:
                self._finish_facility(update.query, update.state, update.data, update.detail)
        elif isinstance(update, FacilityCacheReply):
            self._receive_facility_cache(update)
        elif isinstance(update, Reading):
            self._receive_reading(update)

    def _receive_gap(self, update):
        log.info("The provider lost %d updates", update.dropped_updates)
        # Values may have changed during the loss, so nothing is presented as current until it
        # arrives again. Samples already queued for consumers were valid and stay queued.
        self._readings.clear()
        self._fail_facilities("Provider updates were lost during the facility query.")
        self._invalidate_facility_watches("Provider updates were lost from the facility cache.")
        if self._input_catalog.session_id is not None:
            self._discarded_input_generation = self._input_catalog.generation
            self._receive_input_catalog(InputCatalog(
                None, 0, InputCatalogState.UNAVAILABLE,
                detail="Provider updates were lost. Discover aircraft inputs again.",
            ))
        # On-change and one-shot values would otherwise stay unavailable until they change.
        for signal in self._by_signal:
            if signal.system_state and self._system_state_provider is not None:
                self._state_refreshes.add(signal)
            elif (signal.client_data_region is not None and self._client_data_provider is not None
                  and signal.client_data_mode in (ClientDataMode.ONCE, ClientDataMode.ON_SET)
                  and len(self._client_refreshes) < 128):
                self._client_refreshes[signal] = None
        for consumer in self._consumers.values():
            self._reset_delivery(consumer)
            consumer.offer(Gap(update.dropped_updates, "Provider update queue lost data."))

    def _receive_input_catalog(self, catalog):
        previous = self._input_catalog
        if (catalog.session_id, catalog.generation) != (previous.session_id, previous.generation):
            self._input_descriptors.clear()
            self._input_parameters.clear()
            for request in tuple(self._input_requests):
                if request[1] is not None:
                    del self._input_requests[request]
            for item in tuple(self._pending.values()):
                if item.request.input_id is not None:
                    may_have_sent = item.dispatching or item.result.state is CommandState.SENT
                    state = CommandState.UNCERTAIN if may_have_sent else CommandState.NOT_SENT
                    self._finish(item, state, "Aircraft Input Events were invalidated.")
            reason = "The aircraft inputs changed. Discover them again and subscribe again."
            for consumer in self._consumers.values():
                consumer.remove_data(lambda item: item.entry.signal.input_id is not None)
                consumer.remove_control(lambda item: isinstance(item, (InputCatalog, InputParameters)))
                for subscription in tuple(consumer.subscriptions.values()):
                    if subscription.signal.input_id is not None:
                        del consumer.subscriptions[subscription.public.id]
                        self._index_remove(subscription)
                        consumer.ended[subscription.public.id] = (reason, subscription.public.capability)
                        if len(consumer.ended) > _MAX_ENDED_SUBSCRIPTIONS:
                            del consumer.ended[next(iter(consumer.ended))]
                        consumer.offer(SubscriptionEnded(
                            subscription.public.id, subscription.public.capability.id, reason,
                        ))
        self._input_catalog = catalog
        if catalog.state is InputCatalogState.READY:
            self._input_descriptors = {item.id: item for item in catalog.inputs}
        for consumer in self._consumers.values():
            consumer.offer(catalog)

    def _receive_reading(self, reading):
        signal = reading.signal
        if signal.event:
            self._lifecycle_change(reading)
            for entry in self._by_signal.get(signal, ()):
                if not entry.event_enabled:
                    continue
                capability_id = entry.public.capability.id
                if reading.error is not None:
                    entry.owner.offer(_QueuedUpdate(entry, SignalError(1, reading.error, capability_id, entry.public.id)))
                    continue
                try:
                    value = decode_event_value(signal.value_type, reading.value, signal)
                except ValueError:
                    entry.owner.offer(_QueuedUpdate(entry, SignalError(
                        1, "Provider supplied an invalid event payload.", capability_id, entry.public.id,
                    )))
                    continue
                entry.owner.offer(_QueuedUpdate(entry, Event(
                    capability_id, value, self._provider.source,
                    reading.session_id, reading.received_at, reading.sample_time,
                )))
            return
        previous = self._readings.get(signal)
        if previous is not None and reading.received_monotonic < previous.received_monotonic:
            return
        subscribers = self._by_signal.get(signal)
        if not subscribers:
            return
        self._readings[signal] = reading
        if signal.snapshot is not None:
            for entry in subscribers:
                if reading.error is not None:
                    update = SignalError(1, reading.error, entry.public.capability.id, entry.public.id)
                else:
                    update = self._snapshot(entry, reading)
                entry.owner.offer(_QueuedUpdate(entry, update))
            return
        native = self._demand_intervals.get(signal)
        for entry in subscribers:
            entry.awaiting_refresh = None
            if entry.max_age_ms is not None and reading.error is None:
                self._arm_stale(entry.owner, entry, reading.received_monotonic + entry.max_age_ms / 1000)
            if reading.error is not None or self._due(entry, reading.received_monotonic, native):
                entry.owner.offer(_QueuedObservation(entry, reading))

    @staticmethod
    def _due(entry, received, native):
        """Deliver on the consumer's own schedule, tolerating normal receive jitter.

        When the consumer asked for the shared demand's rate or faster, the provider already
        paces the samples, so every one is delivered. A slower consumer is served on a fixed
        grid with half a provider period of tolerance, and resynchronises after a stall.
        """
        interval = entry.public.interval_ms
        if interval is None or native is None or interval <= native:
            entry.last_delivery = received
            return True
        period = interval / 1000
        slack = native / 2000
        if entry.next_due is not None and received < entry.next_due - slack:
            return False
        on_grid = entry.next_due is not None and received - entry.next_due < period
        entry.next_due = (entry.next_due if on_grid else received) + period
        entry.last_delivery = received
        return True

    def _lifecycle_change(self, reading):
        """Invalidate one-shot states that a simulator lifecycle event may have changed."""
        if reading.error is not None or self._connection.state is not ConnectionState.CONNECTED:
            return
        for state in self._lifecycle.get(reading.signal, ()):
            if state not in self._by_signal or self._system_state_provider is None:
                continue
            previous = self._readings.get(state)
            # Stamp with the event's own receipt time, not Core's processing time, so a fresh
            # reply received after the event in the same batch is still accepted.
            stamp = max(reading.received_monotonic,
                        previous.received_monotonic if previous is not None else 0.0)
            self._receive_reading(Reading(
                reading.session_id, state, None, datetime.now(timezone.utc), stamp,
                error="The simulator reported a change. Waiting for a fresh reply.",
            ))
            self._state_refreshes.add(state)

    def _block_commands(self, detail):
        self._write_blocked = f"Start a new session before submitting more writes. {detail}"
        self._finish_pending(detail)

    def _finish_pending(self, detail):
        for item in tuple(self._pending.values()):
            may_have_sent = item.dispatching or item.result.state is CommandState.SENT
            state = CommandState.UNCERTAIN if may_have_sent else CommandState.NOT_SENT
            self._finish(item, state, detail)

    def _end_session(self, detail, *, failed=False):
        with self._lock:
            self._finish_pending(detail)
            self._fail_facilities(detail)
            self._invalidate_facility_watches(detail)
            self._applied_facility_watches.clear()
            if self._input_catalog.session_id is not None:
                self._receive_input_catalog(InputCatalog(None, 0, InputCatalogState.UNAVAILABLE, detail=detail))
            self._discarded_input_generation = 0
            self._input_requests.clear()
            self._client_refreshes.clear()
            self._state_refreshes.clear()
            self._readings.clear()
            self._retiring.clear()
            self._set_applied_demand(())
            # Samples already queued were received in the ended session and keep its ID. They
            # stay ahead of the connection change so a consumer can still record them.
            for consumer in self._consumers.values():
                self._reset_delivery(consumer)
            self._publish_connection(
                ConnectionState.ERROR if failed else ConnectionState.DISCONNECTED, None, detail,
            )
        try:
            self._provider.disconnect()
        except Exception as error:  # noqa: BLE001 - any cleanup failure must stay visible and retryable
            log.warning("Provider cleanup failed: %r", error)
            with self._lock:
                self._publish_connection(
                    ConnectionState.ERROR, None, f"{detail} Provider cleanup is unresolved: {error}",
                )
        else:
            self._cleanup_required = False

    @staticmethod
    def _reset_delivery(consumer):
        consumer.stale_heap.clear()
        for entry in consumer.subscriptions.values():
            entry.last_delivery = None
            entry.next_due = None
            entry.stale_reported_at = None
            entry.in_stale_heap = False
            entry.awaiting_refresh = None

    def _publish_connection(self, state, session_id, detail=""):
        self._connection = Connection(
            state, self._provider.source, session_id, datetime.now(timezone.utc), detail,
        )
        for consumer in self._consumers.values():
            consumer.offer(self._connection)


def _finite(value):
    if type(value) in (int, float):
        try:
            if value == value and abs(value) != float("inf"):
                return float(value)
        except OverflowError:
            return None
    return None


@dataclass(frozen=True, slots=True, eq=False)
class _QueuedReading:
    entry: _Subscription


@dataclass(frozen=True, slots=True, eq=False)
class _QueuedObservation(_QueuedReading):
    reading: Reading


@dataclass(frozen=True, slots=True, eq=False)
class _QueuedUpdate(_QueuedReading):
    """An event, snapshot or per-subscription error already shaped for the consumer."""

    update: Event | Snapshot | SignalError
