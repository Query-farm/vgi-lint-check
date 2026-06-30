"""Tests for the LLM-backed rules (VGI180 doc-quality, VGI920 agent-suitability)
and their gating + score blending. All offline: reports are constructed by hand.
"""

from tests import fixtures as F
from vgi_lint_check import scoring
from vgi_lint_check.config import Config
from vgi_lint_check.findings import Severity
from vgi_lint_check.review import ObjectReview, ReviewReport
from vgi_lint_check.rules import select_rules
from vgi_lint_check.rules.ai import AgentSuitabilityGate, DocQualityReview
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.simulate import SimReport, TaskVerdict


def _scores(n):
    return {"accuracy": n, "clarity": n, "completeness": n, "audience_fit": n}


# --- VGI180 doc-quality ----------------------------------------------------
def test_doc_quality_fires_below_bar():
    fn = F.func("main", "easter", description="d")
    cat = F.catalog(F.schema("main", functions=[fn]))
    review = ReviewReport(
        "loc",
        "claude",
        [ObjectReview(fn.id.qualified(), "scalar_function", _scores(2), ["be specific"], "vague")],
        1,
        0,
    )
    ctx = RuleContext(cat, Config(), review_report=review)
    ctx.severity = Severity.WARNING
    out = list(DocQualityReview().check(ctx))
    assert out and "2.0/5" in out[0].message and out[0].object_id == fn.id


def test_doc_quality_passes_above_bar():
    fn = F.func("main", "easter", description="d")
    cat = F.catalog(F.schema("main", functions=[fn]))
    review = ReviewReport(
        "loc",
        "claude",
        [ObjectReview(fn.id.qualified(), "scalar_function", _scores(4), [], "")],
        1,
        0,
    )
    ctx = RuleContext(cat, Config(), review_report=review)
    ctx.severity = Severity.WARNING
    assert list(DocQualityReview().check(ctx)) == []


def test_doc_quality_noop_without_report():
    cat = F.catalog(F.schema("main"))
    ctx = RuleContext(cat, Config())  # review_report None
    ctx.severity = Severity.WARNING
    assert list(DocQualityReview().check(ctx)) == []


# --- VGI920 agent-suitability ----------------------------------------------
def _verdict(name, outcome):
    return TaskVerdict(name=name, outcome=outcome, reason="r")


def test_agent_gate_fires_below_threshold():
    cat = F.catalog(F.schema("main", functions=[F.func("main", "f", description="d")]))
    sim = SimReport("loc", "claude", [_verdict("t1", "fail"), _verdict("t2", "pass")], 2, 0)
    ctx = RuleContext(cat, Config(), sim_report=sim)
    ctx.severity = Severity.ERROR
    out = list(AgentSuitabilityGate().check(ctx))
    assert out and "50%" in out[0].message and "t1" in out[0].message


def test_agent_gate_passes_above_threshold():
    cat = F.catalog(F.schema("main"))
    sim = SimReport("loc", "claude", [_verdict("t1", "pass")], 1, 0)  # 100% pass
    ctx = RuleContext(cat, Config(), sim_report=sim)
    ctx.severity = Severity.ERROR
    assert list(AgentSuitabilityGate().check(ctx)) == []


# --- gating ----------------------------------------------------------------
def test_ai_rules_gated_off_by_default():
    codes = {c.code for c in select_rules(Config())}
    assert "VGI180" not in codes and "VGI920" not in codes


def test_ai_rules_enabled_by_flags():
    codes = {c.code for c in select_rules(Config(doc_review=True, agent_check=True))}
    assert "VGI180" in codes and "VGI920" in codes


# --- score blend (#7) ------------------------------------------------------
def test_score_blends_llm_dimensions():
    cat = F.catalog(F.schema("main", comment="c"))
    static = scoring.compute(cat, [])
    blended = scoring.compute(cat, [], agent_score=50, doc_quality=80)
    assert blended.static_score == static.score == 100
    assert blended.agent_score == 50 and blended.doc_quality == 80
    # 100*.55 + 50*.25 + 80*.20 = 83.5 -> 84
    assert blended.score == 84


def test_score_static_only_when_no_llm_passes():
    cat = F.catalog(F.schema("main", comment="c"))
    q = scoring.compute(cat, [])
    assert q.agent_score is None and q.doc_quality is None and q.score == q.static_score
