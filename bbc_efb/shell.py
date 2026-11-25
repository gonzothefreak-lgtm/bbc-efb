"""The offline EFB shell and its shared local settings."""

from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QMainWindow, QMessageBox, QSizeGrip, QStackedWidget, QTabWidget, QVBoxLayout,
    QWidget,
)

from bbc_core import ConnectionState, ConsumerAPI

from .logbook import Logbook
from .storage import FlightStore
from .widgets import button, card, label, page, scroll


ICON_PATH = (Path(__file__).resolve().parent.parent / "reference" / "04_REFERENCE_ASSETS"
             / "BBC EFB Teasers" / "Recorder Corner Icon.png")


def status_dot(colour):
    dot = QPixmap(8, 8)
    dot.fill(Qt.GlobalColor.transparent)
    with QPainter(dot) as painter:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(0, 0, 8, 8)
    return QIcon(dot)


class TitleBar(QFrame):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().toggle_maximised()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class EfbWindow(QMainWindow):
    def __init__(self, settings=None, *, connection_reader: ConsumerAPI | None = None,
                 request_connection=None):
        super().__init__()
        # The window owns this consumer handle. The application host owns Core's session.
        self._connection_reader = connection_reader
        self._request_connection = request_connection
        self._last_connection = None
        self.settings = settings if settings is not None else QSettings("Black Box Club", "BBC EFB")
        self.setWindowTitle("BBC EFB")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(480, 460)
        self.resize(1050, 700)

        surface = QWidget()
        surface.setObjectName("surface")
        self.setCentralWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._make_title_bar())

        self.sections = QStackedWidget()
        self.logbook = Logbook()
        self.pages = {
            "Dispatch": scroll(self._make_dispatch()),
            "In-Flight": self._make_in_flight(),
            "Logbook": self.logbook,
            "Settings": self._make_settings(),
        }
        for view in self.pages.values():
            self.sections.addWidget(view)
        layout.addWidget(self.sections, 1)
        layout.addWidget(self._make_footer())
        self._connection_timer = QTimer(self)
        self._connection_timer.setInterval(200)
        self._connection_timer.timeout.connect(self._refresh_connection)
        self._refresh_connection()
        if connection_reader is not None:
            self._connection_timer.start()
        self._show_section("Dispatch")

        folder = self.settings.value("logbook/folder", "", str)
        if folder:
            self._open_folder(Path(folder))

    def _make_title_bar(self):
        bar = TitleBar()
        bar.setObjectName("top")
        self.title_layout = QGridLayout(bar)
        self.title_layout.setContentsMargins(12, 7, 8, 7)
        self.title_layout.setHorizontalSpacing(12)
        self.title_layout.setVerticalSpacing(5)
        self.title_layout.setColumnStretch(0, 1)
        self.title_layout.setColumnStretch(2, 1)

        brand = QWidget()
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(8)
        icon = label("")
        icon.setFixedWidth(40)
        icon.setAccessibleName("Black Box Club flight recorder")
        artwork = QPixmap(str(ICON_PATH))
        if not artwork.isNull():
            scale = self.devicePixelRatioF()
            artwork = artwork.scaledToWidth(round(40 * scale), Qt.TransformationMode.SmoothTransformation)
            artwork.setDevicePixelRatio(scale)
            icon.setPixmap(artwork)
            brand_layout.addWidget(icon)
        brand_layout.addWidget(label("BBC EFB", "brand"))
        brand_layout.addStretch()

        self.navigation = QWidget()
        nav_layout = QHBoxLayout(self.navigation)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(5)
        self.navigation_group = QButtonGroup(self)
        self.section_buttons = {}
        for name in ("Dispatch", "In-Flight", "Logbook", "Settings"):
            control = button(name, lambda checked=False, section=name: self._show_section(section), "navigation")
            control.setCheckable(True)
            self.navigation_group.addButton(control)
            self.section_buttons[name] = control
            if name != "Settings":
                nav_layout.addWidget(control)

        utilities = QWidget()
        utility_layout = QHBoxLayout(utilities)
        utility_layout.setContentsMargins(0, 0, 0, 0)
        utility_layout.setSpacing(4)
        utility_layout.addWidget(self.section_buttons["Settings"])
        minimise = button("−", self.showMinimized, "window")
        self.maximise = button("□", self.toggle_maximised, "window")
        close = button("×", self.close, "close")
        for control, name in ((minimise, "Minimise"), (self.maximise, "Maximise"), (close, "Close")):
            control.setAccessibleName(name)
            control.setToolTip(name)
            control.setFixedSize(29, 29)
            utility_layout.addWidget(control)

        self.title_layout.addWidget(brand, 0, 0)
        self.title_layout.addWidget(utilities, 0, 2, Qt.AlignmentFlag.AlignRight)
        self._narrow_title = None
        self._layout_title(self.width())
        return bar

    def _layout_title(self, width):
        narrow = width < 650
        if narrow == self._narrow_title:
            return
        self._narrow_title = narrow
        self.title_layout.removeWidget(self.navigation)
        if narrow:
            self.title_layout.addWidget(self.navigation, 1, 0, 1, 3, Qt.AlignmentFlag.AlignLeft)
        else:
            self.title_layout.addWidget(self.navigation, 0, 1, Qt.AlignmentFlag.AlignHCenter)

    def _make_dispatch(self):
        view, layout = page()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(label("Dispatch", "heading"))
        placeholder, _ = card("Dispatch is in development")
        layout.addWidget(placeholder)
        self.folder_prompt, content = card("Logbook data folder", "Choose where your flight records will be saved on this PC.")
        content.addWidget(button("Choose Logbook folder", self._choose_folder, "primary"), alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.folder_prompt)
        layout.addStretch()
        return view

    def _make_in_flight(self):
        view = QWidget()
        layout = QHBoxLayout(view)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        content, body = page()
        body.addWidget(label("In-Flight", "heading"))
        self.module_preview = QStackedWidget()
        empty, _ = card("Choose a module", "Select a module on the right to preview its panel.")
        self.module_preview.addWidget(empty)
        body.addWidget(self.module_preview)
        body.addStretch()
        layout.addWidget(scroll(content), 1)

        launcher, controls = card("Modules")
        launcher.setObjectName("sidebar")
        launcher.setMaximumWidth(185)
        choices = QButtonGroup(launcher)
        for name in ("Aircraft Status", "Radios"):
            placeholder, _ = card(name, "Preview only. This module is in development.")
            index = self.module_preview.addWidget(placeholder)
            control = button(name, lambda checked=False, item=index: self.module_preview.setCurrentIndex(item))
            control.setCheckable(True)
            choices.addButton(control)
            controls.addWidget(control)
        controls.addStretch()
        layout.addWidget(launcher)
        return view

    def _make_settings(self):
        view, layout = page()
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(label("Settings", "heading"))
        self.settings_tabs = QTabWidget()
        layout.addWidget(self.settings_tabs, 1)

        data, data_layout = page()
        data_layout.setContentsMargins(12, 12, 12, 12)
        folder_card, folder_layout = card("Logbook data folder")
        self.folder_label = label("No local folder selected", "muted", wrap=True)
        folder_layout.addWidget(self.folder_label)
        folder_layout.addWidget(button("Choose Logbook folder", self._choose_folder, "primary"),
                                alignment=Qt.AlignmentFlag.AlignLeft)
        data_layout.addWidget(folder_card)
        tools, _ = card("Import, export and backup", "These tools are in development.")
        data_layout.addWidget(tools)
        time, time_layout = card("Time display")
        self.time_zone = QComboBox()
        self.time_zone.setAccessibleName("Time display")
        self.time_zone.addItem("Zulu (UTC)", "utc")
        self.time_zone.addItem("Local time", "local")
        saved_zone = self.settings.value("display/time-zone", "utc", str)
        self.time_zone.setCurrentIndex(1 if saved_zone == "local" else 0)
        self.logbook.use_local_time = self.time_zone.currentData() == "local"
        self.time_zone.currentIndexChanged.connect(self._change_time_zone)
        time_layout.addWidget(self.time_zone, alignment=Qt.AlignmentFlag.AlignLeft)
        data_layout.addWidget(time)
        data_layout.addStretch()
        self.settings_tabs.addTab(scroll(data), "Data Management")

        sim, sim_layout = page()
        sim_layout.setContentsMargins(12, 12, 12, 12)
        diagnostics, diagnostics_layout = card("Connection Diagnostics")
        self.connection_summary = label("", wrap=True)
        self.connection_source = label("", "muted", wrap=True)
        self.connection_detail = label("", "muted", wrap=True)
        for item in (self.connection_summary, self.connection_source, self.connection_detail):
            diagnostics_layout.addWidget(item)
        self.connect_button = button("Connect", self._connect, "primary")
        diagnostics_layout.addWidget(self.connect_button, alignment=Qt.AlignmentFlag.AlignLeft)
        sim_layout.addWidget(diagnostics)
        sim_layout.addStretch()
        self.settings_tabs.addTab(scroll(sim), "SimConnect")

        privacy, privacy_layout = page()
        privacy_layout.setContentsMargins(12, 12, 12, 12)
        retention, _ = card("Flight Logging & Privacy", "Automatic Flight Logging is inactive. Manual records are saved in the local folder you choose. Automatic logging and telemetry retention controls are in development.")
        privacy_layout.addWidget(retention)
        privacy_layout.addStretch()
        self.settings_tabs.addTab(scroll(privacy), "Flight Logging & Privacy")
        return view

    def _make_footer(self):
        footer = QFrame()
        footer.setObjectName("footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(8, 3, 3, 3)
        self.connection_status = button("", lambda: self._show_section("Settings", 1), "status")
        self.logging_status = button("Flight Logging · Inactive", lambda: self._show_section("Settings", 2), "status")
        self.logging_status.setIcon(status_dot("#96a1b0"))
        for control in (self.connection_status, self.logging_status):
            control.setIconSize(QSize(8, 8))
            layout.addWidget(control)
        layout.addStretch()
        self.resize_grip = QSizeGrip(footer)
        layout.addWidget(self.resize_grip, 0, Qt.AlignmentFlag.AlignBottom)
        return footer

    def _refresh_connection(self):
        connection = self._connection_reader.connection() if self._connection_reader is not None else None
        if connection is not None and connection == self._last_connection:
            return
        self._last_connection = connection
        state = connection.state if connection is not None else ConnectionState.DISCONNECTED
        status, colour, summary = {
            ConnectionState.DISCONNECTED: ("Disconnected", "#96a1b0", "SimConnect is disconnected."),
            ConnectionState.CONNECTING: ("Connecting", "#f2ad59", "Black Box Core is opening a connection."),
            ConnectionState.CONNECTED: ("Connected", "#60c89c", "Black Box Core has an active connection."),
            ConnectionState.ERROR: ("Error · Connection failed", "#ff776f", "Black Box Core could not open or maintain the connection."),
        }[state]
        if connection is not None and connection.source.simulated:
            status += " (simulated)"
            summary = "Simulated connection. " + summary
        self.connection_status.setText(f"SimConnect · {status}")
        self.connection_status.setIcon(status_dot(colour))
        self.connection_status.setToolTip(summary)
        self.connection_summary.setText(summary)
        source = connection.source.name if connection is not None else ""
        self.connection_source.setText(f"Provider: {source}" if source else "")
        self.connection_source.setVisible(bool(source))
        detail = connection.detail if connection is not None else "No simulator provider is configured."
        self.connection_detail.setText(detail)
        self.connection_detail.setVisible(bool(detail))
        can_connect = connection is not None and state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR)
        self.connect_button.setVisible(self._request_connection is not None and can_connect)
        self.connect_button.setEnabled(can_connect)
        self.connect_button.setText("Retry connection" if connection is not None and connection.detail else "Connect")

    def _connect(self):
        self.connect_button.setEnabled(False)
        self._request_connection()

    def _show_section(self, name, settings_tab=None):
        current = self.sections.currentWidget()
        if current is self.logbook and name != "Logbook":
            if not self.logbook.can_leave_editor():
                self.section_buttons["Logbook"].setChecked(True)
                return
            if self.logbook.stack.currentWidget() is self.logbook.pages["Editor"]:
                self.logbook.stack.setCurrentWidget(self.logbook.pages[self.logbook.active_section])
        self.sections.setCurrentWidget(self.pages[name])
        self.section_buttons[name].setChecked(True)
        if settings_tab is not None:
            self.settings_tabs.setCurrentIndex(settings_tab)

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose Logbook data folder", self.settings.value("logbook/folder", "", str))
        if folder:
            self._open_folder(Path(folder))

    def _open_folder(self, folder):
        try:
            if not folder.is_dir():
                raise ValueError("The selected folder is unavailable.")
            store = FlightStore(folder)
        except (OSError, ValueError) as error:
            self.folder_label.setText(f"Could not open {folder}\n{error}")
            if self.isVisible():
                QMessageBox.warning(self, "Logbook folder unavailable", str(error))
            return
        self.settings.setValue("logbook/folder", str(folder))
        self.settings.sync()
        self.folder_label.setText(str(folder))
        self.folder_prompt.hide()
        self.logbook.refresh(store)

    def _change_time_zone(self):
        mode = self.time_zone.currentData()
        self.settings.setValue("display/time-zone", mode)
        self.settings.sync()
        self.logbook.use_local_time = mode == "local"
        if self.logbook.selected is not None and self.logbook.stack.currentWidget() is self.logbook.pages["Details"]:
            self.logbook.open_details(self.logbook.selected)

    def toggle_maximised(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "navigation"):
            self._layout_title(event.size().width())

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "resize_grip"):
            maximised = self.isMaximized()
            self.resize_grip.setVisible(not maximised)
            self.maximise.setText("❐" if maximised else "□")
            self.maximise.setAccessibleName("Restore" if maximised else "Maximise")
            self.maximise.setToolTip(self.maximise.accessibleName())

    def closeEvent(self, event):
        if not self.logbook.can_leave_editor():
            event.ignore()
            return
        super().closeEvent(event)
        if event.isAccepted() and self._connection_reader is not None:
            self._connection_timer.stop()
            self._connection_reader.close()
            self._connection_reader = None
            self._refresh_connection()
