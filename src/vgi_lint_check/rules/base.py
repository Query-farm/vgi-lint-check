"""Rule base class and the per-run context rules receive."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..findings import Category, Finding, Severity
from ..model import Catalog, ObjectId, ObjectKind

if TYPE_CHECKING:
    from ..config import Config


@dataclass
class RuleContext:
    """Per-run context handed to each rule's ``check`` method."""

    catalog: Catalog
    config: Config
    # The live haybarn connection (typed ``Any`` — a third-party DB cursor),
    # present only for rules with ``requires_connection``.
    connection: Any | None = None
    # Resolved severity for the rule currently executing (set by the engine).
    severity: Severity = Severity.WARNING


class Rule(ABC):
    code: str  # e.g. "VGI112" — unique, stable
    name: str  # short slug
    category: Category
    default_severity: Severity
    targets: tuple[ObjectKind, ...] = ()
    requires_connection: bool = False
    summary: str = ""  # one-liner shown in `rules`/`explain` and agent output

    @abstractmethod
    def check(self, ctx: RuleContext) -> Iterable[Finding]:  # pragma: no cover
        ...

    # Helper to build a finding at the engine-resolved severity for this rule.
    def finding(self, ctx: RuleContext, object_id: ObjectId, message: str, hint: str) -> Finding:
        return Finding(
            code=self.code,
            severity=ctx.severity,
            category=self.category,
            object_id=object_id,
            message=message,
            hint=hint,
        )
