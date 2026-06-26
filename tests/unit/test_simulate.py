"""Tests for `vgi-lint simulate` — engine, oracle tiers, guard, no-leak, cache.

All offline: a fake backend emits scripted JSON and a fake DB returns canned
result sets keyed by SQL content. No real model or worker is contacted.
"""

import json

from tests import fixtures as F
from vgi_lint_check import simulate as sim
from vgi_lint_check.model import AgentTask, ExampleStatement
from vgi_lint_check.review import ReviewCache
from vgi_lint_check.rules._util import safe_session_sql

_TAGS = {"vgi.doc_llm": "x" * 180, "vgi.doc_md": "y" * 180, "provider": "a", "domain": "b"}


# --------------------------------------------------------------------------
# safe_session_sql guard
# --------------------------------------------------------------------------
def test_safe_session_sql_allows_session_local_and_blocks_escape():
    for ok in [
        "SELECT 1",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT 1",
        "SET threads=2",
        "PRAGMA database_list",
        "CREATE TEMP VIEW v AS SELECT 1",
        "CREATE OR REPLACE TEMPORARY TABLE t AS SELECT 1",
        "FROM tbl SELECT *",
    ]:
        assert safe_session_sql(ok), ok
    for bad in [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x=1",
        "DELETE FROM t",
        "ATTACH 'x.db'",
        "INSTALL spatial",
        "LOAD spatial",
        "COPY t TO 'f.csv'",
        "CREATE TABLE t AS SELECT 1",  # non-temp
        "CREATE VIEW v AS SELECT 1",  # non-temp
        "SELECT 1; DROP TABLE t",  # multi-statement
        "DETACH w",
        "",
    ]:
        assert not safe_session_sql(bad), bad


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _Result:
    def __init__(self, rows, cols):
        self._rows, self.description = rows, [(c,) for c in cols]

    def fetchall(self):
        return self._rows


class _Con:
    """Returns canned result sets keyed by SQL substring; new cursor = same fake."""

    def cursor(self):
        return _Con()

    def execute(self, sql):
        if "strong" in sql:
            return _Result([("strong",)], ["band"])
        if "weak" in sql:
            return _Result([("weak",)], ["band"])
        if "check_pass" in sql:
            return _Result([(True,)], ["ok"])
        if "check_fail" in sql:
            return _Result([(False,)], ["ok"])
        return _Result([(1,)], ["n"])


class _Backend:
    """Emits scripted completions; records every prompt it saw."""

    def __init__(self, script):
        self.script, self.i, self.prompts = script, 0, []

    def complete(self, prompt):
        self.prompts.append(prompt)
        out = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return out


def _catalog_with_tasks(tasks):
    cat = F.catalog(
        F.schema("main", comment="c", tags=_TAGS, tables=[F.table("main", "things", comment="c")])
    )
    cat.agent_test_tasks = tasks
    return cat


# --------------------------------------------------------------------------
# Overview never leaks the solution
# --------------------------------------------------------------------------
def test_overview_excludes_solution_fields():
    task = AgentTask(
        name="t",
        prompt="classify the band",
        success_criteria="SECRET_CRITERIA",
        reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
        check_sql="SELECT check_pass",
    )
    cat = _catalog_with_tasks([task])
    listing = sim.build_listing(cat)
    assert "classify the band" not in listing  # not even the prompt is in the listing
    assert "SECRET_CRITERIA" not in listing
    assert "SELECT 'strong'" not in listing
    assert "check_pass" not in listing


# --------------------------------------------------------------------------
# Discovery tools — local mirror of the ask-AI contract
# --------------------------------------------------------------------------
def test_tool_list_tables_and_describe():
    cat = F.catalog(
        F.schema(
            "main",
            comment="c",
            tags=_TAGS,
            tables=[
                F.table(
                    "main",
                    "things",
                    comment="all the things",
                    columns=[F.col("main", "things", "id", comment="the id", dtype="INTEGER")],
                )
            ],
            functions=[
                F.func(
                    "main",
                    "convert",
                    description="convert a value",
                    parameters=["v", "unit"],
                    arguments=[F.arg("v", "DOUBLE", "the value")],
                )
            ],
        )
    )
    listed = sim.tool_list_tables(cat)
    assert listed["catalog"] == cat.qualifier
    schema = listed["schemas"][0]
    assert schema["tables"][0] == {
        "name": "things",
        "type": str(cat.iter_tables().__next__().kind),
        "comment": "all the things",
        "column_count": 1,
    }
    assert schema["functions"][0]["name"] == "convert"

    desc = sim.tool_describe_table(cat, "main", "things")
    assert desc["columns"] == [{"name": "id", "type": "INTEGER", "comment": "the id"}]
    assert "error" in sim.tool_describe_table(cat, "main", "nope")

    fn = sim.tool_describe_function(cat, "main", "convert")
    assert fn["parameters"] == ["v", "unit"]
    assert fn["arguments"][0] == {"name": "v", "type": "DOUBLE", "description": "the value"}
    assert "error" in sim.tool_describe_function(cat, "main", "nope")


def test_tool_run_sql_guard_and_result():
    blocked = sim.tool_run_sql(_Con(), "INSERT INTO t VALUES (1)", sim.SimLimits())
    assert not blocked["ok"] and blocked["error"].startswith("blocked")
    ok = sim.tool_run_sql(_Con(), "SELECT 1", sim.SimLimits())
    assert ok["ok"] and ok["row_count"] == 1 and ok["columns"] == ["n"]


def test_actor_uses_discovery_tools():
    task = AgentTask(
        name="classify",
        prompt="classify",
        reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
    )
    backend = _Backend(
        [
            json.dumps({"action": "list_tables"}),
            json.dumps({"action": "describe_table", "schema": "main", "table": "things"}),
            json.dumps({"action": "run_sql", "sql": "SELECT band FROM things"}),
            json.dumps({"action": "final", "answer_sql": "SELECT 'strong' AS band"}),
        ]
    )
    rep = sim.simulate_tasks(_catalog_with_tasks([task]), _Con(), backend)
    assert rep.verdicts[0].outcome == "pass"
    # the model saw the tool menu and a discovery trail accumulated
    assert any("list_tables" in p for p in backend.prompts)
    assert any("DISCOVERY SO FAR:\n[0] list_tables" in p for p in backend.prompts)


# --------------------------------------------------------------------------
# Tier 1 — reference comparison
# --------------------------------------------------------------------------
def test_tier1_reference_pass_and_fail():
    task = AgentTask(
        name="classify",
        prompt="classify",
        reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
    )
    cat = _catalog_with_tasks([task])

    # actor explores then returns an answer query that yields 'strong' -> pass
    backend = _Backend(
        [
            json.dumps({"action": "run_sql", "sql": "SELECT band FROM things"}),
            json.dumps(
                {
                    "action": "final",
                    "answer_sql": "SELECT 'strong' AS band",
                    "answer_summary": "strong",
                    "friction": ["no example for classify"],
                }
            ),
        ]
    )
    rep = sim.simulate_tasks(cat, _Con(), backend)
    v = rep.verdicts[0]
    assert v.outcome == "pass" and v.grader == "reference"
    assert v.friction == ["no example for classify"]
    # the reference SQL never appears in any actor prompt
    assert all("SELECT 'strong'" not in p for p in backend.prompts if "Grade whether" not in p)

    # answer yields 'weak' -> fail
    backend2 = _Backend([json.dumps({"action": "final", "answer_sql": "SELECT 'weak' AS band"})])
    rep2 = sim.simulate_tasks(_catalog_with_tasks([task]), _Con(), backend2)
    assert rep2.verdicts[0].outcome == "fail"


# --------------------------------------------------------------------------
# Tier 2 — check_sql assertion
# --------------------------------------------------------------------------
def test_resultsets_equal_strict_and_opt_out():
    same_vals_diff_names_a = (["dimension", "n"], [("length", 46)])
    same_vals_diff_names_b = (["dim", "count"], [("length", 46)])
    # strict default: different column names -> NOT equal (reference is the contract)
    assert not sim._resultsets_equal(
        same_vals_diff_names_a, same_vals_diff_names_b, unordered=False, ignore_column_names=False
    )
    # opt-out: ignore_column_names -> equal by values
    assert sim._resultsets_equal(
        same_vals_diff_names_a, same_vals_diff_names_b, unordered=False, ignore_column_names=True
    )
    # same names + values -> equal
    assert sim._resultsets_equal(
        same_vals_diff_names_a,
        (["dimension", "n"], [("length", 46)]),
        unordered=False,
        ignore_column_names=False,
    )
    # different values -> not equal
    assert not sim._resultsets_equal(
        (["a"], [("length",)]), (["a"], [("mass",)]), unordered=False, ignore_column_names=False
    )
    # row order matters unless unordered
    a = (["x"], [(1,), (2,)])
    b = (["x"], [(2,), (1,)])
    assert not sim._resultsets_equal(a, b, unordered=False, ignore_column_names=False)
    assert sim._resultsets_equal(a, b, unordered=True, ignore_column_names=False)


def test_tier2_check_sql():
    cat = _catalog_with_tasks([AgentTask(name="c", prompt="do", check_sql="SELECT check_pass")])
    backend = _Backend([json.dumps({"action": "final", "answer_sql": "SELECT 1"})])
    assert sim.simulate_tasks(cat, _Con(), backend).verdicts[0].outcome == "pass"

    cat2 = _catalog_with_tasks([AgentTask(name="c", prompt="do", check_sql="SELECT check_fail")])
    backend2 = _Backend([json.dumps({"action": "final", "answer_sql": "SELECT 1"})])
    assert sim.simulate_tasks(cat2, _Con(), backend2).verdicts[0].outcome == "fail"


# --------------------------------------------------------------------------
# Tier 3 — LLM judge fallback
# --------------------------------------------------------------------------
def test_tier3_judge():
    cat = _catalog_with_tasks([AgentTask(name="j", prompt="do", success_criteria="should be 1")])
    # actor finishes, then the judge call returns a verdict
    backend = _Backend(
        [
            json.dumps({"action": "final", "answer_sql": "SELECT 1", "answer_summary": "it is 1"}),
            json.dumps({"outcome": "pass", "reason": "matches"}),
        ]
    )
    v = sim.simulate_tasks(cat, _Con(), backend).verdicts[0]
    assert v.outcome == "pass" and v.grader == "judge"


# --------------------------------------------------------------------------
# Cache reuse
# --------------------------------------------------------------------------
def test_cache_reuse(tmp_path):
    task = AgentTask(
        name="classify",
        prompt="classify",
        reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
    )
    script = [json.dumps({"action": "final", "answer_sql": "SELECT 'strong' AS band"})]
    cache = ReviewCache(tmp_path / "c.json").load()
    r1 = sim.simulate_tasks(_catalog_with_tasks([task]), _Con(), _Backend(script), cache=cache)
    assert r1.judged == 1 and r1.cached == 0

    cache2 = ReviewCache(tmp_path / "c.json").load()
    b2 = _Backend(script)
    r2 = sim.simulate_tasks(_catalog_with_tasks([task]), _Con(), b2, cache=cache2)
    assert r2.cached == 1 and r2.judged == 0
    assert b2.prompts == []  # cache hit -> no model calls


# --------------------------------------------------------------------------
# Suggest mode + rendering
# --------------------------------------------------------------------------
def test_suggest_emits_json():
    cat = _catalog_with_tasks([])
    proposed = [{"name": "t1", "prompt": "p1", "reference_sql": "SELECT 1"}]
    backend = _Backend([json.dumps(proposed)])
    out = sim.suggest_tasks(cat, backend, 1)
    assert json.loads(out) == proposed


def test_render_terminal_and_json():
    task = AgentTask(
        name="classify",
        prompt="classify",
        reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
    )
    backend = _Backend([json.dumps({"action": "final", "answer_sql": "SELECT 'strong' AS band"})])
    rep = sim.simulate_tasks(_catalog_with_tasks([task]), _Con(), backend)
    txt = sim.render_terminal(rep)
    assert "suitability" in txt and "classify" in txt
    doc = json.loads(sim.render_json(rep))
    assert doc["tool"] == "vgi-lint simulate" and doc["verdicts"][0]["outcome"] == "pass"
