"""Static checks for whether a tutorial's steps can run in duckdb-wasm.

WASM run is *progressive enhancement* — it only works when the worker is served
over HTTP and the SQL avoids features unavailable in a browser sandbox. This
module flags the checkable blockers (reading a local file that isn't fetchable
in-browser, INSTALL/COPY-to-disk) so the renderer can decide whether to offer a
live "Run" button. It is intentionally conservative: a full determination also
needs the worker's declared capability set (a later phase).
"""

from __future__ import annotations

import re

# read_parquet('assets/x.parquet') etc. — a relative/local path can't be fetched
# by a browser page without the asset being hosted alongside it.
_LOCAL_READ = re.compile(
    r"\bread_(?:parquet|csv|csv_auto|json|json_auto|ndjson|text)\s*\(\s*'([^']+)'",
    re.IGNORECASE,
)
_DISK = re.compile(r"\b(install|copy)\b", re.IGNORECASE)
_REMOTE = ("http://", "https://", "s3://", "gs://", "az://")


def non_wasm_reasons(sql: str) -> list[str]:
    """Return reasons ``sql`` could not run in a duckdb-wasm browser page."""
    reasons: list[str] = []
    for m in _LOCAL_READ.finditer(sql):
        path = m.group(1)
        if not path.startswith(_REMOTE):
            reasons.append(f"reads a local file {path!r} that a browser can't fetch")
    if re.search(r"\binstall\b", sql, re.IGNORECASE):
        reasons.append("uses INSTALL (extensions aren't installable in-browser)")
    if re.search(r"\bcopy\b.+\bto\b", sql, re.IGNORECASE):
        reasons.append("uses COPY … TO (no local filesystem in-browser)")
    return reasons
