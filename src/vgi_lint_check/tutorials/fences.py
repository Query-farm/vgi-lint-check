"""Parser for the annotated code-fence info string.

A tutorial SQL fence looks like::

    ```sql {role=step expect=rows}

markdown-it exposes the raw info string (``sql {role=step expect=rows}``) on the
fence token, but the ``{...}`` attribute block is *not* standard Markdown, so we
parse it here. The parser is deliberately forgiving: a malformed info string
returns an error string (which becomes a lint finding) rather than raising.
"""

from __future__ import annotations

import re

# A single ``key=value`` or bare ``key`` attribute. Values may be bare tokens or
# single/double quoted.
_ATTR = re.compile(
    r"""(?P<key>[A-Za-z_][A-Za-z0-9_-]*)
        (?:\s*=\s*
            (?P<val>"[^"]*"|'[^']*'|[^\s}]+)
        )?""",
    re.VERBOSE,
)


def parse_fence_info(info: str) -> tuple[str, dict[str, str], str | None]:
    """Split a fence info string into ``(language, attributes, error)``.

    Args:
        info: The raw fence info string, e.g. ``"sql {role=step expect=rows}"``.

    Returns:
        A ``(lang, attrs, error)`` tuple. ``lang`` is the leading language token
        (``""`` if absent), ``attrs`` maps attribute keys to their unquoted
        string values (bare flags map to ``""``), and ``error`` is a human
        message when the ``{...}`` block is malformed, else ``None``.
    """
    text = (info or "").strip()
    if not text:
        return "", {}, None

    brace = text.find("{")
    if brace == -1:
        return text, {}, None

    lang = text[:brace].strip()
    block = text[brace:]
    if not block.endswith("}"):
        return lang, {}, "unterminated { } attribute block on code fence"

    inner = block[1:-1].strip()
    attrs: dict[str, str] = {}
    pos = 0
    for m in _ATTR.finditer(inner):
        # Reject stray junk between attributes (e.g. an unquoted value with a brace).
        gap = inner[pos : m.start()]
        if gap.strip():
            return lang, attrs, f"unexpected text in fence attributes: {gap.strip()!r}"
        raw = m.group("val")
        attrs[m.group("key")] = _unquote(raw) if raw is not None else ""
        pos = m.end()
    if inner[pos:].strip():
        return lang, attrs, f"unexpected text in fence attributes: {inner[pos:].strip()!r}"
    return lang, attrs, None


def _unquote(value: str) -> str:
    """Strip a matching pair of single or double quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
