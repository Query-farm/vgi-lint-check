"""VGI9xx — opt-in execution against the live worker.

These rules require a connection and only run when ``--execute`` is set. Example
queries (VGI901-908, VGI910) run in one of three modes: ``explain`` (default,
cheapest — validates binding without fetching data), ``limit`` (runs wrapped in a
LIMIT), or ``run`` (executes as written).

VGI911/VGI912 instead probe each relation directly with ``SELECT * ... LIMIT n``
to check that a scan responds promptly and chunks its output sanely.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..connection import ALIAS_RE, sql_str
from ..findings import Category, Finding, Severity
from ..model import AttachOption, Catalog, Function, ObjectKind, Table, View
from ..sql_parse import select_star_call_sql
from ._util import (
    QueryTimeout,
    blank,
    is_bind_error,
    is_filter_policy_error,
    map_isolated_queries,
    map_queries,
    run_with_timeout,
)
from .base import Rule, RuleContext
from .registry import register

EXEC = Category.EXECUTION

# Every object kind that can carry an example query (tag or native Meta.examples).
_EXAMPLE_TARGETS = (
    ObjectKind.TABLE,
    ObjectKind.VIEW,
    ObjectKind.MACRO,
    ObjectKind.SCALAR_FUNCTION,
    ObjectKind.AGGREGATE,
    ObjectKind.TABLE_FUNCTION,
)


def _example_hosts(catalog: Catalog) -> Iterator[Table | View | Function]:
    yield from catalog.iter_table_like()
    yield from catalog.iter_all_functions()


def _example_sqls(catalog: Catalog) -> Iterator[tuple[Table | View | Function, Any]]:
    """Yield (host, example) pairs across every example carrier, deduped by SQL.

    Tables/views carry tag examples; functions (scalar/aggregate/macro/table)
    carry tag and native ``Meta.examples``. A table-backed table function can
    surface the same query on both the table and the function — run each unique
    SQL once, attributed to the first host that declares it.
    """
    seen: set[str] = set()
    for obj in _example_hosts(catalog):
        for ex in obj.examples:
            if blank(ex.sql):
                continue
            key = " ".join((ex.sql or "").split()).lower()
            if key in seen:
                continue
            seen.add(key)
            yield obj, ex


def _prepare(sql: str, mode: str, limit: int) -> str:
    sql = sql.rstrip().rstrip(";")
    if mode == "explain":
        return f"EXPLAIN {sql}"
    if mode == "limit":
        return f"SELECT * FROM ({sql}) AS _vgi_lint_q LIMIT {int(limit)}"
    return sql


@register
class ExampleQueriesExecute(Rule):
    code = "VGI901"
    name = "example-queries-execute"
    category = EXEC
    # An illustrative example (vgi.example_queries / Meta.examples) that doesn't
    # BIND is a real authoring bug (unknown table/column/function, bad types) ->
    # error. One that binds but fails at runtime may just need data/context not
    # present at lint time -> warning. Must-run examples belong in
    # vgi.executable_examples (VGI906, always error).
    default_severity = Severity.ERROR
    targets = _EXAMPLE_TARGETS
    requires_connection = True
    summary = "Example queries must bind (error); a runtime/data failure is a warning."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        mode = ctx.config.execute_mode
        limit = ctx.config.execute_limit
        timeout = ctx.config.execute_timeout

        def work(pair: tuple[Any, Any], cur: Any) -> Finding | None:
            obj, ex = pair
            sql = ex.sql or ""
            prepared = _prepare(sql, mode, limit)
            try:
                run_with_timeout(cur, lambda q=prepared: cur.execute(q), timeout)
            except Exception as e:  # noqa: BLE001 - surface engine/timeout error
                if is_filter_policy_error(e):
                    return None  # the worker's mandatory-filter policy, not a bug
                bind = is_bind_error(e)
                severity = ctx.severity if bind else min(ctx.severity, Severity.WARNING)
                kind = "does not bind" if bind else "failed at runtime"
                hint = (
                    "fix the example SQL so it binds (catalog-qualify references; "
                    "use real columns/types)"
                    if bind
                    else "the example binds but didn't run here (it may need data) — "
                    "move must-run examples to vgi.executable_examples (VGI906)"
                )
                return Finding(
                    code=self.code,
                    severity=severity,
                    category=self.category,
                    object_id=obj.id,
                    message=f"example #{ex.index} {kind}: {type(e).__name__}: {e}",
                    hint=f"{hint}; query: {sql[:120]}",
                )
            return None

        results = map_queries(con, _example_sqls(ctx.catalog), work, ctx.config.execute_concurrency)
        yield from (f for f in results if f is not None)


@register
class ExampleQueriesReturnRows(Rule):
    code = "VGI902"
    name = "example-queries-return-rows"
    category = EXEC
    default_severity = Severity.WARNING  # strict default
    targets = _EXAMPLE_TARGETS
    requires_connection = True
    summary = "Example queries should return at least one row (limit mode)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        limit = max(1, ctx.config.execute_limit)
        timeout = ctx.config.execute_timeout

        def work(pair: tuple[Any, Any], cur: Any) -> Finding | None:
            obj, ex = pair
            sql = ex.sql or ""
            wrapped = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _q LIMIT {limit}"
            try:
                rows = run_with_timeout(cur, lambda q=wrapped: cur.execute(q).fetchall(), timeout)
            except Exception:  # noqa: BLE001 - VGI901 reports execution/timeout errors
                return None
            if not rows:
                return self.finding(
                    ctx,
                    obj.id,
                    f"example #{ex.index} returned no rows",
                    "use an example that returns data so consumers see output",
                )
            return None

        results = map_queries(con, _example_sqls(ctx.catalog), work, ctx.config.execute_concurrency)
        yield from (f for f in results if f is not None)


@register
class ViewExecutes(Rule):
    code = "VGI903"
    name = "view-executes"
    category = EXEC
    default_severity = Severity.ERROR
    targets = (ObjectKind.VIEW,)
    requires_connection = True
    summary = "Every defined view must actually execute against the worker."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        qualifier = ctx.catalog.qualifier
        timeout = ctx.config.execute_timeout

        def work(view: Any, cur: Any) -> Finding | None:
            relation = f'"{qualifier}"."{view.schema}"."{view.name}"'
            try:
                run_with_timeout(
                    cur, lambda r=relation: cur.execute(f"EXPLAIN SELECT * FROM {r}"), timeout
                )
            except Exception as e:  # noqa: BLE001 - surface engine/timeout error
                # A mandatory-filter rejection means the view is wired up and
                # enforcing a scan policy, not that it's broken.
                if is_filter_policy_error(e):
                    return None
                return self.finding(
                    ctx,
                    view.id,
                    f"view does not execute: {type(e).__name__}: {e}",
                    "fix the view definition so it binds and runs against the worker",
                )
            return None

        results = map_queries(con, ctx.catalog.iter_views(), work, ctx.config.execute_concurrency)
        yield from (f for f in results if f is not None)


# --- live attach checks (VGI904 / VGI905) ---------------------------------
# Scalar types whose stringified default the worker's ATTACH binder can cast
# back to the declared type. We pass the default as a quoted string and let the
# binder cast it. Composite types (STRUCT/MAP/arrays) and binary types can't be
# reconstructed from their stringified default, so we skip them.
_CASTABLE_TYPE_PREFIXES = (
    "BOOLEAN",
    "BOOL",
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "INT",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
    "DECIMAL",
    "NUMERIC",
    "VARCHAR",
    "CHAR",
    "TEXT",
    "STRING",
    "DATE",
    "TIME",
    "TIMESTAMP",
    "UUID",
)


def _option_literal(opt: AttachOption) -> str | None:
    """Encode an option's default as a SQL literal for ATTACH, or None to skip.

    The default arrives stringified; we quote it and rely on the worker's ATTACH
    binder to cast it back to the declared type. Only scalar types whose string
    form round-trips are encoded — composite (STRUCT/MAP/array) and binary types,
    whose stringified default is not valid SQL, and options without a default,
    return None so we never emit a literal that would produce a false failure.
    """
    if opt.default is None:
        return None
    t = (opt.type or "").upper().strip()
    if not t or "[" in t:  # unknown, or an array type
        return None
    if not t.startswith(_CASTABLE_TYPE_PREFIXES):  # STRUCT/MAP/BLOB/BIT/...
        return None
    return sql_str(opt.default)


def _try_attach(
    con: Any, catalog_name: str, location: str, clause: str, timeout: float
) -> str | None:
    """Attempt ``ATTACH`` under a throwaway alias; return an error string or None.

    Always DETACHes on the way out so the probe leaves no catalog attached.
    """
    alias = "_vgi_probe"
    stmt = (
        f"ATTACH {sql_str(catalog_name)} AS {alias} "
        f"(TYPE vgi, LOCATION {sql_str(location)}{clause})"
    )
    try:
        run_with_timeout(con, lambda: con.execute(stmt), timeout)
    except Exception as e:  # noqa: BLE001 - relayed to the caller as the verdict
        return f"{type(e).__name__}: {e}"
    finally:
        with contextlib.suppress(Exception):
            con.execute(f"DETACH {alias}")
    return None


@register
class AttachOptionsAccepted(Rule):
    code = "VGI904"
    name = "attach-options-accepted"
    category = EXEC
    default_severity = Severity.ERROR
    targets = (ObjectKind.CATALOG,)
    requires_connection = True
    summary = "Advertised attach options must actually be accepted at ATTACH time."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        cat = ctx.catalog
        opts = list(cat.iter_attach_options())
        if not opts:
            return
        timeout = ctx.config.execute_timeout

        # An option whose name is not a bare identifier can't be passed at all.
        encodable: list[tuple[AttachOption, str]] = []
        for opt in opts:
            if not ALIAS_RE.match(opt.name or ""):
                yield self.finding(
                    ctx,
                    opt.id,
                    f"attach option {opt.name!r} is not a valid identifier, so it "
                    "cannot be supplied at ATTACH",
                    "rename the option to a bare SQL identifier (letters, digits, _)",
                )
                continue
            lit = _option_literal(opt)
            if lit is not None:
                encodable.append((opt, lit))

        if not encodable:
            return
        # Fast path: pass every encodable option at once. If the worker accepts
        # them, we're done in a single attach.
        full = "".join(f", {opt.name} {lit}" for opt, lit in encodable)
        if _try_attach(con, cat.qualifier, cat.location, full, timeout) is None:
            return
        # Something was rejected — re-probe per option to name the culprit(s).
        for opt, lit in encodable:
            err = _try_attach(con, cat.qualifier, cat.location, f", {opt.name} {lit}", timeout)
            if err is not None:
                yield self.finding(
                    ctx,
                    opt.id,
                    f"advertised attach option {opt.name!r} is rejected when passed "
                    f"(default {opt.default!r}): {err}",
                    "make the worker accept the option it advertises, or stop "
                    "advertising it in vgi_catalogs()",
                )


@register
class AdvertisedCatalogsAttachable(Rule):
    code = "VGI905"
    name = "advertised-catalogs-attachable"
    category = EXEC
    default_severity = Severity.ERROR
    targets = (ObjectKind.CATALOG,)
    requires_connection = True
    summary = "Every catalog vgi_catalogs() advertises must be attachable."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        cat = ctx.catalog
        timeout = ctx.config.execute_timeout
        for name in cat.advertised_catalogs:
            # The catalog under lint is already proven attachable; skip it.
            if name == cat.qualifier:
                continue
            err = _try_attach(con, name, cat.location, "", timeout)
            if err is not None:
                yield self.finding(
                    ctx,
                    cat.id,
                    f"advertised catalog {name!r} cannot be attached: {err}",
                    "fix the worker so every catalog it lists in vgi_catalogs() "
                    "can be attached, or stop advertising it",
                )


# --- executable examples (VGI906 / VGI907 / VGI908) -----------------------
def _example_label(ex: Any) -> str:
    """How an executable example is named in findings (its name, or #index)."""
    return ex.name or f"#{ex.index}"


def _timing_key(obj_id: Any, ex: Any) -> str:
    """Stable key for an example's recorded wall-clock time."""
    return f"{obj_id.qualified()}#{ex.index}"


def _run_executable(con: Any, ex: Any, timeout: float) -> dict[int, tuple[list[str], list[Any]]]:
    """Run every statement of an executable example in order.

    Returns a map of statement index -> (columns, rows) for each statement that
    declares an ``expected_result`` (others are executed but not materialized).
    Raises on any failure — the examples are a must-run contract, so a
    mandatory-filter rejection is a real failure (the example wasn't
    self-contained), not a skip.
    """
    captured: dict[int, tuple[list[str], list[Any]]] = {}
    for i, stmt in enumerate(ex.statements):
        sql = (stmt.sql or "").strip().rstrip(";")
        if not sql:
            continue
        result = run_with_timeout(con, lambda q=sql: con.execute(q), timeout)
        if getattr(stmt, "has_expected", False):
            rows = run_with_timeout(con, lambda r=result: r.fetchall(), timeout)
            cols = [d[0] for d in result.description] if result.description else []
            captured[i] = (cols, list(rows or []))
    return captured


def _stringify_tree(x: Any) -> Any:
    """Coerce a JSON/result value to a comparable tree (leaves as strings)."""
    if isinstance(x, dict):
        return {str(k): _stringify_tree(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_stringify_tree(v) for v in x]
    return None if x is None else str(x)


def _json_cell(v: Any) -> Any:
    """Coerce a result cell to a JSON-native value (str fallback for the rest)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)  # Decimal, date/time, uuid, etc. -> their string form


def _render_result(cols: list[str], rows: list[Any]) -> list[Any]:
    """Render a result as the canonical, interpretable form for expected_result.

    A list of row-objects (``{column: value}``) so it is self-documenting and can
    be pasted straight back as ``expected_result``. Falls back to a list of cells
    for a row when column names are unavailable.
    """
    out: list[Any] = []
    for r in rows:
        cells = list(r)
        if cols and len(cols) == len(cells):
            out.append({str(c): _json_cell(v) for c, v in zip(cols, cells, strict=False)})
        else:
            out.append([_json_cell(v) for v in cells])
    return out


def _render_actual(cols: list[str], rows: list[Any], limit: int = 240) -> str:
    """A compact JSON string of the actual result for a debugging hint."""
    try:
        text = json.dumps(_render_result(cols, rows), default=str)
    except (TypeError, ValueError):
        text = repr(rows)
    return text if len(text) <= limit else text[:limit] + "…"


def _result_matches(expected: Any, cols: list[str], rows: list[Any]) -> bool:
    """True when ``expected`` (JSON) matches the result, comparing string leaves.

    Accepts a scalar (1x1 result), a row object / list of row objects (dicts
    keyed by column), a list of rows (lists), or a list of scalars (one column,
    or one row). Cell values are stringified so 6 and "6" compare equal.
    """
    exp = _stringify_tree(expected)
    actual_dicts = _stringify_tree([dict(zip(cols, r, strict=False)) for r in rows])
    actual_lists = _stringify_tree([list(r) for r in rows])
    if not isinstance(expected, (list, dict)):  # scalar
        return len(rows) == 1 and len(rows[0]) == 1 and _stringify_tree(rows[0][0]) == exp
    if isinstance(expected, dict):  # a single row object
        return len(actual_dicts) == 1 and actual_dicts[0] == exp
    if exp in (actual_dicts, actual_lists):  # list of rows
        return True
    if all(not isinstance(e, (list, dict)) for e in exp):  # list of scalars
        if [[c] for c in exp] == actual_lists:  # one column, N rows
            return True
        if len(rows) == 1 and actual_lists[0] == exp:  # one row, N columns
            return True
    return False


@register
class ExecutableExamplesExecute(Rule):
    code = "VGI906"
    name = "executable-examples-execute"
    category = EXEC
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.MACRO,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
    )
    requires_connection = True
    summary = "Every vgi.executable_examples statement must run against the worker."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        timeout = ctx.config.execute_timeout
        items = [
            (obj_id, ex)
            for obj_id, examples, parse_error in ctx.catalog.iter_executable_example_hosts()
            if not parse_error  # VGI507 reports malformed tags
            for ex in examples
            if any((s.sql or "").strip() for s in ex.statements)
        ]

        def work(pair: tuple[Any, Any], cur: Any) -> Finding | None:
            obj_id, ex = pair
            label = _example_label(ex)
            start = time.perf_counter()
            try:
                _run_executable(cur, ex, timeout)
            except Exception as e:  # noqa: BLE001 - surface engine/timeout error
                return self.finding(
                    ctx,
                    obj_id,
                    f"executable example {label!r} failed: {type(e).__name__}: {e}",
                    "make every statement run as written: catalog-qualify "
                    "references (catalog.schema.name), include any required "
                    "filters, and make the example self-contained and "
                    "re-runnable (e.g. CREATE OR REPLACE for any setup)",
                )
            # Record elapsed so VGI908 can flag slow examples without re-running.
            ctx.exec_timings[_timing_key(obj_id, ex)] = time.perf_counter() - start
            return None

        results = map_queries(con, items, work, ctx.config.execute_concurrency)
        yield from (f for f in results if f is not None)


@register
class ExecutableExampleResultMatches(Rule):
    code = "VGI907"
    name = "executable-example-result-matches"
    category = EXEC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.MACRO,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
    )
    requires_connection = True
    summary = "Each executable-example statement's output should match its expected_result."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        timeout = ctx.config.execute_timeout
        items = [
            (obj_id, ex)
            for obj_id, examples, parse_error in ctx.catalog.iter_executable_example_hosts()
            if not parse_error
            for ex in examples
            if any(s.has_expected for s in ex.statements)
        ]

        def work(pair: tuple[Any, Any], cur: Any) -> list[Finding]:
            obj_id, ex = pair
            try:
                captured = _run_executable(cur, ex, timeout)
            except Exception:  # noqa: BLE001 - VGI906 reports execution failures
                return []
            out: list[Finding] = []
            label = _example_label(ex)
            for i, stmt in enumerate(ex.statements):
                if not stmt.has_expected or i not in captured:
                    continue
                cols, rows = captured[i]
                if not _result_matches(stmt.expected_result, cols, rows):
                    actual = _render_actual(cols, rows)
                    out.append(
                        self.finding(
                            ctx,
                            obj_id,
                            f"executable example {label!r} statement #{i} output "
                            f"does not match expected_result; actual: {actual}",
                            "copy the actual output above into expected_result "
                            "(canonical form: a list of row-objects keyed by column, "
                            'e.g. [{"col": value}]). Comparison stringifies cells '
                            "(NULL -> null, true/false lowercase, numbers as printed "
                            "e.g. 1.0 not 1) and matches rows in order",
                        )
                    )
            return out

        for findings in map_queries(con, items, work, ctx.config.execute_concurrency):
            yield from findings


@register
class ExecutableExampleSlow(Rule):
    code = "VGI908"
    name = "executable-example-slow"
    category = EXEC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.MACRO,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
    )
    requires_connection = True
    summary = "An executable example slower than options.slow_example_seconds bloats CI."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        threshold = ctx.config.slow_example_seconds
        if not threshold or threshold <= 0:
            return
        timeout = ctx.config.execute_timeout
        for obj_id, examples, parse_error in ctx.catalog.iter_executable_example_hosts():
            if parse_error:
                continue
            for ex in examples:
                if not any((s.sql or "").strip() for s in ex.statements):
                    continue
                key = _timing_key(obj_id, ex)
                elapsed = ctx.exec_timings.get(key)
                if elapsed is None:
                    # VGI906 didn't record it (e.g. it's disabled) — time it once
                    # here so the check still works, then cache for reuse.
                    start = time.perf_counter()
                    try:
                        _run_executable(con, ex, timeout)
                    except Exception:  # noqa: BLE001 - VGI906 reports failures
                        continue
                    elapsed = time.perf_counter() - start
                    ctx.exec_timings[key] = elapsed
                if elapsed > threshold:
                    label = _example_label(ex)
                    yield self.finding(
                        ctx,
                        obj_id,
                        f"executable example {label!r} is slow ({elapsed:.1f}s > {threshold:g}s)",
                        "speed up the example (smaller inputs, add a LIMIT, avoid "
                        "full scans) — slow examples run on every lint and bloat CI; "
                        "raise options.slow_example_seconds if this is expected",
                    )


def _star_from_example(f: Function) -> str | None:
    """A ``SELECT * FROM f(...)`` derived from one of ``f``'s own example queries.

    Reuses a known-good, binding call (its literal args) from the function's
    examples and rewrites the projection to ``*`` so DESCRIBE sees the full result
    schema, not just the columns the example happened to project.
    """
    for ex in f.examples:
        if blank(ex.sql):
            continue
        star = select_star_call_sql(ex.sql or "", f.name)
        if star:
            return star
    return None


def _canon_live(cur: Any, type_str: str | None, timeout: float) -> str | None:
    """Canonicalize a declared type via the live worker (so worker types resolve)."""
    t = (type_str or "").strip()
    if not t or any(bad in t for bad in (";", "--", "/*", "\n", "\r")):
        return None
    try:
        res = run_with_timeout(cur, lambda: cur.execute("SELECT typeof(NULL::" + t + ")"), timeout)
        row = run_with_timeout(cur, lambda r=res: r.fetchone(), timeout)
    except Exception:  # noqa: BLE001 - unknown/invalid type -> can't canonicalize
        return None
    return str(row[0]) if row and row[0] is not None else None


@register
class ResultSchemaMatches(Rule):
    code = "VGI910"
    name = "result-schema-matches"
    category = EXEC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    requires_connection = True
    summary = "A table function's declared result schema must match what it returns (via DESCRIBE)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        cat = ctx.catalog
        timeout = ctx.config.execute_timeout
        # Only table functions that declare a schema and are not backed by a static
        # table (whose real columns VGI324 already cross-checks).
        targets = [
            f
            for f in cat.iter_all_functions()
            if f.kind is ObjectKind.TABLE_FUNCTION
            and not cat.find_table_like(f.name, f.schema)
            and (f.result_columns or f.result_dynamic_tables)
        ]

        def work(f: Function, cur: Any) -> list[Finding]:
            return self._check_one(ctx, cur, f, timeout)

        for result in map_queries(con, targets, work, ctx.config.execute_concurrency):
            yield from result

    def _check_one(self, ctx: RuleContext, cur: Any, f: Function, timeout: float) -> list[Finding]:
        star = _star_from_example(f)
        if star is None:
            return []  # no example calls this function -> nothing to describe
        try:
            res = run_with_timeout(cur, lambda q=star: cur.execute("DESCRIBE " + q), timeout)
            rows = run_with_timeout(cur, lambda r=res: r.fetchall(), timeout)
        except Exception:  # noqa: BLE001 - execution failures are VGI901's concern, not this rule's
            return []
        actual = [(str(r[0]), str(r[1])) for r in (rows or []) if r and r[0] is not None]
        if f.result_columns:
            return self._compare_static(ctx, cur, f, actual, timeout)
        return self._compare_dynamic(ctx, f, actual)

    def _compare_static(
        self, ctx: RuleContext, cur: Any, f: Function, actual: list[tuple[str, str]], timeout: float
    ) -> list[Finding]:
        out: list[Finding] = []
        actual_types = dict(actual)
        actual_names = [n for n, _ in actual]
        declared = [
            ((c.name or "").strip(), c.type) for c in f.result_columns if (c.name or "").strip()
        ]
        declared_names = [n for n, _ in declared]
        aset, dset = set(actual_names), set(declared_names)
        for name in declared_names:
            if name not in aset:
                out.append(
                    self.finding(
                        ctx,
                        f.id,
                        f"result schema declares column {name!r} that the function does not return",
                        "remove the column or fix its name to match the function's output",
                    )
                )
        for name in actual_names:
            if name not in dset:
                out.append(
                    self.finding(
                        ctx,
                        f.id,
                        f"the function returns column {name!r} not in vgi.result_columns_schema",
                        "add the column to the declared schema",
                    )
                )
        for name, dtype in declared:
            acanon = actual_types.get(name)
            if acanon is None:
                continue
            dcanon = _canon_live(cur, dtype, timeout)
            if dcanon is None:  # invalid/unknown declared type -> VGI322, can't compare
                continue
            if dcanon != acanon and dcanon.upper() != acanon.upper():
                out.append(
                    self.finding(
                        ctx,
                        f.id,
                        f"result column {name!r} is declared {dcanon} but the "
                        f"function returns {acanon}",
                        "fix the declared type to match the function's output",
                    )
                )
        if dset == aset and declared_names != actual_names:
            out.append(
                Finding(
                    code=self.code,
                    severity=min(ctx.severity, Severity.INFO),
                    category=self.category,
                    object_id=f.id,
                    message="declared result-column order does not match the function's output",
                    hint="reorder vgi.result_columns_schema to match the returned columns",
                )
            )
        return out

    def _compare_dynamic(
        self, ctx: RuleContext, f: Function, actual: list[tuple[str, str]]
    ) -> list[Finding]:
        # The example exercises one variant, so a declared column being absent is
        # expected; only a returned column declared by NO variant is a defect.
        declared_names = {
            (c.name or "").strip()
            for table in f.result_dynamic_tables
            for c in table.columns
            if (c.name or "").strip()
        }
        out: list[Finding] = []
        for name, _atype in actual:
            if name not in declared_names:
                out.append(
                    self.finding(
                        ctx,
                        f.id,
                        f"the function returns column {name!r} not declared in any "
                        "vgi.result_dynamic_columns_md variant",
                        "add the column to the appropriate variant table",
                    )
                )
        return out


# --- scan probes (VGI911 / VGI912) -------------------------------------------
#
# The vgi extension reports the shape of the batches a worker put on the wire as
# `extra_info` on its TABLE_SCAN operator, e.g.
#
#     Batches:     "4 (rows: min 1000, avg 2500, max 3000)"
#     Batch Bytes: "78.1 KiB"
#
# These count RecordBatches as they arrived, *before* DuckDB re-slices them to
# STANDARD_VECTOR_SIZE, so they are the only view of the worker's own chunking.
# We read them from `get_profiling_information()` rather than the EXPLAIN ANALYZE
# text, whose fixed-width box wraps the value across lines.

_BATCHES_RE = re.compile(
    r"(?P<count>\d+)\s*\(\s*rows:\s*min\s*(?P<min>\d+),\s*avg\s*(?P<avg>\d+),\s*max\s*(?P<max>\d+)"
)
_BYTES_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]+)\s*$")
_BYTE_UNITS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


def _parse_bytes(text: Any) -> int | None:
    """Decode the extension's human-readable byte string (``"390.6 KiB"``)."""
    m = _BYTES_RE.match(str(text or ""))
    if not m:
        return None
    unit = _BYTE_UNITS.get(m.group(2).lower())
    return None if unit is None else int(float(m.group(1)) * unit)


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"  # pragma: no cover - unreachable, GiB exits above


@dataclass(frozen=True)
class BatchShape:
    """How one worker scan chunked its result onto the wire."""

    function: str | None
    batches: int
    rows_min: int
    rows_avg: int
    rows_max: int
    bytes_total: int | None

    @property
    def avg_bytes(self) -> int | None:
        """Mean bytes per batch, or None when the extension reported no size."""
        if self.bytes_total is None or self.batches <= 0:
            return None
        return self.bytes_total // self.batches


@dataclass(frozen=True)
class ScanProbe:
    """Outcome of one ``SELECT * FROM <obj> LIMIT n`` against the live worker."""

    sql: str
    elapsed: float = 0.0
    shapes: tuple[BatchShape, ...] = ()
    timed_out: bool = False
    error: str | None = None
    skipped: bool = False  # a mandatory-filter policy rejected the bare scan


def _shape_from_extra(extra: dict[str, Any]) -> BatchShape | None:
    m = _BATCHES_RE.search(str(extra.get("Batches") or ""))
    if not m:
        return None
    name = extra.get("Function") or extra.get("Table")
    return BatchShape(
        function=str(name) if name else None,
        batches=int(m.group("count")),
        rows_min=int(m.group("min")),
        rows_avg=int(m.group("avg")),
        rows_max=int(m.group("max")),
        bytes_total=_parse_bytes(extra.get("Batch Bytes")),
    )


def _scan_shapes(cur: Any) -> tuple[BatchShape, ...]:
    """Every worker scan's batch shape in the last profiled query on ``cur``.

    Only the vgi extension sets a ``Batches`` key, so non-VGI scans (``range``,
    a local table) are skipped without needing to match on operator type.
    """
    try:
        info = cur.get_profiling_information()
    except Exception:  # noqa: BLE001 - profiling unsupported / disabled -> no shapes
        return ()
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            return ()
    out: list[BatchShape] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        extra = node.get("extra_info")
        if isinstance(extra, dict):
            shape = _shape_from_extra(extra)
            if shape is not None:
                out.append(shape)
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                walk(child)

    walk(info)
    return tuple(out)


def _run_probe(cur: Any, sql: str, timeout: float) -> ScanProbe:
    """Scan ``sql`` on a disposable cursor, capturing timing and batch shape."""
    start = time.perf_counter()

    def elapsed() -> float:
        return time.perf_counter() - start

    try:
        run_with_timeout(cur, lambda: cur.execute("SET enable_profiling='no_output'"), timeout)
        res = run_with_timeout(cur, lambda: cur.execute(sql), timeout)
        run_with_timeout(cur, lambda r=res: r.fetchall(), timeout)
    except QueryTimeout:
        return ScanProbe(sql=sql, elapsed=elapsed(), timed_out=True)
    except Exception as e:  # noqa: BLE001 - classified below
        if is_filter_policy_error(e):
            return ScanProbe(sql=sql, elapsed=elapsed(), skipped=True)
        if is_bind_error(e):
            # An unbindable probe means a broken example/definition, which
            # VGI901/VGI903/VGI906 already report — don't double-count it here.
            return ScanProbe(sql=sql, elapsed=elapsed(), skipped=True)
        return ScanProbe(sql=sql, elapsed=elapsed(), error=f"{type(e).__name__}: {e}")
    return ScanProbe(sql=sql, elapsed=elapsed(), shapes=_scan_shapes(cur))


def _scan_targets(ctx: RuleContext) -> list[tuple[Any, str, str]]:
    """(object id, display name, probe SQL) for every scannable worker relation.

    Tables and views scan directly. A table function needs a binding call, so it
    is probed only when one of its own examples supplies one — and only when no
    static table already exposes it (that table is probed instead).
    """
    cat = ctx.catalog
    qualifier = cat.qualifier
    limit = int(ctx.config.scan_limit)
    out: list[tuple[Any, str, str]] = []
    for obj in cat.iter_table_like():
        relation = f'"{qualifier}"."{obj.schema}"."{obj.name}"'
        out.append((obj.id, obj.name, f"SELECT * FROM {relation} LIMIT {limit}"))
    for f in cat.iter_all_functions():
        if f.kind is not ObjectKind.TABLE_FUNCTION or cat.find_table_like(f.name, f.schema):
            continue
        star = _star_from_example(f)
        if star:
            out.append((f.id, f.name, f"SELECT * FROM ({star}) AS _vgi_probe LIMIT {limit}"))
    return out


def scan_probes(ctx: RuleContext) -> list[tuple[Any, str, ScanProbe]]:
    """Probe every scannable relation once per run (memoized on the context)."""
    if ctx._scan_probes is not None:
        return ctx._scan_probes  # type: ignore[no-any-return]
    con = ctx.connection
    targets = _scan_targets(ctx) if con is not None else []
    if not targets:
        ctx._scan_probes = []
        return ctx._scan_probes  # type: ignore[no-any-return]
    timeout = ctx.config.scan_timeout or ctx.config.execute_timeout

    def work(item: tuple[Any, str, str], cur: Any) -> ScanProbe:
        return _run_probe(cur, item[2], timeout)

    # Isolated cursors: a wedged scan must not poison the rest of the run. The
    # probe swallows QueryTimeout into `timed_out`, so say so explicitly —
    # otherwise the wedged cursor would be close()d, and close() blocks on it.
    results = map_isolated_queries(
        con,
        targets,
        work,
        ctx.config.execute_concurrency,
        wedged=lambda p: p.timed_out,
    )
    ctx._scan_probes = [(t[0], t[1], p) for t, p in zip(targets, results, strict=True)]
    return ctx._scan_probes  # type: ignore[no-any-return]


_SCAN_TARGETS = (ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.TABLE_FUNCTION)


@register
class ScanResponds(Rule):
    code = "VGI911"
    name = "scan-responds"
    category = EXEC
    default_severity = Severity.ERROR
    targets = _SCAN_TARGETS
    requires_connection = True
    summary = "Every table, view, and table function must yield its first rows promptly."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        limit = int(ctx.config.scan_limit)
        timeout = ctx.config.scan_timeout or ctx.config.execute_timeout
        for obj_id, name, probe in scan_probes(ctx):
            if probe.timed_out:
                yield self.finding(
                    ctx,
                    obj_id,
                    f"SELECT * FROM {name} LIMIT {limit} did not return within {timeout:g}s",
                    "the worker scan is hanging or unbounded — emit a first batch promptly "
                    "from next_batch(); a scan blocked inside its first batch cannot be "
                    "cancelled, so it wedges any client that touches it",
                )
            elif probe.error:
                yield self.finding(
                    ctx,
                    obj_id,
                    f"SELECT * FROM {name} LIMIT {limit} failed: {probe.error}",
                    "make the relation scannable — a bare LIMIT read is the first thing "
                    "any client (and any agent) will try",
                )


@register
class ScanBatchShape(Rule):
    code = "VGI912"
    name = "scan-batch-shape"
    category = EXEC
    default_severity = Severity.WARNING
    targets = _SCAN_TARGETS
    requires_connection = True
    summary = "A worker scan should emit bounded batches, not one oversized batch."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cfg = ctx.config
        for obj_id, name, probe in scan_probes(ctx):
            for shape in probe.shapes:
                reason = self._verdict(shape, cfg)
                if reason is None:
                    continue
                scan = (
                    name
                    if not shape.function or shape.function == name
                    else f"{name} (scan of {shape.function})"
                )
                size = "" if shape.avg_bytes is None else f", {_fmt_bytes(shape.avg_bytes)}/batch"
                yield self.finding(
                    ctx,
                    obj_id,
                    f"{scan} emitted {shape.batches} batch(es) "
                    f"(rows: min {shape.rows_min}, avg {shape.rows_avg}, "
                    f"max {shape.rows_max}{size}) — {reason}",
                    "return bounded batches from next_batch() and let the framework call it "
                    "again — one oversized batch defeats LIMIT push-down (the scan cannot stop "
                    "early) and forces the HTTP transport to buffer the whole result set",
                )

    @staticmethod
    def _verdict(shape: BatchShape, cfg: Any) -> str | None:
        """The first threshold this shape breaches, or None when it is well-behaved."""
        if shape.batches == 1 and shape.rows_max > cfg.single_batch_max_rows:
            return (
                f"the entire result arrived as one batch of {shape.rows_max} rows "
                f"(> {cfg.single_batch_max_rows})"
            )
        if shape.rows_avg > cfg.avg_batch_max_rows:
            return f"mean batch is {shape.rows_avg} rows (> {cfg.avg_batch_max_rows})"
        avg_bytes = shape.avg_bytes
        if avg_bytes is not None and avg_bytes > cfg.max_batch_bytes:
            return f"mean batch is {_fmt_bytes(avg_bytes)} (> {_fmt_bytes(cfg.max_batch_bytes)})"
        return None
