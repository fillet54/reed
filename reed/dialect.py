"""A dialect bundles how one ICD flavour is lowered and validated.

    EXAMPLE = Dialect("example", ExampleLowering, BASE_RULES + (my_rule,),
                      layout=LayoutPolicy(word_bits=16))
    icds = EXAMPLE.lower([xml_text, ...])
    issues = EXAMPLE.validate(icds)

Split rules for expansion will join it later.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .ir import Icd, LayoutPolicy
from .lower import Lowering, lower_many
from .validate import BASE_RULES, Issue, Rule, validate


@dataclass(frozen=True)
class Dialect:
    name: str
    lowering: type[Lowering] = Lowering
    rules: tuple[Rule, ...] = BASE_RULES
    layout: LayoutPolicy = LayoutPolicy()  # word size, byte order, padding

    def lower(
        self, sources: Iterable[str | ET.Element], names: Iterable[str] | None = None
    ) -> dict[str, Icd]:
        return lower_many(sources, self.lowering, names, self.layout)

    def validate(self, icds: Icd | Mapping[str, Icd]) -> list[Issue]:
        return validate(icds, self.rules)


BASE = Dialect("base")
