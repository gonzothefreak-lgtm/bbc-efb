"""Basic saved EFB preferences (Q8): view, density, panel visibility and a few switches.

Qt-free, so the Recorder, tests and a later separate process can read the same file.

The file is UTF-8 JSON next to the data-folder settings file, but separate from it
(``%APPDATA%\\BBC EFB\\preferences.json`` on Windows,
``~/.config/bbc-efb/preferences.json`` elsewhere). Flight records never go here.

Rules that keep the file trustworthy
------------------------------------

* **Versioned.** The file carries ``"format": 1``. A file written by a newer
  format is read (values this build understands and can validate are used) but
  never written back: changes are kept for this session only and
  ``read_only_reason`` says why.
* **Unknown keys are preserved.** Keys this build does not know, and values that
  fail validation, are written back exactly as they were read. An invalid value is
  simply not *used* (the default is used instead) until the pilot changes it.
* **Atomic write.** A save writes a temporary file in the same folder, fsyncs it
  and replaces the old file with ``os.replace``. Windows sharing violations from
  antivirus or sync tools are retried briefly. A failed save leaves the old file
  untouched and raises ``PreferencesSaveError`` with a plain message.
* **A damaged file is kept.** If the file is not valid JSON, it is renamed to
  ``preferences.json.damaged-<UTC time>`` before defaults are used, and
  ``problem`` explains what happened. Nothing is silently discarded.
* **Thread safe.** One lock guards reads, writes and saves. Listeners run after
  the lock is released.

PROPOSALS for the owner (defaults chosen here, all configurable)
----------------------------------------------------------------

* ``flight_logging.automatic`` defaults to **on**: the approved operating style is
  automatic Flight Logging with manual override (Q13).
* ``flight_logging.journal_retention_days`` defaults to ``None`` (keep Recorder
  journals forever). Flight records themselves are never deleted automatically;
  the retention policy is an open owner decision, so no automatic deletion is
  implemented here. The value is stored for the Recorder to honour later.
* ``logbook.history_view`` defaults to ``table``; the on-page selector stays fast
  and Settings remembers the choice (Logbook Design Record, Q3).
* ``inflight.launcher_collapsed`` defaults to off; auto-collapse is undecided, so
  only a manual start-up preference is offered.
* Panel visibility is a ``{panel id: bool}`` map per workspace. Unknown panel ids
  (for example from later modules) are accepted and kept.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from tempfile import NamedTemporaryFile
from types import MappingProxyType
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

PREFERENCES_FORMAT = 1
PREFERENCES_FILE_NAME = "preferences.json"
MAX_PREFERENCES_BYTES = 1024 * 1024
_RETRY_WINERRORS = frozenset({5, 32, 33})
_RETRY_DELAYS_S = (0.02, 0.05, 0.1, 0.2, 0.25, 0.25)
_PANEL_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class PreferencesError(ValueError):
    """A preference key or value was refused."""


class PreferencesSaveError(OSError):
    """The preferences could not be written. The previous file is unchanged."""


def default_preferences_path() -> Path:
    """``%APPDATA%\\BBC EFB\\preferences.json`` on Windows, ``~/.config/bbc-efb/preferences.json`` elsewhere."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "BBC EFB" / PREFERENCES_FILE_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "bbc-efb" / PREFERENCES_FILE_NAME


# Validators ---------------------------------------------------------------

def _bool(value):
    if type(value) is not bool:
        raise PreferencesError("must be true or false")
    return value


def _choice(*choices):
    def check(value):
        if value not in choices:
            raise PreferencesError("must be one of " + ", ".join(choices))
        return value
    check.choices = choices
    return check


def _optional_days(value):
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 36500:
        raise PreferencesError("must be empty (keep forever) or a whole number of days from 1 to 36500")
    return value


def _panels(value):
    if not isinstance(value, Mapping):
        raise PreferencesError("must map panel names to true or false")
    result = {}
    for name, shown in value.items():
        if not isinstance(name, str) or not _PANEL_ID.match(name):
            raise PreferencesError(f"panel name {name!r} is not a valid identifier")
        if type(shown) is not bool:
            raise PreferencesError(f"visibility of {name!r} must be true or false")
        result[name] = shown
    return result


def _panels_read(value):
    """Lenient reading of a stored panel map: valid entries are used, invalid ones are
    ignored (and kept in the file) instead of discarding the whole map."""
    if not isinstance(value, Mapping):
        raise PreferencesError("must map panel names to true or false")
    return {name: shown for name, shown in value.items()
            if isinstance(name, str) and _PANEL_ID.match(name) and type(shown) is bool}


def _reject_constant(name):
    raise ValueError(f"{name} is not a valid JSON number")


def _loads(data: bytes):
    """Strict JSON: NaN and Infinity are refused, because they could never be saved again."""
    return json.loads(data.decode("utf-8-sig"), parse_constant=_reject_constant)


def _optional_text(limit):
    def check(value):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > limit:
            raise PreferencesError(f"must be empty or text of at most {limit} characters")
        if "\x00" in value:
            raise PreferencesError("must not contain a NUL character")
        return value or None
    return check


def _optional_geometry(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096 or not re.fullmatch(r"[A-Za-z0-9+/=]*", value):
        raise PreferencesError("must be empty or base64 text")
    return value or None


@dataclass(frozen=True, slots=True)
class PreferenceSpec:
    key: str
    default: Any
    check: Callable[[Any], Any]
    label: str
    description: str = ""
    read: Callable[[Any], Any] | None = None  # lenient check for stored values (default: check)

    def check_stored(self, value):
        return (self.read or self.check)(value)

    @property
    def choices(self) -> tuple[str, ...]:
        return getattr(self.check, "choices", ())


LOGBOOK_PANELS = MappingProxyType({
    "summary": "Summary metrics",
    "aircraft_profiles": "Aircraft-type profiles",
    "charts": "Charts",
    "recent_flights": "Recent flights",
    "quick_links": "Quick links",
})
INFLIGHT_PANELS = MappingProxyType({
    "modules_launcher": "Modules launcher",
})
HISTORY_VIEWS = MappingProxyType({"table": "Table", "cards": "Cards", "combined": "Combined"})
# Flight History column keys (bbc_efb.logbook_views.history.COLUMN_KEYS, kept Qt-free here; a test
# checks they match). "date" is always shown, so it can never be hidden.
HISTORY_COLUMNS = (
    "date", "callsign", "aircraft", "registration", "departure", "arrival", "operation", "off_block",
    "takeoff", "landing", "on_block", "block", "airborne", "flight_time", "landing_rate", "source", "status",
)
HISTORY_DEFAULT_HIDDEN = ("airborne", "landing", "off_block", "on_block", "registration", "takeoff")


def _hidden_columns(value):
    if not isinstance(value, (list, tuple)):
        raise PreferencesError("must be a list of Flight History column names")
    result = []
    for name in value:
        if not isinstance(name, str) or name not in HISTORY_COLUMNS or name == "date":
            raise PreferencesError(f"{name!r} is not a Flight History column that can be hidden")
        if name not in result:
            result.append(name)
    return sorted(result)
DENSITIES = MappingProxyType({"comfortable": "Comfortable", "compact": "Compact"})
SETTINGS_PAGES = ("simconnect", "flight_logging", "data_management", "layout")

SPECS: Mapping[str, PreferenceSpec] = MappingProxyType({spec.key: spec for spec in (
    PreferenceSpec("flight_logging.automatic", True, _bool, "Automatic Flight Logging",
                   "Start and finish Flight Logging automatically from simulator data. "
                   "Manual Start and Stop stay available."),
    PreferenceSpec("flight_logging.journal_retention_days", None, _optional_days,
                   "Keep Recorder journals",
                   "Empty keeps journals until you delete them. Flight records are never deleted automatically."),
    PreferenceSpec("logbook.history_view", "table", _choice(*HISTORY_VIEWS), "Flight History view"),
    PreferenceSpec("logbook.density", "comfortable", _choice(*DENSITIES), "Logbook density"),
    PreferenceSpec("logbook.hidden_columns", list(HISTORY_DEFAULT_HIDDEN), _hidden_columns,
                   "Hidden Flight History columns"),
    PreferenceSpec("logbook.dashboard_panels", {name: True for name in LOGBOOK_PANELS}, _panels,
                   "Dashboard panels", read=_panels_read),
    PreferenceSpec("inflight.density", "comfortable", _choice(*DENSITIES), "In-Flight density"),
    PreferenceSpec("inflight.panels", {name: True for name in INFLIGHT_PANELS}, _panels, "In-Flight panels",
                   read=_panels_read),
    PreferenceSpec("inflight.launcher_collapsed", False, _bool, "Start with the Modules launcher collapsed"),
    PreferenceSpec("window.geometry", None, _optional_geometry, "Window position and size"),
    PreferenceSpec("window.maximized", False, _bool, "Start maximised"),
    PreferenceSpec("settings.last_page", "simconnect", _choice(*SETTINGS_PAGES), "Last Settings page"),
    PreferenceSpec("simconnect.dll_path", None, _optional_text(1024), "SimConnect.dll chosen by the pilot"),
)})

LAYOUT_KEYS = (
    "logbook.history_view", "logbook.density", "logbook.hidden_columns", "logbook.dashboard_panels", "inflight.density",
    "inflight.panels", "inflight.launcher_collapsed", "window.geometry", "window.maximized",
)


def _spec(key):
    try:
        return SPECS[key]
    except KeyError:
        raise PreferencesError(f"{key!r} is not a known preference.") from None


def _lookup(tree, key):
    node = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(key)
        node = node[part]
    return node


def _store(tree, key, value):
    parts = key.split(".")
    node = tree
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}  # a non-object here was invalid anyway; it is replaced only on a change
            node[part] = child
        node = child
    node[parts[-1]] = value


def _remove(tree, key):
    parts = key.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _utc_stamp(now):
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Preferences:
    """A preferences file. ``Preferences.load(path)`` never raises for a bad file."""

    def __init__(self, path: Path | None, raw: dict | None = None, *, problem: str | None = None,
                 read_only_reason: str | None = None, clock: Callable[[], datetime] | None = None):
        self.path = Path(path) if path is not None else None
        self._raw = raw if isinstance(raw, dict) else {}
        self._lock = threading.RLock()
        self._listeners: list[Callable[[str, Any], None]] = []
        self._dirty = False
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.problem = problem
        self.read_only_reason = read_only_reason

    # Loading ------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str | None = None, *, clock=None) -> "Preferences":
        """Read ``path`` (default: ``default_preferences_path()``). Missing file means defaults."""
        path = Path(path) if path is not None else default_preferences_path()
        clock = clock or (lambda: datetime.now(timezone.utc))
        try:
            size = path.stat().st_size
            if size > MAX_PREFERENCES_BYTES:
                raise ValueError(f"the file is {size} bytes, larger than the 1 MB limit")
            payload = _loads(path.read_bytes())
        except FileNotFoundError:
            return cls(path, {}, clock=clock)
        except (ValueError, UnicodeDecodeError) as error:
            kept = path.with_name(f"{path.name}.damaged-{_utc_stamp(clock())}")
            try:
                os.replace(path, kept)
                where = f"It was kept as {kept.name}."
            except OSError as move_error:
                where = f"It could not be set aside ({move_error}), so it will not be overwritten this session."
                return cls(path, {}, clock=clock,
                           problem=f"Your preferences file could not be read ({error}). {where} Defaults are used.",
                           read_only_reason="The preferences file is damaged and could not be set aside.")
            log.warning("Preferences file %s was damaged (%s) and kept as %s", path, error, kept)
            return cls(path, {}, clock=clock,
                       problem=f"Your preferences file could not be read ({error}). {where} Defaults are used.")
        except OSError as error:
            log.warning("Preferences file %s could not be read: %s", path, error)
            return cls(path, {}, clock=clock,
                       problem=f"Your preferences file could not be read ({error}). Defaults are used.",
                       read_only_reason="The preferences file could not be read, so it is not overwritten.")
        if not isinstance(payload, dict):
            return cls(path, {}, clock=clock,
                       problem="Your preferences file does not hold a JSON object. Defaults are used.",
                       read_only_reason="The preferences file has an unexpected shape, so it is not overwritten.")
        version = payload.get("format", PREFERENCES_FORMAT)
        read_only = None
        problem = None
        if type(version) is not int or version < 1:
            problem = f"The preferences file has an unknown format marker {version!r}."
            read_only = "The preferences file has an unknown format marker, so it is not overwritten."
        elif version > PREFERENCES_FORMAT:
            read_only = (f"These preferences were saved by a newer BBC EFB (format {version}). "
                         "Changes apply to this session only and are not saved.")
        return cls(path, payload, problem=problem, read_only_reason=read_only, clock=clock)

    # Reading ------------------------------------------------------------

    def get(self, key: str) -> Any:
        """The stored value when it is valid, otherwise the default. Always a copy."""
        spec = _spec(key)
        with self._lock:
            try:
                value = spec.check_stored(copy.deepcopy(_lookup(self._raw, key)))
            except (KeyError, PreferencesError):
                value = spec.default
            return copy.deepcopy(value)

    def is_set(self, key: str) -> bool:
        """True when the file holds a valid value for ``key`` (not just the default)."""
        spec = _spec(key)
        with self._lock:
            try:
                spec.check_stored(copy.deepcopy(_lookup(self._raw, key)))
                return True
            except (KeyError, PreferencesError):
                return False

    def invalid_keys(self) -> tuple[str, ...]:
        """Known keys whose stored value is present but not valid (kept, not used)."""
        bad = []
        with self._lock:
            for key, spec in SPECS.items():
                try:
                    stored = _lookup(self._raw, key)
                except KeyError:
                    continue
                try:
                    spec.check(copy.deepcopy(stored))
                except PreferencesError:
                    bad.append(key)
        return tuple(bad)

    def raw(self) -> dict:
        """A deep copy of everything that would be written, unknown keys included."""
        with self._lock:
            return copy.deepcopy(self._raw)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def writable(self) -> bool:
        return self.path is not None and self.read_only_reason is None

    def panel_visible(self, workspace_key: str, panel: str) -> bool:
        """Visibility of one panel; panels not mentioned are visible."""
        panels = self.get(workspace_key)
        return panels.get(panel, True)

    # Changing -----------------------------------------------------------

    def set(self, key: str, value: Any) -> bool:
        """Validate and store ``value``. Returns True when something changed. Not saved until ``save()``."""
        spec = _spec(key)
        try:
            checked = spec.check(copy.deepcopy(value))
        except PreferencesError as error:
            raise PreferencesError(f"{spec.label}: {error}.") from None
        with self._lock:
            try:
                before = _lookup(self._raw, key)
                if before == checked and type(before) is type(checked):
                    return False
            except KeyError:
                pass
            _store(self._raw, key, copy.deepcopy(checked))
            self._dirty = True
        self._notify(key, copy.deepcopy(checked))
        return True

    def set_panel_visible(self, workspace_key: str, panel: str, shown: bool) -> bool:
        """Change one panel's visibility atomically (other panels, including unknown ones, are kept).

        Only the toggled entry is validated and changed: every other stored entry,
        including one this build cannot use, is written back exactly as it was read.
        Listeners run after the lock is released.
        """
        spec = _spec(workspace_key)
        if spec.read is not _panels_read:
            raise PreferencesError(f"{workspace_key!r} is not a panel map.")
        try:
            _panels({panel: shown})
        except PreferencesError as error:
            raise PreferencesError(f"{spec.label}: {error}.") from None
        with self._lock:
            try:
                stored = _lookup(self._raw, workspace_key)
            except KeyError:
                stored = None
            if isinstance(stored, Mapping):
                panels = copy.deepcopy(dict(stored))
            else:
                panels = copy.deepcopy(spec.default)
            if panels.get(panel) is shown and isinstance(stored, Mapping):
                return False
            panels[panel] = shown
            _store(self._raw, workspace_key, panels)
            self._dirty = True
        self._notify(workspace_key, self.get(workspace_key))
        return True

    def reset(self, keys=None) -> tuple[str, ...]:
        """Forget stored values for ``keys`` (default: every known key). Unknown keys stay."""
        keys = tuple(SPECS) if keys is None else tuple(keys)
        for key in keys:
            _spec(key)
        changed = []
        with self._lock:
            for key in keys:
                try:
                    _lookup(self._raw, key)
                except KeyError:
                    continue
                _remove(self._raw, key)
                changed.append(key)
            if changed:
                self._dirty = True
        for key in changed:
            self._notify(key, self.get(key))
        return tuple(changed)

    def adopt(self, data: bytes) -> tuple[str, ...]:
        """Replace everything with a preferences file's bytes (for example from a backup), then save.

        Refuses a file this build cannot read or one written by a newer format.
        Returns the known keys whose effective value changed.
        """
        try:
            payload = _loads(data)
        except (UnicodeDecodeError, ValueError) as error:
            raise PreferencesError(f"Those preferences cannot be read ({error}).") from None
        if not isinstance(payload, dict):
            raise PreferencesError("Those preferences do not hold a JSON object.")
        version = payload.get("format", PREFERENCES_FORMAT)
        if type(version) is not int or not 1 <= version <= PREFERENCES_FORMAT:
            raise PreferencesError(f"Those preferences use format {version!r}, which this build cannot use.")
        before = {key: self.get(key) for key in SPECS}
        with self._lock:
            self._raw = copy.deepcopy(payload)
            self._dirty = True
        changed = tuple(key for key in SPECS if self.get(key) != before[key])
        for key in changed:
            self._notify(key, self.get(key))
        if self.writable:
            self.save()
        return changed

    # Listeners ----------------------------------------------------------

    def subscribe(self, listener: Callable[[str, Any], None]) -> Callable[[], None]:
        """Call ``listener(key, value)`` after each change. Returns an unsubscribe function."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe():
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return unsubscribe

    def _notify(self, key, value):
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(key, value)
            except Exception:  # a broken listener must not stop the others or the change
                log.exception("Preferences listener failed for %s", key)

    # Saving -------------------------------------------------------------

    def save(self) -> bool:
        """Write the file atomically. Returns False when there was nothing to write.

        Raises ``PreferencesSaveError`` when the file is read-only for this build or
        cannot be written; the old file is left unchanged.
        """
        if self.path is None:
            raise PreferencesSaveError("These preferences are not linked to a file.")
        if self.read_only_reason:
            raise PreferencesSaveError(self.read_only_reason)
        with self._lock:
            if not self._dirty and self.path.exists():
                return False
            payload = copy.deepcopy(self._raw)
            payload["format"] = PREFERENCES_FORMAT
            payload["saved_at"] = self._clock().astimezone(timezone.utc).isoformat(timespec="milliseconds")
            try:
                text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
            except (TypeError, ValueError) as error:
                raise PreferencesSaveError(f"Preferences could not be written as JSON: {error}") from None
            try:
                _atomic_write(self.path, text.encode("utf-8"))
            except OSError as error:
                raise PreferencesSaveError(
                    f"Preferences could not be saved to {self.path}: {error.strerror or error}. "
                    "The previous file is unchanged.") from error
            self._raw = payload
            self._dirty = False
            return True


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp",
                                delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt, delay in enumerate((*_RETRY_DELAYS_S, None)):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                if delay is None or getattr(error, "winerror", None) not in _RETRY_WINERRORS:
                    raise
                time.sleep(delay)
        temporary = None
        if os.name != "nt":
            try:
                descriptor = os.open(path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(descriptor)
            except OSError:
                pass
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


__all__ = [
    "DENSITIES", "HISTORY_COLUMNS", "HISTORY_DEFAULT_HIDDEN", "HISTORY_VIEWS", "INFLIGHT_PANELS", "LAYOUT_KEYS", "LOGBOOK_PANELS",
    "PREFERENCES_FORMAT", "PreferenceSpec", "Preferences", "PreferencesError",
    "PreferencesSaveError", "SETTINGS_PAGES", "SPECS", "default_preferences_path",
]
