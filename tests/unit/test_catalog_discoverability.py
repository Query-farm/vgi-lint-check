"""Tests for the catalog (VGI0xx) and discoverability (VGI12x) rules."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.findings import Severity
from vgi_lint_check.model import Release
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext


def codes(cat, **kw):
    cfg = Config(**kw)
    return [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]


# --- catalog required (VGI0xx) --------------------------------------------
def test_catalog_metadata_required_fires_when_missing():
    cat = F.catalog(F.schema("main"), comment=None, tags={}, source_url=None)
    found = set(codes(cat))
    assert {"VGI001", "VGI002", "VGI003", "VGI004"} <= found


def test_catalog_metadata_passes_when_present():
    # F.catalog() defaults supply comment/llm/md/source_url
    cat = F.catalog(F.schema("main", comment="A schema describing main test data"))
    found = set(codes(cat))
    assert not (found & {"VGI001", "VGI002", "VGI003", "VGI004"})


# --- discoverability (VGI12x) ---------------------------------------------
def test_duplicate_descriptions_flagged():
    t1 = F.table("main", "a", comment="Reference data about the system overall")
    t2 = F.table("main", "b", comment="Reference data about the system overall")
    cat = F.catalog(F.schema("main", tables=[t1, t2]))
    assert "VGI120" in set(codes(cat))


def test_short_and_echo_descriptions():
    short = F.table("main", "metrics", comment="metrics")  # short + echo
    cat = F.catalog(F.schema("main", tables=[short]))
    found = set(codes(cat))
    assert "VGI121" in found  # too short
    assert "VGI122" in found  # echoes name


def test_trivial_examples_flagged():
    t = F.table(
        "main",
        "t",
        comment="A table with only a trivial example query",
        examples=[F.example(0, "all", "SELECT * FROM v.main.t")],
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI150" in set(codes(cat))


def test_minimum_examples_flagged():
    t = F.table(
        "main",
        "t",
        comment="A table that ships too few example queries",
        examples=[F.example(0, "x", "SELECT name FROM v.main.t WHERE id=1")],
    )
    cat = F.catalog(F.schema("main", tables=[t]))
    assert "VGI151" in set(codes(cat))  # 1 < default min 3


def test_release_freshness_rules():
    cat = F.catalog(
        F.schema("main"),
        releases=[Release(version="1.0.0", released_at=None, summary="", notes_url=None)],
    )
    found = set(codes(cat))
    assert "VGI140" in found  # no released_at
    assert "VGI141" in found  # no summary/notes_url


def test_classifying_tag_and_units_are_opt_in():
    untagged = F.table(
        "main",
        "t",
        comment="A table with no classifying tags at all",
        columns=[F.col("main", "t", "depth", "the depth value", "INTEGER")],
    )
    cat = F.catalog(F.schema("main", tables=[untagged]))
    # off by default
    assert "VGI123" not in set(codes(cat))
    assert "VGI131" not in set(codes(cat))
    # opt in
    on = codes(cat, severity_overrides={"VGI123": Severity.INFO, "VGI131": Severity.INFO})
    assert "VGI123" in set(on)
    assert "VGI131" in set(on)  # numeric 'depth' comment has no unit
