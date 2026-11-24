# Logbook storage schema (record format 2)

The Logbook keeps the pilot's flights in a folder the pilot chooses. Nothing is hidden in AppData: the only file outside the data folder is the small settings file that remembers where the data folder is (`%APPDATA%\BBC EFB\settings.json` on Windows, see `storage_settings.py`).

```
<data folder>/
  .bbc-efb-writer.lock          exclusive writer lock (held while an EFB has the folder open)
  flights/
    <uuid>.json                 one file per flight, UTF-8 JSON
  migration/
    schema-1/<uuid>.json        byte-for-byte copy of a format-1 file, made before its first migration
```

## Rules that protect the flight log

1. **Append, never rewrite.** Every change adds a revision. Earlier revisions, and every key this version does not understand, are written back exactly as they were read. Only the values the pilot actually changed are normalised (trimmed, upper-cased for codes) and checked against the limits below.
2. **Unknown is not zero.** Optional numbers (duration, crew, passengers, baggage, freight) are `null` when unknown. Recorder measurements are `null` when not measured. Text fields are `""` when unknown.
3. **Byte-level conflict check.** A save re-reads the file and refuses with `RecordConflict` if its SHA-256 differs from the copy the editor opened, even when the only change is letter case or spacing.
4. **One writer per folder.** `FlightStore(root)` takes an exclusive, non-blocking lock on `.bbc-efb-writer.lock` (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere) and keeps it until `close()`. A second EFB gets `StoreLocked` with a plain message and can open the folder with `read_only=True` instead. `StoreLocked` means real lock contention only (`EWOULDBLOCK`/`EAGAIN`/`EACCES`, or `EDEADLOCK` from `msvcrt`); a folder whose file system cannot lock at all (for example `ENOTSUP` or `ENOLCK` on some network shares) raises `DataFolderNotWritable` asking for a local folder. Inside one process, share a single `FlightStore` (the app owns it; the Logbook and the Recorder's sink use that instance): its compare-and-write is serialised by a lock, so the Recorder thread and the UI thread cannot both write revision N+1. `listing()` and `get()` do not take that lock, so a write that is retrying a busy file (up to about a second) never stalls the UI. Writes can block that long, so never call them from the Core pump thread; hand them to a worker thread.
5. **File names.** If a case-sensitive folder holds the same flight id spelled in two cases, `listing()` and `get()`/saves use the same file: the exact lower-case `<uuid>.json` first. The other spelling is listed as a duplicate.
6. **Atomic writes.** New content goes to `flights/.flight-*.tmp`, is fsynced, then replaces the record with `os.replace`. On Windows a sharing or access error (WinError 5, 32, 33) from OneDrive, antivirus or a backup tool is retried for about one second (0.02 s rising to 0.25 s) before `FlightFileBusy` reports it; the old file is untouched. On POSIX the folder is fsynced after the rename. Temporary files older than a minute that a crashed writer left behind are removed when the folder is next opened for writing.
7. **Newer formats are read-only.** A file with `schema` greater than 2 is shown if its structure can be read, marked read-only, and never changed. If it cannot be read, it is listed with that reason.

## File layout

```json
{
  "schema": 2,
  "id": "6f1c6a8e-3f4e-4c4e-9a52-1d2b9d1d2c11",
  "source": "manual | recorder | imported",
  "recorder": { "...": "evidence block, recorder flights only" },
  "import": { "importer": "CSV", "reference": "logbook.csv line 4" },
  "revisions": [
    {
      "saved_at": "2026-11-24T10:05:18.123+00:00",
      "by": "pilot | recorder | import",
      "reason": "optional, why the change was made",
      "origins": { "aircraft": "observed", "departure": "derived" },
      "confirmed": [ "aircraft" ],
      "fields": {
        "flight_date": "2026-11-24",
        "aircraft": "A320",
        "departure": "EGLL",
        "arrival": "LFPG",
        "callsign": "BAW12",
        "registration": "",
        "minutes": 95,
        "notes": "",
        "crew": 2,
        "passengers": null,
        "baggage_kg": null,
        "freight_kg": 0
      }
    }
  ],
  "acknowledgements": [ { "gap": 0, "at": "2026-11-24T12:00:00.000+00:00", "by": "pilot", "note": "" } ],
  "migrated_from": { "schema": 1, "at": "…", "original_copy": "migration/schema-1/<uuid>.json" },
  "extra": { "anything": "an importer or a newer version wants kept" }
}
```

Only `schema`, `id`, `source` and `revisions` are always present. `recorder`, `import`, `acknowledgements`, `migrated_from` and `extra` appear when they apply.

### Top level

| Key | Meaning |
|---|---|
| `schema` | Record format number. This build writes 2 and reads 1 and 2. |
| `id` | Canonical lower-case UUID. Must match the file name (letter case of the file name is ignored). |
| `source` | Who created the flight: `manual` (pilot entry), `recorder` (Black Box Recorder) or `imported`. |
| `recorder` | Recorder evidence (below). Written once when the Recorder creates the flight and never changed afterwards. |
| `import` | Which importer produced the flight and a reference to its source (file and line, for example). |
| `acknowledgements` | Append-only list. The pilot has seen Recorder gap number `gap`. The evidence itself stays unchanged. |
| `migrated_from` | Present when this file was converted from an older format. Names the kept original copy. |
| `extra` | Free object for data this version does not model. Any other unknown top-level key is also kept as it is. |

### Revisions

`revisions[0]` holds the original values. Each later revision is a complete snapshot after one save, so the **per-field provenance** of any field is derived from the list without a second copy that could disagree:

* **origin**: `revisions[0].origins[field]` if given, otherwise from `revisions[0].by` (`pilot` → `pilot`, `recorder` → `recorder`, `import` → `imported`). Allowed origins: `pilot`, `observed`, `derived`, `recorder`, `imported`.
* **original value**: `revisions[0].fields[field]`.
* **corrections**: every later revision where the field changed, with the time (`saved_at`), the previous and new value, who saved it (`by`) and `reason`.
* **confirmations**: every pilot revision (`by` = `pilot`) whose `confirmed` list names the field. A field is **confirmed** when such a revision comes after its last change, so the value shown is the one the pilot checked. A later change clears it again.

`confirmed` (optional) lists fields the pilot checked and confirmed as correct in that revision. `FlightStore.confirm` writes it as a new revision that repeats every value exactly as stored, so a confirmation is part of the history even though nothing changed. It is how the pilot resolves a field the Recorder marked as needing confirmation (`recorder.needs_confirmation` and `recorder.field_status`, for example an aircraft filled from the title only, an ambiguous airport or a suspected diversion) without retyping it. Names this version does not know are ignored but kept; a malformed list is kept as stored, ignored and noted as a problem. A `confirmed` entry in a revision saved by the Recorder or an importer is ignored: only the pilot confirms.

A pilot's answer to a suspected diversion is kept in that revision's `fields` as `diversion`: `{"confirmed": true | false, "planned_arrival": "EGLL", "arrival": "EGKK", "reason": "…"}` (only `confirmed` is required; the other values are text or `null`). Like any unknown `fields` key it is carried into later revisions. The Logbook shows a flight as diverted only when `diversion.confirmed` is `true`.

`saved_at` must be an ISO 8601 date and time with a UTC offset (written in UTC with milliseconds). A record with a missing, non-text or offset-less `saved_at` is listed as unreadable with that reason rather than shown with a wrong date or breaking the History list.

Unknown keys inside a revision, or inside its `fields`, are kept. Unknown `fields` keys are carried into each new revision so data from a newer build is not lost when an older build saves an edit.

### Fields and limits

| Field | Type | Unknown | New or changed value |
|---|---|---|---|
| `flight_date` | `YYYY-MM-DD` text | required | valid date |
| `aircraft` | text | `""` (manual flights require it) | trimmed, upper-cased for pilot input, ≤ 256 characters |
| `departure`, `arrival` | text | `""` (manual flights require them) | trimmed, upper-cased, ≤ 64 characters |
| `callsign`, `registration` | text | `""` | trimmed, upper-cased, ≤ 32 characters |
| `notes` | text | `""` | trimmed, ≤ 20,000 characters |
| `minutes` | whole number | `null` | 0 to 9,999 |
| `crew` | whole number | `null` | 0 to 99 |
| `passengers` | whole number | `null` | 0 to 9,999 |
| `baggage_kg`, `freight_kg` | whole number, kilograms | `null` | 0 to 999,999 |

Limits apply to new or changed values only, measured after normalisation (so `ß` upper-cased to `SS` cannot slip past a limit). A stored value outside a limit (from a hand edit, an import or a later build) is shown, kept unchanged and noted as a problem; it is never truncated. Recorder and imported values are stored exactly as supplied (no case change, no trimming). Text that cannot be encoded as UTF-8 is refused before anything is written.

### Recorder evidence (`recorder`)

| Key | Meaning |
|---|---|
| `journal` | Path of the Recorder journal, relative to the data folder (absolute paths and `..` are refused). |
| `summary` | Plain-English summary written by the Recorder. |
| `time_basis` | `simulator` (simulator UTC, for example from `ABSOLUTE TIME`) or `host` (the computer clock). Every time in the block uses this basis. |
| `simulated` | `true` when the data came from the simulated provider. Such a flight is always labelled simulated and never shown as native telemetry. |
| `off_block`, `takeoff`, `landing`, `on_block` | ISO 8601 times with an offset, or `null` when not recorded. |
| `block_s`, `airborne_s` | Durations in seconds, or `null` when not measured. |
| `touchdown_rate_fpm` | Vertical speed at touchdown in feet per minute as measured, or `null` when not measured. Never inferred. |
| `gaps` | List of `{reason, start, end, kind}`. `reason` is required; `start`/`end` are times in the same basis or `null`. |
| `rules_version` | Version of the Recorder rules that produced the flight. |

Unknown keys in the evidence block are kept.

## Reading format 1

Format 1 (the first concept) stored only manual flights with eight fields and `saved_at` revisions. It is read as it is: values are not normalised on load, missing payload fields read as unknown, and the file is not touched by listing. The first time such a flight is saved:

1. the original bytes are copied to `migration/schema-1/<uuid>.json` (an existing copy is never overwritten);
2. `schema` becomes 2 and `migrated_from` names the copy;
3. all existing revisions are written back unchanged and the new revision is appended.

## Python API (`bbc_efb.storage`)

```python
store = FlightStore(root)                       # writer; raises DataFolderMissing, DataFolderNotWritable, StoreLocked
store = FlightStore(root, create=True)          # only for a folder the pilot has just chosen (creates parents)
reader = FlightStore(root, read_only=True)      # never locks or writes

flights, unreadable = store.listing()           # newest first; unreadable: UnreadableFile(name, kind, reason)
flight = store.get(flight_id)                   # always re-reads the file; RecordMissing if it is gone

store.save(fields)                              # new manual flight (all fields validated and normalised)
store.save(fields, flight, reason="…")          # pilot edit as a new revision; RecordConflict if changed on disk

store.create_recorded(fields, RecorderEvidence(...), origins={"aircraft": "observed"})
store.create_imported(fields, importer="CSV", reference="…", extra={...})
store.correct(flight, {"arrival": "LFPG"}, reason="Recorder lost the arrival", by="pilot")
store.acknowledge_gap(flight, 0, note="Known disconnect")
store.confirm(flight, ["aircraft"])             # pilot confirms values as correct; writes a revision, changes nothing
store.confirm(flight, ["arrival"], diversion={"confirmed": True, "planned_arrival": "EGLL"})

flight.provenance("arrival")   # FieldProvenance(name, origin, original, value, corrections, confirmations, confirmed)
flight.corrected_fields()      # names of fields changed after creation
flight.confirmed_fields()      # names of fields the pilot confirmed after their last change
flight.read_only, flight.read_only_reason, flight.problems, flight.simulated
store.close()                  # releases the writer lock
```

`listing()` keeps an in-memory index keyed by file name with each file's modification time, size and (outside Windows) inode, so a refresh reads only files that changed. `get()` and every save read the file itself, so the cache can never hide a conflict.

Errors are plain-English and subclass the usual built-ins: `RecordConflict`, `RecordReadOnly` and `UnreadableRecord` are `ValueError`; `RecordMissing` and `DataFolderMissing` are `FileNotFoundError`; `DataFolderNotWritable`, `StoreReadOnly` and `FlightFileBusy` are `PermissionError`; `StoreLocked` is `OSError`.

A file in `flights/` that is not shown is reported by `listing()` with a reason: `unreadable` (corrupt JSON, bad `saved_at`, wrong types), `misnamed` (the name is not the flight id, for example a sync-conflict copy `<uuid>-DESKTOP-ABC.json` or a hex-only spelling) or `duplicate` (two files hold the same id). Such files are never changed or deleted by the Logbook.
