"""Tests for default-schema (VGI008), join-path (VGI133), and NOT NULL (VGI804)."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext


def codes(cat, **kw):
    cfg = Config(**kw)
    return [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]


# --- VGI008 default schema -------------------------------------------------
def test_default_schema_must_exist():
    cat = F.catalog(F.schema("smithsonian"))
    cat.default_schema = "main"  # not a schema this catalog exposes
    assert "VGI008" in set(codes(cat))


def test_default_schema_valid_passes():
    cat = F.catalog(F.schema("smithsonian"))
    cat.default_schema = "smithsonian"
    assert "VGI008" not in set(codes(cat))


def test_default_schema_unknown_skipped():
    cat = F.catalog(F.schema("main"))  # default_schema None -> can't determine
    assert "VGI008" not in set(codes(cat))


# --- VGI133 join path documented ------------------------------------------
def _fk(table, ref):
    return F.constraint(
        "main",
        table,
        "FOREIGN KEY",
        columns=["pid"],
        referenced_table=ref,
        referenced_columns=["id"],
    )


def test_join_path_undocumented_flagged():
    child = F.table(
        "main", "child", comment="Child rows keyed by pid", constraints=[_fk("child", "parent")]
    )
    cat = F.catalog(F.schema("main", tables=[child]))
    assert "VGI133" in set(codes(cat))


def test_join_path_documented_passes():
    child = F.table(
        "main",
        "child",
        comment="Child rows; join to parent on pid to get the parent record",
        constraints=[_fk("child", "parent")],
    )
    cat = F.catalog(F.schema("main", tables=[child]))
    assert "VGI133" not in set(codes(cat))


# --- VGI804/805/806 constraint-completeness (partitioned) ------------------
def _table(name, *constraints):
    return F.table(
        "main",
        name,
        comment=f"A {name} table for constraint-completeness tests here",
        columns=[F.col("main", name, "a", "the a value")],
        constraints=list(constraints),
    )


def test_no_constraints_at_all_flagged():
    cat = F.catalog(F.schema("main", tables=[_table("t")]))  # no constraints
    found = set(codes(cat))
    assert "VGI806" in found  # nothing at all
    assert "VGI804" not in found and "VGI805" not in found  # broader rule wins


def test_no_primary_keys_flagged():
    # has a NOT NULL but no PK -> VGI805, not VGI806 (constraints exist)
    cat = F.catalog(
        F.schema("main", tables=[_table("t", F.constraint("main", "t", "NOT NULL", columns=["a"]))])
    )
    found = set(codes(cat))
    assert "VGI805" in found
    assert "VGI806" not in found and "VGI804" not in found


def test_no_not_null_flagged():
    # has a PK but no NOT NULL -> VGI804, not VGI805/806
    cat = F.catalog(
        F.schema(
            "main", tables=[_table("t", F.constraint("main", "t", "PRIMARY KEY", columns=["a"]))]
        )
    )
    found = set(codes(cat))
    assert "VGI804" in found
    assert "VGI805" not in found and "VGI806" not in found


def test_complete_constraints_pass():
    cat = F.catalog(
        F.schema(
            "main",
            tables=[
                _table(
                    "t",
                    F.constraint("main", "t", "PRIMARY KEY", columns=["a"]),
                    F.constraint("main", "t", "NOT NULL", columns=["a"]),
                )
            ],
        )
    )
    found = set(codes(cat))
    assert not (found & {"VGI804", "VGI805", "VGI806"})
