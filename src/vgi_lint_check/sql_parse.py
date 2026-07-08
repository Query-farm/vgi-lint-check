"""AST-based reference extraction from SQL.

Uses DuckDB's built-in ``json_serialize_sql`` — a core function, so no community
extension and no worker attach are required; the parse runs on a private
in-memory connection and every rule that consumes this stays **offline** and
unit-testable.

Why an AST and not a regex: the linter needs to know which catalog objects an
example/task *actually calls* (to measure coverage and catch broken references).
A word-boundary regex confuses a column named like a function, matches a name
inside a string literal, and — most importantly for VGI — cannot see a
table-function invoked in ``FROM`` (``forecast_hourly(1, 2)``), which is the
primary surface of table-centric workers.

In the serialized tree:

* every callable — scalar, aggregate, macro, **and** table-function — is a
  ``FUNCTION`` node carrying ``function_name`` / ``schema`` / ``catalog`` and a
  ``children`` arg list (the ``TABLE_FUNCTION`` wrapper is empty; its ``.function``
  child is the real node);
* tables/views are ``BASE_TABLE`` nodes carrying ``catalog_name`` /
  ``schema_name`` / ``table_name``;
* an argument's ``type`` (``VALUE_CONSTANT`` vs ``COLUMN_REF`` vs a nested
  ``FUNCTION``) distinguishes a live example from one that only passes literals.

Defensive contract (mirrors :mod:`vgi_lint_check.tags`): nothing here raises. A
syntactically invalid or unparseable statement yields ``None`` so callers fall
back to the regex heuristics rather than dropping a finding.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

__all__ = ["Ref", "ParsedRefs", "parse_refs"]


@dataclass(frozen=True)
class Ref:
    """One object reference recovered from a SQL statement.

    ``catalog`` / ``schema`` are the empty string when the reference was written
    unqualified; ``name`` is always the bare object name. ``is_function`` marks a
    call site (scalar/aggregate/macro/table-function) versus a ``BASE_TABLE``
    table/view reference. ``const_only_args`` is true only for a function call
    whose every argument is a literal constant (an example that demonstrates the
    call's *shape* but never feeds it a real column/expression).
    """

    catalog: str
    schema: str
    name: str
    is_function: bool
    const_only_args: bool = False


@dataclass(frozen=True)
class ParsedRefs:
    """The functions and tables a single statement references."""

    functions: tuple[Ref, ...]
    tables: tuple[Ref, ...]

    @property
    def all(self) -> tuple[Ref, ...]:
        """Every reference, functions then tables."""
        return self.functions + self.tables


# A private, lazily-opened in-memory connection used solely for
# ``json_serialize_sql``. haybarn wraps DuckDB (the bare ``duckdb`` module is not
# importable in this environment); a plain connection with nothing loaded is
# enough, since json_serialize_sql is a core built-in.
_conn: Any | None = None
_conn_lock = threading.Lock()
_conn_broken = False


def _connection() -> Any | None:
    global _conn, _conn_broken
    if _conn_broken:
        return None
    if _conn is None:
        with _conn_lock:
            if _conn is None and not _conn_broken:
                try:
                    import haybarn

                    _conn = haybarn.connect()
                except Exception:
                    _conn_broken = True
                    return None
    return _conn


def _serialize(sql: str) -> dict[str, Any] | None:
    """Return the parsed AST for ``sql``, or ``None`` if it will not serialize."""
    con = _connection()
    if con is None:
        return None
    try:
        # json_serialize_sql surfaces syntax errors inside the JSON (error=true)
        # rather than raising; a genuinely un-serializable input raises, which we
        # swallow. Parameterized so the SQL text is never interpolated.
        with _conn_lock:
            row = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        doc = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or doc.get("error"):
        return None
    return doc


def _collect_cte_names(node: Any, out: set[str]) -> None:
    """Gather the names bound by WITH clauses (so their uses aren't read as tables).

    DuckDB serializes a WITH map as ``cte_map.map`` entries whose ``key`` is the
    CTE name.
    """
    if isinstance(node, dict):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map") or []:
                if isinstance(entry, dict):
                    key = entry.get("key")
                    if isinstance(key, str) and key:
                        out.add(key.lower())
        for value in node.values():
            _collect_cte_names(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_cte_names(value, out)


def _arg_const_only(children: Any) -> bool:
    """True if ``children`` is a non-empty arg list of only literal constants."""
    if not isinstance(children, list) or not children:
        return False
    seen = False
    for child in children:
        if not isinstance(child, dict):
            return False
        seen = True
        if child.get("type") != "VALUE_CONSTANT":
            return False
    return seen


def _walk(node: Any, cte_names: set[str], funcs: list[Ref], tables: list[Ref]) -> None:
    if isinstance(node, dict):
        name = node.get("function_name")
        if isinstance(name, str) and name:
            funcs.append(
                Ref(
                    catalog=str(node.get("catalog") or ""),
                    schema=str(node.get("schema") or ""),
                    name=name,
                    is_function=True,
                    const_only_args=_arg_const_only(node.get("children")),
                )
            )
        if node.get("type") == "BASE_TABLE":
            table = node.get("table_name")
            if isinstance(table, str) and table:
                catalog = str(node.get("catalog_name") or "")
                schema = str(node.get("schema_name") or "")
                # An unqualified use of a WITH-bound name is not a base table.
                if not (not catalog and not schema and table.lower() in cte_names):
                    tables.append(
                        Ref(
                            catalog=catalog,
                            schema=schema,
                            name=table,
                            is_function=False,
                        )
                    )
        for value in node.values():
            _walk(value, cte_names, funcs, tables)
    elif isinstance(node, list):
        for value in node:
            _walk(value, cte_names, funcs, tables)


def parse_refs(sql: str) -> ParsedRefs | None:
    """Extract the object references from one SQL statement.

    Returns ``None`` when the statement cannot be parsed (the caller should fall
    back to a regex heuristic); otherwise a :class:`ParsedRefs` — possibly empty,
    which legitimately means "parsed fine, referenced nothing" (e.g. ``SELECT 1``).
    """
    if not sql or not sql.strip():
        return None
    doc = _serialize(sql)
    if doc is None:
        return None
    cte_names: set[str] = set()
    _collect_cte_names(doc, cte_names)
    funcs: list[Ref] = []
    tables: list[Ref] = []
    _walk(doc, cte_names, funcs, tables)
    return ParsedRefs(functions=tuple(funcs), tables=tuple(tables))
