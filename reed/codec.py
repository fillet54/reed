"""Codec: Python values ⇄ packed binary messages.

Values mirror the message:
- a group is a dict of its fields (spares are left out);
- an integer is an int, or a name from its named values;
- a scalar is a number: engineering value = raw × scale + offset;
- a float is a number, stored as its IEEE 754 bits;
- hexdata is `bytes` (a bytearray is fine too), exactly its size; a variable
  one's count field is filled in from its length;
- a string is a str, one Latin-1 byte per character;
- a union is `{case_name: case_value}`; an unnamed union's case name appears
  directly in the enclosing dict;
- an array is a list of element dicts;
- a carried message (MessageRef) is a dict of that message's fields.

Encoding fills in discriminators from the chosen case and counts from list
lengths; a value given for them must agree. Decoding returns them. A field
left out uses its <default>, if it has one (a default needn't be one of the
restricted named values: it's often a "none" value like 0).

Bits are packed MSB-first into words of `layout.word_bits`, each word stored in
`layout.byteorder`. Fields may cross word boundaries. A message always fills
whole words: with `layout.pad` the rest of its last word is an implicit zero
spare, for a carried message too, so fields after one start on a word. Without
it the message must be whole words itself, and a carried one packs tightly.

The layout is the one of the ICD holding the message (set by the dialect).
"""

from __future__ import annotations

import struct
from collections.abc import Mapping

from .ir import DEFAULT, STRING_LENGTH_BITS, VARIABLE, Array, Bytes, Float, Icd, Int, String, LayoutPolicy, Message, MessageRef, Scaled, Spare, Struct, Union
from .paths import Frame, Stack, children, field_path, push, resolve
from .size import find_message, layout_of

MISSING = object()


class CodecError(ValueError):
    pass


def encode(
    message: Message,
    value: Mapping,
    icds: Mapping[str, Icd] | None = None,
) -> bytes:
    icds = icds or {}
    layout = layout_of(icds, message)
    encoder = _Encoder(icds)
    encoder.collect((Frame(message.body, (), (message.name,)),), value)
    bits, nbits = encoder.emit()
    if not layout.pad and nbits % layout.word_bits:
        raise CodecError(
            f"{message.name}: {nbits} bits is not a whole number of {layout.word_bits}-bit words"
        )
    return _to_bytes(bits, nbits, layout)


def decode(
    message: Message,
    data: bytes,
    icds: Mapping[str, Icd] | None = None,
) -> dict:
    icds = icds or {}
    layout = layout_of(icds, message)
    bits, nbits = _from_bytes(data, layout, message.name)
    decoder = _Decoder(icds, _Reader(bits, nbits))

    value = decoder.read((Frame(message.body, (), (message.name,)),))

    left = nbits - decoder.reader.pos
    if left >= layout.word_bits or (left and not layout.pad):
        raise CodecError(f"{message.name}: {left} bits left over after the message")
    return value


# --- encoding -----------------------------------------------------------------


class _Encoder:
    def __init__(self, icds):
        self.icds = icds
        self.leaves: list[tuple[Frame, object]] = []  # stream order
        self.derived: dict[tuple, tuple[int, str]] = {}  # index -> (raw, reason)
        # A default case's discriminator: index -> (case, other cases' values)
        self.open: dict[tuple, tuple[str, dict]] = {}

    def collect(self, stack: Stack, value) -> None:
        frame = stack[-1]
        node = frame.node

        if isinstance(node, (Int, Scaled, Spare)):
            self.leaves.append((frame, value))
        elif isinstance(node, String):
            self.leaves.append((frame, _string(node, value, frame)))
        elif isinstance(node, Float):
            self.leaves.append((frame, _float(node, value, frame)))
        elif isinstance(node, Bytes):
            self.hexdata(stack, value)
        elif isinstance(node, Struct):
            self.struct(stack, value)
        elif isinstance(node, Union):
            self.union(stack, value)
        elif isinstance(node, Array):
            self.array(stack, value)
        elif isinstance(node, MessageRef):
            self.message_ref(stack, value)
        else:
            raise TypeError(f"cannot encode {type(node).__name__}")

    def struct(self, stack: Stack, value) -> None:
        frame = stack[-1]
        if value is MISSING:
            value = {}
        if not isinstance(value, Mapping):
            raise CodecError(f"{_path(frame)}: expected a dict, got {value!r}")

        known = set()
        for child in children(frame):
            node = child.node
            if isinstance(node, Spare):
                self.collect(push(stack, child), 0)
            elif isinstance(node, Union) and not node.name:
                names = [c.name for c in node.cases]
                known.update(names)
                chosen = {k: value[k] for k in names if k in value}
                self.collect(push(stack, child), chosen if chosen else MISSING)
            else:
                known.add(node.name)
                self.collect(push(stack, child), value.get(node.name, MISSING))

        for key in value:
            if key not in known:
                raise CodecError(f"{_path(frame)}: unknown field {key!r}")

    def union(self, stack: Stack, value) -> None:
        frame = stack[-1]
        node: Union = frame.node
        names = [c.name for c in node.cases]

        if not isinstance(value, Mapping) or len(value) != 1 or next(iter(value)) not in names:
            raise CodecError(f"{_path(frame)}: expected one of {', '.join(names)}")

        (name, case_value), = value.items()
        index = names.index(name)
        case = node.cases[index]
        if not node.discrim:
            raise CodecError(f"{_path(frame)}: union has no discrimField")

        target = resolve(node.discrim, stack)
        if target is None:
            raise CodecError(f"{_path(frame)}: discrimField {node.discrim!r} does not resolve")
        if case.value is DEFAULT:
            # The value is the caller's (or the field's default); it just
            # mustn't select another case.
            others = {c.value: c.name for c in node.cases if isinstance(c.value, int)}
            self.open[target.index] = (case.name, others)
        else:
            self.derive(target, case.value, f"case {case.name!r}")

        self.collect(push(stack, children(frame)[index]), case_value)

    def array(self, stack: Stack, value) -> None:
        frame = stack[-1]
        node: Array = frame.node
        if not isinstance(value, list):
            raise CodecError(f"{_path(frame)}: expected a list, got {value!r}")

        target = resolve(node.count, stack)
        if target is None:
            raise CodecError(f"{_path(frame)}: countValue {node.count!r} does not resolve")
        self.derive(target, len(value), f"{len(value)} items")

        for i, item in enumerate(value):
            self.collect(push(stack, _item(frame, i)), item)

    def hexdata(self, stack: Stack, value) -> None:
        frame = stack[-1]
        node: Bytes = frame.node
        if value is MISSING:
            raise CodecError(f"{_path(frame)}: missing")
        if not isinstance(value, (bytes, bytearray)):
            raise CodecError(f"{_path(frame)}: expected bytes, got {value!r}")

        if node.bits is VARIABLE:
            target = resolve(node.count, stack)
            if target is None:
                raise CodecError(f"{_path(frame)}: countVariable {node.count!r} does not resolve")
            self.derive(target, len(value), f"{len(value)} bytes")
        elif len(value) * 8 != node.bits:
            raise CodecError(
                f"{_path(frame)}: {len(value)} bytes; the field holds {node.bits // 8}"
            )
        self.leaves.append((frame, _Packed(len(value) * 8, int.from_bytes(value, "big"))))

    def message_ref(self, stack: Stack, value) -> None:
        frame = stack[-1]
        target = find_message(self.icds, frame.node.target)
        if target is None:
            raise CodecError(f"{_path(frame)}: message {frame.node.target} is not loaded")
        # The carried message is its own scope: its paths can't reach out.
        start = len(self.leaves)
        self.collect((Frame(target.body, frame.index, frame.names),), value)

        # ...and fills whole words of its own.
        used = sum(_leaf_bits(f, v) for f, v in self.leaves[start:])
        layout = layout_of(self.icds, target)
        pad = -used % layout.word_bits if layout.pad else 0
        if pad:
            self.leaves.append((Frame(Spare(pad), frame.index + ("pad",), frame.names), 0))

    def derive(self, target: Frame, value, reason: str) -> None:
        """Record that `target` must hold `value` (an int, or a tuple for a group)."""
        if isinstance(target.node, Struct):
            fields = children(target)
            values = value if isinstance(value, tuple) else (value,)
            for child, v in zip(fields, values):
                self.derive(child, v, reason)
            return
        self.derived[target.index] = (value, reason)

    def emit(self) -> tuple[int, int]:
        bits = nbits = 0
        for frame, value in self.leaves:
            if isinstance(value, _Packed):  # already bits (strings)
                bits = (bits << value.nbits) | value.raw
                nbits += value.nbits
                continue

            node = frame.node
            derived = self.derived.get(frame.index)

            if derived is not None:
                need, reason = derived
                if value is not MISSING and _to_raw(node, value, frame) != _to_raw(node, need, frame):
                    raise CodecError(f"{_path(frame)}: is {value!r} but {reason} needs {need!r}")
                value = need

            restrict = True
            if value is MISSING and getattr(node, "default", None) is not None:
                value, restrict = node.default, False
            opened = self.open.get(frame.index)
            if value is MISSING:
                if opened:
                    raise CodecError(
                        f"{_path(frame)}: missing; case {opened[0]!r} is the default case, "
                        "so give a value"
                    )
                raise CodecError(f"{_path(frame)}: missing")

            raw = 0 if isinstance(node, Spare) else _to_raw(node, value, frame, restrict)
            if opened:
                number = _signed(raw, node)
                if number in opened[1]:
                    raise CodecError(
                        f"{_path(frame)}: {number} belongs to case {opened[1][number]!r}, "
                        "not the default case"
                    )
            bits = (bits << node.bits) | raw
            nbits += node.bits

        return bits, nbits


class _Packed:
    """A leaf turned into its bits up front, e.g. a string of some length."""

    __slots__ = ("nbits", "raw")

    def __init__(self, nbits: int, raw: int):
        self.nbits, self.raw = nbits, raw


def _leaf_bits(frame: Frame, value) -> int:
    return value.nbits if isinstance(value, _Packed) else frame.node.bits


def _string(node: String, value, frame: Frame) -> _Packed:
    if value is MISSING:
        raise CodecError(f"{_path(frame)}: missing")
    if not isinstance(value, str):
        raise CodecError(f"{_path(frame)}: expected a string, got {value!r}")
    try:
        data = value.encode("latin-1")
    except UnicodeEncodeError as e:
        raise CodecError(f"{_path(frame)}: {e.object[e.start]!r} is not a one-byte character")

    if node.bits is VARIABLE:
        if len(data) > 255:
            raise CodecError(
                f"{_path(frame)}: {len(data)} characters; a variable string holds at most 255"
            )
        data = bytes([len(data)]) + data
    else:
        size = node.bits // 8
        if len(data) > size:
            raise CodecError(
                f"{_path(frame)}: {value!r} is {len(data)} characters; the field holds {size}"
            )
        data = data.ljust(size, b"\0")
    return _Packed(len(data) * 8, int.from_bytes(data, "big"))


_FLOAT_FORMAT = {32: ">f", 64: ">d"}


def _float(node: Float, value, frame: Frame) -> _Packed:
    if value is MISSING:
        value = node.default
    if value is None or value is MISSING:
        raise CodecError(f"{_path(frame)}: missing")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodecError(f"{_path(frame)}: expected a number, got {value!r}")
    try:
        data = struct.pack(_FLOAT_FORMAT[node.bits], value)
    except OverflowError:
        raise CodecError(f"{_path(frame)}: {value} does not fit in a {node.bits}-bit float")
    return _Packed(node.bits, int.from_bytes(data, "big"))


def _to_raw(node, value, frame: Frame, restrict: bool = True) -> int:
    """The unsigned raw bits for a leaf value."""
    if isinstance(node, Scaled):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CodecError(f"{_path(frame)}: expected a number, got {value!r}")
        number = round((value - node.offset) / node.scale)
    else:
        number = _int_value(node, value, frame, restrict)

    bits = node.bits
    if node.signed:
        lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    else:
        lo, hi = 0, (1 << bits) - 1
    if not lo <= number <= hi:
        kind = "signed" if node.signed else "unsigned"
        raise CodecError(f"{_path(frame)}: {number} does not fit in {bits} {kind} bits")

    return number & ((1 << bits) - 1)


def _int_value(node: Int, value, frame: Frame, restrict: bool = True) -> int:
    enum = node.enum
    if isinstance(value, str):
        if enum is not None:
            for nv in enum.values:
                if nv.name == value:
                    return nv.value
        raise CodecError(f"{_path(frame)}: {value!r} is not a named value")

    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError(f"{_path(frame)}: expected an integer, got {value!r}")

    if restrict and enum is not None and enum.restrict and value not in {nv.value for nv in enum.values}:
        raise CodecError(f"{_path(frame)}: {value} is not one of the named values")
    return value


# --- decoding -----------------------------------------------------------------


class _Reader:
    def __init__(self, bits: int, nbits: int):
        self.bits = bits
        self.nbits = nbits
        self.pos = 0

    def read(self, n: int, frame: Frame) -> int:
        if self.pos + n > self.nbits:
            raise CodecError(f"{_path(frame)}: message ends after {self.nbits} bits")
        shift = self.nbits - self.pos - n
        self.pos += n
        return (self.bits >> shift) & ((1 << n) - 1)


class _Decoder:
    def __init__(self, icds, reader: _Reader):
        self.icds = icds
        self.reader = reader
        self.values: dict[tuple, object] = {}  # index -> decoded number

    def read(self, stack: Stack):
        frame = stack[-1]
        node = frame.node

        if isinstance(node, Spare):
            self.reader.read(node.bits, frame)
            return MISSING
        if isinstance(node, (Int, Scaled)):
            return self.leaf(frame)
        if isinstance(node, String):
            return self.string(frame)
        if isinstance(node, Bytes):
            if node.bits is VARIABLE:
                size = self.number_at(resolve(node.count, stack), frame, "countVariable")
            else:
                size = node.bits // 8
            return self.reader.read(size * 8, frame).to_bytes(size, "big")
        if isinstance(node, Float):
            data = self.reader.read(node.bits, frame).to_bytes(node.bits // 8, "big")
            return struct.unpack(_FLOAT_FORMAT[node.bits], data)[0]
        if isinstance(node, Struct):
            return self.struct(stack)
        if isinstance(node, Union):
            return self.union(stack)
        if isinstance(node, Array):
            return self.array(stack)
        if isinstance(node, MessageRef):
            target = find_message(self.icds, node.target)
            if target is None:
                raise CodecError(f"{_path(frame)}: message {node.target} is not loaded")
            start = self.reader.pos
            value = self.read((Frame(target.body, frame.index, frame.names),))
            layout = layout_of(self.icds, target)
            if layout.pad:
                self.reader.read(-(self.reader.pos - start) % layout.word_bits, frame)
            return value
        raise TypeError(f"cannot decode {type(node).__name__}")

    def leaf(self, frame: Frame):
        node = frame.node
        raw = self.reader.read(node.bits, frame)
        if node.signed and raw >> (node.bits - 1):
            raw -= 1 << node.bits

        if isinstance(node, Scaled):
            value = raw * node.scale + node.offset
            self.values[frame.index] = value
            return value

        self.values[frame.index] = raw
        if node.enum is not None:
            for nv in node.enum.values:
                if nv.value == raw:
                    return nv.name
            if node.enum.restrict:
                raise CodecError(f"{_path(frame)}: {raw} is not one of the named values")
        return raw

    def string(self, frame: Frame) -> str:
        node: String = frame.node
        if node.bits is VARIABLE:
            size = self.reader.read(STRING_LENGTH_BITS, frame)
        else:
            size = node.bits // 8
        data = self.reader.read(size * 8, frame).to_bytes(size, "big")
        if node.bits is not VARIABLE:
            data = data.split(b"\0", 1)[0]
        return data.decode("latin-1")

    def struct(self, stack: Stack) -> dict:
        result = {}
        for child in children(stack[-1]):
            value = self.read(push(stack, child))
            node = child.node
            if value is MISSING:
                continue
            if isinstance(node, Union) and not node.name:
                result.update(value)
            else:
                result[node.name] = value
        return result

    def union(self, stack: Stack) -> dict:
        frame = stack[-1]
        node: Union = frame.node
        if not node.discrim:
            raise CodecError(f"{_path(frame)}: union has no discrimField")
        discrim = self.number_at(resolve(node.discrim, stack), frame, "discrimField")

        fallback = None
        for i, case in enumerate(node.cases):
            if case.value == discrim:
                return {case.name: self.read(push(stack, children(frame)[i]))}
            if case.value is DEFAULT:
                fallback = i
        if fallback is not None:
            case = node.cases[fallback]
            return {case.name: self.read(push(stack, children(frame)[fallback]))}
        raise CodecError(f"{_path(frame)}: no case for discriminator value {discrim}")

    def array(self, stack: Stack) -> list:
        frame = stack[-1]
        count = self.number_at(resolve(frame.node.count, stack), frame, "countValue")
        return [self.read(push(stack, _item(frame, i))) for i in range(count)]

    def number_at(self, target: Frame | None, frame: Frame, attr: str):
        """The decoded value of a discriminator or count: an int, or a tuple for a group."""
        if target is None:
            raise CodecError(f"{_path(frame)}: {attr} does not resolve")
        if isinstance(target.node, Struct):
            return tuple(self.number_at(c, frame, attr) for c in children(target))
        if target.index not in self.values:
            raise CodecError(f"{_path(frame)}: {attr} {field_path(target)!r} is not decoded yet")
        return self.values[target.index]


# --- helpers ------------------------------------------------------------------


def _signed(raw: int, node) -> int:
    """The number that raw bits stand for."""
    if node.signed and raw >> (node.bits - 1):
        return raw - (1 << node.bits)
    return raw


def _item(frame: Frame, i: int) -> Frame:
    """Frame for array element `i`. Each element gets its own index."""
    return Frame(frame.node.element, frame.index + (i,), frame.names + (f"[{i}]",))


def _path(frame: Frame) -> str:
    return "/".join(frame.names)


def _to_bytes(bits: int, nbits: int, layout: LayoutPolicy) -> bytes:
    word = layout.word_bits
    words = -(-nbits // word)
    total = words * word
    data = (bits << (total - nbits)).to_bytes(total // 8, "big")
    return _swap_words(data, layout)


def _from_bytes(data: bytes, layout: LayoutPolicy, name: str) -> tuple[int, int]:
    word_bytes = layout.word_bits // 8
    if len(data) % word_bytes:
        raise CodecError(
            f"{name}: {len(data)} bytes is not a whole number of {layout.word_bits}-bit words"
        )
    data = _swap_words(data, layout)
    return int.from_bytes(data, "big"), len(data) * 8


def _swap_words(data: bytes, layout: LayoutPolicy) -> bytes:
    if layout.byteorder == "big":
        return data
    n = layout.word_bits // 8
    return b"".join(data[i : i + n][::-1] for i in range(0, len(data), n))
