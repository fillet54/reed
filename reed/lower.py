"""Lower an XML ICD (ElementTree DOM) into the IR.

FieldDef refs are inlined: each fieldDef is lowered once, the first time it is
used, and the same object is placed at every use site. Refs to a message name
stay as MessageRef.
"""

from __future__ import annotations

from dataclasses import replace
from xml.etree import ElementTree as ET

from .ir import (
    VARIABLE,
    Array,
    Case,
    Enum,
    Icd,
    Int,
    Message,
    MessageRef,
    Meta,
    NamedValue,
    Node,
    Scaled,
    Spare,
    Struct,
    Union,
)

# Children that describe their parent rather than being fields of it.
META_TAGS = {"label", "description", "namedValues"}


class LoweringError(ValueError):
    pass


def lower_string(text: str) -> Icd:
    return lower_tree(ET.fromstring(text))


def lower_tree(root: ET.Element) -> Icd:
    return Lowering(root).icd()


def _tag(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def _int(value: str) -> int:
    return int(value, 0) if value.lower().startswith("0x") else int(value)


class Lowering:
    """Base lowering. Dialects subclass it and add `lower_<tag>` methods."""

    def __init__(self, root: ET.Element, namespace: str = ""):
        self.root = root
        self.namespace = namespace
        self.def_elems: dict[str, ET.Element] = {}
        self.message_names: set[str] = set()

        for child in root:
            if _tag(child) == "fieldDefs":
                for d in child:
                    self.def_elems[self.required(d, "id")] = d
            elif _tag(child) == "message":
                self.message_names.add(self.required(child, "name"))

        self.defs: dict[str, Node] = {}
        self.in_progress: list[str] = []

    # --- entry points -------------------------------------------------------

    def icd(self) -> Icd:
        messages = tuple(
            self.message(child) for child in self.root if _tag(child) == "message"
        )
        return Icd(namespace=self.namespace, messages=messages)

    def message(self, elem: ET.Element) -> Message:
        name = self.required(elem, "name")
        return Message(key=(name,), body=Struct(name, self.fields(elem), meta=self.meta(elem)))

    def field(self, elem: ET.Element) -> Node:
        method = getattr(self, f"lower_{_tag(elem)}", None)
        if method is None:
            raise LoweringError(f"unknown element <{_tag(elem)}>")
        return method(elem)

    def fields(self, elem: ET.Element) -> tuple[Node, ...]:
        return tuple(self.field(c) for c in elem if _tag(c) not in META_TAGS)

    # --- node types ---------------------------------------------------------

    def lower_integer(self, elem):
        return Int(
            self.required(elem, "name"),
            self.fixed_bits(elem),
            signed=self.flag(elem, "signed"),
            enum=self.enum(elem),
            meta=self.meta(elem),
        )

    def lower_scalar(self, elem):
        return Scaled(
            self.required(elem, "name"),
            self.fixed_bits(elem),
            scale=float(elem.get("scale", 1)),
            offset=float(elem.get("offset", 0)),
            signed=self.flag(elem, "signed"),
            meta=self.meta(elem),
        )

    def lower_spare(self, elem):
        return Spare(self.fixed_bits(elem))

    def lower_group(self, elem):
        if elem.get("ref"):
            return self.ref(elem)

        name = elem.get("name") or elem.get("id") or ""
        meta = self.meta(elem)
        struct = Struct(name, self.fields(elem), declared_bits=self.bits(elem), meta=meta)

        count = elem.get("countValue")
        if count:
            return Array(name, count, replace(struct, meta=Meta()), meta=meta)
        return struct

    def lower_union(self, elem):
        return Union(
            elem.get("name", ""),
            self.required(elem, "discrimField"),
            tuple(self.case(c) for c in elem if _tag(c) not in META_TAGS),
            meta=self.meta(elem),
        )

    def case(self, elem: ET.Element) -> Case:
        name = self.required(elem, "name")
        value = _int(self.required(elem, "discrimValue"))

        if elem.get("ref"):
            body = self.ref(elem)
        else:
            body = Struct(name, self.fields(elem), meta=self.meta(elem))

        return Case(name, _tag(elem), value, body)

    # --- refs ---------------------------------------------------------------

    def ref(self, elem: ET.Element) -> Node:
        key = elem.get("ref")
        name = elem.get("name") or key
        is_def = key in self.def_elems
        is_message = key in self.message_names

        if is_def and is_message:
            raise LoweringError(f"ambiguous ref {key!r}: both a fieldDef and a message")
        if is_message:
            return MessageRef(name, (self.namespace, key))
        if not is_def:
            raise LoweringError(f"unresolved ref {key!r} on <{_tag(elem)} name={name!r}>")

        return replace(self.field_def(key), name=name)

    def field_def(self, key: str) -> Node:
        if key in self.defs:
            return self.defs[key]

        if key in self.in_progress:
            chain = " -> ".join(self.in_progress[self.in_progress.index(key):] + [key])
            raise LoweringError(f"fieldDef ref cycle: {chain}")

        self.in_progress.append(key)
        try:
            node = self.field(self.def_elems[key])
        finally:
            self.in_progress.pop()

        if hasattr(node, "meta"):
            node = replace(node, meta=replace(node.meta, origin=key))
        self.defs[key] = node
        return node

    # --- attribute helpers --------------------------------------------------

    def required(self, elem: ET.Element, attr: str) -> str:
        value = elem.get(attr)
        if value is None:
            raise LoweringError(f"missing required attribute {attr!r} on <{_tag(elem)}>")
        return value

    def bits(self, elem: ET.Element):
        raw = elem.get("bitCount")
        if raw is None:
            return None
        if raw == "variable":
            return VARIABLE
        return _int(raw)

    def fixed_bits(self, elem: ET.Element) -> int:
        bits = self.bits(elem)
        if bits is None:
            self.required(elem, "bitCount")
        if bits is VARIABLE:
            raise LoweringError(f"bitCount must be a number on <{_tag(elem)}>")
        return bits

    def meta(self, elem: ET.Element) -> Meta:
        label = elem.findtext("label")
        description = elem.findtext("description")
        return Meta(
            label=label.strip() if label else None,
            description=description.strip() if description else None,
        )

    def enum(self, elem: ET.Element) -> Enum | None:
        nv = elem.find("namedValues")
        if nv is None:
            return None
        return Enum(
            tuple(
                NamedValue(self.required(v, "name"), _int(self.required(v, "value")))
                for v in nv
            ),
            restrict=self.flag(nv, "restrict"),
        )

    def flag(self, elem: ET.Element, attr: str) -> bool:
        raw = elem.get(attr, "false")
        if raw.lower() not in ("true", "false"):
            raise LoweringError(f"{attr} must be true or false on <{_tag(elem)}>, got {raw!r}")
        return raw.lower() == "true"
