"""Check system state and notifications through the public API and fake SDK."""

from unittest import TestCase
from uuid import UUID

from bbc_core import (
    ConnectionState, Core, CoreRequestError, Event, FrameSample, InputCatalogState, MissionAction,
    Quality, SimObjectChange,
)
from bbc_core.contract import SignalError  # C7: provider errors are not Gaps
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import (
    ReceiveCustomActionPrefix, ReceiveEvent, ReceiveEventFilename, ReceiveEventFrame, ReceiveEventObject, ReceiveId,
    ReceiveRequest, ReceiveSystemState,
)
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk, packet


class SimulatorSessionOfflineTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100)  # Shared clock for every bbc_core module, sleep included (A139).
        self.sdk = FakeSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.first = self.core.register_consumer()

    def requests(self):
        return self.sdk.matching_calls("SimConnect_RequestSystemState")

    def event_id(self, name):
        return next(args[0] for args, _ in self.sdk.matching_calls("SimConnect_SubscribeToSystemEvent")
                    if args[1].upper() == name.upper())

    def reply(self, request, **fields):
        self.sdk.incoming.append(packet(ReceiveSystemState, ReceiveId.SYSTEM_STATE, request_id=request, **fields))

    def events(self, consumer=None):
        return [update for update in (consumer or self.first).poll() if isinstance(update, Event)]

    def test_five_documented_states_are_typed_allow_empty_plan_and_no_polling(self):
        cases = {
            b"Sim": ("sim.running", {"integer": 1}, True),
            b"DialogMode": ("sim.dialog_mode", {"integer": 0}, False),
            b"AircraftLoaded": ("sim.loaded_aircraft_file", {"text": b"jet.air"}, "jet.air"),
            b"FlightLoaded": ("sim.loaded_flight_file", {"text": b"flight.flt"}, "flight.flt"),
            b"FlightPlan": ("sim.active_flight_plan_file", {"text": b""}, ""),
        }
        subscriptions = {name: self.first.subscribe(case[0]) for name, case in cases.items()}
        with self.assertRaises(CoreRequestError):
            self.first.subscribe("sim.running", interval_ms=1000)
        self.core.connect()
        self.core.pump()
        self.assertEqual({args[1] for args, _ in self.requests()}, cases.keys())
        for (request, name), _ in self.requests():
            self.reply(request, **cases[name][1])
        self.core.pump()
        for name, subscription in subscriptions.items():
            observation = self.first.latest(subscription.id)
            self.assertEqual(observation.value, cases[name][2])
            self.assertIs(observation.quality, Quality.CURRENT)
        self.now += 30
        self.core.pump()
        self.assertEqual(len(self.requests()), 5)

    def test_shares_state_requests_and_refreshes_and_uses_configured_age(self):
        second = self.core.register_consumer()
        a = self.first.subscribe("sim.running", max_age_ms=100)
        b = second.subscribe("sim.running")
        self.core.connect()
        self.first.request_system_state(a.id)
        second.request_system_state(b.id)
        self.core.pump()
        self.assertEqual(len(self.requests()), 1)
        self.reply(self.requests()[-1][0][0], integer=0)
        self.core.pump()
        self.now += 1
        self.assertIs(self.first.latest(a.id).quality, Quality.STALE)
        self.assertIs(second.latest(b.id).quality, Quality.CURRENT)
        self.first.request_system_state(a.id)
        second.request_system_state(b.id)
        self.core.pump()
        self.assertEqual(len(self.requests()), 2)
        self.reply(self.requests()[-1][0][0], integer=1)
        self.core.pump()
        self.assertIs(second.latest(b.id).value, True)

    def test_reject_timeout_invalid_payload_and_recovery_are_local_to_state(self):
        a = self.first.subscribe("sim.running")
        b = self.first.subscribe("sim.active_flight_plan_file")
        self.core.connect()
        self.core.pump()
        requests = {args[1]: (args[0], send) for args, send in self.requests()}
        old_request, _ = requests[b"Sim"]
        self.sdk.exception(requests[b"FlightPlan"][1])
        self.now += 4
        self.core.pump()
        self.assertIn("timed out", self.first.latest(a.id).reason)
        self.assertIn("exception", self.first.latest(b.id).reason)
        self.first.request_system_state(a.id)
        self.core.pump()
        request = self.requests()[-1][0][0]
        self.reply(old_request, integer=1)
        self.sdk.incoming.append(packet(ReceiveRequest, ReceiveId.SYSTEM_STATE, request_id=request))
        self.core.pump()
        self.assertIn("Truncated", self.first.latest(a.id).reason)
        self.first.request_system_state(a.id)
        self.core.pump()
        self.reply(self.requests()[-1][0][0], integer=2)
        self.core.pump()
        self.assertIn("Invalid SimConnect Boolean", self.first.latest(a.id).reason)
        self.first.request_system_state(a.id)
        self.core.pump()
        self.reply(self.requests()[-1][0][0], integer=1)
        self.core.pump()
        self.assertIs(self.first.latest(a.id).value, True)
        self.assertIs(self.first.connection().state, ConnectionState.CONNECTED)

    def test_event_payloads_preserve_pause_flags_filenames_and_metrics(self):
        for capability in ("sim.pause_state", "sim.running_changed", "sim.flight_loaded",
                           "sim.frame", "sim.object_removed", "sim.weather_mode_changed"):
            self.first.subscribe(capability)
        self.core.connect()
        self.core.pump()
        self.sdk.event(self.event_id(b"Pause_EX1"), 4 | 8)
        self.sdk.event(self.event_id(b"Sim"), 1)
        self.sdk.incoming.extend((
            packet(ReceiveEventFilename, ReceiveId.EVENT_FILENAME,
                   event_id=self.event_id(b"FlightLoaded"), filename=b"test.flt"),
            packet(ReceiveEventFrame, ReceiveId.EVENT_FRAME,
                   event_id=self.event_id(b"Frame"), frame_rate=60.5, simulation_rate=2.0),
            packet(ReceiveEventObject, ReceiveId.EVENT_OBJECT,
                   event_id=self.event_id(b"ObjectRemoved"), data=42, object_type=2),
            packet(ReceiveEvent, ReceiveId.EVENT_WEATHER_MODE,
                   event_id=self.event_id(b"WeatherModeChanged"), data=3),
        ))
        self.core.pump()
        values = {event.capability_id: event.value for event in self.events()}
        self.assertEqual(values, {
            "sim.pause_state": 12, "sim.running_changed": True, "sim.flight_loaded": "test.flt",
            "sim.frame": FrameSample(60.5, 2.0), "sim.object_removed": SimObjectChange(42, 2),
            "sim.weather_mode_changed": 3,
        })

    def test_last_enabled_consumer_controls_shared_native_delivery(self):
        second = self.core.register_consumer()
        a = self.first.subscribe("sim.pause_state")
        b = second.subscribe("sim.pause_state")
        self.core.connect()
        self.core.pump()
        id = self.event_id(b"Pause_EX1")
        self.first.set_event_enabled(a.id, False)
        self.sdk.event(id, 4)
        self.core.pump()
        self.assertFalse(self.events())
        self.assertEqual([item.value for item in self.events(second)], [4])
        self.assertFalse(self.sdk.matching_calls("SimConnect_SetSystemEventState"))
        second.set_event_enabled(b.id, False)
        self.core.pump()
        self.first.set_event_enabled(a.id, True)
        self.core.pump()
        toggles = self.sdk.matching_calls("SimConnect_SetSystemEventState")
        self.assertEqual([args for args, _ in toggles], [(id, 0), (id, 1)])
        self.first.close()
        self.core.pump()
        second.close()
        self.core.pump()
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_UnsubscribeFromSystemEvent")), 1)

    def test_mission_action_preserves_guid_and_rejects_unterminated_payload(self):
        self.first.subscribe("sim.mission_action")
        self.core.connect()
        self.core.pump()
        guid = UUID("00112233-4455-6677-8899-aabbccddeeff")
        message = ReceiveCustomActionPrefix()
        message.id = ReceiveId.CUSTOM_ACTION
        message.event_id = self.event_id(b"CustomMissionActionExecuted")
        message.instance_id[:] = guid.bytes_le
        message.wait_for_completion = 1
        for payload in (b"action\0", b"broken"):
            message.size = len(bytes(message)) + len(payload)
            self.sdk.incoming.append(bytes(message) + payload)
        self.core.pump()
        updates = self.first.poll()
        self.assertEqual([item.value for item in updates if isinstance(item, Event)],
                         [MissionAction(guid, True, "action")])
        self.assertTrue(any(isinstance(item, SignalError) and "Missing terminator" in item.reason for item in updates))

    def test_public_load_event_shares_aircraft_input_lifecycle_watch(self):
        subscription = self.first.subscribe("sim.aircraft_loaded")
        self.core.connect()
        self.first.request_aircraft_inputs()
        self.core.pump()
        calls = self.sdk.matching_calls("SimConnect_SubscribeToSystemEvent")
        self.assertEqual(sorted(args[1] for args, _ in calls), [b"AircraftLoaded", b"FlightLoaded"])
        id = self.event_id(b"AircraftLoaded")
        self.sdk.incoming.append(packet(
            ReceiveEventFilename, ReceiveId.EVENT_FILENAME, event_id=id, filename=b"new.air",
        ))
        self.core.pump()
        self.assertEqual([item.value for item in self.events()], ["new.air"])
        self.assertIs(self.first.aircraft_inputs().state, InputCatalogState.UNAVAILABLE)
        self.first.set_event_enabled(subscription.id, False)
        self.core.pump()
        self.first.unsubscribe(subscription.id)
        self.core.pump()
        self.assertFalse(self.sdk.matching_calls("SimConnect_SetSystemEventState"))
        self.assertFalse(self.sdk.matching_calls("SimConnect_UnsubscribeFromSystemEvent"))

    def test_bad_event_type_and_sdk_registration_failure_report_gaps(self):
        a = self.first.subscribe("sim.flight_loaded")
        self.first.subscribe("sim.frame")
        self.core.connect()
        self.core.pump()
        flight = self.event_id(b"FlightLoaded")
        self.sdk.event(flight, 0)
        _, frame_send = next((args, send) for args, send in self.sdk.matching_calls(
            "SimConnect_SubscribeToSystemEvent") if args[1] == b"Frame")
        self.sdk.exception(frame_send)
        self.core.pump()
        gaps = [item.reason for item in self.first.poll() if isinstance(item, SignalError)]
        self.assertEqual(len(gaps), 2)
        self.assertIn("Unexpected SimConnect packet", gaps[0])
        self.assertIn("exception", gaps[1])
        self.first.unsubscribe(a.id)
        self.core.pump()
        self.assertIs(self.first.connection().state, ConnectionState.CONNECTED)

    def test_pending_state_does_not_populate_new_session(self):
        sub = self.first.subscribe("sim.running")
        before = self.core.connect()
        self.core.pump()
        old_request = self.requests()[-1][0][0]
        self.reply(old_request, integer=1)
        self.core.disconnect()
        self.assertIs(self.first.latest(sub.id).quality, Quality.UNAVAILABLE)
        after = self.core.connect()
        self.assertNotEqual(before.session_id, after.session_id)
        self.core.pump()
        self.assertIs(self.first.latest(sub.id).quality, Quality.UNAVAILABLE)
        self.reply(self.requests()[-1][0][0], integer=0)
        self.core.pump()
        self.assertIs(self.first.latest(sub.id).quality, Quality.CURRENT)
        self.assertIs(self.first.latest(sub.id).value, False)
        self.assertEqual(self.first.latest(sub.id).session_id, after.session_id)
