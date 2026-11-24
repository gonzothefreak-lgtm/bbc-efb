"""Host-declared SimConnect Client Data areas and byte regions."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contract import StrEnum


MAX_AREA_BYTES = 8192
CLIENT_DATA_WRITE = "client_data.write"


class ClientDataMode(StrEnum):
    ONCE = "once"
    ON_SET = "on_set"
    SECOND = "second"
    VISUAL_FRAME = "visual_frame"


@dataclass(frozen=True, slots=True)
class ClientDataArea:
    """A host-declared Client Data area.

    The name is sent to SimConnect exactly as written (surrounding spaces removed), so use
    the spelling the area owner publishes, for example ``PMDG_NG3_Data``. Whether SimConnect
    matches Client Data names case-sensitively is not yet verified natively, and the owner's
    spelling is right either way. ``preserve_case=False`` keeps the earlier upper-casing for
    a host that relied on it. Core treats two names that differ only in case as the same
    declaration.
    """

    name: str
    size: int
    create: bool = False
    writable: bool = False
    creator_only: bool = True
    # Only shapes ``name``; two declarations with the same resulting name are equal.
    preserve_case: bool = field(default=True, compare=False)

    def __post_init__(self):
        if not isinstance(self.name, str):
            raise ValueError("Supply an ASCII Client Data area name.")
        name = self.name.strip()
        if not name or len(name) > 255 or not name.isascii() or not name.isprintable():
            raise ValueError("Supply a printable ASCII Client Data area name of at most 255 bytes.")
        if type(self.size) is not int or not 1 <= self.size <= MAX_AREA_BYTES:
            raise ValueError("Client Data area size must be between 1 and 8,192 bytes.")
        if any(type(flag) is not bool
               for flag in (self.create, self.writable, self.creator_only, self.preserve_case)):
            raise ValueError("Client Data area options must be Boolean.")
        object.__setattr__(self, "name", name if self.preserve_case else name.upper())

    @property
    def key(self) -> str:
        """Case-insensitive identity used to detect duplicate declarations."""
        return self.name.casefold()

    def region(self, offset=0, size=None) -> ClientDataRegion:
        if type(offset) is not int:
            raise ValueError("Client Data offsets must be integers.")
        return ClientDataRegion(self, offset, self.size - offset if size is None else size)


@dataclass(frozen=True, slots=True)
class ClientDataRegion:
    area: ClientDataArea
    offset: int
    size: int

    def __post_init__(self):
        if not isinstance(self.area, ClientDataArea):
            raise ValueError("A Client Data region requires an area.")
        if (type(self.offset) is not int or type(self.size) is not int
                or self.offset < 0 or self.size < 1 or self.offset + self.size > self.area.size):
            raise ValueError("The region must fit within the declared Client Data area.")

    @property
    def capability_id(self):
        return f"client_data:{self.area.name}:{self.offset}:{self.size}"
