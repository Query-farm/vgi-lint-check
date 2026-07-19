import json

from tests import fixtures as F
from vgi_lint_check import reporting, scoring
from vgi_lint_check.config import Config
from vgi_lint_check.findings import Severity
from vgi_lint_check.result import Report, VersionResult
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext


def build_report(cat, fail_on=Severity.ERROR):
    cfg = Config()
    findings = run(select_rules(cfg), RuleContext(cat, cfg))
    quality = scoring.compute(cat, findings)
    vr = VersionResult(catalog=cat, findings=findings, quality=quality, diff_summary={"tables": 1})
    return Report(location="loc", alias="v", vgi_version="x", results=[vr], fail_on=fail_on)


def sample_catalog():
    from vgi_lint_check.model import AgentTask

    t = F.table("main", "bare")
    s = F.schema("main", tables=[t])
    cat = F.catalog(s)
    # Satisfy the error-level requirements (a test suite, and a guaranteed-runnable
    # example) so this fixture trips only warnings/info — the invariant this
    # module asserts.
    cat.agent_test_tasks = [AgentTask(name="t", prompt="p")]
    cat.executable_examples = [F.exec_example(0, "demo", [("s", "SELECT 1")])]
    return cat


def test_json_validates_against_published_schema():
    import jsonschema

    from vgi_lint_check.reporting.json_reporter import report_schema

    doc = json.loads(reporting.render_json(build_report(sample_catalog())))
    jsonschema.validate(doc, report_schema())  # raises on any drift


def test_json_schema_and_invariants():
    report = build_report(sample_catalog())
    doc = json.loads(reporting.render_json(report))
    assert doc["tool"] == "vgi-lint"
    assert doc["schema_version"] == 1
    assert doc["summary"]["passed"] is True  # only warnings/info, fail_on=error
    findings = doc["results"][0]["findings"]
    assert findings, "expected findings on a bare table"
    # agent-actionability invariant: every finding has a non-empty fix + summary
    for f in findings:
        assert f["fix"].strip()
        assert f["rule"]["summary"].strip()
        assert f["object"]["qualified"]
        assert f["rule"]["explain"] == f"vgi-lint explain {f['code']}"


def test_json_is_deterministic():
    a = reporting.render_json(build_report(sample_catalog()))
    b = reporting.render_json(build_report(sample_catalog()))
    assert a == b


def test_jsonl_one_object_per_line():
    report = build_report(sample_catalog())
    lines = [json.loads(ln) for ln in reporting.render_jsonl(report).splitlines()]
    assert lines[0]["type"] == "summary"
    assert all(ln["type"] == "finding" for ln in lines[1:])


def test_agent_markdown_has_fixes():
    text = reporting.render_agent(build_report(sample_catalog()))
    assert "# vgi-lint report" in text
    assert "fix:" in text
    assert "VGI111" in text  # bare table missing comment


def test_terminal_renders_without_color():
    text = reporting.render_terminal(build_report(sample_catalog()), color=False)
    assert "Catalog Quality Score" in text
    assert "vgi-lint" in text


def test_fail_on_warning_changes_pass():
    report = build_report(sample_catalog(), fail_on=Severity.WARNING)
    assert report.passed() is False  # there are warnings
    assert "✗ failed" in reporting.render_terminal(report, color=False)


def _many_bare_tables(n):
    # n tables with no comment -> VGI111 fires on each (same code/message/fix)
    tables = [F.table("main", f"t{i}") for i in range(n)]
    return F.catalog(F.schema("main", comment="c", tables=tables))


def test_terminal_groups_by_rule_and_caps():
    report = build_report(_many_bare_tables(25))
    out = reporting.render(report, "terminal", color=False, group_by="rule", max_per_rule=10)
    # the rule appears once with the total count, the fix once, and the tail collapses
    assert out.count("VGI111") == 1
    assert "(25 objects)" in out
    assert out.count("add a one-line comment") == 1  # fix stated once, not 25x
    assert "+15 more" in out


def test_terminal_group_by_object_lists_each():
    report = build_report(_many_bare_tables(3))
    out = reporting.render(report, "terminal", color=False, group_by="object")
    # legacy layout: VGI111 printed per object
    assert out.count("VGI111") == 3


def test_agent_groups_by_rule_no_cap():
    report = build_report(_many_bare_tables(25))
    out = reporting.render(report, "agent")
    # grouped by rule code (not by object), and never truncated for LLMs
    assert "### VGI111" in out
    assert "`v.main.t24`" in out  # the 25th object — beyond the terminal cap — is present
    assert "more (use" not in out
