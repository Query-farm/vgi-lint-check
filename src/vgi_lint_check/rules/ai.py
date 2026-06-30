"""LLM-backed rules: doc-quality review (VGI180) and agent-suitability (VGI920).

These never run in a plain lint. They are gated like the execution/network rules:
``requires_review`` fires only under ``--doc-review`` and ``requires_agent`` only
under ``--agent-check``. The pipeline runs the LLM passes and hands the results
to the rule via ``ctx.review_report`` / ``ctx.sim_report`` — so the verdicts flow
through the normal findings → scoring → gating path.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import Catalog, ObjectId, ObjectKind
from .base import Rule, RuleContext
from .registry import register

_DOC_KINDS = (
    ObjectKind.CATALOG,
    ObjectKind.SCHEMA,
    ObjectKind.TABLE,
    ObjectKind.VIEW,
    ObjectKind.SCALAR_FUNCTION,
    ObjectKind.AGGREGATE,
    ObjectKind.MACRO,
    ObjectKind.TABLE_FUNCTION,
)


def _id_index(catalog: Catalog) -> dict[str, ObjectId]:
    """Map each object's qualified id back to its ObjectId (review keys by qualified)."""
    idx: dict[str, ObjectId] = {catalog.id.qualified(): catalog.id}
    for s in catalog.iter_schemas():
        idx[s.id.qualified()] = s.id
    for t in catalog.iter_table_like():
        idx[t.id.qualified()] = t.id
    for f in catalog.iter_all_functions():
        idx[f.id.qualified()] = f.id
    return idx


@register
class DocQualityReview(Rule):
    code = "VGI180"
    name = "doc-quality-review"
    category = Category.CONTENT
    default_severity = Severity.WARNING
    requires_review = True
    targets = _DOC_KINDS
    summary = "An object's docs should pass an LLM quality review (accuracy/clarity/completeness)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        report = ctx.review_report
        if report is None:  # --doc-review not run
            return
        minimum = ctx.config.options.doc_quality_min
        idx = _id_index(ctx.catalog)
        for r in report.reviews:
            if r.overall == 0 or r.overall >= minimum:
                continue
            oid = idx.get(r.object)
            if oid is None:
                continue
            tip = (
                r.suggestions[0]
                if r.suggestions
                else "tighten the prose so it earns a higher score"
            )
            yield self.finding(
                ctx,
                oid,
                f"doc quality {r.overall}/5 is below the bar ({minimum})"
                + (f" — {r.summary}" if r.summary else ""),
                tip,
            )


@register
class AgentSuitabilityGate(Rule):
    code = "VGI920"
    name = "agent-suitability"
    category = Category.EXECUTION
    default_severity = Severity.ERROR
    requires_agent = True
    targets = (ObjectKind.CATALOG,)
    summary = "An agent must clear the worker's vgi.agent_test_tasks suite (simulate pass-rate)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        report = ctx.sim_report
        if report is None or not report.verdicts:  # --agent-check not run / no tasks
            return
        threshold = ctx.config.options.agent_pass_threshold
        rate = report.pass_rate
        if rate >= threshold:
            return
        failed = [v.name for v in report.verdicts if not v.passed]
        shown = ", ".join(failed[:5]) + ("…" if len(failed) > 5 else "")
        tip = (
            report.suggestions[0]
            if report.suggestions
            else (
                "improve the metadata an agent needs (clearer docs, worked examples) so "
                "the analyst can complete these tasks"
            )
        )
        yield self.finding(
            ctx,
            ctx.catalog.id,
            f"agent pass-rate {int(rate * 100)}% is below the {int(threshold * 100)}% bar; "
            f"{len(failed)} task(s) failed ({shown})",
            tip,
        )
