"""The normalized message model every reed tool works on.

Nodes are frozen and have no parent pointer, so one lowered fieldDef can be
placed at every use site without copying. Context (path from the root, bit
offset) is passed down by the tool walking the tree, never stored on a node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union as TypingUnion

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


@dataclass(frozen=True, slots=True)
class LayoutPolicy:
    word_bits: int = 16
    msb_first: bool = True  # bit 15 is the first bit of a word
    max_bits: int = 512 * 8


@dataclass(frozen=True, slots=True)
class NamedValue:
    name: str
    value: int


@dataclass(frozen=True, slots=True)
class Enum:
    values: tuple[NamedValue, ...]
    restrict: bool = False  # only these values are legal


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


@dataclass(frozen=True, slots=True)
class Scaled:
    name: str
    bits: int
    scale: float = 1.0
    offset: float = 0.0
    signed: bool = False  # raw bits are two's complement
    meta: Meta = field(default_factory=Meta)


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
    value: int | tuple[int, ...]
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
    count: Path  # countValue: field holding the element count
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


Node = TypingUnion[Int, Scaled, Spare, Struct, Union, Array, MessageRef]


@dataclass(frozen=True, slots=True)
class Message:
    key: tuple[str, ...]  # ("command",) or after expansion ("command", "power", "fuel")
    body: Struct
    consts: tuple[tuple[Path, int], ...] = ()  # fields fixed by expansion

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
