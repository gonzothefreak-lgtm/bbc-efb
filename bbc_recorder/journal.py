"""Crash-safe, append-only JSONL journal for a flight that is being recorded.

The Recorder writes one journal per flight while it records. The journal is the
durable evidence of what was observed: if BBC EFB, Windows or the PC stops in the
middle of a flight, everything up to the last written line survives and startup
recovery (``bbc_recorder.recovery``) can turn it into an honest, interrupted flight.

This module has no Qt and no SimConnect dependency.

File layout (``JournalLayout``; every folder name is configurable)::

    <data folder>/recorder/in-progress/<flight_id>.jsonl   journal being written
    <data folder>/recorder/in-progress/<flight_id>.lock    held while a writer has it open
    <data folder>/recorder/journals/<YYYY-MM>/<flight_id>.jsonl[.gz]   closed, kept as evidence
    <data folder>/recorder/discarded/<flight_id>.jsonl     discarded by the pilot, never deleted

Line format. Every line is one UTF-8 JSON object ending in ``\\n``. The last key of
every object is ``"crc"``, the CRC-32 (8 lower-case hex digits) of the line's own
bytes with the ``,"crc":"…"`` member removed. A reader can therefore tell a line that
was cut short, zero-filled after a power cut or damaged on disk from a good line,
without trusting that "it parsed as JSON". Lines are::

    {"seq":0,"type":"header","schema":"bbc-efb.recorder-journal","version":1,
     "flight_id":…,"app_version":…,"created_host_time":…,"started_host_time":…,
     "started_sim_time_s":…,"started_sim_utc":…,"aircraft":{…}|null,
     "source":{…}|null,"rules_version":…|null,"extra":{…},"crc":"…"}
    {"seq":1,"type":"event","event":<RecorderEvent.to_json()>,"crc":"…"}
    …
    {"seq":n,"type":"footer","outcome":"complete|abandoned|interrupted|discarded",
     "closed_host_time":…,"ended_host_time":…|null,"ended_sim_time_s":…|null,
     "ended_sim_utc":…|null,"final_phase":…|null,"event_count":…,"detail":…,
     "lost_lines":{"count":…,"seqs":[…],"kinds":{…},"last_error":…} (only when a write failed),
     "extra":{…},"crc":"…"}

``seq`` increases by one per line so a reader can report missing lines.

Durability rules:

* The journal is opened once, append-only (``O_APPEND``), and held open. It is never
  rewritten, truncated or renamed while open (Windows cannot rename an open file).
* Every line is handed to the operating system with one unbuffered ``os.write`` loop,
  so a crash of BBC EFB itself loses nothing that ``append`` returned for.
* ``os.fsync`` (``FlushFileBuffers`` on Windows) runs when the header is written, on
  every phase change and on important events (``FsyncPolicy.on_kinds``), on close,
  and otherwise at most once every ``FsyncPolicy.interval_s`` seconds. A power cut
  can therefore lose at most the last ``interval_s`` seconds of track samples, never
  a phase change. Hosts should call ``Journal.tick()`` about once a second so the
  final samples before a quiet period are also synced.
* A write that fails part-way (disk full, device removed) leaves a partial line. The
  journal remembers this and starts the next line with ``\\n`` so the damage stays
  confined to one line the reader reports. A write that fails before any byte reaches
  the file (or a line refused as too large) and is not retried still uses up its ``seq``,
  so the reader reports the line as missing, and the journal counts it:
  ``Journal.lost_lines`` and the footer's ``lost_lines`` block. Nothing is ever lost silently.
* Memory is bounded: the writer keeps counters only, never the flight's events.

Locking. A writer holds an exclusive OS lock on a sidecar ``<flight_id>.lock`` file
(``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere). Recovery skips journals
whose lock is held, so a second BBC EFB instance never "recovers" a flight that is
still being recorded. The lock is released (and the lock file removed) on close, and
by the operating system if the process dies.

PROPOSALS for the owner (defaults chosen here, all configurable):

* P-J1 Location: journals live under ``<data folder>/recorder/`` (``in-progress/``,
  ``journals/``, ``discarded/``) next to the Logbook records, so Backup and Export All
  Data pick up the evidence. ``JournalLayout`` changes any of the names.
* P-J2 fsync cadence: at most every 2 s plus every phase change and important event.
  Cost is a handful of syncs per minute; worst-case loss after a power cut is 2 s of
  1 Hz samples.
* P-J3 Closed journals are kept forever as evidence in ``journals/<YYYY-MM>/``.
  ``compress_archived`` (gzip, verified before the original is removed) is provided
  but is never run automatically; nothing in this module deletes a journal.
* P-J4 Non-finite numbers (NaN, infinity) from the simulator are stored as ``null``
  (unknown) and the line is marked ``"sanitized"`` rather than written as invalid JSON.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, BinaryIO, Callable, Iterator, Mapping

import zlib

from bbc_recorder.model import EventKind, Phase, RecorderEvent

__all__ = [
    "JOURNAL_SCHEMA", "JOURNAL_VERSION", "MAX_LINE_BYTES", "MAX_NOTABLE_EVENTS", "PROTECTED_KINDS",
    "Outcome", "IssueKind", "LineType",
    "JournalError", "JournalLockedError", "JournalClosedError", "JournalWriteError",
    "JournalLayout", "FsyncPolicy", "DEFAULT_SYNC_KINDS",
    "Journal", "JournalIssue", "JournalReadResult",
    "read_journal", "iter_journal_events", "is_locked",
    "new_flight_id", "validate_flight_id",
    "archive_journal", "archive_target", "list_archived", "find_archived",
    "compress_archived", "file_sha256",
]

JOURNAL_SCHEMA = "bbc-efb.recorder-journal"
JOURNAL_VERSION = 1
JOURNAL_SUFFIX = ".jsonl"
LOCK_SUFFIX = ".lock"
MAX_LINE_BYTES = 1 << 20          # writer refuses a single line larger than 1 MiB
READ_LINE_LIMIT = 4 << 20         # reader treats anything longer as a damaged line
MAX_REPORTED_ISSUES = 200         # issues kept in a read result; the total is still counted
MAX_NOTABLE_EVENTS = 20_000       # bounded non-sample events (notes, pauses) kept when events are not loaded
MAX_LOST_SEQS = 100               # lost-line seqs a journal lists in its footer; the total is still counted

# Flight evidence kinds a reader always keeps in ``notable_events``, however many notes a
# journal holds: the record's milestones, gaps and end summary are built from these.
PROTECTED_KINDS = frozenset({
    EventKind.FLIGHT_STARTED, EventKind.PHASE_CHANGED, EventKind.BLOCK_OFF, EventKind.TAKEOFF,
    EventKind.TOUCHDOWN, EventKind.BOUNCE, EventKind.BLOCK_ON, EventKind.GAP, EventKind.AIRCRAFT_CHANGED,
    EventKind.CRASHED, EventKind.MANUAL_START, EventKind.MANUAL_STOP, EventKind.FLIGHT_ENDED,
    EventKind.ATTRIBUTION,
})

_IS_WINDOWS = sys.platform == "win32"
_CRC_TAIL = re.compile(rb',"crc":"([0-9a-f]{8})"\}$')
_FLIGHT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)} | {f"LPT{i}" for i in range(10)}
)


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value

    __format__ = str.__format__


class Outcome(_ValueEnum):
    """How a journal was closed (the footer's ``outcome``)."""

    COMPLETE = "complete"          # the Recorder ended the flight normally
    ABANDONED = "abandoned"        # the Recorder closed it without a normal arrival
    INTERRUPTED = "interrupted"    # closed by recovery after the application stopped
    DISCARDED = "discarded"        # the pilot chose to discard it (file is kept)


class LineType(_ValueEnum):
    HEADER = "header"
    EVENT = "event"
    FOOTER = "footer"


class IssueKind(_ValueEnum):
    """Problems a tolerant read can report. None of them stop the read."""

    TORN_TAIL = "torn_tail"            # final line incomplete (crash or power cut mid-write)
    ZERO_FILL = "zero_fill"            # run of NUL bytes (file grew but data never reached disk)
    BAD_UTF8 = "bad_utf8"              # line is not valid UTF-8
    CHECKSUM = "checksum"              # missing or wrong crc: damaged or hand-edited line
    CORRUPT = "corrupt"                # checksum fine but content unusable
    TOO_LONG = "too_long"              # line longer than the reader's limit
    MISSING_LINES = "missing_lines"    # seq jumped: lines are missing
    OUT_OF_ORDER = "out_of_order"      # seq went backwards or repeated
    NO_HEADER = "no_header"            # first good line was not a header
    NEWER_SCHEMA = "newer_schema"      # written by a newer BBC EFB
    AFTER_FOOTER = "after_footer"      # data after the closing line
    UNKNOWN_TYPE = "unknown_type"      # a line type this version does not know


class JournalError(Exception):
    """A journal operation failed. ``str(error)`` is a plain-English message."""


class JournalLockedError(JournalError):
    """Another writer (this or another BBC EFB instance) holds the journal."""


class JournalClosedError(JournalError):
    """The journal was already closed."""


class JournalWriteError(JournalError):
    """The operating system refused a write or sync; ``__cause__`` has the OSError."""


# --------------------------------------------------------------------------- layout


@dataclass(frozen=True, slots=True)
class JournalLayout:
    """Where journals live inside the pilot's data folder."""

    data_dir: Path
    root: str = "recorder"
    in_progress: str = "in-progress"
    archive: str = "journals"
    discarded: str = "discarded"

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir))

    @classmethod
    def of(cls, where: "JournalLayout | Path | str") -> "JournalLayout":
        return where if isinstance(where, JournalLayout) else cls(Path(where))

    @property
    def base(self) -> Path:
        return self.data_dir / self.root if self.root else self.data_dir

    @property
    def in_progress_dir(self) -> Path:
        return self.base / self.in_progress

    @property
    def archive_dir(self) -> Path:
        return self.base / self.archive

    @property
    def discarded_dir(self) -> Path:
        return self.base / self.discarded

    def journal_path(self, flight_id: str) -> Path:
        return self.in_progress_dir / f"{validate_flight_id(flight_id)}{JOURNAL_SUFFIX}"

    def lock_path(self, flight_id: str) -> Path:
        return self.in_progress_dir / f"{validate_flight_id(flight_id)}{LOCK_SUFFIX}"

    def relative(self, path: Path) -> str:
        """``path`` relative to the data folder, with forward slashes (for records)."""
        try:
            return PurePath(os.path.relpath(path, self.data_dir)).as_posix()
        except ValueError:  # different drive on Windows
            return Path(path).as_posix()


def validate_flight_id(flight_id: str) -> str:
    """Return ``flight_id`` if it is safe as a file name on Windows and elsewhere."""
    if not isinstance(flight_id, str) or not _FLIGHT_ID.fullmatch(flight_id):
        raise JournalError(
            f"Flight id {flight_id!r} cannot be used as a journal name. Use 1-80 letters, "
            "digits, '-' or '_', starting with a letter or digit."
        )
    if flight_id.upper() in _WINDOWS_RESERVED:
        raise JournalError(f"Flight id {flight_id!r} is a reserved name on Windows.")
    return flight_id


def new_flight_id(now: datetime | None = None) -> str:
    """A sortable, unique flight id such as ``20261124T101500Z-1f3a9c0d2b7e``."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{moment:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- policy

DEFAULT_SYNC_KINDS: frozenset[EventKind] = frozenset({
    EventKind.FLIGHT_STARTED, EventKind.PHASE_CHANGED, EventKind.BLOCK_OFF,
    EventKind.TAKEOFF, EventKind.TOUCHDOWN, EventKind.BOUNCE, EventKind.BLOCK_ON,
    EventKind.PAUSED, EventKind.RESUMED, EventKind.GAP, EventKind.AIRCRAFT_CHANGED,
    EventKind.POSITION_JUMP, EventKind.CRASHED, EventKind.MANUAL_START,
    EventKind.MANUAL_STOP, EventKind.FLIGHT_ENDED,
})


@dataclass(frozen=True, slots=True)
class FsyncPolicy:
    """When the journal forces data to disk (see PROPOSAL P-J2).

    ``interval_s``: the longest time unsynced lines may wait; ``None`` syncs only on
    phase changes, ``on_kinds`` and close. ``on_phase_change``: sync when an event's
    phase differs from the previous event's. ``on_kinds``: events that always sync.
    """

    interval_s: float | None = 2.0
    on_phase_change: bool = True
    on_kinds: frozenset[EventKind] = DEFAULT_SYNC_KINDS

    def __post_init__(self) -> None:
        if self.interval_s is not None and not (self.interval_s >= 0):
            raise ValueError("FsyncPolicy.interval_s must be zero or more seconds, or None.")
        object.__setattr__(self, "on_kinds", frozenset(EventKind(k) for k in self.on_kinds))


# --------------------------------------------------------------------------- encoding


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, set | frozenset):
        return sorted(value, key=repr)
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Journal cannot store a {type(value).__name__} value: {value!r:.80}")


def _sanitize(value: Any, depth: int = 0) -> Any:
    """Copy of ``value`` with non-finite floats replaced by None (unknown)."""
    if depth > 64:
        raise JournalError("Journal data is nested too deeply (or refers to itself).")
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: _sanitize(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(v, depth + 1) for v in value]
    if isinstance(value, str | int | bool) or value is None:
        return value
    return _sanitize(_json_default(value), depth + 1)


def _dumps(obj: Any, ascii_only: bool) -> str:
    return json.dumps(obj, ensure_ascii=ascii_only, separators=(",", ":"),
                      allow_nan=False, default=_json_default)


def _encode_line(obj: dict[str, Any]) -> bytes:
    """One journal line: compact JSON with a trailing crc member and ``\\n``."""
    try:
        body = _dumps(obj, False)
    except ValueError as error:
        if "Out of range float" not in str(error):
            raise JournalError(f"Journal data could not be encoded: {error}") from error
        obj = dict(_sanitize(obj))
        obj["sanitized"] = "non-finite numbers stored as null"
        body = _dumps(obj, False)
    try:
        raw = body.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates: escape them so the line stays valid UTF-8
        raw = _dumps(obj, True).encode("ascii")
    if not raw.endswith(b"}") or raw == b"{}":
        raise JournalError("Journal lines must be non-empty JSON objects.")
    line = raw[:-1] + b',"crc":"%08x"}\n' % zlib.crc32(raw)
    if len(line) > MAX_LINE_BYTES:
        raise JournalError(
            f"A journal line would be {len(line)} bytes, more than the {MAX_LINE_BYTES} byte limit."
        )
    return line


# --------------------------------------------------------------------------- locking


class _FileLock:
    """Exclusive, non-blocking OS lock on a sidecar file. Released by the OS on exit."""

    __slots__ = ("path", "_fd")

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "_FileLock":
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        for _attempt in range(10):
            fd = os.open(self.path, flags, 0o644)
            if not _try_lock(fd):
                os.close(fd)
                raise JournalLockedError(
                    f"{self.path.stem} is open in another BBC EFB recorder, so it was left alone."
                )
            if _IS_WINDOWS or _same_file(fd, self.path):
                self._fd = fd
                try:
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                except OSError:
                    pass  # the lock itself is what matters
                return self
            # POSIX: the file was removed and recreated between open and lock; retry.
            _unlock(fd)
            os.close(fd)
        raise JournalLockedError(f"The lock for {self.path.stem} keeps changing; try again.")

    def release(self, *, remove: bool = True) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if remove and not _IS_WINDOWS:
                _silent(os.unlink, self.path)  # unlink first so waiters see a stale inode
            _unlock(fd)
        finally:
            os.close(fd)
        if remove and _IS_WINDOWS:
            _silent(os.unlink, self.path)  # fails harmlessly if someone is probing it

    def __enter__(self) -> "_FileLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def _silent(fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except OSError:
        pass


def _same_file(fd: int, path: Path) -> bool:
    try:
        a, b = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


if _IS_WINDOWS:  # pragma: no cover - exercised on Windows CI
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def is_locked(lock_path: Path) -> bool:
    """True when a live writer holds ``lock_path``. Does not create or remove anything."""
    try:
        fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
    except FileNotFoundError:
        return False
    except PermissionError:
        return True  # Windows: the file is being deleted or held exclusively
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


def _fsync_dir(directory: Path) -> None:
    """Make a new or renamed directory entry durable (POSIX only; no-op on Windows)."""
    if _IS_WINDOWS:
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- writer


class Journal:
    """The open journal of one flight. Thread-safe; use ``create`` or ``resume``."""

    def __init__(self, *, path: Path, flight_id: str, fd: int, lock: _FileLock | None,
                 owns_lock: bool, header: Mapping[str, Any], next_seq: int,
                 last_phase: Phase | None, event_count: int, tail_dirty: bool,
                 policy: FsyncPolicy, clock: Callable[[], float],
                 now: Callable[[], datetime]) -> None:
        self.path = path
        self.flight_id = flight_id
        self.header: Mapping[str, Any] = dict(header)
        self.policy = policy
        self._fd: int | None = fd
        self._lock = lock
        self._owns_lock = owns_lock
        self._next_seq = next_seq
        self._last_phase = last_phase
        self._event_count = event_count
        self._tail_dirty = tail_dirty
        self._clock = clock
        self._now = now
        self._mutex = threading.RLock()
        self._unsynced = False
        self._last_sync = clock()
        self._bytes_written = 0
        self._fsync_count = 0
        self._last_event: RecorderEvent | None = None
        self._lost_count = 0
        self._lost_seqs: list[int] = []
        self._lost_kinds: dict[str, int] = {}
        self._lost_error: str | None = None
        # The last append that failed: (event, seq, error). Retrying that same event reuses
        # its seq; any other append (or close) gives the event up as lost and skips the seq.
        self._failed: tuple[RecorderEvent, int, str] | None = None

    # ---- construction

    @classmethod
    def create(cls, where: JournalLayout | Path | str, flight_id: str, *, app_version: str,
               started_host_time: datetime | None = None, started_sim_time_s: float | None = None,
               started_sim_utc: datetime | None = None, aircraft: Mapping[str, Any] | None = None,
               source: Mapping[str, Any] | None = None, rules_version: str | None = None,
               extra: Mapping[str, Any] | None = None, policy: FsyncPolicy | None = None,
               clock: Callable[[], float] = time.monotonic,
               now: Callable[[], datetime] | None = None) -> "Journal":
        """Create ``in-progress/<flight_id>.jsonl`` and write its header (synced).

        Unknown values stay ``None``. Refuses to touch an existing journal.
        """
        layout = JournalLayout.of(where)
        now_fn = now or (lambda: datetime.now(timezone.utc))
        path = layout.journal_path(flight_id)
        layout.in_progress_dir.mkdir(parents=True, exist_ok=True)
        lock = _FileLock(layout.lock_path(flight_id)).acquire()
        try:
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0))
            try:
                fd = os.open(path, flags, 0o644)
            except FileExistsError as error:
                raise JournalError(
                    f"A journal for flight {flight_id} already exists; it was not overwritten."
                ) from error
            except OSError as error:
                raise JournalWriteError(f"The flight journal could not be created: {error.strerror or error}") from error
            created = now_fn()
            header = {
                "seq": 0, "type": LineType.HEADER.value, "schema": JOURNAL_SCHEMA,
                "version": JOURNAL_VERSION, "flight_id": flight_id,
                "app_version": str(app_version),
                "created_host_time": created.isoformat(),
                "started_host_time": _iso(started_host_time),
                "started_sim_time_s": started_sim_time_s,
                "started_sim_utc": _iso(started_sim_utc),
                "aircraft": dict(aircraft) if aircraft is not None else None,
                "source": dict(source) if source is not None else None,
                "rules_version": rules_version,
                "extra": dict(extra or {}),
            }
            journal = cls(path=path, flight_id=flight_id, fd=fd, lock=lock, owns_lock=True,
                          header=header, next_seq=1, last_phase=None, event_count=0,
                          tail_dirty=False, policy=policy or FsyncPolicy(), clock=clock,
                          now=now_fn)
            try:
                journal._write(_encode_line(header))
                journal._sync()
            except BaseException:
                journal._close_fd()
                raise
            _fsync_dir(layout.in_progress_dir)
            return journal
        except BaseException:
            lock.release()
            raise

    @classmethod
    def resume(cls, where: JournalLayout | Path | str, flight_id: str, *,
               policy: FsyncPolicy | None = None, clock: Callable[[], float] = time.monotonic,
               now: Callable[[], datetime] | None = None,
               _lock: _FileLock | None = None) -> "Journal":
        """Reopen an unfinished journal to append to it (recovery's Resume or finalise).

        The existing bytes are never changed. If the file ends in a torn line, the next
        line starts with ``\\n`` so the torn bytes stay as one reported, damaged line.
        """
        layout = JournalLayout.of(where)
        now_fn = now or (lambda: datetime.now(timezone.utc))
        path = layout.journal_path(flight_id)
        lock = _lock or _FileLock(layout.lock_path(flight_id)).acquire()
        try:
            result = read_journal(path, keep_events=False)
            if result.footer is not None:
                raise JournalClosedError(f"The journal for flight {flight_id} is already closed.")
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
            try:
                fd = os.open(path, flags)
            except OSError as error:
                raise JournalWriteError(f"The flight journal could not be reopened: {error.strerror or error}") from error
            last = result.last_event
            journal = cls(path=path, flight_id=flight_id, fd=fd, lock=lock,
                          owns_lock=_lock is None, header=result.header or {},
                          next_seq=max(result.last_seq, result.max_seq_seen) + 1,
                          last_phase=last.phase if last else None,
                          event_count=result.event_count,
                          tail_dirty=result.size_bytes > 0 and not result.ends_with_newline,
                          policy=policy or FsyncPolicy(), clock=clock, now=now_fn)
            journal._last_event = last
            return journal
        except BaseException:
            if _lock is None:
                lock.release()
            raise

    # ---- state

    @property
    def closed(self) -> bool:
        return self._fd is None

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    @property
    def last_event(self) -> RecorderEvent | None:
        return self._last_event

    @property
    def unsynced(self) -> bool:
        """True while some written lines have not yet been forced to disk."""
        return self._unsynced

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def fsync_count(self) -> int:
        return self._fsync_count

    @property
    def lost_lines(self) -> int:
        """Events ``append`` could not write and that were not retried successfully."""
        return self._lost_count + (1 if self._failed is not None else 0)

    def lost_summary(self) -> dict[str, Any] | None:
        """What the footer's ``lost_lines`` block says, or ``None`` when nothing was lost.

        A failed append not yet retried or given up counts as lost.
        """
        with self._mutex:
            count, seqs = self._lost_count, list(self._lost_seqs)
            kinds, error = dict(self._lost_kinds), self._lost_error
            if self._failed is not None:
                event, seq, error = self._failed
                count += 1
                if len(seqs) < MAX_LOST_SEQS:
                    seqs.append(seq)
                kinds[event.kind.value] = kinds.get(event.kind.value, 0) + 1
            if not count:
                return None
            return {"count": count, "seqs": seqs, "kinds": kinds, "last_error": error}

    def _give_up_failed(self) -> None:
        """The failed event is lost: count it and skip its seq, so the reader reports it missing."""
        failed, self._failed = self._failed, None
        if failed is None:
            return
        event, seq, error = failed
        self._next_seq = max(self._next_seq, seq + 1)
        self._lost_count += 1
        if len(self._lost_seqs) < MAX_LOST_SEQS:
            self._lost_seqs.append(seq)
        self._lost_kinds[event.kind.value] = self._lost_kinds.get(event.kind.value, 0) + 1
        self._lost_error = error

    # ---- writing

    def append(self, event: RecorderEvent) -> int:
        """Write one event and return its ``seq``. Raises ``JournalWriteError`` on I/O failure."""
        if not isinstance(event, RecorderEvent):
            raise TypeError("Journal.append expects a RecorderEvent.")
        with self._mutex:
            self._require_open()
            failed = self._failed
            if failed is not None and failed[0] != event:
                self._give_up_failed()
            self._failed = None
            seq = self._next_seq
            try:
                line = _encode_line({"seq": seq, "type": LineType.EVENT.value, "event": event.to_json()})
                self._write(line)
            except JournalError as error:
                # Not written (a partial write is a damaged line the reader reports). Retrying
                # this event reuses the seq; anything else gives it up as lost, skipping the seq
                # so the reader reports a missing line, and the footer counts it.
                self._failed = (event, seq, str(error) or type(error).__name__)
                raise
            self._next_seq = seq + 1
            self._event_count += 1
            self._last_event = event
            policy = self.policy
            force = event.kind in policy.on_kinds or (
                policy.on_phase_change and self._last_phase is not None
                and event.phase != self._last_phase)
            self._last_phase = event.phase
            if force:
                self._sync()
            else:
                self._maybe_sync()
            return seq

    def tick(self) -> bool:
        """Sync pending lines if the interval has passed. Returns True if it synced."""
        with self._mutex:
            if self._fd is None or not self._unsynced:
                return False
            return self._maybe_sync()

    def sync(self) -> None:
        """Force every written line to disk now."""
        with self._mutex:
            self._require_open()
            self._sync()

    def close(self, outcome: Outcome | str = Outcome.COMPLETE, *,
              ended_host_time: datetime | None = None, ended_sim_time_s: float | None = None,
              ended_sim_utc: datetime | None = None, final_phase: Phase | None = None,
              detail: str | None = None, extra: Mapping[str, Any] | None = None) -> Path:
        """Write the footer, sync, close the file and release the lock.

        The end time is what the caller states; this method never derives one. The
        journal stays in ``in-progress/`` until ``archive_journal`` moves it, after the
        flight record has been saved (so a crash in between is still recoverable).
        """
        with self._mutex:
            self._require_open()
            self._give_up_failed()
            footer: dict[str, Any] = {
                "seq": self._next_seq, "type": LineType.FOOTER.value,
                "outcome": Outcome(outcome).value,
                "closed_host_time": self._now().isoformat(),
                "ended_host_time": _iso(ended_host_time),
                "ended_sim_time_s": ended_sim_time_s,
                "ended_sim_utc": _iso(ended_sim_utc),
                "final_phase": Phase(final_phase).value if final_phase is not None else None,
                "event_count": self._event_count,
                "detail": detail,
            }
            lost = self.lost_summary()
            if lost is not None:
                footer["lost_lines"] = lost
            if extra:
                footer["extra"] = dict(extra)
            self._write(_encode_line(footer))
            self._next_seq += 1
            self._sync()
            self._close_fd()
            self._release_lock()
            return self.path

    def abandon(self) -> None:
        """Close without a footer (the journal stays unfinished, for recovery). Idempotent."""
        with self._mutex:
            if self._fd is not None:
                try:
                    if self._unsynced:
                        self._sync()
                except JournalWriteError:
                    pass
                self._close_fd()
            self._release_lock()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # A clean exit without close() and any exception both leave the journal
        # unfinished: only the Recorder knows whether the flight really ended.
        self.abandon()

    def __del__(self) -> None:  # last resort; never raises
        try:
            if getattr(self, "_fd", None) is not None:
                os.close(self._fd)  # type: ignore[arg-type]
        except Exception:
            pass

    # ---- internals

    def _require_open(self) -> None:
        if self._fd is None:
            raise JournalClosedError(f"The journal for flight {self.flight_id} is closed.")

    def _write(self, line: bytes) -> None:
        assert self._fd is not None
        data = (b"\n" + line) if self._tail_dirty else line
        view = memoryview(data)
        written = 0
        try:
            while written < len(data):
                count = os.write(self._fd, view[written:])
                if count <= 0:
                    raise OSError(0, "the operating system accepted no bytes")
                written += count
        except OSError as error:
            if written:
                self._tail_dirty = True
                self._unsynced = True
            raise JournalWriteError(
                f"The flight journal could not be written ({error.strerror or error}). "
                "Recording continues to be attempted; free disk space or check the data folder."
            ) from error
        self._tail_dirty = False
        self._unsynced = True
        self._bytes_written += written

    def _sync(self) -> None:
        assert self._fd is not None
        try:
            os.fsync(self._fd)
        except OSError as error:
            raise JournalWriteError(
                f"The flight journal could not be saved to disk ({error.strerror or error})."
            ) from error
        self._unsynced = False
        self._last_sync = self._clock()
        self._fsync_count += 1

    def _maybe_sync(self) -> bool:
        interval = self.policy.interval_s
        if interval is not None and self._clock() - self._last_sync >= interval:
            self._sync()
            return True
        return False

    def _close_fd(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _release_lock(self) -> None:
        if self._lock is not None and self._owns_lock:
            self._lock.release()
        self._lock = None


# --------------------------------------------------------------------------- reader


@dataclass(frozen=True, slots=True)
class JournalIssue:
    """One problem found while reading. ``line`` is 1-based; ``offset`` is a byte offset."""

    kind: IssueKind
    line: int
    offset: int
    length: int
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "line": self.line, "offset": self.offset,
                "length": self.length, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class JournalReadResult:
    """Everything a tolerant read found. Unknown stays ``None``."""

    path: Path
    header: Mapping[str, Any] | None
    footer: Mapping[str, Any] | None
    events: tuple[RecorderEvent, ...]          # empty when read with keep_events=False
    notable_events: tuple[RecorderEvent, ...]  # every flight-evidence event, plus bounded notes
    event_count: int
    first_event: RecorderEvent | None
    last_event: RecorderEvent | None
    last_seq: int                              # seq of the last accepted line, -1 if none
    max_seq_seen: int
    good_bytes: int                            # offset just after the last accepted line
    size_bytes: int
    ends_with_newline: bool
    torn_tail_bytes: int                       # bytes of an incomplete final line, ignored
    issues: tuple[JournalIssue, ...]
    issue_count: int
    counts_by_kind: Mapping[str, int] = field(default_factory=dict)
    compressed: bool = False
    # Bounded kinds (notes, pauses, ...) left out of ``notable_events``: the first and the
    # last ``MAX_NOTABLE_EVENTS // 2`` are kept, the ones in between only counted.
    notable_omitted: int = 0
    notable_omitted_by_kind: Mapping[str, int] = field(default_factory=dict)

    @property
    def lost_lines(self) -> int:
        """Events the writer itself reported it could not write (the footer's ``lost_lines``)."""
        lost = (self.footer or {}).get("lost_lines")
        count = lost.get("count") if isinstance(lost, Mapping) else None
        return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0

    @property
    def closed(self) -> bool:
        return self.footer is not None

    @property
    def clean(self) -> bool:
        return self.issue_count == 0

    @property
    def outcome(self) -> Outcome | None:
        if self.footer is None:
            return None
        try:
            return Outcome(self.footer.get("outcome"))
        except ValueError:
            return None

    def issues_of(self, kind: IssueKind) -> tuple[JournalIssue, ...]:
        return tuple(i for i in self.issues if i.kind is kind)

    def summary(self) -> str:
        """Plain-English description of the journal's integrity."""
        parts = [f"{self.event_count} event{'s' if self.event_count != 1 else ''}"]
        parts.append("closed" if self.closed else "no closing line")
        if self.torn_tail_bytes:
            parts.append(f"final {self.torn_tail_bytes} bytes incomplete and ignored")
        other = self.issue_count - (1 if self.torn_tail_bytes else 0)
        if other > 0:
            parts.append(f"{other} other problem{'s' if other != 1 else ''} reported")
        return ", ".join(parts)


def _open_binary(path: Path) -> BinaryIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return open(path, "rb")


def _classify_nul(content: bytes) -> bool:
    return b"\x00" in content and not content.strip(b"\x00 \t\r")


def read_journal(path: Path | str, *, keep_events: bool = True,
                 on_event: Callable[[int, RecorderEvent], None] | None = None,
                 line_limit: int = READ_LINE_LIMIT) -> JournalReadResult:
    """Read a journal tolerantly. Damaged lines are skipped and reported, never fatal.

    Safe while a writer is appending: a line the writer has not finished yet is
    reported as a torn tail. ``keep_events=False`` keeps memory bounded for very
    long flights (``on_event`` still sees every event). Raises ``OSError`` only when
    the file itself cannot be opened.
    """
    path = Path(path)
    events: list[RecorderEvent] = []
    notable: list[tuple[int, RecorderEvent]] = []   # (order, event): protected + first bounded
    half = max(1, MAX_NOTABLE_EVENTS // 2)
    bounded_head = 0
    bounded_tail: deque[tuple[int, RecorderEvent]] = deque(maxlen=half)
    omitted = 0
    omitted_kinds: dict[str, int] = {}
    issues: list[JournalIssue] = []
    issue_count = 0
    counts: dict[str, int] = {}
    header: dict[str, Any] | None = None
    footer: dict[str, Any] | None = None
    first: RecorderEvent | None = None
    last: RecorderEvent | None = None
    last_seq = -1
    max_seq = -1
    good_bytes = 0
    offset = 0
    line_no = 0
    torn = 0
    ends_nl = True
    event_count = 0
    seen_any_good = False

    def report(kind: IssueKind, start: int, length: int, detail: str) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < MAX_REPORTED_ISSUES:
            issues.append(JournalIssue(kind, line_no, start, length, detail))

    with _open_binary(path) as handle:
        while True:
            raw = handle.readline(line_limit)
            if not raw:
                break
            line_no += 1
            start = offset
            offset += len(raw)
            terminated = raw.endswith(b"\n")
            if not terminated and len(raw) >= line_limit:
                # Longer than any line we write: skip to the next newline.
                while True:
                    more = handle.readline(line_limit)
                    offset += len(more)
                    if not more or more.endswith(b"\n"):
                        terminated = bool(more)
                        break
                length = offset - start
                if terminated:
                    report(IssueKind.TOO_LONG, start, length,
                           f"Line {line_no} is {length} bytes, longer than any journal line; skipped.")
                else:
                    torn = length
                    ends_nl = False
                    report(IssueKind.TORN_TAIL, start, length,
                           f"The final {length} bytes are an incomplete, overlong line; ignored.")
                continue
            final_unterminated = not terminated
            if final_unterminated:
                ends_nl = False
            content = raw[:-1] if terminated else raw
            if content.endswith(b"\r"):
                content = content[:-1]
            length = len(raw)

            def bad(kind: IssueKind, detail: str) -> None:
                nonlocal torn
                if final_unterminated:
                    torn = length
                    report(IssueKind.TORN_TAIL, start, length,
                           f"The final {length} bytes are an incomplete line ({detail}); ignored.")
                else:
                    report(kind, start, length, f"Line {line_no}: {detail}; skipped.")

            if not content.strip(b" \t\r"):
                continue  # blank line: harmless (e.g. separator after a failed write)
            if _classify_nul(content):
                bad(IssueKind.ZERO_FILL, f"{len(content)} zero bytes, data that never reached the disk")
                continue
            match = _CRC_TAIL.search(content)
            if match is None:
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    bad(IssueKind.BAD_UTF8, "not valid UTF-8")
                    continue
                bad(IssueKind.CHECKSUM, "no checksum, so the line is incomplete or was edited")
                continue
            body = content[:match.start()] + b"}"
            if zlib.crc32(body) != int(match.group(1), 16):
                bad(IssueKind.CHECKSUM, "checksum does not match, so the line is damaged or was edited")
                continue
            try:
                obj = json.loads(body.decode("utf-8"))
            except UnicodeDecodeError:
                bad(IssueKind.BAD_UTF8, "not valid UTF-8")
                continue
            except ValueError as error:
                bad(IssueKind.CORRUPT, f"not valid JSON ({error.msg})")
                continue
            if not isinstance(obj, dict):
                bad(IssueKind.CORRUPT, "not a JSON object")
                continue
            seq = obj.get("seq")
            kind = obj.get("type")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                bad(IssueKind.CORRUPT, "missing sequence number")
                continue
            if footer is not None:
                report(IssueKind.AFTER_FOOTER, start, length,
                       f"Line {line_no} comes after the closing line; ignored.")
                continue
            if seq <= last_seq:
                report(IssueKind.OUT_OF_ORDER, start, length,
                       f"Line {line_no} repeats or goes back to sequence {seq} (after {last_seq}); skipped.")
                continue
            if kind == LineType.HEADER.value:
                if seen_any_good or header is not None:
                    report(IssueKind.CORRUPT, start, length, f"Line {line_no} is a second header; skipped.")
                    continue
                header = obj
                version = obj.get("version")
                if obj.get("schema") != JOURNAL_SCHEMA:
                    report(IssueKind.CORRUPT, start, length,
                           f"Header names schema {obj.get('schema')!r}, not {JOURNAL_SCHEMA!r}.")
                elif isinstance(version, int) and version > JOURNAL_VERSION:
                    report(IssueKind.NEWER_SCHEMA, start, length,
                           f"Journal version {version} is newer than this BBC EFB reads ({JOURNAL_VERSION}).")
            elif kind == LineType.EVENT.value:
                try:
                    event = RecorderEvent.from_json(obj["event"])
                except (KeyError, TypeError, ValueError, AttributeError) as error:
                    bad(IssueKind.CORRUPT, f"event could not be read ({type(error).__name__}: {error})")
                    continue
                if not seen_any_good and header is None:
                    report(IssueKind.NO_HEADER, start, 0, "The journal has no readable header line.")
                event_count += 1
                counts[event.kind.value] = counts.get(event.kind.value, 0) + 1
                if first is None:
                    first = event
                last = event
                if keep_events:
                    events.append(event)
                if event.kind in PROTECTED_KINDS:
                    notable.append((event_count, event))
                elif event.kind is not EventKind.SAMPLE:
                    if bounded_head < half:
                        bounded_head += 1
                        notable.append((event_count, event))
                    else:
                        if len(bounded_tail) == half:
                            dropped = bounded_tail[0][1].kind.value
                            omitted += 1
                            omitted_kinds[dropped] = omitted_kinds.get(dropped, 0) + 1
                        bounded_tail.append((event_count, event))
                if on_event is not None:
                    on_event(seq, event)
            elif kind == LineType.FOOTER.value:
                footer = obj
            else:
                report(IssueKind.UNKNOWN_TYPE, start, length,
                       f"Line {line_no} has unknown type {kind!r}; skipped.")
            if seq != last_seq + 1:
                missing = seq - last_seq - 1
                report(IssueKind.MISSING_LINES, start, 0,
                       f"{missing} line{'s are' if missing != 1 else ' is'} missing before line {line_no} "
                       f"(sequence {last_seq + 1}–{seq - 1}).")
            last_seq = seq
            max_seq = max(max_seq, seq)
            seen_any_good = True
            good_bytes = offset

    if bounded_tail:
        notable.extend(bounded_tail)
        notable.sort(key=lambda item: item[0])
    return JournalReadResult(
        path=path, header=header, footer=footer, events=tuple(events),
        notable_events=tuple(event for _order, event in notable), event_count=event_count, first_event=first,
        last_event=last, last_seq=last_seq, max_seq_seen=max_seq, good_bytes=good_bytes,
        size_bytes=offset, ends_with_newline=ends_nl, torn_tail_bytes=torn,
        issues=tuple(issues), issue_count=issue_count, counts_by_kind=dict(counts),
        compressed=path.suffix == ".gz",
        notable_omitted=omitted, notable_omitted_by_kind=dict(omitted_kinds),
    )


def iter_journal_events(path: Path | str) -> Iterator[RecorderEvent]:
    """Stream the good events of a journal with bounded memory (damaged lines skipped)."""
    path = Path(path)
    with _open_binary(path) as handle:
        for raw in handle:
            content = raw.rstrip(b"\r\n")
            match = _CRC_TAIL.search(content)
            if match is None:
                continue
            body = content[:match.start()] + b"}"
            if zlib.crc32(body) != int(match.group(1), 16):
                continue
            try:
                obj = json.loads(body.decode("utf-8"))
                if obj.get("type") == LineType.EVENT.value:
                    yield RecorderEvent.from_json(obj["event"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue


# --------------------------------------------------------------------------- archive


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _header_month(header: Mapping[str, Any] | None) -> str:
    for key in ("started_host_time", "created_host_time"):
        value = (header or {}).get(key)
        if isinstance(value, str):
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                continue
            if moment.tzinfo is not None:
                moment = moment.astimezone(timezone.utc)
            return f"{moment:%Y-%m}"
    return "undated"


def archive_target(where: JournalLayout | Path | str, flight_id: str,
                   header: Mapping[str, Any] | None) -> Path:
    """Where ``archive_journal`` will put this flight's journal (before collisions)."""
    layout = JournalLayout.of(where)
    validate_flight_id(flight_id)
    return layout.archive_dir / _header_month(header) / f"{flight_id}{JOURNAL_SUFFIX}"


def _move_with_retry(source: Path, target: Path, attempts: int = 8) -> None:
    delay = 0.02
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:  # Windows: antivirus or indexer briefly holds the file
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _free_target(target: Path, source: Path) -> Path | None:
    """``target`` or a numbered sibling; None if an identical copy is already there."""
    if not target.exists():
        return target
    source_hash = file_sha256(source)
    stem, n = target.name[: -len(JOURNAL_SUFFIX)], 2
    candidate = target
    while candidate.exists():
        if candidate.is_file() and file_sha256(candidate) == source_hash:
            return None
        candidate = target.with_name(f"{stem}-{n}{JOURNAL_SUFFIX}")
        n += 1
    return candidate


def _move_closed(where: JournalLayout | Path | str, flight_id: str, directory: Path | None,
                 *, require_footer: bool, lock: _FileLock | None = None) -> Path:
    layout = JournalLayout.of(where)
    source = layout.journal_path(flight_id)
    own = lock is None
    if lock is None:
        lock = _FileLock(layout.lock_path(flight_id))
        lock.acquire()  # refuses while a writer has it open (no rename while open)
    try:
        result = read_journal(source, keep_events=False)
        if require_footer and result.footer is None:
            raise JournalError(
                f"The journal for flight {flight_id} has no closing line and was not archived. "
                "Use recovery to finalise it first."
            )
        target = (directory / f"{flight_id}{JOURNAL_SUFFIX}" if directory is not None
                  else archive_target(layout, flight_id, result.header))
        target.parent.mkdir(parents=True, exist_ok=True)
        free = _free_target(target, source)
        if free is None:  # identical evidence already archived: the in-progress copy is redundant
            os.remove(source)
            _fsync_dir(source.parent)
            return target
        _move_with_retry(source, free)
        _fsync_dir(free.parent)
        _fsync_dir(source.parent)
        return free
    finally:
        if own:
            lock.release()


def archive_journal(where: JournalLayout | Path | str, flight_id: str) -> Path:
    """Move a closed journal to ``journals/<YYYY-MM>/`` and return its new path.

    Call it after the flight record that refers to the journal has been saved. Refuses
    an unfinished journal or one a writer still holds. Never overwrites: a different
    file already at the target gets a ``-2``, ``-3`` … sibling.
    """
    return _move_closed(where, flight_id, None, require_footer=True)


def list_archived(where: JournalLayout | Path | str) -> list[Path]:
    """Every archived journal (plain and gzip), oldest month first."""
    layout = JournalLayout.of(where)
    root = layout.archive_dir
    if not root.is_dir():
        return []
    found = [p for p in root.rglob("*") if p.is_file()
             and (p.name.endswith(JOURNAL_SUFFIX) or p.name.endswith(JOURNAL_SUFFIX + ".gz"))]
    return sorted(found)


def find_archived(where: JournalLayout | Path | str, flight_id: str) -> Path | None:
    validate_flight_id(flight_id)
    for path in list_archived(where):
        name = path.name.removesuffix(".gz").removesuffix(JOURNAL_SUFFIX)
        if name == flight_id:
            return path
    return None


def compress_archived(where: JournalLayout | Path | str, *, older_than_days: float,
                      now: datetime | None = None) -> list[Path]:
    """Gzip archived journals older than ``older_than_days`` (never run automatically).

    Each copy is synced and verified against the original before the original is
    removed, so evidence is never lost. Returns the new ``.gz`` paths.
    """
    moment = (now or datetime.now(timezone.utc)).timestamp()
    done: list[Path] = []
    for path in list_archived(where):
        if path.suffix == ".gz" or moment - path.stat().st_mtime < older_than_days * 86400:
            continue
        target = path.with_name(path.name + ".gz")
        temporary = path.with_name(path.name + ".gz.tmp")
        original_hash = file_sha256(path)
        with open(path, "rb") as source, open(temporary, "wb") as raw:
            with gzip.GzipFile(filename=path.name, mode="wb", fileobj=raw, mtime=0) as packed:
                shutil.copyfileobj(source, packed, 1 << 20)
            raw.flush()
            os.fsync(raw.fileno())
        digest = hashlib.sha256()
        with gzip.open(temporary, "rb") as check:
            for chunk in iter(lambda: check.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != original_hash:
            os.remove(temporary)
            raise JournalError(f"Compressed copy of {path.name} did not match; the original was kept.")
        _move_with_retry(temporary, target)
        os.remove(path)
        _fsync_dir(path.parent)
        done.append(target)
    return done
