"""Result aggregates shared by the orchestration and the reporters."""

from __future__ import annotations

from dataclasses import dataclass, field

from .findings import Finding, Severity
from .scoring import QualityScore


@dataclass
class VersionResult:
    catalog: object  # Catalog
    findings: list[Finding]
    quality: QualityScore
    diff_summary: dict[str, int] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return self.quality.score

    @property
    def data_version(self) -> str | None:
        return self.catalog.data_version

    def counts(self) -> dict[str, int]:
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
    location: str
    alias: str
    vgi_version: str | None
    results: list[VersionResult]
    fail_on: Severity
    has_baseline: bool = False
    comparison: object | None = None  # comparison.Comparison

    def passed(self) -> bool:
        return not any(
            r.gating_findings(self.fail_on, self.has_baseline) for r in self.results
        )

    def total_counts(self) -> dict[str, int]:
        out = {"error": 0, "warning": 0, "info": 0}
        for r in self.results:
            for k, v in r.counts().items():
                out[k] = out.get(k, 0) + v
        return out
