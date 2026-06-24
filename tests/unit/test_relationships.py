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


# --- VGI804 NOT NULL constraints present ----------------------------------
def test_no_not_null_anywhere_flagged():
    t = F.table(
        "main",
        "t",
        comment="A table with columns but no constraints",
        columns=[F.col("main", "t", "a", "the a value")],
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI804" in set(codes(cat))


def test_not_null_present_passes():
    t = F.table(
        "main",
        "t",
        comment="A table with a NOT NULL constraint declared",
        columns=[F.col("main", "t", "a", "the a value")],
        constraints=[F.constraint("main", "t", "NOT NULL", columns=["a"])],
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI804" not in set(codes(cat))
