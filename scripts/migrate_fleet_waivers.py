#!/usr/bin/env python3
"""Migrate fleet lint configs: structured waivers, and drop no-op settings.

Two mechanical cleanups that only make sense fleet-wide:

**1. Redundant options.** ``column_comment_min_ratio = 0.8`` appears in ~31
repos — and 0.8 is the built-in default, so every one of those stanzas is a
no-op. They read as a deliberate relaxation in a config review, which makes the
fleet's waiver posture look worse than it is. Any option set to its own default
is dropped.

**2. Bare-string waivers.** ``ignore = ["VGI146"]`` carries no reason and no
kind, so it cannot be audited, aggregated, or expired — the justification lives
in a TOML comment that no tool reads. This rewrites each entry into the table
form, taking ``reason`` from the comment already attached to it and ``kind``
from the classification table below.

Classification is explicit per (repo, code) rather than inferred: guessing a
waiver's *kind* from prose is exactly the judgement call that must not be
automated, because ``tooling-bug`` becomes a linter backlog item and
``domain-exemption`` becomes permanent. Anything unlisted is reported, not
rewritten.

Usage::

    python scripts/migrate_fleet_waivers.py --root ~/Development
    python scripts/migrate_fleet_waivers.py --root ~/Development --apply
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

# Options whose fleet-wide value equals the built-in default. Dropping them is
# behaviour-preserving by construction — the check below verifies the value.
DEFAULTED_OPTIONS = {"column_comment_min_ratio": 0.8}

# (repo, code) -> kind. Derived by reading each config's own rationale.
#
#   domain-exemption  the rule cannot apply to this worker's shape
#   tooling-bug       the rule or the linter is wrong — becomes linter backlog
#   deferred          a real gap, not yet fixed
#   timing            an execution-window accommodation
KINDS: dict[tuple[str, str], str] = {
    # vgi-adbc is a passthrough connector: its tables/columns are whatever remote
    # database the user attached, so no per-object metadata rule can apply.
    **{
        ("vgi-adbc", code): "domain-exemption"
        for code in (
            "VGI111",
            "VGI112",
            "VGI113",
            "VGI123",
            "VGI126",
            "VGI201",
            "VGI202",
            "VGI307",
            "VGI411",
            "VGI412",
            "VGI413",
            "VGI501",
            "VGI511",
            "VGI520",
            "VGI806",
            "VGI807",
            "VGI151",
        )
    },
    # ...except this one, which the config itself calls a lint/serialization
    # artifact. That is a bug report, and it belongs in the linter's backlog.
    ("vgi-adbc", "VGI515"): "tooling-bug",
    # DuckDB cannot surface a positional argument's name for an *overloaded*
    # table function, so the argument IS named and the linter cannot see it.
    ("vgi-pe", "VGI305"): "tooling-bug",
    # Argument-only workers: every entry point needs a genuine per-key argument,
    # so there is no honest browsable slice.
    ("vgi-tika", "VGI146"): "domain-exemption",
    ("vgi-wikipedia", "VGI146"): "domain-exemption",
    # Solvers pass whole problems as scalar LIST/matrix arguments because a table
    # function has only one subquery slot and most solvers need several matrices.
    ("vgi-ortools", "VGI316"): "domain-exemption",
    # Stored-model functions cannot bind before a model exists; the fit->predict
    # workflow is demonstrated in the catalog's executable examples instead.
    **{
        (repo, code): "domain-exemption"
        for repo in ("vgi-scikit-learn", "vgi-xgboost", "vgi-lightgbm")
        for code in ("VGI901", "VGI902")
    },
    # Keyless streaming tables (many rows, no unique row identity) and mass-noun
    # table names that read wrong pluralized.
    **{("vgi-edgar", code): "domain-exemption" for code in ("VGI807", "VGI144")},
    # Overture partition names are inherently literals — a column cannot be fed
    # as a theme name.
    ("vgi-overture-maps", "VGI513"): "domain-exemption",
}

# One code on its own line inside a multi-line list, with the rationale as a
# trailing comment:  "VGI111", # remote tables are the user's
IGNORE_ENTRY = re.compile(
    r'^(?P<indent>\s*)"(?P<code>VGI\d+)",?(?P<trail>\s*#\s*(?P<comment>.*?))?\s*$'
)
# A whole list on one line, with the rationale in the comment block above it:
#   ignore = ["VGI146"]                  (also extend_ignore, and per-object forms)
#   "cat.s.fn" = { ignore = ["VGI901", "VGI902"] }
INLINE_LIST = re.compile(
    r'^(?P<head>\s*(?:"[^"]+"\s*=\s*\{\s*)?(?:extend_)?ignore\s*=\s*)'
    r"\[(?P<body>[^\]]*)\](?P<tail>.*)$"
)
CODE_IN_LIST = re.compile(r'"(VGI\d+)"')
OPTION_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[a-z_]+)\s*=\s*(?P<value>[^#\n]+?)\s*$")


def _config_paths(root: Path, glob: str) -> list[Path]:
    out = []
    for d in sorted(root.expanduser().glob(glob)):
        for name in ("vgi-lint.toml", "pyproject.toml"):
            p = d / name
            if not p.is_file():
                continue
            try:
                data = tomllib.loads(p.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if name == "vgi-lint.toml" or "vgi-lint-check" in data.get("tool", {}):
                out.append(p)
    return out


def _drop_defaulted_options(lines: list[str], repo: str) -> tuple[list[str], list[str]]:
    """Remove option assignments whose value equals the built-in default."""
    out, dropped, i = [], [], 0
    while i < len(lines):
        m = OPTION_LINE.match(lines[i])
        if m and m.group("key") in DEFAULTED_OPTIONS:
            try:
                value = float(m.group("value"))
            except ValueError:
                out.append(lines[i])
                i += 1
                continue
            if value == DEFAULTED_OPTIONS[m.group("key")]:
                dropped.append(f"{repo}: {m.group('key')} = {m.group('value')} (is the default)")
                i += 1
                # Drop a now-orphaned [.options] header directly above it.
                if (
                    out
                    and out[-1].strip().endswith("options]")
                    and (
                        i >= len(lines) or not lines[i].strip() or lines[i].lstrip().startswith("[")
                    )
                ):
                    out.pop()
                continue
        out.append(lines[i])
        i += 1
    return out, dropped


_GROUP_MEMBER = re.compile(
    r'^\s*(\[[^\]]*per_object[^\]]*\]|"[^"]+"\s*=\s*\{|(?:extend_)?ignore\s*=)'
)


def _comment_block_above(lines: list[str], index: int) -> str:
    """The rationale governing the waiver at ``index``, as prose.

    Takes the contiguous ``#`` block directly above it; failing that, walks up
    past sibling waiver entries (and their section headers) to the block comment
    that introduces the group. Fleet configs consistently write one rationale
    above a run of related per-object waivers rather than repeating it, so
    stopping at the first non-comment line would discard most of them.
    """
    j = index - 1
    while j >= 0:
        stripped = lines[j].strip()
        if stripped.startswith("#"):
            parts = []
            while j >= 0 and lines[j].strip().startswith("#"):
                parts.append(lines[j].strip().lstrip("#").strip())
                j -= 1
            text = " ".join(p for p in reversed(parts) if p)
            return re.sub(r"\s+", " ", text).strip()
        if not stripped or _GROUP_MEMBER.match(lines[j]):
            j -= 1
            continue
        return ""
    return ""


def _structure_waivers(lines: list[str], repo: str) -> tuple[list[str], list[str], list[str]]:
    """Rewrite bare-string ignore entries into the table form.

    Handles both shapes the fleet actually uses: one code per line with a
    trailing comment, and a whole list on one line whose rationale sits in the
    comment block above it.
    """
    out, done, unknown = [], [], []
    for idx, line in enumerate(lines):
        m = IGNORE_ENTRY.match(line)
        if m:
            code = m.group("code")
            reason = (m.group("comment") or "").strip()
            rendered = _render(repo, code, reason, m.group("indent"), done, unknown)
            out.append(rendered if rendered else line)
            continue

        m2 = INLINE_LIST.match(line)
        if not m2 or "{ code" in line:
            out.append(line)
            continue
        codes = CODE_IN_LIST.findall(m2.group("body"))
        if not codes:
            out.append(line)
            continue
        # One rationale covers the whole list; it is the block comment above.
        reason = _comment_block_above(lines, idx)
        entries = []
        ok = True
        for code in codes:
            rendered = _render(repo, code, reason, "", done, unknown, inline=True)
            if rendered is None:
                ok = False
                break
            entries.append(rendered)
        out.append(f"{m2.group('head')}[{', '.join(entries)}]{m2.group('tail')}\n" if ok else line)
    return out, done, unknown


def _render(
    repo: str,
    code: str,
    reason: str,
    indent: str,
    done: list[str],
    unknown: list[str],
    *,
    inline: bool = False,
) -> str | None:
    """Render one waiver as a TOML inline table, or None when it needs a human."""
    kind = KINDS.get((repo, code))
    if kind is None:
        unknown.append(f"{repo}: {code} — no kind assigned; classify it in KINDS")
        return None
    reason = reason.replace('"', "'").replace("\\", "/").strip()
    if not reason:
        unknown.append(f"{repo}: {code} — no comment to lift into `reason`")
        return None
    if len(reason) > 300:
        reason = reason[:297] + "..."
    table = f'{{ code = "{code}", kind = "{kind}", reason = "{reason}" }}'
    done.append(f"{repo}: {code} -> {kind}")
    return table if inline else f"{indent}{table},\n"


def main() -> int:
    """Report (or apply) the fleet config migration."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="~/Development")
    ap.add_argument("--glob", default="vgi-*")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--exclude", default="vgi-lint-check")
    args = ap.parse_args()

    skip = {s.strip() for s in args.exclude.split(",") if s.strip()}
    all_dropped: list[str] = []
    all_done: list[str] = []
    all_unknown: list[str] = []
    writes: list[tuple[Path, str]] = []

    for path in _config_paths(Path(args.root), args.glob):
        repo = path.parent.name
        if repo in skip:
            continue
        text = path.read_text()
        lines = text.splitlines(keepends=True)
        lines, dropped = _drop_defaulted_options(lines, repo)
        lines, done, unknown = _structure_waivers(lines, repo)
        all_dropped += dropped
        all_done += done
        all_unknown += unknown
        new = "".join(lines)
        if new != text:
            # Never write a file we just broke.
            try:
                tomllib.loads(new)
            except tomllib.TOMLDecodeError as e:
                all_unknown.append(f"{repo}: rewrite would not parse ({e}) — skipped")
                continue
            writes.append((path, new))

    print(f"DROP no-op options ({len(all_dropped)}):")
    for d in all_dropped:
        print(f"  {d}")
    print(f"\nSTRUCTURE waivers ({len(all_done)}):")
    for d in all_done:
        print(f"  {d}")
    if all_unknown:
        print(f"\nNEEDS A HUMAN ({len(all_unknown)}):")
        for d in all_unknown:
            print(f"  {d}")
    print(f"\n{len(writes)} file(s) would change")

    if not args.apply:
        print("dry run — nothing written. Re-run with --apply.")
        return 0
    for path, new in writes:
        path.write_text(new)
    print(f"wrote {len(writes)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
