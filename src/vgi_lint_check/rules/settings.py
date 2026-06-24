"""VGI6xx — worker setting documentation."""

from __future__ import annotations

from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import ObjectKind
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .registry import register

SET = Category.SETTINGS


@register
class SettingDescription(Rule):
    code = "VGI601"
    name = "setting-description"
    category = SET
    default_severity = Severity.WARNING
    targets = (ObjectKind.SETTING,)
    summary = "Every worker setting should have a description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.settings:
            if blank(s.description):
                yield self.finding(
                    ctx,
                    s.id,
                    f"setting {s.name!r} has no description",
                    "add a description explaining what the setting controls",
                )


@register
class SettingDescriptionQuality(Rule):
    code = "VGI602"
    name = "setting-description-quality"
    category = SET
    default_severity = Severity.INFO
    targets = (ObjectKind.SETTING,)
    summary = "A setting description should add information, not restate the name."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_description_chars
        for s in ctx.catalog.settings:
            d = s.description
            if blank(d):
                continue
            if is_trivial_echo(d, s.name):
                yield self.finding(
                    ctx,
                    s.id,
                    f"setting description just restates {s.name!r}",
                    "describe what the setting does, not just its name",
                )
            elif len((d or "").strip()) < minlen:
                yield self.finding(
                    ctx,
                    s.id,
                    f"setting description is very short ({len((d or '').strip())} < {minlen})",
                    "expand the description so users understand the setting",
                )
