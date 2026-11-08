"""``vgi-lint dump``: a verified, versioned JSON inventory of one worker catalog.

One ATTACH serves three purposes, so the document is internally consistent:

1. **inventory** — the catalog's identity, schemas, tables, views and functions,
   attach options (with ``required``/``secret``), secret types, settings, example
   queries, executable examples and agent-task prompts. The ``catalog`` object is
   a superset of the Query.Farm website harvest record (same field names);
2. **lint** — the normal lint over the same attachment, so the score is fresh;
3. **verify** — every ``vgi.executable_examples`` example is executed and its
   ``expected_result`` assertions checked; ``verified`` gates listing.

Scalar/aggregate functions whose declared return type is ``ANY`` (the worker
leaves the type to bind time) get a concrete ``return_type`` by binding a sample
call — from the catalog's own example SQL first, then a typed-``NULL`` call — via
``DESCRIBE`` (bind only, nothing executes).

The document shape is versioned by :data:`SCHEMA_VERSION`; additive changes keep
the number, a breaking change bumps it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import platform
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .connection import (
    WorkerConnectionError,
    apply_setup_sql,
    close_quietly,
    connect_loaded,
    derive_alias,
    validate_alias,
)
from .model import (
    TAG_CATEGORIES,
    TAG_CATEGORY,
    TAG_LICENSE,
    TAG_SOURCE_URL,
    TAG_SUBTITLE,
    TAG_TITLE,
    Catalog,
    ExecutableExample,
    Function,
    ObjectId,
    Table,
)
from .result import VersionResult
from .versions import discover_catalogs

SCHEMA_VERSION = 1

# Return types that mean "decided at bind time" in duckdb_functions().
_UNRESOLVED_TYPES = frozenset({"", "ANY", '"ANY"', "NULL", "UNKNOWN"})
# Kinds whose ANY return type we try to resolve by binding a call.
_RESOLVABLE_KINDS = frozenset({"scalar", "aggregate"})
# Cap on candidate SQL statements tried per function overload.
_MAX_CANDIDATES = 8


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _blank(v: Any) -> Any:
    """Map None / whitespace-only strings to None; pass everything else through."""
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    return v


def _decode_tag_value(v: str | None) -> Any:
    """Decode a tag value that holds JSON (object/array); leave plain text alone."""
    s = (v or "").strip()
    if s[:1] in "[{":
        try:
            return json.loads(s)
        except ValueError:
            return v
    return v


def decode_tags(raw: dict[str, str]) -> dict[str, Any]:
    """Every tag, sorted by key, with JSON-valued tags decoded.

    Args:
        raw: The tag map as read from the system tables.

    Returns:
        A new dict with the same keys and decoded values.
    """
    return {k: _decode_tag_value(v) for k, v in sorted(raw.items())}


def _json_safe(v: Any) -> Any:
    """Coerce a value from a result set into something ``json.dumps`` accepts."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return str(v)


def transport_of(location: str) -> str:
    """Classify a LOCATION by how the extension reaches the worker.

    Args:
        location: The ATTACH ``LOCATION`` string.

    Returns:
        ``subprocess`` for a bare command, otherwise the scheme (``http``,
        ``https``, ``unix``, ``launch``, ``docker``, ``github-auto``, ...).
    """
    loc = location.strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", loc)
    if m:
        return m.group(1).lower()
    if loc.lower().startswith("launch:"):
        return "launch"
    return "subprocess"


def _haybarn_version() -> str | None:
    try:
        import haybarn

        return str(getattr(haybarn, "__version__", None) or "") or None
    except Exception:  # noqa: BLE001 - metadata only
        return None


def _engine_version(con: Any) -> str | None:
    try:
        row = con.execute("SELECT version()").fetchone()
        return str(row[0]) if row else None
    except Exception:  # noqa: BLE001 - metadata only
        return None


def _rows(con: Any, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=False)) for r in cur.fetchall()]


def secret_type_rows(con: Any) -> list[dict[str, Any]]:
    """``duckdb_secret_types()`` rows, or ``[]`` when unavailable.

    Args:
        con: A haybarn connection.

    Returns:
        One dict per registered secret type.
    """
    try:
        return _rows(con, "SELECT * FROM duckdb_secret_types()")
    except Exception:  # noqa: BLE001 - optional metadata
        return []


def _secret_type_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("type") or "")


def diff_secret_types(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Secret types the ATTACH registered (present after, absent before).

    The vgi extension registers each secret type a worker declares at ATTACH
    time (``extension = 'vgi'``), so the before/after difference is exactly the
    worker's own secret types.

    Args:
        before: ``duckdb_secret_types()`` rows before ATTACH.
        after: ``duckdb_secret_types()`` rows after ATTACH.

    Returns:
        ``[{name, default_provider, extension}]`` sorted by name.
    """
    seen = {_secret_type_name(r) for r in before}
    out = []
    for r in after:
        name = _secret_type_name(r)
        if name and name not in seen:
            out.append(
                {
                    "name": name,
                    "default_provider": _blank(r.get("default_provider")),
                    "extension": _blank(r.get("extension")),
                }
            )
    return sorted(out, key=lambda d: d["name"])


# --------------------------------------------------------------------------
# return-type resolution
# --------------------------------------------------------------------------
def needs_resolution(return_type: str | None) -> bool:
    """True when a declared return type is a bind-time placeholder (``ANY``).

    Args:
        return_type: ``duckdb_functions().return_type`` for the overload.

    Returns:
        Whether a concrete type must be found by binding a call.
    """
    return (return_type or "").strip().upper() in _UNRESOLVED_TYPES


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def synthetic_call(alias: str, fn: Function) -> str | None:
    """A ``SELECT`` calling ``fn`` with typed ``NULL`` arguments, or None.

    Targets the exact overload by argument types. Returns None when an argument
    type is itself ``ANY``/unknown (no typed NULL can stand in for it) or when a
    type string looks unsafe to interpolate.

    Args:
        alias: Local catalog alias the function lives under.
        fn: The function overload.

    Returns:
        The SQL, or None.
    """
    casts = []
    for t in fn.parameter_types:
        ts = (t or "").strip()
        if not ts or needs_resolution(ts) or any(b in ts for b in (";", "--", "/*", "\n")):
            return None
        casts.append(f"NULL::{ts}")
    name = ".".join(_quote_ident(p) for p in (alias, fn.schema, fn.name))
    return f"SELECT {name}({', '.join(casts)})"


def _candidate_sqls(catalog: Catalog, fn: Function) -> Iterator[str]:
    """Example SQL that may call ``fn``: its own examples first, then the rest."""
    for ex in fn.examples:
        if ex.sql:
            yield ex.sql
    for xx in fn.executable_examples:
        for st in xx.statements:
            if st.sql:
                yield st.sql
    from .corpus import _iter_doc_sql, _iter_exec_sql, _iter_test_sql

    for it in (_iter_doc_sql, _iter_exec_sql, _iter_test_sql):
        for sql, _origin, _where in it(catalog):
            yield sql


@dataclass
class ResolvedType:
    """Outcome of resolving one overload's return type."""

    return_type: str | None
    source: str  # declared | bound | unknown
    bound_from: str | None = None
    error: str | None = None


def resolve_return_type(
    con: Any,
    catalog: Catalog,
    fn: Function,
    declared: str | None,
    *,
    overloads: int,
    timeout: float,
    describe: Callable[[Any, str, float], str] | None = None,
) -> ResolvedType:
    """Resolve a concrete return type for an overload that declares ``ANY``.

    Tries, in order: each call to ``fn`` found in the catalog's example SQL
    (rewritten so the call is the only projection, matched by arity when the
    function is overloaded), then a typed-``NULL`` call that targets this exact
    overload. Each candidate is bound with ``DESCRIBE`` — nothing executes.

    Args:
        con: Live connection with the worker attached.
        catalog: The built catalog (for example SQL and the alias).
        fn: The function overload.
        declared: Its declared ``return_type``.
        overloads: How many overloads share ``fn``'s name and kind.
        timeout: Per-``DESCRIBE`` wall-clock cap in seconds.
        describe: Injection point for tests; defaults to a live ``DESCRIBE``.

    Returns:
        The resolved type and how it was obtained.
    """
    if not needs_resolution(declared):
        return ResolvedType(declared, "declared")
    from .sql_parse import project_call_sqls

    run = describe or _describe_first_type
    arity = len(fn.parameter_types) if overloads > 1 else None
    tried: list[str] = []
    last_err: str | None = None
    synth = synthetic_call(catalog.database, fn)
    seen_src: set[str] = set()
    candidates: list[str] = []
    for src in _candidate_sqls(catalog, fn):
        key = " ".join(src.split()).lower()
        if key in seen_src:
            continue
        seen_src.add(key)
        for sql in project_call_sqls(src, fn.name, arity):
            if sql not in candidates:
                candidates.append(sql)
        if len(candidates) >= _MAX_CANDIDATES:
            break
    # An overloaded name's example call may bind a sibling overload: prefer the
    # exact typed-NULL call there, examples first otherwise (they carry real
    # argument values, which some workers need to pick a type).
    ordered = candidates[:_MAX_CANDIDATES]
    if synth:
        ordered = [synth, *ordered] if overloads > 1 else [*ordered, synth]
    for sql in ordered:
        tried.append(sql)
        try:
            t = run(con, sql, timeout)
        except Exception as e:  # noqa: BLE001 - try the next candidate
            last_err = f"{type(e).__name__}: {' '.join(str(e).split())}"[:300]
            continue
        if t and not needs_resolution(t):
            return ResolvedType(t, "bound", bound_from=sql)
    return ResolvedType(declared, "unknown", error=last_err if tried else "no sample call found")


def _describe_first_type(con: Any, sql: str, timeout: float) -> str:
    """``DESCRIBE <sql>`` on a fresh cursor and return the first column's type."""
    from .rules._util import run_with_timeout

    cur = con.cursor()
    try:
        res = run_with_timeout(cur, lambda: cur.execute(f"DESCRIBE {sql}"), timeout)
        row = run_with_timeout(cur, lambda: res.fetchone(), timeout)
    finally:
        with contextlib.suppress(Exception):
            cur.close()
    if not row:
        raise RuntimeError("DESCRIBE returned no columns")
    return str(row[1])


# --------------------------------------------------------------------------
# catalog serialisation (superset of the website harvest record)
# --------------------------------------------------------------------------
def _exec_sql(ex: ExecutableExample) -> str:
    parts = []
    for st in ex.statements:
        s = (st.sql or "").strip()
        if s:
            parts.append(s if s.endswith(";") else s + ";")
    return "\n".join(parts)


def _constraint_text(c: Any) -> str | None:
    cols = ", ".join(c.columns)
    t = c.constraint_type
    if t == "NOT NULL":
        return None
    if t == "FOREIGN KEY":
        ref = ", ".join(c.referenced_columns)
        return f"FOREIGN KEY ({cols}) REFERENCES {c.referenced_table}({ref})"
    if t == "CHECK":
        expr = c.expression or ""
        return expr if expr.upper().startswith("CHECK") else f"CHECK ({expr})"
    return f"{t} ({cols})"


def _argument(a: Any) -> dict[str, Any]:
    """One ``vgi_function_arguments()`` row in harvest shape plus the full detail."""
    return {
        "name": a.name,
        "type": a.type,
        "description": _blank(a.description),
        "default": _blank(a.default),
        "named": bool(a.calls_by_name),
        "position": a.position,
        "is_const": a.is_const,
        "is_named": a.is_named,
        "is_positional": a.is_positional,
        "is_varargs": a.is_varargs,
        "is_table_input": a.is_table_input,
        "is_any_type": a.is_any_type,
        "choices": _decode_tag_value(a.choices) if a.choices else None,
        "value_range": _blank(a.value_range),
        "pattern": _blank(a.pattern),
    }


def _function_kind(f: Function) -> str:
    ft = f.function_type
    if ft == "table":
        return "table_in_out" if any(a.is_table_input for a in f.arguments) else "table"
    if ft in ("scalar", "aggregate", "macro", "table_macro"):
        return ft
    return "other"


def _result_columns(f: Function) -> list[dict[str, Any]]:
    if f.native_result_columns:
        return [
            {"name": c.name, "type": c.data_type, "description": _blank(c.comment)}
            for c in f.native_result_columns
        ]
    return [
        {"name": c.name, "type": c.type, "description": _blank(c.description)}
        for c in f.all_result_columns()
    ]


@dataclass
class _Examples:
    """Catalog-wide example accumulator (deduped by normalized SQL)."""

    items: list[dict[str, Any]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)

    def add(self, title: Any, desc: Any, sql: Any) -> None:
        s = (sql or "").strip()
        if not s:
            return
        key = re.sub(r"\s+", " ", s).rstrip(";").strip()
        if key in self.seen:
            return
        self.seen.add(key)
        self.items.append({"title": _blank(title), "description": _blank(desc), "sql": s})

    def harvest(self, eq: list[Any], ex: list[ExecutableExample]) -> list[str]:
        out = []
        for e in eq:
            raw = e.raw if isinstance(e.raw, dict) else {}
            self.add(raw.get("title") or raw.get("name"), e.description, e.sql)
            if e.sql:
                out.append(e.sql)
        for x in ex:
            sql = _exec_sql(x)
            self.add(x.name, x.description, sql)
            if sql:
                out.append(sql)
        return out


def serialize_catalog(
    cat: Catalog,
    con: Any,
    *,
    resolve_types: bool = True,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Serialize a built catalog (with the worker still attached on ``con``).

    The field names are a superset of the Query.Farm website harvest record
    (``src/data/vgi-workers/schema.ts`` in the website repo).

    Args:
        cat: The built catalog.
        con: Live connection with the worker attached (for nullability, declared
            return types and bind-time type resolution).
        resolve_types: Resolve ``ANY`` return types by binding a sample call.
        timeout: Per-``DESCRIBE`` cap in seconds for type resolution.

    Returns:
        The ``catalog`` object of the dump document.
    """
    from .tags import decode_categories, decode_example_queries, parse_keywords

    alias = cat.database
    ret_types: dict[tuple[Any, ...], Any] = {}
    for r in _rows(
        con,
        "SELECT schema_name, function_name, function_type, parameter_types, return_type "
        "FROM duckdb_functions() WHERE database_name = ?",
        [alias],
    ):
        key = (
            r["schema_name"],
            r["function_name"],
            r["function_type"],
            tuple(r["parameter_types"] or []),
        )
        ret_types[key] = r["return_type"]
    nullable: dict[tuple[Any, ...], bool] = {}
    for r in _rows(
        con,
        "SELECT schema_name, table_name, column_name, is_nullable "
        "FROM duckdb_columns() WHERE database_name = ?",
        [alias],
    ):
        nullable[(r["schema_name"], r["table_name"], r["column_name"])] = bool(r["is_nullable"])

    examples = _Examples()
    categories: list[str] = []

    def add_cat(c: str | None) -> None:
        if c and c not in categories:
            categories.append(c)

    cat_eq, _ = decode_example_queries(cat.tags)
    examples.harvest(cat_eq, cat.executable_examples)
    if cat.tags.get(TAG_CATEGORIES):
        for c in decode_categories(cat.tags)[0]:
            add_cat(c.display_title)
    if cat.tags.get(TAG_CATEGORY):
        add_cat((cat.tags.get(TAG_CATEGORY) or "").strip())

    counts = {
        "tables": 0,
        "views": 0,
        "scalar_functions": 0,
        "table_functions": 0,
        "macros": 0,
        "aggregates": 0,
        "return_types_bound": 0,
        "return_types_unknown": 0,
    }
    schemas_out = []
    for s in cat.schemas:
        registry = {c.name: c.display_title for c in s.categories}
        for c in s.categories:
            add_cat(c.display_title)

        def obj_cat(o: Any, registry: dict[str, str] = registry) -> None:
            c = (o.tags.get(TAG_CATEGORY) or "").strip()
            if c:
                add_cat(registry.get(c, c))

        s_eq, _ = decode_example_queries(s.tags)
        examples.harvest(s_eq, s.executable_examples)
        tables_out = []
        tables_by_name: dict[str, dict[str, Any]] = {}
        for t in [*s.tables, *s.views]:
            obj_cat(t)
            examples.harvest(t.examples, t.executable_examples)
            is_view = t.kind.value == "view"
            counts["views" if is_view else "tables"] += 1
            tentry = _table_entry(t, s.name, nullable, is_view)
            tables_out.append(tentry)
            if not is_view:
                tables_by_name[t.name] = tentry

        overloads: dict[tuple[str, str], int] = {}
        for f in s.functions:
            k = (f.name, f.function_type)
            overloads[k] = overloads.get(k, 0) + 1
        funcs_out: list[dict[str, Any]] = []
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for f in s.functions:
            if f.function_type == "table" and f.name in tables_by_name:
                # The table-function row backing a VGI table: fold into the table.
                fn_examples = examples.harvest(f.examples, f.executable_examples)
                tentry = tables_by_name[f.name]
                tentry.setdefault("examples", [])
                for e in fn_examples:
                    if e not in tentry["examples"]:
                        tentry["examples"].append(e)
                if f.arguments and not tentry.get("arguments"):
                    tentry["arguments"] = [_argument(a) for a in _ordered_args(f)]
                continue
            obj_cat(f)
            fn_examples = examples.harvest(f.examples, f.executable_examples)
            args = _function_args(f, overloads[(f.name, f.function_type)])
            kind = _function_kind(f)
            sig = (f.name, kind, tuple((a["name"], a["type"]) for a in args))
            if sig in merged:
                prev = merged[sig]
                for e in fn_examples:
                    if e not in prev["examples"]:
                        prev["examples"].append(e)
                continue
            if kind == "scalar":
                counts["scalar_functions"] += 1
            elif kind == "aggregate":
                counts["scalar_functions"] += 1  # harvest-compatible bucket
                counts["aggregates"] += 1
            elif kind in ("table", "table_in_out"):
                counts["table_functions"] += 1
            elif kind in ("macro", "table_macro"):
                counts["macros"] += 1
            declared = ret_types.get((s.name, f.name, f.function_type, tuple(f.parameter_types)))
            entry: dict[str, Any] = {
                "name": f.name,
                "kind": kind,
                "comment": _blank(f.comment) or _blank(f.description),
                "tags": decode_tags(f.tags.raw),
                "return_type": declared,
                "return_type_source": "declared",
                "declared_return_type": declared,
                "parameter_types": list(f.parameter_types),
                "stability": f.stability,
                "arguments": args,
                "examples": list(dict.fromkeys(fn_examples)),
            }
            if kind in ("table", "table_in_out"):
                entry["result_columns"] = _result_columns(f)
            if kind == "macro" or kind == "table_macro":
                entry["macro_definition"] = _blank(f.macro_definition)
            if kind in _RESOLVABLE_KINDS and needs_resolution(declared):
                if resolve_types:
                    rt = resolve_return_type(
                        con,
                        cat,
                        f,
                        declared,
                        overloads=overloads[(f.name, f.function_type)],
                        timeout=timeout,
                    )
                else:
                    rt = ResolvedType(declared, "unknown", error="type resolution disabled")
                entry["return_type"] = rt.return_type
                entry["return_type_source"] = rt.source
                if rt.bound_from:
                    entry["return_type_bound_from"] = rt.bound_from
                if rt.error and rt.source == "unknown":
                    entry["return_type_error"] = rt.error
                counts[
                    "return_types_bound" if rt.source == "bound" else "return_types_unknown"
                ] += 1
            merged[sig] = entry
            funcs_out.append(entry)
        schemas_out.append(
            {
                "name": s.name,
                "comment": _blank(s.comment),
                "tags": decode_tags(s.tags.raw),
                "tables": tables_out,
                "functions": funcs_out,
            }
        )

    exec_examples = _executable_examples(cat)
    counts["example_queries"] = len(examples.items)
    counts["executable_examples"] = len(exec_examples)
    counts["agent_tasks"] = len(cat.agent_test_tasks)
    counts["attach_options"] = len(cat.attach_options)
    counts["settings"] = len(cat.settings)
    return {
        "catalog": cat.catalog_name,
        "advertised_catalogs": cat.advertised_catalogs,
        "implementation_version": cat.implementation_version,
        "data_version": cat.data_version,
        "data_version_spec": cat.data_version_spec,
        "default_schema": cat.default_schema,
        "vgi_version": cat.vgi_version,
        "title": _blank(cat.tags.get(TAG_TITLE)),
        "subtitle": _blank(cat.tags.get(TAG_SUBTITLE)),
        "summary": _blank(cat.comment),
        "comment": _blank(cat.comment),
        "doc_md": _blank(cat.description_md),
        "doc_llm": _blank(cat.description_llm),
        "keywords": parse_keywords(cat.tags.get("vgi.keywords")),
        "categories": categories,
        "license": _blank(cat.tags.get(TAG_LICENSE)),
        "source_url": _blank(cat.tags.get(TAG_SOURCE_URL)) or _blank(cat.source_url),
        "catalog_tags": decode_tags(cat.tags.raw),
        "releases": [
            {
                "version": r.version,
                "released_at": r.released_at,
                "summary": _blank(r.summary),
                "notes_url": _blank(r.notes_url),
            }
            for r in cat.releases
        ],
        "attach_options": [
            {
                "name": o.name,
                "type": o.type,
                "description": _blank(o.description),
                "required": o.required,
                "default": o.default,
                "secret": o.secret,
            }
            for o in cat.attach_options
        ],
        "settings": [
            {
                "name": st.name,
                "type": st.input_type,
                "description": _blank(st.description),
                "scope": _blank(st.scope),
                "value": _blank(st.value),
            }
            for st in cat.settings
        ],
        "example_queries": examples.items,
        "executable_examples": exec_examples,
        "agent_tasks": [t.prompt for t in cat.agent_test_tasks if t.prompt],
        "agent_test_tasks": [
            {"name": t.name, "prompt": t.prompt} for t in cat.agent_test_tasks if t.prompt
        ],
        "schemas": schemas_out,
        "counts": counts,
    }


def _ordered_args(f: Function) -> list[Any]:
    return sorted(f.arguments, key=lambda a: (a.field_index is None, a.field_index or 0))


def _function_args(f: Function, overloads: int) -> list[dict[str, Any]]:
    """Arguments of one overload, from vgi_function_arguments() when unambiguous."""
    if overloads == 1 and f.arguments:
        return [_argument(a) for a in _ordered_args(f)]
    by_name: dict[str, Any] = {}
    for a in f.arguments:
        by_name.setdefault(a.name, a)
    out = []
    for i, p in enumerate(f.parameters):
        t = f.parameter_types[i] if i < len(f.parameter_types) else None
        found = by_name.get(p)
        if found is not None:
            d = _argument(found)
            d["type"] = t or d["type"]
        else:
            d = {"name": p, "type": t, "description": None, "default": None, "named": False}
        out.append(d)
    return out


def _table_entry(
    t: Table, schema: str, nullable: dict[tuple[Any, ...], bool], is_view: bool
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": t.name,
        "kind": "view" if is_view else "table",
        "comment": _blank(t.comment),
        "tags": decode_tags(t.tags.raw),
        "columns": [
            {
                "name": c.name,
                "type": c.data_type,
                "comment": _blank(c.comment),
                "nullable": nullable.get((schema, t.name, c.name), True),
            }
            for c in t.columns
        ],
        "constraints": [x for x in (_constraint_text(c) for c in t.constraints) if x],
    }
    if t.estimated_size is not None:
        entry["estimated_size"] = t.estimated_size
    return entry


def _executable_examples(cat: Catalog) -> list[dict[str, Any]]:
    out = []
    for host, exs, _err in cat.iter_executable_example_hosts():
        for ex in exs:
            out.append(
                {
                    "host": host.qualified(),
                    "host_kind": str(host.kind),
                    "index": ex.index,
                    "name": ex.name,
                    "description": _blank(ex.description),
                    "statements": [
                        {
                            "sql": st.sql,
                            "description": _blank(st.description),
                            **({"expected_result": st.expected_result} if st.has_expected else {}),
                        }
                        for st in ex.statements
                    ],
                }
            )
    return out


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
@dataclass
class ExampleOutcome:
    """Result of running one executable example."""

    host: str
    index: int
    name: str | None
    status: str  # passed | failed | mismatch | timeout | needs_credentials | invalid
    duration_s: float = 0.0
    error: str | None = None
    mismatches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON form of the outcome."""
        d: dict[str, Any] = {
            "host": self.host,
            "index": self.index,
            "name": self.name,
            "status": self.status,
            "passed": self.status == "passed",
            "duration_s": round(self.duration_s, 3),
            "error": self.error,
        }
        if self.mismatches:
            d["mismatches"] = self.mismatches
        return d


FAILING_STATUSES = frozenset({"failed", "mismatch", "timeout", "invalid"})


def run_example(cur: Any, host: ObjectId, ex: ExecutableExample, timeout: float) -> ExampleOutcome:
    """Run one executable example's statements in order and check expectations.

    Args:
        cur: A cursor (fresh per example, so session state from setup statements
            stays with the example and a wedged scan cannot spread).
        host: The object the example is declared on.
        ex: The example.
        timeout: Per-statement wall-clock cap in seconds.

    Returns:
        The outcome; never raises for a worker/SQL failure.
    """
    from .rules._util import QueryTimeout, is_credential_error
    from .rules.execution import _render_actual, _result_matches, _run_executable

    out = ExampleOutcome(host.qualified(), ex.index, ex.name, "passed")
    start = time.perf_counter()
    try:
        captured = _run_executable(cur, ex, timeout)
    except QueryTimeout as e:
        out.status, out.error = "timeout", str(e)
    except Exception as e:  # noqa: BLE001 - classify any engine/worker failure
        msg = f"{type(e).__name__}: {' '.join(str(e).split())}"[:1000]
        out.status = "needs_credentials" if is_credential_error(e) else "failed"
        out.error = msg
    else:
        for i, st in enumerate(ex.statements):
            if not st.has_expected or i not in captured:
                continue
            cols, rows = captured[i]
            if not _result_matches(st.expected_result, cols, rows):
                out.mismatches.append(
                    {
                        "statement": i,
                        "expected": _json_safe(st.expected_result),
                        "actual": _render_actual(cols, rows, limit=1000),
                    }
                )
        if out.mismatches:
            out.status = "mismatch"
            out.error = f"{len(out.mismatches)} statement(s) did not match expected_result"
    out.duration_s = time.perf_counter() - start
    return out


def verify_examples(
    con: Any,
    cat: Catalog,
    *,
    timeout: float,
    concurrency: int = 1,
    require_credentials: bool = False,
    runner: Callable[[Any, ObjectId, ExecutableExample, float], ExampleOutcome] | None = None,
) -> dict[str, Any]:
    """Execute every ``vgi.executable_examples`` example and summarize.

    ``verified`` is true when every example passed — a credential-gated example
    (the worker said a secret is missing) is reported as ``needs_credentials``
    and does not fail verification unless ``require_credentials`` is set. A
    malformed ``vgi.executable_examples`` tag is an ``invalid`` failure.

    Args:
        con: Live connection with the worker attached.
        cat: The built catalog.
        timeout: Per-statement cap in seconds.
        concurrency: Examples run in parallel (each on its own cursor).
        require_credentials: Count ``needs_credentials`` as a failure.
        runner: Injection point for tests; defaults to :func:`run_example`.

    Returns:
        The ``verify`` object of the dump document.
    """
    from .rules._util import map_isolated_queries

    run = runner or run_example
    outcomes: list[ExampleOutcome] = []
    items: list[tuple[ObjectId, ExecutableExample]] = []
    for host, exs, err in cat.iter_executable_example_hosts():
        if err:
            outcomes.append(
                ExampleOutcome(host.qualified(), -1, None, "invalid", error=f"malformed tag: {err}")
            )
            continue
        for ex in exs:
            if any((s.sql or "").strip() for s in ex.statements):
                items.append((host, ex))

    def work(pair: tuple[ObjectId, ExecutableExample], cur: Any) -> ExampleOutcome:
        return run(cur, pair[0], pair[1], timeout)

    start = time.perf_counter()
    outcomes.extend(
        map_isolated_queries(
            con, items, work, max(1, concurrency), wedged=lambda o: o.status == "timeout"
        )
    )
    failing = set(FAILING_STATUSES)
    if require_credentials:
        failing.add("needs_credentials")
    by_status: dict[str, int] = {}
    for o in outcomes:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    failed = sum(1 for o in outcomes if o.status in failing)
    passed = by_status.get("passed", 0)
    creds = by_status.get("needs_credentials", 0)
    if failed:
        status = "failed"
    elif not outcomes:
        status = "no_examples"
    elif creds:
        status = "passed_with_credential_skips"
    else:
        status = "passed"
    return {
        "enabled": True,
        "verified": failed == 0,
        "status": status,
        "total": len(outcomes),
        "passed": passed,
        "failed": failed,
        "needs_credentials": creds,
        "by_status": by_status,
        "require_credentials": require_credentials,
        "duration_s": round(time.perf_counter() - start, 3),
        "examples": [o.to_dict() for o in outcomes],
    }


# --------------------------------------------------------------------------
# lint summary
# --------------------------------------------------------------------------
def lint_summary(vr: VersionResult, config: Config) -> dict[str, Any]:
    """The ``lint`` object: fresh score, level and finding counts.

    Args:
        vr: The lint result for the dumped data version.
        config: The lint configuration (for ``fail_on`` and execute flags).

    Returns:
        The ``lint`` object of the dump document.
    """
    by_code: dict[str, int] = {}
    for f in vr.findings:
        by_code[f.code] = by_code.get(f.code, 0) + 1
    lv = vr.level
    return {
        "score": vr.score,
        "static_score": vr.quality.static_score,
        "level": lv.level.label,
        "level_number": int(lv.level),
        "level_title": lv.level.title,
        "blocker": lv.blocker,
        "counts": vr.counts(),
        "by_code": dict(sorted(by_code.items())),
        "fail_on": config.fail_on.label,
        "passed": not vr.gating_findings(config.fail_on, bool(config.baseline)),
        "executed": config.execute,
        "coverage": {k: v for k, v in vr.quality.coverage.families.items()},
        "findings": [
            {
                "code": f.code,
                "severity": f.severity.label,
                "category": str(f.category),
                "object": f.object_id.qualified(),
                "object_kind": str(f.object_id.kind),
                "message": f.message,
            }
            for f in sorted(vr.findings, key=lambda x: x.sort_key())
        ],
    }


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _tool_version() -> str:
    from . import __version__

    return str(__version__)


def base_document(location: str) -> dict[str, Any]:
    """The envelope every dump document starts from (also used for failures).

    Args:
        location: The worker LOCATION.

    Returns:
        A document with identity/environment fields and ``status`` unset.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "tool": {"name": "vgi-lint-check", "version": _tool_version()},
        "status": None,
        "error": None,
        "location": location,
        "transport": transport_of(location),
        "environment": {
            "vgi_extension_version": None,
            "haybarn_version": _haybarn_version(),
            "engine_version": None,
            "python": platform.python_version(),
            "platform": f"{platform.system().lower()}_{platform.machine().lower()}",
        },
        "verified": False,
    }


def failure_document(location: str, status: str, error: str) -> dict[str, Any]:
    """A dump document for a worker that could not be attached or dumped.

    Args:
        location: The worker LOCATION.
        status: ``attach_failed`` | ``error`` | ``timeout``.
        error: Human-readable cause.

    Returns:
        A schema-conforming document with ``verified: false`` and no catalog.
    """
    doc = base_document(location)
    doc.update(status=status, error=error, catalog=None, lint=None, secret_types=[])
    doc["verify"] = {"enabled": False, "verified": False, "status": status}
    return doc


def dump_worker(
    location: str,
    *,
    config: Config | None = None,
    alias: str | None = None,
    catalog_name: str | None = None,
    install: bool = True,
    spatial: bool = True,
    data_version: str | None = None,
    verify: bool = True,
    resolve_types: bool = True,
    require_credentials: bool = False,
) -> dict[str, Any]:
    """Attach ``location`` once, lint it, serialize it, and verify its examples.

    Args:
        location: Any ATTACH LOCATION the vgi extension accepts.
        config: Lint configuration (attach options, setup SQL, rule selection).
        alias: Local attach alias (defaults to one derived from the catalog name).
        catalog_name: Which advertised catalog to dump (default: the first).
        install: FORCE INSTALL the vgi extension first.
        spatial: Also load the spatial extension.
        data_version: ``data_version_spec`` to attach (default: worker default).
        verify: Execute the executable examples.
        resolve_types: Resolve ``ANY`` return types by binding sample calls.
        require_credentials: Count credential-gated examples as failures.

    Returns:
        The dump document (``status == "ok"``).

    Raises:
        WorkerConnectionError: The worker could not be reached or attached.
    """
    from . import core

    config = config or Config()
    t0 = time.perf_counter()
    doc = base_document(location)
    con, vgi_version = connect_loaded(
        install=install, spatial=spatial, worker_idle_timeout=config.worker_idle_timeout
    )
    holder: dict[str, Any] = {}
    try:
        doc["environment"]["vgi_extension_version"] = vgi_version
        doc["environment"]["engine_version"] = _engine_version(con)
        apply_setup_sql(con, config.setup_sql)
        try:
            catalogs = discover_catalogs(con, location)
        except Exception as e:  # noqa: BLE001 - surfaced as a connection failure
            raise WorkerConnectionError(f"vgi_catalogs({location!r}) failed: {e}") from e
        discovery = core._choose(catalogs, location, catalog_name)
        advertised = [c.catalog for c in catalogs] or [discovery.catalog]
        advertised_opts = {c.catalog: list(c.attach_options) for c in catalogs}
        local_alias = validate_alias(alias) if alias else derive_alias(discovery.catalog)
        secrets_before = secret_type_rows(con)

        def while_attached(catalog: Catalog, live: Any) -> None:
            holder["secret_types"] = diff_secret_types(secrets_before, secret_type_rows(live))
            holder["catalog"] = serialize_catalog(
                catalog, live, resolve_types=resolve_types, timeout=config.execute_timeout
            )
            if verify:
                holder["verify"] = verify_examples(
                    live,
                    catalog,
                    timeout=config.execute_timeout,
                    concurrency=config.execute_concurrency,
                    require_credentials=require_credentials,
                )

        vr = core._lint_one_version(
            con,
            location,
            discovery,
            advertised,
            advertised_opts,
            local_alias,
            data_version,
            vgi_version,
            config,
            False,
            None,
            None,
            while_attached=while_attached,
        )
    finally:
        close_quietly(con)

    doc["status"] = "ok"
    doc["alias"] = local_alias
    doc["catalog"] = holder["catalog"]
    doc["secret_types"] = holder.get("secret_types", [])
    doc["lint"] = lint_summary(vr, config)
    doc["verify"] = holder.get("verify") or {
        "enabled": False,
        "verified": None,
        "status": "skipped",
    }
    doc["verified"] = bool(doc["verify"].get("verified")) if verify else None
    doc["duration_s"] = round(time.perf_counter() - t0, 3)
    return doc
