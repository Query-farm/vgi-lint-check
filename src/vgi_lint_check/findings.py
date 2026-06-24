"""Findings, severities, and rule categories — the shared vocabulary of the
rule engine and the reporters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum

from .model import ObjectId


class Severity(IntEnum):
    OFF = 0
    INFO = 1
    WARNING = 2
    ERROR = 3

    @classmethod
    def parse(cls, value: str | "Severity") -> "Severity":
        if isinstance(value, Severity):
            return value
        try:
            return cls[str(value).strip().upper()]
        except KeyError as e:  # pragma: no cover - guarded by config validation
            raise ValueError(
                f"invalid severity {value!r}; expected one of "
                f"{', '.join(s.name.lower() for s in cls)}"
            ) from e

    @property
    def label(self) -> str:
        return self.name.lower()


class Category(str, Enum):
    DESCRIPTION = "description"
    COLUMNS = "columns"
    FUNCTIONS = "functions"
    TAGS = "tags"
    EXAMPLES = "examples"
    SETTINGS = "settings"
    PRAGMAS = "pragmas"
    EXECUTION = "execution"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Finding:
    """One quality issue against one catalog object.

    ``hint`` is mandatory free-form, language-agnostic prose telling the author
    what to add — never generated code (VGI workers span many languages).
    """

    code: str
    severity: Severity
    category: Category
    object_id: ObjectId
    message: str
    hint: str
    # Set by baseline.classify(): True when not present in the version baseline.
    is_new: bool = True

    def sort_key(self) -> tuple:
        return (
            self.object_id.qualified(),
            -int(self.severity),
            self.code,
            self.object_id.column or "",
        )
