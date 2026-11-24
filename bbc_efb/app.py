"""Start BBC EFB: logging, Qt application, theme, data folder, Core host and Recorder.

``run(options)`` is the whole start-up sequence; ``bbc_efb.__main__`` parses the
command line into ``AppOptions``.

Start-up order
--------------
1. Logging (``bbc_efb.logging_setup``) into the per-user log folder, before
   anything else, so every start-up problem and uncaught exception leaves a trace.
2. High-DPI policy (before the QApplication exists), the QApplication and the theme.
3. The data folder: ``--data-dir`` when given, else the remembered folder, else the
   first-launch prompt (``bbc_efb.settings.choose_data_folder``; a basic built-in
   prompt otherwise). The pilot may decline; the Logbook then asks the shell to
   choose one later. Another running EFB holding the writer lock is reported and
   the Logbook opens read-only. The app owns this one ``FlightStore`` (contract C1):
   the Logbook, Settings and the Recorder's Logbook sink all use it.
4. Logging moves to ``<data folder>/logs`` (not for a read-only folder another EFB
   is writing to), and the start-up notes so far are written there too.
5. Black Box Core (contract C4):
   * ``--simulated``: the simulated provider, labelled SIMULATED in the window
     title, the title bar, the footer, Connection Diagnostics and every recorded
     flight. It is never used without that flag.
   * Otherwise SimConnect.dll is located (``bbc_core.simconnect_sdk``). When found,
     a ``CoreHost`` runs a ``SimConnectProvider`` on its own thread and a
     ``CoreBridge`` brings its status to the Qt thread.
   * When the DLL is not found, or the host cannot be created, the EFB runs
     disconnected and says why in the SimConnect status. It never falls back to
     simulated data.
   * The SimConnect.dll choice in Settings is read again before every connect
     attempt (``SimConnectDllSwitch``). A different file is handed to the host,
     which starts using it at its next attempt; a connected session is never
     interrupted. Without a DLL at start-up there is no host, so a choice made
     then needs a restart, and Settings says so.
6. Flight Logging (``FlightLoggingManager``): when a writable data folder is open,
   unfinished journals from a previous run are recovered as interrupted flights
   (the pilot sees a notice), then the Recorder service (contract C3) is created
   with a ``LogbookSink`` (contract C2) and ticked on the Core thread
   (``CoreHost.add_tick``), so logging continues while the window is minimised.
   The manager follows data-folder changes made in Settings or from the Logbook.
7. Drop-in modules (``bbc_efb.modules``, experimental, off by default).
8. The shell window, which opens in Dispatch.

On exit everything is shut down in reverse order: Recorder (an open flight is saved
as interrupted), modules, bridge, host, store.

Components built by other workstreams are located and constructed by name with only
the keyword arguments their constructors accept (``construct_with``), so a small
signature change elsewhere does not need an edit here. Each failure is logged and
shown honestly.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import logging
import os
from pathlib import Path
import sys
from typing import Any

from . import VERSION
from .footer import LoggingStatus, RecorderServiceAdapter, UnavailableFlightLogging
from .sections import DataFolderControl, ShellServices, construct_with

log = logging.getLogger(__name__)

APP_ID = "BlackBoxClub.BBCEFB"
SIMULATED_WARNING = "SIMULATED: started with --simulated. No data comes from MSFS."
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
RECORDER_CONSUMER = "flight-recorder"


@dataclass(frozen=True, slots=True)
class AppOptions:
    """Command-line choices. ``data_dir`` overrides the remembered folder for this run only."""

    data_dir: Path | None = None
    simulated: bool = False
    log_level: str = "INFO"
    simconnect_dll: Path | None = None
    ask_for_data_folder: bool = True


@dataclass(slots=True)
class StartupReport:
    """What happened during start-up, for the log and Connection Diagnostics."""

    notes: list[str] = field(default_factory=list)
    pilot_notices: list[str] = field(default_factory=list)
    """Notes the pilot must see in the window's notice banner, not only in diagnostics."""

    def add(self, text: str, level: int = logging.INFO) -> None:
        self.notes.append(text)
        log.log(level, "%s", text)

    def tell_pilot(self, text: str, level: int = logging.WARNING) -> None:
        """Record ``text`` like ``add`` and show it in the notice banner when the window opens."""
        self.add(text, level)
        if text not in self.pilot_notices:
            self.pilot_notices.append(text)


# ----------------------------------------------------------------------------
# Qt application
# ----------------------------------------------------------------------------


def configure_high_dpi() -> None:
    """Use Windows' exact scale factors (125 %, 150 %...) instead of rounding them.

    Must run before the QApplication is created; does nothing afterwards.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def set_windows_app_id(app_id: str = APP_ID) -> None:
    """Give the EFB its own taskbar identity on Windows (no effect elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):  # pragma: no cover - Windows only
        log.debug("Taskbar app id not set", exc_info=True)


def create_application(argv: Sequence[str] | None = None):
    """Return the QApplication, creating and theming it when needed."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        configure_high_dpi()
        set_windows_app_id()
        app = QApplication(list(argv) if argv is not None else sys.argv[:1])
    app.setApplicationName("BBC EFB")
    app.setApplicationDisplayName("BBC EFB")
    app.setOrganizationName("Black Box Club")
    app.setApplicationVersion(VERSION)
    from .theme import apply_theme

    apply_theme(app)
    return app


# ----------------------------------------------------------------------------
# Data folder
# ----------------------------------------------------------------------------


def _folder_settings():
    from .storage_settings import DataFolderSettings, default_settings_path

    return DataFolderSettings(default_settings_path())


def _optional(module: str, *names: str) -> Callable[..., Any] | None:
    """The first of ``names`` found in ``module``, or None when the module or names are absent."""
    try:
        loaded = importlib.import_module(module)
    except ImportError:
        return None
    for name in names:
        found = getattr(loaded, name, None)
        if callable(found):
            return found
    return None


def _open_store(folder: Path, report: StartupReport, *, create: bool):
    """Open the data folder for writing; read-only when another EFB holds its lock."""
    from .storage_settings import open_data_folder

    try:
        return open_data_folder(folder, create=create)
    except OSError as error:
        if type(error).__name__ == "StoreLocked":
            report.add(f"The data folder is in use by another BBC EFB window, so the Logbook is read-only "
                       f"here: {error}", logging.WARNING)
            try:
                return open_data_folder(folder, read_only=True)
            except OSError as second:
                report.add(f"The data folder could not be opened read-only either: {second}", logging.ERROR)
                return None
        report.add(f"The data folder {folder} could not be opened: {error}", logging.ERROR)
        return None


def basic_first_launch_prompt(parent, folder_settings, report: StartupReport):
    """Built-in first-launch folder choice, used when Settings offers none.

    Explains why a folder is needed, lets the pilot pick or create one, warns about
    OneDrive, network and drive-root folders, and remembers the choice.
    """
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from .storage_settings import data_folder_notes

    box = QMessageBox(QMessageBox.Icon.Information, "Choose your Logbook data folder",
                      "BBC EFB keeps your flights in a folder you choose on this PC.",
                      QMessageBox.StandardButton.NoButton, parent)
    box.setInformativeText("Choose an existing folder or create a new one, for example 'BBC EFB Data' in "
                           "Documents. You can also do this later from the Logbook.")
    choose = box.addButton("&Choose folder…", QMessageBox.ButtonRole.AcceptRole)
    later = box.addButton("&Not now", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(choose)
    box.setEscapeButton(later)
    box.exec()
    if box.clickedButton() is not choose:
        report.add("No data folder was chosen at start-up; flights cannot be saved until one is chosen.")
        return None
    folder = QFileDialog.getExistingDirectory(parent, "Choose the Logbook data folder")
    if not folder:
        report.add("No data folder was chosen at start-up; flights cannot be saved until one is chosen.")
        return None
    notes = data_folder_notes(folder)
    if notes:
        QMessageBox.warning(parent, "About this folder", "\n\n".join(notes))
    store = _open_store(Path(folder), report, create=True)
    if store is not None and not getattr(store, "read_only", False):
        try:
            folder_settings.remember(store.root)
        except Exception as error:  # noqa: BLE001 - the folder is open; tell the pilot it is not remembered
            report.tell_pilot(_not_remembered(store.root, error))
    return store


def _not_remembered(root: Any, error: BaseException) -> str:
    reason = getattr(error, "strerror", None) or str(error) or type(error).__name__
    return (f"The data folder {root} is open for this session, but BBC EFB could not remember it for the "
            f"next start ({reason}). Choose it again in Settings > Data Management if it is not opened "
            "next time.")


def resolve_store(options: AppOptions, folder_settings, report: StartupReport, *, parent=None):
    """Open the Logbook data folder for this run, asking the pilot only on first launch."""
    if options.data_dir is not None:
        report.add(f"Data folder from the command line: {options.data_dir}")
        return _open_store(Path(options.data_dir), report, create=True)
    remembered = folder_settings.load() if folder_settings is not None else None
    if remembered is not None:
        store = _open_store(remembered, report, create=False)
        if store is not None:
            return store
        report.add("The remembered data folder is not available. Choose it again from the Logbook or Settings.",
                   logging.WARNING)
        return None
    if not options.ask_for_data_folder:
        report.add("No data folder has been chosen yet.")
        return None
    prompt = _optional("bbc_efb.settings", "choose_data_folder")
    if prompt is not None:
        try:
            chosen = construct_with(prompt, {
                "parent": lambda: parent, "first_launch": lambda: True,
            })
        except Exception as error:
            report.add(f"The first-launch folder prompt failed: {error}", logging.ERROR)
            return None
        return _adopt_chosen_folder(chosen, folder_settings, report)
    return basic_first_launch_prompt(parent, folder_settings, report)


def _adopt_chosen_folder(chosen: Any, folder_settings, report: StartupReport):
    """Open and remember a folder the first-launch prompt returned (a path, a store or None)."""
    if chosen is None or chosen == "":
        report.add("No data folder was chosen at start-up; flights cannot be saved until one is chosen.")
        return None
    if isinstance(chosen, (str, os.PathLike)):
        store = _open_store(Path(chosen), report, create=True)
    else:
        store = chosen  # the prompt already opened the folder
    if store is not None and not getattr(store, "read_only", False) and folder_settings is not None:
        try:
            folder_settings.remember(store.root)
        except Exception as error:  # noqa: BLE001 - the folder is open; tell the pilot it is not remembered
            report.tell_pilot(_not_remembered(store.root, error))
    return store


# ----------------------------------------------------------------------------
# Core
# ----------------------------------------------------------------------------


def _disconnected(detail: str, *, simulated: bool = False):
    """An honest local Connection for the footer when no Core host is running."""
    from bbc_core.contract import Connection, ConnectionState, Source

    name = "BBC Core simulated provider" if simulated else "MSFS 2020 SimConnect"
    return Connection(ConnectionState.DISCONNECTED, Source(name, simulated), None,
                      datetime.now(timezone.utc), detail)


def find_dll(options: AppOptions, preferences: Any = None) -> Path | None:
    """SimConnect.dll: ``--simconnect-dll``, else the file chosen in Settings, else the standard search."""
    finder = _optional("bbc_core.simconnect_sdk", "find_simconnect_dll")
    if finder is None:
        return None
    explicit = options.simconnect_dll
    if explicit is None and preferences is not None:
        try:
            chosen = preferences.get("simconnect.dll_path")
        except Exception:
            chosen = None
        explicit = Path(chosen) if chosen else None
    return finder(explicit)


@dataclass(slots=True)
class CoreSetup:
    host: Any = None
    bridge: Any = None
    initial_connection: Any = None
    dll_path: Path | None = None
    dll_switch: Any = None


def _same_file(first: Path | None, second: Path | None) -> bool:
    if first is None or second is None:
        return first is second
    try:
        return os.path.normcase(str(Path(first).resolve())) == os.path.normcase(str(Path(second).resolve()))
    except OSError:
        return os.path.normcase(str(first)) == os.path.normcase(str(second))


class SimConnectDllSwitch:
    """Re-read the SimConnect.dll choice before each connect attempt and hand a new file to the host.

    ``check`` runs on the Core thread (``CoreHost.set_before_connect``) and only reads the
    preference and the file system. When the file differs from the one in use, a factory
    for it goes to ``CoreHost.replace_provider_factory``; the host builds it at that same
    attempt (never during a connected session). ``in_use`` changes, and ``on_change`` is
    called with the new path, once the new provider has been built. When nothing is found
    the current file is kept.
    """

    def __init__(self, options: AppOptions, preferences: Any, host: Any, in_use: Path,
                 on_change: Callable[[Path], None] | None = None):
        self.options = options
        self.preferences = preferences
        self.host = host
        self.in_use = in_use
        self.requested: Path | None = None
        self.on_change = on_change

    def check(self) -> None:
        try:
            wanted = find_dll(self.options, self.preferences)
        except Exception:  # noqa: BLE001 - keep connecting with the current file
            log.exception("The SimConnect.dll choice could not be read again")
            return
        if wanted is None:
            return
        if _same_file(wanted, self.in_use):
            if self.requested is not None:
                self.requested = None
                self.host.replace_provider_factory(None)
            return
        if self.requested is not None and _same_file(wanted, self.requested):
            return
        self.requested = wanted
        build = _simconnect_provider_factory(wanted)

        def factory():
            provider = build()
            self.in_use, self.requested = wanted, None
            log.info("SimConnect.dll is now %s (chosen while BBC EFB was running).", wanted)
            if self.on_change is not None:
                try:
                    self.on_change(wanted)
                except Exception:  # noqa: BLE001
                    log.exception("The new SimConnect.dll could not be reported")
            return provider

        self.host.replace_provider_factory(factory, description=f"SimConnect.dll at {wanted}")


def _simconnect_provider_factory(dll: Path) -> Callable[[], Any]:
    def build():
        from bbc_core.simconnect import SimConnectProvider
        from bbc_core.simconnect_sdk import SimConnectSdk

        sdk = construct_with(SimConnectSdk, {"dll_path": lambda: dll, "wake_event": lambda: True})
        return SimConnectProvider(sdk)

    return build


def _simulated_provider_factory() -> Callable[[], Any]:
    def build():
        from bbc_core.simulated import SimulatedProvider

        return SimulatedProvider()

    return build


def create_core(options: AppOptions, report: StartupReport, preferences: Any = None, *,
                provider_factory: Callable[[], Any] | None = None, host_config: Any = None) -> CoreSetup:
    """Create the Core host and Qt bridge, or an honest disconnected status.

    ``provider_factory`` replaces the simulated provider under ``--simulated`` (tests
    drive a ``FlightTape`` through it); it is refused without ``--simulated``, so a
    real run can never be fed made-up data. ``host_config`` is a ``HostConfig``.
    """
    if provider_factory is not None and not options.simulated:
        raise ValueError("A replacement provider is only allowed with --simulated.")
    if options.simulated:
        factory = provider_factory or _simulated_provider_factory()
        dll = None
        report.add(SIMULATED_WARNING, logging.WARNING)
    else:
        dll = find_dll(options, preferences)
        if dll is None:
            if sys.platform != "win32":
                detail = "SimConnect needs Windows and MSFS 2020. BBC EFB is running without a simulator connection."
            else:
                detail = ("SimConnect.dll was not found next to BBC EFB or in the MSFS SDK (MSFS_SDK). "
                          "BBC EFB is running without a simulator connection.")
            report.add(detail, logging.WARNING)
            return CoreSetup(initial_connection=_disconnected(detail))
        report.add(f"Using SimConnect.dll at {dll}")
        factory = _simconnect_provider_factory(dll)

    host_class = _optional("bbc_core.host", "CoreHost")
    if host_class is None:
        detail = "The Black Box Core host is not part of this build, so BBC EFB cannot connect to the simulator."
        report.add(detail, logging.ERROR)
        return CoreSetup(initial_connection=_disconnected(detail, simulated=options.simulated), dll_path=dll)
    try:
        candidates: dict[str, Callable[[], Any]] = {
            "provider_factory": lambda: factory,
            "provider": factory,
        }
        if host_config is not None:
            candidates["config"] = lambda: host_config
        host = construct_with(host_class, candidates)
    except Exception as error:
        detail = f"Black Box Core could not start: {error}"
        report.add(detail, logging.ERROR)
        log.exception("Core host construction failed")
        return CoreSetup(initial_connection=_disconnected(detail, simulated=options.simulated), dll_path=dll)

    switch = None
    if (not options.simulated and dll is not None and getattr(host, "provider_replaceable", False)
            and callable(getattr(host, "set_before_connect", None))):
        switch = SimConnectDllSwitch(options, preferences, host, dll)
        host.set_before_connect(switch.check)

    bridge = None
    bridge_class = _optional("bbc_efb.core_bridge", "CoreBridge")
    if bridge_class is not None:
        try:
            bridge = construct_with(bridge_class, {"host": lambda: host})
        except Exception as error:
            report.add(f"The Core status bridge could not start: {error}", logging.ERROR)
            log.exception("Core bridge construction failed")
    else:
        report.add("The Core status bridge is not part of this build; SimConnect status will not update.",
                   logging.WARNING)
    for target in (host, bridge):
        start = getattr(target, "start", None)
        if callable(start):
            try:
                start()
            except Exception as error:
                report.add(f"{type(target).__name__} could not start: {error}", logging.ERROR)
                log.exception("Start failed")
    if bridge is not None and host is not None:
        log.info("Black Box Core started (%s)", "SIMULATED provider" if options.simulated else f"SimConnect {dll}")
    initial = _disconnected("Black Box Core is starting.", simulated=options.simulated)
    return CoreSetup(host=host, bridge=bridge, initial_connection=initial, dll_path=dll, dll_switch=switch)




# ----------------------------------------------------------------------------
# Recorder -> Logbook (contract C2)
# ----------------------------------------------------------------------------


class LogbookSink:
    """Fallback ``FlightSink`` for a build without ``bbc_efb.recorder_sink`` (contract C2).

    It looks the store up on every save through ``store_provider`` (the app binds it to the
    folder the Recorder was made for, see ``FlightLoggingManager``). Without a
    store that can take recorded flights it raises an honest ``OSError``: the Recorder
    then keeps the record pending and its journal for recovery, and the footer shows
    that the flight is not saved. It never converts records itself, so a flight is not
    stored half-understood.
    """

    def __init__(self, store_provider: Callable[[], Any]):
        self._current_store = store_provider

    def save_recorded_flight(self, record: dict) -> str:
        store = self._current_store()
        if store is None:
            raise OSError("No Logbook data folder is open.")
        if getattr(store, "read_only", False):
            raise OSError("The Logbook data folder is open read-only.")
        save = getattr(store, "save_recorded_flight", None)
        if not callable(save):
            raise OSError("This build cannot add recorded flights to the Logbook yet. The flight's journal is "
                          "kept so it can be recovered.")
        return save(record)


def make_logbook_sink(store_provider: Callable[[], Any]):
    """``bbc_efb.recorder_sink.LogbookSink`` when the build has it, else the fallback above."""
    preferred = _optional("bbc_efb.recorder_sink", "LogbookSink")
    if preferred is not None:
        try:
            return construct_with(preferred, {"store_provider": lambda: store_provider,
                                              "current_store": lambda: store_provider})
        except Exception:  # noqa: BLE001 - fall back to the built-in sink, but say so
            log.exception("bbc_efb.recorder_sink.LogbookSink could not be created; using the built-in sink")
    return LogbookSink(store_provider)


# ----------------------------------------------------------------------------
# Flight Logging
# ----------------------------------------------------------------------------


# Per-frame snapshots overflow a 256-deep queue within seconds if the Core thread stalls,
# which the Recorder would have to report as gaps.
RECORDER_QUEUE_SIZE = 1024


def _register_recorder_api(host: Any):
    """A read-only Core handle for the Recorder (no control groups)."""
    release = getattr(host, "release", None)
    if callable(release):
        try:
            release(RECORDER_CONSUMER)  # a previous Recorder (other data folder) may hold the name
        except Exception:  # noqa: BLE001
            log.debug("No previous Recorder consumer to release", exc_info=True)
    register = getattr(host, "register_consumer", None)
    if callable(register):
        try:
            return construct_with(register, {"name": lambda: RECORDER_CONSUMER, "control_groups": lambda: (),
                                             "diagnostics": lambda: False,
                                             "queue_size": lambda: RECORDER_QUEUE_SIZE})
        except TypeError:
            pass
    method = getattr(host, "api", None)
    if callable(method):
        return method(RECORDER_CONSUMER)
    return None


def _add_tick(host: Any, tick: Callable[[], Any]) -> Callable[[], Any] | None:
    for name in ("add_tick", "add_pump_listener"):
        method = getattr(host, name, None)
        if callable(method):
            remover = method(tick)
            return remover if callable(remover) else (lambda: None)
    return None


def _describe_recovered(item: Any) -> str:
    record = getattr(item, "record", None)
    if not isinstance(record, dict):
        record = item if isinstance(item, dict) else {}
    aircraft = record.get("aircraft") or {}
    title = aircraft.get("aircraft_title") if isinstance(aircraft, dict) else None
    started = (record.get("started") or {}).get("host_time") if isinstance(record.get("started"), dict) else None
    flight_id = record.get("flight_id") or getattr(item, "flight_id", None)
    when = started[:16].replace("T", " ") + "Z" if isinstance(started, str) and len(started) >= 16 else None
    parts = [part for part in (title, when and f"started {when}") if part]
    return ", ".join(parts) if parts else f"flight {flight_id or 'with no identifier'}"


def recover_unfinished_flights(service: Any, data_dir: Path, sink: Any) -> tuple[list[str], list[str]]:
    """Save journals a previous run left open as interrupted flights. Returns (saved, problems).

    Uses ``RecorderService.recover_unfinished()`` (C3) when the service has it, else
    ``bbc_recorder.recovery``. A journal that cannot be saved stays in
    ``recorder/in-progress`` untouched, and the problem is reported.
    """
    saved: list[str] = []
    problems: list[str] = []
    method = getattr(service, "recover_unfinished", None)
    if callable(method):
        try:
            for item in method() or ():
                ok = getattr(item, "ok", True)
                (saved if ok is not False else problems).append(
                    _describe_recovered(item) if ok is not False
                    else f"{_describe_recovered(item)}: {getattr(item, 'detail', '') or 'not saved'}")
        except Exception as error:  # noqa: BLE001
            log.exception("Recovering unfinished flights failed")
            problems.append(str(error) or type(error).__name__)
        errors = getattr(service, "recovery_errors", ())
        try:
            problems.extend(str(item) for item in (errors() if callable(errors) else errors) or ())
        except Exception:  # noqa: BLE001
            log.debug("Recovery errors could not be read", exc_info=True)
        return saved, problems
    try:
        recovery = importlib.import_module("bbc_recorder.recovery")
    except ImportError:
        return saved, problems
    try:
        found = recovery.find_unfinished(data_dir, load_events=False)
    except Exception as error:  # noqa: BLE001
        log.exception("Looking for unfinished flights failed")
        return saved, [f"Unfinished flights could not be checked: {error}"]
    for flight in found:
        if not getattr(flight, "recoverable", False):
            problems.append(f"{flight.flight_id}: {getattr(flight, 'reason', 'not recoverable')}")
            continue
        try:
            result = recovery.finalize_interrupted(flight, data_dir=data_dir, sink=sink)
        except Exception as error:  # noqa: BLE001 - the journal stays where it is
            log.exception("Unfinished flight %s could not be recovered", flight.flight_id)
            problems.append(f"{flight.flight_id}: {error}")
            continue
        saved.append(_describe_recovered(result))
    return saved, problems


def recovery_notice(saved: Sequence[str], problems: Sequence[str]) -> str:
    """Pilot-facing wording for what start-up recovery did ('' when nothing happened)."""
    parts = []
    if saved:
        count = len(saved)
        parts.append(
            f"BBC EFB found {count} flight{'s' if count != 1 else ''} that {'were' if count != 1 else 'was'} still "
            f"being logged when it last stopped, and saved {'them' if count != 1 else 'it'} to the Logbook as "
            f"interrupted: {'; '.join(saved)}. Review {'them' if count != 1 else 'it'} in the Logbook.")
    if problems:
        parts.append(
            f"{len(problems)} unfinished flight journal{'s' if len(problems) != 1 else ''} could not be saved and "
            f"{'are' if len(problems) != 1 else 'is'} kept in the data folder for another attempt: "
            f"{'; '.join(problems)}.")
    return " ".join(parts)


def create_flight_logging(core: CoreSetup, store: Any, data_dir: Path | None, options: AppOptions,
                          report: StartupReport, *, current_store: Callable[[], Any] | None = None,
                          preferences: Any = None, recorder_overrides: dict[str, Any] | None = None,
                          recover: bool = True, notices: list[str] | None = None):
    """Create the Recorder service for one data folder, adapted for the footer.

    The Recorder registers its own read-only consumer with the Core host (not the Qt
    bridge) and is ticked on the Core thread, so Flight Logging continues while the
    window is minimised or another Section is open. Unfinished journals are recovered
    first (``notices`` receives the pilot-facing notice).
    """
    if store is None:
        return UnavailableFlightLogging("No data folder is open, so flights cannot be logged. Choose a data "
                                        "folder from the Logbook or Settings > Data Management.")
    if getattr(store, "read_only", False):
        return UnavailableFlightLogging("The data folder is open read-only, so flights cannot be logged here.")
    service_class = _optional("bbc_recorder.service", "RecorderService")
    if service_class is None:
        return UnavailableFlightLogging()
    if core.host is None:
        return UnavailableFlightLogging("Black Box Core is not running, so flights cannot be logged "
                                        "automatically. See the SimConnect status.")
    current_store = current_store or (lambda: store)
    sink = make_logbook_sink(current_store)
    overrides = dict(recorder_overrides or {})

    candidates: dict[str, Callable[[], Any]] = {
        "api": lambda: _register_recorder_api(core.host),
        "data_dir": lambda: data_dir,
        "sink": lambda: sink,
        "app_version": lambda: VERSION,
    }
    for name, value in overrides.items():
        candidates[name] = (lambda value=value: value)
    try:
        service = construct_with(service_class, candidates)
    except Exception as error:
        report.add(f"The Flight Recorder could not start: {error}", logging.ERROR)
        log.exception("Recorder construction failed")
        return UnavailableFlightLogging(f"The Flight Recorder could not start: {error}")
    if recover and data_dir is not None:
        saved, problems = recover_unfinished_flights(service, Path(data_dir), sink)
        notice = recovery_notice(saved, problems)
        if notice:
            report.add(notice, logging.WARNING)
            if notices is not None:
                notices.append(notice)
    removers: list[Callable[[], Any]] = []
    tick = getattr(service, "tick", None)
    remover = _add_tick(core.host, tick) if callable(tick) else None
    if remover is not None:
        removers.append(remover)
    else:
        report.add("The Flight Recorder cannot be driven by this Core host, so automatic logging will not "
                   "advance.", logging.ERROR)
    release = getattr(core.host, "release", None)
    if callable(release):
        removers.append(lambda: release(RECORDER_CONSUMER))
    if preferences is not None and callable(getattr(service, "set_automatic", None)):
        try:
            service.set_automatic(bool(preferences.get("flight_logging.automatic")))
        except Exception:
            log.exception("Automatic Flight Logging preference could not be applied")
    if options.simulated:
        report.add("Flight Logging uses SIMULATED data in this session.", logging.WARNING)
    submit = getattr(core.host, "submit", None)
    adapter = RecorderServiceAdapter(service, simulated=options.simulated, before_shutdown=(),
                                     run_on_core=submit if callable(submit) else None)
    # Remove the tick first, then close the service (it saves an open flight), then free the consumer.
    adapter.before_shutdown = removers[:1]
    adapter.after_shutdown = removers[1:]
    return adapter


class FlightLoggingManager:
    """``services.logging``: the one Flight Logging control, following the data folder.

    It implements the footer's ``FlightLogging`` protocol by delegating to the
    Recorder adapter for the open data folder, or to an honest ``Unavailable`` status.
    ``attach(store)`` is called at start-up and by ``DataFolderControl`` whenever the
    folder changes or is released (the control refuses while a flight is open, so a
    switch never cuts a flight in two). Each Recorder saves into the store it was made
    for, so a save retried while a folder switch closes it stays in its own folder.
    """

    def __init__(self, core: CoreSetup, options: AppOptions, report: StartupReport, *,
                 store_provider: Callable[[], Any], preferences: Any = None,
                 recorder_overrides: dict[str, Any] | None = None, notices: list[str] | None = None):
        self.core = core
        self.options = options
        self.report = report
        self.store_provider = store_provider
        self.preferences = preferences
        self.recorder_overrides = recorder_overrides
        self.notices = notices if notices is not None else []
        self.current: Any = UnavailableFlightLogging("No data folder is open, so flights cannot be logged.")
        self.store: Any = None

    # Lifecycle

    def attach(self, store: Any) -> None:
        """Use ``store`` (None to stop logging). The old Recorder is closed first."""
        if store is self.store and store is not None:
            return
        self.detach()
        self.store = store
        data_dir = Path(store.root) if store is not None and getattr(store, "root", None) else None
        self.current = create_flight_logging(
            self.core, store, data_dir, self.options, self.report, current_store=self._bound_store(store),
            preferences=self.preferences, recorder_overrides=self.recorder_overrides, notices=self.notices)

    @staticmethod
    def _bound_store(store: Any) -> Callable[[], Any]:
        """The Logbook sink's store for one Recorder: always the folder that Recorder was made for.

        The Recorder's journals live in ``store``'s folder, so its flights (including a save
        retried when it is closed during a folder switch) must go to that folder's Logbook,
        never to whichever folder ``services.store`` points at by then. Once ``store`` is no
        longer the open folder's store and has been closed, saving fails honestly and the
        journal stays in its own folder for recovery.
        """
        return lambda: store

    def resume_after_failed_exit(self) -> bool:
        """Start Flight Logging again after a Save & Exit that failed or timed out.

        Save & Exit closes the Recorder whether or not the save worked. When the pilot then
        chooses to stay, a fresh Recorder is made for the same data folder, so later flights
        are logged and the unsaved flight's journal is offered for saving again (journals a
        still-running save holds are locked and left alone). Returns True when it restarted.
        """
        store = self.store
        service = self.service
        if store is None or not bool(getattr(service, "closed", False)):
            return False
        self.detach()
        self.attach(store)
        return True

    def detach(self) -> None:
        current, self.current = self.current, UnavailableFlightLogging(
            "No data folder is open, so flights cannot be logged.")
        self.store = None
        shutdown = getattr(current, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:
                log.exception("The Flight Recorder did not close cleanly")

    # FlightLogging protocol

    @property
    def service(self) -> Any:
        return getattr(self.current, "service", None)

    def status(self) -> LoggingStatus:
        return self.current.status()

    def start(self):
        return self.current.start()

    def stop(self):
        return self.current.stop()

    def save_and_exit(self, reason: str):
        return self.current.save_and_exit(reason)

    def set_automatic(self, enabled: bool) -> None:
        method = getattr(self.current, "set_automatic", None)
        if callable(method):
            method(bool(enabled))

    def shutdown(self) -> None:
        self.detach()


# ----------------------------------------------------------------------------
# Drop-in modules (experimental, off by default)
# ----------------------------------------------------------------------------


def create_module_host(store: Any, bridge: Any, report: StartupReport):
    """``bbc_efb.modules.ModuleHost`` for the open data folder, or None.

    The experimental switch is persisted by the module host itself and is off until
    the pilot turns it on in Settings > Modules, after reading the trust notice.
    """
    if store is None or getattr(store, "read_only", False) or bridge is None:
        return None
    try:
        modules = importlib.import_module("bbc_efb.modules")
    except ImportError:
        return None
    try:
        host = modules.ModuleHost(Path(store.root), modules.bridge_api_provider(bridge))
        host.scan()
        if host.experimental_enabled:
            host.load_enabled()
            report.add("Drop-in modules (experimental) are switched on.", logging.WARNING)
    except Exception as error:  # noqa: BLE001 - modules must never stop the EFB from starting
        log.exception("The module host could not start")
        report.add(f"Drop-in modules are unavailable: {error}", logging.WARNING)
        return None
    return host


# ----------------------------------------------------------------------------
# Services and run
# ----------------------------------------------------------------------------


def shutdown_services(services: ShellServices, *, logbook_store: Any = None) -> None:
    """Stop the Recorder, modules, bridge and host, then close data stores. Every step is attempted."""
    steps: list[tuple[str, Callable[[], Any]]] = []
    shutdown = getattr(services.logging, "shutdown", None)
    if callable(shutdown):
        steps.append(("Flight Recorder", shutdown))
    module_host = getattr(services, "module_host", None)
    if module_host is not None and callable(getattr(module_host, "close", None)):
        steps.append(("Drop-in modules", module_host.close))
    for name, target in (("Core bridge", services.bridge), ("Core host", services.host)):
        for method_name in ("stop", "shutdown", "close"):
            method = getattr(target, method_name, None)
            if callable(method):
                steps.append((name, method))
                break
    for hook in list(services.shutdown_hooks):
        steps.append(("shutdown hook", hook))
    stores = []
    for store in (logbook_store, services.store):
        if store is not None and all(store is not other for other in stores):
            stores.append(store)
    for store in stores:
        close = getattr(store, "close", None)
        if callable(close):
            steps.append(("data folder", close))
    for name, step in steps:
        try:
            step()
        except Exception:
            log.exception("%s did not shut down cleanly", name)


def _load_preferences(report: StartupReport):
    loader = _optional("bbc_efb.preferences", "Preferences")
    if loader is None:
        return None
    try:
        preferences = loader.load()
    except Exception as error:
        report.add(f"Preferences could not be loaded: {error}", logging.ERROR)
        return None
    problem = getattr(preferences, "problem", None)
    if problem:
        report.add(str(problem), logging.WARNING)
    return preferences


def bootstrap_logging(level: str = "INFO", report: StartupReport | None = None):
    """Configure logging into the per-user folder unless it is already configured."""
    try:
        from . import logging_setup
    except ImportError:  # pragma: no cover - part of every build
        logging.basicConfig(level=level)
        return None
    current = logging_setup.current_configuration()
    if current is not None:
        return current
    try:
        return logging_setup.configure_logging(None, level)
    except Exception as error:  # noqa: BLE001 - never stop start-up for logging
        logging.basicConfig(level=logging.INFO)
        if report is not None:
            report.add(f"Logging could not be set up: {error}", logging.ERROR)
        return None


def point_logging_at(store: Any, report: StartupReport, *, replay: Sequence[str] = ()):
    """Move logging into ``<data folder>/logs`` and write the start-up notes so far there.

    A folder another EFB is writing to (read-only here) keeps the per-user log, so two
    processes never rotate the same file.
    """
    from . import logging_setup

    if store is None:
        return logging_setup.current_configuration()
    if getattr(store, "read_only", False):
        report.add("The log stays in the per-user log folder because another BBC EFB window is writing to "
                   "this data folder.")
        return logging_setup.current_configuration()
    config = logging_setup.relocate_logging(Path(store.root))
    if config is None:
        return None
    for note in replay:
        log.info("Start-up (before the data folder was known): %s", note)
    reason = getattr(config, "fallback_reason", None)
    if reason:
        report.add(f"Log file is not in the data folder: {reason}", logging.WARNING)
    return config


RESPONSE_TIMES_TIMEOUT_S = 5.0


def _resolve(value: Any, timeout_s: float = RESPONSE_TIMES_TIMEOUT_S) -> Any:
    """The value itself, or a Future's result (``CoreHost.call`` returns a Future, C4)."""
    result = getattr(value, "result", None)
    return result(timeout_s) if callable(result) else value


def response_time_measurer(host: Any) -> Callable[[], Any] | None:
    """A blocking ``() -> response times`` for Settings (it runs off the UI thread), or None.

    Prefers ``CoreHost.run(fn, timeout)``; otherwise waits on the Future ``call(fn)`` returns.
    """
    measure = lambda engine: engine.response_times()  # noqa: E731
    run = getattr(host, "run", None)
    if callable(run):
        return lambda: run(measure, RESPONSE_TIMES_TIMEOUT_S)
    call = getattr(host, "call", None)
    if callable(call):
        return lambda: _resolve(call(measure))
    return None


def build_services(options: AppOptions, *, parent=None, provider_factory: Callable[[], Any] | None = None,
                   host_config: Any = None, recorder_overrides: dict[str, Any] | None = None) -> ShellServices:
    """Everything except the window. Needs a QApplication when a prompt may be shown.

    ``provider_factory``, ``host_config`` and ``recorder_overrides`` exist for tests and
    development tools; ``provider_factory`` is accepted only with ``--simulated``.
    """
    report = StartupReport()
    services = ShellServices(simulated=options.simulated, version=VERSION)
    services.logging_config = bootstrap_logging(options.log_level, report)
    log.info("BBC EFB %s starting (simulated=%s)", VERSION, options.simulated)
    try:
        services.folder_settings = _folder_settings()
    except Exception as error:
        report.add(f"Settings storage is not available: {error}", logging.ERROR)
    services.preferences = _load_preferences(report)
    services.store = resolve_store(options, services.folder_settings, report, parent=parent)
    store = services.store
    services.data_dir = Path(store.root) if store is not None else options.data_dir
    try:
        config = point_logging_at(store, report, replay=tuple(report.notes))
        if config is not None:
            services.logging_config = config
    except Exception as error:  # noqa: BLE001
        report.add(f"Logging could not be moved to the data folder: {error}", logging.ERROR)
    core = create_core(options, report, services.preferences, provider_factory=provider_factory,
                       host_config=host_config)
    services.host, services.bridge = core.host, core.bridge
    services.initial_connection, services.dll_path = core.initial_connection, core.dll_path
    if core.dll_switch is not None:
        services.dll_switch = core.dll_switch
        core.dll_switch.on_change = lambda path: setattr(services, "dll_path", path)
    services.measure_response_times = response_time_measurer(core.host)
    manager = FlightLoggingManager(core, options, report, store_provider=lambda: services.store,
                                   preferences=services.preferences, recorder_overrides=recorder_overrides,
                                   notices=services.notices)
    manager.attach(store)
    services.logging = manager
    services.module_host = create_module_host(store, core.bridge, report)
    control = DataFolderControl(services)
    services.data_folder = control

    def follow_folder(new_store: Any) -> None:
        manager.attach(new_store)
        previous = services.module_host
        if previous is not None:
            try:
                previous.close()
            except Exception:  # noqa: BLE001
                log.exception("Drop-in modules did not close cleanly")
        services.module_host = create_module_host(new_store, core.bridge, report)

    control.listeners.append(follow_folder)
    source = getattr(core.bridge, "set_recorder_source", None)
    if callable(source):
        source(manager.status)
    services.startup_notes = tuple(report.notes)
    for notice in report.pilot_notices:
        if notice not in services.notices:
            services.notices.append(notice)
    return services


def run(options: AppOptions, argv: Sequence[str] | None = None) -> int:
    """Start the EFB and block until the window closes. Returns the exit code."""
    bootstrap_logging(options.log_level)
    app = create_application(argv)
    services = build_services(options)
    from .shell import Shell

    window = Shell(services)
    window.show()
    try:
        code = app.exec()
    finally:
        shutdown_services(services)
        log.info("BBC EFB stopped")
    return int(code)
