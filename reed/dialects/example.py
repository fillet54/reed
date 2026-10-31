"""The example dialect (tests/data/example-icd.xml).

Adds system IDs: a 21-bit `systemIdRef` group (7 bits each for system,
subsystem and unit) whose system/subsystem/unit children list the legal values,
and unions keyed by a system ID whose cases follow that same hierarchy.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ..ir import Case, Domain, Int, Struct
from ..lower import META_TAGS, Lowering, _int, _tag

LEVELS = ("system", "subsystem", "unit")
LEVEL_BITS = 7


class ExampleLowering(Lowering):
    # <group2> means exactly the same as <group> in this dialect.
    lower_group2 = Lowering.lower_group

    def lower_type_systemIdRef(self, elem: ET.Element) -> Struct:
        return Struct(
            elem.get("name") or elem.get("id") or "",
            tuple(Int(level, LEVEL_BITS) for level in LEVELS),
            declared_bits=self.bits(elem),
            domain=tuple(self.domain(c) for c in elem if _tag(c) == "system"),
            meta=self.meta(elem),
        )

    def domain(self, elem: ET.Element) -> Domain:
        return Domain(
            self.required(elem, "name"),
            _int(self.required(elem, "value")),
            tuple(self.domain(c) for c in elem if _tag(c) in LEVELS),
        )

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
