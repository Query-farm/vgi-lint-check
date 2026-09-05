"""Check semantic documentation fixtures against the packaged JSON Schemas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vgi_lint_check.semantic_model import SEMANTIC_TAG_SCHEMAS
from vgi_lint_check.semantic_schema import validate_instance


def main() -> int:
    """Validate every examples/semantic tag document; nonzero means docs drifted."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Accepted for CI symmetry.")
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for path in sorted((root / "examples" / "semantic").glob("*.json")):
        values = json.loads(path.read_text(encoding="utf-8"))
        tagged_values: list[tuple[str, object]] = []
        queries: list[object] = []
        if isinstance(values, dict) and isinstance(values.get("catalogs"), list):
            for worker in values["catalogs"]:
                tagged_values.extend(worker.get("catalog_tags", {}).items())
                for table in worker.get("tables", []):
                    tagged_values.extend(table.get("tags", {}).items())
                    for column in table.get("columns", []):
                        tagged_values.extend(column.get("tags", {}).items())
            queries = [case.get("request") for case in values.get("queries", [])]
        else:
            tagged_values = list(values.items())
        for key, value in tagged_values:
            if key == "vgi.agent_test_tasks":
                continue
            schema = SEMANTIC_TAG_SCHEMAS.get(key)
            if schema is None:
                failures.append(f"{path}: unknown semantic tag {key}")
                continue
            failures.extend(f"{path}:{key}: {error}" for error in validate_instance(schema, value))
        for query in queries:
            failures.extend(f"{path}:query: {error}" for error in validate_instance("query", query))
    if failures:
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
