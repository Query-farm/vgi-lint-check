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


# --- VGI173 / VGI174 — description content quality ------------------------
#
# Match identifiers only where they appear "as code" — backticked, called
# (`name(`), or as the tail of a dotted path (`cat.main.easter`). A function
# whose name is also an English word ("holiday") in plain prose is *not* matched,
# so VGI173 flags genuine manifests, not legitimate prose that names one object.
_BACKTICK = re.compile(r"`([^`]+)`")
_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_QUALIFIED = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _code_tokens(text: str) -> set[str]:
    """Identifiers in ``text`` that appear as code (backticked/called/qualified)."""
    toks: set[str] = set()
    for span in _BACKTICK.findall(text):
        toks.update(_IDENT.findall(span))
    toks.update(_CALL.findall(text))
    for path in _QUALIFIED.findall(text):
        toks.update(path.split("."))
    return toks


def _scope_object_names(ctx: RuleContext) -> Iterator[tuple[ObjectId, str, set[str]]]:
    """(id, label, object-name set) for the catalog and each schema.

    The name set is the worker's own surface — tables, views, and functions —
    that a description could redundantly enumerate. Schema names are excluded so
    a single qualified example (``cat.main.f``) doesn't inflate the count.
    """
    cat = ctx.catalog
    cat_names = {t.name for t in cat.iter_table_like()} | {f.name for f in cat.iter_all_functions()}
    yield cat.id, "catalog", cat_names
    for s in cat.iter_schemas():
        names = (
            {t.name for t in s.tables} | {v.name for v in s.views} | {f.name for f in s.functions}
        )
        yield s.id, "schema", names


@register
class DescriptionEnumeratesObjects(Rule):
    code = "VGI173"
    name = "description-enumerates-objects"
    category = CONTENT
    default_severity = Severity.ERROR
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA)
    summary = (
        "Catalog/schema descriptions must not just enumerate the worker's own "
        "objects — that inventory is discoverable by listing the schema."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        opts = ctx.config.options
        floor = opts.enumeration_min_objects
        fraction = opts.enumeration_object_fraction
        cat = ctx.catalog
        descriptions = {
            cat.id: (cat.description_llm, cat.description_md),
        }
        for s in cat.iter_schemas():
            descriptions[s.id] = (s.tags.get(TAG_DOC_LLM), s.tags.get(TAG_DOC_MD))
        for oid, label, names in _scope_object_names(ctx):
            total = len(names)
            if total == 0:
                continue
            llm, md = descriptions.get(oid, (None, None))
            for tag, value in (("vgi.doc_llm", llm), ("vgi.doc_md", md)):
                if blank(value):
                    continue
                named = names & _code_tokens(value or "")
                if len(named) >= floor and len(named) / total >= fraction:
                    yield self.finding(
                        ctx,
                        oid,
                        f"{label} {tag} enumerates {len(named)} of {total} worker "
                        f"objects ({', '.join(sorted(named)[:6])}"
                        f"{', …' if len(named) > 6 else ''})",
                        f"don't list the worker's objects in the {label} description — "
                        "an agent discovers them by listing the schema. Describe what "
                        "the worker is for, its key concepts, and when to reach for it",
                    )


# SQL embedded in a description belongs in a ```sql fence (or, better, an
# executable example). A SELECT paired with FROM (either order) is a strong
# signal; a bare "select" in English prose is not.
_SQL_LEAD = re.compile(
    r"^\s*(SELECT|WITH|FROM|PRAGMA|EXPLAIN|CREATE|INSERT|UPDATE|DELETE|COPY|VALUES|TABLE)\b",
    re.IGNORECASE,
)
_SQL_IN_PROSE = re.compile(
    r"\bSELECT\b[\s\S]{0,200}?\bFROM\b"
    r"|\bFROM\b[\s\S]{0,80}?\bSELECT\b"
    r"|^\s*(PRAGMA|EXPLAIN)\b"
    r"|\bWITH\b[\s\S]{0,120}?\bAS\s*\(",
    re.IGNORECASE | re.MULTILINE,
)
_FENCED_BLOCK = re.compile(r"(?ms)^[ \t]*(```+|~~~+)[^\n]*\n.*?^[ \t]*\1[ \t]*$")
_INLINE_CODE = re.compile(r"`+([^`]+)`+")
# An inline `code` span that is a *statement* (verb + at least one more token),
# not a bare keyword reference like `SELECT` or a type name like `DATE`.
_SQL_INLINE = re.compile(
    r"^\s*(SELECT|WITH|FROM|PRAGMA|EXPLAIN|CREATE|INSERT|UPDATE|DELETE|COPY|VALUES)\b\s+\S",
    re.IGNORECASE,
)


def _looks_like_sql(body: str | None) -> bool:
    """True when a code block's body opens with a SQL statement verb."""
    return bool(_SQL_LEAD.search(body or ""))


def _iter_prose(ctx: RuleContext) -> Iterator[tuple[ObjectId, str, str]]:
    """(id, label, text) for every doc_llm / doc_md across described objects."""
    for oid, _comment, llm, md, _src in _described(ctx):
        for label, text in (("vgi.doc_llm", llm), ("vgi.doc_md", md)):
            if not blank(text):
                yield oid, label, text or ""


@register
class DescriptionSqlFenced(Rule):
    code = "VGI174"
    name = "description-sql-fenced"
    category = CONTENT
    default_severity = Severity.ERROR
    targets = _DOC_TARGET_KINDS
    summary = "SQL in a description must live in a ```sql code fence (or an executable example)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, label, text in _iter_prose(ctx):
            # Mislabeled/unlabeled fences whose body is SQL.
            for tok in _MD.parse(text):
                if tok.type == "fence":
                    lang = (tok.info or "").strip().split(" ")[0].lower() if tok.info else ""
                    if lang != "sql" and _looks_like_sql(tok.content):
                        yield self.finding(
                            ctx,
                            oid,
                            f"{label} has a code fence with SQL but no ```sql language tag",
                            "tag the fence ```sql — or, better, move the query into an "
                            "executable example (VGI5xx) so it is actually run",
                        )
                elif tok.type == "code_block" and _looks_like_sql(tok.content):
                    yield self.finding(
                        ctx,
                        oid,
                        f"{label} has an indented code block containing SQL",
                        "use a ```sql fence — or, better, an executable example (VGI5xx)",
                    )
            # Raw SQL sitting in prose (outside any code), once code is stripped out.
            stripped = _FENCED_BLOCK.sub(" ", text)
            prose = _INLINE_CODE.sub(" ", stripped)
            if _SQL_IN_PROSE.search(prose):
                yield self.finding(
                    ctx,
                    oid,
                    f"{label} contains a raw SQL statement in prose (not in a ```sql fence)",
                    "wrap the query in a ```sql code fence — or, better, add it as an "
                    "executable example (VGI5xx) so it is tested, not just shown",
                )
            # SQL statements tucked into inline `code` spans (still not runnable).
            inline = [span for span in _INLINE_CODE.findall(stripped) if _SQL_INLINE.search(span)]
            if inline:
                sample = " ".join(inline[0].split())
                sample = sample[:47] + "…" if len(sample) > 48 else sample
                count = (
                    f"{len(inline)} inline code spans" if len(inline) > 1 else "an inline code span"
                )
                yield self.finding(
                    ctx,
                    oid,
                    f"{label} has a SQL statement in {count} (e.g. `{sample}`), not a ```sql fence",
                    "move runnable SQL into a ```sql code fence — or, better, an "
                    "executable example (VGI5xx) so it is tested, not just shown",
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


# --- VGI175 / VGI176 — catalog/schema listing docs (structure + paragraphs) ---
#
# The catalog and schema vgi.doc_md are the worker's listing page. These reward
# using Markdown (not a plain-prose blob) and splitting it into paragraphs.
_MD_HEADER = re.compile(r"^\s{0,3}#{1,6}\s", re.MULTILINE)
_MD_LIST = re.compile(r"^\s*([-*+]|\d+\.)\s", re.MULTILINE)
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_MD_BLOCKQUOTE = re.compile(r"^\s*>\s", re.MULTILINE)
_MD_LINK = re.compile(r"(?<!\!)\[[^\]]+\]\([^)]+\)")
_MD_FENCE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)


def _has_markdown_structure(md: str) -> bool:
    """True when ``md`` uses any Markdown structure (vs. a plain-prose blob)."""
    return any(
        pat.search(md)
        for pat in (_MD_HEADER, _MD_LIST, _MD_TABLE_ROW, _MD_BLOCKQUOTE, _MD_LINK, _MD_FENCE)
    )


def _paragraph_count(md: str) -> int:
    """Count blank-line-separated content blocks, ignoring lone header lines."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (md or "").strip()) if b.strip()]
    return sum(1 for b in blocks if not (b.startswith("#") and "\n" not in b))


def _iter_listing_md(ctx: RuleContext) -> Iterator[tuple[ObjectId, str, str]]:
    """(id, label, doc_md) for the catalog and each schema that carries a doc_md."""
    cat = ctx.catalog
    if not blank(cat.description_md):
        yield cat.id, "catalog", cat.description_md or ""
    for s in cat.iter_schemas():
        md = s.tags.get(TAG_DOC_MD)
        if not blank(md):
            yield s.id, "schema", md or ""


@register
class ListingDocUsesMarkdown(Rule):
    code = "VGI175"
    name = "listing-doc-uses-markdown"
    category = CONTENT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA)
    summary = "Catalog/schema vgi.doc_md should use Markdown structure, not be a plain-prose blob."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, label, md in _iter_listing_md(ctx):
            if not _has_markdown_structure(md):
                yield self.finding(
                    ctx,
                    oid,
                    f"{label} vgi.doc_md is plain prose (no Markdown structure)",
                    f"format the {label} listing with Markdown — headers, lists, a table, "
                    "or links — so it renders as a rich listing page, not a wall of text",
                )


@register
class ListingDocMultiParagraph(Rule):
    code = "VGI176"
    name = "listing-doc-multi-paragraph"
    category = CONTENT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA)
    summary = "Catalog/schema vgi.doc_md should be multiple paragraphs, not a single block."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, label, md in _iter_listing_md(ctx):
            if _paragraph_count(md) < 2:
                yield self.finding(
                    ctx,
                    oid,
                    f"{label} vgi.doc_md is a single paragraph",
                    f"break the {label} description into multiple paragraphs (blank-line "
                    "separated) — e.g. what it is, key concepts, and when to use it",
                )
