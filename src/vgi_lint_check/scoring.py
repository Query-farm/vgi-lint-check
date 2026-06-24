"""Per-family coverage and a 0-100 Catalog Quality Score.

Coverage is computed structurally from the catalog (independent of which rules
are enabled) so the score is stable run-to-run.

Formula::

    base    = 100 * weighted_mean(family_coverage, FAMILY_WEIGHTS)
              # weights renormalized over the families that apply to the catalog
    penalty = min(MAX_ERROR_PENALTY,   ERROR_PENALTY   * #error_findings)
            + min(MAX_WARNING_PENALTY, WARNING_PENALTY * #warning_findings)
    score   = clamp(round(base - penalty), 0, 100)

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
FAMILY_WEIGHTS = {
    "descriptions": 0.30,
    "columns": 0.30,
    "function_docs": 0.20,
    "examples": 0.20,
}
ERROR_PENALTY = 2.0  # points off per ERROR finding (capped)
MAX_ERROR_PENALTY = 30.0
WARNING_PENALTY = 0.5  # points off per WARNING finding (capped)
MAX_WARNING_PENALTY = 15.0


@dataclass
class Coverage:
    """Per-family documentation coverage ratios."""

    # family -> ratio in [0,1], or None when the family is not applicable
    families: dict[str, float | None] = field(default_factory=dict)


@dataclass
class QualityScore:
    """A 0-100 catalog quality score plus its coverage breakdown."""

    score: int
    coverage: Coverage


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
            "settings": _ratio(set_ok, set_total),
            "pragmas": _ratio(prag_ok, prag_total),
        }
    )


def compute(cat: Catalog, findings: Iterable[Finding]) -> QualityScore:
    """Compute the quality score from coverage and finding penalties."""
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

    errors = sum(1 for f in findings if f.severity is Severity.ERROR)
    warnings = sum(1 for f in findings if f.severity is Severity.WARNING)
    penalty = min(MAX_ERROR_PENALTY, errors * ERROR_PENALTY) + min(
        MAX_WARNING_PENALTY, warnings * WARNING_PENALTY
    )
    score = max(0, min(100, round(base - penalty)))
    return QualityScore(score=score, coverage=coverage)
