"""Per-data-version baselines.

A baseline records finding fingerprints ``(code, qualified-object)`` so that
later runs fail only on *new* findings (regressions). Each data version gets its
own file: ``<prefix>.<version>.json`` (workers with no versions use
``<prefix>.default.json``). ``classify`` marks findings new-vs-known.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

_BASELINE_VERSION = 1


def _safe_version(data_version: str | None) -> str:
    if not data_version:
        return "default"
    return re.sub(r"[^A-Za-z0-9._-]", "_", data_version)


def baseline_path(prefix: str, data_version: str | None) -> Path:
    """Compute the per-version baseline file path from a prefix.

    A prefix ending in ``.json`` keeps its stem: ``foo.json`` -> ``foo.<v>.json``.
    """
    p = Path(prefix)
    if p.suffix == ".json":
        stem = p.with_suffix("")
    else:
        stem = p
    return Path(f"{stem}.{_safe_version(data_version)}.json")


def fingerprint(finding) -> str:
    return f"{finding.code}\t{finding.object_id.qualified()}"


def load(prefix: str, data_version: str | None) -> set[str] | None:
    """Return the set of recorded fingerprints, or None if no baseline exists."""
    path = baseline_path(prefix, data_version)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return set(data.get("findings", []))


def write(prefix: str, data_version: str | None, findings) -> Path:
    path = baseline_path(prefix, data_version)
    payload = {
        "baseline_version": _BASELINE_VERSION,
        "data_version": data_version,
        "findings": sorted({fingerprint(f) for f in findings}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def classify(findings, prefix: str | None, data_version: str | None):
    """Tag each finding ``is_new`` relative to the version baseline.

    With no baseline configured or no baseline file, every finding is new.
    """
    if not prefix:
        return list(findings)
    known = load(prefix, data_version)
    if known is None:
        return list(findings)
    out = []
    for f in findings:
        out.append(dataclasses.replace(f, is_new=fingerprint(f) not in known))
    return out
