"""In-process Core host and the API handed to each consumer."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Lock, RLock
from time import monotonic
from uuid import uuid4

from .aircraft_inputs import INPUT_WRITE, encode_input_arguments
from .catalog import COMMANDS, READS, ReadMapping, decode_event_value, decode_value
from .client_data import CLIENT_DATA_WRITE, ClientDataArea, ClientDataMode, ClientDataRegion
from .contract import (
    API_VERSION, Capability, CapabilityStatus, CommandResult, CommandState, Connection,
    ConnectionState, CoreRequestError, Discovery, Event, Gap, InputCatalog, InputCatalogState,
    InputId, InputParameters, Kind, Observation, Quality, Subscription, Update, ValueType,
)
from .facilities import (
    AirportFilter, AirportReference, FacilityCacheSubscription, FacilityCacheUpdate, FacilityChange, FacilityKind,
    FacilityQuery, FacilityResult, FacilityState, NavaidKind, NavaidReference,
)
from .provider import (
    AircraftInputProvider, ClientDataProvider, Command, CommandOutcome, CommandRejected, Demand, Disconnect,
    FacilityCacheProvider, FacilityCacheReply, FacilityProvider, FacilityReply, IngressGap,
    Outcome, Provider, ProviderError, Reading, Signal,
    SystemStateProvider,
)
from .sdk_simvar import CLIENT_EVENT_SEND, SIMVAR_WRITE, SimVar, client_event_name, client_event_words
from .simulator_ops import (
    OPERATION_GROUPS, ControllerInput, ObjectField, ObjectKind, SimObjectRef, SimulatorOperation,
    SimulatorRead, SimulatorReadState, SimulatorResult, operation_arguments,
)


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
_MAX_FACILITY_REQUESTS = 8
_ADVANCED_COMMANDS = {f"sdk.simulator.{kind.value}": kind for kind in SimulatorOperation}
_SDK_COMMANDS.update({
    key: Capability(key, Kind.COMMAND, None, None, kind.value.replace("_", " "), OPERATION_GROUPS[kind])
    for key, kind in _ADVANCED_COMMANDS.items()
})
_MAX_FACILITY_RESULTS = 16


def _sdk_simvar_signal(variable):
    if not isinstance(variable, SimVar):
        raise CoreRequestError("Supply a SimVar address.")
    return Signal(variable.sdk_name, variable.unit, variable.value_type, simvar_format=variable.format,
                  local_simvar=variable.name.startswith("L:"))


def _positive_integer(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise CoreRequestError(f"{label} must be an integer from 1 to {maximum}.")
    return value


@dataclass(slots=True)
class _Subscription:
    public: Subscription
    signal: Signal
    max_age_ms: int | None
    last_delivery: float | None = None
    stale_reported_at: float | None = None
    event_enabled: bool = True


@dataclass(slots=True)
class _Consumer:
    groups: frozenset[str]
    diagnostics: bool
    queue_size: int
    subscriptions: dict[str, _Subscription] = field(default_factory=dict)
    queue: deque[Update | FacilityResult | FacilityCacheUpdate | SimulatorResult | _QueuedReading] = field(default_factory=deque)
    results: dict[str, CommandResult] = field(default_factory=dict)
    facility_results: dict[str, FacilityResult | SimulatorResult] = field(default_factory=dict)
    facility_subscriptions: dict[str, FacilityKind] = field(default_factory=dict)
    dropped: int = 0

    def offer(self, update):
        if len(self.queue) == self.queue_size:
            discarded = self.queue.popleft()
            self.dropped += discarded.dropped_updates if isinstance(discarded, Gap) else 1
        self.queue.append(update)


@dataclass(slots=True)
class _Pending:
    consumer_id: str
    request: Command
    result: CommandResult
    deadline: float
    dispatching: bool = False


@dataclass(slots=True)
class _FacilityRead:
    result: FacilityResult | SimulatorResult
    consumers: set[str] = field(default_factory=set)
    dispatched: bool = False


@dataclass(slots=True)
class _FacilityWatch:
    id: str
    subscribers: dict[str, str] = field(default_factory=dict)
    records: dict[tuple, AirportReference | NavaidReference] | None = None
    error: str = ""
    received_at: datetime | None = None
    session_id: str | None = None


class ConsumerAPI:
    """Module entry point. The host grants access when it creates this handle."""

    def __init__(self, host, consumer_id, initial_connection):
        self._host = host
        self._consumer_id = consumer_id
        self.initial_connection = initial_connection

    def discover(self) -> Discovery:
        return self._host._discover(self._consumer_id)

    def connection(self) -> Connection:
        return self._host._connection_for(self._consumer_id)

    def subscribe(self, capability_id, *, interval_ms=None, max_age_ms=None) -> Subscription:
        return self._host._subscribe_named(self._consumer_id, capability_id, interval_ms, max_age_ms)

    def subscribe_simvar(self, name, unit, value_type, *, interval_ms=None,
                         max_age_ms=None) -> Subscription:
        return self._host._subscribe_simvar(
            self._consumer_id, name, unit, value_type, interval_ms, max_age_ms,
        )

    def subscribe_sdk_simvar(self, variable: SimVar, *, interval_ms=None, max_age_ms=None) -> Subscription:
        return self._host._subscribe_sdk_simvar(self._consumer_id, variable, interval_ms, max_age_ms)

    def latest(self, subscription_id) -> Observation:
        return self._host._latest(self._consumer_id, subscription_id)

    def request_system_state(self, subscription_id) -> None:
        self._host._refresh_system_state(self._consumer_id, subscription_id)

    def set_event_enabled(self, subscription_id, enabled: bool) -> None:
        self._host._set_event_enabled(self._consumer_id, subscription_id, enabled)

    def poll(self, *, limit=128) -> tuple[Update | FacilityResult | FacilityCacheUpdate | SimulatorResult, ...]:
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

    def submit_command(self, capability_id, argument=None) -> CommandResult:
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

    def subscribe_input(self, input_id: InputId, *, max_age_ms=None) -> Subscription:
        return self._host._subscribe_input(self._consumer_id, input_id, max_age_ms)

    def request_input_value(self, subscription_id) -> None:
        self._host._refresh_input(self._consumer_id, subscription_id)

    def submit_input(self, input_id: InputId, arguments=()) -> CommandResult:
        return self._host._submit_command(
            self._consumer_id, INPUT_WRITE, arguments, input_id=input_id,
        )

    def client_data_areas(self) -> tuple[ClientDataArea, ...]:
        return self._host._client_data_areas_for(self._consumer_id)

    def subscribe_client_data(self, region: ClientDataRegion, *, mode=ClientDataMode.ON_SET, max_age_ms=None) -> Subscription:
        return self._host._subscribe_client_data(self._consumer_id, region, mode, max_age_ms)

    def request_client_data(self, subscription_id) -> None:
        self._host._refresh_client_data(self._consumer_id, subscription_id)

    def submit_client_data(self, region: ClientDataRegion, payload: bytes) -> CommandResult:
        return self._host._submit_command(
            self._consumer_id, CLIENT_DATA_WRITE, payload, client_data_region=region,
        )

    def command_result(self, command_id) -> CommandResult | None:
        return self._host._command_result(self._consumer_id, command_id)

    def unsubscribe(self, subscription_id) -> None:
        self._host._unsubscribe(self._consumer_id, subscription_id)

    def close(self) -> None:
        self._host._close_consumer(self._consumer_id)


class Core:
    """Own one provider session. The application explicitly drives its lifecycle."""

    def __init__(self, provider: Provider, *, command_timeout_seconds=5):
        if not 0 < command_timeout_seconds <= 60:
            raise ValueError("Command timeout must be positive and at most 60 seconds.")
        _positive_integer(provider.minimum_interval_ms, "Provider minimum interval", 3_600_000)
        self._provider = provider
        self._input_provider = provider if isinstance(provider, AircraftInputProvider) else None
        self._client_data_provider = provider if isinstance(provider, ClientDataProvider) else None
        self._system_state_provider = provider if isinstance(provider, SystemStateProvider) else None
        self._facility_provider = provider if isinstance(provider, FacilityProvider) else None
        self._facility_cache_provider = provider if isinstance(provider, FacilityCacheProvider) else None
        self._command_timeout_seconds = command_timeout_seconds
        self._lock = RLock()
        self._provider_lock = Lock()
        self._consumers: dict[str, _Consumer] = {}
        self._readings: dict[Signal, Reading] = {}
        self._pending: dict[str, _Pending] = {}
        self._applied_demand: tuple[Demand, ...] = ()
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
        self._connection = Connection(
            ConnectionState.DISCONNECTED, provider.source, None, datetime.now(timezone.utc),
        )

    def register_consumer(self, *, control_groups=(), diagnostics=False, queue_size=256):
        """Host-only registration. Consumers receive the returned API, not the host."""
        _positive_integer(queue_size, "Queue size", 4096)
        groups = frozenset(control_groups)
        known_groups = {entry.capability.control_group for entry in COMMANDS.values()} | {
            "aircraft_inputs", "client_data",
        }
        known_groups.update(entry.control_group for entry in _SDK_COMMANDS.values())
        if not groups <= known_groups:
            raise CoreRequestError("Unknown control group.")
        with self._lock:
            if len(self._consumers) >= 64:
                raise CoreRequestError("Core consumer limit reached.")
            consumer_id = uuid4().hex
            self._consumers[consumer_id] = _Consumer(groups, diagnostics, queue_size)
            return ConsumerAPI(self, consumer_id, self._connection)

    def register_client_data(self, area: ClientDataArea) -> None:
        """Host-only declaration. Does not create or alter a simulator area."""
        if not isinstance(area, ClientDataArea):
            raise CoreRequestError("Supply a ClientDataArea declaration.")
        if self._client_data_provider is None:
            raise CoreRequestError("The selected provider does not expose Client Data.")
        with self._lock:
            existing = self._client_data_areas.get(area.name)
            if existing is not None and existing != area:
                raise CoreRequestError("This Client Data name already has a different declaration.")
            self._client_data_areas[area.name] = area

    def connect(self) -> Connection:
        with self._provider_lock:
            with self._lock:
                if self._cleanup_required:
                    return self._connection
                session = uuid4().hex
                self._publish_connection(ConnectionState.CONNECTING, session)
                self._cleanup_required = True
            try:
                self._provider.connect(session)
            except ProviderError as error:
                self._end_session(str(error), failed=True)
            else:
                with self._lock:
                    self._applied_demand = ()
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

    def pump(self, *, limit=256) -> None:
        """Reconcile demand, receive updates and dispatch one queued command."""
        _positive_integer(limit, "Batch size", 4096)
        with self._provider_lock:
            with self._lock:
                if self._connection.state is not ConnectionState.CONNECTED:
                    return
                demand = self._desired_demand()
                requests = tuple(self._input_requests)
                self._input_requests.clear()
                client_refreshes = tuple(self._client_refreshes)
                self._client_refreshes.clear()
                state_refreshes = tuple(self._state_refreshes)
                self._state_refreshes.clear()
                facility_reads = tuple(
                    (query, read) for query, read in self._facility_reads.items() if not read.dispatched
                )
                for _, read in facility_reads:
                    read.dispatched = True
                watches = {kind: watch.id for kind, watch in self._facility_watches.items()}
            try:
                if demand != self._applied_demand:
                    self._provider.replace_demand(demand)
                    self._applied_demand = demand
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
                self._end_session(str(error), failed=True)
                return
            for update in updates:
                if update.session_id != self._connection.session_id:
                    continue
                if isinstance(update, Disconnect):
                    self._end_session(update.reason)
                    return
                with self._lock:
                    self._receive(update)
            self._dispatch_next()

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
            self._remember_facility(self._consumers[consumer_id], result)

    def _fail_facilities(self, reason):
        for query in tuple(self._facility_reads):
            self._finish_facility(query, FacilityState.UNAVAILABLE, detail=reason)

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
            watch.received_at, tuple(watch.records.values()) if watch.records is not None else (), watch.error,
        )

    def _remove_facility_subscription(self, consumer, subscription_id):
        kind = consumer.facility_subscriptions.pop(subscription_id, None)
        if kind is not None:
            watch = self._facility_watches[kind]
            del watch.subscribers[subscription_id]
            if not watch.subscribers:
                del self._facility_watches[kind]
            consumer.queue = deque(item for item in consumer.queue
                                   if not isinstance(item, FacilityCacheUpdate) or item.subscription_id != subscription_id)

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
            consumer.queue = deque(item for item in consumer.queue if not isinstance(item, FacilityCacheUpdate))
        for kind, watch in self._facility_watches.items():
            self._receive_facility_cache(FacilityCacheReply(
                self._connection.session_id, watch.id, kind, FacilityChange.UNAVAILABLE, detail=detail,
            ))
            watch.id = uuid4().hex

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
            entry = self._consumer(consumer_id).subscriptions.get(subscription_id)
            if entry is None or entry.signal.input_id is None:
                raise CoreRequestError("An active aircraft input subscription is required.")
            self._queue_input_request(consumer_id, "value", entry.signal.input_id)

    def _client_data_areas_for(self, consumer_id):
        with self._lock:
            self._consumer(consumer_id)
            return tuple(self._client_data_areas.values())

    def _require_client_data_region(self, region):
        if (not isinstance(region, ClientDataRegion)
                or self._client_data_areas.get(region.area.name) != region.area):
            raise CoreRequestError("Use a region from a host-declared Client Data area.")

    def _subscribe_client_data(self, consumer_id, region, mode, max_age_ms):
        with self._lock:
            consumer = self._consumer(consumer_id)
            self._require_client_data_region(region)
            if not isinstance(mode, ClientDataMode):
                raise CoreRequestError("Select a ClientDataMode.")
            signal = Signal(region.area.name, None, ValueType.BYTES, client_data_region=region, client_data_mode=mode)
            capability_id = region.capability_id if mode is ClientDataMode.ON_SET else f"{region.capability_id}:{mode.value}"
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
            entry.event_enabled = enabled
            if not enabled:
                consumer.queue = deque(
                    item for item in consumer.queue
                    if not isinstance(item, _QueuedEvent) or item.entry is not entry
                )

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
        if len(consumer.subscriptions) >= 128:
            raise CoreRequestError("Consumer subscription limit reached.")
        if not self._provider.supports_signal(entry.signal):
            raise CoreRequestError("The provider does not support this capability.")
        if entry.signal.system_state and interval_ms is not None:
            raise CoreRequestError("System state uses one-shot requests, without a polling interval.")
        if (entry.signal.input_id is not None or entry.signal.client_data_region is not None
                or entry.signal.system_state):
            if max_age_ms is not None:
                _positive_integer(max_age_ms, "Maximum age", 86_400_000)
        elif entry.capability.kind is Kind.EVENT:
            if interval_ms is not None or max_age_ms is not None:
                raise CoreRequestError("Events do not accept cadence or freshness options.")
        else:
            if interval_ms is not None:
                _positive_integer(interval_ms, "Interval", 3_600_000)
            interval_ms = max(interval_ms or 1000, self._provider.minimum_interval_ms)
            if max_age_ms is None:
                max_age_ms = 3 * interval_ms
            else:
                _positive_integer(max_age_ms, "Maximum age", 86_400_000)
        public = Subscription(uuid4().hex, entry.capability, interval_ms)
        consumer.subscriptions[public.id] = _Subscription(public, entry.signal, max_age_ms)
        return public

    def _desired_demand(self):
        signals = {}
        enabled_events = set()
        for consumer in self._consumers.values():
            for entry in consumer.subscriptions.values():
                if entry.signal.event and entry.event_enabled:
                    enabled_events.add(entry.signal)
                interval = entry.public.interval_ms
                existing = signals.get(entry.signal)
                if existing is None or (interval is not None and interval < existing):
                    signals[entry.signal] = interval
        return tuple(
            Demand(signal, interval, not signal.event or signal in enabled_events) for signal, interval in sorted(
                signals.items(), key=lambda pair: (
                    pair[0].name, pair[0].unit or "", pair[0].value_type.value, pair[0].event,
                    pair[0].client_data_region.offset if pair[0].client_data_region else 0,
                    pair[0].client_data_region.size if pair[0].client_data_region else 0,
                    pair[0].simvar_format.value if pair[0].simvar_format else "",
                ),
            )
        )

    def _latest(self, consumer_id, subscription_id):
        with self._lock:
            entry = self._consumer(consumer_id).subscriptions.get(subscription_id)
            if entry is None or entry.public.capability.kind is not Kind.VALUE:
                raise CoreRequestError("An active value subscription is required.")
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
            return self._observation(entry, reading, monotonic())

    def _observation(self, entry, reading, now):
        capability = entry.public.capability
        try:
            if reading.error is not None:
                raise ValueError(reading.error)
            value = decode_value(capability.value_type, reading.value)
        except ValueError as error:
            return Observation(
                capability.id, None, capability.unit, Quality.UNAVAILABLE, self._provider.source,
                reading.session_id, reading.received_at, reading.sample_time, str(error),
            )
        stale = (entry.max_age_ms is not None
                 and (now - reading.received_monotonic) * 1000 > entry.max_age_ms)
        return Observation(
            capability.id, value, capability.unit, Quality.STALE if stale else Quality.CURRENT,
            self._provider.source, reading.session_id, reading.received_at, reading.sample_time,
            "Receipt age exceeds the subscription limit." if stale else None,
        )

    def _poll(self, consumer_id, limit):
        _positive_integer(limit, "Batch size", 4096)
        with self._lock:
            consumer = self._consumer(consumer_id)
            batch = []
            if consumer.dropped:
                batch.append(Gap(consumer.dropped, "Consumer update queue lost data."))
                consumer.dropped = 0
            now = monotonic()
            while consumer.queue and len(batch) < limit:
                update = consumer.queue.popleft()
                if isinstance(update, _QueuedObservation):
                    observation = self._observation(update.entry, update.reading, now)
                    if (observation.quality is Quality.STALE
                            and self._readings.get(update.entry.signal) is update.reading):
                        update.entry.stale_reported_at = update.reading.received_monotonic
                    update = observation
                elif isinstance(update, _QueuedEvent):
                    update = update.event
                batch.append(update)
            for entry in consumer.subscriptions.values():
                if len(batch) >= limit:
                    break
                reading = self._readings.get(entry.signal)
                if (reading is not None and entry.max_age_ms is not None
                        and entry.stale_reported_at != reading.received_monotonic
                        and (now - reading.received_monotonic) * 1000 > entry.max_age_ms):
                    entry.stale_reported_at = reading.received_monotonic
                    observation = self._observation(entry, reading, now)
                    if observation.quality is Quality.STALE:
                        batch.append(observation)
            return tuple(batch)

    def _unsubscribe(self, consumer_id, subscription_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            self._remove_facility_subscription(consumer, subscription_id)
            existing = consumer.subscriptions.pop(subscription_id, None)
            if existing is not None:
                consumer.queue = deque(
                    item for item in consumer.queue
                    if not isinstance(item, _QueuedReading) or item.entry is not existing
                )
                self._prune_readings()

    def _close_consumer(self, consumer_id):
        with self._lock:
            consumer = self._consumers.pop(consumer_id, None)
            if consumer is None:
                return
            for subscription in tuple(consumer.facility_subscriptions):
                self._remove_facility_subscription(consumer, subscription)
            for item in tuple(self._pending.values()):
                if (item.consumer_id == consumer_id and not item.dispatching
                        and item.result.state is CommandState.ACCEPTED):
                    del self._pending[item.request.id]
            for query, read in tuple(self._facility_reads.items()):
                read.consumers.discard(consumer_id)
                if not read.consumers and not read.dispatched:
                    del self._facility_reads[query]
            self._prune_readings()

    def _prune_readings(self):
        active = {
            entry.signal for consumer in self._consumers.values()
            for entry in consumer.subscriptions.values()
        }
        self._readings = {signal: reading for signal, reading in self._readings.items() if signal in active}

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
            try:
                if self._connection.state is not ConnectionState.CONNECTED or session is None:
                    raise CoreRequestError("A connected simulator session is required.")
                if self._write_blocked is not None:
                    raise CoreRequestError(self._write_blocked)
                if len(self._pending) >= 64:
                    raise CoreRequestError("Command queue is full.")
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
                elif input_id is None:
                    entry = COMMANDS.get(capability_id)
                    if entry is None or not self._provider.supports_command(entry.operation):
                        raise CoreRequestError("Unsupported simulator command.")
                    if entry.capability.control_group not in consumer.groups:
                        raise CoreRequestError("The consumer lacks access to this control group.")
                    operation, arguments = entry.operation, entry.arguments(value)
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
            except CoreRequestError as error:
                rejected = replace(result, state=CommandState.REJECTED, detail=str(error))
                self._remember_result(consumer, rejected)
                return rejected
            self._pending[command_id] = _Pending(
                consumer_id, Command(
                    command_id, session, operation, arguments, input_id, client_data_region,
                    sdk_signal, sdk_event, consumer_id,
                ),
                result, monotonic() + self._command_timeout_seconds,
            )
            consumer.offer(result)
            return result

    def _command_result(self, consumer_id, command_id):
        with self._lock:
            consumer = self._consumer(consumer_id)
            item = self._pending.get(command_id)
            if item is not None and item.consumer_id == consumer_id:
                return item.result
            return consumer.results.get(command_id)

    def _dispatch_next(self):
        with self._lock:
            now = monotonic()
            for item in tuple(self._pending.values()):
                if item.deadline <= now:
                    if item.result.state is CommandState.SENT:
                        self._block_commands("A sent command expired before confirmation.")
                        return
                    self._finish(item, CommandState.NOT_SENT, "The command deadline expired.")
            if any(item.result.state is CommandState.SENT for item in self._pending.values()):
                return
            item = next(iter(self._pending.values()), None)
            if item is None:
                return
            item.dispatching = True
        try:
            self._provider.send(item.request)
        except CommandRejected as error:
            with self._lock:
                self._finish(item, CommandState.REJECTED, str(error))
            return
        except ProviderError as error:
            with self._lock:
                self._block_commands(str(error))
            return
        with self._lock:
            item.result = replace(item.result, state=CommandState.SENT)
            item.dispatching = False
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

    def _receive(self, update):
        if isinstance(update, IngressGap):
            self._readings.clear()
            self._fail_facilities("Provider updates were lost during the facility query.")
            self._invalidate_facility_watches("Provider updates were lost from the facility cache.")
            if self._input_catalog.session_id is not None:
                self._discarded_input_generation = self._input_catalog.generation
                self._receive_input_catalog(InputCatalog(
                    None, 0, InputCatalogState.UNAVAILABLE,
                    detail="Provider updates were lost. Discover aircraft inputs again.",
                ))
            for consumer in self._consumers.values():
                self._discard_readings(consumer)
                consumer.offer(Gap(update.dropped_updates, "Provider update queue lost data."))
        elif isinstance(update, CommandOutcome):
            item = self._pending.get(update.command_id)
            if item is not None and item.result.state is CommandState.SENT:
                self._finish(item, _TERMINAL_OUTCOMES[update.outcome], update.detail, update.data)
                if update.outcome is Outcome.UNCERTAIN:
                    self._block_commands(update.detail)
        elif isinstance(update, InputCatalog):
            if update.generation > self._discarded_input_generation:
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
            for consumer in self._consumers.values():
                for subscription in tuple(consumer.subscriptions.values()):
                    if subscription.signal.input_id is not None:
                        del consumer.subscriptions[subscription.public.id]
                consumer.queue = deque(
                    update for update in consumer.queue
                    if not (isinstance(update, (InputCatalog, InputParameters))
                            or (isinstance(update, _QueuedReading)
                                and update.entry.signal.input_id is not None))
                )
            self._prune_readings()
        self._input_catalog = catalog
        if catalog.state is InputCatalogState.READY:
            self._input_descriptors = {item.id: item for item in catalog.inputs}
        for consumer in self._consumers.values():
            consumer.offer(catalog)

    def _receive_reading(self, reading):
        previous = self._readings.get(reading.signal)
        if previous is not None and reading.received_monotonic < previous.received_monotonic:
            return
        for consumer in self._consumers.values():
            for entry in consumer.subscriptions.values():
                if entry.signal != reading.signal:
                    continue
                if reading.signal.event:
                    if not entry.event_enabled:
                        continue
                    if reading.error is not None:
                        consumer.offer(Gap(1, reading.error))
                        continue
                    try:
                        value = decode_event_value(entry.signal.value_type, reading.value)
                    except ValueError:
                        consumer.offer(Gap(1, "Provider supplied an invalid event payload."))
                        continue
                    consumer.offer(_QueuedEvent(entry, Event(
                        entry.public.capability.id, value, self._provider.source,
                        reading.session_id, reading.received_at, reading.sample_time,
                    )))
                    continue
                self._readings[reading.signal] = reading
                elapsed = None if entry.last_delivery is None else (
                    reading.received_monotonic - entry.last_delivery
                ) * 1000
                if entry.public.interval_ms is None or elapsed is None or elapsed >= entry.public.interval_ms:
                    entry.last_delivery = reading.received_monotonic
                    consumer.offer(_QueuedObservation(entry, reading))

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
            self._applied_demand = ()
            for consumer in self._consumers.values():
                self._discard_readings(consumer)
            self._publish_connection(
                ConnectionState.ERROR if failed else ConnectionState.DISCONNECTED, None, detail,
            )
        try:
            self._provider.disconnect()
        except ProviderError as error:
            with self._lock:
                self._publish_connection(
                    ConnectionState.ERROR, None, f"{detail} Provider cleanup is unresolved: {error}",
                )
        else:
            self._cleanup_required = False

    @staticmethod
    def _discard_readings(consumer):
        retained = deque(item for item in consumer.queue if not isinstance(item, _QueuedReading))
        consumer.dropped += len(consumer.queue) - len(retained)
        consumer.queue = retained
        for entry in consumer.subscriptions.values():
            entry.last_delivery = None
            entry.stale_reported_at = None

    def _publish_connection(self, state, session_id, detail=""):
        self._connection = Connection(
            state, self._provider.source, session_id, datetime.now(timezone.utc), detail,
        )
        for consumer in self._consumers.values():
            consumer.offer(self._connection)


@dataclass(frozen=True, slots=True)
class _QueuedReading:
    entry: _Subscription


@dataclass(frozen=True, slots=True)
class _QueuedObservation(_QueuedReading):
    reading: Reading


@dataclass(frozen=True, slots=True)
class _QueuedEvent(_QueuedReading):
    event: Event
