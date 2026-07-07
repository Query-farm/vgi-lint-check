"""Command-line interface.

Root command is ``lint`` (so ``vgi-lint <location>`` just works); ``rules``,
``explain``, ``versions``, and ``init`` are subcommands.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import click

from . import reporting
from .config import Config, load_config
from .connection import WorkerConnectionError, connect_loaded
from .core import lint_worker
from .exit_codes import EXIT_CONNECTION, EXIT_FINDINGS, EXIT_TOOL_ERROR
from .findings import Severity
from .rules.registry import REGISTRY


class DefaultGroup(click.Group):
    """A group that routes an unknown first token to a default command.

    This lets ``vgi-lint <location>`` and ``vgi-lint --format json <location>``
    work without naming the ``lint`` subcommand explicitly.
    """

    def __init__(self, *args: Any, default: str | None = None, **kwargs: Any) -> None:
        """Store the default command name and forward to ``click.Group``."""
        super().__init__(*args, **kwargs)
        self.default_cmd = default

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Inject the default command when the first token is a bare argument."""
        if args and self.default_cmd:
            first = args[0]
            if first.startswith("-"):
                pass  # group-level option (e.g. --help, --version)
            elif first in self.commands:
                pass  # explicit subcommand
            else:
                args = [self.default_cmd, *args]  # a LOCATION -> route to lint
        return super().parse_args(ctx, args)


@click.group(cls=DefaultGroup, default="lint", invoke_without_command=False)
@click.version_option(package_name="vgi-lint-check", prog_name="vgi-lint")
def app() -> None:
    """Lint the metadata quality of a VGI worker."""


# --------------------------------------------------------------------------
# lint
# --------------------------------------------------------------------------
@app.command()
@click.argument("location", required=False)
@click.option("--as", "alias", default=None, help="Local catalog alias handle.")
@click.option("--catalog", "catalog_name", default=None, help="Worker catalog name.")
@click.option(
    "--spatial/--no-spatial",
    default=True,
    help="Load the spatial extension (best-effort; on by default). --no-spatial to skip.",
)
@click.option("--install/--no-install", default=True, help="FORCE INSTALL vgi from community.")
@click.option(
    "--data-version",
    "data_versions",
    multiple=True,
    help="Lint a specific data version (repeatable).",
)
@click.option(
    "--all-data-versions", is_flag=True, help="Discover and lint every published version."
)
@click.option(
    "--execute/--no-execute",
    default=None,
    help="Run execution rules (VGI9xx) against the worker. On by default; "
    "--no-execute skips them for a static-only lint.",
)
@click.option("--execute-mode", type=click.Choice(["explain", "limit", "run"]), default=None)
@click.option("--execute-limit", type=int, default=None)
@click.option(
    "--execute-concurrency",
    type=int,
    default=None,
    help="Run example queries across N cursors in parallel (default: CPU count; "
    "lower it for a rate-limited worker).",
)
@click.option(
    "--check-links/--no-check-links",
    default=None,
    help="Resolve URLs/images in descriptions over HTTP (VGI171). On by default.",
)
@click.option(
    "--doc-review",
    is_flag=True,
    help="LLM doc-quality review (VGI180); folds into the score. Uses your claude subscription.",
)
@click.option(
    "--agent-check",
    is_flag=True,
    help="Run the agent suite (simulate) + gate on pass-rate (VGI920); folds into the score.",
)
@click.option("--ai", is_flag=True, help="Shorthand for --doc-review --agent-check.")
@click.option(
    "--ai-backend", type=click.Choice(["claude", "api"]), default=None, help="LLM backend."
)
@click.option("--ai-model", default=None, help="LLM model override for the AI passes.")
@click.option(
    "--ai-concurrency",
    type=int,
    default=None,
    help="Parallel LLM batches for --doc-review (default 4; raise to speed up a large catalog).",
)
@click.option(
    "--no-ai-cache",
    is_flag=True,
    help="Disable the LLM verdict cache (by default re-runs reuse unchanged verdicts).",
)
@click.option(
    "--select",
    default=None,
    help="Comma list/globs of rule codes to enable (replaces the default set).",
)
@click.option(
    "--extend-select", default=None, help="Comma list/globs of rule codes to also enable."
)
@click.option("--ignore", default=None, help="Comma list/globs of rule codes to disable.")
@click.option(
    "--extend-ignore", default=None, help="Comma list/globs of rule codes to also disable."
)
@click.option("--category", "categories", default=None, help="Comma list of categories.")
@click.option("--severity", "severities", multiple=True, help="CODE=LEVEL override (repeatable).")
@click.option("--baseline", default=None, help="Baseline file prefix (per-version).")
@click.option("--update-baseline", is_flag=True, help="Write/refresh the baseline file(s).")
@click.option("--fail-on", type=click.Choice(["info", "warning", "error", "never"]), default=None)
@click.option("--format", "fmt", type=click.Choice(list(reporting.FORMATS)), default="terminal")
@click.option(
    "--group-by",
    type=click.Choice(["rule", "object"]),
    default="rule",
    help="Group findings by rule (collapse a rule firing on many objects) or by object.",
)
@click.option(
    "--max-per-rule",
    type=int,
    default=10,
    help="Objects listed per rule before collapsing the tail into '+N more' (0 = all).",
)
@click.option(
    "--output", type=click.Path(dir_okay=False), default=None, help="Write report to FILE."
)
@click.option("--color", type=click.Choice(["auto", "always", "never"]), default="auto")
@click.option(
    "--attach-option",
    "attach_options",
    multiple=True,
    metavar="KEY=VALUE",
    help="Extra ATTACH option (repeatable) for workers that require options/credentials to "
    "attach, e.g. --attach-option provider=imap --attach-option secret=lint. A worker that "
    "resolves credentials lazily attaches fine with a placeholder secret for static linting.",
)
@click.option(
    "--setup-sql",
    "setup_sql",
    multiple=True,
    metavar="SQL",
    help="SQL to run on the connection before ATTACH (repeatable), e.g. a CREATE SECRET needed "
    "by --execute rules.",
)
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option(
    "--trace",
    "trace_path",
    is_flag=False,
    flag_value="vgi-lint-trace.log",
    default=None,
    metavar="FILE",
    help="Write a per-phase / per-rule timing log to FILE (default vgi-lint-trace.log) to "
    "diagnose a slow lint.",
)
@click.option("--traceback", is_flag=True, help="Show a full traceback on an unexpected error.")
@click.option("--quiet", "-q", is_flag=True)
@click.pass_context
def lint(
    ctx: click.Context,
    location: str | None,
    alias: str | None,
    catalog_name: str | None,
    spatial: bool,
    install: bool,
    data_versions: tuple[str, ...],
    all_data_versions: bool,
    execute: bool | None,
    execute_mode: str | None,
    execute_limit: int | None,
    execute_concurrency: int | None,
    check_links: bool | None,
    doc_review: bool,
    agent_check: bool,
    ai: bool,
    ai_backend: str | None,
    ai_model: str | None,
    ai_concurrency: int | None,
    no_ai_cache: bool,
    select: str | None,
    extend_select: str | None,
    ignore: str | None,
    extend_ignore: str | None,
    categories: str | None,
    severities: tuple[str, ...],
    baseline: str | None,
    update_baseline: bool,
    fail_on: str | None,
    fmt: str,
    group_by: str,
    max_per_rule: int,
    output: str | None,
    color: str,
    attach_options: tuple[str, ...],
    setup_sql: tuple[str, ...],
    config_path: str | None,
    trace_path: str | None,
    traceback: bool,
    quiet: bool,
) -> None:
    """Lint a worker at LOCATION (URL or subprocess command)."""
    cfg = load_config(config_path)
    if trace_path is not None:
        cfg.trace = trace_path
    _apply_cli_overrides(
        cfg,
        select=select,
        extend_select=extend_select,
        ignore=ignore,
        extend_ignore=extend_ignore,
        categories=categories,
        severities=severities,
        execute=execute,
        execute_mode=execute_mode,
        execute_limit=execute_limit,
        execute_concurrency=execute_concurrency,
        check_links=check_links,
        doc_review=doc_review or ai,
        agent_check=agent_check or ai,
        ai_backend=ai_backend,
        ai_model=ai_model,
        ai_concurrency=ai_concurrency,
        no_ai_cache=no_ai_cache,
        fail_on=fail_on,
        baseline=baseline,
        attach_options=attach_options,
        setup_sql=setup_sql,
    )
    _warn_unknown_selectors(cfg)
    location = location or cfg.location
    if not location:
        raise click.UsageError(
            "no worker LOCATION given and none configured in [tool.vgi-lint-check] location"
        )

    try:
        report = lint_worker(
            location,
            alias=alias,
            catalog_name=catalog_name,
            config=cfg,
            install=install,
            spatial=spatial,
            data_versions=list(data_versions) or None,
            all_versions=all_data_versions,
            update_baseline=update_baseline,
        )
    except WorkerConnectionError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        ctx.exit(EXIT_CONNECTION)
    except click.ClickException:
        raise
    except Exception as e:  # noqa: BLE001 - top-level guard
        if traceback:
            raise
        click.secho(f"error: {type(e).__name__}: {e}", fg="red", err=True)
        click.secho("(run with --traceback for a full stack trace)", fg="red", err=True)
        ctx.exit(EXIT_TOOL_ERROR)

    if update_baseline and not quiet:
        click.secho("baseline refreshed — gating skipped for this run", fg="yellow", err=True)

    use_color = _resolve_color(color)
    text = reporting.render(
        report, fmt, color=use_color, group_by=group_by, max_per_rule=max_per_rule
    )
    if output:
        try:
            payload = text if text.endswith("\n") else text + "\n"
            Path(output).write_text(payload, encoding="utf-8")
        except OSError as e:
            raise click.ClickException(f"could not write {output}: {e}") from e
        if not quiet:
            click.echo(f"wrote {fmt} report to {output}")
    elif not quiet:
        click.echo(text)
    ctx.exit(0 if report.passed() else 2)


# --------------------------------------------------------------------------
# rules / explain
# --------------------------------------------------------------------------
@app.command(name="rules")
@click.option("--category", "category", default=None)
@click.option("--format", "fmt", type=click.Choice(["terminal", "json"]), default="terminal")
def rules_cmd(category: str | None, fmt: str) -> None:
    """List the rule catalog."""
    from .rules.registry import all_rule_classes

    items = [c for c in all_rule_classes() if category is None or str(c.category) == category]
    if fmt == "json":
        import json

        click.echo(
            json.dumps(
                [
                    {
                        "code": c.code,
                        "name": c.name,
                        "category": str(c.category),
                        "default_severity": c.default_severity.label,
                        "summary": c.summary,
                        "requires_connection": c.requires_connection,
                    }
                    for c in items
                ],
                indent=2,
            )
        )
        return
    for c in items:
        click.echo(f"{c.code}  {c.default_severity.label:<7} {str(c.category):<12} {c.summary}")


@app.command()
@click.argument("code")
def explain(code: str) -> None:
    """Explain one rule: CODE."""
    from .rules.registry import REGISTRY

    cls = REGISTRY.get(code.upper())
    if cls is None:
        raise click.UsageError(f"unknown rule code {code!r}")
    click.echo(f"{cls.code}  ({cls.category}, default {cls.default_severity.label})")
    click.echo(f"  {cls.name}")
    click.echo()
    click.echo(f"  {cls.summary}")
    targets = ", ".join(str(t) for t in cls.targets) or "—"
    click.echo(f"\n  Applies to: {targets}")
    if cls.default_severity.label == "off":
        click.echo(
            f"  Opt-in: off by default; enable with --severity {cls.code}=warning or config."
        )
    if cls.requires_connection:
        click.echo("  Requires --execute (runs against the worker).")


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------
@app.command(name="versions")
@click.argument("location")
@click.option("--install/--no-install", default=True)
def versions_cmd(location: str, install: bool) -> None:
    """List a worker's published data versions."""
    from .versions import discover_catalogs

    try:
        con, _ = connect_loaded(install=install)
    except WorkerConnectionError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        raise SystemExit(EXIT_CONNECTION) from e
    try:
        catalogs = discover_catalogs(con, location)
    finally:
        con.close()
    for c in catalogs:
        click.echo(
            f"catalog: {c.catalog}  (impl {c.implementation_version or '—'}, "
            f"spec {c.data_version_spec or '—'})"
        )
        if not c.releases:
            click.echo("  (no published data versions — default only)")
        for r in c.releases:
            when = f" {r.released_at}" if r.released_at else ""
            summary = f" — {r.summary}" if r.summary else ""
            click.echo(f"  {r.version}{when}{summary}")


# --------------------------------------------------------------------------
# review (LLM-as-judge, opt-in)
# --------------------------------------------------------------------------
@app.command(name="review")
@click.argument("location", required=False)
@click.option("--as", "alias", default=None, help="Local catalog alias handle.")
@click.option("--catalog", "catalog_name", default=None, help="Worker catalog name.")
@click.option("--spatial/--no-spatial", default=True)
@click.option("--install/--no-install", default=True)
@click.option("--data-version", default=None, help="Review a specific data version.")
@click.option(
    "--review-backend",
    type=click.Choice(["claude", "api"]),
    default="claude",
    help="claude = local Claude Code CLI (your subscription); api = Anthropic API (per-token).",
)
@click.option("--review-model", default=None, help="Model override passed to the backend.")
@click.option(
    "--review-cache",
    type=click.Path(dir_okay=False),
    default=".vgi-review-cache.json",
    help="Verdict cache file (content-hashed); reused so unchanged docs aren't re-judged.",
)
@click.option("--no-review-cache", is_flag=True, help="Disable the verdict cache.")
@click.option("--review-batch", type=int, default=8, help="Objects per model call.")
@click.option(
    "--review-concurrency", type=int, default=4, help="Parallel model calls (batches at once)."
)
@click.option("--format", "fmt", type=click.Choice(["terminal", "json"]), default="terminal")
@click.option("--output", type=click.Path(dir_okay=False), default=None)
@click.option("--traceback", is_flag=True)
@click.pass_context
def review_cmd(
    ctx: click.Context,
    location: str | None,
    alias: str | None,
    catalog_name: str | None,
    spatial: bool,
    install: bool,
    data_version: str | None,
    review_backend: str,
    review_model: str | None,
    review_cache: str,
    no_review_cache: bool,
    review_batch: int,
    review_concurrency: int,
    fmt: str,
    output: str | None,
    traceback: bool,
) -> None:
    """LLM-judge the documentation quality of a worker's objects (advisory)."""
    from . import review as rv
    from .core import load_catalog

    location = location or load_config().location
    if not location:
        raise click.UsageError("no worker LOCATION given and none configured")
    try:
        catalog = load_catalog(
            location,
            alias=alias,
            catalog_name=catalog_name,
            install=install,
            spatial=spatial,
            data_version=data_version,
        )
        backend = rv.make_backend(review_backend, review_model)
        cache = None if no_review_cache else rv.ReviewCache(Path(review_cache)).load()
        report = rv.review_catalog(
            catalog,
            backend,
            backend_name=review_backend,
            cache=cache,
            batch_size=review_batch,
            concurrency=review_concurrency,
        )
    except WorkerConnectionError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        ctx.exit(EXIT_CONNECTION)
    except Exception as e:  # noqa: BLE001 - top-level guard
        if traceback:
            raise
        click.secho(f"error: {type(e).__name__}: {e}", fg="red", err=True)
        ctx.exit(EXIT_TOOL_ERROR)

    text = rv.render_json(report) if fmt == "json" else rv.render_terminal(report)
    if output:
        Path(output).write_text(text if text.endswith("\n") else text + "\n")
    else:
        click.echo(text)


# --------------------------------------------------------------------------
# simulate (agent-suitability testing, opt-in)
# --------------------------------------------------------------------------
@app.command(name="simulate")
@click.argument("location", required=False)
@click.option("--as", "alias", default=None, help="Local catalog alias handle.")
@click.option("--catalog", "catalog_name", default=None, help="Worker catalog name.")
@click.option("--spatial/--no-spatial", default=True)
@click.option("--install/--no-install", default=True)
@click.option("--data-version", default=None, help="Simulate against a specific data version.")
@click.option(
    "--backend",
    "sim_backend",
    type=click.Choice(["claude", "api"]),
    default="claude",
    help="claude = local Claude Code CLI (your subscription); api = Anthropic API (per-token).",
)
@click.option("--model", "sim_model", default=None, help="Model override passed to the backend.")
@click.option(
    "--cache",
    "cache_path",
    type=click.Path(dir_okay=False),
    default=".vgi-sim-cache.json",
    help="Verdict cache (content-hashed); reused unless the overview/task/version changed.",
)
@click.option("--no-cache", is_flag=True, help="Always re-run (no verdict cache).")
@click.option(
    "--max-steps", type=int, default=12, help="Max analyst turns per task (discovery + queries)."
)
@click.option(
    "--max-queries", type=int, default=10, help="Max queries the analyst may run per task."
)
@click.option(
    "--attempts", type=int, default=1, help="Retry a task up to N times; pass if any passes."
)
@click.option("--query-timeout", type=float, default=30.0, help="Per-query wall-clock seconds.")
@click.option("--row-limit", type=int, default=50, help="Row cap on exploration queries.")
@click.option(
    "--concurrency",
    type=int,
    default=4,
    help="Tasks to judge in parallel (each on its own cursor).",
)
@click.option(
    "--session/--no-session",
    "session",
    default=True,
    help="Use a claude session (resume) so each turn sends only the delta, not the whole "
    "transcript. --no-session re-sends the full transcript each turn (stateless).",
)
@click.option(
    "--min-pass-rate",
    type=float,
    default=1.0,
    help="Fail the run (exit 2) if the task pass rate is below this (0..1).",
)
@click.option("--advisory", is_flag=True, help="Never gate — always exit 0.")
@click.option(
    "--verify-references",
    "verify_references",
    is_flag=True,
    help="Authoring check: run each task's reference_sql a few times and flag any that error, "
    "are non-deterministic, or return no rows. No actor, no grading; exits 2 on any failure.",
)
@click.option(
    "--suggest",
    type=int,
    is_flag=False,
    flag_value=0,
    default=None,
    help="Authoring mode: print coverage-driven candidate tasks as vgi.agent_test_tasks JSON "
    "and exit. Bare --suggest sizes the suite to cover the worker; --suggest N caps it at N.",
)
@click.option("--format", "fmt", type=click.Choice(["terminal", "json"]), default="terminal")
@click.option("--output", type=click.Path(dir_okay=False), default=None)
@click.option("--traceback", is_flag=True)
@click.pass_context
def simulate_cmd(
    ctx: click.Context,
    location: str | None,
    alias: str | None,
    catalog_name: str | None,
    spatial: bool,
    install: bool,
    data_version: str | None,
    sim_backend: str,
    sim_model: str | None,
    cache_path: str,
    no_cache: bool,
    max_steps: int,
    max_queries: int,
    attempts: int,
    query_timeout: float,
    row_limit: int,
    concurrency: int,
    session: bool,
    min_pass_rate: float,
    advisory: bool,
    verify_references: bool,
    suggest: int | None,
    fmt: str,
    output: str | None,
    traceback: bool,
) -> None:
    """Run an LLM analyst through a worker's vgi.agent_test_tasks suite (suitability test)."""
    from . import simulate as sm
    from .core import with_attached_catalog
    from .review import ReviewCache, make_backend

    location = location or load_config().location
    if not location:
        raise click.UsageError("no worker LOCATION given and none configured")
    backend = make_backend(sim_backend, sim_model)
    limits = sm.SimLimits(
        max_steps=max_steps,
        max_queries=max_queries,
        attempts=attempts,
        timeout=query_timeout,
        row_limit=row_limit,
        concurrency=concurrency,
        sessions=session,
    )

    def runner(catalog: Any, con: Any) -> Any:
        if suggest is not None:
            return ("suggest", sm.suggest_tasks(catalog, backend, cap=max(0, suggest)))
        if verify_references:
            return ("verify", sm.verify_references(catalog, con, limits))
        cache = None if no_cache else ReviewCache(Path(cache_path)).load()
        return (
            "report",
            sm.simulate_tasks(
                catalog, con, backend, backend_name=sim_backend, limits=limits, cache=cache
            ),
        )

    try:
        mode, result = with_attached_catalog(
            location,
            runner,
            alias=alias,
            catalog_name=catalog_name,
            install=install,
            spatial=spatial,
            data_version=data_version,
        )
    except WorkerConnectionError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        ctx.exit(EXIT_CONNECTION)
    except Exception as e:  # noqa: BLE001 - top-level guard
        if traceback:
            raise
        click.secho(f"error: {type(e).__name__}: {e}", fg="red", err=True)
        ctx.exit(EXIT_TOOL_ERROR)

    if mode == "suggest":
        click.echo(result if not output else "")
        if output:
            Path(output).write_text(result + "\n")
        return

    if mode == "verify":
        click.echo(sm.render_verify(result))
        if not result.ok:
            ctx.exit(EXIT_FINDINGS)
        return

    text = sm.render_json(result) if fmt == "json" else sm.render_terminal(result)
    if output:
        Path(output).write_text(text if text.endswith("\n") else text + "\n")
    else:
        click.echo(text)
    if not advisory and result.verdicts and result.pass_rate < min_pass_rate:
        ctx.exit(EXIT_FINDINGS)


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------
@app.command()
@click.option("--location", default=None, help="Pre-fill the worker location.")
@click.option("--file", "target", type=click.Path(dir_okay=False), default="vgi-lint.toml")
def init(location: str | None, target: str) -> None:
    """Scaffold a [tool.vgi-lint-check] config file."""
    if os.path.exists(target):
        raise click.UsageError(f"{target} already exists")
    loc_line = f'location = "{location}"\n' if location else '# location = "uv run worker.py"\n'
    content = (
        "[tool.vgi-lint-check]\n"
        f"{loc_line}"
        'select = ["ALL"]\n'
        "ignore = []\n"
        'fail_on = "error"\n'
        '# baseline = "vgi-lint-baseline"\n\n'
        "[tool.vgi-lint-check.options]\n"
        "column_comment_min_ratio = 0.8\n"
        "# Require specific tag keys (opt-in; empty by default):\n"
        '# required_schema_tags = ["provider", "domain"]\n'
    )
    with open(target, "w") as fh:
        fh.write(content)
    click.echo(f"wrote {target}")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _apply_cli_overrides(
    cfg: Config,
    *,
    select: str | None,
    extend_select: str | None,
    ignore: str | None,
    extend_ignore: str | None,
    categories: str | None,
    severities: tuple[str, ...],
    execute: bool | None,
    execute_mode: str | None,
    execute_limit: int | None,
    execute_concurrency: int | None,
    check_links: bool | None,
    doc_review: bool = False,
    agent_check: bool = False,
    ai_backend: str | None = None,
    ai_model: str | None = None,
    ai_concurrency: int | None = None,
    no_ai_cache: bool = False,
    fail_on: str | None = None,
    baseline: str | None = None,
    attach_options: tuple[str, ...] = (),
    setup_sql: tuple[str, ...] = (),
) -> None:
    for item in attach_options:
        if "=" not in item:
            raise click.UsageError(f"--attach-option expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        cfg.attach_options[key.strip()] = value
    if setup_sql:
        cfg.setup_sql = cfg.setup_sql + list(setup_sql)
    if select is not None:
        cfg.select = _split(select)
    if extend_select is not None:
        cfg.extend_select = cfg.extend_select + _split(extend_select)
    if ignore is not None:
        cfg.ignore = _split(ignore)
    if extend_ignore is not None:
        cfg.extend_ignore = cfg.extend_ignore + _split(extend_ignore)
    if categories is not None:
        cfg.categories = _split(categories)
    for item in severities:
        if "=" not in item:
            raise click.UsageError(f"--severity expects CODE=LEVEL, got {item!r}")
        code, level = item.split("=", 1)
        cfg.severity_overrides[code.strip()] = Severity.parse(level)
    if execute is not None:
        cfg.execute = execute
    if execute_mode is not None:
        cfg.execute_mode = execute_mode
    if execute_limit is not None:
        cfg.execute_limit = execute_limit
    if execute_concurrency is not None:
        cfg.execute_concurrency = execute_concurrency
    if check_links is not None:
        cfg.check_links = check_links
    if doc_review:
        cfg.doc_review = True
    if agent_check:
        cfg.agent_check = True
    if ai_backend is not None:
        cfg.ai_backend = ai_backend
    if ai_model is not None:
        cfg.ai_model = ai_model
    if ai_concurrency is not None:
        cfg.ai_concurrency = ai_concurrency
    if no_ai_cache:
        cfg.ai_cache = False
    if fail_on is not None:
        cfg.fail_on = Severity.OFF if fail_on == "never" else Severity.parse(fail_on)
    if baseline is not None:
        cfg.baseline = baseline


def _warn_unknown_selectors(cfg: Config) -> None:
    unknown = cfg.unknown_selectors(REGISTRY.keys())
    if unknown:
        click.secho(
            f"warning: rule selector(s) match no known rule: {', '.join(unknown)}",
            fg="yellow",
            err=True,
        )


def _resolve_color(mode: str) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty() and not os.environ.get("CI")


# --------------------------------------------------------------------------
# tutorials — lint / build / init executable tutorials (VGI13xx)
# --------------------------------------------------------------------------
@app.group("tutorials")
def tutorials_grp() -> None:
    """Lint, build, and scaffold executable ``*.vgi.md`` tutorials."""


def _collect_tutorials(paths: tuple[str, ...]) -> tuple[list[Any], Any]:
    """Load docs from the given files/dirs; return (docs, hub-or-None)."""
    from .tutorials.hub import find_hub
    from .tutorials.loader import load_dir, load_tutorial

    docs: list[Any] = []
    hub = None
    for p in paths:
        path = Path(p)
        if path.is_dir():
            docs.extend(load_dir(path))
            hub = hub or find_hub(path)
        else:
            docs.append(load_tutorial(path))
    return docs, hub


@tutorials_grp.command("lint")
@click.argument("paths", nargs=-1, required=True)
@click.option("--format", "fmt", type=click.Choice(["terminal", "json"]), default="terminal")
@click.option("--select", multiple=True, help="Only run these rule codes/globs.")
@click.option("--ignore", multiple=True, help="Skip these rule codes/globs.")
@click.option("--config", "config_path", default=None, help="Path to a config file.")
@click.option(
    "--fail-on", type=click.Choice(["error", "warning", "info", "never"]), default="error"
)
@click.option("--judge", is_flag=True, help="LLM narrative-quality review (VGI1370).")
@click.option("--ai-backend", type=click.Choice(["claude", "api"]), default="claude")
@click.option("--ai-model", default=None)
def tutorials_lint(
    paths: tuple[str, ...],
    fmt: str,
    select: tuple[str, ...],
    ignore: tuple[str, ...],
    config_path: str | None,
    fail_on: str,
    judge: bool,
    ai_backend: str,
    ai_model: str | None,
) -> None:
    """Statically lint tutorials (offline; ``--judge`` adds an LLM review)."""
    from .rules.tutorials import (
        TutorialContext,
        render_findings,
        run_tutorial_rules,
        tutorial_rules,
    )

    cfg = load_config(config_path)
    cfg.execute = False  # static only: connection rules stay gated to `verify`
    if select:
        cfg.select = list(select)
    if ignore:
        cfg.ignore = list(ignore)
    cfg.fail_on = Severity.parse(fail_on) if fail_on != "never" else Severity.OFF

    backend = None
    if judge:
        from .review import make_backend

        cfg.doc_review = True  # ungate the requires_review rule (VGI1370)
        backend = make_backend(ai_backend, ai_model)

    docs, hub = _collect_tutorials(paths)
    ctx = TutorialContext(docs=docs, config=cfg, hub=hub, backend=backend)
    findings = run_tutorial_rules(tutorial_rules(), ctx)
    click.echo(render_findings(findings, fmt))

    gating = [f for f in findings if cfg.fail_on is not Severity.OFF and f.severity >= cfg.fail_on]
    sys.exit(EXIT_FINDINGS if gating else 0)


@tutorials_grp.command("verify")
@click.argument("paths", nargs=-1, required=True)
@click.option("--location", default=None, help="Worker LOCATION (single-worker tutorials).")
@click.option(
    "--worker-location",
    "worker_locations",
    multiple=True,
    metavar="WORKER=LOCATION",
    help="Per-worker LOCATION for composition tutorials (repeatable).",
)
@click.option("--install/--no-install", default=True, help="FORCE INSTALL vgi from community.")
@click.option("--format", "fmt", type=click.Choice(["terminal", "json"]), default="terminal")
@click.option("--select", multiple=True)
@click.option("--ignore", multiple=True)
@click.option("--config", "config_path", default=None)
@click.option(
    "--fail-on", type=click.Choice(["error", "warning", "info", "never"]), default="error"
)
def tutorials_verify(
    paths: tuple[str, ...],
    location: str | None,
    worker_locations: tuple[str, ...],
    install: bool,
    fmt: str,
    select: tuple[str, ...],
    ignore: tuple[str, ...],
    config_path: str | None,
    fail_on: str,
) -> None:
    """Execute tutorials against a live worker and check the pinned results."""
    from .core import with_attached_catalogs
    from .rules.tutorials import (
        TutorialContext,
        render_findings,
        run_tutorial_rules,
        tutorial_rules,
    )
    from .tutorials.runner import run_tutorial

    cfg = load_config(config_path)
    cfg.execute = True  # enable the connection rules (VGI1340–1343)
    if select:
        cfg.select = list(select)
    if ignore:
        cfg.ignore = list(ignore)
    cfg.fail_on = Severity.parse(fail_on) if fail_on != "never" else Severity.OFF

    docs, hub = _collect_tutorials(paths)

    # Resolve worker -> LOCATION and per-worker data-version pin.
    locmap: dict[str, str] = {}
    for wl in worker_locations:
        w, _, loc = wl.partition("=")
        locmap[w.strip()] = loc.strip()
    needed = sorted({w for d in docs if d.front_matter for w in d.front_matter.workers})
    if location and len(needed) == 1 and needed[0] not in locmap:
        locmap[needed[0]] = location
    missing = [w for w in needed if w not in locmap]
    if missing:
        click.secho(
            f"no LOCATION for worker(s): {', '.join(missing)} — pass --location "
            "(single worker) or --worker-location WORKER=LOCATION",
            fg="red",
            err=True,
        )
        sys.exit(EXIT_TOOL_ERROR)
    dvmap: dict[str, str] = {}
    for d in docs:
        for spec in d.front_matter.attach if d.front_matter else []:
            if spec.data_version and spec.worker not in dvmap:
                dvmap[spec.worker] = spec.data_version
    specs = [(locmap[w], dvmap.get(w), w) for w in needed]

    def _runner(catalogs: dict[str, Any], con: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        results = {d.slug: run_tutorial(con, d, timeout=cfg.execute_timeout) for d in docs}
        return catalogs, results

    try:
        catalogs, results = with_attached_catalogs(specs, _runner, install=install)
    except WorkerConnectionError as e:
        click.secho(str(e), fg="red", err=True)
        sys.exit(EXIT_CONNECTION)

    ctx = TutorialContext(
        docs=docs, config=cfg, hub=hub, catalogs=catalogs, connection=True, results=results
    )
    findings = run_tutorial_rules(tutorial_rules(), ctx)
    click.echo(render_findings(findings, fmt))
    gating = [f for f in findings if cfg.fail_on is not Severity.OFF and f.severity >= cfg.fail_on]
    sys.exit(EXIT_FINDINGS if gating else 0)


@tutorials_grp.command("build")
@click.argument("paths", nargs=-1, required=True)
@click.option("--out", "out_dir", default="_site", help="Output directory for HTML.")
@click.option("--base-url", default=None, help="Site base URL for canonical/breadcrumb links.")
@click.option(
    "--wasm-endpoint",
    default=None,
    help="HTTP worker endpoint; enables the live Run button on wasm-safe tutorials.",
)
@click.option("--open", "open_browser", is_flag=True, help="Open the result in the browser.")
def tutorials_build(
    paths: tuple[str, ...],
    out_dir: str,
    base_url: str | None,
    wasm_endpoint: str | None,
    open_browser: bool,
) -> None:
    """Render tutorials to self-contained HTML (a suite when a hub is present)."""
    import subprocess

    from .tutorials.hub import find_hub, nav_for
    from .tutorials.render import render_html, render_hub

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    docs, _ = _collect_tutorials(paths)
    written: list[Path] = []
    # A hub is per-directory; find the hub for the dir a doc lives in.
    hubs: dict[str, Any] = {}
    for doc in docs:
        d = str(Path(doc.path).parent)
        if d not in hubs:
            hubs[d] = find_hub(d)
        hub = hubs[d]
        nav = nav_for(hub, doc.slug) if hub else None
        target = out / f"{doc.slug}.html"
        target.write_text(
            render_html(doc, nav=nav, base_url=base_url, wasm_endpoint=wasm_endpoint),
            encoding="utf-8",
        )
        written.append(target)
    for d, hub in hubs.items():
        if hub is not None:
            hub_docs = [doc for doc in docs if str(Path(doc.path).parent) == d]
            (out / "index.html").write_text(render_hub(hub, hub_docs), encoding="utf-8")
            written.append(out / "index.html")
    for w in written:
        click.echo(f"wrote {w}")
    if open_browser and written:
        first = out / "index.html" if (out / "index.html").exists() else written[0]
        subprocess.run(["open", str(first)], check=False)


@tutorials_grp.command("suggest")
@click.argument("location")
@click.option("--data-version", default=None, help="Pin a worker data version.")
@click.option(
    "--cap", type=int, default=0, help="Propose at most N tutorials (0 = size to worker)."
)
@click.option("--format", "fmt", type=click.Choice(["terminal", "json"]), default="terminal")
@click.option(
    "--fleet",
    "fleet_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML {worker_id: one-liner} index of other workers, so it can propose compositions.",
)
@click.option("--ai-backend", type=click.Choice(["claude", "api"]), default="claude")
@click.option("--ai-model", default=None)
def tutorials_suggest(
    location: str,
    data_version: str | None,
    cap: int,
    fmt: str,
    fleet_path: str | None,
    ai_backend: str,
    ai_model: str | None,
) -> None:
    """Propose a coverage-driven tutorial suite for a worker (LLM planner)."""
    from .core import with_attached_catalog
    from .review import make_backend
    from .tutorials.suggest import render_suggestions, suggest_tutorials

    backend = make_backend(ai_backend, ai_model)
    fleet = None
    if fleet_path:
        import yaml

        loaded = yaml.safe_load(Path(fleet_path).read_text())
        fleet = {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else None

    def _runner(catalog: Any, con: Any) -> Any:
        return suggest_tutorials(catalog, backend, cap=cap, fleet=fleet)

    try:
        proposals = with_attached_catalog(location, _runner, data_version=data_version)
    except WorkerConnectionError as e:
        click.secho(str(e), fg="red", err=True)
        sys.exit(EXIT_CONNECTION)
    click.echo(render_suggestions(proposals, fmt))


@tutorials_grp.command("init")
@click.option("--worker", required=True, help="Worker catalog id (e.g. cal).")
@click.option("--slug", required=True, help="Tutorial slug (kebab-case; becomes the filename).")
@click.option(
    "--tier", type=click.Choice(["quickstart", "recipe", "composition"]), default="quickstart"
)
@click.option("--out", "out_dir", default=".", help="Directory to write the .vgi.md into.")
@click.option("--draft", is_flag=True, help="LLM-draft the tutorial (needs --location + --job).")
@click.option("--job", default=None, help="The task the drafted tutorial should teach.")
@click.option("--location", default=None, help="Worker LOCATION (for --draft catalog context).")
@click.option("--ai-backend", type=click.Choice(["claude", "api"]), default="claude")
@click.option("--ai-model", default=None)
def tutorials_init(
    worker: str,
    slug: str,
    tier: str,
    out_dir: str,
    draft: bool,
    job: str | None,
    location: str | None,
    ai_backend: str,
    ai_model: str | None,
) -> None:
    """Scaffold a compliant ``.vgi.md`` (static skeleton, or an LLM ``--draft``)."""
    target = Path(out_dir) / f"{slug}.vgi.md"
    if target.exists():
        click.secho(f"refusing to overwrite {target}", fg="red", err=True)
        sys.exit(EXIT_TOOL_ERROR)
    target.parent.mkdir(parents=True, exist_ok=True)

    if draft:
        if not (location and job):
            click.secho("--draft needs --location and --job", fg="red", err=True)
            sys.exit(EXIT_TOOL_ERROR)
        from .core import with_attached_catalog
        from .review import make_backend
        from .tutorials.suggest import draft_tutorial

        backend = make_backend(ai_backend, ai_model)

        def _runner(catalog: Any, con: Any) -> str:
            return draft_tutorial(catalog, backend, worker=worker, slug=slug, tier=tier, job=job)

        try:
            content = with_attached_catalog(location, _runner)
        except WorkerConnectionError as e:
            click.secho(str(e), fg="red", err=True)
            sys.exit(EXIT_CONNECTION)
    else:
        from .tutorials.scaffold import scaffold_tutorial

        content = scaffold_tutorial(worker=worker, slug=slug, tier=tier)

    target.write_text(content, encoding="utf-8")
    click.echo(f"wrote {target}")


if __name__ == "__main__":  # pragma: no cover
    app()
