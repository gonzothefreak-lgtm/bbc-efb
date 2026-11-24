"""Manual flight entry, History, record details and record-based summaries."""

from collections import Counter

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QButtonGroup, QDateEdit, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox,
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
        self.flights = []
        self.selected = None
        self.editing = None
        self.visible_flights = []
        self.active_section = "Dashboard"
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        sidebar, sidebar_layout = card("Logbook")
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(174)
        layout.addWidget(sidebar)
        self.nav_group = QButtonGroup(self)
        self.nav = {}
        for name in ("Dashboard", "Flight History", "Black Box Recorder", "Statistics"):
            control = button(name, lambda checked=False, section=name: self.navigate(section))
            control.setCheckable(True)
            self.nav_group.addButton(control)
            sidebar_layout.addWidget(control)
            self.nav[name] = control
        sidebar_layout.addStretch()
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
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
        for caption in ("Recorded flights", "Time entered", "Aircraft types"):
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
        history_table = table(self.HEADERS)
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
        self.store = store
        self.flights, unreadable = store.listing() if store else ([], [])
        types = Counter(flight.fields.aircraft for flight in self.flights)
        durations = [flight.fields.minutes for flight in self.flights if flight.fields.minutes is not None]
        total_minutes = sum(durations)
        time_text = f"{total_minutes // 60}h {total_minutes % 60:02}m" if durations else "Unknown"
        for warning, metrics, rows, recent_table, add in (self.dashboard, self.statistics):
            warning.setVisible(bool(unreadable))
            warning.setText(f"{len(unreadable)} unreadable flight file(s) remain in the data folder." if unreadable else "")
            for value, metric in zip((len(self.flights), time_text, len(types)), metrics):
                metric.setText(str(value))
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
                add.setEnabled(store is not None)
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
    def populate(widget, flights):
        widget.clearSelection()
        widget.setRowCount(len(flights))
        for row, flight in enumerate(flights):
            fields = flight.fields
            duration = "Unknown" if fields.minutes is None else f"{fields.minutes} min"
            values = (fields.flight_date, fields.aircraft, fields.departure, fields.arrival,
                      fields.callsign or "—", duration)
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
        self.populate(history_table, self.visible_flights)
        for row, flight in enumerate(self.visible_flights):
            if flight.id == selected_id:
                history_table.selectRow(row)
                break
        add.setEnabled(self.store is not None)
        state.setText(
            f"{len(self.visible_flights)} of {len(self.flights)} saved flights"
            if self.store else "Choose a local data folder in Settings."
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
        profile_layout.addWidget(label("Pilot-entered flight", "muted"))
        self.detail_layout.addWidget(profile)
        overview, overview_layout = card("Overview")
        for caption, value in (
            ("Callsign", fields.callsign or "Unknown"),
            ("Registration", fields.registration or "Unknown"),
            ("Duration", "Unknown" if fields.minutes is None else f"{fields.minutes} min"),
        ):
            overview_layout.addWidget(label(f"{caption}   {value}", wrap=True))
        self.detail_layout.addWidget(overview)
        notes, _ = card("Notes", fields.notes or "No notes entered.")
        history, revisions = card("Record History")
        previous = None
        for revision in flight.revisions:
            current = revision.fields
            if previous is None:
                summary = "Manual record created"
            else:
                names = {
                    "flight_date": "date", "aircraft": "aircraft", "departure": "departure",
                    "arrival": "destination", "callsign": "callsign", "registration": "registration",
                    "minutes": "duration", "notes": "notes",
                }
                summary = "Edited " + ", ".join(
                    display for name, display in names.items()
                    if getattr(previous, name) != getattr(current, name)
                )
            revisions.addWidget(label(f"{revision.saved_at.replace('T', ' ').replace('+00:00', ' UTC')}  ·  {summary}", "muted", wrap=True))
            previous = current
        self.detail_layout.addWidget(notes)
        self.detail_layout.addWidget(history)
        self.detail_layout.addStretch()
        self.stack.setCurrentWidget(self.pages["Details"])
        self.pages["Details"].verticalScrollBar().setValue(0)

    def new_flight(self):
        self.start_editor(None)

    def start_editor(self, flight):
        if self.store is None:
            return
        self.editing = flight
        self.stack.setCurrentWidget(self.pages["Editor"])
        self.pages["Editor"].verticalScrollBar().setValue(0)
        self.editor.open(flight)

    def save_flight(self):
        try:
            saved = self.store.save(self.editor.values(), self.editing)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            QMessageBox.warning(self, "Flight could not be saved", str(error))
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
