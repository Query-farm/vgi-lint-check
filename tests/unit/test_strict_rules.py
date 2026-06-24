"""Tests for the strict-default rules added in the quality pass."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext

_TAGS = {
    "vgi.description_llm": "Zoo domain for LLM use, with plenty of length here to pass.",
    "vgi.description_md": "## Zoo\nAnimals and attributes — full reference, long enough text.",
    "provider": "acme",
    "domain": "zoo",
}


def _codes(cat, **kw):
    cfg = Config(**kw)
    if "options" in kw:
        cfg.options = kw["options"]
    return [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]


def test_vgi130_placeholder_text():
    t = F.table("main", "t", comment="TODO: write this", tags=_TAGS)
    s = F.schema("main", comment="c", tags=_TAGS, tables=[t])
    assert "VGI130" in _codes(F.catalog(s))
    ok = F.table("main", "t", comment="A real, finished description of the table.", tags=_TAGS)
    s2 = F.schema("main", comment="c", tags=_TAGS, tables=[ok])
    assert "VGI130" not in _codes(F.catalog(s2))


def test_vgi132_category_reuse():
    # 4 tables each with a unique 'domain' value -> no reuse -> flagged
    tables = [
        F.table("main", f"t{i}", comment="c", tags={**_TAGS, "domain": f"d{i}"}) for i in range(4)
    ]
    s = F.schema("main", comment="c", tags=_TAGS, tables=tables)
    assert "VGI132" in _codes(F.catalog(s))
    # reused values across objects -> clean
    shared = [
        F.table("main", f"t{i}", comment="c", tags={**_TAGS, "domain": "zoo"}) for i in range(4)
    ]
    s2 = F.schema("main", comment="c", tags=_TAGS, tables=shared)
    assert "VGI132" not in _codes(F.catalog(s2))


def test_vgi807_table_primary_key():
    no_pk = F.table("main", "t", comment="c", tags=_TAGS, columns=[F.col("main", "t", "x")])
    s = F.schema("main", comment="c", tags=_TAGS, tables=[no_pk])
    assert "VGI807" in _codes(F.catalog(s))
    with_pk = F.table(
        "main",
        "t",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "t", "x")],
        constraints=[F.constraint("main", "t", "PRIMARY KEY", columns=["x"])],
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, tables=[with_pk])
    assert "VGI807" not in _codes(F.catalog(s2))


def test_vgi808_suggested_foreign_key():
    owner = F.table(
        "main",
        "owner",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "owner", "id")],
        constraints=[F.constraint("main", "owner", "PRIMARY KEY", columns=["id"])],
    )
    pet = F.table(
        "main",
        "pet",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "pet", "owner_id")],
        constraints=[F.constraint("main", "pet", "PRIMARY KEY", columns=["owner_id"])],
    )
    s = F.schema("main", comment="c", tags=_TAGS, tables=[owner, pet])
    found = [
        f
        for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))
        if f.code == "VGI808"
    ]
    assert found and "owner" in found[0].message


def test_vgi204_timestamp_timezone():
    naive = F.table(
        "main",
        "t",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "t", "ts", "the event time", "TIMESTAMP")],
    )
    s = F.schema("main", comment="c", tags=_TAGS, tables=[naive])
    assert "VGI204" in _codes(F.catalog(s))
    documented = F.table(
        "main",
        "t",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "t", "ts", "event time in UTC", "TIMESTAMP")],
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, tables=[documented])
    assert "VGI204" not in _codes(F.catalog(s2))


def test_vgi013_license_spdx():
    s = F.schema("main", comment="c", tags=_TAGS, tables=[F.table("main", "t", comment="c")])
    bad = F.catalog(s, tags={**F.catalog(s).tags.raw, "vgi.license": "Weird Homemade License"})
    assert "VGI013" in _codes(bad)
    good = F.catalog(s, tags={**F.catalog(s).tags.raw, "vgi.license": "Apache-2.0"})
    assert "VGI013" not in _codes(good)
    custom = F.catalog(s, tags={**F.catalog(s).tags.raw, "vgi.license": "LicenseRef-QueryFarm"})
    assert "VGI013" not in _codes(custom)


def test_vgi510_deterministic_example():
    flaky = F.func(
        "main",
        "f",
        description="d",
        executable_examples=[
            F.exec_example(
                0,
                "top 5",
                [("s", "SELECT v.main.f(x) FROM v.main.t LIMIT 5", [{"a": 1}, {"a": 2}])],
            ),
        ],
    )
    s = F.schema("main", comment="c", tags=_TAGS, functions=[flaky])
    assert "VGI510" in _codes(F.catalog(s))
    ordered = F.func(
        "main",
        "g",
        description="d",
        executable_examples=[
            F.exec_example(
                0,
                "top 5",
                [("s", "SELECT v.main.g(x) FROM v.main.t ORDER BY x LIMIT 5", [{"a": 1}])],
            ),
        ],
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, functions=[ordered])
    assert "VGI510" not in _codes(F.catalog(s2))
