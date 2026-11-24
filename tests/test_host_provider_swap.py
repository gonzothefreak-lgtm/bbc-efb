"""CoreHost: replacing the provider factory while disconnected (a newly chosen SimConnect.dll)."""

from __future__ import annotations

import unittest

from bbc_core.host import CoreHost, HostConfig, HostState
from bbc_core.provider import AircraftInputProvider, ProviderError
from bbc_core.simulated import SimulatedProvider

from test_host_core import FAST, ThreadRecordingProvider, wait_until


class TaggedProvider(ThreadRecordingProvider):
    def __init__(self, tag, **kwargs):
        super().__init__(minimum_interval_ms=100, **kwargs)
        self.tag = tag
        self.connects = 0

    def connect(self, session_id):
        self.connects += 1
        return super().connect(session_id)


class ProviderSwapTests(unittest.TestCase):
    def make(self, first=None, **config):
        first = first if first is not None else TaggedProvider("first")
        options = dict(FAST)
        options.update(config)
        host = CoreHost(config=HostConfig(**options), provider_factory=lambda: first)
        self.addCleanup(host.stop)
        return host, first

    def test_factory_builds_the_first_provider_and_core_sees_its_interfaces(self):
        host, first = self.make()
        self.assertIs(host.provider, first)
        self.assertTrue(host.provider_replaceable)
        # Core decides on optional interfaces with isinstance; the stand-in must pass the same checks.
        self.assertEqual(isinstance(host._core._provider, AircraftInputProvider),
                         isinstance(first, AircraftInputProvider))

    def test_plain_host_refuses_replacement(self):
        host = CoreHost(SimulatedProvider(), config=HostConfig(**FAST))
        self.addCleanup(host.stop)
        self.assertFalse(host.provider_replaceable)
        with self.assertRaises(RuntimeError):
            host.replace_provider_factory(lambda: SimulatedProvider())

    def test_connected_session_is_untouched_and_replacement_applies_on_next_connect(self):
        host, first = self.make()
        second = TaggedProvider("second")
        api = host.api("display")
        api.subscribe("aircraft.ground_speed", interval_ms=100)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        host.replace_provider_factory(lambda: second, description="SimConnect.dll at D:\\New\\SimConnect.dll")
        self.assertEqual(host.diagnostics()["pending_provider"], "SimConnect.dll at D:\\New\\SimConnect.dll")
        # Still the first provider while its session lasts.
        self.assertIs(host.provider, first)
        self.assertEqual(second.connects, 0)
        host.disconnect().result(2)
        host.connect_now().result(2)
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED and second.connects == 1))
        self.assertIs(host.provider, second)
        self.assertIsNone(host.pending_provider)
        self.assertEqual(first.connects, 1)
        # The existing consumer keeps its subscription on the new provider.
        self.assertTrue(wait_until(lambda: bool(second.threads.get("replace_demand"))))
        self.assertTrue(second.all_threads() <= {host.worker_thread.ident})

    def test_failing_factory_is_an_honest_failed_attempt_and_keeps_retrying(self):
        host, first = self.make(auto_connect=False)
        calls = []

        def broken():
            calls.append(1)
            raise ProviderError("Supply an absolute path to the local SimConnect.dll.")

        host.replace_provider_factory(broken, description="SimConnect.dll at D:\\Bad\\SimConnect.dll")
        host.start()
        host.connect_now().result(2)
        self.assertTrue(wait_until(lambda: len(calls) >= 2))
        status = host.status()
        self.assertIn("SimConnect.dll at D:\\Bad\\SimConnect.dll could not be used", host.status().last_error)
        self.assertEqual(first.connects, 0)  # the old provider is not quietly used instead
        self.assertIs(host.provider, first)
        self.assertIn(status.state, (HostState.WAITING, HostState.CONNECTING))
        # Withdrawing it lets the current provider connect again.
        host.replace_provider_factory(None)
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertEqual(first.connects, 1)

    def test_incompatible_replacement_is_refused(self):
        host, first = self.make(auto_connect=False)

        class Bare:
            pass

        host.replace_provider_factory(Bare, description="a bare object")
        host.start()
        host.connect_now().result(2)
        self.assertTrue(wait_until(lambda: "lacks" in host.status().last_error))
        self.assertIs(host.provider, first)

    def test_before_connect_hook_runs_on_the_core_thread_and_can_hand_over_a_factory(self):
        host, first = self.make(auto_connect=False)
        second = TaggedProvider("second")
        seen = []

        def hook():
            seen.append(host.on_core_thread())
            if host.provider is first and host.pending_provider is None:
                host.replace_provider_factory(lambda: second, description="the chosen file")

        host.set_before_connect(hook)
        host.start()
        host.connect_now().result(2)
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertEqual(seen[:1], [True])
        self.assertIs(host.provider, second)
        self.assertEqual(first.connects, 0)

    def test_hook_error_does_not_block_connecting(self):
        host, first = self.make()

        def hook():
            raise RuntimeError("preferences unavailable")

        host.set_before_connect(hook)
        with self.assertLogs("bbc_core.host", level="ERROR"):
            host.start()
            self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertEqual(first.connects, 1)

    def test_simconnect_provider_can_be_replaced_by_another_simconnect_provider(self):
        from bbc_core.simconnect import SimConnectProvider
        from test_simconnect_provider_offline import FakeSdk

        first, second = SimConnectProvider(FakeSdk()), SimConnectProvider(FakeSdk())
        host = CoreHost(config=HostConfig(**FAST, auto_connect=False), provider_factory=lambda: first)
        self.addCleanup(host.stop)
        host.replace_provider_factory(lambda: second, description="SimConnect.dll at D:\\New\\SimConnect.dll")
        host.start()
        host.connect_now().result(2)
        self.assertTrue(wait_until(lambda: host.provider is second))
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED), host.status())
        self.assertNotIn("lacks", host.status().last_error)

    def test_ready_core_cannot_be_combined_with_a_factory(self):
        from bbc_core import Core

        provider = SimulatedProvider()
        with self.assertRaises(ValueError):
            CoreHost(provider, core=Core(provider), provider_factory=lambda: provider)


if __name__ == "__main__":
    unittest.main()
