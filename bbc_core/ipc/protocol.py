"""EXPERIMENTAL wire protocol for a standalone Black Box Core process.

Not wired into the EFB. The first whole-EFB build calls Core in-process. This module is a
prototype for the second release, where Core moves to its own process and modules reach it
over a versioned local interface. See ``docs/STANDALONE_CORE_IPC.md``.

Framing
    Every message is one UTF-8 JSON object sent with ``Connection.send_bytes``. The
    ``multiprocessing.connection`` transport prefixes each message with its byte length, so a
    reader always knows where a message ends and can refuse one that is too large before
    reading it. Pickle is never used: a peer only ever sends data, never code.

Values
    Contract dataclasses, enums, datetimes, UUIDs, bytes, tuples, sets and non-finite floats
    are carried as tagged JSON objects (see :func:`encode`). Only types listed in the wire
    registry can cross the interface, so an unknown or changed type fails loudly instead of
    arriving as something else. :func:`schema_fingerprint` hashes the registry so both ends can
    prove they agree on every field name before any data flows.

Versioning
    ``protocol_version`` covers framing, message types and tags. ``api_version`` is the Core
    contract version (``bbc_core.contract.API_VERSION``). Both are negotiated in the ``hello``
    message, whose shape must stay readable by every later version.
"""

from __future__ import annotations

import base64
import getpass
import os
import sys
import tempfile
import time
import dataclasses
import hashlib
import hmac
import json
import re
import secrets
from datetime import date, datetime, timedelta
from enum import Enum
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from .. import client_data as _client_data
from .. import contract as _contract
from .. import facilities as _facilities
from .. import sdk_simvar as _sdk_simvar
from .. import simulator_ops as _simulator_ops
from ..contract import API_VERSION, CoreRequestError

EXPERIMENTAL = True

PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS: tuple[int, ...] = (1,)

#: Largest message accepted before authentication completes.
MAX_HANDSHAKE_BYTES = 64 * 1024
#: Default largest message after authentication. A worldwide airport list can be several MB.
DEFAULT_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
#: Deepest nesting of tagged values accepted from a peer.
MAX_DEPTH = 48
#: Length of the random nonces used in the handshake.
NONCE_BYTES = 32
#: Shortest accepted shared secret.
MIN_AUTHKEY_BYTES = 32

# Message types. ``hello`` and ``reject`` keep their shape across every protocol version.
HELLO = "hello"
CHALLENGE = "challenge"
AUTH = "auth"
WELCOME = "welcome"
REJECT = "reject"
CALL = "call"
RESULT = "result"
ERROR = "error"
UPDATES = "updates"
PING = "ping"
PONG = "pong"
BYE = "bye"
SHUTDOWN = "shutdown"
FATAL = "fatal"

# Delivery modes a client may request.
DELIVERY_PULL = "pull"   # poll() is a request; Core's own bounded queues hold updates
DELIVERY_PUSH = "push"   # the server streams updates; the client buffers them locally
DELIVERY_MODES = (DELIVERY_PULL, DELIVERY_PUSH)

# Rejection codes sent in a ``reject`` message.
REJECT_VERSION = "version_mismatch"
REJECT_API = "api_mismatch"
REJECT_SCHEMA = "schema_mismatch"
REJECT_AUTH = "auth_failed"
REJECT_ACCESS = "access_denied"
REJECT_BUSY = "busy"
REJECT_SHUTTING_DOWN = "shutting_down"
REJECT_BAD_REQUEST = "bad_request"
REJECT_REGISTRATION = "registration_failed"
REJECT_TIMEOUT = "timeout"

#: ConsumerAPI methods a remote module may call. ``close`` is the ``bye`` message instead.
CONSUMER_METHODS: frozenset[str] = frozenset({
    "discover", "connection", "subscribe", "subscribe_simvar", "subscribe_sdk_simvar",
    "subscribe_snapshot", "latest_snapshot", "latest", "request_system_state", "set_event_enabled",
    "poll", "request_airport", "request_nearby_airports", "request_worldwide_airports",
    "request_nearby_navaids", "request_navaid", "request_jetways", "facility_result",
    "request_controllers", "request_dialog_notifications", "request_nearby_objects",
    "request_object_data", "simulator_result", "subscribe_facility_cache", "facility_cache_snapshot",
    "submit_command", "submit_simvar", "submit_client_event", "subscribe_client_event",
    "submit_simulator_operation", "subscribe_controller", "request_aircraft_inputs", "aircraft_inputs",
    "request_input_parameters", "input_parameters", "subscribe_input", "request_input_value",
    "submit_input", "client_data_areas", "subscribe_client_data", "request_client_data",
    "submit_client_data", "command_result", "unsubscribe",
})


class WireError(ValueError):
    """A message or value does not follow the wire protocol."""


# -- Type registry ---------------------------------------------------------------------------

# Core-internal request descriptions that never cross the consumer interface.
_INTERNAL_TYPES = frozenset({"FacilityQuery", "SimulatorRead"})
_REGISTRY_MODULES = (_contract, _sdk_simvar, _client_data, _facilities, _simulator_ops)


def _collect_types(modules) -> dict[str, type]:
    """Public dataclasses and enums defined in ``modules``, keyed by class name."""
    found: dict[str, type] = {}
    for module in modules:
        for name, value in sorted(vars(module).items()):
            if name.startswith("_") or not isinstance(value, type) or value.__module__ != module.__name__:
                continue
            if value is _contract.StrEnum or name in _INTERNAL_TYPES:
                continue
            if not (dataclasses.is_dataclass(value) or issubclass(value, Enum)):
                continue
            if name in found and found[name] is not value:
                raise RuntimeError(f"Two wire types share the name {name}.")
            found[name] = value
    return found


WIRE_TYPES: Mapping[str, type] = MappingProxyType(_collect_types(_REGISTRY_MODULES))


def _fingerprint(types: Mapping[str, type]) -> str:
    lines = []
    for name in sorted(types):
        cls = types[name]
        if issubclass(cls, Enum):
            members = ",".join(f"{member.name}={member.value!r}" for member in cls)
            lines.append(f"enum {name}:{members}")
        else:
            fields = ",".join(field.name for field in dataclasses.fields(cls))
            lines.append(f"class {name}:{fields}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:32]


_SCHEMA = _fingerprint(WIRE_TYPES)


def schema_fingerprint() -> str:
    """A short hash of every wire type's name, field names and enum members."""
    return _SCHEMA


# -- Value encoding --------------------------------------------------------------------------

def encode(value: Any) -> Any:
    """Turn a contract value into JSON-compatible data.

    Plain JSON arrays are tuples, because the contract uses tuples throughout. A list is
    sent as ``{"$l": [...]}`` only if it ever appears; dicts always travel as key/value pairs
    so keys keep their type. Raises :class:`WireError` for a type outside the registry.
    """
    return _encode(value, 0)


def _encode(value, depth):
    if depth > MAX_DEPTH:
        raise WireError("The value is nested too deeply for the local interface.")
    if value is None or type(value) is bool or type(value) is str:
        return value
    if isinstance(value, Enum):
        cls = type(value)
        if WIRE_TYPES.get(cls.__name__) is not cls:
            raise WireError(f"{cls.__name__} is not part of the wire contract.")
        raw = value.value
        return {"$e": cls.__name__, "v": raw if isinstance(raw, (str, int)) else _encode(raw, depth + 1)}
    if type(value) is int:
        return value
    if type(value) is float:
        if isfinite(value):
            return value
        return {"$f": "nan" if value != value else ("inf" if value > 0 else "-inf")}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$d": value.isoformat()}
    if isinstance(value, timedelta):
        return {"$td": [value.days, value.seconds, value.microseconds]}
    if isinstance(value, UUID):
        return {"$uuid": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        cls = type(value)
        if WIRE_TYPES.get(cls.__name__) is not cls:
            raise WireError(f"{cls.__name__} is not part of the wire contract.")
        return {"$t": cls.__name__, "f": {
            field.name: _encode(getattr(value, field.name), depth + 1) for field in dataclasses.fields(cls)
        }}
    if type(value) is tuple:
        return [_encode(item, depth + 1) for item in value]
    if type(value) is list:
        return {"$l": [_encode(item, depth + 1) for item in value]}
    if isinstance(value, (set, frozenset)):
        items = [_encode(item, depth + 1) for item in value]
        items.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {"$set": items, "frozen": isinstance(value, frozenset)}
    if type(value) is dict:
        return {"$map": [[_encode(key, depth + 1), _encode(item, depth + 1)] for key, item in value.items()]}
    raise WireError(f"A {type(value).__name__} value cannot be sent over the local interface.")


def decode(data: Any, *, validate: bool = True) -> Any:
    """Rebuild a value produced by :func:`encode`.

    With ``validate`` (used for anything a client sends), dataclasses are rebuilt through
    their constructors, so a remote request gets exactly the validation an in-process call
    gets. Without it (used for Core's own answers), fields are restored as sent, so the value
    is bit-for-bit what Core produced even if validation rules change later.
    """
    try:
        return _decode(data, validate, 0)
    except _Rejected as rejected:
        raise rejected.error from None


class _Rejected(Exception):
    """Carries a constructor's own validation error past the malformed-value handler."""

    def __init__(self, error: Exception):
        super().__init__(str(error))
        self.error = error


def _decode(data, validate, depth):
    if depth > MAX_DEPTH:
        raise WireError("The value is nested too deeply for the local interface.")
    if data is None or type(data) in (bool, str, int):
        return data
    if type(data) is float:
        if not isfinite(data):
            raise WireError("Non-finite numbers must be tagged.")
        return data
    if type(data) is list:
        return tuple(_decode(item, validate, depth + 1) for item in data)
    if type(data) is not dict:
        raise WireError(f"Unexpected JSON value of type {type(data).__name__}.")
    try:
        if "$t" in data:
            _exact_keys(data, {"$t", "f"})
            return _decode_dataclass(data["$t"], data["f"], validate, depth)
        if "$e" in data:
            _exact_keys(data, {"$e", "v"})
            cls = _wire_type(data["$e"])
            if not issubclass(cls, Enum):
                raise WireError(f"{data['$e']} is not an enum.")
            raw = data["v"]
            if type(raw) not in (str, int):
                raw = _decode(raw, validate, depth + 1)
            try:
                return cls(raw)
            except ValueError as error:
                raise WireError(f"{raw!r} is not a valid {cls.__name__}.") from error
        if "$dt" in data:
            _exact_keys(data, {"$dt"})
            return datetime.fromisoformat(_text(data["$dt"]))
        if "$d" in data:
            _exact_keys(data, {"$d"})
            return date.fromisoformat(_text(data["$d"]))
        if "$td" in data:
            _exact_keys(data, {"$td"})
            days, seconds, micro = data["$td"]
            if not all(type(part) is int for part in (days, seconds, micro)):
                raise WireError("A duration needs three integers.")
            return timedelta(days=days, seconds=seconds, microseconds=micro)
        if "$uuid" in data:
            _exact_keys(data, {"$uuid"})
            return UUID(_text(data["$uuid"]))
        if "$b" in data:
            _exact_keys(data, {"$b"})
            return base64.b64decode(_text(data["$b"]).encode("ascii"), validate=True)
        if "$f" in data:
            _exact_keys(data, {"$f"})
            special = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}
            if data["$f"] not in special:
                raise WireError("Unknown special number.")
            return special[data["$f"]]
        if "$l" in data:
            _exact_keys(data, {"$l"})
            return [_decode(item, validate, depth + 1) for item in _array(data["$l"])]
        if "$set" in data:
            _exact_keys(data, {"$set", "frozen"})
            items = [_decode(item, validate, depth + 1) for item in _array(data["$set"])]
            return frozenset(items) if data["frozen"] is True else set(items)
        if "$map" in data:
            _exact_keys(data, {"$map"})
            result = {}
            for pair in _array(data["$map"]):
                if type(pair) is not list or len(pair) != 2:
                    raise WireError("A map entry must be a key and a value.")
                result[_decode(pair[0], validate, depth + 1)] = _decode(pair[1], validate, depth + 1)
            return result
    except (WireError, _Rejected):
        raise
    except (TypeError, ValueError, KeyError) as error:
        raise WireError(f"Malformed tagged value: {error}") from error
    raise WireError("Untagged JSON objects are not part of the wire protocol.")


def _decode_dataclass(name, fields, validate, depth):
    cls = _wire_type(name)
    if not dataclasses.is_dataclass(cls):
        raise WireError(f"{name} is not a data type.")
    if type(fields) is not dict:
        raise WireError(f"{name} fields must be an object.")
    known = {field.name: field for field in dataclasses.fields(cls)}
    unknown = set(fields) - set(known)
    if unknown:
        raise WireError(f"{name} has no field {sorted(unknown)[0]}; the peers disagree on the contract.")
    values = {key: _decode(item, validate, depth + 1) for key, item in fields.items()}
    if validate:
        init = {key: item for key, item in values.items() if known[key].init}
        try:
            instance = cls(**init)
        except TypeError as error:
            raise WireError(f"{name} could not be rebuilt: {error}") from error
        except ValueError as error:  # CoreRequestError included: the same error an in-process call gets
            raise _Rejected(error) from error
        for key, item in values.items():
            if not known[key].init:
                object.__setattr__(instance, key, item)
        return instance
    missing = [field for field in known.values() if field.name not in values]
    instance = object.__new__(cls)
    for field in missing:
        if field.default is not dataclasses.MISSING:
            values[field.name] = field.default
        elif field.default_factory is not dataclasses.MISSING:
            values[field.name] = field.default_factory()
        else:
            raise WireError(f"{name} is missing its {field.name} field.")
    for key, item in values.items():
        object.__setattr__(instance, key, item)
    return instance


def _wire_type(name):
    cls = WIRE_TYPES.get(name) if type(name) is str else None
    if cls is None:
        raise WireError(f"{name!r} is not part of the wire contract.")
    return cls


def _exact_keys(data, allowed):
    if set(data) != allowed:
        raise WireError(f"Unexpected keys in a tagged value: {sorted(set(data) ^ allowed)}.")


def _text(value):
    if type(value) is not str:
        raise WireError("Expected text.")
    return value


def _array(value):
    if type(value) is not list:
        raise WireError("Expected an array.")
    return value


# -- Messages --------------------------------------------------------------------------------

def dumps(message: Mapping[str, Any]) -> bytes:
    """Serialise one message. Values inside must already be encoded."""
    try:
        return json.dumps(message, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WireError(f"The message could not be serialised: {error}") from error


def loads(data: bytes) -> dict[str, Any]:
    """Parse one message and check it is an object with a text ``type``."""
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise WireError("The message is not valid UTF-8 JSON.") from error
    if type(message) is not dict or type(message.get("type")) is not str:
        raise WireError("A message must be a JSON object with a text type.")
    return message


def send_message(connection, message: Mapping[str, Any]) -> None:
    connection.send_bytes(dumps(message))


def recv_message(connection, max_bytes: int) -> dict[str, Any]:
    """Receive one message. Raises EOFError/OSError when the peer is gone or oversends."""
    return loads(connection.recv_bytes(max_bytes))


# -- Errors ----------------------------------------------------------------------------------

#: Exception kinds re-raised as the same class on the client.
_ERROR_KINDS = {"CoreRequestError": CoreRequestError, "ValueError": ValueError, "TypeError": TypeError}


class RemoteCoreError(RuntimeError):
    """Core failed while handling a remote request, for a reason other than a bad request."""


def encode_error(error: BaseException) -> dict[str, str]:
    kind = type(error).__name__ if type(error) in _ERROR_KINDS.values() else "InternalError"
    if isinstance(error, WireError):
        kind = "WireError"
    elif isinstance(error, CoreRequestError):
        kind = "CoreRequestError"
    message = str(error) if kind != "InternalError" else (
        f"Core failed while handling the request ({type(error).__name__}: {error})."
    )
    return {"kind": kind, "message": message}


def decode_error(data: Any) -> Exception:
    if type(data) is not dict or type(data.get("message")) is not str:
        return RemoteCoreError("Core reported an error in an unreadable form.")
    kind = data.get("kind")
    if kind == "WireError":
        return CoreRequestError(f"The local interface could not carry the request: {data['message']}")
    cls = _ERROR_KINDS.get(kind)
    return cls(data["message"]) if cls else RemoteCoreError(data["message"])


# -- Versions --------------------------------------------------------------------------------

_API_PATTERN = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.]+))?")


def parse_api_version(text: str) -> tuple[int, int, int, str] | None:
    """``"0.1-draft"`` -> ``(0, 1, 0, "draft")``. Returns None for an unreadable version."""
    match = _API_PATTERN.fullmatch(text) if type(text) is str else None
    if match is None:
        return None
    major, minor, patch, tag = match.groups()
    return int(major), int(minor), int(patch or 0), tag or ""


def api_versions_compatible(client: str, server: str) -> tuple[bool, str]:
    """PROPOSAL rule: same major; while major is 0 also same minor; pre-release tags must match.

    A server may be a newer patch or, for major >= 1, a newer minor, because new capabilities
    and optional fields may be added. Everything else is refused with a plain reason.
    """
    a, b = parse_api_version(client), parse_api_version(server)
    if a is None or b is None:
        return False, f"Core API version {client!r} or {server!r} cannot be read."
    if a[3] != b[3] and (a[3] or b[3]):
        return False, f"This module was built for Core API {client}; this Core provides {server}."
    if a[0] != b[0]:
        return False, f"This module was built for Core API {client}; this Core provides {server}."
    if a[0] == 0 and a[1] != b[1]:
        return False, f"This module was built for Core API {client}; this Core provides {server}."
    if (b[1], b[2]) < (a[1], a[2]):
        return False, f"This module needs Core API {client} or newer; this Core provides {server}."
    return True, ""


def choose_protocol_version(offered: Any, supported: tuple[int, ...] = SUPPORTED_PROTOCOL_VERSIONS) -> int | None:
    if type(offered) is not list or not all(type(item) is int for item in offered):
        return None
    common = set(offered) & set(supported)
    return max(common) if common else None


# -- Handshake proofs ------------------------------------------------------------------------

def new_nonce() -> bytes:
    return secrets.token_bytes(NONCE_BYTES)


def proof(key: bytes, label: str, *parts: bytes) -> str:
    """HMAC-SHA256 over a role label and the handshake parts, as hex."""
    mac = hmac.new(key, label.encode("ascii"), hashlib.sha256)
    for part in parts:
        mac.update(len(part).to_bytes(4, "big"))
        mac.update(part)
    return mac.hexdigest()


def proof_matches(expected: str, received: Any) -> bool:
    return type(received) is str and hmac.compare_digest(expected.encode("ascii"), received.encode("ascii", "replace"))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: Any, *, size: int | None = None) -> bytes:
    if type(text) is not str:
        raise WireError("Expected base64 text.")
    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise WireError("Invalid base64 text.") from error
    if size is not None and len(data) != size:
        raise WireError("A handshake value has the wrong length.")
    return data


def hello_digest(message: Mapping[str, Any]) -> bytes:
    """Bind the authenticated session to the exact hello the client sent."""
    return hashlib.sha256(json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8")).digest()


# -- Local transport -------------------------------------------------------------------------

def default_family() -> str:
    """``AF_PIPE`` (a Windows named pipe) on Windows, ``AF_UNIX`` (a socket file) elsewhere."""
    return "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"


def _user_tag() -> str:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name available; fall back to a fixed tag
        name = "user"
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def runtime_directory() -> Path:
    """A private per-user directory for the AF_UNIX socket, created with mode 0700."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(base) / "bbc-core" if base else Path(tempfile.gettempdir()) / f"bbc-core-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"{root} must belong to this user and be private (mode 0700).")
    return root


def default_address(family: str | None = None) -> str:
    r"""The well-known local address of the standalone Core for this user.

    A Windows named pipe is ``\\.\pipe\bbc-core-<user hash>-p1``; elsewhere a socket file in
    :func:`runtime_directory`. The ``p1`` suffix is the protocol generation, so an
    incompatible future Core can listen beside this one during an upgrade.
    """
    family = family or default_family()
    if family == "AF_PIPE":
        return rf"\\.\pipe\bbc-core-{_user_tag()}-p{PROTOCOL_VERSION}"
    if family == "AF_UNIX":
        return str(runtime_directory() / f"core-p{PROTOCOL_VERSION}.sock")
    raise ValueError("The standalone Core listens only on a local named pipe or socket file.")


def default_authkey_path() -> Path:
    """Where the per-install shared secret lives (not created until asked)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "BBC-EFB" / "core-ipc.key"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "bbc-efb" / "core-ipc.key"


def load_or_create_authkey(path: str | os.PathLike | None = None, *, wait_s: float = 1.0) -> bytes:
    """Read the shared secret, or create a random 32-byte one readable only by this user.

    On Windows the file inherits the per-user ACL of ``%LOCALAPPDATA%``. Elsewhere it is
    created with mode 0600 and refused if group or other users can read it.

    The key is written in full to a temporary file in the same folder and only then linked
    into place (``os.link``, which fails when the key already exists), so the key file is
    never seen half written. When two processes start together, one key wins and both
    return it. A short key file is waited on for up to ``wait_s`` seconds (an older build may
    still be writing it). An empty one, left by an interrupted first start of an older build,
    is then moved aside as ``core-ipc.key.<time>.broken`` and replaced with a new key; a
    non-empty short key is a configuration error and raises ``ValueError``.
    """
    target = Path(path) if path is not None else default_authkey_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(wait_s))
    repaired = False
    while True:
        key = _read_authkey(target)
        if key is None:
            _create_authkey(target)
            continue
        if len(key) >= MIN_AUTHKEY_BYTES:
            return key
        if time.monotonic() < deadline:
            time.sleep(0.02)
            continue
        if key or repaired:
            # A short key someone put there is a configuration error, never silently replaced.
            raise ValueError(f"{target} holds a key shorter than {MIN_AUTHKEY_BYTES} bytes.")
        repaired = True
        _move_short_authkey_aside(target)


def _read_authkey(target: Path) -> bytes | None:
    """The key file's bytes, or None when there is no key file."""
    try:
        mode = target.stat().st_mode
    except FileNotFoundError:
        return None
    if sys.platform != "win32" and mode & 0o077:
        raise PermissionError(f"{target} is readable by other users; restrict it to mode 0600.")
    try:
        return target.read_bytes()
    except FileNotFoundError:
        return None


def _create_authkey(target: Path) -> None:
    """Put a fully written new key at ``target`` unless a key is already there."""
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            if sys.platform != "win32":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(secrets.token_bytes(MIN_AUTHKEY_BYTES))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass  # another process created the key first; it is read and used
        except OSError:
            if sys.platform != "win32":
                raise
            # A Windows volume without hard links: rename refuses to replace an existing file there.
            try:
                os.rename(temporary, target)
            except FileExistsError:
                pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _move_short_authkey_aside(target: Path) -> None:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    aside = target.with_name(f"{target.name}.{stamp}-{secrets.token_hex(3)}.broken")
    try:
        os.rename(target, aside)
    except FileNotFoundError:
        return  # another process already moved it
    try:
        moved = aside.read_bytes()
    except OSError:
        return
    if len(moved) >= MIN_AUTHKEY_BYTES:
        # A good key replaced the short one just before the move: put it back, never lose it.
        try:
            os.link(aside, target)
        except FileExistsError:
            pass
        except OSError:
            return
        os.unlink(aside)


def check_authkey(key: Any) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_AUTHKEY_BYTES:
        raise ValueError(f"An IPC key must be at least {MIN_AUTHKEY_BYTES} random bytes.")
    return bytes(key)


__all__ = [
    "API_VERSION", "CONSUMER_METHODS", "check_authkey", "default_address", "default_authkey_path",
    "default_family", "load_or_create_authkey", "runtime_directory", "DEFAULT_MAX_MESSAGE_BYTES", "DELIVERY_MODES", "DELIVERY_PULL",
    "DELIVERY_PUSH", "EXPERIMENTAL", "PROTOCOL_VERSION", "SUPPORTED_PROTOCOL_VERSIONS", "WIRE_TYPES",
    "RemoteCoreError", "WireError", "api_versions_compatible", "choose_protocol_version", "decode",
    "decode_error", "dumps", "encode", "encode_error", "loads", "parse_api_version", "recv_message",
    "schema_fingerprint", "send_message",
]
