"""Opt-in timing tracer for a lint run (``--trace``).

Records nested phase spans and per-rule timings, then writes a human-readable
timeline + "slowest" summaries to a log file so a slow lint can be diagnosed
(e.g. execution rules hammering a live worker) without guessing.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _Event:
    kind: str  # phase | rule
    name: str
    ms: float
    depth: int
    detail: str = ""


@dataclass
class Tracer:
    """Collects timed spans; ``dump()`` writes the report. Cheap when unused."""

    path: Path
    events: list[_Event] = field(default_factory=list)
    _depth: int = 0
    _start: float = field(default_factory=time.perf_counter)

    @contextmanager
    def span(self, kind: str, name: str, detail: str = "") -> Iterator[None]:
        """Time a nested span; records its wall-clock ms on exit (even on error)."""
        depth = self._depth
        self._depth += 1
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._depth -= 1
            self.events.append(
                _Event(kind, name, (time.perf_counter() - t0) * 1000.0, depth, detail)
            )

    def dump(self) -> None:
        """Write the timeline + slowest-rule / phase summaries to ``self.path``."""
        total = (time.perf_counter() - self._start) * 1000.0
        out: list[str] = [f"vgi-lint trace — total {total:,.0f} ms", "", "== timeline =="]
        for e in self.events:
            detail = f"  ({e.detail})" if e.detail else ""
            out.append(f"{e.ms:>10,.1f} ms  {'  ' * e.depth}{e.kind}:{e.name}{detail}")

        rules = sorted((e for e in self.events if e.kind == "rule"), key=lambda e: -e.ms)
        if rules:
            out += ["", "== slowest rules (top 20) =="]
            for e in rules[:20]:
                detail = f"  ({e.detail})" if e.detail else ""
                out.append(f"{e.ms:>10,.1f} ms  {e.name}{detail}")
            rule_total = sum(e.ms for e in rules)
            out.append(f"{rule_total:>10,.1f} ms  [all {len(rules)} rules]")

        phases = sorted((e for e in self.events if e.kind == "phase"), key=lambda e: -e.ms)
        if phases:
            out += ["", "== phases (slowest first) =="]
            for e in phases:
                out.append(f"{e.ms:>10,.1f} ms  {e.name}")

        self.path.write_text("\n".join(out) + "\n")
