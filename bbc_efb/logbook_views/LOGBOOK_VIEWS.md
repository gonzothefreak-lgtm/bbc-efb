# Logbook views (Q1–Q14 Logbook pages)

This package holds the approved Logbook pages from the Logbook Design Record v1.2 and `sources/04_LOGBOOK_AND_RECORDER.md`:

| Page | File | Approved decision |
|---|---|---|
| Dashboard | `dashboard.py` (`DashboardPage`) | Q1–Q2 |
| Flight History: Table, Cards, Combined | `history.py`, `cards.py`, `filters.py` | Q3, Q8 (view and column preferences), Q12 |
| Flight Details overlay | `details.py` | Q3, Q9 (provenance), Q10, Q11 |
| Black Box (Recorder Management) | `recorder_management.py` | Q4 |
| Statistics | `statistics.py` | Q5 |
| Logbook section body | `dashboard.py` (`LogbookWorkspace`) | Q1, Q6–Q7 (no Aircraft or Reports page) |
| Charts | `charts.py` | Drawn with QPainter. No new dependency. |
| Aircraft art | `aircraft_art.py` | Shared type-based side-on profiles, with an honest generic fallback |

Every page reads read-only `FlightView` objects from `model.py`. `model.py` imports no Qt and no storage. It builds views from the stored record dictionaries described in `bbc_efb/STORAGE_SCHEMA.md` (formats 1 and 2), or from any object shaped like `bbc_efb.storage.Flight`. The adapter uses `flight.raw` when it is present and the attributes otherwise.

## Honesty rules the pages follow

- **Unknown stays unknown.** A missing number is shown as "—", "Not recorded", "Not measured" or "Not entered", in muted italics. It is never shown as 0. Totals add only the known values and state how many flights had none. For example: "1,784.2 h · 120 flights without a known time".
- **Measured values only.** These values appear only when the record holds them:
  - touchdown rate: from Recorder evidence, or from an imported record that carries one, labelled "Imported";
  - landing count: from Recorder touchdowns or a recorded landing time, or from a pilot or imported `landings` value;
  - distance: Recorder-measured, or a pilot or imported `distance_nm`.

  Nothing is estimated from routes or aircraft types. A manual record's `touchdown_rate_fpm` is ignored.
- **Payload** adds only baggage and freight. No crew or passenger mass is assumed. A partial sum reads "At least 18,420 kg (incomplete: baggage unknown)".
- **Diversions** show only when `diversion.confirmed` is `true` (Q9).
- **Fields that need confirmation.** A Recorder field the Recorder could not be sure of (`recorder.field_status` / `recorder.needs_confirmation`: an aircraft from the title only, an ambiguous or low-confidence airport, a registration from a weak source) and an unanswered suspected diversion (`recorder.arrival_assessment.verdict == "diversion_suspected"`) become `FlightView.field_checks`. History and the cards lead the status with "Needs confirmation" (`FlightView.review_label`; the record status itself is unchanged), and the "Field confirmation" filter shows only those flights, or only flights with a pilot-confirmed field. Flight Details lists each one with **Confirm as correct** (a pilot revision that changes no value and names the field in `confirmed`, written by `FlightStore.confirm`) and **Correct…** (the editor). A suspected diversion offers **Confirm diversion** and **Not a diversion**, which also store the pilot's answer as `diversion`. A pilot correction or confirmation after the field's last change clears the check; a later change brings it back.
- **Simulated data** is labelled everywhere: the header, the Black Box tab, cards and the Source column. Dashboard and Statistics totals exclude it by default and say how many flights were excluded.
- **Gaps** (Q4):
  - Only Recorder flights can have gaps.
  - Manual flights never get warnings, and imported flights never get warnings for missing telemetry.
  - An imported flight goes to the queue only when the record has real problems, such as an invalid date or an out-of-range value.
  - Accepting a gap appends an acknowledgement. The gap stays in the record, and the flight then shows "Reviewed, data gaps accepted".
- **Aircraft art:**
  - A recognised type gets a drawn *family* silhouette. Its caption says "Twin-engine narrow-body jet, family illustration".
  - An unrecognised type gets a neutral dashed generic profile captioned "Generic profile: no artwork for this type". It is never an unrelated aircraft.
  - Livery never changes the type identity.
  - `AircraftArtLibrary.register_image(type, pixmap)` is the hook for Main's shared image library. A registered image always wins.

## Record mapping (what the pages read)

| FlightView | Stored record | Notes |
|---|---|---|
| `id`, `source`, `schema` | `id` (lower-cased), `source` (format 1 ⇒ `manual`), `schema` | An unknown source is shown as imported, with a problem listed |
| `flight_date` … `freight_kg` | `revisions[-1].fields` | Bools and non-integers in number fields read as unknown |
| `provenance[field]` | origin from `revisions[0].origins`, otherwise from `revisions[0].by`; original from `revisions[0].fields`; corrections from later revisions | Same rule as `Flight.provenance` in storage |
| `revisions[i].changes` | difference between consecutive revisions | |
| `operation`, `freight_description`, `payload_total_kg`, `distance_nm`, `landings`, `diversion` | unknown keys in `revisions[-1].fields` first, then top-level `extra` | **PROPOSAL:** these keys are not in schema v2 yet. Storage keeps them, unchanged, in `extra` or in revision fields. |
| `block_s`, `airborne_s`, `off_block`, `takeoff`, `landing`, `on_block`, `time_basis`, `journal`, `summary`, `rules_version` | `recorder.*` | As defined in `STORAGE_SCHEMA.md` |
| `touchdown_rate_fpm` | `recorder.touchdown_rate_fpm`. For imported records only: `touchdown_rate_fpm` in revision fields or `extra` | Origin is kept: `recorder` or `imported` |
| `touchdowns` | `recorder.touchdowns` = `[{time, rate_fpm, bounce}]` | **PROPOSAL** (unknown keys in the evidence block are kept by storage) |
| `evidence_events` | `recorder.events` = list of `RecorderEvent.to_json()` without `sample` | **PROPOSAL**. Optional: a compact copy for when the journal is gone |
| `recorder_state` | `recorder.status` (or `recorder.state`): `complete`, `interrupted`, `incomplete`, `recovered`, `abandoned` | **PROPOSAL**. Interrupted with no gap ⇒ attention |
| `distance_nm`, `landings` (measured) | `recorder.distance_nm`, `recorder.landings` | **PROPOSAL** |
| `gaps[i]` | `recorder.gaps[i]` plus the `acknowledgements` entry whose `gap` is `i` | |
| `importer`, `import_reference` | `import.importer`, `import.reference` | |

### Journal (Black Box tab)

`load_journal(data_root, relative_path)` reads the Recorder's JSONL journal, but only when the pilot opens the Black Box tab:

- Each line is `RecorderEvent.to_json()`, or `{"seq": n, "event": {...}}`.
- A torn or unreadable line is counted and reported, and never fatal.
- Paths outside the data folder are refused.
- Files over 256 MB are not opened.
- `sample` events with `altitude_ft` (or `indicated_altitude_ft`, `plane_altitude_ft`, `alt_ft`, `altitude`) draw the side-on altitude profile, thinned to 2,000 points.
- Touchdown and bounce rates are read from `rate_fpm`, `touchdown_rate_fpm`, `vertical_speed_fpm` or `touchdown_normal_velocity_fpm`.

If there is no journal, the tab shows the saved evidence (times, gaps, touchdowns) and says so.

## Public interface

```python
from bbc_efb.logbook_views import (
    LogbookWorkspace, FlightView, flight_view, flight_views, FilterState,
    StoreActions, GapAcknowledger, journal_loader_for, summarise, AircraftArtLibrary,
)

workspace = LogbookWorkspace(
    actions=StoreActions(store),                 # GapAcknowledger; None = read-only (Accept disabled)
    journal_loader=journal_loader_for(data_root),
    preferences={"view_mode": "combined", "hidden_columns": ["registration", ...]},
    can_edit=store.writable,
)
flights, unreadable = store.listing()
workspace.set_flights(flights, unreadable)       # Flight objects, record dicts or FlightView
workspace.navigate("dashboard" | "history" | "black_box" | "statistics")
workspace.open_flight(flight_id, tab="overview" | "black_box" | "operations" | "notes" | "history")
workspace.set_flight_logging_status("Flight Logging: Ready")   # text owned by the Recorder
workspace.set_recorder_status(status)            # live Black Box card from RecorderStatus / LoggingStatus
workspace.set_actions(StoreActions(store) or None)   # after a data folder change (None: read-only)
workspace.set_journal_loader(journal_loader_for(root))
workspace.reset_browsing()                       # close the overlay, clear selection and filters
workspace.set_can_edit(False, "The Logbook folder is read-only.")

# signals
workspace.edit_requested(str flight_id)          # open the unified Flight Editor (logbook.py)
workspace.confirm_requested(str flight_id, tuple names, dict | None diversion)
                                                 # Logbook.confirm_fields -> StoreActions.confirm_fields(seen=view)
workspace.new_flight_requested()
workspace.records_changed()                      # after accepting gaps: reload store.listing()
workspace.diagnostics_requested()                # Settings > SimConnect > Connection Diagnostics
workspace.flight_logging_settings_requested()    # Settings > Flight Logging & Privacy
workspace.preferences_changed(dict)              # persist with the Settings preferences store
```

Each page can also be used on its own: `DashboardPage`, `FlightHistoryPage` (give it `details=FlightDetailsOverlay(host)`), `RecorderManagementPage(actions=...)`, `StatisticsPage`, and `FlightDetailsOverlay(host, journal_loader=..., library=...)`.

## Integration (done in round 3)

`bbc_efb/logbook.py` now hosts one `LogbookWorkspace` plus the unified `FlightEditor`. It follows contract C1: the app owns the single `FlightStore`; the Logbook gets it through `set_store(store | None)`, asks the shell for a folder with `data_folder_requested`, and feeds the Black Box page with `set_recorder_status(status)`. `live_status.describe_recorder_status` (no Qt) words the live status; it says "not available" only when the status itself is `unavailable`. Corrections are labelled with who made the latest one ("corrected by Recorder").

## Integration steps (original notes)

1. In `logbook.py`, replace the old summary and History pages with one `LogbookWorkspace`. Keep `FlightEditor` as the unified editor and connect `edit_requested` and `new_flight_requested` to it. After a save, call `workspace.set_flights(*store.listing())`.
2. Wire `records_changed` to the same reload.
3. Use `StoreActions(store)` for gap acknowledgement. `acknowledge_gaps(flight_id, indexes, note, seen=view)` re-reads the flight, refuses with `StaleListing` (writing nothing) when it differs from the view the pilot reviewed, and chains each `store.acknowledge_gap(flight, index, note=...)` on the flight the previous one returned, so the store's digest check applies between them. The single-gap `acknowledge_gap(..., seen=None)` is kept for compatibility.
4. Pass `delete=` only once the owner approves a retention policy. Until then the Delete button stays disabled, with an explanation.
5. Pass `journal_loader_for(store_root)` so the Black Box tab can read archived journals.
6. Persist `preferences_changed` with the Settings preferences store (Q8 basic preferences). Read them back into `preferences=`.
7. Forward `diagnostics_requested` and `flight_logging_settings_requested` to the shell's Settings navigation.
8. Feed `set_flight_logging_status` from the Recorder service, never from the SimConnect state (Q14: separate statuses).
9. In the shell: for a frameless main window, the overlay covers the workspace only, so the footer statuses stay visible (Q14).

## Proposals for the owner

These are open decisions in the sources. Each default below is configurable or isolated in one function.

1. **Logged flight time.** Uses the record's duration (`minutes`, which carries pilot corrections) if set, otherwise the Recorder's measured block time. Airborne time is shown separately, never substituted (`FlightView.flight_time_s`).
2. **Touchdown-rate statistics** use the magnitude of the measured descent rate, so stored sign conventions don't matter:
   - Best is the softest. Worst is the firmest.
   - The distribution uses neutral 50 fpm bands, with no grading or colours, because grading thresholds are not approved (`statistics.RATE_BANDS`).
   - Per-flight values are shown as stored.
3. **Simulated-data flights** are excluded from Dashboard and Statistics totals by default, with a visible count and an "Include simulated-data flights" switch. They are always listed in History, labelled.
4. **Attention queue contents:**
   - unreviewed Recorder gaps;
   - Recorder flights marked interrupted with no gap that explains them;
   - Recorder flights with no evidence block;
   - imported records with problems;
   - unreadable, misnamed or duplicate files from `store.listing()`.

   Manual records are never queued (`recorder_management.attention_items_for`).
5. **Accepting gaps** accepts every unreviewed gap of a flight in one step, with one optional note. The evidence is untouched.
6. **Delete recording** is offered only when the host supplies a delete action. A soft delete to a recoverable folder is suggested, and it is always behind a confirmation.
7. **Date filter.** While a date range is active, flights with an unknown date are hidden. They come back when the range is cleared.
8. **Default History columns.** Hidden by default: registration, off-block, takeoff, landing, on-block and airborne. The pilot can show them from the column header menu (Q8 basic preferences).
9. **Times are shown in UTC on the record's basis**, labelled "simulator UTC" or "computer clock, UTC". Local-time display needs airport time zones and is not attempted.
10. **New optional record keys** (`operation`, `freight_description`, `payload_total_kg`, `distance_nm`, `landings`, `diversion`, `recorder.touchdowns`, `recorder.events`, `recorder.status`, `recorder.distance_nm`, `recorder.landings`) are read if present. Storage v2 keeps them unchanged, and they could be promoted to first-class fields in a later schema.
11. **Aircraft art.** Drawn family silhouettes stand in until Main's shared image library exists. Generic fallback for unrecognised types.

## Performance (offscreen, 2,000 mixed flights, Apple silicon)

| Operation | Time |
|---|---|
| Convert to views | about 50 ms |
| Workspace load (History and Dashboard) | about 25 ms |
| Filter change | about 2 ms |
| Sort | about 5 ms (sorting runs in the source model, not via `data()` calls in the proxy) |
| Paint the Combined view | about 11 ms |
| Statistics page render | about 12 ms |

Hidden pages are refreshed only when shown. Cards are virtualised with `QListView` icon mode and uniform item sizes.

## Tests

`tests/test_logviews_*.py` (114 offline cases; the Logbook integration is in `tests/test_efb_logbook.py`): model mapping and honesty rules, storage interop, journal reading, filter logic and the filter bar, 2,000-flight performance and filter correctness against the pure predicate, view modes and coordinated selection, preserved browsing context, the overlay (tabs, resize, Esc and focus return, read-only), summaries, Dashboard, Statistics, the workspace, the attention queue and acknowledgement flow, aircraft classification and charts.

```
cd bbc-efb-fixes
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -t . -p "test_logviews_*.py"
```
