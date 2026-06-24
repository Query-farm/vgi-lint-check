"""Reporters: render a Report to terminal / json / agent / jsonl."""

from __future__ import annotations

from .agent_reporter import render_agent
from .json_reporter import render_json, render_jsonl, to_dict
from .terminal import render_terminal

FORMATS = ("terminal", "json", "agent", "jsonl")


def render(report, fmt: str, *, color: bool = True) -> str:
    if fmt == "terminal":
        return render_terminal(report, color=color)
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
