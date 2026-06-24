"""VGI3xx — function and macro documentation.

Per-parameter docs are not exposed by ``duckdb_functions()``; VGI301/302 split by
whether the function exposes parameters so exactly one fires per undocumented
function. Table-functions are excluded — their documentation lives on the table.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import TAG_RESULT_COLUMNS_MD, ObjectKind
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .registry import register

FUNC = Category.FUNCTIONS

# DuckDB's placeholder names for unnamed/positional parameters.
_UNNAMED_PARAM = re.compile(r"^(col)?\d+$", re.IGNORECASE)


def _is_unnamed(param: str) -> bool:
    return blank(param) or bool(_UNNAMED_PARAM.match(param.strip()))


@register
class FunctionDescription(Rule):
    code = "VGI301"
    name = "function-description"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.MACRO, ObjectKind.AGGREGATE)
    summary = "Functions/macros without parameters still need a description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_functions():
            if not f.parameters and blank(f.description) and blank(f.comment):
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} has no description",
                    "add a description (or COMMENT) explaining what it returns",
                )


@register
class FunctionParametersUndocumented(Rule):
    code = "VGI302"
    name = "function-parameters-undocumented"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.MACRO, ObjectKind.AGGREGATE)
    summary = "A function that takes parameters must describe what it does."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_functions():
            if f.parameters and blank(f.description) and blank(f.comment):
                params = ", ".join(f.parameters)
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} takes parameters ({params}) but has no description",
                    "add a description covering the parameters and the return value",
                )


@register
class FunctionDescriptionQuality(Rule):
    code = "VGI304"
    name = "function-description-quality"
    category = FUNC
    default_severity = Severity.INFO
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.MACRO, ObjectKind.AGGREGATE)
    summary = "A function description should be substantive, not a stub or echo."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_description_chars
        for f in ctx.catalog.iter_functions():
            desc = f.description or f.comment
            if blank(desc):
                continue  # presence handled by VGI301/302
            if is_trivial_echo(desc, f.name):
                yield self.finding(
                    ctx,
                    f.id,
                    f"description just restates the name ({f.name!r})",
                    "describe what the function does and returns",
                )
            elif len((desc or "").strip()) < minlen:
                yield self.finding(
                    ctx,
                    f.id,
                    f"description is very short ({len((desc or '').strip())} < {minlen} chars)",
                    "expand the description so consumers understand the function",
                )


@register
class FunctionArgumentsNamed(Rule):
    code = "VGI305"
    name = "function-arguments-named"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.MACRO,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "All function/macro arguments should be named, not positional."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_all_functions():
            unnamed = [p for p in f.parameters if _is_unnamed(p)]
            if unnamed:
                shown = ", ".join(p or "<empty>" for p in unnamed)
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} has unnamed/positional argument(s): {shown}",
                    "give every parameter a descriptive name",
                )


@register
class FunctionExample(Rule):
    code = "VGI306"
    name = "function-example"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE)
    summary = "Scalar/aggregate functions should ship an example query."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_functions():
            if f.kind in (ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE) and not f.examples:
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} function has no example query",
                    "add a 'vgi.example_queries' tag showing the function in use",
                )


@register
class TableFunctionColumnsDocumented(Rule):
    code = "VGI307"
    name = "table-function-columns-documented"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = (
        "A table function with a dynamic schema (no backing table) must document "
        "its returned columns in a 'vgi.result_columns_md' tag."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        for f in cat.iter_all_functions():
            if f.kind is not ObjectKind.TABLE_FUNCTION:
                continue
            # A table function backed by a static table already has its columns
            # documented via that table's column comments.
            if cat.find_table_like(f.name, f.schema):
                continue
            if not f.tags.has(TAG_RESULT_COLUMNS_MD):
                yield self.finding(
                    ctx,
                    f.id,
                    f"table function has no documented return columns ('{TAG_RESULT_COLUMNS_MD}')",
                    "DuckDB can't expose a dynamic table-function schema — add a "
                    "'vgi.result_columns_md' tag with a Markdown table of the returned "
                    "columns (note any columns that vary by argument)",
                )


@register
class MacroExample(Rule):
    code = "VGI303"
    name = "macro-example"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.MACRO,)
    summary = "Macros should ship at least one example query showing usage."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for m in ctx.catalog.iter_macros():
            if not m.examples:
                yield self.finding(
                    ctx,
                    m.id,
                    "macro has no example query",
                    "add a 'vgi.example_queries' tag showing the macro in use",
                )


@register
class AllScalarFunctionsVolatile(Rule):
    code = "VGI308"
    name = "all-scalar-functions-volatile"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "Every scalar function being VOLATILE usually means stability was never set."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        # Only scalar functions that report a stability (macros/table-functions
        # report None). VOLATILE disables constant-folding/caching, so a worker
        # whose scalars are *all* volatile most likely left the default unset.
        scalars = [
            f
            for f in ctx.catalog.iter_functions()
            if f.kind is ObjectKind.SCALAR_FUNCTION and f.stability
        ]
        if len(scalars) >= 2 and all(f.is_volatile for f in scalars):
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"all {len(scalars)} scalar functions are VOLATILE",
                "set each deterministic scalar function's stability to CONSISTENT "
                "(only truly non-deterministic ones — random/now — should be "
                "VOLATILE); a blanket VOLATILE usually means it was never set",
            )


@register
class VolatileScalarFunction(Rule):
    code = "VGI309"
    name = "volatile-scalar-function"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE)
    summary = "Flag each VOLATILE scalar/aggregate function for a stability audit."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_functions():
            if f.kind not in (ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE):
                continue
            if f.is_volatile:
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} function is VOLATILE",
                    "confirm this function is genuinely non-deterministic; if it "
                    "always returns the same output for the same input, mark it "
                    "CONSISTENT so the engine can optimize it",
                )
