"""Rule base class and the per-run context rules receive."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..findings import Category, Finding, Severity
from ..model import Catalog, ObjectId, ObjectKind

if TYPE_CHECKING:
    from ..config import Config


@dataclass
class RuleContext:
    """Per-run context handed to each rule's ``check`` method."""

    catalog: Catalog
    config: Config
    # The live haybarn connection (typed ``Any`` — a third-party DB cursor),
    # present only for rules with ``requires_connection``.
    connection: Any | None = None
    # URL -> HTTP status resolver, wired only for real runs when --check-links is
    # on; network rules no-op when it is None (keeps tests offline).
    link_resolver: Any | None = None
    # URL -> linkcheck.ImageInfo probe, wired alongside link_resolver under
    # --check-links; the icon-image rule (VGI015) no-ops when it is None.
    image_probe: Any | None = None
    # Resolved severity for the rule currently executing (set by the engine).
    severity: Severity = Severity.WARNING
    # Per-executable-example wall-clock seconds, recorded by VGI906 as it runs so
    # VGI908 (slow-example) can report without a second execution pass.
    exec_timings: dict[str, float] = field(default_factory=dict)
    # LLM-pass results, populated by the pipeline only under --doc-review /
    # --agent-check; the requires_review / requires_agent rules read them.
    review_report: Any | None = None  # review.ReviewReport
    sim_report: Any | None = None  # simulate.SimReport
    # Timing tracer, set only under --trace; the engine times each rule with it.
    tracer: Any | None = None  # trace.Tracer
    # Lazily-computed, run-shared corpus coverage (parse-based). Do not read
    # directly — call ``corpus_coverage()`` so it is parsed at most once per run.
    _corpus: Any = None  # corpus.CorpusCoverage
    # Lazily-computed scan probes, shared by VGI911 (responsiveness) and VGI912
    # (batch shape) so each relation is scanned once per run. Populated by
    # ``rules.execution.scan_probes``; None means "not probed yet".
    _scan_probes: Any = None  # list[tuple[ObjectId, str, execution.ScanProbe]]

    def corpus_coverage(self) -> Any:
        """Parse-based coverage of the worker surface (memoized for the run)."""
        if self._corpus is None:
            from ..corpus import compute_corpus_coverage

            self._corpus = compute_corpus_coverage(self.catalog)
        return self._corpus


class Rule(ABC):
    code: str  # e.g. "VGI112" — unique, stable
    name: str  # short slug
    category: Category
    default_severity: Severity
    targets: tuple[ObjectKind, ...] = ()
    requires_connection: bool = False  # gated by --execute (runs SQL on the worker)
    requires_network: bool = False  # gated by --check-links (makes outbound HTTP)
    requires_review: bool = False  # gated by --doc-review (LLM doc-quality judge)
    requires_agent: bool = False  # gated by --agent-check (runs `simulate` + LLM)
    summary: str = ""  # one-liner shown in `rules`/`explain` and agent output

    @abstractmethod
    def check(self, ctx: RuleContext) -> Iterable[Finding]:  # pragma: no cover
        ...

    # Helper to build a finding at the engine-resolved severity for this rule.
    def finding(self, ctx: RuleContext, object_id: ObjectId, message: str, hint: str) -> Finding:
        return Finding(
            code=self.code,
            severity=ctx.severity,
            category=self.category,
            object_id=object_id,
            message=message,
            hint=hint,
        )
