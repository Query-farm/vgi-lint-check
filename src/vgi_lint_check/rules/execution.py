"""VGI9xx — opt-in execution of example queries against the live worker.

These rules require a connection and only run when ``--execute`` is set. Modes:
``explain`` (default, cheapest — validates binding without fetching data),
``limit`` (runs wrapped in a LIMIT), or ``run`` (executes as written).
"""

from __future__ import annotations

from ..findings import Category, Severity
from ..model import ObjectKind
from ._util import blank
from .base import Rule
from .registry import register

EXEC = Category.EXECUTION


def _example_hosts(catalog):
    yield from catalog.iter_table_like()
    yield from catalog.iter_macros()


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
    targets = (ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.MACRO)
    requires_connection = True
    summary = "Every example query should bind/execute against the worker."

    def check(self, ctx):
        con = ctx.connection
        if con is None:
            return
        mode = ctx.config.execute_mode
        limit = ctx.config.execute_limit
        for obj in _example_hosts(ctx.catalog):
            for ex in obj.examples:
                if blank(ex.sql):
                    continue
                try:
                    con.execute(_prepare(ex.sql, mode, limit))
                except Exception as e:  # noqa: BLE001 - surface engine error
                    yield self.finding(
                        ctx, obj.id,
                        f"example #{ex.index} failed: {type(e).__name__}: {e}",
                        f"fix the example SQL; query: {ex.sql[:120]}",
                    )


@register
class ExampleQueriesReturnRows(Rule):
    code = "VGI902"
    name = "example-queries-return-rows"
    category = EXEC
    default_severity = Severity.OFF  # opt-in even beyond --execute
    targets = (ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.MACRO)
    requires_connection = True
    summary = "Example queries should return at least one row (limit mode)."

    def check(self, ctx):
        con = ctx.connection
        if con is None:
            return
        limit = max(1, ctx.config.execute_limit)
        for obj in _example_hosts(ctx.catalog):
            for ex in obj.examples:
                if blank(ex.sql):
                    continue
                wrapped = f"SELECT * FROM ({ex.sql.rstrip().rstrip(';')}) AS _q LIMIT {limit}"
                try:
                    rows = con.execute(wrapped).fetchall()
                except Exception:  # noqa: BLE001 - VGI901 reports execution errors
                    continue
                if not rows:
                    yield self.finding(
                        ctx, obj.id,
                        f"example #{ex.index} returned no rows",
                        "use an example that returns data so consumers see output",
                    )
