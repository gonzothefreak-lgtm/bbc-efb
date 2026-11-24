"""The BBC EFB main window: a compact, dark, frameless Windows shell.

Layout, top to bottom:

* ``TitleBar`` (``QFrame#top``): discreet brand at the left, the three primary
  Section tabs (Dispatch, In-Flight, Logbook) in the middle, then Settings and
  the minimise, maximise/restore and close controls. Drag the empty bar to move
  the window (``QWindow.startSystemMove``, so Windows Snap still works); double-
  click it to maximise or restore.
* A ``QStackedWidget`` of Sections. Only Dispatch, the launch Section, is built at
  start-up; every other Section is built the first time it is opened.
* ``Footer`` (``bbc_efb.footer``): SimConnect and Flight Logging, side by side.

Eight invisible ``ResizeGrip`` widgets along the edges and corners call
``QWindow.startSystemResize`` so resizing feels native. They are hidden while the
window is maximised.

Keyboard: Ctrl+1/2/3 open Dispatch, In-Flight and Logbook, Ctrl+, opens Settings,
Alt+D/I/L/S work as mnemonics, and Tab reaches every control. All controls have
accessible names; state is always in words, never only in colour.

When the window is minimised the shell stops its own UI timers and tells the Core
bridge (or host) ``set_ui_active(False)``. Core and the Flight Recorder keep
running, because they do not depend on the window.

The SimConnect status follows ``CoreBridge.connection_changed`` and the Flight
Logging status follows ``CoreBridge.recorder_status_changed`` (or a 1-second poll
when there is no bridge). The two are never merged. When a recorded flight has been
saved the Logbook re-reads the store, and notices such as "flights recovered at
start-up" are shown in a banner under the title bar until dismissed.

Data folder: the app owns the one store (contract C1). The Logbook asks for a folder
with ``data_folder_requested``; the shell picks one and switches through
``DataFolderControl``, which also refuses while a flight is open or while the
Logbook editor has unsaved changes the pilot wants to keep.

Closing while a flight is open asks **Cancel** or **Save & Exit**. Save & Exit asks
the Recorder to save the flight as it is (incomplete, closed by the pilot) and
reports a failure in plain words instead of exiting as if it had worked.

PROPOSALS for the owner (defaults implemented here, each easy to change):

1. Close while logging: Save & Exit saves the open flight as *incomplete*, with
   the reason "EFB closed by the pilot during Flight Logging"; it never marks the
   flight completed. If the save fails or does not finish within 10 seconds, the
   pilot sees the error and can return to the EFB (default) or exit anyway.
2. Manual Stop: confirmation defaults to *Keep logging*, so pressing Enter by
   mistake never ends a flight.
3. Minimised: the footer clock and the 1-second Flight Logging status refresh stop;
   the Core bridge is told the UI is inactive. Flight Logging continues.
4. Settings is a title-bar control, not a fourth tab (UI Standards §2). It is
   highlighted while open.
5. The footer clock shows the PC's UTC time, labelled as such, not simulator time.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from .footer import ActionResult, Footer, LoggingState, LoggingStatus, SAVE_TIMEOUT_S, resolve_action
from .sections import (
    DIAGNOSTICS_PAGE, LAUNCH_SECTION, LOGBOOK, PRIVACY_PAGE, SETTINGS, DataFolderControl,
    SectionLoadFailure, SectionSpec, ShellServices, default_sections, open_settings_page, plain_title,
)

log = logging.getLogger(__name__)

APP_NAME = "BBC EFB"
MINIMUM_SIZE = QSize(760, 520)
DEFAULT_SIZE = QSize(1280, 800)
GRIP = 6
STATUS_REFRESH_MS = 1000
MODULE_TICK_MS = 200
UNSAVED_EDITS_REASON = ("The Logbook has unsaved flight changes. Save or discard them in the Logbook before "
                        "you switch data folders.")
CLOSE_REASON = "EFB closed by the pilot during Flight Logging"

SAVE_AND_EXIT = "save"
CANCEL = "cancel"
STAY = "stay"
EXIT_ANYWAY = "exit"


def _icon_font() -> str | None:
    """Windows' own caption glyph font, when installed (Windows 11, then 10)."""
    try:
        families = set(QFontDatabase.families())
    except Exception:  # pragma: no cover - font database unavailable
        return None
    for family in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if family in families:
            return family
    return None


class _Glyphs:
    def __init__(self) -> None:
        self.font = _icon_font()
        if self.font:
            self.minimise, self.maximise, self.restore, self.close = "", "", "", ""
        else:
            self.minimise, self.maximise, self.restore, self.close = "–", "□", "❐", "✕"


# ----------------------------------------------------------------------------
# Title bar and resize grips
# ----------------------------------------------------------------------------


class TitleBar(QFrame):
    """Brand, Section tabs, Settings and the window controls. Empty space drags the window."""

    section_requested = Signal(str)

    def __init__(self, window: QMainWindow, sections: Iterable[SectionSpec], *, simulated: bool = False):
        super().__init__(window)
        self._window = window
        self.setObjectName("top")
        self.setAccessibleName("Title bar")
        self.setStyleSheet(
            "QPushButton#tab { background: transparent; border: 1px solid transparent; padding: 6px 14px; }"
            "QPushButton#tab:hover { background: #1d2f42; }"
            "QPushButton#tab:checked { background: #175b8f; border-color: #3285bf; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 3, 2, 3)
        layout.setSpacing(4)
        mark = QLabel("BBC")
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(34, 22)
        mark.setAccessibleName("")
        brand = QLabel("EFB")
        brand.setObjectName("brand")
        brand.setAccessibleName(APP_NAME)
        layout.addWidget(mark)
        layout.addWidget(brand)
        self.simulated_badge = QLabel("SIMULATED")
        self.simulated_badge.setObjectName("warning")
        self.simulated_badge.setToolTip("Started with --simulated: no data comes from MSFS.")
        self.simulated_badge.setAccessibleName("Simulated mode: no data comes from MSFS")
        self.simulated_badge.setVisible(simulated)
        layout.addWidget(self.simulated_badge)
        layout.addStretch(1)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tabs: dict[str, QPushButton] = {}
        settings_spec: SectionSpec | None = None
        for spec in sections:
            if not spec.tab:
                settings_spec = spec
                continue
            self.tabs[spec.key] = self._section_button(spec, "tab")
            layout.addWidget(self.tabs[spec.key])
        layout.addStretch(1)
        if settings_spec is not None:
            self.tabs[settings_spec.key] = self._section_button(settings_spec, "tab")
            layout.addWidget(self.tabs[settings_spec.key])
            layout.addSpacing(8)
        glyphs = _Glyphs()
        self._glyphs = glyphs
        self.minimise_button = self._window_button(glyphs.minimise, "window", "Minimise")
        self.maximise_button = self._window_button(glyphs.maximise, "window", "Maximise")
        self.close_button = self._window_button(glyphs.close, "close", "Close BBC EFB")
        self.minimise_button.clicked.connect(window.showMinimized)
        self.maximise_button.clicked.connect(self.toggle_maximised)
        self.close_button.clicked.connect(window.close)
        for control in (self.minimise_button, self.maximise_button, self.close_button):
            layout.addWidget(control)
        self._drag_offset: QPoint | None = None

    def _section_button(self, spec: SectionSpec, name: str) -> QPushButton:
        control = QPushButton(spec.title)
        control.setObjectName(name)
        control.setCheckable(True)
        title = plain_title(spec.title)
        control.setAccessibleName(f"{title} section")
        hint = f" ({spec.shortcut})" if spec.shortcut else ""
        control.setToolTip(f"{spec.description or title}{hint}")
        control.clicked.connect(lambda checked=False, key=spec.key: self.section_requested.emit(key))
        self.group.addButton(control)
        return control

    def _window_button(self, glyph: str, name: str, accessible: str) -> QPushButton:
        control = QPushButton(glyph)
        control.setObjectName(name)
        control.setFixedSize(44, 30)
        control.setAccessibleName(accessible)
        control.setToolTip(accessible)
        control.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if self._glyphs.font:
            font = control.font()
            font.setFamily(self._glyphs.font)
            control.setFont(font)
        return control

    def set_current(self, key: str) -> None:
        control = self.tabs.get(key)
        if control is not None:
            control.setChecked(True)

    def toggle_maximised(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def update_window_state(self) -> None:
        maximised = self._window.isMaximized()
        self.maximise_button.setText(self._glyphs.restore if maximised else self._glyphs.maximise)
        text = "Restore" if maximised else "Maximise"
        self.maximise_button.setAccessibleName(text)
        self.maximise_button.setToolTip(text)

    # Dragging (only presses on the bar itself or its labels reach here)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            # Fallback for platforms without system move: move it ourselves.
            self._drag_offset = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._window.isMaximized():
                return
            self._window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximised()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


_EDGE_CURSORS = {
    "left": Qt.CursorShape.SizeHorCursor, "right": Qt.CursorShape.SizeHorCursor,
    "top": Qt.CursorShape.SizeVerCursor, "bottom": Qt.CursorShape.SizeVerCursor,
    "top_left": Qt.CursorShape.SizeFDiagCursor, "bottom_right": Qt.CursorShape.SizeFDiagCursor,
    "top_right": Qt.CursorShape.SizeBDiagCursor, "bottom_left": Qt.CursorShape.SizeBDiagCursor,
}


def _edges(name: str) -> Qt.Edge:
    edges = Qt.Edge(0)
    for part, edge in (("left", Qt.Edge.LeftEdge), ("right", Qt.Edge.RightEdge),
                       ("top", Qt.Edge.TopEdge), ("bottom", Qt.Edge.BottomEdge)):
        if part in name.split("_"):
            edges |= edge
    return edges


class ResizeGrip(QWidget):
    """An invisible strip on one window edge or corner that starts a native resize."""

    def __init__(self, window: QMainWindow, name: str):
        super().__init__(window)
        self._window = window
        self.name = name
        self.edges = _edges(name)
        self.setCursor(_EDGE_CURSORS[name])
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._start: tuple[QPoint, QRect] | None = None

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        handle = self._window.windowHandle()
        if handle is not None and handle.startSystemResize(self.edges):
            event.accept()
            return
        self._start = (event.globalPosition().toPoint(), self._window.geometry())
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._start is None:
            return
        origin, geometry = self._start
        delta = event.globalPosition().toPoint() - origin
        rect = QRect(geometry)
        minimum = self._window.minimumSize()
        if self.edges & Qt.Edge.LeftEdge:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - minimum.width()))
        if self.edges & Qt.Edge.RightEdge:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + minimum.width()))
        if self.edges & Qt.Edge.TopEdge:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - minimum.height()))
        if self.edges & Qt.Edge.BottomEdge:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + minimum.height()))
        self._window.setGeometry(rect)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._start = None


class NoticeBanner(QFrame):
    """Pilot notices under the title bar (for example flights recovered at start-up).

    Words carry the meaning; the banner stays until the pilot dismisses it, and it
    never blocks the window.
    """

    open_logbook_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("notice")
        self.setAccessibleName("Notices")
        self.setStyleSheet("QFrame#notice { background: #3a2f12; border-bottom: 1px solid #e3b341; }")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(8)
        self.label = QLabel()
        self.label.setObjectName("warning")
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label, 1)
        self.open_button = QPushButton("Open &Logbook")
        self.open_button.setAccessibleName("Open the Logbook")
        self.open_button.clicked.connect(self.open_logbook_requested)
        layout.addWidget(self.open_button)
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setAccessibleName("Dismiss notices")
        self.dismiss_button.clicked.connect(self.dismiss)
        layout.addWidget(self.dismiss_button)
        self.notices: list[str] = []
        self.hide()

    def add(self, text: str) -> None:
        if not text or text in self.notices:
            return
        self.notices.append(text)
        joined = "\n\n".join(self.notices)
        self.label.setText(joined)
        self.label.setAccessibleName(joined)
        self.setVisible(True)

    def dismiss(self) -> None:
        self.notices.clear()
        self.label.clear()
        self.hide()


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------


class Shell(QMainWindow):
    """The EFB window. ``services`` carries Core, Recorder and storage (see ``ShellServices``)."""

    section_changed = Signal(str)
    ui_active_changed = Signal(bool)
    # Emitted by the module host's change listener; queued onto the UI thread by Qt.
    _modules_changed = Signal()

    def __init__(self, services: ShellServices | None = None, *,
                 sections: Iterable[SectionSpec] | None = None, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.services = services if services is not None else ShellServices()
        self.specs: dict[str, SectionSpec] = {spec.key: spec for spec in (sections or default_sections())}
        if LAUNCH_SECTION not in self.specs:
            raise ValueError("The Section list must include Dispatch, the launch Section.")
        self.sections: dict[str, QWidget] = {}
        self.current_section = ""
        self.ui_active = True
        self._close_approved = False
        self._latest_connection = self.services.initial_connection
        title = APP_NAME + (" - SIMULATED" if self.services.simulated else "")
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.setMinimumSize(MINIMUM_SIZE)

        central = QWidget()
        central.setObjectName("surface")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.title_bar = TitleBar(self, self.specs.values(), simulated=self.services.simulated)
        self.title_bar.section_requested.connect(self.show_section)
        root.addWidget(self.title_bar)
        self.notice_banner = NoticeBanner()
        self.notice_banner.open_logbook_requested.connect(lambda: self.show_section(LOGBOOK))
        root.addWidget(self.notice_banner)
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.stack, 1)
        self.footer = Footer()
        self.footer.set_simulated_mode(self.services.simulated)
        self.footer.diagnostics_requested.connect(self.open_connection_diagnostics)
        self.footer.start_requested.connect(self.start_logging)
        self.footer.stop_requested.connect(self.request_stop_logging)
        self.footer.privacy_requested.connect(lambda: self.open_settings(PRIVACY_PAGE))
        root.addWidget(self.footer)
        self.setCentralWidget(central)

        self.grips = {name: ResizeGrip(self, name) for name in _EDGE_CURSORS}
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(STATUS_REFRESH_MS)
        self.status_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.status_timer.timeout.connect(self.refresh_logging_status)
        self._shortcuts = []
        for spec in self.specs.values():
            if spec.shortcut:
                shortcut = QShortcut(QKeySequence(spec.shortcut), self)
                shortcut.activated.connect(lambda key=spec.key: self.show_section(key))
                self._shortcuts.append(shortcut)

        self.module_timer = QTimer(self)
        self.module_timer.setInterval(MODULE_TICK_MS)
        self.module_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.module_timer.timeout.connect(self.tick_modules)
        self._modules_changed.connect(self._update_module_timer)
        self._watched_module_host: Any = None
        self._watch_module_host()
        self._recorder_signal = False
        self._last_logging_status: LoggingStatus | None = None

        if self.services.data_folder is None:
            self.services.data_folder = DataFolderControl(self.services)
        listeners = getattr(self.services.data_folder, "listeners", None)
        if isinstance(listeners, list):
            listeners.append(self.on_store_changed)
        vetoes = getattr(self.services.data_folder, "vetoes", None)
        if isinstance(vetoes, list):
            vetoes.append(self._logbook_veto)
        self._connect_bridge()
        for notice in self.services.take_notices():
            self.show_notice(notice)
        self.footer.set_connection(self._latest_connection)
        self.refresh_logging_status()
        self._apply_default_geometry()
        self.show_section(LAUNCH_SECTION)
        self.set_ui_active(True)

    # Geometry

    def _apply_default_geometry(self) -> None:
        screen = QGuiApplication.primaryScreen()
        size = QSize(DEFAULT_SIZE)
        if screen is not None:
            available = screen.availableGeometry()
            size = size.boundedTo(QSize(int(available.width() * 0.92), int(available.height() * 0.92)))
            size = size.expandedTo(MINIMUM_SIZE)
            self.resize(size)
            self.move(available.center() - self.rect().center())
        else:
            self.resize(size)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._place_grips()

    def _place_grips(self) -> None:
        width, height = self.width(), self.height()
        corner = GRIP * 2
        rects = {
            "left": QRect(0, corner, GRIP, height - 2 * corner),
            "right": QRect(width - GRIP, corner, GRIP, height - 2 * corner),
            "top": QRect(corner, 0, width - 2 * corner, GRIP),
            "bottom": QRect(corner, height - GRIP, width - 2 * corner, GRIP),
            "top_left": QRect(0, 0, corner, corner),
            "top_right": QRect(width - corner, 0, corner, corner),
            "bottom_left": QRect(0, height - corner, corner, corner),
            "bottom_right": QRect(width - corner, height - corner, corner, corner),
        }
        resizable = not (self.isMaximized() or self.isFullScreen())
        for name, grip in self.grips.items():
            grip.setGeometry(rects[name])
            grip.setVisible(resizable)
            grip.raise_()

    # Sections

    def show_section(self, key: str) -> QWidget | None:
        """Open a Section, building it on first use."""
        spec = self.specs.get(key)
        if spec is None:
            log.warning("Unknown Section %r requested", key)
            return None
        widget = self.sections.get(key)
        if widget is None:
            widget = self._build_section(spec)
            self.sections[key] = widget
            self.stack.addWidget(widget)
        previous = self.sections.get(self.current_section)
        if previous is not None and previous is not widget:
            deactivate = getattr(previous, "set_ui_active", None)
            if callable(deactivate):
                deactivate(False)  # hidden Sections stop avoidable refresh work
        self.stack.setCurrentWidget(widget)
        self.current_section = key
        self.title_bar.set_current(key)
        if key == SETTINGS:
            self._forward_connection(widget, self._latest_connection)
        activate = getattr(widget, "set_ui_active", None)
        if callable(activate) and self.ui_active:
            activate(True)
        self.section_changed.emit(key)
        return widget

    def _build_section(self, spec: SectionSpec) -> QWidget:
        title = plain_title(spec.title)
        try:
            widget = spec.factory(self.services)
        except Exception as error:  # a broken Section must not take the shell down
            log.exception("Section %s could not be built", title)
            return SectionLoadFailure(title, error)
        if not isinstance(widget, QWidget):
            return SectionLoadFailure(title, f"the Section factory returned {type(widget).__name__}")
        if spec.key == SETTINGS:
            self._wire_settings(widget)
        elif spec.key == LOGBOOK:
            self._wire_logbook(widget)
        return widget

    def _wire_logbook(self, widget: QWidget) -> None:
        """Connect the Logbook's requests (contract C1) and give it the current Flight Logging status."""
        logbook = getattr(widget, "logbook", widget)
        requested = getattr(logbook, "data_folder_requested", None)
        if requested is not None and hasattr(requested, "connect"):
            requested.connect(self.request_data_folder)
        for name in ("diagnostics_requested",):
            signal = getattr(logbook, name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(self.open_connection_diagnostics)
        signal = getattr(logbook, "flight_logging_settings_requested", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(lambda: self.open_settings(PRIVACY_PAGE))
        self._forward_recorder_status(widget, self._last_logging_status or self.refresh_logging_status())

    @staticmethod
    def _forward_recorder_status(widget: QWidget | None, status: Any) -> None:
        method = getattr(widget, "set_recorder_status", None)
        if callable(method) and status is not None:
            try:
                method(status)
            except Exception:
                log.exception("The Logbook could not show the Flight Logging status")

    def _logbook_veto(self) -> str | None:
        """DataFolderControl veto: unsaved Logbook edits the pilot wants to keep."""
        logbook = self.sections.get(LOGBOOK)
        check = getattr(logbook, "can_switch_store", None) or getattr(logbook, "can_close", None)
        if callable(check) and not check():
            return UNSAVED_EDITS_REASON
        return None

    # Data folder (contract C1: the app owns the store; the Logbook asks)

    def _pick_folder(self) -> Any:
        """Ask the pilot for a data folder. Returns a path, or None when cancelled."""
        current = getattr(self.services.data_folder, "current", None)
        suggested = current() if callable(current) else None
        try:
            from .settings import choose_data_folder
        except ImportError:
            from PySide6.QtWidgets import QFileDialog

            folder = QFileDialog.getExistingDirectory(self, "Choose the Logbook data folder",
                                                      str(suggested or ""))
            return folder or None
        return choose_data_folder(self, suggested=suggested, first_launch=False)

    def request_data_folder(self, *_args: Any) -> Any:
        """The Logbook asked to choose or change the data folder. Returns the new store or None."""
        folder = self._pick_folder()
        if not folder:
            return None
        return self.change_data_folder(folder)

    def change_data_folder(self, folder: Any) -> Any:
        """Switch every part of the EFB to ``folder`` through ``DataFolderControl``. Never raises."""
        control = self.services.data_folder
        change = getattr(control, "change", None)
        if not callable(change):
            self._warn("Data folder not changed", "This build cannot change the data folder here.")
            return None
        try:
            return change(folder)
        except OSError as error:
            self._warn("Data folder not changed", str(error) or type(error).__name__)
        except Exception as error:  # another component failed; say so instead of crashing the window
            log.exception("Changing the data folder failed")
            self._warn("Data folder not changed", f"BBC EFB could not open that folder: {error}")
        return None

    # Notices

    def show_notice(self, text: str) -> None:
        self.notice_banner.add(text)

    def _wire_settings(self, widget: QWidget) -> None:
        """Connect the Settings signals the shell cares about, when the page has them."""
        automatic = getattr(widget, "automatic_logging_changed", None)
        if automatic is not None and hasattr(automatic, "connect"):
            automatic.connect(self.set_automatic_logging)

    def set_automatic_logging(self, enabled: bool) -> None:
        method = getattr(self.services.logging, "set_automatic", None)
        if callable(method):
            try:
                method(bool(enabled))
            except Exception:
                log.exception("Automatic Flight Logging could not be switched %s", "on" if enabled else "off")
        self.refresh_logging_status()

    def on_store_changed(self, store: Any) -> None:
        """The data folder changed (Settings or the Logbook's request): show it everywhere."""
        logbook = self.sections.get(LOGBOOK)
        refresh = getattr(logbook, "refresh", None)
        if callable(refresh):
            try:
                refresh(store)
            except Exception:
                log.exception("The Logbook could not switch data folders")
        for notice in self.services.take_notices():
            self.show_notice(notice)
        self._last_logging_status = None
        self.refresh_logging_status()
        self._watch_module_host()
        self._update_module_timer()

    @property
    def loaded_sections(self) -> tuple[str, ...]:
        return tuple(self.sections)

    def open_settings(self, page: str = "") -> QWidget | None:
        widget = self.show_section(SETTINGS)
        if widget is not None and page:
            if not open_settings_page(widget, page):
                log.info("Settings page %s is not available", page)
        return widget

    def open_connection_diagnostics(self) -> QWidget | None:
        """Settings > SimConnect > Connection Diagnostics (the SimConnect status destination)."""
        return self.open_settings(DIAGNOSTICS_PAGE)

    # Core bridge

    def _connect_bridge(self) -> None:
        bridge = self.services.bridge
        if bridge is None:
            return
        signal = getattr(bridge, "connection_changed", None)
        if signal is not None:
            signal.connect(self.on_connection_changed)
        recorder = getattr(bridge, "recorder_status_changed", None)
        if recorder is not None and hasattr(recorder, "connect"):
            recorder.connect(self.on_recorder_status)
            self._recorder_signal = True
        current = getattr(bridge, "connection", None)
        try:
            connection = current() if callable(current) else current
        except Exception:
            log.exception("Core bridge connection could not be read")
            connection = None
        if connection is not None:
            self._latest_connection = connection

    def on_connection_changed(self, connection: Any) -> None:
        """SimConnect state from Core. It never changes the Flight Logging status."""
        self._latest_connection = connection
        self.footer.set_connection(connection)
        settings = self.sections.get(SETTINGS)
        if settings is not None:
            self._forward_connection(settings, connection)

    @staticmethod
    def _forward_connection(widget: QWidget, connection: Any) -> None:
        for name in ("set_connection", "on_connection_changed"):
            forward = getattr(widget, name, None)
            if callable(forward):
                try:
                    forward(connection)
                except Exception:
                    log.exception("Settings could not show the connection state")
                return

    # Flight Logging

    def refresh_logging_status(self) -> LoggingStatus:
        try:
            status = self.services.logging.status()
        except Exception as error:
            log.exception("Flight Logging status could not be read")
            status = LoggingStatus(LoggingState.UNAVAILABLE, f"Flight Logging status could not be read: {error}")
        self.on_recorder_status(status)
        return status

    def on_recorder_status(self, status: Any) -> None:
        """Flight Logging status from the Recorder. It never changes the SimConnect status.

        When a flight stops being open, or the Recorder reports a flight logged, the
        Logbook re-reads the store so the saved flight appears.
        """
        if not isinstance(status, LoggingStatus):
            try:
                status = self.services.logging.status()
            except Exception as error:
                status = LoggingStatus(LoggingState.UNAVAILABLE, f"Flight Logging status could not be read: {error}")
        previous = self._last_logging_status
        self._last_logging_status = status
        self.footer.set_logging_status(status)
        logbook = self.sections.get(LOGBOOK)
        if logbook is None:
            return
        if status != previous:
            self._forward_recorder_status(logbook, status)
        if previous is not None and (
                (previous.flight_open and not status.flight_open)
                or (status.state is LoggingState.LOGGED and previous.state is not LoggingState.LOGGED)
                or (status.flight != previous.flight and not status.flight_open)):
            reload = getattr(logbook, "reload", None)
            if callable(reload):
                try:
                    reload()
                except Exception:
                    log.exception("The Logbook could not show the newly saved flight")

    def start_logging(self) -> ActionResult:
        outcome = resolve_action(self.services.logging.start())
        if not outcome.ok:
            self._warn("Flight Logging did not start", outcome.message or "The Flight Recorder did not start.")
        self.refresh_logging_status()
        return outcome

    def request_stop_logging(self) -> ActionResult | None:
        """Manual Stop, after the pilot confirms. Returns None when the pilot keeps logging."""
        status = self.refresh_logging_status()
        if not status.flight_open:
            return None
        if not self._confirm_stop(status):
            return None
        outcome = resolve_action(self.services.logging.stop())
        if not outcome.ok:
            self._warn("Flight Logging did not stop",
                       f"{outcome.message or 'The Flight Recorder did not stop.'} The flight is still open.")
        self.refresh_logging_status()
        return outcome

    # Dialog hooks (methods so tests and hosts can replace them)

    def _warn(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _choose(self, icon: QMessageBox.Icon, title: str, text: str,
                buttons: list[tuple[str, str, QMessageBox.ButtonRole]], default: str, escape: str,
                informative: str = "") -> str:
        box = QMessageBox(icon, title, text, QMessageBox.StandardButton.NoButton, self)
        if informative:
            box.setInformativeText(informative)
        made = {}
        for key, label, role in buttons:
            made[key] = box.addButton(label, role)
            made[key].setAccessibleName(label.replace("&", ""))
        box.setDefaultButton(made[default])
        box.setEscapeButton(made[escape])
        box.exec()
        clicked = box.clickedButton()
        for key, control in made.items():
            if control is clicked:
                return key
        return escape

    def _confirm_stop(self, status: LoggingStatus) -> bool:
        choice = self._choose(
            QMessageBox.Icon.Question, "Stop Flight Logging?",
            "Stop logging this flight now?",
            [("stop", "&Stop logging", QMessageBox.ButtonRole.AcceptRole),
             ("keep", "&Keep logging", QMessageBox.ButtonRole.RejectRole)],
            default="keep", escape="keep",
            informative="The flight is closed at this point and saved as stopped by the pilot. "
                        "Anything after this is not logged.",
        )
        return choice == "stop"

    def _ask_close_while_logging(self, status: LoggingStatus) -> str:
        return self._choose(
            QMessageBox.Icon.Warning, "A flight is being logged",
            "Flight Logging is active. Close BBC EFB?",
            [(SAVE_AND_EXIT, "Save && E&xit", QMessageBox.ButtonRole.AcceptRole),
             (CANCEL, "&Cancel", QMessageBox.ButtonRole.RejectRole)],
            default=CANCEL, escape=CANCEL,
            informative="Save & Exit saves the flight as it is now, marked incomplete because the EFB was "
                        "closed during logging. It is not marked as a completed flight. Cancel keeps the EFB "
                        "open and keeps logging.",
        )

    def _report_save_failure(self, message: str) -> str:
        return self._choose(
            QMessageBox.Icon.Critical, "The flight was not saved",
            f"BBC EFB could not save the flight: {message}",
            [(STAY, "&Return to BBC EFB", QMessageBox.ButtonRole.RejectRole),
             (EXIT_ANYWAY, "E&xit without saving", QMessageBox.ButtonRole.DestructiveRole)],
            default=STAY, escape=STAY,
            informative="Flight Logging stopped for this flight; its journal is kept. If you exit now, the "
                        "flight is not in the Logbook unless the Flight Recorder recovers it the next time BBC "
                        "EFB starts. Returning keeps the EFB open and starts Flight Logging again, which tries "
                        "to save the kept flight once more and logs later flights.",
        )

    def _wait_cursor(self, on: bool) -> None:
        if on:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    # Close

    def save_and_exit(self) -> ActionResult:
        """Ask the Recorder to save the open flight as incomplete. Never raises."""
        self._wait_cursor(True)
        try:
            try:
                raw = self.services.logging.save_and_exit(CLOSE_REASON)
            except Exception as error:
                log.exception("Save & Exit failed")
                return ActionResult(False, str(error) or type(error).__name__)
            return resolve_action(raw, SAVE_TIMEOUT_S)
        finally:
            self._wait_cursor(False)

    def _resume_logging_after_failed_exit(self) -> None:
        """The pilot stays after a failed Save & Exit: Save & Exit closed the Recorder, so start it again."""
        resume = getattr(self.services.logging, "resume_after_failed_exit", None)
        if callable(resume):
            try:
                if resume():
                    log.info("Flight Logging restarted after a failed Save & Exit")
            except Exception:
                log.exception("Flight Logging could not be restarted after a failed Save & Exit")
        for notice in self.services.take_notices():
            self.show_notice(notice)
        self._last_logging_status = None
        self.refresh_logging_status()

    def closeEvent(self, event):  # noqa: N802
        if self._close_approved:
            event.accept()
            return
        logbook = self.sections.get(LOGBOOK)
        can_close = getattr(logbook, "can_close", None)
        if callable(can_close) and not can_close():
            event.ignore()
            return
        status = self.refresh_logging_status()
        if status.flight_open:
            if self._ask_close_while_logging(status) != SAVE_AND_EXIT:
                event.ignore()
                return
            outcome = self.save_and_exit()
            if outcome.ok:
                log.info("Flight saved on exit: %s", outcome.message or "ok")
            else:
                log.error("Flight not saved on exit: %s", outcome.message)
                self.refresh_logging_status()
                if self._report_save_failure(outcome.message or "no reason was given") != EXIT_ANYWAY:
                    self._resume_logging_after_failed_exit()
                    event.ignore()
                    return
                log.warning("Pilot exited without a saved flight")
        self._close_approved = True
        self.status_timer.stop()
        self.module_timer.stop()
        self.footer.set_active(False)
        event.accept()

    # Minimise and UI activity

    def changeEvent(self, event):  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            self.title_bar.update_window_state()
            self._place_grips()
            minimised = bool(self.windowState() & Qt.WindowState.WindowMinimized)
            self.set_ui_active(not minimised)
        super().changeEvent(event)

    def set_ui_active(self, active: bool) -> None:
        """Stop or restart avoidable UI work. Core and Recorder consumers keep running."""
        changed = active != self.ui_active
        self.ui_active = active
        if active:
            self.refresh_logging_status()
            self.footer.set_connection(self._latest_connection)
            if not self._recorder_signal:
                self.status_timer.start()
        else:
            self.status_timer.stop()
        self._update_module_timer()
        self.footer.set_active(active)
        current = self.sections.get(self.current_section)
        activate = getattr(current, "set_ui_active", None)
        if callable(activate):
            activate(active)
        if changed:
            self._tell_core(active)
            self.ui_active_changed.emit(active)

    # Drop-in modules (experimental)

    def _on_modules_changed(self) -> None:
        # May be called from any thread that changed the module host; the signal hops to the UI thread.
        self._modules_changed.emit()

    def _watch_module_host(self) -> None:
        """Follow the current module host, so a module turned on from Settings starts ticking."""
        host = self.services.module_host
        if host is self._watched_module_host:
            return
        previous = self._watched_module_host
        remove = getattr(previous, "remove_listener", None)
        if callable(remove):
            try:
                remove(self._on_modules_changed)
            except Exception:
                log.exception("Could not stop following the previous module host")
        self._watched_module_host = host
        add = getattr(host, "add_listener", None)
        if callable(add):
            try:
                add(self._on_modules_changed)
            except Exception:
                log.exception("Could not follow drop-in module changes")

    def _update_module_timer(self) -> None:
        host = self.services.module_host
        loaded = getattr(host, "loaded", None)
        try:
            running = bool(loaded()) if callable(loaded) else False
        except Exception:
            running = False
        if running and self.ui_active and not self._close_approved:
            self.module_timer.start()
        else:
            self.module_timer.stop()

    def tick_modules(self) -> None:
        """Let loaded drop-in modules run their ``tick`` on the UI thread."""
        host = self.services.module_host
        tick = getattr(host, "tick", None)
        if callable(tick):
            try:
                tick()
            except (Exception, SystemExit):  # a module must never close the EFB
                log.exception("Drop-in modules failed to tick")
        self._update_module_timer()

    def _tell_core(self, active: bool) -> None:
        for target in (self.services.bridge, self.services.host):
            method = getattr(target, "set_ui_active", None)
            if callable(method):
                try:
                    method(active)
                except Exception:
                    log.exception("Core could not be told the UI is %s", "active" if active else "inactive")
                return
