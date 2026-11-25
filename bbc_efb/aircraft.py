"""Exact Logbook aircraft identities and the shared profile-art lookup."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AircraftProfile:
    name: str
    code: str
    role: str

    @property
    def drawing(self):
        slug = self.name.casefold().replace(" ", "-")
        return Path(__file__).with_name("assets") / "aircraft" / f"{slug}.png"


# Type designators follow ICAO Doc 8643. They can be shared by distinct variants.
_PROFILES = tuple(AircraftProfile("Boeing " + model, code, role) for model, code, role in (
    ("707-120B", "B701", "Passenger"),
    ("707-320B", "B703", "Passenger"),
    ("720B", "B720", "Passenger"),
    ("717-200", "B712", "Passenger"),
    ("727-100", "B721", "Passenger"),
    ("727-200", "B722", "Passenger"),
    ("737-200", "B732", "Passenger"),
    ("737-200 Advanced", "B732", "Passenger"),
    ("737-300", "B733", "Passenger"),
    ("737-300BDSF", "B733", "Freighter"),
    ("737-400", "B734", "Passenger"),
    ("737-400BDSF", "B734", "Freighter"),
    ("737-700", "B737", "Passenger"),
    ("737-700BDSF", "B737", "Freighter"),
    ("737-800", "B738", "Passenger"),
    ("737-800BCF", "B738", "Freighter"),
    ("737-800BDSF", "B738", "Freighter"),
    ("737-900ER", "B739", "Passenger"),
    ("737 MAX 8", "B38M", "Passenger"),
    ("737 MAX 9", "B39M", "Passenger"),
    ("747-100", "B741", "Passenger"),
    ("747-200", "B742", "Passenger"),
    ("747-400", "B744", "Passenger"),
    ("747-400F", "B744", "Freighter"),
    ("747-400BCF", "B744", "Freighter"),
    ("747-400BDSF", "B744", "Freighter"),
    ("747-8i", "B748", "Passenger"),
    ("747-8F", "B748", "Freighter"),
    ("757-200", "B752", "Passenger"),
    ("757-200PF", "B752", "Freighter"),
    ("757-300", "B753", "Passenger"),
    ("767-300ER", "B763", "Passenger"),
    ("767-300F", "B763", "Freighter"),
    ("767-300BCF", "B763", "Freighter"),
    ("767-300BDSF", "B763", "Freighter"),
    ("777-200", "B772", "Passenger"),
    ("777-200ER", "B772", "Passenger"),
    ("777-300ER", "B77W", "Passenger"),
    ("777F", "B77L", "Freighter"),
    ("787-8", "B788", "Passenger"),
    ("787-9", "B789", "Passenger"),
    ("787-10", "B78X", "Passenger"),
)) + (
    AircraftProfile("Airbus A330-300", "A333", "Passenger"),
    AircraftProfile("Dash 8 Q400", "DH8D", "Passenger"),
)


def _normalise_name(value):
    return " ".join(value.upper().replace("–", "-").split())


_EXACT_NAMES = {}
for _profile in _PROFILES:
    _key = _normalise_name(_profile.name)
    _EXACT_NAMES[_key] = _profile
    if _key.startswith("BOEING "):
        _bare = _key.removeprefix("BOEING ")
        for _alias in (_key, _bare, "B" + _bare):
            _EXACT_NAMES[_alias] = _profile
            if _alias.endswith(("BCF", "BDSF")):
                _suffix = "BDSF" if _alias.endswith("BDSF") else "BCF"
                _EXACT_NAMES[_alias.removesuffix(_suffix) + " " + _suffix] = _profile

for _alias, _name in {
    "BOEING 737-8": "BOEING 737 MAX 8",
    "BOEING 737-9": "BOEING 737 MAX 9",
    "BOEING 747-8I ASOBO": "BOEING 747-8I",
    "BOEING 747-8 INTERCONTINENTAL": "BOEING 747-8I",
    "PMDG 737-800": "BOEING 737-800",
    "PMDG 737-800BCF": "BOEING 737-800BCF",
    "PMDG 737-800BDSF": "BOEING 737-800BDSF",
    "BOEING 777-F": "BOEING 777F",
}.items():
    _EXACT_NAMES[_alias] = _EXACT_NAMES[_name]

_CODES = frozenset(profile.code for profile in _PROFILES)


def aircraft_profile(value):
    """Resolve explicit names only. A type code alone cannot select artwork."""
    return _EXACT_NAMES.get(_normalise_name(value))


def aircraft_model_names():
    return tuple(profile.name for profile in _PROFILES)


def aircraft_identity(value):
    profile = aircraft_profile(value)
    return profile.name if profile is not None else value.strip().upper()


def aircraft_type_code(value):
    profile = aircraft_profile(value)
    if profile is not None:
        return profile.code
    code = _normalise_name(value)
    return code if code in _CODES else ""
