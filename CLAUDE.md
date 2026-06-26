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
- `review.py` — LLM-as-judge doc review (`ReviewBackend`, `make_backend`,
  `ReviewCache`). Backend is single-shot `complete(prompt) -> str`; default is the
  local `claude -p` CLI (runs on the user's subscription), `api` uses Anthropic API.
- `simulate.py` — agent-suitability testing (see below).
- `cli.py` — `click` app; subcommands `lint` (default), `review`, `simulate`, `rules`,
  `explain`, `init`, `diff`, …

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
VGI11x structure. The profile is **strict by default**; users opt out via
`[tool.vgi-lint-check]` `ignore`/`severity`.

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
- `--suggest N` is an authoring helper (propose tasks); generation is authoring-time,
  the suite is fixed for testing.

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
