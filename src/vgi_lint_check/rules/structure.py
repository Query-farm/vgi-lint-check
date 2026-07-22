"""VGI11x (structure) — schema organization."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import Function, ObjectId, ObjectKind
from .base import Rule, RuleContext
from .registry import register

# Cross-object consistency floor: too few names to infer a convention.
_CONSISTENCY_FLOOR = 4
_SNAKE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")
# Words ending in 's' that are singular — don't treat them as plurals (VGI144).
_SINGULAR_S = frozenset(
    {"status", "series", "analysis", "metadata", "address", "process", "class", "index", "axis"}
)


def _name_style(name: str) -> str:
    """Classify a name's case/separator style into a bucket."""
    if _SNAKE.match(name):
        return "snake"
    if "-" in name:
        return "kebab"
    if name.isupper():
        return "screaming"
    if re.search(r"[A-Z]", name):
        return "pascal" if name[:1].isupper() else "camel"
    return "other"


def _to_snake(name: str) -> str:
    """Best-effort snake_case suggestion for an outlier name."""
    s = re.sub(r"[-\s]+", "_", name)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    return s.lower()


def _is_plural(name: str) -> bool:
    """Heuristic: does the last token of ``name`` read as a plural noun?"""
    base = name.split("_")[-1].lower()
    if base in _SINGULAR_S or len(base) < 3:
        return False
    return base.endswith("s") and not base.endswith(("ss", "us", "is"))


@register
class SchemaNotEmpty(Rule):
    code = "VGI110"
    name = "schema-not-empty"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCHEMA,)
    summary = "A schema should contain at least one table, view, or function."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if not (s.tables or s.views or s.functions):
                yield self.finding(
                    ctx,
                    s.id,
                    "schema is empty — no tables, views, or functions",
                    "add the objects this schema is meant to expose, or drop the "
                    "schema if it was registered by mistake",
                )


@register
class SchemaObjectCount(Rule):
    code = "VGI117"
    name = "schema-object-count"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING  # gated by max_schema_objects (default 50)
    targets = (ObjectKind.SCHEMA,)
    summary = "A schema with more than options.max_schema_objects objects is hard to explore."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        limit = ctx.config.options.max_schema_objects
        if limit and limit > 0:
            for s in ctx.catalog.iter_schemas():
                count = len(s.tables) + len(s.views) + len(s.functions)
                if count > limit:
                    yield self.finding(
                        ctx,
                        s.id,
                        f"schema has {count} objects (> {limit})",
                        "split the schema into smaller, focused schemas",
                    )


@register
class ExcessiveTableCount(Rule):
    code = "VGI134"
    name = "excessive-table-count"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "Warn when a catalog defines more tables than options.max_tables."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        limit = ctx.config.options.max_tables
        if not limit or limit <= 0:
            return
        count = sum(1 for _ in ctx.catalog.iter_tables())
        if count > limit:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"catalog defines {count} tables (> {limit})",
                "this many tables is hard for agents to explore — group related "
                "data, or raise options.max_tables if it is intentional",
            )


@register
class ExcessiveFunctionCount(Rule):
    code = "VGI135"
    name = "excessive-function-count"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "Warn when a catalog defines more functions than options.max_functions."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        limit = ctx.config.options.max_functions
        if not limit or limit <= 0:
            return
        count = sum(1 for _ in ctx.catalog.iter_all_functions())
        if count > limit:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"catalog defines {count} functions (> {limit})",
                "this many functions is hard for agents to explore — consolidate "
                "them, or raise options.max_functions if it is intentional",
            )


@register
class LongTableName(Rule):
    code = "VGI136"
    name = "long-table-name"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Warn on table/view names longer than options.max_table_name_length."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        limit = ctx.config.options.max_table_name_length
        if not limit or limit <= 0:
            return
        for t in ctx.catalog.iter_table_like():
            n = len(t.name or "")
            if n > limit:
                yield self.finding(
                    ctx,
                    t.id,
                    f"{t.kind} name is {n} characters (> {limit})",
                    "use a shorter, memorable name so it is easy to type and read",
                )


@register
class LongFunctionName(Rule):
    code = "VGI137"
    name = "long-function-name"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "Warn on function names longer than options.max_function_name_length."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        limit = ctx.config.options.max_function_name_length
        if not limit or limit <= 0:
            return
        for f in ctx.catalog.iter_all_functions():
            n = len(f.name or "")
            if n > limit:
                yield self.finding(
                    ctx,
                    f.id,
                    f"function name is {n} characters (> {limit})",
                    "use a shorter, memorable name so it is easy to type and read",
                )


@register
class RedundantNamePrefix(Rule):
    code = "VGI142"
    name = "redundant-name-prefix"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "Object names shouldn't carry a redundant retrieval verb (get_/list_)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        prefixes = [p.lower() for p in ctx.config.options.redundant_name_prefixes if p]
        if not prefixes:
            return
        objects = [(t.id, t.name, str(t.kind)) for t in ctx.catalog.iter_table_like()]
        objects += [(f.id, f.name, "function") for f in ctx.catalog.iter_all_functions()]
        for oid, name, kind in objects:
            low = (name or "").lower()
            for p in prefixes:
                # Prefix + at least one more char, so `playlist`/`budget` are safe
                # and a bare `get`/`list` isn't flagged.
                if low.startswith(p) and len(low) > len(p):
                    stripped = (name or "")[len(p) :]
                    yield self.finding(
                        ctx,
                        oid,
                        f"{kind} name {name!r} starts with the redundant prefix {p!r}",
                        f"a {kind} is already a queryable collection — consider renaming "
                        f"{name!r} to {stripped!r} so it reads naturally in SQL",
                    )
                    break


@register
class NameStyleConsistent(Rule):
    code = "VGI143"
    name = "name-style-consistent"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "Object names should share one case/separator style (e.g. snake_case)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        named: list[tuple[ObjectId, str]] = [(s.id, s.name) for s in cat.iter_schemas()]
        named += [(t.id, t.name) for t in cat.iter_table_like()]
        named += [(f.id, f.name) for f in cat.iter_all_functions()]
        if len(named) < _CONSISTENCY_FLOOR:
            return
        styles = Counter(_name_style(n) for _oid, n in named)
        dominant, dom_count = styles.most_common(1)[0]
        # Only judge when there's a clear house style (>=70% share).
        if dom_count / len(named) < 0.7:
            return
        for oid, name in named:
            if _name_style(name) != dominant:
                suggestion = f" (e.g. {_to_snake(name)!r})" if dominant == "snake" else ""
                yield self.finding(
                    ctx,
                    oid,
                    f"name {name!r} is {_name_style(name)} but the worker's names are "
                    f"{dominant} ({dom_count}/{len(named)})",
                    f"rename to match the {dominant} house style{suggestion} so the API "
                    "reads consistently",
                )


@register
class TableNameNumberConsistent(Rule):
    code = "VGI144"
    name = "table-name-number-consistent"
    category = Category.STRUCTURE
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Tables/views should be consistently singular or plural, not a mix."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        objs = list(ctx.catalog.iter_table_like())
        if len(objs) < _CONSISTENCY_FLOOR:
            return
        buckets: dict[bool, list[tuple[ObjectId, str]]] = {True: [], False: []}  # is_plural -> …
        for t in objs:
            buckets[_is_plural(t.name)].append((t.id, t.name))
        plural, singular = buckets[True], buckets[False]
        # Only flag when both conventions are present (a genuine mix).
        if not plural or not singular:
            return
        minority = plural if len(plural) <= len(singular) else singular
        want = "singular" if minority is plural else "plural"
        for oid, name in minority:
            yield self.finding(
                ctx,
                oid,
                f"table/view name {name!r} is "
                f"{'plural' if minority is plural else 'singular'} but most are "
                f"{'singular' if minority is plural else 'plural'} "
                f"({len(singular)} singular / {len(plural)} plural)",
                f"make table/view naming consistent — prefer the {want} form used by the majority",
            )


# A view body that is a bare pass-through of a parameterless table-function call:
#   SELECT <*|col-list> FROM [catalog.][schema.]fn()
# The trailing `$` after the empty argument list rejects any WHERE/JOIN/GROUP/
# ORDER/LIMIT/UNION or additional source — those would make the view do real work.
_PASSTHROUGH_VIEW = re.compile(
    r"""(?isx)
    ^\s*SELECT\s+(?P<cols>.+?)\s+FROM\s+
    (?P<src>[\w".]+)\s*\(\s*\)\s*$
    """
)
# Strips an optional `CREATE [OR REPLACE] [TEMP…] VIEW <name> [(cols)] AS` prefix,
# so the rule works whether sql_definition is the full DDL or just the SELECT.
_VIEW_DDL_PREFIX = re.compile(r"(?is)^\s*CREATE\s+.*?\bVIEW\b.*?\bAS\b\s+")
# A single bare (optionally qualified/quoted) column reference — no calls,
# operators, literals, CASE, or `AS` renames. Used to confirm the select list is
# a pure projection and not a transformation.
_BARE_COLUMN = re.compile(r'^[\w".]+$')


def _is_passthrough_projection(cols: str) -> bool:
    """True when ``cols`` is ``*`` or a comma list of bare column references."""
    cols = cols.strip()
    if cols == "*":
        return True
    parts = [p.strip() for p in cols.split(",")]
    return bool(parts) and all(_BARE_COLUMN.match(p) for p in parts)


@register
class ViewWrapsTableFunction(Rule):
    code = "VGI145"
    name = "view-wraps-table-function"
    category = Category.STRUCTURE
    default_severity = Severity.ERROR
    targets = (ObjectKind.VIEW,)
    summary = "A view that only wraps a parameterless table function should be a table."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        # Index parameterless table functions by name for O(1) lookup.
        param_free_tfs = {
            f.name
            for f in cat.iter_all_functions()
            if f.kind is ObjectKind.TABLE_FUNCTION and not f.parameters
        }
        if not param_free_tfs:
            return
        for view in cat.iter_views():
            body = view.sql_definition
            if not body:
                continue
            body = _VIEW_DDL_PREFIX.sub("", body.strip()).rstrip(";").strip()
            m = _PASSTHROUGH_VIEW.match(body)
            if not m or not _is_passthrough_projection(m.group("cols")):
                continue
            # Last dotted segment of the source is the function name.
            fn = m.group("src").split(".")[-1].strip('"')
            if fn not in param_free_tfs:
                continue
            yield self.finding(
                ctx,
                view.id,
                f"view trivially wraps the parameterless table function "
                f"{fn}() and returns its columns unchanged — a view over a "
                f"table function is pure indirection",
                "expose this data as a regular table that scans the function "
                "(a scan-backed table) instead of a view over a table function, "
                "so consumers get SELECT * FROM schema.name with no redundant "
                "view layer and no parentheses",
            )


@register
class TableFunctionsWithoutBrowsableTable(Rule):
    code = "VGI146"
    name = "table-functions-without-browsable-table"
    category = Category.STRUCTURE
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = (
        "A worker with table functions but no browsable table/view makes an agent "
        "guess arguments before it can see any data."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        # Any directly-browsable relation — a plain table or a view, including a
        # table backing a table function — satisfies this: an agent can SELECT *
        # from it without knowing arguments.
        if any(True for _ in cat.iter_table_like()):
            return
        tfs = [f for f in cat.iter_all_functions() if f.kind is ObjectKind.TABLE_FUNCTION]
        if not tfs:
            return
        # The escape hatch this rule's own fix hint names, now actually honored.
        #
        # For a worker whose every entry point takes a genuine per-key argument
        # (a document to parse, an address to look up), there is no honest
        # browsable slice to expose. The only object that would satisfy this rule
        # is a view cataloguing the worker's own functions — which VGI327 forbids
        # as a duplicate of duckdb_functions(). Firing here would leave the author
        # with no compliant move, so the rule instead accepts the alternative it
        # already recommends: discovery made cheap on the functions themselves.
        if _discovery_is_cheap(tfs):
            return
        yield self.finding(
            ctx,
            cat.id,
            f"worker exposes {len(tfs)} table function(s) with no browsable table or view, "
            "and its functions are not documented well enough to substitute",
            "an agent must know a function's arguments before it can retrieve any "
            "data. If a browsable entry point is feasible, expose a plain table or a "
            "no-required-argument (table-backed) function that returns a sensible "
            "default slice. If arguments are genuinely required (per-key lookups, "
            "unbounded/rate-limited upstreams), make discovery cheap instead — give "
            "every table function a described example query and a description on "
            "every argument, and this rule stands down",
        )


def _discovery_is_cheap(tfs: list[Function]) -> bool:
    """True when every table function is self-describing enough to browse without one.

    Deliberately strict on *every* function, not a majority: one undocumented
    entry point is exactly the one an agent will guess wrong about.
    """
    for f in tfs:
        if not any((ex.description or "").strip() for ex in f.examples):
            return False
        # Arguments are only readable on a vgi extension new enough to expose
        # vgi_function_arguments(); when the list is empty we cannot judge, and
        # VGI312 already gates the documentation itself, so don't double-report.
        if any(not (a.description or "").strip() for a in f.arguments):
            return False
    return True
