"""Aircraft art classification and honest fallback; QPainter charts."""

from __future__ import annotations

import unittest as _unittest

try:
    import PySide6  # noqa: F401
except ImportError:  # The UI extra is not installed: skip, do not fail (CI installs it).
    raise _unittest.SkipTest("PySide6 is not installed; the Qt tests need the ui extra.")

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from tests.test_logviews_support import app
except ImportError:  # discovered without the package prefix
    from test_logviews_support import app  # type: ignore[no-redef]

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPixmap

from bbc_efb.logbook_views import aircraft_art as art
from bbc_efb.logbook_views import charts


class ClassifyTests(unittest.TestCase):
    CASES = {
        "B738": "narrowbody", "B737-800BCF": "narrowbody", "Boeing 737-800": "narrowbody", "737": "narrowbody",
        "A320-251N": "narrowbody", "A20N": "narrowbody", "E190": "narrowbody", "BCS3": "narrowbody",
        "B77W": "widebody", "A330-300": "widebody", "Airbus A350-900": "widebody", "B789": "widebody",
        "B748": "widebody_quad", "A380": "widebody_quad",
        "CRJ900": "rear_jet", "C25C": "rear_jet", "Learjet 75": "rear_jet", "G650": "rear_jet",
        "DHC-8-Q400": "turboprop", "AT72-600": "turboprop", "ATR 72": "turboprop", "DH8D": "turboprop",
        "King Air 350": "turboprop",
        "C172": "single_prop", "C208 Caravan": "single_prop", "DA40": "single_prop", "PA28": "single_prop",
        "TBM 930": "single_prop", "SR22": "single_prop",
        "DA62": "twin_prop", "BE58": "twin_prop", "PA44": "twin_prop",
        "EC135": "helicopter", "H125": "helicopter", "R44": "helicopter",
    }

    def test_known_types(self):
        for text, family in self.CASES.items():
            with self.subTest(text=text):
                match = art.classify(text)
                self.assertEqual(match.family, family)
                self.assertFalse(match.generic)
                self.assertIn("family illustration", match.caption)

    def test_unknown_types_get_generic_profile(self):
        for text in ("", "   ", "XYZ9", "Mystery Jet", "Spaceship"):
            with self.subTest(text=text):
                match = art.classify(text)
                self.assertTrue(match.generic)
                self.assertEqual(match.caption, "Generic profile: no artwork for this type")

    def test_livery_does_not_matter(self):
        self.assertEqual(art.classify("b738").family, art.classify("B738").family)
        self.assertEqual(art.normalise_type("737-800 BCF"), "737800BCF")


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def test_pixmaps_are_drawn_and_cached(self):
        library = art.AircraftArtLibrary()
        first = library.pixmap("B738", QSize(200, 80))
        self.assertIs(library.pixmap("A320", QSize(200, 80)), first)  # same family, same size
        self.assertIsNot(library.pixmap("DH8D", QSize(200, 80)), first)
        image = first.toImage()
        painted = sum(1 for x in range(0, 200, 4) for y in range(0, 80, 4) if image.pixelColor(x, y).alpha() > 0)
        self.assertGreater(painted, 20)
        for family in list(art.FAMILY_LABELS):
            pixmap = QPixmap(100, 40)
            pixmap.fill(Qt.GlobalColor.transparent)
            from PySide6.QtGui import QPainter
            from PySide6.QtCore import QRectF
            painter = QPainter(pixmap)
            art.paint_profile(painter, QRectF(0, 0, 100, 40), family)
            painter.end()

    def test_registered_image_wins(self):
        library = art.AircraftArtLibrary()
        image = QPixmap(300, 120)
        image.fill(QColor("red"))
        library.register_image("B738", image)
        self.assertEqual(library.caption("b738"), "Aircraft profile image")
        pixmap = library.pixmap("B738", QSize(100, 40))
        self.assertEqual(pixmap.toImage().pixelColor(50, 20).red(), 255)
        self.assertIn("family illustration", library.caption("A320"))  # other types unaffected
        with self.assertRaises(ValueError):
            library.register_image("", image)

    def test_cache_is_bounded(self):
        library = art.AircraftArtLibrary(max_cached=3)
        for width in range(10, 20):
            library.pixmap("B738", QSize(width * 10, 40))
        self.assertLessEqual(len(library._cache), 3)

    def test_widget(self):
        widget = art.AircraftArtWidget(art.AircraftArtLibrary())
        widget.set_type("Mystery")
        self.assertIn("Generic profile", widget.toolTip())
        self.assertIn("Mystery", widget.accessibleName())
        widget.resize(260, 104)
        self.assertFalse(widget.grab().isNull())


class ChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app()

    def test_donut(self):
        chart = charts.DonutChart("Time", max_segments=2)
        chart.resize(340, 190)
        chart.set_data([charts.Segment("A", 6), charts.Segment("B", 3), charts.Segment("C", 1), charts.Segment("D", 0)],
                       "10 h", "total", "1 flight without time")
        self.assertEqual([s.label for s in chart.segments], ["A", "Other (2)"])  # zero-value D is dropped
        self.assertEqual(chart.accessibleDescription(), "A: 60%; Other (2): 40%. 1 flight without time")
        chart.grab()
        chart.set_data([])
        self.assertEqual(chart.accessibleDescription(), chart.empty_text)
        chart.grab()

    def test_group_small_keeps_small_lists(self):
        segments = [charts.Segment("A", 1), charts.Segment("B", 2)]
        self.assertEqual(charts.group_small(segments, 5), segments)

    def test_bars(self):
        chart = charts.BarChart("Rates")
        chart.resize(360, 200)
        chart.set_data([charts.Segment("< 100", 2, "2"), charts.Segment("100–150", 0, "0")], "fpm", "Measured on 2")
        self.assertIn("< 100: 2", chart.accessibleDescription())
        chart.grab()
        chart.set_data([charts.Segment("x", 0)])
        self.assertEqual(chart.summary_text(), chart.empty_text)

    def test_horizontal_bars(self):
        chart = charts.HorizontalBarChart("Airports", max_rows=2)
        chart.resize(340, 200)
        chart.set_data([charts.Segment(c, n) for c, n in (("YSSY", 5), ("YMML", 9), ("YBBN", 1))])
        self.assertEqual([r.label for r in chart.rows], ["YMML", "YSSY"])
        self.assertIn("1 more not shown", chart.accessibleDescription())
        chart.grab()

    def test_profile_and_timeline(self):
        profile = charts.ProfileChart()
        profile.resize(520, 180)
        profile.set_data([(0, 0), (600, 35000), (1200, 0)], [charts.Marker(10, "Takeoff")], [(300, 400)])
        self.assertIn("highest 35,000 ft", profile.accessibleDescription())
        self.assertIn("1 data gap", profile.accessibleDescription())
        profile.grab()
        profile.set_data([])
        self.assertIn("No altitude", profile.accessibleDescription())
        strip = charts.TimelineStrip()
        strip.resize(420, strip.sizeHint().height())
        strip.set_data([charts.Milestone("OFF", "08:00"), charts.Milestone("TO", "—", False)])
        self.assertEqual(strip.accessibleDescription(), "OFF 08:00; TO —")
        strip.grab()


if __name__ == "__main__":
    unittest.main()
