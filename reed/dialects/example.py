"""The example dialect (tests/data/example-icd.xml).

Adds system IDs: a fieldDef with xsi:type="subsystemDef" is a 21-bit group
(7 bits each for system, subsystem and unit) whose system/subsystem/unit
children list the legal values. Fields use it with
`<group name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/>`.
Unions keyed by a system ID have cases that follow that same hierarchy.

Also: <group2> is another spelling of <group>, <commandkey> (the unique key
field) and <timecsecs> (centiseconds since midnight UTC) are <integer>s,
<faultCode> is an unsigned <integer>,
<apCommand> is a union with the cases of //Command/CommandBody keyed by
../CommandNumber, and a command with protocolled="false" limits its whole
message to 50 bytes.

Words are 16 bits: every message is a whole number of words, the rest of the
last one an implicit spare.
"""

from __future__ import annotations

from dataclasses import replace
from xml.etree import ElementTree as ET

from ..dialect import Dialect
from ..ir import Case, Domain, Int, LayoutPolicy, Struct, Union
from ..lower import META_TAGS, XSI_TYPE, Lowering, LoweringError, _describe, _Element, _int, _tag
from ..validate import BASE_RULES, rule

LEVELS = ("system", "subsystem", "unit")
LEVEL_BITS = 7


class ExampleLowering(Lowering):
    # <group2> means exactly the same as <group> in this dialect.
    lower_group2 = Lowering.lower_group
    # <commandkey> (used for the unique key field) is an <integer>.
    lower_commandkey = Lowering.lower_integer
    # <timecsecs> (centiseconds since midnight UTC) is an <integer>.
    lower_timecsecs = Lowering.lower_integer

    def lower_faultCode(self, elem: ET.Element) -> Int:
        """<faultCode>: an <integer> that is always unsigned."""
        if self.flag(elem, "signed"):
            raise LoweringError(f"{_describe(elem)} is always unsigned")
        return self.lower_integer(elem)

    # <apCommand> stands for a union of the top-level commands: the cases of
    # this union, picked by the CommandNumber beside it unless it gives a
    # discrimField of its own.
    AP_COMMAND_UNION = "//Command/CommandBody"
    AP_COMMAND_DISCRIM = "../CommandNumber"

    def lower_apCommand(self, elem: ET.Element):
        discrim = elem.get("discrimField", self.AP_COMMAND_DISCRIM)
        return self.cases_from(elem, self.AP_COMMAND_UNION, discrim, what="apCommand")

    def lower_type_subsystemDef(self, elem: ET.Element) -> Struct:
        """The definition: the ID's layout and its legal values."""
        return Struct(
            elem.get("name") or elem.get("id") or "",
            tuple(Int(level, LEVEL_BITS) for level in LEVELS),
            declared_bits=self.bits(elem),
            domain=tuple(self.domain(c) for c in elem if _tag(c) == "system"),
            meta=self.meta(elem),
        )

    def lower_type_subsystemIDRef(self, elem: ET.Element) -> Struct:
        """A use of a subsystemDef: an ordinary ref, checked to point at one."""
        ref = elem.get("ref")
        if not ref:
            raise LoweringError(
                f"<{_tag(elem)} name={elem.get('name')!r}>: xsi:type subsystemIDRef needs a ref"
            )
        module, key = self.module_for(ref)
        target = module.def_elems.get(key)
        if target is not None and target.get(XSI_TYPE) != "subsystemDef":
            raise LoweringError(f"subsystemIDRef {ref!r} is not a subsystemDef")
        return self.ref(elem)

    def domain(self, elem: ET.Element) -> Domain:
        return Domain(
            self.required(elem, "name"),
            _int(self.required(elem, "value")),
            tuple(self.domain(c) for c in elem if _tag(c) in LEVELS),
        )

    def case(self, elem: ET.Element) -> Case:
        case = super().case(elem)
        if elem.get("protocolled") is None:
            return case
        props = case.meta.props + (("protocolled", self.flag(elem, "protocolled")),)
        return replace(case, meta=replace(case.meta, props=props))

    def lower_type_subsystemRef(self, elem: ET.Element) -> Union:
        """A union keyed by a system ID, its cases a system/subsystem/unit tree.

        It stands for three nested unions (on system, subsystem, unit), kept
        flat: one case per level that has fields of its own, valued by the
        whole ID. A level's fields are the 0 ("any/all") value of the levels
        below it: <system name="vehicle"><group .../></system> is (1, 0, 0). A
        unit is always a case.

        Levels are named; their values come from the subsystemDef (a dialect
        assumption: there is one, see subsystem_def). A value given here must
        agree with it. The subsystem_ref_discrim rule checks that the
        discrimField is a subsystemIDRef of that same def.
        """
        if elem.get("ref"):
            return self.ref(elem)  # the union fieldDef carries the tree
        if _tag(elem) != "union":
            raise LoweringError(
                f'xsi:type="subsystemRef" only applies to <union>, not {_describe(elem)}'
            )
        key, label, definition = self.subsystem_def()

        shell = _Element(elem.tag, elem.attrib)  # the union without its cases
        shell.extend(c for c in elem if _tag(c) in META_TAGS)
        shell.line = getattr(elem, "line", None)
        union = self.lower_union(shell)

        cases = []
        for child in elem:
            if _tag(child) in META_TAGS:
                continue
            if _tag(child) != "system":
                raise LoweringError(
                    f"a subsystemRef union holds <system>s, not <{_tag(child)}>",
                    line=getattr(child, "line", None),
                )
            cases.extend(self.system_cases(child, (), (), definition.domain, label))
        meta = replace(union.meta, props=union.meta.props + ((SUBSYSTEM_DEF, (key, label)),))
        return replace(union, cases=tuple(cases), meta=meta)

    def subsystem_def(self) -> tuple[tuple[str, str], str, Struct]:
        """The subsystemDef: (namespace, id), its name for messages, and the
        lowered def. Every loaded ICD's subsystemDefs are gathered once, by
        alias:id, into the catalog's context; there must be exactly one."""
        defs = self.catalog.context.get("subsystemDefs")
        if defs is None:
            defs = {}
            for module in self.catalog.modules.values():
                for key, d in module.def_elems.items():
                    if d.get(XSI_TYPE) == "subsystemDef":
                        label = f"{module.alias}:{key}" if module.alias else key
                        defs[(module.namespace, key)] = (label, module)
            self.catalog.context["subsystemDefs"] = defs

        if len(defs) != 1:
            labels = ", ".join(label for label, _ in defs.values()) or "none"
            raise LoweringError(
                "a subsystemRef union takes its values from the subsystemDef, "
                f"but {len(defs)} are loaded: {labels}"
            )
        ((key, (label, module)),) = defs.items()
        return key, label, module.field_def(key[1])

    def cases(self, elem: ET.Element):
        if _tag(elem) == "system":
            raise LoweringError(
                '<system> in a union needs xsi:type="subsystemRef" on the union',
                line=getattr(elem, "line", None),
            )
        yield from super().cases(elem)

    def system_cases(self, elem: ET.Element, names: tuple, values: tuple, domain, label: str):
        """A case for this level if it has fields (always for a unit), then the
        levels below. Values come from `domain`, this level's choices in the
        subsystemDef; lower levels' value is 0 ("any/all")."""
        level, name = _tag(elem), self.required(elem, "name")
        found = next((d for d in domain if d.name == name), None)
        under = f" under {'.'.join(names)}" if names else ""
        line = getattr(elem, "line", None)
        if found is None:
            raise LoweringError(f"no {level} {name!r}{under} in subsystemDef {label!r}", line=line)
        given = elem.get("value")
        if given is not None and _int(given) != found.value:
            raise LoweringError(
                f"{level} {name!r} has value {_int(given)}, but {found.value} in "
                f"subsystemDef {label!r}",
                line=line,
            )
        names, values = names + (name,), values + (found.value,)

        fields = tuple(
            self.field(c) for c in elem if _tag(c) not in META_TAGS and _tag(c) not in LEVELS
        )
        if fields or level == LEVELS[-1]:
            padded = values + (0,) * (len(LEVELS) - len(values))
            body = Struct(name, fields, meta=self.meta(elem))
            yield Case(".".join(names), level, padded, body)

        for child in elem:
            if _tag(child) in LEVELS:
                yield from self.system_cases(child, names, values, found.children, label)


# Union meta prop of a subsystemRef union: ((namespace, id), label) of its subsystemDef.
SUBSYSTEM_DEF = "subsystem-def"


@rule(Union)
def subsystem_ref_discrim(union, ctx):
    """A subsystemRef union's discrimField must be a subsystemIDRef of the
    subsystemDef its values came from."""
    source = union.meta.prop(SUBSYSTEM_DEF)
    if source is None or not union.discrim:
        return
    key, label = source
    target = ctx.resolve(union.discrim)
    if target is None:
        return  # the discriminator rule reports it
    if not isinstance(target.node, Struct) or target.node.meta.origin != key:
        yield ctx.error(
            "subsystem-ref-discrim",
            f"discrimField {union.discrim!r} is not a subsystemIDRef of {label!r}, "
            "where this union's values come from",
        )


UNPROTOCOLLED_MAX_BITS = 50 * 8


@rule(Case)
def unprotocolled_command_size(case, ctx):
    """A command with protocolled="false" limits its whole message to 50 bytes."""
    if case.kind != "command" or case.meta.prop("protocolled", True):
        return

    size = ctx.message_size(choose={id(ctx.node): case})
    if size.hi > UNPROTOCOLLED_MAX_BITS:
        yield ctx.error(
            "unprotocolled-too-large",
            f"command {case.name!r} is not protocolled, so its message is limited to "
            f"50 bytes ({UNPROTOCOLLED_MAX_BITS} bits); it is up to {size}",
            path=f"{ctx.path}/{case.name}",
        )


EXAMPLE = Dialect(
    "example",
    ExampleLowering,
    BASE_RULES + (unprotocolled_command_size, subsystem_ref_discrim),
    layout=LayoutPolicy(word_bits=16, byteorder="big", pad=True),
    expand=(
        "Command/header/CommandNumber",  # command bodies
        "Return/header/ReturnNumber",  # return bodies
        "Return/ReturnBody/*/systemId",  # system IDs
        "TestCommand/header/TestNumber",  # test commands (test-icd.xml)
    ),
)
