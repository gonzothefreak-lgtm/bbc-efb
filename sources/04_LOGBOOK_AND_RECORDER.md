# Logbook and Flight Recorder

The first whole-EFB build targets the full approved Q1–Q14 Logbook, including genuine automatic Flight Logging through read-only MSFS 2020 SimConnect, manual entry and manual override. The illustrated Logbook Design Record v1.2 remains the detail reference. Advanced layout customisation retains its C0.2 deferral.

| Decision | Requirement |
|---|---|
| Q1-Q2 | Dashboard, Flight History, Black Box Recorder Management and Statistics. Dashboard combines real summaries, aircraft-type profiles, charts, recent flights and quick links. |
| Q3 | Table, Cards and Combined History, shared search/filters and an in-EFB resizable Flight Details overlay. Preserve browsing context. |
| Q4 | Recorder Management attention queue for genuine native gaps. Acknowledgement preserves evidence. Manual and imported flights do not get native-gap warnings merely for missing telemetry. |
| Q5-Q7 | Statistics from verifiable records. No separate Aircraft or Reports pages. Settings owns Export All Data, with separate restorable backup. |
| Q8 | Shared layout model for Logbook and In-Flight. Save basic preferences and visibility in C0.1. Full drag/reorder/dock/presets deferred. |
| Q9 | Unified manual, captured and supported imported editing. Required manual fields are aircraft type, From, To and flight date. Preserve original values, corrections and provenance. Show diversions only on pilot confirmation. |
| Q10 | Optional crew, passengers, baggage and freight. Label incomplete sums and unknown totals. Detailed freight assignment and manifests are deferred. |
| Q11 | Persistent full side-on profile header in Flight Details; Overview, Black Box, Operations, Notes and Record History as applicable. Type-based art with honest fallback. |
| Q12 | Hybrid adaptive layout with minimum readable sizes and scrolling where needed. |
| Q13-Q14 | Automatic Flight Logging and manual override, Stop confirmation, privacy information and independent persistent footer statuses. |

The Recorder consumes Core observations, owns flight state and writes durable local records. Open decisions include flight start/end, block and airborne clocks, pause/Active Pause, simulator rate, touchdown and bounce, circuits/multiple sectors, interruption, crash recovery and local record schema. Retain gaps and provenance. Treat unsupported values as unavailable and never infer a pilot-confirmed diversion, airport, landing rate or payload without evidence.
