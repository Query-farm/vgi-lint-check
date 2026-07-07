"""Executable VGI worker tutorials: loading, validating, and rendering.

Tutorials are ``tutorials/*.vgi.md`` files that live in a worker's own git
repo (not catalog tags). Each is CommonMark + YAML front-matter + SQL fences
annotated with an execution role, plus optional pinned expected-result blocks
and small static assets (data/images/media). This package parses them into an
immutable :class:`~vgi_lint_check.tutorials.model.TutorialDoc` graph, renders
them to self-contained HTML, and (later phases) lints and executes them.
"""

from __future__ import annotations

from .loader import load_dir, load_tutorial
from .model import (
    AttachSpec,
    StepResult,
    TutorialAsset,
    TutorialDoc,
    TutorialFrontMatter,
    TutorialStep,
)
from .render import render_html

__all__ = [
    "AttachSpec",
    "StepResult",
    "TutorialAsset",
    "TutorialDoc",
    "TutorialFrontMatter",
    "TutorialStep",
    "load_dir",
    "load_tutorial",
    "render_html",
]
