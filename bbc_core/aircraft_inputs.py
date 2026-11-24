"""Aircraft Input Event argument encoding for the Core API."""

from __future__ import annotations

from math import isfinite
from struct import pack

from .contract import CoreRequestError, Scalar


INPUT_WRITE = "aircraft.input.set"


def encode_input_arguments(types: tuple[str, ...], values: tuple[Scalar, ...]) -> bytes:
    if len(types) != len(values):
        raise CoreRequestError("Supply the documented number of aircraft input arguments.")
    parts = []
    for kind, value in zip(types, values, strict=True):
        kind = kind.lower()
        if kind == "float64":
            try:
                if type(value) in (int, float) and isfinite(value):
                    parts.append(pack("<d", value))
                    continue
            except OverflowError:
                pass
            raise CoreRequestError("An aircraft input FLOAT64 argument must be finite.")
        if kind in ("char[256]", "char[260]"):
            size = 256 if kind == "char[256]" else 260
            if not isinstance(value, str) or not value.isascii() or "\0" in value or len(value) >= size:
                raise CoreRequestError(f"Supply an ASCII string shorter than {size} bytes.")
            parts.append(value.encode("ascii").ljust(size, b"\0"))
            continue
        raise CoreRequestError(f"Unsupported aircraft input argument type: {kind}.")
    return b"".join(parts)
