"""VGI0xx — catalog-level metadata (the worker's "listing").

The catalog is the worker's product listing: its description and provenance are
what a data catalog, MCP directory, or agent shows when surfacing the worker.
These are required (default warning), not opt-in.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import (
    TAG_DESCRIPTION_LLM,
    TAG_DESCRIPTION_MD,
    ObjectKind,
)
from ._util import blank
from .base import Rule, RuleContext
from .registry import register

CAT = Category.CATALOG


@register
class CatalogComment(Rule):
    code = "VGI001"
    name = "catalog-comment"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "The catalog must have a comment — the worker's one-line description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if blank(cat.comment):
            yield self.finding(
                ctx,
                cat.id,
                "catalog has no description",
                "set a catalog comment describing what this worker provides",
            )


@register
class CatalogLLMDescription(Rule):
    code = "VGI002"
    name = "catalog-description-llm"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "The catalog must carry a 'vgi.description_llm' tag for agents."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if blank(cat.description_llm):
            yield self.finding(
                ctx,
                cat.id,
                f"catalog missing '{TAG_DESCRIPTION_LLM}' tag",
                "add a catalog 'vgi.description_llm' tag describing what it covers "
                "and the questions it answers, for LLM/agent tool selection",
            )


@register
class CatalogMarkdownDescription(Rule):
    code = "VGI003"
    name = "catalog-description-md"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "The catalog must carry a 'vgi.description_md' tag for human docs."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if blank(cat.description_md):
            yield self.finding(
                ctx,
                cat.id,
                f"catalog missing '{TAG_DESCRIPTION_MD}' tag",
                "add a catalog 'vgi.description_md' tag: a Markdown overview shown "
                "on listing/describe pages",
            )


@register
class CatalogSourceUrl(Rule):
    code = "VGI004"
    name = "catalog-source-url"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "The catalog should advertise a source_url (provenance / about link)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if blank(cat.source_url):
            yield self.finding(
                ctx,
                cat.id,
                "catalog has no source_url",
                "advertise a source_url (repo/docs/dataset homepage) so consumers "
                "can verify provenance and learn more",
            )
