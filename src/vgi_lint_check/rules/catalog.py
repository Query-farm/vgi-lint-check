"""VGI0xx — catalog-level metadata (the worker's "listing").

The catalog is the worker's product listing: its description and provenance are
what a data catalog, MCP directory, or agent shows when surfacing the worker.
These are required (default warning), not opt-in.
"""

from __future__ import annotations

from collections.abc import Iterator

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from ..findings import Category, Finding, Severity
from ..linkcheck import DISPLAYABLE_IMAGE_FORMATS, is_broken
from ..model import (
    TAG_DOC_LLM,
    TAG_DOC_MD,
    TAG_ICON_URL,
    TAG_LICENSE,
    TAG_SUPPORT_CONTACT,
    TAG_SUPPORT_POLICY_URL,
    ObjectKind,
)
from ._util import blank
from .base import Rule, RuleContext
from .registry import register

CAT = Category.CATALOG


def _parse_spec(spec: str) -> SpecifierSet | None:
    try:
        return SpecifierSet(spec)
    except InvalidSpecifier:
        return None


def _parse_version(version: str) -> Version | None:
    try:
        return Version(version)
    except InvalidVersion:
        return None


def _is_http_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def _looks_like_url_attempt(value: str) -> bool:
    """True when a value seems intended as a URL (so a bad one should be flagged)."""
    v = value.strip().lower()
    return "://" in v or v.startswith("www.")


@register
class CatalogNotEmpty(Rule):
    code = "VGI011"
    name = "catalog-not-empty"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "A catalog must expose at least one table, view, or function."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if not cat.has_objects():
            yield self.finding(
                ctx,
                cat.id,
                "catalog exposes no tables, views, or functions",
                "this worker advertises a catalog but contributes no objects — "
                "expose its data/functions, or check the attach succeeded",
            )


@register
class WorkerCatalogCount(Rule):
    code = "VGI012"
    name = "worker-catalog-count"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "A worker should advertise at least one catalog and not an unbounded number."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        n = len(cat.advertised_catalogs)
        if n == 0:
            yield self.finding(
                ctx,
                cat.id,
                "worker advertises no catalogs via vgi_catalogs()",
                "a worker must expose at least one catalog — check the worker's "
                "catalogs() RPC returns its catalog",
            )
            return
        limit = ctx.config.options.max_catalogs
        if limit and n > limit:
            yield self.finding(
                ctx,
                cat.id,
                f"worker advertises {n} catalogs (> {limit})",
                "a worker exposing this many catalogs is usually a bug or a "
                "listing that should be split; raise options.max_catalogs if "
                "this is intentional",
            )


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
    summary = "The catalog must carry a 'vgi.doc_llm' tag for agents."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if blank(cat.description_llm):
            yield self.finding(
                ctx,
                cat.id,
                f"catalog missing '{TAG_DOC_LLM}' tag",
                "add a catalog 'vgi.doc_llm' tag describing what it covers "
                "and the questions it answers, for LLM/agent tool selection",
            )


@register
class CatalogMarkdownDescription(Rule):
    code = "VGI003"
    name = "catalog-description-md"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "The catalog must carry a 'vgi.doc_md' tag for human docs."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if blank(cat.description_md):
            yield self.finding(
                ctx,
                cat.id,
                f"catalog missing '{TAG_DOC_MD}' tag",
                "add a catalog 'vgi.doc_md' tag: a Markdown overview shown "
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


@register
class CatalogSupport(Rule):
    code = "VGI009"
    name = "catalog-support"
    category = CAT
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG,)
    summary = "The catalog should advertise a support contact and support policy URL."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        tags = ctx.catalog.tags
        if not tags.has(TAG_SUPPORT_CONTACT):
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"catalog has no support contact ('{TAG_SUPPORT_CONTACT}')",
                "add a 'vgi.support_contact' tag (email or URL) for issues and bugs",
            )
        if not tags.has(TAG_SUPPORT_POLICY_URL):
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"catalog has no support policy URL ('{TAG_SUPPORT_POLICY_URL}')",
                "add a 'vgi.support_policy_url' tag linking to the support/SLA policy",
            )


@register
class SupportLinksValid(Rule):
    code = "VGI010"
    name = "support-links-valid"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "A URL in the support contact / policy must be a valid http(s) URL."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        tags = ctx.catalog.tags
        contact = tags.get(TAG_SUPPORT_CONTACT) or ""
        # The contact may be an email; only validate it when it looks like a URL.
        if contact and _looks_like_url_attempt(contact) and not _is_http_url(contact):
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"support contact looks like a URL but is not a valid http(s) URL: {contact!r}",
                "use an absolute http(s) URL (or a plain email address)",
            )
        policy = tags.get(TAG_SUPPORT_POLICY_URL) or ""
        if policy and not _is_http_url(policy):
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"support policy URL is not a valid http(s) URL: {policy!r}",
                "use an absolute http(s) URL for the support policy",
            )


@register
class CatalogIconUrlValid(Rule):
    code = "VGI014"
    name = "catalog-icon-url-valid"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "vgi.icon_url, when set, must be a valid http(s) URL."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        url = (ctx.catalog.tags.get(TAG_ICON_URL) or "").strip()
        if not url:
            return  # the icon is opt-in; only its shape is checked when present
        if not _is_http_url(url):
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"vgi.icon_url is not a valid http(s) URL: {url!r}",
                "use an absolute http(s) URL to a browser-displayable image "
                "(PNG/SVG/WebP/…) for the catalog's icon/logo",
            )


@register
class CatalogIconImage(Rule):
    code = "VGI015"
    name = "catalog-icon-image"
    category = CAT
    default_severity = Severity.WARNING
    requires_network = True
    targets = (ObjectKind.CATALOG,)
    summary = "vgi.icon_url must resolve to a browser-displayable image at a reasonable resolution."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        probe = ctx.image_probe
        if probe is None:  # no probe wired (offline / --no-check-links)
            return
        url = (ctx.catalog.tags.get(TAG_ICON_URL) or "").strip()
        if not url or not _is_http_url(url):
            return  # VGI014 reports a missing/malformed URL; nothing to fetch
        info = probe(url)
        if info.error is not None:  # DNS/timeout/TLS — unverifiable, stay quiet
            return
        cid = ctx.catalog.id
        if is_broken(info.status):
            yield self.finding(
                ctx,
                cid,
                f"vgi.icon_url is broken (HTTP {info.status}): {url}",
                "fix or remove the dead icon URL",
            )
            return
        if info.fmt not in DISPLAYABLE_IMAGE_FORMATS:
            declared = f" (Content-Type {info.content_type})" if info.content_type else ""
            yield self.finding(
                ctx,
                cid,
                f"vgi.icon_url is not a browser-displayable image{declared}: {url}",
                "point vgi.icon_url at a PNG, SVG, JPEG, WebP, GIF, BMP, ICO, or AVIF "
                "image so browsers can render it in an <img> tag",
            )
            return
        opts = ctx.config.options
        if info.size_bytes is not None and info.size_bytes > opts.icon_max_bytes:
            yield self.finding(
                ctx,
                cid,
                f"vgi.icon_url image is {info.size_bytes} bytes (> {opts.icon_max_bytes}): {url}",
                "ship a smaller icon (compress it or reduce its dimensions) so "
                "listings load quickly",
            )
        # SVG/AVIF report no pixel dimensions — only judge resolution when known.
        if info.width and info.height:
            smaller = min(info.width, info.height)
            larger = max(info.width, info.height)
            if smaller < opts.icon_min_dimension:
                yield self.finding(
                    ctx,
                    cid,
                    f"vgi.icon_url image is only {info.width}x{info.height} "
                    f"(min {opts.icon_min_dimension}px per side): {url}",
                    "use a higher-resolution icon so it stays crisp when scaled up "
                    "in listings and directories",
                )
            elif larger > opts.icon_max_dimension:
                yield self.finding(
                    ctx,
                    cid,
                    f"vgi.icon_url image is {info.width}x{info.height} "
                    f"(max {opts.icon_max_dimension}px per side): {url}",
                    "downscale the icon — an oversized image is wasteful to ship "
                    "for a listing thumbnail",
                )


@register
class DefaultSchemaValid(Rule):
    code = "VGI008"
    name = "default-schema-valid"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "The catalog's default schema must resolve to a schema that exists."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        ds = cat.default_schema
        if blank(ds):
            return  # could not determine the default schema; don't guess
        names = {s.name for s in cat.iter_schemas()}
        if ds not in names:
            yield self.finding(
                ctx,
                cat.id,
                f"default schema {ds!r} is not a schema in this catalog "
                f"(available: {', '.join(sorted(names)) or 'none'})",
                "set the worker's default schema to one it actually exposes, so "
                "agents that land on it without a schema find data",
            )


@register
class DataVersionSpecValid(Rule):
    code = "VGI005"
    name = "data-version-spec-valid"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "data_version_spec, when set, must be a valid semver version range."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        spec = ctx.catalog.data_version_spec
        if not blank(spec) and _parse_spec(spec or "") is None:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"data_version_spec is not a valid version range: {spec!r}",
                "use a semver range like '>=1.0.0,<2.0.0'",
            )


@register
class ReleaseVersionsValid(Rule):
    code = "VGI006"
    name = "release-version-valid"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "Every published data-version release must be a valid semver version."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for rel in ctx.catalog.releases:
            if _parse_version(rel.version) is None:
                yield self.finding(
                    ctx,
                    ctx.catalog.id,
                    f"release version is not valid semver: {rel.version!r}",
                    "use a semver version like '1.2.0'",
                )


@register
class ReleasesWithinSpec(Rule):
    code = "VGI007"
    name = "releases-within-spec"
    category = CAT
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "Every published release must be contained by data_version_spec."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        if blank(cat.data_version_spec):
            return
        spec = _parse_spec(cat.data_version_spec or "")
        if spec is None:
            return  # VGI005 reports the invalid spec
        for rel in cat.releases:
            version = _parse_version(rel.version)
            if version is None:
                continue  # VGI006 reports the invalid version
            if not spec.contains(version, prereleases=True):
                yield self.finding(
                    ctx,
                    cat.id,
                    f"release {rel.version!r} is not within data_version_spec "
                    f"'{cat.data_version_spec}'",
                    "widen data_version_spec or remove/relabel the out-of-range release",
                )


# A small set of common SPDX license identifiers; SPDX also allows a
# "LicenseRef-" prefix for custom/source-available licenses.
_SPDX_IDS = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "MPL-2.0",
        "GPL-2.0-only",
        "GPL-3.0-only",
        "LGPL-2.1-only",
        "LGPL-3.0-only",
        "AGPL-3.0-only",
        "Unlicense",
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "0BSD",
        "Zlib",
        "BSL-1.0",
        "EPL-2.0",
        "Proprietary",
    }
)
_SPDX_LOWER = frozenset(s.lower() for s in _SPDX_IDS)


@register
class LicenseValidSpdx(Rule):
    code = "VGI013"
    name = "license-valid-spdx"
    category = CAT
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG,)
    summary = "vgi.license should be an SPDX identifier (or a LicenseRef-… for custom)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        value = (ctx.catalog.tags.get(TAG_LICENSE) or "").strip()
        if not value:
            return  # presence is VGI's licensing rule; this only checks validity
        if value.lower() in _SPDX_LOWER or value.lower().startswith("licenseref-"):
            return
        yield self.finding(
            ctx,
            ctx.catalog.id,
            f"vgi.license {value!r} is not a recognized SPDX identifier",
            "use an SPDX id (e.g. MIT, Apache-2.0) or a 'LicenseRef-<name>' for a "
            "custom/source-available license so tools can interpret it",
        )
