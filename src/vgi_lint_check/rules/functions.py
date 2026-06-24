"""VGI3xx — function and macro documentation.

Per-parameter docs are not exposed by ``duckdb_functions()``; VGI301/302 split by
whether the function exposes parameters so exactly one fires per undocumented
function. Table-functions are excluded — their documentation lives on the table.
"""

from __future__ import annotations

from ..findings import Category, Severity
from ..model import ObjectKind
from ._util import blank
from .base import Rule
from .registry import register

FUNC = Category.FUNCTIONS


@register
class FunctionDescription(Rule):
    code = "VGI301"
    name = "function-description"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.MACRO, ObjectKind.AGGREGATE)
    summary = "Functions/macros without parameters still need a description."

    def check(self, ctx):
        for f in ctx.catalog.iter_functions():
            if not f.parameters and blank(f.description) and blank(f.comment):
                yield self.finding(
                    ctx, f.id, f"{f.function_type} has no description",
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

    def check(self, ctx):
        for f in ctx.catalog.iter_functions():
            if f.parameters and blank(f.description) and blank(f.comment):
                params = ", ".join(f.parameters)
                yield self.finding(
                    ctx, f.id,
                    f"{f.function_type} takes parameters ({params}) but has no description",
                    "add a description covering the parameters and the return value",
                )


@register
class MacroExample(Rule):
    code = "VGI303"
    name = "macro-example"
    category = FUNC
    default_severity = Severity.INFO
    targets = (ObjectKind.MACRO,)
    summary = "Macros should ship at least one example query showing usage."

    def check(self, ctx):
        for m in ctx.catalog.iter_macros():
            if not m.examples:
                yield self.finding(
                    ctx, m.id, "macro has no example query",
                    "add a 'vgi.example_queries' tag showing the macro in use",
                )
