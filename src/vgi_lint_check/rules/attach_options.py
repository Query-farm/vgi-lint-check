"""VGI10xx — attach-option documentation.

A worker advertises its attach-time options through ``vgi_catalogs()`` *before*
attach (name, description, type, default). An agent picking the worker relies on
those descriptions to know what each option does and which are required (implied
by the absence of a default), so they should be documented like any other API.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import ObjectKind
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .registry import register

AO = Category.ATTACH_OPTIONS


@register
class AttachOptionDescription(Rule):
    code = "VGI1001"
    name = "attach-option-description"
    category = AO
    default_severity = Severity.WARNING
    targets = (ObjectKind.ATTACH_OPTION,)
    summary = "Every advertised attach option should have a description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for opt in ctx.catalog.iter_attach_options():
            if blank(opt.description):
                req = " (required — it has no default)" if opt.required else ""
                yield self.finding(
                    ctx,
                    opt.id,
                    f"attach option {opt.name!r} has no description{req}",
                    "describe what the option controls and its accepted values so "
                    "callers know how to attach the worker",
                )


@register
class AttachOptionDescriptionQuality(Rule):
    code = "VGI1002"
    name = "attach-option-description-quality"
    category = AO
    default_severity = Severity.INFO
    targets = (ObjectKind.ATTACH_OPTION,)
    summary = "An attach-option description should add information, not restate the name."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_description_chars
        for opt in ctx.catalog.iter_attach_options():
            d = opt.description
            if blank(d):
                continue
            if is_trivial_echo(d, opt.name):
                yield self.finding(
                    ctx,
                    opt.id,
                    f"attach-option description just restates {opt.name!r}",
                    "describe what the option does, not just its name",
                )
            elif len((d or "").strip()) < minlen:
                yield self.finding(
                    ctx,
                    opt.id,
                    f"attach-option description is very short "
                    f"({len((d or '').strip())} < {minlen})",
                    "expand the description so callers understand the option",
                )
