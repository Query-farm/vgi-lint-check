"""Per-family coverage and a 0-100 Catalog Quality Score.

Coverage is computed structurally from the catalog (independent of which rules
are enabled) so the score is stable run-to-run.

Formula::

    base    = 100 * weighted_mean(family_coverage, FAMILY_WEIGHTS)
              # weights renormalized over the families that apply to the catalog
    headline = weighted_mean([base, agent_score?, doc_quality?], BLEND_WEIGHTS)
              # the LLM dimensions only participate when their pass (--doc-review /
              # --agent-check) ran; otherwise headline == base
    penalty = min(MAX_ERROR_PENALTY,   ERROR_PENALTY   * #error_findings)
            + min(MAX_WARNING_PENALTY, WARNING_PENALTY * #warning_findings)
    score   = clamp(round(headline - penalty), 0, 100)

INFO findings do not affect the score. Penalties keep a catalog with full
coverage but many real defects from scoring 100.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .findings import Severity
from .model import Catalog

if TYPE_CHECKING:
    from .findings import Finding

# Family weights (sum need not be 1; normalized over present families).
# Categories are now a first-class, required dimension of the metadata.
FAMILY_WEIGHTS = {
    "descriptions": 0.25,
    "columns": 0.25,
    "function_docs": 0.15,
    "examples": 0.15,
    "categories": 0.20,
}
ERROR_PENALTY = 2.0  # points off per ERROR finding (capped)
MAX_ERROR_PENALTY = 30.0
WARNING_PENALTY = 0.5  # points off per WARNING finding (capped)
MAX_WARNING_PENALTY = 15.0

# Headline-blend weights for the optional LLM dimensions (only counted when that
# pass ran). The static metadata score always participates; weights are
# renormalized over whichever components are present.
BLEND_WEIGHTS = {"static": 0.55, "agent": 0.25, "doc_quality": 0.20}


@dataclass
class Coverage:
    """Per-family documentation coverage ratios."""

    # family -> ratio in [0,1], or None when the family is not applicable
    families: dict[str, float | None] = field(default_factory=dict)


@dataclass
class QualityScore:
    """A 0-100 catalog quality score plus its coverage breakdown.

    ``score`` is the headline: static metadata coverage blended with the optional
    LLM dimensions (``agent_score`` agent-suitability, ``doc_quality``
    description-quality) when those passes ran, minus finding penalties.
    ``static_score`` is the metadata-only headline (the pre-LLM number).
    """

    score: int
    coverage: Coverage
    static_score: int = 0
    agent_score: int | None = None
    doc_quality: int | None = None


def _ratio(documented: int, total: int) -> float | None:
    if total == 0:
        return None
    return documented / total


def compute_coverage(cat: Catalog) -> Coverage:
    """Compute per-family documentation coverage for a catalog."""
    # descriptions: schemas + tables/views with a comment
    desc_total = desc_ok = 0
    for s in cat.iter_schemas():
        desc_total += 1
        desc_ok += 1 if (s.comment or "").strip() else 0
    for t in cat.iter_table_like():
        desc_total += 1
        desc_ok += 1 if (t.comment or "").strip() else 0

    # columns: documented columns across tables/views
    col_total = col_ok = 0
    for c in cat.iter_columns():
        col_total += 1
        col_ok += 1 if c.documented else 0

    # function docs: non-table functions with a description/comment
    fn_total = fn_ok = 0
    for f in cat.iter_functions():
        fn_total += 1
        fn_ok += 1 if (f.description or f.comment or "").strip() else 0

    # examples: example-hosting objects (tables/views/macros) with >=1 example
    ex_total = ex_ok = 0
    for obj in cat.iter_table_like():
        ex_total += 1
        ex_ok += 1 if obj.examples else 0
    for m in cat.iter_macros():
        ex_total += 1
        ex_ok += 1 if m.examples else 0

    # categories: objects carrying a valid vgi.category, over the categorizable
    # objects of schemas that declare a vgi.categories registry. None (skipped)
    # for non-adopters, so the family only shows for workers using categories.
    cat_total = cat_ok = 0
    for s in cat.iter_schemas():
        if not s.categories:
            continue
        valid = {c.name for c in s.categories}
        for cobj in s.iter_categorizable():
            cat_total += 1
            cat_ok += 1 if cobj.category in valid else 0

    # settings / pragmas (reported but not weighted into the headline score)
    set_total = len(cat.settings)
    set_ok = sum(1 for s in cat.settings if (s.description or "").strip())
    prag_total = len(cat.pragmas)
    prag_ok = sum(1 for p in cat.pragmas if (p.description or "").strip())

    return Coverage(
        families={
            "descriptions": _ratio(desc_ok, desc_total),
            "columns": _ratio(col_ok, col_total),
            "function_docs": _ratio(fn_ok, fn_total),
            "examples": _ratio(ex_ok, ex_total),
            "categories": _ratio(cat_ok, cat_total),
            "settings": _ratio(set_ok, set_total),
            "pragmas": _ratio(prag_ok, prag_total),
        }
    )


def compute(
    cat: Catalog,
    findings: Iterable[Finding],
    *,
    agent_score: int | None = None,
    doc_quality: int | None = None,
) -> QualityScore:
    """Compute the quality score from coverage, optional LLM dimensions, and penalties.

    ``agent_score`` (0-100 agent-suitability from ``simulate``) and ``doc_quality``
    (0-100, normalized LLM doc review) are blended into the headline only when the
    corresponding pass ran; otherwise the headline is the static metadata score.
    """
    coverage = compute_coverage(cat)
    weighted_sum = 0.0
    weight_total = 0.0
    for family, weight in FAMILY_WEIGHTS.items():
        ratio = coverage.families.get(family)
        if ratio is None:
            continue
        weighted_sum += weight * ratio
        weight_total += weight
    base = (weighted_sum / weight_total * 100.0) if weight_total else 100.0

    # Blend the static base with whichever LLM dimensions were produced.
    components = [(base, BLEND_WEIGHTS["static"])]
    if agent_score is not None:
        components.append((float(agent_score), BLEND_WEIGHTS["agent"]))
    if doc_quality is not None:
        components.append((float(doc_quality), BLEND_WEIGHTS["doc_quality"]))
    wsum = sum(w for _, w in components)
    blended = sum(v * w for v, w in components) / wsum

    finding_list = list(findings)
    errors = sum(1 for f in finding_list if f.severity is Severity.ERROR)
    warnings = sum(1 for f in finding_list if f.severity is Severity.WARNING)
    penalty = min(MAX_ERROR_PENALTY, errors * ERROR_PENALTY) + min(
        MAX_WARNING_PENALTY, warnings * WARNING_PENALTY
    )

    def _clamp(v: float) -> int:
        return max(0, min(100, round(v)))

    return QualityScore(
        score=_clamp(blended - penalty),
        coverage=coverage,
        static_score=_clamp(base - penalty),
        agent_score=agent_score,
        doc_quality=doc_quality,
    )
