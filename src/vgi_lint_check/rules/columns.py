"""VGI2xx — column documentation."""

from __future__ import annotations

from ..findings import Category, Severity
from ..model import ObjectKind
from ._util import is_trivial_echo
from .base import Rule
from .registry import register

COL = Category.COLUMNS


@register
class ColumnCommentCoverage(Rule):
    code = "VGI201"
    name = "column-comment-coverage"
    category = COL
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "A table/view should document at least the configured share of columns."

    def check(self, ctx):
        thr = ctx.config.options.column_comment_min_ratio
        for t in ctx.catalog.iter_table_like():
            cols = t.columns
            if not cols:
                continue
            documented = sum(1 for c in cols if c.documented)
            ratio = documented / len(cols)
            if ratio < thr:
                yield self.finding(
                    ctx, t.id,
                    f"column comment coverage {documented}/{len(cols)} "
                    f"({ratio:.0%}) below {thr:.0%}",
                    "add a comment to each undocumented column (meaning + units)",
                )


@register
class EveryColumnCommented(Rule):
    code = "VGI202"
    name = "column-comment-required"
    category = COL
    default_severity = Severity.OFF  # opt-in: stricter than VGI201
    targets = (ObjectKind.COLUMN,)
    summary = "Stricter variant: every single column must have a comment."

    def check(self, ctx):
        for c in ctx.catalog.iter_columns():
            if not c.documented:
                yield self.finding(
                    ctx, c.id, "column has no comment",
                    "add a comment describing this column's meaning and units",
                )


@register
class ColumnCommentNotEcho(Rule):
    code = "VGI203"
    name = "column-comment-not-echo"
    category = COL
    default_severity = Severity.INFO
    targets = (ObjectKind.COLUMN,)
    summary = "A column comment should add information, not restate the name."

    def check(self, ctx):
        for c in ctx.catalog.iter_columns():
            if is_trivial_echo(c.comment, c.name):
                yield self.finding(
                    ctx, c.id,
                    f"column comment just restates the name ({c.name!r})",
                    "describe the column's meaning/units instead of echoing its name",
                )
