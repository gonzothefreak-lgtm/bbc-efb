"""Scaling guards for Core's receive and pump paths (finding A141), and control-lane overflow (A36).

The guards count work, not time: every Python line executed during the measured pump is
counted with ``sys.settrace``, so the result is the same on every machine and run and does
not depend on load, the garbage collector or timer resolution. They compare N against 4N:
a linear path costs about 4x the work, a quadratic one about 16x.
"""

from datetime import datetime, timezone
import sys
from unittest import TestCase
from unittest.mock import patch

from bbc_core import Connection, ConnectionState, Core, Gap, Observation, SimVar, ValueType
from bbc_core import core as core_module
from bbc_core.provider import Reading
from bbc_core.simulated import SimulatedProvider

from test_core_support import FakeClock

_LINEAR_RATIO_LIMIT = 6.0  # Above linear (4x) plus fixed costs, well below quadratic (16x).


def count_lines(action):
    """Run ``action`` and return how many Python lines it executed (in any module)."""
    executed = 0

    def local(frame, event, arg):
        nonlocal executed
        if event == "line":
            executed += 1
        return local

    previous = sys.gettrace()
    sys.settrace(lambda frame, event, arg: local)
    try:
        action()
    finally:
        sys.settrace(previous)
    return executed


class _InjectingProvider(SimulatedProvider):
    def __init__(self, clock):
        super().__init__(clock=clock, periodic=False)

    def inject(self, signal, value, at):
        self._offer(Reading(self._session_id, signal, value, datetime.now(timezone.utc), at))


def _signal(index):
    return SimVar(f"PERF SIGNAL {index}", "number", ValueType.NUMBER)


class _Rig:
    def __init__(self, consumers, signals, shared):
        self.clock = FakeClock()
        self.provider = _InjectingProvider(self.clock)
        self.core = Core(self.provider)
        self.consumers = [self.core.register_consumer(queue_size=4096) for _ in range(consumers)]
        self.variables = [_signal(index) for index in range(signals)]
        for number, consumer in enumerate(self.consumers):
            for index, variable in enumerate(self.variables):
                if shared or index % consumers == number:
                    consumer.subscribe_sdk_simvar(variable)
        self.core.connect()
        self.core.pump()
        for consumer in self.consumers:
            consumer.poll(limit=4096)
        by_name = {signal.name: signal for signal in self.core._by_signal}
        self.signals = [by_name[variable.name.upper()] for variable in self.variables]

    def burst(self, samples):
        """Work done by one pump that receives ``samples`` readings of every signal."""
        for sample in range(samples):
            self.clock.advance(1)
            for signal in self.signals:
                self.provider.inject(signal, float(sample), self.clock.now)
        work = count_lines(lambda: self.core.pump(limit=4096))
        delivered = 0
        for consumer in self.consumers:
            while batch := consumer.poll(limit=4096):
                delivered += len(batch)
        self.core.disconnect()
        return work, delivered

    def idle(self, pumps):
        """Work done by ``pumps`` pumps with nothing to receive."""
        self.core.pump()
        work = count_lines(lambda: [self.core.pump() for _ in range(pumps)])
        self.core.disconnect()
        return work


class ReceiveScalingTests(TestCase):
    def assert_linear(self, small, large, what):
        ratio = large / max(small, 1)
        self.assertLess(ratio, _LINEAR_RATIO_LIMIT, f"{what}: 4x load did {ratio:.1f}x the work ({small} -> {large})")

    def test_receive_work_grows_linearly_with_readings(self):
        small, small_delivered = _Rig(1, 100, shared=True).burst(10)
        large, large_delivered = _Rig(1, 100, shared=True).burst(40)
        self.assertEqual((small_delivered, large_delivered), (1000, 4000))
        self.assert_linear(small, large, "readings")

    def test_shared_delivery_grows_linearly_with_consumers(self):
        # 2 vs 8 consumers, all sharing the same 100 SimVars.
        small, small_delivered = _Rig(2, 100, shared=True).burst(4)
        large, large_delivered = _Rig(8, 100, shared=True).burst(4)
        self.assertEqual((small_delivered, large_delivered), (800, 3200))
        self.assert_linear(small, large, "consumers")
        # Ten consumers on one signal set cost no more per delivered update than two.
        ten, ten_delivered = _Rig(10, 100, shared=True).burst(4)
        self.assertEqual(ten_delivered, 4000)
        self.assertLessEqual(ten / ten_delivered, small / small_delivered)

    def test_idle_pump_work_does_not_grow_with_subscriptions(self):
        few = _Rig(10, 10, shared=False).idle(200)
        many = _Rig(10, 1000, shared=False).idle(200)
        self.assertGreater(few, 0)
        self.assertLessEqual(many, few * 1.1, f"idle pumps: {few} lines with 10 subscriptions, {many} with 1000")

    def test_the_work_meter_catches_a_quadratic_receive_path(self):
        # The guard is only useful if it fails on the regression it exists for: a receive
        # path that rescans the consumer queue for every reading (the A141 shape).
        original = core_module._Consumer.offer

        def rescanning_offer(consumer, update):
            for _ in consumer.data:
                pass
            original(consumer, update)

        with patch.object(core_module._Consumer, "offer", rescanning_offer):
            small, _ = _Rig(1, 100, shared=True).burst(10)
            large, _ = _Rig(1, 100, shared=True).burst(40)
        self.assertGreater(large / small, _LINEAR_RATIO_LIMIT * 1.5)


class ControlLaneOverflowTests(TestCase):
    def test_overflowing_status_lane_reports_a_gap_and_keeps_the_newest_updates(self):
        provider = SimulatedProvider()
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer(queue_size=1)
        lane = core._consumers[consumer._consumer_id].control_size
        for _ in range(lane):
            core.connect()
            core.disconnect()
        core.connect()
        updates = []
        while True:
            batch = consumer.poll(limit=4096)
            if not batch:
                break
            updates.extend(batch)
        gap, *rest = updates
        self.assertIsInstance(gap, Gap)
        self.assertIn("status queue lost updates", gap.reason)
        self.assertEqual(len(rest), lane)
        self.assertTrue(all(isinstance(item, Connection) for item in rest))
        self.assertIs(rest[-1].state, ConnectionState.CONNECTED)
        self.assertGreaterEqual(gap.dropped_updates, lane)  # At least two updates per cycle were made.
        # The loss is reported once.
        self.assertEqual(consumer.poll(), ())

    def test_a_dropped_gap_carries_its_own_count_into_the_next_gap(self):
        provider = SimulatedProvider()
        core = Core(provider)
        self.addCleanup(core.disconnect)
        consumer = core.register_consumer(queue_size=1)
        internal = core._consumers[consumer._consumer_id]
        internal.offer(Gap(7, "Provider update queue lost data."))
        for _ in range(internal.control_size):
            internal.offer(Gap(1, "filler"))
        first = consumer.poll(limit=1)[0]
        self.assertIsInstance(first, Gap)
        self.assertEqual(first.dropped_updates, 7)
        self.assertNotIsInstance(first, Observation)
