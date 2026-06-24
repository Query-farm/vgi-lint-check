"""vgi-lint — a metadata-quality linter for VGI workers.

Public API:
    lint_worker(location, ...) -> Report   # connect, lint, score
    Catalog, Finding, Severity, Category, QualityScore
"""

from __future__ import annotations

from .config import Config
from .core import lint_worker
from .findings import Category, Finding, Severity
from .model import Catalog
from .result import Report, VersionResult
from .scoring import QualityScore

__all__ = [
    "lint_worker",
    "Report",
    "VersionResult",
    "Catalog",
    "Finding",
    "Severity",
    "Category",
    "QualityScore",
    "Config",
]
