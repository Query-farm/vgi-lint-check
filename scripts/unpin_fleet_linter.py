#!/usr/bin/env python3
"""Remove ``version:`` pins from every fleet repo's ``vgi-lint-check`` CI gate.

Fleet policy: **workers do not pin the linter.** The action's ``version`` input
defaults to ``>=0.26.0``, which resolves to whatever is newest at run time, so an
unpinned repo adopts every new rule as it ships. That is deliberate — the point
of the gate is to hold workers to the *current* quality bar, and a pin quietly
converts the gate into a snapshot of whatever the bar was on the day someone
wrote the number down.

The failure a pin is meant to prevent — publishing a rule and turning N untouched
repos red — is handled instead by sweeping the fleet against the new version
*before* releasing it::

    vgi-lint fleet run ~/Development/vgi-fleet-manifest.toml --jobs 6

That gives the blast radius up front, with none of the staleness. A repo that
genuinely cannot track latest should carry a baseline (only new findings gate),
not a pin.

Usage::

    python scripts/unpin_fleet_linter.py --root ~/Development
    python scripts/unpin_fleet_linter.py --root ~/Development --apply

Dry-run by default. Repos whose pin carries a written rationale are reported
separately: removing the pin without removing the now-false comment would leave
the file contradicting itself, so those are rewritten comment-and-all.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

USES = re.compile(r"^(?P<indent>\s*)uses:\s*Query-farm/vgi-lint-check@(?P<ref>\S+)\s*$")
VERSION_LINE = re.compile(r"^(?P<indent>\s*)version:\s*[\"']?(?P<value>[^\"'\s]+)[\"']?\s*$")
WITH_LINE = re.compile(r"^(?P<indent>\s*)with:\s*$")


@dataclass
class Change:
    """One workflow file's pin removal."""

    path: Path
    repo: str
    before: str
    rationale: str = ""
    new_text: str = field(default="", repr=False)

    @property
    def had_rationale(self) -> bool:
        """True when a comment justified the pin (and was dropped with it)."""
        return bool(self.rationale)


def _unpin_file(text: str) -> tuple[str, str, str]:
    """Return ``(new_text, removed_version, dropped_rationale)`` for one workflow.

    Line-wise on purpose: these workflows carry comments and templated ``${{ }}``
    expressions that a YAML round-trip would reformat, and the rewrite must touch
    only the pin (plus the comment that justified it).
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = USES.match(line)
        if not m:
            continue
        base_indent = len(m.group("indent"))
        with_at = None
        for j in range(i + 1, min(i + 12, len(lines))):
            wm = WITH_LINE.match(lines[j])
            if wm and len(wm.group("indent")) >= base_indent:
                with_at = j
                break
            if (
                lines[j].strip().startswith("- ")
                and len(lines[j]) - len(lines[j].lstrip()) <= base_indent
            ):
                break
        if with_at is None:
            continue
        item_indent = len(WITH_LINE.match(lines[with_at]).group("indent"))
        k, version_at = with_at + 1, None
        while k < len(lines):
            if not lines[k].strip():
                k += 1
                continue
            indent = len(lines[k]) - len(lines[k].lstrip())
            if indent <= item_indent:
                break
            vm = VERSION_LINE.match(lines[k])
            if vm and indent == item_indent + 2:
                version_at = k
                break
            k += 1
        if version_at is None:
            return text, "", ""

        removed = VERSION_LINE.match(lines[version_at]).group("value")
        # Take the comment block above with it — it exists only to explain a pin
        # that is about to stop existing.
        start = version_at
        rationale_parts = []
        while start - 1 >= 0 and lines[start - 1].strip().startswith("#"):
            start -= 1
            rationale_parts.append(lines[start].strip().lstrip("#").strip())
        rationale = " ".join(reversed(rationale_parts)).strip()
        out = lines[:start] + lines[version_at + 1 :]
        return "".join(out), removed, rationale
    return text, "", ""


def plan(root: Path, glob: str) -> list[Change]:
    """Compute the unpin change for every fleet repo under ``root``."""
    changes = []
    for repo in sorted(root.expanduser().glob(glob)):
        wf = repo / ".github" / "workflows"
        if not wf.is_dir():
            continue
        for f in sorted(wf.glob("*.y*ml")):
            try:
                text = f.read_text()
            except OSError:
                continue
            if "Query-farm/vgi-lint-check@" not in text:
                continue
            new, removed, rationale = _unpin_file(text)
            if not removed:
                continue
            changes.append(Change(f, repo.name, removed, rationale, new))
    return changes


def main() -> int:
    """Report (or apply) the fleet-wide unpin."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="~/Development")
    ap.add_argument("--glob", default="vgi-*")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--exclude", default="vgi-lint-check")
    args = ap.parse_args()

    skip = {s.strip() for s in args.exclude.split(",") if s.strip()}
    changes = [c for c in plan(Path(args.root), args.glob) if c.repo not in skip]
    plain = [c for c in changes if not c.had_rationale]
    justified = [c for c in changes if c.had_rationale]

    print(f"UNPIN ({len(plain)} repos) — will track the latest published linter:")
    for c in plain:
        print(f"  {c.repo:<26} was {c.before:<10} {c.path.name}")

    if justified:
        print(
            f"\nUNPIN + DROP RATIONALE ({len(justified)} repos) — the pin carried a written\n"
            "reason, which is removed with it. Read these before applying; if the reason\n"
            "still holds, the right answer is a baseline, not a pin:"
        )
        for c in justified:
            print(f"  {c.repo:<26} was {c.before}")
            print(f"      dropped comment: {c.rationale[:170]}")

    if not args.apply:
        print(f"\n{len(changes)} file(s) would change. Dry run — re-run with --apply.")
        return 0
    for c in changes:
        c.path.write_text(c.new_text)
    print(f"\nwrote {len(changes)} workflow file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
