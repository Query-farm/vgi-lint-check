"""VGI1xx — descriptions for schemas, tables, and views."""

from __future__ import annotations

from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import TAG_DESCRIPTION_LLM, TAG_DESCRIPTION_MD, ObjectKind
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


@register
class LLMDescription(Rule):
    code = "VGI112"
    name = "description-llm"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Tables/views should carry a 'vgi.description_llm' tag for LLM consumers."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_llm_description_chars
        for t in ctx.catalog.iter_table_like():
            d = t.description_llm
            if blank(d):
                yield self.finding(
                    ctx,
                    t.id,
                    f"missing '{TAG_DESCRIPTION_LLM}' tag",
                    "add a 'vgi.description_llm' tag: concise prose aimed at LLMs",
                )
            elif len((d or "").strip()) < minlen:
                yield self.finding(
                    ctx,
                    t.id,
                    f"'{TAG_DESCRIPTION_LLM}' is very short "
                    f"({len((d or '').strip())} < {minlen} chars)",
                    "expand the LLM description so an agent can use the object",
                )


@register
class MarkdownDescription(Rule):
    code = "VGI113"
    name = "description-md"
    category = DESC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Tables/views should carry a 'vgi.description_md' tag for docs."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for t in ctx.catalog.iter_table_like():
            if blank(t.description_md):
                yield self.finding(
                    ctx,
                    t.id,
                    f"missing '{TAG_DESCRIPTION_MD}' tag",
                    "add a 'vgi.description_md' tag with a Markdown description",
                )


@register
class MarkdownNotIdenticalToLLM(Rule):
    code = "VGI114"
    name = "description-md-distinct"
    category = DESC
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "The Markdown description should be richer than the LLM one."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for t in ctx.catalog.iter_table_like():
            llm, md = t.description_llm, t.description_md
            if not blank(llm) and not blank(md) and (llm or "").strip() == (md or "").strip():
                yield self.finding(
                    ctx,
                    t.id,
                    "vgi.description_md is identical to vgi.description_llm",
                    "make the Markdown description richer than the LLM one",
                )
