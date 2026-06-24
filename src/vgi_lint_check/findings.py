"""Findings, severities, and rule categories.

The shared vocabulary of the rule engine and the reporters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from .model import ObjectId


class Severity(IntEnum):
    """Finding severity (also the gating threshold for ``--fail-on``)."""

    OFF = 0
    INFO = 1
    WARNING = 2
    ERROR = 3

    @classmethod
    def parse(cls, value: str | Severity) -> Severity:
        """Parse a severity from a name (case-insensitive) or pass one through."""
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
        """Lower-case name (``error``/``warning``/``info``/``off``)."""
        return self.name.lower()


class Category(StrEnum):
    """The rule family a finding belongs to."""

    CATALOG = "catalog"
    DISCOVERABILITY = "discoverability"
    CONTENT = "content"
    DESCRIPTION = "description"
    COLUMNS = "columns"
    FUNCTIONS = "functions"
    TAGS = "tags"
    EXAMPLES = "examples"
    SETTINGS = "settings"
    PRAGMAS = "pragmas"
    ATTACH_OPTIONS = "attach_options"
    CONSTRAINTS = "constraints"
    STRUCTURE = "structure"
    EXECUTION = "execution"


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

    def sort_key(self) -> tuple[str, int, str, str]:
        """Deterministic ordering key: (object, -severity, code, column)."""
        return (
            self.object_id.qualified(),
            -int(self.severity),
            self.code,
            self.object_id.column or "",
        )
