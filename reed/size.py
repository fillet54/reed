"""Size analysis: bits a node takes, as a range plus an exact formula when one exists.

    Size(lo, hi, linear)

`lo`/`hi` bound the size in bits (`hi` may be math.inf when a count can't be
resolved). `linear` is `a + Σ bᵢ·countᵢ` when the size is exactly that, which
is the case for fixed layouts and arrays of fixed elements; unions whose cases
differ have no formula.

With `layout.pad` (the default) a message fills whole words of its ICD's
`layout.word_bits`: what is left of the last word is an implicit spare.
Message sizes include it, carried messages too; field and group sizes don't.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .ir import STRING_LENGTH_BITS, VARIABLE, Array, Icd, Int, LayoutPolicy, Message, MessageRef, MsgKey, Scaled, Spare, String, Struct, Union
from .paths import Stack, children, field_path, push, resolve, root_stack


@dataclass(frozen=True, slots=True)
class Linear:
    const: int = 0
    terms: tuple[tuple[str, int], ...] = ()  # (count field path, bits per count)

    def __add__(self, other: Linear) -> Linear:
        terms = dict(self.terms)
        for name, bits in other.terms:
            terms[name] = terms.get(name, 0) + bits
        return Linear(self.const + other.const, tuple(terms.items()))

    def __str__(self):
        parts = [str(self.const)] if self.const or not self.terms else []
        parts += [f"{bits}·{name}" for name, bits in self.terms]
        return " + ".join(parts)


@dataclass(frozen=True, slots=True)
class Size:
    lo: int
    hi: int | float
    linear: Linear | None = None

    @classmethod
    def fixed(cls, bits: int) -> Size:
        return cls(bits, bits, Linear(bits))

    @property
    def is_fixed(self) -> bool:
        return self.lo == self.hi

    def __add__(self, other: Size) -> Size:
        linear = self.linear + other.linear if self.linear and other.linear else None
        return Size(self.lo + other.lo, self.hi + other.hi, linear)

    @staticmethod
    def join(sizes: list[Size]) -> Size:
        """One of several alternatives (union cases)."""
        if not sizes:
            return Size.fixed(0)
        linears = {s.linear for s in sizes}
        linear = sizes[0].linear if len(linears) == 1 else None
        return Size(min(s.lo for s in sizes), max(s.hi for s in sizes), linear)

    def padded(self, word_bits: int) -> Size:
        """This size rounded up to whole words."""

        def up(bits):
            return bits if bits == math.inf else -(-bits // word_bits) * word_bits

        linear = self.linear
        if linear is not None and not linear.terms:
            linear = Linear(up(linear.const))
        elif linear is not None and (
            linear.const % word_bits or any(b % word_bits for _, b in linear.terms)
        ):
            linear = None  # rounding up a + b·n isn't linear
        return Size(up(self.lo), up(self.hi), linear)

    def __str__(self):
        hi = "∞" if self.hi == math.inf else str(self.hi)
        if self.is_fixed:
            return f"{self.lo} bits"
        if self.linear is not None:
            return f"{self.linear} bits ({self.lo}–{hi})"
        return f"{self.lo}–{hi} bits"


UNBOUNDED = Size(0, math.inf)


def size_of(
    message: Message,
    icds: Mapping[str, Icd] | None = None,
    choose: Mapping[int, object] | None = None,
    padded: bool = True,
) -> Size:
    """Size of a whole message, with its implicit spare unless `padded=False`.

    `icds` (keyed by namespace) is needed to size MessageRefs and find the word
    size. `choose` maps id(union) -> case to size the message with that case
    selected.
    """
    return Sizer(icds, choose).message(message, padded)


class Sizer:
    def __init__(self, icds=None, choose=None):
        self.icds = icds or {}
        self.choose = choose or {}
        self.active: set[MsgKey] = set()  # message refs being sized, to stop cycles

    def message(self, message: Message, padded: bool = True) -> Size:
        size = self.size(root_stack(message.body, message.name))
        layout = layout_of(self.icds, message)
        return size.padded(layout.word_bits) if padded and layout.pad else size

    def size(self, stack: Stack) -> Size:
        node = stack[-1].node

        if isinstance(node, (Int, Scaled, Spare)):
            return Size.fixed(node.bits)

        if isinstance(node, String):
            if node.bits is VARIABLE:
                return Size(STRING_LENGTH_BITS, STRING_LENGTH_BITS + 255 * 8)
            return Size.fixed(node.bits)

        if isinstance(node, Struct):
            total = Size.fixed(0)
            for child in children(stack[-1]):
                total = total + self.size(push(stack, child))
            return total

        if isinstance(node, Union):
            frames = children(stack[-1])
            chosen = self.choose.get(id(node))
            if chosen is not None:
                frames = [f for f, case in zip(frames, node.cases) if case is chosen]
            return Size.join([self.size(push(stack, f)) for f in frames])

        if isinstance(node, Array):
            return self.array(node, stack)

        if isinstance(node, MessageRef):
            return self.message_ref(node)

        raise TypeError(f"cannot size {type(node).__name__}")

    def array(self, node: Array, stack: Stack) -> Size:
        (element_frame,) = children(stack[-1])
        element = self.size(push(stack, element_frame))

        count_frame = resolve(node.count, stack)
        bounds = count_range(count_frame.node) if count_frame else None
        if bounds is None:
            return UNBOUNDED

        lo, hi = bounds
        linear = None
        if element.is_fixed:
            linear = Linear(0, ((field_path(count_frame), element.lo),))
        return Size(lo * element.lo, hi * element.hi, linear)

    def message_ref(self, node: MessageRef) -> Size:
        target = find_message(self.icds, node.target)
        if target is None or node.target in self.active:
            return UNBOUNDED

        self.active.add(node.target)
        try:
            return self.message(target)
        finally:
            self.active.discard(node.target)


def count_range(node) -> tuple[int, int] | None:
    """Smallest and largest value a count field can hold."""
    if not isinstance(node, Int):
        return None
    if node.enum is not None and node.enum.restrict and node.enum.values:
        values = [v.value for v in node.enum.values]
        return min(values), max(values)
    if node.signed:
        return 0, 2 ** (node.bits - 1) - 1
    return 0, 2**node.bits - 1


def layout_of(icds: Mapping[str, Icd], message: Message) -> LayoutPolicy:
    """The layout of the ICD that holds `message` (the default when not loaded)."""
    icd = icds.get(message.namespace)
    return icd.layout if icd is not None else LayoutPolicy()


def find_message(icds: Mapping[str, Icd], key: MsgKey) -> Message | None:
    namespace, name = key
    icd = icds.get(namespace)
    if icd is None:
        return None
    return next((m for m in icd.messages if m.name == name), None)
