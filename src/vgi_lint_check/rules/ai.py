"""LLM-backed rules: doc review (VGI180), agent suitability (VGI920), Jev (VGI190/191).

VGI190 checks an example's description against its SQL and VGI191 an object's
category, both with TypeSafe's Jev model.

These never run in a plain lint. They are gated like the execution/network rules:
``requires_review`` fires only under ``--doc-review``, ``requires_agent`` only
under ``--agent-check`` and ``requires_jev`` only under ``--jev``. The pipeline runs
the passes and hands the results to the rule via ``ctx.review_report`` /
``ctx.sim_report`` / ``ctx.jev_report`` — so the verdicts flow through the normal
findings → scoring → gating path.
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


_EXAMPLE_KINDS = (
    ObjectKind.TABLE,
    ObjectKind.VIEW,
    ObjectKind.SCALAR_FUNCTION,
    ObjectKind.AGGREGATE,
    ObjectKind.MACRO,
    ObjectKind.TABLE_FUNCTION,
)


@register
class ExampleDescriptionMatchesSql(Rule):
    code = "VGI190"
    name = "example-description-matches-sql"
    category = Category.CONTENT
    default_severity = Severity.INFO
    requires_jev = True
    targets = _EXAMPLE_KINDS
    summary = (
        "An example's description should say what its SQL does (checked by TypeSafe's "
        "Jev model under --jev; flagged only when Jev is confident they disagree)."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        report = ctx.jev_report
        if report is None:  # --jev not run
            return
        floor = ctx.config.options.jev_example_min_confidence
        for key, item in report.examples.items():
            ans = report.get(key)
            if ans is None or item.object_id is None:
                continue
            if ans.choice != "inaccurate" or (ans.confidence or 0) < floor:
                continue
            yield self.finding(
                ctx,
                item.object_id,
                f"{item.where}: the description {item.description.strip()[:120]!r} doesn't "
                f"match what the query does (Jev, confidence {ans.confidence:.2f})",
                "rewrite the description to say what the query actually returns, or change "
                "the query to do what the description promises",
            )


@register
class CategoryFitsObject(Rule):
    code = "VGI191"
    name = "category-fits-object"
    category = Category.CONTENT
    default_severity = Severity.INFO
    requires_jev = True
    targets = _EXAMPLE_KINDS
    summary = (
        "An object's vgi.category should be the best fit among its schema's categories "
        "(checked by TypeSafe's Jev model under --jev; flagged only when Jev confidently "
        "picks another)."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        report = ctx.jev_report
        if report is None:
            return
        floor = ctx.config.options.jev_category_min_confidence
        for key, item in report.categories.items():
            ans = report.get(key)
            if ans is None or item.object_id is None or not ans.choice:
                continue
            if ans.choice == item.declared or (ans.confidence or 0) < floor:
                continue
            yield self.finding(
                ctx,
                item.object_id,
                f"{item.where} is in category {item.declared!r}, but {ans.choice!r} fits it "
                f"better (Jev, confidence {ans.confidence:.2f})",
                f"move it to {ans.choice!r}, or reword its comment so its category is clear",
            )
