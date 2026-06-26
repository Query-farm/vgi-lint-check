"""VGI810 — sampled foreign-key referential-integrity probe (offline)."""

from __future__ import annotations

import pytest

import tests.fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.findings import Severity
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.rules.constraints import ForeignKeyReferencesResolve


class FakeCursor:
    """Answers the rule's three query shapes from canned child/parent data.

    - ``USING SAMPLE`` (rung 1): random child sample, or raise if unsupported.
    - ``LIMIT``        (rung 2): deterministic child fallback.
    - ``IN (...)``     parent probe: the subset of params present in the parent.
    A ``policy_error`` makes every child scan raise a mandatory-filter rejection.
    """

    def __init__(self, child, parent, *, support_sample=True, policy_error=False):
        self.child, self.parent = list(child), set(parent)
        self.support_sample = support_sample
        self.policy_error = policy_error
        self.sql_seen: list[str] = []
        self._result: list[tuple] = []

    def execute(self, sql, params=None):
        self.sql_seen.append(sql)
        if " IN (" in sql:  # parent probe
            self._result = [(v,) for v in (params or []) if v in self.parent]
        elif "USING SAMPLE" in sql:  # rung 1
            if self.policy_error:
                raise RuntimeError("Invalid Error: a WHERE filter is required for this scan")
            if not self.support_sample:
                raise RuntimeError("Parser Error: USING SAMPLE is not supported here")
            self._result = [(v,) for v in self.child]
        else:  # rung 2 (LIMIT fallback)
            if self.policy_error:
                raise RuntimeError("Invalid Error: a WHERE filter is required for this scan")
            self._result = [(v,) for v in self.child]
        return self

    def fetchall(self):
        return self._result


def _catalog():
    users = F.table(
        "main",
        "users",
        columns=[F.col("main", "users", "id", dtype="INTEGER")],
        constraints=[F.constraint("main", "users", "PRIMARY KEY", columns=["id"])],
    )
    orders = F.table(
        "main",
        "orders",
        columns=[F.col("main", "orders", "user_id", dtype="INTEGER")],
        constraints=[
            F.constraint(
                "main",
                "orders",
                "FOREIGN KEY",
                columns=["user_id"],
                referenced_table="users",
                referenced_columns=["id"],
            )
        ],
    )
    return F.catalog(F.schema("main", comment="c", tables=[users, orders]))


def _run(con, cfg=None):
    cfg = cfg or Config(execute=True)
    ctx = RuleContext(_catalog(), cfg, connection=con)
    ctx.severity = Severity.WARNING
    return list(ForeignKeyReferencesResolve().check(ctx))


def test_orphan_value_is_flagged():
    out = _run(FakeCursor(child=[1, 2, 3], parent={1, 2}))
    assert len(out) == 1 and out[0].code == "VGI810"
    assert "1 of 3" in out[0].message
    assert "random sample" in out[0].message
    assert '"orders".user_id' in out[0].message and '"users".id' in out[0].message


def test_all_values_resolve_no_finding():
    assert _run(FakeCursor(child=[1, 2, 3], parent={1, 2, 3})) == []


def test_falls_back_to_limit_when_sampling_unsupported():
    con = FakeCursor(child=[1, 2, 3], parent={1, 2}, support_sample=False)
    out = _run(con)
    assert len(out) == 1
    # Under-filled LIMIT => exhaustive => exact wording, not "random sample".
    assert "all 3 distinct values" in out[0].message
    assert any("LIMIT" in s for s in con.sql_seen)


def test_filter_policy_scan_is_skipped_not_failed():
    con = FakeCursor(child=[1, 2, 3], parent=set(), policy_error=True)
    assert _run(con) == []  # mandatory-filter policy: skip, never false-positive


def test_all_null_column_emits_nothing():
    assert _run(FakeCursor(child=[], parent={1, 2})) == []


def test_disabled_without_execute():
    cfg = Config(execute=False)
    assert cfg.effective_severity(ForeignKeyReferencesResolve()) == Severity.OFF


@pytest.mark.parametrize("size", [10, 250])
def test_sample_size_is_configurable(size):
    cfg = Config(execute=True)
    cfg.sample_size = size
    con = FakeCursor(child=[1, 2], parent={1, 2}, support_sample=True)
    _run(con, cfg)
    assert any(f"USING SAMPLE {size} ROWS" in s for s in con.sql_seen)
