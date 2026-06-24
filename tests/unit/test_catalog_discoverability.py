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


# --- data-version semver validity (VGI005/006/007) -----------------------
def test_data_version_spec_and_releases_valid():
    cat = F.catalog(
        F.schema("main"),
        releases=[Release(version="1.0.0"), Release(version="1.5.0")],
    )
    cat.data_version_spec = ">=1.0.0,<2.0.0"
    found = set(codes(cat))
    assert not (found & {"VGI005", "VGI006", "VGI007"})


def test_invalid_data_version_spec_flagged():
    cat = F.catalog(F.schema("main"))
    cat.data_version_spec = "not a spec"
    assert "VGI005" in set(codes(cat))


def test_invalid_release_version_flagged():
    cat = F.catalog(F.schema("main"), releases=[Release(version="v1.2.three")])
    assert "VGI006" in set(codes(cat))


def test_release_outside_spec_flagged():
    cat = F.catalog(
        F.schema("main"),
        releases=[Release(version="1.0.0"), Release(version="2.5.0")],
    )
    cat.data_version_spec = ">=1.0.0,<2.0.0"
    found = set(codes(cat))
    assert "VGI007" in found  # 2.5.0 is outside >=1.0.0,<2.0.0
    assert "VGI005" not in found  # the spec itself is valid


# --- discoverability (VGI12x) ---------------------------------------------
def test_duplicate_descriptions_flagged():
    t1 = F.table("main", "a", comment="Reference data about the system overall")
    t2 = F.table("main", "b", comment="Reference data about the system overall")
    cat = F.catalog(F.schema("main", tables=[t1, t2]))
    assert "VGI120" in set(codes(cat))


def test_duplicate_descriptions_span_schemas_and_functions():
    dup = "Reference data describing the overall system and its parts"
    f1 = F.func("main", "f1", "scalar", description=dup)
    f2 = F.func("main", "f2", "scalar", description=dup)
    s = F.schema("main", comment=dup, functions=[f1, f2])
    cfg = Config()
    findings = [
        f for f in run(select_rules(cfg), RuleContext(F.catalog(s), cfg)) if f.code == "VGI120"
    ]
    flagged = {f.object_id.qualified() for f in findings}
    # schema + both functions all flagged as sharing one description
    assert {"v.main", "v.main.f1", "v.main.f2"} <= flagged
    assert findings[0].severity is Severity.WARNING


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


def test_minimum_examples_counts_table_functions():
    # A table-function-only worker (e.g. a model-registry worker) keeps all its
    # examples on table-functions: excluded from iter_functions() and with no
    # materialized table rows. VGI151 must still count them (via
    # iter_all_functions()), so a worker shipping >= min examples does not fire.
    fns = [
        F.func(
            "main",
            f"f{i}",
            ftype="table",
            description="A table function that ships an example",
            examples=[F.example(0, "demo", f"SELECT * FROM v.main.f{i}()")],
        )
        for i in range(3)
    ]
    cat = F.catalog(F.schema("main", functions=fns))
    assert "VGI151" not in set(codes(cat))  # 3 table-function examples >= min 3


def test_release_freshness_rules():
    cat = F.catalog(
        F.schema("main"),
        releases=[Release(version="1.0.0", released_at=None, summary="", notes_url=None)],
    )
    found = set(codes(cat))
    assert "VGI140" in found  # no released_at
    assert "VGI141" in found  # no summary/notes_url


def test_title_keywords_present_opt_in_and_quality():
    # presence rules (VGI124 title, VGI126 keywords) are off by default
    t = F.table("main", "t", comment="A table for testing title/keyword conventions")
    cat = F.catalog(F.schema("main", tables=[t]))
    base = set(codes(cat))
    assert "VGI124" not in base and "VGI126" not in base

    # quality rules fire when the tags ARE set badly
    bad = F.table(
        "main",
        "metrics",
        comment="A metrics table for testing tag quality checks",
        tags={"vgi.title": "metrics", "vgi.keywords": "a, a, b"},  # echo + duplicate
    )
    cat2 = F.catalog(F.schema("main", tables=[bad]))
    found = set(codes(cat2))
    assert "VGI125" in found  # title echoes the name
    assert "VGI127" in found  # duplicate keywords


def test_source_url_present_opt_in_and_valid():
    bad = F.table(
        "main",
        "t",
        comment="A table whose source link is not a real URL here",
        tags={"vgi.source_url": "see the repo"},  # not http(s)
    )
    cat = F.catalog(F.schema("main", tables=[bad]))
    found = set(codes(cat))
    assert "VGI128" not in found  # presence is opt-in
    assert "VGI129" in found  # invalid URL flagged when present


def test_catalog_support_tags():
    # F.catalog defaults supply support contact + policy url -> no finding
    assert "VGI009" not in set(codes(F.catalog(F.schema("main"))))
    bare = F.catalog(
        F.schema("main"),
        tags={"vgi.description_llm": "x" * 50, "vgi.description_md": "y" * 90},
    )
    assert "VGI009" in set(codes(bare))


def test_catalog_attribution_required_tags():
    # F.catalog defaults supply author/copyright/license -> no finding
    assert "VGI160" not in set(codes(F.catalog(F.schema("main"))))
    bare = F.catalog(
        F.schema("main"),
        tags={"vgi.description_llm": "x" * 50, "vgi.description_md": "y" * 90},
    )
    assert "VGI160" in set(codes(bare))


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
