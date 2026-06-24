"""VGI11x (structure) — schema organization."""

from __future__ import annotations

from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import ObjectKind
from .base import Rule, RuleContext
from .registry import register


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
    default_severity = Severity.OFF  # opt-in; also gated by max_schema_objects
    targets = (ObjectKind.SCHEMA,)
    summary = "Flag a schema with more objects than options.max_schema_objects."

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
