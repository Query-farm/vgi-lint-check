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
from ..model import (
    TAG_AUTHOR,
    TAG_COPYRIGHT,
    TAG_KEYWORDS,
    TAG_LICENSE,
    TAG_SOURCE_URL,
    TAG_TITLE,
    ObjectId,
    ObjectKind,
    TagSet,
)
from ..tags import parse_keywords
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .registry import register

DISC = Category.DISCOVERABILITY


# Object kinds that can carry a title/keywords tag, with (id, tags, name).
def _taggable(ctx: RuleContext) -> Iterator[tuple[ObjectId, TagSet, str]]:
    cat = ctx.catalog
    yield cat.id, cat.tags, cat.qualifier
    for s in cat.iter_schemas():
        yield s.id, s.tags, s.name
    for t in cat.iter_table_like():
        yield t.id, t.tags, t.name
    for f in cat.iter_functions():
        yield f.id, f.tags, f.name


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


def _looks_like_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


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
class TitlePresent(Rule):
    code = "VGI124"
    name = "title-present"
    category = DISC
    default_severity = Severity.OFF  # opt-in: not every worker uses display titles
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Objects should carry a 'vgi.title' display name (human/marketing)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _taggable(ctx):
            if not tags.has(TAG_TITLE):
                yield self.finding(
                    ctx,
                    oid,
                    "no 'vgi.title' display name",
                    "add a 'vgi.title' tag — a human-friendly name for listings/UIs",
                )


@register
class TitleQuality(Rule):
    code = "VGI125"
    name = "title-quality"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "A 'vgi.title', when set, should differ from the machine name."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, name in _taggable(ctx):
            title = tags.get(TAG_TITLE)
            if not blank(title) and is_trivial_echo(title, name):
                yield self.finding(
                    ctx,
                    oid,
                    f"'vgi.title' just restates the machine name ({name!r})",
                    "use a human-friendly display title, not the identifier",
                )


@register
class KeywordsPresent(Rule):
    code = "VGI126"
    name = "keywords-present"
    category = DISC
    default_severity = Severity.OFF  # opt-in
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Objects should carry 'vgi.keywords' (search terms / synonyms)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _taggable(ctx):
            if not tags.has(TAG_KEYWORDS):
                yield self.finding(
                    ctx,
                    oid,
                    "no 'vgi.keywords' search terms",
                    "add a 'vgi.keywords' tag: comma-separated search terms/synonyms",
                )


@register
class KeywordsWellFormed(Rule):
    code = "VGI127"
    name = "keywords-well-formed"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "'vgi.keywords', when set, should be non-empty with no duplicates."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _taggable(ctx):
            if not tags.has(TAG_KEYWORDS):
                continue
            kws = parse_keywords(tags.get(TAG_KEYWORDS))
            lowered = [k.lower() for k in kws]
            if not kws:
                yield self.finding(
                    ctx,
                    oid,
                    "'vgi.keywords' has no usable keywords",
                    "provide comma-separated keywords, e.g. 'seismic, tremor, magnitude'",
                )
            elif len(set(lowered)) != len(lowered):
                yield self.finding(
                    ctx,
                    oid,
                    "'vgi.keywords' contains duplicate keywords",
                    "remove duplicate keywords",
                )


@register
class SourceUrlPresent(Rule):
    code = "VGI128"
    name = "source-url-present"
    category = DISC
    default_severity = Severity.OFF  # opt-in
    targets = (ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Objects should link to their implementation via 'vgi.source_url'."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _taggable(ctx):
            if oid.kind is ObjectKind.CATALOG:
                continue  # catalog provenance is covered by VGI004 (source_url)
            if not tags.has(TAG_SOURCE_URL):
                yield self.finding(
                    ctx,
                    oid,
                    "no 'vgi.source_url' implementation link",
                    "add a 'vgi.source_url' tag linking to the repo/file that "
                    "implements this object",
                )


@register
class SourceUrlValid(Rule):
    code = "VGI129"
    name = "source-url-valid"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "'vgi.source_url', when set, should be an http(s) URL."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _taggable(ctx):
            url = tags.get(TAG_SOURCE_URL)
            if not blank(url) and not _looks_like_url(url or ""):
                yield self.finding(
                    ctx,
                    oid,
                    f"'vgi.source_url' is not an http(s) URL: {url!r}",
                    "use an absolute http(s) link to the source",
                )


@register
class CatalogAttribution(Rule):
    code = "VGI160"
    name = "catalog-attribution"
    category = DISC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG,)
    summary = "The catalog should declare author, copyright, and license tags."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        tags = ctx.catalog.tags
        for key, what in (
            (TAG_AUTHOR, "author/maintainer ('vgi.author')"),
            (TAG_COPYRIGHT, "copyright notice ('vgi.copyright')"),
            (TAG_LICENSE, "license ('vgi.license')"),
        ):
            if not tags.has(key):
                yield self.finding(
                    ctx,
                    ctx.catalog.id,
                    f"catalog has no {what}",
                    f"add a '{key}' tag so consumers know provenance and terms of use",
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
        # Count every example the worker ships. Use iter_all_functions() (not
        # iter_functions()) so table-function examples are included — a
        # table-function-only worker (e.g. a model-registry worker) keeps all
        # its examples on table-functions, which iter_functions() excludes
        # (they're correlated to a table) and which need not materialize a
        # table row to be counted.
        total = sum(len(t.examples) for t in ctx.catalog.iter_table_like())
        total += sum(len(f.examples) for f in ctx.catalog.iter_all_functions())
        if total < minimum:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"worker ships only {total} example queries (< {minimum})",
                "add more example queries — they are the worker's demo for humans and agents",
            )
