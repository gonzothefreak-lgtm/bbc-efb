"""Explicitly simulated provider. It creates no worker or simulator connection.

Every result it produces is labelled simulated. It behaves like the live SimConnect adapter
where that matters for module and Recorder development:

- it accepts any plain SDK SimVar, the catalog system states and the catalog system events,
  plus grouped snapshots, and serves scripted values for them;
- it re-sends demanded values on their demand cadence (SimConnect ``Period.SECOND`` sends
  unchanged values too), stamped at the time each sample was due, with optional jitter;
- it uses the live adapter's 1,000 ms minimum interval and whole-second interval
  granularity unless told otherwise, so Core reports the same cadence (1500 ms is 2000 ms);
- it applies the same COM channel and spacing rule as Core and the live adapter, and
  rejects an 8.33 kHz channel on a 25 kHz radio before sending;
- it validates commands like the live adapter and rejects what the adapter would reject;
- it models SIMULATION TIME, ABSOLUTE TIME, ZULU TIME, SIMULATION RATE and pause flags;
- a :class:`FlightTape` replays a timed script of values, events, pauses, rate changes,
  provider gaps and a disconnect through the same path.

Native timing, units and aircraft behaviour are not modelled beyond that. Simulated evidence
never replaces live validation.
"""

from __future__ import annotations

import heapq
import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import count
from math import isfinite
from random import Random
from time import monotonic
from typing import Callable, Iterable

from .catalog import (
    COM1_ACTIVE, COM1_SPACING, COM1_STANDBY, ENGINE1_THROTTLE, OPERATIONS, PAUSE_STATE, READS, SYSTEM_EVENTS,
    SYSTEM_STATES, CommandTransport, com_channel_problem,
)
from .contract import PauseState, SnapshotPeriod, Source, StrEnum, ValueType, simulator_utc
from .provider import (
    CommandOutcome, CommandRejected, Disconnect, IngressGap, Outcome, ProviderError, Reading, Signal,
    SnapshotFrame,
)
from .sdk_simvar import CLIENT_EVENT_SEND, SIMVAR_WRITE, SimVar

log = logging.getLogger(__name__)

_PLAIN_TYPES = (ValueType.BOOLEAN, ValueType.INTEGER, ValueType.NUMBER, ValueType.TEXT)
_SIM_TIME = Signal("SIMULATION TIME", "seconds", ValueType.NUMBER)
_ABSOLUTE_TIME = Signal("ABSOLUTE TIME", "seconds", ValueType.NUMBER)
_ZULU_TIME = Signal("ZULU TIME", "seconds", ValueType.NUMBER)
_SIM_RATE = Signal("SIMULATION RATE", "number", ValueType.NUMBER)
_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
_FROZEN = PauseState.FULL | PauseState.FULL_WITH_SOUND | PauseState.ACTIVE | PauseState.SIM
_MAX_UPDATES = 4096


class TapeAction(StrEnum):
    """Special flight-tape entries that are not plain values or events."""

    PAUSE = "pause"            # value: PauseState flags (int)
    RATE = "rate"              # value: simulation rate (number)
    GAP = "gap"                # value: number of lost provider updates to report
    DISCONNECT = "disconnect"  # value: optional reason text


@dataclass(frozen=True, slots=True)
class TapeEntry:
    """One timed step. ``at_s`` counts seconds from the moment the tape starts playing.

    ``target`` is a catalog capability ID, a Signal, a SimVar or a TapeAction. A value
    target sets the value that later periodic samples report. An event target emits the
    event at that time.
    """

    at_s: float
    target: str | Signal | SimVar | TapeAction
    value: object = None


class FlightTape:
    """A replayable, time-ordered script for :class:`SimulatedProvider`.

    Build one from ``(seconds, target, value)`` rows or from JSON lines such as::

        {"t": 0, "id": "aircraft.on_ground", "value": true}
        {"t": 12.5, "simvar": ["VERTICAL SPEED", "feet per minute", "number"], "value": -180}
        {"t": 30, "action": "pause", "value": 4}
        {"t": 90, "action": "disconnect"}

    A ``simvar`` list is ``[name, unit, value_type]`` with an optional fourth index item.
    """

    def __init__(self, entries: Iterable[TapeEntry | tuple]):
        steps = []
        for index, entry in enumerate(entries):
            try:
                if not isinstance(entry, TapeEntry):
                    entry = TapeEntry(*entry)
                if type(entry.at_s) not in (int, float) or not isfinite(entry.at_s) or entry.at_s < 0:
                    raise ValueError("times must be finite, non-negative seconds")
                if isinstance(entry.target, TapeAction):
                    _check_action(entry.target, entry.value)
                elif isinstance(entry.target, str) and entry.target not in READS:
                    raise ValueError(f"unknown capability {entry.target}")
                elif not isinstance(entry.target, (str, Signal, SimVar)):
                    raise ValueError("the target must be a capability ID, Signal, SimVar or TapeAction")
            except (TypeError, ValueError) as error:
                raise ValueError(f"Flight tape entry {index} is invalid: {error}.") from error
            steps.append(entry)
        # Stable sort keeps the written order for entries at the same time.
        self.entries: tuple[TapeEntry, ...] = tuple(sorted(steps, key=lambda item: item.at_s))

    @property
    def duration_s(self) -> float:
        return self.entries[-1].at_s if self.entries else 0.0

    @classmethod
    def from_jsonl(cls, lines: Iterable[str]) -> FlightTape:
        entries = []
        for number, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
                at = row["t"]
                if "action" in row:
                    target = TapeAction(row["action"])
                elif "simvar" in row:
                    name, unit, kind, *index = row["simvar"]
                    target = SimVar(name, unit, ValueType(kind), index=index[0] if index else None)
                else:
                    target = row["id"]
                    if target not in READS:
                        raise ValueError(f"unknown capability {target}")
                if isinstance(target, TapeAction):
                    _check_action(target, row.get("value"))
                entries.append(TapeEntry(at, target, row.get("value")))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Flight tape line {number} is invalid: {error}") from error
        return cls(entries)


def _check_action(action, value):
    """Reject a bad action value when the tape is built, not later inside drain()."""
    if action is TapeAction.RATE:
        if type(value) not in (int, float) or not isfinite(value) or not 0 < value <= 128:
            raise ValueError("a rate must be a finite number above 0 and at most 128")
    elif action is TapeAction.PAUSE:
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("pause flags must be a PauseState integer")
        PauseState(value)
    elif action is TapeAction.GAP:
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("a gap must be a positive whole number of lost updates")
    elif action is TapeAction.DISCONNECT:
        if value is not None and not isinstance(value, str):
            raise ValueError("a disconnect reason must be text")


class SimulatedProvider:
    """Drive this provider and Core from the same serial owner.

    ``clock`` returns monotonic seconds (default ``time.monotonic``) and ``wall_clock`` returns
    the matching aware UTC datetime (default now). Inject both to replay a tape faster than
    real time. ``jitter_ms`` moves each periodic sample up to that many milliseconds either side
    of its grid time, reproducibly with ``seed``. ``command_delay_s`` makes command outcomes
    arrive that long after sending, like the live adapter's readback window; with
    ``confirm_commands=False`` the caller completes each command with ``complete_command``.
    ``interval_granularity_ms`` (default the live adapter's 1000, or ``minimum_interval_ms``
    when that is smaller) and ``max_command_timeout_seconds`` are published for Core as the
    live adapter publishes them.
    """

    source = Source("BBC Core simulated provider", simulated=True)

    def __init__(self, *, confirm_commands=True, minimum_interval_ms=1000, periodic=True,
                 clock: Callable[[], float] | None = None, wall_clock: Callable[[], datetime] | None = None,
                 jitter_ms=0, seed=None, command_delay_s=0.0, frame_rate_hz=30.0,
                 absolute_time_s: float | None = None, max_command_timeout_seconds=4.0,
                 interval_granularity_ms: int | None = None):
        if type(minimum_interval_ms) is not int or not 1 <= minimum_interval_ms <= 3_600_000:
            raise ValueError("The minimum interval must be 1 to 3,600,000 ms.")
        if type(jitter_ms) not in (int, float) or not 0 <= jitter_ms < minimum_interval_ms / 2:
            raise ValueError("Jitter must be non-negative and less than half the minimum interval.")
        if type(command_delay_s) not in (int, float) or not 0 <= command_delay_s <= 60:
            raise ValueError("The command delay must be 0 to 60 seconds.")
        if type(frame_rate_hz) not in (int, float) or not 1 <= frame_rate_hz <= 1000:
            raise ValueError("The simulated frame rate must be 1 to 1,000 Hz.")
        if interval_granularity_ms is None:
            # The live adapter's whole seconds, or finer when a test asks for sub-second cadence.
            interval_granularity_ms = min(1000, minimum_interval_ms)
        if type(interval_granularity_ms) is not int or not 1 <= interval_granularity_ms <= 3_600_000:
            raise ValueError("The interval granularity must be 1 to 3,600,000 ms.")
        if type(max_command_timeout_seconds) not in (int, float) or not 0 < max_command_timeout_seconds <= 600:
            raise ValueError("The command timeout must be positive and at most 600 seconds.")
        self.minimum_interval_ms = minimum_interval_ms
        self.interval_granularity_ms = interval_granularity_ms
        self.max_command_timeout_seconds = max(float(max_command_timeout_seconds), float(command_delay_s))
        self._confirm_commands = confirm_commands
        self._periodic = periodic
        self._clock = clock
        self._wall_clock = wall_clock
        self._jitter_s = jitter_ms / 1000
        self._random = Random(seed)
        self._command_delay_s = float(command_delay_s)
        self._frame_s = 1 / frame_rate_hz
        self._initial_absolute = absolute_time_s
        self._catalog_signals = frozenset(entry.signal for entry in READS.values())
        self._events = frozenset(SYSTEM_EVENTS.values())
        self._states = frozenset(SYSTEM_STATES.values())
        self._signals_by_id = {id: entry.signal for id, entry in READS.items()}
        self._values: dict[Signal, object] = {
            self._signals_by_id["aircraft.title"]: "Simulated aircraft",
            self._signals_by_id["aircraft.ground_speed"]: 0.0,
            self._signals_by_id["aircraft.on_ground"]: 1,
            self._signals_by_id["aircraft.latitude"]: 0.0,
            self._signals_by_id["aircraft.longitude"]: 0.0,
            COM1_ACTIVE: 118_100_000,
            COM1_STANDBY: 121_500_000,
            COM1_SPACING: 0,
            ENGINE1_THROTTLE: 0.0,
            SYSTEM_STATES["SIM"]: True,
            SYSTEM_STATES["DIALOGMODE"]: False,
            SYSTEM_STATES["AIRCRAFTLOADED"]: "SimObjects\\Airplanes\\Simulated\\aircraft.cfg",
            SYSTEM_STATES["FLIGHTLOADED"]: "",
            SYSTEM_STATES["FLIGHTPLAN"]: "",
        }
        self._session_id = None
        self._reset_session()

    @property
    def clock(self) -> Callable[[], float] | None:
        """The injected monotonic clock, if any. Core adopts it so both judge time alike."""
        return self._clock

    # -- Session -------------------------------------------------------------------------------

    def _reset_session(self):
        self._transport_lost = False
        self._outstanding = None
        self._outstanding_due = None
        self._dropped = 0
        self._updates = deque()
        self.active_demand = ()
        self._demand = {}
        self._enabled_events = frozenset()
        self._schedule = []
        self._next_due = {}
        self._last_stamp = {}
        self._last_snapshot = {}
        self._sequence = count()
        self._tape = None
        self._tape_index = 0
        self._tape_start = 0.0
        self._model_at = None
        self._rate = 1.0
        self._pause = PauseState.NONE
        self._sim_time = 0.0
        self._absolute_time = None

    def supports_signal(self, signal):
        if signal.snapshot is not None:
            return (signal.value_type is ValueType.SNAPSHOT and bool(signal.snapshot.fields)
                    and all(self._plain(field) for field in signal.snapshot.fields))
        if signal.event:
            return signal in self._events
        if signal.system_state:
            return signal in self._states
        return self._plain(signal)

    @staticmethod
    def _plain(signal):
        if (signal.event or signal.system_state or signal.input_id is not None or signal.snapshot is not None
                or signal.client_data_region is not None or signal.client_event or signal.hardware_input is not None):
            return False
        return (signal.value_type in _PLAIN_TYPES and signal.name.isascii()
                and (signal.unit is None) == (signal.value_type is ValueType.TEXT))

    def supports_command(self, operation):
        return operation in OPERATIONS or operation in (SIMVAR_WRITE, CLIENT_EVENT_SEND)

    def connect(self, session_id):
        if self._session_id is not None:
            raise ProviderError("The simulated provider already has a session.")
        self._reset_session()
        self._session_id = session_id
        now = self._now()
        self._model_at = now
        if self._initial_absolute is not None:
            self._absolute_time = float(self._initial_absolute)
        else:
            wall = self._wall_clock() if self._wall_clock else datetime.now(timezone.utc)
            self._absolute_time = (wall - _EPOCH).total_seconds()

    def disconnect(self):
        self._session_id = None
        self._reset_session()

    def replace_demand(self, demand):
        # When loss is already queued, let drain deliver that transition.
        if self._transport_lost:
            return
        self._require_session()
        if any(not self.supports_signal(item.signal) for item in demand):
            raise ProviderError("The simulated provider does not offer a requested signal.")
        now = self._now()
        self._advance_model(now)
        previous = self._demand
        self.active_demand = demand
        self._demand = {item.signal: item for item in demand}
        self._enabled_events = frozenset(item.signal for item in demand if item.signal.event and item.event_enabled)
        for signal in tuple(self._next_due):
            if signal not in self._demand:
                del self._next_due[signal]
                self._last_snapshot.pop(signal, None)
        for item in demand:
            signal = item.signal
            if signal.event:
                continue
            if signal.system_state:
                if signal not in previous:
                    self.request_system_state(signal)
                continue
            old = previous.get(signal)
            if old is None:
                self._emit_value(signal, now)
            if old is None or old.interval_ms != item.interval_ms:
                self._arm(signal, now)

    # -- Scripting -----------------------------------------------------------------------------

    def publish(self, target, value):
        """Set a value and send it at once, or emit an event, at the provider clock's time."""
        self._require_session()
        signal = self._signal(target)
        now = self._now()
        self._advance_model(now)
        if signal.event:
            self._emit_event(signal, value, now)
            return
        if signal == _SIM_RATE:
            self._set_rate(value, now)
        self._values[signal] = value
        if signal.system_state:
            self._emit_reading(signal, value, now)
        else:
            self._emit_value(signal, now)

    def set_value(self, target, value, *, emit=False):
        """Script a value. Without ``emit`` it appears at the next periodic sample, like live data."""
        signal = self._signal(target)
        if signal.event:
            raise ValueError("Use publish() or emit_event() for an event.")
        if signal == _SIM_RATE:
            self._set_rate(value, self._now())
        self._values[signal] = value
        if emit:
            self.publish(signal, value)

    def emit_event(self, target, value=0):
        self._require_session()
        signal = self._signal(target)
        if not signal.event:
            raise ValueError("Use set_value() or publish() for a value.")
        self._emit_event(signal, value, self._now())

    def set_pause(self, flags):
        """Change the pause flags. Emits Pause_EX1, Pause and Paused/Unpaused when demanded."""
        self._require_session()
        self._apply_pause(PauseState(int(flags)), self._now())

    def set_simulation_rate(self, rate):
        self._require_session()
        self._set_rate(rate, self._now())

    def drop_updates(self, count_=1):
        """Report ``count_`` lost provider updates at the next drain, as an overflow would."""
        self._require_session()
        if type(count_) is not int or count_ < 1:
            raise ValueError("Report at least one lost update.")
        self._dropped += count_

    def play(self, tape: FlightTape, *, start: float | None = None):
        """Replay ``tape`` from ``start`` (provider clock seconds, default now) as the clock advances."""
        self._require_session()
        if not isinstance(tape, FlightTape):
            raise ValueError("Supply a FlightTape.")
        self._tape = tape
        self._tape_index = 0
        self._tape_start = self._now() if start is None else float(start)

    @property
    def tape_finished(self) -> bool:
        return self._tape is None or self._tape_index >= len(self._tape.entries)

    # -- Commands ------------------------------------------------------------------------------

    def send(self, command):
        self._require_session()
        if command.session_id != self._session_id:
            raise CommandRejected("The command belongs to a different simulated session.")
        if not self.supports_command(command.operation):
            raise CommandRejected("The simulated provider does not support this command.")
        if self._outstanding is not None:
            raise CommandRejected("The simulated provider is still waiting on another command.")
        if command.operation == "COM_STBY_RADIO_SET_HZ":
            problem = com_channel_problem(command.arguments[0], self._values.get(COM1_SPACING))
            if problem is not None:
                raise CommandRejected(problem)
        if command.operation == SIMVAR_WRITE and (command.sdk_signal is None or not self._plain(command.sdk_signal)):
            raise CommandRejected("The simulated provider cannot write this SimVar.")
        self._outstanding = command
        if self._confirm_commands:
            if self._command_delay_s:
                self._outstanding_due = self._now() + self._command_delay_s
            else:
                self.complete_command()

    def complete_command(self, *, accepted=True, _at=None):
        self._require_session()
        command = self._outstanding
        if command is None:
            raise ProviderError("There is no outstanding simulated command.")
        self._outstanding = None
        self._outstanding_due = None
        now = self._now() if _at is None else _at
        if not accepted:
            self._offer(CommandOutcome(
                command.session_id, command.id, Outcome.REJECTED,
                "The simulated provider rejected the command.",
            ))
            return
        if command.operation == SIMVAR_WRITE:
            self._set_and_emit(command.sdk_signal, command.arguments[0], now)
            self._confirm(command)
            return
        if command.operation == CLIENT_EVENT_SEND:
            self._offer(CommandOutcome(
                command.session_id, command.id, Outcome.UNVERIFIED,
                "The simulated event was sent and has no configured state readback.",
            ))
            return
        mapping = OPERATIONS[command.operation]
        if mapping.transport is CommandTransport.SIMVAR:
            self._set_and_emit(mapping.signal, command.arguments[0], now)
        elif mapping.transport is CommandTransport.EXTENDED_EVENT:
            self._offer(CommandOutcome(
                command.session_id, command.id, Outcome.UNVERIFIED,
                "The simulated event was sent and has no configured state readback.",
            ))
            return
        elif command.operation == "COM_STBY_RADIO_SET_HZ":
            problem = com_channel_problem(command.arguments[0], self._values.get(COM1_SPACING))
            if problem is not None:
                # The spacing changed while the command was in flight; the radio kept its value.
                self._offer(CommandOutcome(command.session_id, command.id, Outcome.REJECTED, problem))
                return
            self._set_and_emit(COM1_STANDBY, command.arguments[0], now)
        elif command.operation == "COM_STBY_RADIO_SWAP":
            self._values[COM1_ACTIVE], self._values[COM1_STANDBY] = (
                self._values[COM1_STANDBY], self._values[COM1_ACTIVE],
            )
            self._emit_value(COM1_ACTIVE, now)
            self._emit_value(COM1_STANDBY, now)
        else:
            raise ProviderError("The simulated provider has no implementation for this command.")
        self._confirm(command)

    def _confirm(self, command):
        self._offer(CommandOutcome(
            command.session_id, command.id, Outcome.CONFIRMED, "Confirmed against the simulated state.",
        ))

    def _set_and_emit(self, signal, value, now):
        self._values[signal] = value
        self._emit_value(signal, now)

    # -- System state --------------------------------------------------------------------------

    def request_system_state(self, signal):
        if self._session_id is not None and self._transport_lost:
            # The queued Disconnect reports the loss; a request must not turn it into an error.
            return
        self._require_session()
        now = self._now()
        if signal not in self._states:
            self._emit_reading(signal, None, now, error="The simulated provider does not offer this state.")
        elif self._values.get(signal) is None:
            self._emit_reading(signal, None, now, error="The simulated provider has no value for this state.")
        else:
            self._emit_reading(signal, self._values[signal], now)

    # -- Transport -----------------------------------------------------------------------------

    def lose_connection(self):
        self._require_session()
        log.debug("Simulated transport loss for session %s", self._session_id)
        self._transport_lost = True
        self._dropped += len(self._updates)
        self._updates.clear()
        self._offer(Disconnect(self._session_id, "Simulated transport loss."))

    def drain(self, limit):
        if self._session_id is not None and not self._transport_lost:
            self._run_until(self._now())
        batch = []
        if self._dropped:
            batch.append(IngressGap(self._session_id, self._dropped))
            self._dropped = 0
        while self._updates and len(batch) < limit:
            batch.append(self._updates.popleft())
        return tuple(batch)

    def _run_until(self, now):
        """Replay tape steps, command completions and periodic samples due by ``now``, in time order."""
        while not self._transport_lost:
            candidates = []
            if self._tape is not None and self._tape_index < len(self._tape.entries):
                candidates.append((self._tape_start + self._tape.entries[self._tape_index].at_s, 0))
            if self._outstanding_due is not None:
                candidates.append((self._outstanding_due, 1))
            due = self._peek_schedule() if self._periodic else None
            if due is not None:
                candidates.append((due, 2))
            if not candidates:
                return
            at, kind = min(candidates)
            if at > now:
                return
            self._advance_model(at)
            if kind == 0:
                entry = self._tape.entries[self._tape_index]
                self._tape_index += 1
                self._apply_tape(entry, at)
            elif kind == 1:
                self.complete_command(_at=at)
            else:
                _, _, signal = heapq.heappop(self._schedule)
                nominal, _ = self._next_due.pop(signal)
                self._emit_value(signal, at)
                self._arm(signal, at, nominal)
        return

    def _apply_tape(self, entry, at):
        target, value = entry.target, entry.value
        if target is TapeAction.PAUSE:
            self._apply_pause(PauseState(int(value)), at)
        elif target is TapeAction.RATE:
            self._set_rate(value, at)
        elif target is TapeAction.GAP:
            self._dropped += int(value or 1)
        elif target is TapeAction.DISCONNECT:
            log.debug("Flight tape disconnects session %s at %.3f", self._session_id, at)
            self._transport_lost = True
            self._offer(Disconnect(self._session_id, str(value or "Simulated transport loss.")))
        else:
            signal = self._signal(target)
            if signal.event:
                # A tape row without a value is a plain notification, as emit_event() defaults it.
                self._emit_event(signal, 0 if value is None else value, at)
            else:
                if signal == _SIM_RATE:
                    self._set_rate(value, at)
                # A state change shows up when Core next requests the state, as in the simulator.
                self._values[signal] = value

    # -- Periodic schedule ---------------------------------------------------------------------

    def _step_s(self, signal):
        demand = self._demand.get(signal)
        if signal.snapshot is not None:
            spec = signal.snapshot
            if spec.period is SnapshotPeriod.SECOND:
                return max(spec.every * 1000, self.minimum_interval_ms) / 1000
            return spec.every * self._frame_s
        interval = demand.interval_ms if demand is not None else None
        return max(interval or 1000, self.minimum_interval_ms) / 1000

    def _arm(self, signal, now, nominal=None):
        if not self._periodic or signal not in self._demand:
            return
        step = self._step_s(signal)
        nominal = (now if nominal is None else nominal) + step
        if nominal <= now:  # fell behind (for example after a long pause in pumping): resync
            nominal = now + step
        token = next(self._sequence)
        self._next_due[signal] = (nominal, token)
        jitter = self._random.uniform(-self._jitter_s, self._jitter_s) if self._jitter_s else 0.0
        heapq.heappush(self._schedule, (max(nominal + jitter, now), token, signal))

    def _peek_schedule(self):
        while self._schedule:
            at, token, signal = self._schedule[0]
            current = self._next_due.get(signal)
            if current is not None and current[1] == token:
                return at
            heapq.heappop(self._schedule)
        return None

    # -- Simulator clock model -----------------------------------------------------------------

    def _advance_model(self, at):
        if self._model_at is None or at <= self._model_at:
            return
        elapsed = at - self._model_at
        self._model_at = at
        if not self._pause & _FROZEN:
            self._sim_time += elapsed * self._rate
            if self._absolute_time is not None:
                self._absolute_time += elapsed * self._rate

    def _set_rate(self, rate, at):
        if type(rate) not in (int, float) or not isfinite(rate) or not 0 < rate <= 128:
            raise ValueError("The simulation rate must be a positive number up to 128.")
        self._advance_model(at)
        self._rate = float(rate)
        self._values.pop(_SIM_RATE, None)

    def _apply_pause(self, flags, at):
        self._advance_model(at)
        before = self._pause
        self._pause = flags
        self._emit_event(PAUSE_STATE, int(flags), at)
        if bool(before & _FROZEN) != bool(flags & _FROZEN):
            paused = bool(flags & _FROZEN)
            self._emit_event(SYSTEM_EVENTS["PAUSE"], int(paused), at)
            self._emit_event(SYSTEM_EVENTS["PAUSED" if paused else "UNPAUSED"], 0, at)

    def _model_value(self, signal):
        if signal == _SIM_TIME:
            return self._sim_time
        if signal == _ABSOLUTE_TIME:
            return self._absolute_time
        if signal == _ZULU_TIME:
            return None if self._absolute_time is None else self._absolute_time % 86_400
        if signal == _SIM_RATE:
            return self._rate
        return None

    def _value(self, signal):
        if signal in self._values:
            return self._values[signal]
        return self._model_value(signal)

    # -- Emission ------------------------------------------------------------------------------

    def _emit_value(self, signal, stamp):
        if signal not in self._demand:
            return
        if signal.snapshot is not None:
            values = tuple(self._value(field) for field in signal.snapshot.fields)
            if signal.snapshot.changed_only and self._last_snapshot.get(signal) == values:
                return
            self._last_snapshot[signal] = values
            if signal.snapshot.changed_only:
                # Like the live adapter: the clocks change every frame, so a changed-only
                # snapshot does not read them and reports no simulator time.
                self._emit_reading(signal, SnapshotFrame(values), stamp)
                return
            frame = SnapshotFrame(values, self._sim_time, self._absolute_time, self._rate)
            self._emit_reading(signal, frame, stamp, sample_time=simulator_utc(self._absolute_time))
            return
        value = self._value(signal)
        if value is not None:
            # Like the live adapter, single values carry host receipt time only.
            self._emit_reading(signal, value, stamp)

    def _emit_event(self, signal, value, stamp):
        if signal in self._enabled_events:
            self._emit_reading(signal, value, stamp)

    def _emit_reading(self, signal, value, stamp, *, error=None, sample_time=None):
        if self._session_id is None:
            return
        stamp = max(stamp, self._last_stamp.get(signal, stamp))
        self._last_stamp[signal] = stamp
        self._offer(Reading(
            self._session_id, signal, value, self._utc_at(stamp), stamp, sample_time, error,
        ))

    def _utc_at(self, stamp):
        wall = self._wall_clock() if self._wall_clock else datetime.now(timezone.utc)
        return wall - timedelta(seconds=max(0.0, self._now() - stamp))

    def _now(self):
        return self._clock() if self._clock is not None else monotonic()

    def _signal(self, target):
        if isinstance(target, Signal):
            return target
        if isinstance(target, SimVar):
            return Signal(target.sdk_name, target.unit, target.value_type, simvar_format=target.format,
                          local_simvar=target.name.startswith("L:"))
        try:
            return self._signals_by_id[target]
        except KeyError as error:
            raise ValueError(f"Unknown simulated capability: {target}.") from error

    def _require_session(self):
        if self._session_id is None or self._transport_lost:
            raise ProviderError("The simulated session is unavailable.")

    def _offer(self, update):
        if len(self._updates) >= _MAX_UPDATES:
            self._updates.popleft()
            self._dropped += 1
        self._updates.append(update)
