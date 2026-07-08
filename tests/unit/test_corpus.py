"""Tests for AST-based reference extraction (sql_parse) + corpus coverage.

Offline: sql_parse serializes on a private in-memory DuckDB (json_serialize_sql
is a core built-in — no worker attach, no network).
"""

from __future__ import annotations

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.corpus import compute_corpus_coverage
from vgi_lint_check.findings import Severity
from vgi_lint_check.model import AgentTask, ExampleStatement
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.rules.examples import (
    ExampleReferenceResolves,
    MacroDemonstratedOnInput,
    ObjectUndemonstrated,
    ObjectUntested,
    TestReferenceResolves,
)
from vgi_lint_check.sql_parse import parse_refs


# --- sql_parse ------------------------------------------------------------
def test_parse_captures_table_function_and_scalar():
    refs = parse_refs(
        "SELECT om.main.decode(weather_code) FROM om.main.forecast(52.5, 13.4) ORDER BY t"
    )
    assert refs is not None
    names = {(r.name, r.const_only_args) for r in refs.functions}
    # table-function forecast (const args) AND the macro on a column (live).
    assert ("forecast", True) in names
    assert ("decode", False) in names
    assert refs.tables == ()  # a table-function is not a BASE_TABLE


def test_parse_const_only_macro():
    refs = parse_refs("SELECT om.main.decode(61)")
    assert refs is not None
    (fn,) = refs.functions
    assert fn.name == "decode" and fn.const_only_args is True
    assert fn.catalog == "om" and fn.schema == "main"


def test_parse_base_table_and_cte_excluded():
    refs = parse_refs("WITH c AS (SELECT * FROM om.main.t) SELECT * FROM c")
    assert refs is not None
    # the real base table is captured; the CTE alias `c` is not.
    assert [(t.catalog, t.schema, t.name) for t in refs.tables] == [("om", "main", "t")]


def test_parse_invalid_returns_none():
    assert parse_refs("this is not sql @@@") is None
    assert parse_refs("   ") is None


def test_parse_empty_select_is_not_none():
    refs = parse_refs("SELECT 1")
    assert refs is not None and refs.all == ()


# --- corpus coverage ------------------------------------------------------
def _catalog(*, decode_example: str, extra_examples=()):
    """A worker with a table-fn, a decoder macro, and an undemonstrated table-fn."""
    forecast = F.func(
        "main",
        "forecast",
        "table",
        examples=[F.example(1, "forecast Berlin", "SELECT * FROM v.main.forecast(52.5, 13.4)")],
    )
    decode = F.func("main", "decode", "macro", examples=[F.example(1, "decode", decode_example)])
    elevation = F.func("main", "elevation", "table")  # no example, no task
    ghost_host = F.func(
        "main",
        "with_ghost",
        "scalar",
        examples=[F.example(i, d, s) for i, d, s in extra_examples],
    )
    return F.catalog(F.schema("main", functions=[forecast, decode, elevation, ghost_host]))


def test_coverage_demonstrated_and_undemonstrated():
    cat = _catalog(decode_example="SELECT v.main.decode(61)")
    cov = compute_corpus_coverage(cat)
    assert "main.forecast" in cov.demonstrated
    assert "main.decode" in cov.demonstrated
    # elevation and with_ghost have no self-calling example.
    undemo = {oid.name for oid in cov.undemonstrated()}
    assert "elevation" in undemo
    assert cov.doc_ratio() == 2 / 4


def test_macro_const_only_detected_and_cleared():
    const = compute_corpus_coverage(_catalog(decode_example="SELECT v.main.decode(61)"))
    assert "main.decode" in const.macro_const_only

    live = compute_corpus_coverage(
        _catalog(decode_example="SELECT v.main.decode(weather_code) FROM v.main.forecast(1,2)")
    )
    assert live.macro_const_only == {}


def test_broken_doc_reference():
    cat = _catalog(
        decode_example="SELECT v.main.decode(61)",
        extra_examples=[(1, "typo", "SELECT v.main.no_such_fn(1)")],
    )
    cov = compute_corpus_coverage(cat)
    broken = [(b.reference, b.source) for b in cov.broken]
    assert ("main.no_such_fn", "doc") in broken


def test_test_coverage_and_broken_test_reference():
    cat = _catalog(decode_example="SELECT v.main.decode(61)")
    cat.agent_test_tasks = [
        AgentTask(
            name="t1",
            prompt="p",
            reference_statements=[
                ExampleStatement(description=None, sql="SELECT * FROM v.main.forecast(1,2)")
            ],
            check_sql="SELECT v.main.ghost_check(1)",
        )
    ]
    cov = compute_corpus_coverage(cat)
    assert cov.has_test_suite is True
    assert "main.forecast" in cov.tested
    assert cov.test_ratio() == 1 / 4
    assert ("main.ghost_check", "test") in [(b.reference, b.source) for b in cov.broken]


def test_no_suite_means_test_ratio_none():
    cov = compute_corpus_coverage(_catalog(decode_example="SELECT v.main.decode(61)"))
    assert cov.has_test_suite is False
    assert cov.test_ratio() is None


# --- rules ----------------------------------------------------------------
def _ctx(cat):
    return RuleContext(cat, Config())


def test_rule_object_undemonstrated():
    cat = _catalog(decode_example="SELECT v.main.decode(61)")
    findings = list(ObjectUndemonstrated().check(_ctx(cat)))
    assert all(f.severity is Severity.WARNING for f in findings)
    assert {f.object_id.name for f in findings} >= {"elevation"}
    assert "forecast" not in {f.object_id.name for f in findings}


def test_rule_macro_demonstrated_on_input():
    const = list(
        MacroDemonstratedOnInput().check(_ctx(_catalog(decode_example="SELECT v.main.decode(61)")))
    )
    assert {f.object_id.name for f in const} == {"decode"}
    live = list(
        MacroDemonstratedOnInput().check(
            _ctx(_catalog(decode_example="SELECT v.main.decode(x) FROM v.main.forecast(1,2)"))
        )
    )
    assert live == []


def test_rule_example_reference_resolves():
    cat = _catalog(
        decode_example="SELECT v.main.decode(61)",
        extra_examples=[(1, "typo", "SELECT v.main.no_such_fn(1)")],
    )
    findings = list(ExampleReferenceResolves().check(_ctx(cat)))
    assert len(findings) == 1
    assert "no_such_fn" in findings[0].message


def test_rule_object_untested_gated_on_suite():
    cat = _catalog(decode_example="SELECT v.main.decode(61)")
    assert list(ObjectUntested().check(_ctx(cat))) == []  # no suite -> silent
    cat.agent_test_tasks = [
        AgentTask(
            name="t1",
            prompt="p",
            reference_statements=[
                ExampleStatement(description=None, sql="SELECT * FROM v.main.forecast(1,2)")
            ],
        )
    ]
    untested = {f.object_id.name for f in ObjectUntested().check(_ctx(cat))}
    assert "forecast" not in untested
    assert {"decode", "elevation"} <= untested


def test_rule_test_reference_resolves():
    cat = _catalog(decode_example="SELECT v.main.decode(61)")
    cat.agent_test_tasks = [
        AgentTask(name="t1", prompt="p", check_sql="SELECT v.main.ghost_check(1)")
    ]
    findings = list(TestReferenceResolves().check(_ctx(cat)))
    assert len(findings) == 1 and "ghost_check" in findings[0].message
