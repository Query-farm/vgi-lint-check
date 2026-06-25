"""Agent-suitability simulation: can a SQL analyst accomplish real tasks here?

`vgi-lint simulate` runs an LLM "analyst" through each task a worker declares in
``vgi.agent_test_tasks``. For each task the analyst sees only the catalog overview
(what's actually exposed) and the task *prompt* — never the solution — and drives
a bounded ReAct loop: it writes read-only / session-local SQL, vgi-lint executes
it against the live worker, feeds results back, and it iterates until it answers.

Grading is execution-based and layered (strongest available wins):
1. reference solution → compare terminal result set(s) (deterministic),
2. ``check_sql`` assertion over the analyst's post-session state,
3. an LLM judge against ``success_criteria`` (soft fallback),
4. else: a final answer backed by ≥1 successful query.

The same machinery powers ``--suggest`` (propose candidate tasks for authors) and
reuses the ``review`` LLM backends (Claude CLI on your subscription by default).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .model import TAG_DOC_LLM, AgentTask, Catalog, ObjectKind
from .review import ReviewBackend, ReviewCache  # backends + JSON verdict cache
from .rules._util import run_with_timeout, safe_session_sql
from .rules.execution import _render_result  # canonical result rendering (VGI907)

_ANSWER_LIMIT = 1000  # row cap when materializing the answer / reference for grading
_VERB = re.compile(r"\s*([A-Za-z_]+)")
_WRAPPABLE = frozenset({"select", "with", "values", "table", "from"})


# --------------------------------------------------------------------------
# Limits / results
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SimLimits:
    """Per-task bounds for the analyst loop."""

    max_steps: int = 6
    max_queries: int = 8
    attempts: int = 1
    timeout: float = 30.0
    row_limit: int = 50


@dataclass
class TaskStep:
    """One analyst action and what came back."""

    sql: str
    ok: bool
    error: str | None = None
    cols: list[str] = field(default_factory=list)
    rows: list[Any] = field(default_factory=list)
    blocked: bool = False  # rejected by the safe-SQL guard


@dataclass
class TaskRun:
    """The transcript of one attempt at a task."""

    steps: list[TaskStep]
    answer_sql: list[str]
    answer_summary: str
    friction: list[str]


@dataclass
class TaskVerdict:
    """The graded outcome of a task."""

    name: str
    outcome: str  # pass | partial | fail
    reason: str
    friction: list[str] = field(default_factory=list)
    queries: int = 0
    grader: str = ""  # reference | check_sql | judge | answered

    @property
    def passed(self) -> bool:
        """True for a full pass (partial/fail are not)."""
        return self.outcome == "pass"


@dataclass
class SimReport:
    """Result of simulating a worker's declared task suite."""

    location: str
    backend: str
    verdicts: list[TaskVerdict]
    judged: int
    cached: int

    @property
    def pass_rate(self) -> float:
        """Fraction of tasks that fully passed."""
        if not self.verdicts:
            return 0.0
        return round(sum(v.passed for v in self.verdicts) / len(self.verdicts), 2)

    @property
    def score(self) -> int:
        """Agent-suitability score 0-100 (pass=1, partial=0.5)."""
        if not self.verdicts:
            return 0
        pts = sum(
            1.0 if v.outcome == "pass" else 0.5 if v.outcome == "partial" else 0
            for v in self.verdicts
        )
        return round(100 * pts / len(self.verdicts))


# --------------------------------------------------------------------------
# Catalog overview (what the analyst sees — never the solution)
# --------------------------------------------------------------------------
def build_overview(catalog: Catalog) -> str:
    """Compact text of exactly what an analyst sees post-attach.

    Includes schemas/tables/columns/functions and *illustrative* example queries.
    Excludes ``vgi.agent_test_tasks`` solutions entirely — the analyst must
    rediscover the path from metadata.
    """
    out: list[str] = [f"Catalog: {catalog.qualifier}"]
    if catalog.description_llm or catalog.comment:
        out.append(f"  {(catalog.description_llm or catalog.comment or '').strip()[:400]}")
    for s in catalog.iter_schemas():
        sd = (s.tags.get(TAG_DOC_LLM) or s.comment or "").strip()
        out.append(f"\nSchema {catalog.qualifier}.{s.name}" + (f" — {sd[:200]}" if sd else ""))
        for t in (*s.tables, *s.views):
            td = (t.description_llm or t.comment or "").strip()
            out.append(
                f"  {t.kind} {catalog.qualifier}.{s.name}.{t.name}"
                + (f" — {td[:200]}" if td else "")
            )
            for c in t.columns:
                cc = f" — {c.comment.strip()[:120]}" if c.comment else ""
                out.append(f"      {c.name} {c.data_type or ''}{cc}")
            for ex in t.examples[:2]:
                if ex.sql:
                    out.append(f"      e.g. {ex.sql.strip()[:160]}")
        for f in s.functions:
            if f.kind is ObjectKind.TABLE_FUNCTION and catalog.find_table_like(f.name, f.schema):
                continue  # documented via its backing table
            sig = ", ".join(f.parameters) if f.parameters else ""
            fd = (f.description or f.comment or "").strip()
            out.append(
                f"  {f.function_type} {catalog.qualifier}.{s.name}.{f.name}({sig})"
                + (f" — {fd[:200]}" if fd else "")
            )
    return "\n".join(out)


# --------------------------------------------------------------------------
# JSON parsing of model actions
# --------------------------------------------------------------------------
def _extract_json(text: str) -> Any:
    """Extract the first balanced JSON object/array from a model completion."""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in model output")
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unterminated JSON in model output")


def _as_sql_list(value: Any) -> list[str]:
    """Coerce an answer_sql value (string or list of strings) to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return []


# --------------------------------------------------------------------------
# Executing SQL during the loop / grading
# --------------------------------------------------------------------------
def _wrap_limit(sql: str, limit: int) -> str:
    """Bound a row-returning query with LIMIT; leave SET/PRAGMA/DDL/EXPLAIN as-is."""
    body = sql.strip().rstrip(";")
    m = _VERB.match(body)
    verb = m.group(1).lower() if m else ""
    if verb in _WRAPPABLE:
        return f"SELECT * FROM ({body}) AS _vgi_sim LIMIT {int(limit)}"
    return body


def _run(cur: Any, sql: str, timeout: float) -> tuple[list[str], list[Any]]:
    """Execute ``sql`` on ``cur`` under the timeout; return (cols, rows)."""
    result = run_with_timeout(cur, lambda: cur.execute(sql), timeout)
    rows = run_with_timeout(cur, lambda r=result: r.fetchall(), timeout)
    cols = [d[0] for d in result.description] if result.description else []
    return cols, list(rows or [])


def _resultsets_equal(
    a: tuple[list[str], list[Any]], b: tuple[list[str], list[Any]], unordered: bool
) -> bool:
    """Compare two live result sets by VALUES, ignoring column names.

    The analyst's column aliases are cosmetic; what's graded is the data.
    Position- and (unless ``unordered``) row-order-sensitive, since most analyst
    tasks are ranked/top-N. Passing empty cols to ``_render_result`` yields plain
    value lists rather than column-keyed dicts.
    """
    ra, rb = _render_result([], a[1]), _render_result([], b[1])
    if unordered:
        return sorted(map(_canon, ra)) == sorted(map(_canon, rb))
    return ra == rb


def _canon(row: Any) -> str:
    return json.dumps(row, sort_keys=True, default=str)


# --------------------------------------------------------------------------
# The analyst loop + grading
# --------------------------------------------------------------------------
_ACTOR = (
    "You are a SQL analyst working in DuckDB with a data catalog attached. Accomplish the "
    "TASK using ONLY what the catalog exposes. You may run read-only / session-local SQL "
    "(SELECT, WITH, temp views, SET) — one statement per step. Explore as needed, then finish.\n\n"
    "Respond with ONE JSON object and nothing else:\n"
    '  run a query: {"thought":"...","action":"query","sql":"<one SQL statement>"}\n'
    '  when done:   {"thought":"...","action":"final",'
    '"answer_sql":"<SELECT whose result IS the answer>","answer_summary":"...",'
    '"friction":["anything missing/confusing in the metadata, else omit"]}'
)


def run_task(
    catalog: Catalog,
    con: Any,
    backend: ReviewBackend,
    task: AgentTask,
    overview: str,
    limits: SimLimits,
) -> TaskRun:
    """Drive the bounded ReAct loop for one task on its own cursor (state accrues)."""
    cur = con.cursor()
    steps: list[TaskStep] = []
    answer_sql: list[str] = []
    summary = ""
    friction: list[str] = []
    queries = 0
    for _ in range(max(1, limits.max_steps)):
        prompt = (
            f"{_ACTOR}\n\nCATALOG OVERVIEW:\n{overview}\n\nTASK: {task.prompt}\n\n"
            f"TRANSCRIPT SO FAR:\n{_transcript(steps)}"
        )
        try:
            action = _extract_json(backend.complete(prompt))
        except (ValueError, json.JSONDecodeError):
            break
        if not isinstance(action, dict):
            break
        kind = action.get("action")
        if kind == "final":
            answer_sql = _as_sql_list(action.get("answer_sql"))
            summary = str(action.get("answer_summary") or "")
            friction = [str(x) for x in (action.get("friction") or [])]
            break
        if kind != "query":
            break
        sql = str(action.get("sql") or "").strip()
        if not sql:
            break
        if not safe_session_sql(sql):
            steps.append(
                TaskStep(sql=sql, ok=False, blocked=True, error="blocked by read-only guard")
            )
            friction.append(f"attempted a non-read-only statement: {sql[:80]}")
            continue
        queries += 1
        try:
            cols, rows = _run(cur, _wrap_limit(sql, limits.row_limit), limits.timeout)
            steps.append(TaskStep(sql=sql, ok=True, cols=cols, rows=rows))
        except Exception as e:  # noqa: BLE001 - relayed to the analyst as an observation
            steps.append(TaskStep(sql=sql, ok=False, error=f"{type(e).__name__}: {e}"))
        if queries >= limits.max_queries:
            break
    # keep the agent cursor alive on the run for tier-2 grading
    run = TaskRun(steps=steps, answer_sql=answer_sql, answer_summary=summary, friction=friction)
    run._cur = cur  # type: ignore[attr-defined]
    return run


def _transcript(steps: list[TaskStep]) -> str:
    if not steps:
        return "(nothing yet — start exploring)"
    out: list[str] = []
    for i, s in enumerate(steps):
        out.append(f"[{i}] SQL: {s.sql}")
        if s.blocked:
            out.append("    -> blocked (read-only / session-local statements only)")
        elif s.ok:
            out.append(f"    -> {len(s.rows)} row(s); {_render_actual(s.cols, s.rows)}")
        else:
            out.append(f"    -> error: {s.error}")
    return "\n".join(out)


def _render_actual(cols: list[str], rows: list[Any], limit: int = 400) -> str:
    try:
        text = json.dumps(_render_result(cols, rows[:20]), default=str)
    except (TypeError, ValueError):
        text = repr(rows[:20])
    return text if len(text) <= limit else text[:limit] + "…"


_JUDGE = (
    "Grade whether a SQL analyst accomplished a task. Return ONE JSON object: "
    '{{"outcome":"pass|partial|fail","reason":"one line"}}.\n\n'
    "TASK: {task}\nSUCCESS CRITERIA: {criteria}\nANALYST SUMMARY: {summary}\n"
    "ANALYST RESULT: {result}"
)


def grade_task(
    con: Any, backend: ReviewBackend, task: AgentTask, run: TaskRun, limits: SimLimits
) -> TaskVerdict:
    """Grade a run with the strongest available oracle (reference / check / judge)."""
    cur = getattr(run, "_cur", None) or con.cursor()
    queries = sum(1 for s in run.steps if not s.blocked)

    def mk(outcome: str, reason: str, grader: str) -> TaskVerdict:
        return TaskVerdict(
            name=task.name,
            outcome=outcome,
            reason=reason,
            friction=run.friction,
            queries=queries,
            grader=grader,
        )

    # Tier 1 — deterministic: compare the agent's answer to the reference's terminal output.
    if task.reference_statements:
        try:
            expected = _terminal_result(con.cursor(), task.reference_statements, limits.timeout)
        except Exception as e:  # noqa: BLE001 - a broken reference is an authoring bug
            return mk("fail", f"reference_sql failed: {e}", "reference")
        if not run.answer_sql:
            return mk("fail", "analyst gave no answer query", "reference")
        try:
            actual = _run(cur, _wrap_limit(run.answer_sql[0], _ANSWER_LIMIT), limits.timeout)
        except Exception as e:  # noqa: BLE001
            return mk("fail", f"answer query failed: {e}", "reference")
        ok = _resultsets_equal(expected, actual, task.unordered)
        reason = "result matches reference" if ok else "result differs from the reference solution"
        return mk("pass" if ok else "fail", reason, "reference")

    # Tier 2 — assertion over the analyst's post-session state.
    if task.check_sql:
        try:
            _cols, rows = _run(cur, task.check_sql, limits.timeout)
        except Exception as e:  # noqa: BLE001
            return mk("fail", f"check_sql failed: {e}", "check_sql")
        ok = bool(rows and rows[0] and rows[0][0])
        return mk(
            "pass" if ok else "fail",
            "check_sql asserted true" if ok else "check_sql asserted false",
            "check_sql",
        )

    # Tier 3 — soft LLM judge against success_criteria.
    if task.success_criteria:
        result_text = ""
        if run.answer_sql:
            try:
                result_text = _render_actual(
                    *_run(cur, _wrap_limit(run.answer_sql[0], _ANSWER_LIMIT), limits.timeout)
                )
            except Exception:  # noqa: BLE001
                result_text = "(answer query failed)"
        prompt = _JUDGE.format(
            task=task.prompt,
            criteria=task.success_criteria,
            summary=run.answer_summary,
            result=result_text,
        )
        try:
            verdict = _extract_json(backend.complete(prompt))
            outcome = str(verdict.get("outcome", "fail")).lower()
            if outcome not in ("pass", "partial", "fail"):
                outcome = "fail"
            return mk(outcome, str(verdict.get("reason", "")), "judge")
        except (ValueError, json.JSONDecodeError, AttributeError):
            return mk("fail", "judge returned no verdict", "judge")

    # No oracle: pass if a final answer is backed by ≥1 successful query.
    answered = bool(run.answer_sql) and any(s.ok for s in run.steps)
    return mk(
        "pass" if answered else "fail",
        "produced an answer (no reference to verify against)" if answered else "no answer produced",
        "answered",
    )


def _terminal_result(
    cur: Any, statements: list[Any], timeout: float
) -> tuple[list[str], list[Any]]:
    """Run a reference statement sequence in order; return the last row-returning result."""
    last: tuple[list[str], list[Any]] = ([], [])
    for stmt in statements:
        sql = (stmt.sql or "").strip().rstrip(";")
        if not sql:
            continue
        cols, rows = _run(cur, _wrap_limit(sql, _ANSWER_LIMIT), timeout)
        if cols:
            last = (cols, rows)
    return last


# --------------------------------------------------------------------------
# Suite runner + cache
# --------------------------------------------------------------------------
def _task_key(overview: str, task: AgentTask, data_version: str | None) -> str:
    blob = json.dumps([overview, task.raw, data_version], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def simulate_tasks(
    catalog: Catalog,
    con: Any,
    backend: ReviewBackend,
    *,
    backend_name: str = "claude",
    limits: SimLimits | None = None,
    cache: ReviewCache | None = None,
) -> SimReport:
    """Run every declared task (with retries) and aggregate a suitability report."""
    limits = limits or SimLimits()
    overview = build_overview(catalog)
    verdicts: list[TaskVerdict] = []
    judged = cached = 0
    for task in catalog.agent_test_tasks:
        key = _task_key(overview, task, catalog.data_version)
        hit = _cache_get(cache, key)
        if hit is not None:
            verdicts.append(hit)
            cached += 1
            continue
        best: TaskVerdict | None = None
        for _ in range(max(1, limits.attempts)):
            run = run_task(catalog, con, backend, task, overview, limits)
            v = grade_task(con, backend, task, run, limits)
            if best is None or (v.passed and not best.passed):
                best = v
            if v.passed:
                break
        assert best is not None
        verdicts.append(best)
        judged += 1
        _cache_put(cache, key, best)
    if cache:
        cache.save()
    return SimReport(catalog.location, backend_name, verdicts, judged, cached)


def _cache_get(cache: ReviewCache | None, key: str) -> TaskVerdict | None:
    if cache is None:
        return None
    d = cache._data.get(key)  # noqa: SLF001 - shared JSON cache
    return TaskVerdict(**d) if d else None


def _cache_put(cache: ReviewCache | None, key: str, v: TaskVerdict) -> None:
    if cache is not None:
        cache._data[key] = {k: val for k, val in asdict(v).items()}  # noqa: SLF001


# --------------------------------------------------------------------------
# Suggest mode (authoring helper)
# --------------------------------------------------------------------------
_SUGGEST = (
    "Propose {n} realistic analyst tasks an end user would want to accomplish against this "
    "catalog, each solvable with SQL using ONLY what's exposed. Return ONLY a JSON array of "
    '{{"name": "...", "prompt": "...", "reference_sql": "<a correct canonical solution>"}} '
    "objects suitable for the vgi.agent_test_tasks tag.\n\nCATALOG OVERVIEW:\n{overview}"
)


def suggest_tasks(catalog: Catalog, backend: ReviewBackend, n: int = 5) -> str:
    """Ask the LLM to propose candidate tasks as ready-to-paste tag JSON."""
    raw = backend.complete(_SUGGEST.format(n=n, overview=build_overview(catalog)))
    try:
        data = _extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        return raw.strip()
    return json.dumps(data, indent=2)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def render_terminal(report: SimReport) -> str:
    """Human-readable simulation report."""
    out = [
        f"agent simulation  {report.location}  ·  backend={report.backend}  ·  "
        f"judged {report.judged} · cached {report.cached}",
        f"suitability {report.score}/100  ·  pass rate {int(report.pass_rate * 100)}% "
        f"({sum(v.passed for v in report.verdicts)}/{len(report.verdicts)} tasks)",
        "",
    ]
    mark = {"pass": "✓", "partial": "~", "fail": "✗"}
    for v in report.verdicts:
        out.append(
            f"{mark.get(v.outcome, '?')} {v.name}  [{v.outcome}, {v.grader}, {v.queries} queries]"
        )
        if v.reason:
            out.append(f"    ↳ {v.reason}")
        for fr in v.friction:
            out.append(f"    · friction: {fr}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_json(report: SimReport) -> str:
    """Machine-readable simulation report."""
    return json.dumps(
        {
            "tool": "vgi-lint simulate",
            "location": report.location,
            "backend": report.backend,
            "score": report.score,
            "pass_rate": report.pass_rate,
            "judged": report.judged,
            "cached": report.cached,
            "verdicts": [asdict(v) for v in report.verdicts],
        },
        indent=2,
    )
