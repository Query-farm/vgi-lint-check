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
