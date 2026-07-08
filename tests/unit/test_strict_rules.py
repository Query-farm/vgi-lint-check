"""Tests for the strict-default rules added in the quality pass."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.model import ObjectKind
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext

_TAGS = {
    "vgi.doc_llm": (
        "Zoo domain covering animals, their attributes, and the sounds they make, "
        "with enough detail for LLM tool selection to explain the schema's scope "
        "and the entities it contains and how they relate to one another."
    ),
    "vgi.doc_md": (
        "## Zoo\n\nA detailed reference for the zoo domain: animals, attributes, "
        "and sounds, with narrative covering each table and how to use the schema "
        "in practice when answering questions about animals."
    ),
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


def test_vgi310_function_overuses_any():
    all_any = F.func("main", "f", description="d", parameters=["a", "b", "c"])
    all_any.parameter_types[:] = ["ANY", "ANY", "ANY"]
    typed = F.func("main", "g", description="d", parameters=["a", "b"])
    typed.parameter_types[:] = ["VARCHAR", "ANY"]
    one_any = F.func("main", "h", description="d", parameters=["a"])
    one_any.parameter_types[:] = ["ANY"]  # single generic arg is fine
    s = F.schema("main", comment="c", tags=_TAGS, functions=[all_any, typed, one_any])
    flagged = {
        f.object_id.name
        for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))
        if f.code == "VGI310"
    }
    assert flagged == {"f"}


def test_vgi138_keywords_comma_is_error():
    from vgi_lint_check.findings import Severity as Sev

    legacy = F.table("main", "t", comment="c", tags={**_TAGS, "vgi.keywords": "a, b"})
    s = F.schema("main", comment="c", tags=_TAGS, tables=[legacy])
    findings = [
        f
        for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))
        if f.code == "VGI138"
    ]
    assert findings and findings[0].severity is Sev.ERROR


def test_vgi406_classification_tags():
    # valid array on a table -> clean
    ok = F.table(
        "main", "t", comment="c", tags={**_TAGS, "vgi.classification_tags": '["geo","timeseries"]'}
    )
    s = F.schema("main", comment="c", tags=_TAGS, tables=[ok])
    assert "VGI406" not in _codes(F.catalog(s))
    # malformed (not a JSON array of strings) -> flagged
    bad = F.table(
        "main", "t", comment="c", tags={**_TAGS, "vgi.classification_tags": "geo, timeseries"}
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, tables=[bad])
    assert "VGI406" in _codes(F.catalog(s2))
    # present on the catalog -> not allowed
    cat = F.catalog(
        F.schema("main", comment="c", tags=_TAGS, tables=[F.table("main", "t", comment="c")])
    )
    cat.tags.raw["vgi.classification_tags"] = '["x"]'
    msgs = [
        f.message
        for f in run(select_rules(Config()), RuleContext(cat, Config()))
        if f.code == "VGI406"
    ]
    assert any("not allowed on the catalog" in m for m in msgs)


def test_vgi406_accepts_deprecated_category_tags_alias():
    # The old vgi.category_tags still resolves through the alias: malformed value
    # is still flagged by VGI406, and the deprecated key is nudged by VGI405.
    bad = F.table("main", "t", comment="c", tags={**_TAGS, "vgi.category_tags": "geo, timeseries"})
    s = F.schema("main", comment="c", tags=_TAGS, tables=[bad])
    codes = _codes(F.catalog(s))
    assert "VGI406" in codes  # validated through the canonical key
    assert "VGI405" in codes  # and flagged as deprecated


def test_vgi311_parameterless_table_function():
    # parameterless table function NOT exposed as a table -> flagged
    standalone = F.func("main", "metrics", "table", description="d")
    s = F.schema("main", comment="c", tags=_TAGS, functions=[standalone])
    assert "VGI311" in _codes(F.catalog(s))
    # ...but a parameterless table function with a backing table of the same name is fine
    backed_fn = F.func("main", "eruptions", "table", description="d")
    backing = F.table("main", "eruptions", comment="c", tags=_TAGS)
    s2 = F.schema("main", comment="c", tags=_TAGS, tables=[backing], functions=[backed_fn])
    assert "VGI311" not in _codes(F.catalog(s2))
    # a table function that TAKES arguments is legitimately a function
    parametric = F.func("main", "near", "table", description="d", parameters=["lat", "lng"])
    s3 = F.schema("main", comment="c", tags=_TAGS, functions=[parametric])
    assert "VGI311" not in _codes(F.catalog(s3))


def test_vgi103_listing_descriptions_detailed():
    # a schema with short doc_llm/doc_md is flagged (present but under 160)
    short = F.schema(
        "main",
        comment="c",
        tags={"vgi.doc_llm": "Zoo of animals.", "vgi.doc_md": "## Zoo\nAnimals."},
        tables=[F.table("main", "t", comment="c")],
    )
    codes = _codes(F.catalog(short))
    assert "VGI103" in codes
    # the detailed _TAGS schema is clean
    ok = F.schema("main", comment="c", tags=_TAGS, tables=[F.table("main", "t", comment="c")])
    assert "VGI103" not in _codes(F.catalog(ok))
    # catalog with a short doc (under 300) is flagged
    cat = F.catalog(
        ok, tags={**F.catalog(ok).tags.raw, "vgi.doc_llm": "Short.", "vgi.doc_md": "## x\nShort."}
    )
    assert "VGI103" in _codes(cat)


def test_vgi809_shared_column_suggests_relationship():
    # 'customer_id' in two tables, no FK, no 'customer' table -> INFO suggestion
    orders = F.table(
        "main",
        "orders",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "orders", "customer_id"), F.col("main", "orders", "total")],
    )
    invoices = F.table(
        "main",
        "invoices",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "invoices", "customer_id"), F.col("main", "invoices", "amount")],
    )
    s = F.schema("main", comment="c", tags=_TAGS, tables=[orders, invoices])
    found = [
        f
        for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))
        if f.code == "VGI809"
    ]
    assert found and any("customer_id" in f.message for f in found)
    # declaring a FK on customer_id silences it
    orders2 = F.table(
        "main",
        "orders",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "orders", "customer_id")],
        constraints=[
            F.constraint(
                "main",
                "orders",
                "FOREIGN KEY",
                columns=["customer_id"],
                referenced_table="invoices",
                referenced_columns=["customer_id"],
            )
        ],
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, tables=[orders2, invoices])
    assert "VGI809" not in [
        f.code for f in run(select_rules(Config()), RuleContext(F.catalog(s2), Config()))
    ]
    # a generic per-table 'id' (not prefixed) and unique columns are not flagged
    a = F.table(
        "main",
        "a",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "a", "id"), F.col("main", "a", "x")],
    )
    b = F.table(
        "main",
        "b",
        comment="c",
        tags=_TAGS,
        columns=[F.col("main", "b", "id"), F.col("main", "b", "y")],
    )
    s3 = F.schema("main", comment="c", tags=_TAGS, tables=[a, b])
    assert "VGI809" not in [
        f.code for f in run(select_rules(Config()), RuleContext(F.catalog(s3), Config()))
    ]


def test_vgi901_bind_error_is_error_runtime_is_warning():
    from vgi_lint_check.findings import Severity as Sev
    from vgi_lint_check.rules.execution import ExampleQueriesExecute

    t_bind = F.table(
        "main",
        "a",
        comment="c",
        tags=_TAGS,
        examples=[F.example(0, "x", "SELECT * FROM v.main.nope")],
    )
    t_run = F.table(
        "main", "b", comment="c", tags=_TAGS, examples=[F.example(0, "x", "SELECT * FROM v.main.b")]
    )

    class Con:
        def __init__(self, exc):
            self.exc = exc

        def execute(self, sql):
            raise self.exc

    def run_one(table, exc):
        cfg = Config(execute=True, execute_concurrency=1)
        ctx = RuleContext(
            F.catalog(F.schema("main", comment="c", tags=_TAGS, tables=[table])),
            cfg,
            connection=Con(exc),
        )
        ctx.severity = Sev.ERROR
        return list(ExampleQueriesExecute().check(ctx))

    bind = run_one(t_bind, RuntimeError("Catalog Error: Table with name nope does not exist"))
    assert len(bind) == 1 and bind[0].severity is Sev.ERROR and "does not bind" in bind[0].message
    runtime = run_one(t_run, RuntimeError("Out of Range Error: value too large"))
    assert (
        len(runtime) == 1 and runtime[0].severity is Sev.WARNING and "runtime" in runtime[0].message
    )


def test_vgi312_function_arguments_undocumented():
    # all args documented -> no finding
    ok = F.func(
        "main",
        "multiply",
        description="d",
        arguments=[
            F.arg("value", "DOUBLE", "the number to scale"),
            F.arg("factor", "DOUBLE", "the multiplier"),
        ],
    )
    s = F.schema("main", comment="c", tags=_TAGS, functions=[ok])
    assert "VGI312" not in _codes(F.catalog(s))

    # one undocumented arg -> finding listing it
    partial = F.func(
        "main",
        "multiply",
        description="d",
        arguments=[
            F.arg("value", "DOUBLE", "the number to scale"),
            F.arg("factor", "DOUBLE", None),
        ],
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, functions=[partial])
    found = [
        f
        for f in run(select_rules(Config()), RuleContext(F.catalog(s2), Config()))
        if f.code == "VGI312"
    ]
    assert found and "factor" in found[0].message and "value" not in found[0].message

    # const/varargs/any args are still flagged, and annotated in the message
    flagged = F.func(
        "main",
        "f",
        description="d",
        arguments=[
            F.arg("opts", "ANY", None, is_const=True, is_any_type=True),
        ],
    )
    s3 = F.schema("main", comment="c", tags=_TAGS, functions=[flagged])
    msg = [
        f.message
        for f in run(select_rules(Config()), RuleContext(F.catalog(s3), Config()))
        if f.code == "VGI312"
    ][0]
    assert "opts" in msg and "const" in msg and "any-type" in msg


def test_vgi312_silent_without_argument_data():
    # older vgi extension: no arguments populated -> rule emits nothing (no crash)
    no_args = F.func("main", "multiply", description="d", parameters=["value", "factor"])
    s = F.schema("main", comment="c", tags=_TAGS, functions=[no_args])
    assert "VGI312" not in _codes(F.catalog(s))


def test_vgi313_argument_description_states_type():
    def codes_for(*args):
        s = F.schema(
            "main",
            comment="c",
            tags=_TAGS,
            functions=[F.func("main", "f", description="d", arguments=list(args))],
        )
        return [
            f.code
            for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))
            if f.code == "VGI313"
        ]

    # restates an unambiguous type -> flagged
    assert codes_for(F.arg("unit", "VARCHAR", "the VARCHAR unit name")) == ["VGI313"]
    # restates the argument's OWN (ambiguous) type -> flagged
    assert codes_for(F.arg("value", "DOUBLE", "the double value to convert")) == ["VGI313"]
    # clean description -> not flagged
    assert codes_for(F.arg("unit", "VARCHAR", "the unit to convert from, e.g. 'meter'")) == []
    # ambiguous English use of a type word that is NOT the arg's type -> not flagged
    assert codes_for(F.arg("factor", "BIGINT", "double the input when true")) == []
    # a type token inside a larger word ('characters') is not a false match
    assert codes_for(F.arg("s", "VARCHAR", "number of characters to read")) == []
    # 'decimal'/'numeric'/'interval' are common English words: not flagged unless
    # they are the argument's OWN declared type
    assert codes_for(F.arg("lat", "DOUBLE", "Latitude in decimal degrees (WGS84).")) == []
    assert codes_for(F.arg("gap", "BIGINT", "the interval between samples")) == []
    assert codes_for(F.arg("amount", "DECIMAL", "the decimal amount to add")) == ["VGI313"]


def _vgi126_flagged_kinds(cat):
    findings = run(select_rules(Config()), RuleContext(cat, Config()))
    return {f.object_id.kind for f in findings if f.code == "VGI126"}


def test_vgi126_not_required_on_functions_or_macros():
    # regression: VGI126 (keywords) is scoped to catalog/schema/table/view and must
    # not leak onto functions/macros, which iter_functions() also yields.
    fn = F.func("main", "sf", description="d")  # scalar function, no keywords
    mac = F.func("main", "mm", ftype="macro", description="d")  # macro, no keywords
    s = F.schema("main", comment="c", tags=_TAGS, functions=[fn, mac])
    flagged = _vgi126_flagged_kinds(F.catalog(s))
    assert ObjectKind.SCALAR_FUNCTION not in flagged
    assert ObjectKind.MACRO not in flagged


def test_vgi126_still_required_on_tables():
    # guard against over-suppression: a table without keywords is still flagged.
    t = F.table("main", "t", comment="c")  # table, no keywords
    s = F.schema("main", comment="c", tags=_TAGS, tables=[t])
    assert ObjectKind.TABLE in _vgi126_flagged_kinds(F.catalog(s))


def test_vgi314_function_redocuments_arguments():
    def codes_for(doc_md, *args):
        fn = F.func(
            "main",
            "candles",
            description="Historical OHLC bars",
            arguments=list(args),
            tags={"vgi.doc_md": doc_md},
        )
        s = F.schema("main", comment="c", tags=_TAGS, functions=[fn])
        return {f.code for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))}

    period = F.arg("period", "VARCHAR", "Candle width, e.g. '5m'. Defaults to '5m'.")
    start = F.arg("start_time", "TIMESTAMP", "Start of the range, interpreted as UTC.")

    # doc_md re-documents the params as a bullet list -> flagged
    param_list = (
        "Historical OHLC bars.\n\n"
        "- **period** — count + unit, e.g. `'5m'`. Default `'5m'`.\n"
        "- **start_time** — UTC timestamp; defaults to midnight today.\n"
    )
    assert "VGI314" in codes_for(param_list, period, start)

    # a doc that merely mentions one arg in prose (no param list) -> not flagged
    prose = "Historical OHLC bars for symbols; `period` controls the bar width."
    assert "VGI314" not in codes_for(prose, period, start)

    # verbatim copy of an argument's whole doc -> flagged
    verbatim = "Historical OHLC bars. Start of the range, interpreted as UTC."
    assert "VGI314" in codes_for(verbatim, period, start)


def test_vgi404_unknown_vgi_namespace_tag():
    def run404(tags):
        s = F.schema(
            "main", comment="c", tags={**_TAGS, **tags}, tables=[F.table("main", "t", comment="c")]
        )
        return [
            f
            for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))
            if f.code == "VGI404"
        ]

    # a typo'd reserved-namespace key is flagged with a did-you-mean
    found = run404({"vgi.keyword": "x"})
    assert found and "vgi.keywords" in found[0].hint
    # an unrecognized vgi.* key with no near match is still flagged
    assert run404({"vgi.frobnicate": "x"})
    # non-vgi user tags are NOT flagged (extensibility; VGI403 covers opt-in)
    assert run404({"team": "data-eng", "provider": "acme"}) == []
    # a valid reserved key is fine
    assert run404({"vgi.keywords": '["a"]'}) == []


def test_vgi908_slow_executable_example_identifies_it():
    import time as _time

    from vgi_lint_check.findings import Severity as Sev
    from vgi_lint_check.rules.execution import ExecutableExampleSlow

    fast = F.exec_example(0, "fast", [("s", "SELECT 1")], name="quick")
    slow = F.exec_example(1, "slow", [("s", "SELECT sleep(1)")], name="heavy-scan")
    fn = F.func("main", "f", description="d", executable_examples=[fast, slow])
    s = F.schema("main", comment="c", tags=_TAGS, functions=[fn])

    class Con:
        def execute(self, sql):
            if "sleep" in sql:
                _time.sleep(0.2)
            return self

        def fetchall(self):
            return []

    cfg = Config(execute=True)
    cfg.slow_example_seconds = 0.1  # 100ms threshold
    ctx = RuleContext(F.catalog(s), cfg, connection=Con())
    ctx.severity = Sev.WARNING
    out = list(ExecutableExampleSlow().check(ctx))
    assert len(out) == 1  # only the slow one
    assert "heavy-scan" in out[0].message  # the message names the slow example
    assert "quick" not in out[0].message


def test_vgi407_agent_test_tasks_valid():
    # VGI407 surfaces the catalog's pre-decoded agent_test_tasks_parse_error
    # (the loader sets it; the decoder shape is tested in test_loader).
    s = F.schema("main", comment="c", tags=_TAGS, tables=[F.table("main", "t", comment="c")])
    clean = F.catalog(s)
    assert "VGI407" not in _codes(clean)
    bad = F.catalog(s)
    bad.agent_test_tasks_parse_error = "entry #0 has no 'name'"
    assert "VGI407" in _codes(bad)


def test_vgi152_agent_test_tasks_present_nudge():
    from vgi_lint_check.model import AgentTask

    s = F.schema("main", comment="c", tags=_TAGS, tables=[F.table("main", "t", comment="c")])
    # No agent_test_tasks declared -> INFO nudge to add a suite.
    assert "VGI152" in _codes(F.catalog(s))
    # A declared suite satisfies it.
    cat = F.catalog(s)
    cat.agent_test_tasks = [AgentTask(name="t", prompt="p")]
    assert "VGI152" not in _codes(cat)


def test_vgi316_single_array_arg_suggests_table():
    def codes_for(*args):
        fn = F.func("main", "solve", description="d", arguments=list(args))
        s = F.schema("main", comment="c", tags=_TAGS, functions=[fn])
        return {f.code for f in run(select_rules(Config()), RuleContext(F.catalog(s), Config()))}

    # a single 2-D array argument (a matrix / list of rows) -> fires
    assert "VGI316" in codes_for(F.arg("distance", "BIGINT[][]", "the distance matrix"))
    # a list-of-struct (typed rows) -> fires
    assert "VGI316" in codes_for(F.arg("rows", "STRUCT(a INTEGER, b INTEGER)[]", "rows"))
    # two array args -> DOES NOT fire (DuckDB takes only one table input)
    assert "VGI316" not in codes_for(
        F.arg("a", "BIGINT[][]", "matrix a"), F.arg("b", "BIGINT[][]", "matrix b")
    )
    # a plain 1-D array (a vector) -> not flagged
    assert "VGI316" not in codes_for(F.arg("v", "BIGINT[]", "a vector of weights"))


def test_vgi145_view_wraps_table_function():
    # A view whose body is a bare SELECT over a parameterless table function is
    # pure indirection -> flagged as an error (should be a scan-backed table).
    tf = F.func("main", "exchanges", "table", description="d", tags=_TAGS)
    v = F.view(
        "main",
        "exchanges_v",
        comment="c",
        tags=_TAGS,
        sql_definition="CREATE VIEW exchanges_v AS SELECT code FROM cal.main.exchanges();",
    )
    s = F.schema("main", comment="c", tags=_TAGS, views=[v], functions=[tf])
    assert "VGI145" in _codes(F.catalog(s))

    # SELECT * over the same parameterless table function -> also flagged.
    v_star = F.view(
        "main",
        "exchanges_v",
        comment="c",
        tags=_TAGS,
        sql_definition="SELECT * FROM main.exchanges()",
    )
    s_star = F.schema("main", comment="c", tags=_TAGS, views=[v_star], functions=[tf])
    assert "VGI145" in _codes(F.catalog(s_star))


def test_vgi145_clean_cases():
    # A view that filters/transforms is doing real work -> not flagged.
    tf = F.func("main", "exchanges", "table", description="d", tags=_TAGS)
    filtered = F.view(
        "main",
        "us_exchanges",
        comment="c",
        tags=_TAGS,
        sql_definition="SELECT code FROM main.exchanges() WHERE country = 'US'",
    )
    s = F.schema("main", comment="c", tags=_TAGS, views=[filtered], functions=[tf])
    assert "VGI145" not in _codes(F.catalog(s))

    # A view over a table function that TAKES parameters is not the anti-pattern.
    param_tf = F.func("main", "history", "table", description="d", tags=_TAGS, parameters=["since"])
    over_param = F.view(
        "main",
        "history_v",
        comment="c",
        tags=_TAGS,
        sql_definition="SELECT * FROM main.history()",
    )
    s2 = F.schema("main", comment="c", tags=_TAGS, views=[over_param], functions=[param_tf])
    assert "VGI145" not in _codes(F.catalog(s2))

    # A view over a real base relation (no matching parameterless table fn) is fine.
    plain = F.view(
        "main",
        "plain_v",
        comment="c",
        tags=_TAGS,
        sql_definition="SELECT * FROM main.some_table()",
    )
    s3 = F.schema("main", comment="c", tags=_TAGS, views=[plain])
    assert "VGI145" not in _codes(F.catalog(s3))
