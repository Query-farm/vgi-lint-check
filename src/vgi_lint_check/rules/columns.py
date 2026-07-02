"""VGI2xx — column documentation."""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import ObjectKind
from ._util import base_type, blank, is_trivial_echo
from .base import Rule, RuleContext
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

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        thr = ctx.config.options.column_comment_min_ratio
        for t in ctx.catalog.iter_table_like():
            cols = t.columns
            if not cols:
                continue
            documented = sum(1 for c in cols if c.documented)
            ratio = documented / len(cols)
            if ratio < thr:
                yield self.finding(
                    ctx,
                    t.id,
                    f"column comment coverage {documented}/{len(cols)} "
                    f"({ratio:.0%}) below {thr:.0%}",
                    "add a comment to each undocumented column (meaning + units)",
                )


@register
class EveryColumnCommented(Rule):
    code = "VGI202"
    name = "column-comment-required"
    category = COL
    default_severity = Severity.WARNING  # strict default
    targets = (ObjectKind.COLUMN,)
    summary = "Stricter variant: every single column must have a comment."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for c in ctx.catalog.iter_columns():
            if not c.documented:
                yield self.finding(
                    ctx,
                    c.id,
                    "column has no comment",
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

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for c in ctx.catalog.iter_columns():
            if is_trivial_echo(c.comment, c.name):
                yield self.finding(
                    ctx,
                    c.id,
                    f"column comment just restates the name ({c.name!r})",
                    "describe the column's meaning/units instead of echoing its name",
                )


# A naive (no-zone) timestamp/time type — agents can't interpret it without a tz.
_NAIVE_TS = re.compile(r"\b(TIMESTAMP|DATETIME|TIME)\b", re.IGNORECASE)
_HAS_ZONE = re.compile(r"WITH TIME ZONE|TIMESTAMPTZ|TIMETZ|\bTZ\b", re.IGNORECASE)
_TZ_MENTION = re.compile(
    r"\b(utc|gmt|timezone|time zone|tz|offset|zulu|local time)\b", re.IGNORECASE
)


@register
class TimestampTimezoneDocumented(Rule):
    code = "VGI204"
    name = "timestamp-timezone-documented"
    category = COL
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "A naive TIMESTAMP/TIME column should document its timezone assumption."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for t in ctx.catalog.iter_table_like():
            for c in t.columns:
                dt = c.data_type or ""
                if not _NAIVE_TS.search(dt) or _HAS_ZONE.search(dt):
                    continue
                if c.comment and _TZ_MENTION.search(c.comment):
                    continue
                yield self.finding(
                    ctx,
                    c.id,
                    f"naive {dt} column does not document its timezone",
                    "state the timezone (e.g. UTC) in the column comment, or use "
                    "TIMESTAMP WITH TIME ZONE so values are unambiguous",
                )


@register
class ColumnTypeConsistent(Rule):
    code = "VGI205"
    name = "column-type-consistent"
    category = COL
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "A column name should map to one SQL type across all tables/views (no type drift)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        ignore = {n.lower() for n in ctx.config.options.type_consistency_ignore_names}
        # column name -> {base type: [table names]}
        by_name: dict[str, dict[str, list[str]]] = {}
        for t in ctx.catalog.iter_table_like():
            for c in t.columns:
                if blank(c.data_type) or c.name.lower() in ignore:
                    continue
                bt = base_type(c.data_type)
                if not bt:
                    continue
                tbls = by_name.setdefault(c.name.lower(), {}).setdefault(bt, [])
                if t.name not in tbls:
                    tbls.append(t.name)
        for name, types in by_name.items():
            distinct_tables = {t for tbls in types.values() for t in tbls}
            if len(types) < 2 or len(distinct_tables) < 2:
                continue  # need >=2 distinct base types across >=2 distinct tables
            detail = "; ".join(
                f"{bt.upper()} ({', '.join(sorted(tbls))})" for bt, tbls in sorted(types.items())
            )
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"column {name!r} has {len(types)} different types across tables: {detail}",
                "use one consistent type for the same column concept — differing types make "
                "joins and reasoning harder (add it to options.type_consistency_ignore_names "
                "if the collision is intentional)",
            )
