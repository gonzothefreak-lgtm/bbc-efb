"""Flight entry, History, record details and record-based summaries."""

from collections import Counter
from datetime import datetime, timezone

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QBoxLayout, QButtonGroup, QDateEdit, QFormLayout, QGridLayout, QHBoxLayout, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QSpinBox, QStackedWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .storage import FlightFields
from .widgets import button, card, label, page, scroll, table


class FlightEditor(QWidget):
    def __init__(self, save, cancel):
        super().__init__()
        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        self.heading = label("", "heading")
        header.addWidget(self.heading, 1)
        header.addWidget(button("Cancel", cancel))
        self.save_button = button("Save flight", save, "primary")
        header.addWidget(self.save_button)
        outer.addLayout(header)

        form_card, contents = card("Flight record", "Aircraft type, From, To and flight date are required.")
        form = QFormLayout()
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("dd MMM yyyy")
        self.date.setDateRange(QDate(1900, 1, 1), QDate(9999, 12, 31))
        form.addRow("&Flight date", self.date)
        self.text_fields = {}
        for name, caption, limit in (
            ("aircraft", "&Aircraft type", 40), ("departure", "&From", 24),
            ("arrival", "&To", 24), ("callsign", "&Callsign", 24),
            ("registration", "&Registration", 24),
        ):
            field = QLineEdit()
            field.setMaxLength(limit)
            self.text_fields[name] = field
            form.addRow(caption, field)
        self.minutes = QSpinBox()
        self.minutes.setRange(-1, 9999)
        self.minutes.setSpecialValueText("Unknown")
        self.minutes.setSuffix(" min")
        form.addRow("&Duration", self.minutes)
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(94)
        form.addRow("&Notes", self.notes)
        contents.addLayout(form)
        outer.addWidget(form_card)
        outer.addStretch()
        self.original = None

    def open(self, flight):
        self.heading.setText("Edit flight" if flight else "New manual flight")
        current = flight.fields if flight else None
        self.date.setDate(
            QDate.fromString(current.flight_date, Qt.DateFormat.ISODate) if current else QDate.currentDate()
        )
        for name, field in self.text_fields.items():
            field.setText(getattr(current, name) if current else "")
        self.minutes.setValue(current.minutes if current and current.minutes is not None else -1)
        self.notes.setPlainText(current.notes if current else "")
        self.original = self.values()
        self.text_fields["aircraft"].setFocus()

    def values(self):
        minutes = self.minutes.value()
        return FlightFields(
            flight_date=self.date.date().toString(Qt.DateFormat.ISODate),
            minutes=None if minutes < 0 else minutes,
            notes=self.notes.toPlainText(),
            **{name: field.text() for name, field in self.text_fields.items()},
        )


class Logbook(QWidget):
    HEADERS = ("Date", "Aircraft", "From", "To", "Callsign", "Duration")

    def __init__(self):
        super().__init__()
        self.store = None
        self.load_error = ""
        self.use_local_time = False
        self.flights = []
        self.selected = None
        self.editing = None
        self.visible_flights = []
        self.active_section = "Dashboard"
        self.section_layout = QHBoxLayout(self)
        self.section_layout.setContentsMargins(12, 12, 12, 12)
        self.sidebar, sidebar_layout = card("Logbook")
        self.sidebar.setObjectName("sidebar")
        self.section_layout.addWidget(self.sidebar)
        self.sidebar_links = QGridLayout()
        sidebar_layout.addLayout(self.sidebar_links)
        self.nav_group = QButtonGroup(self)
        self.nav = {}
        for name in ("Dashboard", "Flight History", "Black Box Recorder", "Statistics"):
            control = button(name, lambda checked=False, section=name: self.navigate(section))
            control.setCheckable(True)
            self.nav_group.addButton(control)
            self.nav[name] = control
        sidebar_layout.addStretch()
        self._narrow_sidebar = None
        self._layout_sidebar(self.width())
        self.stack = QStackedWidget()
        self.section_layout.addWidget(self.stack, 1)
        self.pages = {}
        self.dashboard = self.create_summary("Dashboard", recent=True)
        self.history = self.create_history()
        black_box, black_box_layout = page()
        black_box_layout.addWidget(label("Black Box Recorder", "heading"))
        unavailable, _ = card("Automatic Flight Logging inactive", "This concept stores pilot-entered flights. Simulator telemetry and Recorder events are unavailable.")
        black_box_layout.addWidget(unavailable)
        black_box_layout.addStretch()
        self.add_page("Black Box Recorder", black_box)
        self.statistics = self.create_summary("Statistics")
        self.detail, self.detail_layout = page()
        self.add_page("Details", self.detail)
        self.editor = FlightEditor(self.save_flight, self.cancel_edit)
        self.add_page("Editor", self.editor)
        self.navigate("Dashboard")

    def add_page(self, name, widget):
        frame = scroll(widget)
        self.pages[name] = frame
        self.stack.addWidget(frame)

    def _layout_sidebar(self, width):
        narrow = width < 650
        if narrow == self._narrow_sidebar:
            return
        self._narrow_sidebar = narrow
        self.section_layout.setDirection(QBoxLayout.Direction.TopToBottom if narrow else
                                         QBoxLayout.Direction.LeftToRight)
        self.sidebar.setMinimumWidth(0 if narrow else 174)
        self.sidebar.setMaximumWidth(16777215 if narrow else 174)
        for index, control in enumerate(self.nav.values()):
            self.sidebar_links.removeWidget(control)
            row, column = divmod(index, 2) if narrow else (index, 0)
            self.sidebar_links.addWidget(control, row, column)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_sidebar(event.size().width())

    def navigate(self, name):
        if not self.can_leave_editor():
            self.nav[self.active_section].setChecked(True)
            return
        if name in self.nav:
            self.active_section = name
            self.nav[name].setChecked(True)
        self.stack.setCurrentWidget(self.pages[name])

    def create_summary(self, name, recent=False):
        root, layout = page()
        heading = QHBoxLayout()
        heading.addWidget(label(name, "heading"), 1)
        add = button("New manual flight", self.new_flight, "primary") if recent else None
        if add:
            add.setEnabled(False)
            heading.addWidget(add)
        layout.addLayout(heading)
        warning = label("", "warning", wrap=True)
        warning.hide()
        layout.addWidget(warning)
        metrics = QHBoxLayout()
        metrics_widgets = []
        for caption in ("Total flights", "Time entered", "Aircraft types"):
            metric, metric_layout = card(caption)
            value = label("0", "metric")
            metric_layout.addWidget(value)
            metrics.addWidget(metric, 1)
            metrics_widgets.append(value)
        layout.addLayout(metrics)
        breakdown, breakdown_layout = card("Flights by aircraft")
        rows = QVBoxLayout()
        breakdown_layout.addLayout(rows)
        layout.addWidget(breakdown)
        recent_table = None
        if recent:
            recent_card, recent_layout = card("Recent flights")
            recent_table = table(self.HEADERS)
            recent_table.setMinimumHeight(215)
            recent_table.cellDoubleClicked.connect(
                lambda row, _column: self.open_details(self.flights[row])
            )
            recent_layout.addWidget(recent_table)
            layout.addWidget(recent_card)
        layout.addStretch()
        self.add_page(name, root)
        return warning, metrics_widgets, rows, recent_table, add

    def create_history(self):
        root, layout = page()
        heading = QHBoxLayout()
        heading.addWidget(label("Flight History", "heading"), 1)
        add = button("New manual flight", self.new_flight, "primary")
        heading.addWidget(add)
        layout.addLayout(heading)
        search_row = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Search aircraft, route, callsign or date")
        search.setAccessibleName("Search flight history")
        search.textChanged.connect(self.render_history)
        search_row.addWidget(search, 1)
        open_button = button("Open flight", self.open_selected)
        open_button.setEnabled(False)
        search_row.addWidget(open_button)
        layout.addLayout(search_row)
        history_table = table((*self.HEADERS, "Landing rate", "Status"))
        history_table.setObjectName("flightHistory")
        history_table.verticalHeader().setDefaultSectionSize(28)
        history_table.setMinimumHeight(325)
        history_table.itemSelectionChanged.connect(
            lambda: open_button.setEnabled(history_table.currentRow() >= 0 and bool(history_table.selectedItems()))
        )
        history_table.cellDoubleClicked.connect(lambda _row, _column: self.open_selected())
        layout.addWidget(history_table, 1)
        state = label("", "muted", wrap=True)
        layout.addWidget(state)
        self.add_page("Flight History", root)
        return search, history_table, state, add

    def refresh(self, store):
        if self.store is not None and store is not None and self.store.root != store.root:
            self.selected = None
            self.editing = None
            self.navigate("Dashboard")
        self.store = store
        self.load_error = ""
        try:
            self.flights, unreadable = store.listing() if store else ([], [])
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            self.flights, unreadable = [], []
            self.load_error = f"Logbook could not be opened: {error}"
        warning_text = self.load_error
        if unreadable:
            warning_text = "Some flight files could not be read: " + ", ".join(unreadable)
        types = Counter(flight.fields.aircraft for flight in self.flights)
        durations = [flight.fields.minutes for flight in self.flights if flight.fields.minutes is not None]
        total_minutes = sum(durations)
        total_flights = sum(flight.status != "interrupted" or flight.fields.arrival not in ("", "UNKNOWN")
                            for flight in self.flights)
        time_text = f"{total_minutes // 60}h {total_minutes % 60:02}m" if durations else "Unknown"
        for warning, metrics, rows, recent_table, add in (self.dashboard, self.statistics):
            warning.setVisible(bool(warning_text))
            warning.setText(warning_text)
            for value, metric in zip((total_flights, time_text, len(types)), metrics):
                metric.setText("Unavailable" if self.load_error else str(value))
            self.clear_layout(rows)
            if not types:
                rows.addWidget(label("Saved flights will appear here." if store else "Choose a local data folder in Settings.", "muted", wrap=True))
            for aircraft, count in sorted(types.items(), key=lambda pair: (-pair[1], pair[0])):
                row = QHBoxLayout()
                row.addWidget(label(aircraft), 1)
                bar = QProgressBar()
                bar.setRange(0, len(self.flights))
                bar.setValue(count)
                bar.setTextVisible(False)
                bar.setAccessibleName(f"{aircraft}: {count} of {len(self.flights)} flights")
                row.addWidget(bar, 3)
                row.addWidget(label(str(count), "muted"))
                rows.addLayout(row)
            if recent_table is not None:
                self.populate(recent_table, self.flights[:5])
            if add:
                add.setEnabled(store is not None and not self.load_error)
        self.render_history()

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                Logbook.clear_layout(item.layout())
                item.layout().deleteLater()

    @staticmethod
    def populate(widget, flights, *, show_landing_rate=False):
        widget.clearSelection()
        widget.setRowCount(len(flights))
        for row, flight in enumerate(flights):
            fields = flight.fields
            duration = "Unknown" if fields.minutes is None else f"{fields.minutes} min"
            values = (fields.flight_date, fields.aircraft, fields.departure, fields.arrival,
                      fields.callsign or "—", duration)
            if show_landing_rate:
                rate = flight.recording.landing_rate_fpm if flight.recording is not None else None
                status = "Stopped" if flight.recording and flight.recording.manual_stop else flight.status.capitalize()
                values += ("—" if rate is None else f"{rate:g} fpm", status)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                widget.setItem(row, column, item)

    def render_history(self):
        search, history_table, state, add = self.history
        query = search.text().strip().casefold()
        selected_id = self.selected.id if self.selected else None
        self.visible_flights = [
            flight for flight in self.flights
            if query in " ".join((
                flight.fields.flight_date, flight.fields.aircraft, flight.fields.departure,
                flight.fields.arrival, flight.fields.callsign, flight.fields.registration,
            )).casefold()
        ]
        self.populate(history_table, self.visible_flights, show_landing_rate=True)
        for row, flight in enumerate(self.visible_flights):
            if flight.id == selected_id:
                history_table.selectRow(row)
                break
        add.setEnabled(self.store is not None and not self.load_error)
        state.setText(
            self.load_error or (f"{len(self.visible_flights)} of {len(self.flights)} saved flights"
                                if self.store else "Choose a local data folder in Settings.")
        )

    def open_selected(self):
        row = self.history[1].currentRow()
        if self.history[1].selectedItems() and 0 <= row < len(self.visible_flights):
            self.open_details(self.visible_flights[row])

    def open_details(self, flight):
        self.selected = flight
        fields = flight.fields
        self.clear_layout(self.detail_layout)
        top = QHBoxLayout()
        top.addWidget(button("Back", lambda: self.navigate(self.active_section)))
        top.addWidget(label("Flight Details", "heading"), 1)
        top.addWidget(button("Edit flight", lambda: self.start_editor(flight), "primary"))
        self.detail_layout.addLayout(top)
        profile, profile_layout = card(f"{fields.departure}  →  {fields.arrival}")
        profile_layout.addWidget(label(f"{fields.aircraft}  ·  {fields.flight_date}", "metric", wrap=True))
        recording = flight.recording
        origin = "Pilot-entered flight"
        if recording is not None:
            origin = "Automatically recorded flight" if recording.manual_start is None else "Recording started by pilot"
            if recording.simulated:
                origin += " (simulated)"
        profile_layout.addWidget(label(origin, "muted"))
        if flight.status == "interrupted":
            state = "Stopped by pilot" if recording and recording.manual_stop else "Interrupted"
            profile_layout.addWidget(label(state, "warning"))
        self.detail_layout.addWidget(profile)
        overview, overview_layout = card("Overview")
        for caption, value in (
            ("Callsign", fields.callsign or "Unknown"),
            ("Registration", fields.registration or "Unknown"),
            ("Duration", "Unknown" if fields.minutes is None else f"{fields.minutes} min"),
        ):
            overview_layout.addWidget(label(f"{caption}   {value}", wrap=True))
        self.detail_layout.addWidget(overview)
        if recording is not None:
            black_box, measurements = card("Recorded flight")
            for caption, value in (
                ("Gate out", recording.gate_out), ("Takeoff", recording.takeoff),
                ("Touchdown", recording.touchdown), ("Gate in", recording.gate_in),
            ):
                measurements.addWidget(label(f"{caption}   {self._sim_timestamp(value)}", wrap=True))
            if recording.manual_start is not None:
                measurements.addWidget(label(f"Logging started   {self._sim_timestamp(recording.manual_start)}", wrap=True))
            if recording.manual_stop is not None:
                measurements.addWidget(label(f"Logging stopped   {self._sim_timestamp(recording.manual_stop)}", wrap=True))
            block_label = "Gate-to-gate" if recording.manual_start is None and recording.gate_in else "Captured time"
            air_label = "Airborne captured" if recording.manual_start_airborne or not recording.touchdown else "Airborne"
            measurements.addWidget(label(f"{block_label}   {recording.block_seconds:g} sec"))
            measurements.addWidget(label(f"{air_label}   {recording.airborne_seconds:g} sec"))
            rate = recording.landing_rate_fpm
            measurements.addWidget(label(
                "Landing rate   Unavailable" if rate is None else f"Landing rate   {rate:g} fpm",
            ))
            measurements.addWidget(label(f"Source   {recording.provider}", "muted", wrap=True))
            for gap in recording.gaps:
                after = self._sim_timestamp(gap.after)
                before = self._sim_timestamp(gap.before)
                measurements.addWidget(label(f"Gap   {after} → {before}. {gap.reason}", wrap=True))
            self.detail_layout.addWidget(black_box)
        notes, _ = card("Notes", fields.notes or "No notes entered.")
        history, revisions = card("Record History")
        names = {
            "flight_date": "date", "aircraft": "aircraft", "departure": "departure",
            "arrival": "destination", "callsign": "callsign", "registration": "registration",
            "minutes": "duration", "notes": "notes",
        }
        previous = None
        for revision in flight.revisions:
            current = revision.fields
            if previous is None:
                summary = "Automatic record created" if recording is not None else "Manual record created"
            else:
                summary = "Flight corrected"
            saved_at = datetime.fromisoformat(revision.saved_at)
            shown = saved_at.astimezone() if self.use_local_time else saved_at.astimezone(timezone.utc)
            timestamp = shown.strftime("%Y-%m-%d %H:%M:%S %Z")
            revisions.addWidget(label(f"{timestamp}  ·  {summary}", "muted", wrap=True))
            if previous is not None:
                for name, display in names.items():
                    before, after = getattr(previous, name), getattr(current, name)
                    if before != after:
                        old = "Unknown" if before is None or before == "" else before
                        new = "Unknown" if after is None or after == "" else after
                        revisions.addWidget(label(f"{display.capitalize()}: {old} → {new}", wrap=True))
            previous = current
        self.detail_layout.addWidget(notes)
        self.detail_layout.addWidget(history)
        self.detail_layout.addStretch()
        self.stack.setCurrentWidget(self.pages["Details"])
        self.pages["Details"].verticalScrollBar().setValue(0)

    def _sim_timestamp(self, value):
        if value is None:
            return "Unknown"
        timestamp = datetime.fromisoformat(value)
        timestamp = timestamp.astimezone() if self.use_local_time else timestamp.astimezone(timezone.utc)
        return timestamp.strftime("%Y-%m-%d %H:%M:%S %Z") + " (simulator time)"

    def new_flight(self):
        self.start_editor(None)

    def start_editor(self, flight):
        if self.store is None or self.load_error:
            return
        self.editing = flight
        self.stack.setCurrentWidget(self.pages["Editor"])
        self.pages["Editor"].verticalScrollBar().setValue(0)
        self.editor.open(flight)

    def save_flight(self):
        try:
            saved = self.store.save(self.editor.values(), self.editing)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            QMessageBox.warning(self, "Flight save could not finish", str(error))
            return
        self.selected = saved
        self.refresh(self.store)
        self.open_details(saved)

    def can_leave_editor(self):
        if self.stack.currentWidget() is not self.pages["Editor"] or self.editor.values() == self.editor.original:
            return True
        answer = QMessageBox.question(
            self, "Discard unsaved flight changes?", "Your changes have not been saved.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def cancel_edit(self):
        if self.can_leave_editor():
            if self.editing is not None:
                self.open_details(self.editing)
            else:
                self.stack.setCurrentWidget(self.pages[self.active_section])
