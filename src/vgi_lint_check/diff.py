"""Before/after attach diff.

Primary purpose: scope globally-registered objects that carry no catalog
qualifier — worker **settings** (``duckdb_settings()``) and **pragmas**
(``duckdb_functions()`` rows with ``function_type='pragma'``) — by taking the
set difference of before vs. after attach. Also produces a small "what the
worker added" summary (counts by kind) for the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .snapshot import Snapshot


@dataclass
class SnapshotDiff:
    """Worker-scoped settings/pragmas plus a counts-by-kind summary."""

    setting_rows: list[dict[str, Any]] = field(default_factory=list)
    pragma_rows: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


def _setting_key(r: dict[str, Any]) -> Any:
    return r.get("name")


def _pragma_rows(snapshot: Snapshot) -> dict[tuple[Any, Any], dict[str, Any]]:
    out: dict[tuple[Any, Any], dict[str, Any]] = {}
    for r in snapshot.functions:
        if (r.get("function_type") or "").lower() == "pragma":
            out[(r.get("schema_name"), r.get("function_name"))] = r
    return out


def diff_snapshots(before: Snapshot, after: Snapshot, alias: str) -> SnapshotDiff:
    """Diff before/after attach to scope worker settings, pragmas, and counts."""
    # Settings have no catalog qualifier -> the worker's settings are the rows
    # present after attach but not before (the extension's own vgi_* settings
    # are already in `before`).
    before_settings = {_setting_key(r) for r in before.settings}
    setting_rows = [r for r in after.settings if _setting_key(r) not in before_settings]

    before_pragmas = _pragma_rows(before)
    after_pragmas = _pragma_rows(after)
    pragma_rows = [v for k, v in after_pragmas.items() if k not in before_pragmas]

    summary = {
        "schemas": _count_added(before.schemas, after.schemas, alias, "schema_name"),
        "tables": _count_added(before.tables, after.tables, alias, "table_name"),
        "views": _count_added(before.views, after.views, alias, "view_name"),
        "functions": _count_added(before.functions, after.functions, alias, "function_name"),
        "settings": len(setting_rows),
        "pragmas": len(pragma_rows),
    }
    return SnapshotDiff(setting_rows=setting_rows, pragma_rows=pragma_rows, summary=summary)


def _count_added(
    before_rows: list[dict[str, Any]],
    after_rows: list[dict[str, Any]],
    alias: str,
    name_col: str,
) -> int:
    def keys(rows: list[dict[str, Any]]) -> set[tuple[Any, Any]]:
        return {
            (r.get("schema_name"), r.get(name_col)) for r in rows if r.get("database_name") == alias
        }

    return len(keys(after_rows) - keys(before_rows))
