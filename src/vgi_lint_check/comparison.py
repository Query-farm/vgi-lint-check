"""Cross-version comparison for multi-version runs."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .findings import Severity

if TYPE_CHECKING:
    from .findings import Finding
    from .model import Catalog, ObjectId
    from .result import VersionResult


@dataclass
class VersionRow:
    """One row of the cross-version comparison table."""

    data_version: str | None
    score: int
    counts: dict[str, int]
    delta_score: int | None
    added_objects: list[str] = field(default_factory=list)
    removed_objects: list[str] = field(default_factory=list)
    identical_to_prev: bool = False


@dataclass
class Comparison:
    """The ordered set of per-version rows for a multi-version run."""

    rows: list[VersionRow] = field(default_factory=list)


def _counts(findings: Iterable[Finding]) -> dict[str, int]:
    out = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        if f.severity is Severity.ERROR:
            out["error"] += 1
        elif f.severity is Severity.WARNING:
            out["warning"] += 1
        elif f.severity is Severity.INFO:
            out["info"] += 1
    return out


def _object_keys(result: VersionResult) -> set[str]:
    return {o.qualified() for o in _iter_object_ids(result.catalog)}


def _iter_object_ids(cat: Catalog) -> Iterator[ObjectId]:
    for s in cat.iter_schemas():
        yield s.id
    for t in cat.iter_table_like():
        yield t.id
        for c in t.columns:
            yield c.id
    for f in cat.iter_functions():
        yield f.id


def build(results: Iterable[VersionResult]) -> Comparison:
    """Build a comparison from an ordered list of VersionResult-like objects.

    Each result must expose ``.catalog`` (with ``.data_version``), ``.findings``,
    and ``.score`` (an int). Rows are emitted in the given order; deltas compare
    against the previous row.
    """
    rows: list[VersionRow] = []
    prev: VersionResult | None = None
    prev_objs: set[str] = set()
    for r in results:
        objs = _object_keys(r)
        delta: int | None = None if prev is None else (r.score - prev.score)
        added: list[str] = sorted(objs - prev_objs) if prev is not None else []
        removed: list[str] = sorted(prev_objs - objs) if prev is not None else []
        identical = (
            prev is not None
            and not added
            and not removed
            and _same_findings(r.findings, prev.findings)
        )
        rows.append(
            VersionRow(
                data_version=r.catalog.data_version,
                score=r.score,
                counts=_counts(r.findings),
                delta_score=delta,
                added_objects=added,
                removed_objects=removed,
                identical_to_prev=identical,
            )
        )
        prev = r
        prev_objs = objs
    return Comparison(rows=rows)


def _same_findings(a: Iterable[Finding], b: Iterable[Finding]) -> bool:
    ka = {(f.code, f.object_id.qualified()) for f in a}
    kb = {(f.code, f.object_id.qualified()) for f in b}
    return ka == kb
