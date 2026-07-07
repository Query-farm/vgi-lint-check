"""LLM-assisted tutorial planning: propose a topic suite, draft a tutorial.

``suggest_tutorials`` mirrors ``simulate.suggest_tasks``: it feeds the worker's
catalog to the LLM in small **coverage-driven batches** and asks for a ranked set
of topic-specific tutorials, recomputing which objects are still uncovered each
round so the suite is sized to the worker. ``draft_tutorial`` turns one chosen
topic into a first-draft ``.vgi.md`` a human then edits (and ``verify`` fills in
the pinned results). Both reuse the existing ``review`` backend contract.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

from ..simulate import _extract_json, _object_lines, _unique_objects, build_listing

if TYPE_CHECKING:
    from ..model import Catalog
    from ..review import ReviewBackend

_BATCH = 6
_MAX_ROUNDS = 12

_SUGGEST = (
    "You design SQL tutorials for a DuckDB extension worker (catalog '{catalog}'). "
    "Propose deep, topic-specific tutorials — each a distinct real job a user "
    "searches for, not a tour of the API. Cover the TARGET OBJECTS below. Return "
    "ONLY a JSON array of "
    '{{"slug","title","keyword","job","tier","functions","with"}} where: slug is '
    "kebab-case; title is a task-shaped searchable phrase (30-70 chars); keyword is "
    "the primary search query the tutorial targets (2-5 words a real user would "
    "google); job is one sentence; tier is quickstart|recipe|composition; functions "
    "is the list of qualified object names it exercises; and 'with' is a list of "
    "OTHER worker catalog ids it composes with (only for tier=composition, drawn from "
    "the FLEET list below — else []).\n\n"
    "TARGET OBJECTS (uncovered):\n{targets}\n\n"
    "ALREADY PROPOSED (avoid duplicating):\n{covered}\n\n"
    "WORKER OVERVIEW:\n{overview}\n\n"
    "{fleet}"
)

_COMPOSE = (
    "Propose 1-2 COMPOSITION tutorials that combine the DuckDB worker catalog "
    "'{catalog}' with a genuinely related worker from the FLEET, for a bigger "
    "real job than either does alone (e.g. a RAG stack, a redaction pipeline, an "
    "enrichment join). Return ONLY a JSON array of "
    '{{"slug","title","keyword","job","tier","functions","with"}} with '
    "tier=\"composition\" and 'with' listing the partner catalog ids from the "
    "FLEET. Only propose a composition that a real user would actually build.\n\n"
    "THIS WORKER:\n{overview}\n\n{fleet}"
)

_DRAFT = (
    "Write a complete DuckDB tutorial as a single .vgi.md file for the worker "
    "catalog '{worker}'. The job: {job}\n\n"
    "Rules: start with YAML front-matter containing exactly these keys — title "
    "(task-shaped, 30-70 chars), slug: {slug}, worker: {worker}, description "
    "(120-200 chars, NO colon characters), keywords (list), difficulty, est_minutes, "
    "tier: {tier}, dataset (name+provenance), datePublished: {today}, dateModified: "
    "{today}, runtime: {{wasm: auto}}. Then a one-paragraph problem statement BEFORE "
    "any code. Then 3-4 steps as ```sql {{role=step expect=rows}}``` blocks, each call "
    "FULLY QUALIFIED as {worker}.main.fn(...) (never SET search_path), each followed "
    "by a ```result``` block with placeholder values you mark TODO. End with a "
    "'Next steps' section containing at least two markdown links. Use only these "
    "functions:\n{functions}\n\nReturn ONLY the file content, no commentary."
)


def suggest_tutorials(
    catalog: Catalog,
    backend: ReviewBackend,
    cap: int = 0,
    fleet: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Propose a coverage-sized set of tutorials for ``catalog`` via the LLM.

    Args:
        catalog: The attached worker catalog.
        backend: An LLM backend (``complete(prompt) -> str``).
        cap: Stop after proposing this many tutorials (0 = size to the worker).
        fleet: Optional ``{worker_id: one-liner}`` index of the OTHER workers, so the
            planner can propose cross-worker ``composition`` tutorials (a ``with``
            list of partner ids). Without it, it stays single-worker.

    Returns:
        A list of proposal dicts (slug/title/keyword/job/tier/functions/with).
    """
    objects = _unique_objects(catalog)
    lines = _object_lines(catalog)
    overview = build_listing(catalog)
    cat_id = objects[0][0].split(".")[0] if objects else ""
    fleet_block = _fleet_block(fleet, cat_id)
    covered: set[str] = set()
    proposed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(_MAX_ROUNDS):
        uncovered = [(q, b) for q, b in objects if q not in covered]
        if not uncovered or (cap and len(proposed) >= cap):
            break
        targets = "\n".join(lines.get(q, q) for q, _ in uncovered[:_BATCH])
        prompt = _SUGGEST.format(
            catalog=cat_id,
            targets=targets,
            covered="\n".join(f"{p['slug']}: {p.get('title', '')}" for p in proposed) or "(none)",
            overview=overview,
            fleet=fleet_block,
        )
        data = _extract_json(backend.complete(prompt))
        batch = (
            [d for d in data if isinstance(d, dict) and d.get("slug")]
            if isinstance(data, list)
            else []
        )
        if not batch:
            break
        before = len(covered)
        for d in batch:
            if d["slug"] in seen:
                continue
            seen.add(d["slug"])
            proposed.append(d)
            for fn in d.get("functions") or []:
                covered.add(str(fn))
                covered.update(q for q, b in objects if b == str(fn) or q.endswith(f".{fn}"))
        if len(covered) == before:  # no forward progress
            break
    proposed = proposed[:cap] if cap else proposed
    # Dedicated composition round: coverage-driven batching never volunteers a
    # cross-worker tutorial, so ask for them explicitly when a fleet is given.
    if fleet and not any(p.get("tier") == "composition" for p in proposed):
        comp = _extract_json(
            backend.complete(_COMPOSE.format(catalog=cat_id, overview=overview, fleet=fleet_block))
        )
        if isinstance(comp, list):
            for d in comp:
                slug = d.get("slug") if isinstance(d, dict) else None
                if isinstance(slug, str) and slug not in seen and d.get("tier") == "composition":
                    seen.add(slug)
                    proposed.append(d)
    return proposed


def _fleet_block(fleet: dict[str, str] | None, cat_id: str) -> str:
    """Render the FLEET section of the prompt (empty when no fleet is given)."""
    if not fleet:
        return "(No fleet provided — set tier only to quickstart/recipe, 'with' to [].)"
    others = "\n".join(f"- {k}: {v}" for k, v in sorted(fleet.items()) if k != cat_id)
    return (
        "FLEET — other workers you may compose with. Propose 1-2 tier=composition "
        "tutorials that combine this worker with a genuinely related one, naming the "
        "partner catalog ids in 'with':\n" + others
    )


def draft_tutorial(
    catalog: Catalog, backend: ReviewBackend, *, worker: str, slug: str, tier: str, job: str
) -> str:
    """Generate a first-draft ``.vgi.md`` for ``job`` from the worker's catalog."""
    lines = _object_lines(catalog)
    functions = "\n".join(sorted(lines.values()))
    prompt = _DRAFT.format(
        worker=worker,
        slug=slug,
        tier=tier,
        job=job,
        today=date.today().isoformat(),
        functions=functions,
    )
    return backend.complete(prompt).strip()


def render_suggestions(proposals: list[dict[str, Any]], fmt: str = "terminal") -> str:
    """Render the proposed suite as terminal text or JSON."""
    if fmt == "json":
        return json.dumps(proposals, indent=2)
    if not proposals:
        return "no tutorials proposed"
    out = [f"Proposed {len(proposals)} tutorial(s):", ""]
    for n, p in enumerate(proposals, 1):
        withs = f" + {', '.join(p['with'])}" if p.get("with") else ""
        out.append(f"{n:2d}. [{p.get('tier', '?')}]{withs} {p.get('title', p['slug'])}")
        out.append(f"    slug: {p['slug']}")
        if p.get("keyword"):
            out.append(f"    🔍    {p['keyword']}")
        if p.get("job"):
            out.append(f"    job:  {p['job']}")
        if p.get("functions"):
            out.append(f"    uses: {', '.join(str(f) for f in p['functions'])}")
        out.append("")
    return "\n".join(out)
