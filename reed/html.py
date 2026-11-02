"""HTML viewer: every message's variant tree, in one self-contained page.

`field_rows(message, icds)` lays a message out as table rows: each field with
its bit offset (unknown after a variable-size field), size, type, named
values, default, docs and, in a variant, its fixed discriminator value. A
union that wasn't split lists every case from the same offset; array element
fields have offsets relative to the element; a carried message is one row
that links to its own tree.

`page(icds, dialect)` expands the ICDs and embeds the trees as JSON in a page
whose script draws a navigation tree and, for each fully expanded variant, the
field table and a word-by-word bit map. The page needs no server or network: open the file.
"""

from __future__ import annotations

import base64
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
from .lower import CASES_OF
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
            "hidden": False,  # nestContents="false": no group in the table
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
            hidden = not node.nest
            row = self.add(name or "(group)", "group", "group", *args, tiles, hidden=hidden, **docs)
            if isinstance(row["const"], list):  # a system ID: one value per field
                for field, value in zip(node.fields, row["const"]):
                    self.consts.setdefault("/".join(here + (field.name,)), value)
            self.fields(stack, depth + (not hidden), offset, here, relative, tiles)

        elif isinstance(node, Union) and node.meta.prop(CASES_OF):
            # Another union's cases (an <apCommand>): a link, not every case again.
            namespace, message, union_path = node.meta.prop(CASES_OF)
            self.add(
                name or "(union)",
                "union",
                f"union on {node.discrim}: cases of {message}/{union_path}",
                *args,
                tiles,
                link=message_key(self.icds, namespace, message),
                **docs,
            )

        elif isinstance(node, Union):
            hidden = not node.nest
            self.add(
                name or "(union)", "union", f"union on {node.discrim}", *args, tiles, hidden=hidden, **docs
            )
            depth -= hidden  # its cases sit where it would
            for case, frame in zip(node.cases, children(stack[-1])):
                case_stack = push(stack, frame)
                case_size = self.sizer.size(case_stack)
                case_path = here + (case.name,)
                value = _jsonable(case.value)
                self.add(
                    case.name,
                    "case",
                    f"= {_show(case.value)}" if case.value is not DEFAULT else "default case",
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

    Bits are numbered across all the words the field covers. A 4-bit field at
    the start of a 16-bit word is ("0", "15–12"); a 32-bit one is ("2–3",
    "31–0"); 16 bits from the middle of word 4 are ("4–5", "23–8"). An unknown end is "…", an unknown offset (or no bits)
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
    last = (offset + bits - 1) // word_bits
    top = (last - first + 1) * word_bits - 1  # the MSB of the words covered
    hi, lo = top - start, top - (start + bits - 1)
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
    return render(data)


def diff_page(tree: list[dict], dialect, title: str = "ICD diff", old: str = "old", new: str = "new") -> str:
    """The viewer page in diff mode, for a merged tree from `reed.diff.diff`: a
    summary of every change, then each variant's field table with them marked."""
    from .diff import counts

    data = {
        "title": title,
        "dialect": dialect.name,
        "diff": {"old": old, "new": new, "counts": counts(tree)},
        "icds": tree,
    }
    return render(data)


def render(data: dict) -> str:
    """The viewer page around `data`."""
    title = data["title"]
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    template = resources.files("reed").joinpath("viewer.html").read_text(encoding="utf-8")
    return (
        template.replace("{{title}}", html.escape(title))
        .replace("{{fonts}}", font_faces())
        .replace("{{data}}", blob)
    )


# IBM Plex (SIL Open Font License 1.1: reed/fonts/OFL.txt), embedded so the
# page needs no network.
FONTS = (
    ("IBM Plex Sans", 400, "IBMPlexSans-Regular"),
    ("IBM Plex Sans", 500, "IBMPlexSans-Medium"),
    ("IBM Plex Sans", 600, "IBMPlexSans-SemiBold"),
    ("IBM Plex Mono", 400, "IBMPlexMono-Regular"),
    ("IBM Plex Mono", 600, "IBMPlexMono-SemiBold"),
)


def font_faces() -> str:
    """@font-face rules for FONTS, with the fonts inline as data URIs."""
    fonts = resources.files("reed").joinpath("fonts")
    rules = []
    for family, weight, file in FONTS:
        data = base64.b64encode(fonts.joinpath(f"{file}.woff2").read_bytes()).decode("ascii")
        rules.append(
            f'@font-face {{ font-family: "{family}"; font-weight: {weight}; font-style: normal; '
            f'font-display: swap; src: url(data:font/woff2;base64,{data}) format("woff2"); }}'
        )
    return "\n".join(rules)


def _variant(variant: Variant, icds, namespace: str) -> dict:
    """A variant for the page. Only a leaf (fully expanded) has rows and a
    layout: a parent is shown by its variants."""
    message = variant.message
    shown = {
        "consts": [[path, _jsonable(value)] for path, value in message.consts],
        "word_bits": layout_of(icds, message).word_bits,
        "rows": field_rows(message, icds),
    }
    return {
        "key": message_key(icds, namespace, variant.name),
        "name": variant.name,
        "id": ", ".join(_show(v) for v in variant.id),
        "size": describe_size(message, icds),
        "label": message.body.meta.label,
        "description": message.body.meta.description,
        **({} if variant.children else shown),
        "children": [_variant(c, icds, namespace) for c in variant.children],
    }
