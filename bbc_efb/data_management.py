"""Data folder checks, Export All Data, and restorable Backup / Restore.

Qt-free and storage-API-free: this module works on the files in the pilot's data
folder, so it keeps working while ``storage.py`` evolves, and it never changes a
flight record.

Export All Data and Backup are deliberately different features (Q5-Q7, UI
Standards section 8):

* **Export All Data** (``export_all_data``) is a one-way copy for the pilot to
  read or take to another program: a ZIP with ``README.txt``, a spreadsheet-ready
  ``flights_summary.csv``, ``export_info.json`` and every data file unchanged
  under ``data/``. It is not a backup and BBC EFB never imports it.
* **Backup** (``create_backup``) is a restorable snapshot: a ZIP whose
  ``manifest.json`` lists every file with its size and SHA-256, the record schema
  versions found, the app version and the creation time. ``inspect_backup``
  checks a backup without changing anything; ``restore_backup`` checks it again,
  extracts into a staging folder next to the target, re-checks every file, and
  only then puts the data in place. It restores into a missing or empty folder.
  A folder that already holds files is replaced only when the caller passes
  ``replace_existing=True`` (the UI asks the pilot first), and even then the old
  contents are kept, renamed to ``<folder>.pre-restore-<UTC time>`` beside it.
  Nothing is ever overwritten.

Data folder layout assumed here (see ``STORAGE_SCHEMA.md``): ``flights/*.json``
are flight records; every other file in the folder is pilot data too (Recorder
journals, migration copies) and is included, except the writer lock, temporary
``*.tmp`` files, operating-system clutter (``.DS_Store``, ``Thumbs.db``,
``desktop.ini``), hidden staging folders and, unless asked, ``logs/``.

Consistency: files are read one at a time while the EFB may still be writing. Each
flight file is replaced atomically by storage, so every file in a backup is a
complete version of itself, and the manifest checksums are computed from exactly
the bytes put in the ZIP. A flight saved during the backup may or may not be
included.

PROPOSALS for the owner (defaults chosen here)
----------------------------------------------

* Logs are left out of exports and backups by default (``include_logs=False``):
  they are diagnostics, not flight data, and can contain file paths.
* The manifest stores only the data folder's *name*, not its full path, so a
  backup shared for support does not reveal the Windows user name.
* An export or backup must not be written inside the data folder itself (it would
  be swept into the next backup and grow without bound).
* Restore into the folder currently in use requires the EFB to close that folder
  first (the writer lock). The previous contents are renamed, never deleted;
  deleting the kept copy is left to the pilot.
* Changing the data folder does not move or copy flights. The old folder is left
  as it is and the pilot can restore a backup into the new one. (Migration and
  open-folder controls "remain proposed until separately agreed".)
* CSV cells that start with ``=``, ``+``, ``-``, ``@``, tab or carriage return get
  a leading apostrophe so a spreadsheet does not run them as formulas. The JSON
  files in the same export keep the exact values.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping
import uuid
import zipfile

log = logging.getLogger(__name__)

EXPORT_FORMAT = 1
MANIFEST_FORMAT = 1
BACKUP_KIND = "bbc-efb-backup"
EXPORT_KIND = "bbc-efb-export"
MANIFEST_NAME = "manifest.json"
DATA_PREFIX = "data/"
CONFIG_PREFIX = "config/"
LOCK_NAME = ".bbc-efb-writer.lock"
FLIGHTS_FOLDER = "flights"
LOGS_FOLDER = "logs"
OS_CLUTTER = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
CHUNK = 1024 * 1024
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.IGNORECASE)
_CONFIG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
CSV_FIELDS = (
    "flight_date", "aircraft", "departure", "arrival", "callsign", "registration", "minutes",
    "crew", "passengers", "baggage_kg", "freight_kg", "notes",
)
CSV_HEADER = (
    "id", "status", "source", "schema", *CSV_FIELDS, "revisions", "corrected_fields",
    "first_saved_at", "last_saved_at", "simulated", "file",
)

Clock = Callable[[], datetime]
Progress = Callable[[int, int], None]


# Errors -------------------------------------------------------------------

class DataManagementError(Exception):
    """A data operation could not be done. ``str(error)`` is a plain-English message."""


class DestinationExists(DataManagementError):
    """The chosen export or backup file already exists."""


class DestinationInsideDataFolder(DataManagementError):
    """An export or backup was aimed inside the data folder it copies."""


class BackupInvalid(DataManagementError):
    """The file is not a usable BBC EFB backup. ``problems`` lists every reason found."""

    def __init__(self, problems):
        self.problems = tuple(problems)
        first = self.problems[0] if self.problems else "unknown problem"
        more = f" (and {len(self.problems) - 1} more problems)" if len(self.problems) > 1 else ""
        super().__init__(f"This backup cannot be restored: {first}{more}")


class TargetNotEmpty(DataManagementError):
    """The restore target already holds files; the pilot must confirm replacing them."""

    def __init__(self, target, entries):
        self.target = Path(target)
        self.entries = tuple(entries)
        super().__init__(
            f"{self.target} already contains {len(self.entries)} item(s). Choose an empty folder, or confirm "
            "that the current contents should be set aside (they are renamed, not deleted).")


# Small helpers --------------------------------------------------------------

def _now(clock: Clock | None) -> datetime:
    moment = clock() if clock is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("The clock must return an aware datetime.")
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_export_name(clock: Clock | None = None) -> str:
    return f"BBC EFB Export {_now(clock).strftime('%Y-%m-%d %H%MZ')}.zip"


def default_backup_name(clock: Clock | None = None) -> str:
    return f"BBC EFB Backup {_now(clock).strftime('%Y-%m-%d %H%MZ')}.zip"


def _absolute(path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child = Path(os.path.realpath(child))
        parent = Path(os.path.realpath(parent))
    except OSError:
        child, parent = _absolute(child), _absolute(parent)
    if os.name == "nt":
        child, parent = Path(str(child).casefold()), Path(str(parent).casefold())
    return child == parent or parent in child.parents


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


human_size = _human_size


def _fsync_folder(folder: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _os_message(error: OSError) -> str:
    return error.strerror or str(error) or type(error).__name__


# Data folder checks ---------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FolderCheck:
    """Result of ``check_data_folder``. ``usable`` is False when ``problem`` is set."""

    path: Path
    usable: bool
    problem: str | None = None
    notes: tuple[str, ...] = ()
    created: bool = False
    existing_flights: int = 0
    other_items: int = 0


def probe_writable(folder) -> str | None:
    """Write, read back and delete a small file in ``folder``. Returns a problem or None."""
    folder = Path(folder)
    probe = folder / f".bbc-efb-write-test-{uuid.uuid4().hex}.tmp"
    payload = b"BBC EFB write test\n"
    try:
        with open(probe, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if probe.read_bytes() != payload:
            return "A test file written to this folder read back differently."
    except OSError as error:
        return f"BBC EFB cannot write to this folder ({_os_message(error)})."
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            log.warning("Write-test file %s could not be removed: %s", probe, error)
    return None


def _temporary_root() -> Path | None:
    """The system temporary-files folder (a separate function so tests can replace it)."""
    return Path(tempfile.gettempdir())


def folder_notes(folder) -> tuple[str, ...]:
    """Plain-English cautions about a candidate data folder (may be empty)."""
    folder = _absolute(folder)
    text = str(folder)
    notes = []
    parts = [part.casefold() for part in folder.parts]
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or os.environ.get("OneDriveCommercial")
    if any(part.startswith("onedrive") for part in parts) or (onedrive and text.casefold().startswith(onedrive.casefold())):
        notes.append("This folder is inside OneDrive. OneDrive can lock flight files while it syncs and can keep "
                     "online-only copies, so a local folder outside OneDrive is more reliable.")
    if text.startswith("\\\\") or PureWindowsPath(text).drive.startswith("\\\\"):
        notes.append("This is a network folder. Saving fails while the network is unavailable.")
    if folder.anchor and folder == Path(folder.anchor):
        notes.append("This is the top of a drive. Choose or create a dedicated folder such as 'BBC EFB Data'.")
    try:
        temp = _temporary_root()
        if temp is not None and _is_inside(folder, temp):
            notes.append("This folder is inside the temporary-files folder, which Windows or cleanup tools may empty.")
    except OSError:
        pass
    program = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent) if getattr(sys, "frozen", False) else None
    if program is not None and _is_inside(folder, program):
        notes.append("This folder is inside the BBC EFB program folder. Replacing the program with a new version "
                     "could delete your flights.")
    return tuple(notes)


def _first_missing(folder: Path) -> list[Path]:
    missing = []
    current = folder
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    return missing


def check_data_folder(folder, *, create: bool = False) -> FolderCheck:
    """Check that ``folder`` can hold the Logbook; with ``create=True`` make it (and its parents).

    Folders created here are removed again (when still empty) if the write test fails.
    """
    if folder is None or str(folder).strip() == "":
        return FolderCheck(Path(""), False, "No folder was chosen.")
    path = _absolute(folder)
    created_dirs: list[Path] = []
    if path.exists() and not path.is_dir():
        return FolderCheck(path, False, "This is a file, not a folder.")
    if not path.exists():
        if not create:
            return FolderCheck(path, False, "This folder does not exist (it may have been renamed, deleted, or be on "
                                            "a drive that is not connected).")
        created_dirs = _first_missing(path)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return FolderCheck(path, False, f"The folder could not be created ({_os_message(error)}).")
    problem = probe_writable(path)
    if problem is not None:
        for made in created_dirs:  # deepest first
            try:
                made.rmdir()
            except OSError:
                break
        return FolderCheck(path, False, problem, folder_notes(path), created=False)
    flights = 0
    other = 0
    try:
        for entry in os.scandir(path):
            if entry.name == FLIGHTS_FOLDER and entry.is_dir(follow_symlinks=False):
                flights = sum(1 for item in os.scandir(entry.path)
                              if item.name.lower().endswith(".json") and item.is_file(follow_symlinks=False))
            elif entry.name not in (LOCK_NAME, LOGS_FOLDER, "migration", "recorder") \
                    and entry.name.casefold() not in OS_CLUTTER:
                other += 1
    except OSError as error:
        return FolderCheck(path, False, f"The folder could not be listed ({_os_message(error)}).")
    notes = list(folder_notes(path))
    if flights:
        notes.append(f"This folder already holds {flights} BBC EFB flight file(s); they will be shown in the Logbook.")
    if other:
        notes.append(f"This folder already contains {other} other item(s). BBC EFB adds its own 'flights' folder "
                     "next to them and does not change them.")
    return FolderCheck(path, True, None, tuple(notes), created=bool(created_dirs), existing_flights=flights,
                       other_items=other)


@dataclass(frozen=True, slots=True)
class DataFolderSummary:
    path: Path
    exists: bool
    flight_files: int = 0
    other_files: int = 0
    total_bytes: int = 0
    problem: str | None = None

    def describe(self) -> str:
        if not self.exists:
            return self.problem or "The data folder is not available."
        text = (f"{self.flight_files} flight file(s), {self.other_files} other file(s), "
                f"{_human_size(self.total_bytes)} in total.")
        return f"{text} {self.problem}" if self.problem else text


def summarize_data_folder(folder, *, include_logs: bool = True) -> DataFolderSummary:
    """Count what is in the data folder (cheap; reads directory entries only)."""
    path = _absolute(folder)
    if not path.is_dir():
        return DataFolderSummary(path, False, problem="The data folder does not exist or is not connected.")
    try:
        files, _skipped = _collect(path, include_logs=include_logs)
    except OSError as error:
        return DataFolderSummary(path, True, problem=f"Some of it could not be listed ({_os_message(error)}).")
    flights = sum(1 for rel, _, _ in files if _is_flight(rel))
    return DataFolderSummary(path, True, flights, len(files) - flights, sum(size for _, _, size in files))


# Collecting files -----------------------------------------------------------

def _is_flight(rel: str) -> bool:
    parts = rel.split("/")
    return len(parts) == 2 and parts[0] == FLIGHTS_FOLDER and parts[1].lower().endswith(".json")


def _excluded_file(name: str) -> bool:
    lower = name.casefold()
    return name == LOCK_NAME or lower.endswith(".tmp") or lower in OS_CLUTTER


def _collect(root: Path, *, include_logs: bool, exclude: Iterable[Path] = ()) -> tuple[list, list]:
    """Return ([(posix relative path, absolute path, size)], [(relative path, reason)]) sorted by path."""
    excluded = [_absolute(item) for item in exclude]
    found = []
    skipped = []
    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=None):
        current_path = Path(current)
        rel_dir = current_path.relative_to(root).as_posix()
        keep = []
        for name in dirnames:
            child = current_path / name
            if name.startswith(".") or child.is_symlink():
                continue
            if rel_dir == "." and name == LOGS_FOLDER and not include_logs:
                continue
            keep.append(name)
        dirnames[:] = sorted(keep)
        for name in sorted(filenames):
            if _excluded_file(name):
                continue
            absolute = current_path / name
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if any(absolute == item for item in excluded):
                continue
            if absolute.is_symlink():
                skipped.append((rel, "it is a shortcut (symbolic link), which is not followed"))
                continue
            try:
                size = absolute.stat().st_size
            except OSError as error:
                skipped.append((rel, _os_message(error)))
                continue
            found.append((rel, absolute, size))
    found.sort(key=lambda item: item[0])
    return found, skipped


def _check_destination(destination, data_folder: Path, *, overwrite: bool) -> Path:
    destination = _absolute(destination)
    if destination.suffix.lower() != ".zip":
        destination = destination.with_name(destination.name + ".zip")
    if _is_inside(destination, data_folder):
        raise DestinationInsideDataFolder(
            "Choose a location outside the data folder. A copy saved inside it would be included in the next "
            "export or backup.")
    if destination.exists() and not overwrite:
        raise DestinationExists(f"{destination.name} already exists. Choose another name, or confirm replacing it.")
    if destination.exists() and destination.is_dir():
        raise DestinationExists(f"{destination} is a folder.")
    return destination


class _AtomicZip:
    """Write a ZIP to a hidden temporary file beside ``destination`` and move it into place on success."""

    def __init__(self, destination: Path):
        self.destination = destination
        self.temporary: Path | None = None
        self.handle = None
        self.zip: zipfile.ZipFile | None = None

    def __enter__(self) -> zipfile.ZipFile:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{self.destination.stem}-", suffix=".partial",
                                            dir=self.destination.parent)
        self.temporary = Path(name)
        self.handle = os.fdopen(descriptor, "w+b")
        self.zip = zipfile.ZipFile(self.handle, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
        return self.zip

    def __exit__(self, kind, error, trace):
        try:
            if self.zip is not None:
                self.zip.close()
            if kind is None:
                self.handle.flush()
                os.fsync(self.handle.fileno())
            self.handle.close()
            if kind is None:
                os.replace(self.temporary, self.destination)
                self.temporary = None
                _fsync_folder(self.destination.parent)
        finally:
            if self.handle is not None and not self.handle.closed:
                self.handle.close()
            if self.temporary is not None:
                try:
                    self.temporary.unlink()
                except OSError:
                    pass
        return False


def _zip_info(name: str, source: Path | None, moment: datetime, size: int = 0) -> zipfile.ZipInfo:
    when = moment
    if source is not None:
        try:
            when = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass
    date_time = when.timetuple()[:6]
    if date_time[0] < 1980:
        date_time = (1980, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(name, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    info.file_size = size
    return info


def _copy_into_zip(archive: zipfile.ZipFile, name: str, source: Path, moment: datetime, size_hint: int):
    """Stream ``source`` into ``archive`` as ``name``. Returns (size, sha256 hex, bytes if a flight record)."""
    digest = hashlib.sha256()
    size = 0
    keep = bytearray() if _is_flight(name[len(DATA_PREFIX):] if name.startswith(DATA_PREFIX) else name) else None
    info = _zip_info(name, source, moment, size_hint)
    with open(source, "rb") as handle, archive.open(info, "w", force_zip64=size_hint > 1 << 30) as target:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            target.write(chunk)
            size += len(chunk)
            if keep is not None and len(keep) <= MAX_RECORD_BYTES:
                keep.extend(chunk)
    return size, digest.hexdigest(), (bytes(keep) if keep is not None and len(keep) <= MAX_RECORD_BYTES else None)


# Reading flight records for the summary (duck-typed, never raises) ---------

@dataclass(frozen=True, slots=True)
class RecordSummary:
    file: str
    status: str                      # "ok", "unreadable: <reason>"
    record_id: str = ""
    source: str = ""
    schema: int | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    revisions: int = 0
    corrected: tuple[str, ...] = ()
    first_saved_at: str = ""
    last_saved_at: str = ""
    simulated: bool | None = None


def summarize_record(file_name: str, data: bytes | None) -> RecordSummary:
    """Read the current values of one flight record (schema 1 or 2) without trusting it."""
    if data is None:
        return RecordSummary(file_name, f"unreadable: larger than {MAX_RECORD_BYTES // (1024 * 1024)} MB")
    try:
        payload = json.loads(data.decode("utf-8-sig"))  # BOM allowed, as storage._decode
    except (UnicodeDecodeError, ValueError) as error:
        return RecordSummary(file_name, f"unreadable: not valid UTF-8 JSON ({error})")
    if not isinstance(payload, dict):
        return RecordSummary(file_name, "unreadable: not a JSON object")
    schema = payload.get("schema")
    schema = schema if type(schema) is int else None
    record_id = payload.get("id") if isinstance(payload.get("id"), str) else ""
    source = payload.get("source")
    source = source if isinstance(source, str) else ("manual" if schema == 1 else "")
    revisions = payload.get("revisions")
    if not isinstance(revisions, list) or not revisions or not all(isinstance(item, dict) for item in revisions):
        return RecordSummary(file_name, "unreadable: no revisions", record_id, source, schema)
    first = revisions[0].get("fields")
    last = revisions[-1].get("fields")
    if not isinstance(first, dict) or not isinstance(last, dict):
        return RecordSummary(file_name, "unreadable: a revision has no fields", record_id, source, schema)
    corrected = []
    for name in CSV_FIELDS:
        original = first.get(name)
        if any(isinstance(item.get("fields"), dict) and item["fields"].get(name) != original
               for item in revisions[1:]):
            corrected.append(name)
    evidence = payload.get("recorder")
    simulated = evidence.get("simulated") if isinstance(evidence, dict) else None
    status = "ok"
    if schema is not None and schema > 2:
        status = f"ok (record format {schema} is newer than this build; values shown as stored)"
    elif schema is None:
        status = "ok (no record format number)"
    return RecordSummary(
        file_name, status, record_id, source, schema, {name: last.get(name) for name in CSV_FIELDS},
        len(revisions), tuple(corrected),
        str(revisions[0].get("saved_at") or ""), str(revisions[-1].get("saved_at") or ""),
        simulated if isinstance(simulated, bool) else None,
    )


def _csv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _summary_csv(rows: list[RecordSummary]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow([
            _csv_cell(row.record_id), row.status, _csv_cell(row.source), _csv_cell(row.schema),
            *(_csv_cell(row.fields.get(name)) for name in CSV_FIELDS),
            row.revisions or "", " ".join(row.corrected), _csv_cell(row.first_saved_at),
            _csv_cell(row.last_saved_at), _csv_cell(row.simulated), row.file,
        ])
    return buffer.getvalue().encode("utf-8-sig")  # BOM so Excel reads accented text correctly


EXPORT_README = """BBC EFB - Export All Data
========================

Created: {created} (UTC)
App version: {app_version}
Flights: {flights} ({unreadable} could not be read and are listed with the reason)
Files: {files}

This is an EXPORT: a one-way copy of your Logbook for reading, spreadsheets or
other programs. It is NOT a backup and BBC EFB cannot restore from it. To keep a
restorable copy, use Settings > Data Management > Create Backup.

Contents
--------

flights_summary.csv
    One row per flight with its CURRENT values (after your corrections).
    UTF-8 with a byte-order mark so spreadsheet programs show accents correctly.
    An EMPTY cell means the value is unknown or was not recorded. It never
    means zero; a real zero is written as 0.
    status           "ok", or "unreadable: <reason>" when the file could not be read.
    source           manual (entered by you), recorder (Flight Logging) or imported.
    schema           record format number of the file.
    flight_date      YYYY-MM-DD.
    aircraft, departure, arrival, callsign, registration, notes   text.
    minutes          flight duration in minutes.
    crew, passengers whole numbers.
    baggage_kg, freight_kg   kilograms.
    revisions        how many times the flight was saved.
    corrected_fields fields changed after the first save. The original values
                     and every correction are kept in the JSON file.
    first_saved_at, last_saved_at   ISO 8601 times with a UTC offset.
    simulated        true when the data came from the simulated test provider,
                     false for native simulator data, empty for manual flights.
    file             the JSON file inside data/.
    Text starting with = + - @ gets a leading apostrophe so spreadsheets do not
    treat it as a formula. The JSON files keep the exact text.

data/
    Every file from your data folder, unchanged:
    data/flights/<id>.json   one complete record per flight, including the
                             original values, each correction with its time and
                             reason, and Flight Logging evidence. The format is
                             described in STORAGE_SCHEMA.md in the BBC EFB source.
    Other folders (for example Flight Logging journals or migration copies)
    are copied as they are.{logs_note}

export_info.json
    Machine-readable details of this export.

Privacy
-------

This file contains your flight history. BBC EFB never sends it anywhere; share it
only with people you choose.
"""


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    flights: int
    unreadable: tuple[tuple[str, str], ...]
    files: int
    total_bytes: int
    skipped: tuple[tuple[str, str], ...] = ()

    def describe(self) -> str:
        text = f"Exported {self.flights} flight(s) and {self.files} file(s) to {self.path.name}."
        if self.unreadable:
            text += f" {len(self.unreadable)} flight file(s) could not be read and are listed in the summary."
        if self.skipped:
            text += f" {len(self.skipped)} file(s) could not be copied: " + "; ".join(
                f"{rel} ({reason})" for rel, reason in self.skipped[:5])
        return text


def export_all_data(data_folder, destination, *, app_version: str, include_logs: bool = False,
                    overwrite: bool = False, clock: Clock | None = None,
                    progress: Progress | None = None) -> ExportResult:
    """Write Export All Data as a ZIP. The data folder is only read."""
    root = _absolute(data_folder)
    if not root.is_dir():
        raise DataManagementError(f"The data folder {root} is not available.")
    destination = _check_destination(destination, root, overwrite=overwrite)
    moment = _now(clock)
    files, skipped = _collect(root, include_logs=include_logs, exclude=(destination,))
    rows: list[RecordSummary] = []
    copied = 0
    total = 0
    try:
        with _AtomicZip(destination) as archive:
            for index, (rel, absolute, size) in enumerate(files):
                try:
                    written, _digest, kept = _copy_into_zip(archive, DATA_PREFIX + rel, absolute, moment, size)
                except FileNotFoundError:
                    skipped.append((rel, "it was removed while the export was running"))
                    continue
                except OSError as error:
                    skipped.append((rel, _os_message(error)))
                    continue
                copied += 1
                total += written
                if _is_flight(rel):
                    rows.append(summarize_record(DATA_PREFIX + rel, kept))
                if progress is not None:
                    progress(index + 1, len(files))
            unreadable = tuple((row.file, row.status) for row in rows if row.status.startswith("unreadable"))
            readable = len(rows) - len(unreadable)
            archive.writestr(_zip_info("flights_summary.csv", None, moment), _summary_csv(rows))
            info = {
                "kind": EXPORT_KIND, "export_format": EXPORT_FORMAT, "app_version": app_version,
                "created_at": _iso(moment), "flights": readable, "unreadable_flights": len(unreadable),
                "files": copied, "total_bytes": total, "logs_included": include_logs,
                "skipped": [{"path": rel, "reason": reason} for rel, reason in skipped],
                "note": "This is an export, not a backup. BBC EFB does not restore from it.",
            }
            archive.writestr(_zip_info("export_info.json", None, moment),
                             json.dumps(info, ensure_ascii=False, indent=2) + "\n")
            logs_note = ("\n    logs/ holds diagnostic logs." if include_logs
                         else "\n    Diagnostic logs are not included.")
            archive.writestr(_zip_info("README.txt", None, moment), EXPORT_README.format(
                created=_iso(moment), app_version=app_version, flights=readable, unreadable=len(unreadable),
                files=copied, logs_note=logs_note).replace("\n", "\r\n"))
    except OSError as error:
        raise DataManagementError(f"The export could not be written ({_os_message(error)}). "
                                  "Nothing was saved.") from error
    return ExportResult(destination, readable, unreadable, copied, total, tuple(skipped))


# Backup ---------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    files: int
    total_bytes: int
    flights: int
    schema_versions: Mapping[str, Any]
    skipped: tuple[tuple[str, str], ...] = ()

    def describe(self) -> str:
        text = (f"Backup saved as {self.path.name}: {self.flights} flight(s), {self.files} file(s), "
                f"{_human_size(self.total_bytes)}.")
        if self.skipped:
            text += (f" WARNING: {len(self.skipped)} file(s) could not be read and are NOT in this backup: "
                     + "; ".join(f"{rel} ({reason})" for rel, reason in self.skipped[:5]))
        return text


def _record_schema(data: bytes | None) -> str:
    if data is None:
        return "unreadable"
    try:
        payload = json.loads(data.decode("utf-8-sig"))  # BOM allowed, as storage._decode
    except (UnicodeDecodeError, ValueError):
        return "unreadable"
    schema = payload.get("schema") if isinstance(payload, dict) else None
    return str(schema) if type(schema) is int else "unknown"


def create_backup(data_folder, destination, *, app_version: str, record_schema: int | None = None,
                  config_files: Mapping[str, Path] | None = None, include_logs: bool = False,
                  overwrite: bool = False, clock: Clock | None = None,
                  progress: Progress | None = None) -> BackupResult:
    """Write a restorable backup ZIP with ``manifest.json`` (sizes and SHA-256 of every file).

    ``record_schema`` is the record format this build writes (``storage.SCHEMA_VERSION``).
    ``config_files`` maps simple names to files outside the data folder (for example
    ``{"preferences.json": path}``); they are stored under ``config/`` and verified on restore
    but never written into the data folder.
    """
    root = _absolute(data_folder)
    if not root.is_dir():
        raise DataManagementError(f"The data folder {root} is not available.")
    destination = _check_destination(destination, root, overwrite=overwrite)
    moment = _now(clock)
    config_files = dict(config_files or {})
    for name in config_files:
        if not _CONFIG_NAME.match(name):
            raise ValueError(f"Configuration name {name!r} must be a simple file name.")
    files, skipped = _collect(root, include_logs=include_logs, exclude=(destination,))
    entries = []
    schemas: dict[str, int] = {}
    config_formats: dict[str, Any] = {}
    flights = 0
    total = 0
    work = [(DATA_PREFIX + rel, absolute, size, rel) for rel, absolute, size in files]
    for name, source in sorted(config_files.items()):
        source = Path(source)
        try:
            work.append((CONFIG_PREFIX + name, source, source.stat().st_size, None))
        except OSError as error:
            skipped.append((CONFIG_PREFIX + name, _os_message(error)))
    try:
        with _AtomicZip(destination) as archive:
            for index, (name, source, size, rel) in enumerate(work):
                try:
                    written, digest, kept = _copy_into_zip(archive, name, source, moment, size)
                except FileNotFoundError:
                    skipped.append((name, "it was removed while the backup was running"))
                    continue
                except OSError as error:
                    skipped.append((name, _os_message(error)))
                    continue
                entries.append({"path": name, "size": written, "sha256": digest})
                total += written
                if rel is not None and _is_flight(rel):
                    flights += 1
                    version = _record_schema(kept)
                    schemas[version] = schemas.get(version, 0) + 1
                elif rel is None:
                    try:
                        payload = json.loads(Path(source).read_bytes().decode("utf-8-sig"))
                        config_formats[name[len(CONFIG_PREFIX):]] = payload.get("format") \
                            if isinstance(payload, dict) else None
                    except (OSError, ValueError, UnicodeDecodeError):
                        config_formats[name[len(CONFIG_PREFIX):]] = None
                if progress is not None:
                    progress(index + 1, len(work))
            schema_versions = {
                "manifest": MANIFEST_FORMAT,
                "flight_record_written_by_app": record_schema,
                "flight_records_found": dict(sorted(schemas.items())),
                "config_formats": config_formats,
            }
            manifest = {
                "kind": BACKUP_KIND,
                "manifest_format": MANIFEST_FORMAT,
                "app_version": app_version,
                "created_at": _iso(moment),
                "source_folder_name": root.name,
                "logs_included": include_logs,
                "schema_versions": schema_versions,
                "flight_count": flights,
                "file_count": len(entries),
                "total_bytes": total,
                "files": entries,
                "skipped": [{"path": rel, "reason": reason} for rel, reason in skipped],
            }
            archive.writestr(_zip_info(MANIFEST_NAME, None, moment),
                             json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    except OSError as error:
        raise DataManagementError(f"The backup could not be written ({_os_message(error)}). "
                                  "Nothing was saved.") from error
    return BackupResult(destination, len(entries), total, flights, schema_versions, tuple(skipped))


# Inspecting and restoring ---------------------------------------------------

def unsafe_member_reason(name: str) -> str | None:
    """Why ``name`` must not be extracted (absolute, ``..``, reserved on Windows ...), or None."""
    if not isinstance(name, str) or not name:
        return "an empty file name"
    if "\\" in name or ":" in name or "\x00" in name or any(ord(ch) < 32 for ch in name):
        return f"{name!r} has characters that are not allowed in a file name"
    if name.startswith("/") or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive:
        return f"{name!r} is an absolute path"
    parts = name.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return f"{name!r} leaves its folder or has an empty part"
        if part != part.rstrip(" ."):
            return f"{name!r} has a part ending in a space or dot"
        if _WINDOWS_RESERVED.match(part):
            return f"{name!r} uses a name reserved by Windows"
        if any(ch in part for ch in '<>"|?*'):
            return f"{name!r} has characters that are not allowed in a file name"
    if len(name) > 400:
        return f"{name[:40]!r}... is too long"
    return None


@dataclass(frozen=True, slots=True)
class BackupInspection:
    path: Path
    ok: bool
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    manifest: Mapping[str, Any] | None
    checksums_verified: bool

    @property
    def created_at(self) -> str:
        return str((self.manifest or {}).get("created_at", ""))

    @property
    def app_version(self) -> str:
        return str((self.manifest or {}).get("app_version", ""))

    @property
    def flight_count(self) -> int | None:
        value = (self.manifest or {}).get("flight_count")
        return value if type(value) is int else None

    @property
    def total_bytes(self) -> int:
        return sum(item["size"] for item in self.files)

    @property
    def files(self) -> tuple[Mapping[str, Any], ...]:
        files = (self.manifest or {}).get("files")
        return tuple(files) if isinstance(files, list) and self.ok else ()

    def describe(self) -> str:
        if not self.ok:
            return "This backup cannot be restored:\n- " + "\n- ".join(self.problems)
        flights = self.flight_count
        text = (f"Backup created {self.created_at or 'at an unknown time'} by BBC EFB "
                f"{self.app_version or '(version not recorded)'}: "
                f"{flights if flights is not None else 'an unknown number of'} flight(s), "
                f"{len(self.files)} file(s), {_human_size(self.total_bytes)}.")
        if self.checksums_verified:
            text += " Every file matched its checksum."
        if self.warnings:
            text += "\nNotes:\n- " + "\n- ".join(self.warnings)
        return text


def _validate_manifest(manifest) -> tuple[list[str], list[str], dict[str, Mapping[str, Any]]]:
    problems: list[str] = []
    warnings: list[str] = []
    listed: dict[str, Mapping[str, Any]] = {}
    if not isinstance(manifest, dict):
        return ["manifest.json is not a JSON object."], warnings, listed
    if manifest.get("kind") != BACKUP_KIND:
        problems.append("manifest.json does not describe a BBC EFB backup"
                        + (" (this looks like an Export All Data file, which cannot be restored)"
                           if manifest.get("kind") == EXPORT_KIND else "") + ".")
    version = manifest.get("manifest_format")
    if type(version) is not int or version < 1:
        problems.append("manifest.json has no valid format number.")
    elif version > MANIFEST_FORMAT:
        problems.append(f"The backup was made by a newer BBC EFB (manifest format {version}). "
                        "Update BBC EFB to restore it.")
    files = manifest.get("files")
    if not isinstance(files, list):
        problems.append("manifest.json has no file list.")
        return problems, warnings, listed
    seen_folded: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            problems.append("manifest.json has a file entry that is not an object.")
            continue
        name, size, digest = item.get("path"), item.get("size"), item.get("sha256")
        reason = unsafe_member_reason(name) if isinstance(name, str) else "a file entry without a path"
        if reason is None and not (name.startswith(DATA_PREFIX) or name.startswith(CONFIG_PREFIX)):
            reason = f"{name!r} is outside the data/ and config/ folders"
        if reason is None and name.startswith(CONFIG_PREFIX) and not _CONFIG_NAME.match(name[len(CONFIG_PREFIX):]):
            reason = f"{name!r} is not a simple configuration file name"
        if reason is not None:
            problems.append(f"Unsafe file entry: {reason}.")
            continue
        if type(size) is not int or size < 0:
            problems.append(f"{name}: the manifest has no valid size.")
            continue
        if not isinstance(digest, str) or not _SHA256.match(digest):
            problems.append(f"{name}: the manifest has no valid SHA-256 checksum.")
            continue
        folded = name.casefold()
        if folded in seen_folded:
            problems.append(f"{name} is listed twice (letter case ignored, as on Windows).")
            continue
        seen_folded.add(folded)
        listed[name] = item
    # A file and a folder of the same name cannot both exist.
    folders = {"/".join(name.casefold().split("/")[:i]) for name in listed for i in range(1, name.count("/") + 1)}
    for name in listed:
        if name.casefold() in folders:
            problems.append(f"{name} is listed both as a file and as a folder.")
    found = (manifest.get("schema_versions") or {}).get("flight_records_found") \
        if isinstance(manifest.get("schema_versions"), dict) else None
    if isinstance(found, dict):
        newer = [key for key in found if key.isdigit() and int(key) > 2]
        if newer:
            warnings.append("Some flights use a newer record format (" + ", ".join(sorted(newer)) +
                            "). This version shows them read-only.")
        if found.get("unreadable"):
            warnings.append(f"{found['unreadable']} flight file(s) could not be read when the backup was made; "
                            "they are restored exactly as they were.")
    skipped = manifest.get("skipped")
    if isinstance(skipped, list) and skipped:
        warnings.append(f"{len(skipped)} file(s) could not be read when the backup was made and are not in it.")
    return problems, warnings, listed


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, expected_size: int, sink=None) -> tuple[int, str]:
    """Stream one member, refusing more bytes than the manifest promised. Returns (size, sha256)."""
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as source:
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                raise BackupInvalid([f"{info.filename} is larger than the manifest says."])
            digest.update(chunk)
            if sink is not None:
                sink.write(chunk)
    return size, digest.hexdigest()


def inspect_backup(path, *, verify_checksums: bool = True) -> BackupInspection:
    """Check a backup ZIP without changing anything. Never raises for a bad file."""
    path = _absolute(path)
    problems: list[str] = []
    warnings: list[str] = []
    manifest = None
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            by_name: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                if info.filename in by_name:
                    problems.append(f"{info.filename} appears twice in the ZIP.")
                by_name[info.filename] = info
            manifest_info = by_name.get(MANIFEST_NAME)
            if manifest_info is None:
                problems.append("There is no manifest.json, so this is not a BBC EFB backup "
                                "(an Export All Data file cannot be restored).")
                return BackupInspection(path, False, tuple(problems), (), None, False)
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                problems.append("manifest.json is unreasonably large.")
                return BackupInspection(path, False, tuple(problems), (), None, False)
            try:
                manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, zipfile.BadZipFile, OSError, RuntimeError) as error:
                problems.append(f"manifest.json cannot be read ({error}).")
                return BackupInspection(path, False, tuple(problems), (), None, False)
            found, notes, listed = _validate_manifest(manifest)
            problems.extend(found)
            warnings.extend(notes)
            for name, info in by_name.items():
                if name == MANIFEST_NAME or name.endswith("/"):
                    continue
                if name not in listed:
                    problems.append(f"{name} is in the ZIP but not in the manifest.")
            for name, item in listed.items():
                info = by_name.get(name)
                if info is None:
                    problems.append(f"{name} is listed in the manifest but missing from the ZIP.")
                    continue
                if info.flag_bits & 0x1:
                    problems.append(f"{name} is encrypted.")
                    continue
                if info.file_size != item["size"]:
                    problems.append(f"{name} has a different size than the manifest says.")
                    continue
                if verify_checksums:
                    try:
                        size, digest = _read_member(archive, info, item["size"])
                    except BackupInvalid as error:
                        problems.extend(error.problems)
                        continue
                    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, ValueError) as error:
                        problems.append(f"{name} cannot be read from the ZIP ({error}).")
                        continue
                    if size != item["size"] or digest != item["sha256"]:
                        problems.append(f"{name} does not match its checksum: the backup is damaged or was changed.")
    except FileNotFoundError:
        problems.append("The backup file does not exist.")
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        problems.append(f"This is not a readable ZIP file ({error}).")
    except OSError as error:
        problems.append(f"The backup file cannot be read ({_os_message(error)}).")
    return BackupInspection(path, not problems, tuple(problems), tuple(warnings),
                            manifest if isinstance(manifest, dict) else None,
                            verify_checksums and not problems)


def folder_entries(folder) -> tuple[str, ...]:
    """Names in ``folder`` that count as content (operating-system clutter is ignored)."""
    try:
        return tuple(sorted(entry.name for entry in os.scandir(folder) if entry.name.casefold() not in OS_CLUTTER))
    except FileNotFoundError:
        return ()


@dataclass(frozen=True, slots=True)
class RestoreResult:
    target: Path
    files: int
    flights: int
    previous_data: Path | None
    config: Mapping[str, bytes]
    warnings: tuple[str, ...] = ()

    def describe(self) -> str:
        text = f"Restored {self.flights} flight(s) and {self.files} file(s) into {self.target}."
        if self.previous_data is not None:
            text += f" The previous contents were kept in {self.previous_data}."
        if self.warnings:
            text += " " + " ".join(self.warnings)
        return text


def _remove_tree_quietly(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        log.warning("Restore staging folder %s could not be removed: %s", path, error)


def restore_backup(backup, target, *, replace_existing: bool = False, clock: Clock | None = None,
                   progress: Progress | None = None) -> RestoreResult:
    """Restore a verified backup into ``target`` (a missing or empty folder).

    With ``replace_existing=True`` a non-empty ``target`` is renamed to
    ``<target>.pre-restore-<UTC time>`` first; nothing is deleted or overwritten.
    The EFB must have closed the data folder (released its writer lock) first.
    Raises ``BackupInvalid``, ``TargetNotEmpty`` or ``DataManagementError``.
    """
    backup = _absolute(backup)
    target = _absolute(target)
    inspection = inspect_backup(backup, verify_checksums=True)
    if not inspection.ok:
        raise BackupInvalid(inspection.problems)
    if target.exists() and not target.is_dir():
        raise DataManagementError(f"{target} is a file, not a folder.")
    if _is_inside(backup, target) and replace_existing:
        raise DataManagementError("The backup file is inside the folder it would replace. Move the backup "
                                  "somewhere else first.")
    existing = folder_entries(target) if target.exists() else ()
    if existing and not replace_existing:
        raise TargetNotEmpty(target, existing)
    if target.anchor and target == Path(target.anchor):
        raise DataManagementError("A backup cannot be restored into the top of a drive. Choose a folder.")
    moment = _now(clock)
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DataManagementError(f"The folder {parent} could not be created ({_os_message(error)}).") from error
    try:
        free = shutil.disk_usage(parent).free
    except OSError:
        free = None
    needed = inspection.total_bytes
    if free is not None and free < needed + 16 * 1024 * 1024:
        raise DataManagementError(f"There is not enough free space: the backup needs {_human_size(needed)} "
                                  f"and {_human_size(free)} is free.")
    staging = parent / f".{target.name}.restore-{_stamp(moment)}-{uuid.uuid4().hex[:8]}"
    config: dict[str, bytes] = {}
    restored = 0
    flights = 0
    try:
        staging.mkdir()
    except OSError as error:
        raise DataManagementError(f"A staging folder could not be created next to {target} "
                                  f"({_os_message(error)}).") from error
    try:
        with zipfile.ZipFile(backup, "r") as archive:
            items = inspection.files
            for index, item in enumerate(items):
                name = item["path"]
                info = archive.getinfo(name)
                if name.startswith(CONFIG_PREFIX):
                    buffer = io.BytesIO()
                    size, digest = _read_member(archive, info, item["size"], buffer)
                    if digest != item["sha256"] or size != item["size"]:
                        raise BackupInvalid([f"{name} changed while it was being restored."])
                    config[name[len(CONFIG_PREFIX):]] = buffer.getvalue()
                    continue
                rel = name[len(DATA_PREFIX):]
                destination = staging.joinpath(*rel.split("/"))
                if not _is_inside(destination, staging):
                    raise BackupInvalid([f"{name} would be written outside the restore folder."])
                destination.parent.mkdir(parents=True, exist_ok=True)
                with open(destination, "xb") as handle:
                    size, digest = _read_member(archive, info, item["size"], handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                if digest != item["sha256"] or size != item["size"]:
                    raise BackupInvalid([f"{name} changed while it was being restored."])
                restored += 1
                if _is_flight(rel):
                    flights += 1
                if progress is not None:
                    progress(index + 1, len(items))
        _fsync_folder(staging)
        previous = None
        if existing:
            previous = parent / f"{target.name}.pre-restore-{_stamp(moment)}"
            suffix = 1
            while previous.exists():
                suffix += 1
                previous = parent / f"{target.name}.pre-restore-{_stamp(moment)}-{suffix}"
            try:
                os.rename(target, previous)
            except OSError as error:
                raise DataManagementError(
                    f"The current contents of {target} could not be set aside ({_os_message(error)}). Close the "
                    "Logbook data folder (or any program using it) and try again. Nothing was changed.") from error
            try:
                os.rename(staging, target)
            except OSError as error:
                try:
                    os.rename(previous, target)
                except OSError as rollback:
                    raise DataManagementError(
                        f"The restore failed ({_os_message(error)}) and the previous data could not be put back "
                        f"({_os_message(rollback)}). Your previous data is safe in {previous}; the restored copy "
                        f"is in {staging}.") from error
                raise DataManagementError(f"The restore could not be completed ({_os_message(error)}). "
                                          "Nothing was changed.") from error
        elif not target.exists():
            try:
                os.rename(staging, target)
            except OSError as error:
                raise DataManagementError(f"The restore could not be completed ({_os_message(error)}). "
                                          "Nothing was changed.") from error
        else:
            # Empty folder (perhaps with OS clutter we must not delete): move entries in one by one.
            moved = []
            try:
                for entry in sorted(os.listdir(staging)):
                    if os.path.lexists(target / entry):
                        raise DataManagementError(f"{target / entry} appeared during the restore. Nothing was "
                                                  "overwritten.")
                    os.rename(staging / entry, target / entry)
                    moved.append(entry)
            except (OSError, DataManagementError) as error:
                for entry in reversed(moved):
                    try:
                        os.rename(target / entry, staging / entry)
                    except OSError:
                        log.error("Restore rollback could not move %s back", target / entry)
                if isinstance(error, DataManagementError):
                    raise
                raise DataManagementError(f"The restore could not be completed ({_os_message(error)}). "
                                          "Nothing was changed.") from error
            staging.rmdir()
        _fsync_folder(parent)
        return RestoreResult(target, restored, flights, previous, config, inspection.warnings)
    except BackupInvalid:
        _remove_tree_quietly(staging)
        raise
    except DataManagementError as error:
        if staging.exists() and "restored copy is in" not in str(error):
            _remove_tree_quietly(staging)
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as error:
        _remove_tree_quietly(staging)
        message = _os_message(error) if isinstance(error, OSError) else str(error)
        raise DataManagementError(f"The restore could not be completed ({message}). Nothing was changed.") from error


__all__ = [
    "BACKUP_KIND", "BackupInspection", "BackupInvalid", "BackupResult", "CSV_HEADER", "DataFolderSummary",
    "DataManagementError", "DestinationExists", "DestinationInsideDataFolder", "EXPORT_KIND", "ExportResult",
    "FolderCheck", "MANIFEST_FORMAT", "RecordSummary", "RestoreResult", "TargetNotEmpty", "check_data_folder",
    "create_backup", "default_backup_name", "default_export_name", "export_all_data", "folder_entries",
    "folder_notes", "human_size", "inspect_backup", "probe_writable", "restore_backup", "summarize_data_folder",
    "summarize_record", "unsafe_member_reason",
]
