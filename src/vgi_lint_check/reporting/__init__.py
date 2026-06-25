"""Reporters: render a Report to terminal / json / agent / jsonl."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent_reporter import render_agent
from .json_reporter import render_json, render_jsonl, to_dict
from .terminal import render_terminal

if TYPE_CHECKING:
    from ..result import Report

FORMATS = ("terminal", "json", "agent", "jsonl")


def render(
    report: Report,
    fmt: str,
    *,
    color: bool = True,
    group_by: str = "rule",
    max_per_rule: int = 10,
) -> str:
    """Render ``report`` in the requested format (terminal/json/jsonl/agent)."""
    if fmt == "terminal":
        return render_terminal(report, color=color, group_by=group_by, max_per_rule=max_per_rule)
    if fmt == "json":
        return render_json(report)
    if fmt == "jsonl":
        return render_jsonl(report)
    if fmt == "agent":
        return render_agent(report)
    raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")


__all__ = [
    "render",
    "render_terminal",
    "render_json",
    "render_jsonl",
    "render_agent",
    "to_dict",
    "FORMATS",
]
