"""ICD dialects: Lowering subclasses and rules for each XML ICD flavour."""

from __future__ import annotations

NAMES = ("base", "example")


def get(name: str):
    """The Dialect registered under `name`."""
    if name == "base":
        from ..dialect import BASE

        return BASE
    if name == "example":
        from .example import EXAMPLE

        return EXAMPLE
    raise KeyError(name)
