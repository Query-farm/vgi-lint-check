"""Rule registration and discovery.

Rules self-register with ``@register`` (collision-detecting). Importing the rule
modules (done in ``rules/__init__.py``) populates the registry — no runtime file
scanning, so the catalog is static and fast.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Rule

if TYPE_CHECKING:
    from ..config import Config

REGISTRY: dict[str, type[Rule]] = {}


def register(cls: type[Rule]) -> type[Rule]:
    code = getattr(cls, "code", None)
    if not code:
        raise ValueError(f"rule {cls.__name__} has no code")
    if code in REGISTRY:
        raise ValueError(f"duplicate rule code {code}: {cls.__name__} vs {REGISTRY[code].__name__}")
    REGISTRY[code] = cls
    return cls


def all_rule_classes() -> list[type[Rule]]:
    return [REGISTRY[c] for c in sorted(REGISTRY)]


def all_rules() -> list[Rule]:
    return [cls() for cls in all_rule_classes()]


def select_rules(config: Config) -> list[Rule]:
    """Instantiate the rules enabled for this config (severity != OFF)."""
    from ..findings import Severity

    out = []
    for cls in all_rule_classes():
        rule = cls()
        if config.effective_severity(rule) is not Severity.OFF:
            out.append(rule)
    return out
