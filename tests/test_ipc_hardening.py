"""Regression tests for the experimental IPC key file and the Windows pipe listener."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from bbc_core.ipc import protocol
from bbc_core.ipc import server as ipc_server
from bbc_core.ipc.protocol import MIN_AUTHKEY_BYTES, load_or_create_authkey


class AuthkeyFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "keys" / "core-ipc.key"

    def test_first_start_creates_a_private_full_length_key_and_reuses_it(self):
        key = load_or_create_authkey(self.path)
        self.assertEqual(len(key), MIN_AUTHKEY_BYTES)
        self.assertEqual(load_or_create_authkey(self.path), key)
        if sys.platform != "win32":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [], "no temporary file is left behind")

    def test_empty_key_left_by_an_interrupted_first_start_is_replaced(self):
        self.path.parent.mkdir(parents=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        key = load_or_create_authkey(self.path, wait_s=0.05)
        self.assertEqual(len(key), MIN_AUTHKEY_BYTES)
        self.assertEqual(self.path.read_bytes(), key)
        self.assertEqual(load_or_create_authkey(self.path, wait_s=0.05), key, "the repair is permanent")
        broken = [p for p in self.path.parent.iterdir() if p.name.endswith(".broken")]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0].read_bytes(), b"")

    def test_key_file_never_appears_half_written_to_a_concurrent_start(self):
        started = threading.Event()
        release = threading.Event()
        real = secrets.token_bytes
        results = {}

        def slow_token_bytes(count):
            if threading.current_thread().name == "creator":
                started.set()
                release.wait(5)
            return real(count)

        def creator():
            results["creator"] = load_or_create_authkey(self.path)

        with mock.patch.object(protocol.secrets, "token_bytes", slow_token_bytes):
            thread = threading.Thread(target=creator, name="creator")
            thread.start()
            self.assertTrue(started.wait(5))
            # The creator is stuck before writing: a second start must not see an empty key.
            results["second"] = load_or_create_authkey(self.path, wait_s=0)
            release.set()
            thread.join(5)
        self.assertEqual(len(results["second"]), MIN_AUTHKEY_BYTES)
        self.assertEqual(results["creator"], results["second"], "both starts use the one key that won")
        self.assertEqual(self.path.read_bytes(), results["second"])


class _FakeWinapi:
    PIPE_ACCESS_DUPLEX = 0x3
    FILE_FLAG_OVERLAPPED = 0x40000000
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x80000
    PIPE_TYPE_MESSAGE = 0x4
    PIPE_READMODE_MESSAGE = 0x2
    PIPE_WAIT = 0x0
    PIPE_UNLIMITED_INSTANCES = 255
    NMPWAIT_WAIT_FOREVER = 0xFFFFFFFF
    NULL = 0

    def __init__(self):
        self.calls = []

    def CreateNamedPipe(self, *args):  # noqa: N802 - Windows API name
        self.calls.append(args)
        return len(self.calls)


class _FakePipeListener:
    def __init__(self, address, backlog=None):
        self._address = address
        self._handle_queue = [self._new_handle(first=True)]

    def _new_handle(self, first=False):  # pragma: no cover - replaced by the subclass
        raise AssertionError("the stock handle factory must not be used")


class PipeListenerTests(unittest.TestCase):
    def test_every_pipe_instance_rejects_remote_clients(self):
        winapi = _FakeWinapi()
        connection = types.SimpleNamespace(PipeListener=_FakePipeListener, BUFSIZE=8192)
        listener = ipc_server.open_listener(r"\\.\pipe\bbc-core-test-p1", "AF_PIPE", 16,
                                            connection_module=connection, winapi=winapi)
        inner = listener._listener
        inner._new_handle()  # what accept() does for the next instance
        self.assertEqual(len(winapi.calls), 2)
        for index, call in enumerate(winapi.calls):
            address, open_mode, pipe_mode = call[:3]
            self.assertEqual(address, r"\\.\pipe\bbc-core-test-p1")
            self.assertTrue(pipe_mode & ipc_server.PIPE_REJECT_REMOTE_CLIENTS, "remote clients must be refused")
            self.assertTrue(pipe_mode & winapi.PIPE_TYPE_MESSAGE)
            self.assertEqual(bool(open_mode & winapi.FILE_FLAG_FIRST_PIPE_INSTANCE), index == 0)
            self.assertEqual(call[4:6], (8192, 8192))
        self.assertIsNone(listener._authkey, "the handshake runs per client, never in accept()")
        self.assertEqual(ipc_server.PIPE_REJECT_REMOTE_CLIENTS, 0x8)

    def test_socket_files_still_use_the_stock_listener(self):
        with mock.patch.object(ipc_server, "Listener") as stock:
            ipc_server.open_listener("/tmp/x.sock", "AF_UNIX", 4)
        stock.assert_called_once_with("/tmp/x.sock", family="AF_UNIX", backlog=4)

    def test_docstrings_no_longer_overstate_the_isolation(self):
        import bbc_core.ipc as package
        self.assertIn("PIPE_REJECT_REMOTE_CLIENTS", package.__doc__)
        self.assertIn("PIPE_REJECT_REMOTE_CLIENTS", ipc_server.__doc__)
        self.assertNotIn("local-only", package.__doc__)



class PipeSecurityDocumentationTests(unittest.TestCase):
    """The IPC guide states the pipe's real security: remote clients refused, default DACL."""

    def test_the_guide_names_the_remote_client_flag_and_the_default_dacl(self):
        guide = (Path(__file__).resolve().parents[1] / "docs" / "STANDALONE_CORE_IPC.md").read_text(encoding="utf-8")
        flattened = " ".join(guide.split())
        self.assertIn("PIPE_REJECT_REMOTE_CLIENTS", flattened)
        self.assertIn("default DACL (not owner-only)", flattened)
        self.assertIn("stdlib default, which allows other local accounts to *open* the pipe", flattened)
        # The flag the guide names is the one the server really adds.
        self.assertEqual(ipc_server.PIPE_REJECT_REMOTE_CLIENTS, 0x00000008)


if __name__ == "__main__":
    unittest.main()
