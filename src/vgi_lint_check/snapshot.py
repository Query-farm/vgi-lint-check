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
