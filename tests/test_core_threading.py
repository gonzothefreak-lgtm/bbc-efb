"""Concurrent consumer calls against a pumping host, and bounds on Core's own work per pump."""

import threading
from random import Random
from time import perf_counter, sleep
from unittest import TestCase
from unittest.mock import patch

from bbc_core import CommandState, Core, CoreRequestError, SimVar, ValueType
from bbc_core import core as core_module
from bbc_core.simulated import SimulatedProvider

from test_core_support import FakeClock

_RESULT_HISTORY = 100
TERMINAL = {CommandState.CONFIRMED, CommandState.REJECTED, CommandState.NOT_SENT,
            CommandState.UNCERTAIN, CommandState.UNVERIFIED}


class ThreadingTests(TestCase):
    def test_consumers_on_other_threads_while_the_host_pumps(self):
        provider = SimulatedProvider(minimum_interval_ms=100, command_delay_s=0.02)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        core.connect()
        stop = threading.Event()
        errors = []
        submitted = []
        lock = threading.Lock()

        def pump():
            try:
                while not stop.is_set():
                    core.pump()
                    sleep(0.002)
            except Exception as error:  # noqa: BLE001 - reported by the assertion below
                errors.append(error)

        def module(seed):
            rng = Random(seed)
            try:
                api = core.register_consumer(control_groups=("radios",), queue_size=64)
                subscriptions = []
                mine = 0  # Core keeps the newest 128 results per module; stay within that.
                deadline = perf_counter() + 1.2
                while perf_counter() < deadline:
                    action = rng.random()
                    if action < 0.25 and len(subscriptions) < 20:
                        subscriptions.append(api.subscribe(rng.choice(
                            ("aircraft.ground_speed", "aircraft.on_ground", "sim.pause_state", "aircraft.title"))))
                    elif action < 0.4 and subscriptions:
                        api.unsubscribe(subscriptions.pop(rng.randrange(len(subscriptions))).id)
                    elif action < 0.55 and mine < _RESULT_HISTORY:
                        mine += 1
                        result = api.submit_command("radio.com1.swap")
                        with lock:
                            submitted.append((api, result.id))
                    elif action < 0.8:
                        api.poll()
                    else:
                        for subscription in subscriptions:
                            if subscription.capability.kind.value == "value":
                                api.latest(subscription.id)
                    sleep(0.0005)
                if seed % 2:
                    api.close()
            except CoreRequestError as error:
                errors.append(error)
            except Exception as error:  # noqa: BLE001 - reported by the assertion below
                errors.append(error)

        pumper = threading.Thread(target=pump, name="core-pump")
        modules = [threading.Thread(target=module, args=(seed,), name=f"module-{seed}") for seed in range(4)]
        before = set(threading.enumerate())
        pumper.start()
        for thread in modules:
            thread.start()
        for thread in modules:
            thread.join(10)
        deadline = perf_counter() + 5
        while True:
            states, missing = [], []
            for api, command_id in submitted:
                try:
                    result = api.command_result(command_id)
                except CoreRequestError:
                    continue  # that module closed its handle
                if result is None:
                    missing.append(command_id)
                else:
                    states.append(result.state)
            if (not missing and all(state in TERMINAL for state in states)) or perf_counter() >= deadline:
                break
            sleep(0.01)
        stop.set()
        pumper.join(10)
        self.assertEqual(errors, [])
        self.assertTrue(submitted)
        # The open modules (even seeds) must still be able to read every result they submitted.
        self.assertTrue(states, "no result was checked")
        self.assertEqual(missing, [], "a command result was lost")
        self.assertTrue(all(state in TERMINAL for state in states), states)
        self.assertEqual(set(threading.enumerate()) - before, set())


class WorkBoundTests(TestCase):
    """Bounds on Core's own work, counted rather than timed so the result never depends on
    the machine. The earlier full scans did work proportional to every subscription."""

    def build(self, consumers=10, per_consumer=100):
        clock = FakeClock()
        provider = SimulatedProvider(clock=clock, periodic=False)
        core = Core(provider)
        self.addCleanup(core.disconnect)
        apis, variables = [], []
        for c in range(consumers):
            api = core.register_consumer(queue_size=4096)
            for s in range(per_consumer):
                variable = SimVar(f"BENCH SIGNAL {c} {s}", "number", ValueType.NUMBER)
                provider.set_value(variable, 0.0)
                api.subscribe_sdk_simvar(variable)
                variables.append(variable)
            apis.append(api)
        core.connect()
        core.pump(limit=4096)
        for api in apis:
            api.poll(limit=4096)
        return clock, provider, core, apis, variables

    def count(self, target, name, *, static=False):
        """Count calls to ``target.name`` for the rest of the test."""
        calls = []
        original = getattr(target, name)

        def counted(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        patcher = patch.object(target, name, staticmethod(counted) if static else counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_idle_pump_does_not_rebuild_demand(self):
        clock, provider, core, apis, _ = self.build()
        keys = self.count(core_module, "_signal_key")
        replaced = self.count(provider, "replace_demand")
        for _ in range(200):
            core.pump()
        self.assertEqual(keys, [])
        self.assertEqual(replaced, [])

    def test_a_one_second_burst_fans_out_by_signal_not_by_every_subscription(self):
        clock, provider, core, apis, variables = self.build()
        visits = self.count(Core, "_due", static=True)
        stale_scans = self.count(Core, "_stale_candidates")
        clock.advance(1)
        for variable in variables:
            provider.publish(variable, 1.0)
        core.pump(limit=4096)
        # One subscriber per signal: one scheduling decision per reading, not readings x subscriptions.
        self.assertEqual(len(visits), len(variables))
        self.assertEqual(sum(len(api.poll(limit=4096)) for api in apis), len(variables))
        self.assertEqual(len(stale_scans), len(apis))  # One heap check per poll, never a full scan.

    def test_a_stale_check_visits_only_due_subscriptions(self):
        clock, provider, core, apis, variables = self.build(consumers=2, per_consumer=100)
        visited = self.count(core_module.heapq, "heappop")
        clock.advance(0.5)  # Nothing is due yet (3 s default limit).
        for api in apis:
            api.poll(limit=4096)
        self.assertEqual(visited, [])

    def test_tearing_down_a_page_of_subscriptions_is_cheap(self):
        clock, provider, core, apis, _ = self.build(per_consumer=128)
        module = core.register_consumer()
        subscriptions = [module.subscribe_sdk_simvar(SimVar(f"PAGE SIGNAL {i}", "number", ValueType.NUMBER))
                         for i in range(128)]
        keys = self.count(core_module, "_signal_key")
        removed = self.count(Core, "_index_remove")
        for subscription in subscriptions:
            module.unsubscribe(subscription.id)
        # Each unsubscribe touches only its own signal, and demand is rebuilt lazily, once.
        self.assertEqual(len(removed), 128)
        self.assertEqual(keys, [])
        core.pump()
        self.assertEqual(len(keys), len(core._applied_demand))
