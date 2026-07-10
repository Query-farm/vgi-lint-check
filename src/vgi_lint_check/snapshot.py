"""Bulk reads of DuckDB system tables into plain row dicts.

A ``Snapshot`` is taken before and after ATTACH. Catalog objects are scoped from
the *after* snapshot by ``database_name == alias``; globally-registered settings
and pragmas (which carry no catalog qualifier) are scoped by the before/after
diff (see ``diff.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# (attribute, sql) — one bulk query each. Selecting * keeps us resilient to
# column additions; the loader reads columns by name with .get().
_SYSTEM_TABLES = {
    "databases": "SELECT * FROM duckdb_databases()",
    "schemas": "SELECT * FROM duckdb_schemas()",
    "tables": "SELECT * FROM duckdb_tables()",
    "columns": "SELECT * FROM duckdb_columns()",
    "views": "SELECT * FROM duckdb_views()",
    "functions": "SELECT * FROM duckdb_functions()",
    "constraints": "SELECT * FROM duckdb_constraints()",
    "settings": "SELECT * FROM duckdb_settings()",
}


@dataclass
class Snapshot:
    """Raw system-table rows captured from one connection state."""

    databases: list[dict[str, Any]] = field(default_factory=list)
    schemas: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    columns: list[dict[str, Any]] = field(default_factory=list)
    views: list[dict[str, Any]] = field(default_factory=list)
    functions: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    settings: list[dict[str, Any]] = field(default_factory=list)


def _rows(con: Any, sql: str) -> list[dict[str, Any]]:
    cur = con.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]


def take_snapshot(con: Any) -> Snapshot:
    """Read all tracked system tables into a :class:`Snapshot`."""
    return Snapshot(**{attr: _rows(con, sql) for attr, sql in _SYSTEM_TABLES.items()})


# Selecting * keeps us resilient to column additions (newer vgi extensions add
# per-argument constraint columns — arg_default/arg_choices/arg_range/arg_pattern);
# the loader reads columns by name with .get(). An explicit column list would raise
# a binder error against an older extension missing a listed column and — swallowed
# by the except below — silently drop ALL argument metadata.
_FUNCTION_ARGUMENTS_SQL = """
SELECT * FROM vgi_function_arguments()
WHERE catalog_name = ?
ORDER BY function_name, field_index
"""


def fetch_function_arguments(con: Any, alias: str) -> list[dict[str, Any]]:
    """Per-argument metadata from ``vgi_function_arguments()``, scoped to ``alias``.

    Returns ``[]`` when the table function is unavailable — it only exists in
    newer ``vgi`` extensions, and the linter and the extension version
    independently, so an older extension must degrade silently, never crash.
    """
    try:
        cur = con.execute(_FUNCTION_ARGUMENTS_SQL, [alias])
    except Exception:  # noqa: BLE001 - version skew: table function not present
        return []
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]


# Selecting * (rather than just ``handler``) keeps us resilient to column
# additions/renames; the caller reads the ``handler`` column by name with .get().
_COPY_FORMATS_SQL = """
SELECT * FROM vgi_copy_formats()
WHERE catalog_name = ?
"""


def fetch_copy_handlers(con: Any, alias: str) -> list[dict[str, Any]]:
    """Copy-format handler rows from ``vgi_copy_formats()``, scoped to ``alias``.

    The ``vgi`` SDK registers each custom ``COPY ... (FORMAT 'x')`` handler as a
    table function under its handler name, but that function binds *only* inside a
    COPY statement and hard-errors on direct invocation — and COPY statements can
    never be serialized by ``json_serialize_sql``, so no example/test SQL can ever
    both parse (for coverage) and execute. Such handlers are therefore not part of
    the coverable surface; the caller uses the ``handler`` column to exclude them.

    Returns ``[]`` when the table function is unavailable — it only exists in
    newer ``vgi`` extensions, so an older extension must degrade silently (the
    linter then behaves exactly as before), never crash.
    """
    try:
        cur = con.execute(_COPY_FORMATS_SQL, [alias])
    except Exception:  # noqa: BLE001 - version skew: table function not present
        return []
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]
