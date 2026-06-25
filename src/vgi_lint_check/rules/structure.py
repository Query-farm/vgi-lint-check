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
