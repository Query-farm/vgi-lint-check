"""Command-line interface.

Root command is ``lint`` (so ``vgi-lint <location>`` just works); ``rules``,
``explain``, ``versions``, and ``init`` are subcommands.
"""

from __future__ import annotations

import os
import sys

import click

from . import reporting
from .config import Config, Options, load_config
from .connection import WorkerConnectionError, connect_loaded, derive_alias
from .core import lint_worker
from .exit_codes import EXIT_CONNECTION, EXIT_TOOL_ERROR
from .findings import Severity


class DefaultGroup(click.Group):
    """A group that routes an unknown first token to a default command, so
    ``vgi-lint <location>`` and ``vgi-lint --format json <location>`` work."""

    def __init__(self, *args, default=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_cmd = default

    def parse_args(self, ctx, args):
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
def app():
    """Lint the metadata quality of a VGI worker."""


# --------------------------------------------------------------------------
# lint
# --------------------------------------------------------------------------
@app.command()
@click.argument("location", required=False)
@click.option("--as", "alias", default=None, help="Local catalog alias handle.")
@click.option("--catalog", "catalog_name", default=None, help="Worker catalog name.")
@click.option("--spatial/--no-spatial", default=False, help="Load the spatial extension.")
@click.option("--install/--no-install", default=True, help="INSTALL vgi from community.")
@click.option("--data-version", "data_versions", multiple=True, help="Lint a specific data version (repeatable).")
@click.option("--all-data-versions", is_flag=True, help="Discover and lint every published version.")
@click.option("--execute", is_flag=True, help="Run example queries (enables VGI9xx).")
@click.option("--execute-mode", type=click.Choice(["explain", "limit", "run"]), default=None)
@click.option("--execute-limit", type=int, default=None)
@click.option("--select", default=None, help="Comma list/globs of rule codes to enable.")
@click.option("--ignore", default=None, help="Comma list/globs of rule codes to disable.")
@click.option("--category", "categories", default=None, help="Comma list of categories.")
@click.option("--severity", "severities", multiple=True, help="CODE=LEVEL override (repeatable).")
@click.option("--baseline", default=None, help="Baseline file prefix (per-version).")
@click.option("--update-baseline", is_flag=True, help="Write/refresh the baseline file(s).")
@click.option("--fail-on", type=click.Choice(["info", "warning", "error", "never"]), default=None)
@click.option("--format", "fmt", type=click.Choice(list(reporting.FORMATS)), default="terminal")
@click.option("--output", type=click.Path(dir_okay=False), default=None, help="Write report to FILE.")
@click.option("--color", type=click.Choice(["auto", "always", "never"]), default="auto")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--quiet", "-q", is_flag=True)
def lint(location, alias, catalog_name, spatial, install, data_versions,
         all_data_versions, execute, execute_mode, execute_limit, select, ignore,
         categories, severities, baseline, update_baseline, fail_on, fmt, output,
         color, config_path, quiet):
    """Lint a worker at LOCATION (URL or subprocess command)."""
    cfg = load_config(config_path)
    _apply_cli_overrides(
        cfg, select=select, ignore=ignore, categories=categories,
        severities=severities, execute=execute, execute_mode=execute_mode,
        execute_limit=execute_limit, fail_on=fail_on, baseline=baseline,
    )
    location = location or cfg.location
    if not location:
        raise click.UsageError(
            "no worker LOCATION given and none configured in "
            "[tool.vgi-lint-check] location"
        )

    try:
        report = lint_worker(
            location, alias=alias, catalog_name=catalog_name, config=cfg,
            install=install, spatial=spatial,
            data_versions=list(data_versions) or None,
            all_versions=all_data_versions, update_baseline=update_baseline,
        )
    except WorkerConnectionError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        raise SystemExit(EXIT_CONNECTION)
    except Exception as e:  # noqa: BLE001
        click.secho(f"error: {e}", fg="red", err=True)
        raise SystemExit(EXIT_TOOL_ERROR)

    use_color = _resolve_color(color)
    text = reporting.render(report, fmt, color=use_color)
    if output:
        with open(output, "w") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        if not quiet:
            click.echo(f"wrote {fmt} report to {output}")
    elif not quiet:
        click.echo(text)
    raise SystemExit(0 if report.passed() else 2)


# --------------------------------------------------------------------------
# rules / explain
# --------------------------------------------------------------------------
@app.command(name="rules")
@click.option("--category", "category", default=None)
@click.option("--format", "fmt", type=click.Choice(["terminal", "json"]), default="terminal")
def rules_cmd(category, fmt):
    """List the rule catalog."""
    from .rules.registry import all_rule_classes

    items = [
        c for c in all_rule_classes()
        if category is None or str(c.category) == category
    ]
    if fmt == "json":
        import json

        click.echo(json.dumps([
            {"code": c.code, "name": c.name, "category": str(c.category),
             "default_severity": c.default_severity.label, "summary": c.summary,
             "requires_connection": c.requires_connection}
            for c in items
        ], indent=2))
        return
    for c in items:
        click.echo(
            f"{c.code}  {c.default_severity.label:<7} {str(c.category):<12} {c.summary}"
        )


@app.command()
@click.argument("code")
def explain(code):
    """Explain one rule: CODE."""
    from .rules.registry import REGISTRY

    cls = REGISTRY.get(code.upper())
    if cls is None:
        raise click.UsageError(f"unknown rule code {code!r}")
    click.echo(f"{cls.code}  ({cls.category}, default {cls.default_severity.label})")
    click.echo(f"  {cls.name}")
    click.echo()
    click.echo(f"  {cls.summary}")
    if cls.requires_connection:
        click.echo("\n  Requires --execute (runs against the worker).")


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------
@app.command(name="versions")
@click.argument("location")
@click.option("--install/--no-install", default=True)
def versions_cmd(location, install):
    """List a worker's published data versions."""
    from .versions import discover_catalogs

    try:
        con, _ = connect_loaded(install=install)
    except WorkerConnectionError as e:
        click.secho(f"error: {e}", fg="red", err=True)
        raise SystemExit(EXIT_CONNECTION)
    try:
        catalogs = discover_catalogs(con, location)
    finally:
        con.close()
    for c in catalogs:
        click.echo(f"catalog: {c.catalog}  (impl {c.implementation_version or '—'}, "
                   f"spec {c.data_version_spec or '—'})")
        if not c.releases:
            click.echo("  (no published data versions — default only)")
        for r in c.releases:
            when = f" {r.released_at}" if r.released_at else ""
            summary = f" — {r.summary}" if r.summary else ""
            click.echo(f"  {r.version}{when}{summary}")


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------
@app.command()
@click.option("--location", default=None, help="Pre-fill the worker location.")
@click.option("--file", "target", type=click.Path(dir_okay=False), default="vgi-lint.toml")
def init(location, target):
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
        'required_schema_tags = ["provider", "domain"]\n'
        "column_comment_min_ratio = 0.8\n"
    )
    with open(target, "w") as fh:
        fh.write(content)
    click.echo(f"wrote {target}")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _apply_cli_overrides(cfg: Config, *, select, ignore, categories, severities,
                         execute, execute_mode, execute_limit, fail_on, baseline):
    if select is not None:
        cfg.select = _split(select)
    if ignore is not None:
        cfg.ignore = _split(ignore)
    if categories is not None:
        cfg.categories = _split(categories)
    for item in severities:
        if "=" not in item:
            raise click.UsageError(f"--severity expects CODE=LEVEL, got {item!r}")
        code, level = item.split("=", 1)
        cfg.severity_overrides[code.strip()] = Severity.parse(level)
    if execute:
        cfg.execute = True
    if execute_mode is not None:
        cfg.execute_mode = execute_mode
    if execute_limit is not None:
        cfg.execute_limit = execute_limit
    if fail_on is not None:
        cfg.fail_on = Severity.OFF if fail_on == "never" else Severity.parse(fail_on)
    if baseline is not None:
        cfg.baseline = baseline


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


if __name__ == "__main__":  # pragma: no cover
    app()
