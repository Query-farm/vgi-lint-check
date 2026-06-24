from dataclasses import dataclass

from vgi_lint_check import baseline, comparison, scoring
from vgi_lint_check.findings import Category, Finding, Severity
from vgi_lint_check.model import ObjectId, ObjectKind

from tests import fixtures as F


def _finding(code, sev=Severity.WARNING, name="x"):
    oid = ObjectId("v", ObjectKind.TABLE, schema="main", name=name)
    return Finding(code, sev, Category.DESCRIPTION, oid, "msg", "hint")


def test_score_perfect_catalog():
    t = F.table("main", "t", comment="c",
                columns=[F.col("main", "t", "a", "doc")],
                examples=[F.example(0, "d", "SELECT * FROM t")])
    s = F.schema("main", comment="c", tables=[t])
    qs = scoring.compute(F.catalog(s), [])
    assert qs.score == 100
    assert qs.coverage.families["columns"] == 1.0


def test_score_drops_with_gaps_and_errors():
    t = F.table("main", "t", columns=[F.col("main", "t", "a", None)])
    s = F.schema("main", tables=[t])
    qs = scoring.compute(F.catalog(s), [_finding("VGI502", Severity.ERROR)])
    assert qs.score < 100
    assert qs.coverage.families["columns"] == 0.0


def test_coverage_none_when_no_columns():
    s = F.schema("main", comment="c")
    qs = scoring.compute(F.catalog(s), [])
    assert qs.coverage.families["columns"] is None


def test_baseline_roundtrip_and_classify(tmp_path):
    prefix = str(tmp_path / "bl")
    findings = [_finding("VGI112", name="a"), _finding("VGI113", name="b")]
    path = baseline.write(prefix, "1.0.0", findings)
    assert path.name == "bl.1.0.0.json"

    # one known, one new
    later = [_finding("VGI112", name="a"), _finding("VGI201", name="c")]
    classified = baseline.classify(later, prefix, "1.0.0")
    by_code = {f.code: f.is_new for f in classified}
    assert by_code["VGI112"] is False
    assert by_code["VGI201"] is True


def test_baseline_default_when_no_version(tmp_path):
    prefix = str(tmp_path / "bl.json")
    p = baseline.baseline_path(prefix, None)
    assert p.name == "bl.default.json"


def test_classify_no_prefix_all_new():
    out = baseline.classify([_finding("VGI112")], None, "1.0.0")
    assert all(f.is_new for f in out)


@dataclass
class _Result:
    catalog: object
    findings: list
    score: int


def test_comparison_deltas_and_added_objects():
    s1 = F.schema("main", comment="c", tables=[F.table("main", "t")])
    cat1 = F.catalog(s1)
    cat1.data_version = "1.0.0"
    s2 = F.schema("main", comment="c",
                  tables=[F.table("main", "t"), F.table("main", "t2")])
    cat2 = F.catalog(s2)
    cat2.data_version = "2.0.0"

    comp = comparison.build([
        _Result(cat1, [_finding("VGI112")], 70),
        _Result(cat2, [_finding("VGI112")], 80),
    ])
    assert [r.data_version for r in comp.rows] == ["1.0.0", "2.0.0"]
    assert comp.rows[1].delta_score == 10
    assert "v.main.t2" in comp.rows[1].added_objects
