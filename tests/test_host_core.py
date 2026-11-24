"""CoreHost: worker-thread ownership, supervision, crash isolation and shutdown."""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import Future

from bbc_core import ConnectionState, Core, Observation
from bbc_core.host import (
    DEFAULT_BACKOFF_S, NOT_RUNNING_TEXT, CoreHost, HostConfig, HostState, HostStopped,
    backoff_delay, describe_connect_failure, is_not_running,
)
from bbc_core.provider import ProviderError
from bbc_core.simulated import SimulatedProvider

FAST = dict(pump_interval_s=0.01, idle_tick_s=0.05, backoff_s=(0.05, 0.1, 0.2), join_timeout_s=2.0)


def wait_until(predicate, timeout=3.0, step=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class ThreadRecordingProvider(SimulatedProvider):
    """SimulatedProvider that records which thread runs each provider I/O call."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.threads: dict[str, set[int]] = {}
        self.calls_lock = threading.Lock()

    def _note(self, name):
        with self.calls_lock:
            self.threads.setdefault(name, set()).add(threading.get_ident())

    def connect(self, session_id):
        self._note("connect")
        return super().connect(session_id)

    def drain(self, limit):
        self._note("drain")
        return super().drain(limit)

    def replace_demand(self, demand):
        self._note("replace_demand")
        return super().replace_demand(demand)

    def disconnect(self):
        self._note("disconnect")
        return super().disconnect()

    def all_threads(self):
        with self.calls_lock:
            return set().union(*self.threads.values()) if self.threads else set()


class NotRunningProvider(SimulatedProvider):
    """Fails ``failures`` connects the way SimConnect does when MSFS is not running."""

    def __init__(self, failures, **kwargs):
        super().__init__(**kwargs)
        self.failures = failures
        self.attempts: list[float] = []

    def connect(self, session_id):
        self.attempts.append(time.monotonic())
        if len(self.attempts) <= self.failures:
            raise ProviderError("SimConnect_Open failed with HRESULT 0x80004005.")
        return super().connect(session_id)


class BlockingConnectProvider(SimulatedProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def connect(self, session_id):
        self.entered.set()
        self.release.wait(10)
        return super().connect(session_id)


class SlowDrainProvider(SimulatedProvider):
    def __init__(self, delay, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay

    def drain(self, limit):
        time.sleep(self.delay)
        return super().drain(limit)


class RaisingDrainProvider(SimulatedProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.raise_next = threading.Event()

    def drain(self, limit):
        if self.raise_next.is_set():
            self.raise_next.clear()
            raise RuntimeError("adapter bug")
        return super().drain(limit)


class WaitingProvider(SimulatedProvider):
    """Offers the optional wait()/wake() pair the SimConnect adapter exposes."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.waits: list[float] = []
        self.wakes = 0
        self._event = threading.Event()

    def wait(self, timeout):
        self.waits.append(timeout)
        signalled = self._event.wait(timeout)
        self._event.clear()
        return signalled

    def wake(self):
        self.wakes += 1
        self._event.set()


class InstantWaitProvider(SimulatedProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.waits = 0

    def wait(self, timeout):
        self.waits += 1
        return True  # always "data ready": the host must not spin


class ExplodingCore(Core):
    """A Core whose pump raises outside its own provider handling (a Core bug)."""

    def __init__(self, provider, **kwargs):
        super().__init__(provider, **kwargs)
        self.explode = threading.Event()

    def pump(self, *, limit=256):
        if self.explode.is_set():
            self.explode.clear()
            raise ValueError("core bug")
        return super().pump(limit=limit)


class PureFunctionTests(unittest.TestCase):
    def test_default_backoff_schedule(self):
        self.assertEqual(DEFAULT_BACKOFF_S, (1.0, 2.0, 5.0, 10.0, 30.0))
        delays = [backoff_delay(n) for n in range(1, 8)]
        self.assertEqual(delays, [1, 2, 5, 10, 30, 30, 30])
        self.assertEqual(backoff_delay(0), 0.0)

    def test_failure_wording(self):
        self.assertEqual(describe_connect_failure("SimConnect_Open failed with HRESULT 0x80004005."),
                         NOT_RUNNING_TEXT)
        self.assertEqual(describe_connect_failure("Timed out waiting for SimConnect OPEN."), NOT_RUNNING_TEXT)
        self.assertTrue(is_not_running("MSFS quit before confirming SimConnect OPEN."))
        self.assertEqual(describe_connect_failure("SimConnect.dll is not signed."),
                         "Could not connect to the simulator: SimConnect.dll is not signed.")
        self.assertEqual(describe_connect_failure(""), "Could not connect to the simulator.")
        self.assertFalse(is_not_running("disk full"))

    def test_config_validation(self):
        for bad in (dict(pump_interval_s=0), dict(pump_limit=0), dict(backoff_s=()), dict(backoff_s=(0,)),
                    dict(join_timeout_s=0), dict(min_pump_spacing_s=1.0), dict(max_consecutive_crashes=0),
                    dict(idle_tick_s=0.001), dict(describe_failure=None)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                HostConfig(**bad)
        self.assertEqual(HostConfig(backoff_s=[1, 2]).backoff_s, (1.0, 2.0))


class CoreHostTests(unittest.TestCase):
    def make(self, provider=None, **config):
        provider = provider if provider is not None else ThreadRecordingProvider(minimum_interval_ms=100)
        options = dict(FAST)
        options.update(config)
        core_factory = options.pop("core_factory", Core)
        host = CoreHost(provider, config=HostConfig(**options), core_factory=core_factory)
        self.addCleanup(host.stop)
        return host, provider

    def test_connects_and_pumps_only_on_the_worker_thread(self):
        host, provider = self.make()
        api = host.api("display")
        sub = api.subscribe("aircraft.ground_speed", interval_ms=100)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertIn("simulated", host.status().detail)
        host.run(lambda core: provider.publish("aircraft.ground_speed", 42))
        self.assertTrue(wait_until(lambda: api.latest(sub.id).value == 42.0))
        self.assertTrue(wait_until(lambda: host.stats().pumps > 3))
        worker = host.worker_thread.ident
        self.assertEqual(provider.all_threads(), {worker})
        self.assertNotEqual(worker, threading.main_thread().ident)
        self.assertEqual(host.stats().worker_thread, "bbc-core")
        updates = api.poll()
        self.assertTrue(any(isinstance(item, Observation) and item.value == 42.0 for item in updates))
        self.assertTrue(host.stop())
        self.assertEqual(provider.all_threads(), {worker})  # disconnect ran on the worker too
        self.assertEqual(host.status().state, HostState.STOPPED)
        self.assertEqual(api.connection().state, ConnectionState.DISCONNECTED)

    def test_retries_with_backoff_and_honest_status_while_simulator_is_absent(self):
        provider = NotRunningProvider(4, minimum_interval_ms=100)
        host, _ = self.make(provider)
        seen = []
        host.add_status_listener(seen.append)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED, timeout=5))
        waiting = [status for status in seen if status.state is HostState.WAITING]
        self.assertEqual([status.retry_delay_s for status in waiting], [0.05, 0.1, 0.2, 0.2])
        self.assertTrue(all(status.simulator_unavailable for status in waiting))
        self.assertTrue(all(status.detail.startswith(NOT_RUNNING_TEXT) for status in waiting))
        self.assertEqual(waiting[0].detail, f"{NOT_RUNNING_TEXT} Retrying in 0.05 s.")
        self.assertTrue(all(status.retry_at is not None for status in waiting))
        self.assertIn("0x80004005", waiting[-1].connection.detail)  # raw detail kept
        gaps = [b - a for a, b in zip(provider.attempts, provider.attempts[1:])]
        for gap, expected in zip(gaps, (0.05, 0.1, 0.2, 0.2)):
            self.assertGreaterEqual(gap, expected - 0.01)
        connected = host.status()
        self.assertEqual((connected.failures, connected.retry_at, connected.simulator_unavailable), (0, None, False))
        self.assertEqual(host.stats().connect_attempts, 5)

    def test_reconnects_after_the_session_ends(self):
        host, provider = self.make()
        api = host.api("display")
        sub = api.subscribe("aircraft.ground_speed", interval_ms=100)
        seen = []
        host.add_status_listener(seen.append)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        first = host.status().connection.session_id
        host.run(lambda core: provider.lose_connection())
        self.assertTrue(wait_until(
            lambda: host.status().state is HostState.CONNECTED and host.status().connection.session_id != first))
        ended = [s for s in seen if s.state is HostState.WAITING]
        self.assertTrue(ended)
        self.assertIn("The simulator session ended: Simulated transport loss.", ended[0].detail)
        self.assertEqual(ended[0].retry_delay_s, 0.05)
        # Core re-applies the subscription in the new session.
        host.run(lambda core: provider.publish("aircraft.ground_speed", 7))
        self.assertTrue(wait_until(lambda: api.latest(sub.id).value == 7.0))
        self.assertEqual(host.stats().sessions, 2)

    def test_provider_fault_in_pump_ends_session_and_supervisor_restarts(self):
        provider = RaisingDrainProvider(minimum_interval_ms=100)
        host, _ = self.make(provider)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        first = host.status().connection.session_id
        with self.assertLogs("bbc_core", level="ERROR"):
            provider.raise_next.set()
            self.assertTrue(wait_until(lambda: host.status().connection.session_id not in (None, first)))
        self.assertEqual(host.status().state, HostState.CONNECTED)

    def test_exception_escaping_core_is_isolated_and_restarted(self):
        host, provider = self.make(core_factory=ExplodingCore)
        seen = []
        host.add_status_listener(seen.append)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        first = host.status().connection.session_id
        with self.assertLogs("bbc_core.host", level="ERROR") as logs:
            host.run(lambda core: core.explode.set())
            self.assertTrue(wait_until(lambda: host.stats().crashes == 1))
        self.assertTrue(any("internal error" in line for line in logs.output))
        self.assertTrue(wait_until(
            lambda: host.status().state is HostState.CONNECTED and host.status().connection.session_id != first))
        crash = [s for s in seen if s.state is HostState.WAITING]
        self.assertIn("internal error", crash[0].detail)
        self.assertIn("core bug", crash[0].last_error)
        self.assertEqual(host.status().crashes, 1)
        self.assertTrue(host.running)

    def test_crash_limit_turns_automatic_connection_off(self):
        host, _ = self.make(core_factory=ExplodingCore, max_consecutive_crashes=1)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        with self.assertLogs("bbc_core.host", level="ERROR"):
            host.run(lambda core: core.explode.set())
            self.assertTrue(wait_until(lambda: host.status().state is HostState.IDLE))
        self.assertFalse(host.status().auto_connect)
        self.assertIn("Reconnect", host.status().detail)
        host.connect_now().result(2)
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))

    def test_pump_listener_errors_never_end_the_session(self):
        host, _ = self.make()
        calls = []

        def bad():
            calls.append(threading.get_ident())
            raise RuntimeError("listener bug")

        host.add_pump_listener(bad)
        with self.assertLogs("bbc_core.host", level="ERROR"):
            host.start()
            self.assertTrue(wait_until(lambda: len(calls) >= 3))
        self.assertEqual(host.status().state, HostState.CONNECTED)
        self.assertEqual(set(calls), {host.worker_thread.ident})
        self.assertGreaterEqual(host.stats().listener_errors, 3)
        self.assertEqual(host.stats().sessions, 1)

    def test_run_on_worker_returns_results_and_exceptions(self):
        host, _ = self.make()
        host.start()
        self.assertEqual(host.run(lambda core: threading.get_ident()), host.worker_thread.ident)
        self.assertIsInstance(host.run(lambda core: core), Core)
        with self.assertRaises(KeyError):
            host.run(lambda core: {}["missing"])
        self.assertTrue(host.running)  # a failing call is not a crash
        self.assertEqual(host.stats().crashes, 0)
        # Nested run from the worker runs inline instead of deadlocking.
        self.assertEqual(host.run(lambda core: host.run(lambda inner: 5)), 5)
        self.assertEqual(host.run(lambda: "no core needed"), "no core needed")
        with self.assertRaises(TypeError):
            host.submit("not callable")

    def test_calls_queued_before_start_run_after_start(self):
        host, _ = self.make()
        future = host.submit(lambda core: "ran")
        self.assertFalse(future.done())
        host.start()
        self.assertEqual(future.result(2), "ran")

    def test_call_timeout_cancels(self):
        host, _ = self.make()
        gate = threading.Event()
        host.start()
        blocker = host.submit(lambda core: gate.wait(5))
        with self.assertRaises(TimeoutError):
            host.run(lambda core: "late", timeout=0.05)
        gate.set()
        self.assertTrue(blocker.result(2))

    def test_stop_within_timeout_and_pending_calls_fail(self):
        host, provider = self.make()
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        started = time.monotonic()
        self.assertTrue(host.stop(timeout=1.0))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(host.running)
        self.assertIsNone(provider._session_id)
        with self.assertRaises(HostStopped):
            host.submit(lambda core: None)
        with self.assertRaises(RuntimeError):
            host.start()
        self.assertTrue(host.stop())  # idempotent

    def test_stop_when_never_started(self):
        provider = ThreadRecordingProvider()
        host = CoreHost(provider, config=HostConfig(**FAST))
        pending = host.submit(lambda core: None)
        self.assertTrue(host.stop())
        self.assertIsInstance(pending.exception(1), HostStopped)
        self.assertEqual(host.status().state, HostState.STOPPED)
        self.assertEqual(provider.all_threads(), set())

    def test_blocked_provider_stop_reports_timeout_then_finishes(self):
        provider = BlockingConnectProvider()
        host, _ = self.make(provider)
        host.start()
        self.assertTrue(provider.entered.wait(2))
        pending = host.submit(lambda core: "never")
        started = time.monotonic()
        with self.assertLogs("bbc_core.host", level="WARNING"):
            self.assertFalse(host.stop(timeout=0.2))
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(host.status().state, HostState.STOPPING)
        self.assertIn("did not stop within", host.status().detail)
        provider.release.set()
        self.assertTrue(host.wait_stopped(3))
        self.assertIsInstance(pending.exception(1), HostStopped)
        self.assertIsNone(provider._session_id)  # the session opened late is closed on the way out

    def test_slow_provider_does_not_block_consumer_calls_from_other_threads(self):
        provider = SlowDrainProvider(0.12, minimum_interval_ms=100)
        host, _ = self.make(provider, slow_pump_warning_s=0.1)
        api = host.api("ui")
        api.subscribe("aircraft.ground_speed", interval_ms=100)
        with self.assertLogs("bbc_core.host", level="WARNING"):
            host.start()
            self.assertTrue(wait_until(lambda: host.stats().slow_pumps >= 2))
        worst = 0.0
        for _ in range(30):
            started = time.perf_counter()
            api.poll(limit=16)
            api.connection()
            worst = max(worst, time.perf_counter() - started)
            time.sleep(0.005)
        self.assertLess(worst, 0.05)  # the UI-side calls never wait for the 120 ms drain
        self.assertGreaterEqual(host.stats().max_pump_s, 0.1)

    def test_uses_provider_wait_and_wake(self):
        provider = WaitingProvider(minimum_interval_ms=100)
        host, _ = self.make(provider, pump_interval_s=0.2, idle_tick_s=0.5)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertTrue(wait_until(lambda: len(provider.waits) >= 2))
        self.assertTrue(all(0 <= timeout <= 0.2 + 1e-6 for timeout in provider.waits))
        # A queued call wakes the provider wait instead of waiting for the 200 ms tick.
        time.sleep(0.05)
        started = time.monotonic()
        host.run(lambda core: None)
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertGreaterEqual(provider.wakes, 1)

    def test_instant_provider_wait_does_not_spin(self):
        provider = InstantWaitProvider(minimum_interval_ms=100)
        host, _ = self.make(provider, pump_interval_s=0.02, min_pump_spacing_s=0.02)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        before = host.stats().pumps
        time.sleep(0.3)
        pumps = host.stats().pumps - before
        self.assertLessEqual(pumps, 20)  # bounded by min spacing, not thousands
        self.assertGreater(pumps, 3)

    def test_fixed_cadence_without_provider_wait(self):
        host, _ = self.make(pump_interval_s=0.05)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        before = host.stats().pumps
        time.sleep(0.5)
        pumps = host.stats().pumps - before
        self.assertTrue(4 <= pumps <= 12, pumps)

    def test_disconnect_and_connect_now(self):
        host, provider = self.make()
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        host.disconnect().result(2)
        self.assertTrue(wait_until(lambda: host.status().state is HostState.IDLE))
        time.sleep(0.15)
        self.assertEqual(host.status().state, HostState.IDLE)  # no automatic reconnect
        self.assertIsNone(provider._session_id)
        host.connect_now().result(2)
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        host.set_auto_connect(False).result(2)
        self.assertTrue(wait_until(lambda: not host.status().auto_connect or host.status().state is HostState.CONNECTED))
        host.disconnect().result(2)
        self.assertTrue(wait_until(lambda: host.status().state is HostState.IDLE))
        self.assertFalse(host.status().auto_connect)

    def test_auto_connect_off_at_start_stays_idle(self):
        host, provider = self.make(auto_connect=False)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.IDLE))
        self.assertEqual(provider.all_threads(), set())
        self.assertIn("Automatic connection is off", host.status().detail)

    def test_ui_active_hook(self):
        host, _ = self.make()
        seen, statuses = [], []
        host.add_ui_listener(seen.append)
        remove = host.add_status_listener(statuses.append)
        self.assertTrue(host.ui_active)
        host.set_ui_active(False)
        host.set_ui_active(False)
        self.assertEqual(seen, [False])
        self.assertFalse(host.status().ui_active)
        self.assertFalse(statuses[-1].ui_active)
        remove()
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertFalse(host.status().ui_active)  # kept across later status changes
        self.assertTrue(host.stats().pumps > 0)    # Core keeps pumping while minimised
        host.set_ui_active(True)
        self.assertEqual(seen, [False, True])

    def test_named_consumers(self):
        host, _ = self.make()
        api = host.api("radios", control_groups=("radios",))
        self.assertIs(host.consumer("radios"), api)
        with self.assertRaises(ValueError):
            host.api("radios")
        with self.assertRaises(ValueError):
            host.api("  ")
        self.assertEqual(host.consumer_names(), ("radios",))
        self.assertTrue(host.release("radios"))
        self.assertFalse(host.release("radios"))
        self.assertIsNone(host.consumer("radios"))

    def test_context_manager(self):
        provider = ThreadRecordingProvider()
        with CoreHost(provider, config=HostConfig(**FAST)) as host:
            self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertFalse(host.running)
        self.assertEqual(host.status().state, HostState.STOPPED)

    def test_stop_from_worker_is_refused(self):
        host, _ = self.make()
        host.start()
        future: Future = host.submit(lambda core: host.stop())
        self.assertIsInstance(future.exception(2), RuntimeError)



class FakeSdk:
    """Stands in for SimConnectSdk: wait() blocks on an event that wake() signals."""

    def __init__(self):
        self.event = threading.Event()
        self.wakes = 0
        self.waits = 0

    def wait(self, timeout):
        self.waits += 1
        signalled = self.event.wait(timeout)
        self.event.clear()
        return signalled

    def wake(self):
        self.wakes += 1
        self.event.set()


class SdkWaitProvider(SimulatedProvider):
    """Like SimConnectProvider today: wait() delegates to its SDK and there is no wake()."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._sdk = FakeSdk()

    def wait(self, timeout):
        return self._sdk.wait(timeout)


class ContractC4Tests(unittest.TestCase):
    """Shared contract C4: add_tick, call -> Future, register_consumer, connection, stop."""

    def make(self, provider=None, **config):
        provider = provider if provider is not None else ThreadRecordingProvider(minimum_interval_ms=100)
        options = dict(FAST)
        options.update(config)
        host = CoreHost(provider, config=HostConfig(**options))
        self.addCleanup(host.stop)
        return host, provider

    def test_tick_runs_on_core_thread_after_pumps_with_or_without_core_argument(self):
        host, _ = self.make()
        plain, with_core = [], []
        remove = host.add_tick(lambda: plain.append(threading.get_ident()))
        host.add_tick(lambda core: with_core.append(core))
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertTrue(wait_until(lambda: len(plain) >= 5 and len(with_core) >= 5))
        self.assertEqual(set(plain), {host.worker_thread.ident})
        self.assertTrue(all(isinstance(core, Core) for core in with_core))
        remove()
        host.run(lambda core: None)  # one full pass after removal
        count = len(plain)
        time.sleep(0.1)
        self.assertEqual(len(plain), count)
        self.assertGreater(host.stats().ticks, 0)

    def test_bound_method_tick_without_arguments(self):
        class Service:
            def __init__(self):
                self.ticks = 0

            def tick(self, now=None):  # a default must not receive the Core
                assert now is None
                self.ticks += 1

        host, _ = self.make()
        service = Service()
        host.add_pump_listener(service.tick)  # older name, same behaviour
        host.start()
        self.assertTrue(wait_until(lambda: service.ticks >= 3))
        self.assertEqual(host.stats().tick_errors, 0)
        self.assertTrue(host.remove_tick(service.tick))
        self.assertFalse(host.remove_tick(service.tick))

    def test_tick_that_raises_is_isolated_and_logged_rate_limited(self):
        host, _ = self.make()
        good = []

        def bad():
            raise RuntimeError("recorder bug")

        host.add_tick(bad)
        host.add_tick(lambda: good.append(1))
        with self.assertLogs("bbc_core.host", level="DEBUG") as logs:
            host.start()
            self.assertTrue(wait_until(lambda: host.stats().tick_errors >= 5 and len(good) >= 5))
        errors = [record for record in logs.records if record.levelname == "ERROR"]
        self.assertEqual(len(errors), 1)  # first failure with traceback, then rate-limited
        self.assertIsNotNone(errors[0].exc_info)
        self.assertIn("stays registered", errors[0].getMessage())
        self.assertEqual(host.status().state, HostState.CONNECTED)
        self.assertEqual(host.stats().sessions, 1)
        self.assertEqual(host.stats().crashes, 0)
        diagnostics = host.diagnostics()
        failing = [entry for entry in diagnostics["tick_callbacks"] if entry["errors"]]
        self.assertEqual(len(failing), 1)
        self.assertIn("recorder bug", failing[0]["last_error"])

    def test_slow_tick_is_counted_warned_and_does_not_burst(self):
        host, _ = self.make(pump_interval_s=0.01, slow_tick_warning_s=0.05)
        host.add_tick(lambda: time.sleep(0.08))
        with self.assertLogs("bbc_core.host", level="WARNING") as logs:
            host.start()
            self.assertTrue(wait_until(lambda: host.stats().slow_ticks >= 3, timeout=5))
        warnings = [line for line in logs.output if "Tick callback" in line]
        self.assertEqual(len(warnings), 1)  # rate-limited
        self.assertGreaterEqual(host.stats().max_tick_s, 0.08)
        # The Core thread still serves calls between slow ticks.
        started = time.monotonic()
        self.assertEqual(host.call(lambda core: "served").result(2), "served")
        self.assertLess(time.monotonic() - started, 0.5)
        before = host.stats().pumps
        time.sleep(0.4)
        self.assertLessEqual(host.stats().pumps - before, 7)  # about one pump per 80 ms tick, no catch-up

    def test_ticks_keep_running_while_disconnected(self):
        provider = NotRunningProvider(10_000, minimum_interval_ms=100)
        host, _ = self.make(provider, idle_tick_s=0.05, backoff_s=(5.0,))
        ticks = []
        host.add_tick(lambda: ticks.append(time.monotonic()))
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.WAITING))
        self.assertTrue(wait_until(lambda: len(ticks) >= 4, timeout=2))
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertTrue(all(gap >= 0.03 for gap in gaps), gaps)  # idle cadence, not a spin
        self.assertEqual(host.stats().pumps, 0)

    def test_call_from_another_thread_returns_a_future(self):
        host, _ = self.make()
        host.start()
        results = {}

        def worker():
            future = host.call(lambda core: threading.get_ident())
            results["future"] = future
            results["value"] = future.result(2)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(3)
        self.assertIsInstance(results["future"], Future)
        self.assertEqual(results["value"], host.worker_thread.ident)
        self.assertNotEqual(results["value"], thread.ident)
        failing = host.call(lambda core: 1 / 0)
        self.assertIsInstance(failing.exception(2), ZeroDivisionError)
        self.assertEqual(host.call(lambda: "no-arg").result(2), "no-arg")
        with self.assertRaises(TypeError):
            host.call("not callable")

    def test_call_does_not_block_the_caller(self):
        host, _ = self.make()
        gate = threading.Event()
        host.start()
        started = time.monotonic()
        future = host.call(lambda core: gate.wait(2))
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertFalse(future.done())
        gate.set()
        self.assertTrue(future.result(2))

    def test_call_on_core_thread_returns_finished_future(self):
        host, _ = self.make()
        host.start()
        inner = host.run(lambda core: host.call(lambda inner_core: 7))
        self.assertTrue(inner.done())
        self.assertEqual(inner.result(0), 7)
        failed = host.run(lambda core: host.call(lambda inner_core: {}["x"]))
        self.assertIsInstance(failed.exception(0), KeyError)

    def test_call_after_stop_returns_failed_future(self):
        host, _ = self.make()
        host.start()
        self.assertTrue(host.stop(1.0))
        future = host.call(lambda core: None)
        self.assertTrue(future.done())
        self.assertIsInstance(future.exception(0), HostStopped)

    def test_register_consumer_and_connection(self):
        host, provider = self.make()
        self.assertEqual(host.connection().state, ConnectionState.DISCONNECTED)
        anonymous = host.register_consumer(queue_size=32)
        named = host.register_consumer(name="radios", control_groups=("radios",))
        self.assertIs(host.consumer("radios"), named)
        self.assertEqual(host.consumer_names(), ("radios",))
        sub = anonymous.subscribe("aircraft.ground_speed", interval_ms=100)
        host.start()
        self.assertTrue(wait_until(lambda: host.connection().state is ConnectionState.CONNECTED))
        self.assertEqual(host.connection().session_id, host.status().connection.session_id)
        host.run(lambda core: provider.publish("aircraft.ground_speed", 12))
        self.assertTrue(wait_until(lambda: anonymous.latest(sub.id).value == 12.0))
        host.stop(1.0)
        self.assertEqual(host.connection().state, ConnectionState.DISCONNECTED)

    def test_stop_joins_within_timeout_and_ends_the_session(self):
        provider = WaitingProvider(minimum_interval_ms=100)
        host, _ = self.make(provider, pump_interval_s=1.0, idle_tick_s=2.0)
        host.add_tick(lambda: None)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        time.sleep(0.05)  # worker is now blocked in provider.wait(~1 s)
        started = time.monotonic()
        self.assertTrue(host.stop(timeout=0.5))
        self.assertLess(time.monotonic() - started, 0.5)  # wake() cut the 1 s wait short
        self.assertIsNone(provider._session_id)
        self.assertEqual(host.status().state, HostState.STOPPED)

    def test_stop_with_blocked_tick_reports_and_final_status_is_stopped(self):
        host, _ = self.make(slow_tick_warning_s=10)
        gate, entered = threading.Event(), threading.Event()

        def stuck():
            entered.set()
            gate.wait(5)

        host.add_tick(stuck)
        host.start()
        self.assertTrue(entered.wait(2))
        with self.assertLogs("bbc_core.host", level="WARNING"):
            self.assertFalse(host.stop(timeout=0.1))
        self.assertIn("tick callback", host.status().detail)
        gate.set()
        self.assertTrue(host.wait_stopped(2))
        self.assertEqual(host.status().state, HostState.STOPPED)

    def test_provider_wait_with_sdk_wake_fallback(self):
        provider = SdkWaitProvider(minimum_interval_ms=100)
        host, _ = self.make(provider, pump_interval_s=0.3, idle_tick_s=0.5)
        self.assertTrue(host.diagnostics()["provider_wait"])
        self.assertTrue(host.diagnostics()["provider_wake"])
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertTrue(wait_until(lambda: provider._sdk.waits >= 2))
        time.sleep(0.05)
        started = time.monotonic()
        self.assertIsNone(host.call(lambda core: None).result(2))
        self.assertLess(time.monotonic() - started, 0.2)  # not the 300 ms tick
        self.assertGreaterEqual(provider._sdk.wakes, 1)

    def test_simconnect_provider_wake_is_preferred_over_the_sdk_fallback(self):
        from bbc_core.simconnect import SimConnectProvider
        self.assertTrue(callable(getattr(SimConnectProvider, "wake", None)))
        provider = SdkWaitProvider(minimum_interval_ms=100)
        own = []
        provider.wake = lambda: own.append(1)  # a provider-level wake(), as SimConnectProvider has
        host, _ = self.make(provider, pump_interval_s=0.3, idle_tick_s=0.5)
        self.assertIs(host._find_provider_wake(), provider.wake)
        host.stop()

    def test_provider_wait_used_between_pumps(self):
        provider = WaitingProvider(minimum_interval_ms=100)
        host, _ = self.make(provider, pump_interval_s=0.1, idle_tick_s=0.5)
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertTrue(wait_until(lambda: len(provider.waits) >= 3))
        # Data arriving (the SDK signalling its event) wakes the worker before the tick.
        before = host.stats().pumps
        provider.wake()
        self.assertTrue(wait_until(lambda: host.stats().pumps > before, timeout=0.08))

    def test_no_wake_without_provider_wait(self):
        provider = ThreadRecordingProvider()
        provider._sdk = FakeSdk()  # an SDK wake is only used when the provider waits on it
        host = CoreHost(provider, config=HostConfig(**FAST))
        self.assertFalse(host.diagnostics()["provider_wake"])
        host.stop()

    def test_diagnostics_are_plain_values(self):
        import json

        host, _ = self.make()
        host.api("display")
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        diagnostics = host.diagnostics()
        json.dumps(diagnostics)
        self.assertEqual(diagnostics["state"], "connected")
        self.assertEqual(diagnostics["consumers"], ["display"])
        self.assertTrue(diagnostics["simulated"])
        self.assertTrue(diagnostics["running"])

    def test_core_thread_identity_is_forgotten_after_stop(self):
        host, _ = self.make()
        host.start()
        self.assertTrue(host.run(lambda core: host.on_core_thread()))
        self.assertTrue(host.stop(1.0))
        self.assertIsNone(host._thread_ident)
        self.assertFalse(host.on_core_thread())

    def test_late_status_never_overwrites_final_stopped(self):
        host, _ = self.make()
        host.start()
        self.assertTrue(wait_until(lambda: host.status().state is HostState.CONNECTED))
        self.assertTrue(host.stop(1.0))
        host._set_status(HostState.STOPPING, "late report from stop()")
        self.assertEqual(host.status().state, HostState.STOPPED)

    def test_tick_callback_validation(self):
        host, _ = self.make()
        with self.assertRaises(TypeError):
            host.add_tick("nope")
        with self.assertRaises(TypeError):
            host.add_tick(lambda a, b: None)
        with self.assertRaises(ValueError):
            HostConfig(slow_tick_warning_s=0)


if __name__ == "__main__":
    unittest.main()
