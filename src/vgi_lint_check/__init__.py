"""vgi-lint — a metadata-quality linter for VGI workers.

Public API:
    lint_worker(location, ...) -> Report   # connect, lint, score
    Catalog, Finding, Severity, Category, QualityScore
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .config import Config
from .core import lint_worker
from .findings import Category, Finding, Severity
from .levels import Level, LevelReport
from .model import Catalog
from .result import Report, VersionResult
from .scoring import QualityScore

try:
    __version__ = _pkg_version("vgi-lint-check")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+dev"

__all__ = [
    "__version__",
    "Level",
    "LevelReport",
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
