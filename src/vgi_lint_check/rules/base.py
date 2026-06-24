"""Rule base class and the per-run context rules receive."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from ..findings import Category, Finding, Severity
from ..model import Catalog, ObjectId, ObjectKind


@dataclass
class RuleContext:
    catalog: Catalog
    config: object  # vgi_lint_check.config.Config
    connection: object | None = None
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
    def finding(
        self, ctx: RuleContext, object_id: ObjectId, message: str, hint: str
    ) -> Finding:
        return Finding(
            code=self.code,
            severity=ctx.severity,
            category=self.category,
            object_id=object_id,
            message=message,
            hint=hint,
        )
