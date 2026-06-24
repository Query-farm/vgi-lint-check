"""VGI17x — description content validation (Markdown + link resolution)."""

from __future__ import annotations

import re
from collections.abc import Iterator

from markdown_it import MarkdownIt

from ..findings import Category, Finding, Severity
from ..linkcheck import is_broken
from ..model import (
    TAG_DOC_LLM,
    TAG_DOC_MD,
    TAG_RESULT_COLUMNS_MD,
    TAG_SOURCE_URL,
    TAG_SUPPORT_CONTACT,
    TAG_SUPPORT_POLICY_URL,
    DocLink,
    ObjectId,
    ObjectKind,
    TagSet,
)
from ..tags import decode_doc_links
from ._util import blank
from .base import Rule, RuleContext
from .registry import register

CONTENT = Category.CONTENT
_DOC_TARGET_KINDS = (
    ObjectKind.CATALOG,
    ObjectKind.SCHEMA,
    ObjectKind.TABLE,
    ObjectKind.VIEW,
    ObjectKind.SCALAR_FUNCTION,
    ObjectKind.AGGREGATE,
    ObjectKind.MACRO,
    ObjectKind.TABLE_FUNCTION,
)


def _iter_tags(ctx: RuleContext) -> Iterator[tuple[ObjectId, TagSet]]:
    """(id, tags) for every object that can carry documentation tags."""
    cat = ctx.catalog
    yield cat.id, cat.tags
    for s in cat.iter_schemas():
        yield s.id, s.tags
    for t in cat.iter_table_like():
        yield t.id, t.tags
    for f in cat.iter_all_functions():
        yield f.id, f.tags


def _iter_doc_links(ctx: RuleContext) -> Iterator[tuple[ObjectId, list[DocLink], str | None]]:
    """(id, doc_links, parse_error) for every object carrying vgi.doc_links."""
    for oid, tags in _iter_tags(ctx):
        links, err = decode_doc_links(tags)
        if links or err:
            yield oid, links, err


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
    """(id, markdown) for every object that carries a vgi.doc_md."""
    cat = ctx.catalog
    if not blank(cat.description_md):
        yield cat.id, cat.description_md or ""
    for s in ctx.catalog.iter_schemas():
        md = s.tags.get(TAG_DOC_MD)
        if not blank(md):
            yield s.id, md or ""
    for t in ctx.catalog.iter_table_like():
        md = t.tags.get(TAG_DOC_MD)
        if not blank(md):
            yield t.id, md or ""
    for f in ctx.catalog.iter_all_functions():
        for md in (f.tags.get(TAG_DOC_MD), f.tags.get(TAG_RESULT_COLUMNS_MD)):
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
    summary = "vgi.doc_md should be valid Markdown (no empty/broken links)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, md in _iter_md(ctx):
            for kind, url in _md_targets(md):
                if blank(url):
                    yield self.finding(
                        ctx,
                        oid,
                        f"vgi.doc_md has a Markdown {kind} with an empty target",
                        f"give the {kind} a target or remove the empty {kind} syntax",
                    )
            if len(_FENCE.findall(md)) % 2 != 0:
                yield self.finding(
                    ctx,
                    oid,
                    "vgi.doc_md has an unterminated code fence",
                    "close the ``` / ~~~ fenced code block",
                )


@register
class DocLinksWellFormed(Rule):
    code = "VGI172"
    name = "doc-links-well-formed"
    category = CONTENT
    default_severity = Severity.ERROR
    targets = _DOC_TARGET_KINDS
    summary = "vgi.doc_links must be a JSON array of http(s) URLs (or {title?, url} objects)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, links, err in _iter_doc_links(ctx):
            if err:
                yield self.finding(
                    ctx,
                    oid,
                    f"vgi.doc_links is not valid: {err}",
                    'use a JSON array of URL strings or {"title": "...", "url": "..."} '
                    "objects pointing at additional documentation",
                )
                continue
            for i, link in enumerate(links):
                url = (link.url or "").strip()
                if not url:
                    yield self.finding(
                        ctx, oid, f"vgi.doc_links entry #{i} has no url", "give every entry a url"
                    )
                elif not url.startswith(("http://", "https://")):
                    yield self.finding(
                        ctx,
                        oid,
                        f"vgi.doc_links entry #{i} is not an http(s) URL: {url!r}",
                        "use an absolute http(s) URL so consumers can open it",
                    )


def _iter_urls(ctx: RuleContext) -> Iterator[tuple[ObjectId, str, str]]:
    """(id, url, label) for every http(s) URL worth resolving in the catalog."""
    cat = ctx.catalog
    if not blank(cat.source_url):
        yield cat.id, cat.source_url or "", "source_url"
    # Catalog support links (the contact may be a mailto/email; only resolve URLs).
    for tag, label in (
        (TAG_SUPPORT_POLICY_URL, "vgi.support_policy_url"),
        (TAG_SUPPORT_CONTACT, "vgi.support_contact"),
    ):
        value = cat.tags.get(tag) or ""
        if value.startswith(("http://", "https://")):
            yield cat.id, value, label
    for oid, comment, llm, md, src_tag in _described(ctx):
        if not blank(src_tag):
            yield oid, src_tag or "", "vgi.source_url"
        for label, text in (("comment", comment), ("vgi.doc_llm", llm)):
            for url in _BARE_URL.findall(text or ""):
                yield oid, url, label
        for kind, url in _md_targets(md or ""):
            if url.startswith(("http://", "https://")):
                yield oid, url, f"vgi.doc_md {kind}"
    for f in cat.iter_all_functions():
        for kind, url in _md_targets(f.tags.get(TAG_RESULT_COLUMNS_MD) or ""):
            if url.startswith(("http://", "https://")):
                yield f.id, url, f"vgi.result_columns_md {kind}"
    # vgi.doc_links URLs are resolved too (only well-formed http(s) ones).
    for oid, links, err in _iter_doc_links(ctx):
        if err:
            continue
        for link in links:
            url = (link.url or "").strip()
            if url.startswith(("http://", "https://")):
                yield oid, url, "vgi.doc_links"


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
            s.tags.get(TAG_DOC_LLM),
            s.tags.get(TAG_DOC_MD),
            s.tags.get(TAG_SOURCE_URL),
        )
    for t in cat.iter_table_like():
        yield (
            t.id,
            t.comment,
            t.tags.get(TAG_DOC_LLM),
            t.tags.get(TAG_DOC_MD),
            t.tags.get(TAG_SOURCE_URL),
        )
    for f in cat.iter_all_functions():
        yield (
            f.id,
            f.comment,
            f.tags.get(TAG_DOC_LLM),
            f.tags.get(TAG_DOC_MD),
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
