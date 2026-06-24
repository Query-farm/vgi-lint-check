"""End-to-end orchestration: connect, discover, lint each data version, score.

This is the importable entry point other tools call (``lint_worker``). It owns
the connection lifecycle and returns a fully populated :class:`Report`.
"""

from __future__ import annotations

from typing import Any

from . import baseline as _baseline
from . import comparison as _comparison
from . import scoring
from .config import Config
from .connection import attached, connect_loaded, derive_alias, validate_alias
from .diff import diff_snapshots
from .linkcheck import make_link_resolver
from .loader import build_catalog
from .model import Release
from .result import Report, VersionResult
from .rules import run, select_rules
from .rules.base import RuleContext
from .snapshot import take_snapshot
from .versions import CatalogDiscovery, discover_catalogs, resolve_versions


def lint_worker(
    location: str,
    *,
    alias: str | None = None,
    catalog_name: str | None = None,
    config: Config | None = None,
    install: bool = True,
    spatial: bool = False,
    data_versions: list[str] | None = None,
    all_versions: bool = False,
    update_baseline: bool = False,
) -> Report:
    """Connect to a worker, lint each data version, and return a :class:`Report`."""
    config = config or Config()
    con, vgi_version = connect_loaded(install=install, spatial=spatial)
    try:
        discovery = _discover(con, location, catalog_name)
        name = discovery.catalog
        local_alias = validate_alias(alias) if alias else derive_alias(name)
        versions = resolve_versions(
            con, location, explicit=data_versions, all_versions=all_versions
        )
        results = [
            _lint_one_version(
                con,
                location,
                discovery,
                local_alias,
                dv,
                vgi_version,
                config,
                update_baseline,
            )
            for dv in versions
        ]
    finally:
        con.close()

    comp = _comparison.build(results) if len(results) > 1 else None
    return Report(
        location=location,
        alias=local_alias,
        vgi_version=vgi_version,
        results=results,
        fail_on=config.fail_on,
        has_baseline=bool(config.baseline),
        comparison=comp,
    )


def _discover(con: Any, location: str, catalog_name: str | None) -> CatalogDiscovery:
    """Discover the target catalog's listing metadata via ``vgi_catalogs``.

    When ``catalog_name`` is given, match it; otherwise take the first catalog.
    Falls back to a minimal record if discovery returns nothing.
    """
    catalogs = discover_catalogs(con, location)
    if catalog_name is not None:
        for c in catalogs:
            if c.catalog == catalog_name:
                return c
        return CatalogDiscovery(catalog_name, None, None, None, [])
    if not catalogs:
        raise RuntimeError(f"worker at {location!r} advertised no catalogs via vgi_catalogs()")
    return catalogs[0]


def _lint_one_version(
    con: Any,
    location: str,
    discovery: CatalogDiscovery,
    alias: str,
    data_version: str | None,
    vgi_version: str | None,
    config: Config,
    update_baseline: bool,
) -> VersionResult:
    releases = [
        Release(r.version, r.released_at, r.summary, r.notes_url) for r in discovery.releases
    ]
    before = take_snapshot(con)
    with attached(con, location, discovery.catalog, alias, data_version=data_version):
        after = take_snapshot(con)
        diff = diff_snapshots(before, after, alias)
        catalog = build_catalog(
            after,
            alias,
            location,
            vgi_version=vgi_version,
            data_version=data_version,
            catalog_name=discovery.catalog,
            source_url=discovery.source_url,
            implementation_version=discovery.implementation_version,
            releases=releases,
            setting_rows=diff.setting_rows,
            pragma_rows=diff.pragma_rows,
        )
        rules = select_rules(config)
        needs_con = any(getattr(r, "requires_connection", False) for r in rules)
        needs_net = any(getattr(r, "requires_network", False) for r in rules)
        resolver = make_link_resolver(config.link_timeout) if needs_net else None
        ctx = RuleContext(
            catalog,
            config,
            connection=con if needs_con else None,
            link_resolver=resolver,
        )
        findings = run(rules, ctx)

    if config.baseline:
        if update_baseline:
            _baseline.write(config.baseline, catalog.data_version, findings)
        findings = _baseline.classify(findings, config.baseline, catalog.data_version)

    quality = scoring.compute(catalog, findings)
    return VersionResult(
        catalog=catalog, findings=findings, quality=quality, diff_summary=diff.summary
    )
