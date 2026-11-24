"""EXPERIMENTAL client for a standalone Core process. Not wired into the EFB.

:class:`RemoteConsumerAPI` has the same method names and arguments as
``bbc_core.core.ConsumerAPI`` and returns the same contract types, so a module written
against the in-process API can be handed either one.

Differences a module can notice, all deliberate and labelled:

- If contact with the Core process is lost, ``poll()`` returns one ``Connection`` in state
  ``ERROR`` whose detail says contact was lost and the simulator state is unknown, then
  nothing. ``connection()`` keeps returning that notice. Every other call raises
  :class:`CoreUnavailableError`. Nothing is invented about the simulator.
- In push delivery the client holds updates in a bounded local buffer with the same two
  lanes as Core's own per-consumer queue: a data lane (``Observation``, ``Event``,
  ``Snapshot``, ``SignalError``) and a status lane (``Connection``, ``CommandResult``,
  ``Gap``, ``SubscriptionEnded``, catalogs and request results). A burst of samples can
  therefore never push a connection change or a command outcome out. If the module falls
  behind, the oldest entries of the full lane are dropped and the next ``poll()`` starts
  with a ``Gap`` that counts them, just as Core's own queues do. Updates are returned in
  the order they arrived.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from itertools import count
from multiprocessing.connection import Client
from typing import Any, Mapping

from ..contract import (
    API_VERSION, Connection, ConnectionState, CoreRequestError, Event, Gap, Observation, SignalError, Snapshot, Source,
)
from . import protocol as wire

log = logging.getLogger(__name__)

#: Update types that share the local data lane, as in Core's own consumer queue.
DATA_LANE_TYPES = (Observation, Event, Snapshot, SignalError)
#: Smallest status-lane size, matching Core's minimum control queue.
MIN_STATUS_BUFFER = 64
DATA_LOSS_REASON = "The local interface buffer was full, so updates were lost before this module read them."
STATUS_LOSS_REASON = (
    "The local interface status buffer was full, so status updates were lost before this module read them. "
    "Read connection() and command_result() for the current state."
)

_UNKNOWN_SOURCE = Source("Standalone Core (unknown provider)", simulated=False)


class CoreUnavailableError(ConnectionError):
    """The standalone Core process cannot be reached, or contact with it was lost."""


class HandshakeRejected(ConnectionError):
    """The Core refused this client. ``code`` is one of the protocol's ``REJECT_*`` codes."""

    def __init__(self, code: str, reason: str, *, server_protocol_versions=(), server_api_version=None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.server_protocol_versions = tuple(server_protocol_versions or ())
        self.server_api_version = server_api_version


class ServerNotTrusted(ConnectionError):
    """The process at the address could not prove it holds the Core IPC key."""


class _Waiter:
    __slots__ = ("event", "message")

    def __init__(self):
        self.event = threading.Event()
        self.message: dict | None = None


def connect(
    address: str | None = None,
    *,
    authkey: bytes,
    principal: str = "default",
    client_name: str = "",
    control_groups=(),
    diagnostics: bool = False,
    delivery: str = wire.DELIVERY_PULL,
    queue_size: int = 256,
    family: str | None = None,
    timeout_s: float = 5.0,
    call_timeout_s: float = 10.0,
    buffer_size: int = 4096,
    max_message_bytes: int = wire.DEFAULT_MAX_MESSAGE_BYTES,
    api_version: str = API_VERSION,
    protocol_versions: tuple[int, ...] = wire.SUPPORTED_PROTOCOL_VERSIONS,
    schema: str | None = None,
) -> RemoteConsumerAPI:
    """Connect, authenticate both ways and register a consumer with the standalone Core.

    ``control_groups`` are requested, not assumed: read ``granted_control_groups`` and
    ``denied_control_groups`` on the result. ``api_version``, ``protocol_versions`` and
    ``schema`` default to this build's own values; overriding them is for compatibility tests.
    """
    key = wire.check_authkey(authkey)
    family = family or wire.default_family()
    address = address or wire.default_address(family)
    if delivery not in wire.DELIVERY_MODES:
        raise ValueError("Delivery must be 'pull' or 'push'.")
    _check_buffer_size(buffer_size)
    try:
        connection = Client(address, family=family)
    except (OSError, EOFError) as error:
        raise CoreUnavailableError(f"The standalone Core is not reachable at {address}: {error}") from error
    try:
        welcome = _handshake(connection, key, {
            "type": wire.HELLO,
            "protocol_versions": list(protocol_versions),
            "api_version": api_version,
            "schema": schema or wire.schema_fingerprint(),
            "principal": principal,
            "client_name": client_name,
            "client_pid": os.getpid(),
            "control_groups": sorted(set(control_groups)),
            "diagnostics": bool(diagnostics),
            "delivery": delivery,
            "queue_size": queue_size,
            "nonce": wire.b64(wire.new_nonce()),
        }, timeout_s)
    except BaseException:
        connection.close()
        raise
    return RemoteConsumerAPI(connection, welcome, call_timeout_s=call_timeout_s, buffer_size=buffer_size,
                             max_message_bytes=max_message_bytes)


def _check_buffer_size(size):
    if type(size) is not int or not 1 <= size <= 1_000_000:
        raise ValueError("The local buffer size must be an integer from 1 to 1,000,000.")


def _handshake(connection, key, hello, timeout_s):
    def receive():
        try:
            if not connection.poll(timeout_s):
                raise CoreUnavailableError("The standalone Core did not answer the handshake in time.")
            message = wire.recv_message(connection, wire.MAX_HANDSHAKE_BYTES * 4)
        except (EOFError, OSError) as error:
            raise CoreUnavailableError(f"The standalone Core closed the connection during the handshake: {error}") from error
        except wire.WireError as error:
            raise CoreUnavailableError(f"The standalone Core sent an unreadable handshake: {error}") from error
        if message["type"] == wire.REJECT:
            raise HandshakeRejected(
                str(message.get("code")), str(message.get("reason")),
                server_protocol_versions=message.get("server_protocol_versions") or (),
                server_api_version=message.get("server_api_version"),
            )
        return message

    try:
        wire.send_message(connection, hello)
    except (OSError, EOFError) as error:
        raise CoreUnavailableError(f"The standalone Core closed the connection: {error}") from error
    challenge = receive()
    if challenge["type"] != wire.CHALLENGE:
        raise CoreUnavailableError("The standalone Core answered the hello with an unexpected message.")
    client_nonce = wire.unb64(hello["nonce"])
    try:
        server_nonce = wire.unb64(challenge.get("nonce"), size=wire.NONCE_BYTES)
    except wire.WireError as error:
        raise ServerNotTrusted("The process at this address sent an invalid challenge.") from error
    digest = wire.hello_digest(hello)
    expected = wire.proof(key, "bbc-core-server", client_nonce, server_nonce, digest)
    if not wire.proof_matches(expected, challenge.get("proof")):
        raise ServerNotTrusted(
            "The process at this address could not prove it holds the Core IPC key, so nothing was sent to it."
        )
    wire.send_message(connection, {
        "type": wire.AUTH, "proof": wire.proof(key, "bbc-core-client", client_nonce, server_nonce, digest),
    })
    welcome = receive()
    if welcome["type"] != wire.WELCOME:
        raise CoreUnavailableError("The standalone Core finished the handshake with an unexpected message.")
    return welcome


class RemoteConsumerAPI:
    """EXPERIMENTAL proxy with the same methods as ``ConsumerAPI``, over the local interface."""

    def __init__(self, connection, welcome: Mapping[str, Any], *, call_timeout_s: float = 10.0,
                 buffer_size: int = 4096, max_message_bytes: int = wire.DEFAULT_MAX_MESSAGE_BYTES):
        self._connection = connection
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ids = count(1)
        self._waiters: dict[int, _Waiter] = {}
        self._call_timeout = call_timeout_s
        self._max_bytes = max_message_bytes
        self.protocol_version: int = welcome["protocol_version"]
        self.api_version: str = welcome["api_version"]
        self.server_name: str = welcome.get("server_name", "")
        self.client_id: str = welcome.get("client_id", "")
        self.delivery: str = welcome.get("delivery", wire.DELIVERY_PULL)
        self.granted_control_groups = frozenset(welcome.get("control_groups", ()))
        self.denied_control_groups = frozenset(welcome.get("denied_control_groups", ()))
        self.diagnostics: bool = bool(welcome.get("diagnostics", False))
        initial = welcome.get("initial_connection")
        self.initial_connection: Connection | None = wire.decode(initial, validate=False) if initial else None
        self._source = self.initial_connection.source if self.initial_connection else _UNKNOWN_SOURCE
        _check_buffer_size(buffer_size)
        self._data: deque = deque()       # (arrival number, update) for samples
        self._status: deque = deque()     # (arrival number, update) for everything else
        self._arrivals = count()
        self._buffer_size = buffer_size
        self._status_size = max(MIN_STATUS_BUFFER, buffer_size)
        self._buffer_dropped = 0
        self._status_dropped = 0
        self._lost: Connection | None = None
        self._lost_reported = False
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, name="bbc-core-ipc-reader", daemon=True)
        self._reader.start()

    # -- State ----------------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._lost is None

    @property
    def lost_reason(self) -> str | None:
        return self._lost.detail if self._lost is not None else None

    def _mark_lost(self, detail: str):
        with self._state_lock:
            if self._lost is not None:
                return
            self._lost = Connection(ConnectionState.ERROR, self._source, None, datetime.now(timezone.utc), detail)
            waiters = tuple(self._waiters.values())
            self._waiters.clear()
        for waiter in waiters:
            waiter.event.set()

    # -- Reader ---------------------------------------------------------------------------------

    def _read_loop(self):
        reason = "Lost contact with the standalone Core process, so the simulator connection state is unknown."
        try:
            while True:
                message = wire.recv_message(self._connection, self._max_bytes)
                kind = message["type"]
                if kind in (wire.RESULT, wire.ERROR, wire.PONG):
                    with self._state_lock:
                        waiter = self._waiters.pop(message.get("id"), None)
                    if waiter is not None:
                        waiter.message = message
                        waiter.event.set()
                elif kind == wire.UPDATES:
                    self._buffer_updates(message.get("items"))
                elif kind == wire.SHUTDOWN:
                    reason = (f"The standalone Core closed this connection: {message.get('reason')} "
                              "The simulator connection state is unknown.")
                    break
                elif kind == wire.FATAL:
                    reason = (f"The standalone Core ended this connection after a protocol error: "
                              f"{message.get('reason')} The simulator connection state is unknown.")
                    break
                else:
                    log.warning("Ignoring an unexpected %r message from Core", kind)
        except (EOFError, OSError) as error:
            if self._closing:
                reason = "This module closed its connection to the standalone Core."
            elif isinstance(error, OSError) and str(error):
                reason = f"{reason} ({error})"
        except wire.WireError as error:
            reason = f"The standalone Core sent an unreadable message ({error}). The simulator connection state is unknown."
        except Exception:  # noqa: BLE001 - the reader must always report loss
            log.exception("The IPC reader failed")
        finally:
            self._mark_lost(reason)
            try:
                self._connection.close()
            except OSError:
                pass

    def _buffer_updates(self, items):
        if type(items) is not list:
            raise wire.WireError("Updates must be a list.")
        decoded = [self._decode_update(item) for item in items]
        with self._state_lock:
            for update in decoded:
                item = (next(self._arrivals), update)
                if isinstance(update, DATA_LANE_TYPES):
                    if len(self._data) >= self._buffer_size:
                        self._data.popleft()
                        self._buffer_dropped += 1
                    self._data.append(item)
                else:
                    if len(self._status) >= self._status_size:
                        _, lost = self._status.popleft()
                        self._status_dropped += lost.dropped_updates if isinstance(lost, Gap) else 1
                    self._status.append(item)

    @property
    def buffered(self) -> int:
        """Updates waiting in the local push buffer (both lanes)."""
        with self._state_lock:
            return len(self._data) + len(self._status)

    def _decode_update(self, item):
        update = wire.decode(item, validate=False)
        if isinstance(update, Connection):
            self._source = update.source
        return update

    # -- Calls ----------------------------------------------------------------------------------

    def _call(self, method: str, *args, **kwargs):
        if self._lost is not None:
            raise CoreUnavailableError(self._lost.detail)
        try:
            payload = {"args": [wire.encode(item) for item in args],
                       "kwargs": {name: wire.encode(item) for name, item in kwargs.items()}}
        except wire.WireError as error:
            raise CoreRequestError(f"This argument cannot be sent to the standalone Core: {error}") from error
        call_id = next(self._ids)
        waiter = _Waiter()
        with self._state_lock:
            if self._lost is not None:
                raise CoreUnavailableError(self._lost.detail)
            self._waiters[call_id] = waiter
        try:
            with self._send_lock:
                wire.send_message(self._connection, {"type": wire.CALL, "id": call_id, "method": method, **payload})
        except (OSError, EOFError, ValueError) as error:
            # The reader may still hold the server's own explanation (a shutdown notice), so let
            # it finish first; its reason wins over the bare transport error.
            if threading.current_thread() is not self._reader:
                self._reader.join(1.0)
            self._mark_lost(f"Lost contact with the standalone Core process ({error}). "
                            "The simulator connection state is unknown.")
            raise CoreUnavailableError(self._lost.detail) from error
        if not waiter.event.wait(self._call_timeout):
            with self._state_lock:
                self._waiters.pop(call_id, None)
            raise CoreUnavailableError(f"The standalone Core did not answer {method}() within {self._call_timeout:g} s.")
        message = waiter.message
        if message is None:
            raise CoreUnavailableError(self._lost.detail if self._lost else "Lost contact with the standalone Core.")
        if message["type"] == wire.ERROR:
            raise wire.decode_error(message.get("error"))
        if method == "poll":
            items = message.get("value")
            if type(items) is not list:
                raise CoreUnavailableError("The standalone Core sent an unreadable poll result.")
            return tuple(self._decode_update(item) for item in items)
        return wire.decode(message.get("value"), validate=False)

    def ping(self) -> bool:
        """Round-trip check. Returns False if the Core does not answer."""
        if self._lost is not None:
            return False
        call_id = next(self._ids)
        waiter = _Waiter()
        with self._state_lock:
            self._waiters[call_id] = waiter
        try:
            with self._send_lock:
                wire.send_message(self._connection, {"type": wire.PING, "id": call_id})
        except (OSError, EOFError):
            return False
        return waiter.event.wait(self._call_timeout) and waiter.message is not None

    def close(self) -> None:
        """Say goodbye; the Core closes this consumer and its subscriptions."""
        if self._closing:
            return
        self._closing = True
        if self._lost is None:
            try:
                with self._send_lock:
                    wire.send_message(self._connection, {"type": wire.BYE})
            except (OSError, EOFError):
                pass
        self._reader.join(2.0)
        if self._reader.is_alive():
            try:
                self._connection.close()
            except OSError:
                pass
        self._mark_lost("This module closed its connection to the standalone Core.")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- ConsumerAPI mirror ---------------------------------------------------------------------

    def poll(self, *, limit: int = 128):
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise CoreRequestError("Batch size must be an integer from 1 to 4096.")
        if self.delivery == wire.DELIVERY_PUSH:
            with self._state_lock:
                batch = []
                if self._status_dropped:
                    batch.append(Gap(self._status_dropped, STATUS_LOSS_REASON))
                    self._status_dropped = 0
                if self._buffer_dropped:
                    batch.append(Gap(self._buffer_dropped, DATA_LOSS_REASON))
                    self._buffer_dropped = 0
                data, status = self._data, self._status
                while (data or status) and len(batch) < limit:
                    if status and (not data or status[0][0] < data[0][0]):
                        batch.append(status.popleft()[1])
                    else:
                        batch.append(data.popleft()[1])
                if data or status:
                    return tuple(batch)
            return tuple(batch) + self._lost_notice(limit - len(batch))
        if self._lost is not None:
            return self._lost_notice(limit)
        try:
            return self._call("poll", limit=limit)
        except CoreUnavailableError:
            return self._lost_notice(limit)

    def _lost_notice(self, room):
        with self._state_lock:
            if self._lost is None or self._lost_reported or room < 1 or self._closing:
                return ()
            self._lost_reported = True
            return (self._lost,)

    def connection(self) -> Connection:
        if self._lost is not None:
            return self._lost
        try:
            return self._call("connection")
        except CoreUnavailableError:
            if self._lost is not None:
                return self._lost
            raise

    def discover(self):
        return self._call("discover")

    def subscribe(self, capability_id, *, interval_ms=None, max_age_ms=None):
        return self._call("subscribe", capability_id, interval_ms=interval_ms, max_age_ms=max_age_ms)

    def subscribe_simvar(self, name, unit, value_type, *, interval_ms=None, max_age_ms=None):
        return self._call("subscribe_simvar", name, unit, value_type, interval_ms=interval_ms, max_age_ms=max_age_ms)

    def subscribe_sdk_simvar(self, variable, *, interval_ms=None, max_age_ms=None):
        return self._call("subscribe_sdk_simvar", variable, interval_ms=interval_ms, max_age_ms=max_age_ms)

    def subscribe_snapshot(self, fields, *, period=None, every=1, changed_only=False):
        kwargs = {"every": every, "changed_only": changed_only}
        if period is not None:
            kwargs["period"] = period
        return self._call("subscribe_snapshot", fields, **kwargs)

    def latest_snapshot(self, subscription_id):
        return self._call("latest_snapshot", subscription_id)

    def latest(self, subscription_id):
        return self._call("latest", subscription_id)

    def request_system_state(self, subscription_id):
        return self._call("request_system_state", subscription_id)

    def set_event_enabled(self, subscription_id, enabled):
        return self._call("set_event_enabled", subscription_id, enabled)

    def request_airport(self, icao, *, region="", filters=()):
        return self._call("request_airport", icao, region=region, filters=filters)

    def request_nearby_airports(self):
        return self._call("request_nearby_airports")

    def request_worldwide_airports(self):
        return self._call("request_worldwide_airports")

    def request_nearby_navaids(self, kind):
        return self._call("request_nearby_navaids", kind)

    def request_navaid(self, kind, icao, *, region=""):
        return self._call("request_navaid", kind, icao, region=region)

    def request_jetways(self, icao, *, parking_indices=()):
        return self._call("request_jetways", icao, parking_indices=parking_indices)

    def facility_result(self, request_id):
        return self._call("facility_result", request_id)

    def request_controllers(self):
        return self._call("request_controllers")

    def request_dialog_notifications(self):
        return self._call("request_dialog_notifications")

    def request_nearby_objects(self, kind, radius_m, field):
        return self._call("request_nearby_objects", kind, radius_m, field)

    def request_object_data(self, field, target=None):
        return self._call("request_object_data", field, target)

    def simulator_result(self, request_id):
        return self._call("simulator_result", request_id)

    def subscribe_facility_cache(self, kind):
        return self._call("subscribe_facility_cache", kind)

    def facility_cache_snapshot(self, subscription_id):
        return self._call("facility_cache_snapshot", subscription_id)

    def submit_command(self, capability_id, argument=None):
        return self._call("submit_command", capability_id, argument)

    def submit_simvar(self, variable, value):
        return self._call("submit_simvar", variable, value)

    def submit_client_event(self, event, arguments=()):
        return self._call("submit_client_event", event, arguments)

    def subscribe_client_event(self, event):
        return self._call("subscribe_client_event", event)

    def submit_simulator_operation(self, operation, *arguments):
        return self._call("submit_simulator_operation", operation, *arguments)

    def subscribe_controller(self, control):
        return self._call("subscribe_controller", control)

    def request_aircraft_inputs(self):
        return self._call("request_aircraft_inputs")

    def aircraft_inputs(self):
        return self._call("aircraft_inputs")

    def request_input_parameters(self, input_id):
        return self._call("request_input_parameters", input_id)

    def input_parameters(self, input_id):
        return self._call("input_parameters", input_id)

    def subscribe_input(self, input_id, *, max_age_ms=None):
        return self._call("subscribe_input", input_id, max_age_ms=max_age_ms)

    def request_input_value(self, subscription_id):
        return self._call("request_input_value", subscription_id)

    def submit_input(self, input_id, arguments=()):
        return self._call("submit_input", input_id, arguments)

    def client_data_areas(self):
        return self._call("client_data_areas")

    def subscribe_client_data(self, region, *, mode=None, max_age_ms=None, changed_only=False):
        kwargs = {"max_age_ms": max_age_ms}
        if mode is not None:
            kwargs["mode"] = mode
        if changed_only:  # sent only when used, so the default call works with any Core build
            kwargs["changed_only"] = changed_only
        return self._call("subscribe_client_data", region, **kwargs)

    def request_client_data(self, subscription_id):
        return self._call("request_client_data", subscription_id)

    def submit_client_data(self, region, payload):
        return self._call("submit_client_data", region, payload)

    def command_result(self, command_id):
        return self._call("command_result", command_id)

    def unsubscribe(self, subscription_id):
        return self._call("unsubscribe", subscription_id)


__all__ = [
    "DATA_LANE_TYPES", "MIN_STATUS_BUFFER", "CoreUnavailableError", "HandshakeRejected", "RemoteConsumerAPI",
    "ServerNotTrusted", "connect",
]
