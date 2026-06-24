"""VGI11x (structure) — schema organization. Opt-in."""

from __future__ import annotations

from ..findings import Category, Severity
from ..model import ObjectKind
from .base import Rule
from .registry import register


@register
class SchemaObjectCount(Rule):
    code = "VGI117"
    name = "schema-object-count"
    category = Category.STRUCTURE
    default_severity = Severity.OFF  # opt-in; also gated by max_schema_objects
    targets = (ObjectKind.SCHEMA,)
    summary = "Flag a schema with more objects than options.max_schema_objects."

    def check(self, ctx):
        limit = ctx.config.options.max_schema_objects
        if limit and limit > 0:
            for s in ctx.catalog.iter_schemas():
                count = len(s.tables) + len(s.views) + len(s.functions)
                if count > limit:
                    yield self.finding(
                        ctx, s.id,
                        f"schema has {count} objects (> {limit})",
                        "split the schema into smaller, focused schemas",
                    )
