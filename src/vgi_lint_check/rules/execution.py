"""VGI9xx — opt-in execution of example queries against the live worker.

These rules require a connection and only run when ``--execute`` is set. Modes:
``explain`` (default, cheapest — validates binding without fetching data),
``limit`` (runs wrapped in a LIMIT), or ``run`` (executes as written).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

from ..connection import ALIAS_RE, sql_str
from ..findings import Category, Finding, Severity
from ..model import AttachOption, Catalog, Function, ObjectKind, Table, View
from ._util import blank, is_filter_policy_error, run_with_timeout
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
    # Illustrative examples (vgi.example_queries / Meta.examples) may need data or
    # context not present at lint time, so a failure is a warning, not a gate.
    # For must-run examples use vgi.executable_examples (VGI906, ERROR).
    default_severity = Severity.WARNING
    targets = _EXAMPLE_TARGETS
    requires_connection = True
    summary = "Illustrative example queries should bind/execute (best-effort; warning)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        mode = ctx.config.execute_mode
        limit = ctx.config.execute_limit
        timeout = ctx.config.execute_timeout
        for obj, ex in _example_sqls(ctx.catalog):
            sql = ex.sql or ""
            prepared = _prepare(sql, mode, limit)
            try:
                run_with_timeout(con, lambda q=prepared: con.execute(q), timeout)
            except Exception as e:  # noqa: BLE001 - surface engine/timeout error
                yield self.finding(
                    ctx,
                    obj.id,
                    f"example #{ex.index} failed: {type(e).__name__}: {e}",
                    "fix the example SQL, or move must-run examples to "
                    f"vgi.executable_examples (VGI906); query: {sql[:120]}",
                )


@register
class ExampleQueriesReturnRows(Rule):
    code = "VGI902"
    name = "example-queries-return-rows"
    category = EXEC
    default_severity = Severity.OFF  # opt-in even beyond --execute
    targets = _EXAMPLE_TARGETS
    requires_connection = True
    summary = "Example queries should return at least one row (limit mode)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        limit = max(1, ctx.config.execute_limit)
        timeout = ctx.config.execute_timeout
        for obj, ex in _example_sqls(ctx.catalog):
            sql = ex.sql or ""
            wrapped = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS _q LIMIT {limit}"
            try:
                rows = run_with_timeout(con, lambda q=wrapped: con.execute(q).fetchall(), timeout)
            except Exception:  # noqa: BLE001 - VGI901 reports execution/timeout errors
                continue
            if not rows:
                yield self.finding(
                    ctx,
                    obj.id,
                    f"example #{ex.index} returned no rows",
                    "use an example that returns data so consumers see output",
                )


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
        for view in ctx.catalog.iter_views():
            relation = f'"{qualifier}"."{view.schema}"."{view.name}"'
            try:
                run_with_timeout(
                    con, lambda r=relation: con.execute(f"EXPLAIN SELECT * FROM {r}"), timeout
                )
            except Exception as e:  # noqa: BLE001 - surface engine/timeout error
                # A mandatory-filter rejection means the view is wired up and
                # enforcing a scan policy, not that it's broken.
                if is_filter_policy_error(e):
                    continue
                yield self.finding(
                    ctx,
                    view.id,
                    f"view does not execute: {type(e).__name__}: {e}",
                    "fix the view definition so it binds and runs against the worker",
                )


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


# --- executable examples (VGI906 / VGI907) --------------------------------
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
        for obj_id, examples, parse_error in ctx.catalog.iter_executable_example_hosts():
            if parse_error:  # VGI507 reports malformed tags
                continue
            for ex in examples:
                if not any((s.sql or "").strip() for s in ex.statements):
                    continue
                label = ex.name or f"#{ex.index}"
                try:
                    _run_executable(con, ex, timeout)
                except Exception as e:  # noqa: BLE001 - surface engine/timeout error
                    yield self.finding(
                        ctx,
                        obj_id,
                        f"executable example {label!r} failed: {type(e).__name__}: {e}",
                        "make every statement run as written: catalog-qualify "
                        "references (catalog.schema.name), include any required "
                        "filters, and make the example self-contained and "
                        "re-runnable (e.g. CREATE OR REPLACE for any setup)",
                    )


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
        for obj_id, examples, parse_error in ctx.catalog.iter_executable_example_hosts():
            if parse_error:
                continue
            for ex in examples:
                if not any(s.has_expected for s in ex.statements):
                    continue
                try:
                    captured = _run_executable(con, ex, timeout)
                except Exception:  # noqa: BLE001 - VGI906 reports execution failures
                    continue
                label = ex.name or f"#{ex.index}"
                for i, stmt in enumerate(ex.statements):
                    if not stmt.has_expected or i not in captured:
                        continue
                    cols, rows = captured[i]
                    if not _result_matches(stmt.expected_result, cols, rows):
                        actual = _render_actual(cols, rows)
                        yield self.finding(
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
