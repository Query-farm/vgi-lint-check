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

Object coverage: the catalog itself, schemas, tables, views, columns,
scalar/aggregate functions, macros, settings, pragmas, and constraints. Rule
families:

| Family | Codes | Examples |
| --- | --- | --- |
| Catalog | VGI0xx | catalog description, `vgi.description_llm`/`_md`, `source_url`, default schema resolves, `data_version_spec` semver + releases within it |
| Descriptions | VGI1xx | schema/table/view comment, `vgi.description_llm`, `vgi.description_md` |
| Discoverability | VGI12x | duplicate/short/echoed descriptions, join-path docs, release freshness, example richness, units (opt-in) |
| Content | VGI17x | `vgi.description_md` is valid Markdown; description links/images & source URLs resolve (no 404) |
| Columns | VGI2xx | column-comment coverage (tables **and views**), comment-not-echo |
| Functions | VGI3xx | description (+ quality), documented parameters, named arguments, examples |
| Tags | VGI4xx | required tag keys (opt-in), reserved-tag validity |
| Examples | VGI5xx | `vgi.example_queries` present, valid JSON, complete entries, **catalog-qualified** |
| Settings | VGI6xx | setting descriptions |
| Pragmas | VGI7xx | pragma descriptions |
| Constraints | VGI8xx | FK/PK/check validity — references must point at real tables & columns; completeness nudges (no constraints / no PKs / no NOT NULL anywhere) |
| Structure | VGI11x | schema object-count cap (opt-in) |
| Execution | VGI9xx | example queries & CHECK constraints bind/execute (opt-in, `--execute`); per-query timeout so nothing runs forever |

See **[RULES.md](RULES.md)** for the full per-rule reference (codes, default
severities, and what each checks). Run `vgi-lint rules` to list them from your
installed version, or `vgi-lint explain VGI112` for one.

**Link checking is on by default** (VGI171): URLs and images in descriptions,
and `source_url`/`vgi.source_url` repo links, are resolved over HTTP and flagged
if they 404. Only definitive client errors (4xx) are reported — timeouts, DNS
failures, 5xx, and access-gated codes are skipped so CI isn't flaky. Disable
with `--no-check-links` (or run fully offline).

**Executing example queries** (`--execute`, opt-in) runs every example a worker
ships against the live catalog. Examples are collected from **both** carriers —
the `vgi.example_queries` tag *and* a function's native `Meta.examples`
(DuckDB's `duckdb_functions().examples` column) — deduped by SQL, across tables,
views, macros, and scalar/aggregate/table functions. Each query runs under a
per-query wall-clock cap (`execute_timeout`, default 30s) and is cancelled if it
exceeds it, so a runaway example can never hang a lint run:

```toml
[tool.vgi-lint-check.execution]
enabled = true       # same as --execute
mode    = "explain"  # explain (bind-only, cheapest) | limit | run
limit   = 1          # row cap for limit/VGI902 modes
timeout = 30.0       # per-query seconds; 0 disables the guard
```

## Reserved tags

VGI workers attach metadata via tags; `vgi-lint` recognizes these reserved keys
(set them on the catalog, a schema, a table/view, or — where noted — a function):

| Tag | Purpose |
| --- | --- |
| `vgi.description_llm` | Concise description aimed at LLMs/agents (tool selection) |
| `vgi.description_md` | Markdown description for human docs / listing pages |
| `vgi.example_queries` | JSON list of `{"description","sql"}` example queries |
| `vgi.title` | Human/marketing display name (vs. the machine name) |
| `vgi.keywords` | Comma-separated search keywords / synonyms |
| `vgi.columns_md` | Markdown doc of a table function's returned columns (for dynamic schemas DuckDB can't expose) |
| `vgi.source_url` | Link to where the object is implemented (repo/file) |
| `vgi.author` | Author / maintainer attribution (catalog) |
| `vgi.copyright` | Copyright notice (catalog) |
| `vgi.license` | License name or SPDX identifier (catalog) |
| `vgi.support_contact` | Where to report issues/bugs — email or URL (catalog) |
| `vgi.support_policy_url` | Link to the support / SLA policy (catalog) |

`vgi.description_llm`/`_md` are **required on the catalog and every schema**
(the catalog is the worker's listing; schemas are its sections). They're
**optional on tables, views, and functions** (opt-in to require, but validated
when set — e.g. minimum length, must differ). The catalog `source_url` is
required; titles, keywords, and per-object source links are opt-in but validated
when set; author/copyright/license are encouraged (info). Tune any of this via
config.

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

## Security / trust boundary

A subprocess `LOCATION` is **executed as a command** to launch the worker (the
`vgi` extension spawns it). Treat `location` like any shell command: never pass
an attacker-controlled value, and in CI never derive it from untrusted input
(e.g. a fork PR title/branch). Prefer a fixed path or HTTP URL you control.

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
