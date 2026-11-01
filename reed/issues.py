"""Issues found in an ICD, by lowering or validation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    path: str  # "command/commandBody/power", or "file.xml:12" from lowering
    message: str

    def __str__(self):
        return f"{self.severity}: {self.path}: {self.message} [{self.code}]"
