"""Tag normalization and example-query decoding.

DuckDB returns ``tags`` as a MAP(VARCHAR, VARCHAR) which haybarn materializes as
a Python ``dict``. The reserved ``vgi.example_queries`` tag is a JSON-encoded
string (a list of ``{"description", "sql"}`` objects) that we decode defensively
— a malformed value becomes a parse-error string rather than an exception.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .model import (
    TAG_EXAMPLE_QUERIES,
    TAG_EXECUTABLE_EXAMPLES,
    ExampleQuery,
    ExampleStatement,
    ExecutableExample,
    TagSet,
)


def to_tagset(raw: Any) -> TagSet:
    """Coerce a duckdb tags value into a TagSet.

    Accepts a dict (the common haybarn case), a list of (key, value) pairs, or
    None. Values are stringified; non-string keys are dropped.
    """
    if raw is None:
        return TagSet({})
    items: Iterable[tuple[Any, Any]]
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, (list, tuple)):
        pairs: list[tuple[Any, Any]] = []
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs.append((entry[0], entry[1]))
            elif isinstance(entry, dict) and "key" in entry and "value" in entry:
                pairs.append((entry["key"], entry["value"]))
        items = pairs
    else:  # unexpected shape; treat as empty rather than crash
        return TagSet({})
    out: dict[str, str] = {}
    for k, v in items:
        if k is None:
            continue
        out[str(k)] = "" if v is None else str(v)
    return TagSet(out)


def parse_keywords(value: str | None) -> list[str]:
    """Split a ``vgi.keywords`` value (comma-separated) into trimmed keywords."""
    if not value:
        return []
    return [kw.strip() for kw in str(value).split(",") if kw.strip()]


def decode_example_queries(tags: TagSet) -> tuple[list[ExampleQuery], str | None]:
    """Decode the ``vgi.example_queries`` tag into (examples, parse_error).

    Returns ([], None) when the tag is absent. On malformed JSON or an
    unexpected shape, returns ([], "<reason>") so a rule can flag it.
    """
    raw = tags.get(TAG_EXAMPLE_QUERIES)
    if raw is None or not str(raw).strip():
        return [], None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        return [], f"invalid JSON: {e}"
    if not isinstance(data, list):
        return [], f"expected a JSON list of objects, got {type(data).__name__}"
    examples: list[ExampleQuery] = []
    for i, item in enumerate(data):
        if isinstance(item, dict):
            desc = item.get("description")
            sql = item.get("sql")
            examples.append(
                ExampleQuery(
                    index=i,
                    description=None if desc is None else str(desc),
                    sql=None if sql is None else str(sql),
                    raw=item,
                )
            )
        else:
            return [], f"entry #{i} is not an object ({type(item).__name__})"
    return examples, None


def _decode_statements(raw_sql: Any) -> tuple[list[ExampleStatement], str | None]:
    """Normalize an example's ``sql`` (string | [string] | [{description, sql, ...}]).

    A statement object may carry an ``expected_result`` to assert that step's
    output; string statements carry none.
    """
    if raw_sql is None:
        return [], "missing 'sql'"
    if isinstance(raw_sql, str):
        return [ExampleStatement(description=None, sql=raw_sql)], None
    if not isinstance(raw_sql, list):
        return [], f"'sql' must be a string or a list, got {type(raw_sql).__name__}"
    statements: list[ExampleStatement] = []
    for j, step in enumerate(raw_sql):
        if isinstance(step, str):
            statements.append(ExampleStatement(description=None, sql=step))
        elif isinstance(step, dict):
            sql = step.get("sql")
            desc = step.get("description")
            statements.append(
                ExampleStatement(
                    description=None if desc is None else str(desc),
                    sql=None if sql is None else str(sql),
                    expected_result=step.get("expected_result"),
                    has_expected="expected_result" in step,
                )
            )
        else:
            return [], f"statement #{j} must be a string or object ({type(step).__name__})"
    return statements, None


def decode_executable_examples(tags: TagSet) -> tuple[list[ExecutableExample], str | None]:
    """Decode the ``vgi.executable_examples`` tag into (examples, parse_error).

    Each entry is ``{name?, description, sql}`` where ``sql`` is a string, a list
    of strings, or a list of ``{description, sql, expected_result?}`` steps run in
    order. Returns ([], None) when the tag is absent; ([], "<reason>") on a
    malformed value so a rule can flag it.
    """
    raw = tags.get(TAG_EXECUTABLE_EXAMPLES)
    if raw is None or not str(raw).strip():
        return [], None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        return [], f"invalid JSON: {e}"
    if not isinstance(data, list):
        return [], f"expected a JSON list of objects, got {type(data).__name__}"
    examples: list[ExecutableExample] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return [], f"entry #{i} is not an object ({type(item).__name__})"
        statements, serr = _decode_statements(item.get("sql"))
        if serr is not None:
            return [], f"entry #{i}: {serr}"
        name = item.get("name")
        desc = item.get("description")
        examples.append(
            ExecutableExample(
                index=i,
                name=None if name is None else str(name),
                description=None if desc is None else str(desc),
                statements=statements,
                raw=item,
            )
        )
    return examples, None
