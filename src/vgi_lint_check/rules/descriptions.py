"""VGI1xx — descriptions for schemas, tables, and views."""

from __future__ import annotations

from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import (
    TAG_DESCRIPTION_LLM,
    TAG_DESCRIPTION_MD,
    ObjectId,
    ObjectKind,
    TagSet,
)
from ._util import blank
from .base import Rule, RuleContext
from .registry import register

DESC = Category.DESCRIPTION


@register
class SchemaComment(Rule):
    code = "VGI101"
    name = "schema-comment"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCHEMA,)
    summary = "Every schema should have a comment describing the domain it covers."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if blank(s.comment):
                yield self.finding(
                    ctx,
                    s.id,
                    "schema has no comment",
                    "add a comment describing what this schema/domain contains",
                )


@register
class TableComment(Rule):
    code = "VGI111"
    name = "table-comment"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE,)
    summary = "Every table should have a one-line comment describing its rows."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for t in ctx.catalog.iter_tables():
            if blank(t.comment):
                yield self.finding(
                    ctx,
                    t.id,
                    "table has no comment",
                    "add a one-line comment describing what a row represents",
                )


@register
class ViewComment(Rule):
    code = "VGI115"
    name = "view-comment"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.VIEW,)
    summary = "Every view should have a comment describing what it returns."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for v in ctx.catalog.iter_views():
            if blank(v.comment):
                yield self.finding(
                    ctx,
                    v.id,
                    "view has no comment",
                    "add a comment describing what this view returns",
                )


# Object kinds that may carry vgi.description_llm/md, beyond schemas (which are
# required, see VGI116/VGI118): tables, views, and every function kind.
_OPTIONAL_DESC_KINDS = (
    ObjectKind.TABLE,
    ObjectKind.VIEW,
    ObjectKind.SCALAR_FUNCTION,
    ObjectKind.AGGREGATE,
    ObjectKind.MACRO,
    ObjectKind.TABLE_FUNCTION,
)


def _optional_desc_objects(ctx: RuleContext) -> Iterator[tuple[ObjectId, TagSet, str]]:
    """(id, tags, name) for tables/views and all functions (incl table-functions)."""
    for t in ctx.catalog.iter_table_like():
        yield t.id, t.tags, t.name
    for f in ctx.catalog.iter_all_functions():
        yield f.id, f.tags, f.name


def _described_objects(ctx: RuleContext) -> Iterator[tuple[ObjectId, TagSet, str]]:
    """Every object that may carry a description (for validity checks)."""
    cat = ctx.catalog
    yield cat.id, cat.tags, cat.qualifier
    for s in cat.iter_schemas():
        yield s.id, s.tags, s.name
    yield from _optional_desc_objects(ctx)


@register
class SchemaLLMDescription(Rule):
    code = "VGI116"
    name = "schema-description-llm"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCHEMA,)
    summary = "Every schema must carry a 'vgi.description_llm' tag (required)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if not s.tags.has(TAG_DESCRIPTION_LLM):
                yield self.finding(
                    ctx,
                    s.id,
                    f"schema missing '{TAG_DESCRIPTION_LLM}' tag",
                    "add a 'vgi.description_llm' tag: concise prose aimed at LLMs",
                )


@register
class SchemaMarkdownDescription(Rule):
    code = "VGI118"
    name = "schema-description-md"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCHEMA,)
    summary = "Every schema must carry a 'vgi.description_md' tag (required)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if not s.tags.has(TAG_DESCRIPTION_MD):
                yield self.finding(
                    ctx,
                    s.id,
                    f"schema missing '{TAG_DESCRIPTION_MD}' tag",
                    "add a 'vgi.description_md' tag with a Markdown description",
                )


@register
class LLMDescription(Rule):
    code = "VGI112"
    name = "description-llm"
    category = DESC
    default_severity = Severity.WARNING  # strict default (was opt-in for tables/views/functions)
    targets = _OPTIONAL_DESC_KINDS
    summary = "Tables/views/functions may carry a 'vgi.description_llm' tag (opt-in)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _optional_desc_objects(ctx):
            if not tags.has(TAG_DESCRIPTION_LLM):
                yield self.finding(
                    ctx,
                    oid,
                    f"missing '{TAG_DESCRIPTION_LLM}' tag",
                    "add a 'vgi.description_llm' tag: concise prose aimed at LLMs",
                )


@register
class MarkdownDescription(Rule):
    code = "VGI113"
    name = "description-md"
    category = DESC
    default_severity = Severity.WARNING  # strict default (was opt-in for tables/views/functions)
    targets = _OPTIONAL_DESC_KINDS
    summary = "Tables/views/functions may carry a 'vgi.description_md' tag (opt-in)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _optional_desc_objects(ctx):
            if not tags.has(TAG_DESCRIPTION_MD):
                yield self.finding(
                    ctx,
                    oid,
                    f"missing '{TAG_DESCRIPTION_MD}' tag",
                    "add a 'vgi.description_md' tag with a Markdown description",
                )


@register
class LLMDescriptionTooShort(Rule):
    code = "VGI119"
    name = "description-llm-too-short"
    category = DESC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, *_OPTIONAL_DESC_KINDS)
    summary = "A 'vgi.description_llm', when set, should be substantive."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_llm_description_chars
        for oid, tags, _name in _described_objects(ctx):
            d = tags.get(TAG_DESCRIPTION_LLM)
            if not blank(d) and len((d or "").strip()) < minlen:
                yield self.finding(
                    ctx,
                    oid,
                    f"'{TAG_DESCRIPTION_LLM}' is very short "
                    f"({len((d or '').strip())} < {minlen} chars)",
                    "expand the LLM description so an agent can use the object",
                )


@register
class MarkdownNotIdenticalToLLM(Rule):
    code = "VGI114"
    name = "description-md-distinct"
    category = DESC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, *_OPTIONAL_DESC_KINDS)
    summary = "The Markdown description should be richer than the LLM one."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _described_objects(ctx):
            llm = tags.get(TAG_DESCRIPTION_LLM)
            md = tags.get(TAG_DESCRIPTION_MD)
            if not blank(llm) and not blank(md) and (llm or "").strip() == (md or "").strip():
                yield self.finding(
                    ctx,
                    oid,
                    "vgi.description_md is identical to vgi.description_llm",
                    "make the Markdown description richer than the LLM one",
                )
