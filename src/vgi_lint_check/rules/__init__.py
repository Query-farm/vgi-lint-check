"""Rule families. Importing this package registers every rule.

The submodule imports are side-effecting (each ``@register`` populates the
registry), so they must run for ``REGISTRY`` to be complete.
"""

from __future__ import annotations

from . import (  # noqa: F401  (imported for registration side effects)
    columns,
    descriptions,
    examples,
    execution,
    functions,
    pragmas,
    settings,
    tags,
)
from .base import Rule, RuleContext
from .engine import run
from .registry import REGISTRY, all_rule_classes, all_rules, register, select_rules

__all__ = [
    "Rule",
    "RuleContext",
    "run",
    "REGISTRY",
    "all_rule_classes",
    "all_rules",
    "register",
    "select_rules",
]
