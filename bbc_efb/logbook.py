"""Flight entry, History, record details and record-based summaries."""

from collections import Counter
from datetime import datetime, timezone

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QBoxLayout, QButtonGroup, QComboBox, QCompleter, QDateEdit, QDialog, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout,
    QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .aircraft import aircraft_model_names, aircraft_profile
from .dashboard import AircraftPie, CHART_COLOURS, Dashboard
from .storage import FlightFields
from .widgets import aircraft_picture, button, card, label, page, scroll, table


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
            ("planned_departure", "Planned departure", 24), ("planned_arrival", "Planned destination", 24),
            ("actual_arrival", "Confirmed actual arrival", 24), ("real_departure", "Real departure (ISO with offset)", 40),
            ("flight_kind", "Flight type", 40), ("operating_region", "Operating region", 60),
        ):
            field = QLineEdit()
            field.setMaxLength(limit)
            self.text_fields[name] = field
            form.addRow(caption, field)
        aircraft_field = self.text_fields["aircraft"]
        aircraft_names = QCompleter(list(aircraft_model_names()), aircraft_field)
        aircraft_names.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        aircraft_field.setCompleter(aircraft_names)
        self.minutes = QSpinBox()
        self.minutes.setRange(-1, 9999)
        self.minutes.setSpecialValueText("Unknown")
        self.minutes.setSuffix(" min")
        form.addRow("&Duration", self.minutes)
        self.optional_numbers = {}
        for name, caption, suffix, control in (
            ("planned_nm", "Planned distance", " NM", QDoubleSpinBox()),
            ("flown_nm", "Flown distance", " NM", QDoubleSpinBox()),
            ("crew", "Crew", "", QSpinBox()), ("passengers", "Passengers", "", QSpinBox()),
            ("baggage_kg", "Baggage", " kg", QDoubleSpinBox()),
            ("freight_kg", "Freight", " kg", QDoubleSpinBox()),
        ):
            control.setRange(-1, 9999999)
            control.setSpecialValueText("Unknown")
            control.setSuffix(suffix)
            self.optional_numbers[name] = control
            form.addRow(caption, control)
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
        for name, control in self.optional_numbers.items():
            value = getattr(current, name) if current else None
            control.setValue(-1 if value is None else value)
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
            **{name: None if control.value() < 0 else control.value()
               for name, control in self.optional_numbers.items()},
        )


class Logbook(QWidget):
    HEADERS = ("Date", "Aircraft", "From", "To", "Callsign", "Duration")

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings
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
        self.splitter = QSplitter()
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.stack)
        self.section_layout.addWidget(self.splitter, 1)
        self.pages = {}
        self.dashboard = Dashboard(self.new_flight, self.navigate, lambda row: self.open_details(self.flights[row]))
        self.add_page("Dashboard", self.dashboard)
        self.pages["Dashboard"].viewport().installEventFilter(self.dashboard)
        self.history = self.create_history()
        black_box, black_box_layout = page()
        black_box_layout.addWidget(label("Black Box Recorder", "heading"))
        queue, queue_layout = card("Recordings to review")
        self.gap_rows = QVBoxLayout()
        queue_layout.addLayout(self.gap_rows)
        black_box_layout.addWidget(queue)
        black_box_layout.addStretch()
        self.add_page("Black Box Recorder", black_box)
        self.statistics = self.create_statistics()
        self.detail, self.detail_layout = page()
        self.pages["Details"] = scroll(self.detail)
        self.splitter.addWidget(self.pages["Details"])
        self.pages["Details"].hide()
        self.editor = FlightEditor(self.save_flight, self.cancel_edit)
        self.add_page("Editor", self.editor)
        self.refresh(None)
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
        if hasattr(self, "splitter"):
            self.splitter.setOrientation(Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal)
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
        self.pages["Details"].hide()
        self.stack.setCurrentWidget(self.pages[name])

    def create_statistics(self):
        root, layout = page()
        heading = QHBoxLayout()
        heading.addWidget(label("Statistics", "heading"), 1)
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
        chart = AircraftPie()
        breakdown_layout.addWidget(chart)
        rows = QVBoxLayout()
        breakdown_layout.addLayout(rows)
        layout.addWidget(breakdown)
        airports_card, airports_layout = card("Airports visited")
        airports = table(("Airport", "Flights"))
        airports.setMinimumHeight(150)
        airports_layout.addWidget(airports)
        layout.addWidget(airports_card)
        layout.addStretch()
        self.add_page("Statistics", root)
        return warning, metrics_widgets, chart, rows, airports

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
        filters = QHBoxLayout()
        self.history_mode = QComboBox()
        self.history_mode.addItems(("Table", "Cards", "Combined"))
        self.history_mode.setAccessibleName("History view")
        if self.settings is not None:
            self.history_mode.setCurrentText(self.settings.value("logbook/historyView", "Table", type=str))
        self.history_status = QComboBox()
        self.history_status.addItems(("All statuses", "Complete", "Interrupted", "In progress", "Stopped"))
        self.history_source = QComboBox()
        self.history_source.addItems(("All sources", "Manual", "Recorded", "Imported", "Simulated"))
        for control in (self.history_mode, self.history_status, self.history_source):
            control.setAccessibleName(control.accessibleName() or control.currentText())
            filters.addWidget(control)
        filters.addStretch()
        layout.addLayout(filters)
        history_table = table((*self.HEADERS, "Landing rate", "Status"))
        history_table.setObjectName("flightHistory")
        history_table.verticalHeader().setDefaultSectionSize(28)
        history_table.setMinimumHeight(325)
        self.history_open = open_button
        self.history_cards = QListWidget()
        self.history_cards.setAccessibleName("Flight cards")
        self.history_cards.setAlternatingRowColors(True)
        self.history_cards.setMinimumHeight(160)
        history_table.itemSelectionChanged.connect(
            lambda: self._select_history(history_table.currentRow() if history_table.selectedItems() else -1, self.history_cards)
        )
        self.history_cards.currentRowChanged.connect(
            lambda row: self._select_history(row, history_table)
        )
        history_table.cellDoubleClicked.connect(lambda _row, _column: self.open_selected())
        layout.addWidget(history_table, 1)
        self.history_cards.itemDoubleClicked.connect(lambda _item: self.open_selected())
        layout.addWidget(self.history_cards, 1)
        for control in (self.history_mode, self.history_status, self.history_source):
            control.currentTextChanged.connect(self.render_history)
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
        total_flights = sum(flight.status != "interrupted" or flight.fields.visited_arrival not in ("", "UNKNOWN")
                            for flight in self.flights)
        time_text = f"{total_minutes // 60}h {total_minutes % 60:02}m" if durations else "Unknown"
        breakdown = sorted(types.items(), key=lambda pair: (-pair[1], pair[0]))
        self.dashboard.refresh(self.flights, total_flights, warning_text, store is not None, self.load_error)
        warning, metrics, chart, rows, airports = self.statistics
        warning.setVisible(bool(warning_text))
        warning.setText(warning_text)
        for value, metric in zip((total_flights, time_text, len(types)), metrics):
            metric.setText("Unavailable" if self.load_error else str(value))
        self.clear_layout(rows)
        entries = breakdown[:len(CHART_COLOURS) - 1]
        if len(entries) < len(breakdown):
            entries.append(("Other aircraft", sum(count for _, count in breakdown[len(entries):])))
        chart.show_values([count for _, count in entries], f"{len(self.flights)} flights")
        if not types:
            rows.addWidget(label("Saved flights will appear here." if store else "Choose a local data folder in Settings.", "muted", wrap=True))
        for (aircraft, count), colour in zip(entries, CHART_COLOURS):
            row = QHBoxLayout()
            marker = label("")
            marker.setFixedSize(10, 10)
            marker.setStyleSheet(f"background: {colour}; border-radius: 2px")
            row.addWidget(marker)
            row.addWidget(label(aircraft), 1)
            row.addWidget(label(str(count), "muted"))
            rows.addLayout(row)
        visits = Counter(airport for flight in self.flights
                         for airport in {flight.fields.departure, flight.fields.visited_arrival}
                         if airport not in ("", "UNKNOWN"))
        airports.setRowCount(len(visits))
        for row, (airport, count) in enumerate(sorted(visits.items())):
            airports.setItem(row, 0, QTableWidgetItem(airport))
            airports.setItem(row, 1, QTableWidgetItem(str(count)))
        self._render_gaps()
        self.render_history()

    def _render_gaps(self):
        self.clear_layout(self.gap_rows)
        pending = [(flight, gap) for flight in self.flights for gap in flight.pending_gaps]
        if not pending:
            self.gap_rows.addWidget(label(self.load_error or "No recording gaps to review.", "muted", wrap=True))
        for flight, gap in pending:
            summary, contents = card(
                f"{flight.fields.flight_date} · {flight.fields.aircraft}",
                f"{self._sim_timestamp(gap.after)} → {self._sim_timestamp(gap.before)}\n{gap.reason}",
            )
            actions = QHBoxLayout()
            actions.addWidget(button("Open flight", lambda checked=False, saved=flight: self.open_details(saved)))
            actions.addWidget(button("Acknowledge", lambda checked=False, saved=flight, key=gap.review_key:
                                     self._acknowledge_gap(saved.id, key)))
            actions.addStretch()
            contents.addLayout(actions)
            self.gap_rows.addWidget(summary)

    def _acknowledge_gap(self, flight_id, key):
        try:
            saved = self.store.acknowledge_gap(flight_id, key)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not acknowledge recording gap", str(error))
            return
        self.refresh(self.store)
        if self.selected and self.selected.id == flight_id and not self.pages["Details"].isHidden():
            self.open_details(saved)

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
            values = (fields.flight_date, fields.aircraft, fields.departure, fields.visited_arrival,
                      fields.callsign or "—", duration)
            if show_landing_rate:
                rate = flight.landing_rate
                status = Logbook._status(flight)
                values += ("—" if rate is None else f"{rate:g} fpm", status)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(f"{value} ({fields.aircraft_code})" if column == 1 and fields.aircraft_code else value)
                widget.setItem(row, column, item)

    def render_history(self):
        search, history_table, state, add = self.history
        query = search.text().strip().casefold()
        row = history_table.currentRow()
        selected_id = self.visible_flights[row].id if history_table.selectedItems() and 0 <= row < len(self.visible_flights) else None
        mode = self.history_mode.currentText()
        if self.settings is not None:
            self.settings.setValue("logbook/historyView", mode)
        history_table.setVisible(mode != "Cards")
        self.history_cards.setVisible(mode != "Table")
        status = self.history_status.currentText()
        source = self.history_source.currentText()
        self.visible_flights = [
            flight for flight in self.flights
            if (status == "All statuses" or status == self._status(flight))
            and (source == "All sources" or source == self._source(flight))
            and query in " ".join((
                flight.fields.flight_date, flight.fields.aircraft, flight.fields.departure,
                flight.fields.arrival, flight.fields.callsign, flight.fields.registration, flight.fields.aircraft_code,
            )).casefold()
        ]
        positions = (history_table.verticalScrollBar().value(), self.history_cards.verticalScrollBar().value())
        history_table.blockSignals(True)
        self.history_cards.blockSignals(True)
        self.populate(history_table, self.visible_flights, show_landing_rate=True)
        self.history_cards.clear()
        selected_row = -1
        for row, flight in enumerate(self.visible_flights):
            fields = flight.fields
            self.history_cards.addItem(
                f"{fields.departure} → {fields.visited_arrival}   ·   {fields.aircraft}\n"
                f"{fields.flight_date}   ·   {fields.callsign or 'No callsign'}   ·   {self._status(flight)}"
            )
            if flight.id == selected_id:
                history_table.selectRow(row)
                selected_row = row
        self.history_cards.setCurrentRow(selected_row)
        history_table.blockSignals(False)
        self.history_cards.blockSignals(False)
        self.history_open.setEnabled(selected_row >= 0)
        history_table.verticalScrollBar().setValue(positions[0])
        self.history_cards.verticalScrollBar().setValue(positions[1])
        add.setEnabled(self.store is not None and not self.load_error)
        state.setText(
            self.load_error or (f"{len(self.visible_flights)} of {len(self.flights)} saved flights"
                                if self.store else "Choose a local data folder in Settings.")
        )

    @staticmethod
    def _source(flight):
        if flight.recording:
            return "Simulated" if flight.recording.simulated else "Recorded"
        return "Imported" if flight.source == "imported" else "Manual"

    @staticmethod
    def _status(flight):
        if flight.recording is not None and flight.recording.manual_stop:
            return "Stopped"
        if flight.checkpoint is not None and not flight.recording.manual_stop:
            return "In progress"
        return "Complete" if flight.status == "complete" else "Interrupted"

    def _select_history(self, row, other):
        other.blockSignals(True)
        if isinstance(other, QListWidget):
            other.setCurrentRow(row)
        elif row >= 0:
            other.selectRow(row)
        else:
            other.clearSelection()
        other.blockSignals(False)
        self.history_open.setEnabled(row >= 0)

    def open_selected(self):
        row = self.history[1].currentRow()
        if self.history[1].selectedItems() and 0 <= row < len(self.visible_flights):
            self.open_details(self.visible_flights[row])

    def open_details(self, flight):
        if not self.can_leave_editor():
            return
        self.selected = flight
        fields = flight.fields
        self.clear_layout(self.detail_layout)
        top = QHBoxLayout()
        top.addWidget(button("Back", lambda: self.navigate(self.active_section)))
        top.addWidget(label("Flight Details", "heading"), 1)
        top.addWidget(button("Edit flight", lambda: self.start_editor(flight), "primary"))
        self.detail_layout.addLayout(top)
        profile, profile_layout = card(f"{fields.departure}  →  {fields.visited_arrival}")
        profile_layout.addWidget(label(f"{fields.aircraft}  ·  {fields.flight_date}", "metric", wrap=True))
        aircraft = aircraft_profile(fields.aircraft)
        if fields.aircraft_code:
            identity = fields.aircraft_code if aircraft is None else f"{fields.aircraft_code}  ·  {aircraft.role}"
            profile_layout.addWidget(label(identity, "muted"))
        picture = aircraft_picture(fields.aircraft, height=150, width=360)
        if picture is not None:
            profile_layout.addWidget(picture, alignment=Qt.AlignmentFlag.AlignLeft)
        recording = flight.recording
        origin = "Imported flight" if flight.source == "imported" else "Pilot-entered flight"
        if recording is not None:
            origin = "Automatically recorded flight" if recording.manual_start is None else "Recording started by pilot"
            if recording.simulated:
                origin += " (simulated)"
        profile_layout.addWidget(label(origin, "muted"))
        if flight.status == "interrupted":
            state = "Stopped by pilot" if recording and recording.manual_stop else "Interrupted"
            profile_layout.addWidget(label(state, "warning"))
        self.detail_layout.addWidget(profile)
        tabs = QTabWidget()
        self.detail_layout.addWidget(tabs, 1)
        overview, overview_layout = card("Overview")
        for caption, value in (
            ("Callsign", fields.callsign or "Unknown"),
            ("Registration", fields.registration or "Unknown"),
            ("Duration", "Unknown" if fields.minutes is None else f"{fields.minutes} min"),
            ("Planned departure", fields.planned_departure or "Unknown"),
            ("Planned destination", fields.planned_arrival or "Unknown"),
            ("Confirmed actual arrival", fields.actual_arrival or "Unknown"),
            ("Real departure", self._timestamp(fields.real_departure) if fields.real_departure else "Unknown"),
        ):
            overview_layout.addWidget(label(f"{caption}   {value}", wrap=True))
        overview_layout.addStretch()
        tabs.addTab(scroll(overview), "Overview")
        black_box, measurements = card("Flight evidence")
        if recording is not None:
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
            measurements.addWidget(label(f"Source   {recording.provider}", "muted", wrap=True))
            for gap in recording.gaps:
                after = self._sim_timestamp(gap.after)
                before = self._sim_timestamp(gap.before)
                review = " (acknowledged)" if gap.review_key in flight.acknowledged_gaps else ""
                measurements.addWidget(label(f"Gap   {after} → {before}. {gap.reason}{review}", wrap=True))
        rate = flight.landing_rate
        if rate is None:
            measurements.addWidget(label("Landing rate   Unavailable"))
        else:
            rate_source = recording.provider if recording and recording.landing_rate_fpm is not None else flight.reported_landing_source
            measurements.addWidget(label(f"Landing rate   {rate:g} fpm", wrap=True))
            measurements.addWidget(label(f"Landing rate source   {rate_source}", "muted", wrap=True))
        if self.store is not None and recording is not None and recording.touchdown is not None:
            measurements.addWidget(button("View landing replay", lambda: self._show_landing_replay(flight)))
        measurements.addStretch()
        tabs.addTab(scroll(black_box), "Black Box")
        operations, operations_layout = card("Operations")
        for caption, value, unit in (
            ("Flight type", fields.flight_kind, ""), ("Operating region", fields.operating_region, ""),
            ("Planned distance", fields.planned_nm, " NM"), ("Flown distance", fields.flown_nm, " NM"),
            ("Crew", fields.crew, ""), ("Passengers", fields.passengers, ""),
            ("Baggage", fields.baggage_kg, " kg"), ("Freight", fields.freight_kg, " kg"),
        ):
            shown = "Unknown" if value in (None, "") else f"{value}{unit}"
            operations_layout.addWidget(label(f"{caption}   {shown}", wrap=True))
        operations_layout.addStretch()
        tabs.addTab(scroll(operations), "Operations")
        notes, _ = card("Notes", fields.notes or "No notes entered.")
        history, revisions = card("Record History")
        names = {
            "flight_date": "date", "aircraft": "aircraft", "departure": "departure",
            "arrival": "destination", "callsign": "callsign", "registration": "registration",
            "minutes": "duration", "notes": "notes",
            "planned_departure": "planned departure", "planned_arrival": "planned destination",
            "actual_arrival": "confirmed actual arrival", "real_departure": "real departure",
            "flight_kind": "flight type", "operating_region": "operating region",
            "planned_nm": "planned distance", "flown_nm": "flown distance",
            "crew": "crew", "passengers": "passengers", "baggage_kg": "baggage", "freight_kg": "freight",
        }
        previous = None
        for revision in flight.revisions:
            current = revision.fields
            if revision.source:
                summary = f"Source report · {revision.source}"
            elif previous is None:
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
        revisions.addStretch()
        tabs.addTab(scroll(notes), "Notes")
        tabs.addTab(scroll(history), "Record History")
        self.stack.setCurrentWidget(self.pages[self.active_section])
        if self.pages["Details"].isHidden():
            self.pages["Details"].show()
            self.splitter.setSizes([2, 3])
        self.pages["Details"].verticalScrollBar().setValue(0)

    def _show_landing_replay(self, flight):
        try:
            samples = self.store.landing_replay(flight)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            QMessageBox.warning(self, "Landing replay could not be opened", str(error))
            return
        if not samples:
            QMessageBox.information(self, "Landing replay", "No replay samples were saved around this landing.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Landing replay samples")
        dialog.resize(680, 440)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(
            "Samples around the recorded touchdown. Brief contacts or peak forces can fall between samples.",
            "muted", wrap=True,
        ))
        rows = table(("Simulator time", "Ground speed", "Contact", "Latitude", "Longitude"))
        rows.setRowCount(len(samples))
        for row, sample in enumerate(samples):
            values = (
                self._timestamp(sample["sim_time"]), f"{sample['ground_speed']:g} kt",
                "On ground" if sample["on_ground"] else "Airborne",
                f"{sample['latitude']:.5f}", f"{sample['longitude']:.5f}",
            )
            for column, value in enumerate(values):
                rows.setItem(row, column, QTableWidgetItem(value))
        layout.addWidget(rows, 1)
        layout.addWidget(button("Close", dialog.accept))
        dialog.exec()

    def _sim_timestamp(self, value):
        if value is None:
            return "Unknown"
        return self._timestamp(value) + " (simulator time)"

    def _timestamp(self, value):
        timestamp = datetime.fromisoformat(value)
        timestamp = timestamp.astimezone() if self.use_local_time else timestamp.astimezone(timezone.utc)
        return timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")

    def new_flight(self):
        self.start_editor(None)

    def start_editor(self, flight):
        if self.store is None or self.load_error:
            return
        self.editing = flight
        self.pages["Details"].hide()
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
        self.editor.original = self.editor.values()
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
                self.stack.setCurrentWidget(self.pages[self.active_section])
                self.open_details(self.editing)
            else:
                self.stack.setCurrentWidget(self.pages[self.active_section])
