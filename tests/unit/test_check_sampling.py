"""VGI811 — sampled CHECK-constraint validation against the live worker (offline)."""

from __future__ import annotations

import tests.fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.findings import Severity
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.rules.constraints import CheckConstraintHolds


class FakeCursor:
    """Answers the rule's ``count(*) / count(*) FILTER`` aggregate.

    Returns a single ``(n, bad)`` row. ``USING SAMPLE`` can be made to raise to
    exercise the deterministic ``LIMIT`` fallback or a mandatory-filter skip.
    """

    def __init__(self, n, bad, *, support_sample=True, policy_error=False):
        self.n, self.bad = n, bad
        self.support_sample = support_sample
        self.policy_error = policy_error
        self.sql_seen: list[str] = []
        self._result: list[tuple] = []

    def execute(self, sql, params=None):
        self.sql_seen.append(sql)
        if "USING SAMPLE" in sql:
            if self.policy_error:
                raise RuntimeError("Invalid Error: a WHERE filter is required for this scan")
            if not self.support_sample:
                raise RuntimeError("Parser Error: USING SAMPLE is not supported here")
        elif self.policy_error:
            raise RuntimeError("Invalid Error: a WHERE filter is required for this scan")
        self._result = [(self.n, self.bad)]
        return self

    def fetchall(self):
        return self._result


def _catalog(expr="price >= 0"):
    accounts = F.table(
        "main",
        "accounts",
        columns=[F.col("main", "accounts", "price", dtype="INTEGER")],
        constraints=[F.constraint("main", "accounts", "CHECK", expression=f"CHECK({expr})")],
    )
    return F.catalog(F.schema("main", comment="c", tables=[accounts]))


def _run(con, cfg=None, expr="price >= 0"):
    cfg = cfg or Config(execute=True)
    ctx = RuleContext(_catalog(expr), cfg, connection=con)
    ctx.severity = Severity.WARNING
    return list(CheckConstraintHolds().check(ctx))


def test_violating_rows_are_flagged():
    out = _run(FakeCursor(n=100, bad=3))
    assert len(out) == 1 and out[0].code == "VGI811"
    assert "3 of 100" in out[0].message
    assert "random sample of 100 rows" in out[0].message
    assert "price >= 0" in out[0].message


def test_all_rows_satisfy_no_finding():
    assert _run(FakeCursor(n=100, bad=0)) == []


def test_uses_is_false_semantics():
    con = FakeCursor(n=10, bad=1)
    _run(con)
    assert any("IS FALSE" in s for s in con.sql_seen)


def test_falls_back_to_limit_when_sampling_unsupported():
    con = FakeCursor(n=7, bad=2, support_sample=False)
    out = _run(con)
    assert len(out) == 1
    assert "all 7 rows" in out[0].message  # under-filled LIMIT => exhaustive
    assert any("LIMIT" in s for s in con.sql_seen)


def test_filter_policy_scan_is_skipped():
    assert _run(FakeCursor(n=0, bad=0, policy_error=True)) == []


def test_empty_table_emits_nothing():
    assert _run(FakeCursor(n=0, bad=0)) == []


def test_disabled_without_execute():
    cfg = Config(execute=False)
    assert cfg.effective_severity(CheckConstraintHolds()) == Severity.OFF
