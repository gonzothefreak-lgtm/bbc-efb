"""Flight tape format, FakeConsumerAPI behaviour, recording and the generated scenarios."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from bbc_core.contract import Connection, ConnectionState, Event, Gap, PauseState, Snapshot, SnapshotPeriod, ValueType
from bbc_core.sdk_simvar import SimVar
from bbc_recorder import tape as T

T0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
GS = SimVar("GROUND VELOCITY", "knots", ValueType.NUMBER)
TD_FPS = SimVar("PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", ValueType.NUMBER)
TD_FPM = SimVar("PLANE TOUCHDOWN NORMAL VELOCITY", "feet per minute", ValueType.NUMBER)
ON_GROUND = SimVar("SIM ON GROUND", "bool", ValueType.BOOLEAN)
TITLE = SimVar("TITLE", None, ValueType.TEXT)


def connected_api(**kwargs) -> T.FakeConsumerAPI:
    api = T.FakeConsumerAPI(clock=T.TapeClock(T0), **kwargs)
    api.push_connection(ConnectionState.CONNECTED, "s1")
    api.poll()
    return api


def push(api, dt, *, frames=6, paused=False, sim=None, **values):
    api.clock.advance(dt)
    api.push_frame(sim_time=sim if sim is not None else api.clock.t, abs_time=6.39e10 + api.clock.t, rate=1.0,
                   values={key.replace("_", " "): value for key, value in values.items()}, frames=frames,
                   paused=paused)


def snapshots(api):
    return [item for item in api.poll(limit=4096) if isinstance(item, Snapshot)]


class TapeFormatTests(unittest.TestCase):
    def test_round_trip_and_header(self):
        tape = T.SCENARIOS["bounced_landing"]()
        with tempfile.TemporaryDirectory() as tmp:
            path = tape.write(Path(tmp) / "a" / "b.tape.jsonl")
            first = json.loads(path.read_text(encoding="utf-8").split("\n", 1)[0])
            self.assertEqual(first["tape"], T.TAPE_FORMAT)
            self.assertEqual(first["version"], T.TAPE_VERSION)
            back = T.FlightTape.read(path, strict=True)
        self.assertEqual(back.items, tape.items)
        self.assertEqual(back.header.fields, tape.header.fields)
        self.assertEqual(back.header.start_host_utc, tape.header.start_host_utc)
        self.assertEqual(back.header.expect, tape.header.expect)
        self.assertEqual(back.issues, ())

    def test_torn_last_line_is_skipped_and_reported(self):
        tape = T.SCENARIOS["parked_only"]()
        with tempfile.TemporaryDirectory() as tmp:
            path = tape.write(Path(tmp) / "t.jsonl")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"t": 999, "type": "frame", "values": {"SIM ON')
            back = T.FlightTape.read(path)
            self.assertEqual(len(back.items), len(tape.items))
            self.assertEqual(len(back.issues), 1)
            with self.assertRaises(T.TapeError):
                T.FlightTape.read(path, strict=True)

    def test_rejects_foreign_newer_and_backwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jsonl"
            path.write_text('{"hello": 1}\n', encoding="utf-8")
            with self.assertRaises(T.TapeError):
                T.FlightTape.read(path)
            header = T.SCENARIOS["parked_only"]().header.to_json()
            header["version"] = T.TAPE_VERSION + 1
            path.write_text(json.dumps(header) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(T.TapeError, "newer"):
                T.FlightTape.read(path)
            header["version"] = T.TAPE_VERSION
            lines = [json.dumps(header), json.dumps({"t": 5, "type": "note", "text": "a"}),
                     json.dumps({"t": 4, "type": "note", "text": "b"}),
                     json.dumps({"t": 6, "type": "bogus"})]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            back = T.FlightTape.read(path)
            self.assertEqual([item.data["text"] for item in back.items], ["a"])
            self.assertEqual(len(back.issues), 2)

    def test_generate_and_info_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(T.main(["generate", tmp]), 0)
            paths = sorted(Path(tmp).glob("*.tape.jsonl"))
            self.assertEqual(len(paths), len(T.SCENARIOS))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(T.main(["info", str(paths[0])]), 0)
            self.assertIn("frame", out.getvalue())
            with redirect_stdout(io.StringIO()):
                import contextlib
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(T.main([]), 2)


class FakeApiTests(unittest.TestCase):
    def test_second_period_keeps_one_hertz_under_jitter(self):
        api = connected_api()
        sub = api.subscribe_snapshot((GS,), period=SnapshotPeriod.SECOND)
        # 5 Hz frames with +-30 ms jitter for 60 s.
        times = []
        t = 0.0
        for index in range(300):
            target = (index + 1) * 0.2 + (0.03 if index % 3 == 0 else -0.03)
            push(api, target - t, GROUND_VELOCITY=1.0)
            t = target
        got = [s for s in snapshots(api) if s.subscription_id == sub.id]
        self.assertIn(len(got), (59, 60, 61), "no cadence halving: about one per second")
        times = [s.received_at for s in got]
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        self.assertLess(max(gaps), 1.3)

    def test_sim_frame_decimation_and_pause(self):
        api = connected_api()
        every6 = api.subscribe_snapshot((GS,), period=SnapshotPeriod.SIM_FRAME, every=6)
        every12 = api.subscribe_snapshot((GS,), period=SnapshotPeriod.SIM_FRAME, every=12)
        for _ in range(10):
            push(api, 0.2, frames=6, GROUND_VELOCITY=5.0)
        push(api, 1.0, frames=0, paused=True, GROUND_VELOCITY=5.0)
        got = snapshots(api)
        self.assertEqual(sum(1 for s in got if s.subscription_id == every6.id), 10)
        self.assertEqual(sum(1 for s in got if s.subscription_id == every12.id), 5)

    def test_changed_only_and_unknown_values(self):
        api = connected_api()
        sub = api.subscribe_snapshot((GS, TITLE), period=SnapshotPeriod.SIM_FRAME, changed_only=True)
        push(api, 0.2, frames=1, GROUND_VELOCITY=5.0)
        push(api, 0.2, frames=1, GROUND_VELOCITY=5.0)
        push(api, 0.2, frames=1, GROUND_VELOCITY=6.0)
        got = [s for s in snapshots(api) if s.subscription_id == sub.id]
        self.assertEqual([s.values for s in got], [(5.0, None), (6.0, None)])  # TITLE absent: unknown
        self.assertEqual(api.latest_snapshot(sub.id).values, (6.0, None))

    def test_unit_conversion(self):
        api = T.FakeConsumerAPI(clock=T.TapeClock(T0), fields=(TD_FPS,))
        api.push_connection(ConnectionState.CONNECTED, "s1")
        sub = api.subscribe_snapshot((TD_FPM,), period=SnapshotPeriod.SIM_FRAME)
        push(api, 0.2, frames=1, PLANE_TOUCHDOWN_NORMAL_VELOCITY=-3.0)
        self.assertEqual(api.latest_snapshot(sub.id).values, (-180.0,))
        self.assertIsNone(T.convert_unit(1.0, "furlongs", "feet"))
        self.assertEqual(T.convert_unit(True, "bool", "number"), True)

    def test_validation_and_unsupported(self):
        api = connected_api(unsupported=("sim.crash_reset",))
        from bbc_core.contract import CoreRequestError

        with self.assertRaises(CoreRequestError):
            api.subscribe("sim.crash_reset")
        with self.assertRaises(CoreRequestError):
            api.subscribe("no.such.capability")
        with self.assertRaises(CoreRequestError):
            api.subscribe_snapshot((GS, GS))
        with self.assertRaises(CoreRequestError):
            api.subscribe_snapshot((GS,), every=0)
        with self.assertRaises(CoreRequestError):
            api.unsubscribe("nope")

    def test_queue_overflow_reports_gap_and_keeps_control_updates(self):
        api = connected_api(queue_limit=5)
        api.subscribe_snapshot((GS,), period=SnapshotPeriod.SIM_FRAME)
        for _ in range(8):
            push(api, 0.2, frames=1, GROUND_VELOCITY=5.0)
        api.push_connection(ConnectionState.DISCONNECTED, None, "gone")
        batch = api.poll()
        self.assertIsInstance(batch[0], Gap)
        self.assertEqual(batch[0].dropped_updates, 3)
        self.assertIsInstance(batch[-1], Connection)
        self.assertEqual(batch[-1].state, ConnectionState.DISCONNECTED)

    def test_events_and_disconnected_frames(self):
        api = T.FakeConsumerAPI(clock=T.TapeClock(T0))
        sub = api.subscribe_snapshot((GS,))
        api.subscribe("sim.pause_state")
        push(api, 1.0, GROUND_VELOCITY=1.0)  # not connected: nothing
        api.push_event("sim.pause_state", 4)
        self.assertEqual(api.poll(), ())
        api.push_connection(ConnectionState.CONNECTED, "s1")
        api.push_event("sim.pause_state", 4)
        api.push_event("sim.stopped", 0)  # not subscribed: dropped
        batch = api.poll()
        events = [item for item in batch if isinstance(item, Event)]
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0].value, PauseState)
        self.assertTrue(events[0].value.active_pause)
        self.assertIsNone(api.latest_snapshot(sub.id))

    def test_nearby_airports(self):
        api = connected_api(airports=[T.AIRPORTS["YSSY"]])
        pending = api.request_nearby_airports()
        self.assertEqual(pending.state.value, "pending")
        result = [item for item in api.poll() if getattr(item, "id", None) == pending.id][0]
        self.assertEqual(result.state.value, "ready")
        self.assertEqual(result.data[0].icao, "YSSY")
        self.assertIs(api.facility_result(pending.id), result)
        empty = connected_api()
        pending = empty.request_nearby_airports()
        self.assertEqual(empty.facility_result(pending.id).state.value, "no_data")


class RecordingTests(unittest.TestCase):
    def test_writer_records_core_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = connected_api()
            sub = api.subscribe_snapshot((GS, ON_GROUND))
            other = api.subscribe_snapshot((TITLE,))
            api.subscribe("sim.pause_state")
            writer = T.TapeWriter(Path(tmp) / "rec.jsonl", name="rec", fields=(GS, ON_GROUND))
            recording = T.RecordingConsumerAPI(api, writer)
            recording._snapshot_ids.add(sub.id)
            api.push_connection(ConnectionState.CONNECTED, "s2")
            push(api, 1.0, GROUND_VELOCITY=7.0, SIM_ON_GROUND=True)
            api.push_event("sim.pause_state", 1)
            api.push_gap(3, "lost")
            recording.poll()
            writer.note("end")
            path = writer.close()
            tape = T.FlightTape.read(path, strict=True)
        types = [item.type for item in tape.items]
        self.assertEqual(types, ["connection", "frame", "event", "gap", "note"])
        frame = tape.items[1]
        self.assertEqual(frame.data["values"], {"GROUND VELOCITY": 7.0, "SIM ON GROUND": True})
        self.assertNotIn(other.id, recording._snapshot_ids)
        self.assertEqual(tape.header.fields, (GS, ON_GROUND))

    def test_signal_error_is_recorded_and_replayed_as_its_own_type_not_a_gap(self):
        from bbc_core.contract import SignalError
        with tempfile.TemporaryDirectory() as tmp:
            api = connected_api()
            writer = T.TapeWriter(Path(tmp) / "rec.jsonl", name="rec", fields=(GS, ON_GROUND))
            recording = T.RecordingConsumerAPI(api, writer)
            api.push_signal_error(2, "Unknown SimVar", "sdk.simvar:X", "sub-9")
            batch = recording.poll()
            path = writer.close()
            tape = T.FlightTape.read(path, strict=True)
        errors = [u for u in batch if isinstance(u, SignalError)]
        self.assertEqual(len(errors), 1)
        self.assertNotIsInstance(errors[0], Gap)
        self.assertEqual([item.type for item in tape.items], ["signal_error"])
        self.assertEqual(tape.items[0].data, {"dropped": 2, "reason": "Unknown SimVar",
                                              "capability_id": "sdk.simvar:X", "subscription_id": "sub-9"})
        replayer = T.TapeReplayer(tape)
        replayer.release_until(tape.items[-1].t)
        replayed = [u for u in replayer.api.poll() if isinstance(u, (SignalError, Gap))]
        self.assertEqual(len(replayed), 1)
        self.assertIsInstance(replayed[0], SignalError)
        self.assertEqual(replayed[0].capability_id, "sdk.simvar:X")


class ScenarioTests(unittest.TestCase):
    def test_every_scenario_is_valid_and_labelled_synthetic(self):
        for name, factory in T.SCENARIOS.items():
            with self.subTest(name):
                tape = factory()
                self.assertTrue(tape.header.simulated)
                self.assertIn("synthetic", tape.header.source_name)
                times = [item.t for item in tape.items]
                self.assertEqual(times, sorted(times))
                frames = list(tape.frames())
                self.assertGreater(len(frames), 100)
                sim_times = [f.data["sim_time"] for f in frames]
                self.assertEqual(sim_times, sorted(sim_times), "simulator time never goes backwards in a tape")
                names = {variable.sdk_name for variable in tape.header.fields}
                for frame in frames[:5]:
                    self.assertLessEqual(set(frame.data["values"]), names)

    def test_normal_flight_features(self):
        tape = T.normal_flight()
        frames = list(tape.frames())
        epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
        from datetime import timedelta

        first = epoch + timedelta(seconds=frames[0].data["abs_time"])
        last = epoch + timedelta(seconds=frames[-1].data["abs_time"])
        self.assertEqual((first.day, last.day), (24, 25), "crosses 00:00Z")
        self.assertIn(4.0, {f.data["rate"] for f in frames})
        pauses = [item.data["value"] for item in tape.items if item.type == "event"
                  and item.data["capability_id"] == "sim.pause_state"]
        self.assertEqual(pauses, [1, 0, 4, 0])
        paused = [f for f in frames if f.data["paused"]]
        self.assertTrue(paused)
        self.assertEqual(len({f.data["sim_time"] for f in paused}), 2, "simulator time stands still in pause")
        spacing = [round(b.t - a.t, 3) for a, b in zip(frames, frames[1:])]
        self.assertIn(0.2, spacing)
        self.assertIn(1.0, spacing)

    def test_parked_only_never_moves(self):
        tape = T.parked_only()
        self.assertTrue(all(f.data["values"]["GROUND VELOCITY"] == 0.0 for f in tape.frames()))

    def test_replayer_runs_hooks_in_time(self):
        tape = T.parked_only()
        api = T.FakeConsumerAPI.for_tape(tape)
        replayer = T.TapeReplayer(tape, api)
        seen = []
        ticks = replayer.run(lambda: None, tick_s=0.5, hooks={10.0: lambda: seen.append(api.clock.t)})
        self.assertEqual(seen, [10.0])
        self.assertTrue(replayer.finished)
        self.assertGreater(ticks, tape.duration_s / 0.5 - 2)
        with self.assertRaises(ValueError):
            api.clock.advance(-1)


if __name__ == "__main__":
    unittest.main()


class CoreSessionTests(unittest.TestCase):
    """bbc_recorder.tape.core_sessions: Recorder tapes as bbc_core.simulated FlightTape sessions."""

    def test_sessions_actions_and_values(self):
        from bbc_core.simulated import TapeAction

        sessions = T.core_sessions(T.SCENARIOS["disconnect_midflight"]())
        self.assertEqual(len(sessions), 2)
        first, second = sessions
        self.assertEqual(first.start_t, 0.0)
        self.assertIsNotNone(first.end_t)
        self.assertIsNone(second.end_t)
        self.assertGreater(second.start_t, first.end_t)
        last = first.tape.entries[-1]
        self.assertIs(last.target, TapeAction.DISCONNECT)
        self.assertEqual(last.value, "MSFS stopped responding.")
        self.assertIsNotNone(first.absolute_time_s)
        names = {entry.target.sdk_name for entry in first.tape.entries if isinstance(entry.target, SimVar)}
        self.assertIn("SIM ON GROUND", names)
        self.assertIn("ATC ID", names)

    def test_pause_rate_and_gap_become_provider_actions(self):
        from bbc_core.simulated import TapeAction

        (normal,) = T.core_sessions(T.SCENARIOS["normal_flight"]())
        actions = [(entry.target, entry.value) for entry in normal.tape.entries if isinstance(entry.target, TapeAction)]
        self.assertIn((TapeAction.PAUSE, 1), actions)
        self.assertIn((TapeAction.PAUSE, 4), actions)
        self.assertIn((TapeAction.RATE, 4.0), actions)
        (gap,) = T.core_sessions(T.SCENARIOS["queue_gap_on_approach"]())
        self.assertIn((TapeAction.GAP, 37), [(e.target, e.value) for e in gap.tape.entries])
        # Only changes are scripted after the first frame.
        on_ground = [e.value for e in normal.tape.entries
                     if isinstance(e.target, SimVar) and e.target.sdk_name == "SIM ON GROUND"]
        self.assertEqual(on_ground, [True, False, True])

    def test_tape_fields_are_what_the_service_subscribes(self):
        from bbc_recorder.attribution import identity_simvars
        from bbc_recorder.rules import snapshot_simvars

        self.assertEqual(T.tape_fields(), snapshot_simvars() + identity_simvars())
