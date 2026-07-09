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
        tags={"vgi.doc_llm": "x" * 50, "vgi.doc_md": "y" * 90},
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


def test_table_function_columns_documented():
    import json

    # un-backed table function that declares no result schema -> flagged
    tf = F.func("main", "read_thing", ftype="table", description="reads things dynamically")
    s = F.schema("main", comment="c", functions=[tf])
    assert "VGI307" in set(codes(F.catalog(s)))

    # declaring a static vgi.result_columns_schema clears it
    tf2 = F.func(
        "main",
        "read_thing",
        ftype="table",
        description="reads things dynamically",
        tags={
            "vgi.result_columns_schema": json.dumps(
                [{"name": "id", "type": "INTEGER", "description": "row id"}]
            )
        },
    )
    assert "VGI307" not in set(codes(F.catalog(F.schema("main", comment="c", functions=[tf2]))))

    # so does a dynamic vgi.result_dynamic_columns_md
    tf3 = F.func(
        "main",
        "read_thing",
        ftype="table",
        description="reads things dynamically",
        tags={
            "vgi.result_dynamic_columns_md": (
                "Varies by mode.\n\n| Name | Type | Description |\n"
                "| --- | --- | --- |\n| id | INTEGER | row id |\n"
            )
        },
    )
    assert "VGI307" not in set(codes(F.catalog(F.schema("main", comment="c", functions=[tf3]))))

    # declaring BOTH is a contradiction -> flagged
    tf4 = F.func(
        "main",
        "read_thing",
        ftype="table",
        description="reads things dynamically",
        tags={
            "vgi.result_columns_schema": json.dumps(
                [{"name": "id", "type": "INTEGER", "description": "row id"}]
            ),
            "vgi.result_dynamic_columns_md": (
                "| Name | Type | Description |\n| --- | --- | --- |\n| id | INTEGER | row id |\n"
            ),
        },
    )
    assert "VGI307" in set(codes(F.catalog(F.schema("main", comment="c", functions=[tf4]))))

    # a table function backed by a same-named table is covered by the table's
    # columns -> not flagged
    backed = F.func("main", "animals", ftype="table", description="scan animals")
    tbl = F.table("main", "animals", comment="Animals table for testing backed table funcs")
    s3 = F.schema("main", comment="c", tables=[tbl], functions=[backed])
    assert "VGI307" not in set(codes(F.catalog(s3)))


def test_view_executes_rule_flags_broken_view():
    from vgi_lint_check.rules.execution import ViewExecutes

    v = F.view("main", "broken_view", comment="A view that fails to bind/execute")
    s = F.schema("main", comment="c", views=[v])

    class BadCon:
        def execute(self, sql):
            raise RuntimeError("Binder Error: referenced column missing")

    cfg = Config(execute=True)
    ctx = RuleContext(F.catalog(s), cfg, connection=BadCon())
    ctx.severity = Severity.ERROR
    out = list(ViewExecutes().check(ctx))
    assert out and out[0].code == "VGI903"

    class OkCon:
        def execute(self, sql):
            return self

    ctx2 = RuleContext(F.catalog(s), cfg, connection=OkCon())
    ctx2.severity = Severity.ERROR
    assert list(ViewExecutes().check(ctx2)) == []


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


# --- structure / schema size ----------------------------------------------
def test_schema_object_count_warns_over_default():
    # a small schema is under the default cap (50) -> clean
    small = F.schema("main", comment="c", tables=[_tbl("a", ["x"]), _tbl("b", ["x"])])
    assert "VGI117" not in set(codes(F.catalog(small)))
    # a schema with > 50 objects warns by default
    big = F.schema("main", comment="c", tables=[_tbl(f"t{i}", ["x"]) for i in range(60)])
    assert "VGI117" in set(codes(F.catalog(big)))
    # ...and the threshold is configurable
    cfg = Config()
    cfg.options = Options(max_schema_objects=1)
    assert "VGI117" in {f.code for f in findings(F.catalog(small), cfg)}


def test_schema_examples_strict_default():
    s = F.schema("main", comment="c")
    # on by default under the strict profile; can be turned off
    assert "VGI506" in set(codes(F.catalog(s)))
    cfg = Config(severity_overrides={"VGI506": Severity.OFF})
    assert "VGI506" not in {f.code for f in findings(F.catalog(s), cfg)}
