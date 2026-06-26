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
    TAG_AGENT_TEST_TASKS,
    TAG_DOC_LINKS,
    TAG_EXAMPLE_QUERIES,
    TAG_EXECUTABLE_EXAMPLES,
    AgentTask,
    DocLink,
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
    """Parse a ``vgi.keywords`` value into trimmed keywords.

    Prefers a JSON array of strings (``["a","b"]``); falls back to the legacy
    comma-separated form (``"a, b"``) for back-compat.
    """
    if not value:
        return []
    s = str(value).strip()
    if s.startswith("["):
        try:
            data = json.loads(s)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, list):
            return [str(k).strip() for k in data if str(k).strip()]
    return [kw.strip() for kw in s.split(",") if kw.strip()]


def keywords_is_json_array(value: str | None) -> bool:
    """True when ``value`` is a JSON array of strings (the preferred form)."""
    if not value or not str(value).strip().startswith("["):
        return False
    try:
        data = json.loads(str(value))
    except (ValueError, TypeError):
        return False
    return isinstance(data, list) and all(isinstance(k, str) for k in data)


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


def decode_string_list(value: str | None) -> tuple[list[str], str | None]:
    """Decode a value that must be a JSON array of strings into (items, error)."""
    if value is None or not str(value).strip():
        return [], None
    try:
        data = json.loads(str(value))
    except (ValueError, TypeError) as e:
        return [], f"invalid JSON: {e}"
    if not isinstance(data, list):
        return [], f"expected a JSON array of strings, got {type(data).__name__}"
    if not all(isinstance(k, str) for k in data):
        return [], "every element must be a string"
    return [k.strip() for k in data if k.strip()], None


def decode_doc_links(tags: TagSet) -> tuple[list[DocLink], str | None]:
    """Decode the ``vgi.doc_links`` tag into (links, parse_error).

    Each entry is a URL string or a ``{"title"?, "url"}`` object. Returns
    ([], None) when the tag is absent; ([], "<reason>") on a malformed value.
    """
    raw = tags.get(TAG_DOC_LINKS)
    if raw is None or not str(raw).strip():
        return [], None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        return [], f"invalid JSON: {e}"
    if not isinstance(data, list):
        return [], f"expected a JSON list, got {type(data).__name__}"
    links: list[DocLink] = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            links.append(DocLink(title=None, url=item))
        elif isinstance(item, dict):
            url = item.get("url")
            title = item.get("title")
            links.append(
                DocLink(
                    title=None if title is None else str(title),
                    url=None if url is None else str(url),
                )
            )
        else:
            return [], f"entry #{i} must be a URL string or object ({type(item).__name__})"
    return links, None


def decode_agent_test_tasks(tags: TagSet) -> tuple[list[AgentTask], str | None]:
    """Decode the ``vgi.agent_test_tasks`` tag into (tasks, parse_error).

    Each entry is ``{name, prompt, success_criteria?, reference_sql?, check_sql?,
    unordered?}`` where ``reference_sql`` is a string, a list of strings, or a
    list of ``{description, sql, expected_result?}`` steps (the canonical
    solution sequence). Returns ([], None) when the tag is absent; ([], "<reason>")
    on a malformed value so a rule can flag it.
    """
    raw = tags.get(TAG_AGENT_TEST_TASKS)
    if raw is None or not str(raw).strip():
        return [], None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        return [], f"invalid JSON: {e}"
    if not isinstance(data, list):
        return [], f"expected a JSON list of objects, got {type(data).__name__}"
    tasks: list[AgentTask] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return [], f"entry #{i} is not an object ({type(item).__name__})"
        name = item.get("name")
        prompt = item.get("prompt")
        if not (name and str(name).strip()):
            return [], f"entry #{i} has no 'name'"
        if not (prompt and str(prompt).strip()):
            return [], f"entry #{i} has no 'prompt'"
        ref: list[ExampleStatement] = []
        if item.get("reference_sql") is not None:
            ref, serr = _decode_statements(item.get("reference_sql"))
            if serr is not None:
                return [], f"entry #{i} reference_sql: {serr}"
        crit = item.get("success_criteria")
        check = item.get("check_sql")
        tasks.append(
            AgentTask(
                name=str(name),
                prompt=str(prompt),
                success_criteria=None if crit is None else str(crit),
                reference_statements=ref,
                check_sql=None if check is None else str(check),
                unordered=bool(item.get("unordered", False)),
                ignore_column_names=bool(item.get("ignore_column_names", False)),
                raw=item,
            )
        )
    return tasks, None
