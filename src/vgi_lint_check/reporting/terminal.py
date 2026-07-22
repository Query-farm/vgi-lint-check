"""Human-friendly terminal report (rich).

Object-grouped, with fix hints, coverage bars, a quality score, and a
cross-version comparison table.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from rich.console import Console

from ._collapse import group_by_rule
from .json_reporter import _rule_summaries, to_dict

if TYPE_CHECKING:
    from rich.console import Console as RichConsole

    from ..result import Report

_SEV_STYLE = {"error": "bold red", "warning": "yellow", "info": "cyan"}
# Objects listed per rule before collapsing the tail into "+N more".
_MAX_PER_RULE = 10


def _bar(ratio: float, width: int = 10) -> str:
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def render_terminal(
    report: Report,
    *,
    color: bool = True,
    group_by: str = "rule",
    max_per_rule: int = _MAX_PER_RULE,
) -> str:
    """Render the report as a rich-formatted terminal string.

    ``group_by`` is ``"rule"`` (default — collapse a rule that fires on many
    objects into one block) or ``"object"`` (the per-object layout).
    """
    doc = to_dict(report)
    summaries = _rule_summaries()
    buf = io.StringIO()
    con = Console(file=buf, force_terminal=color, no_color=not color, width=100, highlight=False)

    w = doc["worker"]
    ver = f" vgi {w['vgi_version']}" if w["vgi_version"] else ""
    con.print(f"[bold]vgi-lint[/bold]  {w['alias']}  ({w['location']}){ver}")

    multi = len(doc["results"]) > 1
    for r in doc["results"]:
        con.print()
        if multi or r["data_version"]:
            label = r["data_version"] or "default"
            con.rule(f"data version {label}")
        diff = r.get("diff") or {}
        if diff:
            added = " · ".join(f"{v} {k}" for k, v in diff.items() if v)
            if added:
                con.print(f"  [dim]worker added {added}[/dim]")
        if group_by == "object":
            _print_findings_by_object(con, r["findings"])
        else:
            _print_findings_by_rule(con, r["findings"], summaries, max_per_rule)
        _print_coverage(con, r["coverage"])
        dims = []
        if r.get("agent_score") is not None:
            dims.append(f"agent {r['agent_score']}")
        if r.get("doc_quality") is not None:
            dims.append(f"doc-quality {r['doc_quality']}")
        dim_str = (
            f"  [dim](static {r.get('static_score', r['score'])} · {' · '.join(dims)})[/dim]"
            if dims
            else ""
        )
        con.print(
            f"  [bold]Catalog Quality Score[/bold]  {r['score']} / 100{dim_str}"
            f"     [dim]{_summary_line(r['counts'])}[/dim]"
        )
        _print_level(con, r.get("level"))
        _print_waivers(con, r.get("waivers") or [])

    if doc.get("comparison"):
        _print_comparison(con, doc["comparison"])

    con.print()
    s = doc["summary"]
    if s["passed"]:
        con.print("  [green]✓ passed[/green]")
    elif not sum(s["findings"].values()) and s.get("dead_waivers"):
        # The findings line would read "0 findings" and explain nothing; name the
        # actual reason the run failed.
        con.print(
            f"  [bold red]✗ failed[/bold red] — {s['dead_waivers']} waiver(s) suppress "
            f"nothing or are malformed (--audit-waivers)"
        )
    else:
        con.print(
            f"  [bold red]✗ failed[/bold red] — findings ≥ {s['fail_on']} "
            f"({_summary_line(s['findings'])})"
        )
    if sum(s["findings"].values()):
        con.print(
            "  [dim]Tag reference: TAGS.md · run `vgi-lint explain <CODE>` for any rule.[/dim]"
        )
    return buf.getvalue()


def _print_findings_by_rule(
    con: RichConsole,
    findings: list[dict[str, Any]],
    summaries: dict[str, str],
    max_per_rule: int,
) -> None:
    """Group by rule: state the rule + fix once, list affected objects compactly."""
    if not findings:
        con.print("  [green]✓ no findings[/green]")
        return
    for g in group_by_rule(findings, summaries):
        style = _SEV_STYLE.get(g.severity, "white")
        n = g.count
        head = f"  [{style}]{g.code}[/{style}]  {g.severity:<7} {g.summary}"
        head += f"  [dim]({n} object{'s' if n != 1 else ''})[/dim]"
        con.print(head)
        shared_fix = g.shared_fix
        if shared_fix:
            con.print(f"      [dim]↳ {shared_fix}[/dim]")
        shared_msg = g.shared_message
        shown = g.items if max_per_rule <= 0 else g.items[:max_per_rule]
        for f in shown:
            new = " [dim](new)[/dim]" if f["is_new"] else ""
            # Drop the message when identical for all (it adds nothing per line);
            # show per-object fix only when fixes vary across the rule.
            detail = "" if shared_msg else f"  [dim]{f['message']}[/dim]"
            con.print(f"      · [bold]{f['object']['qualified']}[/bold]{detail}{new}")
            if not shared_fix:
                con.print(f"          [dim]↳ {f['fix']}[/dim]")
        hidden = n - len(shown)
        if hidden > 0:
            con.print(f"      [dim]… +{hidden} more (use --format json for all)[/dim]")


def _print_findings_by_object(con: RichConsole, findings: list[dict[str, Any]]) -> None:
    if not findings:
        con.print("  [green]✓ no findings[/green]")
        return
    by_obj: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for f in findings:
        q = f["object"]["qualified"]
        if q not in by_obj:
            by_obj[q] = []
            order.append(q)
        by_obj[q].append(f)
    for q in order:
        items = by_obj[q]
        kind = items[0]["object"]["kind"]
        con.print(f"  [bold]{q}[/bold]  [dim]{kind}[/dim]")
        for f in items:
            style = _SEV_STYLE.get(f["severity"], "white")
            tag = " [dim](new)[/dim]" if f["is_new"] else ""
            con.print(f"    [{style}]{f['code']}[/{style}]  {f['severity']:<7} {f['message']}{tag}")
            con.print(f"        [dim]↳ {f['fix']}[/dim]")


def _print_coverage(con: RichConsole, coverage: dict[str, float | None]) -> None:
    parts = []
    for fam, ratio in coverage.items():
        if ratio is None:
            continue
        parts.append(f"{fam} {_bar(ratio)} {int(ratio * 100):>3}%")
    if parts:
        con.print("  [bold]Coverage[/bold]")
        for p in parts:
            con.print(f"    {p}")


_LEVEL_STYLE = {0: "red", 1: "yellow", 2: "cyan", 3: "green", 4: "bold green"}


def _print_level(con: RichConsole, level: dict[str, Any] | None) -> None:
    """Print the assurance level — how much of the score was actually verified."""
    if not level:
        return
    style = _LEVEL_STYLE.get(int(level["level"]), "white")
    ran = [name for name, t in level["tiers"].items() if t["ran"]]
    blocker = f"  [dim]— {level['blocker']}[/dim]" if level.get("blocker") else ""
    con.print(
        f"  [bold]Assurance Level[/bold]        "
        f"[{style}]{level['label']} {level['title']}[/{style}]"
        f"{blocker}"
    )
    con.print(f"    [dim]verified: {', '.join(ran) or 'nothing'}[/dim]")


def _print_waivers(con: RichConsole, waivers: list[dict[str, Any]]) -> None:
    """Print the waiver audit: what each suppression actually bought."""
    if not waivers:
        return
    dead = [w for w in waivers if w["dead"]]
    con.print(f"  [bold]Waivers[/bold]  [dim]({len(waivers)} declared)[/dim]")
    for w in waivers:
        scope = w["scope"] or "catalog-wide"
        if w["dead"]:
            con.print(
                f"    [yellow]{w['code']}[/yellow]  [dim]{scope}[/dim]  "
                f"[yellow]suppresses nothing — delete it[/yellow]"
            )
        elif w.get("unconfirmed"):
            con.print(
                f"    {w['code']}  [dim]{scope} · {w['kind']} · nothing suppressed this "
                f"run, but an execution rule's verdict moves with session state — "
                f"re-check before removing[/dim]"
            )
        else:
            con.print(
                f"    {w['code']}  [dim]{scope} · {w['kind']} · "
                f"hides {w['suppressed']} finding(s) on {len(w['objects'])} object(s)[/dim]"
            )
        for problem in w["problems"]:
            con.print(f"        [yellow]↳ {problem}[/yellow]")
    if dead:
        con.print(
            f"    [yellow]{len(dead)} dead waiver(s): a suppression that hides nothing is a "
            f"standing claim nobody revisits.[/yellow]"
        )


def _print_comparison(con: RichConsole, comp: dict[str, Any]) -> None:
    from rich.table import Table

    con.print()
    table = Table(title="data version comparison", title_justify="left")
    for head in ("version", "score", "error", "warn", "info", "Δscore", "notes"):
        table.add_column(head)
    for row in comp["rows"]:
        d = "" if row["delta_score"] is None else f"{row['delta_score']:+d}"
        notes = []
        if row["identical_to_prev"]:
            notes.append("identical metadata")
        if row["added_objects"]:
            notes.append(f"+{len(row['added_objects'])} objects")
        if row["removed_objects"]:
            notes.append(f"-{len(row['removed_objects'])} objects")
        c = row["counts"]
        table.add_row(
            str(row["data_version"]),
            str(row["score"]),
            str(c["error"]),
            str(c["warning"]),
            str(c["info"]),
            d,
            ", ".join(notes),
        )
    con.print(table)


def _summary_line(counts: dict[str, int]) -> str:
    return (
        f"{sum(counts.values())} findings  "
        f"{counts.get('error', 0)} error · {counts.get('warning', 0)} warning · "
        f"{counts.get('info', 0)} info"
    )
