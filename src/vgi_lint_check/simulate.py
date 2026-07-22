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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any

from .model import TAG_DOC_LLM, AgentTask, Catalog, Function, ObjectKind, Table
from .review import (  # backends + cache + sessions
    ReviewBackend,
    ReviewCache,
    backend_fingerprint,
    make_conversation,
)
from .rules._util import (
    is_bind_error,
    is_filter_policy_error,
    run_with_timeout,
    safe_session_sql,
)
from .rules.execution import _render_result  # canonical result rendering (VGI907)


def _classify_error(error: str) -> str:
    """Bucket a failed action's error into a metadata-actionable kind.

    ``bind`` = unknown table/column/function or type mismatch (a discoverability
    gap); ``requirement`` = a mandatory-filter / usage policy the agent hit by
    trial and error (an unsurfaced requirement); ``runtime`` = everything else.
    """
    if is_filter_policy_error(error):
        return "requirement"
    if is_bind_error(error):
        return "bind"
    return "runtime"


_ANSWER_LIMIT = 1000  # row cap when materializing the answer / reference for grading
_VERB = re.compile(r"\s*([A-Za-z_]+)")
_WRAPPABLE = frozenset({"select", "with", "values", "table", "from"})


# --------------------------------------------------------------------------
# Limits / results
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SimLimits:
    """Per-task bounds for the analyst loop."""

    max_steps: int = 12
    max_queries: int = 10
    attempts: int = 1
    timeout: float = 30.0
    row_limit: int = 50
    concurrency: int = 4  # tasks judged in parallel (each on its own cursor)
    sessions: bool = True  # use a claude session (resume) so only deltas are re-sent


@dataclass
class TaskStep:
    """One analyst SQL action and what came back."""

    sql: str
    ok: bool
    error: str | None = None
    error_kind: str | None = None  # bind | requirement | runtime | blocked
    cols: list[str] = field(default_factory=list)
    rows: list[Any] = field(default_factory=list)
    blocked: bool = False  # rejected by the safe-SQL guard


@dataclass
class TraceEvent:
    """One discovery tool call on the analyst's path (for path scoring)."""

    kind: str  # list_tables | list_categories | describe_table | describe_function
    target: str  # object name (or "")
    found: bool = True  # describe of a real object vs a miss
    redundant: bool = False  # the same object was already described


@dataclass
class PathMetrics:
    """How efficiently the analyst reached an answer — the discoverability signal.

    The score penalizes *wasted* effort (errors, re-inspection, non-convergence),
    not raw step count, so an inherently complex task isn't punished for being
    complex — only for the worker's metadata making it harder than it needs to be.
    """

    discovery_calls: int = 0
    queries: int = 0
    bind_errors: int = 0
    requirement_errors: int = 0
    runtime_errors: int = 0
    blocked: int = 0
    redundant_describes: int = 0
    not_found: int = 0
    hit_ceiling: bool = False
    score: int = 100  # 0-100 discoverability (100 = went straight to the answer)


@dataclass
class TaskRun:
    """The transcript of one attempt at a task."""

    steps: list[TaskStep]
    answer_sql: list[str]
    answer_summary: str
    friction: list[str]
    discovery: list[TraceEvent] = field(default_factory=list)
    hit_ceiling: bool = False  # exhausted the step budget without a final answer


@dataclass
class TaskVerdict:
    """The graded outcome of a task plus its discovery-path assessment."""

    name: str
    outcome: str  # pass | partial | fail
    reason: str
    friction: list[str] = field(default_factory=list)
    queries: int = 0
    grader: str = ""  # reference | check_sql | judge | answered
    path: PathMetrics = field(default_factory=PathMetrics)
    suggestions: list[str] = field(default_factory=list)
    attempts_used: int = 1

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
    coverage: Coverage = field(default_factory=lambda: Coverage())

    @property
    def pass_rate(self) -> float:
        """Fraction of tasks that fully passed."""
        if not self.verdicts:
            return 0.0
        return round(sum(v.passed for v in self.verdicts) / len(self.verdicts), 2)

    @property
    def discoverability(self) -> int:
        """Mean per-task discovery-path score 0-100 (how easily an agent solves).

        Distinct from pass-rate: a task can pass yet score low here because the
        agent reached the answer only through errors and re-inspection.
        """
        if not self.verdicts:
            return 0
        return round(sum(v.path.score for v in self.verdicts) / len(self.verdicts))

    @property
    def suggestions(self) -> list[str]:
        """De-duplicated metadata-improvement suggestions across all tasks."""
        seen: dict[str, None] = {}
        for v in self.verdicts:
            for s in v.suggestions:
                seen.setdefault(s, None)
        return list(seen)

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
# Bounded orientation listing (the preamble — names + one-liners, never columns
# or the solution). The analyst drills in via the discovery tools below.
# --------------------------------------------------------------------------
def _listing_signature(obj: Function) -> str:
    """Compact call signature for the orientation listing.

    Shows the CALLING CONVENTION, not just parameter names: a named-only
    argument renders as ``name := …``. A bare comma-joined name list reads as a
    positional signature, so an analyst writes the positional call it implies and
    eats a bind error — measured against a worker whose regression functions take
    named-only ``formula``/``family``, that one line cost a bind error on 5 of 7
    tasks and failed one outright, with the analyst reporting that positional
    arguments failed "despite matching the documented signature".

    ``describe_function`` already reports ``calling`` per argument and a ``usage``
    template; the listing must not contradict them. Falls back to the bare
    parameter names when a worker exposes no argument metadata.
    """
    args = list(getattr(obj, "arguments", None) or ())
    if not args:
        return ", ".join(obj.parameters) if obj.parameters else ""
    parts: list[str] = []
    for a in args:
        # Table inputs and positional args are already written as bare values;
        # only a named arg needs its `:=` spelled out.
        parts.append(f"{a.name} := …" if a.is_named and not a.is_table_input else a.name)
    return ", ".join(parts)


def _listing_line(catalog: Catalog, schema: str, obj: Table | Function, indent: str) -> str | None:
    """One indented orientation line for a table/view/function (None to skip)."""
    qual = catalog.qualifier
    if isinstance(obj, Table):  # Table or its View subclass (kind distinguishes)
        td = (obj.description_llm or obj.comment or "").strip()
        return f"{indent}{obj.kind} {qual}.{schema}.{obj.name}" + (f" — {td[:160]}" if td else "")
    if obj.kind is ObjectKind.TABLE_FUNCTION and catalog.find_table_like(obj.name, obj.schema):
        return None  # documented via its backing table
    sig = _listing_signature(obj)
    fd = (obj.description or obj.comment or "").strip()
    return f"{indent}{obj.function_type} {qual}.{schema}.{obj.name}({sig})" + (
        f" — {fd[:160]}" if fd else ""
    )


def build_listing(catalog: Catalog) -> str:
    """List schemas, tables/views, and functions with their one-line descriptions.

    A bounded orientation map — no columns (use describe_table) and no task
    solution. The analyst drills into detail through the discovery tools. A schema
    that declares a ``vgi.categories`` registry is rendered grouped by category, in
    registry order, so the agent triages sections instead of a flat object list.
    """
    out: list[str] = [f"Catalog: {catalog.qualifier}"]
    if catalog.description_llm or catalog.comment:
        out.append(f"  {(catalog.description_llm or catalog.comment or '').strip()[:300]}")
    for s in catalog.iter_schemas():
        sd = (s.tags.get(TAG_DOC_LLM) or s.comment or "").strip()
        out.append(f"\nschema {catalog.qualifier}.{s.name}" + (f" — {sd[:160]}" if sd else ""))
        if s.categories:
            for cat, objs in s.iter_by_category():
                if cat is None:
                    out.append("  [uncategorized]")
                else:
                    cd = (cat.description or "").strip()
                    out.append(
                        f"  [{cat.name}] {cat.display_title}" + (f" — {cd[:160]}" if cd else "")
                    )
                for obj in objs:
                    line = _listing_line(catalog, s.name, obj, "    ")
                    if line:
                        out.append(line)
        else:
            for t in (*s.tables, *s.views):
                line = _listing_line(catalog, s.name, t, "  ")
                if line:
                    out.append(line)
            for f in s.functions:
                line = _listing_line(catalog, s.name, f, "  ")
                if line:
                    out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------
# Discovery tools — a local mirror of the vgi-web-frontend "ask AI" contract,
# answered from the Catalog model + the live connection (no MCP server needed).
# Each returns a JSON-serializable dict. None ever exposes a task's solution.
# --------------------------------------------------------------------------
def tool_list_tables(catalog: Catalog) -> dict[str, Any]:
    """Schemas, tables/views (name + comment + column count), and functions."""
    schemas = []
    for s in catalog.iter_schemas():
        schemas.append(
            {
                "name": s.name,
                "comment": s.tags.get(TAG_DOC_LLM) or s.comment,
                "tables": [
                    {
                        "name": t.name,
                        "type": str(t.kind),
                        "comment": t.description_llm or t.comment,
                        "column_count": len(t.columns),
                        **({"category": t.category} if t.category else {}),
                    }
                    for t in s.tables
                ],
                "views": [
                    {
                        "name": v.name,
                        "type": "view",
                        "comment": v.description_llm or v.comment,
                        **({"category": v.category} if v.category else {}),
                    }
                    for v in s.views
                ],
                "functions": [
                    {
                        "name": f.name,
                        "type": f.function_type,
                        "parameters": list(f.parameters),
                        "comment": f.description or f.comment,
                        **({"category": f.category} if f.category else {}),
                    }
                    for f in s.functions
                    if not (
                        f.kind is ObjectKind.TABLE_FUNCTION
                        and catalog.find_table_like(f.name, f.schema)
                    )
                ],
            }
        )
    return {
        "catalog": catalog.qualifier,
        "default_schema": catalog.default_schema,
        "schemas": schemas,
    }


def tool_list_categories(catalog: Catalog, schema: str) -> dict[str, Any]:
    """Categories of a schema (registry order) with their objects — a navigation map."""
    for s in catalog.iter_schemas():
        if s.name != schema:
            continue
        categories: list[dict[str, Any]] = []
        uncategorized: list[str] = []
        for cat, objs in s.iter_by_category():
            names = [o.name for o in objs]
            if cat is None:
                uncategorized = names
            else:
                categories.append(
                    {
                        "name": cat.name,
                        "title": cat.display_title,
                        "description": cat.description,
                        "object_count": len(names),
                        "objects": names,
                    }
                )
        return {"schema": schema, "categories": categories, "uncategorized": uncategorized}
    return {"error": f"no schema {schema!r} — call list_tables to see what exists"}


def _resolve_schema(catalog: Catalog, schema: str) -> str:
    """Accept a bare or catalog-qualified schema name and return the bare one.

    The listing prints fully-qualified names (``catalog.schema.object``), so an
    analyst reasonably passes ``"statsmodels.main"`` back into a describe tool.
    Rejecting that as not-found is a self-inflicted dead end: the tool taught the
    name and then refused it. Strip a leading ``<catalog>.`` so both forms work.
    """
    prefix = f"{catalog.qualifier}."
    if schema.startswith(prefix):
        return schema[len(prefix) :]
    return schema


def tool_describe_table(catalog: Catalog, schema: str, table: str) -> dict[str, Any]:
    """Columns (name/type/nullable/comment), constraints, and examples for a table."""
    schema = _resolve_schema(catalog, schema)
    for t in catalog.iter_table_like():
        if t.schema == schema and t.name == table:
            pk = [
                c.columns
                for _t, c in catalog.iter_constraints()
                if _t is t and c.constraint_type == "PRIMARY KEY"
            ]
            fks = [
                {
                    "columns": c.columns,
                    "references": f"{c.referenced_table}({','.join(c.referenced_columns)})",
                }
                for _t, c in catalog.iter_constraints()
                if _t is t and c.constraint_type == "FOREIGN KEY"
            ]
            return {
                "schema": schema,
                "name": table,
                "type": str(t.kind),
                "comment": t.description_llm or t.comment,
                "doc_md": t.description_md,
                **({"category": t.category} if t.category else {}),
                "primary_key": pk[0] if pk else None,
                "foreign_keys": fks or None,
                "columns": [
                    {"name": c.name, "type": c.data_type, "comment": c.comment} for c in t.columns
                ],
                "examples": [e.sql for e in t.examples if e.sql],
            }
    return {"error": f"no table {schema}.{table!r} — call list_tables to see what exists"}


def _arg_calling(a: Any) -> str:
    """How an argument is passed: ``table`` input, ``named`` (name := …), or positional."""
    if a.is_table_input:
        return "table"
    if a.is_named:
        return "named"
    return "positional"


def _decode_json(raw: str) -> Any:
    """Best-effort JSON decode of surfaced constraint text; fall back to the raw string."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _arg_constraints(a: Any) -> dict[str, Any]:
    """Discovery-facing per-argument constraints, when the worker declares them.

    Surfaces ``allowed_values`` (the closed choice set), ``default``, ``range``
    (interval notation), and ``pattern`` so the analyst can read valid inputs
    instead of learning them by trial-and-error. These are all *public* argument
    metadata from ``vgi_function_arguments()`` — never grader-only task fields, so
    the no-leak invariant holds.
    """
    out: dict[str, Any] = {}
    if getattr(a, "choices", None) is not None:
        out["allowed_values"] = _decode_json(a.choices)
    if getattr(a, "default", None) is not None:
        out["default"] = _decode_json(a.default)
    if getattr(a, "value_range", None) is not None:
        out["range"] = a.value_range
    if getattr(a, "pattern", None) is not None:
        out["pattern"] = a.pattern
    return out


def _usage_hint(catalog: Catalog, schema: str, name: str, arguments: list[Any]) -> str | None:
    """A copy-pasteable call template that makes the calling convention explicit.

    Named args show ``name := <type>``, table inputs show a subquery placeholder, and
    positional args show their name — so the analyst doesn't guess argument syntax
    (the common source of bind errors on functions that mix the two, like fit()).
    """
    if not arguments:
        return None
    parts: list[str] = []
    for a in arguments:
        if a.is_table_input:
            parts.append("<table-or-subquery>")
        elif a.is_named:
            parts.append(f"{a.name} := <{a.type or 'value'}>")
        else:
            parts.append(a.name)
    return f"{catalog.qualifier}.{schema}.{name}({', '.join(parts)})"


def tool_describe_function(catalog: Catalog, schema: str, name: str) -> dict[str, Any]:
    """Signature, description, per-argument docs, and the calling convention."""
    schema = _resolve_schema(catalog, schema)
    for f in catalog.iter_all_functions():
        if f.schema == schema and f.name == name:
            out = {
                "schema": schema,
                "name": name,
                "function_type": f.function_type,
                "description": f.description or f.comment,
                "doc_llm": f.tags.get(TAG_DOC_LLM),
                **({"category": f.category} if f.category else {}),
                "parameters": list(f.parameters),
                "arguments": [
                    {
                        "name": a.name,
                        "type": a.type,
                        "description": a.description,
                        "calling": _arg_calling(a),
                        **({"varargs": True} if a.is_varargs else {}),
                        **_arg_constraints(a),
                    }
                    for a in f.arguments
                ],
                "examples": [e.sql for e in f.examples if e.sql],
            }
            usage = _usage_hint(catalog, schema, name, f.arguments)
            if usage:
                out["usage"] = usage
            return out
    return {"error": f"no function {schema}.{name!r} — call list_tables to see what exists"}


def tool_run_sql(cur: Any, sql: str, limits: SimLimits) -> dict[str, Any]:
    """Run a read-only / session-local query; return columns + the first rows."""
    if not safe_session_sql(sql):
        return {
            "ok": False,
            "error": "blocked: only read-only / session-local SQL is allowed "
            "(SELECT/WITH/EXPLAIN/SET/temp objects)",
        }
    try:
        cols, rows = _run(cur, _wrap_limit(sql, limits.row_limit), limits.timeout)
    except Exception as e:  # noqa: BLE001 - relayed to the analyst as an observation
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "columns": cols, "rows": _render_result(cols, rows), "row_count": len(rows)}


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
    a: tuple[list[str], list[Any]],
    b: tuple[list[str], list[Any]],
    *,
    unordered: bool,
    ignore_column_names: bool,
) -> bool:
    """Compare two live result sets, the reference being the contract.

    Strict by default: column names + values must match (the task prompt should
    pin output column names, so name them in the reference). With
    ``ignore_column_names`` the comparison is by VALUES only. Row order matters
    unless ``unordered`` (most analyst tasks are ranked/top-N).
    """
    if ignore_column_names:
        ra, rb = _render_result([], a[1]), _render_result([], b[1])
    else:
        ra, rb = _render_result(*a), _render_result(*b)
    if unordered:
        return sorted(map(_canon, ra)) == sorted(map(_canon, rb))
    return ra == rb


def _canon(row: Any) -> str:
    return json.dumps(row, sort_keys=True, default=str)


# --------------------------------------------------------------------------
# The analyst loop + grading
# --------------------------------------------------------------------------
_ACTOR = (
    "You are a SQL analyst assistant connected to a DuckDB database with a data catalog "
    "attached. Accomplish the TASK using ONLY what the catalog exposes — you discover the "
    "schema through tools, exactly like a real agent would.\n\n"
    "TOOLS — respond with ONE JSON object per turn and nothing else:\n"
    '  {"thought":"...","action":"list_tables"}'
    "  — list schemas, tables/views, and functions with their one-line descriptions\n"
    '  {"thought":"...","action":"list_categories","schema":"..."}'
    "  — list a schema's categories (ordered sections) and the objects in each\n"
    '  {"thought":"...","action":"describe_table","schema":"...","table":"..."}'
    "  — columns, types, constraints, and examples for one table/view\n"
    '  {"thought":"...","action":"describe_function","schema":"...","name":"..."}'
    "  — signature, description, and per-argument docs for one function\n"
    '  {"thought":"...","action":"run_sql","sql":"<one SQL statement>"}'
    "  — run one read-only / session-local statement (SELECT, WITH, temp view)\n"
    '  {"thought":"...","action":"final","answer_sql":"<SELECT whose result IS the answer>",'
    '"answer_summary":"...","friction":["missing/confusing metadata, else omit"]}\n\n'
    "RULES (mirror real-agent best practice):\n"
    "- Call describe_table / describe_function to learn columns and signatures before "
    "querying — never guess column or argument names.\n"
    "- Reference objects by their three-part name catalog.schema.object.\n"
    "- Do ALL arithmetic in SQL; combine data with JOINs in SQL.\n"
    "- Avoid SELECT * in your final answer; select only the columns the task needs.\n"
    "- The orientation listing below is just a starting map — drill in with the tools."
)


def _dispatch(
    catalog: Catalog, cur: Any, action: dict[str, Any], limits: SimLimits
) -> dict[str, Any]:
    """Answer one discovery/run_sql tool call from the Catalog model + live cursor."""
    kind = action.get("action")
    if kind == "list_tables":
        return tool_list_tables(catalog)
    if kind == "list_categories":
        return tool_list_categories(catalog, str(action.get("schema") or ""))
    if kind == "describe_table":
        return tool_describe_table(
            catalog, str(action.get("schema") or ""), str(action.get("table") or "")
        )
    if kind == "describe_function":
        return tool_describe_function(
            catalog, str(action.get("schema") or ""), str(action.get("name") or "")
        )
    if kind == "run_sql":
        return tool_run_sql(cur, str(action.get("sql") or "").strip(), limits)
    return {"error": f"unknown action {kind!r}"}


def run_task(
    catalog: Catalog,
    con: Any,
    backend: ReviewBackend,
    task: AgentTask,
    listing: str,
    limits: SimLimits,
) -> TaskRun:
    """Drive the bounded tool-mediated ReAct loop on its own cursor (state accrues).

    Runs over a :class:`Conversation`: the first message carries the full preamble,
    listing, and task; each later message carries only the latest tool result (a
    delta), so with a session backend the growing transcript isn't re-transmitted.
    """
    cur = con.cursor()
    convo = make_conversation(backend, sessions=limits.sessions)
    steps: list[TaskStep] = []
    discovery: list[TraceEvent] = []
    described: set[str] = set()  # objects already described (to spot re-inspection)
    answer_sql: list[str] = []
    summary = ""
    friction: list[str] = []
    finished = False
    queries = 0
    message = (
        f"{_ACTOR}\n\nORIENTATION (names only — use the tools for detail):\n{listing}\n\n"
        f"TASK: {task.prompt}\n\nBegin: reply with your first JSON action."
    )
    for _ in range(max(1, limits.max_steps)):
        try:
            action = _extract_json(convo.send(message))
        except (ValueError, json.JSONDecodeError):
            break
        if not isinstance(action, dict):
            break
        kind = action.get("action")
        if kind == "final":
            answer_sql = _as_sql_list(action.get("answer_sql"))
            summary = str(action.get("answer_summary") or "")
            friction = [str(x) for x in (action.get("friction") or [])]
            finished = True
            break
        if kind == "run_sql":
            sql = str(action.get("sql") or "").strip()
            if not sql:
                break
            result = tool_run_sql(cur, sql, limits)
            if result.get("ok"):
                steps.append(
                    TaskStep(sql=sql, ok=True, cols=result["columns"], rows=result["rows"])
                )
            else:
                err = result.get("error", "")
                blocked = err.startswith("blocked")
                ekind = "blocked" if blocked else _classify_error(err)
                if blocked:
                    friction.append(f"attempted a non-read-only statement: {sql[:80]}")
                steps.append(
                    TaskStep(sql=sql, ok=False, blocked=blocked, error=err, error_kind=ekind)
                )
            queries += 1
            message = _observation("run_sql", result)
            if queries >= limits.max_queries:
                break
            continue
        if kind in ("list_tables", "list_categories", "describe_table", "describe_function"):
            result = _dispatch(catalog, cur, action, limits)
            label = str(action.get("table") or action.get("name") or action.get("schema") or "")
            key = f"{kind}:{action.get('schema') or ''}.{label}"
            discovery.append(
                TraceEvent(
                    kind=kind, target=label, found="error" not in result, redundant=key in described
                )
            )
            described.add(key)
            message = _observation(f"{kind} {label}".strip(), result)
            continue
        break  # unknown / malformed action
    # keep the agent cursor alive on the run for tier-2 grading
    run = TaskRun(
        steps=steps,
        answer_sql=answer_sql,
        answer_summary=summary,
        friction=friction,
        discovery=discovery,
        hit_ceiling=not finished,
    )
    run._cur = cur  # type: ignore[attr-defined]
    return run


def _observation(label: str, result: dict[str, Any]) -> str:
    """The delta message fed back to the analyst after a tool call."""
    return (
        f"RESULT of {label}:\n{_clip(json.dumps(result, default=str))}\n\n"
        "Reply with your next JSON action (or a final answer)."
    )


def _clip(text: str, limit: int = 1200) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


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
        ok = _resultsets_equal(
            expected,
            actual,
            unordered=task.unordered,
            ignore_column_names=task.ignore_column_names,
        )
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
# Path scoring + suggestions (the discoverability signal)
# --------------------------------------------------------------------------
# Per-event discoverability penalties. Each names a *wasted* action — effort the
# worker's metadata should have spared the agent — so the score is independent of
# a task's intrinsic complexity (legitimate steps cost nothing).
_PENALTY = {
    "bind": 15,  # guessed a wrong column/function name → not discoverable
    "requirement": 15,  # hit an unsurfaced usage requirement (mandatory filter)
    "runtime": 8,  # a query failed at runtime
    "blocked": 5,  # tried to escape the read-only session
    "redundant": 10,  # re-inspected an object → its description was too thin
    "not_found": 8,  # looked up an object that doesn't exist → wrong mental model
    "ceiling": 40,  # never converged within the step budget
}


def compute_path_metrics(run: TaskRun) -> PathMetrics:
    """Score how cleanly the analyst reached an answer (penalize wasted effort)."""
    bind = sum(1 for s in run.steps if s.error_kind == "bind")
    req = sum(1 for s in run.steps if s.error_kind == "requirement")
    runtime = sum(1 for s in run.steps if s.error_kind == "runtime")
    blocked = sum(1 for s in run.steps if s.blocked)
    redundant = sum(1 for e in run.discovery if e.redundant)
    not_found = sum(1 for e in run.discovery if not e.found)
    queries = sum(1 for s in run.steps if not s.blocked)
    penalty = (
        _PENALTY["bind"] * bind
        + _PENALTY["requirement"] * req
        + _PENALTY["runtime"] * runtime
        + _PENALTY["blocked"] * blocked
        + _PENALTY["redundant"] * redundant
        + _PENALTY["not_found"] * not_found
        + (_PENALTY["ceiling"] if run.hit_ceiling else 0)
    )
    return PathMetrics(
        discovery_calls=len(run.discovery),
        queries=queries,
        bind_errors=bind,
        requirement_errors=req,
        runtime_errors=runtime,
        blocked=blocked,
        redundant_describes=redundant,
        not_found=not_found,
        hit_ceiling=run.hit_ceiling,
        score=max(0, 100 - penalty),
    )


def build_suggestions(run: TaskRun, m: PathMetrics) -> list[str]:
    """Turn a scored path into concrete worker-metadata improvement suggestions."""
    out: list[str] = []
    if m.hit_ceiling:
        out.append(
            "Agent never converged within the step budget — no example demonstrates this; "
            "add a vgi.executable_examples entry showing the canonical solution."
        )
    if m.bind_errors:
        out.append(
            f"Agent ran {m.bind_errors} quer{'y' if m.bind_errors == 1 else 'ies'} that failed "
            "to bind (unknown column/function/table) before solving — names or struct paths "
            "aren't discoverable; tighten per-object docs (VGI2xx/VGI3xx) or add a worked example."
        )
    if m.requirement_errors:
        out.append(
            f"Agent hit a usage requirement (e.g. a mandatory filter) {m.requirement_errors} "
            "time(s) by trial and error — state it in the catalog/table doc_llm."
        )
    if m.redundant_describes:
        out.append(
            "Agent re-inspected an object it had already described — that description may be "
            "too thin to act on (VGI112/VGI113)."
        )
    if m.not_found:
        out.append(
            "Agent looked up an object that doesn't exist — the metadata implies a name that "
            "isn't there; align docs/examples with the real schema."
        )
    out.extend(f"Analyst noted: {f}" for f in run.friction)
    return out


# --------------------------------------------------------------------------
# Suite runner + cache
# --------------------------------------------------------------------------
def _task_key(overview: str, task: AgentTask, data_version: str | None, salt: str = "") -> str:
    blob = json.dumps([salt, overview, task.raw, data_version], sort_keys=True, default=str)
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
    """Run every declared task (with retries) and aggregate a suitability report.

    Cache-miss tasks are judged in parallel (``limits.concurrency``), each on its
    own ``con.cursor()`` so the worker pool serves them concurrently; results are
    reassembled in declaration order and the cache is written on the main thread.
    """
    limits = limits or SimLimits()
    listing = build_listing(catalog)
    salt = backend_fingerprint(backend)

    def judge(task: AgentTask) -> TaskVerdict:
        best: TaskVerdict | None = None
        best_run: TaskRun | None = None
        attempts = 0
        for _ in range(max(1, limits.attempts)):
            attempts += 1
            run = run_task(catalog, con, backend, task, listing, limits)
            v = grade_task(con, backend, task, run, limits)
            if best is None or (v.passed and not best.passed):
                best, best_run = v, run
            if v.passed:
                break
        assert best is not None and best_run is not None
        best.path = compute_path_metrics(best_run)
        best.suggestions = build_suggestions(best_run, best.path)
        best.attempts_used = attempts
        return best

    # Resolve cache hits up front; collect the misses (with their slot + key) to judge.
    slots: list[TaskVerdict | None] = [None] * len(catalog.agent_test_tasks)
    misses: list[tuple[int, AgentTask, str]] = []
    cached = 0
    for i, task in enumerate(catalog.agent_test_tasks):
        key = _task_key(listing, task, catalog.data_version, salt)
        hit = _cache_get(cache, key)
        if hit is not None:
            slots[i] = hit
            cached += 1
        else:
            misses.append((i, task, key))

    if limits.concurrency > 1 and len(misses) > 1:
        with ThreadPoolExecutor(max_workers=limits.concurrency) as ex:
            futs = {ex.submit(judge, task): (i, key) for i, task, key in misses}
            for fut in as_completed(futs):
                i, key = futs[fut]
                v = fut.result()
                slots[i] = v
                _cache_put(cache, key, v)  # main thread only — no lock needed
    else:
        for i, task, key in misses:
            v = judge(task)
            slots[i] = v
            _cache_put(cache, key, v)

    if cache:
        cache.save()
    verdicts = [v for v in slots if v is not None]
    return SimReport(
        catalog.location, backend_name, verdicts, len(misses), cached, compute_coverage(catalog)
    )


def _cache_get(cache: ReviewCache | None, key: str) -> TaskVerdict | None:
    if cache is None:
        return None
    d = cache._data.get(key)  # noqa: SLF001 - shared JSON cache
    if not d:
        return None
    d = dict(d)
    if isinstance(d.get("path"), dict):
        d["path"] = PathMetrics(**d["path"])
    return TaskVerdict(**d)


def _cache_put(cache: ReviewCache | None, key: str, v: TaskVerdict) -> None:
    if cache is not None:
        cache._data[key] = {k: val for k, val in asdict(v).items()}  # noqa: SLF001


# --------------------------------------------------------------------------
# Suite coverage (which of the worker's functions the declared tasks exercise)
# --------------------------------------------------------------------------
@dataclass
class Coverage:
    """Static coverage: the worker objects a declared suite's reference SQL touches.

    "Objects" are the queryable API surface — functions plus tables/views — so the
    metric is meaningful for both function-centric and table-centric workers.
    Computed from the tasks' ``reference_sql`` / ``check_sql`` (not from a run), so
    it answers "does this suite even touch the whole API?" independent of pass/fail.
    """

    covered: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Number of distinct worker objects considered."""
        return len(self.covered) + len(self.uncovered)

    @property
    def pct(self) -> int:
        """Percent of the worker's objects exercised by ≥1 task (100 if none)."""
        return round(100 * len(self.covered) / self.total) if self.total else 100


def _unique_objects(catalog: Catalog) -> list[tuple[str, str]]:
    """Distinct (qualified_name, bare_name) for the worker's functions + tables/views."""
    seen: dict[str, str] = {}
    for f in catalog.iter_all_functions():
        seen[f"{catalog.qualifier}.{f.schema}.{f.name}"] = f.name
    for t in catalog.iter_table_like():
        seen.setdefault(f"{catalog.qualifier}.{t.schema}.{t.name}", t.name)
    return sorted(seen.items())


def _suite_sql(catalog: Catalog) -> str:
    """All reference/check SQL across the declared suite, lowercased, for scanning."""
    parts: list[str] = []
    for t in catalog.agent_test_tasks:
        parts.extend(s.sql or "" for s in t.reference_statements)
        if t.check_sql:
            parts.append(t.check_sql)
    return " ".join(parts).lower()


def _referenced(text: str, objects: list[tuple[str, str]]) -> set[str]:
    """Qualified names from ``objects`` whose bare name appears in ``text`` (a call)."""
    low = text.lower()
    return {q for q, name in objects if re.search(rf"\b{re.escape(name.lower())}\b", low)}


def compute_coverage(catalog: Catalog) -> Coverage:
    """Which objects (functions + tables) the suite's reference/check SQL touches."""
    objects = _unique_objects(catalog)
    hit = _referenced(_suite_sql(catalog), objects)
    covered = [q for q, _ in objects if q in hit]
    uncovered = [q for q, _ in objects if q not in hit]
    return Coverage(covered=covered, uncovered=uncovered)


# --------------------------------------------------------------------------
# Reference verification — an authoring/CI gate that the declared references are
# sound (they run, and they're deterministic) BEFORE trusting a graded run.
# --------------------------------------------------------------------------
@dataclass
class RefCheck:
    """The verification outcome for one task's reference SQL."""

    name: str
    status: str  # ok | error | nondeterministic | empty | no-reference
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the reference is sound (or there's nothing to verify)."""
        return self.status in ("ok", "no-reference")


@dataclass
class VerifyReport:
    """Result of verifying every declared task's reference SQL."""

    location: str
    checks: list[RefCheck]

    @property
    def ok(self) -> bool:
        """True when every reference is sound."""
        return all(c.ok for c in self.checks)


def verify_references(
    catalog: Catalog, con: Any, limits: SimLimits | None = None, runs: int = 3
) -> VerifyReport:
    """Run each task's ``reference_sql`` ``runs`` times and check it's sound.

    Flags references that error, return different results across runs (a random or
    unseeded reference), or return no rows. This catches authoring bugs the graded
    simulation would otherwise surface only as a flaky failure; it does NOT judge
    whether an agent can *reproduce* the answer (that's what the simulation does).
    """
    limits = limits or SimLimits()
    runs = max(2, runs)
    checks: list[RefCheck] = []
    for task in catalog.agent_test_tasks:
        if not task.reference_statements:
            checks.append(RefCheck(task.name, "no-reference", "no reference_sql to verify"))
            continue
        results: list[tuple[list[str], list[Any]]] = []
        err: str | None = None
        for _ in range(runs):
            try:
                results.append(
                    _terminal_result(con.cursor(), task.reference_statements, limits.timeout)
                )
            except Exception as e:  # noqa: BLE001 - reported as the check's failure
                err = f"{type(e).__name__}: {e}"
                break
        if err is not None:
            checks.append(RefCheck(task.name, "error", err[:200]))
            continue
        first = results[0]
        stable = all(
            _resultsets_equal(
                first, r, unordered=task.unordered, ignore_column_names=task.ignore_column_names
            )
            for r in results[1:]
        )
        if not stable:
            checks.append(
                RefCheck(task.name, "nondeterministic", f"differs across {runs} runs (seed it?)")
            )
        elif not first[1]:
            checks.append(RefCheck(task.name, "empty", "returns no rows"))
        else:
            checks.append(RefCheck(task.name, "ok", f"{len(first[1])} row(s), stable ×{runs}"))
    return VerifyReport(catalog.location, checks)


def render_verify(report: VerifyReport) -> str:
    """Human-readable reference-verification report."""
    mark = {"ok": "✓", "no-reference": "·"}
    out = [f"reference verification  {report.location}", ""]
    for c in report.checks:
        out.append(
            f"{mark.get(c.status, '✗')} {c.name}  [{c.status}]"
            + (f" — {c.detail}" if c.detail else "")
        )
    bad = [c for c in report.checks if not c.ok]
    out.append("")
    out.append(f"{len(report.checks) - len(bad)}/{len(report.checks)} references sound")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# Suggest mode (authoring helper) — coverage-driven, batched for large catalogs
# --------------------------------------------------------------------------
# One small LLM call per batch of uncovered objects keeps every prompt fast and
# well under the backend timeout, so suggestion scales to any catalog size.
_SUGGEST_BATCH_SIZE = 6
_SUGGEST_MAX_ROUNDS = 12

_SUGGEST = (
    "You are designing a FIXED acceptance suite of analyst tasks for a data worker, run by an "
    "automated agent-suitability test. Propose realistic analyst tasks — things an end user would "
    "actually ask — that together EXERCISE the specific worker objects listed under TARGET below "
    "(each target object should be used by at least one task). Mix single-call smoke tests and "
    "multi-step tasks. Each task must be solvable with SQL using ONLY what's exposed, with a "
    "correct canonical reference_sql, and must name its output columns (grading is strict on "
    "column names, values, and row order). Avoid non-deterministic results: pass a fixed "
    "random_state/seed where a function samples, and don't depend on shared mutable state.\n\n"
    "Return ONLY a JSON array of "
    '{{"name": "...", "prompt": "...", "reference_sql": "<a correct canonical solution>"}} '
    "objects.\n\n"
    "TARGET objects to cover in this batch:\n{targets}\n\n"
    "Other objects already covered (do not write tasks for these):\n{covered}\n\n"
    "CATALOG OVERVIEW (for reference):\n{overview}"
)


def _object_lines(catalog: Catalog) -> dict[str, str]:
    """Map qualified object name -> a one-line signature + short description.

    Long arg signatures are reduced to argument names so a worker with huge
    UNION/STRUCT parameter types can't blow the suggest prompt past the backend.
    """
    lines: dict[str, str] = {}
    for f in catalog.iter_all_functions():
        q = f"{catalog.qualifier}.{f.schema}.{f.name}"
        usage = _usage_hint(catalog, f.schema, f.name, f.arguments)
        sig = usage or f"{q}({', '.join(f.parameters)})"
        if len(sig) > 140:  # collapse mega-types to bare arg names
            args = ", ".join(a.name for a in f.arguments) or ", ".join(f.parameters)
            sig = f"{q}({args})"
        desc = (
            (f.description or f.comment or f.tags.get(TAG_DOC_LLM) or "").strip().replace("\n", " ")
        )
        lines.setdefault(q, f"{sig}" + (f" — {desc[:120]}" if desc else ""))
    for t in catalog.iter_table_like():
        q = f"{catalog.qualifier}.{t.schema}.{t.name}"
        desc = (t.description_llm or t.comment or "").strip().replace("\n", " ")
        lines.setdefault(q, f"{q} (table)" + (f" — {desc[:120]}" if desc else ""))
    return lines


def suggest_tasks(catalog: Catalog, backend: ReviewBackend, cap: int = 0) -> str:
    """Propose a coverage-driven suite as ready-to-paste tag JSON.

    Iterates over small batches of uncovered objects — recomputing coverage from
    the proposals each round — so each LLM call stays fast and the suite grows
    until the worker is covered (or ``cap`` tasks, when ``cap`` > 0). Already
    covered objects (from any existing suite) are skipped.
    """
    objects = _unique_objects(catalog)
    lines = _object_lines(catalog)
    overview = build_listing(catalog)
    covered: set[str] = _referenced(_suite_sql(catalog), objects)
    proposed: list[dict[str, Any]] = []

    for _ in range(_SUGGEST_MAX_ROUNDS):
        uncovered = [q for q, _ in objects if q not in covered]
        if not uncovered or (cap and len(proposed) >= cap):
            break
        targets = uncovered[:_SUGGEST_BATCH_SIZE]
        prompt = _SUGGEST.format(
            targets="\n".join(f"- {lines[q]}" for q in targets),
            covered=", ".join(sorted(covered)) or "(none yet)",
            overview=overview,
        )
        try:
            batch = _extract_json(backend.complete(prompt))
        except (ValueError, json.JSONDecodeError):
            break
        new = [t for t in batch if isinstance(t, dict) and t.get("reference_sql")]
        if not new:
            break
        before = len(covered)
        for t in new:
            proposed.append(t)
            covered |= _referenced(str(t.get("reference_sql", "")), objects)
        if len(covered) == before:  # no forward progress — stop rather than loop
            break

    if cap and cap > 0:
        proposed = proposed[:cap]
    return json.dumps(proposed, indent=2)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def render_terminal(report: SimReport) -> str:
    """Human-readable simulation report."""
    out = [
        f"agent simulation  {report.location}  ·  backend={report.backend}  ·  "
        f"judged {report.judged} · cached {report.cached}",
        f"suitability {report.score}/100  ·  pass rate {int(report.pass_rate * 100)}% "
        f"({sum(v.passed for v in report.verdicts)}/{len(report.verdicts)} tasks)  ·  "
        f"discoverability {report.discoverability}/100",
        f"object coverage {len(report.coverage.covered)}/{report.coverage.total} "
        f"({report.coverage.pct}%)"
        + (
            f"  ·  untested: {', '.join(report.coverage.uncovered)}"
            if report.coverage.uncovered
            else ""
        ),
        "",
    ]
    mark = {"pass": "✓", "partial": "~", "fail": "✗"}
    for v in report.verdicts:
        p = v.path
        extra = f", {v.attempts_used} attempts" if v.attempts_used > 1 else ""
        out.append(
            f"{mark.get(v.outcome, '?')} {v.name}  [{v.outcome}, {v.grader}, "
            f"path {p.score}/100{extra}]"
        )
        if v.reason:
            out.append(f"    ↳ {v.reason}")
        out.append(
            f"    · path: {p.discovery_calls} lookups, {p.queries} queries" + _path_faults(p)
        )
        for s in v.suggestions:
            out.append(f"    · suggest: {s}")
        out.append("")
    if report.suggestions:
        out.append("metadata improvements (deduped across tasks):")
        out.extend(f"  - {s}" for s in report.suggestions)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _path_faults(p: PathMetrics) -> str:
    """Compact ' · N bind errors, hit step ceiling' tail for the path line."""
    bits = []
    if p.bind_errors:
        bits.append(f"{p.bind_errors} bind error{'s' if p.bind_errors > 1 else ''}")
    if p.requirement_errors:
        bits.append(f"{p.requirement_errors} requirement miss")
    if p.runtime_errors:
        bits.append(f"{p.runtime_errors} runtime error{'s' if p.runtime_errors > 1 else ''}")
    if p.blocked:
        bits.append(f"{p.blocked} blocked")
    if p.redundant_describes:
        bits.append(f"{p.redundant_describes} re-inspection")
    if p.not_found:
        bits.append(f"{p.not_found} not-found lookup")
    if p.hit_ceiling:
        bits.append("hit step ceiling")
    return (" — " + ", ".join(bits)) if bits else ""


def render_json(report: SimReport) -> str:
    """Machine-readable simulation report."""
    return json.dumps(
        {
            "tool": "vgi-lint simulate",
            "location": report.location,
            "backend": report.backend,
            "score": report.score,
            "pass_rate": report.pass_rate,
            "discoverability": report.discoverability,
            "coverage": {
                "pct": report.coverage.pct,
                "covered": report.coverage.covered,
                "uncovered": report.coverage.uncovered,
            },
            "judged": report.judged,
            "cached": report.cached,
            "suggestions": report.suggestions,
            "verdicts": [asdict(v) for v in report.verdicts],
        },
        indent=2,
    )
