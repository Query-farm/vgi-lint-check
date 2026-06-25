from vgi_lint_check.loader import build_catalog
from vgi_lint_check.model import ObjectKind
from vgi_lint_check.snapshot import Snapshot


def make_snapshot():
    return Snapshot(
        schemas=[
            {
                "database_name": "v",
                "schema_name": "main",
                "comment": "Main schema",
                "tags": {"provider": "acme", "domain": "demo"},
            },
            {"database_name": "other", "schema_name": "x", "comment": None, "tags": {}},
        ],
        tables=[
            {
                "database_name": "v",
                "schema_name": "main",
                "table_name": "animals",
                "comment": "Animals",
                "column_count": 2,
                "internal": False,
                "tags": {
                    "vgi.doc_llm": "Animals for LLMs",
                    "vgi.example_queries": '[{"description":"all","sql":"SELECT * FROM animals"}]',
                },
            },
            {
                "database_name": "v",
                "schema_name": "main",
                "table_name": "broken",
                "comment": None,
                "column_count": 1,
                "internal": True,
                "tags": {"vgi.example_queries": "{not json"},
            },
        ],
        columns=[
            {
                "database_name": "v",
                "schema_name": "main",
                "table_name": "animals",
                "column_name": "name",
                "data_type": "VARCHAR",
                "comment": "the name",
            },
            {
                "database_name": "v",
                "schema_name": "main",
                "table_name": "animals",
                "column_name": "legs",
                "data_type": "INTEGER",
                "comment": None,
            },
        ],
        views=[
            {
                "database_name": "v",
                "schema_name": "main",
                "view_name": "av",
                "comment": "a view",
                "internal": False,
                "tags": {},
                "sql": "SELECT 1",
            },
        ],
        functions=[
            {
                "database_name": "v",
                "schema_name": "main",
                "function_name": "animals",
                "function_type": "table",
                "description": "scan animals",
                "internal": False,
                "tags": {},
                "parameters": [],
                "parameter_types": [],
                "examples": [],
            },
            {
                "database_name": "v",
                "schema_name": "main",
                "function_name": "loud",
                "function_type": "macro",
                "description": None,
                "internal": False,
                "tags": {},
                "parameters": ["x"],
                "parameter_types": ["VARCHAR"],
                "examples": [],
                "macro_definition": "upper(x)",
            },
        ],
        settings=[],
    )


def test_scoping_by_alias_keeps_internal():
    # VGI marks worker objects internal=true, so they must be KEPT; only the
    # alias filter excludes other catalogs.
    cat = build_catalog(make_snapshot(), "v", "loc")
    assert {s.name for s in cat.schemas} == {"main"}  # 'other' excluded by alias
    tables = list(cat.iter_tables())
    assert {t.name for t in tables} == {"animals", "broken"}


def test_tags_and_examples():
    cat = build_catalog(make_snapshot(), "v", "loc")
    animals = next(t for t in cat.iter_tables() if t.name == "animals")
    assert animals.description_llm == "Animals for LLMs"
    assert animals.tags.plain == {}  # only reserved keys present
    assert len(animals.examples) == 1
    assert animals.examples[0].sql == "SELECT * FROM animals"
    assert animals.examples_parse_error is None

    broken = next(t for t in cat.iter_tables() if t.name == "broken")
    assert broken.examples == []
    assert broken.examples_parse_error is not None


def test_columns_attached():
    cat = build_catalog(make_snapshot(), "v", "loc")
    animals = next(t for t in cat.iter_tables() if t.name == "animals")
    assert [c.name for c in animals.columns] == ["name", "legs"]
    assert animals.columns[0].documented and not animals.columns[1].documented


def test_table_function_correlation():
    cat = build_catalog(make_snapshot(), "v", "loc")
    animals = next(t for t in cat.iter_tables() if t.name == "animals")
    assert animals.backing_function is not None
    assert animals.backing_function.function_type == "table"
    # table-functions are excluded from iter_functions()
    fns = list(cat.iter_functions())
    assert {f.name for f in fns} == {"loud"}
    assert next(iter(fns)).is_macro


def test_native_examples_merged():
    # The VGI extension surfaces a function's Meta.examples into the native
    # duckdb_functions().examples column (VARCHAR[]). The vgi.example_queries tag
    # is an independent carrier; the loader merges BOTH (deduped by SQL) so every
    # example the worker ships is seen by static rules and run by --execute.
    snap = Snapshot(
        schemas=[{"database_name": "v", "schema_name": "main", "comment": None, "tags": {}}],
        functions=[
            # native examples only, no tag
            {
                "database_name": "v",
                "schema_name": "main",
                "function_name": "r2_score",
                "function_type": "aggregate",
                "description": "R^2",
                "internal": True,
                "tags": {},
                "parameters": [],
                "parameter_types": [],
                "examples": ["SELECT main.r2_score(a, b) FROM t"],
            },
            # tag-encoded examples take precedence over the native column
            {
                "database_name": "v",
                "schema_name": "main",
                "function_name": "tagged",
                "function_type": "scalar",
                "description": "d",
                "internal": True,
                "tags": {"vgi.example_queries": '[{"description":"x","sql":"SELECT tag"}]'},
                "parameters": [],
                "parameter_types": [],
                "examples": ["SELECT native", "SELECT tag"],  # 2nd dups the tag query
            },
        ],
    )
    cat = build_catalog(snap, "v", "loc")
    fns = {f.name: f for f in cat.iter_functions()}
    assert [e.sql for e in fns["r2_score"].examples] == ["SELECT main.r2_score(a, b) FROM t"]
    assert fns["r2_score"].examples_parse_error is None
    # tag first (it carries a description), then the distinct native query; the
    # native entry that duplicates the tag SQL is dropped, and indexes are 0..n.
    assert [e.sql for e in fns["tagged"].examples] == ["SELECT tag", "SELECT native"]
    assert fns["tagged"].examples[0].description == "x"
    assert [e.index for e in fns["tagged"].examples] == [0, 1]


def test_settings_and_pragmas_diff_scoped():
    cat = build_catalog(
        make_snapshot(),
        "v",
        "loc",
        setting_rows=[
            {
                "name": "v_opt",
                "description": "an option",
                "input_type": "VARCHAR",
                "scope": "GLOBAL",
                "value": "x",
            }
        ],
        pragma_rows=[{"function_name": "v_pragma", "description": None, "tags": {}}],
    )
    assert [s.name for s in cat.settings] == ["v_opt"]
    assert cat.settings[0].id.kind is ObjectKind.SETTING
    assert [p.name for p in cat.pragmas] == ["v_pragma"]


def test_function_arguments_joined_and_version_skew():

    snap = Snapshot(
        schemas=[{"database_name": "v", "schema_name": "main", "comment": None, "tags": {}}],
        functions=[
            {
                "database_name": "v",
                "schema_name": "main",
                "function_name": "multiply",
                "function_type": "scalar",
                "description": "scale a value",
                "internal": True,
                "tags": {},
                "parameters": ["value", "factor"],
                "parameter_types": ["DOUBLE", "DOUBLE"],
                "examples": [],
            },
        ],
    )
    arg_rows = [
        {
            "schema_name": "main",
            "function_name": "multiply",
            "arg_name": "value",
            "arg_type": "DOUBLE",
            "arg_description": "the number",
            "is_const": False,
        },
        {
            "schema_name": "main",
            "function_name": "multiply",
            "arg_name": "factor",
            "arg_type": "DOUBLE",
            "arg_description": None,
            "is_const": True,
        },
    ]
    cat = build_catalog(snap, "v", "loc", argument_rows=arg_rows)
    fn = next(f for f in cat.iter_functions() if f.name == "multiply")
    assert [a.name for a in fn.arguments] == ["value", "factor"]
    assert fn.arguments[0].description == "the number"
    assert fn.arguments[1].description is None and fn.arguments[1].is_const is True

    # with no rows (older extension), arguments is empty and nothing breaks
    cat2 = build_catalog(snap, "v", "loc", argument_rows=None)
    assert next(f for f in cat2.iter_functions() if f.name == "multiply").arguments == []


class _RaisingCon:
    def execute(self, sql, params=None):
        raise RuntimeError(
            "Catalog Error: Table Function with name vgi_function_arguments does not exist"
        )


def test_fetch_function_arguments_version_skew_returns_empty():
    from vgi_lint_check.snapshot import fetch_function_arguments

    # an older vgi extension lacking the table function must not crash
    assert fetch_function_arguments(_RaisingCon(), "v") == []


def test_agent_test_tasks_decoded_on_catalog():
    import json as _json

    tasks = _json.dumps(
        [
            {"name": "t1", "prompt": "find top 5", "reference_sql": "SELECT 1", "unordered": True},
            {
                "name": "t2",
                "prompt": "count",
                "reference_sql": [
                    {"description": "setup", "sql": "SET x=1"},
                    {"description": "q", "sql": "SELECT count(*) FROM t"},
                ],
                "check_sql": "SELECT true",
                "success_criteria": "one number",
            },
        ]
    )
    snap = Snapshot(
        databases=[{"database_name": "v", "comment": "c", "tags": {"vgi.agent_test_tasks": tasks}}],
        schemas=[{"database_name": "v", "schema_name": "main", "comment": None, "tags": {}}],
    )
    cat = build_catalog(snap, "v", "loc")
    assert cat.agent_test_tasks_parse_error is None
    assert [t.name for t in cat.agent_test_tasks] == ["t1", "t2"]
    assert cat.agent_test_tasks[0].unordered is True
    assert len(cat.agent_test_tasks[1].reference_statements) == 2
    assert cat.agent_test_tasks[1].check_sql == "SELECT true"

    # malformed -> parse error recorded, no crash
    bad = Snapshot(
        databases=[
            {"database_name": "v", "comment": "c", "tags": {"vgi.agent_test_tasks": "[{}]"}}
        ],
        schemas=[{"database_name": "v", "schema_name": "main", "comment": None, "tags": {}}],
    )
    cat2 = build_catalog(bad, "v", "loc")
    assert cat2.agent_test_tasks == [] and cat2.agent_test_tasks_parse_error is not None
