"""Tests for vgi.executable_examples: decoding, VGI507/906/907, result match."""

from tests import fixtures as F
from vgi_lint_check.config import Config
from vgi_lint_check.findings import Severity
from vgi_lint_check.model import TagSet
from vgi_lint_check.rules import run, select_rules
from vgi_lint_check.rules.base import RuleContext
from vgi_lint_check.rules.execution import (
    ExecutableExampleResultMatches,
    ExecutableExamplesExecute,
    _result_matches,
)
from vgi_lint_check.tags import decode_executable_examples

_SCHEMA_TAGS = {
    "vgi.description_llm": "Zoo domain for LLM use, with enough length here.",
    "vgi.description_md": "## Zoo\nAnimals and attributes — full reference, long enough.",
    "provider": "acme",
    "domain": "zoo",
}


# --- decoder --------------------------------------------------------------
def test_decode_sql_polymorphism():
    tag = """[
      {"description": "scalar", "sql": "SELECT 1"},
      {"description": "list of strings", "sql": ["SET x=1", "SELECT 2"]},
      {"description": "list of steps", "sql": [{"description": "step", "sql": "SELECT 3"}]}
    ]"""
    examples, err = decode_executable_examples(TagSet({"vgi.executable_examples": tag}))
    assert err is None
    assert [len(e.statements) for e in examples] == [1, 2, 1]
    assert examples[0].statements[0].sql == "SELECT 1"
    assert examples[1].statements[1].sql == "SELECT 2"
    assert examples[2].statements[0].description == "step"


def test_decode_expected_result_per_statement():
    # expected_result lives on a statement, so multi-statement examples can
    # assert any step's output.
    tag = """[{"description":"d","sql":[
      {"description":"setup","sql":"SET x=1"},
      {"description":"check","sql":"SELECT 1 AS x","expected_result":[{"x":1}]}
    ]}]"""
    examples, err = decode_executable_examples(TagSet({"vgi.executable_examples": tag}))
    assert err is None
    stmts = examples[0].statements
    assert stmts[0].has_expected is False  # setup has no expectation
    assert stmts[1].has_expected is True
    assert stmts[1].expected_result == [{"x": 1}]


def test_decode_errors():
    bad = decode_executable_examples(TagSet({"vgi.executable_examples": "not json"}))
    assert bad[0] == [] and "invalid JSON" in bad[1]
    not_list = decode_executable_examples(TagSet({"vgi.executable_examples": '{"a":1}'}))
    assert "list" in not_list[1]
    absent = decode_executable_examples(TagSet({}))
    assert absent == ([], None)


# --- VGI507 (static well-formedness) --------------------------------------
def _lint(cat, **kw):
    cfg = Config(**kw)
    return [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]


def test_vgi507_flags_malformed_and_incomplete():
    good = F.exec_example(0, "classify a quake", [("step", "SELECT v.main.f(6)")])
    no_desc = F.exec_example(1, None, [("step", "SELECT 1")])
    no_sql = F.exec_example(2, "has no sql", [(None, "")])
    fn = F.func("main", "f", description="d", executable_examples=[good, no_desc, no_sql])
    bad_fn = F.func("main", "g", description="d", exec_parse_error="invalid JSON: x")
    s = F.schema("main", comment="c", tags=_SCHEMA_TAGS, functions=[fn, bad_fn])
    codes = _lint(F.catalog(s))
    assert codes.count("VGI507") == 3  # malformed tag + missing desc + missing sql
    # the well-formed example produced no VGI507


def test_vgi508_limits_example_count():
    from vgi_lint_check.config import Options

    many = [F.exec_example(i, f"ex {i}", [("s", "SELECT 1")]) for i in range(4)]
    fn = F.func("main", "f", description="d", executable_examples=many)
    s = F.schema("main", comment="c", tags=_SCHEMA_TAGS, functions=[fn])
    cat = F.catalog(s)
    # default cap (10) -> not flagged
    assert "VGI508" not in _lint(cat)
    # lower the cap -> flagged
    cfg = Config()
    cfg.options = Options(max_executable_examples=2)
    codes = [f.code for f in run(select_rules(cfg), RuleContext(cat, cfg))]
    assert "VGI508" in codes


# --- VGI906 / VGI907 (execution + result) ---------------------------------
class FakeResult:
    def __init__(self, rows, cols):
        self._rows, self.description = rows, [(c,) for c in cols]

    def fetchall(self):
        return self._rows


class RecordingCon:
    """Runs SQL; raises for any statement containing 'BOOM'; returns canned rows."""

    def __init__(self, rows=None, cols=None):
        self.rows, self.cols, self.ran = rows or [], cols or [], []

    def execute(self, sql):
        self.ran.append(sql)
        if "BOOM" in sql:
            raise RuntimeError("Binder Error: nope")
        return FakeResult(self.rows, self.cols)


def _run(rule, cat, con):
    cfg = Config(execute=True)
    ctx = RuleContext(cat, cfg, connection=con)
    ctx.severity = Severity.ERROR
    return list(rule.check(ctx))


def test_vgi906_runs_all_statements_and_flags_failure():
    ok = F.exec_example(0, "ok", [("a", "SELECT 1"), ("b", "SELECT 2")])
    broken = F.exec_example(1, "broken", [("a", "SELECT BOOM")], name="boom-example")
    fn = F.func("main", "f", description="d", executable_examples=[ok, broken])
    s = F.schema("main", comment="c", tags=_SCHEMA_TAGS, functions=[fn])
    con = RecordingCon()
    out = _run(ExecutableExamplesExecute(), F.catalog(s), con)
    assert len(out) == 1 and out[0].code == "VGI906"
    assert "boom-example" in out[0].message
    # both statements of the ok example were executed
    assert "SELECT 1" in con.ran and "SELECT 2" in con.ran


def test_vgi907_compares_expected_result():
    match = F.exec_example(0, "match", [("a", "SELECT 'strong' AS class", [{"class": "strong"}])])
    fn = F.func("main", "f", description="d", executable_examples=[match])
    s = F.schema("main", comment="c", tags=_SCHEMA_TAGS, functions=[fn])
    con = RecordingCon(rows=[("strong",)], cols=["class"])
    assert _run(ExecutableExampleResultMatches(), F.catalog(s), con) == []

    mismatch = F.exec_example(
        0, "mismatch", [("a", "SELECT 'weak' AS class", [{"class": "strong"}])]
    )
    fn2 = F.func("main", "f", description="d", executable_examples=[mismatch])
    s2 = F.schema("main", comment="c", tags=_SCHEMA_TAGS, functions=[fn2])
    con2 = RecordingCon(rows=[("weak",)], cols=["class"])
    out = _run(ExecutableExampleResultMatches(), F.catalog(s2), con2)
    assert len(out) == 1 and out[0].code == "VGI907"


# --- result matcher units -------------------------------------------------
def test_result_matches_shapes():
    # scalar vs 1x1
    assert _result_matches("strong", ["class"], [("strong",)])
    assert not _result_matches("weak", ["class"], [("strong",)])
    # numbers compare as strings (6 == "6")
    assert _result_matches(6, ["n"], [(6,)])
    # list of row objects
    assert _result_matches([{"a": 1, "b": "x"}], ["a", "b"], [(1, "x")])
    # list of rows (lists)
    assert _result_matches([[1, "x"], [2, "y"]], ["a", "b"], [(1, "x"), (2, "y")])
    # list of scalars, single column
    assert _result_matches([1, 2, 3], ["n"], [(1,), (2,), (3,)])
    # single row, multiple columns
    assert _result_matches([1, "x"], ["a", "b"], [(1, "x")])
