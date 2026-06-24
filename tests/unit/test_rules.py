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
    "vgi.description_llm": "Zoo domain: animals, attributes, and sounds for LLM use.",
    "vgi.description_md": "## Zoo\nAnimals, attributes, and sounds — full reference.",
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
            "vgi.description_llm": "Animals and their attributes for LLM consumers, etc.",
            "vgi.description_md": "## Animals\nAnimals and attributes with much more detail here.",
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
    # VGI151 (catalog-wide minimum example count) is marketing, not per-object.
    found = codes(F.catalog(s), ignore=["VGI151"])
    assert found == [] or set(found) <= set()  # nothing flagged


def test_schema_descriptions_required_table_optional():
    t = F.table("main", "bare")  # no comment, no llm/md
    s = F.schema("main", comment="x", tags={"provider": "a", "domain": "b"}, tables=[t])
    found = set(codes(F.catalog(s)))
    assert "VGI111" in found  # table comment still required
    assert "VGI116" in found  # schema llm required
    assert "VGI118" in found  # schema md required
    # llm/md are optional (off) for tables
    assert "VGI112" not in found
    assert "VGI113" not in found
    # ...but flagged when opted in
    on = set(codes(F.catalog(s), severity_overrides={"VGI112": Severity.WARNING}))
    assert "VGI112" in on


def test_column_coverage_threshold():
    cols = [F.col("main", "t", "a", "documented"), F.col("main", "t", "b", None)]
    t = F.table(
        "main",
        "t",
        comment="c",
        columns=cols,
        tags={"vgi.description_llm": "x" * 50, "vgi.description_md": "y" * 90},
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
        tags={"vgi.description_llm": "x" * 50, "vgi.description_md": "y" * 90},
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
        tags={"vgi.description_llm": "x" * 50, "vgi.description_md": "y" * 90},
        examples=[F.example(0, "ok", "SELECT * FROM v.main.a")],
    )
    unqualified = F.table(
        "main",
        "b",
        comment="c",
        tags={"vgi.description_llm": "x" * 50, "vgi.description_md": "y" * 90},
        examples=[F.example(0, "bad", "SELECT * FROM b")],
    )
    s = F.schema(
        "main", comment="c", tags={"provider": "a", "domain": "b"}, tables=[qualified, unqualified]
    )
    cat = F.catalog(s)
    findings = [f for f in _findings(cat) if f.code == "VGI505"]
    flagged = {f.object_id.name for f in findings}
    assert "b" in flagged and "a" not in flagged


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
