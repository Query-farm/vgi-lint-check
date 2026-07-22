"""End-to-end orchestration: connect, discover, lint each data version, score.

This is the importable entry point other tools call (``lint_worker``). It owns
the connection lifecycle and returns a fully populated :class:`Report`.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import baseline as _baseline
from . import comparison as _comparison
from . import levels, scoring
from .config import Config
from .connection import (
    apply_setup_sql,
    attached,
    close_quietly,
    connect_loaded,
    derive_alias,
    is_subprocess_location,
    read_default_schema,
    validate_alias,
)
from .diff import diff_snapshots
from .linkcheck import make_image_probe, make_link_resolver
from .loader import build_catalog
from .model import Release
from .result import Report, VersionResult
from .rules import run, select_rules
from .rules.base import RuleContext
from .rules.engine import audit_waivers
from .snapshot import fetch_copy_handlers, fetch_function_arguments, take_snapshot
from .trace import Tracer
from .versions import CatalogDiscovery, discover_catalogs, resolve_versions


@contextlib.contextmanager
def _span(tracer: Tracer | None, kind: str, name: str) -> Iterator[None]:
    """Time ``name`` under ``tracer`` (a no-op when tracing is off)."""
    if tracer is None:
        yield
    else:
        with tracer.span(kind, name):
            yield


@dataclass
class _RelaunchMeter:
    """Accumulate subprocess-worker (re)launch cost across a lint run.

    A bare-command ``LOCATION`` is spawned fresh by the vgi extension on a cold
    pool miss (per data version, per process), so a multi-version or fleet run can
    pay a cold start many times over. ``spawn()`` times each probe/ATTACH so a run
    that spends too long launching workers can warn and point at a persistent
    transport.
    """

    count: int = 0
    seconds: float = 0.0

    @contextlib.contextmanager
    def spawn(self) -> Iterator[None]:
        """Time one worker probe/ATTACH, recording its count and wall-clock seconds."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.seconds += time.perf_counter() - t0
            self.count += 1


def lint_worker(
    location: str,
    *,
    alias: str | None = None,
    catalog_name: str | None = None,
    config: Config | None = None,
    install: bool = True,
    spatial: bool = True,
    data_versions: list[str] | None = None,
    all_versions: bool = False,
    update_baseline: bool = False,
) -> Report:
    """Connect to a worker, lint each data version, and return a :class:`Report`."""
    config = config or Config()
    tracer = Tracer(Path(config.trace)) if config.trace else None
    meter = _RelaunchMeter()
    with _span(tracer, "phase", "connect+load (INSTALL/LOAD vgi)"):
        con, vgi_version = connect_loaded(
            install=install, spatial=spatial, worker_idle_timeout=config.worker_idle_timeout
        )
    try:
        apply_setup_sql(con, config.setup_sql)
        with _span(tracer, "phase", "discover catalogs"), meter.spawn():
            catalogs = discover_catalogs(con, location)
        discovery = _choose(catalogs, location, catalog_name)
        advertised = [c.catalog for c in catalogs] or [discovery.catalog]
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
                advertised,
                local_alias,
                dv,
                vgi_version,
                config,
                update_baseline,
                tracer,
                meter,
            )
            for dv in versions
        ]
    finally:
        close_quietly(con)
        if tracer is not None:
            tracer.dump()
    _maybe_warn_relaunch(location, meter, config)

    comp = _comparison.build(results) if len(results) > 1 else None
    return Report(
        location=location,
        alias=local_alias,
        vgi_version=vgi_version,
        results=results,
        fail_on=config.fail_on,
        has_baseline=bool(config.baseline),
        comparison=comp,
        audited_waivers=config.audit_waivers,
    )


def with_attached_catalog(
    location: str,
    runner: Any,
    *,
    alias: str | None = None,
    catalog_name: str | None = None,
    install: bool = True,
    spatial: bool = True,
    data_version: str | None = None,
    attach_options: dict[str, str] | None = None,
    setup_sql: list[str] | tuple[str, ...] | None = None,
) -> Any:
    """Run ``runner(catalog, con)`` with the live attached connection still open.

    Used by ``vgi-lint simulate``, which must execute the analyst's SQL against the
    worker after the catalog is built (``load_catalog`` closes the connection).
    Returns the runner's result.
    """

    def _run(catalog: Any, con: Any) -> Any:
        return runner(catalog, con)

    return _load_catalog(
        location,
        alias=alias,
        catalog_name=catalog_name,
        install=install,
        spatial=spatial,
        data_version=data_version,
        attach_options=attach_options,
        setup_sql=setup_sql,
        _while_open=_run,
    )


def with_attached_catalogs(
    specs: list[tuple[str, str | None, str | None]],
    runner: Any,
    *,
    install: bool = True,
    spatial: bool = True,
) -> Any:
    """Attach several workers on one connection, then run ``runner(catalogs, con)``.

    ``specs`` is a list of ``(location, data_version, alias)``. Every worker is
    attached simultaneously on a single connection (via an ``ExitStack`` of
    ``attached()`` contexts) so a composition tutorial can query across them, then
    ``runner`` is called with ``{alias: Catalog}`` and the live connection. The
    connection is closed on exit. Returns the runner's result.
    """
    con, vgi_version = connect_loaded(install=install, spatial=spatial)
    try:
        catalogs: dict[str, Any] = {}
        with contextlib.ExitStack() as stack:
            for location, data_version, alias in specs:
                discovered = discover_catalogs(con, location)
                discovery = _choose(discovered, location, None)
                advertised = [c.catalog for c in discovered] or [discovery.catalog]
                local_alias = validate_alias(alias) if alias else derive_alias(discovery.catalog)
                releases = [
                    Release(r.version, r.released_at, r.summary, r.notes_url)
                    for r in discovery.releases
                ]
                before = take_snapshot(con)
                stack.enter_context(
                    attached(
                        con, location, discovery.catalog, local_alias, data_version=data_version
                    )
                )
                after = take_snapshot(con)
                diff = diff_snapshots(before, after, local_alias)
                catalogs[local_alias] = build_catalog(
                    after,
                    local_alias,
                    location,
                    vgi_version=vgi_version,
                    data_version=data_version,
                    catalog_name=discovery.catalog,
                    source_url=discovery.source_url,
                    implementation_version=discovery.implementation_version,
                    data_version_spec=discovery.data_version_spec,
                    default_schema=read_default_schema(con, local_alias),
                    releases=releases,
                    setting_rows=diff.setting_rows,
                    pragma_rows=diff.pragma_rows,
                    attach_options=discovery.attach_options,
                    advertised_catalogs=advertised,
                    argument_rows=fetch_function_arguments(con, local_alias),
                    copy_handler_rows=fetch_copy_handlers(con, local_alias),
                )
            return runner(catalogs, con)
    finally:
        close_quietly(con)


def load_catalog(
    location: str,
    *,
    alias: str | None = None,
    catalog_name: str | None = None,
    install: bool = True,
    spatial: bool = True,
    data_version: str | None = None,
    attach_options: dict[str, str] | None = None,
    setup_sql: list[str] | tuple[str, ...] | None = None,
) -> Any:
    """Connect, attach, and return the built :class:`Catalog` (no rules run).

    Used by ``vgi-lint review`` to get the metadata without linting it.
    """
    return _load_catalog(
        location,
        alias=alias,
        catalog_name=catalog_name,
        install=install,
        spatial=spatial,
        data_version=data_version,
        attach_options=attach_options,
        setup_sql=setup_sql,
        _while_open=None,
    )


def _load_catalog(
    location: str,
    *,
    alias: str | None = None,
    catalog_name: str | None = None,
    install: bool = True,
    spatial: bool = True,
    data_version: str | None = None,
    attach_options: dict[str, str] | None = None,
    setup_sql: list[str] | tuple[str, ...] | None = None,
    _while_open: Any = None,
) -> Any:
    """Build the catalog (shared by load_catalog/with_attached_catalog).

    If ``_while_open`` is given, call it with the live connection and return its
    result; otherwise return the built catalog.
    """
    con, vgi_version = connect_loaded(install=install, spatial=spatial)
    try:
        apply_setup_sql(con, setup_sql)
        catalogs = discover_catalogs(con, location)
        discovery = _choose(catalogs, location, catalog_name)
        advertised = [c.catalog for c in catalogs] or [discovery.catalog]
        local_alias = validate_alias(alias) if alias else derive_alias(discovery.catalog)
        releases = [
            Release(r.version, r.released_at, r.summary, r.notes_url) for r in discovery.releases
        ]
        before = take_snapshot(con)
        with attached(
            con,
            location,
            discovery.catalog,
            local_alias,
            data_version=data_version,
            attach_options=attach_options,
        ):
            after = take_snapshot(con)
            diff = diff_snapshots(before, after, local_alias)
            catalog = build_catalog(
                after,
                local_alias,
                location,
                vgi_version=vgi_version,
                data_version=data_version,
                catalog_name=discovery.catalog,
                source_url=discovery.source_url,
                implementation_version=discovery.implementation_version,
                data_version_spec=discovery.data_version_spec,
                default_schema=read_default_schema(con, local_alias),
                releases=releases,
                setting_rows=diff.setting_rows,
                pragma_rows=diff.pragma_rows,
                attach_options=discovery.attach_options,
                advertised_catalogs=advertised,
                argument_rows=fetch_function_arguments(con, local_alias),
                copy_handler_rows=fetch_copy_handlers(con, local_alias),
            )
            # When a runner is given (simulate), execute it while the connection
            # is still attached so it can run SQL against the worker.
            return _while_open(catalog, con) if _while_open is not None else catalog
    finally:
        close_quietly(con)


def _choose(
    catalogs: list[CatalogDiscovery], location: str, catalog_name: str | None
) -> CatalogDiscovery:
    """Pick which advertised catalog to lint.

    When ``catalog_name`` is given, match it; otherwise take the first catalog.
    Falls back to a minimal record if discovery returns nothing.
    """
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
    advertised: list[str],
    alias: str,
    data_version: str | None,
    vgi_version: str | None,
    config: Config,
    update_baseline: bool,
    tracer: Tracer | None = None,
    meter: _RelaunchMeter | None = None,
) -> VersionResult:
    releases = [
        Release(r.version, r.released_at, r.summary, r.notes_url) for r in discovery.releases
    ]
    with _span(tracer, "phase", "snapshot (pre-attach)"):
        before = take_snapshot(con)
    with contextlib.ExitStack() as stack:
        _m = meter.spawn() if meter else contextlib.nullcontext()
        with _span(tracer, "phase", "ATTACH worker"), _m:
            stack.enter_context(
                attached(
                    con,
                    location,
                    discovery.catalog,
                    alias,
                    data_version=data_version,
                    attach_options=config.attach_options,
                )
            )
        with _span(tracer, "phase", "snapshot (post-attach)"):
            after = take_snapshot(con)
        diff = diff_snapshots(before, after, alias)
        default_schema = read_default_schema(con, alias)
        with _span(tracer, "phase", "build catalog (+ function args)"):
            catalog = build_catalog(
                after,
                alias,
                location,
                vgi_version=vgi_version,
                data_version=data_version,
                catalog_name=discovery.catalog,
                source_url=discovery.source_url,
                implementation_version=discovery.implementation_version,
                data_version_spec=discovery.data_version_spec,
                default_schema=default_schema,
                releases=releases,
                setting_rows=diff.setting_rows,
                pragma_rows=diff.pragma_rows,
                attach_options=discovery.attach_options,
                advertised_catalogs=advertised,
                argument_rows=fetch_function_arguments(con, alias),
                copy_handler_rows=fetch_copy_handlers(con, alias),
            )
        rules = select_rules(config)
        needs_con = any(getattr(r, "requires_connection", False) for r in rules)
        needs_net = any(getattr(r, "requires_network", False) for r in rules)
        resolver = make_link_resolver(config.link_timeout) if needs_net else None
        image_probe = make_image_probe(config.link_timeout) if needs_net else None
        review_report, sim_report = _run_ai_passes(catalog, con, config, rules, tracer)
        ctx = RuleContext(
            catalog,
            config,
            connection=con if needs_con else None,
            link_resolver=resolver,
            image_probe=image_probe,
            review_report=review_report,
            sim_report=sim_report,
            tracer=tracer,
        )
        with _span(tracer, "phase", "run rules"):
            findings = run(rules, ctx)
        waiver_audit: list[Any] = []
        if config.audit_waivers and config.waivers:
            with _span(tracer, "phase", "audit waivers"):
                waiver_audit = audit_waivers(rules, ctx)

    if config.baseline:
        if update_baseline:
            _baseline.write(config.baseline, catalog.data_version, findings)
        findings = _baseline.classify(findings, config.baseline, catalog.data_version)

    agent_score = sim_report.score if (sim_report and sim_report.verdicts) else None
    with _span(tracer, "phase", "score"):
        quality = scoring.compute(
            catalog, findings, agent_score=agent_score, doc_quality=_doc_quality(review_report)
        )
    level = levels.compute(
        findings,
        executed=config.execute,
        doc_reviewed=config.doc_review,
        agent_checked=config.agent_check,
    )
    return VersionResult(
        catalog=catalog,
        findings=findings,
        quality=quality,
        diff_summary=diff.summary,
        level=level,
        waiver_audit=waiver_audit,
    )


def _doc_quality(report: Any) -> int | None:
    """Normalize a ReviewReport's 1-5 mean into a 0-100 score (None if not run)."""
    if report is None or not report.reviews:
        return None
    return int(round((report.score - 1) / 4 * 100))


def _run_ai_passes(
    catalog: Any, con: Any, config: Config, rules: Any, tracer: Tracer | None = None
) -> tuple[Any, Any]:
    """Run the opt-in LLM passes (doc-review / agent-simulation) when enabled.

    Gated by the selected rules: only runs a pass when a ``requires_review`` /
    ``requires_agent`` rule survived selection (i.e. --doc-review / --agent-check
    is on). Uses the configured backend (the ``claude`` subscription CLI by default).
    """
    needs_review = any(getattr(r, "requires_review", False) for r in rules)
    needs_agent = any(getattr(r, "requires_agent", False) for r in rules)
    if not (needs_review or needs_agent):
        return None, None
    from pathlib import Path

    from .review import ReviewCache, make_backend, review_catalog

    backend = make_backend(config.ai_backend, config.ai_model)

    def _cache(name: str) -> Any:
        # Same files as the standalone `review`/`simulate` commands, so their runs
        # warm the lint's cache and vice versa.
        return ReviewCache(Path(name)).load() if config.ai_cache else None

    # An LLM pass must never crash the lint: a flaky/unparseable model response
    # degrades that dimension to "not run" (static-only score) with a warning,
    # not a hard failure of the whole report.
    review_report = None
    if needs_review:
        try:
            with _span(tracer, "phase", "doc-review LLM (VGI180)"):
                review_report = review_catalog(
                    catalog,
                    backend,
                    backend_name=config.ai_backend,
                    cache=_cache(".vgi-review-cache.json"),
                    concurrency=config.ai_concurrency,
                )
        except Exception as e:  # noqa: BLE001 - any backend/parse failure degrades gracefully
            _warn_ai_pass("doc-review", e)
    sim_report = None
    if needs_agent:
        from .simulate import simulate_tasks

        try:
            with _span(tracer, "phase", "agent-check LLM (VGI920)"):
                sim_report = simulate_tasks(
                    catalog,
                    con,
                    backend,
                    backend_name=config.ai_backend,
                    cache=_cache(".vgi-sim-cache.json"),
                )
        except Exception as e:  # noqa: BLE001
            _warn_ai_pass("agent-check", e)
    return review_report, sim_report


def _warn_ai_pass(name: str, err: Exception) -> None:
    """Emit a stderr notice that an LLM pass failed (the lint continues statically)."""
    print(
        f"warning: {name} pass failed ({type(err).__name__}: {err}); "
        "scoring without it. Re-run to retry the LLM pass.",
        file=sys.stderr,
    )


def _maybe_warn_relaunch(location: str, meter: _RelaunchMeter, config: Config) -> None:
    """Warn once when a subprocess worker was (re)launched slowly/repeatedly this run.

    Only fires for the plain-subprocess transport (a persistent endpoint has nothing
    to relaunch) and only when the cumulative launch time crosses the configured
    threshold, so a fast run stays silent. Points at persistent transports for the
    cross-process/fleet case the pool keepalive cannot fix.
    """
    if (
        config.relaunch_warn_seconds <= 0
        or not is_subprocess_location(location)
        or meter.count < 2
        or meter.seconds < config.relaunch_warn_seconds
    ):
        return
    print(
        f"warning: spent ~{meter.seconds:.1f}s (re)launching the subprocess worker "
        f"{meter.count}× this run. A bare-command LOCATION cold-starts a fresh worker "
        f"per data version and per vgi-lint process — the extension's warm pool does "
        f"not survive across processes. For fleet/CI or --all-data-versions runs, run "
        f"the worker persistently and attach to it: LOCATION 'launch:<cmd>' (if the "
        f"worker speaks the launcher protocol), 'unix:///path/to.sock', or "
        f"'http://host:port/'.",
        file=sys.stderr,
    )
