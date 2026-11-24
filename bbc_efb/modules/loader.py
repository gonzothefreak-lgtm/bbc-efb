"""Discover, check, load and unload drop-in EFB modules.

EXPERIMENTAL. Drop-in modules are scheduled for the second release, so the
whole feature is OFF until the pilot turns it on
(:meth:`ModuleHost.set_experimental_enabled`). While it is off, the host does
not scan the modules folder and never imports anything from it. Built-in
modules registered in code are unaffected by that switch.

Trust: a module is ordinary Python running inside the EFB process. There is no
sandbox. The control-group grant limits which *Core commands* a module's handle
may send; it does not stop the code itself from reading files, opening sockets
or reaching into the interpreter. Pilots should install modules only from
authors they trust. This stays true until Core runs in its own process.

This file is Qt-free and SimConnect-free. It talks to Core only through an
``ApiProvider`` callable that returns a ``ConsumerAPI`` for a set of control
groups. Pass a ``CoreHost`` (or a ``Core``) and each module gets its own
consumer from ``register_consumer`` (:func:`host_api_provider`); see also
:func:`core_api_provider` and :func:`bridge_api_provider`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
import threading
from typing import Any, Protocol

from .manifest import (
    ManifestResult, ModuleManifest, Problem, ProblemCode, api_compatibility, read_manifest,
)

log = logging.getLogger(__name__)

STATE_FORMAT = 1
MODULES_FOLDER = "modules"
MODULE_DATA_FOLDER = "module_data"
STATE_FILE = "module-state.json"
EXPERIMENTAL_LABEL = "Experimental"
TRUST_NOTICE = (
    "Modules run as ordinary Python code inside the EFB, with the same access to your computer as the EFB "
    "itself. Control-group grants limit which simulator commands a module can send through Core; they are "
    "not a security sandbox. Install modules only from authors you trust."
)


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


# -- Control groups -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ControlGroup:
    """A named set of Core write commands that a module may be granted."""

    name: str
    description: str
    warning: str = ""


DEFAULT_CONTROL_GROUPS: Mapping[str, ControlGroup] = {
    group.name: group for group in (
        ControlGroup("radios", "Tune and swap radio frequencies."),
        ControlGroup("engine_controls", "Move engine controls such as the throttle."),
        ControlGroup("electrical", "Change electrical bus connections."),
        ControlGroup("aircraft_inputs", "Set aircraft Input Events (cockpit knobs, switches and levers)."),
        ControlGroup("client_data", "Write Client Data areas shared with other simulator add-ons."),
        ControlGroup("simvar_writes", "Write any documented simulator variable.",
                     "Broad access: the module can change most aircraft and simulator state."),
        ControlGroup("client_events", "Send any documented simulator event.",
                     "Broad access: the module can trigger most cockpit and simulator commands."),
        ControlGroup("ai_objects", "Create, move and remove AI aircraft and simulated objects.",
                     "Can add or remove traffic and objects in your session."),
        ControlGroup("flight_files", "Save the current flight and load flights or flight plans.",
                     "Can replace your current flight or flight plan. A save can overwrite a file."),
        ControlGroup("reserved_keys", "Capture reserved simulator key combinations such as Tab combinations.",
                     "Can take over simulator keyboard shortcuts."),
        ControlGroup("simulator_actions", "Run experimental simulator actions.",
                     "Experimental simulator calls whose native behaviour is not yet validated."),
    )
}


# -- Persisted preferences ----------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ModuleSetting:
    enabled: bool = False
    granted_groups: tuple[str, ...] = ()
    granted_for_version: str = ""


@dataclass(frozen=True, slots=True)
class ModulePreferences:
    experimental_enabled: bool = False
    modules: Mapping[str, ModuleSetting] = field(default_factory=dict)


NEWER_STATE_MESSAGE = (
    "Module settings were written by a newer EFB. They are kept unchanged, and module settings cannot be "
    "changed in this EFB until that newer EFB is used again or the file is removed."
)


class ModuleSettingsReadOnly(OSError):
    """Module settings cannot be saved because a newer EFB wrote them (they are kept unchanged)."""


class ModuleStateStore:
    """``module-state.json``: the experimental switch, enabled modules and grants.

    A corrupt file is reported and treated as defaults. It is moved to
    ``module-state.json.corrupt`` (or a timestamped ``.corrupt`` name when that
    backup already exists) before the first save replaces it, so nothing the
    pilot set is silently destroyed.

    A file written by a newer EFB (a higher ``format``) is not corrupt: it is
    left exactly as it is. The store becomes read-only (:attr:`read_only`) and
    :meth:`save` refuses with :class:`ModuleSettingsReadOnly`, so this EFB can
    never push the newer build's settings aside.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.load_problem = ""
        self._corrupt = False
        self._newer = False

    @property
    def read_only(self) -> bool:
        """True when the file was written by a newer EFB and must be kept unchanged."""
        return self._newer

    def load(self) -> ModulePreferences:
        self.load_problem = ""
        self._corrupt = False
        self._newer = False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ModulePreferences()
        except (OSError, ValueError) as error:
            return self._bad(f"Module settings could not be read ({error}); using defaults.")
        if not isinstance(payload, dict) or type(payload.get("format")) is not int:
            return self._bad("Module settings are not in a recognised format; using defaults.")
        if payload["format"] > STATE_FORMAT:
            message = NEWER_STATE_MESSAGE
            log.warning("%s: %s", self.path, message)
            self.load_problem = message
            self._newer = True
            return ModulePreferences()
        modules = {}
        raw_modules = payload.get("modules", {})
        if isinstance(raw_modules, dict):
            for key, raw in raw_modules.items():
                if not isinstance(key, str) or not isinstance(raw, dict):
                    continue
                groups = raw.get("granted_groups", [])
                if not isinstance(groups, list):
                    groups = []
                version = raw.get("granted_for_version", "")
                modules[key] = ModuleSetting(
                    raw.get("enabled") is True,
                    tuple(sorted({item for item in groups if isinstance(item, str)})),
                    version if isinstance(version, str) else "",
                )
        return ModulePreferences(payload.get("experimental_enabled") is True, modules)

    def _bad(self, message):
        log.warning("%s: %s", self.path, message)
        self.load_problem = message
        self._corrupt = True
        return ModulePreferences()

    def save(self, preferences: ModulePreferences) -> None:
        if self._newer:
            raise ModuleSettingsReadOnly(NEWER_STATE_MESSAGE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._corrupt and self.path.exists():
            backup = self.path.with_name(self.path.name + ".corrupt")
            if backup.exists():
                # Never overwrite an earlier backup: a second bad file gets its own name.
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = self.path.with_name(f"{self.path.name}.{stamp}.corrupt")
            try:
                os.replace(self.path, backup)
                log.warning("Kept unreadable module settings as %s", backup)
            except OSError as error:
                log.warning("Could not keep unreadable module settings: %s", error)
            self._corrupt = False
        payload = {
            "format": STATE_FORMAT,
            "experimental_enabled": preferences.experimental_enabled,
            "modules": {
                key: {
                    "enabled": setting.enabled,
                    "granted_groups": list(setting.granted_groups),
                    "granted_for_version": setting.granted_for_version,
                }
                for key, setting in sorted(preferences.modules.items())
            },
        }
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, prefix=".module-state-",
                                suffix=".tmp", delete=False) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        try:
            os.replace(temporary, self.path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise


# -- Policy -------------------------------------------------------------------------------------

class PartialGrant(StrEnum):
    """What to do when a module requests control groups the pilot has not granted."""

    LOAD_GRANTED = "load_granted"   # load with only the granted groups; the rest are rejected by Core
    REFUSE = "refuse"               # do not load until every requested group is granted


@dataclass(frozen=True, slots=True)
class ModulePolicy:
    """Owner-decision defaults. Each field is a PROPOSAL documented in docs/MODULE_AUTHORING.md."""

    partial_grants: PartialGrant = PartialGrant.LOAD_GRANTED
    regrant_on_version_change: bool = True
    auto_grant_builtin: bool = True
    tick_failure_limit: int = 3
    max_modules: int = 100


# -- Records ------------------------------------------------------------------------------------

class ModuleStatus(StrEnum):
    INVALID = "invalid"                      # bad manifest, duplicate id or name conflict
    INCOMPATIBLE = "incompatible"            # API version or unknown control group
    MISSING_PREREQUISITE = "missing_prerequisite"
    DISABLED = "disabled"                    # valid, but the pilot has not enabled it
    READY = "ready"                          # valid and enabled, not loaded yet
    LOADED = "loaded"
    FAILED = "failed"                        # the last load or tick raised


_STATUS_TEXT = {
    ModuleStatus.INVALID: "Cannot be used",
    ModuleStatus.INCOMPATIBLE: "Not compatible with this EFB",
    ModuleStatus.MISSING_PREREQUISITE: "Missing a prerequisite",
    ModuleStatus.DISABLED: "Off",
    ModuleStatus.READY: "On, not running",
    ModuleStatus.LOADED: "Running",
    ModuleStatus.FAILED: "Stopped after an error",
}


@dataclass(frozen=True, slots=True)
class ModuleRecord:
    """One module as the Settings page should show it. Immutable snapshot."""

    key: str
    manifest: ModuleManifest | None
    folder: Path | None
    builtin: bool
    status: ModuleStatus
    enabled: bool
    requested_groups: tuple[str, ...] = ()
    granted_groups: tuple[str, ...] = ()
    problems: tuple[Problem, ...] = ()
    warnings: tuple[Problem, ...] = ()

    @property
    def id(self) -> str | None:
        return self.manifest.id if self.manifest is not None else None

    @property
    def name(self) -> str:
        return self.manifest.name if self.manifest is not None else (self.folder.name if self.folder else self.key)

    @property
    def missing_groups(self) -> tuple[str, ...]:
        return tuple(group for group in self.requested_groups if group not in self.granted_groups)

    @property
    def summary(self) -> str:
        label = _STATUS_TEXT[self.status]
        if self.problems:
            return f"{label}: {self.problems[0].message}"
        return label


@dataclass(frozen=True, slots=True)
class ModuleContext:
    """What a module's factory receives. Keep a reference; it is valid until ``close()``."""

    api: Any
    manifest: ModuleManifest
    module_dir: Path | None
    data_dir: Path
    granted_groups: frozenset[str]
    denied_groups: frozenset[str]
    host_api_version: str
    builtin: bool
    log: logging.Logger

    def ensure_data_dir(self) -> Path:
        """Create and return this module's own data folder (``<data folder>/module_data/<id>``)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


class ApiProvider(Protocol):
    def __call__(self, name: str, control_groups: tuple[str, ...]) -> Any: ...


ModuleFactory = Callable[[ModuleContext], Any]


def core_api_provider(core) -> ApiProvider:
    """Adapter for a ``bbc_core.core.Core`` owned on the calling thread (tests, headless hosts)."""

    def provide(name: str, control_groups: tuple[str, ...]):
        return core.register_consumer(control_groups=tuple(control_groups), diagnostics=False)

    return provide


class _ReleasingAPI:
    """A consumer handle whose ``close()`` releases its unique name in the host (once)."""

    __slots__ = ("_api", "_release", "_released", "_lock")

    def __init__(self, api, release: Callable[[], Any]):
        self._api = api
        self._release = release
        self._released = False
        self._lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self._api, name)

    def close(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()


def _named_provider(register, release, prefix: str) -> ApiProvider:
    """Use a host's named ``api(name, ...)`` registry with a unique name per handle.

    Names must be unique in the host, so a restarted module (or the capability probe) would
    collide with its own earlier handle; every handle therefore gets a fresh name, and closing
    the handle releases that name (which also closes the Core consumer).
    """
    counter = iter(range(1, 1 << 62))
    lock = threading.Lock()

    def provide(name: str, control_groups: tuple[str, ...]):
        with lock:
            number = next(counter)
        consumer_name = f"{prefix}{name}#{number}"
        api = register(consumer_name, control_groups=tuple(control_groups), diagnostics=False)
        if callable(release):
            return _ReleasingAPI(api, lambda: release(consumer_name))
        return api

    return provide


def host_api_provider(host) -> ApiProvider:
    """Adapter for ``bbc_core.host.CoreHost`` (shared contract C4), or a ``Core``.

    Each module handle is its own unnamed Core consumer from ``host.register_consumer(...)``.
    The module polls it itself (from ``tick()`` on the UI thread; Core's lock makes that safe
    beside the Core thread's pump). A host that only offers the named ``api(name, ...)`` and
    ``release(name)`` pair is used with a unique name per handle, released on close.
    """
    register = getattr(host, "register_consumer", None)
    if callable(register):
        def provide(name: str, control_groups: tuple[str, ...]):
            return register(control_groups=tuple(control_groups), diagnostics=False)

        return provide
    named = getattr(host, "api", None)
    if callable(named):
        return _named_provider(named, getattr(host, "release", None), "module:")
    raise TypeError("Modules need a Core host with register_consumer() or api().")


def bridge_api_provider(bridge) -> ApiProvider:
    """Adapter for ``bbc_efb.core_bridge.CoreBridge``.

    A module must poll its own handle. Consumers made with ``CoreBridge.api()`` are drained by
    the bridge's timer onto a Qt channel, which would take the module's updates away from its
    ``poll()``. So the bridge's ``host`` is used through :func:`host_api_provider` when the
    bridge exposes it. Only a bridge without a host falls back to ``CoreBridge.api()`` with a
    unique name per handle, released through ``CoreBridge.release(name)`` on close.
    """
    host = getattr(bridge, "host", None)
    if host is not None and (callable(getattr(host, "register_consumer", None)) or callable(getattr(host, "api", None))):
        return host_api_provider(host)
    return _named_provider(bridge.api, getattr(bridge, "release", None), "module:")


def as_api_provider(target) -> ApiProvider:
    """An ``ApiProvider`` callable as is; a CoreHost or Core through :func:`host_api_provider`."""
    if callable(getattr(target, "register_consumer", None)) or (
            not callable(target) and callable(getattr(target, "api", None))):
        return host_api_provider(target)
    if callable(target):
        return target
    raise TypeError("ModuleHost needs an ApiProvider callable, a CoreHost or a Core.")


class ScopedAPI:
    """The handle a module receives: forwards public ``ConsumerAPI`` calls only.

    This is a guard against accidents (reaching into Core internals, using a
    handle after unload), not a security boundary. ``close()`` is idempotent and
    the host always closes the handle when the module unloads.
    """

    __slots__ = ("_api", "_closed", "granted_groups", "_lock")

    def __init__(self, api, granted_groups: frozenset[str]):
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "granted_groups", granted_groups)
        object.__setattr__(self, "_lock", threading.Lock())

    @property
    def closed(self) -> bool:
        return self._closed

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(f"Modules cannot use Core internals ({name}).")
        if self._closed:
            raise RuntimeError("This module's Core connection is closed.")
        return getattr(self._api, name)

    def __setattr__(self, name, value):
        raise AttributeError("The module API handle is read-only.")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            object.__setattr__(self, "_closed", True)
        close = getattr(self._api, "close", None)
        if close is not None:
            close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<ScopedAPI {state} groups={sorted(self.granted_groups)}>"


# -- Host ---------------------------------------------------------------------------------------

@dataclass(slots=True)
class _Entry:
    key: str
    manifest: ModuleManifest | None
    folder: Path | None
    builtin: bool
    factory: ModuleFactory | None = None
    problems: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    static_status: ModuleStatus | None = None
    failure: tuple[ModuleStatus, Problem] | None = None


@dataclass(slots=True)
class _Loaded:
    key: str
    api: ScopedAPI
    instance: Any
    context: ModuleContext
    path_entry: str | None
    package: str | None
    tick_failures: int = 0


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def _describe(error: BaseException) -> str:
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


class ModuleHost:
    """Own the module lifecycle for one data folder. Call from one thread (the UI thread).

    Typical use::

        host = ModuleHost(data_folder, core_host)   # CoreHost.register_consumer per module
        host.register_builtin(radio_manifest, RadioModule)
        host.scan()
        host.load_enabled()
        ... host.tick() from the UI timer ...
        host.close()
    """

    def __init__(self, data_folder: Path, api_provider: Any, *, api_version: str | None = None,
                 control_groups: Mapping[str, ControlGroup] | None = None,
                 policy: ModulePolicy | None = None, state_path: Path | None = None,
                 modules_folder: Path | None = None):
        self.data_folder = Path(data_folder)
        self.modules_folder = Path(modules_folder) if modules_folder else self.data_folder / MODULES_FOLDER
        self._api_provider = as_api_provider(api_provider)
        if api_version is None:
            from bbc_core.contract import API_VERSION
            api_version = API_VERSION
        self.api_version = api_version
        self.control_groups = dict(control_groups if control_groups is not None else DEFAULT_CONTROL_GROUPS)
        self.policy = policy or ModulePolicy()
        self._store = ModuleStateStore(state_path or self.data_folder / STATE_FILE)
        self._preferences = self._store.load()
        self._lock = threading.RLock()
        self._builtins: dict[str, _Entry] = {}
        self._dropins: dict[str, _Entry] = {}
        self._loaded: dict[str, _Loaded] = {}
        self._ignored: list[tuple[Path, str]] = []
        self._scanned = False
        self._listeners: list[Callable[[], None]] = []
        self._change_depth = 0
        self._change_before: tuple[str, ...] = ()

    # -- change notification -------------------------------------------------------------------

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Call ``callback()`` whenever the set of running modules changes.

        The EFB shell uses this to start its module timer when a module is turned on from
        Settings, and to stop it when the last module stops. Callbacks run on the calling
        thread after the change; an error in one is logged and never undoes the change.
        """
        if not callable(callback):
            raise TypeError("A module-change listener must be callable.")
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _changes(self) -> "_ChangeScope":
        return _ChangeScope(self)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - a listener must never break the module host
                log.exception("A module-change listener failed")

    def _require_writable(self) -> None:
        if self._store.read_only:
            raise ModuleSettingsReadOnly(NEWER_STATE_MESSAGE)

    @property
    def settings_read_only(self) -> bool:
        """True when module settings were written by a newer EFB and cannot be changed here."""
        return self._store.read_only

    # -- preferences ---------------------------------------------------------------------------

    @property
    def experimental_enabled(self) -> bool:
        return self._preferences.experimental_enabled

    @property
    def settings_problem(self) -> str:
        """A plain-English note when module settings could not be read, else ''."""
        return self._store.load_problem

    def feature_message(self) -> str:
        if not self.experimental_enabled:
            return f"Drop-in modules ({EXPERIMENTAL_LABEL.lower()}) are off. Turn them on in Settings to use them."
        if not self.modules_folder.is_dir():
            return f"Drop-in modules ({EXPERIMENTAL_LABEL.lower()}) are on. No modules folder yet at {self.modules_folder}."
        return f"Drop-in modules ({EXPERIMENTAL_LABEL.lower()}) are on. Modules folder: {self.modules_folder}."

    def set_experimental_enabled(self, enabled: bool) -> None:
        """Turn the drop-in feature on or off. Off unloads every drop-in module immediately."""
        with self._lock, self._changes():
            self._require_writable()
            enabled = bool(enabled)
            if enabled == self._preferences.experimental_enabled:
                return
            if not enabled:
                for key in self._load_order(list(self._loaded))[::-1]:
                    if key in self._dropins:
                        for dependent in self._dependents(key)[::-1]:
                            self._unload(dependent, None)
                        self._unload(key, None)
                self._dropins.clear()
                self._ignored.clear()
            self._preferences = replace(self._preferences, experimental_enabled=enabled)
            self._store.save(self._preferences)
            if not enabled:
                self._check_dependencies()
            if enabled:
                try:
                    self.modules_folder.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    log.warning("Could not create the modules folder %s: %s", self.modules_folder, error)

    def _setting(self, key: str) -> ModuleSetting:
        setting = self._preferences.modules.get(key)
        if setting is not None:
            return setting
        return ModuleSetting(enabled=key in self._builtins)

    def _put_setting(self, key: str, setting: ModuleSetting) -> None:
        modules = dict(self._preferences.modules)
        modules[key] = setting
        self._preferences = replace(self._preferences, modules=modules)
        self._store.save(self._preferences)

    # -- registration and discovery ------------------------------------------------------------

    def register_builtin(self, manifest: ModuleManifest, factory: ModuleFactory) -> ModuleRecord:
        """Declare a module that ships with the EFB. Its requested groups are granted automatically."""
        if not isinstance(manifest, ModuleManifest):
            raise TypeError("Supply a ModuleManifest (see ModuleManifest.build).")
        if not callable(factory):
            raise TypeError("A built-in module needs a callable factory.")
        with self._lock:
            if manifest.id in self._builtins:
                raise ValueError(f"Built-in module {manifest.id} is already registered.")
            entry = _Entry(manifest.id, manifest, None, True, factory)
            self._check_static(entry)
            self._builtins[manifest.id] = entry
            if self._scanned and self.experimental_enabled:
                self._scan()
            return self._record(manifest.id)

    def scan(self) -> tuple[ModuleRecord, ...]:
        """Re-read the modules folder (only when the feature is on). Loaded modules keep running."""
        with self._lock:
            self._scan()
            return self.records()

    def _scan(self) -> None:
        self._scanned = True
        previous = self._dropins
        self._dropins = {}
        self._ignored = []
        if self.experimental_enabled:
            self._scan_folder(previous)
        self._check_dependencies()

    def _scan_folder(self, previous: dict[str, _Entry]) -> None:
        try:
            children = sorted(self.modules_folder.iterdir(), key=lambda item: item.name.lower())
        except FileNotFoundError:
            return
        except OSError as error:
            self._ignored.append((self.modules_folder, f"The modules folder could not be read: {error}."))
            return

        found: list[_Entry] = []
        for child in children:
            if child.name.startswith((".", "_")):
                continue
            if not child.is_dir():
                if child.suffix.lower() in (".zip", ".whl"):
                    self._ignored.append((child, "Packaged modules are not supported yet. "
                                                 "Unzip it into its own folder inside the modules folder."))
                continue
            if len(found) >= self.policy.max_modules:
                self._ignored.append((child, f"More than {self.policy.max_modules} module folders; the rest "
                                             "were not read."))
                break
            try:
                result = read_manifest(child)
            except Exception as error:  # noqa: BLE001 - one folder must never abort the scan
                log.exception("Reading the manifest in %s failed", child)
                result = ManifestResult(None, (Problem(ProblemCode.BAD_MANIFEST,
                                                       f"module.json could not be read: {_describe(error)}."),))
            entry = _Entry(result.manifest.id if result.manifest else f"folder:{child.name}",
                           result.manifest, child, False)
            entry.problems.extend(result.problems)
            entry.warnings.extend(result.warnings)
            if result.manifest is None:
                entry.static_status = ModuleStatus.INVALID
            found.append(entry)

        by_id: dict[str, list[_Entry]] = {}
        for entry in found:
            if entry.manifest is not None:
                by_id.setdefault(entry.manifest.id, []).append(entry)
        for module_id, entries in by_id.items():
            clash_builtin = module_id in self._builtins
            if len(entries) > 1 or clash_builtin:
                where = ", ".join(entry.folder.name for entry in entries)
                message = (f"The id \"{module_id}\" is used by a built-in module." if clash_builtin else
                           f"The id \"{module_id}\" is used by more than one folder ({where}). Remove all but one.")
                for entry in entries:
                    entry.key = f"{module_id}@{entry.folder.name}"
                    entry.problems.append(Problem(ProblemCode.DUPLICATE_ID, message))
                    entry.static_status = ModuleStatus.INVALID

        packages: dict[str, list[_Entry]] = {}
        for entry in found:
            if entry.static_status is None:
                packages.setdefault(entry.manifest.entry.top_level, []).append(entry)
        for package, entries in packages.items():
            if len(entries) > 1:
                names = ", ".join(entry.manifest.id for entry in entries)
                for entry in entries:
                    entry.problems.append(Problem(
                        ProblemCode.NAME_CONFLICT,
                        f"Modules {names} all use the Python package \"{package}\". Each module needs its own "
                        "package name."))
                    entry.static_status = ModuleStatus.INVALID

        # Scan-time notes on a kept (running) entry are recomputed on every scan, never accumulated.
        for entry in previous.values():
            entry.warnings = [w for w in entry.warnings if not isinstance(w, _ScanNote)]

        for entry in found:
            if entry.static_status is None:
                loaded = self._loaded.get(entry.key)
                if loaded is not None and previous.get(entry.key) is not None:
                    # Keep the record the running code was loaded from; changes apply on reload.
                    old = previous[entry.key]
                    if old.manifest != entry.manifest:
                        old.warnings.append(_CHANGED_ON_DISK)
                    self._dropins[entry.key] = old
                    continue
                self._check_static(entry)
            self._dropins[entry.key] = entry
        # A running drop-in whose key is missing from this scan stays listed until unloaded. Say why
        # honestly: its folder may be gone, or another folder may now claim the same id.
        for key, entry in previous.items():
            if key in self._loaded and key not in self._dropins:
                folder = entry.folder
                if folder is not None and (folder / "module.json").is_file():
                    clashing = [other.folder.name for other in found
                                if other.manifest is not None and other.manifest.id == key
                                and other.folder != folder]
                    if clashing:
                        entry.warnings.append(_duplicate_while_running(clashing))
                    else:
                        entry.warnings.append(_UNREADABLE_ON_DISK)
                else:
                    entry.warnings.append(_REMOVED_FROM_DISK)
                self._dropins[key] = entry

    def _check_static(self, entry: _Entry) -> None:
        manifest = entry.manifest
        if not entry.builtin and entry.folder.name != manifest.id:
            entry.warnings.append(Problem(ProblemCode.NOTE,
                                          f"The folder is named \"{entry.folder.name}\"; naming it "
                                          f"\"{manifest.id}\" makes it easier to find."))
        blocking, warning = api_compatibility(manifest, self.api_version)
        if warning is not None:
            entry.warnings.append(warning)
        if blocking is not None:
            entry.problems.append(blocking)
            entry.static_status = ModuleStatus.INCOMPATIBLE
            return
        unknown = [group for group in manifest.control_groups if group not in self.control_groups]
        if unknown:
            entry.problems.append(Problem(
                ProblemCode.UNKNOWN_CONTROL_GROUP,
                f"Requests control group{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}, which this "
                "EFB does not offer. A newer EFB may be needed."))
            entry.static_status = ModuleStatus.INCOMPATIBLE
            return
        if entry.builtin:
            return
        package = manifest.entry.top_level
        folder = entry.folder
        if not ((folder / package).is_dir() or (folder / f"{package}.py").is_file()):
            entry.problems.append(Problem(
                ProblemCode.BAD_MANIFEST,
                f"\"entry\" names the package \"{package}\", but the module folder has no \"{package}\" "
                f"folder or \"{package}.py\" file."))
            entry.static_status = ModuleStatus.INVALID
            return
        conflict = self._outside_conflict(package, entry.key)
        if conflict:
            entry.problems.append(Problem(ProblemCode.NAME_CONFLICT, conflict))
            entry.static_status = ModuleStatus.INVALID

    def _outside_conflict(self, package: str, key: str) -> str:
        if package in sys.builtin_module_names or package in getattr(sys, "stdlib_module_names", ()):
            return f"The package name \"{package}\" is a Python standard-library name. Rename the package."
        for loaded in self._loaded.values():
            if loaded.package == package:
                if loaded.key == key:
                    return ""
                return f"The package name \"{package}\" is used by the running module \"{loaded.key}\"."
        if package in sys.modules:
            return (f"The package name \"{package}\" is already used inside the EFB. Rename the package, "
                    "for example after the module id.")
        try:
            spec = importlib.util.find_spec(package)
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            return (f"The package name \"{package}\" is already installed elsewhere on this computer. "
                    "Rename the package, for example after the module id.")
        return ""

    def _check_dependencies(self) -> None:
        """Mark modules whose required modules are not available (repeats until stable)."""
        for entry in self._all_entries():
            entry.problems = [p for p in entry.problems if not getattr(p, "_dependency", False)]
            if entry.static_status is ModuleStatus.MISSING_PREREQUISITE and not entry.problems:
                entry.static_status = None
        changed = True
        while changed:
            changed = False
            for entry in self._all_entries():
                if entry.static_status is not None or entry.manifest is None:
                    continue
                for required in entry.manifest.requires.modules:
                    other = self._entry(required)
                    reason = ""
                    if other is None:
                        reason = f"Needs the module \"{required}\", which is not installed."
                    elif other.static_status is not None:
                        reason = f"Needs the module \"{required}\", which cannot be used."
                    if reason:
                        entry.problems.append(_DependencyProblem(ProblemCode.MISSING_PREREQUISITE, reason))
                        entry.static_status = ModuleStatus.MISSING_PREREQUISITE
                        changed = True
                        break

    def _all_entries(self) -> list[_Entry]:
        return [*self._builtins.values(), *self._dropins.values()]

    def _entry(self, key: str) -> _Entry | None:
        return self._builtins.get(key) or self._dropins.get(key)

    # -- records -------------------------------------------------------------------------------

    def ignored(self) -> tuple[tuple[Path, str], ...]:
        """Items in the modules folder that are not module folders, with the reason."""
        return tuple(self._ignored)

    def records(self) -> tuple[ModuleRecord, ...]:
        with self._lock:
            keys = [*sorted(self._builtins), *sorted(self._dropins)]
            return tuple(self._record(key) for key in keys)

    def record(self, key: str) -> ModuleRecord | None:
        with self._lock:
            return self._record(key) if self._entry(key) is not None else None

    def _granted(self, entry: _Entry) -> tuple[str, ...]:
        manifest = entry.manifest
        if manifest is None:
            return ()
        if entry.builtin and self.policy.auto_grant_builtin:
            return manifest.control_groups
        setting = self._setting(entry.key)
        if self.policy.regrant_on_version_change and setting.granted_for_version != str(manifest.version):
            return ()
        return tuple(group for group in manifest.control_groups if group in setting.granted_groups)

    def _record(self, key: str) -> ModuleRecord:
        entry = self._entry(key)
        setting = self._setting(key)
        requested = entry.manifest.control_groups if entry.manifest else ()
        granted = self._granted(entry)
        warnings = list(entry.warnings)
        problems = list(entry.problems)
        if key in self._loaded:
            status = ModuleStatus.LOADED
            granted = tuple(sorted(self._loaded[key].context.granted_groups))
        elif entry.static_status is not None:
            status = entry.static_status
        elif entry.failure is not None:
            status, problem = entry.failure
            problems.append(problem)
        elif not setting.enabled:
            status = ModuleStatus.DISABLED
        else:
            status = ModuleStatus.READY
        missing = [group for group in requested if group not in granted]
        if missing and entry.manifest is not None and entry.static_status is None:
            warnings.append(Problem(
                ProblemCode.NOT_GRANTED,
                f"Not granted: {', '.join(missing)}. The module can read data but Core will reject its "
                f"{'commands' if len(missing) > 1 else 'command'} in "
                f"{'these groups' if len(missing) > 1 else 'this group'}."))
        return ModuleRecord(key, entry.manifest, entry.folder, entry.builtin, status, setting.enabled,
                            requested, granted, tuple(problems), tuple(warnings))

    # -- pilot choices -------------------------------------------------------------------------

    def set_enabled(self, key: str, enabled: bool) -> ModuleRecord:
        """Remember whether the pilot wants this module. Disabling also unloads it (and its dependents)."""
        with self._lock, self._changes():
            self._require_writable()
            entry = self._require(key)
            setting = self._setting(key)
            if not enabled and key in self._loaded:
                for dependent in self._dependents(key)[::-1]:
                    self._unload(dependent, None)
                self._unload(key, None)
            self._put_setting(key, replace(setting, enabled=bool(enabled)))
            entry.failure = None
            return self._record(key)

    def grant(self, key: str, groups: Iterable[str]) -> ModuleRecord:
        """Grant control groups the module requested. Applies the next time the module loads."""
        with self._lock:
            self._require_writable()
            entry = self._require(key)
            if entry.manifest is None:
                raise ValueError("This module has no readable manifest.")
            groups = set(groups)
            unrequested = groups - set(entry.manifest.control_groups)
            if unrequested:
                raise ValueError(f"The module did not request: {', '.join(sorted(unrequested))}.")
            if entry.builtin:
                return self._record(key)
            setting = self._setting(key)
            current = set(self._granted(entry))
            self._put_setting(key, replace(setting, granted_groups=tuple(sorted(current | groups)),
                                           granted_for_version=str(entry.manifest.version)))
            if entry.failure is not None and entry.failure[1].code is ProblemCode.NOT_GRANTED:
                entry.failure = None
            return self._record(key)

    def revoke(self, key: str, groups: Iterable[str] | None = None) -> ModuleRecord:
        """Withdraw grants (all when ``groups`` is None). Applies the next time the module loads."""
        with self._lock:
            self._require_writable()
            entry = self._require(key)
            setting = self._setting(key)
            remaining = () if groups is None else tuple(g for g in setting.granted_groups if g not in set(groups))
            self._put_setting(key, replace(setting, granted_groups=remaining))
            return self._record(key)

    def _require(self, key: str) -> _Entry:
        entry = self._entry(key)
        if entry is None:
            raise KeyError(f"No module \"{key}\" is known. Scan the modules folder first.")
        return entry

    # -- load and unload -----------------------------------------------------------------------

    def load_enabled(self) -> tuple[ModuleRecord, ...]:
        """Load every enabled, usable module (built-ins always; drop-ins only when the feature is on)."""
        with self._lock, self._changes():
            if not self._scanned:
                self._scan()
            keys = [entry.key for entry in self._all_entries()
                    if entry.static_status is None and self._setting(entry.key).enabled]
            for key in self._load_order(keys):
                if key not in self._loaded:
                    self._load(key, [])
            return self.records()

    def load(self, key: str) -> ModuleRecord:
        """Load one module now (and the modules it requires). The pilot must have enabled it."""
        with self._lock, self._changes():
            entry = self._require(key)
            if key not in self._loaded:
                if not self._setting(key).enabled:
                    return replace(self._record(key), problems=(Problem(
                        ProblemCode.NOTE, "Turn the module on before loading it."),))
                self._load(key, [])
            return self._record(entry.key)

    def reload(self, key: str) -> ModuleRecord:
        """Unload and load again, picking up new code, manifest and grants."""
        with self._lock, self._changes():
            dependents = [d for d in self._dependents(key) if d in self._loaded]
            for dependent in dependents[::-1]:
                self._unload(dependent, None)
            self._unload(key, None)
            if key in self._dropins:
                self._scan()
            record = self.load(key) if self._entry(key) is not None else None
            for dependent in dependents:
                if self._entry(dependent) is not None:
                    self.load(dependent)
            return record if record is not None else self._missing_record(key)

    def unload(self, key: str) -> ModuleRecord:
        """Stop a running module (and anything that requires it). It stays enabled for next launch."""
        with self._lock, self._changes():
            self._require(key)
            for dependent in self._dependents(key)[::-1]:
                self._unload(dependent, None)
            self._unload(key, None)
            return self._record(key)

    def close(self) -> None:
        """Unload every module in reverse load order. Safe to call more than once."""
        with self._lock, self._changes():
            for key in self._load_order(list(self._loaded))[::-1]:
                self._unload(key, None)

    def instance(self, key: str) -> Any:
        """The object the module's factory returned, or None when it is not loaded."""
        loaded = self._loaded.get(key)
        return loaded.instance if loaded is not None else None

    def loaded(self) -> tuple[str, ...]:
        return tuple(self._loaded)

    def tick(self) -> None:
        """Call ``tick()`` on each running module that has one. Repeated errors stop the module.

        ``SystemExit`` from module code counts as an error like any other: it must never reach
        the UI timer slot, where it would end the EFB without the normal save-on-exit flow.
        """
        with self._lock, self._changes():
            for key in list(self._loaded):
                loaded = self._loaded.get(key)
                if loaded is None:
                    continue
                tick = getattr(loaded.instance, "tick", None)
                if not callable(tick):
                    continue
                try:
                    tick()
                    loaded.tick_failures = 0
                except (Exception, SystemExit) as error:  # noqa: BLE001 - module code is untrusted
                    loaded.tick_failures += 1
                    logging.getLogger(f"bbc_efb.modules.{key}").exception("Module tick failed")
                    if loaded.tick_failures >= self.policy.tick_failure_limit:
                        problem = Problem(ProblemCode.RUNTIME_ERROR,
                                          f"Stopped after {loaded.tick_failures} errors in a row. "
                                          f"Last error: {_describe(error)}.")
                        for dependent in self._dependents(key)[::-1]:
                            self._unload(dependent, None)
                        self._unload(key, None)
                        failed = self._entry(key)
                        if failed is not None:
                            failed.failure = (ModuleStatus.FAILED, problem)

    def _missing_record(self, key):
        return ModuleRecord(key, None, None, False, ModuleStatus.INVALID, False,
                            problems=(Problem(ProblemCode.BAD_MANIFEST, "The module is no longer installed."),))

    def _load_order(self, keys: list[str]) -> list[str]:
        order: list[str] = []
        visiting: set[str] = set()

        def visit(key):
            if key in order or key in visiting:
                return
            visiting.add(key)
            entry = self._entry(key)
            if entry is not None and entry.manifest is not None:
                for required in entry.manifest.requires.modules:
                    if self._entry(required) is not None:
                        visit(required)
            visiting.discard(key)
            order.append(key)

        for key in sorted(keys):
            visit(key)
        return [key for key in order if key in keys or key in self._loaded]

    def _dependents(self, key: str) -> list[str]:
        """Loaded modules that require ``key`` directly or indirectly, in load order."""
        result: list[str] = []
        frontier = [key]
        while frontier:
            current = frontier.pop()
            for other in self._loaded:
                entry = self._entry(other)
                if entry and entry.manifest and current in entry.manifest.requires.modules and other not in result:
                    result.append(other)
                    frontier.append(other)
        return self._load_order(result)

    def _fail(self, entry: _Entry, status: ModuleStatus, code: ProblemCode, message: str) -> bool:
        entry.failure = (status, Problem(code, message))
        log.warning("Module %s: %s", entry.key, message)
        return False

    def _load(self, key: str, chain: list[str]) -> bool:
        entry = self._entry(key)
        if entry is None:
            return False
        if key in self._loaded:
            return True
        if key in chain:
            return self._fail(entry, ModuleStatus.MISSING_PREREQUISITE, ProblemCode.MISSING_PREREQUISITE,
                              "Required modules depend on each other in a loop: " + " -> ".join([*chain, key]) + ".")
        if entry.static_status is not None:
            return False
        if not entry.builtin and not self.experimental_enabled:
            return False
        entry.failure = None
        manifest = entry.manifest

        for required in manifest.requires.modules:
            other = self._entry(required)
            if other is None or not self._setting(required).enabled:
                return self._fail(entry, ModuleStatus.MISSING_PREREQUISITE, ProblemCode.MISSING_PREREQUISITE,
                                  f"Needs the module \"{required}\". Turn it on first.")
            if not self._load(required, [*chain, key]):
                return self._fail(entry, ModuleStatus.MISSING_PREREQUISITE, ProblemCode.MISSING_PREREQUISITE,
                                  f"Needs the module \"{required}\", which did not start.")

        granted = self._granted(entry)
        denied = tuple(group for group in manifest.control_groups if group not in granted)
        if denied and self.policy.partial_grants is PartialGrant.REFUSE:
            return self._fail(entry, ModuleStatus.READY, ProblemCode.NOT_GRANTED,
                              f"Waiting for you to grant: {', '.join(denied)}.")

        path_entry = None
        package = None
        if not entry.builtin:
            package = manifest.entry.top_level
            conflict = self._outside_conflict(package, key)
            if conflict:
                return self._fail(entry, ModuleStatus.INVALID, ProblemCode.NAME_CONFLICT, conflict)
            path_entry = str(entry.folder.resolve())
            if path_entry not in sys.path:
                sys.path.append(path_entry)
            importlib.invalidate_caches()
            for name in manifest.requires.python_packages:
                try:
                    found = importlib.util.find_spec(name) is not None
                except (ImportError, ValueError):
                    found = False
                if not found:
                    self._cleanup(path_entry, package)
                    return self._fail(entry, ModuleStatus.MISSING_PREREQUISITE, ProblemCode.MISSING_PREREQUISITE,
                                      f"Needs the Python package \"{name}\", which is not installed.")

        try:
            raw_api = self._api_provider(manifest.id, tuple(granted))
        except Exception as error:  # noqa: BLE001
            self._cleanup(path_entry, package)
            return self._fail(entry, ModuleStatus.FAILED, ProblemCode.CORE_REFUSED,
                              f"Core did not give the module a connection: {_describe(error)}.")
        api = ScopedAPI(raw_api, frozenset(granted))

        def abandon(status, code, message):
            try:
                api.close()
            except Exception:  # noqa: BLE001
                log.exception("Closing the Core handle for %s failed", key)
            self._cleanup(path_entry, package)
            return self._fail(entry, status, code, message)

        if manifest.requires.capabilities:
            try:
                discovery = api.discover()
                supported = {item.capability.id for item in discovery.capabilities if item.provider_supported}
            except Exception as error:  # noqa: BLE001
                return abandon(ModuleStatus.FAILED, ProblemCode.CORE_REFUSED,
                               f"Core could not list its capabilities: {_describe(error)}.")
            missing = [cap for cap in manifest.requires.capabilities if cap not in supported]
            if missing and denied:
                # Discovery hides commands in groups this handle lacks. Ask once with every requested
                # group (host-side only, closed at once) so a missing grant is not reported as missing support.
                try:
                    probe = self._api_provider(f"{manifest.id} (capability check)", tuple(manifest.control_groups))
                    try:
                        probe_supported = {item.capability.id for item in probe.discover().capabilities
                                           if item.provider_supported}
                    finally:
                        probe.close()
                    missing = [cap for cap in missing if cap not in probe_supported]
                except Exception:  # noqa: BLE001
                    log.exception("Capability check with requested groups failed for %s", key)
            if missing:
                return abandon(ModuleStatus.MISSING_PREREQUISITE, ProblemCode.MISSING_PREREQUISITE,
                               f"Needs Core capabilit{'ies' if len(missing) > 1 else 'y'} {', '.join(missing)}, "
                               "which this EFB's simulator connection does not support.")

        if entry.builtin:
            factory = entry.factory
        else:
            try:
                imported = importlib.import_module(manifest.entry.module)
            except (Exception, SystemExit) as error:  # noqa: BLE001
                logging.getLogger(f"bbc_efb.modules.{key}").exception("Module import failed")
                missing_name = getattr(error, "name", None) if isinstance(error, ModuleNotFoundError) else None
                if missing_name and missing_name.split(".")[0] != package:
                    return abandon(ModuleStatus.MISSING_PREREQUISITE, ProblemCode.MISSING_PREREQUISITE,
                                   f"Needs the Python package \"{missing_name}\", which is not installed.")
                return abandon(ModuleStatus.FAILED, ProblemCode.IMPORT_ERROR,
                               f"Could not import {manifest.entry.module}: {_describe(error)}.")
            origin = getattr(imported, "__file__", None)
            if origin is None or not _is_within(Path(origin), entry.folder):
                return abandon(ModuleStatus.INVALID, ProblemCode.NAME_CONFLICT,
                               f"{manifest.entry.module} was found outside the module folder. Rename the package.")
            factory = imported
            try:
                for part in manifest.entry.attribute.split("."):
                    factory = getattr(factory, part)
            except AttributeError:
                return abandon(ModuleStatus.INVALID, ProblemCode.BAD_MANIFEST,
                               f"{manifest.entry.module} has no \"{manifest.entry.attribute}\".")
            if not callable(factory):
                return abandon(ModuleStatus.INVALID, ProblemCode.BAD_MANIFEST,
                               f"\"{manifest.entry}\" is not callable.")

        context = ModuleContext(
            api=api, manifest=manifest, module_dir=entry.folder,
            data_dir=self.data_folder / MODULE_DATA_FOLDER / manifest.id,
            granted_groups=frozenset(granted), denied_groups=frozenset(denied),
            host_api_version=self.api_version, builtin=entry.builtin,
            log=logging.getLogger(f"bbc_efb.modules.{manifest.id}"),
        )
        try:
            instance = factory(context)
        except (Exception, SystemExit) as error:  # noqa: BLE001
            context.log.exception("Module start-up failed")
            return abandon(ModuleStatus.FAILED, ProblemCode.FACTORY_ERROR,
                           f"The module failed to start: {_describe(error)}.")
        self._loaded[key] = _Loaded(key, api, instance, context, path_entry, package)
        log.info("Loaded module %s %s (%s) with groups %s at %s", manifest.id, manifest.version,
                 "built-in" if entry.builtin else "drop-in", sorted(granted),
                 datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return True

    def _unload(self, key: str, status_after: ModuleStatus | None) -> None:
        loaded = self._loaded.pop(key, None)
        if loaded is None:
            return
        entry = self._entry(key)
        close = getattr(loaded.instance, "close", None)
        if callable(close):
            try:
                close()
            except (Exception, SystemExit) as error:  # noqa: BLE001
                loaded.context.log.exception("Module close failed")
                if entry is not None:
                    entry.warnings = [w for w in entry.warnings if w.code is not ProblemCode.CLOSE_ERROR]
                    entry.warnings.append(Problem(ProblemCode.CLOSE_ERROR,
                                                  f"The module reported an error while stopping: {_describe(error)}."))
        try:
            loaded.api.close()
        except Exception:  # noqa: BLE001
            log.exception("Closing the Core handle for %s failed", key)
        self._cleanup(loaded.path_entry, loaded.package)
        log.info("Unloaded module %s", key)

    @staticmethod
    def _cleanup(path_entry: str | None, package: str | None) -> None:
        if path_entry is not None:
            while path_entry in sys.path:
                sys.path.remove(path_entry)
        if package:
            for name in [name for name in sys.modules if name == package or name.startswith(package + ".")]:
                del sys.modules[name]
        importlib.invalidate_caches()


class _ScanNote(Problem):
    """A note added by a scan; removed and recomputed on the next scan, never accumulated."""

    __slots__ = ()


_CHANGED_ON_DISK = _ScanNote(ProblemCode.NOTE, "The module changed on disk. Reload it to use the new version.")
_REMOVED_FROM_DISK = _ScanNote(ProblemCode.NOTE,
                               "The module folder was removed. It keeps running until it is unloaded.")
_UNREADABLE_ON_DISK = _ScanNote(ProblemCode.NOTE, "The module folder can no longer be used as it is on disk. "
                                                  "It keeps running until it is unloaded.")


def _duplicate_while_running(folders: list[str]) -> Problem:
    return _ScanNote(ProblemCode.NOTE,
                     f"Another folder ({', '.join(sorted(folders))}) now uses this module's id. This module keeps "
                     "running; remove the other folder so the id is used by one folder only.")


class _ChangeScope:
    """Notify host listeners once, when the outermost public call changed the running modules."""

    __slots__ = ("host",)

    def __init__(self, host: ModuleHost):
        self.host = host

    def __enter__(self):
        host = self.host
        if host._change_depth == 0:
            host._change_before = tuple(host._loaded)
        host._change_depth += 1
        return self

    def __exit__(self, *exc):
        host = self.host
        host._change_depth -= 1
        if host._change_depth == 0 and tuple(host._loaded) != host._change_before:
            host._notify()
        return False


class _DependencyProblem(Problem):
    """A Problem recomputed on every scan (marked so stale dependency reasons are cleared)."""

    __slots__ = ()
    _dependency = True
