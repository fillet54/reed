"""HTML viewer: every message's variant tree, in one self-contained page.

`field_rows(message, icds)` lays a message out as table rows: each field with
its bit offset (unknown after a variable-size field), size, type, named
values, default, docs and, in a variant, its fixed discriminator value. A
union that wasn't split lists every case from the same offset; array element
fields have offsets relative to the element; a carried message is one row
that links to its own tree.

`page(icds, dialect)` expands the ICDs and embeds the trees as JSON in a page
whose script draws a navigation tree, a word-by-word bit map and the field
table. The page needs no server or network: open the file.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Mapping
from importlib import resources

from .expand import Variant, expand
from .ir import (
    DEFAULT,
    VARIABLE,
    Array,
    Bytes,
    Float,
    Icd,
    Int,
    Message,
    MessageRef,
    Scaled,
    Spare,
    String,
    Struct,
    Union,
)
from .paths import Stack, children, push, root_stack
from .size import Sizer, describe_size, layout_of

LEAVES = (Int, Scaled, Float, String, Bytes, Spare)


def field_rows(message: Message, icds: Mapping[str, Icd]) -> list[dict]:
    """`message` as table rows, in stream order (see the module docstring)."""
    layout = layout_of(icds, message)
    rows = _Rows(icds, message, layout.word_bits)
    end = rows.fields(root_stack(message.body, message.name), 0, 0, (), False, True)

    if layout.pad and end is not None and end % layout.word_bits:
        bits = -end % layout.word_bits
        rows.add("(pad)", "pad", "implicit spare", 0, end, bits, f"{bits} bits", (), False, True)
    return rows.rows


class _Rows:
    def __init__(self, icds: Mapping[str, Icd], message: Message, word_bits: int):
        self.icds = icds
        self.word_bits = word_bits
        self.sizer = Sizer(icds)
        self.consts = {path: _jsonable(value) for path, value in message.consts}
        self.rows: list[dict] = []

    def add(self, name, kind, type_, depth, offset, bits, size, path, relative, tiles, **extra):
        row = {
            "depth": depth,
            "name": name,
            "path": "/".join(path),
            "kind": kind,
            "type": type_,
            "offset": offset,
            "bits": bits,
            "size": size,
            "relative": relative,
            "map": tiles,  # part of the bit map: fields that tile the message
            **dict(zip(("word", "bit_span"), word_span(offset, bits, self.word_bits, relative))),
            "const": self.consts.get("/".join(path)),
            "values": [],
            "restrict": False,
            "default": None,
            "label": None,
            "description": None,
            "case_value": None,
            "link": None,
        }
        row.update(extra)
        self.rows.append(row)
        return row

    def fields(self, stack: Stack, depth, offset, path, relative, tiles):
        """Rows for the fields of the struct on top of `stack`; the offset after them."""
        for child in children(stack[-1]):
            offset = self.field(push(stack, child), depth, offset, path, relative, tiles)
        return offset

    def field(self, stack: Stack, depth, offset, path, relative, tiles):
        node = stack[-1].node
        size = self.sizer.size(stack)
        bits = size.lo if size.is_fixed else None
        name = getattr(node, "name", "") or ""
        here = path + (name,) if name else path
        args = (depth, offset, bits, str(size), here, relative)
        meta = getattr(node, "meta", None)
        docs = {"label": meta.label, "description": meta.description} if meta else {}

        if isinstance(node, LEAVES):
            extra = dict(docs)
            if isinstance(node, Int) and node.enum is not None:
                extra["values"] = [[v.name, v.value] for v in node.enum.values]
                extra["restrict"] = node.enum.restrict
            if getattr(node, "default", None) is not None:
                extra["default"] = node.default
            kind = type(node).__name__.lower()
            kind = {"int": "integer", "scaled": "scalar", "float": "float"}.get(kind, kind)
            self.add(name or "(spare)", kind, _type(node), *args, tiles, **extra)

        elif isinstance(node, Struct):
            row = self.add(name or "(group)", "group", "group", *args, tiles, **docs)
            if isinstance(row["const"], list):  # a system ID: one value per field
                for field, value in zip(node.fields, row["const"]):
                    self.consts.setdefault("/".join(here + (field.name,)), value)
            self.fields(stack, depth + 1, offset, here, relative, tiles)

        elif isinstance(node, Union):
            self.add(name or "(union)", "union", f"union on {node.discrim}", *args, tiles, **docs)
            for case, frame in zip(node.cases, children(stack[-1])):
                case_stack = push(stack, frame)
                case_size = self.sizer.size(case_stack)
                case_path = here + (case.name,)
                value = _jsonable(case.value)
                self.add(
                    case.name,
                    "case",
                    f"= {value}" if case.value is not DEFAULT else "default case",
                    depth + 1,
                    offset,
                    case_size.lo if case_size.is_fixed else None,
                    str(case_size),
                    case_path,
                    relative,
                    False,
                    case_value=value,
                    label=case.meta.label or getattr(case.body, "meta", case.meta).label,
                    description=case.meta.description
                    or getattr(case.body, "meta", case.meta).description,
                )
                if isinstance(case.body, Struct):
                    self.fields(case_stack, depth + 2, offset, case_path, relative, False)
                else:
                    self.field(case_stack, depth + 2, offset, case_path, relative, False)

        elif isinstance(node, Array):
            self.add(name, "array", f"array × {node.count}", *args, tiles, **docs)
            (element,) = children(stack[-1])
            element_stack = push(stack, element)
            if isinstance(element.node, Struct):
                self.fields(element_stack, depth + 1, 0, here, True, False)
            else:
                self.field(element_stack, depth + 1, 0, here, True, False)

        elif isinstance(node, MessageRef):
            link = message_key(self.icds, *node.target)
            self.add(name, "message", f"message {link}", *args, tiles, link=link)

        else:
            raise TypeError(f"no rows for {type(node).__name__}")

        if offset is None or bits is None:
            return None
        return offset + bits


def word_span(offset, bits, word_bits: int, relative: bool = False) -> tuple[str, str]:
    """The words a field covers and its bits in them, numbered MSB first.

    A 4-bit field at the start of a 16-bit word is ("0", "15–12"); a 32-bit one
    is ("2–3", "15–0"). An unknown end is "…", an unknown offset (or no bits)
    "—", and an offset relative to an array element has a "+".
    """
    if offset is None:
        return "—", "—"
    first, start = divmod(offset, word_bits)
    hi = word_bits - 1 - start
    sign = "+" if relative else ""
    if bits is None:
        return f"{sign}{first}–…", f"{hi}–…"
    if bits == 0:
        return f"{sign}{first}", "—"
    last, end = divmod(offset + bits - 1, word_bits)
    lo = word_bits - 1 - end
    words = f"{sign}{first}" if last == first else f"{sign}{first}–{last}"
    return words, str(hi) if hi == lo and bits == 1 else f"{hi}–{lo}"


def _type(node) -> str:
    sign = "int" if getattr(node, "signed", False) else "uint"
    if isinstance(node, Int):
        return f"{sign}{node.bits}"
    if isinstance(node, Scaled):
        return f"scaled {sign}{node.bits} × {node.scale:g} + {node.offset:g}"
    if isinstance(node, Float):
        return f"float{node.bits}"
    if isinstance(node, String):
        if node.bits is VARIABLE:
            return "string, variable (8-bit length)"
        return f"string, {node.bits // 8} chars"
    if isinstance(node, Bytes):
        if node.bits is VARIABLE:
            return f"hexdata × {node.count}"
        return f"hexdata, {node.bits // 8} bytes"
    return "spare"


def _show(value) -> str:
    """A discriminator value as text: 4, (4, 1, 2) or default."""
    if isinstance(value, tuple):
        return "(" + ", ".join(map(_show, value)) + ")"
    return "default" if value is DEFAULT else str(value)


def _jsonable(value):
    if value is DEFAULT:
        return "default"
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


# --- the page -----------------------------------------------------------------


def icd_label(icd: Icd) -> str:
    return icd.alias or icd.namespace


def message_key(icds: Mapping[str, Icd], namespace: str, name: str) -> str:
    """How the page names a message (or variant): "alias:name", or just "name"."""
    icd = icds.get(namespace)
    label = icd_label(icd) if icd is not None else namespace
    return f"{label}:{name}" if label else name


def page(icds: Mapping[str, Icd], dialect, title: str = "ICD") -> str:
    """The viewer page for `icds`, expanded by `dialect`."""
    trees = expand(icds, dialect)
    data = {
        "title": title,
        "dialect": dialect.name,
        "icds": [
            {
                "label": icd_label(icds[namespace]),
                "namespace": namespace,
                "messages": [_variant(v, icds, namespace) for v in roots],
            }
            for namespace, roots in trees.items()
        ],
    }
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    template = resources.files("reed").joinpath("viewer.html").read_text(encoding="utf-8")
    return template.replace("{{title}}", html.escape(title)).replace("{{data}}", blob)


def _variant(variant: Variant, icds, namespace: str) -> dict:
    message = variant.message
    return {
        "key": message_key(icds, namespace, variant.name),
        "name": variant.name,
        "id": ", ".join(_show(v) for v in variant.id),
        "size": describe_size(message, icds),
        "consts": [[path, _jsonable(value)] for path, value in message.consts],
        "word_bits": layout_of(icds, message).word_bits,
        "label": message.body.meta.label,
        "description": message.body.meta.description,
        "rows": field_rows(message, icds),
        "children": [_variant(c, icds, namespace) for c in variant.children],
    }
