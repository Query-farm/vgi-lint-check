"""Tests for the strict-default rules added in the quality pass."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext

_TAGS = {
    "vgi.doc_llm": "Zoo domain for LLM use, with plenty of length here to pass.",
    "vgi.doc_md": "## Zoo\nAnimals and attributes — full reference, long enough text.",
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


def test_vgi102_description_not_duplicate():
    dup = "Classifies a Richter magnitude into a severity band."
    fn = F.func(
        "main",
        "magnitude_class",
        description=dup,
        tags={"vgi.doc_llm": dup, "vgi.doc_md": "## Magnitude\nA fuller writeup."},
    )
    s = F.schema("main", comment="c", tags=_TAGS, functions=[fn])
    found = [
        f
        for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))
        if f.code == "VGI102"
    ]
    assert found and "doc_llm" in found[0].message
    # a genuinely distinct llm description is not flagged
    fn2 = F.func(
        "main",
        "magnitude_class",
        description=dup,
        tags={
            "vgi.doc_llm": "Use this to bucket quakes for alerting; input is Richter, "
            "output is one of micro/minor/light/moderate/strong/major.",
            "vgi.doc_md": "## Magnitude\nA fuller writeup.",
        },
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, functions=[fn2])
    assert "VGI102" not in [
        f.code for f in run(select_rules(Config()), RuleContext(F.catalog(s2), Config()))
    ]


def test_deprecated_doc_tag_aliases_resolve():
    # the old keys still satisfy presence rules (dual recognition)...
    fn = F.func(
        "main",
        "f",
        description="short",
        tags={
            "vgi.description_llm": "An LLM-oriented narrative for the function.",
            "vgi.description_md": "## f\nA fuller writeup of f.",
        },
    )
    s = F.schema("main", comment="c", tags=_TAGS, functions=[fn])
    codes = _codes(F.catalog(s))
    assert "VGI112" not in codes  # description_llm present via the deprecated key
    assert "VGI113" not in codes
    # ...but the migration rule (VGI405) nudges toward the new keys
    assert codes.count("VGI405") == 2  # one per deprecated key on the function


def test_vgi405_names_the_new_key():
    fn = F.func("main", "f", description="d", tags={"vgi.description_md": "## f\nwriteup"})
    s = F.schema("main", comment="c", tags=_TAGS, functions=[fn])
    msgs = [
        f.message
        for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))
        if f.code == "VGI405"
    ]
    assert any("vgi.description_md" in m for m in msgs)


def test_result_columns_md_rename_dual_recognition():
    from vgi_lint_check.model import TagSet

    # old key resolves to the new canonical key
    assert TagSet({"vgi.columns_md": "## cols"}).has("vgi.result_columns_md")
    assert TagSet({"vgi.columns_md": "## cols"}).get("vgi.result_columns_md") == "## cols"
    # VGI307 (table fn columns) is satisfied by either key; VGI405 nudges migration
    tf = F.func("main", "scan", "table", description="d", tags={"vgi.columns_md": "## a | b"})
    s = F.schema("main", comment="c", tags=_TAGS, functions=[tf])
    codes = _codes(F.catalog(s))
    assert "VGI307" not in codes  # columns documented via the old key
    assert "VGI405" in codes  # ...but flagged as deprecated


def test_vgi172_doc_links():
    from vgi_lint_check.model import TagSet
    from vgi_lint_check.tags import decode_doc_links

    # decoder accepts bare strings and {title,url} objects
    links, err = decode_doc_links(
        TagSet(
            {"vgi.doc_links": '["https://a.test/x", {"title": "API", "url": "https://b.test/api"}]'}
        )
    )
    assert err is None and [lk.url for lk in links] == ["https://a.test/x", "https://b.test/api"]
    assert links[1].title == "API"
    # VGI172 flags malformed JSON and non-http entries
    bad = F.func("main", "f", description="d", tags={**_TAGS, "vgi.doc_links": '["see the wiki"]'})
    s = F.schema("main", comment="c", tags=_TAGS, functions=[bad])
    assert "VGI172" in _codes(F.catalog(s))
    good = F.func(
        "main",
        "g",
        description="d",
        tags={**_TAGS, "vgi.doc_links": '[{"title":"Docs","url":"https://x.test/d"}]'},
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, functions=[good])
    assert "VGI172" not in _codes(F.catalog(s2))


def test_vgi138_keywords_json_array():
    from vgi_lint_check.tags import keywords_is_json_array, parse_keywords

    # parse_keywords accepts both forms
    assert parse_keywords('["a", "b"]') == ["a", "b"]
    assert parse_keywords("a, b") == ["a", "b"]  # legacy
    assert keywords_is_json_array('["a","b"]') and not keywords_is_json_array("a, b")
    # legacy comma form is flagged; JSON array is clean
    legacy = F.table("main", "t", comment="c", tags={**_TAGS, "vgi.keywords": "seismic, tremor"})
    s = F.schema("main", comment="c", tags=_TAGS, tables=[legacy])
    assert "VGI138" in _codes(F.catalog(s))
    ok = F.table("main", "t", comment="c", tags={**_TAGS, "vgi.keywords": '["seismic","tremor"]'})
    s2 = F.schema("main", comment="c", tags=_TAGS, tables=[ok])
    assert "VGI138" not in _codes(F.catalog(s2))


def test_vgi139_source_url_catalog_only():
    # source_url on a function is flagged; the same value belongs on the catalog
    fn = F.func("main", "predict", description="d", tags={"vgi.source_url": "https://x.test/repo"})
    s = F.schema("main", comment="c", tags=_TAGS, functions=[fn])
    codes = _codes(F.catalog(s))
    assert "VGI139" in codes
    # VGI128 (per-object source_url required) is now opt-in -> off by default
    assert "VGI128" not in codes
    # a catalog-level source_url (the discovery field) does not trip VGI139
    clean = F.schema("main", comment="c", tags=_TAGS, tables=[F.table("main", "t", comment="c")])
    assert "VGI139" not in _codes(F.catalog(clean))
