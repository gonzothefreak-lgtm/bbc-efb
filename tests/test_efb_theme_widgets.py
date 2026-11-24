"""Offscreen tests for bbc_efb/theme.py, bbc_efb/widgets.py and bbc_efb/storage_settings.py."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCalendarWidget, QLineEdit, QTableView, QTableWidgetItem, QWidget,
)

from bbc_efb import theme  # noqa: E402
from bbc_efb.storage_settings import DataFolderSettings, data_folder_notes  # noqa: E402
from bbc_efb.widgets import table  # noqa: E402

APP = QApplication.instance() or QApplication([])


def luminance(image, x, y):
    colour = image.pixelColor(x, y)
    return 0.2126 * colour.red() + 0.7152 * colour.green() + 0.0722 * colour.blue()


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.previous_style = APP.styleSheet()
        self.previous_palette = APP.palette()
        theme.apply_theme(APP)

    def tearDown(self):
        APP.setStyleSheet(self.previous_style)
        APP.setPalette(self.previous_palette)

    def test_disabled_input_looks_different_from_enabled(self):
        images = []
        for enabled in (True, False):
            field = QLineEdit("WWWW 1234")
            field.resize(160, 32)
            field.setEnabled(enabled)
            field.ensurePolished()
            images.append(field.grab().toImage())
        self.assertNotEqual(images[0], images[1])
        self.assertIn("QLineEdit:disabled", theme.STYLE)
        self.assertIn("*:disabled", theme.STYLE)

    def test_palette_has_a_distinct_disabled_text_colour(self):
        palette = APP.palette()
        active = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Text)
        disabled = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        self.assertNotEqual(active, disabled)
        self.assertLess(palette.color(QPalette.ColorRole.Base).lightness(), 60)

    def test_calendar_popup_day_view_is_dark(self):
        calendar = QCalendarWidget()
        calendar.resize(320, 240)
        calendar.show()
        APP.processEvents()
        view = calendar.findChild(QTableView)
        image = view.grab().toImage()
        samples = [luminance(image, x, y) for x in range(5, image.width(), 37) for y in range(5, image.height(), 29)]
        self.assertLess(sum(samples) / len(samples), 110)
        calendar.close()

    def test_font_sizes_scale_from_the_application_font(self):
        self.assertIn("font-size: 10.0pt", theme.style(10))
        self.assertIn("font-size: 15.0pt", theme.style(15))
        self.assertNotIn("px;", theme.style(10).split("font-size")[1][:12])


class TableActivationTests(unittest.TestCase):
    def make(self):
        self.calls = []
        widget = table(("A", "B"), accessible_name="Things", on_activate=self.calls.append)
        widget.setRowCount(3)
        for row in range(3):
            widget.setItem(row, 0, QTableWidgetItem(str(row)))
        return widget

    def test_enter_and_return_activate_the_current_row_once(self):
        widget = self.make()
        widget.selectRow(2)
        QTest.keyClick(widget, Qt.Key.Key_Return)
        QTest.keyClick(widget, Qt.Key.Key_Enter)
        self.assertEqual(self.calls, [2, 2])
        self.assertEqual(widget.accessibleName(), "Things")

    def test_enter_without_selection_does_nothing(self):
        widget = self.make()
        widget.clearSelection()
        QTest.keyClick(widget, Qt.Key.Key_Return)
        self.assertEqual(self.calls, [])

    def test_double_click_activates_once(self):
        widget = self.make()
        widget.resize(300, 200)
        widget.show()
        APP.processEvents()
        rect = widget.visualItemRect(widget.item(1, 0))
        QTest.mouseClick(widget.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        QTest.mouseDClick(widget.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertEqual(self.calls, [1])
        widget.close()

    def test_tab_leaves_the_table(self):
        self.assertFalse(self.make().tabKeyNavigation())


class SettingsTests(unittest.TestCase):
    def test_remember_and_load_round_trip_keeps_other_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cfg" / "settings.json"
            settings = DataFolderSettings(path)
            self.assertIsNone(settings.load())
            path.parent.mkdir()
            path.write_text('{"layout": "compact"}', encoding="utf-8")
            settings.remember(Path(folder) / "data")
            self.assertEqual(settings.load(), Path(folder) / "data")
            self.assertIn("compact", path.read_text(encoding="utf-8"))
            path.write_text("not json", encoding="utf-8")
            with self.assertLogs("bbc_efb.storage_settings", "WARNING"):
                self.assertIsNone(settings.load())

    def test_folder_notes_are_honest(self):
        with mock.patch.dict(os.environ, {"OneDrive": ""}):
            self.assertTrue(data_folder_notes(Path.home() / "OneDrive" / "Documents" / "BBC EFB"))
            self.assertTrue(data_folder_notes(Path(Path.home().anchor)))
            self.assertEqual(data_folder_notes(Path.home() / "BBC EFB Data"), ())


if __name__ == "__main__":
    unittest.main()
