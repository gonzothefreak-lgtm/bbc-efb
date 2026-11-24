"""Check receive-side client event subscriptions against a packet-level fake."""

from struct import pack
from unittest import TestCase

from bbc_core import ClientEventData, CommandState, ConnectionState, Core, CoreRequestError, Event
from bbc_core.contract import SignalError  # C7: provider errors are not Gaps
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import ReceiveEvent, ReceiveEventHeader, ReceiveId
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk, packet


class ClientEventsOfflineTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock()  # Shared clock for every bbc_core module, sleep included (A139).
        self.sdk = FakeSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.addCleanup(self.core.disconnect)
        self.first = self.core.register_consumer()
        self.core.connect()

    def group_calls(self, action):
        return self.sdk.matching_calls(action)

    def ids(self, name):
        event = next(args[0] for args, _ in self.sdk.matching_calls("SimConnect_MapClientEventToSimEvent")
                     if args[1] == name.encode("ascii"))
        group = next(args[0] for args, _ in self.sdk.matching_calls("SimConnect_AddClientEventToNotificationGroup")
                     if args[1] == event)
        return group, event

    def emit(self, group, event, *data):
        if len(data) == 1:
            self.sdk.incoming.append(packet(ReceiveEvent, ReceiveId.EVENT, data=data[0], event_id=event, group_id=group))
        else:
            self.sdk.incoming.append(packet(ReceiveEventHeader, ReceiveId.EVENT_EX1, pack("<5I", *data),
                                            event_id=event, group_id=group))

    @staticmethod
    def events(consumer):
        return [update for update in consumer.poll() if isinstance(update, Event)]

    def test_shares_read_only_subscription_and_preserves_standard_and_extended_payloads(self):
        self.first.subscribe_client_event("landing_lights_set")
        peer = self.core.register_consumer()
        peer.subscribe_client_event("LANDING_LIGHTS_SET")
        self.core.pump()
        group, event = self.ids("LANDING_LIGHTS_SET")
        configured = self.group_calls("SimConnect_AddClientEventToNotificationGroup")
        self.assertEqual([args for args, _ in configured], [(group, event, 0)])
        priority = self.group_calls("SimConnect_SetNotificationGroupPriority")
        self.assertEqual([args for args, _ in priority], [(group, 2_000_000_000)])
        self.emit(group, event, 1)
        self.emit(0xFFFFFFFF, event, 0xFFFFFFFF, 1, 0, 0x80000000, 0)
        self.core.pump()
        expected = [ClientEventData((1,)), ClientEventData((0xFFFFFFFF, 1, 0, 0x80000000, 0))]
        for consumer in (self.first, peer):
            updates = self.events(consumer)
            self.assertEqual([item.value for item in updates], expected)
            self.assertTrue(all(item.capability_id == "sdk.client_event:LANDING_LIGHTS_SET" for item in updates))

    def test_event_and_group_filtering_leave_system_notifications_separate(self):
        self.first.subscribe_client_event("PAUSE_TOGGLE")
        self.first.subscribe("sim.pause_state")
        self.core.pump()
        group, event = self.ids("PAUSE_TOGGLE")
        pause, _ = self.sdk.matching_calls("SimConnect_SubscribeToSystemEvent")[0]
        self.emit(group + 1000, event, 3)
        self.emit(group, event + 1000, 4, 0, 0, 0, 0)
        self.sdk.event(pause[0], 4)
        self.emit(group, event, 0)
        self.core.pump()
        self.assertEqual([(update.capability_id, update.value) for update in self.events(self.first)],
                         [("sim.pause_state", 4), ("sdk.client_event:PAUSE_TOGGLE", ClientEventData((0,)))])

    def test_last_enabled_consumer_controls_delivery_and_group_cleanup(self):
        a = self.first.subscribe_client_event("LANDING_LIGHTS_SET")
        peer = self.core.register_consumer()
        b = peer.subscribe_client_event("LANDING_LIGHTS_SET")
        other = peer.subscribe_client_event("PARKING_BRAKES")
        self.core.pump()
        group, event = self.ids("LANDING_LIGHTS_SET")
        self.first.set_event_enabled(a.id, False)
        self.emit(group, event, 1)
        self.core.pump()
        self.assertFalse(self.events(self.first))
        self.assertEqual(len(self.events(peer)), 1)
        self.assertFalse(self.group_calls("SimConnect_RemoveClientEvent"))
        peer.unsubscribe(b.id)
        self.core.pump()
        self.assertEqual([args for args, _ in self.group_calls("SimConnect_RemoveClientEvent")], [(group, event)])
        self.first.set_event_enabled(a.id, True)
        self.emit(group, event, 9)
        self.core.pump()
        self.assertFalse(self.events(self.first))
        self.emit(group, event, 1)
        self.core.pump()
        self.assertEqual([item.value for item in self.events(self.first)], [ClientEventData((1,))])
        self.first.close()
        peer.unsubscribe(other.id)
        self.core.pump()
        self.assertEqual([args for args, _ in self.group_calls("SimConnect_ClearNotificationGroup")], [(group,)])

    def test_rejected_mapping_and_malformed_packet_report_gaps_only_for_affected_consumers(self):
        self.first.subscribe_client_event("LANDING_LIGHTS_SET")
        peer = self.core.register_consumer()
        peer.subscribe_client_event("PARKING_BRAKES")
        self.core.pump()
        first_id = self.ids("LANDING_LIGHTS_SET")[1]
        sent_map = next(sent for args, sent in self.sdk.matching_calls("SimConnect_MapClientEventToSimEvent")
                        if args[0] == first_id)
        self.sdk.exception(sent_map)
        group, event = self.ids("PARKING_BRAKES")
        self.sdk.incoming.append(packet(ReceiveEventHeader, ReceiveId.EVENT_EX1,
                                        event_id=event, group_id=group))
        self.emit(group, event, 5)
        self.core.pump()
        failed = [item.reason for item in self.first.poll() if isinstance(item, SignalError)]
        self.assertEqual(len(failed), 1)
        self.assertIn("SimConnect exception", failed[0])
        updates = peer.poll()
        self.assertTrue(any(isinstance(item, SignalError) and "packet size" in item.reason for item in updates))
        self.assertEqual([item.value for item in updates if isinstance(item, Event)], [ClientEventData((5,))])
        self.assertIs(self.first.connection().state, ConnectionState.CONNECTED)

    def test_adding_a_group_member_may_fail_without_disabling_other_events(self):
        self.first.subscribe_client_event("LANDING_LIGHTS_SET")
        self.first.subscribe_client_event("PARKING_BRAKES")
        self.core.pump()
        group, failed = self.ids("LANDING_LIGHTS_SET")
        _, good = self.ids("PARKING_BRAKES")
        sent = next(sent for args, sent in self.group_calls("SimConnect_AddClientEventToNotificationGroup")
                    if args[1] == failed)
        self.sdk.exception(sent)
        self.emit(group, failed, 1)
        self.emit(group, good, 2)
        self.core.pump()
        updates = self.first.poll()
        self.assertTrue(any(isinstance(item, SignalError) for item in updates))
        self.assertEqual([item.value for item in updates if isinstance(item, Event)], [ClientEventData((2,))])

    def test_reconnect_remaps_active_names_and_rejects_invalid_inputs(self):
        with self.assertRaises(CoreRequestError):
            self.first.subscribe_client_event("K:PARKING_BRAKES")
        self.first.subscribe_client_event("PARKING_BRAKES")
        self.core.pump()
        one = self.first.connection().session_id
        self.core.disconnect()
        self.core.connect()
        self.core.pump()
        self.assertEqual(len(self.group_calls("SimConnect_AddClientEventToNotificationGroup")), 2)
        group, event, _ = self.group_calls("SimConnect_AddClientEventToNotificationGroup")[-1][0]
        self.emit(group, event, 0)
        self.core.pump()
        update, = self.events(self.first)
        self.assertNotEqual(update.session_id, one)
        self.assertEqual(update.session_id, self.first.connection().session_id)

    def test_send_and_receive_share_mapping_and_notifications_do_not_confirm_commands(self):
        consumer = self.core.register_consumer(control_groups=("client_events",))
        consumer.subscribe_client_event("AP_HEADING_BUG_SET_EX1")
        self.core.pump()
        group, event = self.ids("AP_HEADING_BUG_SET_EX1")
        command = consumer.submit_client_event("AP_HEADING_BUG_SET_EX1", (90, 1))
        self.core.pump()
        ((args, _),) = self.sdk.matching_calls("SimConnect_TransmitClientEvent_EX1")
        self.assertEqual(args[1], event)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_MapClientEventToSimEvent")), 1)
        self.emit(group, event, 90, 1, 0, 0, 0)
        self.core.pump()
        self.assertEqual(self.events(consumer)[0].value.words, (90, 1, 0, 0, 0))
        self.assertIs(consumer.command_result(command.id).state, CommandState.SENT)

    def test_mixed_case_custom_event_maps_one_spelling_for_subscribe_and_transmit(self):
        consumer = self.core.register_consumer(control_groups=("client_events",))
        subscription = consumer.subscribe_client_event("Vendor.MyEvent")
        self.assertEqual(subscription.capability.id, "sdk.client_event:Vendor.MyEvent")
        self.core.pump()
        group, event = self.ids("Vendor.MyEvent")
        self.sdk.apply_write = False  # The fake simulates only the COM radio events.
        command = consumer.submit_client_event("Vendor.MyEvent", (7,))
        self.core.pump()
        names = [args[1] for args, _ in self.sdk.matching_calls("SimConnect_MapClientEventToSimEvent")]
        self.assertEqual(names, [b"Vendor.MyEvent"])
        transmit = self.sdk.matching_calls("SimConnect_TransmitClientEvent_EX1") or self.sdk.matching_calls(
            "SimConnect_TransmitClientEvent")
        self.assertEqual(transmit[0][0][1], event)
        self.emit(group, event, 7)
        self.core.pump()
        self.assertEqual(len(self.events(consumer)), 1)
        self.assertIs(consumer.command_result(command.id).state, CommandState.SENT)

    def test_cleanup_rejection_keeps_session_and_drops_events_from_the_retired_group(self):
        # A refused cleanup is local: the session stays up, the old group is retired
        # so stray events are dropped, and later subscriptions use a fresh group.
        subscription = self.first.subscribe_client_event("LANDING_LIGHTS_SET")
        self.core.pump()
        group, event = self.ids("LANDING_LIGHTS_SET")
        self.first.unsubscribe(subscription.id)
        self.core.pump()
        (_, sent), = self.group_calls("SimConnect_ClearNotificationGroup")
        self.sdk.exception(sent)
        self.core.pump()
        self.assertIs(self.first.connection().state, ConnectionState.CONNECTED)
        self.first.subscribe_client_event("LANDING_LIGHTS_SET")
        self.core.pump()
        self.core.pump()
        new_group = self.group_calls("SimConnect_AddClientEventToNotificationGroup")[-1][0][0]
        self.assertNotEqual(new_group, group)
        self.emit(group, event, 1)
        self.emit(new_group, event, 2)
        self.core.pump()
        self.assertEqual([item.value for item in self.events(self.first)], [ClientEventData((2,))])
