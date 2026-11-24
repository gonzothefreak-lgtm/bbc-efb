"""CoreHost: the one worker thread that owns Core and its provider.

SimConnect clients are not thread-safe, and Core expects one serial owner for provider
lifecycle and I/O (see ``bbc_core/provider.py``). ``CoreHost`` is that owner. It is
plain ``threading`` code with no Qt dependency, so Core stays independent of the UI and
the Recorder can run on it while the window is minimised.

What runs where
---------------
On the Core thread only (the thread ``CoreHost.start`` creates):

* ``Core.connect``, ``Core.pump`` and ``Core.disconnect``, and therefore every provider
  call (open, demand, drain, send, close);
* functions handed to ``submit``/``call``/``run`` (they receive the ``Core``; a function
  that takes no arguments is called without it);
* tick callbacks (``add_tick``, also reachable as ``add_pump_listener``), for example the
  Recorder's ``RecorderService.tick`` draining its own ``ConsumerAPI`` so it keeps
  recording while the UI is hidden. They run after every pump and, while no session is
  connected, about every ``HostConfig.idle_tick_s`` (default 0.5 s), so the Recorder still
  sees a lost session, flushes its journal and times gaps when MSFS is gone.

From any thread:

* every ``ConsumerAPI`` method. Core already guards consumer state with its own lock,
  and none of them performs provider I/O, so UI code may poll its handle on the UI
  thread (``bbc_efb.core_bridge.CoreBridge`` does exactly that, in small batches);
* ``status``, ``stats``, ``diagnostics``, ``connection``, ``api``, ``register_consumer``,
  ``release``, ``submit``, ``call``, ``run``, ``add_tick``, ``set_ui_active``,
  ``connect_now``, ``disconnect``, ``set_auto_connect`` and ``stop``.

Shared contract C4 (round 3)
----------------------------
``add_tick(callback)`` -> remover; ``call(fn)`` -> ``concurrent.futures.Future`` (never
blocks; runs inline and returns a finished Future when already on the Core thread; a
finished Future holding ``HostStopped`` after ``stop``); ``register_consumer(**kw)`` ->
``ConsumerAPI``; ``set_ui_active(bool)``; ``connection()`` -> ``Connection``; ``start()``;
``stop(timeout)``. ``run(fn, timeout)`` is the blocking form that returns the result.

Loop
----
Each pass of the worker loop runs queued calls, supervises the connection, pumps Core
when a session is connected, runs pump listeners and then idles until the next tick.
Ticks are on a fixed grid (``HostConfig.pump_interval_s``, default 50 ms) and do not
catch up after an overrun, so a slow pass never causes a burst. When the provider offers
``wait(timeout)`` (the SimConnect adapter does, backed by the SDK's Win32 wake event when
one was created) the worker blocks there instead, so it wakes as soon as SimConnect
queues data, and never later than the next tick. ``HostConfig.min_pump_spacing_s`` stops
a provider whose ``wait`` returns at once from turning the loop into a spin. When the
provider also offers ``wake()``, queued calls and ``stop`` signal it so they do not wait
for the tick. ``SimConnectProvider`` has both (its ``wake()`` signals the SDK's wake
event). For a provider with ``wait()`` but no ``wake()`` the host falls back to the
provider's SDK ``wake()`` (see ``_find_provider_wake``).

Supervision
-----------
With automatic connection on (the default) the worker connects at start. A failed
attempt is retried after 1, 2, 5, 10 and then every 30 s (``HostConfig.backoff_s``), with
an honest status line: "MSFS is not running or SimConnect is unavailable. Retrying in
5 s." When a connected session ends (MSFS quit, transport loss, provider fault) the
worker reconnects after the first backoff step and the schedule restarts. Core keeps
every consumer's subscriptions across sessions and re-applies demand on reconnect, so
modules do not need to resubscribe.

Crash isolation
---------------
An exception escaping Core (Core already turns provider faults into a session error, so
this means a Core or adapter bug) is logged with its traceback, the session is ended, and
the supervisor restarts it with the same backoff. Exceptions from a pump listener are
logged and the listener stays registered; they never end the shared session. A tick
that keeps failing is logged with its traceback the first time and then at most once a
minute with a count, so a broken Recorder cannot flood the log. A tick slower than
``HostConfig.slow_tick_warning_s`` is counted and logged (rate-limited); it delays the next
pump but the loop never bursts to catch up. Exceptions from a submitted function are
returned through its ``Future``.

Shutdown
--------
``stop(timeout)`` asks the worker to finish, fails calls that never ran with
``HostStopped``, disconnects Core on the worker thread and joins it. It returns False and
logs a warning if the worker did not finish in time, which happens only when a provider
call blocks (for example a SimConnect open that has not answered). The thread is a
daemon, so the process can still exit.

Replacing the provider
----------------------
A host built with ``provider_factory`` (the EFB does this for SimConnect) can be given a
different factory later with ``replace_provider_factory(factory, description=...)``, for
example after the pilot chooses another SimConnect.dll. Nothing changes while a session
is connected: the new factory is used at the next connect attempt, after the current
session has ended (or after ``disconnect``/``connect_now``). The new provider must offer
every public attribute the first one offered, because Core decided at construction which
optional provider interfaces it uses; consumers, subscriptions and tick callbacks stay as
they are. When the factory raises, the attempt counts as a failed connect with an honest
status line and the retry schedule applies; the factory is tried again on the next
attempt until it works or is withdrawn with ``replace_provider_factory(None)``.
``set_before_connect(fn)`` registers a callable the worker runs before each connect
attempt, so the owner can re-read a setting (the EFB re-reads the chosen SimConnect.dll)
and hand over a new factory just in time.

UI activity
-----------
``set_ui_active(False)`` records that the window is minimised or hidden. CoreHost itself
keeps pumping (flight logging must not stop), but UI modules can watch the flag
(``ui_active``, ``add_ui_listener`` or the status) and drop display-only subscriptions so
Core lowers simulator demand. ``CoreBridge.watch_window`` sets it from Qt window state.

PROPOSALS for the owner (defaults chosen here, all configurable in ``HostConfig``)
---------------------------------------------------------------------------------
1. Retry schedule: 1, 2, 5, 10, then every 30 s, forever. The EFB is normally started
   before MSFS, so it should keep trying quietly rather than give up.
2. After a session ends, reconnect after 1 s and restart the schedule. Core never
   replays commands across sessions, so a reconnect cannot repeat a write.
3. "Not running" wording: a connect failure that mentions E_FAIL/0x80004005, a failed
   ``SimConnect_Open``, a timed-out OPEN or MSFS quitting before OPEN is shown as "MSFS is
   not running or SimConnect is unavailable." Anything else is shown with its own detail.
   The raw detail is always kept in ``HostStatus.connection.detail``.
4. Internal errors: end the session and restart with backoff, without limit
   (``max_consecutive_crashes=None``). Setting a limit turns automatic connection off
   after that many back-to-back crashes and says so in the status.
5. Minimised window: Core keeps pumping at the normal cadence; only UI consumers react to
   ``ui_active``. The bridge drains its consumers every 500 ms instead of every 50 ms.
6. Tick callbacks also run about every 0.5 s while MSFS is not connected, so the Recorder
   can mark a lost session and flush its journal. A failing tick is never removed
   automatically (that would silently stop Flight Logging); it is logged and counted in
   ``stats()``/``diagnostics()`` instead.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, Protocol, TypeVar

from .contract import Connection, ConnectionState, CoreRequestError, StrEnum
from .core import ConsumerAPI, Core
from .provider import Provider

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
_MAX_CALLS_PER_PASS = 64
_SLOW_PUMP_LOG_INTERVAL_S = 60.0
_TICK_LOG_INTERVAL_S = 60.0
_NOT_RUNNING_MARKERS = (
    "0x80004005", "e_fail", "simconnect_open", "timed out waiting for simconnect open",
    "quit before confirming simconnect open",
)
NOT_RUNNING_TEXT = "MSFS is not running or SimConnect is unavailable."


class HostStopped(RuntimeError):
    """The Core host has stopped (or was never started and is now stopped)."""


class HostState(StrEnum):
    STOPPED = "stopped"        # not started yet, or finished
    STARTING = "starting"      # thread started, first pass not done
    IDLE = "idle"              # running, not connected, automatic connection is off
    CONNECTING = "connecting"  # a connect attempt is in progress on the Core thread
    CONNECTED = "connected"    # a provider session is open and being pumped
    WAITING = "waiting"        # not connected, next automatic attempt is scheduled
    STOPPING = "stopping"      # stop requested, worker still finishing


def describe_connect_failure(detail: str) -> str:
    """Plain-English summary of a failed connect. The raw detail is kept elsewhere."""
    text = (detail or "").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _NOT_RUNNING_MARKERS):
        return NOT_RUNNING_TEXT
    if not text:
        return "Could not connect to the simulator."
    return f"Could not connect to the simulator: {text.rstrip('.')}."


def is_not_running(detail: str) -> bool:
    """True when a connect failure looks like 'the simulator is not there' rather than a fault."""
    lowered = (detail or "").lower()
    return any(marker in lowered for marker in _NOT_RUNNING_MARKERS)


def backoff_delay(failures: int, schedule: tuple[float, ...] = DEFAULT_BACKOFF_S) -> float:
    """Delay before the next attempt after ``failures`` consecutive failures (1-based)."""
    if failures < 1:
        return 0.0
    return float(schedule[min(failures, len(schedule)) - 1])


@dataclass(frozen=True, slots=True)
class HostConfig:
    """CoreHost tuning. Times are seconds."""

    pump_interval_s: float = 0.05
    pump_limit: int = 256
    backoff_s: tuple[float, ...] = DEFAULT_BACKOFF_S
    auto_connect: bool = True
    join_timeout_s: float = 5.0
    call_timeout_s: float = 5.0
    use_provider_wait: bool = True
    min_pump_spacing_s: float = 0.005
    idle_tick_s: float = 0.5
    slow_pump_warning_s: float = 0.25
    slow_tick_warning_s: float = 0.1
    max_consecutive_crashes: int | None = None
    describe_failure: Callable[[str], str] = describe_connect_failure
    thread_name: str = "bbc-core"

    def __post_init__(self):
        if not 0.001 <= self.pump_interval_s <= 5:
            raise ValueError("The pump interval must be 1 ms to 5 s.")
        if type(self.pump_limit) is not int or not 1 <= self.pump_limit <= 4096:
            raise ValueError("The pump batch size must be 1 to 4,096.")
        schedule = tuple(float(step) for step in self.backoff_s)
        if not schedule or any(not 0 < step <= 3600 for step in schedule):
            raise ValueError("The retry schedule needs at least one step, each above 0 and at most 3,600 s.")
        object.__setattr__(self, "backoff_s", schedule)
        if not 0 < self.join_timeout_s <= 120:
            raise ValueError("The shutdown timeout must be above 0 and at most 120 s.")
        if not 0 < self.call_timeout_s <= 600:
            raise ValueError("The call timeout must be above 0 and at most 600 s.")
        if not 0 <= self.min_pump_spacing_s <= self.pump_interval_s:
            raise ValueError("The minimum pump spacing must be between 0 and the pump interval.")
        if not self.pump_interval_s <= self.idle_tick_s <= 60:
            raise ValueError("The idle tick must be between the pump interval and 60 s.")
        if not 0 < self.slow_pump_warning_s <= 60:
            raise ValueError("The slow pump warning must be above 0 and at most 60 s.")
        if not 0 < self.slow_tick_warning_s <= 60:
            raise ValueError("The slow tick warning must be above 0 and at most 60 s.")
        if self.max_consecutive_crashes is not None and (
                type(self.max_consecutive_crashes) is not int or self.max_consecutive_crashes < 1):
            raise ValueError("The crash limit must be a positive whole number or None.")
        if not callable(self.describe_failure):
            raise ValueError("describe_failure must be callable.")


@dataclass(frozen=True, slots=True)
class HostStatus:
    """What the host is doing, in a form a status bar can show directly.

    ``connection`` is Core's own connection record (raw detail included). ``detail`` is
    the plain-English line for the pilot. ``retry_at`` is on the host's monotonic clock;
    use ``retry_in`` for a countdown. ``simulator_unavailable`` is True while the last
    failure looked like "MSFS is not running".
    """

    state: HostState
    connection: Connection
    detail: str
    ui_active: bool = True
    auto_connect: bool = True
    failures: int = 0
    retry_at: float | None = None
    retry_delay_s: float | None = None
    simulator_unavailable: bool = False
    crashes: int = 0
    last_error: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def connected(self) -> bool:
        return self.state is HostState.CONNECTED

    def retry_in(self, now: float | None = None) -> float | None:
        """Seconds until the next automatic attempt, or None when none is scheduled."""
        if self.retry_at is None:
            return None
        return max(0.0, self.retry_at - (monotonic() if now is None else now))


@dataclass(frozen=True, slots=True)
class HostStats:
    """Worker counters for diagnostics. Durations are seconds of host monotonic time."""

    passes: int = 0
    pumps: int = 0
    calls_run: int = 0
    connect_attempts: int = 0
    sessions: int = 0
    crashes: int = 0
    listener_errors: int = 0
    last_pump_s: float = 0.0
    max_pump_s: float = 0.0
    slow_pumps: int = 0
    worker_thread: str = ""
    ticks: int = 0
    tick_errors: int = 0
    slow_ticks: int = 0
    last_tick_s: float = 0.0
    max_tick_s: float = 0.0


class PumpListener(Protocol):
    def __call__(self) -> None: ...


class _Listeners:
    """Copy-on-write listener list, safe to change from any thread."""

    __slots__ = ("_lock", "_items")

    def __init__(self):
        self._lock = threading.Lock()
        self._items: tuple = ()

    def add(self, fn, *, check: bool = True) -> Callable[[], None]:
        if check and not callable(fn):
            raise TypeError("A listener must be callable.")
        with self._lock:
            self._items = self._items + (fn,)

        def remove():
            self.remove_item(fn)
        return remove

    def remove_item(self, fn) -> None:
        with self._lock:
            items = list(self._items)
            if fn in items:
                items.remove(fn)
                self._items = tuple(items)

    def snapshot(self) -> tuple:
        return self._items


def _takes_core(fn: Callable, *, prefer_core: bool) -> bool:
    """Decide whether ``fn`` is called as ``fn(core)`` or ``fn()``.

    ``prefer_core`` picks the answer when both forms bind (a parameter with a default):
    submitted calls historically take the Core, tick callbacks take nothing. A callable
    whose signature cannot be read gets the preferred form.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return prefer_core
    def binds(*args) -> bool:
        try:
            signature.bind(*args)
        except TypeError:
            return False
        return True
    with_core, without = binds(None), binds()
    if with_core and without:
        return prefer_core
    if with_core:
        return True
    if without:
        return False
    raise TypeError(f"{fn!r} must take no arguments or exactly one (the Core).")


def _describe(fn: Callable) -> str:
    name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)
    owner = getattr(getattr(fn, "__self__", None), "__class__", None)
    if owner is not None and "." not in name:
        name = f"{owner.__name__}.{name}"
    return name


class _Tick:
    """One tick callback with its own error and slowness bookkeeping (Core thread only)."""

    __slots__ = ("fn", "name", "with_core", "errors", "suppressed", "last_error_log", "last_slow_log", "last_error")

    def __init__(self, fn: Callable):
        if not callable(fn):
            raise TypeError("A tick callback must be callable.")
        self.fn = fn
        self.name = _describe(fn)
        self.with_core = _takes_core(fn, prefer_core=False)
        self.errors = 0
        self.suppressed = 0
        self.last_error_log: float | None = None
        self.last_slow_log: float | None = None
        self.last_error = ""


class _ProviderSlotBase:
    """Stands in for the provider inside Core so the provider can be replaced while disconnected.

    Each public attribute of the first provider is forwarded by a class-level property, so
    ``isinstance`` checks against Core's runtime-checkable provider protocols (which look
    attributes up statically) see the same interfaces the real provider offers.
    """

    __slots__ = ("_current",)
    _forwarded: frozenset = frozenset()

    def __init__(self, provider):
        self._current = provider

    def __repr__(self) -> str:
        return f"<replaceable {self._current!r}>"


def _public_names(provider) -> frozenset:
    return frozenset(name for name in dir(provider) if not name.startswith("_"))


def _forward(name: str) -> property:
    return property(lambda self: getattr(self._current, name), doc=f"Forwarded to the current provider's {name}.")


def _provider_slot(provider) -> _ProviderSlotBase:
    names = _public_names(provider)
    namespace: dict[str, Any] = {name: _forward(name) for name in names}
    namespace["__slots__"] = ()
    namespace["_forwarded"] = names
    cls = type(f"Replaceable{type(provider).__name__}", (_ProviderSlotBase,), namespace)
    return cls(provider)


class CoreHost:
    """Own ``Core`` and its provider on one worker thread. See the module docstring.

    Pass either a ready ``core`` built on ``provider``, or let the host build one with
    ``core_factory(provider)`` (default ``Core``). Building Core touches provider metadata
    only, never provider I/O, so it is safe on the caller's thread.

    With ``provider_factory`` the provider can be replaced later while disconnected (see
    "Replacing the provider" in the module docstring). ``provider`` may then be omitted,
    and the factory builds the first one. A ready ``core`` cannot be combined with it.
    """

    def __init__(self, provider: Provider | None = None, *, config: HostConfig | None = None,
                 core: Core | None = None, core_factory: Callable[[Provider], Core] = Core,
                 clock: Callable[[], float] = monotonic,
                 provider_factory: Callable[[], Provider] | None = None):
        self._config = config if config is not None else HostConfig()
        if provider_factory is not None and not callable(provider_factory):
            raise TypeError("provider_factory must be callable.")
        if provider_factory is not None and core is not None:
            raise ValueError("A replaceable provider cannot be combined with a ready-made Core.")
        if provider is None:
            if provider_factory is None:
                raise ValueError("Supply a provider or a provider_factory.")
            provider = provider_factory()
        self._provider = provider
        self._slot = _provider_slot(provider) if provider_factory is not None else None
        self._pending_factory: tuple[Callable[[], Provider], str] | None = None
        self._before_connect: Callable[[], Any] | None = None
        self._provider_description = ""
        core_provider = self._slot if self._slot is not None else provider
        self._core = core if core is not None else core_factory(core_provider)
        self._clock = clock
        self._lock = threading.RLock()
        self._calls: deque[tuple[Callable, Future]] = deque()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._finished = threading.Event()
        self._final_status = False  # set once the worker reports its last (STOPPED) status
        self._thread: threading.Thread | None = None
        self._thread_ident: int | None = None
        self._consumers: dict[str, ConsumerAPI] = {}
        self._ticks = _Listeners()  # holds _Tick entries; see add_tick
        self._last_tick_at: float | None = None
        self._status_listeners = _Listeners()
        self._ui_listeners = _Listeners()
        self._ui_active = True
        # The host's own handle: it reads the connection and drains its status lane.
        self._watch = self._core.register_consumer(queue_size=16)
        # Supervisor state. Touched only on the worker thread once it runs.
        self._auto_connect = self._config.auto_connect
        self._failures = 0
        self._crash_streak = 0
        self._next_attempt: float | None = None
        self._session_id: str | None = None
        self._last_error = ""
        self._simulator_unavailable = False
        self._last_slow_log = -_SLOW_PUMP_LOG_INTERVAL_S
        self._provider_wait = self._find_provider_wait()
        self._provider_wake = self._find_provider_wake()
        self._stats = HostStats()
        self._status = HostStatus(
            HostState.STOPPED, self._watch.connection(), "Not started.",
            auto_connect=self._auto_connect,
        )

    # -- Public: lifecycle ---------------------------------------------------------------------

    @property
    def config(self) -> HostConfig:
        return self._config

    @property
    def provider(self) -> Provider:
        """The provider. Touch it only from the Core thread (``submit``/``call``)."""
        return self._provider

    @property
    def provider_replaceable(self) -> bool:
        """True when this host was built with ``provider_factory``."""
        return self._slot is not None

    @property
    def pending_provider(self) -> str | None:
        """The description of a replacement waiting for the next connect attempt, or None."""
        pending = self._pending_factory
        return pending[1] if pending is not None else None

    def replace_provider_factory(self, factory: Callable[[], Provider] | None, *,
                                 description: str = "") -> None:
        """Use ``factory`` from the next connect attempt on. ``None`` withdraws a waiting one.

        Safe from any thread. A connected session is never touched: the replacement waits
        until the session has ended. Raises ``RuntimeError`` when the host was not built
        with ``provider_factory``.
        """
        if self._slot is None:
            raise RuntimeError("This Core host was not built with a provider factory, so its provider "
                               "cannot be replaced.")
        if factory is not None and not callable(factory):
            raise TypeError("The provider factory must be callable.")
        with self._lock:
            self._pending_factory = None if factory is None else (factory, str(description or "a new provider"))
        if factory is not None:
            log.info("A replacement provider (%s) will be used at the next connect attempt.",
                     description or "a new provider")
        self._notify_worker()

    def set_before_connect(self, fn: Callable[[], Any] | None) -> None:
        """Run ``fn()`` on the Core thread before every connect attempt (None removes it).

        Exceptions are logged and the attempt goes ahead with the current provider.
        """
        if fn is not None and not callable(fn):
            raise TypeError("The before-connect hook must be callable.")
        self._before_connect = fn

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def worker_thread(self) -> threading.Thread | None:
        return self._thread

    def on_core_thread(self) -> bool:
        return self._thread_ident is not None and threading.get_ident() == self._thread_ident

    def start(self) -> CoreHost:
        """Start the worker thread. A host runs once; build a new host to run again."""
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("This Core host was already started.")
            if self._stopping.is_set():
                raise HostStopped("This Core host was stopped before it started.")
            self._thread = threading.Thread(target=self._run, name=self._config.thread_name, daemon=True)
            self._set_status(HostState.STARTING, "Starting the Core thread.")
            self._thread.start()
        return self

    def stop(self, timeout: float | None = None) -> bool:
        """Stop the worker, disconnect Core and join. Returns True when the worker finished."""
        timeout = self._config.join_timeout_s if timeout is None else timeout
        if self.on_core_thread():
            raise RuntimeError("stop() cannot be called from the Core thread; call it from the host's owner.")
        with self._lock:
            thread = self._thread
            already = self._stopping.is_set()
            self._stopping.set()
            if thread is None:
                self._fail_queued_calls()
                if not already:
                    self._set_status(HostState.STOPPED, "Stopped. The Core thread never started.")
                self._final_status = True
                self._finished.set()
                return True
        if not already:
            self._set_status(HostState.STOPPING, "Stopping the Core thread.")
        self._notify_worker()
        thread.join(timeout)
        if thread.is_alive():
            log.warning("The Core thread did not stop within %.1f s; a provider call or tick callback is "
                        "probably blocked.", timeout)
            self._set_status(
                HostState.STOPPING,
                f"The Core thread did not stop within {timeout:g} s. A simulator call or tick callback may be "
                "blocked; the session will be closed when it returns.",
            )
            return False
        return True

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def __enter__(self) -> CoreHost:
        if self._thread is None:
            self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- Public: consumers ---------------------------------------------------------------------

    def api(self, name: str, control_groups=(), diagnostics: bool = False, *, queue_size: int = 256) -> ConsumerAPI:
        """Register a named consumer and return its handle. Names are unique per host."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A consumer needs a non-empty name.")
        with self._lock:
            if name in self._consumers:
                raise ValueError(f"A consumer named {name!r} is already registered.")
            api = self._core.register_consumer(
                control_groups=control_groups, diagnostics=diagnostics, queue_size=queue_size,
            )
            self._consumers[name] = api
        self._notify_worker()
        return api

    def register_consumer(self, *, name: str | None = None, control_groups=(), diagnostics: bool = False,
                          queue_size: int = 256) -> ConsumerAPI:
        """Register a consumer with the hosted Core and return its handle (C4).

        Same keywords as ``Core.register_consumer``, so anything that hands out consumers
        (the IPC server, module host) can use a CoreHost in place of a Core. With
        ``name`` it is the same as ``api(name, ...)`` (unique, listed, releasable).
        """
        if name is not None:
            return self.api(name, control_groups, diagnostics, queue_size=queue_size)
        api = self._core.register_consumer(
            control_groups=control_groups, diagnostics=diagnostics, queue_size=queue_size,
        )
        self._notify_worker()
        return api

    def connection(self) -> Connection:
        """Core's current connection record. Safe from any thread (C4)."""
        try:
            return self._watch.connection()
        except CoreRequestError:
            return self._status.connection

    def consumer(self, name: str) -> ConsumerAPI | None:
        with self._lock:
            return self._consumers.get(name)

    def consumer_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._consumers)

    def release(self, name: str) -> bool:
        """Close a named consumer. Returns False when the name is unknown."""
        with self._lock:
            api = self._consumers.pop(name, None)
        if api is None:
            return False
        try:
            api.close()
        except CoreRequestError:
            pass
        self._notify_worker()
        return True

    # -- Public: running code on the Core thread -----------------------------------------------

    def submit(self, fn: Callable[[Core], T]) -> Future:
        """Queue ``fn(core)`` to run on the Core thread. Returns a Future for its result.

        Calls queued before ``start`` run on the first pass. Raises ``HostStopped`` once
        ``stop`` has been called.
        """
        if not callable(fn):
            raise TypeError("submit() needs a callable that takes the Core.")
        future: Future = Future()
        with self._lock:
            if self._stopping.is_set():
                raise HostStopped("The Core host has stopped, so it cannot run more calls.")
            self._calls.append((fn, future))
        self._notify_worker()
        return future

    def call(self, fn: Callable[..., T]) -> Future:
        """Run ``fn(core)`` (or ``fn()``) on the Core thread; return a ``Future`` at once (C4).

        Never blocks the caller, so it is safe from the UI thread. When already on the
        Core thread it runs inline and returns a finished Future, so a tick or a queued
        call can use it without deadlocking. After ``stop`` the Future is already failed
        with ``HostStopped`` instead of raising, so UI callers handle one error path.
        Use ``run`` for the blocking form.
        """
        if not callable(fn):
            raise TypeError("call() needs a callable.")
        if self.on_core_thread():
            future: Future = Future()
            future.set_running_or_notify_cancel()
            try:
                result = self._invoke(fn)
            except Exception as error:  # noqa: BLE001 - delivered through the Future
                future.set_exception(error)
            else:
                future.set_result(result)
            return future
        try:
            return self.submit(fn)
        except HostStopped as error:
            failed: Future = Future()
            failed.set_running_or_notify_cancel()
            failed.set_exception(error)
            return failed

    def run(self, fn: Callable[..., T], timeout: float | None = None) -> T:
        """Run ``fn(core)`` on the Core thread and return its result (blocking).

        Runs inline when already on the Core thread. Do not use it on the UI thread for
        anything slow; use ``call`` (or ``CoreBridge.run_on_core``). On timeout the call
        is cancelled if it has not started and ``TimeoutError`` is raised.
        """
        if self.on_core_thread():
            return self._invoke(fn)
        timeout = self._config.call_timeout_s if timeout is None else timeout
        future = self.submit(fn)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError(f"The Core thread did not run the call within {timeout:g} s.") from None

    def _invoke(self, fn):
        return fn(self._core) if _takes_core(fn, prefer_core=True) else fn()

    # -- Public: supervision controls ----------------------------------------------------------

    def connect_now(self) -> Future:
        """Turn automatic connection on and try at once, restarting the retry schedule."""
        def run(_core):
            self._auto_connect = True
            self._failures = 0
            self._crash_streak = 0
            self._next_attempt = self._clock()
        return self.submit(run)

    def disconnect(self) -> Future:
        """End the session and turn automatic connection off until ``connect_now``."""
        def run(core):
            self._auto_connect = False
            self._next_attempt = None
            core.disconnect()
            self._session_id = None
            self._set_status(HostState.IDLE, "Disconnected. Automatic connection is off.")
        return self.submit(run)

    def set_auto_connect(self, enabled: bool) -> Future:
        def run(_core):
            self._auto_connect = bool(enabled)
            if enabled and self._next_attempt is None:
                self._next_attempt = self._clock()
            if not enabled:
                self._next_attempt = None
        return self.submit(run)

    # -- Public: status and hooks --------------------------------------------------------------

    def status(self) -> HostStatus:
        return self._status

    def stats(self) -> HostStats:
        return self._stats

    def diagnostics(self) -> dict[str, Any]:
        """Plain values for Settings > SimConnect diagnostics. Safe from any thread.

        ``CoreBridge.diagnostics()`` is expected to include this dictionary. Nothing in it
        touches the provider, so it never waits for the Core thread.
        """
        status, stats = self._status, self._stats
        connection = status.connection
        source = getattr(connection, "source", None)
        ticks = []
        for entry in self._ticks.snapshot():
            ticks.append({"name": entry.name, "errors": entry.errors, "last_error": entry.last_error})
        return {
            "state": status.state.value,
            "detail": status.detail,
            "connection_state": getattr(getattr(connection, "state", None), "value", str(connection.state)),
            "connection_detail": getattr(connection, "detail", ""),
            "session_id": getattr(connection, "session_id", None),
            "source": getattr(source, "name", None),
            "simulated": bool(getattr(source, "simulated", False)),
            "auto_connect": status.auto_connect,
            "ui_active": status.ui_active,
            "failures": status.failures,
            "retry_in_s": status.retry_in(self._clock()) if status.retry_at is not None else None,
            "simulator_unavailable": status.simulator_unavailable,
            "crashes": status.crashes,
            "last_error": status.last_error,
            "running": self.running,
            "worker_thread": stats.worker_thread,
            "provider_wait": self._provider_wait is not None,
            "provider_wake": self._provider_wake is not None,
            "provider_replaceable": self._slot is not None,
            "pending_provider": self.pending_provider,
            "pump_interval_ms": round(self._config.pump_interval_s * 1000),
            "passes": stats.passes,
            "pumps": stats.pumps,
            "calls_run": stats.calls_run,
            "connect_attempts": stats.connect_attempts,
            "sessions": stats.sessions,
            "last_pump_ms": round(stats.last_pump_s * 1000, 1),
            "max_pump_ms": round(stats.max_pump_s * 1000, 1),
            "slow_pumps": stats.slow_pumps,
            "ticks": stats.ticks,
            "tick_errors": stats.tick_errors,
            "slow_ticks": stats.slow_ticks,
            "max_tick_ms": round(stats.max_tick_s * 1000, 1),
            "tick_callbacks": ticks,
            "consumers": list(self.consumer_names()),
        }

    def add_status_listener(self, fn: Callable[[HostStatus], None]) -> Callable[[], None]:
        """Call ``fn(status)`` on every status change. Returns a function that removes it.

        Listeners run on the thread that changed the status (usually the Core thread).
        Keep them short and thread-safe; a Qt receiver should hop threads with a queued
        signal, as CoreBridge does.
        """
        return self._status_listeners.add(fn)

    def add_tick(self, callback: Callable[..., Any]) -> Callable[[], None]:
        """Run ``callback()`` (or ``callback(core)``) on the Core thread after each pump (C4).

        While no session is connected it also runs about every ``idle_tick_s``, so a
        Recorder notices the lost session and keeps its journal and gap timers moving.
        Exceptions are logged (rate-limited) and counted; the callback stays registered
        and the session is not touched. Returns a function that removes the callback;
        once it returns, the callback will not be started again (one already running on
        the Core thread finishes).
        """
        entry = _Tick(callback)
        remove_entry = self._ticks.add(entry, check=False)
        self._notify_worker()
        return remove_entry

    def remove_tick(self, callback: Callable[..., Any]) -> bool:
        """Remove every registration of ``callback``. Returns False when none was found."""
        removed = False
        for entry in self._ticks.snapshot():
            if entry.fn == callback:
                self._ticks.remove_item(entry)
                removed = True
        return removed

    def add_pump_listener(self, fn: Callable[..., Any]) -> Callable[[], None]:
        """Older name for ``add_tick``; kept for existing callers."""
        return self.add_tick(fn)

    def add_ui_listener(self, fn: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``fn(active)`` when ``set_ui_active`` changes the flag. Returns a remover."""
        return self._ui_listeners.add(fn)

    @property
    def ui_active(self) -> bool:
        return self._ui_active

    def set_ui_active(self, active: bool) -> None:
        """Record whether the UI is visible. Listeners run on the calling thread."""
        active = bool(active)
        with self._lock:
            if active == self._ui_active:
                return
            self._ui_active = active
            self._status = replace(self._status, ui_active=active, updated_at=datetime.now(timezone.utc))
            status = self._status
        log.info("UI is now %s.", "active" if active else "inactive (minimised or hidden)")
        for listener in self._ui_listeners.snapshot():
            self._safe(listener, active, what="UI activity listener")
        for listener in self._status_listeners.snapshot():
            self._safe(listener, status, what="status listener")

    # -- Worker --------------------------------------------------------------------------------

    def _run(self):
        self._thread_ident = threading.get_ident()
        self._stats = replace(self._stats, worker_thread=threading.current_thread().name)
        self._next_attempt = self._clock() if self._auto_connect else None
        next_tick = self._clock()
        try:
            while not self._stopping.is_set():
                self._wake.clear()
                self._guarded(self._run_calls)
                if self._stopping.is_set():
                    break
                self._guarded(self._supervise)
                if self._stopping.is_set():
                    break
                pumped = False
                if self._session_id is not None:
                    pumped = self._guarded(self._pump)
                    if self._stopping.is_set():
                        break
                    # Report a session that ended inside this pump without waiting a tick.
                    self._guarded(self._observe_connection)
                if pumped or self._idle_tick_due():
                    self._run_ticks()
                    if self._stopping.is_set():
                        break
                self._stats = replace(self._stats, passes=self._stats.passes + 1)
                next_tick = self._idle(next_tick)
        except BaseException:
            log.exception("The Core thread was interrupted")
            raise
        finally:
            self._shutdown_on_worker()

    def _guarded(self, step) -> bool:
        try:
            result = step()
        except Exception as error:  # noqa: BLE001 - crash isolation: log, end session, restart
            self._crash(error)
            return False
        return True if result is None else bool(result)

    def _run_calls(self):
        for _ in range(_MAX_CALLS_PER_PASS):
            with self._lock:
                if not self._calls:
                    return
                fn, future = self._calls.popleft()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = self._invoke(fn)
            except Exception as error:  # noqa: BLE001 - the caller receives it through the Future
                log.debug("A call on the Core thread raised %r", error)
                future.set_exception(error)
            except BaseException as error:
                future.set_exception(error)
                raise
            else:
                future.set_result(result)
            finally:
                self._stats = replace(self._stats, calls_run=self._stats.calls_run + 1)

    def _observe_connection(self) -> Connection:
        """Drain the host's status lane and react to a session that ended."""
        while self._watch.poll(limit=16):
            pass
        connection = self._watch.connection()
        if connection.state is ConnectionState.CONNECTED:
            if self._session_id != connection.session_id:
                # Connected by a submitted call rather than by the supervisor.
                self._on_connected(connection)
            return connection
        if self._session_id is not None:
            self._session_id = None
            ended = connection.detail or "The simulator session ended."
            self._last_error = ended
            self._failures = 0
            if self._auto_connect:
                delay = backoff_delay(1, self._config.backoff_s)
                self._next_attempt = self._clock() + delay
                log.info("Simulator session ended (%s); reconnecting in %g s.", ended, delay)
                self._set_status(
                    HostState.WAITING,
                    f"The simulator session ended: {ended.rstrip('.')}. Reconnecting in {delay:g} s.",
                    retry_delay_s=delay,
                )
            else:
                self._set_status(HostState.IDLE, f"The simulator session ended: {ended.rstrip('.')}. "
                                                 "Automatic connection is off.")
        return connection

    def _supervise(self):
        connection = self._observe_connection()
        if connection.state is ConnectionState.CONNECTED:
            return
        if not self._auto_connect:
            if self._status.state not in (HostState.IDLE,):
                self._set_status(HostState.IDLE, "Not connected. Automatic connection is off.")
            return
        now = self._clock()
        if self._next_attempt is None:
            self._next_attempt = now
        if now < self._next_attempt:
            return
        self._attempt_connect()

    def _attempt_connect(self):
        attempt = self._failures + 1
        self._stats = replace(self._stats, connect_attempts=self._stats.connect_attempts + 1)
        swapped = self._prepare_provider()
        if swapped is None:
            return  # the replacement could not be built; status and retry are already set
        using = f" using {self._provider_description}" if swapped and self._provider_description else ""
        self._set_status(
            HostState.CONNECTING,
            f"Connecting to the simulator{using}." if attempt == 1
            else f"Connecting to the simulator{using} (attempt {attempt}).",
        )
        connection = self._core.connect()
        while self._watch.poll(limit=16):
            pass
        if connection.state is ConnectionState.CONNECTED:
            self._on_connected(connection)
            return
        self._failures += 1
        delay = backoff_delay(self._failures, self._config.backoff_s)
        self._next_attempt = self._clock() + delay
        self._last_error = connection.detail
        self._simulator_unavailable = is_not_running(connection.detail)
        summary = self._config.describe_failure(connection.detail)
        log.info("Connect attempt %d failed (%s); retrying in %g s.", attempt, connection.detail, delay)
        self._set_status(HostState.WAITING, f"{summary} Retrying in {delay:g} s.", retry_delay_s=delay)

    def _prepare_provider(self) -> bool | None:
        """Run the before-connect hook and apply a waiting replacement (Core thread, disconnected).

        Returns True when the provider was replaced, False when nothing changed, and None
        when the replacement failed (counted as a failed attempt).
        """
        hook = self._before_connect
        if hook is not None:
            try:
                hook()
            except Exception:  # noqa: BLE001 - never block connecting on the owner's hook
                log.exception("The before-connect hook failed; connecting with the current provider")
        with self._lock:
            pending = self._pending_factory
        if pending is None or self._slot is None:
            return False
        if getattr(self._core, "_cleanup_required", False):
            # The previous session's provider still needs its cleanup; it must run on that provider.
            self._core.disconnect()
            if getattr(self._core, "_cleanup_required", False):
                return False
        factory, description = pending
        try:
            provider = factory()
            missing = self._slot._forwarded - _public_names(provider)
            if missing:
                names = sorted(missing)
                more = f" and {len(names) - 5} more" if len(names) > 5 else ""
                raise ValueError("it is not the same kind of provider (it lacks " + ", ".join(names[:5]) + more + ")")
        except Exception as error:  # noqa: BLE001 - reported to the pilot, retried with backoff
            log.warning("The replacement provider (%s) could not be prepared: %r", description, error)
            self._failures += 1
            delay = backoff_delay(self._failures, self._config.backoff_s)
            self._next_attempt = self._clock() + delay
            self._last_error = f"{description} could not be used: {error}"
            self._simulator_unavailable = False
            self._set_status(HostState.WAITING, f"{description[:1].upper()}{description[1:]} could not be used: "
                                                f"{error}. Retrying in {delay:g} s.", retry_delay_s=delay)
            return None
        with self._lock:
            if self._pending_factory is pending:
                self._pending_factory = None
        self._provider = provider
        self._slot._current = provider
        self._provider_description = description
        self._provider_wait = self._find_provider_wait()
        self._provider_wake = self._find_provider_wake()
        log.info("The provider was replaced (%s).", description)
        return True

    def _on_connected(self, connection: Connection):
        self._session_id = connection.session_id
        self._failures = 0
        self._crash_streak = 0
        self._next_attempt = None
        self._simulator_unavailable = False
        self._stats = replace(self._stats, sessions=self._stats.sessions + 1)
        source = connection.source
        suffix = " (simulated)" if getattr(source, "simulated", False) else ""
        log.info("Connected to %s%s.", source.name, suffix)
        self._set_status(HostState.CONNECTED, f"Connected to {source.name}{suffix}.")

    def _pump(self):
        started = self._clock()
        self._core.pump(limit=self._config.pump_limit)
        elapsed = self._clock() - started
        stats = self._stats
        slow = elapsed >= self._config.slow_pump_warning_s
        self._stats = replace(
            stats, pumps=stats.pumps + 1, last_pump_s=elapsed, max_pump_s=max(stats.max_pump_s, elapsed),
            slow_pumps=stats.slow_pumps + (1 if slow else 0),
        )
        if slow and started - self._last_slow_log >= _SLOW_PUMP_LOG_INTERVAL_S:
            self._last_slow_log = started
            log.warning("A Core pump took %.0f ms (warning threshold %.0f ms).",
                        elapsed * 1000, self._config.slow_pump_warning_s * 1000)
        return True

    def _idle_tick_due(self) -> bool:
        """While no session is pumped, tick about every ``idle_tick_s``."""
        if self._session_id is not None:
            return False
        last = self._last_tick_at
        return last is None or self._clock() - last >= self._config.idle_tick_s - 1e-3

    def _run_ticks(self):
        self._last_tick_at = self._clock()
        for entry in self._ticks.snapshot():
            if self._stopping.is_set():
                return
            if entry not in self._ticks.snapshot():
                continue  # removed by an earlier callback in this pass
            self._run_tick(entry)

    def _run_tick(self, entry: _Tick):
        started = self._clock()
        failed = None
        try:
            if entry.with_core:
                entry.fn(self._core)
            else:
                entry.fn()
        except Exception as error:  # noqa: BLE001 - one callback must never break the host
            failed = error
        elapsed = self._clock() - started
        stats = self._stats
        slow = elapsed >= self._config.slow_tick_warning_s
        self._stats = replace(
            stats, ticks=stats.ticks + 1, last_tick_s=elapsed, max_tick_s=max(stats.max_tick_s, elapsed),
            slow_ticks=stats.slow_ticks + (1 if slow else 0),
            tick_errors=stats.tick_errors + (1 if failed is not None else 0),
            listener_errors=stats.listener_errors + (1 if failed is not None else 0),
        )
        if failed is not None:
            entry.errors += 1
            entry.last_error = repr(failed)
            if entry.last_error_log is None or started - entry.last_error_log >= _TICK_LOG_INTERVAL_S:
                more = f" ({entry.suppressed} more failures since the last report)" if entry.suppressed else ""
                log.error("Tick callback %s raised; it stays registered and the session continues%s",
                          entry.name, more, exc_info=(type(failed), failed, failed.__traceback__))
                entry.last_error_log = started
                entry.suppressed = 0
            else:
                entry.suppressed += 1
                log.debug("Tick callback %s raised again: %r", entry.name, failed)
        if slow and (entry.last_slow_log is None or started - entry.last_slow_log >= _TICK_LOG_INTERVAL_S):
            entry.last_slow_log = started
            log.warning("Tick callback %s took %.0f ms on the Core thread (warning threshold %.0f ms); "
                        "it delays the next pump.", entry.name, elapsed * 1000,
                        self._config.slow_tick_warning_s * 1000)

    def _crash(self, error: BaseException):
        log.exception("The Core thread hit an internal error; ending the session: %r", error)
        self._crash_streak += 1
        self._failures += 1
        self._last_error = repr(error)
        self._stats = replace(self._stats, crashes=self._stats.crashes + 1)
        try:
            self._core.disconnect()
        except Exception:  # noqa: BLE001 - keep the worker alive whatever cleanup does
            log.exception("Ending the session after an internal error also failed")
        self._session_id = None
        limit = self._config.max_consecutive_crashes
        if limit is not None and self._crash_streak >= limit:
            self._auto_connect = False
            self._next_attempt = None
            self._set_status(
                HostState.IDLE,
                f"The Core thread stopped connecting after {self._crash_streak} internal errors in a row "
                f"(last: {error!r}). Use Reconnect to try again.",
            )
            return
        if not self._auto_connect:
            self._set_status(HostState.IDLE, f"The Core thread hit an internal error and ended the session: "
                                             f"{error!r}. Automatic connection is off.")
            return
        delay = backoff_delay(self._failures, self._config.backoff_s)
        self._next_attempt = self._clock() + delay
        self._set_status(
            HostState.WAITING,
            f"The Core thread hit an internal error and ended the session: {error!r}. Restarting in {delay:g} s.",
            retry_delay_s=delay,
        )

    def _idle(self, next_tick: float) -> float:
        """Sleep until the next tick (or data, or a wake). Returns the following tick time."""
        if self._stopping.is_set():
            return next_tick
        interval = self._config.pump_interval_s
        now = self._clock()
        next_tick += interval
        if next_tick <= now:
            next_tick = now + interval  # overran: skip missed ticks rather than burst
        if self._session_id is None:
            # Not pumping: sleep until the next attempt, bounded so calls and stats stay fresh.
            timeout = self._config.idle_tick_s
            if self._last_tick_at is not None and self._ticks.snapshot():
                # Wake in time for the next idle tick even after an early wake-up.
                timeout = min(timeout, max(0.0, self._last_tick_at + self._config.idle_tick_s - now))
            if self._next_attempt is not None:
                timeout = min(timeout, max(0.0, self._next_attempt - now))
            if self._wait_wake(timeout):
                return self._clock()
            return next_tick if self._next_attempt is None else max(next_tick, self._clock())
        timeout = max(0.0, next_tick - now)
        waiter = self._provider_wait
        if waiter is not None and not self._has_work():
            started = self._clock()
            try:
                waiter(timeout)
            except Exception:  # noqa: BLE001 - fall back to plain sleeping
                log.exception("The provider's wait() failed; the Core thread will poll on its timer instead")
                self._provider_wait = None
                self._wait_wake(timeout)
                return next_tick
            spent = self._clock() - started
            if spent < self._config.min_pump_spacing_s:
                self._wait_wake(self._config.min_pump_spacing_s - spent)
            return next_tick
        self._wait_wake(timeout)
        return next_tick

    def _wait_wake(self, timeout: float) -> bool:
        if timeout <= 0:
            return self._wake.is_set()
        return self._wake.wait(timeout)

    def _has_work(self) -> bool:
        return self._wake.is_set() or bool(self._calls) or self._stopping.is_set()

    def _find_provider_wake(self):
        """The provider's ``wake()``, or its SDK's when the provider only offers ``wait()``.

        ``SimConnectProvider.wait`` blocks on ``SimConnectSdk``'s Win32 wake event and
        ``SimConnectProvider.wake`` signals it. The SDK fallback serves a provider that has
        ``wait()`` but no ``wake()``; ``SimConnectSdk.wake`` touches only the event handle,
        so any thread may call it.
        """
        wake = getattr(self._provider, "wake", None)
        if callable(wake):
            return wake
        if self._provider_wait is None:
            return None
        for attribute in ("sdk", "_sdk"):
            sdk = getattr(self._provider, attribute, None)
            wake = getattr(sdk, "wake", None)
            if sdk is not None and callable(wake):
                return wake
        return None

    def _find_provider_wait(self):
        if not self._config.use_provider_wait:
            return None
        waiter = getattr(self._provider, "wait", None)
        return waiter if callable(waiter) else None

    def _notify_worker(self):
        self._wake.set()
        if self._provider_wake is not None and self._thread is not None:
            try:
                self._provider_wake()
            except Exception:  # noqa: BLE001 - the tick still picks the work up
                log.debug("The provider's wake() failed", exc_info=True)

    def _shutdown_on_worker(self):
        try:
            self._fail_queued_calls()
            try:
                self._core.disconnect()
            except Exception:  # noqa: BLE001 - shutting down regardless
                log.exception("Disconnecting Core during shutdown failed")
            self._session_id = None
            self._next_attempt = None
            while True:
                try:
                    if not self._watch.poll(limit=16):
                        break
                except CoreRequestError:
                    break
            with self._lock:
                self._final_status = True
            self._set_status(HostState.STOPPED, "Stopped. The simulator session is closed.")
            log.info("The Core thread stopped.")
        finally:
            self._finished.set()
            # Thread idents are reused once a thread ends; forget ours so a later thread
            # with the same ident is never mistaken for the Core thread (inline calls).
            self._thread_ident = None

    def _fail_queued_calls(self):
        with self._lock:
            calls, self._calls = list(self._calls), deque()
        for _, future in calls:
            if future.set_running_or_notify_cancel():
                future.set_exception(HostStopped("The Core host stopped before running this call."))

    # -- Helpers -------------------------------------------------------------------------------

    def _set_status(self, state: HostState, detail: str, *, retry_delay_s: float | None = None):
        try:
            connection = self._watch.connection()
        except CoreRequestError:
            connection = self._status.connection
        with self._lock:
            if self._final_status and state is not HostState.STOPPED:
                return  # the worker already finished; never overwrite its final STOPPED status
            self._status = HostStatus(
                state, connection, detail, ui_active=self._ui_active, auto_connect=self._auto_connect,
                failures=self._failures,
                retry_at=self._next_attempt if state is HostState.WAITING else None,
                retry_delay_s=retry_delay_s if state is HostState.WAITING else None,
                simulator_unavailable=self._simulator_unavailable and state is HostState.WAITING,
                crashes=self._stats.crashes, last_error=self._last_error,
            )
            status = self._status
        for listener in self._status_listeners.snapshot():
            self._safe(listener, status, what="status listener")

    @staticmethod
    def _safe(fn, *args, what: str) -> bool:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - one listener must never break the host
            log.exception("A %s raised; it stays registered", what)
            return False
        return True


__all__ = [
    "CoreHost", "DEFAULT_BACKOFF_S", "HostConfig", "HostState", "HostStats", "HostStatus", "HostStopped",
    "NOT_RUNNING_TEXT", "backoff_delay", "describe_connect_failure", "is_not_running",
]
