"""EXPERIMENTAL standalone Core server. Not wired into the EFB and never started by it.

:class:`CoreServer` listens on a local named pipe (Windows) or socket file (elsewhere) and
gives each authenticated client its own Core consumer, exactly as ``Core.register_consumer``
would in-process. When a client leaves, for any reason, its consumer is closed, which ends
its subscriptions and lowers simulator demand. The server never opens a network (TCP/IP) port.

Remote pipe clients
    A Windows named pipe is reachable over SMB (``\\\\<computer>\\pipe\\...``) unless it is
    created with ``PIPE_REJECT_REMOTE_CLIENTS``. The stock ``multiprocessing`` listener does
    not set that flag, so :func:`open_listener` creates every pipe instance with it (see
    :func:`local_pipe_listener_class`). The pipe still uses Windows' default security
    descriptor, not an owner-only DACL, and the flag has not yet been checked on a native
    Windows install; until then, treat the key handshake as the guard against other clients.

Threads
    One accept thread, plus one thread per client that both answers that client's requests
    and (in push mode) streams its updates, so each connection has a single writer. Core's
    own locks make ``ConsumerAPI`` safe to call from these threads while the Core host thread
    pumps the provider. :class:`CorePumpThread` is a small stand-in for that host thread for
    tests and experiments; the real process should run ``bbc_core.host.CoreHost``.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
import multiprocessing.connection as _mp_connection
from multiprocessing.connection import Client, Listener
from typing import Any, Callable, Mapping, Protocol

from ..contract import API_VERSION, CoreRequestError
from . import protocol as wire

log = logging.getLogger(__name__)


# -- Listener ---------------------------------------------------------------------------------

#: CreateNamedPipe dwPipeMode flag: refuse clients connecting from another computer (SMB).
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008


def local_pipe_listener_class(connection_module: Any = None, winapi: Any = None) -> type:
    """A ``multiprocessing`` PipeListener whose pipe instances refuse remote (SMB) clients.

    It repeats CPython's ``PipeListener._new_handle`` (unchanged from 3.10 to 3.12) with
    ``PIPE_REJECT_REMOTE_CLIENTS`` added to the pipe mode. The modules are parameters so the
    flag can be checked off Windows with stand-ins.
    """
    if connection_module is None:
        connection_module = _mp_connection
    if winapi is None:
        import _winapi as winapi  # Windows only
    base = connection_module.PipeListener
    buffer_size = connection_module.BUFSIZE

    class LocalOnlyPipeListener(base):
        def _new_handle(self, first=False):
            flags = winapi.PIPE_ACCESS_DUPLEX | winapi.FILE_FLAG_OVERLAPPED
            if first:
                flags |= winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
            return winapi.CreateNamedPipe(
                self._address, flags,
                winapi.PIPE_TYPE_MESSAGE | winapi.PIPE_READMODE_MESSAGE | winapi.PIPE_WAIT
                | PIPE_REJECT_REMOTE_CLIENTS,
                winapi.PIPE_UNLIMITED_INSTANCES, buffer_size, buffer_size,
                winapi.NMPWAIT_WAIT_FOREVER, winapi.NULL,
            )

    return LocalOnlyPipeListener


def open_listener(address: Any, family: str, backlog: int, *, connection_module: Any = None,
                  winapi: Any = None) -> Listener:
    """A ``Listener`` on a local pipe or socket file. Named pipes refuse remote clients."""
    if family != "AF_PIPE":
        return Listener(address, family=family, backlog=backlog)
    pipe_class = local_pipe_listener_class(connection_module, winapi)
    listener = Listener.__new__(Listener)
    listener._listener = pipe_class(address, backlog)
    listener._authkey = None  # the key handshake runs per client, never in accept()
    return listener


# -- Access control --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ClientHello:
    """What a client asked for. ``client_name`` and ``client_pid`` are self-declared labels."""

    principal: str
    client_name: str
    client_pid: int | None
    protocol_version: int
    api_version: str
    control_groups: frozenset[str]
    diagnostics: bool
    delivery: str
    queue_size: int


@dataclass(frozen=True, slots=True)
class Grant:
    """What a principal may hold. The server grants the intersection with what was asked."""

    control_groups: frozenset[str] = frozenset()
    diagnostics: bool = False


class AccessDenied(Exception):
    """Raised by an access policy to refuse a client, with a plain reason."""


class AccessPolicy(Protocol):
    def grant(self, hello: ClientHello) -> Grant:
        """Return the most this client may hold, or raise AccessDenied."""


class ReadOnlyPolicy:
    """PROPOSAL default: every authenticated client may read; nobody may write."""

    def grant(self, hello: ClientHello) -> Grant:
        return Grant()


class PrincipalPolicy:
    """Grants keyed by principal (the key ID the client proved it holds), not by its name.

    ``default`` applies to a principal with no entry; ``None`` refuses such principals.
    """

    def __init__(self, grants: Mapping[str, Grant], *, default: Grant | None = Grant()):
        self._grants = dict(grants)
        self._default = default

    def grant(self, hello: ClientHello) -> Grant:
        grant = self._grants.get(hello.principal, self._default)
        if grant is None:
            raise AccessDenied("This client is not allowed to use the standalone Core.")
        return grant


@dataclass(frozen=True, slots=True)
class ClientInfo:
    """Server-side view of one connected client, for diagnostics."""

    client_id: str
    principal: str
    client_name: str
    client_pid: int | None
    control_groups: frozenset[str]
    denied_control_groups: frozenset[str]
    diagnostics: bool
    delivery: str
    connected_at: datetime


class ConsumerRegistrar(Protocol):
    """Anything that hands out consumers: ``Core`` itself, or the Core host."""

    def register_consumer(self, *, control_groups=(), diagnostics=False, queue_size=256): ...


class _ReleasingConsumer:
    """A consumer handle whose ``close()`` releases its name in the host that registered it."""

    __slots__ = ("_api", "_release", "_released", "_lock")

    def __init__(self, api, release: Callable[[], object]):
        self._api = api
        self._release = release
        self._released = False
        self._lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self._api, name)

    def close(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()


class _NamedHostRegistrar:
    """Adapter for a ``CoreHost`` that only offers named ``api(name, ...)`` and ``release(name)``.

    Each remote client gets its own unique name, released (and so closed in Core) when the
    client leaves. A host with ``register_consumer`` (shared contract C4) is used directly.
    """

    def __init__(self, host):
        self._host = host
        self._ids = count(1)

    def register_consumer(self, *, control_groups=(), diagnostics=False, queue_size=256):
        name = f"ipc-client-{next(self._ids)}-{os.urandom(3).hex()}"
        api = self._host.api(name, tuple(control_groups), diagnostics, queue_size=queue_size)
        release = getattr(self._host, "release", None)
        return _ReleasingConsumer(api, (lambda: release(name)) if callable(release) else api.close)


def as_registrar(target) -> ConsumerRegistrar:
    """``Core`` or a ``CoreHost`` with ``register_consumer``; else a host with named ``api()``."""
    if callable(getattr(target, "register_consumer", None)):
        return target
    if callable(getattr(target, "api", None)):
        return _NamedHostRegistrar(target)
    raise TypeError("CoreServer needs a Core or a Core host that can register consumers.")


# -- Pump stand-in ---------------------------------------------------------------------------

class CorePumpThread:
    """EXPERIMENTAL stand-in for the Core host thread: calls ``core.pump()`` on a timer.

    Tests and experiments only. The standalone Core process should run the real host.
    """

    def __init__(self, core, *, interval_s: float = 0.01, name: str = "bbc-core-pump"):
        if not 0 < interval_s <= 1:
            raise ValueError("The pump interval must be between 0 and 1 second.")
        self._core = core
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.error: BaseException | None = None

    def start(self) -> CorePumpThread:
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self._core.pump()
            except BaseException as error:  # noqa: BLE001 - record and stop; never spin on a fault
                log.exception("Core pump failed")
                self.error = error
                return

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


# -- Server ----------------------------------------------------------------------------------

@dataclass(slots=True, eq=False)
class _Client:
    connection: Any
    thread: threading.Thread | None = None
    info: ClientInfo | None = None
    api: Any = None
    closed: threading.Event = field(default_factory=threading.Event)
    guard: threading.Lock = field(default_factory=threading.Lock)


class CoreServer:
    """EXPERIMENTAL local server carrying the Core consumer API to other processes.

    ``registrar`` is the ``Core``, or a ``CoreHost`` (its ``register_consumer``, or its
    named ``api()``/``release()`` pair, see :func:`as_registrar`). ``authkeys``
    is one shared secret (principal ``"default"``) or a mapping of principal to secret; a
    client must prove it holds the secret for the principal it names, and the server proves
    it holds the same secret before the client sends anything else.
    """

    def __init__(
        self,
        registrar: ConsumerRegistrar,
        *,
        authkeys: bytes | Mapping[str, bytes],
        address: str | None = None,
        family: str | None = None,
        policy: AccessPolicy | None = None,
        server_name: str = "BBC Core (experimental standalone)",
        max_clients: int = 16,
        max_message_bytes: int = wire.DEFAULT_MAX_MESSAGE_BYTES,
        handshake_timeout_s: float = 5.0,
        push_interval_s: float = 0.02,
        push_batch: int = 256,
        idle_poll_s: float = 0.1,
        max_queue_size: int = 4096,
        api_version: str = API_VERSION,
        protocol_versions: tuple[int, ...] = wire.SUPPORTED_PROTOCOL_VERSIONS,
        schema: str | None = None,
        on_client_change: Callable[[ClientInfo, bool], None] | None = None,
    ):
        if isinstance(authkeys, (bytes, bytearray)):
            authkeys = {"default": authkeys}
        if not authkeys:
            raise ValueError("Supply at least one IPC key.")
        self._keys = {str(name): wire.check_authkey(key) for name, key in authkeys.items()}
        self._registrar = as_registrar(registrar)
        self._family = family or wire.default_family()
        if self._family not in ("AF_PIPE", "AF_UNIX"):
            raise ValueError("The standalone Core listens only on a local named pipe or socket file.")
        self._address = address or wire.default_address(self._family)
        self._policy = policy or ReadOnlyPolicy()
        self.server_name = server_name
        if type(max_clients) is not int or not 1 <= max_clients <= 64:
            raise ValueError("max_clients must be 1 to 64.")
        if type(push_batch) is not int or not 1 <= push_batch <= 4096:
            raise ValueError("push_batch must be 1 to 4096.")
        self._max_clients = max_clients
        self._max_bytes = max_message_bytes
        self._handshake_timeout = handshake_timeout_s
        self._push_interval = push_interval_s
        self._push_batch = push_batch
        self._idle_poll = idle_poll_s
        self._max_queue = max_queue_size
        self._api_version = api_version
        self._protocol_versions = tuple(protocol_versions)
        self._schema = schema or wire.schema_fingerprint()
        self._on_client_change = on_client_change
        self._listener: Listener | None = None
        self._accept_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._clients: dict[int, _Client] = {}
        self._ids = count(1)
        self._stopping = threading.Event()
        self._shutdown_reason = "The standalone Core is shutting down."

    # -- Lifecycle ------------------------------------------------------------------------------

    @property
    def address(self) -> str:
        return self._address

    @property
    def family(self) -> str:
        return self._family

    @property
    def running(self) -> bool:
        return self._accept_thread is not None and self._accept_thread.is_alive()

    def start(self) -> CoreServer:
        if self._listener is not None:
            raise RuntimeError("The server has already been started.")
        if self._family == "AF_UNIX":
            self._clear_stale_socket()
        # No authkey here: the handshake runs in each client's own thread, so one slow or
        # hostile client can never stall the accept loop.
        self._listener = open_listener(self._address, self._family, self._max_clients)
        if self._family == "AF_UNIX":
            os.chmod(self._address, 0o600)
        self._accept_thread = threading.Thread(target=self._accept_loop, name="bbc-core-ipc-accept", daemon=True)
        self._accept_thread.start()
        log.info("Experimental standalone Core listening on %s", self._address)
        return self

    def _clear_stale_socket(self):
        try:
            info = os.lstat(self._address)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise FileExistsError(f"{self._address} exists and is not a socket.")
        try:
            Client(self._address, family="AF_UNIX").close()
        except (ConnectionRefusedError, FileNotFoundError):
            os.unlink(self._address)  # left behind by a Core that did not exit cleanly
        else:
            raise FileExistsError("Another standalone Core is already listening at this address.")

    def close(self, reason: str = "The standalone Core is shutting down.", *, timeout: float = 3.0) -> tuple[str, ...]:
        """Tell every client, close their consumers and stop listening.

        Returns the IDs of clients whose threads did not finish within ``timeout`` (for
        example a client process that stopped reading); their consumers are closed anyway.
        """
        if self._stopping.is_set():
            return ()
        self._shutdown_reason = reason
        self._stopping.set()
        if self._accept_thread is not None and self._accept_thread.is_alive():
            try:  # wake the blocking accept() so the loop sees the stop flag
                Client(self._address, family=self._family).close()
            except OSError:
                pass
            self._accept_thread.join(timeout)
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            clients = tuple(self._clients.values())
        stuck = []
        for client in clients:
            if client.thread is not None:
                client.thread.join(timeout)
            if not client.closed.is_set():
                stuck.append(client.info.client_id if client.info else "unidentified")
                self._release(client)
        return tuple(stuck)

    def __enter__(self):
        return self.start() if self._listener is None else self

    def __exit__(self, *exc):
        self.close()

    def clients(self) -> tuple[ClientInfo, ...]:
        with self._lock:
            return tuple(client.info for client in self._clients.values() if client.info is not None)

    # -- Accept ---------------------------------------------------------------------------------

    def _accept_loop(self):
        while not self._stopping.is_set():
            try:
                connection = self._listener.accept()
            except OSError as error:
                if self._stopping.is_set():
                    break
                log.warning("Accepting a local client failed: %s", error)
                self._stopping.wait(0.05)
                continue
            if self._stopping.is_set():
                connection.close()
                break
            client = _Client(connection)
            with self._lock:
                busy = len(self._clients) >= self._max_clients
                if not busy:
                    key = next(self._ids)
                    self._clients[key] = client
            if busy:
                threading.Thread(target=self._refuse_busy, args=(connection,), name="bbc-core-ipc-busy",
                                 daemon=True).start()
                continue
            client.thread = threading.Thread(
                target=self._serve, args=(key, client), name=f"bbc-core-ipc-client-{key}", daemon=True,
            )
            client.thread.start()

    # -- Per client -----------------------------------------------------------------------------

    def _serve(self, key: int, client: _Client):
        connection = client.connection
        try:
            hello = self._handshake(key, client)
            if hello is not None:
                self._loop(client, hello)
        except (EOFError, OSError):
            pass  # the client went away; cleanup below
        except Exception:  # noqa: BLE001 - one client's fault must never stop the server
            log.exception("A local client session failed")
            self._send_quietly(connection, {"type": wire.FATAL, "reason": "The server failed while serving this client."})
        finally:
            self._release(client)
            with self._lock:
                self._clients.pop(key, None)

    def _refuse_busy(self, connection):
        # Read the hello first, so the client is listening when the refusal arrives.
        try:
            self._receive_handshake(connection)
        except (wire.WireError, EOFError, OSError):
            pass
        self._reject(connection, wire.REJECT_BUSY, "The standalone Core already has its maximum number of clients.")
        try:
            connection.close()
        except OSError:
            pass

    def _release(self, client: _Client):
        with client.guard:
            if client.closed.is_set():
                return
            client.closed.set()
            api, client.api = client.api, None
        if api is not None:
            try:
                api.close()  # ends subscriptions and pending unsent commands, lowers demand
            except Exception:  # noqa: BLE001 - cleanup continues
                log.exception("Closing a remote client's consumer failed")
        try:
            client.connection.close()
        except OSError:
            pass
        if client.info is not None and self._on_client_change is not None:
            self._notify(client.info, False)

    def _notify(self, info, connected):
        try:
            self._on_client_change(info, connected)
        except Exception:  # noqa: BLE001 - a diagnostics callback must not break sessions
            log.exception("The client-change callback failed")

    def _reject(self, connection, code, reason):
        self._send_quietly(connection, {
            "type": wire.REJECT, "code": code, "reason": reason,
            "server_protocol_versions": list(self._protocol_versions), "server_api_version": self._api_version,
        })

    @staticmethod
    def _send_quietly(connection, message):
        try:
            wire.send_message(connection, message)
        except (OSError, EOFError, ValueError):
            pass

    def _receive_handshake(self, connection):
        if not connection.poll(self._handshake_timeout):
            return None
        return wire.recv_message(connection, wire.MAX_HANDSHAKE_BYTES)

    def _handshake(self, key: int, client: _Client) -> ClientHello | None:
        connection = client.connection
        try:
            message = self._receive_handshake(connection)
        except wire.WireError:
            self._reject(connection, wire.REJECT_BAD_REQUEST, "The first message was not a readable hello.")
            return None
        if message is None:
            self._reject(connection, wire.REJECT_TIMEOUT, "No hello arrived in time.")
            return None
        if message.get("type") != wire.HELLO:
            self._reject(connection, wire.REJECT_BAD_REQUEST, "The first message must be a hello.")
            return None
        if self._stopping.is_set():
            self._reject(connection, wire.REJECT_SHUTTING_DOWN, self._shutdown_reason)
            return None
        version = wire.choose_protocol_version(message.get("protocol_versions"), self._protocol_versions)
        if version is None:
            self._reject(connection, wire.REJECT_VERSION, (
                f"This Core speaks local protocol version {self._format_versions(self._protocol_versions)}; "
                f"the client offered {self._format_versions(message.get('protocol_versions'))}."
            ))
            return None
        compatible, reason = wire.api_versions_compatible(str(message.get("api_version")), self._api_version)
        if not compatible:
            self._reject(connection, wire.REJECT_API, reason)
            return None
        if message.get("schema") != self._schema:
            self._reject(connection, wire.REJECT_SCHEMA, (
                "The client and this Core describe the contract types differently "
                "(schema fingerprints differ), so data could be misread. Use matching builds."
            ))
            return None
        try:
            hello = self._parse_hello(message, version)
            client_nonce = wire.unb64(message.get("nonce"), size=wire.NONCE_BYTES)
        except (wire.WireError, ValueError) as error:
            self._reject(connection, wire.REJECT_BAD_REQUEST, str(error))
            return None
        secret = self._keys.get(hello.principal)
        if secret is None:
            self._reject(connection, wire.REJECT_AUTH, "Authentication failed. Check the Core IPC key and principal.")
            return None
        server_nonce = wire.new_nonce()
        digest = wire.hello_digest(message)
        wire.send_message(connection, {
            "type": wire.CHALLENGE, "nonce": wire.b64(server_nonce),
            "proof": wire.proof(secret, "bbc-core-server", client_nonce, server_nonce, digest),
        })
        try:
            reply = self._receive_handshake(connection)
        except wire.WireError:
            reply = {}
        if reply is None:
            self._reject(connection, wire.REJECT_TIMEOUT, "Authentication did not finish in time.")
            return None
        expected = wire.proof(secret, "bbc-core-client", client_nonce, server_nonce, digest)
        if (reply.get("type") != wire.AUTH
                or not wire.proof_matches(expected, reply.get("proof"))):
            self._reject(connection, wire.REJECT_AUTH, "Authentication failed. Check the Core IPC key.")
            return None
        try:
            grant = self._policy.grant(hello)
        except AccessDenied as error:
            self._reject(connection, wire.REJECT_ACCESS, str(error) or "Access denied.")
            return None
        granted = frozenset(hello.control_groups & grant.control_groups)
        denied = frozenset(hello.control_groups - granted)
        diagnostics = hello.diagnostics and grant.diagnostics
        try:
            api = self._registrar.register_consumer(
                control_groups=granted, diagnostics=diagnostics, queue_size=hello.queue_size,
            )
        except CoreRequestError as error:
            self._reject(connection, wire.REJECT_REGISTRATION, str(error))
            return None
        except Exception as error:  # noqa: BLE001 - a stopped host or similar: refuse this client only
            log.warning("Registering a remote consumer failed: %s", error)
            self._reject(connection, wire.REJECT_REGISTRATION,
                         f"The standalone Core could not register this client ({type(error).__name__}: {error}).")
            return None
        with client.guard:
            if client.closed.is_set():  # the server closed this client while it registered
                api.close()
                return None
            client.api = api
        client_id = f"c{key}-{os.urandom(4).hex()}"
        client.info = ClientInfo(
            client_id, hello.principal, hello.client_name, hello.client_pid, granted, denied, diagnostics,
            hello.delivery, datetime.now(timezone.utc),
        )
        initial = getattr(api, "initial_connection", None)
        try:
            payload = wire.encode(initial)
        except wire.WireError:
            payload = None
        wire.send_message(connection, {
            "type": wire.WELCOME, "protocol_version": version, "api_version": self._api_version,
            "schema": self._schema, "server_name": self.server_name, "client_id": client_id,
            "control_groups": sorted(granted), "denied_control_groups": sorted(denied),
            "diagnostics": diagnostics, "delivery": hello.delivery, "initial_connection": payload,
            "max_message_bytes": self._max_bytes,
        })
        if self._on_client_change is not None:
            self._notify(client.info, True)
        return hello

    @staticmethod
    def _format_versions(versions):
        if type(versions) not in (list, tuple) or not versions:
            return "none"
        return ", ".join(str(item) for item in versions)

    def _parse_hello(self, message, version) -> ClientHello:
        principal = message.get("principal", "default")
        name = message.get("client_name", "")
        pid = message.get("client_pid")
        groups = message.get("control_groups", [])
        diagnostics = message.get("diagnostics", False)
        delivery = message.get("delivery", wire.DELIVERY_PULL)
        queue_size = message.get("queue_size", 256)
        if type(principal) is not str or not 1 <= len(principal) <= 64:
            raise ValueError("The principal must be 1 to 64 characters.")
        if type(name) is not str or len(name) > 128:
            raise ValueError("The client name must be text of at most 128 characters.")
        if pid is not None and type(pid) is not int:
            raise ValueError("The client process ID must be an integer.")
        if type(groups) is not list or not all(type(item) is str for item in groups) or len(groups) > 64:
            raise ValueError("Control groups must be a list of names.")
        if type(diagnostics) is not bool:
            raise ValueError("The diagnostics flag must be Boolean.")
        if delivery not in wire.DELIVERY_MODES:
            raise ValueError("Delivery must be 'pull' or 'push'.")
        if type(queue_size) is not int or not 1 <= queue_size <= self._max_queue:
            raise ValueError(f"The queue size must be 1 to {self._max_queue}.")
        return ClientHello(principal, name, pid, version, str(message.get("api_version")),
                           frozenset(groups), diagnostics, delivery, queue_size)

    def _loop(self, client: _Client, hello: ClientHello):
        connection, api = client.connection, client.api
        push = hello.delivery == wire.DELIVERY_PUSH
        backlog = False
        while not self._stopping.is_set():
            wait = 0 if backlog else (self._push_interval if push else self._idle_poll)
            if connection.poll(wait):
                try:
                    message = wire.recv_message(connection, self._max_bytes)
                except wire.WireError as error:
                    self._send_quietly(connection, {"type": wire.FATAL, "reason": str(error)})
                    return
                if not self._handle(connection, api, message, push):
                    return
            if push:
                batch = api.poll(limit=self._push_batch)
                backlog = len(batch) >= self._push_batch
                if batch:
                    wire.send_message(connection, {"type": wire.UPDATES, "items": self._encode_updates(batch)})
        self._send_quietly(connection, {"type": wire.SHUTDOWN, "reason": self._shutdown_reason})

    def _handle(self, connection, api, message, push) -> bool:
        kind = message["type"]
        if kind == wire.CALL:
            wire.send_message(connection, self._call(api, message, push))
            return True
        if kind == wire.PING:
            wire.send_message(connection, {"type": wire.PONG, "id": message.get("id")})
            return True
        if kind == wire.BYE:
            return False
        self._send_quietly(connection, {"type": wire.FATAL, "reason": f"Unexpected message type {kind!r}."})
        return False

    def _call(self, api, message, push) -> dict:
        call_id = message.get("id")
        if type(call_id) is not int:
            return {"type": wire.ERROR, "id": None, "error": {"kind": "WireError", "message": "A call needs an integer id."}}
        method = message.get("method")
        try:
            if method not in wire.CONSUMER_METHODS:
                raise CoreRequestError(f"{method!r} is not a Core consumer method.")
            if push and method == "poll":
                raise CoreRequestError("This connection receives pushed updates; poll() reads them locally.")
            args = message.get("args", [])
            kwargs = message.get("kwargs", {})
            if type(args) is not list or type(kwargs) is not dict or not all(type(k) is str for k in kwargs):
                raise wire.WireError("Call arguments must be a list and an object.")
            args = tuple(wire.decode(item) for item in args)
            kwargs = {name: wire.decode(item) for name, item in kwargs.items()}
            try:
                value = getattr(api, method)(*args, **kwargs)
            except TypeError as error:
                if "argument" in str(error):
                    raise CoreRequestError(f"{method}() was called with the wrong arguments: {error}") from error
                raise
            encoded = self._encode_updates(value) if method == "poll" else wire.encode(value)
        except Exception as error:  # noqa: BLE001 - every failure goes back to the caller
            if not isinstance(error, (CoreRequestError, ValueError)):
                log.exception("Core failed while serving %s", method)
            return {"type": wire.ERROR, "id": call_id, "error": wire.encode_error(error)}
        return {"type": wire.RESULT, "id": call_id, "value": encoded}

    @staticmethod
    def _encode_updates(batch):
        from ..contract import Gap  # local import keeps the module surface small

        items = []
        for update in batch:
            try:
                items.append(wire.encode(update))
            except wire.WireError as error:
                log.error("An update could not be encoded for IPC: %s", error)
                items.append(wire.encode(Gap(1, (
                    f"Core could not send one {type(update).__name__} update over the local interface."
                ))))
        return items


__all__ = [
    "AccessDenied", "AccessPolicy", "ClientHello", "ClientInfo", "ConsumerRegistrar", "CorePumpThread",
    "CoreServer", "Grant", "PrincipalPolicy", "ReadOnlyPolicy", "as_registrar",
]
