"""VGI5xx — example queries (static checks)."""

from __future__ import annotations

from ..findings import Category, Severity
from ..model import TAG_EXAMPLE_QUERIES, ObjectKind
from ._util import blank
from .base import Rule
from .registry import register

EX = Category.EXAMPLES


def _example_hosts(catalog):
    """Objects that may carry vgi.example_queries: tables, views, macros."""
    yield from catalog.iter_table_like()
    yield from catalog.iter_macros()


@register
class ExampleQueriesPresent(Rule):
    code = "VGI501"
    name = "example-queries-present"
    category = EX
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Tables/views should ship example queries (macros are covered by VGI303)."

    def check(self, ctx):
        # macros have their own example rule (VGI303); avoid double-flagging.
        for obj in ctx.catalog.iter_table_like():
            if not obj.examples and obj.examples_parse_error is None:
                yield self.finding(
                    ctx, obj.id, "no vgi.example_queries",
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

    def check(self, ctx):
        for obj in _example_hosts(ctx.catalog):
            if obj.examples_parse_error:
                yield self.finding(
                    ctx, obj.id,
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

    def check(self, ctx):
        for obj in _example_hosts(ctx.catalog):
            for ex in obj.examples:
                if blank(ex.description):
                    yield self.finding(
                        ctx, obj.id, f"example #{ex.index} has no description",
                        "give every example a human-readable description",
                    )
                if blank(ex.sql):
                    yield self.finding(
                        ctx, obj.id, f"example #{ex.index} has no sql",
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

    def check(self, ctx):
        for s in ctx.catalog.iter_schemas():
            if not s.tags.has(TAG_EXAMPLE_QUERIES):
                yield self.finding(
                    ctx, s.id, "schema has no vgi.example_queries",
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

    def check(self, ctx):
        qualifier = ctx.catalog.qualifier
        if not qualifier:
            return
        needle = f"{qualifier.lower()}."
        for obj in _example_hosts(ctx.catalog):
            for ex in obj.examples:
                if blank(ex.sql):
                    continue
                if needle not in ex.sql.lower():
                    yield self.finding(
                        ctx, obj.id,
                        f"example #{ex.index} does not qualify references with "
                        f"the catalog ({qualifier!r})",
                        f"qualify tables/macros as {qualifier}.schema.name so the "
                        "example runs when the worker is attached as a catalog",
                    )


@register
class ExampleReferencesObject(Rule):
    code = "VGI504"
    name = "example-references-object"
    category = EX
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "An example for an object should reference that object's name."

    def check(self, ctx):
        for obj in ctx.catalog.iter_table_like():
            for ex in obj.examples:
                if ex.sql and obj.name and obj.name.lower() not in ex.sql.lower():
                    yield self.finding(
                        ctx, obj.id,
                        f"example #{ex.index} does not reference {obj.name!r}",
                        "make the example query actually use this object",
                    )
