# Standalone Core local interface (experimental)

> **Status: EXPERIMENTAL PROTOTYPE. Not wired into the EFB and off by default: nothing starts a
> server unless code calls `CoreServer.start()`.** The first whole-EFB build runs
> Black Box Core in-process (sources/08, 24 November 2026), and nothing in `bbc_efb/` imports
> `bbc_core.ipc`. This package and document are design input for the second release. Every
> rule marked **PROPOSAL** needs an owner decision before it is relied on. Tested offline only,
> with the explicitly simulated provider, on macOS (AF_UNIX). The Windows named-pipe path uses
> the same code through `multiprocessing.connection` but has **not yet been run on Windows**.

## 1. Why and what

The approved architecture (sources/02) moves Core into its own process after the first-build
review, carrying the consumer API "over a versioned local interface". Review finding A69 noted
that the API returns frozen dataclasses with datetimes, enums, UUIDs and bytes, and has no
serialisation layer. This prototype provides:

| Piece | Module | Purpose |
|---|---|---|
| Wire protocol | `bbc_core/ipc/protocol.py` | Framing, message types, tagged value encoding, version and schema negotiation, handshake proofs, local addresses and key file |
| Server | `bbc_core/ipc/server.py` | `CoreServer`: listens locally, authenticates, registers one Core consumer per client, applies control-group grants, cleans up on disconnect and shutdown |
| Client | `bbc_core/ipc/client.py` | `connect()` and `RemoteConsumerAPI`, which has the same method names and arguments as `ConsumerAPI` and returns the same contract types |

Core itself is unchanged. The server calls the ordinary `ConsumerAPI` that
`Core.register_consumer()` returns, so Core's validation, bounded queues, gap reporting,
command coalescing and demand reconciliation behave exactly as they do in-process.

```text
 module process                       Core process
 ┌──────────────────────┐   local    ┌────────────────────────────────────────┐
 │ RemoteConsumerAPI    │  pipe or   │ CoreServer ── thread per client ──┐    │
 │  .subscribe_snapshot ├──socket────┤   handshake, grants, calls, push  │    │
 │  .poll()  (reader    │  file only │                                   ▼    │
 │   thread, buffer)    │            │   ConsumerAPI (one per client) ─► Core │
 └──────────────────────┘            │   CoreHost thread: Core.pump() ─► SimConnect
                                     └────────────────────────────────────────┘
```

## 2. Transport

* **Windows:** a named pipe, `\\.\pipe\bbc-core-<hash of user name>-p1`, through
  `multiprocessing.connection.Listener(family="AF_PIPE")`. The stdlib creates the first pipe
  instance with `FILE_FLAG_FIRST_PIPE_INSTANCE`, so a second process cannot silently take over
  a pipe name Core already holds. `server.open_listener` uses a `PipeListener` subclass that
  creates every pipe instance with `PIPE_REJECT_REMOTE_CLIENTS`, so SMB clients on other
  machines are refused. The pipe still has the stdlib default DACL (not owner-only); see
  section 10. Neither has been checked on Windows yet.
* **Elsewhere (development and CI):** a socket file `core-p1.sock` in a private per-user
  directory (`$XDG_RUNTIME_DIR/bbc-core` or `<tmp>/bbc-core-<uid>`, mode 0700); the socket
  itself is set to 0600. A socket file left by a crashed Core is detected (nothing answers)
  and replaced; a live one is never replaced.
* **No network port** is ever opened, and `AF_INET` is refused. Importing the package opens
  nothing; only `CoreServer.start()` listens. That keeps the "no unapproved listeners" rule
  (sources/02): the EFB does not start it.
* **Framing:** `Connection.send_bytes()` length-prefixes every message, so a reader knows
  where a message ends and can refuse an oversized one before reading it (64 KiB before
  authentication, 32 MiB after, both configurable). **Pickle is never used**: only JSON data
  crosses the boundary, so a peer can never make the other side run code.
* The `p1` suffix is the protocol generation, so an incompatible future Core can listen beside
  this one during an upgrade.

## 3. Messages

Every message is one UTF-8 JSON object with a text `type`.

| Direction | `type` | Contents |
|---|---|---|
| C → S | `hello` | `protocol_versions` (list), `api_version`, `schema`, `principal`, `client_name`, `client_pid`, `control_groups`, `diagnostics`, `delivery` (`pull`/`push`), `queue_size`, `nonce` |
| S → C | `challenge` | server `nonce`, server `proof` |
| C → S | `auth` | client `proof` |
| S → C | `welcome` | chosen `protocol_version`, `api_version`, `schema`, `server_name`, `client_id`, granted `control_groups`, `denied_control_groups`, `diagnostics`, `delivery`, `initial_connection`, `max_message_bytes` |
| S → C | `reject` | `code`, plain-English `reason`, `server_protocol_versions`, `server_api_version` |
| C → S | `call` | `id`, `method` (a `ConsumerAPI` method name), `args`, `kwargs` (encoded values) |
| S → C | `result` / `error` | `id` and `value`, or `id` and `error` {`kind`, `message`} |
| S → C | `updates` | `items`: encoded updates (push delivery only) |
| C ↔ S | `ping` / `pong` | `id` |
| C → S | `bye` | graceful close; the server closes the consumer |
| S → C | `shutdown` | `reason`, then the server closes the connection |
| S → C | `fatal` | `reason` for a protocol violation, then the server closes the connection |

The shapes of `hello` and `reject` are frozen across all protocol versions, so any future
Core can always tell an old client why it was refused.

**Calls.** Only the 44 names in `protocol.CONSUMER_METHODS` can be called. That is every
public `ConsumerAPI` method except `close` (which is `bye`). A test fails as soon as
`ConsumerAPI` gains a method without a wire decision, and another checks that
`RemoteConsumerAPI` has the same parameter names and kinds. Wrong arguments come back as
`CoreRequestError`, not as a crash.

**Errors.** `CoreRequestError`, `ValueError` and `TypeError` are raised again on the client as
the same class with the same message. A request that cannot be encoded raises
`CoreRequestError`. Anything else raises `RemoteCoreError`. Refused commands are still
`CommandResult(state=REJECTED)`, exactly as in-process.

## 4. Values on the wire

`protocol.encode()` / `decode()` walk dataclasses generically through `dataclasses.fields()`,
so a new contract field is carried without hand-written code.

| Python value | JSON |
|---|---|
| `None`, `bool`, `int`, `str`, finite `float` | as is |
| NaN, ±infinity | `{"$f": "nan" \| "inf" \| "-inf"}` (JSON has no such numbers) |
| `tuple` | JSON array (the contract uses tuples throughout) |
| `list` | `{"$l": [...]}` |
| `set` / `frozenset` | `{"$set": [...], "frozen": bool}` (sorted, so deterministic) |
| `dict` | `{"$map": [[key, value], ...]}` (keys keep their types) |
| `bytes` | `{"$b": base64}` |
| `datetime` / `date` / `timedelta` | `{"$dt": isoformat}` / `{"$d": ...}` / `{"$td": [days, s, µs]}`; a naive time stays naive and an aware one keeps its offset |
| `UUID` | `{"$uuid": text}` |
| Enum / StrEnum / IntFlag (`PauseState`) | `{"$e": class name, "v": value}`; unknown pause bits are kept |
| Contract dataclass | `{"$t": class name, "f": {field: value}}` |

* **Registry.** Only public dataclasses and enums defined in `contract`, `sdk_simvar`,
  `client_data`, `facilities` and `simulator_ops` can cross (66 types today; the registry is
built from those modules, so new public types join it automatically). Core-internal
  request types (`FacilityQuery`, `SimulatorRead`) are excluded. An unregistered type, a
  look-alike class with a registered name, an unknown field, an untagged object or deep
  nesting is refused with `WireError`. Nothing is coerced into something else.
* **Validation.** Values a *client* sends are rebuilt through their constructors, so a remote
  `SimVar("bad name!", ...)` gets the same `CoreRequestError` and message as in-process. Values
  *Core* sends are restored field by field without re-validation, so the module sees exactly
  what Core produced.
* **Schema fingerprint.** `schema_fingerprint()` hashes every registered type name, field name
  and enum member. Both sides send it in the handshake. A mismatch is refused (`schema_mismatch`)
  instead of risking a misread field.
* **Every update type.** Everything `poll()` can return crosses unchanged and keeps its
  exact type: `Connection`, `Observation`, `Event`, `Gap`, `SignalError`, `CommandResult`,
  `InputCatalog`, `InputParameters`, `Snapshot` (with its simulator clocks and `None` field
  values), `SubscriptionEnded`, and the facility and simulator results. `SignalError` is its
  own type, not a `Gap` (shared contract C7), so a provider error never arrives as data loss
  or the reverse. `PauseState` flags arrive as `PauseState`, unknown bits kept, and still
  compare equal to the plain integer. A test builds a sample of every member of
  `bbc_core.contract.Update` from its field types and round-trips it, so a new update type
  or field is covered automatically.
* **Unencodable updates.** If Core ever produces an update the wire cannot carry, the server
  sends a `Gap(1, "Core could not send one <Type> update over the local interface.")` in its
  place. Nothing is dropped silently.

## 5. Versions

* `protocol_version` (an integer, now 1) covers framing, message types and tags. The client
  offers a list and the server picks the highest common one, or refuses with
  `version_mismatch`, naming both sides' versions.
* `api_version` is `bbc_core.contract.API_VERSION` (now `0.1-draft`). **PROPOSAL compatibility
  rule** (`protocol.api_versions_compatible`): the major version must match; while major is 0
  the minor must match too; pre-release tags such as `-draft` must match exactly; and the server
  may be the same or newer within those limits, because new capabilities and optional fields
  are additive. A refusal reads, for example: "This module was built for Core API 0.9-draft;
  this Core provides 0.1-draft."
* Schema fingerprint: see section 4. **PROPOSAL:** keep the strict match until the API leaves
  draft, then allow additive field changes with defaults (the decoder already fills a missing
  defaulted field).

## 6. Authentication and access

**Handshake (mutual).** Each side proves it holds the shared secret for the named principal
without sending it:

1. The client sends `hello` with a random 32-byte nonce.
2. The server checks the versions, schema and fields, then sends its own nonce and
   `HMAC-SHA256(key, "bbc-core-server" | client nonce | server nonce | sha256(hello))`.
3. The client checks that proof *before sending anything else*. A process squatting on the
   address without the key gets `ServerNotTrusted` and no data.
4. The client answers with the same HMAC under the label `"bbc-core-client"`. The server
   compares it in constant time and refuses with `auth_failed` if it differs.

The proofs cover a hash of the exact `hello`, so the requested groups cannot be changed after
the fact. The whole handshake runs in the client's own server thread, so a silent or hostile
client cannot stall the accept loop. It times out after `handshake_timeout_s` (5 s by default).

**Keys.** `load_or_create_authkey()` keeps a random 32-byte per-install secret in
`%LOCALAPPDATA%\BBC-EFB\core-ipc.key` on Windows (inheriting the per-user profile ACL) or
`~/.config/bbc-efb/core-ipc.key` elsewhere (created 0600, refused if others can read it).
`CoreServer(authkeys=...)` takes one key (principal `default`) or a mapping of principal to key.

**Grants.** After authentication, the server asks its `AccessPolicy` for the most this
principal may hold, and grants the intersection with what the client requested. The welcome
reports the granted and the denied groups, so a module can say plainly what it cannot do.
Then the server calls `register_consumer(control_groups=granted, diagnostics=..., queue_size=...)`.

* `ReadOnlyPolicy` (default, **PROPOSAL**): any authenticated client may read; nobody may write.
* `PrincipalPolicy({principal: Grant(...)}, default=...)`: grants per principal. `default=None`
  refuses principals that have no entry (`access_denied`).

Grants are tied to the principal, which the client proved by holding its key. `client_name`
and `client_pid` are self-declared labels, kept for diagnostics only.

## 7. Lifecycle

**Per client.** The server runs one thread per client. It is the only writer on that
connection. It answers calls in order, and in push mode it drains `api.poll()` every
`push_interval_s` (20 ms by default), with no wait while a full batch remains. When a client
leaves, for any reason (`bye`, EOF, a killed process, a protocol violation, a server
shutdown), the server calls `ConsumerAPI.close()`. Core then drops that consumer's
subscriptions, its facility watches and its unsent commands, and the next pump lowers
simulator demand. Tests prove this for a graceful close, a dropped connection and a child
process killed with `os._exit`.

**Delivery modes.**

* `pull` (default, **PROPOSAL**): `poll()` is a request. Core's own bounded per-consumer queues
  hold updates and report losses as `Gap`, exactly as in-process.
* `push`: the server streams `updates` and the client's reader thread buffers them in the
  same two lanes as Core's own consumer queue. The data lane (`Observation`, `Event`,
  `Snapshot`, `SignalError`; `buffer_size`, default 4,096) drops its oldest sample when
  full. The status lane (everything else: `Connection`, `CommandResult`, `Gap`,
  `SubscriptionEnded`, catalogs, request results; at least 64) is separate, so a burst of
  samples can never push out a connection change or a command outcome. Each lane reports
  its own loss at the start of the next `poll()`: `Gap(n, "The local interface buffer was
  full, ...")` for samples and `Gap(n, "The local interface status buffer was full, ...")`
  for status updates. Updates come back in arrival order. A `poll` call is refused on a
  push connection.

**Loss of contact.** The client's reader thread notices EOF, `shutdown` or `fatal`. Pending
calls fail with `CoreUnavailableError`. `poll()` then returns **one**
`Connection(state=ERROR, session_id=None, detail=...)`, for example "The standalone Core closed
this connection: <reason> The simulator connection state is unknown.", and afterwards nothing.
`connection()` keeps returning that notice, and every other call raises
`CoreUnavailableError`. The notice keeps the last known `Source` (so simulated stays labelled
simulated) and never claims a simulator session. A module that closed itself is not told it
"lost" anything.

**Server shutdown.** `CoreServer.close(reason)` stops accepting (it wakes `accept()` with a
local self-connection, which works for both pipes and sockets). Each client thread sends
`shutdown` with the reason, closes its consumer and exits. `close()` returns the IDs of any
clients whose threads did not finish in time. Their consumers are closed anyway. The socket file is
removed. Calling `close()` twice is harmless, and a new server can start on the same address.

**Capacity and quotas.** `max_clients` (16 by default, at most Core's 64-consumer limit)
applies. An extra client gets `busy` after its hello is read, so it always receives the
refusal. Core's own "consumer limit reached" error, or any other failure to register (for
example a stopped Core host), comes back as `registration_failed` with the reason. Each
remote client is an ordinary Core consumer, so Core's per-consumer quotas (128
subscriptions, 16 pending commands, 4 facility requests, a queue of at most 4,096) apply
unchanged; a call over a quota raises the same `CoreRequestError` with the same message as
in-process.

## 8. Using it (experimental)

```python
# Core process (prototype)
from bbc_core.host import CoreHost
from bbc_core.ipc import CoreServer, Grant, PrincipalPolicy, load_or_create_authkey

host = CoreHost(provider).start()    # pumps Core on its own thread and reconnects
server = CoreServer(host, authkeys=load_or_create_authkey(),
                    policy=PrincipalPolicy({"default": Grant(frozenset({"radios"}))})).start()
...
server.close("The EFB is closing.")
host.stop()
```

Without a host (tests), pass a `Core` and run `CorePumpThread(core)` beside it.

```python
# Module process
from bbc_core.contract import SnapshotPeriod, ValueType
from bbc_core.ipc import connect, load_or_create_authkey
from bbc_core.sdk_simvar import SimVar

api = connect(authkey=load_or_create_authkey(), client_name="My module", control_groups=("radios",))
print(api.granted_control_groups, api.denied_control_groups)
sub = api.subscribe_snapshot((SimVar("PLANE ALTITUDE", "feet", ValueType.NUMBER),), period=SnapshotPeriod.SECOND)
for update in api.poll():
    ...
api.close()
```

## 9. Integration notes

* **Core host.** `CoreServer` takes a `CoreHost` directly and gives each client its own
  unnamed consumer through `CoreHost.register_consumer(control_groups=, diagnostics=,
  queue_size=)` (shared contract C4); a test runs the server over the real host with no
  pump thread. `as_registrar()` also accepts a `Core`, or a host that only offers named
  `api(name, ...)` / `release(name)`, in which case each client gets a unique name that is
  released when it leaves. `CorePumpThread` exists only for tests and experiments. Core's
  `RLock` makes the per-client threads safe alongside the host's pump thread.
* **EFB.** A later `CoreBridge` could take a `RemoteConsumerAPI` in place of a `ConsumerAPI`:
  same methods, same update types. The bridge's poll timer keeps working in both delivery
  modes. `CoreUnavailableError` (a `ConnectionError`) is the new failure the shell must
  show as "Core not running" rather than "Simulator disconnected".
* **Packaging.** `pyproject.toml` now finds every package automatically, so `bbc_core.ipc`
  ships in a built wheel (the repository fix for missing subpackages). Shipping it changes
  nothing at run time: no EFB code imports it, importing it opens nothing, and only an
  explicit `CoreServer.start()` listens. It uses the standard library only. Whether the
  portable build should leave it out until the owner approves the second-release design is
  a packaging decision.
* **Offline guard.** The IPC tests are in `tests/` (`test_ipc_protocol.py`,
  `test_ipc_server.py`) and run in the ordinary suite. The guarded runner
  (`tests/test_repo_offline_guard.py`) blocks `socket.*` and `subprocess.*` audit events. The
  transport tests detect that runner and read its own rule: while it blocks socket files
  they are skipped with a reason (no guard violation is caused); once the runner allows
  AF_UNIX socket files (an allowance requested this round) they run under it too. The one
  test that kills a child process is always skipped under the guard, which blocks process
  creation by design; the in-process abrupt-disconnect test covers the same clean-up. On
  Windows the named pipe raises no `socket.*` events.
* **Run the tests:**
  `PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t . -p "test_ipc_*.py"`

## 10. Known limitations

* Not run on Windows yet. Every pipe instance is created with `PIPE_REJECT_REMOTE_CLIENTS`
  (checked offline with stand-ins only). The named-pipe security descriptor is still the stdlib
  default, which allows other local accounts to *open* the pipe. The HMAC handshake keeps them
  out, but an owner-approved hardening step should add an explicit DACL for the current user,
  which needs `ctypes`/`_winapi` work.
* A client process that stops reading while the server is mid-`send` can hold its own server
  thread. No other client is affected, and `close()` reports and releases it. A writer queue
  per client would remove this.
* The client PID is self-declared. On Windows, `GetNamedPipeClientProcessId` could verify it.
* Facility results may be several MB (worldwide airports). They travel as one message, up to
  the 32 MiB cap. Streaming pages would need a protocol version 2.
* No heartbeat. Local pipe and socket EOF is immediate when a process dies. `ping()` exists
  for supervisors.
* Push batching is time-based (20 ms). Latency and CPU have not been measured against MSFS.

## 11. Open owner decisions

| # | Decision | Prototype default (PROPOSAL) |
|---|---|---|
| 1 | **Hosting**: who starts the Core process: the EFB on launch, a per-user background process, or a Windows service? Is it one Core per user session? | Not decided. The server is a library object; the prototype runs it in-process for tests. A per-user process started by the EFB fits the portable-ZIP delivery best and needs no admin rights. |
| 2 | **Supervision and restart**: who restarts a crashed Core, how often, and how do clients reconnect? | Clients get one clear "lost contact" notice and must reconnect explicitly. No automatic reconnect, so a module never silently mixes two Core sessions. |
| 3 | **Access control**: one shared key, or a key per module? Who approves control groups (manifest plus a user toggle in Settings, as in A70)? | A per-install shared key, `ReadOnlyPolicy` (no writes) by default, and `PrincipalPolicy` ready for per-module keys and grants. |
| 4 | **Key storage**: a plain per-user file or DPAPI-protected? Rotation? | A random 32-byte file under `%LOCALAPPDATA%\BBC-EFB`, no rotation. |
| 5 | **API version rule** and when `-draft` ends. | As section 5. |
| 6 | **Delivery default**: pull or push? | Pull, because it keeps Core's own queue and gap semantics as the single source of truth. |
| 7 | **Message limits and capacity** (32 MiB, 16 clients, 4,096 queue). | As stated. Tune after a native performance check. |
| 8 | **Recorder process**: does the Recorder become an IPC client too (sources/02 "separate EFB, Core and Recorder processes")? | The design allows it: the Recorder would be one more principal with read-only grants. |
| 9 | **Diagnostics access**: who may hold `diagnostics=True`? | Only a principal whose `Grant` allows it. |
| 10 | **Windows pipe hardening** (DACL, reject remote clients) before any release. | Required before release. Remote clients are refused (`PIPE_REJECT_REMOTE_CLIENTS`, not yet run on Windows); an owner-only DACL is not done. |

Before the EFB switches to standalone Core, sources/08 also requires verified process startup
and shutdown, client disconnect and restart, and SimConnect recovery with an independent
client, run against MSFS under the applicable approvals.
