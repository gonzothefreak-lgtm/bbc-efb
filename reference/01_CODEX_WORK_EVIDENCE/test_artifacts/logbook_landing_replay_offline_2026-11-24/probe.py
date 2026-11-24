"""Check landing telemetry delivery with a fake SDK and synthetic timing."""

import ctypes  # Load Python's ctypes support before guarding native library loads.
from pathlib import Path
from struct import pack
import sys
import unittest


BLOCKED = []


def guard(event, args):
    if (event == "ctypes.dlopen"
            or event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec"))
            or event in ("os.system", "os.startfile", "_winapi.CreateProcess")):
        BLOCKED.append(event)
        raise RuntimeError(f"Offline guard blocked {event}")


sys.addaudithook(guard)
ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from bbc_core import Core, ValueType
from bbc_core.simconnect import SimConnectProvider
from bbc_core.simconnect_abi import Period
from test_simconnect_provider_offline import FakeSdk


class LandingReplayOfflineTests(unittest.TestCase):
    def test_direct_adapter_clamps_fast_landing_subscriptions_to_one_second(self):
        sdk = FakeSdk()
        core = Core(SimConnectProvider(sdk))
        self.addCleanup(core.disconnect)
        recorder = core.register_consumer(diagnostics=True)
        requests = (
            ("SIM ON GROUND", "bool", ValueType.BOOLEAN, pack("<i", 1), True),
            ("VERTICAL SPEED", "feet per second", ValueType.NUMBER, pack("<d", -4), -4),
            ("PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", ValueType.NUMBER,
             pack("<d", -2.5), -2.5),
            ("G FORCE", "GForce", ValueType.NUMBER, pack("<d", 1.37), 1.37),
        )
        subscriptions = [recorder.subscribe_simvar(name, unit, kind, interval_ms=20)
                         for name, unit, kind, _, _ in requests]
        core.connect()
        core.pump()
        calls = sdk.matching_calls("SimConnect_RequestDataOnSimObject")
        self.assertEqual(len(calls), len(requests))
        self.assertTrue(all(args[3:8] == (Period.SECOND, 0, 0, 0, 0) for args, _ in calls))
        self.assertTrue(all(subscription.interval_ms == 1000 for subscription in subscriptions))

        definitions = {fields[0][0].decode(): definition
                       for definition, fields in sdk.definitions.items()}
        for name, _, _, payload, _ in requests:
            sdk.data(definitions[name], payload)
        core.pump()
        for subscription, (_, _, _, _, expected) in zip(subscriptions, requests):
            with self.subTest(value=expected):
                sample = recorder.latest(subscription.id)
                self.assertTrue(sample.source.simulated)
                self.assertEqual(sample.value, expected)

    def test_ideal_sampling_can_miss_brief_events(self):
        for interval_ms, expected in ((1000, (35, 6)), (100, (100, 60)), (50, (100, 100))):
            # Sweep every whole-ms phase relative to an arbitrary event start.
            counts = tuple(sum(phase < width for phase in range(interval_ms))
                           for width in (350, 60))
            percentages = tuple(count * 100 // interval_ms for count in counts)
            with self.subTest(interval_ms=interval_ms):
                self.assertEqual(percentages, expected)
            print(f"{interval_ms:4d} ms: bounce {counts[0]}/{interval_ms}, "
                  f"peak window {counts[1]}/{interval_ms}", flush=True)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LandingReplayOfflineTests)
    )
    print(f"Offline guard violations: {len(BLOCKED)}")
    raise SystemExit(0 if result.wasSuccessful() and not BLOCKED else 1)
