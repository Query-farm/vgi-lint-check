"""Assurance levels: what a lint run actually proved about a worker.

The Catalog Quality Score answers *how complete is the metadata*. It does not
answer *how much of it was checked*, and those are different questions: a worker
linted with ``--no-execute`` and no LLM passes can score 100 while nothing beyond
the presence of text was ever verified.

The level is that second axis — a small, absolute, fleet-comparable ladder:

===== ================ ==========================================================
Level Name             Attained when
===== ================ ==========================================================
L0    unverified       the structural tier itself has errors/warnings
L1    structural       static metadata is clean (present, parseable, resolvable)
L2    behavioral       + ``--execute`` ran: examples bind and run, scans respond
L3    semantic         + ``--doc-review``/``--agent-check`` ran: docs judged true,
                       an agent cleared the worker's own test suite
L4    documented       + at least one executable tutorial verified against the
                       worker (supplied by ``vgi-lint fleet``, not by ``lint``)
===== ================ ==========================================================

A tier *clears* when it ran and produced no error- or warning-severity finding.
The bar is fixed at WARNING deliberately: the level must mean the same thing in
every repo, so it does not move with a repo's own ``fail_on``. Info findings are
style/nudge severity and never hold a level back.

The level is monotonic — L3 requires L2 requires L1 — so a single number says
both how far the worker got and where it stopped. ``LevelReport.blocker``
carries the actionable half: *why* it stopped there.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum

from .findings import Finding, Severity

# The severity at which a tier is considered to have failed. Fixed (not the
# repo's fail_on) so levels are comparable across the fleet.
TIER_BAR = Severity.WARNING


class Level(IntEnum):
    """How much of a worker's quality was actually verified."""

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4

    @property
    def label(self) -> str:
        """Short badge form (``L2``)."""
        return f"L{int(self)}"

    @property
    def title(self) -> str:
        """Human name of the level (``behavioral``)."""
        return _TITLES[self]


_TITLES = {
    Level.L0: "unverified",
    Level.L1: "structural",
    Level.L2: "behavioral",
    Level.L3: "semantic",
    Level.L4: "documented",
}

# Tier keys, in ladder order. Each maps to the Level it unlocks.
TIERS = ("structural", "behavioral", "semantic", "documented")
_TIER_LEVEL = {
    "structural": Level.L1,
    "behavioral": Level.L2,
    "semantic": Level.L3,
    "documented": Level.L4,
}


@dataclass(frozen=True)
class TierStatus:
    """Whether one tier ran, and whether it came back clean."""

    name: str
    ran: bool
    errors: int = 0
    warnings: int = 0
    # Why the tier did not run (shown as the blocker when it gates the level).
    skipped_because: str = ""

    @property
    def clear(self) -> bool:
        """True when the tier ran and produced no error/warning findings."""
        return self.ran and self.errors == 0 and self.warnings == 0


@dataclass(frozen=True)
class LevelReport:
    """The attained level plus the per-tier evidence behind it."""

    level: Level = Level.L0
    tiers: dict[str, TierStatus] = field(default_factory=dict)
    blocker: str = "not assessed"

    def to_dict(self) -> dict[str, object]:
        """Serialize for the JSON report contract."""
        return {
            "level": int(self.level),
            "label": self.level.label,
            "title": self.level.title,
            "blocker": self.blocker,
            "tiers": {
                name: {
                    "ran": t.ran,
                    "clear": t.clear,
                    "errors": t.errors,
                    "warnings": t.warnings,
                    "skipped_because": t.skipped_because,
                }
                for name, t in self.tiers.items()
            },
        }


def tier_of(code: str) -> str:
    """The assurance tier a rule code belongs to.

    Derived from the rule's own gating flags rather than a hand-kept list, so a
    new rule lands in the right tier the moment it declares how it is gated.
    Unknown codes (a rule from a newer/older linter reading an old baseline)
    fall back to ``structural`` — the tier that always runs.
    """
    from .rules.registry import REGISTRY

    rule = REGISTRY.get(code)
    if rule is None:
        return "structural"
    if getattr(rule, "requires_review", False) or getattr(rule, "requires_agent", False):
        return "semantic"
    if getattr(rule, "requires_connection", False):
        return "behavioral"
    return "structural"


def compute(
    findings: Iterable[Finding],
    *,
    executed: bool,
    doc_reviewed: bool,
    agent_checked: bool,
    tutorials: bool | None = None,
) -> LevelReport:
    """Compute the assurance level for one lint result.

    ``executed`` / ``doc_reviewed`` / ``agent_checked`` say which passes actually
    ran. ``tutorials`` is the L4 input and is supplied out of band by
    ``vgi-lint fleet`` (True = at least one tutorial verified against the worker,
    False = the worker ships none, None = not assessed).
    """
    counts: dict[str, list[int]] = {name: [0, 0] for name in TIERS}
    for f in findings:
        tier = tier_of(f.code)
        if f.severity is Severity.ERROR:
            counts[tier][0] += 1
        elif f.severity is Severity.WARNING:
            counts[tier][1] += 1

    semantic_ran = doc_reviewed and agent_checked
    semantic_why = ""
    if not semantic_ran:
        missing = [
            flag
            for flag, on in (("--doc-review", doc_reviewed), ("--agent-check", agent_checked))
            if not on
        ]
        semantic_why = f"{' and '.join(missing)} not run"

    tiers = {
        "structural": TierStatus(
            "structural", True, errors=counts["structural"][0], warnings=counts["structural"][1]
        ),
        "behavioral": TierStatus(
            "behavioral",
            executed,
            errors=counts["behavioral"][0],
            warnings=counts["behavioral"][1],
            skipped_because="" if executed else "--no-execute: example queries never ran",
        ),
        "semantic": TierStatus(
            "semantic",
            semantic_ran,
            errors=counts["semantic"][0],
            warnings=counts["semantic"][1],
            skipped_because=semantic_why,
        ),
        "documented": TierStatus(
            "documented",
            tutorials is not None,
            errors=0 if tutorials else 1,
            skipped_because="" if tutorials is not None else "tutorials not assessed",
        ),
    }

    level = Level.L0
    blocker = ""
    for name in TIERS:
        t = tiers[name]
        if t.clear:
            level = _TIER_LEVEL[name]
            continue
        blocker = _blocker(t)
        break

    return LevelReport(level=level, tiers=tiers, blocker=blocker)


def _blocker(t: TierStatus) -> str:
    if not t.ran:
        return t.skipped_because or f"{t.name} tier not run"
    if t.name == "documented":
        return "no verified tutorial"
    bits = []
    if t.errors:
        bits.append(f"{t.errors} error")
    if t.warnings:
        bits.append(f"{t.warnings} warning")
    return f"{t.name} tier has {' and '.join(bits)} finding(s)"
