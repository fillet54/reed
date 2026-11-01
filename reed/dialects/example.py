"""The example dialect (tests/data/example-icd.xml).

Adds system IDs: a fieldDef with xsi:type="subsystemDef" is a 21-bit group
(7 bits each for system, subsystem and unit) whose system/subsystem/unit
children list the legal values. Fields use it with
`<group name="systemId" ref="SubsystemType" xsi:type="subsystemIDRef"/>`.
Unions keyed by a system ID have cases that follow that same hierarchy.

Also: <group2> is another spelling of <group>, <commandkey> (the unique key
field) and <mpmCheck> (a checksum field) are <integer>s, <apCommand> is a
union with the cases of //Command/CommandBody keyed by ../CommandNumber,
and a command with protocolled="false" limits its whole message to 50 bytes.

Words are 16 bits: every message is a whole number of words, the rest of the
last one an implicit spare.
"""

from __future__ import annotations

from dataclasses import replace
from xml.etree import ElementTree as ET

from ..dialect import Dialect
from ..ir import Case, Domain, Int, LayoutPolicy, Struct
from ..lower import META_TAGS, XSI_TYPE, Lowering, LoweringError, _int, _tag
from ..validate import BASE_RULES, rule

LEVELS = ("system", "subsystem", "unit")
LEVEL_BITS = 7


class ExampleLowering(Lowering):
    # <group2> means exactly the same as <group> in this dialect.
    lower_group2 = Lowering.lower_group
    # <commandkey> (used for the unique key field) is an <integer>.
    lower_commandkey = Lowering.lower_integer
    # <mpmCheck> (a checksum field) is an <integer>.
    lower_mpmCheck = Lowering.lower_integer

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

    def cases(self, elem: ET.Element):
        if _tag(elem) == "system":
            yield from self.system_cases(elem, (), ())
        else:
            yield from super().cases(elem)

    def system_cases(self, elem: ET.Element, names: tuple, values: tuple):
        """One case per level. Lower levels' value is 0 ("the whole system")."""
        name = self.required(elem, "name")
        names = names + (name,)
        values = values + (_int(self.required(elem, "value")),)

        fields = tuple(
            self.field(c) for c in elem if _tag(c) not in META_TAGS and _tag(c) not in LEVELS
        )
        padded = values + (0,) * (len(LEVELS) - len(values))
        yield Case(".".join(names), _tag(elem), padded, Struct(name, fields, meta=self.meta(elem)))

        for child in elem:
            if _tag(child) in LEVELS:
                yield from self.system_cases(child, names, values)


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
    BASE_RULES + (unprotocolled_command_size,),
    layout=LayoutPolicy(word_bits=16, byteorder="big", pad=True),
    expand=(
        "command/header/id",  # command bodies
        "return/header/id",  # return bodies
        "return/returnBody/*/systemId",  # system IDs
    ),
)
