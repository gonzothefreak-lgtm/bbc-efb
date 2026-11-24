# Black Box Core and SimConnect

## Fresh design baseline

On 23 November 2026 the owner directed a fresh design for SimConnect and Black Box Core. **Core** is short for **Black Box Core** throughout the project. The translation layer, provider mappings, data dictionary and consumer API belong inside Core. R3.1 remains a preserved reference for design lessons and regression scenarios. Its code, dependency choices and prior test results do not carry forward as the new baseline.

Design the internal boundaries around this flow:

1. **SimConnect adapter** owns the single local MSFS 2020 session. It turns native responses into typed observations and events and sends approved write requests.
2. **Core translation layer and broker** map provider data to documented capability IDs and units, track quality and continuity, share demand, and deliver bounded updates.
3. **Core consumer API** exposes the versioned contract to the EFB, its modules and the Flight Recorder. The Recorder owns flight-event interpretation and the saved record.

The first design checkpoint covers discovery, subscriptions, latest values, status and gaps, and write requests and results. For the offline evidence, exercise shared demand with two consumers, stale data and disconnect behaviour, and the radio command lifecycle. Select the MSFS binding and agree the native validation sequence separately.

### Resources for the fresh design

| Resource checked | What it provides |
|---|---|
| Installed MSFS 2020 SDK 0.24.6.0 under `C:\MSFS SDK` | Local documentation, `SimConnect.h`, x64 DLL, native link libraries, managed assembly and SDK EULA. The DLL checksum matches the preserved load-only evidence. The installed root has no `Samples` directory. Microsoft's online sample reference documents open/close, request data, system events and dispatch. |
| Current Source masters and former specialist handoffs | Architecture, consumer requirements and Recorder research. The current text Data Requirements Register v0.4 supersedes the candidate-field lists in the earlier handoffs. Retain the handoffs for reasoning and provenance. |
| Preserved R3.1 source, reports and original wrapper packages | Prior implementation and regression scenarios for connection, subscriptions, stale data, reconnect and shutdown. Both local wrapper artifacts match the upstream PyPI release hashes. The old failure mode is a check for candidate selection. |
| Official SDK and language options | Microsoft documents native and managed SimConnect clients. The owner selected a direct Python binding to the Microsoft x64 SDK. The managed assembly and a .NET SDK are available locally as alternatives. |

Microsoft documents that SimConnect clients are not thread-safe. The fresh adapter design should give open, request, dispatch and close one serial owner. For the binding comparison check loader provenance, numeric and string data, required event packet types, session cleanup, licence and local packaging. Start with Microsoft’s SDK contracts. [SimConnect SDK](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/SimConnect_SDK.htm) · [Official samples](https://docs.flightsimulator.com/html/Samples_And_Tutorials/Samples/VisualStudio/SimConnect_Samples.htm) · [Managed clients](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/Programming_SimConnect_Clients_using_Managed_Code.htm)

Upstream `simconnect-H` main now contains the loader imports that are missing from the preserved v0.7.2 release. The published package checked is still v0.7.2. Treat main as a source candidate requiring its own evaluation. The old specialist handoff mentions `pysimconnect 0.3.0`. The upstream package index lists 0.2.6 as the latest release examined. [simconnect-H release](https://pypi.org/project/simconnect-H/) · [Upstream loader source](https://github.com/hjznb887/simconnect-H/blob/main/simconnect_native/client.py) · [pysimconnect releases](https://pypi.org/project/pysimconnect/#history)

Supporting originals remain available in the previous workspace. [Core handoff](<../../Workspace - BBC EFB and Black Box Core/project sources/02_ALL_OTHER_AVAILABLE_FILES/BBC_Black_Box_Core_Dev_C01_Handoff_to_Main_2026-11-23.docx>) · [Recorder telemetry handoff](<../../Workspace - BBC EFB and Black Box Core/project sources/02_ALL_OTHER_AVAILABLE_FILES/BBC_EFB_SimConnect_Logbook_Telemetry_Research_Handoff_2026-11-23_v0.1.docx>)

## Preserved requirements and evidence

Core is the shared generic information gateway. One demand-driven connection supplies typed SimVar and supported events to active consumers, with source identity, receipt time, connection generation, bounded delivery, quality and explicit continuity gaps. The Recorder interprets that evidence and owns flight-event rules.

The initial target is local MSFS 2020. MSFS 2024 and native simulator validation require separate approval. The supplied simconnect-H v0.7.2 distribution was evaluated for R3.1. The fresh design uses a direct Python binding to the Microsoft x64 SDK. FSUIPC7 integration is deferred beyond C0.1. See [Aircraft and extension boundaries](06_AIRCRAFT_AND_EXTENSIONS.md).

Use the preserved Integration Development Record v0.8 for Q1-Q15 (Q11 withdrawn) and Data Requirements Register v0.4 for the full candidate catalog, including SC-001-016. No live mapping, cadence or aircraft compatibility has been validated. Aircraft identification, position, on-ground state, clocks, pause and touchdown variables are research inputs. Last-touchdown values may be stale from another flight. Simulated receipt timestamps do not establish native sample age.

R3.1 uses separate numeric/string paths, staged demand, generation gating, bounded subscriber queues and per-stream health. Keep the adapter's subsecond limits, event-payload limitations and uncertain native cleanup explicit until validated. Do not share a simulator handle across unrelated clients. Validate approved variables, units, event availability and cadence before relying on them for recording.

The initial source review found that R3.1 already has a provider boundary and broker methods for shared subscriptions, latest values, connection notices and bounded polling. The typed `ConsumerAPI` protocol currently declares only consumer registration. Complete the consumer-facing contract during the translation review and decide how consumers identify continuity changes. The adapter rejects old callbacks internally. Broker messages carry connection notices and gap counts, with no explicit session identifier in the public observation type. Use the candidate README, code and preserved reports when interpreting the older statements about implementation and test status in `docs/INTERFACES.md`. [Recovered candidate](<../reference/01_CODEX_WORK_EVIDENCE/development reviews/BBC_EFB_Black_Box_Core_R3.1_Source_Candidate_2026-11-23/README.md>)

The owner wants the consumer interface to support new EFB modules and future applications without changing Core for each consumer. Keep the versioned contract independent of the app, Qt and transport, with supported capabilities discoverable and unsupported requests explicit. User-installable modules are now a deployment requirement. The module package format, loading model and cross-process access still need design. The existing simulated suite provides Core and adapter evidence. The integration checkpoint covers consumers through the agreed translation interface in the EFB. The choice of a permanent pilot-facing readout remains open.

## Core consumer API and data dictionary draft

The **Core consumer API** is the interface a module calls. Its **API contract** defines requests, responses, units, errors, freshness, connection changes and compatibility. The **data dictionary** lists what a developer can request, and the **consumer guide** shows how to use the API. The current implementation and the first EFB build use in-process calls. The standalone Core phase after the first-build review will carry the consumer contract over a versioned local interface. Start from the [SimConnect Data Requirements Register](../reference/00_CURRENT_PROJECT_SOURCE_MASTERS/BBC_EFB_SimConnect_Data_Requirements_Register.txt) for the wider mapping research. The identifiers and mappings below are proposals.

### Proposed API contract

| Operation | What the module can rely on |
|---|---|
| Discover capabilities | Get the contract version and discover Core data, event and command IDs, types, units, and provider support. A known capability has a separate runtime availability state. |
| Register a consumer | Get a handle for this module instance, an initial connection snapshot and an independent bounded update queue. Registration itself creates no simulator demand. |
| Subscribe to a capability | Receive a subscription handle or a clear reason for refusal. A request can specify a preferred cadence where applicable. Core shares matching provider demand and reports its accepted cadence policy. Actual timing remains an observation. |
| Get the latest value | Read the latest observation for an active subscription or an explicit unavailable result. Reading it creates no extra simulator request. Quality and timestamps travel with the value. |
| Read updates | Read a bounded batch of updates without blocking. Updates can contain observations, events, connection transitions and gap notices. Reading one consumer's updates leaves other consumers' queues intact. |
| Submit a command | Request a documented write or simulator action. Receive a command ID and updates for the outcome, including a reason for rejection or uncertainty. |
| Release a subscription or consumer | Drop that consumer's demand. Closing a consumer releases all its subscriptions. The provider request can be retired once the last interested consumer leaves. |

An observation carries a capability ID, value and unit, source, receipt time, session identifier, and quality. Keep simulator sample time optional and separate from receipt time. Use `current`, `stale` or `unavailable` for quality and distinguish simulated from live sources independently. A stale value retains its original receipt time. An unavailable result has a reason and carries no value. These rules preserve valid values such as zero, false and empty text.

On disconnect, report the transition and invalidate the previous session's current values. Keep active subscription intent across reconnect. Give the new session a new identifier and wait for fresh observations before presenting values as current. If a consumer queue overflows, report an explicit gap to that consumer. A module can use the latest value to refresh its display after a gap. The Recorder decides how that gap affects a flight record.

For a consumer, a capability ID has stable meaning, type and units within the same compatible version. New capabilities can be added. Changes to the meaning or shape of existing data require a compatibility decision. Keep the contract usable independently of Qt, the chosen SimConnect wrapper and future IPC. Future non-SimConnect providers should be able to map compatible capabilities without changing existing module calls.

The owner approved using trusted internal diagnostics to explore new read-only SimConnect variables through Core. Normal modules use documented capability IDs. Internal diagnostics can request a candidate SimVar by its SDK name, unit and type through the shared provider path. A candidate can be documented as research, then added as a supported capability once its meaning, mapping and behaviour are validated. This design decision leaves implementation and runtime testing for separate approval.

**Example for review.** An In-Flight consumer and a second diagnostic consumer subscribe to `aircraft.ground_speed`. Core asks its provider once and reports each consumer's updates independently. Closing diagnostics leaves In-Flight subscribed. A disconnect makes the readout unavailable. After reconnect, the readout resumes when new-session data arrives. A simulated source follows the same contract and can be identified from the returned source metadata.

### Full read and write design target

The owner confirmed that the first Core milestone includes full SimConnect read and write capability. It must cover reading values, subscribing to events, setting writable data and sending supported simulator commands or events. Core owns translation to the provider and routes requests through the same centrally managed connection. Compare binding candidates against both read and write coverage. Inventory the SDK operation families and record their support, prerequisites and evidence before calling coverage complete.

The proposed command interface lists capabilities in the developer reference with argument types, units, valid ranges, provider and aircraft support, access requirements and confirmation method. Separate request acceptance, dispatch and confirmed outcome. For example, a COM1 standby command reports when it is sent and confirms the change from a fresh frequency observation when readback is supported. Report uncertainty if a submitted command loses its connection before confirmation. Treat automatic replay of unconfirmed commands as off by default. Detailed write permissions and validation remain open.

The owner approved grouping related write capabilities for built-in modules. The EFB registers a built-in module with Core and supplies its access at registration, such as tuning and swapping for the radio module. A module calls the named command and handles the result. Routine commands require no manual permission setup or prompts. Core checks whether the command is supported and its arguments are valid. Keep the same command interface for future user-built modules and apps. Decide how the host establishes their access when user-module loading or external app access is designed.

#### Initial SimConnect coverage map

The [SimConnect function dictionary](10_SIMCONNECT_FUNCTION_DICTIONARY.md) inventories all 91 functions in the installed SDK header and DLL. It identifies the underlying calls the direct adapter currently uses and records deprecated or undocumented exports.

#### Deployment requirement

The owner requires full operational SimConnect coverage and straightforward use of compatible user-built modules on deployment. Account for every function in the 91-entry inventory. Implement and validate the supported function families. Provide a supported API operation for module use where applicable. Core manages shared lifecycle and dispatch so all modules use one session. Verify deprecated and undocumented entries against the available SDK evidence and record explicit availability and reasons where a function cannot be used.

Add a general, typed request path for SDK reads, writes and events. A module should be able to use a supported operation even when Core has no hand-written alias for its particular SimVar or command. The published dictionary and runtime discovery should describe accepted IDs or SDK names, argument types and units, provider and aircraft prerequisites, and confirmation behaviour. Keep named shortcuts such as COM1 tuning where they help. The module authoring guide should document the package and entry point a developer needs to use. The EFB discovers compatible packages in its module location and supplies the Core API when each module loads. Package format, module trust and compatibility handling need a concrete host design. [Module workflow](06_AIRCRAFT_AND_EXTENSIONS.md)

| SDK operation family | Coverage to account for in the binding review |
|---|---|
| Session and diagnostics | Open, dispatch, simulator exceptions, close and request correlation. |
| Simulator object data | Typed one-shot and subscribed reads, indexed values and writes to supported writable fields. |
| System notifications and simulator commands | Subscribe and unsubscribe to system notifications. Map and transmit supported simulator commands, including events with multiple arguments where available. |
| Aircraft input events | Discover the loaded aircraft's input events and parameter types. Read, subscribe and set supported inputs. |
| Client Data | Separate named-area read and write operations with provider-specific schemas and availability. |
| Other SDK families | Record the intended scope for facilities, jetways, AI objects, flight files, camera, missions, controllers and header-only or legacy calls before claiming coverage. |

The SDK documents `COM_STBY_RADIO_SET_HZ` for setting COM1 standby and `COM_STBY_RADIO_SWAP` for swapping. Active and standby frequencies and spacing mode have separate indexed readback variables. Verify channel representation, aircraft support and command outcome during separately authorised native validation. These mappings are documentation evidence. [SimConnect API reference](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/SimConnect_API_Reference.htm) · [Radio events](https://docs.flightsimulator.com/html/Programming_Tools/Event_IDs/Aircraft_Radio_Navigation_Events.htm) · [Radio variables](https://docs.flightsimulator.com/html/Programming_Tools/SimVars/Aircraft_SimVars/Aircraft_RadioNavigation_Variables.htm)

#### Binding comparison

| Option | Finding |
|---|---|
| Direct Python binding to the Microsoft x64 SDK | Owner-approved design direction. Keep DLL calls and packet decoding inside the adapter, using standard-library `ctypes` and the installed SDK header. The header declares object writes, multi-argument commands, aircraft Input Events and Client Data. We own validation of each function signature, packet layout and error path, plus local DLL loading and deployment. Header declarations are not runtime evidence. |
| Third-party Python wrapper | The preserved `simconnect-H` v0.7.2 wheel binds object writes and basic client events. Static inspection found no calls in its Python source for multi-argument event transmission, aircraft Input Events or Client Data operations. The upstream loader import correction does not establish coverage for those families. |
| Microsoft managed wrapper or a compiled adapter | Viable alternatives if direct Python binding proves unsuitable. A managed route needs a Python integration and deployment decision. Microsoft documents a fixed-width string path and no variable-length strings in the managed wrapper. A compiled native adapter would add a separate build and deployment step. |

The owner approved the direct binding as a design direction on 23 November 2026. The proposed first implementation step is the consumer contract, translation and a starter dictionary, exercised with simulated reads and radio commands. Use header-derived packet layout checks for the first adapter operations. This initial slice is one step toward full read/write coverage. It is followed by separately approved DLL loading and live validation. No binding was loaded or tested in this comparison. [Microsoft API reference](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/SimConnect_API_Reference.htm) · [Python ctypes](https://docs.python.org/3.12/library/ctypes.html) · [Published simconnect-H](https://pypi.org/project/simconnect-H/) · [Microsoft managed wrapper](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/Programming_SimConnect_Clients_using_Managed_Code.htm)

### Starter data dictionary

| Proposed module-facing ID | Returned data | MSFS 2020 mapping candidate | Usage |
|---|---|---|---|
| `aircraft.title` | Text | `TITLE` | SDK lists the aircraft configuration title, up to 128 characters. It can differ by livery. The current R3.1 string path needs review before use. |
| `aircraft.altitude_msl` | Proposed number in feet above mean sea level | `PLANE ALTITUDE`, feet | SDK lists aircraft altitude in feet and defines above-ground altitude separately. Verify the intended reference datum before promising it in the public ID. |
| `aircraft.on_ground` | Boolean | `SIM ON GROUND`, bool | SDK Boolean encoding uses zero for false and nonzero for true. The translation should normalise the value. The Recorder owns interpretation of transitions. |
| `aircraft.latitude` and `aircraft.longitude` | Proposed signed decimal degrees | `PLANE LATITUDE` and `PLANE LONGITUDE` | SDK lists radians and documents requests in degrees for the same unit family. Position alone provides no airport identifier. |
| `aircraft.ground_speed` | Number in knots | `GROUND VELOCITY` | Speed relative to the earth's surface. |
| `sim.pause_state` | Proposed event with pause flags | `Pause_EX1` | SDK documents detailed pause state. The R3.1 adapter does not currently accept this event. |
| No module-facing ID proposed | Last-touchdown observation in feet per second | `PLANE TOUCHDOWN NORMAL VELOCITY` | Research only. Validate sign, freshness and association with the current landing before proposing a landing metric. |

Each entry should record its meaning, type and unit, whether it is a value or an event, provider mapping, supported options and known limitations, and validation status. Validation status belongs to the catalog entry. Freshness and availability belong to each returned observation. A request can be supported while its value is temporarily stale or unavailable. Sampling limits and defaults should be listed only once agreed and verified.

**How a module uses it.** Register as a consumer, look up the capability, request the data it needs, and read through Core. Use a latest value for a readout and the message stream when individual events or continuity matter. Handle stale and unavailable values, connection changes, and gaps explicitly. Release the subscription when it is no longer needed. Core shares matching demand and the adapter handles the provider-specific request. The [R3.1 interface draft](<../reference/01_CODEX_WORK_EVIDENCE/development reviews/BBC_EFB_Black_Box_Core_R3.1_Source_Candidate_2026-11-23/docs/INTERFACES.md>) shows the current low-level call pattern. The module-facing names and lookup API remain to be agreed.

A future machine-readable catalog could drive both capability discovery and the developer reference, keeping IDs, units and support information aligned. A new module using an existing capability should need only the documented consumer interface. A new provider mapping or capability may still need adapter work and validation.

### SDK and adapter cross-check

- The installed MSFS 2020 SDK 0.24.6.0 documentation and header were checked against Microsoft's published definitions. Generic observations target `SIMCONNECT_OBJECT_ID_USER`. The SDK defines per-frame and per-second request periods. The R3.1 adapter currently uses a one-second native period and rejects requests below 1,000 ms. That is an adapter constraint. Actual delivery cadence remains unverified. [Variable definitions](https://docs.flightsimulator.com/html/Programming_Tools/SimVars/Aircraft_SimVars/Aircraft_Misc_Variables.htm) · [Boolean variables](https://docs.flightsimulator.com/html/Programming_Tools/SimVars/Miscellaneous_Variables.htm) · [Data definitions](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_AddToDataDefinition.htm) · [Request periods and Boolean encoding](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_RequestDataOnSimObject.htm)
- The preserved `simconnect-H` v0.7.2 wheel matches its recorded hash. Static inspection shows that R3.1 requests periodic strings through the wrapper's `STRINGV` path. Microsoft documents variable-length strings for one-shot requests only. A fixed-width or one-shot approach is a candidate for later validation. Microsoft documents filename-bearing `AircraftLoaded` and `FlightLoaded` events. R3.1's wrapper path dispatches ordinary event packets. List these as SDK-documented but unavailable in the current adapter. [System events](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_SubscribeToSystemEvent.htm) · [Request data](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_RequestDataOnSimObject.htm)
- MSFS 2024 has separate SimConnect and SimVar documentation. FSUIPC7 has its own offset interface. Treat each as a separate provider mapping with its own validation status. For aircraft-specific data, the iniBuilds/Synaptic A220 has its own published variable and event list. The official PMDG documents reviewed did not establish the proposed 737 Client Data mapping. Aircraft integrations remain parked. [MSFS 2024 SimConnect](https://docs.flightsimulator.com/msfs2024/retail/programming-apis/simconnect/simconnect-sdk/) · [FSUIPC7](https://fsuipc.com/fsuipc7/) · [A220 integration references](https://forum.inibuilds.com/topic/37280-lvars-events-list/) · [PMDG documentation](https://pmdg.com/documentation/)

The draft distinguishes what the SDK documents, what the current adapter can request, and what has been observed live. This review was static. No SDK calls, simulator sessions or tests were run.

## First Core implementation step

The owner authorised coding and subsequently approved offline verification. The fresh `bbc_core` package implements the in-process consumer interface, starter catalog, translation, shared subscriptions, a bounded update queue and command outcomes. The EFB host registers consumers and assigns control groups. The host drives provider connection, updates and command dispatch explicitly. Provider I/O has a serial owner and runs outside the consumer state lock.

The implemented starter catalog includes aircraft title, ground speed, air/ground state, position, a pause event, and COM1 frequency readback, standby setting and swapping. The altitude datum and live radio channel representation still need validation. The simulated provider labels every result, and session loss makes outstanding writes uncertain without replay. An unconfirmed write blocks further writes for that session while healthy reads can continue. The owner agreed to keep the simulated provider in development source for offline tests and exclude it from the packaged pilot-facing app. When MSFS is unavailable, the app should show that state and the manual Logbook remains in scope. The starter SDK packet and function declarations in `simconnect_abi.py` passed the offline comparison against the header.

For an internal module, the host supplies a `ConsumerAPI` handle. The module calls `discover()`, subscribes by capability ID, then calls `latest()` or `poll()`. A built-in radio module with the `radios` group calls `submit_command("radio.com1.set_standby", frequency_hz)` or `submit_command("radio.com1.swap")` and follows the command ID to its result. When the module is done, it releases the subscription or closes the handle. The offline suite passed 14 cases. No application build, DLL loading or simulator connection occurred. [Test report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_first_offline_2026-11-24/REPORT.md)

## First native adapter step

The owner approved coding and fake-SDK tests for the initial direct adapter. The implementation forces a local-only connection, waits for an OPEN packet, and keeps SDK calls on one serial owner. It maps shared typed demand and ordinary pause notifications to SDK requests, decodes numeric and fixed-width strings, and reports correlated errors and uncertain cleanup. Core has no automatic reconnect or command replay. The DLL loader takes an explicit absolute path and requires an explicit host call to load it.

COM1 standby tuning and swapping request a fresh baseline, transmit one event and check subsequent one-shot readback. A missing baseline rejects the command before transmission. A rejected event and an unconfirmed readback produce distinct outcomes. Readback failure can block further writes while healthy telemetry continues. The starter adapter was the first slice. The expanded write step below adds typed object writes and extended events.

The guarded offline run passed 28 cases, including 14 new fake-SDK adapter cases. The developer-facing consumer contract was preserved. The separately approved load-only preflight then resolved 13/13 declared exports with the adapter's own loader and made no SimConnect function calls. Runtime packet behaviour, channel spacing, aircraft support, native teardown and frame-time impact remain unverified. [Adapter test report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_native_adapter_offline_2026-11-24/REPORT.md) · [DLL preflight report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_sdk_dll_preflight_2026-11-24/REPORT.md)

## Consumer API developer guide

This is the implemented internal API, currently labelled `0.1-draft`. Module code uses the `ConsumerAPI` handle supplied by the EFB host. The host owns provider setup, connection and pumping. It assigns control groups automatically when registering built-in modules.

| Call on the supplied handle | What to expect |
|---|---|
| `discover()` and `connection()` | Find capability IDs, data types and units, ordered command parameters, provider support and connection state. Command discovery reflects the host-assigned groups. Provider support is separate from a value being available now. |
| `subscribe(id, interval_ms=…, max_age_ms=…)` | Request a value or event. Returns a subscription and the accepted interval for values. Use the returned subscription ID with `latest()` or `unsubscribe()`. Event subscriptions take no interval or age arguments. |
| `latest(subscription_id)` | Returns a typed observation with source, session, receipt time, unit and quality. The value is optional. `stale` can carry the last value and its original timestamp. `unavailable` carries a reason. |
| `poll(limit=128)` | Read your bounded update queue. Handle connection changes and gaps explicitly. Use observation and event IDs to route updates to your module. |
| `submit_command(id, argument)` | Send no argument, one scalar, or an ordered tuple for multiple arguments as listed by discovery. Returns a command ID and initial state. |
| `command_result(command_id)` | Read the latest result while Core retains it. Command updates also appear in `poll()`. |
| `unsubscribe(subscription_id)` and `close()` | Release one subscription or end the consumer. Shared requests remain while another consumer still needs them. |

Command states distinguish `accepted` (queued), `sent` (submitted to the provider), `confirmed` (matched state readback), `rejected`, `not_sent`, `uncertain` (expected confirmation could not be established), and `unverified` (sent with no configured readback). An unverified dispatch does not establish a simulator state change. Modules should display the outcome and avoid replaying a one-shot or toggle command after loss.

### Current command entries

| Capability ID | Argument | Control group | Confirmation |
|---|---|---|---|
| `radio.com1.set_standby` | Integer Hz | `radios` | COM1 standby frequency readback |
| `radio.com1.swap` | No argument | `radios` | Active and standby readback against a fresh baseline |
| `aircraft.engine1.set_throttle` | Finite number, 0–100 percent | `engine_controls` | Engine 1 throttle SimVar readback |
| `aircraft.electrical.toggle_bus_connection` | Tuple of source and target bus indices, each an unsigned 32-bit integer | `electrical` | Dispatch only. Aircraft-specific state readback is pending. |

The expanded adapter uses `SimConnect_SetDataOnSimObject` for a typed writable value and `SimConnect_TransmitClientEvent_EX1` for extended events with up to five SDK parameters. It exposes named capabilities through the existing API. The table shows the starter catalog rather than every SDK command. Four new fake-SDK cases brought the guarded suite to 32 passing cases. Live channel representation, aircraft-specific support and write outcomes are unverified. [Expanded write test report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_expanded_writes_offline_2026-11-24/REPORT.md) · [Object data writes](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_SetDataOnSimObject.htm) · [Extended events](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_TransmitClientEvent_EX1.htm) · [Engine variables](https://docs.flightsimulator.com/html/Programming_Tools/SimVars/Aircraft_SimVars/Aircraft_Engine_Variables.htm) · [Electrical events](https://docs.flightsimulator.com/html/Programming_Tools/Event_IDs/Aircraft_Electrical_Events.htm)

### Client Data

The host declares each named area with `Core.register_client_data(ClientDataArea(name, size, ...))`. Declaration is local to Core. The first subscription maps the name and creates a simulator area only if the host explicitly set `create=True`. The host can permit writes with `writable=True` and restrict an area it creates to its creator with `creator_only=True`, the default. Use the layout published by the area owner. SimConnect can pad an out-of-bounds read with zeros, so a successful read alone cannot validate the real area size. [CreateClientData](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_CreateClientData.htm) · [RequestClientData](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_RequestClientData.htm)

| Consumer API call | Behaviour |
|---|---|
| `client_data_areas()` | Lists the host's declarations, including when offline. A declaration does not establish that the area exists. |
| `subscribe_client_data(area.region(offset, size), max_age_ms=…)` | Shares one stream per region, with an initial read and updates when SimConnect reports a set. Read exact bytes through `latest()` and `poll()`. |
| `request_client_data(subscription_id)` | Requests a one-shot refresh for an active subscription. The result arrives through the same subscription. |
| `submit_client_data(region, payload)` | Queues an exact-size byte payload. The host must assign the `client_data` group, allow writes for the area and have an active successful one-shot read in the current session. Check `command_result()` for the readback outcome. |

The current path supports a contiguous byte region with one-shot and on-set reads. Core uses tagged writes to target that region and compares the returned bytes exactly. Advanced cadence and schema options, already-existing owned areas on reconnect and compatibility with third-party layouts need further work before deployment. [SetClientData](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_SetClientData.htm) · [Client Data offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_client_data_offline_2026-11-24/REPORT.md)

### Simulator session state and notifications

Use `subscribe()` with one of the state IDs below, then read `latest()` or `poll()`. Core issues a shared initial request for active subscribers when connected. Call `request_system_state(subscription_id)` for another reading. It coalesces overlapping requests for the same state. Set `max_age_ms` if the state needs a freshness limit. State snapshots have no periodic interval. A timeout, rejected request or invalid response reports an unavailable value. [Microsoft state requests](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/General/SimConnect_RequestSystemState.htm)

| State capability | Value |
|---|---|
| `sim.running` | Boolean simulation state |
| `sim.dialog_mode` | Boolean dialog state |
| `sim.loaded_aircraft_file` | Last loaded aircraft file path |
| `sim.loaded_flight_file` | Last loaded flight file path |
| `sim.active_flight_plan_file` | Active flight plan path. An empty string means no active plan. |

Read notifications through `poll()`. Call `set_event_enabled(subscription_id, False)` to pause delivery to that consumer and use `True` to resume. Core suspends the SDK event only when every subscriber disables it and Core has no internal need for it. Call `unsubscribe()` to release the subscription. Aircraft Input Event discovery shares the aircraft and flight load subscriptions. [Microsoft system events](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_SubscribeToSystemEvent.htm) · [Microsoft event delivery state](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_SetSystemEventState.htm)

| Event capabilities | Payload |
|---|---|
| `sim.running_changed`, `sim.pause_changed` | Boolean running and basic pause states |
| `sim.pause_state` | SDK pause flags, including Active Pause |
| `sim.aircraft_loaded`, `sim.flight_loaded`, `sim.flight_saved`, `sim.flight_plan_activated` | File path text |
| `sim.frame`, `sim.pause_frame` | `FrameSample` with frames per second and simulation rate. These subscriptions can generate updates at the visual frame rate. |
| `sim.object_added`, `sim.object_removed` | `SimObjectChange` with object ID and SDK object type |
| `sim.mission_action` | `MissionAction` with instance UUID, completion flag and payload text |
| `sim.started`, `sim.stopped`, `sim.paused`, `sim.unpaused`, `sim.flight_plan_deactivated`, `sim.aircraft_crashed`, `sim.crash_reset`, `sim.position_changed`, `sim.sound_changed`, `sim.view_changed`, `sim.weather_mode_changed`, `sim.tick_1sec`, `sim.tick_4sec`, `sim.tick_6hz` | SDK integer event data |

The running, pause and load signals are inputs to future Recorder rules. The SDK can issue multiple start and stop events while loading or resetting a flight, so the Recorder still needs approved flight boundaries. Native event delivery, filename encoding, payloads and cadence remain pending. [Session offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_session_state_offline_2026-11-24/REPORT.md) · [Microsoft system events](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_SubscribeToSystemEvent.htm)

### General SimVars and client events

Use the MSFS 2020 SDK tables to pick a variable, its unit and index, or an event and its ordered parameters. These calls use the host's Core handle and shared user-aircraft session. They accept SDK names beyond the starter catalog. Core validates the argument shapes and encodings. The SDK and loaded aircraft determine whether a specific variable or event is available and whether a variable is writable. [SimVar index](https://docs.flightsimulator.com/html/Programming_Tools/SimVars/Simulation_Variables.htm) · [Event IDs](https://docs.flightsimulator.com/html/Programming_Tools/Event_IDs/Event_IDs.htm)

| Consumer call | Behaviour |
|---|---|
| `subscribe_sdk_simvar(variable, interval_ms=…, max_age_ms=…)` | Requests typed simulator data. Read it with `latest(subscription.id)` or `poll()`. Identical variable names, units, types, indices and formats share a native subscription. |
| `submit_simvar(variable, value)` | Queues a write to a writable SimVar. Requires the host-assigned `simvar_writes` group. Core reports confirmation when it observes the expected value after sending the write. Values the simulator clamps or ignores can produce `uncertain`. |
| `submit_client_event(event, arguments=())` | Queues a documented simulator event. Requires the host-assigned `client_events` group. Use zero to five ordered arguments. Each is a Boolean or an integer whose value fits a signed or unsigned 32-bit word. Generic events report `unverified` after dispatch unless the SDK reports a rejection. Use existing named commands for their configured readback. |

```python
from bbc_core import SimVar, ValueType

# `api` is the ConsumerAPI handle supplied by the host.
panel_light = SimVar("LIGHT POTENTIOMETER", "percent", ValueType.NUMBER, index=2)
subscription = api.subscribe_sdk_simvar(panel_light, interval_ms=1000)
observation = api.latest(subscription.id)  # May be unavailable while waiting for a sample.

light_write = api.submit_simvar(panel_light, 60.0)
landing_lights = api.submit_client_event("LANDING_LIGHTS_ON")
result = api.command_result(light_write.id)  # Poll or query later for the final result.
```

Default SDK formats are INT32 for Boolean and integer, FLOAT64 for number, and STRING256 for text. Pass `format=SimVarFormat.INT64`, `FLOAT32` or an appropriate fixed width text format when required. Text uses `unit=None`. For a documented local `L:` variable use FLOAT64 with no index. For standard SimVars, supply a separate 1-based index where the SDK calls for one. Use event names without the `K:` prefix. Core uses the standard transmit call for zero or one argument and the extended call for two to five. Read-only consumers can subscribe without either write group. [SDK data definitions](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_AddToDataDefinition.htm) · [Extended event transmission](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_TransmitClientEvent_EX1.htm)

This checkpoint covers scalar and fixed-width text data and simulator event transmission for the user-aircraft session. Structured SimVar data and AI object targeting are tracked separately. Live testing still needs to verify aircraft-specific support, units, write readback, and string handling. [Offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_sdk_simvar_and_client_events_offline_2026-11-24/REPORT.md)

### Airport facilities and jetways

Use `request_airport("YSSY")` for typed airport details or `request_nearby_airports()` for a snapshot of the airports in SimConnect's local scenery cache. These one-shot calls use the shared Core session and require a connected provider that supports airport queries. An optional `region` can refine an airport identifier. Identical outstanding queries share one provider request. [Microsoft airport details](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_RequestFacilityData.htm) · [Microsoft facility cache query](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_RequestFacilitiesList_EX1.htm)

```python
from bbc_core import FacilityState

request = api.request_airport("YSSY")  # api is the host-supplied ConsumerAPI.

# Check on a later UI update after the host pumps Core.
result = api.facility_result(request.id)
if result is not None and result.state is FacilityState.READY:
    airport = result.data
    runway_lengths_m = [runway.length_m for runway in airport.runways]
    parking = airport.parking
    jetways = airport.jetways
```

Terminal results also arrive through `poll()`. A result includes its session, source and receipt time. `pending` means Core is still waiting for the full reply. `ready` can contain an empty nearby list or airport child list. `no_data` means the simulator ended an airport details request without a record. `unavailable` carries the reason when a request fails, times out or is interrupted. Core checks that every result includes all listed records before publishing it.

Airport and runway coordinates are in degrees. Elevation, runway dimensions, parking radius and parking offsets are in metres. Runway numbers, designators, parking labels and static jetway gate assignments use SDK values. The nearby airport list covers the simulator's local cache, whose radius varies. [Airport field definitions](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_AddToFacilityDefinition.htm) · [Offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_airport_facilities_offline_2026-11-24/REPORT.md)

### Navaid and jetway state queries

Use `request_navaid(kind, icao, region=…)` for VOR, NDB and waypoint records. Choose `NavaidKind.VOR`, `NavaidKind.NDB` or `NavaidKind.WAYPOINT` and pass the simulator region when known. Core sends the SDK type discriminator and returns all complete matching records it receives. VOR and NDB results carry the requested identifier, the station name, position, raw frequency and range. Their requested record schema has no returned region. Waypoint results include the simulator's identifier and region, position, type, magnetic variation and terminal flag. Frequencies stay in the SDK's raw numeric format pending native verification. [Facility type lookup](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_RequestFacilityData_EX1.htm) · [Facility fields](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_AddToFacilityDefinition.htm)

Use `request_jetways(icao, parking_indices=…)` to read the simulator's current state for spawned jetways. Pass a tuple of SDK parking indices or omit it to send an unfiltered request. Core returns multipart snapshots as one result. Each record includes world position, attitude, animation status, door and object IDs, and `fully_attached` for the SDK's fully attached status. The world altitude uses the SDK's LATLONALT unit of feet. [Jetway request](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_RequestJetwayData.htm) · [Jetway fields](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Structures_And_Enumerations/SIMCONNECT_JETWAY_DATA.htm) · [LATLONALT units](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Structures_And_Enumerations/SIMCONNECT_DATA_LATLONALT.htm)

```python
from bbc_core import FacilityState, NavaidKind

navaid = api.request_navaid(NavaidKind.VOR, "SY")
jetways = api.request_jetways("YSSY")

# Check later after the host pumps Core.
result = api.facility_result(jetways.id)
if result is not None and result.state is FacilityState.READY:
    attached_gates = [item.parking_index for item in result.data if item.fully_attached]
```

Terminal results also arrive through `poll()`. An explicit empty jetway list is `ready` with an empty tuple. A navaid request that ends without a record reports `no_data`. A timeout or missing page reports `unavailable`. Jetway requests run in sequence because the SDK has no caller-assigned request ID for this call. After a timeout or an ambiguous response, Core requires a fresh SimConnect session for further jetway queries so a late packet cannot be attributed to the next request. Other facility queries can continue in the current session. Native reply behaviour and scenery coverage remain to be verified. [Offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_navaids_and_jetways_offline_2026-11-24/REPORT.md)

### Facility lists and cache updates

Use `request_worldwide_airports()` for the simulator's airport catalog or `request_nearby_airports()` for the active scenery cache. Use `request_nearby_navaids(NavaidKind.VOR)`, `NavaidKind.NDB` or `NavaidKind.WAYPOINT` for the nearby navaid lists. Each returns an ID for `facility_result()`. Core publishes a list after all pages are present. List records carry identifier, region and position. Navaid records also carry magnetic variation, frequency in hertz where provided by the list format, and available VOR signal details. [Worldwide list](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_RequestFacilitesList.htm) · [Nearby lists](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_RequestFacilitiesList_EX1.htm)

```python
from bbc_core import FacilityCacheUpdate, FacilityKind

watch = api.subscribe_facility_cache(FacilityKind.AIRPORT)

# After the host pumps Core, handle the initial snapshot and later changes.
for update in api.poll():
    if isinstance(update, FacilityCacheUpdate) and update.subscription_id == watch.id:
        print(update.change, len(update.entries))

# Read the current snapshot when redrawing or recovering from a gap.
snapshot = api.facility_cache_snapshot(watch.id)

# Release the subscription when the view or module closes.
api.unsubscribe(watch.id)
```

Cache subscriptions are shared by facility kind. The first complete in-range batch is the initial snapshot. Later batches report additions and removals. Removal entries carry identity and position. Core uses identifier, region and coordinates to track the local set. A consumer joining an active watch receives the current snapshot. `facility_cache_snapshot()` returns `None` while awaiting the first reply, or `unavailable` when the cache cannot be trusted. Core ends the SDK subscription after the last consumer leaves and reacquires watches with active demand on reconnect. Native validation still needs to confirm the initial batch and removal formats and the cost of a full airport list. [Cache notifications](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_SubscribeToFacilities_EX1.htm) · [Cache unsubscription](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_UnsubscribeToFacilities_EX1.htm) · [Offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_facility_lists_and_cache_offline_2026-11-24/REPORT.md)

### Airport detail filters

Pass a tuple of `AirportFilter` values to `request_airport()`. Each rule checks one supported SDK integer field for equality. Use the SDK's enumerated values for surface, parking type, parking name and gate. An empty tuple requests all the supported airport detail fields without filters. The rules apply to their collection. Other collections remain complete. [Facility fields](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_AddToFacilityDefinition.htm) · [SDK filter reference](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_AddFacilityDataDefinitionFilter.htm)

```python
from bbc_core import AirportFilter, AirportFilterField

gate_medium = AirportFilter(AirportFilterField.PARKING_TYPE, 9)
request = api.request_airport("YSSY", filters=(gate_medium,))
# Check facility_result(request.id) after the host pumps Core.
```

| Collection | Supported filter fields |
|---|---|
| Runways | Primary number, secondary number, surface |
| Parking | Type, name, number |
| Static jetways | Parking gate, parking spot |

Equal outstanding requests share one result. Core keeps concurrent filter sets independent and clears old rules before reusing a completed definition. It checks each returned record against the requested rules. A completed airport reply with no matches in a filtered collection has an empty tuple for that collection. Results retain the indices supplied by the simulator. Native verification still needs to confirm filtered counts, indices, combined filters and the no-match response. [SDK clear-all reference](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Facilities/SimConnect_ClearAllFacilityDataDefinitionFilters.htm) · [Offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_airport_filters_offline_2026-11-24/REPORT.md)

### Client event reception

Call `subscribe_client_event(name)` with an MSFS SDK event name such as `LANDING_LIGHTS_SET`, without the `K:` prefix. Read matching `Event` updates from `poll()`. Core shares each event across readers, reuses an existing send mapping for the same event, and uses a non-masking notification group. Read-only consumers can subscribe without the `client_events` write group. [MSFS event names](https://docs.flightsimulator.com/html/Programming_Tools/Event_IDs/Event_IDs.htm) · [Notification group membership](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_AddClientEventToNotificationGroup.htm)

```python
from bbc_core import Event

subscription = api.subscribe_client_event("LANDING_LIGHTS_SET")
# After the host pumps Core, check for matching notifications.
for update in api.poll():
    if isinstance(update, Event) and update.capability_id == subscription.capability.id:
        print(update.value.words)

api.unsubscribe(subscription.id)
```

`ClientEventData.words` contains one unsigned 32-bit word for a standard packet or five words for an extended packet, including zeros. Interpret each word using the relevant event's SDK documentation. A notification may echo a command and should be handled as an event. Use command outcomes and configured readback to track writes. Temporarily suspend one consumer using `set_event_enabled()`. Core removes the native group membership when no enabled consumer needs it and clears the group when empty. Native tests still need to establish which actions are published and verify event ordering and rate. [Extended event packet](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Structures_And_Enumerations/SIMCONNECT_RECV_EVENT_EX1.htm) · [Offline report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_client_event_reception_offline_2026-11-24/REPORT.md)

SimConnect also exposes mapped joystick buttons, axes, sliders and hat switches while a flight is loaded. The controller API below uses separate input groups. [MSFS 2020 hardware input mapping](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/InputEvents/SimConnect_MapInputEventToClientEvent_EX1.htm)

### Experimental simulator calls

> [!CAUTION]
> 🔴 **MSFS 2020 may silently ignore these three calls.** Microsoft lists `SAVE_FLIGHT`, `LOAD_FLIGHT_PLAN` and `RESERVE_KEY` as returning no error and no response. The calls are public for add-on compatibility. A result of `sent` or `unverified` does not establish that a file was saved, a plan was loaded, or a key was reserved. Treat the effect as unknown unless you have separate confirmation. [Flight save](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Flights/SimConnect_FlightSave.htm) · [Flight plan load](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Flights/SimConnect_FlightPlanLoad.htm) · [Reserved keys](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_RequestReservedKey.htm)

| Operation for `submit_simulator_operation()` | Arguments | Host-assigned group |
|---|---|---|
| `SimulatorOperation.SAVE_FLIGHT` | Absolute local `.FLT` path, title, description | `flight_files` |
| `SimulatorOperation.LOAD_FLIGHT_PLAN` | Absolute local `.PLN` path | `flight_files` |
| `SimulatorOperation.RESERVE_KEY` | One to three single characters for the SDK's exclusive Tab combinations. Letter case distinguishes the Shift modifier. | `reserved_keys` |

A matching reserved-key reply can confirm assignment and returns the combination in `command_result().data`. After a silent reservation, Core requires a new session before another reservation request because late replies carry no request ID. The EFB Logbook uses its own storage workflow.

### Controllers, object data and simulator operations

Call `request_controllers()` to get the device IDs and descriptions that MSFS reports. For a controller mapping, pass a `ControllerInput` to `subscribe_controller()` and consume its `HardwareSample` through `poll()`. Choose `button`, `POV` or `slider` with an index, or an SDK-named axis such as `XAxis`. Samples carry a signed axis value or a button press/release flag. Core shares equal mappings, registers them without masking pilot input, and tears them down when no enabled subscriber needs them. [Controller discovery](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/InputEvents/SimConnect_EnumerateControllers.htm) · [Hardware mapping](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/InputEvents/SimConnect_MapInputEventToClientEvent_EX1.htm)

```python
from bbc_core import ControllerInput, ObjectKind, SimVar, ValueType

devices = api.request_controllers()
# Check simulator_result(devices.id) after the host pumps Core.
button = api.subscribe_controller(ControllerInput(0, "button", 2))
aircraft = api.request_nearby_objects(ObjectKind.AIRCRAFT, 5000, SimVar("TITLE", None, ValueType.TEXT))
# Later, retrieve simulator_result(aircraft.id) and release button.id when done.
```

`request_nearby_objects(kind, radius_m, field)` returns a complete tuple of object samples. `request_object_data(field, target=None)` reads a single typed field from a current-session reference or the user aircraft. Both can use a scalar `SimVar` or a structured `ObjectField`. Structured formats cover initial position, latitude/longitude/altitude, waypoint, marker, XYZ and one-shot variable text. Fixed structures return decoded field tuples in the SDK's field order. A query result has a state, source, session, receipt time and optional data. For pull-based clients use `simulator_result(id)`; terminal results also arrive via `poll()`. [Nearby object query](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_RequestDataOnSimObjectType.htm)

| Operation for `submit_simulator_operation()` | Arguments in order | Group |
|---|---|---|
| `CREATE_PARKED_AIRCRAFT` | Container title, tail number, airport identifier | `ai_objects` |
| `CREATE_ENROUTE_AIRCRAFT` | Container title, tail number, flight number, absolute local `.PLN` path, initial plan position, touch-and-go flag | `ai_objects` |
| `CREATE_NON_ATC_AIRCRAFT` | Container title, tail number, `InitialPosition` | `ai_objects` |
| `CREATE_SIMULATED_OBJECT` | Container title, `InitialPosition` | `ai_objects` |
| `RELEASE_AI`, `REMOVE_OBJECT` | Owned `SimObjectRef` from a creation result | `ai_objects` |
| `SET_AI_FLIGHT_PLAN` | Owned object reference, absolute local `.PLN` path | `ai_objects` |
| `WRITE_OBJECT_DATA` | Owned object reference, fixed-size `ObjectField`, packed SDK bytes or `InitialPosition` | `ai_objects` |
| `LOAD_FLIGHT` | Absolute local `.FLT` path | `flight_files` |
| `EXECUTE_ACTION` | Documented action name, packed arguments as bytes | `simulator_actions` |

AI creation is confirmed by an assigned object ID. An object found by a nearby query can be read. Writes require the creation reference belonging to the same consumer in the current session. A dispatch without readback remains `unverified`, including an action callback whose effect was not independently checked. Use pilot-chosen local paths for file operations. Add-ons are responsible for choosing suitable action arguments using the SDK schema. [AI object removal and ownership](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/AI_Object/SimConnect_AIRemoveObject.htm) · [Action format](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/General/SimConnect_ExecuteAction.htm)

### Additional request modes and host tools

- Client Data supports `ClientDataMode.ONCE`, `ON_SET`, `SECOND` and `VISUAL_FRAME`. Use `subscribe_client_data(region, mode=…)`. Equivalent region and mode subscriptions share one provider request. Use visual-frame mode only for data the consumer needs at that rate. Writes continue to require a successful current-session baseline and the declared layout.
- Airport filters also support exact `AIRPORT_NAME`, `RUNWAY_LENGTH` and `PARKING_RADIUS`. Length and radius use SDK FLOAT32 equality in metres. The host can choose the additions-only legacy provider mode with `SimConnectProvider(..., legacy_facility_additions=True)`. In legacy mode the initial airport list is worldwide and no removals follow. The default EX1 mode supplies nearby additions and removals.
- An enabled client event consumer can request dialog-mode delivery with `request_dialog_notifications()`. Its result reports dispatch. The events arrive separately through `poll()`.
- The host can select the callback dispatch transport with `SimConnectSdk(..., callback_dispatch=True)`. Pull-based dispatch remains the default. `Core.response_times()` runs one SDK diagnostic on demand. The SDK wrapper owns the bounded `retrieve_string()` and `insert_string()` buffer helpers. These methods are host tools and are absent from the consumer handle. [Dialog delivery](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Events_And_Data/SimConnect_RequestNotificationGroup.htm) · [SDK string helper](https://docs.flightsimulator.com/html/Programming_Tools/SimConnect/API_Reference/Debug/SimConnect_RetrieveString.htm)

The 115-test guarded offline suite includes these paths. Native packet layouts, actual controller and scenery behavior, simulator file effects, add-on compatibility and performance still need validation. [Batch report](../reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_remaining_sdk_batch_offline_2026-11-24/REPORT.md)
