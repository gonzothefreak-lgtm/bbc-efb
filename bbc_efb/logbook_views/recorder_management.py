"""Black Box Recorder Management: an attention queue, not a second flight library (Q4).

What needs attention (``attention_queue``):

* Recorder flights with data gaps the pilot has not reviewed yet;
* Recorder flights marked interrupted (without a gap that explains it);
* Recorder flights whose evidence block is missing;
* imported flights whose content is invalid or contradictory (record problems);
* files in the flights folder that could not be read at all.

Manual and imported flights are never flagged for ordinary missing telemetry.
Accepting gaps records a review (``acknowledgements`` in the record); the gap and the
missing data stay in the record, and the flight shows "Reviewed, data gaps accepted".
Delete is offered only when the host supplies a delete action (retention policy is an
open owner decision), always behind a confirmation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .live_status import LiveStatus, describe_recorder_status
from .model import FlightView, flight_view, format_date

KIND_TITLES = {
    "gaps": "Data gaps need review",
    "interrupted": "Flight Logging was interrupted",
    "evidence_missing": "Recorder evidence missing",
    "import_review": "Imported record needs review",
    "unreadable": "File could not be read",
    "confirm_fields": "Fields need confirmation",
}
KIND_ORDER = ("unreadable", "evidence_missing", "interrupted", "gaps", "import_review", "confirm_fields")


@dataclass(frozen=True, slots=True)
class AttentionItem:
    kind: str
    title: str
    detail: str
    flight_id: str = ""
    file_name: str = ""
    gap_indexes: tuple[int, ...] = ()
    flight_label: str = ""

    @property
    def can_accept_gaps(self) -> bool:
        return self.kind == "gaps" and bool(self.gap_indexes)


class StaleListing(ValueError):
    """The flight changed after the Black Box list was built; nothing was written."""


@runtime_checkable
class GapAcknowledger(Protocol):
    """What Recorder Management needs from the Logbook store (wired at integration)."""

    def acknowledge_gap(self, flight_id: str, gap_index: int, note: str) -> None: ...


class StoreActions:
    """Adapter from a ``bbc_efb.storage.FlightStore``-like object (duck typed).

    ``store.get(flight_id)`` must return the current flight and
    ``store.acknowledge_gap(flight, gap, note=..., by="pilot")`` must append an acknowledgement.
    ``delete`` is optional: pass a callable ``delete(flight_id)`` only when deletion has an
    approved retention policy (for example a soft delete to a recoverable folder).
    """

    def __init__(self, store: Any, delete: Callable[[str], None] | None = None):
        self.store = store
        self._delete = delete

    def acknowledge_gap(self, flight_id: str, gap_index: int, note: str, *, seen: FlightView | None = None) -> None:
        self.acknowledge_gaps(flight_id, (gap_index,), note, seen=seen)

    def acknowledge_gaps(self, flight_id: str, gap_indexes: Sequence[int], note: str, *,
                         seen: FlightView | None = None,
                         on_accepted: Callable[[int], None] | None = None) -> None:
        """Acknowledge ``gap_indexes`` of one flight.

        ``seen`` is the ``FlightView`` the pilot reviewed. When given, the flight is re-read
        and compared with it first: if the file changed since it was listed (for example a
        sync tool restored another version) ``StaleListing`` is raised and nothing is written,
        so gap positions are never applied to content the pilot did not see. Each write then
        passes the flight returned by the previous one, so the store's own compare-and-write
        check stays in force between the acknowledgements.
        """
        flight = self.store.get(flight_id)
        if seen is not None:
            try:
                current = flight_view(flight)
            except Exception:  # an unshowable record is not what the pilot saw
                current = None
            if current != seen:
                raise StaleListing(
                    "This flight changed on disk after the Black Box list was shown (another window "
                    "or program saved it). The list has been refreshed; review the gaps again.")
        for index in gap_indexes:
            updated = self.store.acknowledge_gap(flight, index, note=note)
            if updated is not None:
                flight = updated
            if on_accepted is not None:
                on_accepted(index)

    def confirm_fields(self, flight_id: str, names: Sequence[str], *, seen: FlightView | None = None,
                       diversion: dict | None = None) -> Any:
        """Record the pilot's confirmation of ``names`` (values unchanged) with ``store.confirm``.

        As for gaps, ``seen`` is the view the pilot checked: if the flight changed on disk
        since, ``StaleListing`` is raised and nothing is written.
        """
        flight = self.store.get(flight_id)
        if seen is not None:
            try:
                current = flight_view(flight)
            except Exception:  # an unshowable record is not what the pilot saw
                current = None
            if current != seen:
                raise StaleListing(
                    "This flight changed on disk after it was shown (another window or program saved "
                    "it). It has been refreshed; check the fields again.")
        return self.store.confirm(flight, tuple(names), diversion=diversion)

    @property
    def can_delete(self) -> bool:
        return self._delete is not None

    def delete_recording(self, flight_id: str) -> None:
        if self._delete is None:
            raise PermissionError("Deleting recordings is not available in this build.")
        self._delete(flight_id)


def _flight_label(view: FlightView) -> str:
    parts = [format_date(view), view.route]
    if view.callsign:
        parts.append(view.callsign)
    if view.aircraft:
        parts.append(view.aircraft)
    return ", ".join(parts)


def attention_items_for(view: FlightView) -> list[AttentionItem]:
    """Attention items for one flight. Manual flights never produce any."""
    label = _flight_label(view)
    items: list[AttentionItem] = []
    if view.source == "recorder":
        if not view.has_evidence:
            items.append(AttentionItem(
                "evidence_missing", KIND_TITLES["evidence_missing"],
                "The record says it came from the Recorder but holds no Recorder evidence. "
                "Its measured values cannot be shown or checked.", view.id, flight_label=label))
        pending = view.unacknowledged_gaps
        if pending:
            reasons = "; ".join(dict.fromkeys(gap.reason for gap in pending))
            noun = "gap" if len(pending) == 1 else "gaps"
            items.append(AttentionItem(
                "gaps", KIND_TITLES["gaps"], f"{len(pending)} {noun} not reviewed: {reasons}",
                view.id, gap_indexes=tuple(gap.index for gap in pending), flight_label=label))
        if view.interrupted and not view.gaps:
            items.append(AttentionItem(
                "interrupted", KIND_TITLES["interrupted"],
                "Flight Logging stopped unexpectedly and the record does not say what is missing. "
                "Review the flight; its saved data is kept.", view.id, flight_label=label))
        if view.field_checks:
            items.append(AttentionItem(
                "confirm_fields", KIND_TITLES["confirm_fields"],
                " ".join(check.text for check in view.field_checks)
                + " Open the flight to confirm them as correct or correct them.", view.id, flight_label=label))
    elif view.source == "imported" and view.problems:
        items.append(AttentionItem(
            "import_review", KIND_TITLES["import_review"], " ".join(view.problems), view.id, flight_label=label))
    return items


def attention_queue(views: Iterable[FlightView], unreadable: Iterable[Any] = ()) -> list[AttentionItem]:
    """Everything needing the pilot's attention, most serious first, newest flight first."""
    items: list[AttentionItem] = []
    ordered = sorted(views, key=lambda v: (v.sort_date, v.updated_at), reverse=True)
    for view in ordered:
        items.extend(attention_items_for(view))
    for entry in unreadable:
        name = getattr(entry, "name", None) or str(entry)
        reason = getattr(entry, "reason", "") or "The file could not be read."
        kind = getattr(entry, "kind", "unreadable")
        detail = reason if kind == "unreadable" else f"{reason} ({kind})"
        items.append(AttentionItem("unreadable", KIND_TITLES["unreadable"], detail, file_name=name, flight_label=name))
    rank = {kind: index for index, kind in enumerate(KIND_ORDER)}
    items.sort(key=lambda item: rank.get(item.kind, len(rank)))  # stable: keeps newest-first inside a kind
    return items


def reviewed_flights(views: Iterable[FlightView]) -> list[FlightView]:
    return [view for view in views if view.status == "reviewed"]


class RecorderManagementPage(QWidget):
    """Recorder status summary, attention queue and review actions."""

    review_requested = Signal(str)        # open Flight Details on the Black Box tab
    diagnostics_requested = Signal()      # Settings > SimConnect > Connection Diagnostics
    flight_logging_settings_requested = Signal()
    records_changed = Signal()            # after an acknowledgement or deletion: reload the Logbook

    def __init__(self, parent=None, *, actions: GapAcknowledger | None = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.actions = actions
        self.items: list[AttentionItem] = []
        self._views: dict[str, FlightView] = {}
        # Dialog hooks (replaced in tests). Return None to cancel.
        self.ask_note: Callable[[AttentionItem], str | None] = self._ask_note
        self.confirm_delete: Callable[[AttentionItem], bool] = self._confirm_delete
        self.show_error: Callable[[str, str], None] = lambda title, text: QMessageBox.warning(self, title, text)

        title = QLabel("Black Box")
        title.setObjectName("heading")
        subtitle = QLabel("Recorder Management: recordings that need your attention. Completed flights are in "
                          "Flight History; per-flight analysis is in Flight Details > Black Box.")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        self.logging_status = QLabel("Flight Logging status is shown in the footer.")
        self.logging_status.setObjectName("muted")
        self.logging_status.setWordWrap(True)
        self.live_headline = QLabel("")
        self.live_headline.setStyleSheet("font-weight: 650;")
        self.live_headline.setWordWrap(True)
        self.live_headline.hide()
        self.live_facts = QLabel("")
        self.live_facts.setObjectName("muted")
        self.live_facts.setWordWrap(True)
        self.live_facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.live_facts.hide()
        self.simulated_note = QLabel("SIMULATED: this data comes from the simulated provider, not from "
                                     "Microsoft Flight Simulator.")
        self.simulated_note.setObjectName("warning")
        self.simulated_note.setWordWrap(True)
        self.simulated_note.hide()
        self.live: LiveStatus = describe_recorder_status(None)
        self.counts = QLabel("")
        self.counts.setWordWrap(True)
        diagnostics = QPushButton("SimConnect diagnostics")
        diagnostics.setToolTip("Opens Settings > SimConnect > Connection Diagnostics")
        diagnostics.clicked.connect(self.diagnostics_requested)
        privacy = QPushButton("Flight Logging && Privacy")
        privacy.clicked.connect(self.flight_logging_settings_requested)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Issue", "Flight or file", "Details", "Kind"])
        self.table.setColumnHidden(3, True)
        self.table.setAccessibleName("Recordings needing attention")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.setTabKeyNavigation(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, max(260, self.table.fontMetrics().averageCharWidth() * 46))
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.cellDoubleClicked.connect(lambda *_: self.review_selected())
        self.empty = QLabel("Nothing needs attention. Recordings with gaps or interruptions will appear here.")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)

        self.review_button = QPushButton("Review")
        self.review_button.setToolTip("Open the flight's Black Box details")
        self.review_button.clicked.connect(self.review_selected)
        self.accept_button = QPushButton("Accept gaps…")
        self.accept_button.setToolTip("Mark the gaps as reviewed. The gaps and the missing data stay in the record.")
        self.accept_button.clicked.connect(self.accept_selected)
        self.delete_button = QPushButton("Delete recording…")
        self.delete_button.clicked.connect(self.delete_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(self.review_button)
        buttons.addWidget(self.accept_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)

        status = QFrame()
        status.setObjectName("card")
        status_layout = QVBoxLayout(status)
        status_layout.setContentsMargins(14, 12, 14, 12)
        heading = QLabel("Flight Logging now")
        heading.setObjectName("cardTitle")
        status_layout.addWidget(heading)
        status_layout.addWidget(self.live_headline)
        status_layout.addWidget(self.logging_status)
        status_layout.addWidget(self.live_facts)
        status_layout.addWidget(self.simulated_note)
        status_layout.addWidget(self.counts)
        links = QHBoxLayout()
        links.addWidget(diagnostics)
        links.addWidget(privacy)
        links.addStretch(1)
        status_layout.addLayout(links)

        queue = QFrame()
        queue.setObjectName("card")
        queue_layout = QVBoxLayout(queue)
        queue_layout.setContentsMargins(14, 12, 14, 12)
        queue_heading = QLabel("Needs attention")
        queue_heading.setObjectName("cardTitle")
        queue_layout.addWidget(queue_heading)
        queue_layout.addWidget(self.empty)
        queue_layout.addWidget(self.table, 1)
        queue_layout.addLayout(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(status)
        layout.addWidget(queue, 1)
        self._sync_buttons()

    # Data ----------------------------------------------------------------------------

    def set_flight_logging_status(self, text: str) -> None:
        """Show the Recorder's own state as text (owned by the Recorder, never by SimConnect)."""
        self.set_recorder_status(text or None)

    def set_recorder_status(self, status: Any) -> LiveStatus:
        """Show the live Recorder status (a RecorderStatus, a footer LoggingStatus, text or None).

        Only the status itself can say Flight Logging is unavailable; a running Recorder is
        always described by its real state.
        """
        live = describe_recorder_status(status)
        self.live = live
        known = bool(live.state)  # a structured status; text or None is shown as one line
        self.live_headline.setText(live.headline + (" (SIMULATED)" if live.simulated
                                                     and "SIMULATED" not in live.headline else ""))
        self.live_headline.setVisible(known)
        colour = {"ok": "#8fe0a6", "busy": "#ffd693", "error": "#ff9b94"}.get(live.tone, "")
        self.live_headline.setStyleSheet("font-weight: 650;" + (f" color: {colour};" if colour else ""))
        self.logging_status.setText(live.detail if known else
                                    f"{live.headline}. {live.detail}" if live.detail else live.headline)
        self.logging_status.setVisible(bool(self.logging_status.text()))
        self.live_facts.setText("\n".join(f"{name}: {value}" for name, value in live.facts))
        self.live_facts.setVisible(bool(live.facts))
        self.simulated_note.setVisible(live.simulated)
        self.live_headline.setAccessibleName(f"Flight Logging now: {live.text}")
        return live

    def set_actions(self, actions: GapAcknowledger | None) -> None:
        """Use another store's actions (the data folder changed), or None when read-only."""
        self.actions = actions
        self._sync_buttons()

    def set_flights(self, views: Sequence[FlightView], unreadable: Iterable[Any] = ()) -> None:
        selected = self.selected_item()
        self._views = {view.id: view for view in views}
        self.items = attention_queue(views, unreadable)
        reviewed = len(reviewed_flights(views))
        recorded = sum(1 for view in views if view.source == "recorder")
        simulated = sum(1 for view in views if view.simulated)
        parts = [f"{len(self.items):,} item{'s' if len(self.items) != 1 else ''} need attention",
                 f"{recorded:,} Recorder flight{'s' if recorded != 1 else ''} in the Logbook",
                 f"{reviewed:,} reviewed with accepted gaps"]
        if simulated:
            parts.append(f"{simulated:,} from simulated data")
        self.counts.setText(". ".join(parts) + ".")
        # Drop the old selection first: a kept row index would now point at a different item
        # (for example the next flight after accepting gaps) that the pilot never chose.
        self.table.clearSelection()
        self.table.setRowCount(len(self.items))
        for row, item in enumerate(self.items):
            values = (item.title, item.flight_label, item.detail, item.kind)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(row, column, cell)
        self.table.resizeRowsToContents()
        self.empty.setVisible(not self.items)
        self.table.setVisible(bool(self.items))
        if selected is not None:
            for row, item in enumerate(self.items):
                if (item.flight_id, item.file_name, item.kind) == (selected.flight_id, selected.file_name, selected.kind):
                    self.table.selectRow(row)
                    break
        self._sync_buttons()

    def selected_item(self) -> AttentionItem | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        row = rows[0].row()
        return self.items[row] if 0 <= row < len(self.items) else None

    def select_row(self, row: int) -> None:
        self.table.selectRow(row)
        self._sync_buttons()

    def _sync_buttons(self):
        item = self.selected_item()
        self.review_button.setEnabled(item is not None and bool(item.flight_id))
        can_ack = item is not None and item.can_accept_gaps and self.actions is not None
        self.accept_button.setEnabled(can_ack)
        if item is not None and item.can_accept_gaps and self.actions is None:
            self.accept_button.setToolTip("Accepting gaps is not available: the Logbook folder is read-only or not open.")
        can_delete = (item is not None and bool(item.flight_id) and self.actions is not None
                      and getattr(self.actions, "can_delete", False))
        self.delete_button.setEnabled(can_delete)
        self.delete_button.setToolTip("" if can_delete else "Deleting recordings is not available in this build.")

    # Actions -------------------------------------------------------------------------

    def review_selected(self):
        item = self.selected_item()
        if item is not None and item.flight_id:
            self.review_requested.emit(item.flight_id)

    def accept_selected(self) -> bool:
        item = self.selected_item()
        if item is None or not item.can_accept_gaps or self.actions is None:
            return False
        note = self.ask_note(item)
        if note is None:
            return False
        done = 0
        stale = False

        def accepted(_index):
            nonlocal done
            done += 1

        try:
            bulk = getattr(self.actions, "acknowledge_gaps", None)
            if callable(bulk):
                # Pass what the pilot reviewed so a changed file is refused, not acknowledged.
                bulk(item.flight_id, item.gap_indexes, note.strip(),
                     seen=self._views.get(item.flight_id), on_accepted=accepted)
            else:
                for index in item.gap_indexes:
                    self.actions.acknowledge_gap(item.flight_id, index, note.strip())
                    done += 1
        except (OSError, ValueError) as error:
            stale = isinstance(error, StaleListing) or type(error).__name__ == "RecordConflict"
            message = str(error) or type(error).__name__
            if done:
                message += f"\n\n{done} of {len(item.gap_indexes)} gaps were accepted before the problem."
            self.show_error("Gaps not accepted", message)
            if done or stale:
                self.records_changed.emit()
            return False
        self.records_changed.emit()
        return True

    def delete_selected(self) -> bool:
        item = self.selected_item()
        if item is None or not item.flight_id or self.actions is None or not getattr(self.actions, "can_delete", False):
            return False
        if not self.confirm_delete(item):
            return False
        try:
            self.actions.delete_recording(item.flight_id)
        except (OSError, ValueError) as error:
            self.show_error("Recording not deleted", str(error) or type(error).__name__)
            return False
        self.records_changed.emit()
        return True

    def _ask_note(self, item: AttentionItem) -> str | None:
        count = len(item.gap_indexes)
        text, ok = QInputDialog.getText(
            self, "Accept data gaps",
            f"Accept {count} gap{'s' if count != 1 else ''} for {item.flight_label}?\n\n"
            "The flight will show “Reviewed, data gaps accepted”. The gaps and the missing data "
            "stay in the record.\n\nOptional note:", QLineEdit.EchoMode.Normal, "")
        return text if ok else None

    def _confirm_delete(self, item: AttentionItem) -> bool:
        answer = QMessageBox.question(
            self, "Delete recording",
            f"Delete the recording for {item.flight_label}?\n\nThis removes the flight from the Logbook.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes


__all__ = [
    "AttentionItem", "GapAcknowledger", "RecorderManagementPage", "StaleListing", "StoreActions", "attention_items_for",
    "attention_queue", "reviewed_flights",
]
