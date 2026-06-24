"""Configuration: rule selection, severity, tuning options, resolution order.

Configuration covers rule selection, per-rule severity, tuning options, and the
deterministic severity-resolution order rules depend on.

Precedence (lowest -> highest) is applied by the loader in ``cli.py``:
built-in defaults < pyproject.toml < dedicated file < CLI flags.
"""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .findings import Severity

if TYPE_CHECKING:
    from .model import ObjectId
    from .rules.base import Rule


@dataclass
class Options:
    """Rule tuning knobs (the ``[tool.vgi-lint-check.options]`` table)."""

    column_comment_min_ratio: float = 0.8
    min_llm_description_chars: int = 40
    min_md_description_chars: int = 80
    min_description_chars: int = 12
    # Flag a schema with more than this many objects (0 = disabled).
    max_schema_objects: int = 0
    # Flag a worker advertising more than this many catalogs (0 = disabled).
    max_catalogs: int = 100
    # Warn (never error) on a catalog with more than this many tables / functions
    # (0 = disabled). Generous so only genuinely excessive catalogs are flagged.
    max_tables: int = 500
    max_functions: int = 500
    # Warn on over-long table/function names (0 = disabled).
    max_table_name_length: int = 64
    max_function_name_length: int = 64
    # Required tags are opt-in: VGI401 only fires for keys you list here. There
    # is no universal tag every schema/table must carry.
    required_schema_tags: list[str] = field(default_factory=list)
    required_table_tags: list[str] = field(default_factory=list)
    allowed_tag_keys: list[str] = field(default_factory=list)
    # Discoverability (VGI12x) tuning.
    min_meaningful_description_chars: int = 30
    min_example_queries: int = 3
    # Warn when one object declares more than this many executable examples
    # (each runs against the worker under --execute). 0 = disabled.
    max_executable_examples: int = 10
    classifying_tag_keys: list[str] = field(
        default_factory=lambda: ["domain", "category", "provider", "topic"]
    )


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
    check_links: bool = False  # enable network rules (validate description URLs)
    link_timeout: float = 10.0
    fail_on: Severity = Severity.ERROR
    location: str | None = None
    baseline: str | None = None

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
        # 1. execution rules need --execute; network rules need --check-links
        if getattr(rule, "requires_connection", False) and not self.execute:
            return Severity.OFF
        if getattr(rule, "requires_network", False) and not self.check_links:
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
