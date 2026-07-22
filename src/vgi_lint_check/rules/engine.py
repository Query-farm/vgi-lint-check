"""Run selected rules over a catalog and collect findings."""

from __future__ import annotations

from collections.abc import Iterable

from ..config import Waiver, WaiverUsage
from ..findings import Finding, Severity
from .base import Rule, RuleContext


def run(rules: Iterable[Rule], ctx: RuleContext) -> list[Finding]:
    findings: list[Finding] = []
    config = ctx.config
    tracer = ctx.tracer
    for rule in rules:
        sev = config.effective_severity(rule)
        if sev is Severity.OFF:
            continue
        ctx.severity = sev
        if tracer is not None:
            with tracer.span("rule", rule.code):
                produced = list(rule.check(ctx))
        else:
            produced = list(rule.check(ctx))
        for f in produced:
            if config.is_object_ignored(f.object_id, f.code):
                continue
            findings.append(f)
    findings.sort(key=lambda f: f.sort_key())
    return findings


def audit_waivers(rules: Iterable[Rule], ctx: RuleContext) -> list[WaiverUsage]:
    """Run the rules a waiver silenced, to find out which waivers are dead.

    A waiver that suppresses nothing is worse than no waiver: it is a standing
    claim that a rule does not apply, which nobody will revisit, and which hides
    the moment the rule *starts* applying. There is no way to know a waiver is
    dead without running the rule it hides, so this is a deliberate second pass —
    ``--audit-waivers``, not the default.

    Only rules disabled *purely* by an ignore/per-object waiver are re-run. A
    rule that is off because ``--no-execute`` skipped its tier stays off, so the
    audit never smuggles execution back into a static lint.
    """
    from .registry import all_rule_classes

    config = ctx.config
    by_code: dict[str, list[Waiver]] = {}
    for w in config.waivers:
        by_code.setdefault(w.code, []).append(w)

    usage = {id(w): WaiverUsage(waiver=w) for w in config.waivers}
    rule_list = list(rules)

    # Catalog-wide waivers: the rule never ran, so run it now.
    #
    # These are sourced from the full registry, NOT from ``rules``: the caller's
    # list comes from select_rules(), which has already dropped every rule whose
    # effective severity is OFF — including the waived ones. Auditing that list
    # would find no waived rule to run and report every catalog-wide waiver as
    # dead, which is precisely the advice that must never be wrong.
    for cls in all_rule_classes():
        rule = cls()
        if not config.is_waived(rule):
            continue
        ctx.severity = config.effective_severity(rule, lift_waivers=True)
        for f in rule.check(ctx):
            _credit(usage, by_code, f, scope_match=None)

    # Per-object waivers: the rule *did* run; replay it and keep what
    # is_object_ignored dropped.
    per_object_codes = {c for codes in config.per_object.values() for c in codes}
    for rule in rule_list:
        if config.effective_severity(rule) is Severity.OFF:
            continue
        if not any(_glob_may_match(rule.code, c) for c in per_object_codes):
            continue
        ctx.severity = config.effective_severity(rule)
        for f in rule.check(ctx):
            if config.is_object_ignored(f.object_id, f.code):
                _credit(usage, by_code, f, scope_match=f.object_id.qualified())

    return [usage[id(w)] for w in config.waivers]


def _glob_may_match(code: str, pattern: str) -> bool:
    import fnmatch

    return pattern.upper() == "ALL" or fnmatch.fnmatch(code, pattern)


def _credit(
    usage: dict[int, WaiverUsage],
    by_code: dict[str, list[Waiver]],
    finding: Finding,
    *,
    scope_match: str | None,
) -> None:
    """Attribute one suppressed finding to the waiver(s) that would hide it."""
    import fnmatch

    for code, waivers in by_code.items():
        if not _glob_may_match(finding.code, code):
            continue
        for w in waivers:
            if scope_match is None and w.scope is not None:
                continue
            if scope_match is not None and (
                w.scope is None or not fnmatch.fnmatch(scope_match, w.scope)
            ):
                continue
            u = usage[id(w)]
            u.suppressed += 1
            q = finding.object_id.qualified()
            if q not in u.objects:
                u.objects.append(q)
