# CLAUDE.md

Guidance for working in **vgi-lint-check** (CLI: `vgi-lint`) — a `pydoclint`-style
metadata-quality linter for VGI (Vector Gateway Interface) DuckDB-extension
workers. It attaches to a worker as a black box and grades only what surfaces
post-attach through DuckDB system tables, so it is language-agnostic about the
worker's implementation.

## Dev workflow & gates

`uv` manages everything. `haybarn` (the DuckDB client) ships **release candidates
only**, so `prerelease = "allow"` is set — use `uv`, not bare `pip`.

Run the full gate sweep before considering any change done — all must pass:

```bash
uv run ruff format src tests          # format (then check)
uv run ruff check src tests           # lint: E,F,I,UP,B,SIM,D (google docstrings)
uv run mypy                           # strict; files = src/vgi_lint_check
uv run pydoclint src/vgi_lint_check   # google style, docstring/signature agreement
uv run pytest -q                      # unit suite (live tests skipped by default)
uv run python scripts/gen_rules_doc.py --check   # RULES.md must not drift
```

- Line length **100**, double-quoted strings.
- Rule modules are exempt from docstring rules (`rules/**` ignores D101/2/3/6) —
  rule classes are self-documenting via their `code`/`name`/`summary`/`targets`.
- **Live tests** attach to a real worker and are skipped unless you opt in:
  `VGI_LINT_LIVE=1 uv run pytest -m live`. Keep all other tests offline (fake
  backends + canned DB results — see `tests/unit/test_simulate.py`,
  `tests/fixtures.py`).

## Architecture

Pipeline: **connect → load → model → rules/engine → reporting.**

- `connection.py` / `core.py` — attach the worker (`ATTACH … (TYPE vgi, LOCATION
  …)`), keep the connection open where needed (`with_attached_catalog`), DETACH/close.
  v1 supports **local subprocess** and **no-auth HTTP** workers only; `LOCATION`
  is executed as a command — a documented trust boundary, never attacker-controlled.
  Teardown goes through **`close_quietly`**, never a bare `con.close()`: a worker
  scan wedged inside its first batch is uncancellable, and `close()` blocks on it
  forever (see the VGI911 note below).
- `loader.py` — reads DuckDB system tables + decodes reserved `vgi.*` tags into the model.
- `model.py` — the immutable `Catalog`/`Schema`/`Table`/`Column`/`Function`/… graph
  plus `iter_*` accessors. Reserved tag keys live here (`TAG_*` constants +
  `RESERVED_TAG_KEYS`).
- `tags.py` — defensive JSON decoders for reserved tags. Each returns
  `(value, parse_error)` and never raises; a malformed tag becomes a lintable finding,
  not a crash.
- `rules/` — one module per family; `base.Rule` subclasses self-register via
  `@register` (`rules/registry.py`), imported in `rules/__init__.py`. `engine.py`
  resolves severities from config and runs enabled rules.
- `reporting/`, `findings.py`, `scoring.py`, `baseline.py` — output formats
  (terminal grouped-by-rule, `agent`, `json`/`jsonl`), scoring, per-data-version baselines.
- `sql_parse.py` / `corpus.py` — **parse-based coverage.** `sql_parse.parse_refs`
  extracts the objects a SQL statement actually *calls* via DuckDB's built-in
  `json_serialize_sql` (a private in-memory connection — offline, no worker attach, no
  community extension); it is defensive (returns `None` on unparseable SQL, never raises).
  Crucially it sees **table-functions in `FROM`** (a `FUNCTION` node), which a regex —
  and the `parser_tools` community extension — cannot; that blind spot is why we build on
  `json_serialize_sql`, not `parser_tools`. `corpus.compute_corpus_coverage` resolves those
  refs against the worker surface to report which objects are **demonstrated** (called by an
  example), **tested** (exercised by a `vgi.agent_test_tasks` task), and which
  worker-qualified references are **broken** (resolve to nothing). Shared by the VGI5xx
  coverage rules (511 undemonstrated / 512 example-ref-resolves / 513 macro-on-input /
  520 untested / 521 test-ref-resolves) **and** by `scoring.py` (the `example_coverage`
  and `test_coverage` families) so a finding and the score never disagree. Rules read it
  once via `RuleContext.corpus_coverage()` (memoized per run).
- `review.py` — LLM-as-judge doc review (`ReviewBackend`, `make_backend`,
  `ReviewCache`). Backend is single-shot `complete(prompt) -> str`; default is the
  local `claude -p` CLI (runs on the user's subscription), `api` uses Anthropic API.
  **`claude -p` is an agent, not a completion endpoint** — left alone it loads Claude
  Code's tool schemas, MCP servers, settings and the cwd's CLAUDE.md on *every* call
  (~20.6k input tokens, re-sent on each `--resume` turn too). `ClaudeCliBackend` strips
  that back to a plain completion (`--tools "" --strict-mcp-config --mcp-config
  '{"mcpServers":{}}' --setting-sources "" --system-prompt …`) and pins
  `DEFAULT_CLI_MODEL` (the interactive default is a premium 1M-context tier). Measured
  on `vgi-units`: `--ai` went 694k → 53k input tokens, $1.67 → $0.29. The prompt rides on
  **stdin**, never argv — `--tools` is variadic and swallows a positional prompt.
  `VGI_LINT_AI_INHERIT_CONTEXT=1` opts back into the full agent context (~100x cost, and
  verdicts then depend on the directory the lint runs from).
  Verdict caches are salted with `backend_fingerprint()` (model + `PROMPT_REVISION`), so
  changing model or editing the rubric/actor prompt misses the cache instead of silently
  reusing another judge's verdicts — **bump `PROMPT_REVISION` when you touch a prompt.**
- `simulate.py` — agent-suitability testing (see below).
- `tutorials/` + `rules/tutorials.py` — the **tutorials** subsystem: executable
  `.vgi.md` worker tutorials, linted / executed / rendered / LLM-planned (see below).
  File-sourced, so it runs on a **parallel engine**, not the catalog `RuleContext` loop.
- `cli.py` — `click` app; subcommands `lint` (default), `review`, `simulate`, `rules`,
  `explain`, `init`, `versions`, and the `tutorials` group
  (`lint` / `verify` / `build` / `suggest` / `init`).

## Adding / changing a rule

1. Add a `Rule` subclass in the right `rules/<family>.py` with a unique `code`
   (`VGI<family><n>`), `name`, `summary`, `default_severity`, `targets`, and `check()`.
2. Decorate with `@register` (collision-detected at import).
3. **Regenerate the docs:** `uv run python scripts/gen_rules_doc.py` (the `--check`
   variant is a CI gate — RULES.md is generated, never hand-edited).
4. Add a test in `tests/unit/` (mirror an existing family test; use `tests/fixtures.py`).

Rule families: VGI0xx catalog, VGI1xx descriptions/discoverability, VGI17x content,
VGI2xx columns, VGI3xx functions, VGI4xx tags, VGI5xx examples, VGI6xx settings,
VGI7xx pragmas, VGI8xx constraints, VGI9xx execution, VGI10xx attach options,
VGI11x structure, **VGI13xx tutorials** (see below — a *separate* registry). The
profile is **strict by default**; users opt out via `[tool.vgi-lint-check]`
`ignore`/`severity`.

## Scan probes (VGI911 / VGI912)

`SELECT * FROM <relation> LIMIT n` against every table, view, and (via an example's
binding call) table function. One probe per relation, memoized on `RuleContext`
and shared by both rules — `execution.scan_probes(ctx)`.

- **VGI911 scan-responds** (error) — the scan must return within
  `execution.scan_timeout`. Catches a hanging/unbounded producer.
- **VGI912 scan-batch-shape** (warning) — reads the vgi extension's `Batches` /
  `Batch Bytes` `extra_info` off the `TABLE_SCAN` node of
  `get_profiling_information()`. These count RecordBatches **as they came off the
  wire**, before DuckDB re-slices to `STANDARD_VECTOR_SIZE`, so they are the only
  view of the worker's own chunking. Parse `extra_info`, never the `EXPLAIN
  ANALYZE` text — its fixed-width box wraps the value across lines. Requires the
  extension at ≥ `f38b138`; a missing `Batches` key just means no finding.

**Two hard constraints, both verified against a live worker:**

1. `con.interrupt()` only takes effect **between** a scan's batch emissions. A
   worker blocked inside its first batch cannot be cancelled at all, and the
   cursor running it is unusable forever — `cur.close()` on it *blocks*.
2. Therefore probes run through `_util.map_isolated_queries`, which gives each
   item a **fresh, disposable cursor** and abandons (never closes) a wedged one.
   `_run_probe` swallows `QueryTimeout` into `ScanProbe.timed_out`, so it must
   report that back via the `wedged=` predicate or the cursor gets closed and the
   run hangs. Never probe on the parent connection (`map_queries` does when
   `concurrency <= 1`) — a wedge there poisons every later rule.

Thresholds (`[execution]`): `scan_limit`, `scan_timeout`, `single_batch_max_rows`
(fires only when `batches == 1`), `avg_batch_max_rows`, `max_batch_bytes`.

**Tutorial rules are different:** they subclass `TutorialRule` in `rules/tutorials.py`,
register via `@register_tutorial` into a dedicated `TUTORIAL_REGISTRY` (so they never
leak into `vgi-lint lint` and worker rules never leak into `tutorials lint`), and run
against a `TutorialContext` via `run_tutorial_rules` (not `RuleContext`/`engine.run`).
They still reuse `Finding` + `config.effective_severity` (so `requires_connection`
rules are gated by `tutorials verify --execute` and `requires_review` by `--judge`).
`gen_rules_doc` merges both registries, so VGI13xx still lands in RULES.md.

## `simulate` (agent-suitability testing)

`vgi-lint simulate` runs an LLM "analyst" through a worker's **fixed** task suite,
declared in the catalog tag `vgi.agent_test_tasks` (decoded to `AgentTask`).

- **Tool-mediated discovery (do not regress this):** the analyst is given only a
  bounded `build_listing()` (names + one-liners) and discovers detail through tools
  that mirror the production "ask AI" contract — `tool_list_tables`,
  `tool_describe_table`, `tool_describe_function`, `tool_run_sql` — answered from the
  Catalog model + live cursor (no MCP server). The ReAct loop in `run_task` dispatches
  these and records a discovery trail.
- **No-leak invariant (load-bearing, unit-tested):** only a task's `prompt` ever
  reaches the actor. `reference_sql` / `check_sql` / `success_criteria` are
  grader-only — the listing and every tool output must exclude them.
- **Layered, execution-based grading** (`grade_task`, strongest wins): Tier 1
  reference-result compare (deterministic; `_resultsets_equal` is **strict by
  default** on column names + values + order, with per-task `unordered` /
  `ignore_column_names` opt-outs); Tier 2 `check_sql` over post-session state; Tier 3
  LLM judge against `success_criteria`. Friction is always extracted.
- **Path scoring (the discoverability signal).** `run_task` records the full
  trajectory (`TaskRun.discovery` trace + `TaskStep.error_kind` + `hit_ceiling`);
  `compute_path_metrics` turns it into a `PathMetrics` score that penalizes *wasted*
  effort (bind errors, mandatory-filter trial-and-error via `is_filter_policy_error`,
  redundant `describe_table`, not-found lookups, non-convergence) — **not** raw step
  count, so complexity isn't punished. `build_suggestions` maps each fault to a
  concrete metadata fix. `SimReport.discoverability` (mean path score) is reported
  alongside pass-rate; `.suggestions` is the deduped fix list. Design intent: a task
  that *passes but scores low* means the worker's metadata, not the task, is the
  problem — fix the worker (add an example / tighten docs) rather than raise
  `--attempts`, which only masks it.
- **Coverage + parallelism.** `compute_coverage(catalog)` statically reports which
  worker **objects** — functions *and* tables/views (`_unique_objects`) — the suite's
  `reference_sql`/`check_sql` exercise vs. miss (in `SimReport.coverage`, rendered as
  `object coverage N/M`). Counting tables is load-bearing for table-centric workers
  (a geodata worker whose surface is all tables would otherwise read 0/1). `suggest_tasks` is
  coverage-driven (sizes the suite to the worker, not a fixed N). `simulate_tasks`
  judges cache-miss tasks in parallel via `ThreadPoolExecutor` (`SimLimits.concurrency`,
  default 4) — each task already uses its own `con.cursor()`, results reassemble in
  declaration order, and the cache is written on the main thread (no lock).
- **Strict-grading caveat for complex tasks:** open-ended "which X has the most"
  questions can have defensible-but-divergent correct answers (e.g. spatial bbox
  overlap-vs-containment) and are unsuitable for strict reference grading — scope such
  tasks to a single unambiguous answer (e.g. a *named* entity). A hard task that needs
  `--attempts 2` usually means a missing worked example in the worker.
- **Safety:** `rules/_util.safe_session_sql` allows read-only / session-local SQL
  (SELECT/WITH/EXPLAIN/SET/PRAGMA/TEMP DDL) and blocks anything escaping the
  disposable session (INSERT/UPDATE/DELETE/ATTACH/INSTALL/LOAD/COPY-TO/multi-statement).
  Blocked statements surface as friction, never execute.
- `--suggest` is an authoring helper (propose tasks) — coverage-driven and **batched**:
  `suggest_tasks` loops over small batches of uncovered objects, recomputing coverage from
  the proposals each round, so each LLM call stays small and it never hits the backend's
  180s timeout on large catalogs (the failure mode of a single mega-prompt).
- `--verify-references` (`verify_references` / `render_verify`) runs each task's
  `reference_sql` N×3 and flags error / non-deterministic / empty references — an
  authoring/CI gate (exit 2) that productizes the manual probe step and catches the
  unsound-reference class (random/unseeded, typos) before a flaky graded run. It does not
  judge agent reproducibility (that's the simulation).
- **Sessions.** The ReAct loop runs over a `review.Conversation` (`make_conversation`).
  The claude backend's `_ClaudeSession` sets `--session-id <uuid>` on turn 1 and
  `--resume <uuid>` after, so only the delta (the latest tool result) is sent each turn
  rather than re-transmitting the whole transcript (`SimLimits.sessions`, `--session/
  --no-session`, default on; ~24% faster on a small suite, more as turns grow). The API
  backend accumulates the message list; any `complete()`-only backend (e.g. the test
  fake) falls back to `_ResendConversation`, which reproduces the original re-send
  behavior. Each task gets its own session id, so parallel tasks don't collide.

## `tutorials` (executable worker tutorials)

Workers publish **tutorials** as `tutorials/*.vgi.md` files in their own repo (NOT
catalog tags). `vgi-lint-check` lints, executes, renders, and LLM-plans them.

- **Format.** CommonMark + YAML front-matter + SQL fences annotated in the info
  string: ` ```sql {role=setup|step|teardown|illustrative expect=rows|scalar|error|empty} `
  with an adjacent ` ```result ``` ` block for the pinned expected output. One shared
  DuckDB session (one cursor) across a tutorial's steps. Required front-matter set (error
  on missing): title, worker(s), description, slug, keywords, difficulty, est_minutes,
  dataset, datePublished, dateModified, tier (quickstart|recipe|composition). A worker's
  suite is defined by a **hub** `tutorials/index.vgi.yaml` (series order + cross-links; the
  renderer injects prev/next — authors don't hand-wire links). Small static assets
  (`assets:`, kind data|image|media) are declared, size-budgeted, and embedded as data URIs.
- **Composability convention (enforced by VGI1313):** tutorial SQL must **not**
  `SET search_path` and must be **fully-qualified** (`cat.schema.fn(...)`), so each step
  is self-contained and multi-worker compositions work. The runner attaches the worker via
  the front-matter directive — tutorials usually need no `role=setup` block.
- **Feature DuckDB (VGI1327):** these are DuckDB-native tutorials, so the title or keywords
  should say "DuckDB" (or "SQL") — the target query is "how to X in DuckDB". The planner
  prompts and `init` scaffold bake this in; `~/Development/vgi-tutorial-plans.yaml` is 100% so.
- **Package.** `tutorials/` = `model` (frozen dataclasses), `fences` (the bespoke
  `{role= expect=}` parser — defensive, never raises), `frontmatter`/`loader` (never raise;
  problems → `parse_error`/`fm_errors`), `hub`, `jsonld`, `render` (self-contained HTML +
  schema.org JSON-LD; wasm "Run" is progressive enhancement, disabled unless `--wasm-endpoint`
  and the doc is wasm-safe), `runner` (one cursor, `safe_session_sql` gate, string result
  compare), `wasm`, `scaffold`, `suggest`.
- **Commands.**
  - `tutorials lint PATHS…` — static VGI13xx, fully offline. `--judge` adds the LLM
    narrative review (VGI1370). Reuses the shared select/ignore/severity config.
  - `tutorials verify PATHS… --location LOC` (or `--worker-location WORKER=LOC` for a
    composition) — attaches the worker(s) via `core.with_attached_catalogs` (multi-worker,
    one connection, `ExitStack`), runs each step, and checks the pinned results (conn rules
    VGI1340 refs-resolve / 1341 runs / 1342 matches / 1343 slow).
  - `tutorials build PATHS… --out DIR [--base-url … --wasm-endpoint … --open]` — renders
    self-contained HTML (a suite + hub page when an `index.vgi.yaml` is present).
  - `tutorials suggest LOCATION [--fleet FILE --cap N]` — LLM planner (see below).
  - `tutorials init --worker … --slug … [--tier …]` — scaffold a compliant skeleton;
    `--draft --job "…" --location LOC` LLM-drafts one instead.
- **LLM planner (`tutorials/suggest.py`).** `suggest_tutorials` mirrors
  `simulate.suggest_tasks` — coverage-driven batches of uncovered catalog objects, real
  function names, `{slug,title,keyword,job,tier,functions,with}`. It's **single-worker**, so
  compositions need a **fleet index** (`--fleet FILE`, `{catalog_id: one-liner}`); a
  **dedicated `_COMPOSE` round** then proposes cross-worker `composition` tutorials (coverage
  batching never volunteers one). `init --draft` uses `_DRAFT`. All prompts use `str.format`
  with escaped `{{ }}`.
- **Gotchas.** An unquoted colon in a front-matter *value* breaks YAML (caught as VGI1300).
  `TIMESTAMPTZ` renders in the session's local zone, not UTC — format explicitly
  (`strftime(ts AT TIME ZONE 'America/New_York', …)`) and `ROUND` floats so pinned results are
  stable. `verify` catches these as VGI1342 mismatches. Tutorial tests live in
  `tests/unit/test_tutorials.py` (fake cursor + fake backend for the conn/LLM paths).

## Conventions & gotchas

- Reserved-tag defaults must be **opt-in** unless near-universal (the user rejected
  required provider/domain tags). Don't make a new rule strict-by-default without cause.
- `_JUDGE`/`_SUGGEST` prompt templates use `str.format()` — escape literal braces as `{{ }}`.
- `model.py` is large; read targeted sections rather than the whole file.

## Release process (PyPI via Trusted Publishing)

1. Bump `version` in `pyproject.toml`.
2. Run all gates green.
3. Commit + push to `main`.
4. `gh release create vX.Y.Z --title vX.Y.Z --notes "…"` — `publish.yml` triggers on
   the published release and uploads to PyPI. Tags/releases are named `vX.Y.Z`.

Commit messages end with:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

## Cross-repo

- `~/Development/vgi-units` — a Rust VGI worker used for live validation; carries the
  `vgi.agent_test_tasks` suite in `crates/units-worker/src/main.rs`. Build with the
  pinned toolchain: `rustup run 1.90.0 cargo build --release -p units-worker`
  (and `cargo fmt` — CI gates on it). Run live: `VGI_LINT_LIVE=1 uv run vgi-lint
  simulate ~/Development/vgi-units/target/release/units-worker --no-cache`.
- `~/Development/vgi-web-frontend/src/lib/ai-agent.ts` — the production "ask AI" tool
  contract that `simulate`'s discovery tools mirror.
- `~/Development/vgi-tutorial-plans.yaml` — reusable 25-worker tutorial plan cache
  (topics/tiers/keywords/compositions); feed it to `tutorials/index.vgi.yaml` hubs and
  `tutorials init --draft --job`.
- `~/Development/vgi-fleet.yaml` — `{catalog_id: one-liner}` index of the worker fleet,
  passed to `tutorials suggest --fleet` so the planner can propose cross-worker compositions.
- Example tutorials live in `examples/tutorials/` (a `calendar/` suite + hub + single
  tutorials); `tutorials verify` them live against `~/Development/vgi-units/target/release/units-worker`
  or the calendar worker (`sh -c 'cd ~/Development/vgi-calendar && exec uv run calendar_worker.py'`).
