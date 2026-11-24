"""Guarded offline test runner and its own checks.

The offline test reports under reference/ describe an audit-hook guard that
blocks native DLL loads, sockets and subprocesses while the suite runs. This file
keeps that guard in the repository so the "zero guard violations" result can be
reproduced:

    python -B tests/test_repo_offline_guard.py

The runner discovers tests/test_*.py with the repository root as the top-level
folder (the same as ``python -m unittest discover -s tests -t .``), so test
modules are imported as ``tests.test_*``. It prints the guard violation count and
exits non-zero if any test fails or the guard blocked anything. Audit hooks
cannot be removed once installed, so the hook is only installed by main(), never
on import. Under plain ``python -m unittest`` this module only runs the checks
below.

While the guard is active the runner sets ``BBC_OFFLINE_GUARD=1``. The few tests
that must start a subprocess (for example a real ``git ls-files``) check
:func:`guard_active` and skip, so they still run under the plain command.
"""

from __future__ import annotations

import faulthandler
import os
import pathlib
import struct
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT_S = 600.0
GUARD_ENVIRONMENT_VARIABLE = "BBC_OFFLINE_GUARD"


def guard_active() -> bool:
    """True while the guarded runner is running the suite in this process."""

    return os.environ.get(GUARD_ENVIRONMENT_VARIABLE) == "1"


def discover_suite(pattern: str = "test_*.py") -> unittest.TestSuite:
    """Discover tests/ the way ``python -m unittest discover -s tests -t .`` does."""

    return unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=pattern, top_level_dir=str(ROOT))


def offline_guard_violation(event: str, args: tuple) -> bool:
    """Return True when an audit event would break the offline promise."""

    if event == "ctypes.dlopen":
        # Importing ctypes on Linux and macOS opens the running interpreter
        # itself (name None). Loading any named library, such as SimConnect.dll,
        # is blocked.
        return bool(args) and args[0] is not None
    if event.startswith(("socket.", "subprocess.", "os.spawn", "os.exec", "os.posix_spawn")):
        return True
    if event in ("os.system", "os.startfile", "os.fork", "os.forkpty", "_winapi.CreateProcess"):
        return True
    return event == "import" and bool(args) and str(args[0]).split(".")[0] == "simconnect_native"


def main(argv: list[str] | None = None) -> int:
    blocked: list[str] = []

    def guard(event, args):
        if offline_guard_violation(event, args):
            blocked.append(event)
            raise RuntimeError("Offline guard blocked " + event)

    sys.addaudithook(guard)
    os.environ[GUARD_ENVIRONMENT_VARIABLE] = "1"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    print(f"CPython {sys.version.split()[0]} {struct.calcsize('P') * 8}-bit on {sys.platform}", flush=True)
    timeout = float(os.environ.get("BBC_OFFLINE_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    faulthandler.dump_traceback_later(timeout, exit=True)
    try:
        pattern = (argv or sys.argv[1:] or ["test_*.py"])[0]
        suite = discover_suite(pattern)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        faulthandler.cancel_dump_traceback_later()
    print(f"Offline guard violations: {len(blocked)}", flush=True)
    if blocked:
        print("Blocked events: " + ", ".join(sorted(set(blocked))), flush=True)
    return 0 if result.testsRun and result.wasSuccessful() and not blocked else 1


class OfflineGuardTests(unittest.TestCase):
    def test_named_library_loads_are_blocked_but_interpreter_handle_is_allowed(self):
        self.assertTrue(offline_guard_violation("ctypes.dlopen", (r"C:\MSFS SDK\SimConnect.dll",)))
        self.assertTrue(offline_guard_violation("ctypes.dlopen", ("libc.so.6",)))
        self.assertFalse(offline_guard_violation("ctypes.dlopen", (None,)))

    def test_network_and_process_creation_are_blocked(self):
        for event in ("socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system",
                      "os.spawn", "os.exec", "os.posix_spawn", "_winapi.CreateProcess", "os.startfile"):
            with self.subTest(event=event):
                self.assertTrue(offline_guard_violation(event, ()))

    def test_former_wrapper_import_is_blocked_and_ordinary_imports_are_not(self):
        self.assertTrue(offline_guard_violation("import", ("simconnect_native.client",) + (None,) * 4))
        self.assertFalse(offline_guard_violation("import", ("bbc_core.core",) + (None,) * 4))
        self.assertFalse(offline_guard_violation("open", ("notes.txt", "r", 0)))

    def test_runner_discovers_every_test_module(self):
        names = {path.name for path in (ROOT / "tests").glob("test_*.py")}
        self.assertIn(pathlib.Path(__file__).name, names)
        self.assertTrue(any(name.endswith("_offline.py") for name in names))

    def test_runner_uses_the_repository_root_as_top_level_like_dash_t_dot(self):
        suite = discover_suite("test_repo_offline_guard.py")

        def ids(item):
            if isinstance(item, unittest.TestSuite):
                for child in item:
                    yield from ids(child)
            else:
                yield item.id()

        found = list(ids(suite))
        self.assertTrue(found)
        self.assertTrue(all(name.startswith("tests.test_repo_offline_guard.") for name in found), found[:3])
        self.assertTrue((ROOT / "tests" / "__init__.py").is_file(), "-t . needs tests/ to be a package")

    def test_guard_flag_is_read_from_the_environment(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {GUARD_ENVIRONMENT_VARIABLE: "1"}):
            self.assertTrue(guard_active())
        with mock.patch.dict(os.environ, {GUARD_ENVIRONMENT_VARIABLE: ""}):
            self.assertFalse(guard_active())


if __name__ == "__main__":
    raise SystemExit(main())
