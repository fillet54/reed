"""Expansion: split a message into variants, one per case of its listed unions.

A variant is the message with some unions replaced by their chosen case:
- the case's fields go in a group named after the union (an unnamed union's
  fields go straight into the parent);
- the discriminator stays in the layout, fixed: `Message.consts` maps its
  path in the variant to the case value.

The dialect lists which unions split, by the path of their discriminator in
the original message, starting with the message name; `*` matches one level
and an alias prefix (`payload:command/...`) limits a pattern to one ICD:

    Dialect(..., expand=("command/header/id", "command/commandBody/*/subid"))

Expansion repeats inside each variant, so the result is a tree: the root is the
message itself and leaves are fully expanded. Two splittable unions side by
side nest in field order. Unions inside arrays, inside unsplit unions, and
inside carried messages are left alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .ir import DEFAULT, Array, Icd, Message, MessageRef, Struct, Union
from .paths import Frame, Stack, children, push, resolve, root_stack
from .validate import Issue, rule

if TYPE_CHECKING:
    from .dialect import Dialect


class ExpandError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Variant:
    id: tuple  # message name, then each chosen case's value: ("return", 1, (10, 1, 0))
    message: Message  # key = message name, then each chosen case's name
    children: tuple[Variant, ...] = ()

    @property
    def name(self) -> str:
        return "/".join(self.message.key)


def expand(icds: Mapping[str, Icd], dialect: Dialect) -> dict[str, tuple[Variant, ...]]:
    """The variant tree of every message, keyed by namespace.

    Refuses ICDs with validation errors: e.g. duplicate case values would give
    two variants the same id.
    """
    errors = [i for i in dialect.validate(icds) if i.severity == "error"]
    if errors:
        raise ExpandError("not expanding; fix these first:\n" + "\n".join(map(str, errors)))

    return {
        namespace: tuple(
            expand_message(message, dialect.expand, icd.alias) for message in icd.messages
        )
        for namespace, icd in icds.items()
    }


def expand_message(message: Message, patterns: Iterable[str], alias: str = "") -> Variant:
    return _Expander(message, tuple(patterns), alias).variant({}, (), ())


# --- patterns -----------------------------------------------------------------


def matches(pattern: str, alias: str, names: tuple[str, ...]) -> bool:
    """Does `pattern` match a discriminator at `names` (message name first)?"""
    segments = pattern.split("/")
    if ":" in segments[0]:
        prefix, segments[0] = segments[0].split(":", 1)
        if prefix != alias:
            return False
    return len(segments) == len(names) and all(
        s == "*" or s == n for s, n in zip(segments, names)
    )


def matches_message(pattern: str, alias: str, name: str) -> bool:
    """Does the first level of `pattern` match message `name`?"""
    head = pattern.split("/")[0]
    return matches(head, alias, (name,))


def is_listed(union: Union, stack: Stack, patterns, alias: str) -> bool:
    if not union.discrim:
        return False
    target = resolve(union.discrim, stack)
    return target is not None and any(matches(p, alias, target.names) for p in patterns)


# --- building variants --------------------------------------------------------


class _Expander:
    def __init__(self, message: Message, patterns: tuple[str, ...], alias: str):
        self.message = message
        self.patterns = patterns
        self.alias = alias

    def variant(self, choices: dict[int, int], names: tuple, values: tuple) -> Variant:
        build = _Build(self, choices)
        body = build.struct(root_stack(self.message.body, self.message.name), ())

        message = self.message
        if choices:
            message = Message(
                (message.name,) + names, body, tuple(build.consts), message.namespace
            )

        kids = ()
        if build.next is not None:
            union = build.next
            kids = tuple(
                self.variant({**choices, id(union): i}, names + (case.name,), values + (case.value,))
                for i, case in enumerate(union.cases)
            )
        return Variant((self.message.name,) + values, message, kids)


class _Build:
    """One pass over the original message: the variant body for `choices`."""

    def __init__(self, expander: _Expander, choices: dict[int, int]):
        self.expander = expander
        self.choices = choices  # id(union) -> chosen case index
        self.consts: list[tuple[str, object]] = []
        self.next: Union | None = None  # first listed union not chosen yet
        self.paths: dict[tuple, tuple[str, ...]] = {}  # original index -> variant path

    def struct(self, stack: Stack, path: tuple[str, ...]) -> Struct:
        node: Struct = stack[-1].node
        fields = []
        for child in children(stack[-1]):
            field = child.node
            if isinstance(field, Union):
                fields.extend(self.union(push(stack, child), path))
                continue

            field_path = path + (field.name,) if getattr(field, "name", "") else path
            self.paths[child.index] = field_path
            if isinstance(field, Struct):
                field = self.struct(push(stack, child), field_path)
            fields.append(field)  # leaves, arrays and carried messages as they are

        if all(a is b for a, b in zip(fields, node.fields)) and len(fields) == len(node.fields):
            return node  # unchanged: share it
        return replace(node, fields=tuple(fields))

    def union(self, stack: Stack, path: tuple[str, ...]) -> list:
        frame = stack[-1]
        union: Union = frame.node
        chosen = self.choices.get(id(union))

        if chosen is None:
            if self.next is None and is_listed(
                union, stack, self.expander.patterns, self.expander.alias
            ):
                self.next = union
            return [union]

        case = union.cases[chosen]
        target = resolve(union.discrim, stack)
        if case.value is not DEFAULT:  # a default case leaves the discriminator open
            self.consts.append(("/".join(self.paths[target.index]), case.value))

        inner = path + (union.name,) if union.name else path
        case_frame = children(frame)[chosen]
        if isinstance(case.body, MessageRef):
            fields = (case.body,)
        else:
            fields = self.struct(push(stack, case_frame), inner).fields

        if not union.name:
            return list(fields)
        return [Struct(union.name, tuple(fields), meta=union.meta, nest=union.nest)]


# --- validation ---------------------------------------------------------------


def expansion_rules(patterns: tuple[str, ...]) -> tuple:
    """Rules that check a dialect's expand patterns against each union."""

    @rule(Union)
    def expand_in_array(union, ctx):
        if any(isinstance(f.node, Array) for f in ctx.stack[:-1]) and is_listed(
            union, ctx.stack, patterns, ctx.icd.alias
        ):
            yield ctx.error(
                "expand-in-array",
                "listed for expansion, but inside an array each element may choose a "
                "different case",
            )

    return (expand_in_array,)


def unused_patterns(icds: Mapping[str, Icd], patterns: tuple[str, ...]) -> list[Issue]:
    """A warning for each pattern whose message is loaded but has no matching union.

    That's likely a typo. A pattern for a message that isn't loaded is fine: a
    dialect's patterns cover all of its ICDs, not every one of them.
    """
    used, present = set(), set()
    for icd in icds.values():
        for message in icd.messages:
            present.update(p for p in patterns if matches_message(p, icd.alias, message.name))
            for union, stack in _all_unions(root_stack(message.body, message.name)):
                target = resolve(union.discrim, stack) if union.discrim else None
                if target is not None:
                    used.update(p for p in patterns if matches(p, icd.alias, target.names))

    return [
        Issue("warning", "expand-unused", "expand", f"expand pattern {p!r} matches no union")
        for p in patterns
        if p in present and p not in used
    ]


def _all_unions(stack: Stack):
    frame: Frame = stack[-1]
    if isinstance(frame.node, Union):
        yield frame.node, stack
    for child in children(frame):
        yield from _all_unions(push(stack, child))
