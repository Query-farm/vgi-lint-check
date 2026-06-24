"""Tests for the constraint / function / structure rules added later."""

from tests import fixtures as F
from vgi_lint_check.config import Config, Options
from vgi_lint_check.findings import Severity
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext


def codes(cat, **kw):
    cfg = Config(**kw)
    return [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]


def findings(cat, cfg):
    return run(select_rules(cfg), RuleContext(cat, cfg))


# --- constraints ----------------------------------------------------------
def _tbl(name, cols, constraints=()):
    return F.table(
        "main",
        name,
        comment="c",
        tags={"vgi.description_llm": "x" * 50, "vgi.description_md": "y" * 90},
        columns=[F.col("main", name, c, "doc") for c in cols],
        examples=[F.example(0, "d", f"SELECT * FROM v.main.{name}")],
        constraints=constraints,
    )


def test_fk_valid_reference_passes():
    parent = _tbl("parent", ["id"])
    child = _tbl(
        "child",
        ["pid"],
        [
            F.constraint(
                "main",
                "child",
                "FOREIGN KEY",
                columns=["pid"],
                referenced_table="parent",
                referenced_columns=["id"],
            )
        ],
    )
    s = F.schema("main", comment="c", tables=[parent, child])
    assert "VGI801" not in set(codes(F.catalog(s)))


def test_fk_unknown_table_flagged():
    child = _tbl(
        "child",
        ["pid"],
        [
            F.constraint(
                "main",
                "child",
                "FOREIGN KEY",
                columns=["pid"],
                referenced_table="ghost",
                referenced_columns=["id"],
            )
        ],
    )
    s = F.schema("main", comment="c", tables=[child])
    assert "VGI801" in set(codes(F.catalog(s)))


def test_fk_unknown_referenced_column_flagged():
    parent = _tbl("parent", ["id"])
    child = _tbl(
        "child",
        ["pid"],
        [
            F.constraint(
                "main",
                "child",
                "FOREIGN KEY",
                columns=["pid"],
                referenced_table="parent",
                referenced_columns=["nope"],
            )
        ],
    )
    s = F.schema("main", comment="c", tables=[parent, child])
    assert "VGI801" in set(codes(F.catalog(s)))


def test_fk_cross_schema_resolves():
    parent = _tbl("parent", ["id"])
    sp = F.schema("other", comment="c", tables=[parent])
    child = _tbl(
        "child",
        ["pid"],
        [
            F.constraint(
                "main",
                "child",
                "FOREIGN KEY",
                columns=["pid"],
                referenced_table="parent",
                referenced_columns=["id"],
            )
        ],
    )
    sc = F.schema("main", comment="c", tables=[child])
    assert "VGI801" not in set(codes(F.catalog(sc, sp)))


def test_pk_missing_column_flagged():
    t = _tbl("t", ["a"], [F.constraint("main", "t", "PRIMARY KEY", columns=["missing"])])
    s = F.schema("main", comment="c", tables=[t])
    assert "VGI802" in set(codes(F.catalog(s)))


def test_check_binds_rule_uses_connection():
    from vgi_lint_check.rules.constraints import CheckConstraintBinds

    t = _tbl(
        "t", ["a"], [F.constraint("main", "t", "CHECK", columns=["a"], expression="CHECK((a > 0))")]
    )
    s = F.schema("main", comment="c", tables=[t])

    class BadCon:
        def execute(self, sql):
            raise RuntimeError("binder error")

    cfg = Config(execute=True)
    ctx = RuleContext(F.catalog(s), cfg, connection=BadCon())
    ctx.severity = Severity.ERROR
    out = list(CheckConstraintBinds().check(ctx))
    assert out and out[0].code == "VGI803"


# --- functions ------------------------------------------------------------
def test_unnamed_arguments_flagged():
    f0 = F.func("main", "f", "scalar", description="ok", parameters=["col0", "x"])
    s = F.schema("main", comment="c", functions=[f0])
    assert "VGI305" in set(codes(F.catalog(s)))


def test_named_arguments_pass():
    f0 = F.func(
        "main",
        "f",
        "scalar",
        description="does a useful thing",
        parameters=["intensity"],
        examples=[F.example(0, "d", "SELECT v.main.f(1)")],
    )
    s = F.schema("main", comment="c", functions=[f0])
    assert "VGI305" not in set(codes(F.catalog(s)))


def test_function_description_quality_short():
    f0 = F.func("main", "f", "scalar", description="hi", parameters=["x"])
    s = F.schema("main", comment="c", functions=[f0])
    assert "VGI304" in set(codes(F.catalog(s)))


def test_scalar_function_example_flagged():
    f0 = F.func("main", "f", "scalar", description="a thorough description here", parameters=["x"])
    s = F.schema("main", comment="c", functions=[f0])
    assert "VGI306" in set(codes(F.catalog(s)))


# --- structure / schema examples (opt-in) ---------------------------------
def test_schema_object_count_opt_in():
    s = F.schema("main", comment="c", tables=[_tbl("a", ["x"]), _tbl("b", ["x"])])
    # off by default
    assert "VGI117" not in set(codes(F.catalog(s)))
    cfg = Config(severity_overrides={"VGI117": Severity.WARNING})
    cfg.options = Options(max_schema_objects=1)
    assert "VGI117" in {f.code for f in findings(F.catalog(s), cfg)}


def test_schema_examples_opt_in():
    s = F.schema("main", comment="c")
    assert "VGI506" not in set(codes(F.catalog(s)))
    cfg = Config(severity_overrides={"VGI506": Severity.INFO})
    assert "VGI506" in {f.code for f in findings(F.catalog(s), cfg)}
