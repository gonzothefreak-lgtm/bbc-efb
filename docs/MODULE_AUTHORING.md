# Writing a BBC EFB module

> **Experimental. Second-release feature, off by default.** Drop-in modules are
> scheduled for the second release (sources/08). The loader is in the code base
> so the design can be reviewed and tested, but the EFB does not look in the
> modules folder until the pilot turns on **Settings > Modules > Drop-in modules
> (experimental)**. The manifest format, the Core API (`0.1-draft`) and the
> rules below can change before the feature is released.

This guide is for developers who want to build a module that the EFB discovers
by itself. A module does not need Core edits, its own SimConnect connection or
any manual registration. You write a folder with a `module.json` and a Python
package, the pilot drops it into the modules folder and turns it on, and the
EFB hands your code a Core API connection.

## Read this first: modules are trusted code

A module is ordinary Python that runs **inside the EFB process**. There is no
sandbox. Your code can do anything the EFB itself can do: read and write the
pilot's files, use the network, start threads, or crash the application.

What the EFB does control is **simulator write access through Core**. Your
module's Core connection can only send commands in the control groups the
pilot granted. Core rejects anything else. That is an access rule for
simulator commands, not a security boundary for your code.

Pilots should install modules only from authors they trust. The EFB says so
in Settings. This will change only when Core moves into its own process and
modules talk to it over a versioned local interface (sources/05).

## Folder layout

```
<data folder>/
  modules/                        <- the modules folder (created when the feature is turned on)
    example.com1_helper/          <- one folder per module; name it after the module id
      module.json
      com1_helper/                <- your package; the name must be unique
        __init__.py
        main.py
      com1_helper_vendor.py       <- optional pure-Python dependency you ship yourself
  module_data/
    example.com1_helper/          <- your own data folder (created when you ask for it)
  module-state.json               <- the EFB's record of what is on and what is granted
```

Rules the EFB checks:

- Folders whose names start with `.` or `_` are ignored, so you can park a
  module by renaming its folder to `_example.com1_helper`.
- `.zip` and `.whl` files are listed as "not supported yet; unzip it into its
  own folder".
- Your **package name must be unique**. It must not be a Python standard-library
  name (`json`, `logging`...), a name already installed or imported in the EFB
  (`bbc_core`, `PySide6`...), or a name another module uses. Name it after your
  module id, for example `example_com1_helper`.
- The package named in `entry` must be inside the module folder.

## `module.json`

```json
{
  "manifest_format": 1,
  "id": "example.com1_helper",
  "name": "COM1 helper",
  "version": "1.0.0",
  "api_version": ">=0.1,<0.2",
  "entry": "com1_helper.main:create",
  "control_groups": ["radios"],
  "description": "Shows COM1 and swaps active and standby.",
  "author": "A. Developer",
  "homepage": "https://example.com/com1-helper",
  "requires": {
    "capabilities": ["radio.com1.active_frequency", "radio.com1.swap"],
    "modules": [],
    "python_packages": []
  }
}
```

| Key | Required | Meaning |
|---|---|---|
| `manifest_format` | no (default 1) | Manifest format. This EFB reads format 1 and explains when a module needs a newer one. |
| `id` | yes | Stable identity. Lowercase letter first, then lowercase letters, digits, `_`, `.` or `-`; at most 64 characters. Grants and on/off state are stored under it. Reverse-domain style (`yourname.module`) avoids clashes. |
| `name` | yes | Display name, at most 80 characters. |
| `version` | yes | Your module's version: `MAJOR[.MINOR[.PATCH]]` with an optional `-label`. Changing it asks the pilot to grant write groups again (see Grants). |
| `api_version` | yes | Range of Core API versions you support (see below). |
| `entry` | yes | `package.module:factory`. The factory is called with a `ModuleContext`. A dotted attribute such as `pkg.main:Module.create` is allowed. |
| `control_groups` | no | Write groups you would like. Read access needs no group. |
| `description` | no | At most 2000 characters. |
| `author`, `homepage` | no | Shown in Settings. |
| `requires.capabilities` | no | Core capability IDs that must be supported by the current simulator connection. Checked against `discover()` before your code is imported. |
| `requires.modules` | no | Other module ids that must be installed, turned on and running. They are started first and stopped after you. |
| `requires.python_packages` | no | Importable names you need. Checked before your code is imported, with your module folder on the path, so packages you ship in the folder count. |

Unknown keys are reported as notes and ignored, so a newer manifest still
reads on an older EFB where possible.

### Core API versions

The EFB reports its Core API version (today `0.1-draft`). `api_version` is a
comma-separated list of clauses that must all hold: `>=`, `>`, `<=`, `<`,
`==`, `!=`. Labels such as `-draft` are ignored when comparing.

A bare version is the compatible range:

- `"0.1"` means `>=0.1,<0.2`. Before 1.0, every minor version may break calls.
- `"1.2"` means `>=1.2,<2.0`. From 1.0, a minor version only adds.

While Core is a pre-1.0 draft, every module also gets the note "Core API
0.1-draft is a pre-1.0 draft; calls may change in a later EFB release."

## The factory and `ModuleContext`

```python
# com1_helper/main.py
from bbc_core.contract import (
    Connection, ConnectionState, CoreRequestError, Gap, Observation, Quality, SignalError, SubscriptionEnded,
)

COM1 = "radio.com1.active_frequency"                # an integer frequency in Hz


class Com1Helper:
    def __init__(self, context):
        self.context = context
        self.api = context.api
        self.log = context.log
        self.active = None                          # None means "unknown": show it as such
        self.subscription = self.api.subscribe(COM1, interval_ms=1000)
        self.can_swap = "radios" in context.granted_groups

    def tick(self):
        """Called by the EFB on its UI timer. Return quickly."""
        for update in self.api.poll(limit=64):
            if isinstance(update, Observation) and update.capability_id == COM1:
                # A stale observation can still carry the last value; only show CURRENT as current.
                self.active = update.value if update.quality is Quality.CURRENT else None
            elif isinstance(update, Connection) and update.state is not ConnectionState.CONNECTED:
                self.active = None                  # never show the old session's value as current
            elif isinstance(update, SignalError) and update.subscription_id == self.subscription.id:
                self.active = None                  # the simulator could not provide it: unknown
            elif isinstance(update, SubscriptionEnded) and update.subscription_id == self.subscription.id:
                self.active = None                  # Core ended it; ask again
                self.subscription = self.api.subscribe(COM1, interval_ms=1000)
            elif isinstance(update, Gap):           # updates were lost (a SignalError is never a Gap)
                latest = self.api.latest(self.subscription.id)
                self.active = latest.value if latest and latest.quality is Quality.CURRENT else None

    def swap(self):
        if not self.can_swap:
            return "Swapping needs the Radios group. Grant it in Settings > Modules."
        try:
            result = self.api.submit_command("radio.com1.swap")
        except CoreRequestError as error:
            return str(error)
        return result.state

    def close(self):
        """Called once when the module stops. The EFB closes your Core handle afterwards."""
        self.api.unsubscribe(self.subscription.id)


def create(context):
    return Com1Helper(context)
```

The factory receives a `ModuleContext`:

| Field | Meaning |
|---|---|
| `api` | Your Core connection: a `ConsumerAPI` limited to `granted_groups`. See the Consumer API guide in sources/05 for `discover`, `subscribe`, `subscribe_snapshot`, `latest`, `poll`, `submit_command`, `command_result`, `unsubscribe`. |
| `manifest` | Your parsed manifest. |
| `module_dir` | Your module folder (`None` for built-in modules). Treat it as read-only. |
| `data_dir` / `ensure_data_dir()` | `<data folder>/module_data/<id>`. Call `ensure_data_dir()` before writing. Keep your own files here, never next to the flight records. |
| `granted_groups`, `denied_groups` | What the pilot granted, and what you asked for but did not get. |
| `host_api_version` | For example `0.1-draft`. |
| `builtin` | `True` only for modules shipped with the EFB. |
| `log` | A logger named `bbc_efb.modules.<id>`; it goes to the EFB log. |

The factory may return any object, or `None`. The EFB looks for two optional
methods on it:

- `tick()`: called on the EFB's UI timer while you are running. Poll Core here
  and return quickly. Do not block or sleep. After three errors in a row the
  EFB stops your module and shows the last error.
- `close()`: called once when you stop (pilot turns you off, the feature is
  turned off, a module you need stops, or the EFB exits). After it returns,
  the EFB closes your Core handle and removes your package from memory, so do
  not keep threads running past `close()`.

### What the API handle allows

`context.api` forwards the public `ConsumerAPI` calls. Names that start with
`_` are refused, and every call after the module stops raises
`RuntimeError("This module's Core connection is closed.")`. This guards
against mistakes; it is not a sandbox. Modules do **not** get diagnostics
access (raw candidate SimVar exploration is for trusted internal diagnostics
only, sources/08) and get no access to the logbook store. Flight records are
written only by the EFB's Logbook and Flight Recorder.

### What `poll()` returns

`poll()` returns the contract update types (`bbc_core.contract.Update`, plus
facility and simulator request results), in the order Core produced them:

| Update | Meaning for your module |
|---|---|
| `Observation` | A value of one subscription. Check `quality`; `value` can be `None`. |
| `Event` | A simulator event. `sim.pause_state` values are `PauseState` flags (`full_pause`, `active_pause`, `sim_pause`, `paused`); they still compare equal to plain integers. |
| `Snapshot` | Several SimVars from one simulator delivery (`subscribe_snapshot`), with simulator clocks in `simulation_time_s`, `absolute_time_s` and `sample_time`. A field value can be `None`. |
| `SignalError` | The simulator reported an error for one subscription (`subscription_id`, `capability_id`, `reason`). The source failed; show the value as unknown. It is **not** a `Gap`. |
| `Gap` | Updates were lost from a queue (data loss only). Read `latest()` again. |
| `SubscriptionEnded` | Core ended one of your subscriptions (for example the aircraft inputs changed). Subscribe again if you still need it. |
| `Connection` | The simulator session changed. Values of an old session are never current. |
| `CommandResult`, `InputCatalog`, `InputParameters`, facility and simulator results | Outcomes of your own requests. |

Core keeps two bounded lanes per handle: samples (`Observation`, `Event`,
`Snapshot`, `SignalError`) and status (everything else). A burst of samples
cannot push a connection change or command outcome out. Each lane reports its
own losses as a `Gap`. Core also limits each handle (for example 128
subscriptions and 16 pending commands); a request over a limit raises
`CoreRequestError` with the reason, which your module should show rather than
retry in a loop.

### Good-module rules

- Present missing, stale and simulated data honestly. An observation's value
  can be `None`; check `quality`. A `Connection` change ends the old session's
  values. A `Gap` means you missed updates; a `SignalError` means the source
  failed. Test with `isinstance(update, SignalError)`, not `Gap`.
- Treat `source.simulated` as a label you must show: simulated data is never
  presented as real.
- Keep simulator demand low: subscribe only to what you show, at the slowest
  rate that works, and unsubscribe when a view closes. Core shares identical
  demand between modules.
- Show command outcomes as Core reports them (`accepted`, `sent`,
  `confirmed`, `rejected`, `not_sent`, `uncertain`, `unverified`) and do not
  replay toggles or one-shot commands after a loss.
- Do not import Qt in the factory or `tick()` path unless the EFB offers a
  view API for modules (not yet; see proposals).
- Do not change `sys.path`, global logging configuration, or other modules.

## Control groups and grants

Reading needs no grant. Writes are grouped:

| Group | What it allows | Warning shown to the pilot |
|---|---|---|
| `radios` | Tune and swap radio frequencies. | |
| `engine_controls` | Move engine controls such as the throttle. | |
| `electrical` | Change electrical bus connections. | |
| `aircraft_inputs` | Set aircraft Input Events. | |
| `client_data` | Write Client Data areas shared with other add-ons. | |
| `simvar_writes` | Write any documented SimVar. | Broad access. |
| `client_events` | Send any documented simulator event. | Broad access. |
| `ai_objects` | Create, move and remove AI objects. | Can add or remove traffic. |
| `flight_files` | Save the flight, load flights or flight plans. | Can replace the flight; a save can overwrite a file. |
| `reserved_keys` | Capture reserved key combinations. | Can take over simulator shortcuts. |
| `simulator_actions` | Experimental simulator actions. | Native behaviour not validated. |

A module that asks for a group the EFB does not offer is shown as "Not
compatible with this EFB" with the group's name.

**Drop-in modules need an explicit grant.** The pilot turns the module on and
grants each requested group in Settings. Grants are remembered under the module
id and the module version. Grants and on/off changes apply the next time the
module starts (Core fixes a handle's groups when it is created); Settings
offers **Restart module**.

**Built-in modules** (shipped with and registered by the EFB) receive the
groups they request automatically, as the owner approved for built-ins
(sources/05, sources/08). They run whether or not the experimental switch is on,
and the pilot can still turn them off.

## Statuses and reasons

Each module shows one status and plain-English reasons:

| Status | Shown as | Typical reasons |
|---|---|---|
| `invalid` | Cannot be used | Bad manifest (missing key, bad JSON, bad entry, package not in folder), duplicate id, package-name conflict. |
| `incompatible` | Not compatible with this EFB | `api_version` range excludes this EFB; unknown control group; newer manifest format. |
| `missing_prerequisite` | Missing a prerequisite | Required module not installed, off or failed; required capability not supported by the connection; Python package not installed; modules that require each other in a loop. |
| `disabled` | Off | Valid but not turned on. |
| `ready` | On, not running | Turned on; with the "refuse" policy, also waiting for grants. |
| `loaded` | Running | |
| `failed` | Stopped after an error | Import error, factory error, Core refused the connection, repeated `tick()` errors. |

Notes that do not block loading: draft Core API, groups not granted, folder
name differs from id, unknown manifest keys, error while stopping, module
changed on disk (restart to use it), module folder removed while running.

The full traceback of any import, start-up, tick or close error goes to the
EFB log under `bbc_efb.modules.<id>`.

## How loading works

1. The pilot turns on drop-in modules. The EFB creates `<data folder>/modules`.
2. **Discovery** reads each folder's `module.json` without importing anything,
   checks it, and checks the API range, control groups, entry package location,
   package-name conflicts, duplicate ids and required modules.
3. **Loading** (for modules the pilot turned on) starts required modules first,
   then: applies the grant policy; appends the module folder to the **end** of
   `sys.path` (so it can never shadow the standard library or the EFB);
   checks `requires.python_packages`; asks Core for a handle with only the
   granted groups; checks `requires.capabilities` against `discover()`;
   imports the entry module and confirms it came from the module folder; calls
   the factory.
4. Any failure undoes everything done so far: the Core handle is closed, the
   folder is taken off `sys.path` and the package is removed from
   `sys.modules`.
5. **Unloading** stops dependants first, calls `close()`, closes the Core handle
   and removes the path entry and package. Restarting a module re-reads the
   manifest and imports fresh code.

Everything a module folder adds to the top level of the import path is visible
to the whole EFB while it runs. Keep everything inside your one uniquely named
package, plus any vendored dependencies.

## For EFB developers: wiring the host

`bbc_efb.modules` is Qt-free and SimConnect-free.

```python
from bbc_efb.modules import ModuleHost, ModuleManifest

host = ModuleHost(data_folder, core_host)   # a bbc_core.host.CoreHost; or a Core when headless
host.register_builtin(ModuleManifest.build(
    id="bbc.radio", name="Radio", version="1.0", api_version="0.1",
    entry="bbc_efb.radio:create", control_groups=("radios",)), RadioModule)
host.scan()            # no-op for drop-ins while the experimental switch is off
host.load_enabled()
# On the UI timer (after CoreBridge has pumped Core): host.tick()
# Settings: host.records(), host.set_experimental_enabled(), host.set_enabled(),
#           host.grant(), host.revoke(), host.reload(), host.ignored(), host.feature_message()
# Shutdown, before CoreHost stops: host.close()
```

The second argument is a `CoreHost`, a `Core`, or any `ApiProvider` callable
`(name, control_groups) -> ConsumerAPI`:

- Given a `CoreHost` (or a `Core`), each module handle is its own consumer from
  `register_consumer(control_groups=..., diagnostics=False)` (shared contract
  C4; `host_api_provider`). The module polls it itself. A host that offers only
  the named `api(name, ...)` / `release(name)` pair is used with a unique name
  per handle, released when the handle closes, so a restarted module never
  collides with its own earlier name.
- `bridge_api_provider(core_bridge)` uses the bridge's `host` the same way.
  It does **not** use `CoreBridge.api()` when the bridge has a host, because the
  bridge drains those consumers onto its Qt channel, which would take the
  module's updates away from its own `poll()`.
- `core_api_provider(core)` is the explicit form for a `Core` owned on the
  calling thread (tests, headless tools).

The host is meant to be driven from one thread (the UI thread); its methods take
an internal lock, but module `tick()` and `close()` run on the calling thread.
Core's lock makes those calls safe beside the Core thread's pump. Nothing in the
EFB creates a `ModuleHost` yet: the feature stays experimental and off.

The control-group table above is `DEFAULT_CONTROL_GROUPS` in
`bbc_efb/modules/loader.py`. It mirrors the groups Core accepts today
(`radios`, `engine_controls`, `electrical`, `aircraft_inputs`, `client_data`,
`simvar_writes`, `client_events`, `ai_objects`, `flight_files`,
`reserved_keys`, `simulator_actions`). If Core refuses a group, the refusal is
shown verbatim as "Core did not give the module a connection".

State lives in `<data folder>/module-state.json` (format 1, atomic write). An
unreadable file is reported in `host.settings_problem`, treated as defaults
and kept as `module-state.json.corrupt` before the next save. A file from a
newer EFB is not trusted or changed until the pilot changes a setting.

## PROPOSALS for the owner

These are defaults the code implements where sources/05, 06 and 08 leave the
decision open. Each is configurable through `ModulePolicy` or the host
arguments, so changing the decision does not need a rewrite.

1. **Off by default, behind an experimental switch.** Drop-in discovery runs
   only after the pilot turns it on; built-ins are unaffected. Turning it off
   stops every drop-in module immediately.
2. **Trust model: trusted in-process code, stated plainly.** No signing, no
   allow-list and no sandbox in this release. Settings shows `TRUST_NOTICE`
   before the switch is turned on. Revisit when Core moves to its own process
   (possible later options: signed packages, an author allow-list, or modules
   as separate processes over the local Core interface).
3. **Package format: a plain folder with `module.json`.** Zip or wheel
   packages are reported as unsupported for now. An installer that unzips
   into the modules folder could follow.
4. **Partial grants: load with the granted groups only**
   (`PartialGrant.LOAD_GRANTED`). The module can read and is told which groups
   were denied; Core rejects those commands. Alternative: `PartialGrant.REFUSE`
   waits until every requested group is granted.
5. **Grants are tied to the module version** (`regrant_on_version_change=True`).
   A new version of a module must be granted again, because new code may use
   the same group differently. On/off state is kept across versions.
6. **Built-in modules get their requested groups automatically**
   (`auto_grant_builtin=True`), per the approved owner decision. Pilots can
   still turn a built-in module off.
7. **Warnings for high-impact groups.** `simvar_writes`, `client_events`,
   `ai_objects`, `flight_files`, `reserved_keys` and `simulator_actions` carry
   a warning the grant dialog must show prominently (the owner approved public
   flight save, flight plan load and reserved Tab combinations "with a
   prominent warning").
8. **Duplicate ids and shared package names block every copy**, because the EFB
   cannot tell which one the pilot meant.
9. **Runtime failures:** three `tick()` errors in a row stop the module
   (`tick_failure_limit`). A module that blocks inside `tick()` freezes the UI;
   this cannot be prevented in-process.
10. **Module views:** the In-Flight Modules launcher (sources/03) will need a
    view hook. Suggested shape: an optional `create_widget(parent)` on the
    module object that the EFB calls on the UI thread. Not implemented.
11. **Limits:** at most 100 module folders are read (`max_modules`), manifests
    up to 64 KiB.

## Checklist before you share a module

- [ ] `module.json` loads: Settings shows your module as Off, with no reasons.
- [ ] The package name is unique and the folder is named after the id.
- [ ] `api_version` covers the EFB you tested with.
- [ ] Every control group you request is needed, and the module still works
      (read-only) when a group is not granted.
- [ ] `requires.capabilities` lists what you subscribe to.
- [ ] `tick()` returns quickly; `close()` unsubscribes and stops any threads.
- [ ] Stale, unavailable and disconnected states are shown honestly.
