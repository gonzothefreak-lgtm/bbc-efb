"""The Logbook Dashboard and its record-based charts."""

from collections import Counter, defaultdict

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QTableWidgetItem, QVBoxLayout, QWidget

from .aircraft import aircraft_profile
from .widgets import aircraft_picture, button, card, label, table


CHART_COLOURS = ("#64B1FF", "#F69955", "#78CDBD", "#A194E0", "#E2BE6F", "#4A83C9", "#EC8C9F")
LANDING_BINS = (
    ("<−300", -300), ("−300", -250), ("−250", -200), ("−200", -150),
    ("−150", -100), ("−100", -50), ("≥−50", 0),
)


def hours(minutes):
    return f"{minutes // 60:,}h {minutes % 60:02}m"


class AircraftPie(QWidget):
    def __init__(self):
        super().__init__()
        self.values = ()
        self.centre = ""
        self.setFixedSize(132, 132)
        self.setAccessibleName("Aircraft breakdown")

    def show_values(self, values, centre):
        self.values = tuple(values)
        self.centre = centre
        self.setVisible(bool(self.values))
        self.update()

    def paintEvent(self, event):
        if not self.values:
            return
        bounds = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        start, cumulative = 0, 0
        total = sum(self.values)
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#142131"), 2))
            for colour, value in zip(CHART_COLOURS, self.values):
                cumulative += value
                end = round(5760 * cumulative / total)
                painter.setBrush(QColor(colour))
                if end == 5760 and start == 0:
                    painter.drawEllipse(bounds)
                elif end > start:
                    painter.drawPie(bounds, 1440 - start, start - end)
                start = end
            if self.centre:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#142131"))
                painter.drawEllipse(bounds.adjusted(27, 27, -27, -27))
                painter.setPen(QColor("#E7EEF7"))
                painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, self.centre)


class LandingHistogram(QWidget):
    def __init__(self):
        super().__init__()
        self.counts = ()
        self.setMinimumHeight(125)
        self.setAccessibleName("Landing rate distribution in feet per minute")

    def show_rates(self, rates):
        counts = [0] * len(LANDING_BINS)
        for rate in rates:
            index = next((i for i, (_, upper) in enumerate(LANDING_BINS) if rate < upper), len(counts) - 1)
            counts[index] += 1
        self.counts = tuple(counts)
        self.setAccessibleDescription(", ".join(f"{caption}: {count}" for (caption, _), count in zip(LANDING_BINS, counts)))
        self.setVisible(bool(rates))
        self.update()

    def paintEvent(self, event):
        if not any(self.counts):
            return
        plot = QRectF(self.rect()).adjusted(1, 7, -1, -24)
        width = plot.width() / len(self.counts)
        peak = max(self.counts)
        with QPainter(self) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor("#344B61"))
            for fraction in (0.0, .25, .5, .75, 1.0):
                y = round(plot.bottom() - fraction * plot.height())
                painter.drawLine(0, y, self.width(), y)
            font = painter.font()
            font.setPixelSize(10)
            painter.setFont(font)
            for index, ((caption, _), count) in enumerate(zip(LANDING_BINS, self.counts)):
                left = plot.left() + index * width
                bar_height = plot.height() * count / peak
                painter.fillRect(QRectF(left + 4, plot.bottom() - bar_height, width - 8, bar_height),
                                 QColor("#78CDBD"))
                painter.setPen(QColor("#A7BBD0"))
                painter.drawText(QRectF(left, plot.bottom() + 3, width, 18), Qt.AlignmentFlag.AlignCenter, caption)


class Dashboard(QWidget):
    def __init__(self, add_flight, navigate, open_recent):
        super().__init__()
        self.navigation = navigate
        self.setObjectName("page")
        self.layout_mode = None
        self.counts = Counter()
        self.minutes_by_aircraft = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(label("Logbook — Dashboard", "heading"), 1)
        self.new_flight = button("New manual flight", add_flight, "primary")
        self.new_flight.setEnabled(False)
        header.addWidget(self.new_flight)
        root.addLayout(header)
        self.warning = label("", "warning", wrap=True)
        self.warning.hide()
        root.addWidget(self.warning)

        self.metric_grid = QGridLayout()
        self.metric_cards = []
        self.metrics = []
        for name in ("Total flights", "Total flight time", "Total distance", "Total landings", "Average landing rate"):
            panel, contents = card(name)
            value = label("—", "metric")
            contents.addWidget(value)
            self.metric_cards.append(panel)
            self.metrics.append(value)
        self.landing_extremes = label("", "muted", wrap=True)
        self.metric_cards[-1].layout().addWidget(self.landing_extremes)
        root.addLayout(self.metric_grid)

        self.body_grid = QGridLayout()
        self.body_grid.setSpacing(12)
        self.aircraft_card, aircraft_layout = card("My Aircraft")
        self.aircraft_grid = QGridLayout()
        aircraft_layout.addLayout(self.aircraft_grid)
        self.aircraft_empty = label("Saved aircraft will appear here.", "muted", wrap=True)
        aircraft_layout.addWidget(self.aircraft_empty)
        aircraft_layout.addStretch()

        self.breakdown_card, breakdown_layout = card()
        breakdown_header = QHBoxLayout()
        self.breakdown_title = label("Flight time by aircraft", "cardTitle", wrap=True)
        breakdown_header.addWidget(self.breakdown_title, 1)
        self.breakdown_mode = QComboBox()
        self.breakdown_mode.setAccessibleName("Aircraft breakdown measure")
        self.breakdown_mode.addItems(("Flight time", "Flights"))
        self.breakdown_mode.currentIndexChanged.connect(self._refresh_breakdown)
        breakdown_header.addWidget(self.breakdown_mode)
        breakdown_layout.addLayout(breakdown_header)
        breakdown = QHBoxLayout()
        self.pie = AircraftPie()
        breakdown.addWidget(self.pie)
        self.legend = QVBoxLayout()
        breakdown.addLayout(self.legend, 1)
        breakdown_layout.addLayout(breakdown)
        self.breakdown_empty = label("No flight time recorded yet.", "muted", wrap=True)
        breakdown_layout.addWidget(self.breakdown_empty)

        self.landing_card, landing_layout = card("Landing rate distribution (fpm)")
        self.histogram = LandingHistogram()
        landing_layout.addWidget(self.histogram)
        self.landing_empty = label("No recorded landing rates yet.", "muted", wrap=True)
        landing_layout.addWidget(self.landing_empty)

        self.types_card, self.types_layout = card("Flight types")
        self.types_chart = AircraftPie()
        self.types_layout.addWidget(self.types_chart)
        self.types_summary = label("", "muted", wrap=True)
        self.types_layout.addWidget(self.types_summary)
        self.regions_card, self.regions_layout = card("Operations by region")
        self.regions_summary = label("", "muted", wrap=True)
        self.regions_layout.addWidget(self.regions_summary)
        root.addLayout(self.body_grid)

        self.bottom_grid = QGridLayout()
        self.bottom_grid.setSpacing(12)
        self.recent_card, recent_layout = card()
        recent_heading = QHBoxLayout()
        recent_heading.addWidget(label("Recent Flights", "cardTitle"), 1)
        recent_heading.addWidget(button("View Flight History", lambda: navigate("Flight History")))
        recent_layout.addLayout(recent_heading)
        self.recent = table(("Date", "Callsign", "From", "To", "Aircraft", "Flight time", "Landing rate"))
        self.recent.setMinimumHeight(181)
        self.recent.verticalHeader().setDefaultSectionSize(28)
        self.recent.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.recent.setColumnWidth(4, 145)
        self.recent.cellDoubleClicked.connect(lambda row, _column: open_recent(row))
        recent_layout.addWidget(self.recent)
        self.quick_card, quick_layout = card("Quick Links")
        for name, destination in (
            ("Flight History", "Flight History"), ("Black Box", "Black Box Recorder"), ("Statistics", "Statistics"),
        ):
            quick_layout.addWidget(button(name, lambda checked=False, page=destination: navigate(page)))
        quick_layout.addStretch()
        root.addLayout(self.bottom_grid)
        root.addStretch()
        self._arrange(0)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self._arrange(event.size().width())
        return super().eventFilter(watched, event)

    def _arrange(self, width):
        mode = 5 if width >= 865 else 3 if width >= 580 else 2
        if self.layout_mode == mode:
            return
        self.layout_mode = mode
        for index, panel in enumerate(self.metric_cards):
            self.metric_grid.removeWidget(panel)
            row, column = divmod(index, mode)
            span = mode - column if index == len(self.metric_cards) - 1 else 1
            self.metric_grid.addWidget(panel, row, column, 1, span)
        for column in range(len(self.metric_cards)):
            self.metric_grid.setColumnStretch(column, int(column < mode))

        panels = (self.aircraft_card, self.breakdown_card, self.landing_card, self.types_card, self.regions_card)
        for panel in panels:
            self.body_grid.removeWidget(panel)
        if mode == 5:
            slots = ((0, 0, 2, 1), (0, 1, 1, 1), (0, 2, 1, 1), (1, 1, 1, 1), (1, 2, 1, 1))
        elif mode == 3:
            slots = ((0, 0, 2, 1), (0, 1, 1, 1), (1, 1, 1, 1), (2, 0, 1, 1), (2, 1, 1, 1))
        else:
            slots = tuple((row, 0, 1, 1) for row in range(len(panels)))
        for panel, slot in zip(panels, slots):
            self.body_grid.addWidget(panel, *slot)
        for column, stretch in enumerate((42, 29, 29) if mode == 5 else (1, 1, 0) if mode == 3 else (1, 0, 0)):
            self.body_grid.setColumnStretch(column, stretch)

        self.bottom_grid.removeWidget(self.recent_card)
        self.bottom_grid.removeWidget(self.quick_card)
        self.bottom_grid.addWidget(self.recent_card, 0, 0)
        self.bottom_grid.addWidget(self.quick_card, 0 if mode == 5 else 1, 1 if mode == 5 else 0)
        self.bottom_grid.setColumnStretch(0, 63 if mode == 5 else 1)
        self.bottom_grid.setColumnStretch(1, 37 if mode == 5 else 0)

    def refresh(self, flights, flight_count, warning, store_ready, load_error):
        self.warning.setText(warning)
        self.warning.setVisible(bool(warning))
        self.new_flight.setEnabled(store_ready and not load_error)
        self.counts = Counter(flight.fields.aircraft for flight in flights)
        minutes = defaultdict(int)
        rates = []
        distances = []
        landings = 0
        for flight in flights:
            fields = flight.fields
            if fields.minutes is not None:
                minutes[fields.aircraft] += fields.minutes
            if fields.flown_nm is not None:
                distances.append(fields.flown_nm)
            if flight.recording is not None:
                landings += flight.recording.touchdown is not None
            if flight.landing_rate is not None:
                rates.append(flight.landing_rate)
        self.minutes_by_aircraft = minutes
        distance = f"{sum(distances):,.0f} NM" if distances else "—"
        values = (f"{flight_count:,}", hours(sum(minutes.values())) if minutes else "—", distance,
                  f"{landings:,}" if landings else "—", f"{sum(rates) / len(rates):.0f} fpm" if rates else "—")
        for metric, value in zip(self.metrics, values):
            metric.setText("—" if load_error else value)
        self.landing_extremes.setText(f"Softest {max(rates):.0f}  ·  Firmest {min(rates):.0f} fpm" if rates else "")
        self.landing_extremes.setVisible(bool(rates))
        self.landing_empty.setVisible(not rates)
        self.histogram.show_rates(rates)
        self._refresh_aircraft()
        self._refresh_breakdown()
        kinds = Counter(flight.fields.flight_kind or "Unknown" for flight in flights)
        regions = Counter(flight.fields.operating_region or "Unknown" for flight in flights)
        entries = kinds.most_common(len(CHART_COLOURS) - 1)
        if len(kinds) > len(entries):
            entries.append(("Other types", len(flights) - sum(count for _, count in entries)))
        self.types_chart.show_values([count for _, count in entries], str(len(flights)))
        self.types_summary.setText(" · ".join(f"{name} {count}" for name, count in entries) or "No flights saved.")
        self.regions_summary.setText("\n".join(f"{name}   {count}" for name, count in regions.most_common()) or "No flights saved.")

        recent = flights[:5]
        self.recent.setRowCount(len(recent))
        for row, flight in enumerate(recent):
            fields = flight.fields
            rate = flight.landing_rate
            data = (fields.flight_date, fields.callsign or "—", fields.departure, fields.visited_arrival, fields.aircraft,
                    hours(fields.minutes) if fields.minutes is not None else "—", f"{rate:g} fpm" if rate is not None else "—")
            for column, value in enumerate(data):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.recent.setItem(row, column, item)

    def _refresh_aircraft(self):
        self._clear_widgets(self.aircraft_grid)
        self.aircraft_empty.setVisible(not self.counts)
        for index, (aircraft, count) in enumerate(self.counts.most_common(4)):
            panel, contents = card()
            panel.setObjectName("aircraftTile")
            picture = aircraft_picture(aircraft, height=76, width=174)
            if picture is not None:
                contents.addWidget(picture)
            contents.addWidget(label(aircraft, "cardTitle", wrap=True))
            profile = aircraft_profile(aircraft)
            if profile is not None:
                contents.addWidget(label(f"{profile.code}  ·  {profile.role}", "muted", wrap=True))
            time = hours(self.minutes_by_aircraft[aircraft]) if aircraft in self.minutes_by_aircraft else "Time unknown"
            contents.addWidget(label(f"{count} {'flight' if count == 1 else 'flights'}  ·  {time}", "muted", wrap=True))
            contents.addStretch()
            panel.setMinimumHeight(82)
            row, column = divmod(index, 2)
            self.aircraft_grid.addWidget(panel, row, column)

    def _refresh_breakdown(self):
        count_mode = self.breakdown_mode.currentIndex() == 1
        data = self.counts if count_mode else self.minutes_by_aircraft
        entries = sorted(((name, value) for name, value in data.items() if value > 0),
                         key=lambda pair: (-pair[1], pair[0]))
        self.breakdown_title.setText("Flights by aircraft" if count_mode else "Flight time by aircraft")
        self.breakdown_empty.setText("No flights recorded yet." if count_mode else "No flight time recorded yet.")
        self.breakdown_empty.setVisible(not entries)
        limit = len(CHART_COLOURS)
        if len(entries) > limit:
            entries = entries[:limit - 1] + [("Other aircraft", sum(value for _, value in entries[limit - 1:]))]
        total = sum(value for _, value in entries)
        self.pie.show_values((value for _, value in entries), hours(total) if not count_mode else "")
        self._clear_widgets(self.legend)
        for colour, (name, value) in zip(CHART_COLOURS, entries):
            entry = QWidget()
            row = QHBoxLayout(entry)
            row.setContentsMargins(0, 0, 0, 0)
            marker = QFrame()
            marker.setFixedSize(9, 9)
            marker.setStyleSheet(f"background: {colour}; border-radius: 2px;")
            row.addWidget(marker)
            row.addWidget(label(name, wrap=True), 1)
            fraction = f"{value / total:.0%}"
            row.addWidget(label(f"{value}  ·  {fraction}" if count_mode else fraction, "muted"))
            self.legend.addWidget(entry)

    @staticmethod
    def _clear_widgets(layout):
        while layout.count():
            item = layout.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
