"""VGI17x — description content validation (Markdown + link resolution)."""

from __future__ import annotations

import re
from collections.abc import Iterator

from markdown_it import MarkdownIt

from ..findings import Category, Finding, Severity
from ..linkcheck import is_broken
from ..model import (
    TAG_DESCRIPTION_LLM,
    TAG_DESCRIPTION_MD,
    TAG_SOURCE_URL,
    ObjectId,
    ObjectKind,
)
from ._util import blank
from .base import Rule, RuleContext
from .registry import register

CONTENT = Category.CONTENT
_MD = MarkdownIt("commonmark")
_BARE_URL = re.compile(r"https?://[^\s)>\]\"']+")
_FENCE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)


def _md_targets(text: str) -> list[tuple[str, str]]:
    """Return (kind, url) for every link/image in a Markdown string."""
    out: list[tuple[str, str]] = []
    for tok in _MD.parse(text or ""):
        for child in tok.children or []:
            if child.type == "link_open":
                out.append(("link", str(child.attrGet("href") or "")))
            elif child.type == "image":
                out.append(("image", str(child.attrGet("src") or "")))
    return out


def _iter_md(ctx: RuleContext) -> Iterator[tuple[ObjectId, str]]:
    """(id, markdown) for every object that carries a vgi.description_md."""
    cat = ctx.catalog
    if not blank(cat.description_md):
        yield cat.id, cat.description_md or ""
    for s in ctx.catalog.iter_schemas():
        md = s.tags.get(TAG_DESCRIPTION_MD)
        if not blank(md):
            yield s.id, md or ""
    for t in ctx.catalog.iter_table_like():
        md = t.tags.get(TAG_DESCRIPTION_MD)
        if not blank(md):
            yield t.id, md or ""
    for f in ctx.catalog.iter_all_functions():
        md = f.tags.get(TAG_DESCRIPTION_MD)
        if not blank(md):
            yield f.id, md or ""


@register
class MarkdownWellFormed(Rule):
    code = "VGI170"
    name = "markdown-well-formed"
    category = CONTENT
    default_severity = Severity.INFO
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "vgi.description_md should be valid Markdown (no empty/broken links)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, md in _iter_md(ctx):
            for kind, url in _md_targets(md):
                if blank(url):
                    yield self.finding(
                        ctx,
                        oid,
                        f"vgi.description_md has a Markdown {kind} with an empty target",
                        f"give the {kind} a target or remove the empty {kind} syntax",
                    )
            if len(_FENCE.findall(md)) % 2 != 0:
                yield self.finding(
                    ctx,
                    oid,
                    "vgi.description_md has an unterminated code fence",
                    "close the ``` / ~~~ fenced code block",
                )


def _iter_urls(ctx: RuleContext) -> Iterator[tuple[ObjectId, str, str]]:
    """(id, url, label) for every http(s) URL worth resolving in the catalog."""
    cat = ctx.catalog
    if not blank(cat.source_url):
        yield cat.id, cat.source_url or "", "source_url"
    for oid, comment, llm, md, src_tag in _described(ctx):
        if not blank(src_tag):
            yield oid, src_tag or "", "vgi.source_url"
        for label, text in (("comment", comment), ("vgi.description_llm", llm)):
            for url in _BARE_URL.findall(text or ""):
                yield oid, url, label
        for kind, url in _md_targets(md or ""):
            if url.startswith(("http://", "https://")):
                yield oid, url, f"vgi.description_md {kind}"


def _described(
    ctx: RuleContext,
) -> Iterator[tuple[ObjectId, str | None, str | None, str | None, str | None]]:
    """(id, comment, llm, md, source_url_tag) for every described object."""
    cat = ctx.catalog
    yield cat.id, cat.comment, cat.description_llm, cat.description_md, cat.tags.get(TAG_SOURCE_URL)
    for s in cat.iter_schemas():
        yield (
            s.id,
            s.comment,
            s.tags.get(TAG_DESCRIPTION_LLM),
            s.tags.get(TAG_DESCRIPTION_MD),
            s.tags.get(TAG_SOURCE_URL),
        )
    for t in cat.iter_table_like():
        yield (
            t.id,
            t.comment,
            t.tags.get(TAG_DESCRIPTION_LLM),
            t.tags.get(TAG_DESCRIPTION_MD),
            t.tags.get(TAG_SOURCE_URL),
        )
    for f in cat.iter_all_functions():
        yield (
            f.id,
            f.comment,
            f.tags.get(TAG_DESCRIPTION_LLM),
            f.tags.get(TAG_DESCRIPTION_MD),
            f.tags.get(TAG_SOURCE_URL),
        )


@register
class DescriptionLinksResolve(Rule):
    code = "VGI171"
    name = "description-links-resolve"
    category = CONTENT
    default_severity = Severity.WARNING
    requires_network = True
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "Links/images and source URLs in descriptions must resolve (no 404)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        resolve = ctx.link_resolver
        if resolve is None:  # no resolver wired (offline / --no-check-links)
            return
        seen: set[tuple[str, str]] = set()
        for oid, url, label in _iter_urls(ctx):
            key = (oid.qualified(), url)
            if key in seen:
                continue
            seen.add(key)
            status = resolve(url)
            if is_broken(status):
                yield self.finding(
                    ctx,
                    oid,
                    f"{label} link is broken (HTTP {status}): {url}",
                    "fix or remove the dead link/image",
                )
