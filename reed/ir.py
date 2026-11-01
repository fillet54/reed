"""The normalized message model every reed tool works on.

Nodes are frozen and have no parent pointer, so one lowered fieldDef can be
placed at every use site without copying. Context (path from the root, bit
offset) is passed down by the tool walking the tree, never stored on a node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union as TypingUnion

from .issues import Issue

# Relative path to another field, e.g. "../header/id" (discrimField, countValue).
Path = str

# Points at a top-level message: (namespace uri, message name).
MsgKey = tuple[str, str]

# Names a fieldDef: (namespace uri, fieldDef id).
DefKey = tuple[str, str]


class _Variable:
    """Marker for bitCount="variable": the size is computed, not declared."""

    def __repr__(self):
        return "VARIABLE"


VARIABLE = _Variable()


class _Default:
    """Marker for discrimValue="default": the case used when no other matches."""

    def __repr__(self):
        return "default"


DEFAULT = _Default()


@dataclass(frozen=True, slots=True)
class LayoutPolicy:
    """How messages sit in words. Set per dialect; the XML has no syntax for it.

    Bits are always packed MSB-first: the first field is in the high bits.
    """

    word_bits: int = 16
    byteorder: str = "big"  # byte order within a word: "big" or "little"
    pad: bool = True  # fill a message's last word with an implicit spare;
    #                   if False, every message must be whole words itself
    max_bits: int = 512 * 8


@dataclass(frozen=True, slots=True)
class NamedValue:
    name: str
    value: int


@dataclass(frozen=True, slots=True)
class Enum:
    values: tuple[NamedValue, ...]
    restrict: bool = False  # only these values are legal
    from_union: str = ""  # values also come from this union's cases (filled in by lowering)


@dataclass(frozen=True, slots=True, kw_only=True)
class Meta:
    """Documentation and provenance shared by every field node."""

    label: str | None = None
    description: str | None = None
    origin: DefKey | None = None  # fieldDef this was inlined from
    props: tuple[tuple[str, object], ...] = ()  # dialect-specific attributes

    def prop(self, name: str, default=None):
        for key, value in self.props:
            if key == name:
                return value
        return default


@dataclass(frozen=True, slots=True)
class Int:
    name: str
    bits: int
    signed: bool = False
    enum: Enum | None = None
    meta: Meta = field(default_factory=Meta)
    default: int | None = None  # used when a codec isn't given a value


@dataclass(frozen=True, slots=True)
class Scaled:
    name: str
    bits: int
    scale: float = 1.0
    offset: float = 0.0
    signed: bool = False  # raw bits are two's complement
    meta: Meta = field(default_factory=Meta)
    default: float | None = None  # engineering value, used when a codec isn't given one


@dataclass(frozen=True, slots=True)
class Bytes:
    """Raw data (<hexdata>), in stream order.

    Fixed (`bits` a number): bits/8 bytes. Variable (`bits` is VARIABLE): as
    many bytes as the integer at `count` says, a path relative to this field
    like an array's countValue ("../n" is a sibling).
    """

    name: str
    bits: int | object  # int, or VARIABLE
    count: Path = ""
    meta: Meta = field(default_factory=Meta)


@dataclass(frozen=True, slots=True)
class Float:
    """An IEEE 754 binary float: 32 (single) or 64 (double) bits."""

    name: str
    bits: int
    meta: Meta = field(default_factory=Meta)
    default: float | None = None  # used when a codec isn't given a value


FLOAT_BITS = (32, 64)


@dataclass(frozen=True, slots=True)
class String:
    """Text, one byte per character.

    Fixed (`bits` a number): bits/8 characters, zero-filled when shorter, no
    terminator when full. Variable (`bits` is VARIABLE): an 8-bit length, then
    that many characters, no terminator, no alignment.
    """

    name: str
    bits: int | _Variable
    meta: Meta = field(default_factory=Meta)


STRING_LENGTH_BITS = 8  # the length prefix of a variable string


@dataclass(frozen=True, slots=True)
class Spare:
    bits: int


@dataclass(frozen=True, slots=True)
class Domain:
    """A tree of named values, e.g. system/subsystem/unit for a system ID."""

    name: str
    value: int
    children: tuple[Domain, ...] = ()


@dataclass(frozen=True, slots=True)
class Struct:
    name: str
    fields: tuple[Node, ...]
    declared_bits: int | _Variable | None = None  # checked against the computed size
    domain: tuple[Domain, ...] = ()  # allowed values when the struct is an ID
    meta: Meta = field(default_factory=Meta)


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    kind: str  # "command", "return", "system", ...
    value: int | tuple[int, ...] | _Default  # DEFAULT: when no other case matches
    body: Struct | MessageRef
    meta: Meta = field(default_factory=Meta)


@dataclass(frozen=True, slots=True)
class Union:
    name: str
    discrim: Path
    cases: tuple[Case, ...]
    meta: Meta = field(default_factory=Meta)


@dataclass(frozen=True, slots=True)
class Array:
    name: str
    count: Path  # countValue, relative to the array like discrimField: "../n" is a sibling
    element: Struct
    meta: Meta = field(default_factory=Meta)


@dataclass(frozen=True, slots=True)
class MessageRef:
    """A top-level message used inside another, e.g. a carried payload ICD.

    Kept as a reference (not inlined) so the target keeps its own path scope,
    layout and identity.
    """

    name: str
    target: MsgKey


Node = TypingUnion[Int, Scaled, Float, String, Bytes, Spare, Struct, Union, Array, MessageRef]


@dataclass(frozen=True, slots=True)
class Message:
    key: tuple[str, ...]  # ("command",) or after expansion ("command", "power", "fuel")
    body: Struct
    consts: tuple[tuple[Path, int | tuple[int, ...]], ...] = ()  # fields fixed by expansion
    namespace: str = ""  # of the ICD it belongs to (variants keep it)

    @property
    def name(self) -> str:
        """The XML message name; `key` adds the expanded variant after it."""
        return self.key[0]


@dataclass(frozen=True, slots=True)
class Icd:
    namespace: str  # uri from the file's own <namespace> element
    messages: tuple[Message, ...]
    alias: str = ""  # its prefix, e.g. "example" (refs, and <example:command> instances)
    layout: LayoutPolicy = LayoutPolicy()
    issues: tuple[Issue, ...] = ()  # warnings found while lowering
