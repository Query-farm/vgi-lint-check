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
from ..model import (
    TAG_DESCRIPTION_LLM,
    TAG_DESCRIPTION_MD,
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
