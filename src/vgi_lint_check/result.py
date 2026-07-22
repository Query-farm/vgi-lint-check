"""Result aggregates shared by the orchestration and the reporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import WaiverUsage
from .findings import Finding, Severity
from .levels import LevelReport
from .scoring import QualityScore

if TYPE_CHECKING:
    from .comparison import Comparison
    from .model import Catalog


@dataclass
class VersionResult:
    """Findings, quality score, assurance level, and diff summary for one version."""

    catalog: Catalog
    findings: list[Finding]
    quality: QualityScore
    diff_summary: dict[str, int] = field(default_factory=dict)
    # How much was actually verified (see levels.py). Defaults to "nothing but
    # the structural tier ran" so a hand-built result is never over-claimed.
    level: LevelReport = field(default_factory=LevelReport)
    # Waiver audit, populated only under --audit-waivers.
    waiver_audit: list[WaiverUsage] = field(default_factory=list)

    @property
    def score(self) -> int:
        """The 0-100 Catalog Quality Score for this version."""
        return self.quality.score

    @property
    def data_version(self) -> str | None:
        """The data version this result was produced for (or None)."""
        return self.catalog.data_version

    def counts(self) -> dict[str, int]:
        """Count findings by severity label for this version."""
        out = {"error": 0, "warning": 0, "info": 0}
        for f in self.findings:
            out[f.severity.label] = out.get(f.severity.label, 0) + 1
        return out

    def gating_findings(self, fail_on: Severity, has_baseline: bool) -> list[Finding]:
        """Findings that count toward CI failure for this version."""
        if fail_on is Severity.OFF:  # --fail-on never
            return []
        out = []
        for f in self.findings:
            if f.severity < fail_on:
                continue
            if has_baseline and not f.is_new:
                continue
            out.append(f)
        return out


@dataclass
class Report:
    """The full lint result for a worker across all linted data versions."""

    location: str
    alias: str
    vgi_version: str | None
    results: list[VersionResult]
    fail_on: Severity
    has_baseline: bool = False
    comparison: Comparison | None = None
    # True when --audit-waivers ran; dead/expired waivers then gate the run.
    audited_waivers: bool = False

    def dead_waivers(self) -> list[WaiverUsage]:
        """Declared waivers that suppressed nothing, or are themselves malformed.

        Only meaningful once the audit has run — without it every waiver reads as
        dead, because the rules it silences never executed.
        """
        if not self.audited_waivers:
            return []
        return [u for r in self.results for u in r.waiver_audit if u.dead or u.waiver.problems()]

    def passed(self) -> bool:
        """True when no version has gating findings (CI passes).

        Under ``--audit-waivers`` a dead or malformed waiver also fails: the whole
        point of asking for the audit is to be told about them.
        """
        if any(r.gating_findings(self.fail_on, self.has_baseline) for r in self.results):
            return False
        return not self.dead_waivers()

    def total_counts(self) -> dict[str, int]:
        """Sum finding counts by severity across every version."""
        out = {"error": 0, "warning": 0, "info": 0}
        for r in self.results:
            for k, v in r.counts().items():
                out[k] = out.get(k, 0) + v
        return out
