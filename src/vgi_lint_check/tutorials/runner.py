"""Execute a tutorial's steps against a live worker (for ``tutorials verify``).

All of a tutorial's steps run in order on **one** cursor, so state (temp tables,
``SET`` locals) carries forward intentionally — the shared-session model. Each
step is gated read-only/session-local by ``safe_session_sql`` and bounded by
``run_with_timeout``; ``illustrative`` steps are never executed. Results are
compared against the pinned ``result`` block. Nothing raises: a failing step
becomes a :class:`StepResult` with ``ok=False``.
"""

from __future__ import annotations

import time
from typing import Any

from ..rules._util import run_with_timeout, safe_session_sql
from .model import ROLE_ILLUSTRATIVE, ResultBlock, StepResult, TutorialDoc


def run_tutorial(con: Any, doc: TutorialDoc, *, timeout: float = 30.0) -> list[StepResult]:
    """Run ``doc``'s non-illustrative steps on one cursor, in order.

    Args:
        con: A live worker connection (its ``cursor()`` gives a shared session).
        doc: The parsed tutorial.
        timeout: Per-step wall-clock cap in seconds (0 disables the guard).

    Returns:
        One :class:`StepResult` per executed step, in declaration order.
    """
    cur = con.cursor()
    out: list[StepResult] = []
    for step in doc.steps:
        if step.role == ROLE_ILLUSTRATIVE:
            continue
        if not safe_session_sql(step.sql):
            out.append(
                StepResult(
                    step.index, False, "blocked: not read-only/session-local", [], [], None, 0.0
                )
            )
            continue
        started = time.perf_counter()
        try:
            cols, rows = _exec(cur, step.sql, timeout)
            ok, err = True, None
        except Exception as e:  # noqa: BLE001 — a failed step is data, not a crash
            cols, rows, ok, err = [], [], False, f"{type(e).__name__}: {e}"
        elapsed = time.perf_counter() - started
        matched: bool | None = None
        if ok and step.has_expected and step.expected_result is not None:
            matched = compare_result(step.expected_result, cols, rows)
        out.append(StepResult(step.index, ok, err, cols, rows, matched, elapsed))
    return out


def _exec(cur: Any, sql: str, timeout: float) -> tuple[list[str], list[Any]]:
    """Run one statement and return ``(column_names, rows)``."""
    result = run_with_timeout(cur, lambda: cur.execute(sql), timeout)
    rows = run_with_timeout(cur, lambda r=result: r.fetchall(), timeout)
    cols = [d[0] for d in result.description] if result.description else []
    return cols, list(rows or [])


def compare_result(block: ResultBlock, cols: list[str], rows: list[Any]) -> bool:
    """Compare a live ``(cols, rows)`` result to a pinned ``result`` block.

    Comparison is string-based (the pinned block is text): column names must match
    case-insensitively when both are present, and every rendered cell must match.
    """
    have_headers = bool(block.columns and cols)
    if have_headers and [c.strip().lower() for c in block.columns] != [
        c.strip().lower() for c in cols
    ]:
        return False
    actual = [[_cell(c) for c in row] for row in rows]
    expected = [[c.strip() for c in r] for r in block.rows]
    return actual == expected


def _cell(value: Any) -> str:
    """Render one DB cell the way a pinned result table writes it."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
