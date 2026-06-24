"""Data-version discovery and resolution.

A worker publishes data versions as ``releases`` in ``vgi_catalogs('<location>')``
(a JSON string). ``data_version_spec '<semver>'`` selects one at ATTACH. This
module decides *which* versions a run should lint:

- explicit ``--data-version`` specs win;
- ``--all-data-versions`` discovers published releases via ``vgi_catalogs``;
- otherwise lint the single worker-default version (``[None]``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Release:
    """A published data version (a row of the ``releases`` JSON array)."""

    version: str
    released_at: str | None = None
    summary: str = ""
    notes_url: str | None = None


@dataclass(frozen=True)
class CatalogDiscovery:
    """A worker catalog and the data versions it advertises."""

    catalog: str
    implementation_version: str | None
    data_version_spec: str | None
    source_url: str | None
    releases: list[Release]


def discover_catalogs(con: Any, location: str) -> list[CatalogDiscovery]:
    """Run ``vgi_catalogs(location)`` and parse the rows (incl. JSON releases)."""
    cur = con.execute("SELECT * FROM vgi_catalogs(?)", [location])
    names = [d[0] for d in cur.description]
    out: list[CatalogDiscovery] = []
    for row in cur.fetchall():
        r: Any = dict(zip(names, row, strict=False))
        out.append(
            CatalogDiscovery(
                catalog=r.get("catalog"),
                implementation_version=_blank_to_none(r.get("implementation_version")),
                data_version_spec=_blank_to_none(r.get("data_version_spec")),
                source_url=_blank_to_none(r.get("source_url")),
                releases=_parse_releases(r.get("releases")),
            )
        )
    return out


def _blank_to_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_releases(raw: Any) -> list[Release]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
    elif isinstance(raw, list):
        data = raw
    else:
        return []
    releases = []
    for item in data:
        if isinstance(item, dict) and item.get("version"):
            releases.append(
                Release(
                    version=str(item["version"]),
                    released_at=_stringify(item.get("released_at")),
                    summary=str(item.get("summary") or ""),
                    notes_url=item.get("notes_url"),
                )
            )
    return releases


def _stringify(v: Any) -> str | None:
    return None if v is None else str(v)


def resolve_versions(
    con: Any,
    location: str,
    *,
    explicit: list[str] | None = None,
    all_versions: bool = False,
) -> list[str | None]:
    """Return the list of ``data_version_spec`` values to lint.

    ``[None]`` means "lint the worker default version". A returned list is
    ordered newest-first when discovered from releases.
    """
    if explicit:
        return list(explicit)
    if not all_versions:
        return [None]
    catalogs = discover_catalogs(con, location)
    versions: list[str | None] = []
    for c in catalogs:
        for rel in c.releases:
            if rel.version not in versions:
                versions.append(rel.version)
    return versions or [None]
