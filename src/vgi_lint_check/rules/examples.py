"""VGI5xx — example queries (static checks)."""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import (
    TAG_EXAMPLE_QUERIES,
    Catalog,
    Function,
    ObjectKind,
    Table,
    View,
)
from ._util import blank
from .base import Rule, RuleContext
from .registry import register

EX = Category.EXAMPLES

# A complete, self-contained spec of the vgi.executable_examples shape so an agent
# can author a correct tag from a single finding without a second lookup.
_EXECUTABLE_SCHEMA_HINT = (
    "set the vgi.executable_examples tag to a JSON list of "
    '{"name"?, "description", "sql"} objects, where "sql" is a SQL string, a list '
    'of SQL strings, or a list of {"description", "sql", "expected_result"?} steps '
    "run in order. expected_result (optional, per statement) is the statement's "
    'output as a list of row-objects keyed by column, e.g. [{"class": "strong"}] '
    "(a bare scalar or list-of-rows is also accepted). Cells compare as strings "
    "(NULL -> null, booleans lowercase, numbers as printed) and rows in order — on "
    "a mismatch the finding prints the actual output to copy. Catalog-qualify every "
    "reference (catalog.schema.name) and make each example self-contained and "
    "re-runnable so it executes as written when the worker is attached."
)

# Strip single-quoted string literals and -- / /* */ comments so a qualifier
# mentioned only inside a literal/comment does not count as a real reference.
_SQL_LITERAL_OR_COMMENT = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)


def _strip_sql_noise(sql: str) -> str:
    return _SQL_LITERAL_OR_COMMENT.sub(" ", sql)


def _references_catalog(sql: str, qualifier: str) -> bool:
    """True if ``sql`` qualifies an identifier with ``qualifier.`` (word-bounded)."""
    code = _strip_sql_noise(sql)
    pattern = rf"(?<![\w.]){re.escape(qualifier)}\s*\.\s*\w"
    return re.search(pattern, code, re.IGNORECASE) is not None


def _references_identifier(sql: str, name: str) -> bool:
    """True if ``sql`` uses ``name`` as a whole identifier (call or reference).

    Strips string literals and comments first, then matches ``name`` only as a
    complete token — so ``felt`` does not match inside ``unfelt`` or ``felt_at``,
    while a qualified use (``main.felt``) and a call (``felt(...)``) both count.
    """
    code = _strip_sql_noise(sql)
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])"
    return re.search(pattern, code, re.IGNORECASE) is not None


def _example_hosts(catalog: Catalog) -> Iterator[Table | View | Function]:
    """Objects that may carry vgi.example_queries: tables, views, macros."""
    yield from catalog.iter_table_like()
    yield from catalog.iter_macros()


def _named_example_hosts(catalog: Catalog) -> Iterator[Table | View | Function]:
    """Every example-bearing object whose own name an example should use.

    Tables/views plus all function kinds (macro, scalar, aggregate, table
    function) — each carries examples that ought to reference/call it by name.
    """
    yield from catalog.iter_table_like()
    yield from catalog.iter_all_functions()


@register
class ExampleQueriesPresent(Rule):
    code = "VGI501"
    name = "example-queries-present"
    category = EX
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Tables/views should ship example queries (macros are covered by VGI303)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        # macros have their own example rule (VGI303); avoid double-flagging.
        for obj in ctx.catalog.iter_table_like():
            if not obj.examples and obj.examples_parse_error is None:
                yield self.finding(
                    ctx,
                    obj.id,
                    "no vgi.example_queries",
                    "add a 'vgi.example_queries' tag with at least one example",
                )


@register
class ExampleQueriesWellFormed(Rule):
    code = "VGI502"
    name = "example-queries-well-formed"
    category = EX
    default_severity = Severity.ERROR
    targets = (ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.MACRO)
    summary = "The vgi.example_queries tag must be a valid JSON list of objects."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for obj in _example_hosts(ctx.catalog):
            if obj.examples_parse_error:
                yield self.finding(
                    ctx,
                    obj.id,
                    f"vgi.example_queries is not valid: {obj.examples_parse_error}",
                    'use a JSON list of {"description": "...", "sql": "..."} objects',
                )


@register
class ExampleEntriesComplete(Rule):
    code = "VGI503"
    name = "example-entries-complete"
    category = EX
    default_severity = Severity.ERROR
    targets = (ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.MACRO)
    summary = "Each example needs a non-empty description and sql."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for obj in _example_hosts(ctx.catalog):
            for ex in obj.examples:
                if blank(ex.description):
                    yield self.finding(
                        ctx,
                        obj.id,
                        f"example #{ex.index} has no description",
                        "give every example a human-readable description",
                    )
                if blank(ex.sql):
                    yield self.finding(
                        ctx,
                        obj.id,
                        f"example #{ex.index} has no sql",
                        "give every example a non-empty SQL statement",
                    )


@register
class SchemaExampleQueries(Rule):
    code = "VGI506"
    name = "schema-example-queries"
    category = EX
    default_severity = Severity.OFF  # opt-in: schemas rarely carry examples
    targets = (ObjectKind.SCHEMA,)
    summary = "Schemas should carry a vgi.example_queries tag (opt-in)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if not s.tags.has(TAG_EXAMPLE_QUERIES):
                yield self.finding(
                    ctx,
                    s.id,
                    "schema has no vgi.example_queries",
                    "add a 'vgi.example_queries' tag with representative queries",
                )


@register
class ExampleQueriesQualified(Rule):
    code = "VGI505"
    name = "example-queries-qualified"
    category = EX
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.MACRO)
    summary = (
        "Example queries should qualify references with the catalog name "
        "(catalog.schema.table) so they run when the worker is attached."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        qualifier = ctx.catalog.qualifier
        if not qualifier:
            return
        for obj in _example_hosts(ctx.catalog):
            for ex in obj.examples:
                if blank(ex.sql):
                    continue
                if not _references_catalog(ex.sql or "", qualifier):
                    yield self.finding(
                        ctx,
                        obj.id,
                        f"example #{ex.index} does not qualify references with "
                        f"the catalog ({qualifier!r})",
                        f"qualify tables/macros as {qualifier}.schema.name so the "
                        "example runs when the worker is attached as a catalog",
                    )


@register
class ExecutableExamplesWellFormed(Rule):
    code = "VGI507"
    name = "executable-examples-well-formed"
    category = EX
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.MACRO,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = (
        "vgi.executable_examples must be a valid JSON list; each entry needs a "
        "description and at least one non-empty SQL statement."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for obj_id, examples, parse_error in ctx.catalog.iter_executable_example_hosts():
            if parse_error:
                yield self.finding(
                    ctx,
                    obj_id,
                    f"vgi.executable_examples is not valid: {parse_error}",
                    _EXECUTABLE_SCHEMA_HINT,
                )
                continue
            for ex in examples:
                if blank(ex.description):
                    yield self.finding(
                        ctx,
                        obj_id,
                        f"executable example #{ex.index} has no description",
                        "give the example a 'description' (prose an LLM can learn "
                        f"from). {_EXECUTABLE_SCHEMA_HINT}",
                    )
                if not ex.statements or all(blank(s.sql) for s in ex.statements):
                    yield self.finding(
                        ctx,
                        obj_id,
                        f"executable example #{ex.index} has no SQL statement",
                        f"give the example at least one non-empty SQL statement. "
                        f"{_EXECUTABLE_SCHEMA_HINT}",
                    )


@register
class WorkerHasExecutableExamples(Rule):
    code = "VGI509"
    name = "worker-has-executable-examples"
    category = EX
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "A worker should ship at least one vgi.executable_examples (guaranteed-runnable)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        # An empty catalog is the bigger problem (VGI011); don't pile on.
        if not cat.has_objects():
            return
        total = sum(len(examples) for _id, examples, _err in cat.iter_executable_example_hosts())
        if total == 0:
            yield self.finding(
                ctx,
                cat.id,
                "worker ships no executable examples",
                "add a vgi.executable_examples tag to at least one object so agents "
                f"have a guaranteed-runnable, verified example to learn from. "
                f"{_EXECUTABLE_SCHEMA_HINT}",
            )


@register
class TooManyExecutableExamples(Rule):
    code = "VGI508"
    name = "too-many-executable-examples"
    category = EX
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.MACRO,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = (
        "Warn when one object carries more executable examples than "
        "options.max_executable_examples."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        limit = ctx.config.options.max_executable_examples
        if not limit or limit <= 0:
            return
        for obj_id, examples, parse_error in ctx.catalog.iter_executable_example_hosts():
            if parse_error:
                continue
            n = len(examples)
            if n > limit:
                yield self.finding(
                    ctx,
                    obj_id,
                    f"object declares {n} executable examples (> {limit})",
                    "keep a focused, curated set — each one runs against the worker "
                    "under --execute, and a long list is noise for LLMs; move extras "
                    "to vgi.example_queries (illustrative) or raise the limit",
                )


@register
class ExampleReferencesObject(Rule):
    code = "VGI504"
    name = "example-references-object"
    category = EX
    default_severity = Severity.INFO
    targets = (
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.MACRO,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "An example for an object should reference (call) that object by name."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for obj in _named_example_hosts(ctx.catalog):
            name = obj.name
            if not name:
                continue
            verb = "call" if isinstance(obj, Function) else "reference"
            for ex in obj.examples:
                if blank(ex.sql):
                    continue
                if not _references_identifier(ex.sql or "", name):
                    yield self.finding(
                        ctx,
                        obj.id,
                        f"example #{ex.index} does not {verb} {name!r}",
                        f"make the example actually {verb} this object — an example "
                        "that never names it is usually copied from elsewhere",
                    )
