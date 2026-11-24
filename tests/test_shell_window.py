"""Shell window: launch Section, lazy loading, chrome, resizing, keyboard, minimise."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from bbc_core.contract import ConnectionState
from bbc_efb import sections as sections_module
from bbc_efb.footer import LoggingState
from bbc_efb.sections import (
    DIAGNOSTICS_PAGE, DISPATCH, IN_FLIGHT, LOGBOOK, NOT_AVAILABLE, SETTINGS, BasicSettingsPage, DispatchPage,
    InFlightPage, LogbookSection, ReflowGrid, SectionLoadFailure, SectionSpec, construct_with, default_sections,
    feature_card,
)
from bbc_efb.shell import MINIMUM_SIZE, Shell

from tests.test_shell_support import FakeBridge, FakeLogging, connection, qt_app, services


class ShellTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def make(self, **overrides) -> Shell:
        shell = Shell(services(**overrides))
        self.addCleanup(self._dispose, shell)
        return shell

    def _dispose(self, shell):
        shell._close_approved = True
        shell.close()
        shell.deleteLater()
        QApplication.processEvents()


class LaunchTests(ShellTestCase):
    def test_window_is_frameless_with_minimum_size(self):
        shell = self.make()
        self.assertTrue(shell.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(shell.minimumSize(), MINIMUM_SIZE)
        self.assertEqual(shell.windowTitle(), "BBC EFB")

    def test_launches_in_dispatch_and_loads_nothing_else(self):
        shell = self.make()
        self.assertEqual(shell.current_section, DISPATCH)
        self.assertEqual(shell.loaded_sections, (DISPATCH,))
        self.assertIsInstance(shell.stack.currentWidget(), DispatchPage)
        self.assertTrue(shell.title_bar.tabs[DISPATCH].isChecked())

    def test_sections_load_on_demand_and_once(self):
        built = []

        def factory(services):
            built.append(services)
            return QWidget()

        specs = (SectionSpec(DISPATCH, "&Dispatch", DispatchPage),
                 SectionSpec("extra", "&Extra", factory))
        shell = Shell(services(), sections=specs)
        self.addCleanup(self._dispose, shell)
        self.assertEqual(built, [])
        first = shell.show_section("extra")
        second = shell.show_section("extra")
        self.assertIs(first, second)
        self.assertEqual(len(built), 1)
        self.assertIs(built[0], shell.services)

    def test_dispatch_is_required(self):
        with self.assertRaises(ValueError):
            Shell(services(), sections=(SectionSpec("x", "X", QWidget),))

    def test_broken_section_shows_failure_not_crash(self):
        def broken(services):
            raise RuntimeError("factory exploded")

        specs = default_sections() + (SectionSpec("broken", "&Broken", broken),)
        shell = Shell(services(), sections=specs)
        self.addCleanup(self._dispose, shell)
        widget = shell.show_section("broken")
        self.assertIsInstance(widget, SectionLoadFailure)
        self.assertIn("factory exploded", widget.error_text)
        self.assertIsNone(shell.show_section("missing"))
        self.assertEqual(shell.current_section, "broken")

    def test_tabs_title_bar_and_settings_is_not_a_tab(self):
        shell = self.make()
        tabs = shell.title_bar.tabs
        self.assertEqual(set(tabs), {DISPATCH, IN_FLIGHT, LOGBOOK, SETTINGS})
        self.assertEqual(tabs[IN_FLIGHT].text(), "&In-Flight")
        tabs[IN_FLIGHT].click()
        self.assertEqual(shell.current_section, IN_FLIGHT)
        tabs[SETTINGS].click()
        self.assertEqual(shell.current_section, SETTINGS)
        self.assertTrue(tabs[SETTINGS].isChecked())
        self.assertFalse(tabs[IN_FLIGHT].isChecked())

    def test_every_control_has_an_accessible_name(self):
        shell = self.make()
        for key in (IN_FLIGHT, SETTINGS):
            shell.show_section(key)
        for control in shell.findChildren(QPushButton):
            with self.subTest(text=control.text()):
                self.assertTrue(control.accessibleName(), f"no accessible name on {control.text()!r}")

    def test_shortcuts_open_sections(self):
        shell = self.make()
        sequences = {shortcut.key().toString(): shortcut for shortcut in shell._shortcuts}
        self.assertEqual(set(sequences), {"Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+,"})
        sequences["Ctrl+2"].activated.emit()
        self.assertEqual(shell.current_section, IN_FLIGHT)
        sequences["Ctrl+,"].activated.emit()
        self.assertEqual(shell.current_section, SETTINGS)

    def test_simulated_mode_is_labelled_everywhere(self):
        shell = self.make(simulated=True, initial_connection=connection(simulated=True))
        self.assertIn("SIMULATED", shell.windowTitle())
        self.assertFalse(shell.title_bar.simulated_badge.isHidden())
        self.assertFalse(shell.footer.mode_badge.isHidden())
        self.assertIn("SIMULATED", shell.footer.simconnect.text)
        settings = shell.open_connection_diagnostics()
        self.assertIn("SIMULATED", settings.session_rows.values["Mode"].text())

    def test_not_simulated_has_no_simulated_labels(self):
        shell = self.make(initial_connection=connection())
        self.assertTrue(shell.title_bar.simulated_badge.isHidden())
        self.assertTrue(shell.footer.mode_badge.isHidden())
        self.assertNotIn("SIMULATED", shell.footer.simconnect.text)


class WindowControlTests(ShellTestCase):
    def test_maximise_restore_and_grips(self):
        shell = self.make()
        shell.show()
        QApplication.processEvents()
        self.assertTrue(all(not grip.isHidden() for grip in shell.grips.values()))
        self.assertEqual(shell.title_bar.maximise_button.accessibleName(), "Maximise")
        shell.setWindowState(Qt.WindowState.WindowMaximized)
        QApplication.processEvents()
        self.assertEqual(shell.title_bar.maximise_button.accessibleName(), "Restore")
        self.assertTrue(all(grip.isHidden() for grip in shell.grips.values()))
        shell.setWindowState(Qt.WindowState.WindowNoState)
        QApplication.processEvents()
        self.assertEqual(shell.title_bar.maximise_button.accessibleName(), "Maximise")
        self.assertTrue(all(not grip.isHidden() for grip in shell.grips.values()))

    def test_grips_cover_edges_and_corners(self):
        shell = self.make()
        shell.resize(900, 600)
        shell.show()
        QApplication.processEvents()
        grips = shell.grips
        self.assertEqual(len(grips), 8)
        self.assertEqual(grips["left"].geometry().left(), 0)
        self.assertEqual(grips["right"].geometry().right(), shell.width() - 1)
        self.assertEqual(grips["bottom_right"].geometry().bottomRight(), QPoint(shell.width() - 1, shell.height() - 1))
        self.assertTrue(grips["top_left"].edges & Qt.Edge.TopEdge and grips["top_left"].edges & Qt.Edge.LeftEdge)
        self.assertEqual(grips["top"].cursor().shape(), Qt.CursorShape.SizeVerCursor)

    def test_close_button_closes_when_not_logging(self):
        shell = self.make()
        shell.show()
        shell.title_bar.close_button.click()
        self.assertFalse(shell.isVisible())


class LayoutTests(ShellTestCase):
    def test_in_flight_launcher_stays_visible_at_minimum_size(self):
        shell = self.make()
        shell.show()
        page = shell.show_section(IN_FLIGHT)
        for size in ((1600, 900), (MINIMUM_SIZE.width(), MINIMUM_SIZE.height())):
            shell.resize(*size)
            QApplication.processEvents()
            launcher = page.launcher
            self.assertTrue(launcher.isVisible())
            top_left = launcher.mapTo(shell, QPoint(0, 0))
            self.assertGreaterEqual(top_left.x(), 0)
            self.assertLessEqual(top_left.x() + launcher.width(), shell.width())
            self.assertLess(top_left.y() + launcher.height(), shell.footer.mapTo(shell, QPoint(0, 0)).y() + 1)
            self.assertTrue(shell.footer.isVisible())

    def test_launcher_button_brings_module_description_into_view(self):
        shell = self.make()
        page = shell.show_section(IN_FLIGHT)
        self.assertIsInstance(page, InFlightPage)
        page.launcher.buttons["Charts"].click()
        self.assertEqual(page.highlighted, "Charts")
        page.show_module("No such module")
        self.assertEqual(page.highlighted, "Charts")

    def test_placeholders_are_labelled_not_available(self):
        shell = self.make()
        dispatch = shell.sections[DISPATCH]
        self.assertGreaterEqual(len(dispatch.grid.cards), 6)
        for card in dispatch.grid.cards:
            self.assertIn(NOT_AVAILABLE, card.accessibleName())
        page = shell.show_section(IN_FLIGHT)
        for card in page.cards.values():
            self.assertIn(NOT_AVAILABLE, card.accessibleName())
        for control in page.launcher.buttons.values():
            self.assertIn("not available", control.accessibleName())

    def test_reflow_grid_columns(self):
        grid = ReflowGrid([feature_card(str(n), "x") for n in range(5)], minimum_card_width=200, spacing=10)
        self.assertEqual(grid.columns_for(150), 1)
        self.assertEqual(grid.columns_for(410), 2)
        self.assertEqual(grid.columns_for(5000), 3)
        grid.resize(700, 400)
        grid.show()
        QApplication.processEvents()
        self.assertEqual(grid.columns, 3)
        grid.resize(420, 400)
        QApplication.processEvents()
        self.assertEqual(grid.width(), 420)  # the grid can shrink back
        self.assertEqual(grid.columns, 2)
        grid.resize(150, 400)
        QApplication.processEvents()
        self.assertEqual(grid.columns, 1)
        grid.close()


class LogbookAndSettingsTests(ShellTestCase):
    def test_logbook_section_embeds_logbook_lazily(self):
        shell = self.make()
        self.assertNotIn(LOGBOOK, shell.loaded_sections)
        widget = shell.show_section(LOGBOOK)
        self.assertIsInstance(widget, LogbookSection)
        from bbc_efb.logbook import Logbook

        self.assertIsInstance(widget.logbook, Logbook)
        self.assertTrue(widget.can_close())

    def test_logbook_receives_store(self):
        refreshed = []

        class FakeLogbook(QWidget):
            def __init__(self, settings=None):
                super().__init__()
                self.settings = settings

            def refresh(self, store):
                refreshed.append(store)

        store = object()
        original = sections_module.build_logbook
        services_ = services(store=store, folder_settings="folder-settings")
        import bbc_efb.logbook as logbook_module

        saved = logbook_module.Logbook
        logbook_module.Logbook = FakeLogbook
        try:
            widget = original(services_)
        finally:
            logbook_module.Logbook = saved
        self.assertEqual(refreshed, [store])
        self.assertEqual(widget.logbook.settings, "folder-settings")

    def test_settings_falls_back_to_basic_page(self):
        shell = self.make()
        widget = shell.show_section(SETTINGS)
        self.assertIsInstance(widget, BasicSettingsPage)
        self.assertEqual(widget.current_page, DIAGNOSTICS_PAGE)
        self.assertTrue(widget.open_page("data-management"))
        self.assertIn("No data folder", widget.data_rows.values["Location"].text())
        self.assertFalse(widget.open_page("nope"))

    def test_settings_from_module_with_navigation(self):
        import sys
        import types

        opened = []

        class SettingsPage(QWidget):
            def __init__(self, services=None, unrelated_required_kw="ok"):
                super().__init__()
                self.services = services

            def open_page(self, path):
                opened.append(path)

        module = types.ModuleType("bbc_efb_test_settings_module")
        module.SettingsPage = SettingsPage
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        shell = self.make(settings_module=module.__name__)
        widget = shell.open_connection_diagnostics()
        self.assertIsInstance(widget, SettingsPage)
        self.assertIs(widget.services, shell.services)
        self.assertEqual(opened, [DIAGNOSTICS_PAGE])

    def test_construct_with_passes_only_accepted_arguments(self):
        produced = []

        def target(a, *, b=None):
            return (a, b)

        def make(name, value):
            def produce():
                produced.append(name)
                return value
            return produce

        self.assertEqual(construct_with(target, {"a": make("a", 1), "b": make("b", 2), "c": make("c", 3)}), (1, 2))
        self.assertEqual(produced, ["a", "b"])
        self.assertEqual(construct_with(lambda **kw: kw, {"x": lambda: 1}), {"x": 1})


class MinimiseTests(ShellTestCase):
    def test_minimise_suspends_ui_timers_and_tells_core(self):
        bridge = FakeBridge()
        shell = self.make(bridge=bridge)
        shell.show()
        QApplication.processEvents()
        self.assertTrue(shell.status_timer.isActive())
        self.assertTrue(shell.footer.clock_timer.isActive())
        shell.setWindowState(Qt.WindowState.WindowMinimized)
        QApplication.processEvents()
        self.assertFalse(shell.ui_active)
        self.assertFalse(shell.status_timer.isActive())
        self.assertFalse(shell.footer.clock_timer.isActive())
        self.assertEqual(bridge.ui_active_calls, [False])
        shell.setWindowState(Qt.WindowState.WindowNoState)
        QApplication.processEvents()
        self.assertTrue(shell.ui_active)
        self.assertTrue(shell.status_timer.isActive())
        self.assertTrue(shell.footer.clock_timer.isActive())
        self.assertEqual(bridge.ui_active_calls, [False, True])

    def test_hidden_sections_are_told_to_pause(self):
        calls = []

        class Live(QWidget):
            def set_ui_active(self, active):
                calls.append(active)

        specs = default_sections() + (SectionSpec("live", "&Live", lambda services: Live()),)
        shell = Shell(services(), sections=specs)
        self.addCleanup(self._dispose, shell)
        shell.show_section("live")
        shell.show_section(DISPATCH)
        shell.show_section("live")
        shell.set_ui_active(False)
        self.assertEqual(calls, [True, False, True, False])

    def test_minimise_tells_host_when_bridge_lacks_method(self):
        class Host:
            def __init__(self):
                self.calls = []

            def set_ui_active(self, active):
                self.calls.append(active)

        host = Host()
        shell = self.make(host=host)
        shell.set_ui_active(False)
        shell.set_ui_active(False)
        shell.set_ui_active(True)
        self.assertEqual(host.calls, [False, True])

    def test_minimised_shell_still_receives_connection_and_keeps_logging(self):
        bridge = FakeBridge()
        logging_ = FakeLogging(LoggingState.ACTIVE, can_stop=True)
        shell = self.make(bridge=bridge, logging=logging_)
        shell.set_ui_active(False)
        bridge.connection_changed.emit(connection(ConnectionState.ERROR, detail="Quit"))
        self.assertIn("Error", shell.footer.simconnect.text)
        self.assertEqual(logging_.called("stop"), [])
        self.assertEqual(logging_.called("save_and_exit"), [])


if __name__ == "__main__":
    unittest.main()
