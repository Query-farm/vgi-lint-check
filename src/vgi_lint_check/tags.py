"""Tag normalization and example-query decoding.

DuckDB returns ``tags`` as a MAP(VARCHAR, VARCHAR) which haybarn materializes as
a Python ``dict``. The reserved ``vgi.example_queries`` tag is a JSON-encoded
string (a list of ``{"description", "sql"}`` objects) that we decode defensively
— a malformed value becomes a parse-error string rather than an exception.
"""

from __future__ import annotations

import json

from .model import TAG_EXAMPLE_QUERIES, ExampleQuery, TagSet


def to_tagset(raw) -> TagSet:
    """Coerce a duckdb tags value into a TagSet.

    Accepts a dict (the common haybarn case), a list of (key, value) pairs, or
    None. Values are stringified; non-string keys are dropped.
    """
    if raw is None:
        return TagSet({})
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, (list, tuple)):
        items = []
        for entry in raw:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                items.append((entry[0], entry[1]))
            elif isinstance(entry, dict) and "key" in entry and "value" in entry:
                items.append((entry["key"], entry["value"]))
        items = list(items)
    else:  # unexpected shape; treat as empty rather than crash
        return TagSet({})
    out: dict[str, str] = {}
    for k, v in items:
        if k is None:
            continue
        out[str(k)] = "" if v is None else str(v)
    return TagSet(out)


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
