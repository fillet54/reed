"""Lower XML ICDs (ElementTree DOMs) into the IR.

FieldDef refs are inlined: each fieldDef is lowered once, the first time it is
used, and the same object is placed at every use site. Refs to a message name
stay as MessageRef.

A use site can override the fieldDef: its tag and attributes win and the
def's attributes fill in the rest (name, ref, xsi:type and countValue describe
the use and aren't merged); its <label>/<description> replace the def's. No
other children are allowed. A use with overrides gets its own node, shared by
uses with the same overrides.
`countValue` on a ref makes an array of the def.

An ICD names itself with `<namespace alias="x">uri</namespace>` (the alias is
optional); `ref="x:name"` with its own alias resolves locally. Another file is imported with
`<refSchemaLocation alias="s">uri file.xsd</refSchemaLocation>`: `s:name`
resolves in the loaded ICD whose namespace is `uri`. Without an import, a
prefix matches a loaded ICD's own alias. `load_files` finds imported files
next to the importing one, named after the uri's last segment (`.../s-icd`
-> `s-icd.xml`).

`parse` records each element's source line, so a LoweringError can say where
it is: `real.xml:123: missing required attribute 'bitCount' on <scalar ...>`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from contextlib import contextmanager
from dataclasses import replace
from xml.etree import ElementTree as ET
from xml.parsers import expat

from .ir import (
    DEFAULT,
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
    String,
    Struct,
    Union,
)
from .issues import Issue

XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XSI_TYPE = f"{{{XSI_NS}}}type"

# Children that describe their parent rather than being fields of it.
# Attributes that describe the use of a fieldDef rather than override it.
USE_ATTRS = {"name", "ref", XSI_TYPE, "countValue"}
# The only children a ref may have.
REF_CHILDREN = ("label", "description", "namedValues", "default")

META_TAGS = {"label", "description", "namedValues", "default"}


class LoweringError(ValueError):
    """A problem in an ICD. `line` is the source line of the element, when known."""

    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self):
        return f"line {self.line}: {self.message}" if self.line else self.message


class _Element(ET.Element):
    """An element that remembers its source line."""

    line: int | None = None


def parse(text: str) -> ET.Element:
    """Parse ICD XML, declaring the xsi prefix if the file uses it undeclared."""
    if "xsi:" in text and "xmlns:xsi=" not in text:
        text = re.sub(
            r"<([A-Za-z_][\w.\-]*)", rf'<\1 xmlns:xsi="{XSI_NS}"', text, count=1
        )
    parser = ET.XMLParser(target=ET.TreeBuilder(element_factory=_Element))
    parser.feed(text)
    root = parser.close()

    # ElementTree doesn't track lines; a quick expat pass sees the same
    # elements in the same (document) order.
    for elem, line in zip(root.iter(), _start_lines(text)):
        elem.line = line
    return root


def _start_lines(text: str) -> list[int]:
    lines: list[int] = []
    p = expat.ParserCreate()
    p.StartElementHandler = lambda name, attrs: lines.append(p.CurrentLineNumber)
    p.Parse(text, True)
    return lines


@contextmanager
def _at(elem: ET.Element):
    """Give an error from inside `elem` its line, unless something deeper did."""
    try:
        yield
    except LoweringError as e:
        if e.line is None:
            e.line = getattr(elem, "line", None)
        raise


class _FromUnion:
    """Fill namedValues fromUnion="U" from union U's cases, per message.

    U is looked up by name in the groups enclosing the field, innermost first,
    so a shared fieldDef (e.g. a header with the id) gets the union of the
    message it's used in. Only the path down to such a field is copied.
    """

    def __init__(self, lowering: Lowering):
        self.lowering = lowering

    def fill(self, node, scopes: tuple, names: tuple):
        if isinstance(node, Int):
            return self.integer(node, scopes, names)
        if isinstance(node, Struct):
            inner = scopes + (node,)
            fields = tuple(
                self.fill(f, inner, names + ((f.name,) if getattr(f, "name", "") else ()))
                for f in node.fields
            )
            return node if _same(fields, node.fields) else replace(node, fields=fields)
        if isinstance(node, Union):
            cases = tuple(
                replace(c, body=self.fill(c.body, scopes, names + (c.name,))) for c in node.cases
            )
            same = all(a.body is b.body for a, b in zip(cases, node.cases))
            return node if same else replace(node, cases=cases)
        if isinstance(node, Array):
            element = self.fill(node.element, scopes, names)
            return node if element is node.element else replace(node, element=element)
        return node

    def integer(self, node: Int, scopes: tuple, names: tuple) -> Int:
        enum = node.enum
        if enum is None or not enum.from_union or enum.from_union.startswith("#"):
            return node  # "#id" was resolved when lowered
        for scope in reversed(scopes):
            for f in scope.fields:
                if isinstance(f, Union) and f.name == enum.from_union:
                    cases = tuple(
                        NamedValue(c.name, c.value) for c in f.cases if isinstance(c.value, int)
                    )
                    return replace(node, enum=replace(enum, values=cases + enum.values))

        self.lowering.issues.append(
            Issue(
                "warning",
                "from-union-not-found",
                "/".join(names),
                f"namedValues fromUnion {enum.from_union!r}: no union of that name "
                "encloses this field",
            )
        )
        return node


def _renamed(node, name: str):
    """`node` under its use-site name. Spares have no name and stay as they are."""
    return node if isinstance(node, Spare) else replace(node, name=name)


def _same(a: tuple, b: tuple) -> bool:
    return len(a) == len(b) and all(x is y for x, y in zip(a, b))


def _merged(elem: ET.Element, overrides, tag: str | None = None) -> ET.Element:
    """A copy of fieldDef `elem` with the use site's attributes (and tag) applied."""
    merged = _Element(tag or elem.tag, {**elem.attrib, **dict(overrides)})
    merged.extend(list(elem))
    merged.line = getattr(elem, "line", None)
    return merged


def _describe(elem: ET.Element) -> str:
    """`<scalar name='freq'>`, for messages."""
    for attr in ("name", "id"):
        if elem.get(attr):
            return f"<{_tag(elem)} {attr}={elem.get(attr)!r}>"
    return f"<{_tag(elem)}>"


def lower_string(text: str, lowering: type[Lowering] | None = None) -> Icd:
    (icd,) = lower_many([text], lowering).values()
    return icd


def load_files(
    paths: Iterable[str | Path],
    lowering: type[Lowering] | None = None,
    layout: LayoutPolicy | None = None,
) -> dict[str, Icd]:
    """Lower ICD files from disk, plus the files they import (transitively).

    An import's file is looked for next to the importing file; if it isn't
    there, a ref through it fails with a hint. Each file is loaded once.
    """
    queue = [Path(p) for p in paths]
    seen: set[Path] = set()
    roots, names = [], []

    while queue:
        path = queue.pop(0)
        if path.resolve() in seen:
            continue
        seen.add(path.resolve())

        text = path.read_text(encoding="utf-8")
        with _blame(str(path)):
            root = parse(text)
        roots.append(root)
        names.append(str(path))

        for _, uri in _imports(root):
            candidate = path.parent / f"{_file_stem(uri)}.xml"
            if candidate.exists():
                queue.append(candidate)

    return lower_many(roots, lowering, names, layout)


def _imports(root: ET.Element):
    """(alias, uri) of each refSchemaLocation that has both."""
    for child in root:
        if _tag(child) == "refSchemaLocation":
            parts = (child.text or "").split()
            if child.get("alias") and parts:
                yield child.get("alias"), parts[0]


def _file_stem(uri: str) -> str:
    """https://example.com/support-icd -> support-icd"""
    return uri.rstrip("/").rsplit("/", 1)[-1]


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
            module = cls(root, catalog=catalog, layout=layout, source=name)
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
        if isinstance(exc, LoweringError):
            where = f"{self.name}:{exc.line}" if exc.line else self.name
            raise LoweringError(f"{where}: {exc.message}") from exc
        if isinstance(exc, ET.ParseError):
            raise LoweringError(f"{self.name}: {exc}") from exc
        return False


class Catalog:
    """The loaded ICDs, plus lowering state shared across them."""

    def __init__(self):
        self.modules: dict[str, Lowering] = {}
        self.from_union = False  # some namedValues use fromUnion: messages need filling
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
        source: str | None = None,
    ):
        self.root = root
        self.layout = layout or LayoutPolicy()
        self.source = source  # file name, for warnings
        self.issues: list[Issue] = []  # warnings; lowering continues
        self.namespace = ""
        self.alias = ""
        self.def_elems: dict[str, ET.Element] = {}
        self.message_names: set[str] = set()

        namespaces = [c for c in root if _tag(c) == "namespace"]
        if len(namespaces) > 1:
            raise LoweringError("more than one <namespace> element")
        if namespaces:
            self.alias = namespaces[0].get("alias", "")  # optional; rarely used
            self.namespace = (namespaces[0].text or "").strip()

        # Prefixes usable in refs: its own, and its imports (alias -> uri).
        self.aliases: dict[str, Lowering] = {self.alias: self} if self.alias else {}
        self.imports: dict[str, str] = {}
        for child in root:
            if _tag(child) == "refSchemaLocation":
                alias = self.required(child, "alias")
                parts = (child.text or "").split()
                if not parts:
                    raise LoweringError(
                        f"<refSchemaLocation alias={alias!r}> has no namespace uri",
                        line=getattr(child, "line", None),
                    )
                self.imports[alias] = parts[0]

        self.catalog = catalog or Catalog()
        if self.namespace in self.catalog.modules:
            raise LoweringError(
                f"namespace {self.namespace!r} is declared by more than one ICD"
            )
        self.catalog.modules[self.namespace] = self

        for child in root:
            if _tag(child) == "fieldDefs":
                for d in child:
                    if not d.get("id"):
                        self.warn(
                            "def-without-id",
                            d,
                            f"{_describe(d)} in fieldDefs has no id, so nothing can ref it; skipped",
                        )
                        continue
                    self.def_elems[d.get("id")] = d
            elif _tag(child) == "message":
                self.message_names.add(self.required(child, "name"))

        self.defs: dict[tuple, Node] = {}  # (key, overrides) -> lowered fieldDef

    # --- entry points -------------------------------------------------------

    def icd(self) -> Icd:
        messages = tuple(
            self.message(child) for child in self.root if _tag(child) == "message"
        )
        return Icd(
            namespace=self.namespace,
            messages=messages,
            alias=self.alias,
            layout=self.layout,
            issues=tuple(self.issues),
        )

    def message(self, elem: ET.Element) -> Message:
        with _at(elem):
            name = self.required(elem, "name")
            body = Struct(name, self.fields(elem), meta=self.meta(elem))
            if self.catalog.from_union:
                body = _FromUnion(self).fill(body, (), (name,))
            return Message(key=(name,), body=body, namespace=self.namespace)

    def field(self, elem: ET.Element) -> Node:
        with _at(elem):
            xsi_type = elem.get(XSI_TYPE)
            if xsi_type:  # on a ref too: never silently ignored
                method = getattr(self, f"lower_type_{xsi_type}", None)
                if method is None:
                    raise LoweringError(f"unknown xsi:type {xsi_type!r} on {_describe(elem)}")
                return method(elem)

            if elem.get("ref"):
                return self.ref(elem)

            method = getattr(self, f"lower_{_tag(elem)}", None)
            if method is None:
                raise LoweringError(f"unknown element <{_tag(elem)}>")
            return method(elem)

    def fields(self, elem: ET.Element) -> tuple[Node, ...]:
        return tuple(self.field(c) for c in elem if _tag(c) not in META_TAGS)

    # --- node types ---------------------------------------------------------

    def lower_integer(self, elem):
        enum = self.enum(elem)
        return Int(
            self.field_name(elem),
            self.fixed_bits(elem),
            signed=self.flag(elem, "signed"),
            enum=enum,
            meta=self.meta(elem),
            default=self.default(elem, enum),
        )

    def lower_scalar(self, elem):
        return Scaled(
            self.field_name(elem),
            self.fixed_bits(elem),
            scale=float(elem.get("scale", 1)),
            offset=float(elem.get("offset", 0)),
            signed=self.flag(elem, "signed"),
            meta=self.meta(elem),
            default=self.default(elem, None, scaled=True),
        )

    def lower_string(self, elem):
        bits = self.bits(elem)
        if bits is None:
            self.required(elem, "bitCount")  # raises
        return String(self.field_name(elem), bits, meta=self.meta(elem))

    def lower_spare(self, elem):
        return Spare(self.fixed_bits(elem))

    def lower_group(self, elem):
        name = elem.get("name") or elem.get("id") or ""
        meta = self.meta(elem)
        struct = Struct(name, self.fields(elem), declared_bits=self.bits(elem), meta=meta)

        count = elem.get("countValue")
        if count:
            return Array(name, count, replace(struct, meta=Meta()), meta=meta)
        return struct

    def lower_union(self, elem):
        discrim = elem.get("discrimField")
        if discrim is None:
            if not self.catalog.in_progress:  # only fieldDefs may leave it to the use
                self.required(elem, "discrimField")  # raises
            discrim = ""
        return Union(
            elem.get("name", ""),
            discrim,
            tuple(case for c in elem if _tag(c) not in META_TAGS for case in self.cases(c)),
            meta=self.meta(elem),
        )

    def cases(self, elem: ET.Element):
        """Cases contributed by one union child. Dialects may yield several."""
        yield self.case(elem)

    def case(self, elem: ET.Element) -> Case:
        with _at(elem):
            raw = self.required(elem, "discrimValue")
            value = DEFAULT if raw == "default" else _int(raw)

            if _tag(elem) == "spare":
                # A case with no fields, just spare bits: often the default
                # case, for commands without a body.
                name = elem.get("name") or ("default" if value is DEFAULT else f"spare{value}")
                body = Struct(name, (Spare(self.fixed_bits(elem)),))
                return Case(name, "spare", value, body)

            name = self.required(elem, "name")
            if elem.get("ref"):
                body = self.ref(elem, merge=False)  # its attributes are the case's
            else:
                body = Struct(name, self.fields(elem), meta=self.meta(elem))

            return Case(name, _tag(elem), value, body)

    # --- refs ---------------------------------------------------------------

    def ref(self, elem: ET.Element, merge: bool = True) -> Node:
        """A use of a fieldDef or message. With `merge`, the use overrides the def."""
        ref = elem.get("ref")
        module, key = self.module_for(ref)
        name = elem.get("name") or key
        is_def = key in module.def_elems
        is_message = key in module.message_names

        for child in elem:
            if _tag(child) not in REF_CHILDREN:
                raise LoweringError(
                    f"{_describe(elem)} refs {ref!r}, so it can only have <label> and "
                    f"<description> children, not <{_tag(child)}>"
                )

        if is_def and is_message:
            raise LoweringError(f"ambiguous ref {ref!r}: both a fieldDef and a message")
        if is_message:
            return MessageRef(name, (module.namespace, key))
        if not is_def:
            raise LoweringError(f"unresolved ref {ref!r} on <{_tag(elem)} name={name!r}>")

        if not merge:
            return _renamed(module.field_def(key), name)

        overrides = tuple(sorted((k, v) for k, v in elem.attrib.items() if k not in USE_ATTRS))
        def_elem = module.def_elems[key]
        tag = None if self.same_kind(elem, def_elem) else elem.tag
        node = _renamed(module.field_def(key, overrides, tag), name)

        if tag and hasattr(node, "meta"):
            # Reported by validation (ref-tag-mismatch), per use.
            note = (
                f"{_describe(elem)} refs {ref!r}, which is a <{_tag(def_elem)}>"
                + (f" (line {def_elem.line})" if getattr(def_elem, "line", None) else "")
                + f"; the <{_tag(elem)}> wins"
            )
            if getattr(elem, "line", None):
                note = f"line {elem.line}: {note}"
            props = node.meta.props + (("ref-tag-mismatch", note),)
            node = replace(node, meta=replace(node.meta, props=props))

        if elem.find("namedValues") is not None:
            if not isinstance(node, Int):
                raise LoweringError(
                    f"{_describe(elem)}: <namedValues> only applies to integers, "
                    f"and {ref!r} is a <{_tag(def_elem)}>"
                )
            node = replace(node, enum=self.enum(elem))
        if elem.find("default") is not None:
            if not isinstance(node, (Int, Scaled)):
                raise LoweringError(
                    f"{_describe(elem)}: <default> only applies to integers and scalars"
                )
            enum = node.enum if isinstance(node, Int) else None
            node = replace(node, default=self.default(elem, enum, scaled=isinstance(node, Scaled)))

        label, description = (elem.findtext(tag) for tag in ("label", "description"))
        if (label is not None or description is not None) and hasattr(node, "meta"):
            meta = node.meta
            node = replace(
                node,
                meta=replace(
                    meta,
                    label=label.strip() if label is not None else meta.label,
                    description=description.strip() if description is not None else meta.description,
                ),
            )

        count = elem.get("countValue")
        if count:
            if not hasattr(node, "meta"):  # a spare
                return Array(name, count, node)
            return Array(name, count, replace(node, meta=Meta()), meta=node.meta)
        return node

    def same_kind(self, a: ET.Element, b: ET.Element) -> bool:
        """Same tag, or tags this dialect lowers the same way (e.g. group2/group)."""
        if a.tag == b.tag:
            return True
        lower_a = getattr(type(self), f"lower_{_tag(a)}", None)
        return lower_a is not None and lower_a is getattr(type(self), f"lower_{_tag(b)}", None)

    def module_for(self, ref: str) -> tuple[Lowering, str]:
        """Split `alias:key` and find the ICD it names."""
        if ":" not in ref:
            return self, ref

        alias, key = ref.split(":", 1)
        if alias in self.aliases:
            return self.aliases[alias], key

        if alias in self.imports:
            uri = self.imports[alias]
            module = self.catalog.modules.get(uri)
            if module is None:
                raise LoweringError(
                    f"alias {alias!r} imports {uri!r}, which is not loaded "
                    f"(pass its file too, e.g. {_file_stem(uri)}.xml)"
                )
            return module, key

        # No import line: a loaded ICD that calls itself by this alias.
        for module in self.catalog.modules.values():
            if module.alias and module.alias == alias:
                return module, key
        raise LoweringError(f"unknown namespace alias {alias!r} in ref {ref!r}")

    def field_def(
        self, key: str, overrides: tuple[tuple[str, str], ...] = (), tag: str | None = None
    ) -> Node:
        """The lowered fieldDef `key`, with the use site's attribute `overrides`
        applied and, if given, its `tag` instead of the def's."""
        memo = (key, overrides, tag)
        if memo in self.defs:
            return self.defs[memo]

        stack = self.catalog.in_progress
        here = (self.namespace, key)
        if here in stack:
            chain = " -> ".join(
                f"{ns}:{k}" if ns else k for ns, k in stack[stack.index(here):] + [here]
            )
            raise LoweringError(f"fieldDef ref cycle: {chain}")

        elem = self.def_elems[key]
        if overrides or tag:
            elem = _merged(elem, overrides, tag)

        stack.append(here)
        try:
            node = self.field(elem)
        finally:
            stack.pop()

        if hasattr(node, "meta"):
            node = replace(node, meta=replace(node.meta, origin=here))
        self.defs[memo] = node
        return node

    # --- attribute helpers --------------------------------------------------

    def warn(self, code: str, elem: ET.Element, message: str) -> None:
        """Note a problem and carry on; validation reports it with the others."""
        line = getattr(elem, "line", None)
        if self.source and line:
            where = f"{self.source}:{line}"
        else:
            where = self.source or (f"line {line}" if line else "(lowering)")
        self.issues.append(Issue("warning", code, where, message))

    def field_name(self, elem: ET.Element) -> str:
        """`name`, or for a fieldDef its `id` (the use site renames it)."""
        if elem.get("name") is None and elem.get("id") is not None:
            return elem.get("id")
        return self.required(elem, "name")

    def required(self, elem: ET.Element, attr: str) -> str:
        value = elem.get(attr)
        if value is None:
            raise LoweringError(
                f"missing required attribute {attr!r} on {_describe(elem)}",
                line=getattr(elem, "line", None),
            )
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
        values = tuple(
            NamedValue(self.required(v, "name"), _int(self.required(v, "value"))) for v in nv
        )
        from_union = nv.get("fromUnion", "")
        if from_union.startswith("#"):
            values = self.union_def_values(nv, from_union) + values
        elif from_union:
            self.catalog.from_union = True  # filled in per message
        return Enum(values, restrict=self.flag(nv, "restrict"), from_union=from_union)

    def union_def_values(self, nv: ET.Element, from_union: str) -> tuple[NamedValue, ...]:
        """fromUnion="#id": the cases of union fieldDef `id` (or `#alias:id`)."""
        module, key = self.module_for(from_union[1:])
        where = f"namedValues fromUnion {from_union!r}"
        if key not in module.def_elems:
            self.warn("from-union-not-found", nv, f"{where}: no fieldDef with id {key!r}")
            return ()
        tag = _tag(module.def_elems[key])
        if tag != "union":
            self.warn(
                "from-union-not-found", nv, f"{where}: fieldDef {key!r} is a <{tag}>, not a <union>"
            )
            return ()
        union = module.field_def(key)
        return tuple(NamedValue(c.name, c.value) for c in union.cases if isinstance(c.value, int))

    def default(self, elem: ET.Element, enum: Enum | None, scaled: bool = False):
        """<default>: a number, or for an integer a name from its named values."""
        text = elem.findtext("default")
        if text is None:
            return None
        text = text.strip()
        try:
            return float(text) if scaled else _int(text)
        except ValueError:
            pass
        for nv in enum.values if enum else ():
            if nv.name == text:
                return nv.value
        raise LoweringError(
            f"default {text!r} of {_describe(elem)} is not a number"
            + ("" if scaled else " or one of its named values")
        )

    def flag(self, elem: ET.Element, attr: str) -> bool:
        raw = elem.get(attr, "false")
        if raw.lower() not in ("true", "false"):
            raise LoweringError(f"{attr} must be true or false on <{_tag(elem)}>, got {raw!r}")
        return raw.lower() == "true"
