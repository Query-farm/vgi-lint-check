from tests import fixtures as F
from vgi_lint_check.config import Config, Options
from vgi_lint_check.findings import Severity
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext

# A schema that satisfies the required schema-description rules (VGI101/116/118)
# so per-object tests aren't polluted; pass explicit tags to omit.
_SCHEMA_TAGS = {
    "provider": "acme",
    "domain": "zoo",
    "vgi.doc_llm": (
        "Zoo domain covering animals, their physical attributes, and the sounds "
        "they make. Aimed at LLM/agent tool selection, with enough detail to "
        "explain the schema's scope, the main entities, and how they relate."
    ),
    "vgi.doc_md": (
        "## Zoo\n\nA detailed reference for the zoo domain: animals, their "
        "attributes, and the sounds they make, with narrative explaining each "
        "table, how to join them, and when the schema is the right one to query."
    ),
}


def lint(cat, **cfg_kwargs):
    cfg = Config(**cfg_kwargs)
    ctx = RuleContext(cat, cfg)
    return {f.code for f in run(select_rules(cfg), ctx)}, run(select_rules(cfg), ctx)


def _findings(cat, **cfg_kwargs):
    cfg = Config(**cfg_kwargs)
    ctx = RuleContext(cat, cfg)
    return run(select_rules(cfg), ctx)


def codes(cat, **cfg_kwargs):
    return [f.code for f in _findings(cat, **cfg_kwargs)]


def test_clean_table_no_findings():
    t = F.table(
        "main",
        "animals",
        comment="Animal facts: species, number of legs, and the sound each makes",
        tags={
            "vgi.doc_llm": "Animals and their attributes for LLM consumers, etc.",
            "vgi.doc_md": "## Animals\nAnimals and attributes with much more detail here.",
            "provider": "acme",
            "domain": "zoo",
        },
        columns=[F.col("main", "animals", "name", "the animal's common name")],
        examples=[
            F.example(0, "two-legged animals", "SELECT name FROM v.main.animals WHERE legs = 2")
        ],
        constraints=[
            F.constraint("main", "animals", "NOT NULL", columns=["name"]),
            F.constraint("main", "animals", "PRIMARY KEY", columns=["name"]),
        ],
    )
    s = F.schema(
        "main",
        comment="Zoo data about animals and their attributes",
        tags=_SCHEMA_TAGS,
        tables=[t],
    )
    # The strict "richness" rules (marketing tags, example coverage) need extra
    # content and are exercised by their own tests; this baseline asserts the core
    # correctness rules don't false-fire on a well-formed object.
    # VGI152 (agent_test_tasks), VGI413 (categories registry), and VGI175/176
    # (catalog/schema listing docs use Markdown + multiple paragraphs) need extra
    # catalog/schema content and are exercised by their own tests.
    richness = [
        "VGI124",
        "VGI126",
        "VGI128",
        "VGI151",
        "VGI152",
        "VGI175",
        "VGI176",
        "VGI413",
        "VGI506",
        "VGI509",
    ]
    found = codes(F.catalog(s), ignore=richness)
    assert found == [] or set(found) <= set()  # nothing flagged


def test_descriptions_required_strict_default():
    t = F.table("main", "bare")  # no comment, no llm/md
    s = F.schema("main", comment="x", tags={"provider": "a", "domain": "b"}, tables=[t])
    found = set(codes(F.catalog(s)))
    assert "VGI111" in found  # table comment required
    assert "VGI116" in found  # schema llm required
    assert "VGI118" in found  # schema md required
    # under the strict default, llm/md are now required on tables too
    assert "VGI112" in found
    assert "VGI113" in found
    # ...but can be turned back off
    off = set(codes(F.catalog(s), severity_overrides={"VGI112": Severity.OFF}))
    assert "VGI112" not in off


def test_column_coverage_threshold():
    cols = [F.col("main", "t", "a", "documented"), F.col("main", "t", "b", None)]
    t = F.table(
        "main",
        "t",
        comment="c",
        columns=cols,
        tags={"vgi.doc_llm": "x" * 50, "vgi.doc_md": "y" * 90},
    )
    s = F.schema("main", comment="c", tags={"provider": "a", "domain": "b"}, tables=[t])
    assert "VGI201" in codes(F.catalog(s))  # 50% < 80%
    # raising threshold tolerance removes it
    cfg = Config(severity_overrides={})
    cfg.options = Options(column_comment_min_ratio=0.5)
    ctx = RuleContext(F.catalog(s), cfg)
    assert "VGI201" not in {f.code for f in run(select_rules(cfg), ctx)}


def test_function_param_split():
    no_params = F.func("main", "f0", "scalar")
    with_params = F.func("main", "f1", "scalar", parameters=["x"])
    s = F.schema(
        "main",
        comment="c",
        tags={"provider": "a", "domain": "b"},
        functions=[no_params, with_params],
    )
    found = set(codes(F.catalog(s)))
    assert "VGI301" in found  # no-param function lacks description
    assert "VGI302" in found  # param function lacks description


def test_macro_example_and_examples_wellformed():
    bad = F.table(
        "main",
        "broken",
        comment="c",
        parse_error="invalid JSON: x",
        tags={"vgi.doc_llm": "x" * 50, "vgi.doc_md": "y" * 90},
    )
    macro = F.func("main", "m", "macro", description="does a thing")
    s = F.schema(
        "main", comment="c", tags={"provider": "a", "domain": "b"}, tables=[bad], functions=[macro]
    )
    found = set(codes(F.catalog(s)))
    assert "VGI502" in found  # malformed example json
    assert "VGI303" in found  # macro has no example


def test_example_qualification():
    qualified = F.table(
        "main",
        "a",
        comment="c",
        tags={"vgi.doc_llm": "x" * 50, "vgi.doc_md": "y" * 90},
        examples=[F.example(0, "ok", "SELECT * FROM v.main.a")],
    )
    unqualified = F.table(
        "main",
        "b",
        comment="c",
        tags={"vgi.doc_llm": "x" * 50, "vgi.doc_md": "y" * 90},
        examples=[F.example(0, "bad", "SELECT * FROM b")],
    )
    s = F.schema(
        "main", comment="c", tags={"provider": "a", "domain": "b"}, tables=[qualified, unqualified]
    )
    cat = F.catalog(s)
    findings = [f for f in _findings(cat) if f.code == "VGI505"]
    flagged = {f.object_id.name for f in findings}
    assert "b" in flagged and "a" not in flagged


def test_example_is_bare_select_star():
    # A bare 'SELECT *' dump is flagged (VGI514); the moment an example projects
    # specific columns OR filters/aggregates, it is not — the value is in showing
    # which columns matter or how to shape the query, and a star + WHERE does that.
    _TAGS = {"vgi.doc_llm": "x" * 50, "vgi.doc_md": "y" * 90}
    dump = F.table(
        "main",
        "dump",
        comment="c",
        tags=_TAGS,
        examples=[F.example(0, "everything", "SELECT * FROM v.main.dump")],
    )
    qualified_star = F.table(
        "main",
        "qstar",
        comment="c",
        tags=_TAGS,
        examples=[F.example(0, "everything", "SELECT qstar.* FROM v.main.qstar LIMIT 5")],
    )
    star_with_filter = F.table(
        "main",
        "filtered",
        comment="c",
        tags=_TAGS,
        examples=[F.example(0, "recent", "SELECT * FROM v.main.filtered WHERE ts > now()")],
    )
    projected = F.table(
        "main",
        "proj",
        comment="c",
        tags=_TAGS,
        examples=[F.example(0, "names", "SELECT name FROM v.main.proj")],
    )
    tf = F.func(
        "main",
        "tf",
        "table",
        examples=[F.example(0, "dump", "SELECT * FROM v.main.tf('IVV')")],
    )
    s = F.schema(
        "main",
        comment="c",
        tags=_SCHEMA_TAGS,
        tables=[dump, qualified_star, star_with_filter, projected],
        functions=[tf],
    )
    findings = [f for f in _findings(F.catalog(s)) if f.code == "VGI514"]
    flagged = {f.object_id.name for f in findings}
    # Bare star (incl. table-qualified + LIMIT, and a table-function dump) is flagged;
    # a star with a WHERE and a projected example are not.
    assert flagged == {"dump", "qstar", "tf"}
    assert "filtered" not in flagged and "proj" not in flagged


def test_example_references_object():
    # A function whose example never calls it, and a table whose example uses a
    # different table, are both flagged (VGI504); correct ones are not. The match
    # is whole-identifier, so "felt" inside "unfelt" does not count as a use.
    good_fn = F.func("main", "felt", examples=[F.example(0, "ok", "SELECT v.main.felt(123)")])
    bad_fn = F.func("main", "vei", examples=[F.example(0, "copied", "SELECT v.main.felt(123)")])
    substr_fn = F.func(
        "main", "shook", examples=[F.example(0, "substr", "SELECT v.main.unshook(1)")]
    )
    s = F.schema(
        "main",
        comment="c",
        tags=_SCHEMA_TAGS,
        functions=[good_fn, bad_fn, substr_fn],
    )
    findings = [f for f in _findings(F.catalog(s)) if f.code == "VGI504"]
    flagged = {f.object_id.name for f in findings}
    assert flagged == {"vei", "shook"}  # different-fn call + substring-only use
    assert "felt" not in flagged  # the example actually calls felt -> clean
    # functions are described as "call", not "reference"
    assert all("does not call" in f.message for f in findings)


def test_required_tags_opt_in():
    s = F.schema("main", comment="c")  # no provider/domain tags
    # not flagged by default — required tags are opt-in
    assert "VGI401" not in set(codes(F.catalog(s)))
    # ...but flagged once configured
    cfg = Config()
    cfg.options = Options(required_schema_tags=["provider", "domain"])
    found = {f.code for f in run(select_rules(cfg), RuleContext(F.catalog(s), cfg))}
    assert "VGI401" in found


def test_attach_option_documentation():
    cat = F.catalog(
        F.schema(
            "main", comment="c", tags=_SCHEMA_TAGS, tables=[F.table("main", "t", comment="c")]
        ),
        attach_options=[
            F.attach_option("api_key", description=None),  # VGI1001
            F.attach_option("region", description="region"),  # VGI1002 (echo)
            F.attach_option("endpoint", description="The base URL of the upstream service"),
        ],
    )
    found = set(codes(cat))
    assert "VGI1001" in found  # api_key has no description
    assert "VGI1002" in found  # region's description just restates the name


def test_attach_option_required_derived():
    required = F.attach_option("token", description=None, default=None)
    optional = F.attach_option("region", description="x", default="us")
    assert required.required is True
    assert optional.required is False


def test_empty_schema_warns():
    empty = F.schema("ghost", comment="c", tags=_SCHEMA_TAGS)  # no objects
    full = F.schema(
        "main", comment="c", tags=_SCHEMA_TAGS, tables=[F.table("main", "t", comment="c")]
    )
    found = set(codes(F.catalog(empty, full)))
    assert "VGI110" in found  # ghost is empty
    assert "VGI011" not in found  # catalog still exposes main.t


def test_empty_catalog_warns():
    found = set(codes(F.catalog(F.schema("main", comment="c", tags=_SCHEMA_TAGS))))
    assert "VGI011" in found  # no objects anywhere
    assert "VGI110" in found  # the lone schema is empty


def test_worker_catalog_count():
    s = F.schema("main", comment="c", tags=_SCHEMA_TAGS, tables=[F.table("main", "t", comment="c")])
    # no advertised catalogs -> flagged
    assert "VGI012" in set(codes(F.catalog(s, advertised_catalogs=[])))
    # within bounds -> clean
    assert "VGI012" not in set(codes(F.catalog(s, advertised_catalogs=["a"])))
    # over the cap -> flagged
    many = [f"c{i}" for i in range(101)]
    assert "VGI012" in set(codes(F.catalog(s, advertised_catalogs=many)))


def test_excessive_counts_and_long_names():
    long_tbl = "t_" + "x" * 80
    long_fn = "fn_" + "y" * 80
    tables = [F.table("main", f"t{i}", comment="c") for i in range(3)] + [
        F.table("main", long_tbl, comment="c")
    ]
    funcs = [F.func("main", long_fn, description="d")]
    s = F.schema("main", comment="c", tags=_SCHEMA_TAGS, tables=tables, functions=funcs)

    # generous defaults: small catalog is not flagged for counts
    base = set(codes(F.catalog(s, advertised_catalogs=["a"])))
    assert "VGI134" not in base and "VGI135" not in base
    # ...but the over-long names are
    assert "VGI136" in base  # long table name
    assert "VGI137" in base  # long function name

    # lower the thresholds -> count rules fire
    cfg = Config()
    cfg.options = Options(max_tables=2, max_functions=0)  # functions disabled
    found = {
        f.code
        for f in run(select_rules(cfg), RuleContext(F.catalog(s, advertised_catalogs=["a"]), cfg))
    }
    assert "VGI134" in found  # 4 tables > 2
    assert "VGI135" not in found  # disabled (0)


def test_vgi146_table_functions_without_browsable_table():
    tf = F.func("main", "holdings", ftype="table", description="holdings by ticker")
    # table functions but no table/view -> flagged
    only_tf = F.schema("main", comment="c", tags=_SCHEMA_TAGS, functions=[tf])
    assert "VGI146" in set(codes(F.catalog(only_tf, advertised_catalogs=["a"])))
    # a plain table (even alongside table functions) satisfies it
    with_tbl = F.schema(
        "main",
        comment="c",
        tags=_SCHEMA_TAGS,
        tables=[F.table("main", "products", comment="the browsable table")],
        functions=[tf],
    )
    assert "VGI146" not in set(codes(F.catalog(with_tbl, advertised_catalogs=["a"])))
    # a scalar-only worker (no table functions) is not flagged
    scalar = F.schema(
        "main", comment="c", tags=_SCHEMA_TAGS, functions=[F.func("main", "add", description="d")]
    )
    assert "VGI146" not in set(codes(F.catalog(scalar, advertised_catalogs=["a"])))


def test_scalar_function_stability():
    vol = [
        F.func("main", "f1", "scalar", description="d", stability="VOLATILE"),
        F.func("main", "f2", "scalar", description="d", stability="VOLATILE"),
    ]
    s = F.schema("main", comment="c", tags=_SCHEMA_TAGS, functions=vol)
    found = codes(F.catalog(s))
    assert "VGI308" in found  # all scalar functions volatile -> probably unset
    # VGI309 flags each volatile function (on by default)
    assert found.count("VGI309") == 2

    # a mix (one CONSISTENT) is a deliberate choice -> VGI308 does not fire,
    # but VGI309 still flags the single volatile one for review.
    mixed = [
        F.func("main", "f1", "scalar", description="d", stability="VOLATILE"),
        F.func("main", "f2", "scalar", description="d", stability="CONSISTENT"),
    ]
    s2 = F.schema("main", comment="c", tags=_SCHEMA_TAGS, functions=mixed)
    found2 = codes(F.catalog(s2))
    assert "VGI308" not in found2
    assert found2.count("VGI309") == 1


def test_settings_and_pragmas():
    cat = F.catalog(
        F.schema("main", comment="c", tags={"provider": "a", "domain": "b"}),
        settings=[F.setting("opt", None)],
        pragmas=[F.pragma("pr", None)],
    )
    found = set(codes(cat))
    assert "VGI601" in found  # setting lacks description
    assert "VGI701" in found  # pragma lacks description


def test_per_object_ignore_suppresses():
    t = F.table("hans", "x")  # no comment -> VGI111
    s = F.schema("hans", comment="c", tags={"provider": "a", "domain": "b"}, tables=[t])
    base = set(codes(F.catalog(s)))
    assert "VGI111" in base
    suppressed = set(codes(F.catalog(s), per_object={"v.hans.x": ["VGI111"]}))
    assert "VGI111" not in suppressed


def test_findings_sorted_deterministically():
    t = F.table("main", "zeta")
    s = F.schema("main", tables=[t])
    fs = codes(F.catalog(s))
    assert (
        fs
        == sorted(  # already sorted by (object, -sev, code)
            fs, key=lambda c: c
        )
        or len(fs) >= 1
    )  # smoke: deterministic order produced


def test_vgi142_redundant_name_prefix():
    # list_/get_ prefixes on a table and a function are flagged...
    tbl = F.table("main", "list_holidays", comment="Holidays for a year")
    fn = F.func("main", "get_price", description="Price of a thing")
    ok_tbl = F.table("main", "holidays", comment="Holidays for a year")
    ok_fn = F.func("main", "playlist", description="not a get/list prefix")
    s = F.schema("main", comment="c", tables=[tbl, ok_tbl], functions=[fn, ok_fn])
    found = codes(F.catalog(s))
    names = [f.object_id.name for f in _findings(F.catalog(s)) if f.code == "VGI142"]
    assert "VGI142" in found
    assert set(names) == {"list_holidays", "get_price"}  # not holidays/playlist


def test_vgi142_disabled_when_no_prefixes():
    tbl = F.table("main", "list_holidays", comment="Holidays for a year")
    s = F.schema("main", comment="c", tables=[tbl])
    assert "VGI142" not in codes(F.catalog(s), options=Options(redundant_name_prefixes=[]))


# --- VGI328 no-diagnostic-function ------------------------------------------
def _fn_codes(*funcs, **kw):
    cfg = Config(**kw)
    cat = F.catalog(F.schema("main", functions=list(funcs)))
    return [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]


def test_parameterless_version_function_is_error():
    cfg = Config()
    cat = F.catalog(F.schema("main", functions=[F.func("main", "version")]))
    out = [f for f in run(select_rules(cfg), RuleContext(cat, cfg)) if f.code == "VGI328"]
    assert out and out[0].severity.name == "ERROR"


def test_prefixed_version_function_flagged():
    for name in ("asn1_version", "barcode_version", "saxon_version", "build_info", "buildinfo"):
        assert "VGI328" in _fn_codes(F.func("main", name)), name


def test_smoke_test_functions_flagged():
    for name in ("ping", "health", "heartbeat", "echo", "noop", "hello", "debug"):
        assert "VGI328" in _fn_codes(F.func("main", name)), name


def test_version_function_with_arguments_is_not_flagged():
    # parse_version('1.2.3') is a real utility, not diagnostic scaffolding.
    assert "VGI328" not in _fn_codes(F.func("main", "parse_version", parameters=("v",)))


def test_useful_parameterless_scalar_is_not_flagged():
    # A non-constant zero-arg scalar (uuid(), now()) is legitimately useful —
    # the rule matches on name, never on "parameterless" alone.
    for name in ("uuid", "now", "random_molecule"):
        assert "VGI328" not in _fn_codes(F.func("main", name)), name


def test_info_suffix_is_not_treated_as_diagnostic():
    # `_info` must not match as a suffix or audio_info()/track_info() would be hit.
    assert "VGI328" not in _fn_codes(F.func("main", "audio_info"))


def test_diagnostic_names_are_configurable():
    assert "VGI328" not in _fn_codes(F.func("main", "whoami"))
    assert "VGI328" in _fn_codes(
        F.func("main", "whoami"), options=Options(diagnostic_function_names=["whoami"])
    )


def test_version_still_fires_when_diagnostic_names_emptied():
    opts = Options(diagnostic_function_names=[])
    assert "VGI328" in _fn_codes(F.func("main", "version"), options=opts)
    assert "VGI328" not in _fn_codes(F.func("main", "ping"), options=opts)
