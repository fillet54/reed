"""Validation: rules run over the IR and report every issue with its path.

A rule is a function decorated with `@rule(NodeType)`. It is called with each
node of that type and a Context, and yields Issues:

    @rule(Struct)
    def no_empty_groups(node, ctx):
        if not node.fields:
            yield ctx.error("empty-group", f"group {node.name!r} has no fields")

Dialects extend validation by adding rules to BASE_RULES (see reed.dialect).
Node types a rule can target: Message, Struct, Union, Case, Array, Int,
Scaled, Float, String, Bytes, Spare, MessageRef. A Case rule sees the union as `ctx.node`.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from .ir import (
    VARIABLE,
    Array,
    Bytes,
    Case,
    Domain,
    Float,
    Icd,
    Int,
    Message,
    Scaled,
    String,
    Struct,
    Union,
    flattened,
    value_keys,
)
from .issues import Issue
from .paths import Frame, Stack, children, is_before, push, resolve, root_stack
from .size import Size, Sizer


@dataclass(frozen=True, slots=True)
class Rule:
    node_type: type
    check: Callable[..., Iterable[Issue]]
    name: str


def rule(node_type: type):
    def decorator(fn) -> Rule:
        return Rule(node_type, fn, fn.__name__)

    return decorator


@dataclass
class Context:
    icds: Mapping[str, Icd]
    icd: Icd
    message: Message
    stack: Stack
    seen: set = field(default_factory=set)  # keys passed to once()

    @property
    def node(self):
        return self.stack[-1].node

    @property
    def frame(self) -> Frame:
        return self.stack[-1]

    @property
    def path(self) -> str:
        return "/".join(self.frame.names)

    def size(self, frame_stack: Stack | None = None) -> Size:
        return Sizer(self.icds).size(frame_stack or self.stack)

    def message_size(self, choose: Mapping[int, Case] | None = None) -> Size:
        """The whole message, padded to whole words."""
        return Sizer(self.icds, choose).message(self.message)

    def resolve(self, path: str) -> Frame | None:
        """Resolve a path relative to this node: ".." is its parent."""
        return resolve(path, self.stack)

    def once(self, *key) -> bool:
        """True the first time `key` is seen in this validation run."""
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    def error(self, code: str, message: str, path: str | None = None) -> Issue:
        return Issue("error", code, path or self.path, message)

    def warning(self, code: str, message: str, path: str | None = None) -> Issue:
        return Issue("warning", code, path or self.path, message)


def validate(
    icds: Icd | Mapping[str, Icd], rules: Iterable[Rule] | None = None
) -> list[Issue]:
    if isinstance(icds, Icd):
        icds = {icds.namespace: icds}
    rules = tuple(BASE_RULES if rules is None else rules)
    seen: set = set()
    issues: list[Issue] = []

    for icd in icds.values():
        issues += icd.issues  # found while lowering
        for message in icd.messages:
            ctx = Context(icds, icd, message, root_stack(message.body, message.name), seen)
            issues += run(rules, message, ctx)
            issues += walk(rules, ctx)

    return issues


def run(rules, node, ctx: Context) -> list[Issue]:
    found = []
    for r in rules:
        if isinstance(node, r.node_type):
            found += r.check(node, ctx)
    return found


def walk(rules, ctx: Context) -> list[Issue]:
    """Run rules on the node on top of ctx.stack, then on everything inside it."""
    issues = run(rules, ctx.node, ctx)

    if isinstance(ctx.node, Union):
        for case in ctx.node.cases:
            issues += run(rules, case, ctx)

    for child in children(ctx.frame):
        sub = Context(ctx.icds, ctx.icd, ctx.message, push(ctx.stack, child), ctx.seen)
        issues += walk(rules, sub)

    return issues


# --- base rules ---------------------------------------------------------------


@rule(Message)
def message_size(message, ctx):
    size = ctx.message_size()
    limit = ctx.icd.layout.max_bits

    if size.lo > limit:
        yield ctx.error("message-too-large", f"at least {size.lo} bits; the limit is {limit}")
    elif limit < size.hi < math.inf:  # inf: a count is unresolved, reported elsewhere
        yield ctx.warning("message-may-exceed", f"up to {size}; the limit is {limit} bits")


@rule(Message)
def whole_words(message, ctx):
    """Without an implicit spare (layout.pad off) a message must be whole words."""
    layout = ctx.icd.layout
    if layout.pad:
        return
    word = layout.word_bits
    size = ctx.message_size()

    if size.linear is not None:
        whole = size.linear.const % word == 0 and all(b % word == 0 for _, b in size.linear.terms)
    else:  # no formula (cases differ): check the bounds
        whole = size.lo % word == 0 and (size.hi == math.inf or size.hi % word == 0)
    if not whole:
        yield ctx.error(
            "not-whole-words", f"{size} is not always a whole number of {word}-bit words"
        )


@rule(Struct)
def declared_bit_count(node, ctx):
    if node.declared_bits is None:
        return
    size = ctx.size()

    if node.declared_bits is VARIABLE:
        if size.is_fixed:
            yield ctx.warning(
                "variable-is-fixed", f"bitCount is variable but the size is always {size}"
            )
    elif not (size.is_fixed and size.lo == node.declared_bits):
        yield ctx.error(
            "bit-count", f"bitCount is {node.declared_bits} but the fields add up to {size}"
        )


@rule(Union)
def discriminator(node, ctx):
    if not node.discrim:
        yield ctx.error(
            "missing-discrim",
            "union has no discrimField; give one where its fieldDef is used",
        )
        return
    yield from _check_path(ctx, node.discrim, "discrimField")


@rule(Union)
def duplicate_case_values(node, ctx):
    by_value: dict = {}
    for case in node.cases:
        by_value.setdefault(case.value, []).append(case.name)

    for value, names in by_value.items():
        if len(names) > 1:
            yield ctx.error(
                "duplicate-case-value", f"cases {', '.join(names)} all have value {value}"
            )


@rule(Struct)
def duplicate_value_key(node, ctx):
    """Two fields that would share a key in the group's value dict.

    Fields of a group or union with nestContents="false" (or an unnamed union's
    cases) go in the enclosing dict, so their names can clash with its fields.
    """
    if flattened(node):
        return  # its fields are checked with the group that holds them
    seen: set[str] = set()
    for key in (k for f in node.fields for k in value_keys(f)):
        if key in seen and ctx.once("duplicate-value-key", id(node), key):
            yield ctx.error(
                "duplicate-value-key",
                f"two fields would both be {key!r} in this group's value "
                '(fields of nestContents="false" groups and unions go in the enclosing group)',
            )
        seen.add(key)


@rule(Array)
def count_field(node, ctx):
    target = yield from _check_path(ctx, node.count, "countValue")
    if target is not None and not isinstance(target.node, Int):
        yield ctx.error("count-not-integer", f"countValue {node.count!r} is not an integer field")


@rule(Bytes)
def byte_count_field(node, ctx):
    if not node.count:
        return
    target = yield from _check_path(ctx, node.count, "countVariable")
    if target is not None and not isinstance(target.node, Int):
        yield ctx.error(
            "count-not-integer", f"countVariable {node.count!r} is not an integer field"
        )


@rule(Struct)
def duplicate_domain_values(node, ctx):
    if not node.domain:
        return
    # A shared fieldDef appears at every use; report its problems once.
    if node.meta.origin is not None and not ctx.once("domain", node.meta.origin):
        return

    def check(levels: tuple[Domain, ...], parents: tuple[str, ...]):
        by_value: dict = {}
        for d in levels:
            by_value.setdefault(d.value, []).append(d.name)
        where = f" under {'/'.join(parents)}" if parents else ""
        for value, names in by_value.items():
            if len(names) > 1:
                yield ctx.error(
                    "duplicate-domain-value", f"{', '.join(names)}{where} all have value {value}"
                )
        for d in levels:
            yield from check(d.children, parents + (d.name,))

    yield from check(node.domain, ())


def _check_path(ctx: Context, path: str, attr: str):
    """Yield issues for a path that doesn't resolve or comes too late; return its frame."""
    target = ctx.resolve(path)
    if target is None:
        yield ctx.error("unresolved-path", f"{attr} {path!r} does not name a field")
        return None
    if not is_before(target, ctx.frame):
        yield ctx.error(
            "used-before-defined", f"{attr} {path!r} must come before it in the message"
        )
    return target


@rule(String)
def string_bits(node, ctx):
    """A fixed string fills whole words; a variable one needs no alignment."""
    word = ctx.icd.layout.word_bits
    if node.bits is not VARIABLE and node.bits % word:
        yield ctx.error(
            "string-bits", f"bitCount {node.bits} is not a whole number of {word}-bit words"
        )


@rule(Int)
def integer_default(node, ctx):
    yield from _check_default(node, node.default, ctx)


@rule(Scaled)
def scalar_default(node, ctx):
    raw = None if node.default is None else round((node.default - node.offset) / node.scale)
    yield from _check_default(node, raw, ctx)


@rule(Float)
def float_default(node, ctx):
    if node.default is None or node.bits != 32:
        return
    try:
        struct.pack(">f", node.default)
    except OverflowError:
        yield ctx.error(
            "default-out-of-range", f"default {node.default} does not fit in a 32-bit float"
        )


def _check_default(node, raw, ctx):
    """A default must fit in the field's bits (it may be outside restricted values)."""
    if raw is None:
        return
    if node.signed:
        lo, hi = -(1 << (node.bits - 1)), (1 << (node.bits - 1)) - 1
    else:
        lo, hi = 0, (1 << node.bits) - 1
    if not lo <= raw <= hi:
        kind = "signed" if node.signed else "unsigned"
        yield ctx.error(
            "default-out-of-range",
            f"default {node.default} does not fit in {node.bits} {kind} bits",
        )


def _ref_tag_mismatch(node, ctx):
    """A ref whose tag differs from its fieldDef's: the use's tag wins; check it should."""
    note = node.meta.prop("ref-tag-mismatch")
    if note:
        yield ctx.warning("ref-tag-mismatch", note)


ref_tag_mismatch = tuple(
    Rule(t, _ref_tag_mismatch, "ref_tag_mismatch") for t in (Int, Scaled, Float, Struct, Array)
)


BASE_RULES: tuple[Rule, ...] = (
    message_size,
    whole_words,
    declared_bit_count,
    discriminator,
    duplicate_case_values,
    duplicate_value_key,
    count_field,
    byte_count_field,
    duplicate_domain_values,
    string_bits,
    integer_default,
    scalar_default,
    float_default,
    *ref_tag_mismatch,
)
