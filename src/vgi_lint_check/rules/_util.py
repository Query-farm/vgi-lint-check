"""Small shared helpers for rules."""

from __future__ import annotations

import re


def blank(s) -> bool:
    return not (s and str(s).strip())


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_trivial_echo(comment: str | None, name: str | None) -> bool:
    """True when a comment merely restates the object's name."""
    if blank(comment) or blank(name):
        return False
    return _normalize(comment) == _normalize(name)
