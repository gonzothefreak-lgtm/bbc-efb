"""Type-based side-on aircraft art, drawn in code, with an honest generic fallback.

The approved design (Logbook Design Record, "Shared Aircraft Profile Image Library")
asks for one clean full side-on profile per aircraft *type*, used everywhere, and a
clear generic silhouette when no matching art exists, never an unrelated aircraft.

This module ships no images (no licensing questions, no new dependencies). It maps a
type name to a broad *family* and draws a family silhouette with QPainter:

* a recognised type gets its family silhouette and the caption says so
  ("Twin-engine narrow-body jet, family illustration");
* an unrecognised type gets the neutral generic profile and the caption
  "Generic profile: no artwork for this type".

Main Development owns the shared image library. ``AircraftArtLibrary.register_image``
is the hook for real per-type artwork later; registered images always win over the
drawn family art. Livery never changes the type identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

GENERIC = "generic"

FAMILY_LABELS = {
    "widebody": "Twin-aisle wide-body jet",
    "widebody_quad": "Four-engine wide-body jet",
    "narrowbody": "Twin-engine narrow-body jet",
    "rear_jet": "Rear-engine regional or business jet",
    "turboprop": "Twin turboprop",
    "single_prop": "Single-engine propeller aircraft",
    "twin_prop": "Light twin propeller aircraft",
    "helicopter": "Helicopter",
    GENERIC: "Generic profile",
}

# (pattern, family). Patterns are matched against an upper-case type name with spaces,
# hyphens and underscores removed. The first match wins, so specific entries come first.
_RULES: tuple[tuple[str, str], ...] = (
    # helicopters
    (r"^(EC1[2-5]\d|H1[2-7]\d|AS3[56]\d|R22|R44|R66|B06|B407|BELL|AW1[0-9]\d|UH\d|S76|CABRI|G2CA|HELI)", "helicopter"),
    # four-engine wide-bodies
    (r"^(B74\d|B748|7478|747|A38\d|A380|A34\d|A340|IL96|IL76|C17(?!\d)|AN124)", "widebody_quad"),
    # twin wide-bodies
    (r"^(B77\w*|777|B78\w*|787|B76\w*|767|A33\w*|A330|A35\w*|A350|A30\d|A300|A310|MD11|DC10|L101)", "widebody"),
    # rear-engine regional and business jets
    (r"^(CRJ\w*|CL6\w*|E135|E145|ERJ1[34]\w*|ERJ145|F100|F70|MD8\w*|MD90|DC9|B712|7172|C25\w*|C5\d\w*|C68\w*|"
     r"C7\d\w*|CJ\d|CITATION|LJ\d\w*|LEARJET|GLF\w*|G[1-7]\d\d\w*|GULFSTREAM|FA\d\w*|FALCON|E50P|E55P|PHENOM|"
     r"PC24|HDJT|HONDAJET|SF50|VISIONJET|GLEX|GLOBAL|CL30|CL35|CHALLENGER|H25B|HAWKER)", "rear_jet"),
    # narrow-body twins (including E-Jets and A220)
    (r"^(B73\w*|737|B75\w*|757|A31\w*|A32\w*|A2\dN|A20N|A21N|BCS\d|A22\d|E1[79]\d|E2\d\d|E7\d|E9\d|"
     r"ERJ1[79]\w*|EMB1[79]\w*|EMBRAER1[79]\w*|B717|C919|MC21|SSJ|SU95|TU204|BAE146|B46\d|RJ\d\d|AVRO)", "narrowbody"),
    # twin turboprops
    (r"^(AT\d\d|ATR\w*|DH8\w*|DHC8\w*|Q[1-4]00|DASH8|SF34|SAAB340|SB20|J31|J32|J41|JS\d\d|E120|D328|DO328|"
     r"F50|F27|C130|L100|BE20|BE30|BE35\d|BE9\w|KINGAIR|B350|DHC6|DH6|TWINOTTER|PC6|AN2[4-6]|L410|C212|CN35)", "turboprop"),
    # light twins
    (r"^(DA42|DA62|BE58|BE55|BARON|PA44|SEMINOLE|PA34|SENECA|PA31|NAVAJO|C310|C340|C414|C421|P68|TECNAM2006|P2006)", "twin_prop"),
    # single-engine propeller aircraft (piston and turboprop)
    (r"^(C1[5-9]\d|C2[01]\d|C208|CARAVAN|C17\d|C182|SKYHAWK|DA20|DA40|DA50|PA\d\d|PIPER|CUB|J3|SR2\d|CIRRUS|"
     r"TBM\w*|PC12|PC21|PC9|KODIAK|K100|BE33|BE36|BONANZA|M20\w*|MOONEY|DR40\w*|DR400|ROBIN|EXTRA|E300|"
     r"VL3|ICON|A5|CTLS|C150|C152|DHC2|BEAVER|AT8T|AIRTRACTOR|SAVAGE|ZLIN|P210|C337|GA8|AIRVAN|XCUB|CAP10|PITTS)", "single_prop"),
)
_COMPILED = tuple((re.compile(pattern), family) for pattern, family in _RULES)


@dataclass(frozen=True, slots=True)
class ArtMatch:
    """Which art an aircraft type gets, and the honest caption to show with it."""

    type_key: str
    family: str
    generic: bool

    @property
    def caption(self) -> str:
        if self.generic:
            return "Generic profile: no artwork for this type"
        return f"{FAMILY_LABELS[self.family]}, family illustration"


def normalise_type(text: str) -> str:
    return re.sub(r"[\s\-_/.]+", "", str(text or "").upper())


def classify(type_text: str) -> ArtMatch:
    """Map an aircraft type name to an art family. Unknown types get the generic profile."""
    key = normalise_type(type_text)
    if key:
        # Strip common manufacturer prefixes so "Boeing 737-800" and "B738" both match.
        stripped = re.sub(r"^(BOEING|AIRBUS|EMBRAER|BOMBARDIER|CESSNA|DIAMOND|BEECHCRAFT|BEECH|DEHAVILLAND|"
                          r"DEHAVILLANDCANADA|PILATUS|DAHER|SOCATA|MCDONNELLDOUGLAS|LOCKHEED|ANTONOV)", "", key)
        for candidate in (key, stripped, _boeing_style(stripped)):
            for pattern, family in _COMPILED:
                if candidate and pattern.match(candidate):
                    return ArtMatch(key, family, False)
    return ArtMatch(key, GENERIC, True)


def _boeing_style(key: str) -> str:
    """'737800' -> 'B737800' so bare Boeing model numbers match the B7xx rules."""
    return f"B{key}" if re.match(r"^7[0-9]7", key) else key


# Drawing -----------------------------------------------------------------------------
# Every family is drawn in a 100 x 40 unit box (nose left, tail right, gear down) and
# scaled into the target rectangle keeping that aspect ratio.

BODY = QColor("#eef3f8")
BODY_SHADE = QColor("#c4cfdb")
OUTLINE = QColor("#7f93a8")
ACCENT = QColor("#2f7fc4")
GEAR = QColor("#3b4a5a")
WINDOW = QColor("#5b6f84")
GENERIC_BODY = QColor("#4a5d71")
GENERIC_LINE = QColor("#8ea3b8")


def _fuselage(nose_x, tail_x, top, bottom, upsweep=3.0, tail_top=None):
    tail_top = top - upsweep if tail_top is None else tail_top
    path = QPainterPath(QPointF(nose_x + 6, top))
    path.lineTo(tail_x - 14, top)
    path.cubicTo(QPointF(tail_x - 6, top), QPointF(tail_x - 2, tail_top), QPointF(tail_x, tail_top))
    path.lineTo(tail_x, tail_top + 2)
    path.cubicTo(QPointF(tail_x - 4, bottom - 1), QPointF(tail_x - 12, bottom), QPointF(tail_x - 18, bottom))
    path.lineTo(nose_x + 7, bottom)
    path.cubicTo(QPointF(nose_x + 1, bottom), QPointF(nose_x - 0.5, (top + bottom) / 2 + 1), QPointF(nose_x, (top + bottom) / 2))
    path.cubicTo(QPointF(nose_x + 0.5, top + 1), QPointF(nose_x + 3, top), QPointF(nose_x + 6, top))
    path.closeSubpath()
    return path


def _polygon(*points):
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    path.closeSubpath()
    return path


def _wheel(painter, x, y, r=1.6):
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(GEAR)
    painter.drawEllipse(QPointF(x, y), r, r)


def _strut(painter, x, top, bottom):
    painter.setPen(QPen(GEAR, 0.8))
    painter.drawLine(QPointF(x, top), QPointF(x, bottom))


def _windows(painter, x0, x1, y, step=2.2, r=0.45):
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(WINDOW)
    x = x0
    while x < x1:
        painter.drawEllipse(QPointF(x, y), r, r * 1.2)
        x += step


def _fill(painter, path, colour, outline=OUTLINE, width=0.35):
    painter.setPen(QPen(outline, width))
    painter.setBrush(colour)
    painter.drawPath(path)


def _draw_jet(painter, accent, *, length=1.0, height=7.0, engines="wing", quad=False, ttail=False):
    nose, tail = 4.0, 4.0 + 92.0 * length
    mid = 21.0
    top, bottom = mid - height / 2, mid + height / 2
    fin_base = tail - 13
    if ttail:
        fin = _polygon((fin_base, top + 0.5), (tail - 6, top - 12), (tail - 1.5, top - 12), (tail - 0.5, top - 1))
    else:
        fin = _polygon((fin_base, top + 0.5), (tail - 5, top - 13 - height * 0.3), (tail - 0.5, top - 13 - height * 0.3), (tail, top - 2))
    _fill(painter, fin, accent)
    body = _fuselage(nose, tail, top, bottom)
    _fill(painter, body, BODY)
    stab_y = top - 11.5 if ttail else top + 0.5
    _fill(painter, _polygon((tail - 9, stab_y), (tail - 1, stab_y - 1.2), (tail + 1, stab_y - 0.4), (tail - 6, stab_y + 1.0)), BODY_SHADE)
    wing_x = nose + 34 * length
    _fill(painter, _polygon((wing_x, mid + 1.2), (wing_x + 20 * length, mid + 2.6), (wing_x + 22 * length, mid + 3.2), (wing_x + 1, mid + 3.0)), BODY_SHADE)
    _windows(painter, nose + 9, tail - 18, top + height * 0.33)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(WINDOW)
    painter.drawRoundedRect(QRectF(nose + 2.2, top + height * 0.22, 3.2, height * 0.2), 0.4, 0.4)
    eng_h = 4.0 if quad else 4.8 * min(1.3, height / 7.0)
    if engines == "wing":
        positions = (wing_x - 3, wing_x + 7) if quad else (wing_x - 2,)
        for x in positions:
            nacelle = QPainterPath()
            nacelle.addRoundedRect(QRectF(x, bottom - 0.6, 11 * min(1.2, height / 7.0), eng_h), eng_h / 2.2, eng_h / 2.2)
            _fill(painter, nacelle, BODY_SHADE)
            painter.fillRect(QRectF(x + 0.6, bottom - 0.2, 1.2, eng_h - 0.8), accent)
    else:  # rear fuselage engines
        nacelle = QPainterPath()
        nacelle.addRoundedRect(QRectF(tail - 25, top - 2.5, 10, 4.2), 2, 2)
        _fill(painter, nacelle, BODY_SHADE)
        painter.fillRect(QRectF(tail - 24.4, top - 2.0, 1.2, 3.2), accent)
    gear_bottom = bottom + (6.5 if engines == "wing" else 5.0)
    _strut(painter, nose + 9, bottom, gear_bottom - 1.6)
    _wheel(painter, nose + 9, gear_bottom - 1.4, 1.3)
    main_x = wing_x + 11 * length
    _strut(painter, main_x, bottom, gear_bottom - 1.6)
    _wheel(painter, main_x - 1.2, gear_bottom - 1.6)
    _wheel(painter, main_x + 1.8, gear_bottom - 1.6)


def _draw_turboprop(painter, accent):
    nose, tail, top, bottom = 6.0, 94.0, 18.0, 24.5
    _fill(painter, _polygon((tail - 14, top + 0.5), (tail - 6, top - 12), (tail - 1.5, top - 12), (tail, top - 1)), accent)
    _fill(painter, _fuselage(nose, tail, top, bottom, upsweep=2.0), BODY)
    _fill(painter, _polygon((tail - 8, top - 11.5), (tail - 0.5, top - 12.6), (tail + 1.5, top - 11.8), (tail - 5, top - 10.6)), BODY_SHADE)
    _fill(painter, _polygon((34, top - 0.8), (60, top - 0.8), (62, top + 0.6), (34, top + 0.6)), BODY_SHADE)
    nacelle = QPainterPath()
    nacelle.addRoundedRect(QRectF(36, top + 0.2, 14, 4.4), 2, 2)
    _fill(painter, nacelle, BODY_SHADE)
    painter.setPen(QPen(GEAR, 0.5))
    painter.setBrush(QColor(120, 140, 160, 70))
    painter.drawEllipse(QRectF(34.8, top - 6.5, 1.6, 17.5))
    _windows(painter, nose + 10, tail - 20, top + 2.2, step=2.8, r=0.55)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(WINDOW)
    painter.drawRoundedRect(QRectF(nose + 2.4, top + 1.4, 3.2, 1.4), 0.4, 0.4)
    _strut(painter, nose + 8, bottom, bottom + 4.2)
    _wheel(painter, nose + 8, bottom + 4.4, 1.2)
    _strut(painter, 46, bottom, bottom + 4.0)
    _wheel(painter, 46, bottom + 4.4, 1.6)


def _draw_light(painter, accent, *, twin=False):
    nose, tail, top, bottom = 14.0, 88.0, 18.0, 23.5
    _fill(painter, _polygon((tail - 12, top + 0.6), (tail - 5, top - 9), (tail - 1, top - 9), (tail, top - 0.5)), accent)
    body = QPainterPath(QPointF(nose + 4, top - 1))
    body.lineTo(nose + 24, top - 3.5)          # canopy rise
    body.cubicTo(QPointF(nose + 30, top - 3.8), QPointF(nose + 34, top - 1.5), QPointF(nose + 38, top))
    body.lineTo(tail - 2, top)
    body.lineTo(tail, top + 1.6)
    body.lineTo(nose + 30, bottom)
    body.lineTo(nose + 4, bottom)
    body.cubicTo(QPointF(nose - 1, bottom), QPointF(nose - 1, top - 1), QPointF(nose + 4, top - 1))
    body.closeSubpath()
    _fill(painter, body, BODY)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(WINDOW)
    painter.drawPolygon([QPointF(nose + 14, top - 0.8), QPointF(nose + 23, top - 3.0), QPointF(nose + 29, top - 2.4), QPointF(nose + 29, top + 0.4), QPointF(nose + 14, top + 0.4)])
    _fill(painter, _polygon((tail - 8, top + 0.2), (tail + 1, top - 0.4), (tail + 1.5, top + 0.6), (tail - 6, top + 1.2)), BODY_SHADE)
    wing_y = bottom - 1.2
    _fill(painter, _polygon((nose + 14, wing_y), (nose + 34, wing_y + 0.4), (nose + 34, wing_y + 1.6), (nose + 14, wing_y + 1.6)), BODY_SHADE)
    if twin:
        nacelle = QPainterPath()
        nacelle.addRoundedRect(QRectF(nose + 13, wing_y - 2.0, 11, 3.6), 1.6, 1.6)
        _fill(painter, nacelle, BODY_SHADE)
        painter.setPen(QPen(GEAR, 0.5))
        painter.setBrush(QColor(120, 140, 160, 70))
        painter.drawEllipse(QRectF(nose + 12.2, wing_y - 6.5, 1.2, 12))
        painter.drawLine(QPointF(nose - 0.5, (top + bottom) / 2), QPointF(nose + 1.0, (top + bottom) / 2))
    else:
        painter.setPen(QPen(GEAR, 0.5))
        painter.setBrush(QColor(120, 140, 160, 70))
        painter.drawEllipse(QRectF(nose - 1.6, top - 5.5, 1.4, 15.5))
    _strut(painter, nose + 4, bottom, bottom + 4)
    _wheel(painter, nose + 4, bottom + 4.2, 1.3)
    _strut(painter, nose + 27, bottom, bottom + 4)
    _wheel(painter, nose + 27, bottom + 4.2, 1.5)


def _draw_helicopter(painter, accent):
    cabin = QPainterPath(QPointF(20, 26))
    cabin.cubicTo(QPointF(14, 26), QPointF(13, 18), QPointF(20, 15))
    cabin.cubicTo(QPointF(26, 12), QPointF(42, 12), QPointF(48, 15))
    cabin.lineTo(52, 19)
    cabin.lineTo(84, 19.5)
    cabin.lineTo(84, 21.0)
    cabin.lineTo(50, 24)
    cabin.cubicTo(QPointF(46, 26), QPointF(40, 26.5), QPointF(20, 26))
    cabin.closeSubpath()
    _fill(painter, cabin, BODY)
    _fill(painter, _polygon((80, 19.8), (85, 10), (88, 10), (86.5, 21)), accent)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(WINDOW)
    painter.drawPolygon([QPointF(17, 19), QPointF(21, 15.5), QPointF(28, 14.6), QPointF(28, 19.5)])
    painter.setPen(QPen(GEAR, 0.8))
    painter.drawLine(QPointF(34, 12.6), QPointF(34, 9.5))
    painter.setPen(QPen(GEAR, 0.7))
    painter.drawLine(QPointF(6, 9.5), QPointF(66, 8.5))
    painter.drawEllipse(QRectF(84.2, 9.5, 4.5, 4.5))
    painter.drawLine(QPointF(24, 26), QPointF(22, 30))
    painter.drawLine(QPointF(42, 26), QPointF(44, 30))
    painter.setPen(QPen(GEAR, 1.0))
    painter.drawLine(QPointF(15, 30.2), QPointF(50, 30.2))


def _draw_generic(painter):
    """Neutral silhouette: plainly a placeholder, never mistaken for a specific type."""
    top, bottom = 17.5, 24.5
    pen = QPen(GENERIC_LINE, 0.6, Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.setBrush(GENERIC_BODY)
    painter.drawPath(_polygon((80, top + 0.5), (88, top - 11), (93, top - 11), (95, top - 1)))
    painter.drawPath(_fuselage(6, 96, top, bottom))
    painter.drawPath(_polygon((40, bottom - 2.2), (62, bottom - 0.8), (63, bottom + 0.2), (40, bottom + 0.2)))
    painter.setPen(QPen(GENERIC_LINE, 0.7))
    painter.drawLine(QPointF(15, bottom), QPointF(15, bottom + 4))
    painter.drawLine(QPointF(56, bottom), QPointF(56, bottom + 4))
    painter.setBrush(GENERIC_LINE)
    painter.drawEllipse(QPointF(15, bottom + 4.6), 1.2, 1.2)
    painter.drawEllipse(QPointF(56, bottom + 4.6), 1.5, 1.5)


def paint_profile(painter: QPainter, rect: QRectF, family: str, accent: QColor | None = None) -> None:
    """Draw the family silhouette centred in ``rect`` (aspect 100:40)."""
    accent = QColor(accent) if accent is not None else ACCENT
    scale = min(rect.width() / 100.0, rect.height() / 40.0)
    if scale <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.center().x() - 50 * scale, rect.center().y() - 20 * scale)
    painter.scale(scale, scale)
    if family == "widebody":
        _draw_jet(painter, accent, height=9.0)
    elif family == "widebody_quad":
        _draw_jet(painter, accent, height=9.5, quad=True)
    elif family == "narrowbody":
        _draw_jet(painter, accent, length=0.92, height=7.0)
    elif family == "rear_jet":
        _draw_jet(painter, accent, length=0.8, height=6.0, engines="rear", ttail=True)
    elif family == "turboprop":
        _draw_turboprop(painter, accent)
    elif family == "single_prop":
        _draw_light(painter, accent)
    elif family == "twin_prop":
        _draw_light(painter, accent, twin=True)
    elif family == "helicopter":
        _draw_helicopter(painter, accent)
    else:
        _draw_generic(painter)
    painter.restore()


class AircraftArtLibrary:
    """Shared, cached source of aircraft profile pixmaps keyed by type (not livery).

    One instance should be shared by every Logbook page so each type is drawn once
    per size. ``register_image`` lets Main's future image library supply real art.
    """

    def __init__(self, max_cached: int = 256):
        self._cache: dict[tuple[str, int, int, int], QPixmap] = {}
        self._images: dict[str, QPixmap] = {}
        self._max = max_cached

    def register_image(self, type_text: str, pixmap: QPixmap) -> None:
        """Use ``pixmap`` (a side-on profile) for this type everywhere from now on."""
        key = normalise_type(type_text)
        if not key or pixmap.isNull():
            raise ValueError("A registered aircraft image needs a type and a non-empty image.")
        self._images[key] = pixmap
        self._cache = {k: v for k, v in self._cache.items() if k[0] != key}

    def match(self, type_text: str) -> ArtMatch:
        key = normalise_type(type_text)
        if key in self._images:
            return ArtMatch(key, "image", False)
        return classify(type_text)

    def caption(self, type_text: str) -> str:
        match = self.match(type_text)
        return "Aircraft profile image" if match.family == "image" else match.caption

    def pixmap(self, type_text: str, size: QSize, device_pixel_ratio: float = 1.0) -> QPixmap:
        match = self.match(type_text)
        ratio = max(1.0, float(device_pixel_ratio))
        cache_key = (match.type_key if match.family == "image" else match.family, size.width(), size.height(), int(ratio * 100))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        pixmap = QPixmap(max(1, int(size.width() * ratio)), max(1, int(size.height() * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        target = QRectF(0, 0, size.width(), size.height())
        if match.family == "image":
            source = self._images[match.type_key]
            scaled = source.scaled(size * ratio, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            x = (size.width() - scaled.width() / ratio) / 2
            y = (size.height() - scaled.height() / ratio) / 2
            painter.drawPixmap(QPointF(x, y), scaled)
        else:
            paint_profile(painter, target, match.family)
        painter.end()
        if len(self._cache) >= self._max:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cache_key] = pixmap
        return pixmap


_SHARED: AircraftArtLibrary | None = None


def shared_library() -> AircraftArtLibrary:
    """The process-wide art library (created on first use, after QApplication exists)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = AircraftArtLibrary()
    return _SHARED


class AircraftArtWidget(QWidget):
    """Shows the profile for a type with its honest caption as tooltip/accessible text."""

    def __init__(self, library: AircraftArtLibrary | None = None, parent=None):
        super().__init__(parent)
        self.library = library or shared_library()
        self.type_text = ""
        self.setMinimumSize(160, 64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def sizeHint(self):
        return QSize(300, 120)

    def heightForWidth(self, width):
        return int(width * 0.4)

    def hasHeightForWidth(self):
        return True

    def set_type(self, type_text: str) -> None:
        self.type_text = type_text or ""
        caption = self.library.caption(self.type_text)
        self.setToolTip(caption)
        self.setAccessibleName(f"Aircraft profile: {self.type_text or 'type unknown'}")
        self.setAccessibleDescription(caption)
        self.update()

    @property
    def caption(self) -> str:
        return self.library.caption(self.type_text)

    def paintEvent(self, _event):
        painter = QPainter(self)
        size = self.size()
        pixmap = self.library.pixmap(self.type_text, size, self.devicePixelRatioF())
        painter.drawPixmap(0, 0, pixmap)
