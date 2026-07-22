"""Configuration: rule selection, severity, tuning options, resolution order.

Configuration covers rule selection, per-rule severity, tuning options, and the
deterministic severity-resolution order rules depend on.

Precedence (lowest -> highest) is applied by the loader in ``cli.py``:
built-in defaults < pyproject.toml < dedicated file < CLI flags.
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .findings import Severity

if TYPE_CHECKING:
    from .model import ObjectId
    from .rules.base import Rule


def _default_execute_concurrency() -> int:
    """Default parallelism for execution rules: the machine's CPU count.

    Execution rules (VGI9xx) are I/O-bound on the worker — often a live API — so
    running example queries across this many cursors overlaps the round trips.
    Falls back to 4 when the CPU count can't be determined.
    """
    return os.cpu_count() or 4


@dataclass
class Options:
    """Rule tuning knobs (the ``[tool.vgi-lint-check.options]`` table)."""

    column_comment_min_ratio: float = 0.8
    min_llm_description_chars: int = 40
    min_md_description_chars: int = 80
    min_description_chars: int = 12
    # Catalog/schema are the worker's listing — their doc_llm/doc_md should be
    # detailed, well above the object-level floor.
    min_catalog_description_chars: int = 300
    # VGI106: the catalog `comment` (the one-line storefront blurb, distinct from
    # the long doc_llm/doc_md above) must read as a sentence, not a bare name.
    min_catalog_comment_chars: int = 40
    min_schema_description_chars: int = 160
    # VGI173: a catalog/schema description that names this many of the worker's
    # own objects (as code tokens) and covers at least this fraction of them is
    # treated as a redundant "manifest" — info an agent gets by listing the schema.
    enumeration_min_objects: int = 4
    enumeration_object_fraction: float = 0.5
    # VGI180: extra boilerplate phrases to flag, as regex (case-insensitive), on
    # top of the built-in families. Anchor with `[^.]*` rather than `.*` to keep
    # a match inside one sentence. Invalid patterns are skipped, never fatal.
    boilerplate_extra_patterns: list[str] = field(default_factory=list)
    # VGI182: DuckDB type names to exempt from the "must be backticked" check —
    # for a worker where a name doubles as domain vocabulary (e.g. MAP, DATE).
    type_format_ignore_names: list[str] = field(default_factory=list)
    # VGI328: parameterless function names treated as diagnostic scaffolding.
    # Matched whole (case-insensitive); `version`/`build_info` are matched
    # separately as a suffix and aren't listed here. Empty = only version fires.
    diagnostic_function_names: list[str] = field(
        default_factory=lambda: [
            "ping",
            "health",
            "healthcheck",
            "health_check",
            "heartbeat",
            "echo",
            "noop",
            "hello",
            "about",
            "debug",
            "status",
        ]
    )
    # Warn on a schema with more than this many objects (0 = disabled).
    max_schema_objects: int = 50
    # Flag a worker advertising more than this many catalogs (0 = disabled).
    max_catalogs: int = 100
    # Warn (never error) on a catalog with more than this many tables / functions
    # (0 = disabled). Generous so only genuinely excessive catalogs are flagged.
    max_tables: int = 500
    max_functions: int = 500
    # Warn on over-long table/function names (0 = disabled).
    max_table_name_length: int = 64
    max_function_name_length: int = 64
    # VGI142: redundant retrieval-verb prefixes on object names. A table/function
    # is already a queryable collection, so `list_`/`get_` restate what FROM/the
    # call already convey. Empty list disables the rule.
    redundant_name_prefixes: list[str] = field(default_factory=lambda: ["get_", "list_"])
    # VGI205/VGI315: identifier names to skip when checking that a column/argument
    # name maps to one SQL type catalog-wide (e.g. a generic 'id'/'value' that
    # legitimately means different things per object). Empty = check everything.
    type_consistency_ignore_names: list[str] = field(default_factory=list)
    # Required tags are opt-in: VGI401 only fires for keys you list here. There
    # is no universal tag every schema/table must carry.
    required_schema_tags: list[str] = field(default_factory=list)
    required_table_tags: list[str] = field(default_factory=list)
    allowed_tag_keys: list[str] = field(default_factory=list)
    # VGI015: catalog icon (vgi.icon_url) resolution/size budget, checked under
    # --check-links. min/max are the smaller/larger side in pixels; a tiny icon
    # looks blurry when scaled up and an oversized one is wasteful to ship.
    icon_min_dimension: int = 64
    icon_max_dimension: int = 2048
    icon_max_bytes: int = 1048576  # 1 MiB
    # Discoverability (VGI12x) tuning.
    min_meaningful_description_chars: int = 30
    min_example_queries: int = 3
    # Warn when one object declares more than this many executable examples
    # (each runs against the worker under --execute). 0 = disabled.
    max_executable_examples: int = 10
    # Free-form faceting keys for VGI123/132. "category" is intentionally absent:
    # the structured vgi.category + vgi.categories registry (VGI408-412) is the
    # controlled-vocabulary successor, so leaving it here would double-report.
    classifying_tag_keys: list[str] = field(default_factory=lambda: ["domain", "provider", "topic"])
    # A classifying tag should be a small, reused vocabulary. VGI132 warns when a
    # key has more than this many distinct values, or when none are reused.
    max_distinct_categories: int = 12
    # LLM doc-review (VGI180): an object whose mean doc-quality score (1-5) is
    # below this is flagged. Only fires under --doc-review.
    doc_quality_min: int = 3
    # Agent-suitability gate (VGI920): a simulate pass-rate below this fails the
    # run. Only fires under --agent-check.
    agent_pass_threshold: float = 0.8
    # Tutorials (VGI13xx). Asset git-size budget (bytes): per-file and per-tutorial
    # total (VGI1331). Similarity ceiling for anti-sameness (VGI1326): two tutorials
    # whose normalized prose is more similar than this are flagged as formulaic.
    tutorial_max_asset_bytes: int = 262144
    tutorial_max_assets_total_bytes: int = 2097152
    tutorial_similarity_max: float = 0.6
    # Title length window (VGI1320) and description window (VGI1321), in characters.
    tutorial_title_min: int = 30
    tutorial_title_max: int = 70
    tutorial_description_min: int = 120
    tutorial_description_max: int = 200


# Waiver taxonomy. A suppression is not one thing, and conflating the four
# hides the two that are actionable:
#
#   domain-exemption  the rule genuinely cannot apply to this worker (a
#                     passthrough connector whose tables are the user's).
#                     Permanent and legitimate — review on a rule change.
#   timing            an execution-window accommodation. Hides no finding;
#                     usually belongs in [execution], not in `ignore`.
#   tooling-bug       the rule is wrong / the linter misreports. This is a bug
#                     report; `vgi-lint fleet` collects these as the linter's
#                     own backlog instead of leaving them buried in TOML.
#   deferred          a real defect the author chose not to fix yet. Should
#                     carry `expires`.
WAIVER_KINDS = ("domain-exemption", "timing", "tooling-bug", "deferred", "unspecified")


@dataclass(frozen=True)
class Waiver:
    """One rule suppression, with the reasoning that justified it.

    Waivers are declared either as a bare code (``"VGI146"``) or as a table
    (``{code = "VGI146", reason = "...", kind = "domain-exemption"}``). The bare
    form keeps every existing config working and lands in ``unspecified``.
    """

    code: str
    reason: str = ""
    kind: str = "unspecified"
    expires: str | None = None  # ISO date (YYYY-MM-DD)
    scope: str | None = None  # None = catalog-wide; else the per-object glob

    @property
    def where(self) -> str:
        """Human label for the waiver's scope."""
        return self.scope or "catalog-wide"

    def problems(self) -> list[str]:
        """Structural complaints about the waiver itself (not about the rule)."""
        out = []
        if self.kind not in WAIVER_KINDS:
            out.append(f"unknown kind {self.kind!r} (expected one of {', '.join(WAIVER_KINDS)})")
        if self.kind == "unspecified":
            out.append("no reason/kind recorded — declare it as a table to keep the rationale")
        elif not self.reason.strip():
            out.append("kind declared without a reason")
        if self.expires:
            import datetime as _dt

            try:
                due = _dt.date.fromisoformat(self.expires)
            except ValueError:
                out.append(f"expires {self.expires!r} is not an ISO date (YYYY-MM-DD)")
            else:
                if due < _dt.date.today():
                    out.append(f"expired on {self.expires}")
        return out


@dataclass
class WaiverUsage:
    """A waiver plus what it actually suppressed this run (the audit result)."""

    waiver: Waiver
    suppressed: int = 0
    objects: list[str] = field(default_factory=list)
    # True when the waived rule's outcome depends on live session state (an
    # execution rule), so a single quiet pass is not evidence of anything.
    unconfirmed: bool = False

    @property
    def dead(self) -> bool:
        """True when the waiver demonstrably suppressed nothing — it can be deleted.

        Deliberately conservative. An execution rule's verdict depends on session
        state, ordering and timing — a catalog-level executable example can create
        a model that a later per-function example then binds against, so the same
        waiver looks live in one run and dead in the next. One quiet observation
        cannot tell "the rule no longer applies" from "this run happened to pass",
        and advising deletion on that basis deletes load-bearing waivers.

        So those are reported ``unconfirmed`` instead, and never counted as dead.
        """
        return self.suppressed == 0 and not self.unconfirmed


def _parse_waivers(raw: Any, scope: str | None = None) -> tuple[list[str], list[Waiver]]:
    """Split a mixed ignore list into plain codes plus their Waiver records.

    Returns ``(codes, waivers)``; ``codes`` preserves the existing string-list
    contract the selection logic already consumes, so behaviour is unchanged.
    """
    codes: list[str] = []
    waivers: list[Waiver] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            code = str(entry.get("code", "")).strip()
            if not code:
                continue
            codes.append(code)
            waivers.append(
                Waiver(
                    code=code,
                    reason=str(entry.get("reason", "") or ""),
                    kind=str(entry.get("kind", "") or "unspecified"),
                    expires=(str(entry["expires"]) if entry.get("expires") else None),
                    scope=scope,
                )
            )
        else:
            codes.append(str(entry))
            waivers.append(Waiver(code=str(entry), scope=scope))
    return codes, waivers


@dataclass
class Config:
    """Resolved lint configuration: selection, severities, and tuning options."""

    select: list[str] = field(default_factory=lambda: ["ALL"])
    ignore: list[str] = field(default_factory=list)
    # extend_* compose with select/ignore so a CLI --select (which replaces
    # `select`) does not silently discard config-level additions.
    extend_select: list[str] = field(default_factory=list)
    extend_ignore: list[str] = field(default_factory=list)
    categories: list[str] | None = None  # None = all categories
    severity_overrides: dict[str, Severity] = field(default_factory=dict)
    per_object: dict[str, list[str]] = field(default_factory=dict)  # glob -> [codes]
    options: Options = field(default_factory=Options)
    # Every suppression above, with its declared rationale. Parallel to
    # ignore/extend_ignore/per_object (which still drive selection) — this is the
    # auditable view: what was waived, why, and by whom.
    waivers: list[Waiver] = field(default_factory=list)
    # --audit-waivers: run the waived rules anyway (collecting, not reporting,
    # their findings) so a waiver that suppresses nothing can be identified.
    audit_waivers: bool = False

    # runtime / behavioural
    execute: bool = True  # run execution rules (VGI9xx); --no-execute opts out
    execute_mode: str = "explain"
    execute_limit: int = 1
    execute_timeout: float = 30.0  # per-query wall-clock cap (seconds; 0 = off)
    # Run example queries across N cursors (worker pool). Defaults to the CPU
    # count so I/O-bound execution rules overlap their worker round trips; set
    # lower for a rate-limited worker via --execute-concurrency / config.
    execute_concurrency: int = field(default_factory=_default_execute_concurrency)
    slow_example_seconds: float = 5.0  # warn on executable examples slower than this (0 = off)
    # VGI911/VGI912 scan probe: `SELECT * FROM <obj> LIMIT scan_limit`, run on a
    # disposable cursor under its own (shorter) cap. A worker wedged inside its
    # first batch ignores interrupt(), so this timeout is the only backstop.
    scan_limit: int = 10
    scan_timeout: float = 10.0
    # VGI912 batch-shape thresholds, read from the vgi extension's EXPLAIN ANALYZE
    # `Batches` / `Batch Bytes` extra_info. A producer that returns its whole
    # result in one batch defeats LIMIT push-down and forces the HTTP transport to
    # buffer the entire result set.
    single_batch_max_rows: int = 100_000  # batches == 1 and rows > this
    avg_batch_max_rows: int = 300_000  # mean rows per batch above this
    max_batch_bytes: int = 64 * 1024 * 1024  # mean bytes per batch above this
    sample_size: int = 100  # VGI810/VGI811: rows/values sampled per constraint probe
    sample_timeout: float = 10.0  # VGI810/VGI811: per-query cap (shorter than execute_timeout)
    # Agent-suitability run (--agent-check / VGI920). None means "inherit the
    # execution window": it is the same worker paying the same cold-start cost, so
    # a repo that already declared it is slow should not have to say so twice —
    # otherwise a worker configured correctly for L2 fails L3 for a reason that
    # has nothing to do with agent usability. Set explicitly under [simulate].
    sim_timeout: float | None = None
    sim_concurrency: int | None = None
    sim_attempts: int = 1
    sim_max_steps: int = 12
    check_links: bool = False  # enable network rules (validate description URLs)
    link_timeout: float = 10.0
    # LLM passes (use the local `claude -p` subscription backend by default).
    doc_review: bool = False  # VGI180: LLM doc-quality review (requires_review rules)
    agent_check: bool = False  # VGI920: run `simulate` + gate (requires_agent rules)
    ai_backend: str = "claude"  # claude (subscription CLI) | api
    ai_model: str | None = None
    ai_concurrency: int = 4  # parallel LLM batches/tasks for the doc-review pass
    # Content-hash verdict cache for the LLM passes (shared with `review`/`simulate`),
    # so unchanged docs/tasks aren't re-judged on a re-run. --no-ai-cache disables it.
    ai_cache: bool = True
    fail_on: Severity = Severity.ERROR
    location: str | None = None
    baseline: str | None = None
    trace: str | None = None  # when set, write a per-phase / per-rule timing log here
    # Keep the subprocess worker pool warm this many seconds by raising the vgi
    # extension's vgi_worker_pool_idle_limit_seconds (SET before ATTACH). The pool
    # reaps idle workers after 5s by default, so slow phases (LLM passes, the
    # simulate ReAct loop) get the worker reaped and cold-start it again; a run-long
    # keepalive avoids that. 0 = leave the extension default (5s).
    worker_idle_timeout: int = 300
    # Warn when cumulative subprocess-worker (re)launch time in a run exceeds this,
    # nudging fleet/CI users toward a persistent transport (0 = off).
    relaunch_warn_seconds: float = 6.0
    # Extra ATTACH options + pre-attach setup SQL, for workers that require
    # options/credentials to attach (e.g. PROVIDER/SECRET). attach_options render
    # into the ATTACH statement; setup_sql runs on the connection before ATTACH
    # (e.g. CREATE SECRET). Static metadata linting needs no live connection, so
    # a placeholder secret name is enough to satisfy a deferred-credential attach.
    attach_options: dict[str, str] = field(default_factory=dict)
    setup_sql: list[str] = field(default_factory=list)

    # ---- rule selection / severity resolution ----------------------------
    @property
    def _selectors(self) -> list[str]:
        return self.select + self.extend_select

    @property
    def _ignorers(self) -> list[str]:
        return self.ignore + self.extend_ignore

    def _code_matches(self, code: str, patterns: list[str]) -> bool:
        return any(p.upper() == "ALL" or fnmatch.fnmatch(code, p) for p in patterns)

    def effective_severity(
        self, rule: Rule | type[Rule], *, lift_waivers: bool = False
    ) -> Severity:
        """Resolve a rule's severity per the documented order. OFF = disabled.

        ``lift_waivers`` skips only the ``ignore``/``extend_ignore`` gate — every
        other reason a rule is off (no connection, no LLM pass, deselected) still
        applies. The waiver audit uses it to run exactly the rules a waiver
        silenced, and nothing else.
        """
        # 1. execution rules need --execute; network rules need --check-links;
        #    LLM rules need --doc-review / --agent-check.
        if getattr(rule, "requires_connection", False) and not self.execute:
            return Severity.OFF
        if getattr(rule, "requires_network", False) and not self.check_links:
            return Severity.OFF
        if getattr(rule, "requires_review", False) and not self.doc_review:
            return Severity.OFF
        if getattr(rule, "requires_agent", False) and not self.agent_check:
            return Severity.OFF
        # 2. category gate
        if self.categories is not None and str(rule.category) not in self.categories:
            return Severity.OFF
        # 3. select / ignore globs (select and extend-select compose)
        if not self._code_matches(rule.code, self._selectors):
            return Severity.OFF
        if not lift_waivers and self._code_matches(rule.code, self._ignorers):
            return Severity.OFF
        # 4. explicit per-rule override, else the rule default
        return self.severity_overrides.get(rule.code, rule.default_severity)

    def sim_limits(self) -> Any:
        """Build the ``simulate`` bounds for ``--agent-check`` from this config.

        Timeout and concurrency fall back to the execution window rather than to
        ``SimLimits``' own defaults, because the agent run drives the same worker
        through the same cold start as the execution rules do.
        """
        from .simulate import SimLimits

        return SimLimits(
            timeout=self.sim_timeout if self.sim_timeout is not None else self.execute_timeout,
            concurrency=(
                self.sim_concurrency
                if self.sim_concurrency is not None
                else self.execute_concurrency
            ),
            attempts=self.sim_attempts,
            max_steps=self.sim_max_steps,
        )

    def is_waived(self, rule: Rule | type[Rule]) -> bool:
        """True when this rule is off *only* because a catalog-wide waiver hides it."""
        return self.effective_severity(rule) is Severity.OFF and (
            self.effective_severity(rule, lift_waivers=True) is not Severity.OFF
        )

    def unknown_selectors(self, known_codes: Iterable[str]) -> list[str]:
        """Selector/ignore globs that match no known rule code (likely typos)."""
        known = list(known_codes)
        unknown = []
        for pat in self._selectors + self._ignorers + list(self.severity_overrides):
            if pat.upper() == "ALL":
                continue
            if not any(fnmatch.fnmatch(code, pat) for code in known):
                unknown.append(pat)
        return sorted(set(unknown))

    def is_object_ignored(self, object_id: ObjectId, code: str) -> bool:
        """True when ``code`` is suppressed for ``object_id`` by a per-object rule."""
        qualified = object_id.qualified()
        for glob, codes in self.per_object.items():
            if fnmatch.fnmatch(qualified, glob) and (not codes or self._code_matches(code, codes)):
                return True
        return False


# --------------------------------------------------------------------------
# Loading / merging
# --------------------------------------------------------------------------
def _coerce_options(raw: dict[str, Any]) -> Options:
    opts = Options()
    for k, v in (raw or {}).items():
        key = k.replace("-", "_")
        if hasattr(opts, key):
            setattr(opts, key, v)
    return opts


def from_table(raw: dict[str, Any]) -> Config:
    """Build a Config from a parsed ``[tool.vgi-lint-check]`` table."""
    raw = {k.replace("-", "_"): v for k, v in (raw or {}).items()}
    cfg = Config()
    if "select" in raw:
        cfg.select = list(raw["select"])
    if "ignore" in raw:
        cfg.ignore, waivers = _parse_waivers(raw["ignore"])
        cfg.waivers.extend(waivers)
    if "extend_select" in raw:
        cfg.extend_select = list(raw["extend_select"])
    if "extend_ignore" in raw:
        cfg.extend_ignore, waivers = _parse_waivers(raw["extend_ignore"])
        cfg.waivers.extend(waivers)
    if "categories" in raw:
        cfg.categories = list(raw["categories"])
    if "fail_on" in raw:
        cfg.fail_on = Severity.parse(raw["fail_on"])
    if "location" in raw:
        cfg.location = raw["location"]
    if "baseline" in raw:
        cfg.baseline = raw["baseline"]
    if "attach_options" in raw and isinstance(raw["attach_options"], dict):
        cfg.attach_options = {str(k): str(v) for k, v in raw["attach_options"].items()}
    if "setup_sql" in raw:
        val = raw["setup_sql"]
        cfg.setup_sql = [str(val)] if isinstance(val, str) else [str(s) for s in val]
    if "check_links" in raw:
        cfg.check_links = bool(raw["check_links"])
    if "link_timeout" in raw:
        cfg.link_timeout = float(raw["link_timeout"])
    if "worker_idle_timeout" in raw:
        cfg.worker_idle_timeout = int(raw["worker_idle_timeout"])
    if "relaunch_warn_seconds" in raw:
        cfg.relaunch_warn_seconds = float(raw["relaunch_warn_seconds"])
    if "severity" in raw and isinstance(raw["severity"], dict):
        cfg.severity_overrides = {
            code: Severity.parse(level) for code, level in raw["severity"].items()
        }
    if "per_object" in raw and isinstance(raw["per_object"], dict):
        for glob, spec in raw["per_object"].items():
            codes, waivers = _parse_waivers(spec.get("ignore", []), scope=glob)
            cfg.per_object[glob] = codes
            cfg.waivers.extend(waivers)
    if "options" in raw:
        cfg.options = _coerce_options(raw["options"])
    if "execution" in raw and isinstance(raw["execution"], dict):
        ex = raw["execution"]
        cfg.execute = bool(ex.get("enabled", cfg.execute))
        cfg.execute_mode = ex.get("mode", cfg.execute_mode)
        cfg.execute_limit = int(ex.get("limit", cfg.execute_limit))
        cfg.execute_timeout = float(ex.get("timeout", cfg.execute_timeout))
        cfg.execute_concurrency = int(ex.get("concurrency", cfg.execute_concurrency))
        cfg.slow_example_seconds = float(ex.get("slow_seconds", cfg.slow_example_seconds))
        cfg.scan_limit = int(ex.get("scan_limit", cfg.scan_limit))
        cfg.scan_timeout = float(ex.get("scan_timeout", cfg.scan_timeout))
        cfg.single_batch_max_rows = int(ex.get("single_batch_max_rows", cfg.single_batch_max_rows))
        cfg.avg_batch_max_rows = int(ex.get("avg_batch_max_rows", cfg.avg_batch_max_rows))
        cfg.max_batch_bytes = int(ex.get("max_batch_bytes", cfg.max_batch_bytes))
        cfg.sample_size = int(ex.get("sample_size", cfg.sample_size))
        cfg.sample_timeout = float(ex.get("sample_timeout", cfg.sample_timeout))
    if "simulate" in raw and isinstance(raw["simulate"], dict):
        sim = raw["simulate"]
        if "timeout" in sim:
            cfg.sim_timeout = float(sim["timeout"])
        if "concurrency" in sim:
            cfg.sim_concurrency = int(sim["concurrency"])
        cfg.sim_attempts = int(sim.get("attempts", cfg.sim_attempts))
        cfg.sim_max_steps = int(sim.get("max_steps", cfg.sim_max_steps))
    return cfg


def load_config(
    explicit_path: str | Path | None = None,
    start_dir: str | Path | None = None,
) -> Config:
    """Discover and parse config.

    Looks at an explicit file first, then ``vgi-lint.toml`` and
    ``pyproject.toml`` (``[tool.vgi-lint-check]``) from ``start_dir`` upward.
    Returns built-in defaults when nothing is found.
    """
    path = _discover_path(explicit_path, start_dir)
    if path is None:
        return Config()
    data = tomllib.loads(Path(path).read_text())
    if path.name == "pyproject.toml":
        table = data.get("tool", {}).get("vgi-lint-check", {})
    else:
        table = data.get("tool", {}).get("vgi-lint-check", data)
    return from_table(table)


def _discover_path(explicit_path: str | Path | None, start_dir: str | Path | None) -> Path | None:
    if explicit_path:
        return Path(explicit_path)
    start = Path(start_dir or Path.cwd()).resolve()
    for d in [start, *start.parents]:
        for name in ("vgi-lint.toml", "pyproject.toml"):
            candidate = d / name
            if candidate.is_file():
                if name == "pyproject.toml":
                    data = tomllib.loads(candidate.read_text())
                    if "vgi-lint-check" not in data.get("tool", {}):
                        continue
                return candidate
    return None
