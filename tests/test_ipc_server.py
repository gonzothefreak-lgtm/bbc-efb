"""End-to-end checks of the experimental standalone-Core server and client over a real local transport.

Each test runs Core with the explicitly simulated provider and a pump thread in this process,
a CoreServer on a private socket file (a named pipe on Windows), and clients in this process
or in a child Python process. Nothing touches a network or a simulator.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from multiprocessing.connection import Client
from pathlib import Path

from datetime import datetime, timezone

from bbc_core.contract import (
    CommandResult, CommandState, Connection, ConnectionState, CoreRequestError, Discovery, Gap, SignalError,
    Snapshot, SnapshotPeriod, Source, Subscription, SubscriptionEnded, ValueType,
)
from bbc_core.core import Core
from bbc_core.ipc import (
    CorePumpThread, CoreServer, CoreUnavailableError, Grant, HandshakeRejected, PrincipalPolicy, ServerNotTrusted,
    connect,
)
from bbc_core.ipc import client as ipc_client
from bbc_core.ipc import protocol as wire
from bbc_core.sdk_simvar import SimVar
from bbc_core.simulated import SimulatedProvider

ROOT = Path(__file__).resolve().parent.parent
FAMILY = wire.default_family()


def _active_offline_guard():
    """The guarded runner's check function when this process runs under it, else None.

    tests/test_repo_offline_guard.py installs an audit hook (which cannot be detected or removed)
    when it is the main script. Asking its own rule function never opens anything.
    """
    main = sys.modules.get("__main__")
    if Path(getattr(main, "__file__", "") or "").name != "test_repo_offline_guard.py":
        return None
    return getattr(main, "offline_guard_violation", None) or (lambda event, args: True)


def _guard_allows_local_ipc(guard) -> bool:
    if guard is None or FAMILY == "AF_PIPE":  # named pipes raise no socket.* audit events
        return True
    fake = types.SimpleNamespace(family=getattr(socket, "AF_UNIX", 1))
    probes = (("socket.__new__", (fake, fake.family, socket.SOCK_STREAM, 0)),
              ("socket.bind", (fake, "/tmp/bbc-core-probe.sock")),
              ("socket.connect", (fake, "/tmp/bbc-core-probe.sock")))
    return not any(guard(event, args) for event, args in probes)


_GUARD = _active_offline_guard()
LOCAL_IPC_BLOCKED = not _guard_allows_local_ipc(_GUARD)
PROCESSES_BLOCKED = _GUARD is not None
GUARD_REASON = ("the offline guard blocks local socket files; it needs the local-IPC allowance "
                "(AF_UNIX only) before these transport tests can run under it")
ALTITUDE = SimVar("PLANE ALTITUDE", "feet", ValueType.NUMBER)
ON_GROUND = SimVar("SIM ON GROUND", "bool", ValueType.BOOLEAN)
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def poll_until(api, kind, timeout=5.0, collected=None):
    """Poll until an update of ``kind`` arrives; returns it (or None)."""
    found = []

    def check():
        for update in api.poll():
            if collected is not None:
                collected.append(update)
            if isinstance(update, kind):
                found.append(update)
        return found

    wait_for(check, timeout)
    return found[0] if found else None


class IpcHarness(unittest.TestCase):
    server_options: dict = {}

    def setUp(self):
        if LOCAL_IPC_BLOCKED:
            self.skipTest(GUARD_REASON)
        self.folder = tempfile.TemporaryDirectory(prefix="bbcipc")
        if FAMILY == "AF_PIPE":
            self.address = rf"\\.\pipe\bbc-core-test-{secrets.token_hex(6)}"
        else:
            self.address = os.path.join(self.folder.name, "c.sock")
        self.key = secrets.token_bytes(32)
        self.provider = SimulatedProvider(minimum_interval_ms=50)
        self.core = Core(self.provider)
        self.assertIs(self.core.connect().state, ConnectionState.CONNECTED)
        self.pump = CorePumpThread(self.core, interval_s=0.005).start()
        self.server = self.make_server(**self.server_options).start()
        self.clients = []

    def make_server(self, **options):
        options.setdefault("authkeys", self.key)
        options.setdefault("address", self.address)
        return CoreServer(self.core, **options)

    def tearDown(self):
        for api in self.clients:
            api.close()
        self.server.close()
        self.pump.stop()
        self.assertIsNone(self.pump.error)
        self.core.disconnect()
        self.folder.cleanup()

    def connect(self, **options):
        options.setdefault("authkey", self.key)
        options.setdefault("client_name", "test module")
        api = connect(self.address, **options)
        self.clients.append(api)
        return api

    def consumer_count(self):
        return len(self.core._consumers)

    def raw_session(self, **hello):
        """A handshaken connection with no reader thread, for abrupt-exit and protocol tests."""
        connection = Client(self.address, family=FAMILY)
        message = {
            "type": wire.HELLO, "protocol_versions": [1], "api_version": wire.API_VERSION,
            "schema": wire.schema_fingerprint(), "principal": "default", "client_name": "raw",
            "client_pid": os.getpid(), "control_groups": [], "diagnostics": False, "delivery": "pull",
            "queue_size": 64, "nonce": wire.b64(wire.new_nonce()),
        }
        message.update(hello)
        welcome = ipc_client._handshake(connection, self.key, message, 5.0)
        return connection, welcome


class ClientRoundTripTests(IpcHarness):
    def test_subscribe_and_poll_snapshots_like_in_process(self):
        api = self.connect()
        self.assertTrue(api.connected)
        self.assertEqual(api.protocol_version, 1)
        self.assertIs(api.initial_connection.state, ConnectionState.CONNECTED)
        self.assertTrue(api.initial_connection.source.simulated, "simulated data must stay labelled simulated")
        self.assertIs(api.connection().state, ConnectionState.CONNECTED)
        subscription = api.subscribe_snapshot((ALTITUDE, ON_GROUND), period=SnapshotPeriod.SIM_FRAME, every=2)
        self.assertIsInstance(subscription, Subscription)
        snapshot = poll_until(api, Snapshot)
        self.assertIsNotNone(snapshot, "no snapshot arrived over the local interface")
        self.assertEqual(snapshot.subscription_id, subscription.id)
        self.assertEqual(snapshot.names, ("PLANE ALTITUDE", "SIM ON GROUND"))
        self.assertIsNotNone(snapshot.received_at.tzinfo)
        self.assertTrue(snapshot.source.simulated)
        latest = wait_for(lambda: api.latest_snapshot(subscription.id))
        self.assertIsInstance(latest, Snapshot)
        self.assertEqual(self.consumer_count(), 1)

    def test_discovery_matches_an_in_process_consumer(self):
        api = self.connect()
        local = self.core.register_consumer()
        try:
            remote = api.discover()
            self.assertIsInstance(remote, Discovery)
            self.assertEqual(remote, local.discover())
        finally:
            local.close()

    def test_request_errors_are_the_same_as_in_process(self):
        api = self.connect()
        local = self.core.register_consumer()
        try:
            for call in (lambda target: target.subscribe_snapshot(()),
                         lambda target: target.subscribe("no.such.capability"),
                         lambda target: target.latest("missing")):
                with self.assertRaises(CoreRequestError) as in_process:
                    call(local)
                with self.assertRaises(CoreRequestError) as remote:
                    call(api)
                self.assertEqual(str(remote.exception), str(in_process.exception))
        finally:
            local.close()
        with self.assertRaises(CoreRequestError):
            api.poll(limit=0)
        self.assertTrue(api.connected, "a bad request must not end the session")

    def test_refused_command_is_a_rejected_result_like_in_process(self):
        api = self.connect()
        local = self.core.register_consumer()
        try:
            remote = api.submit_command("radio.com1.swap")
            expected = local.submit_command("radio.com1.swap")
        finally:
            local.close()
        self.assertIs(remote.state, CommandState.REJECTED)
        self.assertEqual(remote.detail, expected.detail)
        self.assertIn("lacks access", remote.detail)
        self.assertEqual(api.command_result(remote.id), remote)

    def test_only_consumer_methods_can_be_called(self):
        api = self.connect()
        for method in ("close", "_close_consumer", "register_consumer", "__init__"):
            with self.subTest(method=method), self.assertRaises(CoreRequestError):
                api._call(method)
        with self.assertRaises(CoreRequestError) as caught:
            api._call("subscribe")
        self.assertIn("wrong arguments", str(caught.exception))
        with self.assertRaises(CoreRequestError):
            api._call("subscribe", object())  # not representable on the wire
        self.assertTrue(api.ping())

    def test_unsubscribe_lowers_simulator_demand(self):
        baseline = len(self.provider.active_demand)
        api = self.connect()
        subscription = api.subscribe_snapshot((ALTITUDE,), period=SnapshotPeriod.SIM_FRAME)
        self.assertTrue(wait_for(lambda: len(self.provider.active_demand) > baseline))
        api.unsubscribe(subscription.id)
        self.assertTrue(wait_for(lambda: len(self.provider.active_demand) == baseline))


class CommandAndGrantTests(IpcHarness):
    server_options = {"policy": PrincipalPolicy({"default": Grant(frozenset({"radios"}))})}

    def test_granted_command_is_sent_and_confirmed(self):
        api = self.connect(control_groups=("radios", "engine_controls"))
        self.assertEqual(api.granted_control_groups, {"radios"})
        self.assertEqual(api.denied_control_groups, {"engine_controls"})
        result = api.submit_command("radio.com1.swap")
        self.assertIsInstance(result, CommandResult)
        self.assertIs(result.state, CommandState.ACCEPTED)
        final = []

        def confirmed():
            for update in api.poll():
                if isinstance(update, CommandResult) and update.id == result.id and update.state is CommandState.CONFIRMED:
                    final.append(update)
            return final

        self.assertTrue(wait_for(confirmed), "the command outcome did not come back")
        self.assertIs(api.command_result(result.id).state, CommandState.CONFIRMED)
        refused = api.submit_command("aircraft.engine1.set_throttle", 50.0)
        self.assertIs(refused.state, CommandState.REJECTED)
        self.assertIn("lacks access", refused.detail)

    def test_unrequested_groups_are_not_granted(self):
        api = self.connect()
        self.assertEqual(api.granted_control_groups, frozenset())
        self.assertIs(api.submit_command("radio.com1.swap").state, CommandState.REJECTED)

    def test_policy_can_refuse_unknown_principals(self):
        other_key = secrets.token_bytes(32)
        server = CoreServer(self.core, authkeys={"default": self.key, "guest": other_key},
                            address=self.address + "2" if FAMILY == "AF_UNIX" else self.address + "-2",
                            policy=PrincipalPolicy({"default": Grant()}, default=None)).start()
        try:
            with self.assertRaises(HandshakeRejected) as caught:
                connect(server.address, authkey=other_key, principal="guest")
            self.assertEqual(caught.exception.code, wire.REJECT_ACCESS)
            with self.assertRaises(HandshakeRejected) as caught:
                connect(server.address, authkey=other_key, principal="nobody")
            self.assertEqual(caught.exception.code, wire.REJECT_AUTH)
            api = connect(server.address, authkey=self.key)
            api.close()
        finally:
            server.close()


class PushDeliveryTests(IpcHarness):
    def test_push_delivery_streams_updates(self):
        api = self.connect(delivery="push")
        self.assertEqual(api.delivery, "push")
        api.subscribe_snapshot((ALTITUDE,), period=SnapshotPeriod.SIM_FRAME)
        self.assertIsNotNone(poll_until(api, Snapshot))
        with self.assertRaises(CoreRequestError):
            api._call("poll", limit=4)

    def test_full_local_buffer_reports_a_gap(self):
        api = self.connect(delivery="push", buffer_size=2)
        api.subscribe_snapshot((ALTITUDE,), period=SnapshotPeriod.SIM_FRAME)
        self.assertTrue(wait_for(lambda: api._buffer_dropped >= 3))
        batch = api.poll()
        self.assertIsInstance(batch[0], Gap)
        self.assertIn("local interface buffer", batch[0].reason)
        self.assertGreaterEqual(batch[0].dropped_updates, 3)
        self.assertTrue(all(isinstance(item, Snapshot) for item in batch[1:]))


class CleanupTests(IpcHarness):
    def test_graceful_close_removes_consumer_and_demand(self):
        baseline = len(self.provider.active_demand)
        api = self.connect()
        api.subscribe_snapshot((ALTITUDE,), period=SnapshotPeriod.SIM_FRAME)
        self.assertTrue(wait_for(lambda: len(self.provider.active_demand) > baseline))
        self.assertEqual(len(self.server.clients()), 1)
        api.close()
        self.assertFalse(api.connected)
        self.assertTrue(wait_for(lambda: self.consumer_count() == 0))
        self.assertTrue(wait_for(lambda: len(self.provider.active_demand) == baseline))
        self.assertTrue(wait_for(lambda: self.server.clients() == ()))
        self.assertEqual(api.poll(), (), "a module that closed itself is not told it lost contact")
        with self.assertRaises(CoreUnavailableError):
            api.discover()

    def test_abrupt_disconnect_removes_consumer(self):
        baseline = len(self.provider.active_demand)
        connection, welcome = self.raw_session()
        wire.send_message(connection, {"type": wire.CALL, "id": 1, "method": "subscribe_snapshot",
                                       "args": [wire.encode((ALTITUDE,))], "kwargs": {
                                           "period": wire.encode(SnapshotPeriod.SIM_FRAME)}})
        self.assertEqual(wire.recv_message(connection, 1 << 20)["type"], wire.RESULT)
        self.assertTrue(wait_for(lambda: len(self.provider.active_demand) > baseline))
        connection.close()  # no bye
        self.assertTrue(wait_for(lambda: self.consumer_count() == 0))
        self.assertTrue(wait_for(lambda: len(self.provider.active_demand) == baseline))

    @unittest.skipIf(PROCESSES_BLOCKED, "the offline guard blocks child processes by design")
    def test_killed_client_process_is_cleaned_up(self):
        script = textwrap.dedent("""
            import os, sys
            from bbc_core.contract import SnapshotPeriod, ValueType
            from bbc_core.ipc import connect
            from bbc_core.sdk_simvar import SimVar
            api = connect(sys.argv[1], authkey=bytes.fromhex(os.environ["BBC_TEST_KEY"]), client_name="child")
            api.subscribe_snapshot((SimVar("PLANE ALTITUDE", "feet", ValueType.NUMBER),),
                                   period=SnapshotPeriod.SIM_FRAME)
            print("subscribed", api.client_id, flush=True)
            os._exit(3)  # die without saying goodbye
        """)
        environment = dict(os.environ, BBC_TEST_KEY=self.key.hex(), PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        baseline = len(self.provider.active_demand)
        completed = subprocess.run([sys.executable, "-c", script, self.address], cwd=ROOT, env=environment,
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertIn("subscribed", completed.stdout)
        self.assertTrue(wait_for(lambda: self.consumer_count() == 0))
        self.assertTrue(wait_for(lambda: len(self.provider.active_demand) == baseline))
        self.assertTrue(wait_for(lambda: self.server.clients() == ()))

    def test_protocol_violation_ends_only_that_client(self):
        good = self.connect()
        bad = self.connect()
        with bad._send_lock:
            bad._connection.send_bytes(b"\xffnot json")
        notice = poll_until(bad, Connection)
        self.assertIs(notice.state, ConnectionState.ERROR)
        self.assertIn("protocol error", notice.detail)
        self.assertIn("unknown", notice.detail)
        self.assertTrue(wait_for(lambda: self.consumer_count() == 1))
        self.assertIs(good.connection().state, ConnectionState.CONNECTED)

    def test_unexpected_message_type_is_fatal(self):
        connection, _ = self.raw_session()
        wire.send_message(connection, {"type": "teleport"})
        reply = wire.recv_message(connection, 1 << 20)
        self.assertEqual(reply["type"], wire.FATAL)
        self.assertTrue(wait_for(lambda: self.consumer_count() == 0))
        connection.close()


class ShutdownTests(IpcHarness):
    def test_server_shutdown_tells_clients_and_cleans_up(self):
        pull = self.connect()
        push = self.connect(delivery="push")
        pull.subscribe_snapshot((ALTITUDE,), period=SnapshotPeriod.SIM_FRAME)
        self.assertEqual(self.consumer_count(), 2)
        self.assertEqual(self.server.close("Maintenance test."), ())
        self.assertFalse(self.server.running)
        self.assertEqual(self.consumer_count(), 0)
        for api in (pull, push):
            with self.subTest(delivery=api.delivery):
                notice = poll_until(api, Connection)
                self.assertIsNotNone(notice)
                self.assertIs(notice.state, ConnectionState.ERROR)
                self.assertIn("Maintenance test.", notice.detail)
                self.assertIsNone(notice.session_id, "no simulator session may be claimed after loss")
                self.assertTrue(notice.source.simulated)
                self.assertEqual(api.poll(), (), "the loss is reported once")
                self.assertIs(api.connection(), notice)
                with self.assertRaises(CoreUnavailableError):
                    api.subscribe_snapshot((ALTITUDE,))
        with self.assertRaises(CoreUnavailableError):
            connect(self.address, authkey=self.key, timeout_s=1)
        if FAMILY == "AF_UNIX":
            self.assertFalse(os.path.exists(self.address), "the socket file must be removed")

    def test_close_is_idempotent_and_restartable_on_same_address(self):
        self.server.close()
        self.server.close()
        self.server = self.make_server().start()
        api = self.connect()
        self.assertTrue(api.ping())


class HandshakeTests(IpcHarness):
    server_options = {"handshake_timeout_s": 0.3}

    def assert_rejected(self, code, **options):
        before = self.consumer_count()
        with self.assertRaises(HandshakeRejected) as caught:
            connect(self.address, authkey=options.pop("authkey", self.key), timeout_s=2, **options)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.consumer_count(), before, "a refused client must not leave a consumer")
        return caught.exception

    def test_protocol_version_mismatch_is_rejected_with_reason(self):
        error = self.assert_rejected(wire.REJECT_VERSION, protocol_versions=(99,))
        self.assertEqual(error.server_protocol_versions, (1,))
        self.assertIn("99", error.reason)
        self.assertIn("1", error.reason)

    def test_api_version_mismatch_is_rejected_with_reason(self):
        error = self.assert_rejected(wire.REJECT_API, api_version="0.9-draft")
        self.assertIn("0.9-draft", error.reason)
        self.assertEqual(error.server_api_version, wire.API_VERSION)

    def test_schema_mismatch_is_rejected(self):
        self.assert_rejected(wire.REJECT_SCHEMA, schema="0" * 32)

    def test_bad_hello_fields_are_rejected(self):
        self.assert_rejected(wire.REJECT_BAD_REQUEST, queue_size=10 ** 6)
        self.assert_rejected(wire.REJECT_BAD_REQUEST, principal="")
        with self.assertRaises(ValueError):
            connect(self.address, authkey=self.key, delivery="carrier pigeon")

    def test_server_with_another_key_is_not_trusted(self):
        with self.assertRaises(ServerNotTrusted):
            connect(self.address, authkey=secrets.token_bytes(32), timeout_s=2)
        self.assertEqual(self.consumer_count(), 0)

    def test_client_without_valid_proof_is_refused(self):
        connection = Client(self.address, family=FAMILY)
        try:
            wire.send_message(connection, {
                "type": wire.HELLO, "protocol_versions": [1], "api_version": wire.API_VERSION,
                "schema": wire.schema_fingerprint(), "nonce": wire.b64(wire.new_nonce()),
            })
            self.assertEqual(wire.recv_message(connection, 1 << 16)["type"], wire.CHALLENGE)
            wire.send_message(connection, {"type": wire.AUTH, "proof": "00" * 32})
            reply = wire.recv_message(connection, 1 << 16)
            self.assertEqual((reply["type"], reply["code"]), (wire.REJECT, wire.REJECT_AUTH))
        finally:
            connection.close()
        self.assertEqual(self.consumer_count(), 0)

    def test_silent_client_times_out_and_server_keeps_serving(self):
        connection = Client(self.address, family=FAMILY)
        try:
            self.assertTrue(connection.poll(3))
            reply = wire.recv_message(connection, 1 << 16)
            self.assertEqual((reply["type"], reply["code"]), (wire.REJECT, wire.REJECT_TIMEOUT))
        finally:
            connection.close()
        self.assertTrue(self.connect().ping())

    def test_oversized_handshake_is_dropped(self):
        connection = Client(self.address, family=FAMILY)
        try:
            try:
                connection.send_bytes(b"x" * (wire.MAX_HANDSHAKE_BYTES + 1))
            except OSError:
                pass  # the server may already have hung up mid-send
            self.assertTrue(wait_for(lambda: self._closed(connection), timeout=3))
        finally:
            connection.close()
        self.assertTrue(self.connect().ping())

    @staticmethod
    def _closed(connection):
        try:
            if connection.poll(0.05):
                connection.recv_bytes(1 << 16)
            return False
        except (EOFError, OSError):
            return True


class CapacityTests(IpcHarness):
    server_options = {"max_clients": 1}

    def test_extra_client_is_told_the_server_is_busy(self):
        first = self.connect()
        with self.assertRaises(HandshakeRejected) as caught:
            connect(self.address, authkey=self.key, timeout_s=3)
        self.assertEqual(caught.exception.code, wire.REJECT_BUSY)
        first.close()
        self.assertTrue(wait_for(lambda: not self.server.clients()))
        self.assertTrue(self.connect().ping())

    def test_concurrent_calls_from_several_threads(self):
        api = self.connect()
        errors = []

        def worker():
            try:
                for _ in range(25):
                    self.assertIsInstance(api.discover(), Discovery)
                    api.poll()
            except Exception as error:  # noqa: BLE001 - collected for the assertion
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])


@unittest.skipIf(FAMILY != "AF_UNIX", "socket files only")
@unittest.skipIf(LOCAL_IPC_BLOCKED, GUARD_REASON)
class SocketFileTests(unittest.TestCase):
    def test_stale_socket_is_replaced_and_live_one_is_not(self):
        with tempfile.TemporaryDirectory(prefix="bbcipc") as folder:
            address = os.path.join(folder, "s.sock")
            stale = socket.socket(socket.AF_UNIX)
            stale.bind(address)
            stale.close()  # leaves the file behind, as a crashed Core would
            core = Core(SimulatedProvider())
            key = secrets.token_bytes(32)
            server = CoreServer(core, authkeys=key, address=address).start()
            try:
                self.assertEqual(os.stat(address).st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    CoreServer(core, authkeys=key, address=address).start()
            finally:
                server.close()
            Path(address).write_text("not a socket")
            with self.assertRaises(FileExistsError):
                CoreServer(core, authkeys=key, address=address).start()

    def test_server_refuses_non_local_families_and_short_keys(self):
        core = Core(SimulatedProvider())
        with self.assertRaises(ValueError):
            CoreServer(core, authkeys=secrets.token_bytes(32), family="AF_INET")
        with self.assertRaises(ValueError):
            CoreServer(core, authkeys=b"short")


class _IdleConnection:
    """A transport stand-in for RemoteConsumerAPI: nothing arrives until it is closed."""

    def __init__(self):
        self._closed = threading.Event()
        self.sent = []

    def recv_bytes(self, maxlength=None):
        self._closed.wait(10)
        raise EOFError

    def send_bytes(self, data):
        if self._closed.is_set():
            raise OSError("closed")
        self.sent.append(data)
        if wire.loads(data)["type"] == wire.BYE:
            self.close()  # the peer hangs up after a goodbye, as the server does

    def close(self):
        self._closed.set()


class PushLaneTests(unittest.TestCase):
    """The push buffer keeps Core's two lanes: samples never push status updates out."""

    def make(self, buffer_size):
        api = ipc_client.RemoteConsumerAPI(_IdleConnection(), {
            "protocol_version": 1, "api_version": wire.API_VERSION, "delivery": wire.DELIVERY_PUSH,
        }, buffer_size=buffer_size, call_timeout_s=0.2)
        self.addCleanup(api.close)
        return api

    @staticmethod
    def snapshot(number):
        return Snapshot(f"sub-{number}", ("PLANE ALTITUDE",), (float(number),), 1.0, 2.0, 1.0,
                        Source("Test", simulated=True), "s", NOW)

    def test_sample_burst_keeps_status_updates_in_order(self):
        api = self.make(buffer_size=2)
        connected = Connection(ConnectionState.CONNECTED, Source("Test", simulated=True), "s", NOW)
        result = CommandResult("c1", "radio.com1.swap", CommandState.CONFIRMED, Source("Test", True), "s")
        ended = SubscriptionEnded("sub-x", "input.y", "The aircraft inputs changed.")
        error = SignalError(1, "SimConnect exception", "aircraft.title", "sub-9")
        stream = [connected] + [self.snapshot(n) for n in range(5)] + [result, error, ended, self.snapshot(9)]
        api._buffer_updates([wire.encode(item) for item in stream])
        batch = api.poll(limit=100)
        self.assertIsInstance(batch[0], Gap)
        self.assertIn("local interface buffer", batch[0].reason)
        self.assertEqual(batch[0].dropped_updates, 5, "five samples were dropped from the data lane")
        rest = batch[1:]
        self.assertEqual(rest[:1], (connected,), "the connection change survived the burst")
        self.assertIn(result, rest)
        self.assertIn(ended, rest)
        status = [item for item in rest if not isinstance(item, ipc_client.DATA_LANE_TYPES)]
        self.assertEqual(status, [connected, result, ended], "status updates keep their arrival order")
        samples = [item for item in rest if isinstance(item, ipc_client.DATA_LANE_TYPES)]
        self.assertEqual(samples, [error, self.snapshot(9)], "the newest samples are kept, SignalError with them")
        self.assertLess(rest.index(result), rest.index(samples[-1]), "updates come back in arrival order")

    def test_status_lane_overflow_is_reported_separately(self):
        api = self.make(buffer_size=2)
        room = ipc_client.MIN_STATUS_BUFFER
        results = [CommandResult(f"c{n}", "radio.com1.swap", CommandState.SENT, Source("Test", True), "s")
                   for n in range(room + 3)]
        api._buffer_updates([wire.encode(item) for item in results])
        batch = api.poll(limit=4096)
        self.assertIsInstance(batch[0], Gap)
        self.assertIn("status buffer", batch[0].reason)
        self.assertEqual(batch[0].dropped_updates, 3)
        self.assertEqual(batch[1:], tuple(results[3:]))

    def test_poll_respects_limit_and_leaves_the_rest(self):
        api = self.make(buffer_size=10)
        api._buffer_updates([wire.encode(self.snapshot(n)) for n in range(5)])
        self.assertEqual(len(api.poll(limit=3)), 3)
        self.assertEqual(api.buffered, 2)
        self.assertEqual(len(api.poll(limit=3)), 2)

    def test_bad_buffer_size_is_refused_before_connecting(self):
        with self.assertRaises(ValueError):
            connect("unused-address", authkey=secrets.token_bytes(32), buffer_size=0)


class GuardDetectionTests(unittest.TestCase):
    def test_local_ipc_allowance_is_read_from_the_guard_rule(self):
        self.assertTrue(_guard_allows_local_ipc(None))
        if FAMILY == "AF_UNIX":
            self.assertFalse(_guard_allows_local_ipc(lambda event, args: event.startswith("socket.")))
            allow_unix = (lambda event, args: event.startswith("socket.")
                          and getattr(args[0], "family", None) != socket.AF_UNIX)
            self.assertTrue(_guard_allows_local_ipc(allow_unix))


class NamedHost:
    """A Core host offering only named consumers (api/release), as CoreHost did before C4."""

    def __init__(self, core):
        self.core = core
        self.names = {}

    def api(self, name, control_groups=(), diagnostics=False, *, queue_size=256):
        if name in self.names:
            raise ValueError("duplicate")
        self.names[name] = self.core.register_consumer(
            control_groups=control_groups, diagnostics=diagnostics, queue_size=queue_size)
        return self.names[name]

    def release(self, name):
        api = self.names.pop(name, None)
        if api is not None:
            api.close()
        return api is not None


class NamedHostServingTests(IpcHarness):
    def make_server(self, **options):
        self.named = NamedHost(self.core)
        options.setdefault("authkeys", self.key)
        options.setdefault("address", self.address)
        return CoreServer(self.named, **options)

    def test_named_only_host_gets_a_unique_released_name_per_client(self):
        first, second = self.connect(), self.connect()
        self.assertEqual(len(self.named.names), 2)
        self.assertIs(first.connection().state, ConnectionState.CONNECTED)
        first.close()
        second.close()
        self.assertTrue(wait_for(lambda: not self.named.names), "leaving clients release their names")
        self.assertEqual(self.consumer_count(), 0)


class FailingRegistrarTests(IpcHarness):
    def make_server(self, **options):
        class Stopped:
            def register_consumer(self, **kwargs):
                raise RuntimeError("The Core host has stopped.")

        options.setdefault("authkeys", self.key)
        options.setdefault("address", self.address)
        return CoreServer(Stopped(), **options)

    def test_host_failure_is_a_registration_refusal_not_a_crash(self):
        with self.assertRaises(HandshakeRejected) as caught:
            connect(self.address, authkey=self.key, timeout_s=2)
        self.assertEqual(caught.exception.code, wire.REJECT_REGISTRATION)
        self.assertIn("has stopped", caught.exception.reason)
        self.assertTrue(self.server.running, "the server keeps serving other clients")


class RegistrarAdapterTests(unittest.TestCase):
    def test_registrar_adapter_accepts_cores_and_hosts_and_refuses_others(self):
        from bbc_core.ipc import as_registrar

        core = Core(SimulatedProvider())
        self.assertIs(as_registrar(core), core)
        named = as_registrar(NamedHost(core))
        api = named.register_consumer(control_groups=("radios",), queue_size=8)
        self.assertEqual(len(core._consumers), 1)
        self.assertIs(api.initial_connection.state, ConnectionState.DISCONNECTED, "attributes are forwarded")
        api.close()
        api.close()
        self.assertEqual(len(core._consumers), 0)
        with self.assertRaises(TypeError):
            as_registrar(object())
        with self.assertRaises(TypeError):
            CoreServer(object(), authkeys=secrets.token_bytes(32), address="unused")


class CoreHostServingTests(unittest.TestCase):
    """CoreServer serving the real CoreHost (C4 register_consumer); the host pumps Core."""

    def setUp(self):
        if LOCAL_IPC_BLOCKED:
            self.skipTest(GUARD_REASON)
        from bbc_core.host import CoreHost, HostConfig

        self.folder = tempfile.TemporaryDirectory(prefix="bbcipc")
        self.addCleanup(self.folder.cleanup)
        self.address = (rf"\\.\pipe\bbc-core-test-{secrets.token_hex(6)}" if FAMILY == "AF_PIPE"
                        else os.path.join(self.folder.name, "h.sock"))
        self.key = secrets.token_bytes(32)
        self.host = CoreHost(SimulatedProvider(minimum_interval_ms=50), config=HostConfig(
            pump_interval_s=0.01, idle_tick_s=0.05, backoff_s=(0.05, 0.1, 0.2), join_timeout_s=2.0))
        self.host.start()
        self.addCleanup(self.host.stop)
        self.assertTrue(wait_for(lambda: self.host.status().connected))
        self.server = CoreServer(self.host, authkeys=self.key, address=self.address).start()
        self.addCleanup(self.server.close)

    def consumers(self):
        result = self.host.call(lambda core: len(core._consumers))
        return result.result(5) if hasattr(result, "result") else result

    def test_snapshots_flow_without_a_pump_thread(self):
        baseline = self.consumers()
        api = connect(self.address, authkey=self.key)
        self.addCleanup(api.close)
        self.assertEqual(self.consumers(), baseline + 1)
        api.subscribe_snapshot((ALTITUDE,), period=SnapshotPeriod.SIM_FRAME)
        self.assertIsInstance(poll_until(api, Snapshot), Snapshot)
        api.close()
        self.assertTrue(wait_for(lambda: self.consumers() == baseline))


if __name__ == "__main__":
    unittest.main()
