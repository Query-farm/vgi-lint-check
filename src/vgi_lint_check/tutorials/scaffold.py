"""Scaffold a compliant tutorial skeleton for ``vgi-lint tutorials init``.

The output carries the full required front-matter set and the structural bones
the linter enforces (a problem paragraph before the first query, a qualified
first step that returns rows, a pinned result, and a next-steps section with
links), so an author starts from something that already passes the static rules
and only has to fill in the real content.
"""

from __future__ import annotations

from datetime import date


def scaffold_tutorial(*, worker: str, slug: str, tier: str) -> str:
    """Return a compliant ``.vgi.md`` skeleton string for ``worker``/``slug``."""
    today = date.today().isoformat()
    title = f"Get started with the {worker} worker in DuckDB"
    # No ':' in the value — an unquoted colon breaks YAML front-matter.
    description = (
        f"A hands-on {worker} tutorial that attaches the worker to DuckDB and runs your first "
        f"query against it, then builds toward a real task — entirely in SQL, no external service."
    )
    return f"""---
title: {title}
slug: {slug}
worker: {worker}
description: {description}
keywords: [{worker}, duckdb, tutorial]
difficulty: {"beginner" if tier == "quickstart" else "intermediate"}
est_minutes: 6
tier: {tier}
dataset: {{name: "Inline sample rows", provenance: "synthetic, in-tutorial VALUES"}}
datePublished: {today}
dateModified: {today}
runtime: {{wasm: auto}}
---

## The problem

Describe the real task a reader is stuck on here — one concrete paragraph, before
any SQL, naming who has this problem and why the obvious approach falls short. The
{worker} worker solves it as ordinary DuckDB SQL.

```sql {{role=step expect=rows}}
SELECT {worker}.main.your_function('replace-me') AS result;
```
```result
result
replace-me
```

## Build toward the real outcome

Add two or three more steps that layer toward something a reader would ship, each
fully-qualified (`{worker}.main.fn(...)`) so every block is self-contained.

## Next steps

- Read the [{worker} reference](https://github.com/Query-farm/vgi-{worker}) for the
  full function catalog.
- Browse [Query.Farm](https://query.farm) for the rest of the VGI worker fleet.
"""
