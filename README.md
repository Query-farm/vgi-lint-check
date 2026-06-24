<p align="center">
  <img src="https://raw.githubusercontent.com/Query-farm/vgi-lint-check/main/assets/vgi-logo.png" alt="Vector Gateway Interface" width="320">
</p>

<p align="center">
  <a href="https://pypi.org/project/vgi-lint-check/"><img src="https://img.shields.io/pypi/v/vgi-lint-check" alt="PyPI version"></a>
  <a href="https://pypi.org/project/vgi-lint-check/"><img src="https://img.shields.io/pypi/pyversions/vgi-lint-check" alt="Python versions"></a>
  <a href="https://github.com/Query-farm/vgi-lint-check/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Query-farm/vgi-lint-check/ci.yml?branch=main&amp;label=CI" alt="CI"></a>
  <a href="https://github.com/Query-farm/vgi-lint-check/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Source--Available-blue" alt="License"></a>
</p>

# vgi-lint

A `pydoclint`-style **metadata-quality linter for VGI workers**. It attaches to an
arbitrary VGI worker, reads everything the worker contributes through DuckDB
system tables, and reports quality findings — missing descriptions, undocumented
columns/functions, absent or malformed example queries, untagged objects, and
more — with a quality score, per-data-version baselines, and machine output for
coding agents.

It works with **any** VGI worker regardless of implementation language (Python,
Go, Rust, Java, TypeScript, …): it treats the worker as a black box and inspects
only what surfaces post-attach.

## Install / run

```bash
uv sync                      # haybarn is RC-only; prerelease = "allow" is set
uv run vgi-lint --help
```

## Quick start

```bash
# Lint a local subprocess worker
uv run vgi-lint 'uv run volcano_worker.py'

# Lint a no-auth HTTP worker
uv run vgi-lint http://localhost:9009

# Machine output for a coding agent / CI
uv run vgi-lint http://localhost:9009 --format agent
uv run vgi-lint http://localhost:9009 --format json
```

In a worker's own repo, add a `[tool.vgi-lint-check]` block (see `vgi-lint init`)
with a `location`, then just run `vgi-lint` with no arguments.

> v1 supports **local subprocess** and **no-auth HTTP** workers. Authenticated
> (OAuth) workers are not yet supported.

## What it checks

Object coverage: schemas, tables, views, columns, scalar/aggregate functions,
macros, settings, pragmas, and constraints. Rule families:

| Family | Codes | Examples |
| --- | --- | --- |
| Descriptions | VGI1xx | schema/table/view comment, `vgi.description_llm`, `vgi.description_md` |
| Columns | VGI2xx | column-comment coverage (tables **and views**), comment-not-echo |
| Functions | VGI3xx | description (+ quality), documented parameters, named arguments, examples |
| Tags | VGI4xx | required tag keys (opt-in), reserved-tag validity |
| Examples | VGI5xx | `vgi.example_queries` present, valid JSON, complete entries, **catalog-qualified** |
| Settings | VGI6xx | setting descriptions |
| Pragmas | VGI7xx | pragma descriptions |
| Constraints | VGI8xx | foreign-key/PK/check validity — references must point at real tables & columns |
| Structure | VGI11x | schema object-count cap (opt-in) |
| Execution | VGI9xx | example queries & CHECK constraints bind/execute (opt-in, `--execute`) |

See **[RULES.md](RULES.md)** for the full per-rule reference (codes, default
severities, and what each checks). Run `vgi-lint rules` to list them from your
installed version, or `vgi-lint explain VGI112` for one.

## Data versions

A VGI worker can publish multiple data versions whose metadata differs. The tool
can lint one or all of them and compare quality across versions:

```bash
uv run vgi-lint versions <location>            # list published versions
uv run vgi-lint <location> --data-version 2.0.0
uv run vgi-lint <location> --all-data-versions # per-version report + comparison
```

## Baselines (grandfathering)

Adopt the linter on an existing worker without a wall of failures: record current
findings as a baseline, then fail CI only on **new** findings. Baselines are
per data version (`<prefix>.<version>.json`).

```bash
uv run vgi-lint <location> --baseline vgi-lint-baseline --update-baseline
uv run vgi-lint <location> --baseline vgi-lint-baseline --fail-on warning
```

## Configuration

`[tool.vgi-lint-check]` in `pyproject.toml` (or a dedicated `vgi-lint.toml`):

```toml
[tool.vgi-lint-check]
location = "uv run worker.py"
select = ["ALL"]
ignore = ["VGI113"]
fail_on = "error"

[tool.vgi-lint-check.severity]
VGI201 = "error"

[tool.vgi-lint-check.options]
column_comment_min_ratio = 0.8
# Required tags are opt-in (empty by default) — set them if your workers have a
# tagging convention you want enforced:
# required_schema_tags = ["provider", "domain"]

[tool.vgi-lint-check.per-object]
"volcanos.hans.*" = { ignore = ["VGI112"] }
```

Precedence: defaults < `pyproject.toml` < `vgi-lint.toml` < CLI flags.

## Exit codes

`0` clean (or below `--fail-on`) · `1` config/tool error · `2` findings ≥
`--fail-on` (regressions only when a baseline is set) · `3` connection error.

## GitHub Action (reusable)

This repo ships a composite action so a worker repo can lint itself in CI with a
single step — it installs `uv`, runs the linter (the signed `vgi` community
extension is installed automatically), gates on `fail-on`, and posts the findings
to the job summary. **Build the worker first**, then point the action at it:

```yaml
# .github/workflows/ci.yml — inside a job that has already built the worker
      - name: VGI metadata quality
        uses: Query-farm/vgi-lint-check@v1
        with:
          location: "$PWD/target/release/units-worker"   # binary, command, or HTTP URL
          fail-on: warning                                 # info | warning | error | never
```

Gate releases harder than everyday CI — e.g. `fail-on: warning` on push/PR while
the worker's quality is being raised, and `fail-on: error` (plus `execute: true`)
in the publish workflow:

```yaml
      - uses: Query-farm/vgi-lint-check@v1
        with:
          location: "$PWD/target/release/units-worker"
          fail-on: error
          execute: true        # also run example queries / CHECK constraints (VGI9xx)
```

Key inputs: `location` (required), `fail-on` (default `error`), `version` (pin the
linter, e.g. `0.2.0`), `working-directory`, `data-version` / `all-data-versions`,
`baseline`, `execute`, `format` (`terminal|json|agent|jsonl`), `config`, `args`,
`summary`. The action's `exit-code` is exposed as an output. The action ref `@v1`
tracks the latest v1.x of the action; pin to a tag or SHA for full reproducibility.

## Development

```bash
uv run pytest               # unit tests (offline)
uv run pytest --run-live    # also run live tests against real workers
uv build                    # build sdist + wheel into dist/
```

## Releasing (GitHub Actions → PyPI)

Publishing is automated via GitHub Actions using **PyPI Trusted Publishing**
(OIDC — no API token secret to store):

- `.github/workflows/ci.yml` runs the offline test suite (Python 3.11–3.13) and
  a smoke build on every push/PR.
- `.github/workflows/publish.yml` builds, validates (`twine check`), and uploads
  to PyPI when a **GitHub Release is published**. It first checks that the
  release tag matches the `version` in `pyproject.toml`.

One-time setup on PyPI (Trusted Publisher), under the project's *Publishing*
settings (use a "pending publisher" before the first release):

| Field | Value |
| --- | --- |
| Owner | `Query-farm` |
| Repository | `vgi-lint-check` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

Also create a GitHub Environment named `pypi` in the repo settings (it gates the
publish job and is referenced for the OIDC claim).

To cut a release:

```bash
# bump version in pyproject.toml, commit, then tag + create the release
git tag v0.1.0 && git push origin v0.1.0
gh release create v0.1.0 --generate-notes
```

The release publishing event triggers the workflow. (Prefer a token instead of
OIDC? Replace the `publish` job's trusted-publishing step with
`pypa/gh-action-pypi-publish` configured with `password: ${{ secrets.PYPI_API_TOKEN }}`
and add that repository secret.)
