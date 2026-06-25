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
| Catalog | VGI0xx | catalog description, `vgi.doc_llm`/`_md`, `source_url`, default schema resolves, `data_version_spec` semver + releases within it, **catalog not empty**, worker advertises 1–N catalogs, `vgi.license` is a valid SPDX id |
| Descriptions | VGI1xx | schema/table/view/function comment, `vgi.doc_llm`, `vgi.doc_md` |
| Discoverability | VGI12x/13x | duplicate/short/echoed descriptions, **no placeholder text (TODO/TBD/…)**, classifying tag present + **reused (small vocabulary)**, title/keywords present (**keywords as a JSON array**), **source_url is catalog-only**, join-path docs, release freshness, example richness, column units |
| Content | VGI17x | `vgi.doc_md` is valid Markdown; description links/images & source URLs resolve (no 404) |
| Columns | VGI2xx | column-comment coverage + **every column commented**, comment-not-echo, **naive TIMESTAMP documents its timezone** |
| Functions | VGI3xx | description (+ quality), documented parameters, named arguments, examples, scalar-function stability (all-VOLATILE smell + per-function VOLATILE flag), **every-parameter-ANY smell**, **parameterless table function should be a table** |
| Tags | VGI4xx | required tag keys (opt-in), reserved-tag validity, deprecated-key migration, **`vgi.category_tags` valid (JSON array, not on the catalog)** |
| Examples | VGI5xx | `vgi.example_queries` present, valid JSON, complete, **catalog-qualified**, references its object; `vgi.executable_examples` well-formed + **deterministic (ORDER BY)** |
| Settings | VGI6xx | setting descriptions |
| Pragmas | VGI7xx | pragma descriptions |
| Constraints | VGI8xx | FK/PK/check validity; completeness nudges (no constraints / PKs / NOT NULL anywhere); **per-table primary key**; **`<table>_id` column with no FK suggests one** |
| Attach options | VGI10xx | every `vgi_catalogs()` attach option is documented (description present + meaningful) |
| Structure | VGI11x/13x | **schema not empty**; warn on excessive table/function counts and over-long table/function names; **schema object-count cap (>50 by default)** |
| Execution | VGI9xx | illustrative examples bind (best-effort warning) & **executable examples must run + match expected output**; CHECK constraints bind; advertised attach options are accepted and advertised catalogs attach (`--execute`, **on by default**); per-query timeout so nothing runs forever |

**Strict by default.** `vgi-lint` ships a strict profile: descriptions on every
table/view/function, classifying/title/keyword/source-url tags, column
documentation, per-table primary keys, and example coverage are all enforced by
default. To run a lighter profile, turn rules off in config — e.g.
`ignore = ["VGI112", "VGI113", "VGI124", "VGI126", "VGI202"]` — or set
`[tool.vgi-lint-check.severity]` per code. Use `vgi-lint rules` to see every rule
and its default.

See **[RULES.md](RULES.md)** for the full per-rule reference (codes, default
severities, and what each checks). Run `vgi-lint rules` to list them from your
installed version, or `vgi-lint explain VGI112` for one.

**Link checking is on by default** (VGI171): URLs and images in descriptions,
and `source_url`/`vgi.source_url` repo links, are resolved over HTTP and flagged
if they 404. Only definitive client errors (4xx) are reported — timeouts, DNS
failures, 5xx, and access-gated codes are skipped so CI isn't flaky. Disable
with `--no-check-links` (or run fully offline).

**Execution is on by default** (`--no-execute` for a static-only lint). Execution
rules run against the live worker under a per-query wall-clock cap
(`execute_timeout`, default 30s) so a runaway query can never hang a lint run.

There are **two tiers of examples**:

- **Illustrative** — `vgi.example_queries` *and* a function's native
  `Meta.examples` (DuckDB's `duckdb_functions().examples`), deduped by SQL across
  tables, views, macros, and scalar/aggregate/table functions. These teach usage
  shape and may reference data or context not present at lint time, so a failure
  to bind is a **warning** (VGI901), never a gate.
- **Executable** — `vgi.executable_examples`: self-contained, must-run examples
  that are the contract and the highest-quality material for LLMs. **VGI906**
  runs every statement in order (ERROR if any fails — not filter-skipped, they
  must be self-contained); **VGI907** asserts a statement's output against its
  optional `expected_result` (warning). `expected_result` lives on the
  individual statement, so a multi-statement example can assert any step.

  Write `expected_result` as a **list of row-objects keyed by column name** —
  `[{"class": "strong"}]` — which is self-documenting (a bare scalar or a list of
  rows is also accepted). Comparison stringifies cells (`NULL` → `null`, booleans
  lowercase, numbers as printed — `1.0`, not `1`) and matches rows in order. On a
  mismatch **VGI907 prints the actual output in that exact canonical form**, so
  you can copy it straight into `expected_result` instead of guessing how a value
  is represented.

```jsonc
// vgi.executable_examples on any object (catalog, schema, table, view, function)
[
  {
    "name": "classify a strong quake",
    "description": "magnitude_class buckets a Richter value; 6.2 -> 'strong'.",
    "sql": [                                  // string | [string] | [{description, sql, expected_result?}]
      {"description": "set up a session option", "sql": "SET threads=2"},
      {"description": "Classify magnitude 6.2",
       "sql": "SELECT volcanos.main.magnitude_class(6.2) AS class",
       "expected_result": [{"class": "strong"}]}   // optional; cells compare as strings, rows in order
    ]
  }
]
```

Executable examples should be **re-runnable** (e.g. use `CREATE OR REPLACE`),
since VGI906 and VGI907 each run the statement sequence. Keep the set focused:
**VGI508** warns when one object declares more than
`options.max_executable_examples` (default 10) — each runs against the worker
under `--execute`, and a long list is noise for an LLM. Every executable-example
finding's `fix` is fully self-describing (the JSON shape, `expected_result`
format, and the catalog-qualified/self-contained requirement), so a coding agent
can author or repair the tag straight from `--format agent`/`json` output.

```toml
[tool.vgi-lint-check.execution]
enabled     = true       # default; --no-execute to disable
mode        = "explain"  # explain (bind-only, cheapest) | limit | run — for VGI901
limit       = 1          # row cap for limit/VGI902 modes
timeout     = 30.0       # per-query seconds; 0 disables the guard
concurrency = 1          # run example queries across N cursors in parallel
```

**Parallel execution.** `concurrency > 1` (or `--execute-concurrency N`) runs
example queries across N cursors that share the attach, so the VGI worker pool
serves them from distinct workers. On a compute-bound worker this is roughly
linear (measured ~3.5× at N=4 on `vgi-units`); on a tiny/local worker it's a
wash, since there's no per-query work to overlap. Multi-statement executable
examples stay ordered on their own cursor; findings remain deterministic.

## Attach options

A worker advertises its attach-time options through `vgi_catalogs()` **before**
attach — each option has a `name`, `description`, `type`, and `default_value`.
`vgi-lint` reads them and checks they're documented (**VGI1001/VGI1002**): an
agent choosing the worker relies on those descriptions to know what each option
does. Whether an option is *required* is not flagged on the wire — it's inferred
from the absence of a default. With `--execute`, two live checks also run:

- **VGI904** attaches a throwaway handle passing every advertised option at its
  default and confirms the worker actually accepts each one (options whose type
  can't be reconstructed from a stringified default — `STRUCT`/`MAP`/array/blob —
  are skipped rather than guessed).
- **VGI905** confirms every catalog `vgi_catalogs()` advertises can be attached.

## Reserved tags

VGI workers attach metadata via tags; `vgi-lint` recognizes these reserved keys
(set them on the catalog, a schema, a table/view, or — where noted — a function):

| Tag | Purpose |
| --- | --- |
| `vgi.doc_llm` | LLM-oriented narrative doc — what the object is and when to use it (tool selection). *Complements, doesn't duplicate, the object's own `description`/comment.* |
| `vgi.doc_md` | Richer Markdown narrative doc for human docs / listing pages |
| `vgi.doc_links` | JSON array of links to more docs — URL strings or `{"title","url"}` objects (validated + resolved) |
| `vgi.example_queries` | JSON list of `{"description","sql"}` *illustrative* example queries |
| `vgi.executable_examples` | JSON list of self-contained, **must-run** examples (see below) |
| `vgi.title` | Human/marketing display name (vs. the machine name) |
| `vgi.keywords` | JSON array of search keywords / synonyms — `["a","b"]` (comma-separated string is now a **VGI138 error**) |
| `vgi.category_tags` | JSON array of category labels for faceting — on any object **except the catalog** |
| `vgi.result_columns_md` | Markdown doc of a table function's returned columns (for dynamic schemas DuckDB can't expose) |
| `vgi.source_url` | Link to where the object is implemented (repo/file) |
| `vgi.author` | Author / maintainer attribution (catalog) |
| `vgi.copyright` | Copyright notice (catalog) |
| `vgi.license` | License name or SPDX identifier (catalog) |
| `vgi.support_contact` | Where to report issues/bugs — email or URL (catalog) |
| `vgi.support_policy_url` | Link to the support / SLA policy (catalog) |

> **Renamed:** `vgi.doc_llm`/`vgi.doc_md` (was `vgi.description_llm`/`_md`) and
> `vgi.result_columns_md` (was `vgi.columns_md`). The old keys still work (dual
> recognition) but **VGI405** nudges you to migrate; they'll stop being
> recognized in a future version.

`vgi.doc_llm`/`vgi.doc_md` are **required on the catalog, every schema, and
(under the strict default) every table, view, and function** — and validated
when set (minimum length, must differ from each other and from the object's own
description). The catalog `source_url`, titles, keywords, and per-object source
links are enforced by the strict default; author/copyright/license are
encouraged (info). Relax any of this (e.g. back to optional docs on
tables/views/functions) via
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
          # execution rules (VGI9xx) run by default; set execute: false for static-only
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
