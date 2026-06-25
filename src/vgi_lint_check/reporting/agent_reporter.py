"""LLM/agent-oriented Markdown rendering of a Report.

Same data as the JSON contract, formatted as compact sectioned Markdown that
pastes cleanly into a coding-agent prompt. Findings are grouped **by rule**: the
fix is stated once per rule and the affected objects are listed under it, so a
rule firing on many objects costs one fix instruction plus a short list — far
fewer tokens (and far less repetition) than restating the fix per object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._collapse import group_by_rule
from .json_reporter import _rule_summaries, to_dict

if TYPE_CHECKING:
    from ..result import Report


def render_agent(report: Report) -> str:
    """Render ``report`` as compact, agent-friendly Markdown."""
    doc = to_dict(report)
    summaries = _rule_summaries()
    out: list[str] = []
    w = doc["worker"]
    out.append(f"# vgi-lint report: {w['alias']} ({w['location']})")
    s = doc["summary"]
    out.append(
        f"- score: {s['score']} | findings: {s['findings']} | "
        f"passed: {s['passed']} (fail_on={s['fail_on']})"
    )
    out.append("")

    for r in doc["results"]:
        if r["data_version"]:
            out.append(f"## data version {r['data_version']} — score {r['score']}")
        else:
            out.append(f"## score {r['score']}")
        cov = ", ".join(f"{k} {int(v * 100)}%" for k, v in r["coverage"].items() if v is not None)
        if cov:
            out.append(f"coverage: {cov}")
        out.append("")
        if not r["findings"]:
            out.append("No findings. ✓")
            out.append("")
            continue
        for g in group_by_rule(r["findings"], summaries):
            out.append(f"### {g.code} ({g.severity}) — {g.summary}  · {g.count} object(s)")
            shared_fix = g.shared_fix
            if shared_fix:
                out.append(f"- fix: {shared_fix}")
            shared_msg = g.shared_message
            if shared_msg:
                out.append(f"- detail: {shared_msg}")
            for f in g.items:
                new = " [new]" if f["is_new"] else ""
                line = f"  - `{f['object']['qualified']}`{new}"
                if not shared_msg:
                    line += f": {f['message']}"
                out.append(line)
                if not shared_fix:
                    out.append(f"    - fix: {f['fix']}")
            out.append("")

    comp = doc.get("comparison")
    if comp:
        out.append("## data version comparison")
        out.append("| version | score | error | warning | info | Δscore |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for row in comp["rows"]:
            d = "" if row["delta_score"] is None else f"{row['delta_score']:+d}"
            c = row["counts"]
            out.append(
                f"| {row['data_version']} | {row['score']} | "
                f"{c['error']} | {c['warning']} | {c['info']} | {d} |"
            )
        out.append("")
    return "\n".join(out).rstrip() + "\n"
