"""Explicitly simulated provider. It creates no worker or simulator connection."""

from collections import deque
from datetime import datetime, timezone
from time import monotonic

from .catalog import COM1_ACTIVE, COM1_STANDBY, ENGINE1_THROTTLE, OPERATIONS, READS, CommandTransport
from .contract import Source
from .provider import (
    CommandOutcome, Disconnect, IngressGap, Outcome, ProviderError, Reading,
)


class SimulatedProvider:
    """Drive this provider and Core from the same serial owner."""

    source = Source("BBC Core simulated provider", simulated=True)
    minimum_interval_ms = 100

    def __init__(self, *, confirm_commands=True):
        self._session_id = None
        self._transport_lost = False
        self._confirm_commands = confirm_commands
        self._outstanding = None
        self._dropped = 0
        self._updates = deque()
        self.active_demand = ()
        self._signals_by_id = {id: entry.signal for id, entry in READS.items()}
        self._values = {
            self._signals_by_id["aircraft.title"]: "Simulated aircraft",
            self._signals_by_id["aircraft.ground_speed"]: 0.0,
            self._signals_by_id["aircraft.on_ground"]: 1,
            self._signals_by_id["aircraft.latitude"]: 0.0,
            self._signals_by_id["aircraft.longitude"]: 0.0,
            COM1_ACTIVE: 118_100_000,
            COM1_STANDBY: 121_500_000,
            ENGINE1_THROTTLE: 0.0,
        }

    def supports_signal(self, signal):
        return not signal.system_state and (
            signal in self._values or signal in self._signals_by_id.values()
        )

    def supports_command(self, operation):
        return operation in OPERATIONS

    def connect(self, session_id):
        if self._session_id is not None:
            raise ProviderError("The simulated provider already has a session.")
        self._session_id = session_id

    def disconnect(self):
        self._session_id = None
        self._transport_lost = False
        self._outstanding = None
        self._dropped = 0
        self._updates.clear()
        self.active_demand = ()

    def replace_demand(self, demand):
        # When loss is already queued, let drain deliver that transition.
        if self._transport_lost:
            return
        self._require_session()
        if any(not self.supports_signal(item.signal) for item in demand):
            raise ProviderError("The simulated provider does not offer a requested signal.")
        previous = {item.signal for item in self.active_demand}
        self.active_demand = demand
        for item in demand:
            if item.signal not in previous and item.signal in self._values:
                self._emit_reading(item.signal, self._values[item.signal])

    def publish(self, capability_id, value):
        """Supply one simulated value or event at the caller's chosen time."""
        self._require_session()
        signal = self._signals_by_id[capability_id]
        if not signal.event:
            self._values[signal] = value
        self._emit_reading(signal, value)

    def send(self, command):
        self._require_session()
        if command.session_id != self._session_id or not self.supports_command(command.operation):
            raise ProviderError("Invalid simulated command or session.")
        if self._outstanding is not None:
            raise ProviderError("The simulated provider already has a pending command.")
        self._outstanding = command
        if self._confirm_commands:
            self.complete_command()

    def complete_command(self, *, accepted=True):
        self._require_session()
        command = self._outstanding
        if command is None:
            raise ProviderError("There is no outstanding simulated command.")
        self._outstanding = None
        if not accepted:
            self._offer(CommandOutcome(
                command.session_id, command.id, Outcome.REJECTED,
                "The simulated provider rejected the command.",
            ))
            return
        mapping = OPERATIONS[command.operation]
        if mapping.transport is CommandTransport.SIMVAR:
            self._values[mapping.signal] = command.arguments[0]
            self._emit_reading(mapping.signal, command.arguments[0])
        elif mapping.transport is CommandTransport.EXTENDED_EVENT:
            self._offer(CommandOutcome(
                command.session_id, command.id, Outcome.UNVERIFIED,
                "The simulated event was sent and has no configured state readback.",
            ))
            return
        elif command.operation == "COM_STBY_RADIO_SET_HZ":
            self._values[COM1_STANDBY] = command.arguments[0]
            self._emit_reading(COM1_STANDBY, self._values[COM1_STANDBY])
        elif command.operation == "COM_STBY_RADIO_SWAP":
            self._values[COM1_ACTIVE], self._values[COM1_STANDBY] = (
                self._values[COM1_STANDBY], self._values[COM1_ACTIVE],
            )
            self._emit_reading(COM1_ACTIVE, self._values[COM1_ACTIVE])
            self._emit_reading(COM1_STANDBY, self._values[COM1_STANDBY])
        else:
            raise ProviderError("The simulated provider has no implementation for this command.")
        self._offer(CommandOutcome(
            command.session_id, command.id, Outcome.CONFIRMED,
            "Confirmed against the simulated state.",
        ))

    def lose_connection(self):
        self._require_session()
        self._transport_lost = True
        self._dropped += len(self._updates)
        self._updates.clear()
        self._offer(Disconnect(self._session_id, "Simulated transport loss."))

    def drain(self, limit):
        batch = []
        if self._dropped:
            batch.append(IngressGap(self._session_id, self._dropped))
            self._dropped = 0
        while self._updates and len(batch) < limit:
            batch.append(self._updates.popleft())
        return tuple(batch)

    def _require_session(self):
        if self._session_id is None or self._transport_lost:
            raise ProviderError("The simulated session is unavailable.")

    def _emit_reading(self, signal, value):
        if any(item.signal == signal and item.event_enabled for item in self.active_demand):
            self._offer(Reading(
                self._session_id, signal, value, datetime.now(timezone.utc), monotonic(),
            ))

    def _offer(self, update):
        if len(self._updates) >= 4096:
            self._updates.popleft()
            self._dropped += 1
        self._updates.append(update)
