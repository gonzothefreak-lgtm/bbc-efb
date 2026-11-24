"""The permanent EFB footer: SimConnect and Flight Logging, side by side, never merged.

The two statuses have different owners (UI Design Standards §4):

* **SimConnect** shows Black Box Core's connection state, from the ``Connection``
  objects the Core bridge publishes. Clicking it opens Settings > SimConnect >
  Connection Diagnostics.
* **Flight Logging** shows the Recorder's flight-log lifecycle, from a
  ``FlightLogging`` object. Clicking it opens compact current-flight details with
  Start and Stop as appropriate. Stop asks for confirmation (the shell owns that
  dialog).

Neither status is ever inferred from the other. A lost SimConnect connection does
not turn Flight Logging off in the footer; only the Recorder says what Flight
Logging is doing.

``FlightLogging`` is a small Protocol so the shell does not depend on the
Recorder's internal API. ``RecorderServiceAdapter`` adapts a Recorder service and
``UnavailableFlightLogging`` is used when the build has none.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import inspect
import logging
from typing import Any, Mapping, Protocol, runtime_checkable

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QWidget, QWidgetAction,
)

log = logging.getLogger(__name__)

SAVE_TIMEOUT_S = 10.0
"""How long Save & Exit waits for the Recorder before reporting a failure."""


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value

    __format__ = str.__format__


# ----------------------------------------------------------------------------
# Flight Logging status model
# ----------------------------------------------------------------------------


class LoggingState(_ValueEnum):
    """Pilot-facing Flight Logging states (candidate wording from UI Standards §4)."""

    UNAVAILABLE = "unavailable"   # no Recorder in this build, or it cannot run (no data folder)
    INACTIVE = "inactive"         # Recorder present but cannot log now (no simulator connection)
    READY = "ready"               # Recorder running, no flight open
    ACTIVE = "active"             # a flight is being logged
    PAUSED = "paused"             # flight open, simulator paused
    INTERRUPTED = "interrupted"   # flight open, but data stopped arriving (gap)
    LOGGED = "logged"             # last flight closed and saved
    SAVE_FAILED = "save_failed"   # the Recorder could not save
    ERROR = "error"               # something prevents truthful logging


FLIGHT_OPEN_STATES = frozenset({LoggingState.ACTIVE, LoggingState.PAUSED, LoggingState.INTERRUPTED})

_STATE_TEXT = {
    LoggingState.UNAVAILABLE: "Not available",
    LoggingState.INACTIVE: "Inactive",
    LoggingState.READY: "Ready",
    LoggingState.ACTIVE: "Logging Active",
    LoggingState.PAUSED: "Logging Active (simulator paused)",
    LoggingState.INTERRUPTED: "Logging Interrupted",
    LoggingState.LOGGED: "Flight Logged",
    LoggingState.SAVE_FAILED: "Save failed",
    LoggingState.ERROR: "Error",
}

TONE_OK = "ok"
TONE_BUSY = "busy"
TONE_OFF = "off"
TONE_ERROR = "error"
TONE_COLOURS = {TONE_OK: "#3fb950", TONE_BUSY: "#e3b341", TONE_OFF: "#7d8fa3", TONE_ERROR: "#f85149"}

_LOGGING_TONE = {
    LoggingState.UNAVAILABLE: TONE_OFF,
    LoggingState.INACTIVE: TONE_OFF,
    LoggingState.READY: TONE_OFF,
    LoggingState.ACTIVE: TONE_OK,
    LoggingState.PAUSED: TONE_BUSY,
    LoggingState.INTERRUPTED: TONE_ERROR,
    LoggingState.LOGGED: TONE_OK,
    LoggingState.SAVE_FAILED: TONE_ERROR,
    LoggingState.ERROR: TONE_ERROR,
}


@dataclass(frozen=True, slots=True)
class LoggingStatus:
    """A snapshot of Flight Logging for the footer. Unknown details are left out, not guessed.

    ``flight`` holds (label, value) pairs for the current or last flight, already
    worded for the pilot, for example ``("Aircraft", "Unknown")``. ``text`` replaces
    the default state wording when the Recorder supplies its own. ``open_flight``
    overrides the state-based answer to "is a flight open?" when the Recorder knows.
    """

    state: LoggingState
    detail: str = ""
    flight: tuple[tuple[str, str], ...] = ()
    can_start: bool = False
    can_stop: bool = False
    simulated: bool = False
    text: str = ""
    open_flight: bool | None = None

    @property
    def flight_open(self) -> bool:
        """True while a flight is open, so closing the EFB must ask Cancel or Save & Exit."""
        if self.open_flight is not None:
            return self.open_flight
        return self.state in FLIGHT_OPEN_STATES

    @property
    def label(self) -> str:
        text = self.text or _STATE_TEXT[self.state]
        return f"{text} (SIMULATED)" if self.simulated else text

    @property
    def tone(self) -> str:
        return _LOGGING_TONE[self.state]


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Outcome of a Start, Stop or Save request, in plain English."""

    ok: bool
    message: str = ""


@runtime_checkable
class FlightLogging(Protocol):
    """What the shell needs from the Recorder. Implementations must be cheap to poll."""

    def status(self) -> LoggingStatus: ...

    def start(self) -> ActionResult | Future: ...

    def stop(self) -> ActionResult | Future: ...

    def save_and_exit(self, reason: str) -> ActionResult | Future: ...


class UnavailableFlightLogging:
    """Flight Logging when no Recorder can run. Says why; offers no controls."""

    DEFAULT_REASON = "Automatic Flight Logging is not available in this build."

    def __init__(self, reason: str = DEFAULT_REASON):
        self.reason = reason

    def status(self) -> LoggingStatus:
        return LoggingStatus(LoggingState.UNAVAILABLE, self.reason)

    def start(self) -> ActionResult:
        return ActionResult(False, self.reason)

    def stop(self) -> ActionResult:
        return ActionResult(False, "No flight is being logged.")

    def save_and_exit(self, reason: str) -> ActionResult:
        return ActionResult(True, "No flight was being logged.")

    def shutdown(self) -> None:
        return None


def resolve_action(result: Any, timeout: float = SAVE_TIMEOUT_S) -> ActionResult:
    """Turn whatever a Recorder returned into an ``ActionResult``.

    Accepts an ``ActionResult``, a ``concurrent.futures.Future`` of one (waited for up
    to ``timeout`` seconds), ``None`` or ``True`` (success), ``False`` (failure), or an
    object with ``ok``/``success`` and ``message``/``error`` attributes. A timeout or
    an exception is reported as a failure with the reason.
    """
    if isinstance(result, Future):
        try:
            result = result.result(timeout=timeout)
        except FutureTimeout:
            return ActionResult(False, f"The Flight Recorder did not finish within {timeout:g} seconds.")
        except Exception as error:  # the Recorder's own failure, reported verbatim
            return ActionResult(False, _error_text(error))
    if isinstance(result, ActionResult):
        return result
    if result is None or result is True:
        return ActionResult(True)
    if result is False:
        return ActionResult(False, "The Flight Recorder reported a failure without a reason.")
    ok = getattr(result, "ok", getattr(result, "success", None))
    message = (getattr(result, "message", None) or getattr(result, "detail", None)
               or getattr(result, "error", None) or "")
    if isinstance(ok, bool):
        return ActionResult(ok, str(message))
    return ActionResult(True, str(message))


def _error_text(error: BaseException) -> str:
    text = str(error).strip()
    return text if text else type(error).__name__


# ----------------------------------------------------------------------------
# Recorder adapter
# ----------------------------------------------------------------------------

_PHASE_STATES = {
    "idle": LoggingState.READY,
    "preflight": LoggingState.ACTIVE,
    "taxi_out": LoggingState.ACTIVE,
    "takeoff_roll": LoggingState.ACTIVE,
    "airborne": LoggingState.ACTIVE,
    "landing_roll": LoggingState.ACTIVE,
    "taxi_in": LoggingState.ACTIVE,
    "complete": LoggingState.LOGGED,
    "abandoned": LoggingState.LOGGED,
}

_PHASE_TEXT = {
    "idle": "No flight", "preflight": "Pre-flight", "taxi_out": "Taxi out", "takeoff_roll": "Take-off roll",
    "airborne": "Airborne", "landing_roll": "Landing roll", "taxi_in": "Taxi in", "complete": "Complete",
    "abandoned": "Ended without a normal arrival",
}

_FLIGHT_FIELDS = (
    ("aircraft", "Aircraft"), ("aircraft_type", "Aircraft"), ("callsign", "Callsign"),
    ("departure", "From"), ("arrival", "To"), ("started_at", "Started"), ("block_off", "Block off"),
    ("block_time", "Block time"), ("airborne_time", "Airborne time"),
)


def _value(item: Any) -> str:
    return str(getattr(item, "value", item))


def _first_callable(target: Any, names: Iterable[str]) -> Callable[..., Any] | None:
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            return method
    return None


def _attribute(target: Any, names: Iterable[str]) -> Any:
    for name in names:
        value = getattr(target, name, None)
        if callable(value):
            try:
                value = value()
            except TypeError:
                continue
        if value is not None:
            return value
    return None


def _describe(value: Any) -> str:
    if value is None or value == "":
        return "Unknown"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        seconds = int(value)
        return f"{seconds // 3600}h {seconds % 3600 // 60:02}m"
    return str(value)


_RECORDER_STATES = {
    "unavailable": LoggingState.UNAVAILABLE,
    "saving": LoggingState.ACTIVE,
    "inactive": LoggingState.INACTIVE,
    "armed": LoggingState.READY,
    "recording": LoggingState.ACTIVE,
    "paused": LoggingState.PAUSED,
    "gap": LoggingState.INTERRUPTED,
    "error": LoggingState.ERROR,
}

_OPEN_RECORDER_STATES = frozenset({"recording", "paused", "gap", "saving"})

_START_NAMES = ("manual_start", "start_logging", "start")
_STOP_NAMES = ("manual_stop", "stop_logging", "stop")
_SAVE_NAMES = ("save_and_close", "save_and_exit", "finalise", "finalize", "shutdown")


def _accepts(method: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


def _utc_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%H:%M:%SZ")
    return "Unknown"


def _recorder_flight_rows(flight: Any) -> list[tuple[str, str]]:
    """Rows for ``bbc_recorder.service.CurrentFlight``. Unknown stays Unknown."""
    def text(value: Any) -> str:
        return "Unknown" if value is None or value == "" else str(value)

    rows = [("Phase", text(getattr(flight, "phase_label", None) or _value(getattr(flight, "phase", "")) or None))]
    aircraft = getattr(flight, "aircraft_title", None) or getattr(flight, "atc_model", None)
    rows.append(("Aircraft", text(aircraft)))
    rows.append(("ATC ID", text(getattr(flight, "atc_id", None))))
    started = getattr(flight, "started_at", None)
    how = "manually" if getattr(flight, "started_manually", False) else "automatically"
    rows.append(("Started", f"{_utc_text(started)} (PC clock), {how}"))
    block_off = getattr(flight, "block_off_at", None)
    rows.append(("Block off", _utc_text(block_off) if block_off else "Not yet"))
    elapsed = getattr(flight, "sim_elapsed_s", None)
    rows.append(("Simulator time logged", _describe(elapsed) if elapsed is not None else "Unknown"))
    for name, title in (("touchdowns", "Touchdowns"), ("gaps", "Gaps in data")):
        value = getattr(flight, name, None)
        if value is not None:
            rows.append((title, str(value)))
    journal_ok = getattr(flight, "journal_ok", None)
    if journal_ok is not None:
        rows.append(("Crash-safe journal", "Writing" if journal_ok else
                     "NOT writing: this flight may not survive a crash"))
    return rows


def _c3_flight_rows(reported: Any) -> list[tuple[str, str]]:
    """Rows for a C3 ``RecorderStatus`` (flat: phase, aircraft, departure, times)."""
    def text(value: Any) -> str:
        return "Unknown" if value is None or value == "" else str(getattr(value, "value", value))

    rows = [("Phase", text(getattr(reported, "phase_label", None) or getattr(reported, "phase", None)))]
    rows.append(("Aircraft", text(getattr(reported, "aircraft", None))))
    rows.append(("From", text(getattr(reported, "departure", None))))
    started = getattr(reported, "started_utc", None)
    rows.append(("Started", f"{_utc_text(started)} (UTC)" if isinstance(started, datetime) else "Unknown"))
    for name, title in (("block_time_s", "Block time"), ("airborne_time_s", "Airborne time")):
        value = getattr(reported, name, None)
        rows.append((title, _describe(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                     else "Unknown"))
    return rows


class RecorderServiceAdapter:
    """Adapt a Recorder service object to ``FlightLogging``.

    Designed for ``bbc_recorder.service.RecorderService`` and tolerant of variations:

    * ``logging_status()`` returning a ``LoggingStatus`` is used as is.
    * ``status()`` returning a Recorder status object (``state``, ``label``,
      ``detail``, ``flight``, ``last_flight``, ``can_start``, ``can_stop``,
      ``pending_saves``) is mapped, keeping the Recorder's own wording.
    * Otherwise the state is read from ``phase`` (a ``bbc_recorder.model.Phase`` or
      its value), ``paused``, ``interrupted``/``gap_open``, ``save_error`` and
      ``current_flight``.
    * Start: ``manual_start``/``start_logging``/``start``. Stop:
      ``manual_stop``/``stop_logging``/``stop``, passing ``confirmed=True`` when the
      method takes it (the shell has already asked the pilot). Save & Exit:
      ``save_and_exit``/``finalise``/``finalize``/``shutdown`` with the reason.
    * ``shutdown_service()`` runs ``before_shutdown`` callables (for example removing
      the host pump listener) and then ``close``/``shutdown``.

    Every call is guarded: a failure becomes an honest ``ActionResult`` or an
    ``UNAVAILABLE`` status instead of an exception in the UI. ``simulated`` labels
    every status SIMULATED.
    """

    def __init__(self, service: Any, *, simulated: bool = False,
                 before_shutdown: Iterable[Callable[[], Any]] = (),
                 run_on_core: Callable[[Callable[..., Any]], Future] | None = None):
        self.service = service
        self.simulated = simulated
        self.run_on_core = run_on_core
        self.after_shutdown: list[Callable[[], Any]] = []
        self.before_shutdown = list(before_shutdown)
        self._shut_down = False

    def status(self) -> LoggingStatus:
        service = self.service
        try:
            direct = _first_callable(service, ("logging_status",))
            if direct is not None:
                value = direct()
                if isinstance(value, LoggingStatus):
                    return value
            reported = getattr(service, "status", None)
            if callable(reported):
                value = reported()
                if isinstance(value, LoggingStatus):
                    return value
                if hasattr(value, "state") and hasattr(value, "flight_id"):
                    return self._from_c3_status(value)
                if hasattr(value, "state") and hasattr(value, "can_stop"):
                    return self._from_recorder_status(value)
            return self._status_from_attributes()
        except Exception as error:
            log.exception("Flight Recorder status could not be read")
            return LoggingStatus(LoggingState.UNAVAILABLE, f"Flight Recorder status could not be read: "
                                                           f"{_error_text(error)}", simulated=self.simulated)

    def _from_recorder_status(self, reported: Any) -> LoggingStatus:
        key = _value(getattr(reported, "state", "")).lower()
        state = _RECORDER_STATES.get(key, LoggingState.ERROR)
        flight = getattr(reported, "flight", None)
        open_flight = flight is not None
        detail = str(getattr(reported, "detail", "") or getattr(reported, "reason", "") or "")
        text = str(getattr(reported, "label", "") or "")
        rows: list[tuple[str, str]] = []
        if open_flight:
            rows = _recorder_flight_rows(flight)
        last = getattr(reported, "last_flight", None)
        pending = getattr(reported, "pending_saves", 0) or 0
        if last is not None:
            saved = bool(getattr(last, "saved", False))
            outcome = str(getattr(last, "outcome", "") or "ended")
            rows.append(("Last flight", f"{outcome}, {'saved to the Logbook' if saved else 'NOT saved'}"))
            if not open_flight and saved and state is LoggingState.READY:
                state, text = LoggingState.LOGGED, ""
        if not open_flight and (pending or (last is not None and not getattr(last, "saved", True))):
            state, text = LoggingState.SAVE_FAILED, ""
            message = str(getattr(last, "message", "") or "") if last is not None else ""
            detail = message or detail or f"{pending} finished flight(s) are waiting to be saved."
        return LoggingStatus(state, detail, tuple(rows), can_start=bool(getattr(reported, "can_start", False)),
                             can_stop=bool(getattr(reported, "can_stop", False)), simulated=self.simulated,
                             text=text, open_flight=open_flight)

    def _from_c3_status(self, reported: Any) -> LoggingStatus:
        """Map the C3 ``RecorderStatus`` (state, phase, detail, flight_id, ... simulated)."""
        key = _value(getattr(reported, "state", "")).lower()
        state = _RECORDER_STATES.get(key, LoggingState.ERROR)
        flight_open = getattr(reported, "flight_open", None)
        if not isinstance(flight_open, bool):
            flight_open = key in _OPEN_RECORDER_STATES or (
                getattr(reported, "flight_id", None) is not None and key not in ("inactive", "armed", "unavailable"))
        if key == "saving":
            flight_open = True  # closing the EFB now must still go through Save & Exit
        rows = _c3_flight_rows(reported) if flight_open else []
        text = str(getattr(reported, "label", "") or "")
        if key == "saving" and not text:
            text = "Saving the flight…"
        detail = str(getattr(reported, "detail", "") or "")
        last = getattr(reported, "last_flight", None)
        if last is not None:
            saved = bool(getattr(last, "saved", False))
            outcome = str(getattr(last, "outcome", "") or "ended")
            rows.append(("Last flight", f"{outcome}, {'saved to the Logbook' if saved else 'NOT saved'}"))
            if not flight_open and saved and state is LoggingState.READY:
                state, text = LoggingState.LOGGED, ""
        pending = getattr(reported, "pending_saves", 0) or 0
        if not flight_open and key == "error" and (pending or (last is not None and not getattr(last, "saved", True))):
            state, text = LoggingState.SAVE_FAILED, ""
        simulated = self.simulated or bool(getattr(reported, "simulated", False))
        can_start = getattr(reported, "can_start", None)
        can_stop = getattr(reported, "can_stop", None)
        return LoggingStatus(
            state, detail, tuple(rows),
            can_start=bool(can_start) if can_start is not None else (key == "armed" and not flight_open),
            can_stop=bool(can_stop) if can_stop is not None else (flight_open and key != "saving"),
            simulated=simulated, text=text, open_flight=flight_open)

    def _status_from_attributes(self) -> LoggingStatus:
        service = self.service
        simulated = self.simulated or bool(_attribute(service, ("simulated",)) or False)
        save_error = _attribute(service, ("save_error", "last_save_error"))
        phase = _attribute(service, ("phase",))
        phase_key = _value(phase).lower() if phase is not None else ""
        state = _PHASE_STATES.get(phase_key)
        if state is None:
            if phase is None:
                return LoggingStatus(LoggingState.UNAVAILABLE, "The Flight Recorder did not report a state.",
                                     simulated=simulated)
            state = LoggingState.ACTIVE if _attribute(service, ("flight_open",)) else LoggingState.READY
        if state is LoggingState.ACTIVE:
            if _attribute(service, ("interrupted", "gap_open")):
                state = LoggingState.INTERRUPTED
            elif _attribute(service, ("paused",)):
                state = LoggingState.PAUSED
        detail = str(_attribute(service, ("status_detail", "detail")) or "")
        if save_error:
            state = LoggingState.SAVE_FAILED if state not in FLIGHT_OPEN_STATES else state
            detail = f"The last save failed: {save_error}"
        rows: list[tuple[str, str]] = []
        if phase_key in _PHASE_TEXT and phase_key != "idle":
            rows.append(("Phase", _PHASE_TEXT[phase_key]))
        flight = _attribute(service, ("current_flight", "flight_summary"))
        if flight is not None:
            seen: set[str] = set()
            for key, title in _FLIGHT_FIELDS:
                if title in seen:
                    continue
                present = key in flight if isinstance(flight, Mapping) else hasattr(flight, key)
                if not present:
                    continue
                value = flight.get(key) if isinstance(flight, Mapping) else getattr(flight, key, None)
                if value is None and title == "Aircraft":
                    continue  # another key may name the aircraft
                rows.append((title, _describe(value)))
                seen.add(title)
        open_flight = state in FLIGHT_OPEN_STATES
        can_start = not open_flight and _first_callable(service, _START_NAMES) is not None
        can_stop = open_flight and _first_callable(service, _STOP_NAMES) is not None
        return LoggingStatus(state, detail, tuple(rows), can_start=can_start, can_stop=can_stop,
                             simulated=simulated)

    def _call(self, names: tuple[str, ...], *args: Any, missing: str, **kwargs: Any) -> ActionResult | Future:
        method = _first_callable(self.service, names)
        if method is None:
            return ActionResult(False, missing)
        call_kwargs = {name: value for name, value in kwargs.items() if _accepts(method, name)}
        try:
            result = method(*args, **call_kwargs)
        except Exception as error:
            log.exception("Flight Recorder call %s failed", getattr(method, "__name__", method))
            return ActionResult(False, _error_text(error))
        return result if isinstance(result, Future) else resolve_action(result)

    def start(self) -> ActionResult | Future:
        return self._call(_START_NAMES, missing="This Flight Recorder cannot be started manually.")

    def stop(self) -> ActionResult | Future:
        """Manual Stop. Only call after the pilot confirmed (the shell asks)."""
        return self._call(_STOP_NAMES, missing="This Flight Recorder cannot be stopped manually.", confirmed=True)

    def save_and_exit(self, reason: str, timeout_s: float = SAVE_TIMEOUT_S) -> ActionResult | Future:
        """Save & Exit. ``save_and_close(timeout_s)`` (C3) is preferred; it reports ok/detail.

        Older services (``shutdown(reason)``) are run on the Core thread through
        ``run_on_core`` when one is given, so the save never races a tick, and the shell
        waits for the Future up to ``timeout_s``.
        """
        method = _first_callable(self.service, _SAVE_NAMES)
        if method is None:
            return ActionResult(False, "This Flight Recorder has no way to save the open flight.")
        name = getattr(method, "__name__", "")
        if name == "save_and_close":
            kwargs = {"timeout_s": timeout_s} if _accepts(method, "timeout_s") else {}
            try:
                return resolve_action(method(**kwargs), timeout_s)
            except Exception as error:
                log.exception("Flight Recorder save_and_close failed")
                return ActionResult(False, _error_text(error))
        if self.run_on_core is not None:
            try:
                return self.run_on_core(lambda *_: method(reason))
            except Exception as error:  # the host is stopped: save on this thread instead
                log.info("Saving on the UI thread because the Core thread is not available: %s", error)
        return self._call((name,), reason, missing="This Flight Recorder has no way to save the open flight.")

    def set_automatic(self, enabled: bool) -> None:
        method = getattr(self.service, "set_automatic", None)
        if callable(method):
            method(bool(enabled))

    def shutdown(self) -> None:
        """Stop feeding the Recorder and let it close. Safe to call more than once."""
        if self._shut_down:
            return
        self._shut_down = True
        for step in self.before_shutdown:
            try:
                step()
            except Exception:
                log.exception("Recorder shutdown step failed")
        method = _first_callable(self.service, ("close", "shutdown"))
        try:
            if method is not None:
                method()
        finally:
            for step in self.after_shutdown:
                try:
                    step()
                except Exception:
                    log.exception("Recorder shutdown step failed")


# ----------------------------------------------------------------------------
# SimConnect status model
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionView:
    """How the footer words a Core ``Connection``."""

    state: str
    text: str
    detail: str
    source_text: str
    simulated: bool
    tone: str

    @property
    def footer_text(self) -> str:
        return f"SimConnect: {self.text}"


_CONNECTION_TEXT = {
    "connected": ("Connected", TONE_OK),
    "connecting": ("Connecting…", TONE_BUSY),
    "disconnected": ("Not connected", TONE_OFF),
    "error": ("Error", TONE_ERROR),
}


def describe_connection(connection: Any) -> ConnectionView:
    """Word a ``bbc_core.contract.Connection`` (or None) for the pilot.

    Simulated sources are always labelled SIMULATED, so simulated data can never
    look like MSFS data.
    """
    if connection is None:
        return ConnectionView("unknown", "Not connected", "Black Box Core has not reported a connection yet.",
                              "None", False, TONE_OFF)
    state = _value(getattr(connection, "state", "unknown")).lower()
    text, tone = _CONNECTION_TEXT.get(state, (state.replace("_", " ").capitalize() or "Unknown", TONE_OFF))
    source = getattr(connection, "source", None)
    simulated = bool(getattr(source, "simulated", False))
    source_name = str(getattr(source, "name", "") or "Unknown source")
    if state == "connected" and not simulated and "msfs" in source_name.lower():
        text = "Connected (MSFS 2020)"
    if simulated:
        text = f"{text} - SIMULATED"
        source_text = f"{source_name} (SIMULATED: not data from MSFS)"
    else:
        source_text = source_name
    detail = str(getattr(connection, "detail", "") or "")
    return ConnectionView(state, text, detail, source_text, simulated, tone)


# ----------------------------------------------------------------------------
# Widgets
# ----------------------------------------------------------------------------


class StatusIndicator(QWidget):
    """A coloured dot plus a focusable text button. The text carries the state;
    the dot only reinforces it, so colour is never the only cue."""

    clicked = Signal()

    def __init__(self, name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.name = name
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)
        self.dot = QLabel("●")
        self.dot.setAccessibleName("")
        self.dot.setTextFormat(Qt.TextFormat.PlainText)
        self.button = QPushButton()
        self.button.setObjectName("status")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self.clicked)
        self.button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.dot)
        layout.addWidget(self.button)
        self.tone = ""
        self.set_state(f"{name}: Unknown", TONE_OFF, "")

    @property
    def text(self) -> str:
        return self.button.text()

    def set_state(self, text: str, tone: str, tooltip: str, action_hint: str = "") -> None:
        self.tone = tone
        self.button.setText(text)
        self.dot.setStyleSheet(f"color: {TONE_COLOURS.get(tone, TONE_COLOURS[TONE_OFF])};")
        description = tooltip or text
        self.button.setToolTip(description + (f"\n{action_hint}" if action_hint else ""))
        self.button.setAccessibleName(text)
        self.button.setAccessibleDescription(description + (f" {action_hint}" if action_hint else ""))


class Footer(QFrame):
    """The permanent footer. It is fed by the shell and owns no data of its own.

    Signals: ``diagnostics_requested`` (SimConnect clicked), ``start_requested`` and
    ``stop_requested`` (Flight Logging menu), ``privacy_requested``.
    """

    diagnostics_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    privacy_requested = Signal()

    CLOCK_INTERVAL_MS = 1000

    def __init__(self, parent: QWidget | None = None, *, clock: Callable[[], datetime] | None = None):
        super().__init__(parent)
        self.setObjectName("footer")
        self.setAccessibleName("Status bar")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(6)
        self.simconnect = StatusIndicator("SimConnect")
        self.simconnect.clicked.connect(self.diagnostics_requested)
        layout.addWidget(self.simconnect)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setStyleSheet("color: #2a4055;")
        layout.addWidget(separator)
        self.flight_logging = StatusIndicator("Flight Logging")
        self.flight_logging.clicked.connect(self.open_logging_menu)
        layout.addWidget(self.flight_logging)
        layout.addStretch(1)
        self.mode_badge = QLabel("SIMULATED")
        self.mode_badge.setObjectName("warning")
        self.mode_badge.setAccessibleName("Simulated data mode: no data comes from MSFS")
        self.mode_badge.hide()
        layout.addWidget(self.mode_badge)
        self.clock_label = QLabel()
        self.clock_label.setObjectName("muted")
        self.clock_label.setAccessibleName("PC clock, UTC")
        self.clock_label.setToolTip("Time from this PC's clock, in UTC. Not the simulator time.")
        layout.addWidget(self.clock_label)
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(self.CLOCK_INTERVAL_MS)
        self.clock_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.clock_timer.timeout.connect(self.update_clock)
        self.connection_view = describe_connection(None)
        self.logging_status = LoggingStatus(LoggingState.UNAVAILABLE, UnavailableFlightLogging.DEFAULT_REASON)
        self.menu: QMenu | None = None
        self.set_connection(None)
        self.set_logging_status(self.logging_status)
        self.update_clock()

    # Activity (the shell stops timers while minimised)

    def set_active(self, active: bool) -> None:
        if active:
            self.update_clock()
            self.clock_timer.start()
        else:
            self.clock_timer.stop()

    def update_clock(self) -> None:
        self.clock_label.setText(self._clock().astimezone(timezone.utc).strftime("%H:%MZ"))

    def set_simulated_mode(self, simulated: bool) -> None:
        self.mode_badge.setVisible(simulated)

    # SimConnect

    def set_connection(self, connection: Any) -> None:
        view = describe_connection(connection)
        self.connection_view = view
        tooltip = view.detail or view.text
        if view.source_text and view.source_text != "None":
            tooltip = f"{tooltip}\nSource: {view.source_text}"
        self.simconnect.set_state(view.footer_text, view.tone, tooltip,
                                  "Opens Settings > SimConnect > Connection Diagnostics.")

    # Flight Logging

    def set_logging_status(self, status: LoggingStatus) -> None:
        self.logging_status = status
        tooltip = status.detail or status.label
        self.flight_logging.set_state(f"Flight Logging: {status.label}", status.tone, tooltip,
                                      "Shows current flight details and controls.")

    def build_logging_menu(self) -> QMenu:
        """The compact Flight Logging popup: details first, then the controls."""
        status = self.logging_status
        menu = QMenu(self)
        menu.setAccessibleName("Flight Logging details and controls")
        lines = [f"Flight Logging: {status.label}"]
        if status.detail:
            lines.append(status.detail)
        if status.flight:
            lines.extend(f"{name}: {value}" for name, value in status.flight)
        elif status.flight_open:
            lines.append("No flight details reported yet.")
        info = QLabel("\n".join(lines))
        info.setTextFormat(Qt.TextFormat.PlainText)
        info.setWordWrap(True)
        info.setMinimumWidth(260)
        info.setContentsMargins(12, 8, 12, 8)
        info.setAccessibleName("\n".join(lines))
        info_action = QWidgetAction(menu)
        info_action.setDefaultWidget(info)
        info_action.setEnabled(False)
        menu.addAction(info_action)
        menu.addSeparator()
        start = menu.addAction("Start Flight Logging")
        start.setEnabled(status.can_start)
        start.triggered.connect(self.start_requested)
        stop = menu.addAction("Stop Flight Logging…")
        stop.setEnabled(status.can_stop)
        stop.triggered.connect(self.stop_requested)
        menu.addSeparator()
        privacy = menu.addAction("Flight Logging && Privacy settings")
        privacy.triggered.connect(self.privacy_requested)
        self.menu_actions = {"start": start, "stop": stop, "privacy": privacy}
        return menu

    def open_logging_menu(self) -> None:
        if self.menu is not None:
            self.menu.deleteLater()
        self.menu = self.build_logging_menu()
        button = self.flight_logging.button
        position = button.mapToGlobal(button.rect().topLeft())
        position.setY(position.y() - self.menu.sizeHint().height())
        self.menu.popup(position)
