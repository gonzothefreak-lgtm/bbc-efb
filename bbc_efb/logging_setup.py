"""Application logging for the BBC EFB.

Library code in ``bbc_core`` and ``bbc_efb`` only calls ``logging.getLogger(__name__)``.
This module is the one place where the application attaches handlers. Call
``configure_logging(None)`` as the very first start-up step (before Qt), so start-up
problems reach the per-user log, then ``relocate_logging(data_folder)`` once the
data folder is known. ``release_folder(folder)`` moves the log out of a folder that
is about to be closed, changed or restored into:

* A rotating UTF-8 log file in ``<data folder>/logs``. If the pilot's data folder
  is not chosen yet or cannot be written, a per-user folder is used instead
  (``%LOCALAPPDATA%/BBC EFB/logs`` on Windows) and the reason is recorded.
* A stderr handler only when a console exists. Windowed builds and ``pythonw``
  set ``sys.stderr`` to None, and the log file is then the only record.
* Hooks that log uncaught exceptions from the main thread, from other threads and
  "unraisable" exceptions, which is how Python reports an exception raised inside
  a ctypes callback such as the SimConnect dispatch procedure. Qt slot exceptions
  reach ``sys.excepthook`` as well.
* ``faulthandler`` writes native crash tracebacks to ``fault.log`` in the same
  folder.

Times in the file are UTC, matching the flight log.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any

LOG_FILE_NAME = "bbc-efb.log"
FAULT_FILE_NAME = "fault.log"
MAX_LOG_BYTES = 2_000_000
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s [%(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_log = logging.getLogger(__name__)
_state_lock = threading.Lock()
_state: _InstalledState | None = None


@dataclass(frozen=True, slots=True)
class LoggingConfiguration:
    """What ``configure_logging`` set up, for diagnostics and the Settings page."""

    log_file: Path | None
    console: bool
    fallback_reason: str | None
    """Why the data folder was not used, or None when it was (or none was given)."""
    level: int = logging.INFO
    data_dir: Path | None = None
    """The data folder that was asked for (None for the per-user bootstrap folder)."""


@dataclass(slots=True)
class _InstalledState:
    handlers: tuple[logging.Handler, ...]
    previous_root_level: int
    previous_excepthook: Any
    previous_threading_excepthook: Any
    previous_unraisablehook: Any
    fault_file: IO[str] | None
    faulthandler_was_enabled: bool
    configuration: LoggingConfiguration | None = None


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime


class _LogFileHandler(RotatingFileHandler):
    """A rotating log file that stays closed once it is retired.

    ``logging.FileHandler.emit`` reopens a closed file in append mode. Another thread
    (the Core thread, the Recorder's saver) may already hold this handler, or wait on
    its lock, while ``configure_logging`` re-points the log; without this guard that
    thread would reopen ``<old data folder>/logs/bbc-efb.log`` where ``release_folder``
    cannot see it, and Windows could not rename or delete the old folder. A retired
    handler drops the record instead; the handlers that replaced it are already
    attached to the root logger, so later records reach the new file.
    """

    _retired = False

    @property
    def retired(self) -> bool:
        return self._retired

    def retire(self) -> None:
        self.acquire()
        try:
            self._retired = True
        finally:
            self.release()
        self.close()

    def emit(self, record: logging.LogRecord) -> None:
        if self._retired:  # handle() holds the handler lock here, and retire() sets it under that lock
            return
        super().emit(record)

    def _open(self):
        if self._retired:
            raise ValueError(f"{self.baseFilename} was retired and is not reopened.")
        return super()._open()


def _retire(handler: logging.Handler) -> None:
    try:
        if isinstance(handler, _LogFileHandler):
            handler.retire()
        else:
            handler.close()
    except OSError:
        pass


def user_log_directory() -> Path:
    """Per-user log folder used when the data folder is unavailable."""

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "BBC EFB" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "BBC EFB"
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "bbc-efb" / "logs"


def _prepare_directory(directory: Path) -> str | None:
    """Create the folder and prove it is writable. Returns a problem or None."""

    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        return f"{directory} could not be used for logs ({error.strerror or error})"
    return None


def _resolve_level(level: int | str) -> int:
    if isinstance(level, bool):
        raise TypeError("Log level must be a level name or number.")
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    if not isinstance(resolved, int):
        raise ValueError(f"Unknown log level {level!r}. Use DEBUG, INFO, WARNING, ERROR or CRITICAL.")
    return resolved


def _console_available(stream: Any) -> bool:
    if stream is None:
        return False
    try:
        return callable(getattr(stream, "write", None)) and not getattr(stream, "closed", False)
    except ValueError:
        return False


def configure_logging(data_dir: Path | None, level: int | str = logging.INFO) -> LoggingConfiguration:
    """Attach the application's log handlers and exception hooks.

    Calling it again re-points the handlers it installed earlier (for example from
    the per-user bootstrap folder to ``<data folder>/logs`` once the folder is known,
    or back when the folder is released). The new file is opened before the old one
    is closed, so no line is lost in between and nothing is duplicated. The hooks
    installed by the first call stay in place; ``reset_logging`` restores what was
    there before it.
    """

    global _state
    resolved_level = _resolve_level(level)
    with _state_lock:
        fallback_reason: str | None = None
        directory: Path | None = None
        if data_dir is not None:
            candidate = Path(data_dir) / "logs"
            fallback_reason = _prepare_directory(candidate)
            if fallback_reason is None:
                directory = candidate
        if directory is None:
            candidate = user_log_directory()
            problem = _prepare_directory(candidate)
            if problem is None:
                directory = candidate
            else:
                fallback_reason = "; ".join(part for part in (fallback_reason, problem) if part)

        formatter = _UtcFormatter(LOG_FORMAT, DATE_FORMAT)
        handlers: list[logging.Handler] = []
        log_file: Path | None = None
        fault_file: IO[str] | None = None
        if directory is not None:
            log_file = directory / LOG_FILE_NAME
            try:
                file_handler = _LogFileHandler(log_file, maxBytes=MAX_LOG_BYTES,
                                               backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
            except OSError as error:
                fallback_reason = "; ".join(part for part in (
                    fallback_reason, f"{log_file} could not be opened ({error.strerror or error})") if part)
                log_file = None
            else:
                file_handler.setFormatter(formatter)
                handlers.append(file_handler)
                try:
                    fault_file = open(directory / FAULT_FILE_NAME, "a", encoding="utf-8")
                except OSError:
                    fault_file = None

        console = _console_available(sys.stderr)
        if console:
            stream_handler = logging.StreamHandler(sys.stderr)
            stream_handler.setFormatter(formatter)
            handlers.append(stream_handler)

        root = logging.getLogger()
        previous = _state
        for handler in handlers:
            root.addHandler(handler)
        if previous is None:
            previous_root_level = root.level
            previous_excepthook = sys.excepthook
            previous_threading_excepthook = threading.excepthook
            previous_unraisablehook = sys.unraisablehook
            faulthandler_was_enabled = faulthandler.is_enabled()
        else:
            # Re-pointing: the new handlers are attached, now let go of the old files.
            for handler in previous.handlers:
                root.removeHandler(handler)
                _retire(handler)
            if previous.fault_file is not None:
                faulthandler.disable()
                try:
                    previous.fault_file.close()
                except OSError:
                    pass
            previous_root_level = previous.previous_root_level
            previous_excepthook = previous.previous_excepthook
            previous_threading_excepthook = previous.previous_threading_excepthook
            previous_unraisablehook = previous.previous_unraisablehook
            faulthandler_was_enabled = previous.faulthandler_was_enabled
        root.setLevel(resolved_level)
        logging.captureWarnings(True)
        if fault_file is not None:
            faulthandler.enable(fault_file, all_threads=True)

        configuration = LoggingConfiguration(log_file=log_file, console=console, fallback_reason=fallback_reason,
                                             level=resolved_level,
                                             data_dir=Path(data_dir) if data_dir is not None else None)
        _state = _InstalledState(
            handlers=tuple(handlers),
            previous_root_level=previous_root_level,
            previous_excepthook=previous_excepthook,
            previous_threading_excepthook=previous_threading_excepthook,
            previous_unraisablehook=previous_unraisablehook,
            fault_file=fault_file,
            faulthandler_was_enabled=faulthandler_was_enabled,
            configuration=configuration,
        )
        sys.excepthook = _log_uncaught_exception
        threading.excepthook = _log_uncaught_thread_exception
        sys.unraisablehook = _log_unraisable_exception

    if log_file is None:
        _log.warning("No log file is being written: %s", fallback_reason or "no writable log folder")
    elif fallback_reason:
        _log.warning("Logging to %s instead of the data folder: %s", log_file.parent, fallback_reason)
    _log.info("Logging %s (level %s, file %s, console %s)", "started" if previous is None else "moved",
              logging.getLevelName(resolved_level), log_file, "yes" if console else "no")
    return configuration


def current_configuration() -> LoggingConfiguration | None:
    """What ``configure_logging`` last set up, or None when it is not active."""

    with _state_lock:
        return _state.configuration if _state is not None else None


def relocate_logging(data_dir: Path | None) -> LoggingConfiguration | None:
    """Move the log to ``<data_dir>/logs`` (or the per-user folder for None), keeping the level.

    Does nothing and returns None when logging was never configured (tests, embedding).
    """

    configuration = current_configuration()
    if configuration is None:
        return None
    return configure_logging(data_dir, configuration.level)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (ValueError, OSError):
        return False
    return True


def open_files_inside(folder: Path) -> tuple[Path, ...]:
    """Log files this process holds open inside ``folder`` (the log and fault.log)."""

    with _state_lock:
        state = _state
        if state is None:
            return ()
        paths: list[Path] = []
        for handler in state.handlers:
            name = getattr(handler, "baseFilename", None)
            if name:
                paths.append(Path(name))
        fault_name = getattr(state.fault_file, "name", None) if state.fault_file is not None else None
        if isinstance(fault_name, str):
            paths.append(Path(fault_name))
    return tuple(path for path in paths if _inside(path, Path(folder)))


def release_folder(folder: Path) -> LoggingConfiguration | None:
    """Stop writing inside ``folder``: move the log to the per-user folder if it is there.

    Called before a data folder is closed, renamed by a restore, or left for another
    one, because Windows cannot rename or delete a folder holding open log files.
    Returns the new configuration, or None when nothing had to move.
    """

    if not open_files_inside(folder):
        return None
    return relocate_logging(None)


def reset_logging() -> None:
    """Remove the handlers and hooks installed by ``configure_logging``."""

    with _state_lock:
        _reset_locked()


def _reset_locked() -> None:
    global _state
    state = _state
    if state is None:
        return
    _state = None
    root = logging.getLogger()
    for handler in state.handlers:
        root.removeHandler(handler)
        _retire(handler)
    root.setLevel(state.previous_root_level)
    logging.captureWarnings(False)
    if sys.excepthook is _log_uncaught_exception:
        sys.excepthook = state.previous_excepthook
    if threading.excepthook is _log_uncaught_thread_exception:
        threading.excepthook = state.previous_threading_excepthook
    if sys.unraisablehook is _log_unraisable_exception:
        sys.unraisablehook = state.previous_unraisablehook
    if state.fault_file is not None:
        faulthandler.disable()
        if state.faulthandler_was_enabled and _console_available(sys.stderr):
            try:
                faulthandler.enable()
            except (RuntimeError, ValueError, OSError):
                pass
        state.fault_file.close()


def _log_uncaught_exception(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.getLogger("bbc_efb.uncaught").critical(
        "Uncaught exception; the EFB may need to be restarted.",
        exc_info=(exc_type, exc_value, exc_traceback))


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    thread_name = args.thread.name if args.thread is not None else "unknown thread"
    logging.getLogger("bbc_efb.uncaught").critical(
        "Uncaught exception in thread %s.", thread_name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _log_unraisable_exception(unraisable: Any) -> None:
    exc_type = unraisable.exc_type
    exc_info = (exc_type, unraisable.exc_value, unraisable.exc_traceback) if exc_type is not None else None
    message = unraisable.err_msg or "Exception ignored in"
    try:
        target = repr(unraisable.object)
    except Exception:  # repr of an arbitrary object may itself fail
        target = "<unprintable object>"
    logging.getLogger("bbc_efb.uncaught").error("%s: %s", message, target, exc_info=exc_info)
