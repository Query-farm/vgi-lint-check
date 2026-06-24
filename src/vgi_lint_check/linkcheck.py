"""HTTP link resolution for the content rules (opt-out via --no-check-links).

A resolver maps a URL to an HTTP status code (or ``None`` when it cannot be
reached at all). It is wired only for real runs (in ``core``); rules treat a
missing resolver as "skip", so offline/unit tests never make network calls.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable

LinkResolver = Callable[[str], int | None]

_HEADERS = {"User-Agent": "vgi-lint-check link checker"}


def _fetch(url: str, timeout: float, method: str) -> int | None:
    req = urllib.request.Request(url, method=method, headers=_HEADERS)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as e:
        # Some servers reject HEAD; retry once with GET.
        if method == "HEAD" and e.code in (403, 405, 501):
            return _fetch(url, timeout, "GET")
        return int(e.code)
    except Exception:  # noqa: BLE001 - DNS/timeout/SSL -> unverifiable
        if method == "HEAD":
            return _fetch(url, timeout, "GET")
        return None


def make_link_resolver(timeout: float = 10.0) -> LinkResolver:
    """Return a caching resolver: ``url -> HTTP status`` (or None if unreachable)."""
    cache: dict[str, int | None] = {}

    def resolve(url: str) -> int | None:
        if url not in cache:
            cache[url] = _fetch(url, timeout, "HEAD")
        return cache[url]

    return resolve


def is_broken(status: int | None) -> bool:
    """True when a status is a definitive client-side "broken link".

    None (unreachable: DNS/timeout/TLS), 5xx (transient server), and access-gated
    codes (401/403/429) are NOT treated as broken to avoid flaky failures.
    """
    if status is None:
        return False
    if status in (401, 403, 429):
        return False
    return 400 <= status < 500
