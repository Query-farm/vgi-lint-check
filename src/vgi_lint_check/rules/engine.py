"""Run selected rules over a catalog and collect findings."""

from __future__ import annotations

from collections.abc import Iterable

from ..findings import Finding, Severity
from .base import Rule, RuleContext


def run(rules: Iterable[Rule], ctx: RuleContext) -> list[Finding]:
    findings: list[Finding] = []
    config = ctx.config
    for rule in rules:
        sev = config.effective_severity(rule)
        if sev is Severity.OFF:
            continue
        ctx.severity = sev
        for f in rule.check(ctx):
            if config.is_object_ignored(f.object_id, f.code):
                continue
            findings.append(f)
    findings.sort(key=lambda f: f.sort_key())
    return findings
