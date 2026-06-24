"""Small shared helpers for rules."""

from __future__ import annotations

import re
from typing import Any


def blank(s: Any) -> bool:
    """True when ``s`` is None, empty, or only whitespace."""
    return not (s and str(s).strip())


def _normalize(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_trivial_echo(comment: str | None, name: str | None) -> bool:
    """True when a comment merely restates the object's name."""
    if blank(comment) or blank(name):
        return False
    return _normalize(comment) == _normalize(name)


# A worker may reject an unfiltered scan at bind time (mandatory WHERE filters,
# e.g. bbox predicates) to avoid full-bucket reads. That is a deliberate policy,
# not a broken object — so generated bare-scan probes (view/CHECK execution)
# must treat it as a pass, not a failure.
_FILTER_POLICY = re.compile(
    r"un-?filtered"
    r"|requires?\s+(a\s+)?(where|filter|predicate)"
    r"|(where|filter|predicate)s?\s+(clause\s+)?(are\s+|is\s+)?required"
    r"|full[-\s]?(bucket|table)\s+(read|scan)"
    r"|must\s+(have|include|specify).{0,40}(filter|where|predicate|bbox)",
    re.IGNORECASE,
)


def is_filter_policy_error(error: object) -> bool:
    """True when an exception/message indicates a mandatory-filter rejection.

    Used to keep generated bare-scan probes (e.g. ``EXPLAIN SELECT * FROM view``)
    from false-failing on workers that require WHERE predicates by policy.
    """
    return bool(_FILTER_POLICY.search(str(error)))
