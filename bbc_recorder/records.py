"""Recorded-flight records built only from journal evidence, for live and recovered flights.

The service closes a flight's journal and then hands it to ``bbc_recorder.recovery``
(``finalize_closed`` for a flight that ended, ``finalize_interrupted`` for Save & Exit, a
very long data gap or a journal found at startup). Both call :func:`build_recorded_flight`
as their ``record_builder``, so a live flight and a recovered one produce the same record
shape from the same evidence: what the journal says, nothing more.

The record is ``bbc_recorder.recovery.build_record`` plus a ``logbook`` block: the values a
Logbook (``bbc_efb.recorder_sink``) needs, each with its origin, status and reason. Rules:

* A value the journal does not support is unknown (``None`` or ``""``), never estimated.
* Block time needs both block-off and block-on observed. Airborne time needs a flight that
  ended (its clocks closed) and did not start in the air. An interrupted flight therefore has
  neither, even if the aircraft clearly flew.
* Touchdown rate is the Recorder's measured rate of the final landing, or ``None``.
* Departure and arrival come from the attribution events the service journalled (nearest
  airport facility queries at block-off and block-on). Only a ``known`` match fills the field;
  an ambiguous or unknown one leaves it empty and keeps the candidates as evidence.
* A suspected diversion is kept as evidence for the pilot to confirm. Nothing here confirms one.
* Times use one basis: simulator UTC (``ABSOLUTE TIME``) when every milestone has it, else the
  host clock. Gap times use the same basis; an end that is not known is ``None``.

No Qt, no SimConnect and no Core import.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .model import EventKind
from .recovery import INTERRUPTION_REASON, build_record

RECORD_LOGBOOK_VERSION = 1

# Logbook field names (they match bbc_efb.storage.FlightFields; this module does not import it).
LOGBOOK_FIELDS = ("flight_date", "aircraft", "departure", "arrival", "callsign", "registration", "minutes")

_MAX_MINUTES = 9999


# --------------------------------------------------------------------------------------------
# Helpers over the record's event JSON (RecorderEvent.to_json())
# --------------------------------------------------------------------------------------------


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def _events(record: Mapping[str, Any], kind: EventKind) -> list[Mapping[str, Any]]:
    items = (record.get("milestones") or {}).get(kind.value) or []
    return [item for item in items if isinstance(item, Mapping)]


def _data(event: Mapping[str, Any] | None) -> Mapping[str, Any]:
    data = event.get("data") if isinstance(event, Mapping) else None
    return data if isinstance(data, Mapping) else {}


def final_summary(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The state machine's final ``FlightSummary`` from the journalled FLIGHT_ENDED event."""
    ended = _events(record, EventKind.FLIGHT_ENDED)
    summary = _data(ended[-1]).get("summary") if ended else None
    return summary if isinstance(summary, Mapping) else None


def attributions(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The last journalled attribution per role (``departure``, ``arrival``, ``identity``)."""
    out: dict[str, Mapping[str, Any]] = {}
    for event in _events(record, EventKind.ATTRIBUTION):
        data = _data(event)
        role = data.get("role")
        if isinstance(role, str):
            out[role] = data
    return out


def journal_is_loggable(events: Iterable[Any]) -> bool:
    """True when the journalled events show a flight worth a Logbook entry.

    The same rule as ``FlightRecorder``: block-off, takeoff or touchdown observed, or the
    flight started in the air. Accepts ``RecorderEvent`` objects or their JSON.
    """
    for event in events:
        kind = getattr(event, "kind", None)
        data = getattr(event, "data", None)
        if kind is None and isinstance(event, Mapping):
            kind, data = event.get("kind"), event.get("data")
        kind = getattr(kind, "value", kind)
        if kind in (EventKind.BLOCK_OFF.value, EventKind.TAKEOFF.value, EventKind.TOUCHDOWN.value):
            return True
        if kind == EventKind.PHASE_CHANGED.value and isinstance(data, Mapping) \
                and data.get("reason") == "started in the air":
            return True
        if kind == EventKind.FLIGHT_ENDED.value and isinstance(data, Mapping) and data.get("loggable") is True:
            return True
    return False


def _field(value: Any, origin: str, status: str, reason: str, **extra: Any) -> dict[str, Any]:
    out = {"value": value, "origin": origin, "status": status, "reason": reason}
    out.update(extra)
    return out


def _unknown_text(reason: str) -> dict[str, Any]:
    return _field("", "recorder", "unknown", reason)


def _attributed_field(data: Mapping[str, Any] | None, key: str, label: str, *,
                      accept: tuple[str, ...] = ("known",)) -> dict[str, Any]:
    """A Logbook text field from a journalled ``Attributed.to_record_field()``."""
    item = None
    if data is not None:
        fields = data.get("fields")
        item = fields.get(key) if isinstance(fields, Mapping) else data.get("record_field")
    if not isinstance(item, Mapping):
        return _unknown_text(f"No {label} evidence was recorded.")
    status = str(item.get("status") or "unknown")
    value = item.get("value")
    reason = str(item.get("reason") or "")
    provenance = item.get("provenance")
    origin = provenance if provenance in ("observed", "derived") else "recorder"
    if status in accept and isinstance(value, str) and value.strip():
        return _field(value, origin, status, reason, confidence=item.get("confidence"))
    return _field("", "recorder", status, reason or f"The {label} is unknown.",
                  confidence=item.get("confidence"), candidate=value if isinstance(value, str) else None)


def _milestone(record: Mapping[str, Any], kind: EventKind, *, last: bool = False,
               where: Any = None) -> Mapping[str, Any] | None:
    items = _events(record, kind)
    if where is not None:
        items = [item for item in items if where(item)]
    if not items:
        return None
    return items[-1] if last else items[0]


def _time_basis(milestones: Iterable[Mapping[str, Any] | None], header_sim_utc: Any) -> str:
    present = [event for event in milestones if event is not None]
    if present:
        return "simulator" if all(_parse(event.get("sim_utc")) for event in present) else "host"
    return "simulator" if _parse(header_sim_utc) else "host"


def _instant(event: Mapping[str, Any] | None, basis: str) -> str | None:
    if event is None:
        return None
    moment = _parse(event.get("sim_utc") if basis == "simulator" else event.get("host_time"))
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def _iso_or_none(value: Any) -> str | None:
    moment = _parse(value)
    return moment.astimezone(timezone.utc).isoformat() if moment else None


_GAP_REASONS = {
    "started": "Simulator data stopped",
    "ended": "Simulator data was missing",
    "detected": "No simulator data was received",
    "reported": "Black Box Core lost updates",
    "unresolved": "Simulator data was missing when the flight ended",
}


def _gap_from_summary(item: Mapping[str, Any], basis: str) -> dict[str, Any]:
    status = str(item.get("status") or "")
    reason = str(item.get("reason") or _GAP_REASONS.get(status, "Data was missing"))
    if basis == "simulator":
        start, end = _iso_or_none(item.get("started_sim_utc")), _iso_or_none(item.get("ended_sim_utc"))
    else:
        start, end = _iso_or_none(item.get("started_host")), _iso_or_none(item.get("ended_host"))
    return {"reason": reason, "start": start, "end": end, "kind": status or "gap",
            "host_start": _iso_or_none(item.get("started_host")), "host_end": _iso_or_none(item.get("ended_host")),
            "host_s": item.get("host_s"), "credited_s": item.get("credited_s"),
            "dropped_updates": item.get("dropped_updates")}


def _gaps_from_events(record: Mapping[str, Any], basis: str) -> list[dict[str, Any]]:
    """Gaps of a flight that has no final summary (interrupted): from the journalled GAP events."""
    out: list[dict[str, Any]] = []
    open_gap: Mapping[str, Any] | None = None
    for gap in record.get("gaps") or []:
        data = gap.get("data") if isinstance(gap, Mapping) else None
        if not isinstance(data, Mapping) or data.get("reason") == INTERRUPTION_REASON:
            continue
        status = data.get("status")
        if status == "started":
            open_gap = data
            continue
        if status in ("ended", "detected", "reported"):
            out.append(_gap_from_summary(data, basis))
            if status == "ended":
                open_gap = None
    if open_gap is not None:
        item = dict(open_gap)
        item["status"] = "unresolved"
        out.append(_gap_from_summary(item, basis))
    return out


def _last_sim_utc(record: Mapping[str, Any], until_host: Any) -> str | None:
    """Simulator UTC of the newest journalled event at or before ``until_host`` that has one.

    The last line before an interruption can be a service event without simulator time (an
    airport attribution, a note); the data itself stopped at the newest event that has it.
    """
    limit = _parse(until_host)
    best: tuple[datetime, datetime] | None = None
    candidates = [record.get("last_good_event"), record.get("last_event")]
    for events in (record.get("milestones") or {}).values():
        candidates.extend(events or [])
    for event in candidates:
        if not isinstance(event, Mapping):
            continue
        host, sim = _parse(event.get("host_time")), _parse(event.get("sim_utc"))
        if host is None or sim is None or (limit is not None and host > limit):
            continue
        if best is None or host > best[0]:
            best = (host, sim)
    return best[1].astimezone(timezone.utc).isoformat() if best else None


def _interruption_gap(record: Mapping[str, Any], basis: str) -> dict[str, Any] | None:
    for gap in record.get("gaps") or []:
        data = gap.get("data") if isinstance(gap, Mapping) else None
        if isinstance(data, Mapping) and data.get("reason") == INTERRUPTION_REASON:
            if basis == "simulator":
                start = _iso_or_none(data.get("from_sim_utc")) or _last_sim_utc(record, data.get("from_host_time"))
                end = _iso_or_none(data.get("to_sim_utc"))
            else:
                start, end = _iso_or_none(data.get("from_host_time")), _iso_or_none(data.get("to_host_time"))
            return {"reason": "BBC EFB stopped recording before the flight ended (" +
                              str(data.get("detail") or "application interrupted").rstrip(".") + ")",
                    "start": start, "end": end, "kind": "interrupted",
                    "host_start": _iso_or_none(data.get("from_host_time")),
                    "host_end": _iso_or_none(data.get("to_host_time")), "end_is": data.get("to_is")}
    return None


def _duration_text(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}:{minutes % 60:02d}"


# --------------------------------------------------------------------------------------------
# The Logbook block
# --------------------------------------------------------------------------------------------


def logbook_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Logbook candidate values, with evidence, from a recorded-flight record.

    Works on any record shaped like ``bbc_recorder.recovery.build_record`` output, so the
    Logbook sink can also use it for a record built without :func:`build_recorded_flight`.
    """
    status = str(record.get("status") or "unknown")
    summary = final_summary(record)
    attribution = attributions(record)
    started = record.get("started") if isinstance(record.get("started"), Mapping) else {}
    source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
    simulated = source.get("simulated")

    block_off = _milestone(record, EventKind.BLOCK_OFF)
    takeoff = _milestone(record, EventKind.TAKEOFF)
    landing = _milestone(record, EventKind.TOUCHDOWN, last=True)
    block_on = _milestone(record, EventKind.BLOCK_ON, last=True)
    basis = _time_basis((block_off, takeoff, landing, block_on), started.get("sim_utc"))
    times = {"off_block": _instant(block_off, basis), "takeoff": _instant(takeoff, basis),
             "landing": _instant(landing, basis), "on_block": _instant(block_on, basis)}

    # Durations: only what the evidence supports.
    block_s = airborne_s = None
    duration_notes: list[str] = []
    if summary is not None and summary.get("block_on") and summary.get("block_off") \
            and isinstance(summary.get("block_s"), (int, float)):
        block_s = float(summary["block_s"])
    elif status == "interrupted":
        duration_notes.append("Block time is unknown: the flight was interrupted before block-on.")
    elif summary is not None:
        duration_notes.append(str(summary.get("block_note") or "Block time was not measured."))
    if summary is not None and summary.get("outcome") in ("complete", "abandoned") \
            and not summary.get("started_in_air") and summary.get("takeoffs") \
            and isinstance(summary.get("airborne_s"), (int, float)):
        airborne_s = float(summary["airborne_s"])
    elif summary is not None and summary.get("started_in_air"):
        duration_notes.append("Airborne time is unknown: the flight started in the air.")
    elif status == "interrupted":
        duration_notes.append("Airborne time is unknown: the flight was interrupted.")

    rate = None
    rate_source = None
    if summary is not None:
        for item in reversed(list(summary.get("touchdowns") or [])):
            if isinstance(item, Mapping) and item.get("kind") != "touch_and_go":
                rate, rate_source = item.get("rate_fpm"), item.get("rate_source")
                break
    elif landing is not None:
        rate, rate_source = _data(landing).get("rate_fpm"), _data(landing).get("rate_source")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        rate = None

    if summary is not None:
        gaps = [_gap_from_summary(item, basis) for item in summary.get("gaps") or [] if isinstance(item, Mapping)]
    else:
        gaps = _gaps_from_events(record, basis)
    interruption = _interruption_gap(record, basis)
    if interruption is not None:
        gaps.append(interruption)

    # Fields.
    reference = block_off or takeoff or _milestone(record, EventKind.FLIGHT_STARTED)
    moment = _parse(reference.get("sim_utc") if basis == "simulator" else reference.get("host_time")) \
        if reference is not None else None
    if moment is None:
        moment = _parse(started.get("sim_utc") if basis == "simulator" else started.get("host_time"))
    if moment is None:
        moment = _parse(started.get("host_time"))
        date_basis = "host"
    else:
        date_basis = basis
    if moment is not None:
        flight_date = _field(moment.astimezone(timezone.utc).date().isoformat(), "derived", "known",
                             f"UTC date of {'block-off' if block_off else 'the flight start'} "
                             f"({'simulator' if date_basis == 'simulator' else 'computer'} clock).")
    else:
        flight_date = _field(None, "recorder", "unknown", "No time was recorded for this flight.")
    identity = attribution.get("identity")
    fields = {
        "flight_date": flight_date,
        "aircraft": _attributed_field(identity, "aircraft", "aircraft type", accept=("known", "needs_confirmation")),
        "registration": _attributed_field(identity, "registration", "registration"),
        "callsign": _attributed_field(identity, "callsign", "callsign"),
        "departure": _attributed_field(attribution.get("departure"), "", "departure airport"),
        "arrival": _attributed_field(attribution.get("arrival"), "", "arrival airport"),
        "minutes": (_field(min(_MAX_MINUTES, int(round(block_s / 60))), "derived", "known",
                           "Block time from block-off to block-on, in simulator time.")
                    if block_s is not None else
                    _field(None, "recorder", "unknown", duration_notes[0] if duration_notes
                           else "Block time was not measured.")),
    }
    if status == "interrupted" and not (attribution.get("arrival") or {}).get("record_field"):
        fields["arrival"] = _unknown_text("The flight was interrupted before it arrived.")
    assessment = (attribution.get("arrival") or {}).get("assessment")

    figures: dict[str, Any] = {}
    if summary is not None:
        for key in ("outcome", "end_reason", "trigger", "landings", "bounces", "touch_and_goes", "distance_nm",
                    "distance_bridged_nm", "distance_excluded_nm", "max_alt_msl_ft", "max_sim_rate",
                    "paused_host_s", "active_paused_host_s", "uncredited_gap_s", "block_source",
                    "airborne_source", "block_host_s", "airborne_host_s", "started_in_air", "warnings"):
            if key in summary:
                figures[key] = summary[key]
    else:
        counts = record.get("counts_by_kind") or {}
        figures["touchdowns_recorded"] = counts.get(EventKind.TOUCHDOWN.value, 0)
        figures["takeoffs_recorded"] = counts.get(EventKind.TAKEOFF.value, 0)

    needs_confirmation = _needs_confirmation(fields)
    evidence_warnings = _evidence_warnings(record) + [
        _confirmation_text(name, fields[name]) for name in needs_confirmation]
    text = summary_text(status, fields, block_s, rate, gaps, simulated, figures)
    if evidence_warnings:
        text += " " + " ".join(evidence_warnings)
    return {
        "version": RECORD_LOGBOOK_VERSION,
        "status": status,
        "simulated": simulated if isinstance(simulated, bool) else None,
        "time_basis": basis,
        **times,
        "block_s": block_s,
        "airborne_s": airborne_s,
        "touchdown_rate_fpm": float(rate) if rate is not None else None,
        "touchdown_rate_source": rate_source if rate is not None else None,
        "gaps": gaps,
        "fields": fields,
        "duration_notes": duration_notes,
        "arrival_assessment": assessment if isinstance(assessment, Mapping) else None,
        "attribution": {role: {key: value for key, value in data.items() if key != "role"}
                        for role, data in attribution.items()},
        "figures": figures,
        "evidence_warnings": evidence_warnings,
        # Fields the pilot should confirm: filled from a low-confidence fallback, or left empty
        # with a candidate value (never filled automatically).
        "needs_confirmation": needs_confirmation,
        "summary_text": text,
    }


_FIELD_LABELS = {"aircraft": "Aircraft type", "registration": "Registration", "callsign": "Callsign",
                 "departure": "Departure", "arrival": "Arrival"}


def _needs_confirmation(fields: Mapping[str, Mapping[str, Any]]) -> list[str]:
    out = []
    for name in ("aircraft", "registration", "callsign", "departure", "arrival"):
        item = fields.get(name) or {}
        if item.get("status") == "needs_confirmation" and (item.get("value") or item.get("candidate")):
            out.append(name)
    return out


def _confirmation_text(name: str, item: Mapping[str, Any]) -> str:
    label = _FIELD_LABELS.get(name, name)
    value = item.get("value") or item.get("candidate")
    filled = bool(item.get("value"))
    return (f"{label} needs confirmation: \"{value}\" {'was filled in' if filled else 'is only a candidate'}"
            f" from low-confidence evidence"
            + (f" ({str(item.get('reason')).rstrip('.')})." if item.get("reason") else "."))


def _evidence_warnings(record: Mapping[str, Any]) -> list[str]:
    """Plain-English warnings about evidence the journal itself says is missing."""
    integrity = record.get("integrity") if isinstance(record.get("integrity"), Mapping) else {}
    lost = integrity.get("lost_lines")
    out: list[str] = []
    if isinstance(lost, int) and not isinstance(lost, bool) and lost > 0:
        kinds = integrity.get("lost_kinds") if isinstance(integrity.get("lost_kinds"), Mapping) else {}
        named = ", ".join(str(kind).replace("_", " ") for kind in sorted(kinds))
        out.append(f"Evidence is missing: {lost} recorded event{'s' if lost != 1 else ''} could not be written "
                   f"to the journal{f' ({named})' if named else ''}, so values that depend on "
                   f"{'them' if lost != 1 else 'it'} may be unknown.")
    return out


def summary_text(status: str, fields: Mapping[str, Mapping[str, Any]], block_s: float | None,
                 rate: float | None, gaps: list[Mapping[str, Any]], simulated: Any,
                 figures: Mapping[str, Any]) -> str:
    """One plain-English line for the Logbook's evidence summary."""
    words = {"complete": "Completed flight", "abandoned": "Flight ended without a normal arrival",
             "interrupted": "Interrupted flight", "discarded": "Discarded recording"}
    parts = [words.get(status, "Recorded flight")]
    dep = fields.get("departure", {}).get("value") or "unknown departure"
    arr = fields.get("arrival", {}).get("value") or "unknown arrival"
    parts[0] += f" from {dep} to {arr}"
    details = []
    if block_s is not None:
        details.append(f"block {_duration_text(block_s)}")
    landings = figures.get("landings")
    if isinstance(landings, int):
        details.append(f"{landings} landing{'s' if landings != 1 else ''}")
    if rate is not None:
        details.append(f"touchdown {rate:.0f} fpm")
    if gaps:
        details.append(f"{len(gaps)} data gap{'s' if len(gaps) != 1 else ''}")
    text = parts[0] + (", " + ", ".join(details) if details else "") + "."
    if simulated is True:
        text += " SIMULATED data, not native telemetry."
    elif simulated is not False:
        text += " The data source was not recorded."
    return text


def build_recorded_flight(result: Any, *, flight_id: str, journal_ref: str, journal_sha256: str | None,
                          recovered_host_time: datetime | None) -> dict[str, Any]:
    """``record_builder`` for ``recovery.finalize_closed`` / ``finalize_interrupted``."""
    record = build_record(result, flight_id=flight_id, journal_ref=journal_ref, journal_sha256=journal_sha256,
                          recovered_host_time=recovered_host_time)
    record["summary"] = final_summary(record)
    record["logbook"] = logbook_from_record(record)
    return record


__all__ = [
    "LOGBOOK_FIELDS", "RECORD_LOGBOOK_VERSION", "attributions", "build_recorded_flight", "final_summary",
    "journal_is_loggable", "logbook_from_record", "summary_text",
]
