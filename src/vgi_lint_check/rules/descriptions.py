"""VGI1xx — descriptions for schemas, tables, and views."""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import (
    TAG_DOC_LLM,
    TAG_DOC_MD,
    ObjectId,
    ObjectKind,
    TagSet,
)
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .registry import register

DESC = Category.DESCRIPTION

# A "word" for VGI106: a run of letters/digits with internal punctuation kept
# (so "RFC 8949", "fixed-width", "COMP-3", "s3://" each read as one word).
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9./_-]*")


@register
class CatalogDescriptionSubstantive(Rule):
    code = "VGI106"
    name = "catalog-description-substantive"
    category = DESC
    default_severity = Severity.ERROR
    targets = (ObjectKind.CATALOG,)
    summary = (
        "The catalog comment is the worker's storefront line — it must be a descriptive "
        "sentence, not a bare name or single word."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        text = (cat.comment or "").strip()
        if not text:
            # Absence is VGI103's business; VGI106 only grades what is there.
            return
        minlen = ctx.config.options.min_catalog_comment_chars
        words = _WORD.findall(text)
        if is_trivial_echo(text, cat.qualifier):
            reason = f"is just the catalog name ({cat.qualifier!r})"
        elif len(words) < 2:
            reason = f"is a single word ({text!r})"
        elif len(text) < minlen:
            reason = f"is too short to be descriptive ({len(text)} < {minlen} chars)"
        else:
            return
        yield self.finding(
            ctx,
            cat.id,
            f"catalog description {reason}",
            "write one descriptive sentence saying what the worker does and for whom, "
            "e.g. 'CBOR (RFC 8949) / MessagePack decode & encode for SQL.' — not 'cbor'",
        )


@register
class ListingDescriptionDetailed(Rule):
    code = "VGI103"
    name = "listing-description-detailed"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA)
    summary = "Catalog/schema doc_llm and doc_md should be detailed (well above object-level)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        opts = ctx.config.options
        yield from self._flag(
            ctx,
            cat.id,
            cat.description_llm,
            cat.description_md,
            opts.min_catalog_description_chars,
            "catalog",
        )
        for s in cat.iter_schemas():
            yield from self._flag(
                ctx,
                s.id,
                s.tags.get(TAG_DOC_LLM),
                s.tags.get(TAG_DOC_MD),
                opts.min_schema_description_chars,
                "schema",
            )

    def _flag(
        self,
        ctx: RuleContext,
        oid: ObjectId,
        llm: str | None,
        md: str | None,
        minlen: int,
        label: str,
    ) -> Iterator[Finding]:
        for tag, value in (("vgi.doc_llm", llm), ("vgi.doc_md", md)):
            text = (value or "").strip()
            # Only flag length when set; presence is enforced separately.
            if text and len(text) < minlen:
                yield self.finding(
                    ctx,
                    oid,
                    f"{label} {tag} is short ({len(text)} < {minlen} chars)",
                    f"expand the {label} {tag} into a detailed description — the "
                    f"{label} is part of the worker's listing, so cover what it "
                    "contains, key concepts, and when to use it",
                )


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


# Object kinds that may carry vgi.doc_llm/md, beyond schemas (which are
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
    summary = "Every schema must carry a 'vgi.doc_llm' tag (required)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if not s.tags.has(TAG_DOC_LLM):
                yield self.finding(
                    ctx,
                    s.id,
                    f"schema missing '{TAG_DOC_LLM}' tag",
                    "add a 'vgi.doc_llm' tag: LLM-oriented prose covering what this "
                    "schema/domain contains, its key concepts, and when an agent should "
                    "reach for it — the schema is part of the worker's listing, so don't "
                    "just restate the schema name or enumerate its objects",
                )


@register
class SchemaMarkdownDescription(Rule):
    code = "VGI118"
    name = "schema-description-md"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCHEMA,)
    summary = "Every schema must carry a 'vgi.doc_md' tag (required)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if not s.tags.has(TAG_DOC_MD):
                yield self.finding(
                    ctx,
                    s.id,
                    f"schema missing '{TAG_DOC_MD}' tag",
                    "add a 'vgi.doc_md' tag: a richer Markdown description of the "
                    "schema/domain — what it contains, key concepts, when to use it, and "
                    "worked examples — fuller than the one-line comment, not a copy of it",
                )


@register
class LLMDescription(Rule):
    code = "VGI112"
    name = "description-llm"
    category = DESC
    default_severity = Severity.WARNING  # strict default (was opt-in for tables/views/functions)
    targets = _OPTIONAL_DESC_KINDS
    summary = "Tables/views/functions should carry a 'vgi.doc_llm' tag for agents."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _optional_desc_objects(ctx):
            if not tags.has(TAG_DOC_LLM):
                yield self.finding(
                    ctx,
                    oid,
                    f"missing '{TAG_DOC_LLM}' tag",
                    "add a 'vgi.doc_llm' tag: an LLM-oriented description of "
                    "what this object is and when to use it. It complements the "
                    "object's short description/comment — don't just duplicate it",
                )


@register
class MarkdownDescription(Rule):
    code = "VGI113"
    name = "description-md"
    category = DESC
    default_severity = Severity.WARNING  # strict default (was opt-in for tables/views/functions)
    targets = _OPTIONAL_DESC_KINDS
    summary = "Tables/views/functions should carry a 'vgi.doc_md' tag for human docs."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags, _name in _optional_desc_objects(ctx):
            if not tags.has(TAG_DOC_MD):
                yield self.finding(
                    ctx,
                    oid,
                    f"missing '{TAG_DOC_MD}' tag",
                    "add a 'vgi.doc_md' tag: a richer narrative description "
                    "in Markdown (what it is, columns/returns, caveats, examples) — "
                    "not a copy of the object's one-line description/comment",
                )


@register
class LLMDescriptionTooShort(Rule):
    code = "VGI119"
    name = "description-llm-too-short"
    category = DESC
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA, *_OPTIONAL_DESC_KINDS)
    summary = "A 'vgi.doc_llm', when set, should be substantive."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_llm_description_chars
        for oid, tags, _name in _described_objects(ctx):
            d = tags.get(TAG_DOC_LLM)
            if not blank(d) and len((d or "").strip()) < minlen:
                yield self.finding(
                    ctx,
                    oid,
                    f"'{TAG_DOC_LLM}' is very short ({len((d or '').strip())} < {minlen} chars)",
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
            llm = tags.get(TAG_DOC_LLM)
            md = tags.get(TAG_DOC_MD)
            if not blank(llm) and not blank(md) and (llm or "").strip() == (md or "").strip():
                yield self.finding(
                    ctx,
                    oid,
                    "vgi.doc_md is identical to vgi.doc_llm",
                    "make the Markdown description richer than the LLM one",
                )


def _norm_desc(text: str | None) -> str:
    return " ".join((text or "").split()).strip().lower()


@register
class DescriptionTagNotDuplicate(Rule):
    code = "VGI102"
    name = "description-tag-not-duplicate"
    category = DESC
    default_severity = Severity.INFO
    targets = _OPTIONAL_DESC_KINDS
    summary = (
        "vgi.doc_llm/_md should add narrative detail, not just repeat the "
        "object's own description/comment."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        items: list[tuple[ObjectId, TagSet, str | None]] = []
        for t in cat.iter_table_like():
            items.append((t.id, t.tags, t.comment))
        for f in cat.iter_all_functions():
            items.append((f.id, f.tags, f.description or f.comment))
        for oid, tags, primary in items:
            base = _norm_desc(primary)
            if not base:
                continue
            for key in (TAG_DOC_LLM, TAG_DOC_MD):
                value = tags.get(key)
                if not blank(value) and _norm_desc(value) == base:
                    yield self.finding(
                        ctx,
                        oid,
                        f"{key} just repeats the object's description",
                        f"{key} should add narrative detail an agent/reader can't get "
                        "from the one-line description — purpose, columns/returns, "
                        "caveats, examples — not duplicate it",
                    )
