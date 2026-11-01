"""Lower XML ICDs (ElementTree DOMs) into the IR.

FieldDef refs are inlined: each fieldDef is lowered once, the first time it is
used, and the same object is placed at every use site. Refs to a message name
stay as MessageRef.

An ICD names itself with `<namespace alias="x">uri</namespace>`; `ref="x:name"`
with its own alias resolves locally. Imports of other files (schemaLocation)
will add more aliases.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from xml.etree import ElementTree as ET

from .ir import (
    VARIABLE,
    Array,
    Case,
    Enum,
    Icd,
    Int,
    LayoutPolicy,
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

XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XSI_TYPE = f"{{{XSI_NS}}}type"

# Children that describe their parent rather than being fields of it.
META_TAGS = {"label", "description", "namedValues"}


class LoweringError(ValueError):
    pass


def parse(text: str) -> ET.Element:
    """Parse ICD XML, declaring the xsi prefix if the file uses it undeclared."""
    if "xsi:" in text and "xmlns:xsi=" not in text:
        text = re.sub(
            r"<([A-Za-z_][\w.\-]*)", rf'<\1 xmlns:xsi="{XSI_NS}"', text, count=1
        )
    return ET.fromstring(text)


def lower_string(text: str, lowering: type[Lowering] | None = None) -> Icd:
    (icd,) = lower_many([text], lowering).values()
    return icd


def lower_many(
    sources: Iterable[str | ET.Element],
    lowering: type[Lowering] | None = None,
    names: Iterable[str] | None = None,
    layout: LayoutPolicy | None = None,
) -> dict[str, Icd]:
    """Lower several ICD files, keyed by the namespace uri each one declares.

    `names` (e.g. file paths) prefix error messages from the matching source.
    `layout` (from the dialect) is given to every ICD.
    """
    sources = list(sources)
    names = list(names) if names is not None else [None] * len(sources)
    catalog = Catalog()
    cls = lowering or Lowering
    name_of: dict[str, str | None] = {}

    for source, name in zip(sources, names):
        with _blame(name):
            root = parse(source) if isinstance(source, str) else source
            module = cls(root, catalog=catalog, layout=layout)
        name_of[module.namespace] = name

    icds = {}
    for uri, module in catalog.modules.items():
        with _blame(name_of.get(uri)):
            icds[uri] = module.icd()
    return icds


class _blame:
    """Prefix lowering and XML errors with the source name."""

    def __init__(self, name: str | None):
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None or self.name is None:
            return False
        if isinstance(exc, (LoweringError, ET.ParseError)):
            raise LoweringError(f"{self.name}: {exc}") from exc
        return False


class Catalog:
    """The loaded ICDs, plus lowering state shared across them."""

    def __init__(self):
        self.modules: dict[str, Lowering] = {}
        self.in_progress: list[tuple[str, str]] = []  # fieldDefs being lowered


def _tag(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def _int(value: str) -> int:
    return int(value, 0) if value.lower().startswith("0x") else int(value)


class Lowering:
    """Base lowering. Dialects subclass it and add `lower_<tag>` methods."""

    def __init__(
        self,
        root: ET.Element,
        catalog: Catalog | None = None,
        layout: LayoutPolicy | None = None,
    ):
        self.root = root
        self.layout = layout or LayoutPolicy()
        self.namespace = ""
        self.alias = ""
        self.def_elems: dict[str, ET.Element] = {}
        self.message_names: set[str] = set()

        namespaces = [c for c in root if _tag(c) == "namespace"]
        if len(namespaces) > 1:
            raise LoweringError("more than one <namespace> element")
        if namespaces:
            self.alias = self.required(namespaces[0], "alias")
            self.namespace = (namespaces[0].text or "").strip()

        # Prefixes usable in refs. Imports (schemaLocation) will add to this.
        self.aliases: dict[str, Lowering] = {self.alias: self} if self.alias else {}

        self.catalog = catalog or Catalog()
        if self.namespace in self.catalog.modules:
            raise LoweringError(
                f"namespace {self.namespace!r} is declared by more than one ICD"
            )
        self.catalog.modules[self.namespace] = self

        for child in root:
            if _tag(child) == "fieldDefs":
                for d in child:
                    self.def_elems[self.required(d, "id")] = d
            elif _tag(child) == "message":
                self.message_names.add(self.required(child, "name"))

        self.defs: dict[str, Node] = {}

    # --- entry points -------------------------------------------------------

    def icd(self) -> Icd:
        messages = tuple(
            self.message(child) for child in self.root if _tag(child) == "message"
        )
        return Icd(
            namespace=self.namespace, messages=messages, alias=self.alias, layout=self.layout
        )

    def message(self, elem: ET.Element) -> Message:
        name = self.required(elem, "name")
        return Message(key=(name,), body=Struct(name, self.fields(elem), meta=self.meta(elem)))

    def field(self, elem: ET.Element) -> Node:
        xsi_type = elem.get(XSI_TYPE)
        if xsi_type and not elem.get("ref"):
            method = getattr(self, f"lower_type_{xsi_type}", None)
            if method is None:
                raise LoweringError(f"unknown xsi:type {xsi_type!r} on <{_tag(elem)}>")
            return method(elem)

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
            tuple(case for c in elem if _tag(c) not in META_TAGS for case in self.cases(c)),
            meta=self.meta(elem),
        )

    def cases(self, elem: ET.Element):
        """Cases contributed by one union child. Dialects may yield several."""
        yield self.case(elem)

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
        ref = elem.get("ref")
        module, key = self.module_for(ref)
        name = elem.get("name") or key
        is_def = key in module.def_elems
        is_message = key in module.message_names

        if is_def and is_message:
            raise LoweringError(f"ambiguous ref {ref!r}: both a fieldDef and a message")
        if is_message:
            return MessageRef(name, (module.namespace, key))
        if not is_def:
            raise LoweringError(f"unresolved ref {ref!r} on <{_tag(elem)} name={name!r}>")

        return replace(module.field_def(key), name=name)

    def module_for(self, ref: str) -> tuple[Lowering, str]:
        """Split `alias:key` and find the ICD it names."""
        if ":" not in ref:
            return self, ref

        alias, key = ref.split(":", 1)
        if alias not in self.aliases:
            raise LoweringError(f"unknown namespace alias {alias!r} in ref {ref!r}")
        return self.aliases[alias], key

    def field_def(self, key: str) -> Node:
        if key in self.defs:
            return self.defs[key]

        stack = self.catalog.in_progress
        here = (self.namespace, key)
        if here in stack:
            chain = " -> ".join(
                f"{ns}:{k}" if ns else k for ns, k in stack[stack.index(here):] + [here]
            )
            raise LoweringError(f"fieldDef ref cycle: {chain}")

        stack.append(here)
        try:
            node = self.field(self.def_elems[key])
        finally:
            stack.pop()

        if hasattr(node, "meta"):
            node = replace(node, meta=replace(node.meta, origin=here))
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
