"""A dialect bundles how one ICD flavour is lowered and validated.

    EXAMPLE = Dialect("example", ExampleLowering, BASE_RULES + (my_rule,),
                      layout=LayoutPolicy(word_bits=16))
    icds = EXAMPLE.lower([xml_text, ...])
    issues = EXAMPLE.validate(icds)

`expand` lists the discriminator paths whose unions split into variants
(see reed.expand).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .ir import Icd, LayoutPolicy
from .lower import Lowering, load_files, lower_many
from .validate import BASE_RULES, Issue, Rule, validate


@dataclass(frozen=True)
class Dialect:
    name: str
    lowering: type[Lowering] = Lowering
    rules: tuple[Rule, ...] = BASE_RULES
    layout: LayoutPolicy = LayoutPolicy()  # word size, byte order, padding
    expand: tuple[str, ...] = ()  # discriminator paths to split on, e.g. "command/header/id"

    def lower(
        self, sources: Iterable[str | ET.Element], names: Iterable[str] | None = None
    ) -> dict[str, Icd]:
        return lower_many(sources, self.lowering, names, self.layout)

    def load(self, paths: Iterable[str]) -> dict[str, Icd]:
        """Lower ICD files from disk, following their refSchemaLocation imports."""
        return load_files(paths, self.lowering, self.layout)

    def validate(self, icds: Icd | Mapping[str, Icd]) -> list[Issue]:
        if not self.expand:
            return validate(icds, self.rules)

        from .expand import expansion_rules, unused_patterns

        if isinstance(icds, Icd):
            icds = {icds.namespace: icds}
        issues = validate(icds, self.rules + expansion_rules(self.expand))
        return issues + unused_patterns(icds, self.expand)


BASE = Dialect("base")
