"""Tests for table-function result-schema tags and rules (VGI307/321/322/323/324/326)."""

import json

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.model import TagSet
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.sql_parse import canonical_type
from vgi_lint_check.tags import (
    decode_result_columns_schema,
    decode_result_dynamic_columns_md,
)

SCHEMA = "vgi.result_columns_schema"
DYNAMIC = "vgi.result_dynamic_columns_md"


def codes(cat, **kw):
    cfg = Config(**kw)
    return {f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))}


def _tf(**tags):
    tf = F.func("main", "scan", ftype="table", description="scans stuff", tags=tags or None)
    return F.catalog(F.schema("main", comment="a schema comment", functions=[tf]))


def _static(cols):
    return {SCHEMA: json.dumps(cols)}


# --- decoders ---------------------------------------------------------------
def test_decode_static_schema():
    cols, err = decode_result_columns_schema(
        TagSet(
            {SCHEMA: json.dumps([{"name": "a", "type": "INT", "description": "d"}, {"name": "b"}])}
        )
    )
    assert err is None
    assert [(c.name, c.type, c.documented) for c in cols] == [
        ("a", "INT", True),
        ("b", None, False),
    ]


def test_decode_static_schema_malformed():
    assert decode_result_columns_schema(TagSet({SCHEMA: "{oops"}))[1].startswith("invalid JSON")
    assert "got dict" in decode_result_columns_schema(TagSet({SCHEMA: "{}"}))[1]
    assert "not an object" in decode_result_columns_schema(TagSet({SCHEMA: "[1]"}))[1]
    assert decode_result_columns_schema(TagSet({})) == ([], None)


def test_decode_dynamic_tables():
    md = (
        "Depends on mode.\n\n### summary\n\n| Name | Type | Description |\n"
        "| --- | --- | --- |\n| bucket | `VARCHAR` | key |\n| total | BIGINT | count |\n\n"
        "### detail\n\n| Name | Type | Description |\n|---|---|---|\n| id | BIGINT | row id |\n"
    )
    tables, err = decode_result_dynamic_columns_md(TagSet({DYNAMIC: md}))
    assert err is None
    assert [t.caption for t in tables] == ["summary", "detail"]
    assert [(c.name, c.type) for c in tables[0].columns] == [
        ("bucket", "VARCHAR"),
        ("total", "BIGINT"),
    ]


def test_decode_dynamic_wrong_header_yields_no_table():
    tables, _ = decode_result_dynamic_columns_md(TagSet({DYNAMIC: "| a | b |\n|--|--|\n| 1 | 2 |"}))
    assert tables == []


# --- canonical_type ---------------------------------------------------------
def test_canonical_type_classification():
    assert canonical_type("INT") == ("INTEGER", None)
    assert canonical_type("timestamptz") == ("TIMESTAMP WITH TIME ZONE", None)
    assert canonical_type("decimal(18,3)") == ("DECIMAL(18,3)", None)
    # a typo / worker-defined name is undecidable offline -> deferred (no flag)
    assert canonical_type("VARCHARR") == (None, None)
    # a structural error is a definite failure
    assert canonical_type("DECIMAL(x)")[0] is None
    assert canonical_type("DECIMAL(x)")[1] is not None
    # injection guard
    assert canonical_type("INT; DROP")[1] == "malformed type string"


# --- VGI307 documented / contradiction --------------------------------------
def test_vgi307_missing_schema_flagged():
    assert "VGI307" in codes(_tf())


def test_vgi307_static_or_dynamic_clears():
    assert "VGI307" not in codes(_tf(**_static([{"name": "a", "type": "INT", "description": "d"}])))
    md = "Varies.\n\n| Name | Type | Description |\n|---|---|---|\n| a | INT | d |\n"
    assert "VGI307" not in codes(_tf(**{DYNAMIC: md}))


def test_vgi307_both_is_contradiction():
    md = "| Name | Type | Description |\n|---|---|---|\n| a | INT | d |\n"
    c = codes(
        _tf(**{SCHEMA: json.dumps([{"name": "a", "type": "INT", "description": "d"}]), DYNAMIC: md})
    )
    assert "VGI307" in c


def test_vgi307_backing_table_exempt():
    tf = F.func("main", "animals", ftype="table", description="scan animals")
    tbl = F.table("main", "animals", comment="Animals table for backed table function test")
    cat = F.catalog(F.schema("main", comment="a schema comment", tables=[tbl], functions=[tf]))
    assert "VGI307" not in codes(cat)


# --- VGI321 shape -----------------------------------------------------------
def test_vgi321_parse_error_and_nameless():
    assert "VGI321" in codes(_tf(**{SCHEMA: "{not json"}))
    assert "VGI321" in codes(_tf(**_static([{"type": "INT", "description": "d"}])))  # no name
    assert "VGI321" not in codes(_tf(**_static([{"name": "a", "type": "INT", "description": "d"}])))


# --- VGI322 types (static + dynamic) ----------------------------------------
def test_vgi322_invalid_type_static():
    assert "VGI322" in codes(
        _tf(**_static([{"name": "a", "type": "DECIMAL(x)", "description": "d"}]))
    )


def test_vgi322_missing_type():
    assert "VGI322" in codes(_tf(**_static([{"name": "a", "description": "d"}])))


def test_vgi322_unknown_name_not_flagged_offline():
    # A worker-defined type unknown offline must NOT be flagged (deferred to live).
    assert "VGI322" not in codes(
        _tf(**_static([{"name": "geom", "type": "MYGEOM", "description": "d"}]))
    )


def test_vgi322_invalid_type_in_dynamic_variant():
    md = "| Name | Type | Description |\n|---|---|---|\n| a | NOTATYPE( | d |\n"
    assert "VGI322" in codes(_tf(**{DYNAMIC: md}))


def test_vgi322_valid_types_pass():
    cols = [
        {"name": "a", "type": "INTEGER", "description": "d"},
        {"name": "b", "type": "VARCHAR", "description": "e"},
    ]
    assert "VGI322" not in codes(_tf(**_static(cols)))


# --- VGI323 descriptions ----------------------------------------------------
def test_vgi323_missing_description():
    assert "VGI323" in codes(_tf(**_static([{"name": "a", "type": "INT"}])))
    md = "| Name | Type | Description |\n|---|---|---|\n| a | INT |   |\n"
    assert "VGI323" in codes(_tf(**{DYNAMIC: md}))


# --- VGI326 dynamic structure -----------------------------------------------
def test_vgi326_present_but_no_table():
    assert "VGI326" in codes(_tf(**{DYNAMIC: "It just varies, trust me."}))


def test_vgi326_ok_with_table():
    md = "Varies.\n\n| Name | Type | Description |\n|---|---|---|\n| a | INT | d |\n"
    assert "VGI326" not in codes(_tf(**{DYNAMIC: md}))


# --- VGI324 backing-table cross-check ---------------------------------------
def _backed(cols, table_cols):
    tf = F.func("main", "animals", ftype="table", description="scan animals", tags=_static(cols))
    columns = [F.col("main", "animals", name, dtype=dtype) for name, dtype in table_cols]
    tbl = F.table("main", "animals", columns=columns, comment="Animals backing table for the test")
    return F.catalog(F.schema("main", comment="a schema comment", tables=[tbl], functions=[tf]))


def test_vgi324_type_mismatch_and_phantom():
    cat = _backed(
        [
            {"name": "id", "type": "VARCHAR", "description": "wrong type"},
            {"name": "ghost", "type": "INT", "description": "not in table"},
        ],
        [("id", "INTEGER"), ("name", "VARCHAR")],
    )
    c = codes(cat)
    assert "VGI324" in c


def test_vgi324_match_passes():
    cat = _backed(
        [{"name": "id", "type": "INTEGER", "description": "row id"}],
        [("id", "INTEGER"), ("name", "VARCHAR")],
    )
    assert "VGI324" not in codes(cat)


# --- VGI910 live schema-compare (offline, fake schema-returning connection) ---
class _Res:
    def __init__(self, rows=None, one=None):
        self._rows = rows or []
        self._one = one

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _SchemaCon:
    """Answers DESCRIBE with canned (name, type) rows and typeof(...) via canonical_type."""

    def __init__(self, describe_rows):
        self.describe_rows = describe_rows
        self.ran = []

    def cursor(self):
        return self

    def execute(self, sql):
        self.ran.append(sql)
        up = sql.strip().upper()
        if up.startswith("DESCRIBE"):
            return _Res(rows=self.describe_rows)
        if "TYPEOF(NULL::" in up:
            inner = sql[sql.index("::") + 2 : sql.rindex(")")]
            return _Res(one=(canonical_type(inner)[0],))
        return _Res(rows=[])

    def interrupt(self):  # pragma: no cover - only on timeout
        pass


def _run_910(cat, con):
    from vgi_lint_check.findings import Severity
    from vgi_lint_check.rules.execution import ResultSchemaMatches

    cfg = Config(execute=True)
    ctx = RuleContext(cat, cfg, connection=con)
    ctx.severity = Severity.WARNING
    return list(ResultSchemaMatches().check(ctx))


def _tf_with_example(schema_cols, sql="SELECT ticker FROM x.main.scan(1)"):
    return F.func(
        "main",
        "scan",
        ftype="table",
        description="scans stuff",
        tags=_static(schema_cols),
        examples=[F.example(0, "d", sql)],
    )


def _cat910(fn):
    return F.catalog(F.schema("main", comment="a schema comment", functions=[fn]))


def test_vgi910_match_no_findings():
    fn = _tf_with_example([{"name": "ticker", "type": "VARCHAR", "description": "d"}])
    con = _SchemaCon([("ticker", "VARCHAR")])
    assert _run_910(_cat910(fn), con) == []
    assert any(s.strip().upper().startswith("DESCRIBE") for s in con.ran)


def test_vgi910_type_mismatch_flagged():
    fn = _tf_with_example([{"name": "ticker", "type": "INTEGER", "description": "d"}])
    con = _SchemaCon([("ticker", "VARCHAR")])
    out = _run_910(_cat910(fn), con)
    assert len(out) == 1 and out[0].code == "VGI910"
    assert "declared INTEGER" in out[0].message and "returns VARCHAR" in out[0].message


def test_vgi910_missing_and_extra_columns():
    fn = _tf_with_example(
        [
            {"name": "ticker", "type": "VARCHAR", "description": "d"},
            {"name": "ghost", "type": "VARCHAR", "description": "not returned"},
        ]
    )
    con = _SchemaCon([("ticker", "VARCHAR"), ("surprise", "BIGINT")])
    msgs = [f.message for f in _run_910(_cat910(fn), con)]
    assert any("ghost" in m and "does not return" in m for m in msgs)
    assert any("surprise" in m and "not in vgi.result_columns_schema" in m for m in msgs)


def test_vgi910_no_example_is_skipped():
    fn = F.func(
        "main",
        "scan",
        ftype="table",
        description="scans stuff",
        tags=_static([{"name": "ticker", "type": "VARCHAR", "description": "d"}]),
    )
    con = _SchemaCon([("ticker", "VARCHAR")])
    assert _run_910(_cat910(fn), con) == []
    assert con.ran == []  # nothing described


def test_vgi910_dynamic_union_coverage():
    md = (
        "### a\n\n| Name | Type | Description |\n|---|---|---|\n| x | INT | d |\n\n"
        "### b\n\n| Name | Type | Description |\n|---|---|---|\n| y | INT | d |\n"
    )
    fn = F.func(
        "main",
        "scan",
        ftype="table",
        description="scans stuff",
        tags={DYNAMIC: md},
        examples=[F.example(0, "d", "SELECT x FROM x.main.scan(1)")],
    )
    # returns x (declared in variant a) and z (declared nowhere) -> only z flagged
    con = _SchemaCon([("x", "INTEGER"), ("z", "INTEGER")])
    msgs = [f.message for f in _run_910(_cat910(fn), con)]
    assert len(msgs) == 1 and "z" in msgs[0] and "variant" in msgs[0]
