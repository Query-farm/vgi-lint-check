"""Collapse findings by rule, so a rule firing across many objects reads once.

Per-object grouping repeats the same code + message + fix N times when a rule
fires on N objects (e.g. VGI312 across every function). Grouping by rule states
the rule and its fix once, then lists the affected objects compactly — far less
noise for humans, and far fewer tokens for an LLM acting on the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_SEV_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class RuleGroup:
    """All findings for one rule code, with shared fields hoisted out."""

    code: str
    severity: str
    summary: str  # rule one-liner (from rule.summary)
    items: list[dict[str, Any]] = field(default_factory=list)  # the raw findings

    @property
    def count(self) -> int:
        """Number of findings (objects) under this rule."""
        return len(self.items)

    @property
    def shared_fix(self) -> str | None:
        """The fix hint if every finding shares it, else None (fixes vary)."""
        fixes = {f["fix"] for f in self.items}
        return fixes.pop() if len(fixes) == 1 else None

    @property
    def shared_message(self) -> str | None:
        """The message if every finding shares it, else None (per-object detail)."""
        msgs = {f["message"] for f in self.items}
        return msgs.pop() if len(msgs) == 1 else None


def group_by_rule(
    findings: list[dict[str, Any]], rule_summaries: dict[str, str]
) -> list[RuleGroup]:
    """Group findings by rule code, ordered by severity then code.

    ``rule_summaries`` maps code -> rule.summary (for the group header).
    """
    groups: dict[str, RuleGroup] = {}
    for f in findings:
        g = groups.get(f["code"])
        if g is None:
            g = groups[f["code"]] = RuleGroup(
                code=f["code"],
                severity=f["severity"],
                summary=rule_summaries.get(f["code"], ""),
            )
        g.items.append(f)
    return sorted(
        groups.values(),
        key=lambda g: (_SEV_ORDER.get(g.severity, 9), g.code),
    )
