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

### Workers that require attach options / credentials

Some workers demand options at `ATTACH` time (e.g. a mail worker needing
`PROVIDER`/`SECRET`, or `HOST`/`USERNAME`/`PASSWORD`). Pass them with
`--attach-option KEY=VALUE` (repeatable), and run any prerequisite SQL (e.g. a
`CREATE SECRET`) with `--setup-sql` (repeatable). A worker that resolves
credentials **lazily** (only on the first query) attaches fine with a
*placeholder* secret name — enough for a static, no-connection metadata lint:

```bash
# Static metadata lint of a credential-gated worker (no live account needed)
uv run vgi-lint '/path/to/.venv/bin/vgi-email' \
  --attach-option provider=imap --attach-option secret=lint --no-execute

# If --execute rules need a real connection, create the secret first:
uv run vgi-lint '/path/to/.venv/bin/vgi-email' \
  --setup-sql "CREATE SECRET lint (TYPE imap, HOST 'imap.example.com', USERNAME 'me', PASSWORD 'pw')" \
  --attach-option provider=imap --attach-option secret=lint
```

Equivalent config keys: `attach_options = { provider = "imap", secret = "lint" }`
and `setup_sql = ["CREATE SECRET ..."]` under `[tool.vgi-lint-check]`. The GitHub
Action exposes these as the `attach-options` and `setup-sql` inputs (one
`KEY=VALUE` / statement per line).

> v1 supports **local subprocess** and **no-auth HTTP** workers. Authenticated
> (OAuth) workers are not yet supported.

**Findings are grouped by rule** by default, so a rule firing on many objects
reads as one block — the fix stated once, the affected objects listed beneath
(capped at `--max-per-rule`, default 10, with `… +N more`; `0` = list all). Use
`--group-by object` for the per-object layout. The `agent` format groups by rule
too but never truncates, so an LLM gets one fix instruction plus the full object
list (far fewer tokens than repeating the fix per object). `json`/`jsonl` are
unchanged — the complete, ungrouped contract.

## What it checks

Object coverage: the catalog itself, schemas, tables, views, columns,
scalar/aggregate functions, macros, settings, pragmas, and constraints. Rule
families:

| Family | Codes | Examples |
| --- | --- | --- |
| Catalog | VGI0xx | catalog description, `vgi.doc_llm`/`_md`, `source_url`, default schema resolves, `data_version_spec` semver + releases within it, **catalog not empty**, worker advertises 1–N catalogs, `vgi.license` is a valid SPDX id |
| Descriptions | VGI1xx | schema/table/view/function comment, `vgi.doc_llm`, `vgi.doc_md`; **catalog/schema docs must be detailed** (≥300/≥160 chars) |
| Discoverability | VGI12x/13x | duplicate/short/echoed descriptions, **no placeholder text (TODO/TBD/…)**, classifying tag present + **reused (small vocabulary)**, **`vgi.title` required on catalog + schemas** (optional, validated when set, below), keywords present (**JSON array**), **source_url is catalog-only**, join-path docs, release freshness, example richness, column units |
| Content | VGI17x | `vgi.doc_md` is valid Markdown; description links/images & source URLs resolve (no 404) |
| Columns | VGI2xx | column-comment coverage + **every column commented**, comment-not-echo, **naive TIMESTAMP documents its timezone** |
| Functions | VGI3xx | description (+ quality), documented parameters, named arguments, **per-argument descriptions required** (error) that **don't restate the data type** (warning; needs a vgi extension exposing `vgi_function_arguments()`); **function docs must not re-document their arguments** (a parameter list in the function doc duplicates `vgi_function_arguments()` and drifts); examples, scalar-function stability (all-VOLATILE smell + per-function VOLATILE flag), **every-parameter-ANY smell**, **parameterless table function should be a table** |
| Tags | VGI4xx | required tag keys (opt-in), reserved-tag validity, **unknown `vgi.*` key is likely a typo (did-you-mean)**, deprecated-key migration, `vgi.category_tags` valid, **`vgi.agent_test_tasks` valid** |
| Examples | VGI5xx | `vgi.example_queries` present, valid JSON, complete, **catalog-qualified**, references its object; `vgi.executable_examples` well-formed + **deterministic (ORDER BY)** |
| Settings | VGI6xx | setting descriptions |
| Pragmas | VGI7xx | pragma descriptions |
| Constraints | VGI8xx | FK/PK/check validity; completeness nudges (no constraints / PKs / NOT NULL anywhere); per-table primary key; **`<table>_id` column with no FK suggests one**; **a key-shaped column shared by several tables with no FK may be a missing relationship** |
| Attach options | VGI10xx | every `vgi_catalogs()` attach option is documented (description present + meaningful) |
| Structure | VGI11x/13x/14x | **schema not empty**; warn on excessive table/function counts and over-long table/function names; **schema object-count cap (>50 by default)**; **redundant `get_`/`list_` name prefixes** |
| Execution | VGI9xx | example queries **must bind (error)**, runtime/data failures are warnings; **executable examples must run + match expected output + run fast** (slow ones warn, naming the example); CHECK constraints bind; advertised attach options are accepted and advertised catalogs attach (`--execute`, **on by default**); per-query timeout so nothing runs forever |

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
  shape. VGI901 splits the verdict: an example that **doesn't bind** (unknown
  table/column/function, bad types — a real authoring bug) is an **error**; one
  that binds but **fails at runtime** (it may just need data/context) is a
  **warning**.
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
concurrency = 8          # example-query parallelism; omit to use the CPU count (lower for rate-limited)
slow_seconds = 5.0       # VGI908 warns on an executable example slower than this (0 = off)
```

**VGI908 flags slow executable examples** that bloat CI — naming the offending
example and its measured time (`executable example 'heavy-scan' is slow (8.2s >
5s)`). It reuses the timing VGI906 already measures, so detection adds no extra
execution pass.

**Parallel execution (on by default).** Execution rules run example queries
across N cursors that share the attach, so the VGI worker pool serves them from
distinct workers. **N defaults to the machine's CPU count** — worth the most on an
I/O-bound worker where each query is a live API round trip (measured ~4× on a
credential-gated worker: 117 s → 32 s), roughly linear on a compute-bound worker
(~3.5× at N=4 on `vgi-units`), and a wash on a tiny/local worker. Lower it with
`--execute-concurrency N` (or the config) for a **rate-limited** worker.
Multi-statement executable examples stay ordered on their own cursor; findings
remain deterministic.

**Diagnosing a slow lint.** `--trace [FILE]` (default `vgi-lint-trace.log`)
writes a per-phase and per-rule timing log: `connect+load`, `ATTACH`, catalog
build, each rule's wall-clock, and the AI passes — with a "slowest rules"
summary. On a worker backed by a live API (each `--execute` query is a round
trip), this pinpoints exactly which execution rules dominate. Pair it with
`--execute-concurrency N` to parallelize the example-query round trips.

## Documentation review (LLM-as-judge)

The deterministic linter checks *mechanics* (presence, length, echoes, validity).
`vgi-lint review` adds an **advisory, opt-in** layer that judges what rules can't —
**accuracy, clarity, completeness, audience-fit** — by sending each object's
descriptions **plus its real structure** (columns, types, constraints, examples)
to an LLM with a rubric. Grounding it in the facts is what makes the feedback
reliable (it catches prose that contradicts the schema), not a vibe check.

```bash
vgi-lint review <worker>                 # default backend: the local `claude` CLI
vgi-lint review <worker> --format json   # machine-readable verdicts
```

- **Default backend is the local `claude` CLI** (`claude -p`), so judging runs on
  your **Claude Pro/Max subscription** — no per-token API fees. `--review-backend
  api` uses the Anthropic API instead (needs `ANTHROPIC_API_KEY` + the `anthropic`
  package). Pick a model with `--review-model`.
- **Verdicts are cached by content hash** (`.vgi-review-cache.json`), so unchanged
  docs aren't re-judged — a re-run is free. `--no-review-cache` disables it.
- Run it **standalone** (`vgi-lint review`) for advisory-only per-object sub-scores
  (1–5) + suggestions, **or fold it into a lint** with `vgi-lint lint --doc-review`:
  objects scoring below `options.doc_quality_min` (default 3) become **VGI180**
  findings and the mean doc-quality blends into the headline score.
- Objects are batched per model call (`--review-batch`, default 8) to stay within
  subscription rate limits.

### Folding the LLM passes into a lint

`lint` accepts `--doc-review` (VGI180, above) and `--agent-check` (run the
`simulate` suite, below). With `--agent-check`, **VGI920 fails the run** when the
agent pass-rate is under `options.agent_pass_threshold` (default 0.8), and the
agent-suitability score blends into the headline. `--ai` enables both. All use the
`claude` subscription backend by default (`--ai-backend api` / `--ai-model` to
override).

The **Catalog Quality Score** is static metadata coverage (descriptions, columns,
function-docs, examples, **categories**) minus finding penalties. When the LLM
passes run, the headline becomes a blend — **~55% static · 25% agent · 20%
doc-quality**, renormalized over whichever ran — and `static_score`, `agent_score`,
and `doc_quality` are reported alongside it.

## Agent-suitability testing (`vgi-lint simulate`)

Documentation review grades *prose*. `simulate` answers the harder question: **can
an agent/SQL-analyst actually accomplish real work here using only what's exposed?**
A worker declares a **fixed** task suite in `vgi.agent_test_tasks`; `simulate` runs
an LLM analyst through each one — it sees only a bounded orientation listing and the
task *prompt* (never the solution) and **discovers the schema through tools**, just
like a real agent: `list_tables`, `describe_table`, `describe_function`, and a guarded
`run_sql` (a local mirror of the production "ask AI" tool contract). It iterates until
it answers. It's a real test, not a vibe check: grading is **execution-based**.

```jsonc
// vgi.agent_test_tasks (catalog tag) — a fixed, version-controlled acceptance suite
[
  {
    "name": "kwh to joules",
    "prompt": "How many joules is 100 kWh? Return one column named joules.",  // ONLY this is shown to the analyst
    "reference_sql": "SELECT units.main.convert(100, 'kWh', 'J') AS joules"    // canonical solution — hidden; re-run to grade
  }
]
```

```bash
vgi-lint simulate <worker>              # run the suite (gates on --min-pass-rate)
vgi-lint simulate <worker> --suggest 5  # authoring: propose candidate tasks as tag JSON
```

- **Grading is layered, strongest wins:** (1) compare the analyst's answer to the
  `reference_sql`'s terminal result (deterministic; stores the *query*, so it
  survives data drift), (2) a `check_sql` assertion over the analyst's post-session
  state, (3) an LLM judge against `success_criteria`. Friction (what metadata was
  missing/confusing) is always surfaced.
- **Tool-mediated discovery** — the analyst is handed only a names-and-one-liners
  listing, then pulls columns/signatures/constraints on demand through the discovery
  tools (no full catalog dump). This scales and mirrors how real agents work, so the
  friction it surfaces reflects real metadata gaps.
- **The path is scored, not just the outcome.** Each task gets a **discoverability
  score** (0–100) from *how* the agent got there — penalizing wasted effort the
  metadata should have spared it (queries that failed to bind, hitting a mandatory
  filter by trial-and-error, re-inspecting an object whose description was too thin,
  looking up something that doesn't exist, or never converging). A task can **pass
  yet score low** — that's the signal the docs need work. Each fault becomes a
  concrete **suggestion** (e.g. "add a `vgi.executable_examples` entry showing this",
  "tighten the column docs — VGI2xx"), deduped across the suite. Raw step count is
  *not* penalized, so an inherently complex task isn't marked down for being complex —
  only for friction. Prefer fixing the worker's metadata over raising `--attempts`:
  needing retries is itself a discoverability finding.
- **The solution is hidden from the analyst** — only `prompt` reaches it, so the
  test measures whether the path is *discoverable* from metadata, not whether the
  agent can copy an answer. Strict result grading is the contract: column names,
  values, and row order must match the reference (per-task `unordered` /
  `ignore_column_names` opt-outs), so prompts should name their output columns.
- **Stateful tasks are supported:** the analyst may build session-local state
  (temp views, `SET`); a guard blocks anything that escapes the disposable session
  (worker writes, `ATTACH`/`INSTALL`/`COPY … TO`).
- **It's a test:** exits non-zero when the pass rate is below `--min-pass-rate`
  (default 1.0); `--advisory` never gates; `--attempts N` retries to tame
  actor non-determinism. Same `claude`-CLI-by-default backend and verdict cache as
  `review`.
- **Verify references first:** `vgi-lint simulate <worker> --verify-references` runs each
  task's `reference_sql` a few times and flags any that error, are non-deterministic (a
  random/unseeded reference), or return no rows — an authoring/CI gate (exit 2 on
  failure) that catches unsound references *before* a graded run turns them into flaky
  failures. It checks reference soundness, not whether an agent can reproduce the answer
  (that's the simulation).
- **Object coverage:** the report shows how many of the worker's objects — functions
  **and tables/views** — the suite's `reference_sql` actually exercises
  (`object coverage 16/16 (100%)`) and names the untested ones — so a suite can't
  quietly leave half the API unchecked while scoring 100% pass-rate. (Counting tables
  matters for table-centric workers: a geodata worker whose surface is all tables would
  otherwise read 0/1.) `vgi-lint simulate <worker> --suggest` is
  **coverage-driven and batched**: it iterates over small batches of *uncovered* objects
  (recomputing coverage each round) until the worker is covered, so each LLM call stays
  fast and it scales to large catalogs without hitting the backend timeout (bare
  `--suggest` auto-sizes; `--suggest N` caps at N). Pair it with `--verify-references` to
  drop any unsound proposals.
- **Tasks run in parallel** (`--concurrency`, default 4): each task is judged on its
  own cursor against the VGI worker pool, so a multi-task suite finishes in roughly
  the time of its slowest task, not the sum (~3.4× on a 4-task suite).
- **Sessions, not re-sends** (`--session`, default on): the analyst's ReAct loop runs
  over a `claude` session — turn 1 sets `--session-id`, later turns `--resume` it — so
  only the new tool result is sent each turn instead of re-transmitting the whole
  transcript. Each task gets its own session id (safe under `--concurrency`); the
  Anthropic API backend accumulates messages instead. `--no-session` forces the
  stateless re-send.
- **Double-duty:** the encoded `reference_sql` doubles as curated **few-shot
  guidance** a worker's MCP server / `suggest_queries` can expose to real agents.

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
(set them on the catalog, a schema, a table/view, or — where noted — a function).
**[`TAGS.md`](TAGS.md) is the complete, normative reference** — every tag's
meaning, where it may occur, its value shape, and the rules that govern it. The
table below is a quick index:

| Tag | Purpose |
| --- | --- |
| `vgi.doc_llm` | LLM-oriented narrative doc — what the object is and when to use it (tool selection). *Complements, doesn't duplicate, the object's own `description`/comment.* |
| `vgi.doc_md` | Richer Markdown narrative doc for human docs / listing pages |
| `vgi.doc_links` | JSON array of links to more docs — URL strings or `{"title","url"}` objects (validated + resolved) |
| `vgi.example_queries` | JSON list of `{"description","sql"}` *illustrative* example queries |
| `vgi.executable_examples` | JSON list of self-contained, **must-run** examples (see below) |
| `vgi.agent_test_tasks` | JSON list of fixed analyst tasks `{name, prompt, reference_sql?, success_criteria?, check_sql?}` — the suite `vgi-lint simulate` runs (see below) |
| `vgi.title` | Human/marketing display name (vs. the machine name) |
| `vgi.keywords` | JSON array of search keywords / synonyms — `["a","b"]` (comma-separated string is now a **VGI138 error**) |
| `vgi.category` | The object's single **primary category** — a `name` defined in its schema's `vgi.categories` registry (on tables/views/functions; **VGI409**) |
| `vgi.categories` | Schema-level **ordered registry** of `{"name","description"?,"title"?}` category objects — the navigation sections a schema groups its objects into (**VGI408**) |
| `vgi.classification_tags` | JSON array of cross-cutting facet labels for search — on any object **except the catalog** (was `vgi.category_tags`) |
| `vgi.result_columns_md` | Markdown doc of a table function's returned columns (for dynamic schemas DuckDB can't expose) |
| `vgi.source_url` | Link to where the object is implemented (repo/file) |
| `vgi.author` | Author / maintainer attribution (catalog) |
| `vgi.copyright` | Copyright notice (catalog) |
| `vgi.license` | License name or SPDX identifier (catalog) |
| `vgi.support_contact` | Where to report issues/bugs — email or URL (catalog) |
| `vgi.support_policy_url` | Link to the support / SLA policy (catalog) |

> **Renamed:** `vgi.doc_llm`/`vgi.doc_md` (was `vgi.description_llm`/`_md`),
> `vgi.result_columns_md` (was `vgi.columns_md`), and `vgi.classification_tags`
> (was `vgi.category_tags`). The old keys still work (dual recognition) but
> **VGI405** nudges you to migrate; they'll stop being recognized in **v1.0**.

> **Categories** (`vgi.category` + `vgi.categories`) are **required** — they
> drive navigation, listing sections, and SEO descriptions. Every schema with
> objects must declare an ordered `vgi.categories` registry (**VGI413**) and tag
> every table/view/function with a `vgi.category` naming one of them (**VGI411**).
> **VGI408–412** enforce a valid registry, that every object's category is defined
> in it (an orphan is an error, with a *did-you-mean* hint), that each category is
> described, and that **no category is left empty** (a dead section is an error).
> Categories render as ordered, described sections in the worker listing (and
> `simulate`'s `list_categories` discovery tool).

`vgi.doc_llm`/`vgi.doc_md` are **required on the catalog, every schema, and
(under the strict default) every table, view, and function** — and validated
when set (minimum length, must differ from each other and from the object's own
description). The catalog `source_url` and keywords are enforced by the strict
default; `vgi.title` is required on the **catalog and schemas** only (optional
elsewhere, but validated when set); author/copyright/license are encouraged
(info). Relax any of this (e.g. back to optional docs on tables/views/functions)
via config.

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
`baseline`, `execute`, `spatial`, `format` (`terminal|json|agent|jsonl`),
`config`, `args`, `summary`. The action's `exit-code` is exposed as an output. The action ref `@v1`
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
