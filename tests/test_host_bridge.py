"""CoreBridge: Qt signal delivery on the UI thread, offscreen, with a live CoreHost."""

from __future__ import annotations

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from bbc_core import Connection, ConnectionState, CoreRequestError, Observation  # noqa: E402
from bbc_core.host import CoreHost, HostConfig, HostState  # noqa: E402
from bbc_efb.core_bridge import ConsumerBatch, CoreBridge  # noqa: E402

try:  # discovered as a package (-t .) or as a plain folder (-s tests)
    from tests.test_host_core import FAST, ThreadRecordingProvider  # noqa: E402
except ImportError:  # pragma: no cover - depends on how unittest was started
    from test_host_core import FAST, ThreadRecordingProvider  # noqa: E402

APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.003)
    QCoreApplication.processEvents()
    return predicate()


class FakeApi:
    """Duck-typed ConsumerAPI that always has more updates queued."""

    def __init__(self, label, total=1000):
        self.label = label
        self.remaining = total
        self.polls = []
        self.closed = False

    def poll(self, *, limit=128):
        if self.closed:
            raise CoreRequestError("Consumer is closed or unknown.")
        self.polls.append(threading.get_ident())
        count = min(limit, self.remaining)
        self.remaining -= count
        return tuple(f"{self.label}{i}" for i in range(count))


class CoreBridgeTests(unittest.TestCase):
    def setUp(self):
        self.provider = ThreadRecordingProvider(minimum_interval_ms=100)
        self.host = CoreHost(self.provider, config=HostConfig(**FAST))
        self.addCleanup(self.host.stop)

    def bridge(self, **kwargs):
        options = dict(interval_ms=10)
        options.update(kwargs)
        bridge = CoreBridge(self.host, **options)
        self.addCleanup(bridge.close)
        return bridge

    def test_connection_and_status_signals_arrive_on_the_ui_thread(self):
        bridge = self.bridge()
        connections, statuses, threads = [], [], set()
        bridge.connection_changed.connect(lambda c: (connections.append(c), threads.add(threading.get_ident())))
        bridge.status_changed.connect(lambda s: (statuses.append(s), threads.add(threading.get_ident())))
        self.host.start()
        self.assertTrue(spin_until(lambda: connections and connections[-1].state is ConnectionState.CONNECTED))
        self.assertTrue(all(isinstance(c, Connection) for c in connections))
        self.assertEqual(threads, {threading.main_thread().ident})
        self.assertTrue(spin_until(lambda: statuses and statuses[-1].state is HostState.CONNECTED))
        # Duplicates are suppressed: each connection record is emitted once.
        self.assertEqual(len(connections), len({id(c) for c in connections}))
        self.assertEqual(len(connections), len(set(connections)))
        self.host.stop()
        self.assertTrue(spin_until(lambda: statuses[-1].state is HostState.STOPPED))
        self.assertEqual(connections[-1].state, ConnectionState.DISCONNECTED)

    def test_updates_are_drained_on_ui_thread_and_pump_never_runs_there(self):
        bridge = self.bridge()
        api = bridge.api("display")
        sub = api.subscribe("aircraft.ground_speed", interval_ms=100)
        batches, channel_batches, threads = [], [], set()
        bridge.updates.connect(lambda b: (batches.append(b), threads.add(threading.get_ident())))
        bridge.channel("display").updates.connect(channel_batches.append)
        self.host.start()
        self.assertTrue(spin_until(lambda: self.host.status().state is HostState.CONNECTED))
        self.host.run(lambda core: self.provider.publish("aircraft.ground_speed", 42))

        def got_42():
            return any(isinstance(u, Observation) and u.value == 42.0 for b in batches for u in b.updates)
        self.assertTrue(spin_until(got_42))
        self.assertTrue(all(isinstance(b, ConsumerBatch) and b.name == "display" for b in batches))
        self.assertEqual([b.updates for b in batches], channel_batches)
        self.assertEqual(threads, {threading.main_thread().ident})
        self.assertNotIn(threading.main_thread().ident, self.provider.all_threads())
        self.assertEqual(self.provider.all_threads(), {self.host.worker_thread.ident})
        self.assertEqual(api.latest(sub.id).value, 42.0)

    def test_small_batches_bounded_per_tick_and_round_robin(self):
        bridge = self.bridge(autostart=False, batch_size=5, max_batches_per_tick=2, tick_budget_ms=50)
        first, second = FakeApi("a"), FakeApi("b")
        bridge.attach("a", first)
        bridge.attach("b", second)
        sizes = []
        bridge.updates.connect(lambda b: sizes.append((b.name, len(b.updates))))
        delivered = bridge.drain()
        self.assertEqual(delivered, 20)  # 2 batches of 5 for each consumer
        self.assertTrue(all(size <= 5 for _, size in sizes))
        self.assertEqual({name for name, _ in sizes}, {"a", "b"})
        sizes.clear()
        bridge.drain()
        self.assertEqual(sizes[0][0], "b")  # the next tick starts with the other consumer
        with self.assertRaises(ValueError):
            bridge.attach("a", FakeApi("x"))

    def test_time_budget_limits_a_tick(self):
        bridge = self.bridge(autostart=False, batch_size=1, max_batches_per_tick=64, tick_budget_ms=0.001)
        bridge.attach("busy", FakeApi("x"))
        self.assertLessEqual(bridge.drain(), 2)

    def test_closed_consumer_is_dropped_without_error(self):
        bridge = self.bridge(autostart=False)
        fake = FakeApi("a")
        bridge.attach("a", fake)
        fake.closed = True
        with self.assertLogs("bbc_efb.core_bridge", level="INFO"):
            bridge.drain()
        self.assertEqual(bridge.consumer_names(), ())

    def test_ui_activity_slows_drain_and_reaches_host(self):
        bridge = self.bridge(interval_ms=20, inactive_interval_ms=400)
        changes = []
        bridge.ui_active_changed.connect(changes.append)
        bridge.set_ui_active(False)
        self.assertFalse(self.host.ui_active)
        self.assertTrue(spin_until(lambda: changes == [False]))
        self.assertEqual(bridge._timer.interval(), 400)
        self.host.set_ui_active(True)  # from the host side (any thread) too
        self.assertTrue(spin_until(lambda: changes == [False, True]))
        self.assertEqual(bridge._timer.interval(), 20)

    def test_watch_window_follows_minimise_and_hide(self):
        bridge = self.bridge()
        window = QWidget()
        self.addCleanup(window.deleteLater)
        bridge.watch_window(window)
        self.assertFalse(self.host.ui_active)  # not shown yet
        window.show()
        self.assertTrue(spin_until(lambda: self.host.ui_active))
        window.setWindowState(Qt.WindowState.WindowMinimized)
        self.assertTrue(spin_until(lambda: not self.host.ui_active))
        window.setWindowState(Qt.WindowState.WindowNoState)
        self.assertTrue(spin_until(lambda: self.host.ui_active))
        window.hide()
        self.assertTrue(spin_until(lambda: not self.host.ui_active))

    def test_run_on_core_calls_back_on_ui_thread(self):
        bridge = self.bridge()
        self.host.start()
        results, errors, threads = [], [], set()
        bridge.run_on_core(lambda core: threading.get_ident(),
                           on_result=lambda r: (results.append(r), threads.add(threading.get_ident())))
        bridge.run_on_core(lambda core: 1 / 0, on_error=errors.append)
        self.assertTrue(spin_until(lambda: results and errors))
        self.assertEqual(results, [self.host.worker_thread.ident])
        self.assertIsInstance(errors[0], ZeroDivisionError)
        self.assertEqual(threads, {threading.main_thread().ident})

    def test_run_on_core_after_stop_reports_the_error_instead_of_raising(self):
        from bbc_core.host import HostStopped
        bridge = self.bridge()
        self.host.start()
        self.host.stop(5.0)
        errors = []
        future = bridge.run_on_core(lambda core: 1, on_error=errors.append)
        self.assertIsInstance(future.exception(0), HostStopped)
        self.assertTrue(spin_until(lambda: errors))
        self.assertIsInstance(errors[0], HostStopped)

    def test_close_releases_consumers_and_stops_signals(self):
        bridge = CoreBridge(self.host, interval_ms=10)
        bridge.api("display")
        statuses = []
        bridge.status_changed.connect(statuses.append)
        bridge.close()
        self.assertEqual(self.host.consumer_names(), ())
        self.host.start()
        self.assertTrue(spin_until(lambda: self.host.status().state is HostState.CONNECTED))
        spin_until(lambda: False, timeout=0.1)
        self.assertEqual(statuses, [])
        with self.assertRaises(RuntimeError):
            bridge.api("again")

    def test_invalid_options(self):
        for bad in (dict(interval_ms=0), dict(inactive_interval_ms=1, interval_ms=50), dict(batch_size=0),
                    dict(max_batches_per_tick=0), dict(tick_budget_ms=0)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CoreBridge(self.host, **bad)


if __name__ == "__main__":
    unittest.main()
