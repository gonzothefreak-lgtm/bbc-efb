"""The Recorder's Logbook sink (contract C2): recorded-flight records into the one FlightStore.

``LogbookSink(store_provider).save_recorded_flight(record) -> str`` turns a record built by
``bbc_recorder.records.build_recorded_flight`` (live flights and recovered ones alike) into a
schema-2 Logbook flight with ``FlightStore.create_recorded(fields, RecorderEvidence, ...)``.

* The store is looked up on every save through ``store_provider`` (the app's single
  FlightStore, contract C1), so a data-folder change is followed and no second store is
  ever opened here.
* Saving is idempotent by ``record["flight_id"]``: recovery may hand the same flight over
  again after a crash between "saved" and "journal archived". An existing Logbook flight
  with that Recorder flight id is returned instead of a duplicate.
* Values are stored exactly as the Recorder evidence supports them: unknown text is ``""``,
  unknown numbers are ``None``. Each field's origin is ``observed`` (read from a SimVar),
  ``derived`` (worked out from observations, such as an airport or the block minutes) or
  ``recorder`` (the Recorder left it unknown). Nothing is confirmed on the pilot's behalf:
  a suspected diversion or an ambiguous airport stays evidence for the pilot.
* Simulated data is always labelled simulated (``RecorderEvidence.simulated``).

It raises ``OSError`` (no folder open, read-only folder) or ``ValueError`` (a record it cannot
store honestly); the Recorder then keeps the journal and retries, so nothing is lost.

Call it off the UI thread: the Recorder service calls it from its save thread.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable, Mapping

from .storage import FIELD_NAMES, FlightFields, Gap, RecorderEvidence

RECORDER_FLIGHT_ID = "recorder_flight_id"

_TEXT_LIMIT = 200
_EXTRA_FIGURES = ("outcome", "end_reason", "trigger", "landings", "bounces", "touch_and_goes", "distance_nm",
                  "distance_bridged_nm", "distance_excluded_nm", "max_alt_msl_ft", "max_sim_rate", "paused_host_s",
                  "active_paused_host_s", "uncredited_gap_s", "block_source", "airborne_source", "started_in_air",
                  "warnings", "touchdowns_recorded", "takeoffs_recorded")


def _logbook_block(record: Mapping[str, Any]) -> Mapping[str, Any]:
    block = record.get("logbook")
    if isinstance(block, Mapping):
        return block
    # A record built without bbc_recorder.records (for example plain recovery.build_record).
    from bbc_recorder.records import logbook_from_record  # noqa: PLC0415 - optional, lazy

    return logbook_from_record(record)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Keep only JSON-native values (records come from JSON already; this is a guard)."""
    if depth > 12:
        return None
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value]
    return str(value)


class LogbookSink:
    """``FlightSink`` for ``bbc_recorder``: one Logbook flight per recorded flight."""

    def __init__(self, store_provider: Callable[[], Any]):
        if not callable(store_provider):
            raise TypeError("store_provider must be a callable returning the open FlightStore or None.")
        self._store_provider = store_provider

    def _store(self):
        store = self._store_provider()
        if store is None:
            raise OSError("No Logbook data folder is open, so the recorded flight cannot be saved yet. "
                          "Its journal is kept.")
        if getattr(store, "read_only", False):
            raise OSError("The Logbook data folder is open read-only, so the recorded flight cannot be saved. "
                          "Its journal is kept.")
        return store

    def find_existing(self, store: Any, flight_id: str) -> str | None:
        """The Logbook id of a flight already saved for this Recorder flight, if any.

        Raises ``OSError`` when an earlier save might exist but cannot be checked: any file
        ``listing()`` did not show as a flight (unreadable, misnamed by a sync tool, or a
        duplicate) that cannot be read right now or that names this Recorder flight. Creating
        a new flight then could duplicate it, so the Recorder keeps the journal and tries
        again later.
        """
        flights, unreadable = store.listing()
        for flight in flights:
            evidence = getattr(flight, "evidence", None)
            extra = getattr(evidence, "extra", None) or {}
            if getattr(flight, "source", None) == "recorder" and extra.get(RECORDER_FLIGHT_ID) == flight_id:
                return flight.id
        self._check_unreadable(store, flight_id, unreadable)
        return None

    @staticmethod
    def _check_unreadable(store: Any, flight_id: str, unreadable: Any) -> None:
        folder = getattr(store, "records", None)
        if folder is None:
            return
        needle = flight_id.encode("utf-8")
        # Every kind counts: a misnamed sync copy (``<uuid>-DESKTOP.json``) or a duplicate
        # may be the only copy of an earlier save, and listing() never parses those.
        for entry in unreadable or ():
            kind = getattr(entry, "kind", "unreadable")
            name = str(getattr(entry, "name", entry))
            try:
                data = (Path(folder) / name).read_bytes()
            except OSError as error:
                raise OSError(
                    f"The Logbook file {name} cannot be read right now ({error.strerror or error}), so BBC EFB "
                    "cannot check whether this flight was already saved. Its journal is kept and the save "
                    "is tried again later."
                ) from None
            if needle in data:
                problem = ("cannot be read" if kind == "unreadable" else
                           f"is not shown as a flight ({kind}: {getattr(entry, 'reason', '') or 'see the Black Box page'})")
                raise OSError(
                    f"An earlier save of this recorded flight ({name}) is in the Logbook folder but {problem}. "
                    "A second copy was not created. Its journal is kept; repair, rename or move that file "
                    "and the flight is saved again at the next start."
                )

    def save_recorded_flight(self, record: dict) -> str:
        if not isinstance(record, Mapping):
            raise ValueError("A recorded flight must be a mapping.")
        flight_id = record.get("flight_id")
        if not isinstance(flight_id, str) or not flight_id:
            raise ValueError("The recorded flight has no flight_id, so it cannot be saved safely.")
        status = str(record.get("status") or "unknown")
        if status == "discarded":
            raise ValueError("A discarded recording is not a Logbook flight.")
        store = self._store()
        existing = self.find_existing(store, flight_id)
        if existing is not None:
            return existing
        fields, origins, evidence, reason = self.convert(record)
        flight = store.create_recorded(fields, evidence, origins=origins, reason=reason)
        return flight.id

    # ---- mapping -------------------------------------------------------------------------

    def convert(self, record: Mapping[str, Any]) -> tuple[FlightFields, dict[str, str], RecorderEvidence, str]:
        """Map a record to (FlightFields, origins, RecorderEvidence, revision reason). Pure."""
        block = _logbook_block(record)
        candidates = block.get("fields") if isinstance(block.get("fields"), Mapping) else {}
        values: dict[str, Any] = {}
        origins: dict[str, str] = {}
        field_status: dict[str, Any] = {}
        for name in FIELD_NAMES:
            item = candidates.get(name)
            if not isinstance(item, Mapping):
                continue
            value = item.get("value")
            origin = item.get("origin")
            field_status[name] = {key: item.get(key) for key in ("status", "reason", "confidence", "candidate")
                                  if item.get(key) is not None}
            if name == "flight_date":
                if isinstance(value, str) and value:
                    values[name] = value
            elif name == "minutes":
                if type(value) is int and 0 <= value <= 9999:
                    values[name] = value
            elif isinstance(value, str) and value and len(value) <= _TEXT_LIMIT:
                values[name] = value
            if name in values and origin in ("observed", "derived", "recorder"):
                origins[name] = origin
        if "flight_date" not in values:
            raise ValueError("The recorded flight has no time at all, so it has no flight date. Its journal is kept.")
        for name in FIELD_NAMES:
            origins.setdefault(name, "recorder")
        fields = FlightFields(
            flight_date=values["flight_date"], aircraft=values.get("aircraft", ""),
            departure=values.get("departure", ""), arrival=values.get("arrival", ""),
            callsign=values.get("callsign", ""), registration=values.get("registration", ""),
            minutes=values.get("minutes"), notes="",
        )
        basis = block.get("time_basis") if block.get("time_basis") in ("simulator", "host") else "host"
        simulated = block.get("simulated")
        if not isinstance(simulated, bool):
            source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
            simulated = source.get("simulated")
        source_known = isinstance(simulated, bool)
        gaps = []
        gaps_detail = []
        for item in block.get("gaps") or []:
            if not isinstance(item, Mapping):
                continue
            reason = _text(item.get("reason")).strip() or "Data was missing"
            gaps.append(Gap(reason, item.get("start") or None, item.get("end") or None, _text(item.get("kind"))))
            gaps_detail.append(_json_safe({key: value for key, value in item.items()
                                           if key not in ("reason", "start", "end", "kind")}))
        evidence_ref = record.get("evidence") if isinstance(record.get("evidence"), Mapping) else {}
        journal = evidence_ref.get("journal")
        if not isinstance(journal, str) or not journal.strip():
            raise ValueError("The recorded flight does not name its journal, so it has no evidence. Not saved.")
        figures = block.get("figures") if isinstance(block.get("figures"), Mapping) else {}
        integrity = record.get("integrity") if isinstance(record.get("integrity"), Mapping) else {}
        extra = {
            RECORDER_FLIGHT_ID: record.get("flight_id"),
            "record_kind": record.get("kind"),
            "record_version": record.get("record_version"),
            "status": record.get("status"),
            "journal_sha256": evidence_ref.get("sha256"),
            "close_detail": record.get("close_detail"),
            "interruption": record.get("interruption"),
            "integrity": {key: integrity.get(key) for key in ("clean", "issue_count", "torn_tail_bytes", "size_bytes",
                                                              "lost_lines", "lost_kinds", "writer_error",
                                                              "notes_omitted")
                          if key in integrity},
            "evidence_warnings": list(block.get("evidence_warnings") or []),
            "needs_confirmation": [name for name in block.get("needs_confirmation") or [] if name in FIELD_NAMES],
            "app_version": record.get("app_version"),
            "source": record.get("source"),
            "touchdown_rate_source": block.get("touchdown_rate_source"),
            "duration_notes": block.get("duration_notes") or [],
            "field_status": field_status,
            "attribution": block.get("attribution") or {},
            "arrival_assessment": block.get("arrival_assessment"),
            "figures": {key: figures[key] for key in _EXTRA_FIGURES if key in figures},
            "gap_details": gaps_detail,
        }
        if not source_known:
            extra["simulated_unknown"] = True
        evidence = RecorderEvidence(
            journal=journal, summary=_text(block.get("summary_text")) or "Recorded by the Black Box Recorder.",
            time_basis=basis,
            # An unknown source is never presented as native telemetry.
            simulated=simulated if source_known else True,
            off_block=block.get("off_block"), takeoff=block.get("takeoff"), landing=block.get("landing"),
            on_block=block.get("on_block"), block_s=_number(block.get("block_s")),
            airborne_s=_number(block.get("airborne_s")), touchdown_rate_fpm=_number(block.get("touchdown_rate_fpm")),
            gaps=tuple(gaps), rules_version=_text(record.get("rules_version")),
            extra=_json_safe(extra),
        )
        reason = f"Recorded by the Black Box Recorder ({record.get('status') or 'unknown'})."
        return fields, origins, evidence, reason


__all__ = ["LogbookSink", "RECORDER_FLIGHT_ID"]
