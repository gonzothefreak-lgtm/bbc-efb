"""Startup recovery of flights whose journal was never finished.

On launch the EFB calls ``find_unfinished(data_dir)``. Each ``RecoveredFlight`` says
what survived (events, the last good time) and, in plain English, why the flight is
incomplete. The pilot (or a default rule) then chooses one of:

* ``finalize_interrupted``: close the journal as *interrupted*, with an explicit gap
  from the last good event to the moment of recovery, hand a recorded-flight record to
  the Logbook (``FlightSink``), then archive the journal as evidence.
* ``resume_recording``: reopen the journal and continue the same flight, after an
  explicit gap event (only sensible when the simulator flight is still running).
* ``discard_unfinished``: close it as *discarded* and move it to ``discarded/``. The
  file is kept; nothing here deletes a journal.

Integrity rules (flight-log integrity first):

* An end time is never invented. An interrupted flight's ``ended`` times are ``None``
  (unknown). The gap's ``to`` is the recovery time, labelled as such, because that is
  the only thing actually known: data is missing from the last good line until then.
* The journal is only ever appended to. Torn or damaged bytes stay in the file as
  evidence and are listed in the record's ``integrity`` block.
* A journal whose lock is held by a live writer is never touched (another instance is
  still recording it).
* Order of finalisation is journal footer -> save record -> archive journal. A crash
  between the steps leaves the journal in ``in-progress/`` with its footer, which the
  next start reports as ``CLOSED_NOT_ARCHIVED`` and completes. ``FlightSink`` must
  therefore be idempotent by ``record["flight_id"]`` (save-or-replace).

This module has no Qt and no SimConnect dependency.

PROPOSALS for the owner (defaults here, all configurable):

* P-R1 Recovery is offered, never automatic: ``find_unfinished`` only reads. The EFB
  should ask "Save as interrupted / Resume / Discard". If the pilot dismisses the
  prompt the journal stays in place and is offered again next launch.
* P-R2 The interruption is a ``GAP`` event with ``data.reason == "application_interrupted"``
  spanning last good host time -> recovery host time. Simulator times at the end of
  the gap are unknown (``None``) even if the simulator is running now, because the
  simulator may have been restarted or reloaded in between.
* P-R3 The record's ``status`` is ``"interrupted"`` (journal outcome) and its
  ``ended`` block is all ``None``. Block, airborne and landing values are not computed
  here; ``milestones`` lists the journalled events so the Logbook/Recorder can derive
  what the evidence supports and label the rest unknown.
* P-R3a Exception: a journal without a footer that already holds the flight's
  ``FLIGHT_ENDED`` (complete or abandoned) is closed with that outcome and its journalled
  end times, plus a ``finish_interrupted`` note; no interruption gap is invented.
* P-R4 An empty journal (no events) is still offered; ``discard_unfinished`` is the
  natural choice and keeps the file.
* P-R5 Resume is allowed only for a journal with no footer, and appends a gap event
  first. Whether the EFB offers Resume at all (for example only when the same aircraft
  is still loaded and the simulator time has moved forward) is an owner decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from bbc_recorder.journal import (
    JOURNAL_SUFFIX, LOCK_SUFFIX, FsyncPolicy, Journal, JournalClosedError,
    JournalError, JournalIssue, JournalLayout, JournalReadResult,
    Outcome, _FileLock, _move_closed, archive_target, file_sha256,
    is_locked, read_journal, validate_flight_id,
)
from bbc_recorder.model import EventKind, Phase, RecorderEvent

__all__ = [
    "RECORD_KIND", "RECORD_VERSION", "INTERRUPTION_REASON",
    "RecoveryState", "RecoveredFlight", "FinalizeResult", "FlightSink",
    "find_unfinished", "inspect_journal", "stale_lock_files", "finalize_interrupted", "finalize_closed",
    "resume_recording", "discard_unfinished", "build_record", "journalled_end", "RecoveryError",
    "LOST_LINES_NOTE", "FINISH_INTERRUPTED_NOTE",
]

RECORD_KIND = "bbc.recorded_flight"
RECORD_VERSION = 1
INTERRUPTION_REASON = "application_interrupted"
LOST_LINES_NOTE = "journal_lines_lost"      # service NOTE: the writer's lost-line count before an abandon
FINISH_INTERRUPTED_NOTE = "finish_interrupted"  # recovery NOTE: closed from a journalled FLIGHT_ENDED


class RecoveryError(JournalError):
    """Recovery could not complete; the journal is left where it was."""


class RecoveryState(str, Enum):
    """Why a journal in ``in-progress/`` is not finished."""

    def __str__(self) -> str:
        return self.value

    __format__ = str.__format__

    INTERRUPTED = "interrupted"                  # no closing line: the app stopped mid-flight
    EMPTY = "empty"                              # nothing usable was recorded
    CLOSED_NOT_ARCHIVED = "closed_not_archived"  # closed, but the save/archive step did not finish
    IN_USE = "in_use"                            # a live writer holds it; leave it alone
    UNREADABLE = "unreadable"                    # the file could not be opened


@runtime_checkable
class FlightSink(Protocol):
    """Where a recovered or completed flight record goes (wired to Storage later).

    Must be idempotent by ``record["flight_id"]``: saving the same flight twice
    replaces, never duplicates. Returns the stored record's id.
    """

    def save_recorded_flight(self, record: dict[str, Any]) -> str: ...


@dataclass(frozen=True, slots=True)
class RecoveredFlight:
    flight_id: str
    path: Path
    state: RecoveryState
    reason: str                                  # plain-English, for the pilot
    header: Mapping[str, Any] | None
    footer: Mapping[str, Any] | None
    events: tuple[RecorderEvent, ...]            # empty when found with load_events=False
    notable_events: tuple[RecorderEvent, ...]
    event_count: int
    last_seq: int
    started_host_time: datetime | None
    last_good_host_time: datetime | None         # host time of the last good event (or header)
    last_good_sim_time_s: float | None
    last_good_sim_utc: datetime | None
    last_phase: Phase | None
    torn_tail_bytes: int
    issues: tuple[JournalIssue, ...]
    issue_count: int
    size_bytes: int

    @property
    def recoverable(self) -> bool:
        return self.state in (RecoveryState.INTERRUPTED, RecoveryState.EMPTY,
                              RecoveryState.CLOSED_NOT_ARCHIVED)

    @property
    def aircraft(self) -> Mapping[str, Any] | None:
        value = (self.header or {}).get("aircraft")
        return value if isinstance(value, Mapping) else None


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    flight_id: str
    record: dict[str, Any]
    saved_id: str | None          # what the sink returned, None when no sink was given
    archived_path: Path | None    # None when archive=False
    outcome: Outcome


# --------------------------------------------------------------------------- helpers


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _fmt(moment: datetime | None) -> str:
    if moment is None:
        return "an unknown time"
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
        return f"{moment:%d %b %Y %H:%M:%S} UTC"
    return f"{moment:%d %b %Y %H:%M:%S} (time zone unknown)"


def _integrity_note(result: JournalReadResult) -> str:
    notes = []
    if result.torn_tail_bytes:
        notes.append(f"The final {result.torn_tail_bytes} bytes were an incomplete line and were ignored.")
    other = result.issue_count - (1 if result.torn_tail_bytes else 0)
    if other > 0:
        notes.append(f"{other} other damaged or missing line{'s' if other != 1 else ''} "
                     f"{'were' if other != 1 else 'was'} reported.")
    return (" " + " ".join(notes)) if notes else ""


def _flight_from_result(flight_id: str, result: JournalReadResult, state: str, reason: str,
                        keep_events: bool) -> RecoveredFlight:
    header = result.header
    last = result.last_event
    started = _parse_time((header or {}).get("started_host_time")) or (
        result.first_event.host_time if result.first_event else None)
    last_good = last.host_time if last else _parse_time((header or {}).get("created_host_time"))
    return RecoveredFlight(
        flight_id=flight_id, path=result.path, state=state, reason=reason, header=header,
        footer=result.footer, events=result.events if keep_events else (),
        notable_events=result.notable_events, event_count=result.event_count,
        last_seq=result.last_seq, started_host_time=started, last_good_host_time=last_good,
        last_good_sim_time_s=last.sim_time_s if last else None,
        last_good_sim_utc=last.sim_utc if last else None,
        last_phase=last.phase if last else None, torn_tail_bytes=result.torn_tail_bytes,
        issues=result.issues, issue_count=result.issue_count, size_bytes=result.size_bytes,
    )


def inspect_journal(where: JournalLayout | Path | str, flight_id: str, *,
                    load_events: bool = True) -> RecoveredFlight:
    """Describe one in-progress journal without changing anything."""
    layout = JournalLayout.of(where)
    path = layout.journal_path(flight_id)
    if is_locked(layout.lock_path(flight_id)):
        return RecoveredFlight(
            flight_id=flight_id, path=path, state=RecoveryState.IN_USE,
            reason="This flight is still being recorded by a running BBC EFB, so it was left alone.",
            header=None, footer=None, events=(), notable_events=(), event_count=0, last_seq=-1,
            started_host_time=None, last_good_host_time=None, last_good_sim_time_s=None,
            last_good_sim_utc=None, last_phase=None, torn_tail_bytes=0, issues=(),
            issue_count=0, size_bytes=_size(path))
    try:
        result = read_journal(path, keep_events=load_events)
    except OSError as error:
        return RecoveredFlight(
            flight_id=flight_id, path=path, state=RecoveryState.UNREADABLE,
            reason=f"The journal could not be opened ({error.strerror or error}). It was left in place.",
            header=None, footer=None, events=(), notable_events=(), event_count=0, last_seq=-1,
            started_host_time=None, last_good_host_time=None, last_good_sim_time_s=None,
            last_good_sim_utc=None, last_phase=None, torn_tail_bytes=0, issues=(),
            issue_count=0, size_bytes=_size(path))
    note = _integrity_note(result)
    if result.footer is not None:
        outcome = result.footer.get("outcome", "unknown")
        state = RecoveryState.CLOSED_NOT_ARCHIVED
        reason = (f"The journal was closed ({outcome}) but BBC EFB stopped before the flight "
                  f"was saved to the Logbook and archived.{note}")
    elif result.event_count == 0:
        state = RecoveryState.EMPTY
        reason = ("BBC EFB stopped before anything was recorded for this flight"
                  f"{'' if result.header else ' (not even the opening line survived)'}.{note}")
    else:
        last = result.last_event
        assert last is not None
        sim = f", simulator time {last.sim_time_s:.0f} s" if last.sim_time_s is not None else ""
        reason = (f"BBC EFB stopped while this flight was being recorded. The last good data is "
                  f"from {_fmt(last.host_time)}{sim}, in phase {last.phase.value.replace('_', ' ')}. "
                  f"Nothing after that was recorded.{note}")
        state = RecoveryState.INTERRUPTED
    return _flight_from_result(flight_id, result, state, reason, load_events)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def find_unfinished(data_dir: JournalLayout | Path | str, *, load_events: bool = True,
                    include_in_use: bool = False) -> list[RecoveredFlight]:
    """Every journal left in ``in-progress/``, oldest first. Read-only.

    Journals a live writer holds are left out unless ``include_in_use``. For a very
    long flight, ``load_events=False`` keeps memory bounded (``notable_events``, the
    counts and the last good event are still available).
    """
    layout = JournalLayout.of(data_dir)
    directory = layout.in_progress_dir
    if not directory.is_dir():
        return []
    found: list[RecoveredFlight] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.endswith(JOURNAL_SUFFIX):
            continue
        flight_id = path.name[: -len(JOURNAL_SUFFIX)]
        try:
            validate_flight_id(flight_id)
        except JournalError:
            continue  # not ours
        flight = inspect_journal(layout, flight_id, load_events=load_events)
        if flight.state == RecoveryState.IN_USE and not include_in_use:
            continue
        found.append(flight)
    found.sort(key=lambda f: (f.started_host_time is None,
                              f.started_host_time.timestamp() if f.started_host_time
                              and f.started_host_time.tzinfo else 0.0, f.flight_id))
    return found


def stale_lock_files(data_dir: JournalLayout | Path | str) -> list[Path]:
    """Lock files whose writer is gone and whose journal no longer exists."""
    layout = JournalLayout.of(data_dir)
    if not layout.in_progress_dir.is_dir():
        return []
    stale = []
    for path in layout.in_progress_dir.glob(f"*{LOCK_SUFFIX}"):
        journal = path.with_name(path.name[: -len(LOCK_SUFFIX)] + JOURNAL_SUFFIX)
        if not journal.exists() and not is_locked(path):
            stale.append(path)
    return stale


# --------------------------------------------------------------------------- record


def _event_brief(event: RecorderEvent) -> dict[str, Any]:
    return event.to_json()


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _lost_lines(result: JournalReadResult) -> dict[str, Any] | None:
    """Events the writer reported it could not write: the footer's block, or a journalled note.

    A write that failed before any byte reached the disk leaves nothing for the reader to
    find, so this is the only evidence of it. ``None`` when nothing was reported lost.
    """
    best: dict[str, Any] | None = None
    footer = result.footer or {}
    extra = footer.get("extra") if isinstance(footer.get("extra"), Mapping) else {}
    for footer_lost in (footer.get("lost_lines"), extra.get("lost_lines")):
        if isinstance(footer_lost, Mapping) and _int(footer_lost.get("count")) > _int((best or {}).get("count")):
            best = dict(footer_lost)
    for event in result.notable_events:
        if event.kind is EventKind.NOTE and event.data.get("note") == LOST_LINES_NOTE:
            lost = event.data.get("lost_lines")
            if isinstance(lost, Mapping) and _int(lost.get("count")) > _int((best or {}).get("count")):
                best = dict(lost)
    return best


def build_record(result: JournalReadResult, *, flight_id: str, journal_ref: str,
                 journal_sha256: str | None, recovered_host_time: datetime | None) -> dict[str, Any]:
    """A storage-neutral recorded-flight record built only from journal evidence.

    Nothing is derived beyond what the journal states; unknown values are ``None``.
    The integration phase maps this onto Storage schema v2.
    """
    header = dict(result.header or {})
    footer = dict(result.footer or {})
    outcome = footer.get("outcome")
    first, last = result.first_event, result.last_event
    gaps: list[dict[str, Any]] = []
    milestones: dict[str, list[dict[str, Any]]] = {}
    for event in result.notable_events:
        if event.kind is EventKind.GAP:
            gaps.append({
                "host_time": event.host_time.isoformat(),
                "sim_time_s": event.sim_time_s,
                "phase": event.phase.value,
                "reason": event.data.get("reason"),
                "data": dict(event.data),
            })
        else:
            milestones.setdefault(event.kind.value, []).append(_event_brief(event))
    interruption = next((g for g in gaps if g["reason"] == INTERRUPTION_REASON), None)
    footer_extra = footer.get("extra") if isinstance(footer.get("extra"), Mapping) else {}
    lost = _lost_lines(result)
    lost_count = _int((lost or {}).get("count"))
    issues = [issue.to_json() for issue in result.issues]
    if lost_count:
        kinds = lost.get("kinds") if isinstance(lost.get("kinds"), Mapping) else {}
        named = ", ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in sorted(kinds.items()))
        issues.insert(0, {
            "kind": "write_failed", "line": None, "offset": None, "length": 0,
            "detail": (f"{lost_count} event{'s' if lost_count != 1 else ''} could not be written to the "
                       f"journal{f' ({named})' if named else ''}, so that evidence is missing"
                       + (f": {lost.get('last_error')}" if lost.get("last_error") else ".")),
        })
    return {
        "kind": RECORD_KIND,
        "record_version": RECORD_VERSION,
        "flight_id": flight_id,
        "origin": "recorder",
        "status": outcome or "unknown",
        "app_version": header.get("app_version"),
        "rules_version": header.get("rules_version"),
        "aircraft": header.get("aircraft"),
        "source": header.get("source"),
        "header_extra": header.get("extra") or {},
        "started": {
            "host_time": header.get("started_host_time") or (first.host_time.isoformat() if first else None),
            "sim_time_s": header.get("started_sim_time_s"),
            "sim_utc": header.get("started_sim_utc"),
        },
        "ended": {  # exactly what the footer states; never inferred
            "host_time": footer.get("ended_host_time"),
            "sim_time_s": footer.get("ended_sim_time_s"),
            "sim_utc": footer.get("ended_sim_utc"),
            "final_phase": footer.get("final_phase"),
        },
        "closed_host_time": footer.get("closed_host_time"),
        "close_detail": footer.get("detail"),
        "interruption": None if interruption is None else {
            "from_host_time": interruption["data"].get("from_host_time"),
            "to_host_time": interruption["data"].get("to_host_time"),
            "from_sim_time_s": interruption["data"].get("from_sim_time_s"),
            "to_sim_time_s": interruption["data"].get("to_sim_time_s"),
            "to_is": interruption["data"].get("to_is"),
            "recovered_host_time": recovered_host_time.isoformat() if recovered_host_time else None,
        },
        "last_event": _event_brief(last) if last else None,
        # The last event recorded before any interruption (what the flight really reached).
        "last_good_event": (footer.get("extra") or {}).get("last_good_event", _event_brief(last) if last else None)
        if outcome == Outcome.INTERRUPTED.value else (_event_brief(last) if last else None),
        "event_count": result.event_count,
        "counts_by_kind": dict(result.counts_by_kind),
        "gaps": gaps,
        "milestones": milestones,
        "integrity": {
            "clean": result.clean and not lost_count,
            "issue_count": result.issue_count + (1 if lost_count else 0),
            "issues": issues,
            "torn_tail_bytes": result.torn_tail_bytes,
            "size_bytes": result.size_bytes,
            "lost_lines": lost_count,
            "lost_kinds": dict((lost or {}).get("kinds") or {}) if lost_count else {},
            "writer_error": footer_extra.get("journal_error") if isinstance(footer_extra.get("journal_error"), str)
            else None,
            # Notes and pauses beyond the reader's bound (counted, not listed); flight evidence is never left out.
            "notes_omitted": result.notable_omitted,
            "notes_omitted_by_kind": dict(result.notable_omitted_by_kind),
        },
        "evidence": {"journal": journal_ref, "sha256": journal_sha256},
    }


RecordBuilder = Callable[..., dict[str, Any]]


# --------------------------------------------------------------------------- actions


def _gap_event(result: JournalReadResult, last_good_host: datetime | None, now: datetime,
               detail: str) -> RecorderEvent:
    last = result.last_event
    return RecorderEvent(
        kind=EventKind.GAP,
        phase=last.phase if last else Phase.IDLE,
        host_time=now,
        sim_time_s=None,
        sim_utc=None,
        data={
            "reason": INTERRUPTION_REASON,
            "detail": detail,
            "from_host_time": last_good_host.isoformat() if last_good_host else None,
            "to_host_time": now.isoformat(),
            "to_is": "recovery_time",          # the end of the gap is when recovery ran
            "from_sim_time_s": last.sim_time_s if last else None,
            "from_sim_utc": last.sim_utc.isoformat() if last and last.sim_utc else None,
            "to_sim_time_s": None,             # unknown: never invented
            "to_sim_utc": None,
            "ignored_tail_bytes": result.torn_tail_bytes,
            "ignored_tail_offset": result.good_bytes if result.torn_tail_bytes else None,
        },
    )


def journalled_end(result: JournalReadResult) -> RecorderEvent | None:
    """The FLIGHT_ENDED event of a journal whose flight ended complete or abandoned, else ``None``.

    The service keeps a journal open for a few seconds after FLIGHT_ENDED while the arrival
    airport lookup runs. A journal left without a footer in that window holds a flight that
    really ended, not an interrupted one.
    """
    ended = None
    for event in result.notable_events:
        if event.kind is EventKind.FLIGHT_ENDED:
            ended = event
    if ended is None or str(ended.data.get("outcome") or "") not in (Outcome.COMPLETE.value,
                                                                       Outcome.ABANDONED.value):
        return None
    return ended


def _finalize_locked(layout: JournalLayout, flight_id: str, lock: _FileLock, *,
                     sink: FlightSink | None, now: datetime, archive: bool,
                     record_builder: RecordBuilder, close_if_open: bool,
                     detail: str, footer_extra: Mapping[str, Any] | None = None) -> FinalizeResult:
    path = layout.journal_path(flight_id)
    result = read_journal(path, keep_events=False)
    recovered_at: datetime | None = None
    if result.footer is None:
        if not close_if_open:
            raise RecoveryError(f"The journal for flight {flight_id} has no closing line.")
        last = result.last_event
        last_good = last.host_time if last else _parse_time((result.header or {}).get("created_host_time"))
        ended = journalled_end(result)
        journal = Journal.resume(layout, flight_id, now=lambda: now, _lock=lock)
        try:
            if ended is not None:
                # The flight ended before BBC EFB stopped: close it with its own journalled end
                # (no interruption gap, no invented times) and say what did not finish.
                note = ("BBC EFB stopped after the flight ended, before it finished saving the flight "
                        "(the arrival airport lookup may not have answered); the flight was closed "
                        "from its journalled end.")
                journal.append(RecorderEvent(
                    EventKind.NOTE, ended.phase, now, None, None,
                    {"flight_id": ended.data.get("flight_id", flight_id), "note": FINISH_INTERRUPTED_NOTE,
                     "detail": note, "recovered_host_time": now.isoformat()}))
                journal.close(
                    Outcome(str(ended.data.get("outcome"))), ended_host_time=ended.host_time,
                    ended_sim_time_s=ended.sim_time_s, ended_sim_utc=ended.sim_utc, final_phase=ended.phase,
                    detail=str(ended.data.get("reason") or "") or None,
                    extra={**dict(footer_extra or {}), "closed_by": "recovery", "finish_note": note,
                           "finished_host_time": now.isoformat()},
                )
            else:
                recovered_at = now
                if last is not None or result.header is not None:
                    journal.append(_gap_event(result, last_good, now, detail))
                journal.close(
                    Outcome.INTERRUPTED, ended_host_time=None, ended_sim_time_s=None,
                    ended_sim_utc=None, final_phase=None,
                    detail=detail,
                    extra={**dict(footer_extra or {}), "recovered_host_time": now.isoformat(),
                           "last_good_host_time": last_good.isoformat() if last_good else None,
                           "last_good_event": last.to_json() if last else None},
                )
        finally:
            journal.abandon()  # closes the fd if close() failed; the lock is ours, not its
        result = read_journal(path, keep_events=False)
    outcome = result.outcome or Outcome.INTERRUPTED
    target = archive_target(layout, flight_id, result.header)
    ref = layout.relative(target) if archive else layout.relative(path)
    if recovered_at is None and result.footer is not None:
        recovered_at = _parse_time((result.footer.get("extra") or {}).get("recovered_host_time"))
    record = record_builder(result, flight_id=flight_id, journal_ref=ref,
                            journal_sha256=file_sha256(path), recovered_host_time=recovered_at)
    saved_id: str | None = None
    if sink is not None:
        try:
            saved_id = sink.save_recorded_flight(record)
        except Exception as error:
            raise RecoveryError(
                f"The flight could not be saved to the Logbook ({error}). Its journal was kept "
                "and will be offered again."
            ) from error
    archived: Path | None = None
    if archive:
        archived = _move_closed(layout, flight_id, None, require_footer=True, lock=lock)
        if archived != target:
            record["evidence"]["journal"] = layout.relative(archived)
    return FinalizeResult(flight_id, record, saved_id, archived, outcome)


def finalize_interrupted(flight: RecoveredFlight | str, *, data_dir: JournalLayout | Path | str,
                         sink: FlightSink | None = None, now: datetime | None = None,
                         archive: bool = True, record_builder: RecordBuilder = build_record,
                         detail: str = "BBC EFB stopped before the flight was closed; "
                                       "saved as interrupted at startup.",
                         footer_extra: Mapping[str, Any] | None = None) -> FinalizeResult:
    """Save an unfinished flight as *interrupted* and archive its journal.

    Exception: a journal that already holds the flight's FLIGHT_ENDED event (complete or
    abandoned; the app stopped while the service was still finishing it) is closed with
    that outcome and its journalled end times, plus a note saying the finish did not
    complete. ``footer_extra`` is merged into the footer's ``extra``.

    Appends a ``GAP`` event (last good host time -> now) and an ``interrupted`` footer
    whose end times are ``None``; existing bytes are untouched. For a journal that is
    already closed (``CLOSED_NOT_ARCHIVED``) nothing is appended: the pending save and
    archive are completed. If the sink fails the journal stays in ``in-progress/`` and
    ``RecoveryError`` is raised.

    If ``archive`` changes the file name (an earlier, different archive copy exists),
    ``record["evidence"]["journal"]`` in the returned result is corrected, but the
    sink was given the planned path; this only happens after manual file copying.
    """
    layout = JournalLayout.of(data_dir)
    flight_id = flight.flight_id if isinstance(flight, RecoveredFlight) else validate_flight_id(flight)
    moment = now or _utcnow()
    lock = _FileLock(layout.lock_path(flight_id))
    lock.acquire()
    try:
        return _finalize_locked(layout, flight_id, lock, sink=sink, now=moment, archive=archive,
                                record_builder=record_builder, close_if_open=True, detail=detail,
                                footer_extra=footer_extra)
    finally:
        lock.release()


def finalize_closed(data_dir: JournalLayout | Path | str, flight_id: str, *,
                    sink: FlightSink | None = None, archive: bool = True,
                    record_builder: RecordBuilder = build_record) -> FinalizeResult:
    """Normal end of flight: after ``Journal.close``, save the record then archive.

    Refuses a journal without a footer (use ``finalize_interrupted`` for those).
    """
    layout = JournalLayout.of(data_dir)
    validate_flight_id(flight_id)
    lock = _FileLock(layout.lock_path(flight_id))
    lock.acquire()
    try:
        return _finalize_locked(layout, flight_id, lock, sink=sink, now=_utcnow(), archive=archive,
                                record_builder=record_builder, close_if_open=False, detail="")
    finally:
        lock.release()


def resume_recording(flight: RecoveredFlight | str, *, data_dir: JournalLayout | Path | str,
                     now: datetime | None = None, policy: FsyncPolicy | None = None,
                     detail: str = "BBC EFB stopped during the flight; recording resumed.") -> Journal:
    """Reopen an unfinished journal after an explicit interruption gap. See P-R5.

    Returns the open ``Journal`` (it holds the lock); the Recorder continues appending.
    """
    layout = JournalLayout.of(data_dir)
    flight_id = flight.flight_id if isinstance(flight, RecoveredFlight) else validate_flight_id(flight)
    moment = now or _utcnow()
    journal = Journal.resume(layout, flight_id, policy=policy)
    try:
        result = read_journal(journal.path, keep_events=False)
        last = result.last_event
        last_good = last.host_time if last else _parse_time((result.header or {}).get("created_host_time"))
        journal.append(_gap_event(result, last_good, moment, detail))
        journal.sync()
        return journal
    except BaseException:
        journal.abandon()
        raise


def discard_unfinished(flight: RecoveredFlight | str, *, data_dir: JournalLayout | Path | str,
                       now: datetime | None = None,
                       detail: str = "Discarded by the pilot at startup.") -> Path:
    """Close the journal as *discarded* and move it to ``discarded/``. Never deletes."""
    layout = JournalLayout.of(data_dir)
    flight_id = flight.flight_id if isinstance(flight, RecoveredFlight) else validate_flight_id(flight)
    moment = now or _utcnow()
    lock = _FileLock(layout.lock_path(flight_id))
    lock.acquire()
    try:
        try:
            journal = Journal.resume(layout, flight_id, now=lambda: moment, _lock=lock)
        except JournalClosedError:
            journal = None
        if journal is not None:
            try:
                journal.close(Outcome.DISCARDED, detail=detail,
                              extra={"discarded_host_time": moment.isoformat()})
            finally:
                journal.abandon()
        return _move_closed(layout, flight_id, layout.discarded_dir, require_footer=True, lock=lock)
    finally:
        lock.release()
