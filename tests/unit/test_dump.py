"""Offline tests for ``vgi-lint dump`` (serialization, ANY resolution, verify)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from tests import fixtures as F
from vgi_lint_check import dump as D
from vgi_lint_check.cli import app
from vgi_lint_check.model import ObjectId, ObjectKind
from vgi_lint_check.rules._util import QueryTimeout
from vgi_lint_check.sql_parse import project_call_sqls
from vgi_lint_check.versions import _parse_attach_options


# --------------------------------------------------------------------------
# a canned connection
# --------------------------------------------------------------------------
class _Result:
    def __init__(self, cols, rows):
        self.description = [(c,) for c in cols]
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeCon:
    """Answers the system-table reads and DESCRIBE/statement SQL from dicts."""

    def __init__(self, functions=(), columns=(), describe=None, statements=None):
        self.functions = list(functions)
        self.columns = list(columns)
        self.describe = describe or {}
        self.statements = statements or {}
        self.executed: list[str] = []

    def cursor(self):
        return self

    def close(self):
        pass

    def interrupt(self):
        pass

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if "FROM duckdb_functions()" in sql:
            cols = ["schema_name", "function_name", "function_type", "parameter_types"]
            cols.append("return_type")
            return _Result(cols, [tuple(r) for r in self.functions])
        if "FROM duckdb_columns()" in sql:
            cols = ["schema_name", "table_name", "column_name", "is_nullable"]
            return _Result(cols, [tuple(r) for r in self.columns])
        if sql.startswith("DESCRIBE "):
            body = sql[len("DESCRIBE ") :]
            if body not in self.describe:
                raise RuntimeError(f"Binder Error: cannot bind {body}")
            t = self.describe[body]
            return _Result(["column_name", "column_type"], [("x", t)])
        if sql in self.statements:
            out = self.statements[sql]
            if isinstance(out, Exception):
                raise out
            cols, rows = out
            return _Result(cols, rows)
        raise RuntimeError(f"unexpected SQL {sql!r}")


def _scalar(name, ptypes, examples=()):
    f = F.func("main", name, examples=examples)
    f.parameter_types = list(ptypes)
    f.parameters = [f"a{i}" for i in range(len(ptypes))]
    return f


# --------------------------------------------------------------------------
# attach options
# --------------------------------------------------------------------------
def test_parse_attach_options_keeps_required_and_secret():
    rows = [
        {
            "name": "token",
            "description": "API token",
            "type": "VARCHAR",
            "default_value": None,
            "required": True,
            "secret": True,
        },
        {"name": "region", "type": "VARCHAR", "default_value": "us", "required": False},
        {"name": "legacy", "type": "VARCHAR", "default_value": None},  # older extension
    ]
    opts = _parse_attach_options(rows)
    assert (opts[0].required, opts[0].secret) == (True, True)
    assert (opts[1].required, opts[1].secret) == (False, None)
    assert (opts[2].required, opts[2].secret) == (None, None)
    # JSON-string carrier with textual booleans.
    js = '[{"name":"x","default_value":null,"required":"false","secret":"true"}]'
    (o,) = _parse_attach_options(js)
    assert (o.required, o.secret) == (False, True)


def test_model_required_prefers_declared_flag():
    from vgi_lint_check.model import AttachOption

    oid = ObjectId("v", ObjectKind.ATTACH_OPTION, name="x")
    # Declared optional even though it has no default (e.g. an optional secret).
    assert AttachOption(oid, "x", default=None, declared_required=False).required is False
    # Declared required even with a default.
    assert AttachOption(oid, "x", default="1", declared_required=True).required is True
    # Older extension: inferred from the default.
    assert AttachOption(oid, "x", default=None).required is True


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def test_transport_of():
    assert D.transport_of("uv run worker.py") == "subprocess"
    assert D.transport_of("github-auto://Query-farm/vgi-color@v0.2.0") == "github-auto"
    assert D.transport_of("docker://ghcr.io/query-farm/vgi-x509:0.2.0") == "docker"
    assert D.transport_of("https://w.example/") == "https"
    assert D.transport_of("launch:./w") == "launch"


def test_diff_secret_types():
    before = [{"name": "s3", "default_provider": "config", "extension": "httpfs"}]
    after = [*before, {"name": "azure_graph", "default_provider": "config", "extension": "vgi"}]
    assert D.diff_secret_types(before, after) == [
        {"name": "azure_graph", "default_provider": "config", "extension": "vgi"}
    ]


def test_decode_tags_decodes_json_only():
    out = D.decode_tags({"b": '["x", "y"]', "a": "plain", "c": "{bad"})
    assert list(out) == ["a", "b", "c"]
    assert out["b"] == ["x", "y"] and out["a"] == "plain" and out["c"] == "{bad"


# --------------------------------------------------------------------------
# ANY return-type resolution
# --------------------------------------------------------------------------
def test_project_call_sqls_isolates_the_call():
    sqls = project_call_sqls(
        "SELECT name, color.main.from_hex(hex) AS c FROM color.main.named_colors "
        "WHERE hex IS NOT NULL ORDER BY 1 LIMIT 3",
        "from_hex",
    )
    assert sqls == [
        "SELECT color.main.from_hex(hex) FROM color.main.named_colors WHERE (hex IS NOT NULL)"
    ]
    assert project_call_sqls("SELECT other(1)", "from_hex") == []
    # Arity filter for overloaded names.
    assert project_call_sqls("SELECT f(1, 2)", "f", arity=1) == []
    # A call inside a subquery belongs to another scope.
    assert project_call_sqls("SELECT * FROM (SELECT f(a) FROM t)", "f") == []


def test_needs_resolution():
    assert D.needs_resolution("ANY")
    assert D.needs_resolution(None)
    assert not D.needs_resolution("STRUCT(r INTEGER)")


def test_resolve_from_example_then_synthetic():
    ex = F.example(0, "parse", "SELECT color.main.from_hex('#ff6347') AS rgb")
    f = _scalar("from_hex", ["VARCHAR"], examples=[ex])
    cat = F.catalog(F.schema("main", functions=[f]))
    con = FakeCon(
        describe={"SELECT color.main.from_hex('#ff6347')": "STRUCT(r INTEGER, g INTEGER)"}
    )
    r = D.resolve_return_type(con, cat, f, "ANY", overloads=1, timeout=5)
    assert (r.return_type, r.source) == ("STRUCT(r INTEGER, g INTEGER)", "bound")
    assert r.bound_from == "SELECT color.main.from_hex('#ff6347')"


def test_resolve_falls_back_to_typed_null_call():
    f = _scalar("to_rgb", ["DOUBLE", "DOUBLE"])
    cat = F.catalog(F.schema("main", functions=[f]))
    synth = 'SELECT "v"."main"."to_rgb"(NULL::DOUBLE, NULL::DOUBLE)'
    con = FakeCon(describe={synth: "STRUCT(r INTEGER)"})
    r = D.resolve_return_type(con, cat, f, "ANY", overloads=1, timeout=5)
    assert (r.return_type, r.source, r.bound_from) == ("STRUCT(r INTEGER)", "bound", synth)


def test_resolve_unknown_when_nothing_binds():
    f = _scalar("mystery", ["ANY"])  # no typed NULL can stand in for ANY
    cat = F.catalog(F.schema("main", functions=[f]))
    r = D.resolve_return_type(FakeCon(), cat, f, "ANY", overloads=1, timeout=5)
    assert r.source == "unknown" and r.return_type == "ANY"
    assert r.error == "no sample call found"


def test_resolve_declared_is_untouched():
    f = _scalar("lum", ["VARCHAR"])
    cat = F.catalog(F.schema("main", functions=[f]))
    r = D.resolve_return_type(FakeCon(), cat, f, "DOUBLE", overloads=1, timeout=5)
    assert (r.return_type, r.source) == ("DOUBLE", "declared")


def test_overloaded_prefers_exact_typed_null_call():
    ex = F.example(0, "x", "SELECT f(1)")
    f = _scalar("f", ["BIGINT"], examples=[ex])
    cat = F.catalog(F.schema("main", functions=[f]))
    synth = 'SELECT "v"."main"."f"(NULL::BIGINT)'
    con = FakeCon(describe={synth: "BIGINT[]", "SELECT f(1)": "INTEGER[]"})
    r = D.resolve_return_type(con, cat, f, "ANY", overloads=2, timeout=5)
    assert r.bound_from == synth and r.return_type == "BIGINT[]"


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------
def _sample_catalog():
    ex = F.example(0, "parse", "SELECT v.main.from_hex('#000000')")
    fn = _scalar("from_hex", ["VARCHAR"], examples=[ex])
    fn.arguments = [F.arg("hex", "VARCHAR", "A #rrggbb colour", is_positional=True, position=0)]
    lum = _scalar("luminance", ["VARCHAR"])
    tbl = F.table(
        "main",
        "named_colors",
        comment="Named colours",
        columns=[F.col("main", "named_colors", "name", "Colour name")],
    )
    backing = F.func("main", "named_colors", "table")
    sch = F.schema("main", comment="Colours", tables=[tbl], functions=[fn, lum, backing])
    opt = F.attach_option("token", "API token", default=None)
    opt = type(opt)(opt.id, opt.name, opt.description, opt.type, None, True, True)
    cat = F.catalog(sch, attach_options=[opt])
    cat.catalog_name = "v"
    cat.executable_examples = [
        F.exec_example(0, "parse black", [("one", "SELECT v.main.from_hex('#000000')")])
    ]
    con = FakeCon(
        functions=[
            ("main", "from_hex", "scalar", ["VARCHAR"], "ANY"),
            ("main", "luminance", "scalar", ["VARCHAR"], "DOUBLE"),
            ("main", "named_colors", "table", [], "TABLE"),
        ],
        columns=[("main", "named_colors", "name", False)],
        describe={"SELECT v.main.from_hex('#000000')": "STRUCT(r INTEGER, g INTEGER, b INTEGER)"},
    )
    return cat, con


def test_serialize_catalog_is_harvest_superset():
    cat, con = _sample_catalog()
    out = D.serialize_catalog(cat, con)
    harvest_keys = {
        "catalog",
        "advertised_catalogs",
        "implementation_version",
        "default_schema",
        "vgi_version",
        "title",
        "summary",
        "doc_md",
        "keywords",
        "categories",
        "license",
        "source_url",
        "catalog_tags",
        "attach_options",
        "settings",
        "example_queries",
        "agent_tasks",
        "schemas",
        "counts",
    }
    assert harvest_keys <= set(out)
    json.dumps(out)  # serializable
    (opt,) = out["attach_options"]
    assert opt == {
        "name": "token",
        "type": "VARCHAR",
        "description": "API token",
        "required": True,
        "default": None,
        "secret": True,
    }
    (schema,) = out["schemas"]
    # The backing table function folds into the table, not the function list.
    assert [f["name"] for f in schema["functions"]] == ["from_hex", "luminance"]
    (table,) = schema["tables"]
    assert table["columns"] == [
        {"name": "name", "type": "VARCHAR", "comment": "Colour name", "nullable": False}
    ]
    fh, lum = schema["functions"]
    assert fh["return_type"] == "STRUCT(r INTEGER, g INTEGER, b INTEGER)"
    assert (fh["return_type_source"], fh["declared_return_type"]) == ("bound", "ANY")
    assert (lum["return_type"], lum["return_type_source"]) == ("DOUBLE", "declared")
    (a,) = fh["arguments"]
    assert a["name"] == "hex" and a["description"] == "A #rrggbb colour"
    assert a["is_positional"] is True and a["named"] is False
    assert out["counts"]["return_types_bound"] == 1
    assert out["counts"]["executable_examples"] == 1
    assert out["executable_examples"][0]["statements"][0]["sql"].startswith("SELECT")


def test_serialize_without_type_resolution():
    cat, con = _sample_catalog()
    out = D.serialize_catalog(cat, con, resolve_types=False)
    fh = out["schemas"][0]["functions"][0]
    assert (fh["return_type"], fh["return_type_source"]) == ("ANY", "unknown")
    assert not [s for s in con.executed if s.startswith("DESCRIBE")]


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
def _host():
    return ObjectId("v", ObjectKind.CATALOG)


def test_run_example_pass_mismatch_fail_and_credentials():
    ok = F.exec_example(0, "d", [("s", "SELECT 1 AS x", [{"x": 1}])])
    bad = F.exec_example(1, "d", [("s", "SELECT 2 AS x", [{"x": 1}])])
    boom = F.exec_example(2, "d", [("s", "SELECT boom()")])
    creds = F.exec_example(3, "d", [("s", "SELECT needs_secret()")])
    con = FakeCon(
        statements={
            "SELECT 1 AS x": (["x"], [(1,)]),
            "SELECT 2 AS x": (["x"], [(2,)]),
            "SELECT boom()": RuntimeError("Binder Error: no function boom"),
            "SELECT needs_secret()": RuntimeError(
                "Invalid Input Error: needs_secret: attach an 'azure_graph' secret "
                "(TYPE azure_graph)"
            ),
        }
    )
    assert D.run_example(con, _host(), ok, 5).status == "passed"
    mm = D.run_example(con, _host(), bad, 5)
    assert mm.status == "mismatch" and mm.mismatches[0]["statement"] == 0
    f = D.run_example(con, _host(), boom, 5)
    assert f.status == "failed" and "boom" in (f.error or "")
    c = D.run_example(con, _host(), creds, 5)
    assert c.status == "needs_credentials"


def _verify_catalog(n):
    cat = F.catalog()
    cat.executable_examples = [
        F.exec_example(i, "d", [("s", f"SELECT {i}")], name=f"ex{i}") for i in range(n)
    ]
    return cat


def _runner(statuses):
    def run(cur, host, ex, timeout):
        return D.ExampleOutcome(host.qualified(), ex.index, ex.name, statuses[ex.index])

    return run


def test_verify_all_pass():
    v = D.verify_examples(FakeCon(), _verify_catalog(2), timeout=5, runner=_runner(["passed"] * 2))
    assert v["verified"] is True and v["status"] == "passed"
    assert (v["total"], v["passed"], v["failed"]) == (2, 2, 0)


def test_verify_failure_and_timeout_fail():
    v = D.verify_examples(
        FakeCon(), _verify_catalog(3), timeout=5, runner=_runner(["passed", "failed", "timeout"])
    )
    assert v["verified"] is False and v["status"] == "failed"
    assert v["failed"] == 2 and v["by_status"] == {"passed": 1, "failed": 1, "timeout": 1}


def test_verify_credentials_policy():
    cat = _verify_catalog(2)
    run = _runner(["passed", "needs_credentials"])
    lax = D.verify_examples(FakeCon(), cat, timeout=5, runner=run)
    assert lax["verified"] is True and lax["status"] == "passed_with_credential_skips"
    strict = D.verify_examples(FakeCon(), cat, timeout=5, runner=run, require_credentials=True)
    assert strict["verified"] is False


def test_verify_malformed_tag_is_invalid():
    cat = F.catalog()
    cat.executable_examples_parse_error = "invalid JSON"
    v = D.verify_examples(FakeCon(), cat, timeout=5)
    assert v["verified"] is False and v["by_status"] == {"invalid": 1}


def test_verify_no_examples():
    v = D.verify_examples(FakeCon(), F.catalog(), timeout=5)
    assert v["verified"] is True and v["status"] == "no_examples"


def test_run_example_timeout(monkeypatch):
    import vgi_lint_check.rules.execution as ex_mod

    def _raise(*_a, **_k):
        raise QueryTimeout("query exceeded 1s and was cancelled")

    monkeypatch.setattr(ex_mod, "_run_executable", _raise)
    out = D.run_example(FakeCon(), _host(), F.exec_example(0, "d", [("s", "SELECT 1")]), 1)
    assert out.status == "timeout"


# --------------------------------------------------------------------------
# CLI exit codes
# --------------------------------------------------------------------------
def _fake_doc(verified):
    doc = D.base_document("uv run w.py")
    doc.update(
        status="ok",
        verified=verified,
        catalog={"catalog": "w", "counts": {}},
        lint={"score": 90, "level": "L2"},
        verify={"enabled": True, "verified": verified, "status": "x", "total": 1},
    )
    return doc


def test_cli_dump_exit_codes(monkeypatch, tmp_path):
    import vgi_lint_check.dump as dump_mod

    monkeypatch.setattr(dump_mod, "dump_worker", lambda *a, **k: _fake_doc(False))
    out = tmp_path / "w.json"
    r = CliRunner().invoke(app, ["dump", "uv run w.py", "-o", str(out), "-q"])
    assert r.exit_code == 2
    assert json.loads(out.read_text())["verified"] is False
    r = CliRunner().invoke(app, ["dump", "uv run w.py", "-o", str(out), "-q", "--allow-unverified"])
    assert r.exit_code == 0
    monkeypatch.setattr(dump_mod, "dump_worker", lambda *a, **k: _fake_doc(True))
    r = CliRunner().invoke(app, ["dump", "--location", "uv run w.py", "-q"])
    assert r.exit_code == 0 and json.loads(r.output)["schema_version"] == 1


def test_cli_dump_attach_failure_writes_document(monkeypatch, tmp_path):
    import vgi_lint_check.dump as dump_mod
    from vgi_lint_check.connection import WorkerConnectionError

    def _fail(*_a, **_k):
        raise WorkerConnectionError("couldn't reach it")

    monkeypatch.setattr(dump_mod, "dump_worker", _fail)
    out = tmp_path / "w.json"
    r = CliRunner().invoke(app, ["dump", "x", "-o", str(out), "-q"])
    assert r.exit_code == 3
    doc = json.loads(out.read_text())
    assert doc["status"] == "attach_failed" and doc["verified"] is False
    assert doc["schema_version"] == 1 and doc["catalog"] is None


# --------------------------------------------------------------------------
# fleet dump (process-group timeout)
# --------------------------------------------------------------------------
_FAKE_LINTER = """
import json, sys, time
args = sys.argv[1:]
dest = args[args.index("--output") + 1]
if "sleep" in args[1]:
    time.sleep(60)
json.dump({"status": "ok", "verified": True, "lint": {"score": 77, "level": "L1"},
           "verify": {"status": "passed"}}, open(dest, "w"))
"""


def test_fleet_dump_one_ok_and_timeout(tmp_path):
    import sys

    from vgi_lint_check import fleet

    script = tmp_path / "fake_linter.py"
    script.write_text(_FAKE_LINTER)
    linter = [sys.executable, str(script)]
    specs = [
        fleet.WorkerSpec(name="good", location="run-me"),
        fleet.WorkerSpec(name="slow", location="sleep-me", timeout=1),
        fleet.WorkerSpec(name="off", location="x", skip=True, skip_reason="manual"),
    ]
    out = tmp_path / "dumps"
    res = {r["name"]: r for r in fleet.dump_sweep(specs, out, jobs=2, linter=linter)}
    assert res["good"]["verified"] is True and res["good"]["score"] == 77
    assert res["slow"]["status"] == "timeout" and res["slow"]["verified"] is False
    assert res["off"]["status"] == "skipped"
    slow = json.loads((out / "slow.json").read_text())
    assert slow["status"] == "timeout" and slow["schema_version"] == 1
