"""Loader for the committed semantic worker/physical-data example."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import haybarn

from vgi_lint_check.loader import build_catalog
from vgi_lint_check.model import Catalog
from vgi_lint_check.snapshot import Snapshot

FIXTURE = (
    Path(__file__).resolve().parent.parent / "examples" / "semantic" / "ecommerce-workers.json"
)


def _tags(values: dict[str, Any]) -> dict[str, str]:
    return {
        key: value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        for key, value in values.items()
    }


def load_example() -> tuple[dict[str, Any], dict[str, Catalog], Any]:
    """Load example metadata through ``build_catalog`` and seed physical DuckDB catalogs."""
    fixture: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalogs: dict[str, Catalog] = {}
    connection = haybarn.connect()
    connection.execute("SET TimeZone = 'UTC'")
    for worker in fixture["catalogs"]:
        alias = str(worker["attachment_alias"])
        schema_name = str(worker["schema"])
        snapshot = Snapshot(
            databases=[
                {
                    "database_name": alias,
                    "comment": f"Example {worker['catalog_name']} semantic worker.",
                    "tags": _tags(worker["catalog_tags"]),
                }
            ],
            schemas=[
                {
                    "database_name": alias,
                    "schema_name": schema_name,
                    "comment": f"Modeled {worker['catalog_name']} objects.",
                    "tags": {},
                }
            ],
            tables=[
                {
                    "database_name": alias,
                    "schema_name": schema_name,
                    "table_name": table["name"],
                    "comment": table["comment"],
                    "column_count": len(table["columns"]),
                    "tags": _tags(table["tags"]),
                }
                for table in worker["tables"]
            ],
            columns=[
                {
                    "database_name": alias,
                    "schema_name": schema_name,
                    "table_name": table["name"],
                    "column_name": column["name"],
                    "column_index": index,
                    "data_type": column["type"],
                    "is_nullable": column.get("nullable", True),
                    "comment": column.get("comment"),
                    "tags": _tags(column.get("tags", {})),
                }
                for table in worker["tables"]
                for index, column in enumerate(table["columns"])
            ],
        )
        catalogs[alias] = build_catalog(
            snapshot,
            alias,
            f"example://{worker['catalog_name']}",
            catalog_name=str(worker["catalog_name"]),
            default_schema=schema_name,
        )

        connection.execute(f"ATTACH ':memory:' AS \"{alias}\"")
        for table in worker["tables"]:
            columns = ", ".join(
                f'"{column["name"]}" {column["type"]}'
                + (" NOT NULL" if column.get("nullable") is False else "")
                for column in table["columns"]
            )
            connection.execute(
                f'CREATE TABLE "{alias}"."{schema_name}"."{table["name"]}" ({columns})'
            )
            placeholders = ", ".join("?" for _ in table["columns"])
            insert = (
                f'INSERT INTO "{alias}"."{schema_name}"."{table["name"]}" VALUES ({placeholders})'
            )
            for row in table["rows"]:
                connection.execute(insert, row)
    return fixture, catalogs, connection
