"""Typed, consumer-supplied MSFS SimVar addresses."""

from __future__ import annotations

from dataclasses import dataclass
from re import fullmatch

from .contract import CoreRequestError, StrEnum, ValueType


class SimVarFormat(StrEnum):
    INT32 = "int32"
    INT64 = "int64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING8 = "string8"
    STRING32 = "string32"
    STRING64 = "string64"
    STRING128 = "string128"
    STRING256 = "string256"
    STRING260 = "string260"


_DEFAULT_FORMAT = {
    ValueType.BOOLEAN: SimVarFormat.INT32,
    ValueType.INTEGER: SimVarFormat.INT32,
    ValueType.NUMBER: SimVarFormat.FLOAT64,
    ValueType.TEXT: SimVarFormat.STRING256,
}
_VALID_FORMATS = {
    ValueType.BOOLEAN: {SimVarFormat.INT32},
    ValueType.INTEGER: {SimVarFormat.INT32, SimVarFormat.INT64},
    ValueType.NUMBER: {SimVarFormat.FLOAT32, SimVarFormat.FLOAT64},
    ValueType.TEXT: {
        SimVarFormat.STRING8, SimVarFormat.STRING32, SimVarFormat.STRING64,
        SimVarFormat.STRING128, SimVarFormat.STRING256, SimVarFormat.STRING260,
    },
}


# SDK families whose index counts from zero (for example GEAR IS ON GROUND:0 is the centre or
# nose gear and CONTACT POINT IS ON GROUND:0 the first contact point). Other indexed SimVars,
# such as engines, radios and lights, count from one, so index 0 stays a request error there.
_ZERO_BASED_PREFIXES = ("GEAR ", "WHEEL ", "CONTACT POINT ", "AUTO BRAKE ", "LEFT WHEEL ", "RIGHT WHEEL ",
                        "CENTER WHEEL ", "AUX WHEEL ")


def zero_based_index(name: str) -> bool:
    """True when the SDK documents index 0 for this SimVar family."""
    return name.upper().startswith(_ZERO_BASED_PREFIXES)


@dataclass(frozen=True, slots=True)
class SimVar:
    name: str
    unit: str | None
    value_type: ValueType
    index: int | None = None
    format: SimVarFormat | None = None

    def __post_init__(self):
        if not isinstance(self.name, str):
            raise CoreRequestError("Supply a documented ASCII SimVar name.")
        name = self.name.strip().upper()
        if not fullmatch(r"(?:L:)?[A-Z0-9_][A-Z0-9 _.\-]*", name) or len(name) > 256 or not self.name.isascii():
            raise CoreRequestError("Supply a documented SimVar name and pass its index separately.")
        if self.index is not None:
            lowest = 0 if zero_based_index(name) else 1
            if type(self.index) is not int or not lowest <= self.index <= 0xFFFFFFFF:
                raise CoreRequestError(
                    "SimVar indices start at zero for gear, wheel and contact-point variables and at one otherwise."
                )
        if not isinstance(self.value_type, ValueType) or self.value_type not in _DEFAULT_FORMAT:
            raise CoreRequestError("A SimVar must be Boolean, integer, number or text.")
        if self.format is not None and (
            not isinstance(self.format, SimVarFormat) or self.format not in _VALID_FORMATS[self.value_type]
        ):
            raise CoreRequestError("The SDK format does not match the SimVar value type.")
        if name.startswith("L:") and (
            self.index is not None or self.value_type is not ValueType.NUMBER
            or self.format not in (None, SimVarFormat.FLOAT64)
        ):
            raise CoreRequestError("Local L: variables take a FLOAT64 number and no index.")
        if self.value_type is ValueType.TEXT:
            if self.unit is not None:
                raise CoreRequestError("A string SimVar has no SDK unit.")
        elif (not isinstance(self.unit, str) or not self.unit.isascii()
              or not self.unit.isprintable() or not 1 <= len(self.unit.strip()) <= 80):
            raise CoreRequestError("A numeric or Boolean SimVar requires an SDK unit.")
        object.__setattr__(self, "name", name)
        if self.unit is not None:
            object.__setattr__(self, "unit", self.unit.strip().lower())
        if self.format is _DEFAULT_FORMAT[self.value_type]:
            object.__setattr__(self, "format", None)

    @property
    def sdk_name(self):
        return self.name if self.index is None else f"{self.name}:{self.index}"


SIMVAR_WRITE = "sdk.simvar.write"
CLIENT_EVENT_SEND = "sdk.client_event.send"


def client_event_name(event: str) -> str:
    """Validate an SDK event name.

    Standard events are case-insensitive and are upper-cased. Custom events published by
    add-on aircraft contain one period (for example ``A32NX.FCU_HDG_INC``) and are kept exactly
    as given, because their owners define the spelling. ``K:`` style prefixes are rejected.
    """
    if not isinstance(event, str) or not fullmatch(
        r"[A-Za-z][A-Za-z0-9_]{0,127}(?:\.[A-Za-z0-9_]{1,127})?", event,
    ):
        raise CoreRequestError(
            "Use a documented SDK event name, or a custom event such as VENDOR.EVENT, without a K: prefix."
        )
    return event if "." in event else event.upper()


def client_event_words(event, arguments):
    event = client_event_name(event)
    if not isinstance(arguments, tuple) or len(arguments) > 5:
        raise CoreRequestError("Supply a tuple of zero to five SDK event arguments.")
    words = []
    for argument in arguments:
        if type(argument) not in (bool, int) or not -(1 << 31) <= argument <= 0xFFFFFFFF:
            raise CoreRequestError("Each SDK event argument must fit in a signed or unsigned 32-bit word.")
        words.append(int(argument) & 0xFFFFFFFF)
    return event, tuple(words)
