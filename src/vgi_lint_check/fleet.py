"""Fleet sweep: lint many workers, aggregate, and publish one view.

A single worker's lint result lives in one repo's CI log, where nobody reads it.
At fleet scale that is the whole problem: 100+ green checkmarks say nothing about
whether the catalog as a whole is getting better, which workers are pinned to a
stale rulebook, or where the waivers are piling up.

This module runs the sweep and answers those questions in one artifact.

Design notes
------------
*Each worker is linted in a subprocess.* Never in-process. A VGI worker wedged
inside its first batch cannot be interrupted and its cursor blocks forever on
close (see CLAUDE.md) — in-process that poisons the entire sweep; as a subprocess
it costs one timeout and the sweep moves on. The subprocess also gets the
worker's own repo as its cwd, which is how ``vgi-lint.toml`` discovery works.

*The sweep never gates.* Each lint runs at ``--fail-on never`` so a failing
worker still yields a full report to aggregate. Gating is the fleet's decision,
made once over the whole result set, not 100 separate exit codes.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# A worker that has not answered in this long is wedged, not slow.
DEFAULT_TIMEOUT = 900.0
DEFAULT_JOBS = 4


@dataclass
class WorkerSpec:
    """One worker in the fleet manifest."""

    name: str
    location: str
    directory: str = "."
    execute: bool = True
    doc_review: bool = False
    agent_check: bool = False
    check_links: bool = False
    audit_waivers: bool = True
    attach_options: dict[str, str] = field(default_factory=dict)
    setup_sql: list[str] = field(default_factory=list)
    # Free-form ownership/tier labels carried straight through to the report, so
    # the dashboard can group by whatever the fleet actually cares about.
    tags: list[str] = field(default_factory=list)
    skip: bool = False
    skip_reason: str = ""
    timeout: float = DEFAULT_TIMEOUT

    @property
    def tutorials_dir(self) -> Path:
        """Where this worker's tutorials would live, if it ships any."""
        return Path(self.directory) / "tutorials"

    def has_tutorials(self) -> bool:
        """True when the worker ships at least one ``.vgi.md`` tutorial."""
        d = self.tutorials_dir
        return d.is_dir() and any(d.glob("**/*.vgi.md"))


@dataclass
class WorkerResult:
    """What the sweep learned about one worker."""

    name: str
    status: str  # ok | failed | timeout | skipped | error
    location: str = ""
    directory: str = ""
    tags: list[str] = field(default_factory=list)
    score: int | None = None
    static_score: int | None = None
    agent_score: int | None = None
    doc_quality: int | None = None
    level: int = 0
    level_label: str = "L0"
    level_title: str = "unverified"
    blocker: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    waivers: list[dict[str, Any]] = field(default_factory=list)
    dead_waivers: int = 0
    tooling_bugs: list[dict[str, Any]] = field(default_factory=list)
    top_findings: list[dict[str, Any]] = field(default_factory=list)
    vgi_version: str | None = None
    has_tutorials: bool = False
    duration_s: float = 0.0
    detail: str = ""


def load_manifest(path: str | Path) -> list[WorkerSpec]:
    """Parse a fleet manifest (TOML) into worker specs.

    Manifest shape::

        # optional defaults applied to every worker
        [defaults]
        execute = true

        [[worker]]
        name = "vgi-units"
        directory = "~/Development/vgi-units"
        location = "target/release/units-worker"
    """
    raw = tomllib.loads(Path(path).expanduser().read_text())
    defaults = raw.get("defaults", {}) or {}
    specs = []
    for entry in raw.get("worker", []) or []:
        merged: dict[str, Any] = {**defaults, **entry}
        merged = {k.replace("-", "_"): v for k, v in merged.items()}
        directory = str(Path(str(merged.get("directory", "."))).expanduser())
        merged["directory"] = directory
        known = {f for f in WorkerSpec.__dataclass_fields__}
        specs.append(WorkerSpec(**{k: v for k, v in merged.items() if k in known}))
    return specs


def discover(root: str | Path, *, glob: str = "vgi-*") -> list[WorkerSpec]:
    """Scaffold specs by scanning ``root`` for repos that look like VGI workers.

    A directory qualifies when it carries a lint config *or* a CI workflow that
    invokes the linter — i.e. somebody already decided it is a worker. The
    location is left blank when it can't be inferred; ``fleet init`` writes those
    out commented so a human fills them in rather than the sweep guessing.
    """
    out = []
    for d in sorted(Path(root).expanduser().glob(glob)):
        if not d.is_dir():
            continue
        gated = _reads_lint_config(d) or _has_lint_ci(d)
        if not gated:
            continue
        out.append(
            WorkerSpec(
                name=d.name,
                location=_infer_location(d),
                directory=str(d),
                skip=not _infer_location(d),
                skip_reason="" if _infer_location(d) else "location could not be inferred",
            )
        )
    return out


def _reads_lint_config(d: Path) -> bool:
    if (d / "vgi-lint.toml").is_file():
        return True
    pp = d / "pyproject.toml"
    if pp.is_file():
        try:
            return "vgi-lint-check" in tomllib.loads(pp.read_text()).get("tool", {})
        except (OSError, tomllib.TOMLDecodeError):
            return False
    return False


def _has_lint_ci(d: Path) -> bool:
    wf = d / ".github" / "workflows"
    if not wf.is_dir():
        return False
    for f in wf.glob("*.y*ml"):
        try:
            if "vgi-lint" in f.read_text():
                return True
        except OSError:
            continue
    return False


def _infer_location(d: Path) -> str:
    """Best-effort worker location, in descending order of confidence.

    Only ever returns something that exists (or needs no build): a manifest whose
    locations are aspirational would turn the whole sweep into connection errors
    and hide the real quality signal behind them.
    """
    # 1. The lint config says so — the author's own declaration.
    loc = _location_from_config(d)
    if loc:
        return loc
    # 2. The CI workflow says so. It names the *intended* artifact, which is the
    #    most authoritative answer available — but it points at a build output, so
    #    only take it when that artifact is actually present locally.
    loc = _location_from_ci(d)
    if loc:
        return loc
    # 3. A built compiled worker, in the usual places.
    for pattern in ("target/release/*-worker", "bin/*-worker", "*-worker"):
        for candidate in sorted(d.glob(pattern)):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    # 4. A JVM worker's shaded ("fat") jar. The CI location for these is usually
    #    templated through an env var, so it cannot be read back from the
    #    workflow — but the built artifact is right there and unambiguous.
    jars = sorted(
        d.glob("build/libs/*-all.jar"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if jars:
        return f"java -jar {shlex.quote(str(jars[0]))}"
    # 5. Interpreted workers, which need no build at all.
    for candidate in sorted(d.glob("*_worker.py")):
        return f"uv run {candidate.name}"
    if (d / "src" / "worker.ts").is_file() and (d / "node_modules").is_dir():
        return "bun run src/worker.ts"
    return ""


def _location_from_config(d: Path) -> str:
    for name in ("vgi-lint.toml", "pyproject.toml"):
        p = d / name
        if not p.is_file():
            continue
        try:
            data = tomllib.loads(p.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        table = data.get("tool", {}).get("vgi-lint-check", data if name != "pyproject.toml" else {})
        loc = table.get("location") if isinstance(table, dict) else None
        if loc:
            return str(loc)
    return ""


_CI_LOCATION = re.compile(r"^\s*location:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)


def _location_from_ci(d: Path) -> str:
    """Read the worker location out of the repo's own vgi-lint CI step.

    This is the authoritative answer and is tried before any guess, because a
    sweep that lints something *other* than what CI lints produces findings that
    do not transfer. That is not hypothetical: several Python workers declare
    their SDK floor in two places (``pyproject.toml`` and a PEP 723 header), so
    ``uv run worker.py`` and ``.venv/bin/python worker.py`` resolve to different
    SDK versions — guessing the former reported failures CI would never see.

    Workflow values are templated (``${{ github.workspace }}/bin/x``); the
    workspace resolves to the repo root, and anything still carrying an
    unresolvable expression (``${{ env.VGI_TIKA_WORKER }}``) is skipped.
    """
    wf = d / ".github" / "workflows"
    if not wf.is_dir():
        return ""
    for f in sorted(wf.glob("*.y*ml")):
        try:
            text = f.read_text()
        except OSError:
            continue
        if "vgi-lint-check@" not in text:
            continue
        block = text[text.index("vgi-lint-check@") :]
        m = _CI_LOCATION.search(block)
        if not m:
            continue
        raw = m.group(1).replace("${{ github.workspace }}", str(d)).strip()
        if "${{" in raw:
            continue
        # A location is either a bare executable or a command (an interpreter
        # plus a script). Accept both — only the leading token has to exist —
        # and resolve it against the repo so the sweep can run from anywhere.
        parts = shlex.split(raw)
        if not parts:
            continue
        head = Path(parts[0]) if Path(parts[0]).is_absolute() else d / parts[0]
        if not head.exists():
            continue
        if len(parts) == 1:
            if head.is_file() and os.access(head, os.X_OK):
                return str(head)
            continue
        rest = " ".join(shlex.quote(p) for p in parts[1:])
        return f"{shlex.quote(str(head))} {rest}"
    return ""


def _build_command(spec: WorkerSpec, *, linter: list[str]) -> list[str]:
    cmd = [*linter, "lint", spec.location, "--format", "json", "--fail-on", "never"]
    cmd += ["--execute"] if spec.execute else ["--no-execute"]
    cmd += ["--check-links"] if spec.check_links else ["--no-check-links"]
    if spec.doc_review:
        cmd.append("--doc-review")
    if spec.agent_check:
        cmd.append("--agent-check")
    if spec.audit_waivers:
        cmd.append("--audit-waivers")
    for k, v in spec.attach_options.items():
        cmd += ["--attach-option", f"{k}={v}"]
    for sql in spec.setup_sql:
        cmd += ["--setup-sql", sql]
    return cmd


def lint_one(spec: WorkerSpec, *, linter: list[str] | None = None) -> WorkerResult:
    """Lint one worker in its own process and distill the result."""
    import time

    base = WorkerResult(
        name=spec.name,
        status="skipped",
        location=spec.location,
        directory=spec.directory,
        tags=list(spec.tags),
        has_tutorials=spec.has_tutorials(),
    )
    if spec.skip or not spec.location:
        base.detail = spec.skip_reason or "no location configured"
        return base

    cmd = _build_command(spec, linter=linter or [sys.executable, "-m", "vgi_lint_check"])
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
            cmd,
            cwd=spec.directory or None,
            capture_output=True,
            text=True,
            timeout=spec.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        base.status = "timeout"
        base.duration_s = round(time.monotonic() - started, 1)
        base.detail = f"no result within {spec.timeout:.0f}s — worker likely wedged"
        return base
    except OSError as e:
        base.status = "error"
        base.detail = str(e)
        return base

    base.duration_s = round(time.monotonic() - started, 1)
    doc = _parse_json(proc.stdout)
    if doc is None:
        base.status = "failed"
        base.detail = _tail(proc.stderr or proc.stdout, 400)
        return base
    return _distill(base, doc)


def _parse_json(text: str) -> dict[str, Any] | None:
    """Pull the JSON document out of a lint run's stdout.

    The linter may print progress lines before the document, so fall back to
    scanning for the first ``{`` rather than requiring a pristine stream.
    """
    text = (text or "").strip()
    if not text:
        return None
    for candidate in (text, text[text.find("{") :] if "{" in text else ""):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _distill(base: WorkerResult, doc: dict[str, Any]) -> WorkerResult:
    results = doc.get("results") or []
    base.status = "ok"
    base.vgi_version = (doc.get("worker") or {}).get("vgi_version")
    if not results:
        base.status = "failed"
        base.detail = "worker produced no catalog"
        return base
    r = results[0]
    level = r.get("level") or {}
    base.score = r.get("score")
    base.static_score = r.get("static_score")
    base.agent_score = r.get("agent_score")
    base.doc_quality = r.get("doc_quality")
    base.level = int(level.get("level", 0))
    base.level_label = str(level.get("label", "L0"))
    base.level_title = str(level.get("title", "unverified"))
    base.blocker = str(level.get("blocker", ""))
    base.counts = r.get("counts") or {}
    base.waivers = r.get("waivers") or []
    base.dead_waivers = sum(1 for w in base.waivers if w.get("dead"))
    base.tooling_bugs = [w for w in base.waivers if w.get("kind") == "tooling-bug"]
    # Errors first, then warnings; enough to act on without dragging the whole
    # finding list into the fleet document.
    ranked = sorted(
        r.get("findings") or [],
        key=lambda f: {"error": 0, "warning": 1, "info": 2}.get(f.get("severity", "info"), 3),
    )
    base.top_findings = [
        {
            "code": f.get("code"),
            "severity": f.get("severity"),
            "object": (f.get("object") or {}).get("qualified"),
            "message": f.get("message"),
        }
        for f in ranked[:5]
    ]
    return base


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def sweep(
    specs: list[WorkerSpec],
    *,
    jobs: int = DEFAULT_JOBS,
    linter: list[str] | None = None,
    on_done: Any = None,
) -> list[WorkerResult]:
    """Lint every worker, up to ``jobs`` at a time, preserving manifest order.

    Concurrency is bounded deliberately: each job spawns a real worker process
    (some load multi-gigabyte models), and the fleet's own build notes put the
    practical ceiling around six parallel heavy jobs.
    """
    results: dict[str, WorkerResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(lint_one, s, linter=linter): s for s in specs}
        for fut in concurrent.futures.as_completed(futures):
            spec = futures[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 - one bad worker must not kill the sweep
                res = WorkerResult(
                    name=spec.name, status="error", detail=f"{type(e).__name__}: {e}"
                )
            results[spec.name] = res
            if on_done is not None:
                on_done(res)
    return [results[s.name] for s in specs if s.name in results]


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def summarize(results: list[WorkerResult]) -> dict[str, Any]:
    """Roll the sweep up into the numbers a fleet owner actually asks for."""
    ok = [r for r in results if r.status == "ok"]
    scores = [r.score for r in ok if r.score is not None]
    by_level: dict[str, int] = {}
    for r in ok:
        by_level[r.level_label] = by_level.get(r.level_label, 0) + 1
    waiver_kinds: dict[str, int] = {}
    for r in ok:
        for w in r.waivers:
            k = str(w.get("kind", "unspecified"))
            waiver_kinds[k] = waiver_kinds.get(k, 0) + 1
    return {
        "workers": len(results),
        "linted": len(ok),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "failed": sum(1 for r in results if r.status in ("failed", "error")),
        "timeout": sum(1 for r in results if r.status == "timeout"),
        "score_mean": round(sum(scores) / len(scores), 1) if scores else None,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "by_level": by_level,
        "waivers": sum(len(r.waivers) for r in ok),
        "waiver_kinds": waiver_kinds,
        "dead_waivers": sum(r.dead_waivers for r in ok),
        "tooling_bugs": sum(len(r.tooling_bugs) for r in ok),
        "with_tutorials": sum(1 for r in results if r.has_tutorials),
        "findings": {
            sev: sum(r.counts.get(sev, 0) for r in ok) for sev in ("error", "warning", "info")
        },
    }


def to_document(results: list[WorkerResult], *, linter_version: str) -> dict[str, Any]:
    """The fleet sweep as one JSON document."""
    return {
        "tool": "vgi-lint fleet",
        "schema_version": 1,
        "linter_version": linter_version,
        "summary": summarize(results),
        "workers": [asdict(r) for r in results],
    }


def render_markdown(doc: dict[str, Any]) -> str:
    """A terse Markdown digest — the thing to paste into a status update."""
    s = doc["summary"]
    lines = [
        f"# VGI fleet sweep — vgi-lint {doc['linter_version']}",
        "",
        f"- **{s['linted']}/{s['workers']}** workers linted "
        f"({s['skipped']} skipped · {s['failed']} failed · {s['timeout']} timed out)",
        f"- score mean **{s['score_mean']}** (min {s['score_min']} · max {s['score_max']})",
        "- levels: " + (", ".join(f"{k} {v}" for k, v in sorted(s["by_level"].items())) or "none"),
        f"- findings: {s['findings']['error']} error · {s['findings']['warning']} warning "
        f"· {s['findings']['info']} info",
        f"- waivers: {s['waivers']} declared · **{s['dead_waivers']} dead** "
        f"· {s['tooling_bugs']} filed as linter bugs",
        f"- tutorials: {s['with_tutorials']}/{s['workers']} workers ship any",
        "",
        "| worker | level | score | err | warn | info | waivers | status |",
        "| --- | --- | --: | --: | --: | --: | --: | --- |",
    ]
    for w in sorted(doc["workers"], key=lambda x: (-(x["level"]), -(x["score"] or 0), x["name"])):
        c = w["counts"] or {}
        dead = f" ({w['dead_waivers']} dead)" if w["dead_waivers"] else ""
        lines.append(
            f"| {w['name']} | {w['level_label']} | {w['score'] if w['score'] is not None else '—'} "
            f"| {c.get('error', 0)} | {c.get('warning', 0)} | {c.get('info', 0)} "
            f"| {len(w['waivers'])}{dead} | {w['status']} |"
        )
    return "\n".join(lines) + "\n"


def render_html(doc: dict[str, Any]) -> str:
    """A self-contained, theme-aware dashboard page for the sweep."""
    s = doc["summary"]
    rows = []
    for w in sorted(doc["workers"], key=lambda x: (-(x["level"]), -(x["score"] or 0), x["name"])):
        c = w["counts"] or {}
        blocker = _esc(w["blocker"] or w["detail"] or "")
        dead = f'<span class="bad">{w["dead_waivers"]} dead</span>' if w["dead_waivers"] else ""
        waivers = f"{len(w['waivers'])} {dead}".strip()
        score = w["score"] if w["score"] is not None else "—"
        rows.append(
            f'<tr class="s-{_esc(w["status"])}">'
            f'<td class="name">{_esc(w["name"])}</td>'
            f'<td><span class="lv lv{w["level"]}">{_esc(w["level_label"])}</span> '
            f'<span class="dim">{_esc(w["level_title"])}</span></td>'
            f'<td class="num">{score}</td>'
            f'<td class="num e">{c.get("error", 0)}</td>'
            f'<td class="num w">{c.get("warning", 0)}</td>'
            f'<td class="num i">{c.get("info", 0)}</td>'
            f"<td>{waivers}</td>"
            f"<td>{'yes' if w['has_tutorials'] else '—'}</td>"
            f'<td class="dim">{blocker}</td>'
            "</tr>"
        )
    levels = " ".join(
        f'<span class="lv lv{k[1]}">{_esc(k)}</span>&nbsp;{v}'
        for k, v in sorted(s["by_level"].items())
    )
    kinds = ", ".join(f"{k} {v}" for k, v in sorted(s["waiver_kinds"].items())) or "none"
    return _HTML.format(
        version=_esc(doc["linter_version"]),
        linted=s["linted"],
        workers=s["workers"],
        mean=s["score_mean"] if s["score_mean"] is not None else "—",
        smin=s["score_min"] if s["score_min"] is not None else "—",
        levels=levels or "none",
        errors=s["findings"]["error"],
        warnings=s["findings"]["warning"],
        dead=s["dead_waivers"],
        bugs=s["tooling_bugs"],
        kinds=_esc(kinds),
        tuts=s["with_tutorials"],
        skipped=s["skipped"],
        failed=s["failed"] + s["timeout"],
        rows="\n".join(rows),
    )


def _esc(text: Any) -> str:
    from html import escape

    return escape(str(text if text is not None else ""))


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VGI fleet quality</title>
<style>
:root {{ color-scheme: light dark;
  --bg:#fff; --fg:#16181d; --dim:#6b7280; --line:#e4e6eb; --card:#f7f8fa;
  --e:#c0392b; --w:#b7791f; --i:#2b6cb0; --ok:#1f7a4d; }}
@media (prefers-color-scheme: dark) {{ :root {{
  --bg:#0f1115; --fg:#e6e8ec; --dim:#9aa1ad; --line:#252a33; --card:#171a20;
  --e:#ff6b5e; --w:#e0b341; --i:#63a9ff; --ok:#4ade80; }} }}
:root[data-theme="dark"] {{ --bg:#0f1115; --fg:#e6e8ec; --dim:#9aa1ad; --line:#252a33;
  --card:#171a20; --e:#ff6b5e; --w:#e0b341; --i:#63a9ff; --ok:#4ade80; }}
:root[data-theme="light"] {{ --bg:#fff; --fg:#16181d; --dim:#6b7280; --line:#e4e6eb;
  --card:#f7f8fa; --e:#c0392b; --w:#b7791f; --i:#2b6cb0; --ok:#1f7a4d; }}
body {{ margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1100px; margin:0 auto; }}
h1 {{ font-size:1.5rem; margin:0 0 .25rem; letter-spacing:-.01em; }}
.sub {{ color:var(--dim); margin:0 0 1.75rem; font-size:.9rem; }}
.cards {{ display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
  margin-bottom:1.75rem; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:.85rem 1rem; }}
.card .k {{ color:var(--dim); font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; }}
.card .v {{ font-size:1.45rem; font-weight:600; margin-top:.15rem; }}
.card .n {{ color:var(--dim); font-size:.8rem; }}
.wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; font-size:.88rem; min-width:820px; }}
th,td {{ text-align:left; padding:.5rem .7rem; border-bottom:1px solid var(--line); }}
th {{ background:var(--card); font-size:.74rem; text-transform:uppercase;
  letter-spacing:.05em; color:var(--dim); position:sticky; top:0; }}
tr:last-child td {{ border-bottom:0; }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
td.name {{ font-weight:600; }}
td.e {{ color:var(--e); }} td.w {{ color:var(--w); }} td.i {{ color:var(--i); }}
.dim {{ color:var(--dim); font-size:.85em; }}
.bad {{ color:var(--e); font-weight:600; }}
.lv {{ display:inline-block; padding:.1rem .4rem; border-radius:5px; font-size:.74rem;
  font-weight:700; border:1px solid var(--line); }}
.lv0 {{ color:var(--e); }} .lv1 {{ color:var(--w); }} .lv2 {{ color:var(--i); }}
.lv3, .lv4 {{ color:var(--ok); }}
tr.s-failed td, tr.s-timeout td, tr.s-error td {{ opacity:.65; }}
</style></head><body><main>
<h1>VGI fleet quality</h1>
<p class="sub">vgi-lint {version} · {linted} of {workers} workers linted
 · {skipped} skipped · {failed} failed or timed out</p>
<div class="cards">
  <div class="card"><div class="k">Mean score</div><div class="v">{mean}</div>
    <div class="n">lowest {smin}</div></div>
  <div class="card"><div class="k">Assurance levels</div><div class="v">{levels}</div>
    <div class="n">how much was verified</div></div>
  <div class="card"><div class="k">Findings</div><div class="v">{errors} / {warnings}</div>
    <div class="n">error / warning</div></div>
  <div class="card"><div class="k">Dead waivers</div><div class="v">{dead}</div>
    <div class="n">{kinds}</div></div>
  <div class="card"><div class="k">Linter bugs filed</div><div class="v">{bugs}</div>
    <div class="n">kind = tooling-bug</div></div>
  <div class="card"><div class="k">With tutorials</div><div class="v">{tuts}</div>
    <div class="n">of {workers} workers</div></div>
</div>
<div class="wrap"><table>
<thead><tr><th>Worker</th><th>Level</th><th>Score</th><th>Err</th><th>Warn</th><th>Info</th>
<th>Waivers</th><th>Tutorials</th><th>Blocked by</th></tr></thead>
<tbody>
{rows}
</tbody></table></div>
</main></body></html>
"""


def render_manifest(specs: list[WorkerSpec]) -> str:
    """Serialize discovered specs back out as a manifest to hand-edit."""
    lines = [
        "# vgi-lint fleet manifest.",
        "#",
        "# Generated by `vgi-lint fleet init`. Every worker whose location could not",
        "# be inferred is written with skip = true — fill in `location` and drop the",
        "# skip to bring it into the sweep.",
        "",
        "[defaults]",
        "execute = true",
        "audit_waivers = true",
        "",
    ]
    for s in specs:
        lines.append("[[worker]]")
        lines.append(f'name = "{s.name}"')
        lines.append(f'directory = "{s.directory}"')
        lines.append(f'location = "{s.location}"')
        if s.skip:
            lines.append("skip = true")
            lines.append(f'skip_reason = "{s.skip_reason}"')
        lines.append("")
    return "\n".join(lines)


def quote(cmd: list[str]) -> str:
    """Shell-quote a command for display."""
    return " ".join(shlex.quote(c) for c in cmd)
