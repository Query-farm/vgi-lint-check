"""Tests for the cross-object consistency rules: VGI143/144 (naming), VGI205/315 (types)."""

from tests import fixtures as F
from vgi_lint_check.config import Config, Options
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext


def _findings(cat, **kw):
    cfg = Config(**kw)
    return run(select_rules(cfg), RuleContext(cat, cfg))


def codes(cat, **kw):
    return {f.code for f in _findings(cat, **kw)}


# --- VGI143 name-style-consistent ------------------------------------------
def test_vgi143_flags_style_outlier():
    fns = [F.func("main", n, description="d") for n in ("f_one", "f_two", "f_three")]
    fns.append(F.func("main", "camelFn", description="d"))  # the outlier
    s = F.schema("main", tables=[F.table("main", "things")], functions=fns)
    out = [f for f in _findings(F.catalog(s)) if f.code == "VGI143"]
    assert out and any("camelFn" in f.message for f in out)


def test_vgi143_all_snake_passes():
    fns = [F.func("main", n, description="d") for n in ("f_one", "f_two", "f_three", "f_four")]
    assert "VGI143" not in codes(F.catalog(F.schema("main", functions=fns)))


def test_vgi143_below_floor_ignored():
    # only 2 names (schema + 1 function) -> below the consistency floor
    s = F.schema("main", functions=[F.func("main", "camelFn", description="d")])
    assert "VGI143" not in codes(F.catalog(s))


# --- VGI144 table-name-number-consistent -----------------------------------
def test_vgi144_flags_mixed_plurality():
    tbls = [F.table("main", n) for n in ("orders", "customers", "invoices", "payment")]
    s = F.schema("main", tables=tbls)
    out = [f for f in _findings(F.catalog(s)) if f.code == "VGI144"]
    assert out and any("payment" in f.message for f in out)  # singular minority flagged


def test_vgi144_consistent_plural_passes():
    tbls = [F.table("main", n) for n in ("orders", "customers", "invoices", "payments")]
    assert "VGI144" not in codes(F.catalog(F.schema("main", tables=tbls)))


# --- VGI315 argument-type-consistent ---------------------------------------
def test_vgi315_flags_arg_type_drift():
    f1 = F.func("main", "a", description="d", arguments=[F.arg("symbol", "VARCHAR", "the symbol")])
    f2 = F.func("main", "b", description="d", arguments=[F.arg("symbol", "BIGINT", "the symbol")])
    s = F.schema("main", functions=[f1, f2])
    out = [f for f in _findings(F.catalog(s)) if f.code == "VGI315"]
    assert out and "symbol" in out[0].message
    # opt-out via the ignore list
    assert "VGI315" not in codes(
        F.catalog(s), options=Options(type_consistency_ignore_names=["symbol"])
    )


def test_vgi315_consistent_type_passes():
    f1 = F.func("main", "a", description="d", arguments=[F.arg("symbol", "VARCHAR", "s")])
    f2 = F.func("main", "b", description="d", arguments=[F.arg("symbol", "VARCHAR", "s")])
    assert "VGI315" not in codes(F.catalog(F.schema("main", functions=[f1, f2])))


# --- VGI205 column-type-consistent -----------------------------------------
def test_vgi205_flags_column_type_drift():
    t1 = F.table("main", "a", columns=[F.col("main", "a", "amount", dtype="DOUBLE")])
    t2 = F.table("main", "b", columns=[F.col("main", "b", "amount", dtype="VARCHAR")])
    s = F.schema("main", tables=[t1, t2])
    out = [f for f in _findings(F.catalog(s)) if f.code == "VGI205"]
    assert out and "amount" in out[0].message


def test_vgi205_consistent_type_passes():
    t1 = F.table("main", "a", columns=[F.col("main", "a", "amount", dtype="DOUBLE")])
    t2 = F.table("main", "b", columns=[F.col("main", "b", "amount", dtype="DOUBLE")])
    assert "VGI205" not in codes(F.catalog(F.schema("main", tables=[t1, t2])))
