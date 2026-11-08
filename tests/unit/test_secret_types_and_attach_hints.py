"""Tests for the secret-type rules (VGI1010-1013) and the attach-option hint (VGI1003)."""

import pytest

from tests import fixtures as F
from vgi_lint_check.config import Config, Options
from vgi_lint_check.loader import _build_secret_types
from vgi_lint_check.model import Argument
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.sql_parse import call_constant_args


def findings(cat, code, **kw):
    cfg = Config(**kw)
    return [f for f in run(select_rules(cfg), RuleContext(cat, cfg)) if f.code == code]


# --- VGI1003 constant argument -> attach option -----------------------------
def test_call_constant_args_keys_positional_and_named():
    calls = call_constant_args(
        "SELECT * FROM es.main.es_search('http://localhost:9200', index := 'products', size := 10)"
    )
    assert calls is not None and len(calls) == 1
    c = calls[0]
    assert (c.catalog, c.schema, c.name) == ("es", "main", "es_search")
    assert c.constants == {"#0": "http://localhost:9200", "index": "products", "size": "10"}
    assert c.kinds["#0"] == "STRING" and c.kinds["size"] == "INTEGER"


def test_call_constant_args_unparseable_is_none():
    assert call_constant_args("SELEC nonsense (((") is None


def _es(*sqls, arguments=()):
    fn = F.func(
        "main",
        "es_search",
        "table",
        examples=[F.example(i, f"example {i}", sql) for i, sql in enumerate(sqls)],
        arguments=arguments,
    )
    return F.catalog(F.schema("main", functions=[fn]))


def test_constant_argument_flags_shared_endpoint():
    cat = _es(
        "SELECT * FROM es_search('http://localhost:9200', index := 'products')",
        "SELECT * FROM es_search('http://localhost:9200', index := 'orders')",
        "SELECT count(*) FROM es_search('http://localhost:9200', index := 'logs')",
        arguments=[Argument(name="url", is_positional=True, position=0)],
    )
    out = findings(cat, "VGI1003")
    assert len(out) == 1
    assert "url => 'http://localhost:9200'" in out[0].message
    assert "3 calls" in out[0].message


def test_constant_argument_ignores_varied_values_and_numbers():
    cat = _es(
        "SELECT * FROM es_search('http://a:9200', size := 10)",
        "SELECT * FROM es_search('http://b:9200', size := 10)",
        "SELECT * FROM es_search('http://c:9200', size := 10)",
    )
    assert findings(cat, "VGI1003") == []


def test_constant_argument_needs_min_calls_and_dedupes_sql():
    sql = "SELECT * FROM es_search('http://localhost:9200')"
    assert findings(_es(sql, sql, sql), "VGI1003") == []  # one distinct statement
    two = _es(sql, "SELECT 1 FROM es_search('http://localhost:9200')")
    assert findings(two, "VGI1003") == []
    assert findings(two, "VGI1003", options=Options(constant_argument_min_calls=2))


def test_constant_argument_ignores_other_catalogs():
    cat = _es(
        "SELECT * FROM other.main.es_search('x://same')",
        "SELECT * FROM other.main.es_search('x://same', 1)",
        "SELECT * FROM other.main.es_search('x://same', 2)",
    )
    assert findings(cat, "VGI1003") == []


# --- secret types (VGI1010-1013) ---------------------------------------------
GOOD = {
    "secret_type": "azure_graph",
    "description": "Microsoft Entra app-only client credentials for Azure ARM / Microsoft Graph",
    "doc_url": "https://learn.microsoft.com/entra/identity-platform/quickstart-register-app",
    "parameters": [
        {
            "name": "tenant_id",
            "type": "VARCHAR",
            "required": True,
            "redact": False,
            "description": "Directory (tenant) ID from the app registration overview",
        },
        {
            "name": "client_id",
            "type": "VARCHAR",
            "required": True,
            "redact": False,
            "description": "Application (client) ID of the app registration",
        },
        {
            "name": "client_secret",
            "type": "VARCHAR",
            "required": True,
            "redact": True,
            "description": "A client secret value created under Certificates & secrets",
        },
    ],
    "required_by": ["cost_query"],
}


def _with_secrets(rows):
    cat = F.catalog(F.schema("main"))
    cat.secret_types = _build_secret_types("v", rows)
    return cat


CODES = ["VGI1010", "VGI1011", "VGI1012", "VGI1013"]


def test_secret_rules_silent_when_extension_cannot_report():
    cat = F.catalog(F.schema("main"))
    assert cat.secret_types is None
    for code in CODES:
        assert findings(cat, code) == []


def test_secret_rules_pass_on_well_documented_type():
    cat = _with_secrets([GOOD])
    for code in CODES:
        assert findings(cat, code) == [], code
    st = cat.secret_types[0]
    assert st.required_by == ["cost_query"] and st.parameters[2].redact


@pytest.mark.parametrize(
    ("patch", "code", "needle"),
    [
        ({"description": ""}, "VGI1010", "no meaningful description"),
        ({"description": "azure_graph"}, "VGI1010", "no meaningful description"),
        ({"doc_url": None}, "VGI1012", "no 'doc_url'"),
        ({"doc_url": "learn.microsoft.com"}, "VGI1012", "not an http(s) URL"),
    ],
)
def test_secret_type_level_findings(patch, code, needle):
    out = findings(_with_secrets([{**GOOD, **patch}]), code)
    assert out and needle in out[0].message


def test_secret_parameter_description_lists_missing():
    params = [dict(p) for p in GOOD["parameters"]]
    params[0]["description"] = None
    params[2]["description"] = ""
    out = findings(_with_secrets([{**GOOD, "parameters": params}]), "VGI1011")
    assert out and "tenant_id, client_secret" in out[0].message


def test_secret_parameter_redact_flags_credentials_not_ids():
    params = [dict(p) for p in GOOD["parameters"]]
    params[2]["redact"] = False  # client_secret unredacted
    params.append({"name": "api_key", "redact": False, "description": "x" * 40})
    params.append({"name": "access_key_id", "redact": False, "description": "x" * 40})
    out = findings(_with_secrets([{**GOOD, "parameters": params}]), "VGI1013")
    assert out
    assert "client_secret, api_key" in out[0].message
    assert "access_key_id" not in out[0].message


def test_constant_argument_ignores_repeated_sample_inputs():
    cat = _es(
        "SELECT * FROM es_search('CCO', 1)",
        "SELECT * FROM es_search('CCO', 2)",
        "SELECT * FROM es_search('CCO', 3)",
    )
    assert findings(cat, "VGI1003") == []


def test_constant_argument_flags_config_named_argument():
    cat = _es(
        "SELECT * FROM es_search(q := 'a', region := 'eu-west-1')",
        "SELECT * FROM es_search(q := 'b', region := 'eu-west-1')",
        "SELECT * FROM es_search(q := 'c', region := 'eu-west-1')",
    )
    out = findings(cat, "VGI1003")
    assert len(out) == 1 and "region => 'eu-west-1'" in out[0].message
