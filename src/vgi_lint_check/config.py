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
    min_schema_description_chars: int = 160
    # VGI173: a catalog/schema description that names this many of the worker's
    # own objects (as code tokens) and covers at least this fraction of them is
    # treated as a redundant "manifest" — info an agent gets by listing the schema.
    enumeration_min_objects: int = 4
    enumeration_object_fraction: float = 0.5
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
    sample_size: int = 100  # VGI810/VGI811: rows/values sampled per constraint probe
    sample_timeout: float = 10.0  # VGI810/VGI811: per-query cap (shorter than execute_timeout)
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

    def effective_severity(self, rule: Rule | type[Rule]) -> Severity:
        """Resolve a rule's severity per the documented order. OFF = disabled."""
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
        if self._code_matches(rule.code, self._ignorers):
            return Severity.OFF
        # 4. explicit per-rule override, else the rule default
        return self.severity_overrides.get(rule.code, rule.default_severity)

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
        cfg.ignore = list(raw["ignore"])
    if "extend_select" in raw:
        cfg.extend_select = list(raw["extend_select"])
    if "extend_ignore" in raw:
        cfg.extend_ignore = list(raw["extend_ignore"])
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
    if "severity" in raw and isinstance(raw["severity"], dict):
        cfg.severity_overrides = {
            code: Severity.parse(level) for code, level in raw["severity"].items()
        }
    if "per_object" in raw and isinstance(raw["per_object"], dict):
        cfg.per_object = {
            glob: list(spec.get("ignore", [])) for glob, spec in raw["per_object"].items()
        }
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
        cfg.sample_size = int(ex.get("sample_size", cfg.sample_size))
        cfg.sample_timeout = float(ex.get("sample_timeout", cfg.sample_timeout))
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
