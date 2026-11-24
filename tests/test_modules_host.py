"""How the module host gets Core handles: CoreHost.register_consumer (contract C4), a Core,
a host with only named consumers, and the CoreBridge adapter. Also the control-group table
against the groups Core accepts, and the guide example's handling of the final update types."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import sys
import tempfile
import textwrap
import time
import types
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from bbc_core import core as core_module
from bbc_core.contract import (
    CommandState, Connection, ConnectionState, Gap, Observation, Quality, SignalError, Source, Subscription,
    SubscriptionEnded,
)
from bbc_core.core import Core
from bbc_core.simulated import SimulatedProvider
from bbc_efb.modules import (
    DEFAULT_CONTROL_GROUPS, ModuleHost, ModuleStatus, as_api_provider, bridge_api_provider, core_api_provider,
    host_api_provider,
)

logging.getLogger("bbc_efb.modules").setLevel(logging.CRITICAL)

COM1 = "radio.com1.active_frequency"
GUIDE = Path(__file__).resolve().parent.parent / "docs" / "MODULE_AUTHORING.md"
SOURCE = Source("Test", simulated=True)
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)

MODULE_CODE = '''
from bbc_core.contract import CoreRequestError, Observation
class Module:
    def __init__(self, context):
        self.api = context.api
        self.sub = self.api.subscribe("radio.com1.active_frequency", interval_ms=1000)
        self.value = None
        self.results = []
    def tick(self):
        for update in self.api.poll(limit=64):
            if isinstance(update, Observation) and update.capability_id == "radio.com1.active_frequency":
                self.value = update.value
    def swap(self):
        self.results.append(self.api.submit_command("radio.com1.swap"))
    def close(self):
        self.api.unsubscribe(self.sub.id)
def create(context):
    return Module(context)
'''


def wait_until(predicate, timeout=5.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


class Recorder:
    """A stand-in consumer handle that records calls."""

    def __init__(self, label):
        self.label = label
        self.closed = 0

    def discover(self):
        return types.SimpleNamespace(capabilities=())

    def poll(self, limit=128):
        return ()

    def close(self):
        self.closed += 1


class RegisteringHost:
    """Shape of CoreHost per C4: register_consumer(**kw)."""

    def __init__(self):
        self.calls = []
        self.handles = []

    def register_consumer(self, **kwargs):
        self.calls.append(kwargs)
        handle = Recorder(kwargs)
        self.handles.append(handle)
        return handle


class NamedOnlyHost:
    """An older host shape: unique names through api(name, ...) and release(name)."""

    def __init__(self):
        self.live = {}
        self.released = []

    def api(self, name, control_groups=(), diagnostics=False, *, queue_size=256):
        if name in self.live:
            raise ValueError(f"A consumer named {name!r} is already registered.")
        self.live[name] = Recorder(name)
        return self.live[name]

    def release(self, name):
        self.released.append(name)
        api = self.live.pop(name, None)
        if api is not None:
            api.close()
        return api is not None


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.path_before = list(sys.path)
        self.addCleanup(lambda: self.assertEqual(sys.path, self.path_before))

    def write_module(self, *, groups=("radios",), code=MODULE_CODE):
        module_id = f"test.{uuid4().hex[:8]}"
        package = f"pkg_{uuid4().hex[:10]}"
        folder = self.data / "modules" / module_id
        (folder / package).mkdir(parents=True)
        (folder / package / "__init__.py").write_text(textwrap.dedent(code), encoding="utf-8")
        (folder / "module.json").write_text(json.dumps({
            "manifest_format": 1, "id": module_id, "name": "Sample", "version": "1.0.0",
            "api_version": ">=0.1,<0.2", "entry": f"{package}:create", "control_groups": list(groups),
        }), encoding="utf-8")
        return module_id

    def start(self, target, *, grant=True, groups=("radios",), code=MODULE_CODE):
        host = ModuleHost(self.data, target)
        self.addCleanup(host.close)
        host.set_experimental_enabled(True)
        module_id = self.write_module(groups=groups, code=code)
        host.scan()
        host.set_enabled(module_id, True)
        if grant:
            host.grant(module_id, list(groups))
        record = host.load(module_id)
        self.assertIs(record.status, ModuleStatus.LOADED, record.problems)
        return host, module_id


class ProviderAdapterTests(Base):
    def test_host_with_register_consumer_gives_each_module_its_own_consumer(self):
        target = RegisteringHost()
        provide = host_api_provider(target)
        first = provide("a", ("radios",))
        second = provide("a", ("radios",))
        self.assertIsNot(first, second)
        self.assertEqual(target.calls, [{"control_groups": ("radios",), "diagnostics": False}] * 2,
                         "modules never get diagnostics and ask with exactly the granted groups")

    def test_named_only_host_gets_unique_names_released_on_close(self):
        target = NamedOnlyHost()
        provide = host_api_provider(target)
        first = provide("mod", ())
        second = provide("mod", ())  # a restart would collide without unique names
        self.assertEqual(len(target.live), 2)
        first.close()
        first.close()
        self.assertEqual(len(target.released), 1, "close releases the name once")
        self.assertEqual(len(target.live), 1)
        self.assertEqual(second.poll(), (), "the wrapper forwards the ConsumerAPI calls")

    def test_bridge_with_host_uses_register_consumer_not_bridge_api(self):
        target = RegisteringHost()

        class Bridge:
            host = target

            def api(self, *args, **kwargs):  # pragma: no cover - must not be used
                raise AssertionError("the bridge would drain the module's updates onto its own channel")

        bridge_api_provider(Bridge())("mod", ("radios",))
        self.assertEqual(len(target.calls), 1)

    def test_bridge_without_host_falls_back_to_unique_released_names(self):
        named = NamedOnlyHost()

        class Bridge:
            host = None
            api = named.api
            release = named.release

        provide = bridge_api_provider(Bridge())
        handle = provide("mod", ())
        provide("mod", ())
        handle.close()
        self.assertEqual(len(named.released), 1)

    def test_as_api_provider_accepts_callables_hosts_and_cores(self):
        def provide(name, groups):
            return Recorder(name)

        self.assertIs(as_api_provider(provide), provide)
        target = RegisteringHost()
        as_api_provider(target)("x", ())
        self.assertEqual(len(target.calls), 1)
        with self.assertRaises(TypeError):
            as_api_provider(object())

    def test_module_handle_closes_its_consumer_on_unload_and_reload(self):
        target = NamedOnlyHost()
        host, module_id = self.start(target, groups=(), code="def create(context):\n    return None\n")
        self.assertEqual(len(target.live), 1)
        host.reload(module_id)
        self.assertEqual(len(target.live), 1, "the old handle was released before the new one was made")
        self.assertEqual(host.record(module_id).status, ModuleStatus.LOADED)
        host.unload(module_id)
        self.assertEqual(target.live, {})


class CoreTargetTests(Base):
    def setUp(self):
        super().setUp()
        self.core = Core(SimulatedProvider())
        self.core.connect()
        self.addCleanup(self.core.disconnect)

    def test_module_host_takes_a_core_directly(self):
        host, module_id = self.start(self.core)
        module = host.instance(module_id)
        for _ in range(3):
            self.core.pump()
            host.tick()
        self.assertIsInstance(module.value, int)
        module.swap()
        self.assertIn(module.results[0].state, (CommandState.ACCEPTED, CommandState.SENT, CommandState.CONFIRMED))
        host.close()
        self.assertEqual(len(self.core._consumers), 0)

    def test_control_groups_match_core(self):
        known = {entry.capability.control_group for entry in core_module.COMMANDS.values()}
        known.update(entry.control_group for entry in getattr(core_module, "_SDK_COMMANDS", {}).values())
        known.update({"aircraft_inputs", "client_data"})
        known.discard(None)
        self.assertEqual(set(DEFAULT_CONTROL_GROUPS), known,
                         "the module grant table must offer exactly the groups Core accepts")
        for group in DEFAULT_CONTROL_GROUPS:
            with self.subTest(group=group):
                core_api_provider(self.core)("probe", (group,)).close()


class CoreHostTests(Base):
    """The real CoreHost (C4): each module gets its own consumer from register_consumer."""

    def setUp(self):
        super().setUp()
        from bbc_core.host import CoreHost, HostConfig

        self.core_host = CoreHost(SimulatedProvider(), config=HostConfig(
            pump_interval_s=0.01, idle_tick_s=0.05, backoff_s=(0.05, 0.1, 0.2), join_timeout_s=2.0))
        self.core_host.start()
        self.addCleanup(self.core_host.stop)
        self.assertTrue(wait_until(lambda: self.core_host.status().connected), "the simulated host did not connect")

    def consumers(self):
        result = self.core_host.call(lambda core: len(core._consumers))
        return result.result(5) if hasattr(result, "result") else result  # C4: call() may return a Future

    def test_module_reads_and_writes_through_core_host(self):
        baseline = self.consumers()
        host, module_id = self.start(self.core_host)
        module = host.instance(module_id)
        self.assertEqual(self.consumers(), baseline + 1)

        def read():
            host.tick()
            return isinstance(module.value, int)

        self.assertTrue(wait_until(read), "the module did not receive COM1 through its own handle")
        module.swap()
        self.assertIn(module.results[0].state, (CommandState.ACCEPTED, CommandState.SENT, CommandState.CONFIRMED))
        host.reload(module_id)
        self.assertEqual(self.consumers(), baseline + 1, "a restart replaces the handle instead of adding one")
        host.close()
        self.assertEqual(self.consumers(), baseline)


class GuideUpdateHandlingTests(unittest.TestCase):
    """The guide's example reacts correctly to SignalError, SubscriptionEnded and Gap."""

    def load_guide_module(self):
        text = GUIDE.read_text(encoding="utf-8")
        code = re.search(r"```python\n(# com1_helper/main.py\n.*?)```", text, re.S).group(1)
        namespace: dict = {}
        exec(compile(code, "com1_helper/main.py", "exec"), namespace)  # noqa: S102 - our own docs
        return namespace

    def make(self, updates, latest=None):
        subscriptions = []

        class Api:
            def subscribe(self, capability_id, *, interval_ms=None, max_age_ms=None):
                subscriptions.append(capability_id)
                return Subscription(f"sub-{len(subscriptions)}", None, interval_ms)

            def poll(self, *, limit=128):
                batch = tuple(updates)
                updates.clear()
                return batch

            def latest(self, subscription_id):
                return latest

        context = types.SimpleNamespace(api=Api(), log=logging.getLogger("test"), granted_groups=frozenset())
        module = self.load_guide_module()["create"](context)
        return module, subscriptions

    def observation(self, value, sub="sub-1"):
        return Observation(COM1, value, "Hz", Quality.CURRENT, SOURCE, "s", NOW)

    def test_signal_error_makes_the_value_unknown_and_is_not_a_gap(self):
        updates = [self.observation(118_000_000)]
        module, _ = self.make(updates)
        module.tick()
        self.assertEqual(module.active, 118_000_000)
        error = SignalError(1, "SimConnect exception", COM1, "sub-1")
        self.assertNotIsInstance(error, Gap)
        updates.append(error)
        module.tick()
        self.assertIsNone(module.active)

    def test_subscription_ended_resubscribes(self):
        updates = [self.observation(121_500_000), SubscriptionEnded("sub-1", COM1, "inputs changed")]
        module, subscriptions = self.make(updates)
        module.tick()
        self.assertIsNone(module.active)
        self.assertEqual(subscriptions, [COM1, COM1])
        self.assertEqual(module.subscription.id, "sub-2")

    def test_gap_reads_latest_and_disconnect_clears(self):
        latest = self.observation(122_800_000)
        updates = [Gap(3, "Consumer update queue lost data.")]
        module, _ = self.make(updates, latest=latest)
        module.tick()
        self.assertEqual(module.active, 122_800_000)
        updates.append(Connection(ConnectionState.DISCONNECTED, SOURCE, None, NOW))
        module.tick()
        self.assertIsNone(module.active)


if __name__ == "__main__":
    unittest.main()
