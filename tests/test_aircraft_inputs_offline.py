"""Guarded aircraft Input Event tests against a fake, packet-level SDK."""

from ctypes import string_at
from struct import pack
from unittest import TestCase

from bbc_core import CommandState, ConnectionState, Core, CoreRequestError, InputCatalogState, Quality, ValueType
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import (
    DataType, InputEventDescriptor, Receive, ReceiveEventFilename, ReceiveGetInputPrefix, ReceiveId,
    ReceiveInputParameters, ReceiveListPrefix, ReceiveSubscribeInputPrefix,
)
from test_adapter_support import FakeClock
from test_simconnect_provider_offline import FakeSdk, packet


INPUT_HASH = 0xFEDCBA9876543210
TEXT_HASH = 0xABCDEFABCDEF1234


class FakeInputSdk(FakeSdk):
    def page(self, request, number, total, *descriptors):
        payload = b"".join(bytes(InputEventDescriptor(*values)) for values in descriptors)
        self.incoming.append(packet(
            ReceiveListPrefix, ReceiveId.ENUMERATE_INPUT_EVENTS, payload, request_id=request,
            array_size=len(descriptors), entry_number=number, out_of=total,
        ))

    def parameters(self, input_hash, types):
        self.incoming.append(packet(
            ReceiveInputParameters, ReceiveId.ENUMERATE_INPUT_EVENT_PARAMS,
            hash=input_hash, value=types,
        ))

    def input_value(self, request, value, *, type_id=0):
        self.incoming.append(packet(
            ReceiveGetInputPrefix, ReceiveId.GET_INPUT_EVENT, value,
            request_id=request, type=type_id,
        ))

    def changed_value(self, input_hash, value, *, type_id=0):
        self.incoming.append(packet(
            ReceiveSubscribeInputPrefix, ReceiveId.SUBSCRIBE_INPUT_EVENT, value,
            hash=input_hash, type=type_id,
        ))

    def aircraft_changed(self):
        event_id = next(args[0] for args, _ in self.matching_calls("SimConnect_SubscribeToSystemEvent")
                        if args[1] == b"AircraftLoaded")
        self.incoming.append(packet(
            ReceiveEventFilename, ReceiveId.EVENT_FILENAME,
            event_id=event_id, filename=b"fake-aircraft",
        ))


class AircraftInputOfflineTests(FakeClock, TestCase):
    def setUp(self):
        self.start_clock(100.0)  # Every bbc_core clock and simconnect.sleep (A139).
        self.sdk = FakeInputSdk()
        self.core = Core(SimConnectProvider(self.sdk))
        self.inputs = self.core.register_consumer(control_groups=("aircraft_inputs",))
        self.core.connect()
        self.addCleanup(self.core.disconnect)

    def enumerate(self):
        self.inputs.request_aircraft_inputs()
        self.core.pump()
        return self.sdk.matching_calls("SimConnect_EnumerateInputEvents")[-1][0][0]

    def discover(self):
        request = self.enumerate()
        self.sdk.page(request, 0, 1, (b"TestControl", INPUT_HASH, DataType.FLOAT64))
        self.core.pump()
        return self.inputs.aircraft_inputs().inputs[0].id

    def test_discovery_is_opt_in_and_assembles_multipart_pages(self):
        self.core.pump()
        self.assertEqual(self.sdk.matching_calls("SimConnect_EnumerateInputEvents"), [])
        request = self.enumerate()
        self.sdk.page(request, 1, 2, (b"TestText", TEXT_HASH, DataType.STRING128))
        self.sdk.page(request, 1, 2, (b"TestText", TEXT_HASH, DataType.STRING128))
        self.core.pump()
        self.assertEqual(self.inputs.aircraft_inputs().state, InputCatalogState.LOADING)
        self.sdk.page(request, 0, 2, (b"TestControl", INPUT_HASH, DataType.FLOAT64))
        self.core.pump()
        catalog = self.inputs.aircraft_inputs()
        self.assertEqual(catalog.state, InputCatalogState.READY)
        self.assertEqual([item.id.hash for item in catalog.inputs], [INPUT_HASH, TEXT_HASH])
        self.assertEqual([item.value_type for item in catalog.inputs], [ValueType.NUMBER, ValueType.TEXT])

    def test_shares_subscriptions_primes_with_get_and_releases_on_last_consumer(self):
        ref = self.discover()
        module = self.core.register_consumer()
        first = module.subscribe_input(ref)
        second = self.inputs.subscribe_input(ref)
        self.core.pump()
        ((args, _),) = self.sdk.matching_calls("SimConnect_GetInputEvent")
        self.assertEqual(args[1], INPUT_HASH)
        self.sdk.changed_value(INPUT_HASH, pack("<d", 30))
        self.core.pump()
        self.assertIsNone(module.latest(first.id).value)
        self.sdk.input_value(args[0], pack("<d", 16))
        self.sdk.changed_value(INPUT_HASH, pack("<d", 20))
        self.core.pump()
        self.assertEqual(module.latest(first.id).value, 20)
        self.assertEqual(self.inputs.latest(second.id).value, 20)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_SubscribeInputEvent")), 1)
        self.inputs.request_input_value(second.id)
        self.core.pump()
        requested = self.sdk.matching_calls("SimConnect_GetInputEvent")[-1][0][0]
        self.assertNotEqual(requested, args[0])
        self.sdk.input_value(requested, pack("<d", 21))
        self.core.pump()
        self.assertEqual(module.latest(first.id).value, 21)
        module.close()
        self.core.pump()
        self.assertEqual(self.sdk.matching_calls("SimConnect_UnsubscribeInputEvent"), [])
        self.inputs.unsubscribe(second.id)
        self.core.pump()
        self.assertEqual(self.sdk.matching_calls("SimConnect_UnsubscribeInputEvent")[-1][0], (INPUT_HASH,))

    def test_reads_fixed_text_and_rejects_a_malformed_value(self):
        request = self.enumerate()
        self.sdk.page(request, 0, 1, (b"TestText", TEXT_HASH, DataType.STRING128))
        self.core.pump()
        ref = self.inputs.aircraft_inputs().inputs[0].id
        subscription = self.inputs.subscribe_input(ref)
        self.core.pump()
        requested = self.sdk.matching_calls("SimConnect_GetInputEvent")[-1][0][0]
        self.sdk.input_value(requested, b"ABC\0", type_id=1)
        self.core.pump()
        self.assertEqual(self.inputs.latest(subscription.id).value, "ABC")
        self.sdk.changed_value(TEXT_HASH, b"missing terminator", type_id=1)
        self.core.pump()
        self.assertIn("Malformed", self.inputs.latest(subscription.id).reason)

    def test_write_uses_current_schema_and_the_full_64_bit_hash(self):
        ref = self.discover()
        outsider = self.core.register_consumer()
        self.assertEqual(outsider.submit_input(ref, (3.5,)).state, CommandState.REJECTED)
        self.assertEqual(self.inputs.submit_input(ref, (3.5,)).state, CommandState.REJECTED)
        self.inputs.request_input_parameters(ref)
        self.core.pump()
        self.sdk.parameters(INPUT_HASH, b";FLOAT64;char[256]")
        self.core.pump()
        self.assertEqual(self.inputs.input_parameters(ref).types, ("FLOAT64", "char[256]"))
        for invalid in ((True, "COM"), (float("nan"), "COM"), (1, "AB\0CD"), (1,), (2, "é")):
            self.assertEqual(self.inputs.submit_input(ref, invalid).state, CommandState.REJECTED)
        command = self.inputs.submit_input(ref, (3.5, "COM"))
        self.core.pump()
        ((arguments, _),) = self.sdk.matching_calls("SimConnect_SetInputEvent")
        self.assertEqual(arguments[:2], (INPUT_HASH, 264))
        self.assertEqual(string_at(arguments[2], arguments[1]), pack("<d", 3.5) + b"COM".ljust(256, b"\0"))
        self.assertEqual(self.inputs.command_result(command.id).state, CommandState.SENT)
        self.now += 2
        self.core.pump()
        self.assertEqual(self.inputs.command_result(command.id).state, CommandState.UNVERIFIED)

    def test_async_set_exception_reports_rejected_without_closing_session(self):
        ref = self.discover()
        self.inputs.request_input_parameters(ref)
        self.core.pump()
        self.sdk.parameters(INPUT_HASH, b";FLOAT64")
        self.core.pump()
        command = self.inputs.submit_input(ref, (1.0,))
        self.core.pump()
        _, sent_id = self.sdk.matching_calls("SimConnect_SetInputEvent")[-1]
        self.sdk.exception(sent_id)
        self.core.pump()
        self.assertEqual(self.inputs.command_result(command.id).state, CommandState.REJECTED)
        self.assertEqual(self.inputs.connection().state, ConnectionState.CONNECTED)

    def test_aircraft_reload_invalidates_old_handles_and_unconfirmed_writes(self):
        ref = self.discover()
        subscription = self.inputs.subscribe_input(ref)
        self.inputs.request_input_parameters(ref)
        self.core.pump()
        self.sdk.parameters(INPUT_HASH, b";FLOAT64")
        self.core.pump()
        sent = self.inputs.submit_input(ref, (1.0,))
        queued = self.inputs.submit_input(ref, (2.0,))
        self.core.pump()
        self.sdk.aircraft_changed()
        self.core.pump()
        self.assertEqual(self.inputs.aircraft_inputs().state, InputCatalogState.UNAVAILABLE)
        self.assertEqual(self.inputs.command_result(sent.id).state, CommandState.UNCERTAIN)
        self.assertEqual(self.inputs.command_result(queued.id).state, CommandState.NOT_SENT)
        # A86: the ended subscription reads as UNAVAILABLE with the reason, not as an error.
        ended = self.inputs.latest(subscription.id)
        self.assertIs(ended.quality, Quality.UNAVAILABLE)
        self.assertIsNone(ended.value)
        self.assertIn("aircraft inputs changed", ended.reason)
        with self.assertRaisesRegex(CoreRequestError, "aircraft inputs changed"):
            self.inputs.request_input_value(subscription.id)
        with self.assertRaises(CoreRequestError):
            self.inputs.subscribe_input(ref)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_SetInputEvent")), 1)
        replacement = self.discover()
        self.assertNotEqual(replacement.generation, ref.generation)
        self.assertEqual(replacement.hash, ref.hash)
        self.assertEqual(self.inputs.submit_input(ref, (1.0,)).state, CommandState.REJECTED)

    def test_delayed_hash_only_parameters_cannot_validate_a_replacement_aircraft(self):
        original = self.discover()
        self.inputs.request_input_parameters(original)
        self.core.pump()
        self.sdk.aircraft_changed()
        self.core.pump()
        current = self.discover()
        self.inputs.request_input_parameters(current)
        self.core.pump()
        self.assertIsNone(self.inputs.input_parameters(current).types)
        self.assertEqual(len(self.sdk.matching_calls("SimConnect_EnumerateInputEventParams")), 1)
        self.sdk.parameters(INPUT_HASH, b";FLOAT64")
        self.core.pump()
        self.inputs.request_input_parameters(current)
        self.core.pump()
        self.sdk.parameters(INPUT_HASH, b";char[260]")
        self.core.pump()
        self.assertEqual(self.inputs.input_parameters(current).types, ("char[260]",))

    def test_a_load_packet_behind_the_drain_limit_prevents_a_stale_input_write(self):
        ref = self.discover()
        self.inputs.request_input_parameters(ref)
        self.core.pump()
        self.sdk.parameters(INPUT_HASH, b";FLOAT64")
        self.core.pump()
        command = self.inputs.submit_input(ref, (1.0,))
        self.sdk.incoming.append(packet(Receive, 99))
        self.sdk.aircraft_changed()
        self.core.pump(limit=1)
        self.assertEqual(self.inputs.command_result(command.id).state, CommandState.REJECTED)
        self.assertEqual(self.sdk.matching_calls("SimConnect_SetInputEvent"), [])
        self.core.pump()
        self.assertEqual(self.inputs.aircraft_inputs().state, InputCatalogState.UNAVAILABLE)

    def test_incomplete_catalog_times_out_without_publishing_partial_results(self):
        request = self.enumerate()
        self.sdk.page(request, 0, 2, (b"TestControl", INPUT_HASH, DataType.FLOAT64))
        self.core.pump()
        self.now += 4
        self.core.pump()
        catalog = self.inputs.aircraft_inputs()
        self.assertEqual(catalog.state, InputCatalogState.FAILED)
        self.assertEqual(catalog.inputs, ())
        self.assertEqual(self.inputs.connection().state, ConnectionState.CONNECTED)
