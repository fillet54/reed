"""A dialect bundles how one ICD flavour is lowered and validated.

    EXAMPLE = Dialect("example", ExampleLowering, BASE_RULES + (my_rule,))
    icds = EXAMPLE.lower([xml_text, ...])
    issues = EXAMPLE.validate(icds)

Split rules for expansion will join it later.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .ir import Icd
from .lower import Lowering, lower_many
from .validate import BASE_RULES, Issue, Rule, validate


@dataclass(frozen=True)
class Dialect:
    name: str
    lowering: type[Lowering] = Lowering
    rules: tuple[Rule, ...] = BASE_RULES

    def lower(self, sources: Iterable[str | ET.Element]) -> dict[str, Icd]:
        return lower_many(sources, self.lowering)

    def validate(self, icds: Icd | Mapping[str, Icd]) -> list[Issue]:
        return validate(icds, self.rules)


BASE = Dialect("base")
