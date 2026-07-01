"""Tests for the --trace timing tracer and its wiring into the rule engine."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.trace import Tracer


def test_tracer_records_and_dumps(tmp_path):
    t = Tracer(tmp_path / "trace.log")
    with t.span("phase", "connect"):
        pass
    with t.span("rule", "VGI001"):
        pass
    t.dump()
    text = (tmp_path / "trace.log").read_text()
    assert "== timeline ==" in text
    assert "phase:connect" in text and "rule:VGI001" in text
    assert "slowest rules" in text


def test_tracer_span_records_on_error(tmp_path):
    t = Tracer(tmp_path / "trace.log")
    try:
        with t.span("rule", "VGI999"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert any(e.name == "VGI999" for e in t.events)  # timed even though it raised


def test_engine_times_each_rule_when_tracer_set(tmp_path):
    cat = F.catalog(F.schema("main", comment="c"))
    cfg = Config()
    t = Tracer(tmp_path / "trace.log")
    ctx = RuleContext(cat, cfg, tracer=t)
    run(select_rules(cfg), ctx)
    timed = {e.name for e in t.events if e.kind == "rule"}
    assert len(timed) > 20  # every selected rule got a span
    assert "VGI001" in timed


def test_engine_no_tracer_is_fine():
    cat = F.catalog(F.schema("main", comment="c"))
    cfg = Config()
    # No tracer on the context -> no error, findings still produced.
    findings = run(select_rules(cfg), RuleContext(cat, cfg))
    assert isinstance(findings, list)
