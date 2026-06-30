"""Stable, self-describing machine output for coding agents.

The JSON document is the contract (``schema_version``); the agent and jsonl
renderers derive from the same ``to_dict`` so human and machine views never
diverge.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..findings import Finding
    from ..result import Report

SCHEMA_VERSION = 1


@lru_cache(maxsize=1)
def report_schema() -> dict[str, Any]:
    """Return the parsed JSON Schema for the ``schema_version`` 1 report."""
    text = (files("vgi_lint_check") / "schema" / "report-v1.json").read_text()
    data: dict[str, Any] = json.loads(text)
    return data


def _rule_summaries() -> dict[str, str]:
    from ..rules.registry import REGISTRY

    return {code: (cls.summary or "") for code, cls in REGISTRY.items()}


def _finding_dict(f: Finding, summaries: dict[str, str]) -> dict[str, Any]:
    oid = f.object_id
    return {
        "code": f.code,
        "severity": f.severity.label,
        "category": str(f.category),
        "is_new": f.is_new,
        "object": {
            "kind": str(oid.kind),
            "qualified": oid.qualified(),
            "schema": oid.schema,
            "name": oid.name,
            "column": oid.column,
        },
        "message": f.message,
        "fix": f.hint,
        "rule": {
            "summary": summaries.get(f.code, ""),
            "explain": f"vgi-lint explain {f.code}",
        },
    }


def to_dict(report: Report) -> dict[str, Any]:
    """Build the stable JSON-contract document for a report."""
    summaries = _rule_summaries()
    results = []
    for r in report.results:
        results.append(
            {
                "data_version": r.data_version,
                "score": r.score,
                "static_score": r.quality.static_score,
                "agent_score": r.quality.agent_score,
                "doc_quality": r.quality.doc_quality,
                "coverage": r.quality.coverage.families,
                "diff": r.diff_summary,
                "counts": r.counts(),
                "findings": [_finding_dict(f, summaries) for f in r.findings],
            }
        )
    doc = {
        "tool": "vgi-lint",
        "schema_version": SCHEMA_VERSION,
        "worker": {
            "location": report.location,
            "alias": report.alias,
            "vgi_version": report.vgi_version,
        },
        "summary": {
            "versions": [r.data_version for r in report.results],
            "score": report.results[0].score if report.results else None,
            "findings": report.total_counts(),
            "passed": report.passed(),
            "fail_on": report.fail_on.label,
        },
        "results": results,
        "comparison": _comparison_dict(report.comparison),
    }
    return doc


def _comparison_dict(comp: Any) -> dict[str, Any] | None:
    if comp is None:
        return None
    return {
        "rows": [
            {
                "data_version": row.data_version,
                "score": row.score,
                "counts": row.counts,
                "delta_score": row.delta_score,
                "added_objects": row.added_objects,
                "removed_objects": row.removed_objects,
                "identical_to_prev": row.identical_to_prev,
            }
            for row in comp.rows
        ]
    }


def render_json(report: Report) -> str:
    """Render the report as a pretty-printed JSON document."""
    return json.dumps(to_dict(report), indent=2)


def render_jsonl(report: Report) -> str:
    """One JSON object per line: the summary, then each finding (with version)."""
    summaries = _rule_summaries()
    lines = [json.dumps({"type": "summary", **to_dict(report)["summary"]})]
    for r in report.results:
        for f in r.findings:
            d = _finding_dict(f, summaries)
            d["type"] = "finding"
            d["data_version"] = r.data_version
            lines.append(json.dumps(d))
    return "\n".join(lines)
