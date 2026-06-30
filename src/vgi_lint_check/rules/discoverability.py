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
from ..tags import keywords_is_json_array, parse_keywords
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


def _primary_descriptions(ctx: RuleContext) -> Iterator[tuple[ObjectId, str]]:
    """(id, description) across the catalog, schemas, tables/views, and functions.

    Uses each object's primary description (comment, or description for
    functions). Table-functions are excluded — their description legitimately
    mirrors the table they back.
    """
    cat = ctx.catalog
    if not blank(cat.comment):
        yield cat.id, cat.comment or ""
    for s in cat.iter_schemas():
        if not blank(s.comment):
            yield s.id, s.comment or ""
    for t in cat.iter_table_like():
        if not blank(t.comment):
            yield t.id, t.comment or ""
    for f in cat.iter_functions():  # scalar/aggregate/macro (no table-functions)
        text = f.description or f.comment
        if not blank(text):
            yield f.id, text or ""


@register
class DuplicateDescriptions(Rule):
    code = "VGI120"
    name = "duplicate-descriptions"
    category = DISC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
    )
    summary = "Distinct objects (schemas, tables, functions, ...) must not share a description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        groups: dict[str, list[ObjectId]] = defaultdict(list)
        for oid, text in _primary_descriptions(ctx):
            groups[_norm(text)].append(oid)
        for oids in groups.values():
            if len(oids) > 1:
                others = ", ".join(o.qualified() for o in oids)
                for oid in oids:
                    yield self.finding(
                        ctx,
                        oid,
                        f"description is shared by {len(oids)} objects ({others})",
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
class JoinPathDocumented(Rule):
    code = "VGI133"
    name = "join-path-documented"
    category = DISC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "A table with foreign keys should explain how to join to the referenced tables."

    _JOIN_KW = ("join", "foreign key", "references", "related", "relationship", "->")

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for t in ctx.catalog.iter_table_like():
            refs = sorted(
                {
                    c.referenced_table
                    for c in t.constraints
                    if c.constraint_type == "FOREIGN KEY" and c.referenced_table
                }
            )
            if not refs:
                continue
            text = " ".join(
                x for x in (t.comment, t.description_llm, t.description_md) if x
            ).lower()
            if any(kw in text for kw in self._JOIN_KW):
                continue
            missing = [r for r in refs if r.lower() not in text]
            if missing:
                yield self.finding(
                    ctx,
                    t.id,
                    f"join path(s) to {', '.join(missing)} are not documented",
                    "describe how to join to the referenced table(s) so agents can "
                    "compose multi-table queries",
                )


@register
class ClassifyingTagPresent(Rule):
    code = "VGI123"
    name = "classifying-tag-present"
    category = DISC
    default_severity = Severity.WARNING  # strict default
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
    default_severity = Severity.INFO  # nudge; not every column has units
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
    default_severity = Severity.WARNING
    # Required only on the catalog and its schemas — the worker's listing and its
    # sections, where a human display name pays off. Optional (but validated when
    # set, see VGI125) on tables/views/functions.
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA)
    summary = "The catalog and each schema should carry a 'vgi.title' display name."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        listings = [(cat.id, cat.tags, "catalog")]
        listings += [(s.id, s.tags, "schema") for s in cat.iter_schemas()]
        for oid, tags, label in listings:
            if not tags.has(TAG_TITLE):
                yield self.finding(
                    ctx,
                    oid,
                    f"{label} has no 'vgi.title' display name",
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
    default_severity = Severity.WARNING  # strict default
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Objects should carry 'vgi.keywords' (search terms / synonyms)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _taggable(ctx):
            if not tags.has(TAG_KEYWORDS):
                yield self.finding(
                    ctx,
                    oid,
                    "no 'vgi.keywords' search terms",
                    "add a 'vgi.keywords' tag: a JSON array of strings, e.g. "
                    '["seismic", "tremor", "magnitude"]',
                )


@register
class KeywordsJsonArray(Rule):
    code = "VGI138"
    name = "keywords-json-array"
    category = DISC
    default_severity = Severity.ERROR
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "'vgi.keywords' must be a JSON array of strings, not a comma-separated string."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _taggable(ctx):
            value = tags.get(TAG_KEYWORDS)
            if not tags.has(TAG_KEYWORDS) or keywords_is_json_array(value):
                continue
            yield self.finding(
                ctx,
                oid,
                "'vgi.keywords' is not a JSON array of strings",
                'use a JSON array, e.g. ["seismic", "tremor"] — the comma-separated '
                "string is no longer accepted",
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
    # Opt-in: source_url is catalog-level provenance (VGI004); per-object source
    # links are redundant by default (see VGI139). Enable for granular per-object
    # links if your worker maps each object to a distinct source file.
    default_severity = Severity.OFF
    targets = (ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Objects may link to their implementation via 'vgi.source_url' (opt-in)."

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
class SourceUrlCatalogOnly(Rule):
    code = "VGI139"
    name = "source-url-catalog-only"
    category = DISC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "vgi.source_url belongs on the catalog, not repeated on every object."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        objs = [(s.id, s.tags) for s in cat.iter_schemas()]
        objs += [(t.id, t.tags) for t in cat.iter_table_like()]
        objs += [(f.id, f.tags) for f in cat.iter_all_functions()]
        for oid, tags in objs:
            if tags.has(TAG_SOURCE_URL):
                yield self.finding(
                    ctx,
                    oid,
                    "'vgi.source_url' is set on a non-catalog object",
                    "set source_url once on the catalog (the worker's repo) and "
                    "remove it here — per-object copies are redundant. Enable "
                    "VGI128 if you intend distinct per-object source links.",
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


@register
class AgentTestTasksPresent(Rule):
    code = "VGI152"
    name = "agent-test-tasks-present"
    category = DISC
    default_severity = Severity.ERROR
    targets = (ObjectKind.CATALOG,)
    summary = "A worker must declare vgi.agent_test_tasks so `vgi-lint simulate` can grade it."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if cat.agent_test_tasks or cat.agent_test_tasks_parse_error:
            return  # present (validated by VGI407) or malformed (VGI407 reports it)
        yield self.finding(
            ctx,
            cat.id,
            "no 'vgi.agent_test_tasks' suite",
            "add a 'vgi.agent_test_tasks' tag (catalog): a JSON array of "
            "{name, prompt, reference_sql?} analyst tasks. It is required so "
            "`vgi-lint simulate` can measure how well agents actually use this worker",
        )


# Boilerplate left in metadata — a strong "unfinished" signal.
_PLACEHOLDER = re.compile(
    r"\b(TODO|TBD|FIXME|XXX|HACK|lorem ipsum|changeme|placeholder|"
    r"description here|your description|fill (this )?in|coming soon|wip|"
    r"to be (written|done|filled)|n/?a)\b",
    re.IGNORECASE,
)


@register
class NoPlaceholderText(Rule):
    code = "VGI130"
    name = "no-placeholder-text"
    category = DISC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.COLUMN,
    )
    summary = "Descriptions/comments must not contain placeholder text (TODO/TBD/lorem ipsum/…)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, text in _primary_descriptions(ctx):
            m = _PLACEHOLDER.search(text)
            if m:
                yield self._flag(ctx, oid, m.group(0))
        for t in ctx.catalog.iter_table_like():
            for c in t.columns:
                if c.comment:
                    m = _PLACEHOLDER.search(c.comment)
                    if m:
                        yield self._flag(ctx, c.id, m.group(0))

    def _flag(self, ctx: RuleContext, oid: ObjectId, token: str) -> Finding:
        return self.finding(
            ctx,
            oid,
            f"description contains placeholder text ({token!r})",
            "replace the placeholder with a real description before publishing",
        )


@register
class ClassifyingTagsReused(Rule):
    code = "VGI132"
    name = "classifying-tags-reused"
    category = DISC
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "A classifying tag should be a small, reused vocabulary — not unique per object."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        keys = ctx.config.options.classifying_tag_keys
        cap = ctx.config.options.max_distinct_categories
        # value frequency per classifying key across every tagged object
        freqs: dict[str, dict[str, int]] = {k: defaultdict(int) for k in keys}
        for _id, tags, _name in _taggable(ctx):
            for k in keys:
                v = (tags.get(k) or "").strip()
                if v:
                    freqs[k][v.lower()] += 1
        for k in keys:
            counts = freqs[k]
            total = sum(counts.values())
            distinct = len(counts)
            if total < 4:  # too few to judge a vocabulary
                continue
            if cap and distinct > cap:
                yield self.finding(
                    ctx,
                    ctx.catalog.id,
                    f"classifying tag {k!r} has {distinct} distinct values (> {cap})",
                    "consolidate into a smaller, reused set of categories so objects "
                    "cluster — or raise options.max_distinct_categories",
                )
            elif distinct == total:
                yield self.finding(
                    ctx,
                    ctx.catalog.id,
                    f"classifying tag {k!r} value is unique on all {total} objects",
                    "reuse categories so related objects share one — a unique value "
                    "per object provides no faceting",
                )
