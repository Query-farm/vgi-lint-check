"""VGI9xx — opt-in execution of example queries against the live worker.

These rules require a connection and only run when ``--execute`` is set. Modes:
``explain`` (default, cheapest — validates binding without fetching data),
``limit`` (runs wrapped in a LIMIT), or ``run`` (executes as written).
"""

from __future__ import annotations

import contextlib
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
    default_severity = Severity.ERROR
    targets = _EXAMPLE_TARGETS
    requires_connection = True
    summary = "Every example query should bind/execute against the worker."

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
                    f"fix the example SQL (or raise execute_timeout); query: {sql[:120]}",
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
