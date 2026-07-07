"""Load a worker's tutorial hub (``tutorials/index.vgi.yaml``) and compute nav.

The hub is the higher-level object that owns a worker's *suite*: the ordered
series of tutorials, the hub-page copy, and — derived from that order — the
prev/next/related navigation injected into each spoke. Individual tutorials do
not link to each other by hand; the hub is the single source of that graph.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .model import HubEntry, TutorialHub, TutorialNav

HUB_FILENAME = "index.vgi.yaml"


def load_hub(path: str | os.PathLike[str]) -> TutorialHub:
    """Parse a hub manifest. Never raises; problems land in ``parse_error``."""
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        return TutorialHub(
            worker="",
            title="",
            description="",
            entries=[],
            path=str(p),
            parse_error=f"unreadable hub: {e}",
        )
    if not isinstance(data, dict):
        return TutorialHub(
            worker="",
            title="",
            description="",
            entries=[],
            path=str(p),
            parse_error="hub must be a YAML mapping",
        )
    entries: list[HubEntry] = []
    for item in data.get("series", []) or []:
        if isinstance(item, str):
            entries.append(HubEntry(slug=item))
        elif isinstance(item, dict) and isinstance(item.get("slug"), str):
            entries.append(
                HubEntry(
                    slug=item["slug"],
                    title=_opt(item.get("title")),
                    summary=_opt(item.get("summary")),
                )
            )
    return TutorialHub(
        worker=str(data.get("worker", "")),
        title=str(data.get("title", "")),
        description=str(data.get("description", "")),
        entries=entries,
        path=str(p),
    )


def find_hub(directory: str | os.PathLike[str]) -> TutorialHub | None:
    """Return the hub in ``directory`` if an ``index.vgi.yaml`` exists there."""
    hub_path = Path(directory) / HUB_FILENAME
    return load_hub(hub_path) if hub_path.is_file() else None


def nav_for(hub: TutorialHub, slug: str) -> TutorialNav:
    """Compute prev/next/siblings for the spoke ``slug`` from the hub order."""
    slugs = [e.slug for e in hub.entries]
    if slug not in slugs:
        return TutorialNav(hub_title=hub.title, siblings=list(hub.entries))
    i = slugs.index(slug)
    prev = hub.entries[i - 1] if i > 0 else None
    nxt = hub.entries[i + 1] if i < len(hub.entries) - 1 else None
    return TutorialNav(
        hub_title=hub.title,
        prev_slug=prev.slug if prev else None,
        prev_title=(prev.title or prev.slug) if prev else None,
        next_slug=nxt.slug if nxt else None,
        next_title=(nxt.title or nxt.slug) if nxt else None,
        siblings=list(hub.entries),
    )


def _opt(value: object) -> str | None:
    """Coerce an optional scalar to ``str``."""
    return None if value is None else str(value)
