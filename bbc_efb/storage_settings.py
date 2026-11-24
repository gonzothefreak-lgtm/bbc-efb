"""Remember the pilot's data folder and describe it honestly.

Only the folder path is kept here, in a small JSON file in the user's
configuration folder. Flight records never go there: they stay in the data
folder the pilot chose. Settings (or the first-run prompt) calls
``open_data_folder`` with ``create=True`` only for a folder the pilot has just
picked; at later launches a missing folder is reported, not recreated.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import shutil
import sys
from datetime import datetime, timezone

from .preferences import _atomic_write
from .storage import FlightStore

log = logging.getLogger(__name__)

SETTINGS_FORMAT = 1


def default_settings_path():
    """``%APPDATA%\\BBC EFB\\settings.json`` on Windows, ``~/.config/bbc-efb/settings.json`` elsewhere."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "BBC EFB" / "settings.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "bbc-efb" / "settings.json"


@dataclass(frozen=True, slots=True)
class DataFolderSettings:
    """Load and save the chosen data folder path (nothing else)."""

    path: Path

    def load(self):
        """Return the remembered data folder, or None when none was chosen yet."""
        try:
            payload = json.loads(self.path.read_bytes().decode("utf-8-sig"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeDecodeError) as error:
            log.warning("Settings file %s could not be read: %s", self.path, error)
            return None
        folder = payload.get("data_folder") if isinstance(payload, dict) else None
        return Path(folder) if isinstance(folder, str) and folder else None

    def remember(self, folder):
        """Store ``folder`` atomically. Raises OSError when it could not be stored.

        The write retries briefly on Windows sharing and lock violations (antivirus,
        sync tools), like the preferences and flight-record writers. A settings file
        that is not valid JSON is set aside as ``settings.json.damaged-<UTC time>``
        rather than silently overwritten. A file that exists but cannot be read is
        not overwritten: the OSError is raised so the caller can tell the pilot.
        """
        folder = Path(folder)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(self.path.read_bytes().decode("utf-8-sig"))
            payload = existing if isinstance(existing, dict) else {}
            damaged = not isinstance(existing, dict)
        except FileNotFoundError:
            payload, damaged = {}, False
        except (ValueError, UnicodeDecodeError):
            payload, damaged = {}, True
        if damaged:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            kept = self.path.with_name(f"{self.path.name}.damaged-{stamp}")
            try:
                shutil.copy2(self.path, kept)
                log.warning("Settings file %s was damaged; a copy was kept as %s", self.path, kept.name)
            except OSError as error:
                log.warning("Settings file %s was damaged and could not be copied aside: %s", self.path, error)
        payload.update({"format": SETTINGS_FORMAT, "data_folder": str(folder)})
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _atomic_write(self.path, text.encode("utf-8"))


def data_folder_notes(folder):
    """Plain-English cautions about a candidate data folder (may be empty)."""
    folder = Path(folder)
    notes = []
    text = str(folder)
    parts = [part.casefold() for part in folder.parts]
    if any(part.startswith("onedrive") for part in parts) or (
        os.environ.get("OneDrive") and text.casefold().startswith(os.environ["OneDrive"].casefold())
    ):
        notes.append(
            "This folder is inside OneDrive. OneDrive can lock flight files while it syncs and can "
            "keep online-only copies, so a local folder outside OneDrive is more reliable."
        )
    windows = PureWindowsPath(text)
    if windows.drive.startswith("\\\\") or text.startswith("\\\\"):
        notes.append("This is a network folder. Saving fails while the network is unavailable.")
    if folder.anchor and (folder == Path(folder.anchor) or len(folder.parts) == 1):
        notes.append("This is the top of a drive. Choose or create a dedicated folder such as 'BBC EFB Data'.")
    return tuple(notes)


def open_data_folder(folder, *, create=False, read_only=False):
    """Open ``folder`` as the Logbook data folder (see FlightStore for the errors raised)."""
    return FlightStore(Path(folder), create=create, read_only=read_only)
