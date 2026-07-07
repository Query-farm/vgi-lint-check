"""Build schema.org JSON-LD for a tutorial (TechArticle / HowTo / BreadcrumbList).

Emitting structured data is what makes tutorials legible to search + answer
engines. We always emit a ``TechArticle``; a ``HowTo`` when the tutorial has
executable steps; and a ``BreadcrumbList`` for the hub-and-spoke site graph.
"""

from __future__ import annotations

from .model import ROLE_STEP, TutorialDoc


def build_jsonld(doc: TutorialDoc, base_url: str | None = None) -> list[dict[str, object]]:
    """Return the JSON-LD graph objects for ``doc``.

    Args:
        doc: The parsed tutorial.
        base_url: Optional site base (e.g. ``https://query.farm/tutorials``) used
            to build absolute URLs for breadcrumbs and the article ``url``.

    Returns:
        A list of JSON-LD dicts (each rendered as its own ``<script>`` tag).
    """
    fm = doc.front_matter
    title = (fm.title if fm else None) or doc.slug
    description = (fm.description if fm else None) or ""
    url = f"{base_url.rstrip('/')}/{doc.slug}" if base_url else None

    article: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "TechArticle",
        "headline": title,
        "description": description,
        "author": {"@type": "Organization", "name": "Query Farm"},
        "publisher": {"@type": "Organization", "name": "Query Farm"},
    }
    if fm and fm.keywords:
        article["keywords"] = ", ".join(fm.keywords)
    if fm and fm.date_published:
        article["datePublished"] = fm.date_published
    if fm and fm.date_modified:
        article["dateModified"] = fm.date_modified
    if url:
        article["url"] = url

    graph: list[dict[str, object]] = [article]

    run_steps = [s for s in doc.steps if s.role == ROLE_STEP]
    if run_steps:
        graph.append(
            {
                "@context": "https://schema.org",
                "@type": "HowTo",
                "name": title,
                "description": description,
                "step": [
                    {
                        "@type": "HowToStep",
                        "position": n,
                        "text": (s.sql or "").strip().splitlines()[0] if s.sql.strip() else "",
                    }
                    for n, s in enumerate(run_steps, start=1)
                ],
            }
        )

    crumbs = ["Home"]
    if fm and fm.tier:
        crumbs.append(fm.tier.title())
    crumbs.append(title)
    graph.append(
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": n, "name": name}
                for n, name in enumerate(crumbs, start=1)
            ],
        }
    )
    return graph
