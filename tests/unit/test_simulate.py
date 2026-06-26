"""Tests for `vgi-lint simulate` — engine, oracle tiers, guard, no-leak, cache.

All offline: a fake backend emits scripted JSON and a fake DB returns canned
result sets keyed by SQL content. No real model or worker is contacted.
"""

import json
import threading

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
        if "boom" in sql:
            raise RuntimeError('Binder Error: referenced column "x" does not exist')
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
    """Emits scripted completions; records every prompt it saw (thread-safe)."""

    def __init__(self, script):
        self.script, self.i, self.prompts = script, 0, []
        self._lock = threading.Lock()

    def complete(self, prompt):
        with self._lock:
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
                    "fit",
                    description="fit a model",
                    parameters=["data", "estimator", "target"],
                    arguments=[
                        F.arg("data", "TABLE", "the training data", is_table_input=True),
                        F.arg("estimator", "VARCHAR", "the estimator name", is_named=True),
                        F.arg("target", "VARCHAR", "the label column", is_named=True),
                    ],
                ),
                F.func(
                    "main",
                    "convert",
                    description="convert a value",
                    parameters=["v", "unit"],
                    arguments=[
                        F.arg("v", "DOUBLE", "the value", is_positional=True),
                        F.arg("unit", "VARCHAR", "the unit", is_positional=True),
                    ],
                ),
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
    assert {f["name"] for f in schema["functions"]} == {"fit", "convert"}

    desc = sim.tool_describe_table(cat, "main", "things")
    assert desc["columns"] == [{"name": "id", "type": "INTEGER", "comment": "the id"}]
    assert "error" in sim.tool_describe_table(cat, "main", "nope")

    # positional function: usage names the args in order
    conv = sim.tool_describe_function(cat, "main", "convert")
    assert conv["arguments"][0]["calling"] == "positional"
    assert conv["usage"] == f"{cat.qualifier}.main.convert(v, unit)"

    # mixed table-input + named function: usage makes the := convention explicit
    fit = sim.tool_describe_function(cat, "main", "fit")
    callings = [a["calling"] for a in fit["arguments"]]
    assert callings == ["table", "named", "named"]
    assert fit["usage"] == (
        f"{cat.qualifier}.main.fit(<table-or-subquery>, estimator := <VARCHAR>, "
        "target := <VARCHAR>)"
    )
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
# Path scoring + suggestions (the discoverability signal)
# --------------------------------------------------------------------------
def test_clean_path_scores_100():
    task = AgentTask(
        name="t",
        prompt="p",
        reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
    )
    backend = _Backend([json.dumps({"action": "final", "answer_sql": "SELECT 'strong' AS band"})])
    rep = sim.simulate_tasks(_catalog_with_tasks([task]), _Con(), backend)
    v = rep.verdicts[0]
    assert v.outcome == "pass" and v.path.score == 100 and v.suggestions == []
    assert rep.discoverability == 100


def test_path_penalizes_bind_error_and_reinspection():
    task = AgentTask(
        name="t",
        prompt="p",
        reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
    )
    backend = _Backend(
        [
            json.dumps({"action": "describe_table", "schema": "main", "table": "things"}),
            json.dumps(
                {"action": "describe_table", "schema": "main", "table": "things"}
            ),  # redundant
            json.dumps({"action": "run_sql", "sql": "SELECT boom"}),  # bind error
            json.dumps({"action": "final", "answer_sql": "SELECT 'strong' AS band"}),
        ]
    )
    rep = sim.simulate_tasks(_catalog_with_tasks([task]), _Con(), backend)
    v = rep.verdicts[0]
    assert v.outcome == "pass"  # still solved...
    assert v.path.bind_errors == 1 and v.path.redundant_describes == 1
    assert v.path.score == 100 - 15 - 10  # bind + re-inspection penalties
    joined = " ".join(v.suggestions)
    assert "failed to bind" in joined and "re-inspected" in joined
    assert any("re-inspected" in s for s in rep.suggestions)


def test_compute_path_metrics_ceiling_and_requirement():
    run = sim.TaskRun(
        steps=[
            sim.TaskStep(
                sql="SELECT 1", ok=False, error="WHERE clause is required", error_kind="requirement"
            )
        ],
        answer_sql=[],
        answer_summary="",
        friction=[],
        discovery=[sim.TraceEvent(kind="describe_table", target="nope", found=False)],
        hit_ceiling=True,
    )
    m = sim.compute_path_metrics(run)
    assert m.requirement_errors == 1 and m.not_found == 1 and m.hit_ceiling
    assert m.score == max(0, 100 - 15 - 8 - 40)
    sugg = sim.build_suggestions(run, m)
    assert any("never converged" in s for s in sugg)
    assert any("usage requirement" in s for s in sugg)


# --------------------------------------------------------------------------
# Suite coverage
# --------------------------------------------------------------------------
def test_compute_coverage_flags_untested_functions():
    cat = F.catalog(
        F.schema(
            "main",
            comment="c",
            tags=_TAGS,
            functions=[
                F.func("main", "convert", description="d"),
                F.func("main", "to_base", description="d"),
                F.func("main", "dimension", description="d"),
            ],
        )
    )
    cat.agent_test_tasks = [
        AgentTask(
            name="t",
            prompt="p",
            reference_statements=[ExampleStatement(None, "SELECT v.main.convert(1,'mi','km')")],
            check_sql="SELECT v.main.dimension('mi') IS NOT NULL",
        )
    ]
    cov = sim.compute_coverage(cat)
    # convert (reference) + dimension (check_sql) are covered; to_base is not
    assert any(c.endswith(".convert") for c in cov.covered)
    assert any(c.endswith(".dimension") for c in cov.covered)
    assert cov.uncovered == [f"{cat.qualifier}.main.to_base"]
    assert cov.total == 3 and cov.pct == 67


def test_report_renders_coverage():
    cat = F.catalog(
        F.schema(
            "main",
            comment="c",
            tags=_TAGS,
            tables=[F.table("main", "things", comment="c")],
            functions=[
                F.func("main", "used", description="d"),
                F.func("main", "skipped", description="d"),
            ],
        )
    )
    cat.agent_test_tasks = [
        AgentTask(
            name="t",
            prompt="p",
            reference_statements=[ExampleStatement(None, "SELECT v.main.used()")],
        )
    ]
    backend = _Backend([json.dumps({"action": "final", "answer_sql": "SELECT v.main.used()"})])
    rep = sim.simulate_tasks(cat, _Con(), backend)
    # objects = used + skipped functions + the untouched `things` table -> 1/3 covered
    assert rep.coverage.covered == [f"{cat.qualifier}.main.used"]
    assert rep.coverage.total == 3 and rep.coverage.pct == 33
    txt = sim.render_terminal(rep)
    assert "object coverage 1/3 (33%)" in txt and "untested" in txt
    doc = json.loads(sim.render_json(rep))
    assert doc["coverage"]["pct"] == 33
    assert {u.rsplit(".", 1)[1] for u in doc["coverage"]["uncovered"]} == {"skipped", "things"}


def test_verify_references_flags_error_and_nondeterminism():
    good = AgentTask(
        name="good",
        prompt="p",
        reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
    )
    broken = AgentTask(
        name="broken",
        prompt="p",
        reference_statements=[ExampleStatement(None, "SELECT boom")],  # raises -> error
    )
    noderef = AgentTask(name="nodef", prompt="p", check_sql="SELECT check_pass")  # no reference
    rep = sim.verify_references(_catalog_with_tasks([good, broken, noderef]), _Con(), runs=3)
    by = {c.name: c.status for c in rep.checks}
    assert by == {"good": "ok", "broken": "error", "nodef": "no-reference"}
    assert rep.ok is False  # the broken one fails the gate
    txt = sim.render_verify(rep)
    assert "references sound" in txt and "broken" in txt


def test_verify_references_detects_nondeterministic():
    # a backend-less reference whose result flips every call -> nondeterministic
    class _Flip:
        def __init__(self):
            self.n = 0

        def cursor(self):
            return self

        def execute(self, sql):
            self.n += 1
            return _Result([(self.n,)], ["x"])  # different value each run

    task = AgentTask(
        name="flip",
        prompt="p",
        reference_statements=[ExampleStatement(None, "SELECT x")],
    )
    rep = sim.verify_references(_catalog_with_tasks([task]), _Flip(), runs=3)
    assert rep.checks[0].status == "nondeterministic" and not rep.ok


def test_suggest_is_batched_and_covers_targets():
    cat = F.catalog(
        F.schema(
            "main",
            comment="c",
            tags=_TAGS,
            functions=[
                F.func("main", "fa", description="d"),
                F.func("main", "fb", description="d"),
            ],
        )
    )
    cat.agent_test_tasks = []
    # one task per call, each covering one target function
    backend = _Backend(
        [
            json.dumps([{"name": "t_fa", "prompt": "p", "reference_sql": "SELECT v.main.fa()"}]),
            json.dumps([{"name": "t_fb", "prompt": "p", "reference_sql": "SELECT v.main.fb()"}]),
        ]
    )
    out = json.loads(sim.suggest_tasks(cat, backend))
    assert {t["name"] for t in out} == {"t_fa", "t_fb"}  # both rounds accumulated
    assert any("TARGET objects" in p for p in backend.prompts)


def test_tasks_run_in_parallel_preserving_order():
    # 3 distinct tasks, all solvable by the same fixed final answer; concurrency>1.
    tasks = [
        AgentTask(
            name=f"t{i}",
            prompt=f"p{i}",
            reference_statements=[ExampleStatement(None, "SELECT 'strong' AS band")],
        )
        for i in range(3)
    ]
    backend = _Backend([json.dumps({"action": "final", "answer_sql": "SELECT 'strong' AS band"})])
    rep = sim.simulate_tasks(
        _catalog_with_tasks(tasks), _Con(), backend, limits=sim.SimLimits(concurrency=3)
    )
    assert [v.name for v in rep.verdicts] == ["t0", "t1", "t2"]  # declaration order preserved
    assert all(v.outcome == "pass" for v in rep.verdicts) and rep.judged == 3


def test_suggest_respects_cap():
    cat = F.catalog(
        F.schema(
            "main",
            comment="c",
            tags=_TAGS,
            functions=[F.func("main", f"f{i}", description="d") for i in range(4)],
        )
    )
    cat.agent_test_tasks = []
    # each call proposes a task covering one more function; cap stops accumulation at 2
    backend = _Backend(
        [
            json.dumps([{"name": f"t{i}", "prompt": "p", "reference_sql": f"SELECT v.main.f{i}()"}])
            for i in range(4)
        ]
    )
    out = json.loads(sim.suggest_tasks(cat, backend, cap=2))
    assert len(out) == 2
    assert any("TARGET objects" in p for p in backend.prompts)


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
    assert "suitability" in txt and "classify" in txt and "discoverability" in txt
    assert "path 100/100" in txt
    doc = json.loads(sim.render_json(rep))
    assert doc["tool"] == "vgi-lint simulate" and doc["verdicts"][0]["outcome"] == "pass"
    assert doc["discoverability"] == 100 and doc["verdicts"][0]["path"]["score"] == 100
