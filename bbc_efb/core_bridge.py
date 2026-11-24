"""Qt side of the Core host: signals on the UI thread, never a pump.

``CoreBridge`` is created on the UI thread from a running (or about to run)
``bbc_core.host.CoreHost``. It never calls ``Core.pump`` or touches the provider; the
host's worker thread does all of that. The bridge only:

* relays host status to the UI thread. Status changes happen on the Core thread, so the
  bridge hops threads with an internal queued signal and then emits
  ``status_changed(HostStatus)`` and, when Core's connection record changed,
  ``connection_changed(Connection)``. Receivers therefore always run on the UI thread,
  including plain Python callables connected without a QObject context;
* drains the ``ConsumerAPI`` handles it registered (``api(name, ...)``) on a ``QTimer``
  in small batches (``batch_size`` per poll, at most ``max_batches_per_tick`` polls per
  consumer and a ``tick_budget_ms`` time budget per tick, round-robin so one busy
  consumer cannot starve the others). Each batch is emitted as
  ``updates(ConsumerBatch)`` and on the consumer's own ``ConsumerChannel.updates``.
  ``ConsumerAPI.poll`` only takes Core's consumer-state lock, never the provider lock,
  so the UI thread does not wait on simulator I/O;
* tracks whether the window is visible (``watch_window`` or ``set_ui_active``) and tells
  the host, so UI modules can drop display-only subscriptions while minimised. While the
  UI is inactive the drain timer slows to ``inactive_interval_ms`` so queues do not
  overflow silently, and Core keeps pumping for the Recorder;
* runs a function on the Core thread and delivers its result back on the UI thread
  (``run_on_core``), so UI code never blocks waiting for the Core thread;
* polls the Flight Logging status source the app gives it (``set_recorder_source``,
  normally the Recorder's thread-safe ``status()``) on the UI thread, about once a second
  (every 5 s while the UI is inactive) and emits ``recorder_status_changed`` when it
  changed. The SimConnect and Flight Logging statuses stay independent: one never
  changes the other;
* builds the Connection Diagnostics facts Settings shows (``diagnostics()``).

The Recorder should not use this bridge: it should drain its own handle from a host pump
listener (or its own thread) so recording continues when the UI is hidden.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from dataclasses import dataclass
from time import perf_counter
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal, Slot

from bbc_core.contract import CoreRequestError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConsumerBatch:
    """One drained batch for one named consumer, in Core's delivery order."""

    name: str
    updates: tuple


class ConsumerChannel(QObject):
    """Per-consumer signal source. ``updates`` carries a tuple of Core updates."""

    updates = Signal(object)

    def __init__(self, name: str, api, parent: QObject | None = None):
        super().__init__(parent)
        self._name = name
        self._api = api

    @property
    def name(self) -> str:
        return self._name

    @property
    def api(self):
        return self._api


class _WindowWatcher(QObject):
    """Event filter that maps a window's visibility and minimised state to UI activity."""

    def __init__(self, bridge: CoreBridge, window):
        super().__init__(bridge)
        self._bridge = bridge
        self._window = window

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        if event.type() in (QEvent.Type.WindowStateChange, QEvent.Type.Show, QEvent.Type.Hide):
            self._bridge._apply_window_state(watched)
        return False


class CoreBridge(QObject):
    """Qt signal bridge for a ``CoreHost``. Create and use it on the UI thread."""

    connection_changed = Signal(object)   # bbc_core.contract.Connection
    status_changed = Signal(object)       # bbc_core.host.HostStatus
    updates = Signal(object)              # ConsumerBatch
    ui_active_changed = Signal(bool)
    recorder_status_changed = Signal(object)  # whatever the recorder source returns

    RECORDER_INTERVAL_MS = 1000
    RECORDER_INACTIVE_INTERVAL_MS = 5000

    _status_posted = Signal(object)
    _ui_posted = Signal(bool)
    _call_done = Signal(object)

    def __init__(self, host, parent: QObject | None = None, *, interval_ms: int = 50,
                 inactive_interval_ms: int = 500, batch_size: int = 32, max_batches_per_tick: int = 4,
                 tick_budget_ms: float = 4.0, autostart: bool = True):
        super().__init__(parent)
        if type(interval_ms) is not int or not 1 <= interval_ms <= 10_000:
            raise ValueError("The drain interval must be 1 to 10,000 ms.")
        if type(inactive_interval_ms) is not int or not interval_ms <= inactive_interval_ms <= 60_000:
            raise ValueError("The inactive drain interval must be at least the active one and at most 60 s.")
        if type(batch_size) is not int or not 1 <= batch_size <= 4096:
            raise ValueError("The batch size must be 1 to 4,096.")
        if type(max_batches_per_tick) is not int or not 1 <= max_batches_per_tick <= 64:
            raise ValueError("The batches per tick must be 1 to 64.")
        if not 0 < tick_budget_ms <= 1000:
            raise ValueError("The tick budget must be above 0 and at most 1,000 ms.")
        self._host = host
        self._interval_ms = interval_ms
        self._inactive_interval_ms = inactive_interval_ms
        self._batch_size = batch_size
        self._max_batches = max_batches_per_tick
        self._budget_s = tick_budget_ms / 1000
        self._channels: dict[str, ConsumerChannel] = {}
        self._order: list[str] = []
        self._cursor = 0
        self._closed = False
        self._last_status = None
        self._last_connection = None
        self._ui_active = bool(getattr(host, "ui_active", True))
        self._watchers: list[tuple[object, _WindowWatcher]] = []

        self._status_posted.connect(self._on_status, Qt.ConnectionType.QueuedConnection)
        self._ui_posted.connect(self._on_ui_active, Qt.ConnectionType.QueuedConnection)
        self._call_done.connect(self._on_call_done, Qt.ConnectionType.QueuedConnection)
        self._removers = [
            host.add_status_listener(self._post_status),
            host.add_ui_listener(self._post_ui),
        ]
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self.drain)
        self._recorder_source: Callable[[], object] | None = None
        self._last_recorder_status = None
        self._recorder_error: str | None = None
        self._recorder_timer = QTimer(self)
        self._recorder_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._recorder_timer.timeout.connect(self.poll_recorder)
        self._apply_interval()
        # Deliver the current state on the first event-loop turn.
        self._post_status(host.status())
        if autostart:
            self._timer.start()

    # -- Public --------------------------------------------------------------------------------

    @property
    def host(self):
        return self._host

    @property
    def ui_active(self) -> bool:
        return self._ui_active

    def status(self):
        """The newest host status (may be newer than the last ``status_changed``)."""
        return self._host.status()

    def connection(self):
        return self._host.status().connection

    def api(self, name: str, control_groups=(), diagnostics: bool = False, *, queue_size: int = 256):
        """Register a UI consumer with the host and drain it on this bridge's timer."""
        if self._closed:
            raise RuntimeError("This Core bridge is closed.")
        api = self._host.api(name, control_groups, diagnostics, queue_size=queue_size)
        self._attach(name, api)
        return api

    def attach(self, name: str, api) -> ConsumerChannel:
        """Drain an existing handle (already registered with the host) on this bridge."""
        if self._closed:
            raise RuntimeError("This Core bridge is closed.")
        return self._attach(name, api)

    def channel(self, name: str) -> ConsumerChannel | None:
        return self._channels.get(name)

    def consumer_names(self) -> tuple[str, ...]:
        return tuple(self._order)

    def release(self, name: str) -> bool:
        """Stop draining ``name`` and close it in the host. Returns False when unknown."""
        channel = self._detach(name)
        released = self._host.release(name)
        return channel is not None or released

    def set_ui_active(self, active: bool) -> None:
        """Tell the host whether the UI is visible. ``ui_active_changed`` follows."""
        self._host.set_ui_active(bool(active))

    def watch_window(self, window) -> None:
        """Follow a QWidget or QWindow: minimised or hidden means the UI is inactive."""
        watcher = _WindowWatcher(self, window)
        window.installEventFilter(watcher)
        self._watchers.append((window, watcher))
        self._apply_window_state(window)

    def run_on_core(self, fn: Callable, on_result: Callable | None = None,
                    on_error: Callable[[BaseException], None] | None = None) -> Future:
        """Run ``fn(core)`` on the Core thread; call back on the UI thread. Never blocks.

        After the host stopped, the returned Future is already failed with the host's
        error (``HostStopped``) and ``on_error`` gets it, instead of the call raising.
        """
        try:
            future = self._host.submit(fn)
        except Exception as error:  # noqa: BLE001 - HostStopped and friends: one error path for the UI
            future = Future()
            future.set_exception(error)
        if on_result is not None or on_error is not None:
            future.add_done_callback(lambda done: self._call_done.emit((done, on_result, on_error)))
        return future

    def start(self) -> None:
        if not self._closed:
            self._timer.start()
            if self._recorder_source is not None:
                self._recorder_timer.start()

    # -- Flight Logging status (independent of the SimConnect status) ---------------------------

    def set_recorder_source(self, source: Callable[[], object] | None) -> None:
        """Poll ``source()`` on the UI thread and emit ``recorder_status_changed`` on change.

        ``source`` must be cheap and thread-safe (``RecorderService.status()`` is). None stops
        polling. The first status is emitted on the next poll.
        """
        self._recorder_source = source
        self._last_recorder_status = None
        if source is None or self._closed:
            self._recorder_timer.stop()
            return
        self._recorder_timer.start()

    @Slot()
    def poll_recorder(self):
        """Read the recorder source once; emit when the status changed. Returns the status."""
        source = self._recorder_source
        if source is None or self._closed:
            return None
        try:
            status = source()
        except Exception as error:  # noqa: BLE001 - another component; report it, keep polling
            text = f"{type(error).__name__}: {error}"
            if text != self._recorder_error:
                log.exception("The Flight Logging status could not be read")
            self._recorder_error = text
            return None
        self._recorder_error = None
        if status != self._last_recorder_status:
            self._last_recorder_status = status
            self.recorder_status_changed.emit(status)
        return status

    @property
    def recorder_status(self):
        """The last Flight Logging status emitted (None before the first poll)."""
        return self._last_recorder_status

    # -- Diagnostics --------------------------------------------------------------------------

    def diagnostics(self) -> dict:
        """Facts for Settings > SimConnect > Connection Diagnostics, from the host status.

        Keys follow ``bbc_efb.settings.coerce_diagnostics``: ``connection``, ``state``,
        ``detail``, ``source``, ``session_id``, ``since``, ``last_errors`` and ``notes``.
        Nothing is measured here and nothing is sent to the simulator.
        """
        try:
            status = self._host.status()
        except Exception as error:  # noqa: BLE001
            return {"state": "unknown", "detail": f"Black Box Core status could not be read: {error}"}
        connection = getattr(status, "connection", None)
        state = getattr(getattr(connection, "state", None), "value", None)
        notes = []
        host_state = getattr(getattr(status, "state", None), "value", getattr(status, "state", None))
        if host_state:
            notes.append(f"Core host: {host_state}. {getattr(status, 'detail', '') or ''}".strip())
        retry = getattr(status, "retry_in", None)
        try:
            seconds = retry() if callable(retry) else None
        except Exception:  # noqa: BLE001
            seconds = None
        if seconds is not None:
            notes.append(f"Next connection attempt in {seconds:.0f} s.")
        failures = getattr(status, "failures", 0) or 0
        if failures:
            notes.append(f"Failed connection attempts in a row: {failures}.")
        if getattr(status, "simulator_unavailable", False):
            notes.append("MSFS is not running or SimConnect is unavailable.")
        stats = getattr(self._host, "stats", None)
        try:
            stats = stats() if callable(stats) else None
        except Exception:  # noqa: BLE001
            stats = None
        if stats is not None:
            notes.append(
                f"Pumps: {getattr(stats, 'pumps', 0)}, slow pumps: {getattr(stats, 'slow_pumps', 0)}, "
                f"longest pump: {getattr(stats, 'max_pump_s', 0.0) * 1000:.1f} ms, "
                f"internal errors: {getattr(stats, 'crashes', 0)}.")
        if not self._ui_active:
            notes.append("The EFB window is minimised or hidden; Core and Flight Logging keep running.")
        pending = getattr(self._host, "pending_provider", None)
        pending = pending if isinstance(pending, str) and pending else None
        if pending:
            notes.append(f"Waiting to switch to {pending} at the next connection attempt.")
        last_error = getattr(status, "last_error", "") or ""
        source = getattr(connection, "source", None)
        return {
            "connection": connection,
            "state": state,
            "detail": getattr(connection, "detail", "") or getattr(status, "detail", ""),
            "source": source,
            "session_id": getattr(connection, "session_id", None),
            "since": getattr(connection, "occurred_at", None),
            "last_errors": (last_error,) if last_error else (),
            "notes": tuple(notes),
            "pending_provider": pending,
        }

    def close(self, *, release_consumers: bool = True) -> None:
        """Stop the timer and unhook from the host. Does not stop the host."""
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._recorder_timer.stop()
        self._recorder_source = None
        for remove in self._removers:
            remove()
        self._removers.clear()
        for window, watcher in self._watchers:
            try:
                window.removeEventFilter(watcher)
            except RuntimeError:  # the window was already deleted
                pass
        self._watchers.clear()
        names = list(self._order)
        for name in names:
            self._detach(name)
            if release_consumers:
                self._host.release(name)

    @Slot()
    def drain(self) -> int:
        """Drain registered consumers once. Returns the number of updates delivered."""
        if self._closed or not self._order:
            return 0
        deadline = perf_counter() + self._budget_s
        delivered = 0
        count = len(self._order)
        start = self._cursor % count
        for offset in range(count):
            if perf_counter() >= deadline:
                break
            name = self._order[(start + offset) % count]
            channel = self._channels.get(name)
            if channel is None:
                continue
            for _ in range(self._max_batches):
                try:
                    batch = channel.api.poll(limit=self._batch_size)
                except CoreRequestError:
                    log.info("UI consumer %r was closed elsewhere; the bridge stops draining it.", name)
                    self._detach(name)
                    break
                if not batch:
                    break
                delivered += len(batch)
                channel.updates.emit(batch)
                self.updates.emit(ConsumerBatch(name, batch))
                if self._closed or len(batch) < self._batch_size or perf_counter() >= deadline:
                    break
            if self._closed:
                break
        if self._order:
            self._cursor = (start + 1) % len(self._order)
        return delivered

    # -- Internals -----------------------------------------------------------------------------

    def _attach(self, name, api):
        if name in self._channels:
            raise ValueError(f"The bridge already drains a consumer named {name!r}.")
        channel = ConsumerChannel(name, api, self)
        self._channels[name] = channel
        self._order.append(name)
        return channel

    def _detach(self, name):
        channel = self._channels.pop(name, None)
        if channel is not None:
            self._order.remove(name)
            channel.deleteLater()
        return channel

    # Any thread: hop to the UI thread.
    def _post_status(self, status):
        if not self._closed:
            self._status_posted.emit(status)

    def _post_ui(self, active):
        if not self._closed:
            self._ui_posted.emit(bool(active))

    # UI thread.
    @Slot(object)
    def _on_status(self, status):
        if self._closed:
            return
        # Queued deliveries can arrive out of order relative to newer reads: show the newest.
        latest = self._host.status()
        if latest is not None and latest.updated_at >= status.updated_at:
            status = latest
        if status is self._last_status:
            return
        self._last_status = status
        self.status_changed.emit(status)
        connection = status.connection
        if connection != self._last_connection:
            self._last_connection = connection
            self.connection_changed.emit(connection)
        if status.ui_active != self._ui_active:
            self._on_ui_active(status.ui_active)

    @Slot(bool)
    def _on_ui_active(self, active):
        if self._closed or active == self._ui_active:
            return
        self._ui_active = active
        self._apply_interval()
        self.ui_active_changed.emit(active)

    @Slot(object)
    def _on_call_done(self, payload):
        future, on_result, on_error = payload
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            if on_error is not None:
                on_error(error)
            else:
                log.warning("A call run on the Core thread failed: %r", error)
            return
        if on_result is not None:
            on_result(future.result())

    def _apply_interval(self):
        self._timer.setInterval(self._interval_ms if self._ui_active else self._inactive_interval_ms)
        self._recorder_timer.setInterval(self.RECORDER_INTERVAL_MS if self._ui_active
                                         else self.RECORDER_INACTIVE_INTERVAL_MS)

    def _apply_window_state(self, window):
        try:
            minimised = bool(window.windowState() & Qt.WindowState.WindowMinimized)
            visible = window.isVisible()
        except RuntimeError:  # the window was deleted
            return
        self.set_ui_active(visible and not minimised)


__all__ = ["ConsumerBatch", "ConsumerChannel", "CoreBridge"]
