from vgi_lint_check.config import Config, from_table
from vgi_lint_check.findings import Severity
from vgi_lint_check.model import ObjectId, ObjectKind


class FakeRule:
    def __init__(self, code, category="description", default=Severity.WARNING, rc=False):
        self.code = code
        self.category = category
        self.default_severity = default
        self.requires_connection = rc


def test_default_select_all():
    cfg = Config()
    assert cfg.effective_severity(FakeRule("VGI101")) is Severity.WARNING


def test_ignore_disables():
    cfg = Config(ignore=["VGI1*"])
    assert cfg.effective_severity(FakeRule("VGI112")) is Severity.OFF
    assert cfg.effective_severity(FakeRule("VGI201")) is Severity.WARNING


def test_severity_override():
    cfg = Config(severity_overrides={"VGI201": Severity.ERROR})
    assert cfg.effective_severity(FakeRule("VGI201")) is Severity.ERROR


def test_execution_rule_needs_execute():
    rule = FakeRule("VGI901", default=Severity.ERROR, rc=True)
    cfg = Config(execute=False)
    assert cfg.effective_severity(rule) is Severity.OFF
    cfg.execute = True
    assert cfg.effective_severity(rule) is Severity.ERROR


def test_category_gate():
    cfg = Config(categories=["columns"])
    assert cfg.effective_severity(FakeRule("VGI101", category="description")) is Severity.OFF
    assert cfg.effective_severity(FakeRule("VGI201", category="columns")) is Severity.WARNING


def test_default_off_rule_stays_off():
    cfg = Config()
    assert cfg.effective_severity(FakeRule("VGI202", default=Severity.OFF)) is Severity.OFF


def test_per_object_ignore():
    cfg = Config(per_object={"v.hans.*": ["VGI112"]})
    oid = ObjectId("v", ObjectKind.TABLE, schema="hans", name="x")
    assert cfg.is_object_ignored(oid, "VGI112")
    assert not cfg.is_object_ignored(oid, "VGI201")
    other = ObjectId("v", ObjectKind.TABLE, schema="main", name="x")
    assert not cfg.is_object_ignored(other, "VGI112")


def test_from_table_parsing():
    cfg = from_table({
        "select": ["ALL"],
        "ignore": ["VGI113"],
        "fail-on": "warning",
        "severity": {"VGI201": "error"},
        "options": {"column-comment-min-ratio": 0.5, "required_schema_tags": ["provider"]},
        "per-object": {"v.hans.*": {"ignore": ["VGI112"]}},
        "execution": {"enabled": True, "mode": "limit", "limit": 3},
    })
    assert cfg.ignore == ["VGI113"]
    assert cfg.fail_on is Severity.WARNING
    assert cfg.severity_overrides["VGI201"] is Severity.ERROR
    assert cfg.options.column_comment_min_ratio == 0.5
    assert cfg.options.required_schema_tags == ["provider"]
    assert cfg.per_object == {"v.hans.*": ["VGI112"]}
    assert cfg.execute and cfg.execute_mode == "limit" and cfg.execute_limit == 3
