import json

from click.testing import CliRunner

import vgi_lint_check.cli as cli_module
from vgi_lint_check.cli import app
from vgi_lint_check.semantic_compiler import compile_semantic_query


def run(*args):
    return CliRunner().invoke(app, list(args))


def test_help():
    r = run("--help")
    assert r.exit_code == 0
    assert "Lint the metadata quality" in r.output


def test_rules_lists_catalog():
    r = run("rules")
    assert r.exit_code == 0
    assert "VGI112" in r.output and "VGI901" in r.output


def test_rules_json():
    import json

    r = run("rules", "--format", "json")
    assert r.exit_code == 0
    data = json.loads(r.output)
    codes = {d["code"] for d in data}
    assert "VGI201" in codes
    assert all("summary" in d for d in data)


def test_rules_category_filter():
    r = run("rules", "--category", "examples")
    assert r.exit_code == 0
    assert "VGI501" in r.output
    assert "VGI101" not in r.output


def test_explain_known_and_unknown():
    ok = run("explain", "VGI112")
    assert ok.exit_code == 0
    assert "description-llm" in ok.output
    bad = run("explain", "VGI999")
    assert bad.exit_code != 0


def test_lint_requires_location():
    # No location and no config -> usage error
    r = CliRunner().invoke(app, ["lint"], catch_exceptions=False)
    assert r.exit_code != 0
    assert "no worker LOCATION" in r.output


def test_init_scaffolds(tmp_path):
    target = tmp_path / "vgi-lint.toml"
    r = run("init", "--location", "uv run w.py", "--file", str(target))
    assert r.exit_code == 0
    text = target.read_text()
    assert "[tool.vgi-lint-check]" in text
    assert 'location = "uv run w.py"' in text
    # refuses to overwrite
    r2 = run("init", "--file", str(target))
    assert r2.exit_code != 0


def test_default_command_routing():
    # `vgi-lint --help` shows group help; an unknown first token routes to lint.
    r = run("does-not-exist-location")
    # routed to lint -> fails to connect/usage, but NOT a "no such command" error
    assert "No such command" not in r.output


def test_simulate_help_and_usage():
    r = run("simulate", "--help")
    assert r.exit_code == 0
    assert "agent_test_tasks" in r.output
    assert "--suggest" in r.output and "--min-pass-rate" in r.output
    # no location and no config -> usage error
    r2 = run("simulate")
    assert r2.exit_code != 0


def test_semantic_simulate_help_and_alias_validation():
    help_result = run("semantic-simulate", "--help")
    assert help_result.exit_code == 0
    assert "composed workers' semantic model" in help_result.output
    assert "--agent-tasks-file" in help_result.output
    invalid = run("semantic-simulate", "sales", "crm", "--as", "sales_runtime")
    assert invalid.exit_code != 0
    assert "repeat --as exactly once per LOCATION" in invalid.output


def _patch_attached_catalogs(monkeypatch, catalogs):
    def attached(_specs, runner, *, install=True, spatial=True):
        return runner(catalogs, object())

    monkeypatch.setattr("vgi_lint_check.core.with_attached_catalogs", attached)


def _scalar_request():
    return {
        "measures": [
            {
                "catalog_id": "com.example.sales",
                "entity_id": "orders",
                "member_id": "revenue",
            }
        ]
    }


def test_semantic_compile_file_stdin_and_direct_compiler_parity(monkeypatch, tmp_path):
    from tests.semantic_example import load_example

    _fixture, catalogs, connection = load_example()
    connection.close()
    _patch_attached_catalogs(monkeypatch, catalogs)
    request = _scalar_request()
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request))

    result = run("semantic-compile", "sales", "crm", "--request", str(request_file), "--no-install")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    direct = compile_semantic_query(catalogs, {**request, "compile_only": True})
    assert payload == direct
    assert payload["plan"]["sql"].startswith("SELECT")

    stdin = CliRunner().invoke(
        app,
        ["semantic-compile", "sales", "crm", "--request", "-", "--no-install"],
        input=json.dumps(request),
    )
    assert stdin.exit_code == 0
    assert json.loads(stdin.output) == direct


def test_semantic_compile_correlated_and_capability_diagnostics(monkeypatch, tmp_path):
    from tests.unit.test_semantic_end_to_end import _forecast_catalog, _rehome

    worker = _rehome(_forecast_catalog(), "weather")
    _patch_attached_catalogs(monkeypatch, {"weather": worker})
    request = {
        "measures": [
            {
                "catalog_id": "farm.query.open_meteo",
                "entity_id": "forecast_hourly",
                "member_id": "average_temperature",
            }
        ],
        "inputs": [
            {
                "input_id": "locations",
                "grain": ["location_id"],
                "columns": [
                    {"name": "location_id", "type": "VARCHAR"},
                    {"name": "latitude", "type": "DOUBLE"},
                    {"name": "longitude", "type": "DOUBLE"},
                ],
                "rows": [["berlin", 52.52, 13.41], ["tokyo", 35.69, 139.69]],
            }
        ],
        "source_bindings": [
            {
                "entity": {
                    "catalog_id": "farm.query.open_meteo",
                    "entity_id": "forecast_hourly",
                },
                "driver": {"input_id": "locations"},
                "arguments": {
                    "latitude": {"input_column": "latitude"},
                    "longitude": {"input_column": "longitude"},
                },
            }
        ],
    }
    request_file = tmp_path / "correlated.json"
    request_file.write_text(json.dumps(request))
    result = run("semantic-compile", "weather", "--request", str(request_file))
    assert result.exit_code == 0, result.output
    assert "CROSS JOIN LATERAL" in json.loads(result.output)["plan"]["sql"]

    next(worker.iter_all_functions()).input_from_args = None
    unknown = run("semantic-compile", "weather", "--request", str(request_file))
    assert unknown.exit_code == 2
    diagnostic = json.loads(unknown.output)["diagnostics"][0]
    assert diagnostic["code"] == "correlated_input_capability_unknown"
    assert "upgrade" in diagnostic["message"]


def test_semantic_compile_input_errors_and_semantic_findings(monkeypatch, tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{")
    bad_json = run("semantic-compile", "worker", "--request", str(malformed))
    assert bad_json.exit_code == 1
    assert "error:" in bad_json.output

    alias = run(
        "semantic-compile",
        "sales",
        "crm",
        "--as",
        "sales_runtime",
        "--request",
        str(malformed),
    )
    assert alias.exit_code == 1
    assert "repeat --as exactly once" in alias.output

    from tests.semantic_example import load_example

    _fixture, catalogs, connection = load_example()
    connection.close()
    _patch_attached_catalogs(monkeypatch, catalogs)
    missing = tmp_path / "missing.json"
    missing.write_text(
        json.dumps(
            {
                "measures": [
                    {
                        "catalog_id": "com.example.sales",
                        "entity_id": "orders",
                        "member_id": "missing",
                    }
                ]
            }
        )
    )
    finding = run("semantic-compile", "sales", "crm", "--request", str(missing))
    assert finding.exit_code == 2
    assert json.loads(finding.output)["diagnostics"][0]["code"] == "unknown_member"


def test_semantic_compile_connection_failure_uses_connection_exit(monkeypatch, tmp_path):
    from vgi_lint_check.connection import WorkerConnectionError

    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(_scalar_request()))

    def fail(*_args, **_kwargs):
        raise WorkerConnectionError("attach failed")

    monkeypatch.setattr("vgi_lint_check.core.with_attached_catalogs", fail)
    result = run("semantic-compile", "worker", "--request", str(request_file))
    assert result.exit_code == 3
    assert "attach failed" in result.output


def test_exported_member_schema_contains_unit_contract():
    result = run("spec", "--schema", "member")
    assert result.exit_code == 0
    schema = json.loads(result.output)
    assert "unit_parameter" in schema["$defs"]


def test_lint_agent_tasks_file_is_absolute_and_cwd_independent(monkeypatch, tmp_path):
    sidecar = tmp_path / "private" / "vgi-agent-tests.yaml"
    sidecar.parent.mkdir()
    sidecar.write_text("tasks: []\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    captured = {}

    class Report:
        def passed(self):
            return True

    def fake_lint_worker(_location, **kwargs):
        captured["path"] = kwargs["config"].agent_tasks_file
        return Report()

    monkeypatch.setattr(cli_module, "lint_worker", fake_lint_worker)
    monkeypatch.setattr(cli_module.reporting, "render", lambda *_args, **_kwargs: "")
    monkeypatch.chdir(elsewhere)
    result = run("lint", "worker", "--agent-tasks-file", str(sidecar), "--no-install", "--quiet")
    assert result.exit_code == 0, result.output
    assert captured["path"] == str(sidecar.resolve())
