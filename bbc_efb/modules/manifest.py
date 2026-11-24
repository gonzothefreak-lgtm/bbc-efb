"""Read and check a drop-in module's ``module.json``.

EXPERIMENTAL (second-release feature). Nothing here imports module code or
touches the simulator: this file only parses text and compares versions, so it
is safe to run on any folder the pilot drops into the modules location.

Every problem is reported as a :class:`Problem` with a stable ``code`` and a
plain-English ``message`` that the Settings page can show as-is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import keyword
from pathlib import Path
import re

MANIFEST_NAME = "module.json"
MANIFEST_FORMAT = 1
MANIFEST_MAX_BYTES = 64 * 1024

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*$")
_ID_MAX = 64
_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+]([0-9A-Za-z.-]+))?$")
_TEXT_LIMITS = {"name": 80, "author": 120, "description": 2000, "homepage": 300}
_LIST_MAX = 64
_KNOWN_KEYS = frozenset({
    "manifest_format", "id", "name", "version", "api_version", "entry", "control_groups",
    "description", "author", "homepage", "requires",
})
_REQUIRES_KEYS = frozenset({"capabilities", "modules", "python_packages"})


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ProblemCode(StrEnum):
    """Why a module cannot be offered, loaded or fully trusted."""

    BAD_MANIFEST = "bad_manifest"
    API_VERSION_MISMATCH = "api_version_mismatch"
    MISSING_PREREQUISITE = "missing_prerequisite"
    UNKNOWN_CONTROL_GROUP = "unknown_control_group"
    DUPLICATE_ID = "duplicate_id"
    NAME_CONFLICT = "name_conflict"
    IMPORT_ERROR = "import_error"
    FACTORY_ERROR = "factory_error"
    CORE_REFUSED = "core_refused"
    RUNTIME_ERROR = "runtime_error"
    CLOSE_ERROR = "close_error"
    NOT_GRANTED = "not_granted"
    DRAFT_API = "draft_api"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class Problem:
    code: ProblemCode
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True, order=True)
class Version:
    """A plain ``MAJOR.MINOR.PATCH`` number. A label such as ``-draft`` is kept for display only."""

    major: int
    minor: int = 0
    patch: int = 0
    label: str = field(default="", compare=False)

    @classmethod
    def parse(cls, text: str) -> Version:
        if not isinstance(text, str):
            raise ValueError("a version must be text such as \"1.0\"")
        match = _VERSION_RE.match(text.strip())
        if match is None:
            raise ValueError(f"\"{text}\" is not a version such as 1.0 or 1.2.3")
        major, minor, patch, label = match.groups()
        return cls(int(major), int(minor or 0), int(patch or 0), label or "")

    @property
    def numbers(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        return f"{text}-{self.label}" if self.label else text


_OPERATORS = (">=", "<=", "==", "!=", ">", "<")


@dataclass(frozen=True, slots=True)
class VersionRange:
    """A comma-separated set of clauses, all of which must hold.

    ``">=0.1,<0.2"`` is explicit. A bare ``"0.1"`` is the compatible range: for
    ``0.x`` it means the same minor (0.1.0 up to, not including, 0.2); from 1.0
    it means the same major at that minor or later. Labels such as ``-draft``
    are ignored when comparing; the loader reports a draft API separately.
    """

    text: str
    clauses: tuple[tuple[str, Version], ...]

    @classmethod
    def parse(cls, text: str) -> VersionRange:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("api_version must be text such as \">=0.1,<0.2\"")
        clauses: list[tuple[str, Version]] = []
        for raw in text.split(","):
            part = raw.strip()
            if not part:
                raise ValueError(f"\"{text}\" has an empty clause")
            operator = next((op for op in _OPERATORS if part.startswith(op)), "")
            version = Version.parse(part[len(operator):].strip())
            if operator:
                clauses.append((operator, version))
                continue
            clauses.append((">=", version))
            if version.major == 0:
                clauses.append(("<", Version(0, version.minor + 1)))
            else:
                clauses.append(("<", Version(version.major + 1)))
        return cls(text.strip(), tuple(clauses))

    def contains(self, version: Version) -> bool:
        value = version.numbers
        for operator, bound in self.clauses:
            other = bound.numbers
            ok = {
                ">=": value >= other, "<=": value <= other, ">": value > other,
                "<": value < other, "==": value == other, "!=": value != other,
            }[operator]
            if not ok:
                return False
        return True

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class EntryPoint:
    """``package.module:factory``. The first dotted part must live in the module folder."""

    module: str
    attribute: str

    @property
    def top_level(self) -> str:
        return self.module.split(".", 1)[0]

    @classmethod
    def parse(cls, text: str) -> EntryPoint:
        if not isinstance(text, str) or text.count(":") != 1:
            raise ValueError("entry must look like \"package.module:factory\"")
        module, attribute = (part.strip() for part in text.split(":"))
        for name in (*module.split("."), *attribute.split(".")):
            if not name.isidentifier() or keyword.iskeyword(name):
                raise ValueError(f"entry \"{text}\" contains \"{name}\", which is not a Python name")
        return cls(module, attribute)

    def __str__(self) -> str:
        return f"{self.module}:{self.attribute}"


@dataclass(frozen=True, slots=True)
class Requirements:
    capabilities: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    python_packages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    id: str
    name: str
    version: Version
    api_version: VersionRange
    entry: EntryPoint
    control_groups: tuple[str, ...] = ()
    description: str = ""
    author: str = ""
    homepage: str = ""
    requires: Requirements = Requirements()

    @classmethod
    def build(cls, *, id: str, name: str, version: str, api_version: str, entry: str,
              control_groups=(), description="", author="", homepage="", requires=None) -> ModuleManifest:
        """Convenience for built-in modules declared in code. Raises ValueError on bad input."""
        payload = {
            "manifest_format": MANIFEST_FORMAT, "id": id, "name": name, "version": version,
            "api_version": api_version, "entry": entry, "control_groups": list(control_groups),
            "description": description, "author": author, "homepage": homepage,
            "requires": dict(requires or {}),
        }
        result = manifest_from_dict(payload)
        if result.manifest is None:
            raise ValueError("; ".join(problem.message for problem in result.problems))
        return result.manifest


@dataclass(frozen=True, slots=True)
class ManifestResult:
    """Either a manifest, or the reasons there is none. ``warnings`` never block loading."""

    manifest: ModuleManifest | None
    problems: tuple[Problem, ...] = ()
    warnings: tuple[Problem, ...] = ()

    @property
    def ok(self) -> bool:
        return self.manifest is not None


def _bad(message: str) -> Problem:
    return Problem(ProblemCode.BAD_MANIFEST, message)


def _text(payload, key, problems, *, required):
    value = payload.get(key)
    if value is None:
        if required:
            problems.append(_bad(f"\"{key}\" is missing."))
        return ""
    if not isinstance(value, str):
        problems.append(_bad(f"\"{key}\" must be text."))
        return ""
    value = value.strip()
    if required and not value:
        problems.append(_bad(f"\"{key}\" is empty."))
    limit = _TEXT_LIMITS.get(key)
    if limit is not None and len(value) > limit:
        problems.append(_bad(f"\"{key}\" is longer than {limit} characters."))
    return value


def _names(value, label, problems, pattern=None):
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(_bad(f"\"{label}\" must be a list of text values."))
        return ()
    if len(value) > _LIST_MAX:
        problems.append(_bad(f"\"{label}\" lists more than {_LIST_MAX} entries."))
        return ()
    cleaned = []
    for item in value:
        item = item.strip()
        if not item or (pattern is not None and not pattern(item)):
            problems.append(_bad(f"\"{label}\" contains an invalid entry \"{item}\"."))
            continue
        if item not in cleaned:
            cleaned.append(item)
    return tuple(cleaned)


def _capability_id(text: str) -> bool:
    return len(text) <= 128 and re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+", text) is not None


def _python_name(text: str) -> bool:
    return all(part.isidentifier() and not keyword.iskeyword(part) for part in text.split("."))


def _group_name(text: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text) is not None


def valid_module_id(text: str) -> bool:
    return isinstance(text, str) and len(text) <= _ID_MAX and _ID_RE.match(text) is not None


def manifest_from_dict(payload) -> ManifestResult:
    """Check a decoded manifest. Unknown keys are warnings so newer manifests stay readable."""

    if not isinstance(payload, dict):
        return ManifestResult(None, (_bad("module.json must contain a JSON object."),))
    problems: list[Problem] = []
    warnings: list[Problem] = []

    fmt = payload.get("manifest_format", MANIFEST_FORMAT)
    if type(fmt) is not int:
        problems.append(_bad("\"manifest_format\" must be a whole number."))
    elif fmt > MANIFEST_FORMAT:
        problems.append(_bad(
            f"This module uses manifest format {fmt}; this EFB reads format {MANIFEST_FORMAT}. "
            "Update the EFB or ask the author for a compatible release."))
    elif fmt < 1:
        problems.append(_bad("\"manifest_format\" must be 1 or later."))

    module_id = _text(payload, "id", problems, required=True)
    if module_id and not valid_module_id(module_id):
        problems.append(_bad(
            f"\"id\" \"{module_id}\" must start with a lowercase letter and use only lowercase "
            f"letters, digits, '_', '.' or '-' (at most {_ID_MAX} characters)."))
    name = _text(payload, "name", problems, required=True)
    description = _text(payload, "description", problems, required=False)
    author = _text(payload, "author", problems, required=False)
    homepage = _text(payload, "homepage", problems, required=False)

    version = None
    raw_version = _text(payload, "version", problems, required=True)
    if raw_version:
        try:
            version = Version.parse(raw_version)
        except ValueError as error:
            problems.append(_bad(f"\"version\": {error}."))

    api_range = None
    raw_range = _text(payload, "api_version", problems, required=True)
    if raw_range:
        try:
            api_range = VersionRange.parse(raw_range)
        except ValueError as error:
            problems.append(_bad(f"\"api_version\": {error}."))

    entry = None
    raw_entry = _text(payload, "entry", problems, required=True)
    if raw_entry:
        try:
            entry = EntryPoint.parse(raw_entry)
        except ValueError as error:
            problems.append(_bad(f"\"entry\": {error}."))

    groups = _names(payload.get("control_groups"), "control_groups", problems, _group_name)

    requires = Requirements()
    raw_requires = payload.get("requires")
    if raw_requires is not None:
        if not isinstance(raw_requires, dict):
            problems.append(_bad("\"requires\" must be a JSON object."))
        else:
            for key in sorted(set(raw_requires) - _REQUIRES_KEYS):
                warnings.append(Problem(ProblemCode.NOTE, f"\"requires.{key}\" is not understood and was ignored."))
            requires = Requirements(
                _names(raw_requires.get("capabilities"), "requires.capabilities", problems, _capability_id),
                _names(raw_requires.get("modules"), "requires.modules", problems, valid_module_id),
                _names(raw_requires.get("python_packages"), "requires.python_packages", problems, _python_name),
            )
            if module_id and module_id in requires.modules:
                problems.append(_bad("A module cannot require itself."))

    for key in sorted(set(payload) - _KNOWN_KEYS):
        warnings.append(Problem(ProblemCode.NOTE, f"\"{key}\" is not understood and was ignored."))

    if problems:
        return ManifestResult(None, tuple(problems), tuple(warnings))
    manifest = ModuleManifest(
        module_id, name, version, api_range, entry, groups, description, author, homepage, requires,
    )
    return ManifestResult(manifest, (), tuple(warnings))


def read_manifest(folder: Path) -> ManifestResult:
    """Read ``folder/module.json`` without importing anything from the folder."""

    path = Path(folder) / MANIFEST_NAME
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return ManifestResult(None, (_bad(f"The folder has no {MANIFEST_NAME}."),))
    except OSError as error:
        return ManifestResult(None, (_bad(f"{MANIFEST_NAME} could not be read: {error.strerror or error}."),))
    if size > MANIFEST_MAX_BYTES:
        return ManifestResult(None, (_bad(f"{MANIFEST_NAME} is larger than {MANIFEST_MAX_BYTES // 1024} KiB."),))
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return ManifestResult(None, (_bad(f"{MANIFEST_NAME} is not UTF-8 text."),))
    except OSError as error:
        return ManifestResult(None, (_bad(f"{MANIFEST_NAME} could not be read: {error.strerror or error}."),))
    try:
        payload = json.loads(text)
    except ValueError as error:
        return ManifestResult(None, (_bad(f"{MANIFEST_NAME} is not valid JSON ({error})."),))
    except (RecursionError, MemoryError):
        # A small file of deeply nested arrays or objects exhausts the decoder. Report it for
        # this folder only, so one bad manifest never stops the rest of the scan.
        return ManifestResult(None, (_bad(f"{MANIFEST_NAME} is nested too deeply to read."),))
    try:
        return manifest_from_dict(payload)
    except (RecursionError, MemoryError):
        return ManifestResult(None, (_bad(f"{MANIFEST_NAME} is nested too deeply to read."),))


def api_compatibility(manifest: ModuleManifest, host_api: str) -> tuple[Problem | None, Problem | None]:
    """Return ``(blocking problem, warning)`` for this host's Core API version."""

    try:
        host = Version.parse(host_api)
    except ValueError:
        return (Problem(ProblemCode.API_VERSION_MISMATCH,
                        f"The EFB reports an unreadable Core API version \"{host_api}\"."), None)
    if not manifest.api_version.contains(host):
        return (Problem(ProblemCode.API_VERSION_MISMATCH,
                        f"Needs Core API {manifest.api_version}; this EFB provides {host_api}."), None)
    if host.label or host.major == 0:
        return (None, Problem(ProblemCode.DRAFT_API,
                              f"Core API {host_api} is a pre-1.0 draft; calls may change in a later EFB release."))
    return (None, None)
