"""LLM/agent-oriented Markdown rendering of a Report.

Same data as the JSON contract, formatted as compact sectioned Markdown that
pastes cleanly into a coding-agent prompt: every finding carries an imperative
fix and an inline rule summary, grouped by object, with coverage and score.
"""

from __future__ import annotations

from .json_reporter import to_dict


def render_agent(report) -> str:
    doc = to_dict(report)
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
        cov = ", ".join(
            f"{k} {int(v * 100)}%" for k, v in r["coverage"].items() if v is not None
        )
        if cov:
            out.append(f"coverage: {cov}")
        out.append("")
        if not r["findings"]:
            out.append("No findings. ✓")
            out.append("")
            continue
        by_obj: dict[str, list] = {}
        for f in r["findings"]:
            by_obj.setdefault(f["object"]["qualified"], []).append(f)
        for obj, items in by_obj.items():
            kind = items[0]["object"]["kind"]
            out.append(f"### {obj} ({kind})")
            for f in items:
                new = " [new]" if f["is_new"] else ""
                out.append(
                    f"- **{f['code']}** ({f['severity']}){new}: {f['message']}"
                )
                out.append(f"  - fix: {f['fix']}")
                if f["rule"]["summary"]:
                    out.append(f"  - rule: {f['rule']['summary']}")
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
