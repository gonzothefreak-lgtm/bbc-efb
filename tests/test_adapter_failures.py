"""SimConnect provider failure paths that had no test (finding A36, remainder).

Each test makes one SDK call fail, or one reply arrive malformed, and checks that the
failure stays where it belongs: a command is reported UNCERTAIN or REJECTED (never
CONFIRMED), a single signal reports an error, and the session itself stays up.
"""

from itertools import count
from struct import pack
from unittest import TestCase
from unittest.mock import patch

from bbc_core import CommandState, ConnectionState, Core
from bbc_core.catalog import READS, SYSTEM_EVENTS, SYSTEM_STATES
from bbc_core.facilities import FacilityState
from bbc_core.provider import Demand, Disconnect, FacilityReply, ProviderError, Reading
from bbc_core.simconnect import _UNUSED, SimConnectProvider
from bbc_core.simconnect_abi import Period
from bbc_core.simulator_ops import SimulatorRead
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk


class FailingSdk(FakeSdk):
    """FakeSdk whose named calls raise ProviderError, optionally only after a transmit."""

    def __init__(self):
        super().__init__()
        self.failing = set()
        self.failing_after_transmit = set()
        self.short_readback = False

    def _transmitted(self):
        return bool(self.matching_calls("SimConnect_TransmitClientEvent"))

    def call(self, name, handle, *args):
        if name in self.failing or (name in self.failing_after_transmit and self._transmitted()):
            raise ProviderError(f"fake {name} failure")
        if (self.short_readback and name == "SimConnect_RequestDataOnSimObject" and self._transmitted()
                and args[3] == Period.ONCE and len(self.definitions.get(args[1], ())) == 2):
            self._handle_is_current(handle)
            self.packet_id += 1
            self.calls.append((name, args, self.packet_id))
            self.data(args[0], pack("<d", 1.0), definition=args[1], count=2)  # One value for two fields.
            return
        super().call(name, handle, *args)


class ProviderFailureCase(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100.0)
        self.sdk = FailingSdk()
        self.provider = SimConnectProvider(self.sdk)

    def connect(self):
        self.provider.connect("s")
        self.addCleanup(self.provider.disconnect)
        return list(self.provider.drain(256))


class SessionFailureTests(ProviderFailureCase):
    def test_connect_over_an_open_handle_is_refused_without_a_second_open(self):
        self.connect()
        with self.assertRaisesRegex(ProviderError, "previous SimConnect handle"):
            self.provider.connect("t")
        self.assertIsNotNone(self.sdk.handle)  # The first session is untouched.

    def test_diagnostics_need_a_session_and_a_transport_that_reports_timings(self):
        with self.assertRaisesRegex(ProviderError, "connected SimConnect session"):
            self.provider.response_times()
        self.connect()
        with self.assertRaisesRegex(ProviderError, "does not expose response timings"):
            self.provider.response_times()

    def test_request_id_exhaustion_is_a_provider_error_not_a_reused_id(self):
        self.connect()
        self.provider._ids = count(_UNUSED)
        with self.assertRaisesRegex(ProviderError, "request ID space exhausted"):
            self.provider._new_id()


class SignalFailureTests(ProviderFailureCase):
    def readings(self, updates):
        return [update for update in updates if isinstance(update, Reading)]

    def test_a_refused_system_state_request_reports_an_error_for_that_signal_only(self):
        self.connect()
        state = SYSTEM_STATES["SIM"]
        self.sdk.failing.add("SimConnect_RequestSystemState")
        self.provider.replace_demand((Demand(state, None),))  # Registration requests the state.
        updates = list(self.provider.drain(64))
        (reading,) = self.readings(updates)
        self.assertEqual((reading.signal, reading.value), (state, None))
        self.assertIn("fake SimConnect_RequestSystemState failure", reading.error)
        self.assertFalse(any(isinstance(update, Disconnect) for update in updates))
        self.assertEqual(self.provider._state_requests, {})
        # A later request is tried again rather than waiting on the failed one.
        self.sdk.failing.clear()
        self.provider.request_system_state(state)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_RequestSystemState")), 1)

    def test_a_refused_system_event_subscription_reports_an_error_and_other_signals_register(self):
        self.connect()
        crashed = SYSTEM_EVENTS["CRASHED"]
        speed = READS["aircraft.ground_speed"].signal
        self.sdk.failing.add("SimConnect_SubscribeToSystemEvent")
        self.provider.replace_demand((Demand(crashed, None), Demand(speed, 1000)))
        updates = list(self.provider.drain(64))
        errors = [reading for reading in self.readings(updates) if reading.signal == crashed]
        self.assertEqual(len(errors), 1)
        self.assertIn("SubscribeToSystemEvent", errors[0].error)
        self.assertFalse(any(isinstance(update, Disconnect) for update in updates))
        requests = self.sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.assertEqual(len(requests), 1)  # The SimVar was still requested.

    def test_a_failed_cleanup_call_is_logged_and_the_session_stays_up(self):
        self.connect()
        speed = READS["aircraft.ground_speed"].signal
        self.provider.replace_demand((Demand(speed, 1000),))
        self.provider.drain(64)
        self.sdk.failing.add("SimConnect_ClearDataDefinition")
        with self.assertLogs("bbc_core.simconnect", "WARNING") as logs:
            self.provider.replace_demand(())
        self.assertIn("cleanup call SimConnect_ClearDataDefinition failed", "\n".join(logs.output))
        self.assertNotIn(speed, self.provider._registrations)
        self.assertFalse(any(isinstance(update, Disconnect) for update in self.provider.drain(64)))

    def test_a_refused_dialog_notification_request_is_an_unavailable_reply(self):
        self.connect()
        query = SimulatorRead("dialog")
        with patch.object(self.provider._client_events, "request_dialog_notifications",
                          side_effect=ProviderError("fake dialog refusal")):
            self.provider.request_facility("read-1", query)
        (reply,) = [update for update in self.provider.drain(64) if isinstance(update, FacilityReply)]
        self.assertEqual((reply.id, reply.state), ("read-1", FacilityState.UNAVAILABLE))
        self.assertEqual(reply.detail, "fake dialog refusal")


class CommandFailureTests(FakeClock, TestCase):
    """A command is never reported CONFIRMED when its send or readback path failed."""

    def setUp(self):
        self.start_clock(100.0)
        self.sdk = FailingSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.radio = self.core.register_consumer(control_groups=("radios",))
        self.core.connect()

    def run_tune(self, pumps=4):
        tune = self.radio.submit_command("radio.com1.set_standby", 123_450_000)
        for _ in range(pumps):
            self.core.pump()
            self.advance(0.2)
        return self.radio.command_result(tune.id)

    def test_a_refused_event_mapping_rejects_the_command_before_transmission(self):
        self.sdk.failing.add("SimConnect_MapClientEventToSimEvent")
        result = self.run_tune()
        self.assertIs(result.state, CommandState.REJECTED)
        self.assertIn("fake SimConnect_MapClientEventToSimEvent failure", result.detail)
        self.assertEqual(self.sdk.matching_calls("SimConnect_TransmitClientEvent"), [])
        self.assertIs(self.radio.connection().state, ConnectionState.CONNECTED)

    def test_a_refused_readback_request_makes_the_command_uncertain(self):
        self.sdk.failing_after_transmit.add("SimConnect_RequestDataOnSimObject")
        result = self.run_tune()
        self.assertIs(result.state, CommandState.UNCERTAIN)
        self.assertIn("Could not request", result.detail)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_TransmitClientEvent")), 1)  # Never replayed.
        self.assertIs(self.radio.connection().state, ConnectionState.CONNECTED)

    def test_a_malformed_readback_reply_makes_the_command_uncertain(self):
        self.sdk.short_readback = True
        result = self.run_tune()
        self.assertIs(result.state, CommandState.UNCERTAIN)
        self.assertIn("Invalid SimConnect data definition or payload", result.detail)  # Not a timeout.
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_TransmitClientEvent")), 1)
        self.assertIs(self.radio.connection().state, ConnectionState.CONNECTED)
