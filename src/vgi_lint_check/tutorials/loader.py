"""Load and parse ``*.vgi.md`` tutorial files into the immutable model.

``load_tutorial`` never raises: an unreadable file, missing front-matter, or a
broken fence all produce a :class:`TutorialDoc` with ``parse_error`` set, so the
renderer and linter can report the problem instead of crashing.
"""

from __future__ import annotations

import os
from pathlib import Path

from markdown_it import MarkdownIt

from .fences import parse_fence_info
from .frontmatter import coerce_frontmatter, split_frontmatter
from .model import (
    ROLE_STEP,
    ResultBlock,
    TutorialAsset,
    TutorialDoc,
    TutorialFrontMatter,
    TutorialStep,
)

# A commonmark parser shared across loads (mirrors rules/content.py's _MD).
_MD = MarkdownIt("commonmark")


def load_tutorial(path: str | os.PathLike[str]) -> TutorialDoc:
    """Parse a single tutorial file.

    Args:
        path: Path to a ``*.vgi.md`` file.

    Returns:
        A :class:`TutorialDoc`. On any failure the doc is still returned with
        ``parse_error`` (fatal) or ``fm_errors`` (per-field) populated.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return TutorialDoc(
            path=str(path), front_matter=None, steps=[], body_md="", parse_error=f"unreadable: {e}"
        )

    mapping, body, fm_error = split_frontmatter(text)
    fm: TutorialFrontMatter | None = None
    fm_errors: tuple[str, ...] = ()
    if mapping is not None:
        fm, errs = coerce_frontmatter(mapping)
        fm_errors = tuple(errs)

    steps, step_error = _parse_steps(body)
    assets = _resolve_assets(p, fm, steps, body)

    return TutorialDoc(
        path=str(path),
        front_matter=fm,
        steps=steps,
        body_md=body,
        assets=assets,
        parse_error=fm_error or step_error,
        fm_errors=fm_errors,
    )


def load_dir(root: str | os.PathLike[str]) -> list[TutorialDoc]:
    """Load every ``*.vgi.md`` under ``root`` (recursively), sorted by path."""
    base = Path(root)
    files = sorted(base.rglob("*.vgi.md")) if base.is_dir() else []
    return [load_tutorial(f) for f in files]


def _parse_steps(body: str) -> tuple[list[TutorialStep], str | None]:
    """Walk fence tokens, building steps and pairing adjacent ```result``` blocks."""
    fences = [t for t in _MD.parse(body) if t.type == "fence"]
    steps: list[TutorialStep] = []
    error: str | None = None
    idx = 0
    i = 0
    while i < len(fences):
        tok = fences[i]
        lang, attrs, ferr = parse_fence_info(tok.info)
        if ferr and error is None:
            error = ferr
        if lang == "result":
            i += 1  # a stray/leading result block with no preceding step
            continue

        expected: ResultBlock | None = None
        has_expected = False
        if i + 1 < len(fences):
            nlang, _, _ = parse_fence_info(fences[i + 1].info)
            if nlang == "result":
                expected = _parse_result_block(fences[i + 1].content)
                has_expected = True
                i += 1  # consume the result block

        line = tok.map[0] + 1 if tok.map else 0
        steps.append(
            TutorialStep(
                index=idx,
                role=attrs.get("role", ROLE_STEP),
                expect=attrs.get("expect"),
                sql=tok.content,
                lang=lang or "sql",
                expected_result=expected,
                has_expected=has_expected,
                line=line,
            )
        )
        idx += 1
        i += 1
    return steps, error


def _parse_result_block(content: str) -> ResultBlock:
    """Parse a pinned ```result``` block into columns + string rows.

    The block is a simple whitespace-aligned table: the first non-empty line is
    the header (column names), each following line is a row. Cells are split on
    runs of two-or-more spaces or a tab, so single-token values with no internal
    double-space parse cleanly.
    """
    import re

    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return ResultBlock(columns=[], rows=[])
    split = re.compile(r"\t+| {2,}")
    header = [c.strip() for c in split.split(lines[0].strip())]
    rows = [[c.strip() for c in split.split(ln.strip())] for ln in lines[1:]]
    return ResultBlock(columns=header, rows=rows)


def _resolve_assets(
    p: Path, fm: TutorialFrontMatter | None, steps: list[TutorialStep], body: str
) -> list[TutorialAsset]:
    """Stat declared assets and mark which are referenced by SQL or the body."""
    if fm is None or not fm.assets:
        return []
    base = p.parent
    sql_blob = "\n".join(s.sql for s in steps)
    out: list[TutorialAsset] = []
    for a in fm.assets:
        target = base / a.path
        size = target.stat().st_size if target.exists() else None
        referenced = a.path in sql_blob or a.path in body
        out.append(
            TutorialAsset(
                path=a.path,
                kind=a.kind,
                alt=a.alt,
                provenance=a.provenance,
                sha256=a.sha256,
                size_bytes=size,
                referenced=referenced,
            )
        )
    return out
