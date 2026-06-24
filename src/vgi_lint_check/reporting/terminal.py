"""Human-friendly terminal report (rich): object-grouped, with fix hints,
coverage bars, a quality score, and a cross-version comparison table."""

from __future__ import annotations

import io

from rich.console import Console

from .json_reporter import to_dict

_SEV_STYLE = {"error": "bold red", "warning": "yellow", "info": "cyan"}


def _bar(ratio: float, width: int = 10) -> str:
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def render_terminal(report, *, color: bool = True) -> str:
    doc = to_dict(report)
    buf = io.StringIO()
    con = Console(
        file=buf, force_terminal=color, no_color=not color, width=100, highlight=False
    )

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
        _print_findings(con, r["findings"])
        _print_coverage(con, r["coverage"])
        con.print(
            f"  [bold]Catalog Quality Score[/bold]  {r['score']} / 100"
            f"     [dim]{_summary_line(r['counts'])}[/dim]"
        )

    if doc.get("comparison"):
        _print_comparison(con, doc["comparison"])

    con.print()
    s = doc["summary"]
    if s["passed"]:
        con.print("  [green]✓ passed[/green]")
    else:
        con.print(
            f"  [bold red]✗ failed[/bold red] — findings ≥ {s['fail_on']} "
            f"({_summary_line(s['findings'])})"
        )
    return buf.getvalue()


def _print_findings(con, findings):
    if not findings:
        con.print("  [green]✓ no findings[/green]")
        return
    by_obj: dict[str, list] = {}
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
            con.print(
                f"    [{style}]{f['code']}[/{style}]  {f['severity']:<7} "
                f"{f['message']}{tag}"
            )
            con.print(f"        [dim]↳ {f['fix']}[/dim]")


def _print_coverage(con, coverage):
    parts = []
    for fam, ratio in coverage.items():
        if ratio is None:
            continue
        parts.append(f"{fam} {_bar(ratio)} {int(ratio * 100):>3}%")
    if parts:
        con.print("  [bold]Coverage[/bold]")
        for p in parts:
            con.print(f"    {p}")


def _print_comparison(con, comp):
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


def _summary_line(counts: dict) -> str:
    return (
        f"{sum(counts.values())} findings  "
        f"{counts.get('error', 0)} error · {counts.get('warning', 0)} warning · "
        f"{counts.get('info', 0)} info"
    )
