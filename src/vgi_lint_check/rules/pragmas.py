"""VGI7xx — worker pragma documentation."""

from __future__ import annotations

from ..findings import Category, Severity
from ..model import ObjectKind
from ._util import blank
from .base import Rule
from .registry import register

PRAG = Category.PRAGMAS


@register
class PragmaDescription(Rule):
    code = "VGI701"
    name = "pragma-description"
    category = PRAG
    default_severity = Severity.WARNING
    targets = (ObjectKind.PRAGMA,)
    summary = "Every worker pragma should have a description."

    def check(self, ctx):
        for p in ctx.catalog.pragmas:
            if blank(p.description):
                yield self.finding(
                    ctx, p.id, f"pragma {p.name!r} has no description",
                    "add a description explaining what the pragma does",
                )


@register
class PragmaDescriptionQuality(Rule):
    code = "VGI702"
    name = "pragma-description-quality"
    category = PRAG
    default_severity = Severity.INFO
    targets = (ObjectKind.PRAGMA,)
    summary = "A pragma description should explain its usage/parameters."

    def check(self, ctx):
        minlen = ctx.config.options.min_description_chars
        for p in ctx.catalog.pragmas:
            d = p.description
            if not blank(d) and len(d.strip()) < minlen:
                yield self.finding(
                    ctx, p.id,
                    f"pragma description is very short ({len(d.strip())} < {minlen})",
                    "describe the pragma's effect and any parameters",
                )
