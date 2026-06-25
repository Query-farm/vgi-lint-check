from click.testing import CliRunner

from vgi_lint_check.cli import app


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
