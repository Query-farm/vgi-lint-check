"""VGI12x — discoverability & marketing.

The worker's metadata is its product listing: these rules make it findable
(search/semantic indexing), trustworthy (provenance/freshness), and compelling
(example coverage). Most are informational or opt-in — they raise the bar for a
worker's "listing" without gating CI.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from ..findings import Category, Finding, Severity
from ..model import ObjectId, ObjectKind
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .registry import register

DISC = Category.DISCOVERABILITY

# Tokens that suggest a column comment states a unit/definition.
_UNIT_HINT = re.compile(
    r"\(|%|\b(per|in|metres?|meters?|km|kg|seconds?|hours?|days?|years?"
    r"|usd|dollars?|degrees?|celsius|fahrenheit|count|number of|ratio"
    r"|percent|index|scale)\b",
    re.IGNORECASE,
)
_NUMERIC_TYPE = re.compile(
    r"\b(INT|INTEGER|BIGINT|SMALLINT|TINYINT|HUGEINT|DECIMAL|NUMERIC|DOUBLE|FLOAT|REAL)\b",
    re.IGNORECASE,
)
_TRIVIAL_SELECT = re.compile(r"^\s*SELECT\s+\*\s+FROM\b", re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join(text.split()).strip().lower()


@register
class DuplicateDescriptions(Rule):
    code = "VGI120"
    name = "duplicate-descriptions"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Many objects sharing one description reads as boilerplate (dup content)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        groups: dict[str, list[Any]] = defaultdict(list)
        for t in ctx.catalog.iter_table_like():
            if not blank(t.comment):
                groups[_norm(t.comment or "")].append(t)
        for objs in groups.values():
            if len(objs) > 1:
                for t in objs:
                    yield self.finding(
                        ctx,
                        t.id,
                        f"description is shared by {len(objs)} objects",
                        "give each object a distinct description so search and "
                        "agents can tell them apart",
                    )


@register
class DescriptionTooShort(Rule):
    code = "VGI121"
    name = "description-too-short"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "A description should be substantive enough to index and read well."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_meaningful_description_chars
        objs = [(s.id, s.comment, s.name) for s in ctx.catalog.iter_schemas()]
        objs += [(t.id, t.comment, t.name) for t in ctx.catalog.iter_table_like()]
        for oid, comment, _name in objs:
            text = (comment or "").strip()
            if text and len(text) < minlen:
                yield self.finding(
                    ctx,
                    oid,
                    f"description is very short ({len(text)} < {minlen} chars)",
                    "expand the description with what it contains and why it's useful",
                )


@register
class DescriptionEchoesName(Rule):
    code = "VGI122"
    name = "description-echoes-name"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "A description that just restates the name adds no searchable signal."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if is_trivial_echo(s.comment, s.name):
                yield self._echo(ctx, s.id, s.name)
        for t in ctx.catalog.iter_table_like():
            if is_trivial_echo(t.comment, t.name):
                yield self._echo(ctx, t.id, t.name)

    def _echo(self, ctx: RuleContext, oid: ObjectId, name: str) -> Finding:
        return self.finding(
            ctx,
            oid,
            f"description just restates the name ({name!r})",
            "describe what the object contains, not just its name",
        )


@register
class ClassifyingTagPresent(Rule):
    code = "VGI123"
    name = "classifying-tag-present"
    category = DISC
    default_severity = Severity.OFF  # opt-in: not every worker uses faceting tags
    targets = (ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Objects should carry a classifying tag (domain/category/...) for faceting."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        keys = ctx.config.options.classifying_tag_keys
        if not keys:
            return
        for s in ctx.catalog.iter_schemas():
            if not any(s.tags.has(k) for k in keys):
                yield self._missing(ctx, s.id, keys)
        for t in ctx.catalog.iter_table_like():
            if not any(t.tags.has(k) for k in keys):
                yield self._missing(ctx, t.id, keys)

    def _missing(self, ctx: RuleContext, oid: ObjectId, keys: list[str]) -> Finding:
        return self.finding(
            ctx,
            oid,
            f"no classifying tag (any of: {', '.join(keys)})",
            "add a classifying tag so the object is findable by facet/topic",
        )


@register
class ColumnUnits(Rule):
    code = "VGI131"
    name = "column-units"
    category = DISC
    default_severity = Severity.OFF  # opt-in: heuristic on numeric columns
    targets = (ObjectKind.COLUMN,)
    summary = "Numeric column comments should state units/definition where relevant."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for c in ctx.catalog.iter_columns():
            if blank(c.comment) or not c.data_type:
                continue
            if _NUMERIC_TYPE.search(c.data_type) and not _UNIT_HINT.search(c.comment or ""):
                yield self.finding(
                    ctx,
                    c.id,
                    "numeric column comment states no unit/definition",
                    "state the unit or definition (e.g. 'depth in km', 'count of …')",
                )


@register
class ReleaseDated(Rule):
    code = "VGI140"
    name = "release-dated"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG,)
    summary = "Published data-version releases should carry a release date (freshness)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for rel in ctx.catalog.releases:
            if blank(rel.released_at):
                yield self.finding(
                    ctx,
                    ctx.catalog.id,
                    f"release {rel.version!r} has no released_at date",
                    "date each release — freshness is a discovery and trust signal",
                )


@register
class ReleaseDocumented(Rule):
    code = "VGI141"
    name = "release-documented"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG,)
    summary = "Releases should have a summary or notes_url ('what's new')."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for rel in ctx.catalog.releases:
            if blank(rel.summary) and blank(rel.notes_url):
                yield self.finding(
                    ctx,
                    ctx.catalog.id,
                    f"release {rel.version!r} has no summary or notes_url",
                    "add a one-line summary or a notes_url describing the release",
                )


@register
class ExamplesNotTrivial(Rule):
    code = "VGI150"
    name = "examples-not-trivial"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Example queries should demonstrate value, not only `SELECT * FROM x`."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for t in ctx.catalog.iter_table_like():
            examples = [e for e in t.examples if not blank(e.sql)]
            if examples and all(_TRIVIAL_SELECT.match(e.sql or "") for e in examples):
                yield self.finding(
                    ctx,
                    t.id,
                    "all example queries are trivial `SELECT *`",
                    "add an example that filters/aggregates/joins to show real value",
                )


@register
class MinimumExamples(Rule):
    code = "VGI151"
    name = "minimum-examples"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG,)
    summary = "A worker should ship a minimum number of example queries overall."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minimum = ctx.config.options.min_example_queries
        total = sum(len(t.examples) for t in ctx.catalog.iter_table_like())
        total += sum(len(f.examples) for f in ctx.catalog.iter_functions())
        if total < minimum:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"worker ships only {total} example queries (< {minimum})",
                "add more example queries — they are the worker's demo for humans and agents",
            )
