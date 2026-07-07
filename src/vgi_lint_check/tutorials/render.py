"""Render a parsed tutorial to a single self-contained HTML page.

The output has no external requests: CSS and a tiny copy-button script are
inlined, SQL is highlighted by a small regex tokenizer (no CDN), referenced
images are embedded as ``data:`` URIs, and pinned result blocks render as
tables. The live "Run" button is disabled by default and enabled (progressive
enhancement) only when the page is built with a worker endpoint and the tutorial
is wasm-safe — in-browser execution needs a signed-in, running worker.
"""

from __future__ import annotations

import base64
import html
import json
import re
import sys
from pathlib import Path

from markdown_it import MarkdownIt

from .fences import parse_fence_info
from .hub import find_hub, nav_for
from .jsonld import build_jsonld
from .loader import _parse_result_block, load_dir, load_tutorial
from .model import ROLE_ILLUSTRATIVE, ResultBlock, TutorialDoc, TutorialHub, TutorialNav
from .wasm import non_wasm_reasons

_SQL_KEYWORDS = {
    "select",
    "from",
    "where",
    "group",
    "by",
    "order",
    "having",
    "limit",
    "offset",
    "join",
    "left",
    "right",
    "inner",
    "outer",
    "full",
    "cross",
    "on",
    "using",
    "with",
    "as",
    "and",
    "or",
    "not",
    "in",
    "is",
    "null",
    "like",
    "between",
    "case",
    "when",
    "then",
    "else",
    "end",
    "distinct",
    "union",
    "all",
    "values",
    "attach",
    "detach",
    "type",
    "location",
    "set",
    "search_path",
    "create",
    "table",
    "temp",
    "temporary",
    "view",
    "pragma",
    "explain",
    "asc",
    "desc",
    "over",
    "partition",
    "insert",
    "into",
    "update",
    "delete",
    "cast",
    "date",
    "timestamp",
    "interval",
    "true",
    "false",
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "round",
}

_MIME = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def render_html(
    doc: TutorialDoc,
    *,
    nav: TutorialNav | None = None,
    base_url: str | None = None,
    wasm_endpoint: str | None = None,
) -> str:
    """Render ``doc`` to a complete, standalone HTML document string.

    Args:
        doc: The parsed tutorial.
        nav: Optional series navigation (prev/next/siblings) computed from the
            worker's hub; when given, a "series" nav block is injected.
        base_url: Optional site base for canonical/breadcrumb URLs.
        wasm_endpoint: Optional HTTP worker endpoint; when set and the tutorial is
            wasm-safe, the live "Run" button is enabled (progressive enhancement).

    Returns:
        A complete, self-contained HTML document as a string.
    """
    fm = doc.front_matter
    title = (fm.title if fm else None) or doc.slug
    base_dir = Path(doc.path).parent
    run_enabled = _wasm_runnable(doc) if wasm_endpoint else False

    md = MarkdownIt("commonmark")
    md.renderer.rules["fence"] = _make_fence_rule(run_enabled)  # type: ignore[attr-defined]
    md.renderer.rules["image"] = _make_image_rule(base_dir)  # type: ignore[attr-defined]
    body_html = md.render(doc.body_md)

    jsonld = build_jsonld(doc, base_url)
    ld_tags = "\n".join(
        f'<script type="application/ld+json">{json.dumps(obj)}</script>' for obj in jsonld
    )
    canonical = ""
    if base_url:
        href = f"{base_url.rstrip('/')}/{doc.slug}"
        canonical = f'<link rel="canonical" href="{html.escape(href)}">\n'
    endpoint_attr = ""
    if run_enabled and wasm_endpoint:
        endpoint_attr = f' data-wasm-endpoint="{html.escape(wasm_endpoint)}"'

    banner = ""
    if doc.parse_error:
        banner = f'<div class="banner">⚠ {html.escape(doc.parse_error)}</div>'

    esc_title = html.escape(title)
    esc_desc = html.escape((fm.description if fm else None) or "")
    # Composed with an f-string (not str.format) so the literal braces in the CSS
    # don't need escaping.
    return (
        "<!doctype html>\n"
        f'<html lang="en"{endpoint_attr}><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc_title}</title>\n"
        f'<meta name="description" content="{esc_desc}">\n'
        f"{canonical}"
        f"<style>{_CSS}</style>\n"
        f"{ld_tags}\n"
        "</head><body>\n"
        f'<main class="tutorial">\n{banner}\n'
        f'<header class="tut-header">{_render_header(doc)}</header>\n'
        f'<article class="tut-body">{body_html}</article>\n'
        f"{_render_nav(nav, doc.slug)}"
        '<footer class="tut-footer">Rendered by '
        "<code>vgi-lint tutorials build</code> · a Query.Farm VGI worker tutorial</footer>\n"
        "</main>\n"
        f"<script>{_SCRIPT}</script>\n"
        "</body></html>\n"
    )


# --- markdown-it custom rules ---------------------------------------------


def _wasm_runnable(doc: TutorialDoc) -> bool:
    """True when a tutorial opts into wasm and no step leaves the wasm subset."""
    fm = doc.front_matter
    wasm = str((fm.runtime or {}).get("wasm", "never")) if fm else "never"
    if wasm == "never":
        return False
    return all(not non_wasm_reasons(s.sql) for s in doc.steps if s.role != ROLE_ILLUSTRATIVE)


def _make_fence_rule(run_enabled: bool):  # type: ignore[no-untyped-def]
    """Return a fence renderer; the Run button is live only when ``run_enabled``."""

    def _fence_rule(tokens, idx, options, env, *_):  # type: ignore[no-untyped-def]
        tok = tokens[idx]
        lang, attrs, _err = parse_fence_info(tok.info)
        if lang == "result":
            return _render_result_table(_parse_result_block(tok.content))
        if lang == "sql":
            role = attrs.get("role", "step")
            illus = role == ROLE_ILLUSTRATIVE
            tag = f'<span class="role role-{html.escape(role)}">{html.escape(role)}</span>'
            if run_enabled and not illus:
                run = '<button class="run-btn" title="Run against the worker">▶ Run</button>'
            else:
                run = (
                    '<button class="run-btn" disabled '
                    'title="Live run needs a signed-in, running worker">▶ Run</button>'
                )
            return (
                '<div class="code-card">'
                f'<div class="code-head">{tag}'
                f'<span class="code-actions">{run}'
                '<button class="copy-btn" title="Copy">Copy</button></span></div>'
                f'<pre class="sql{" illus" if illus else ""}">{_highlight_sql(tok.content)}</pre>'
                "</div>"
            )
        # Any other fenced block: plain, escaped.
        label = (
            f'<div class="code-head"><span class="role">{html.escape(lang or "text")}</span></div>'
        )
        return f'<div class="code-card">{label}<pre>{html.escape(tok.content)}</pre></div>'

    return _fence_rule


def _make_image_rule(base_dir: Path):  # type: ignore[no-untyped-def]
    """Return an image renderer that embeds local images as data URIs."""

    def _image_rule(tokens, idx, options, env, *_):  # type: ignore[no-untyped-def]
        tok = tokens[idx]
        src = tok.attrGet("src") or ""
        alt = tok.content or tok.attrGet("alt") or ""
        uri = _data_uri(base_dir / src) if src and not src.startswith(("http", "data:")) else src
        return f'<img src="{html.escape(uri)}" alt="{html.escape(alt)}" loading="lazy">'

    return _image_rule


# --- helpers ---------------------------------------------------------------


def _highlight_sql(sql: str) -> str:
    """Tokenize SQL into escaped, span-wrapped HTML (comments/strings/numbers/keywords)."""
    pattern = re.compile(
        r"(?P<comment>--[^\n]*)"
        r"|(?P<string>'(?:[^']|'')*')"
        r"|(?P<number>\b\d+\.?\d*\b)"
        r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
        r"|(?P<other>.)",
        re.DOTALL,
    )
    out: list[str] = []
    for m in pattern.finditer(sql):
        kind = m.lastgroup
        text = m.group()
        esc = html.escape(text)
        if kind == "comment":
            out.append(f'<span class="c-com">{esc}</span>')
        elif kind == "string":
            out.append(f'<span class="c-str">{esc}</span>')
        elif kind == "number":
            out.append(f'<span class="c-num">{esc}</span>')
        elif kind == "word" and text.lower() in _SQL_KEYWORDS:
            out.append(f'<span class="c-kw">{esc}</span>')
        else:
            out.append(esc)
    return "".join(out)


def _render_result_table(block: ResultBlock) -> str:
    """Render a pinned result block as a table, with swatches for hex-color cells."""
    if not block.columns and not block.rows:
        return '<div class="result"><em>(no rows)</em></div>'
    head = "".join(f"<th>{html.escape(c)}</th>" for c in block.columns)
    body_rows = []
    for row in block.rows:
        cells = "".join(f"<td>{_cell(v)}</td>" for v in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="result"><span class="result-label">result</span>'
        f"<table><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def _cell(value: str) -> str:
    """Render one result cell, prefixing a color swatch when it is a hex color."""
    esc = html.escape(value)
    if _HEX.match(value.strip()):
        return f'<span class="swatch" style="background:{esc}"></span>{esc}'
    return esc


def _data_uri(path: Path) -> str:
    """Read a local image and return a base64 ``data:`` URI (or the path on failure)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return str(path)
    mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _render_nav(nav: TutorialNav | None, current_slug: str) -> str:
    """Render the series nav: 'in this series' list + prev/next pager."""
    if nav is None or not nav.siblings:
        return ""
    items = []
    for e in nav.siblings:
        label = html.escape(e.title or e.slug)
        if e.slug == current_slug:
            items.append(f'<li class="here"><span>{label}</span></li>')
        else:
            items.append(f'<li><a href="{html.escape(e.slug)}.html">{label}</a></li>')
    series = (
        '<div class="series"><div class="series-head">In this series'
        + (f" · {html.escape(nav.hub_title)}" if nav.hub_title else "")
        + f"</div><ol>{''.join(items)}</ol></div>"
    )
    pager_parts = []
    if nav.prev_slug:
        pager_parts.append(
            f'<a class="pg prev" href="{html.escape(nav.prev_slug)}.html">'
            f"<span>← Previous</span>{html.escape(nav.prev_title or nav.prev_slug)}</a>"
        )
    if nav.next_slug:
        pager_parts.append(
            f'<a class="pg next" href="{html.escape(nav.next_slug)}.html">'
            f"<span>Next →</span>{html.escape(nav.next_title or nav.next_slug)}</a>"
        )
    pager = f'<div class="pager">{"".join(pager_parts)}</div>' if pager_parts else ""
    return f'<aside class="tut-nav">{series}{pager}</aside>'


def render_hub(hub: TutorialHub, docs: list[TutorialDoc]) -> str:
    """Render a worker's hub landing page listing its tutorial series."""
    by_slug = {d.slug: d for d in docs}
    cards = []
    for n, e in enumerate(hub.entries, start=1):
        d = by_slug.get(e.slug)
        fm = d.front_matter if d else None
        title = e.title or (fm.title if fm else None) or e.slug
        summary = e.summary or (fm.description if fm else None) or ""
        chips = ""
        if fm:
            bits = [
                b
                for b in (
                    fm.tier,
                    fm.difficulty,
                    f"{fm.est_minutes} min" if fm.est_minutes else None,
                )
                if b
            ]
            chips = "".join(f'<span class="chip">{html.escape(str(b))}</span>' for b in bits)
        cards.append(
            f'<a class="hub-card" href="{html.escape(e.slug)}.html">'
            f'<span class="hub-num">{n:02d}</span>'
            f'<span class="hub-main"><span class="hub-title">{html.escape(title)}</span>'
            f'<span class="hub-sum">{html.escape(summary)}</span>'
            f'<span class="chips">{chips}</span></span></a>'
        )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(hub.title)}</title>\n"
        f'<meta name="description" content="{html.escape(hub.description)}">\n'
        f"<style>{_CSS}</style>\n</head><body>\n"
        '<main class="tutorial">\n'
        '<nav class="crumbs">Tutorials</nav>\n'
        f"<h1>{html.escape(hub.title)}</h1>\n"
        f'<p class="lede">{html.escape(hub.description)}</p>\n'
        f'<div class="hub-list">{"".join(cards)}</div>\n'
        '<footer class="tut-footer">Rendered by '
        "<code>vgi-lint tutorials build</code> · a Query.Farm VGI worker tutorial suite</footer>\n"
        "</main>\n</body></html>\n"
    )


def _render_header(doc: TutorialDoc) -> str:
    """Render the breadcrumb + title + metadata chips + canonical ATTACH snippet."""
    fm = doc.front_matter
    title = (fm.title if fm else None) or doc.slug
    tier = fm.tier if fm else None
    crumb = " › ".join(html.escape(x) for x in ["Tutorials", *([tier.title()] if tier else [])])
    chips = []
    if fm:
        for val in (
            fm.difficulty,
            f"{fm.est_minutes} min" if fm.est_minutes else None,
            fm.tier,
        ):
            if val:
                chips.append(f'<span class="chip">{html.escape(str(val))}</span>')
    chip_html = "".join(chips)
    desc = html.escape(fm.description) if fm and fm.description else ""

    attach = ""
    if fm and fm.attach:
        spec = fm.attach[0]
        dv = f", data_version_spec '{html.escape(spec.data_version)}'" if spec.data_version else ""
        stmt = (
            f"ATTACH '{html.escape(spec.worker)}' AS {html.escape(spec.worker)} "
            f"(TYPE vgi, LOCATION '&lt;your-worker-endpoint&gt;'{dv});"
        )
        attach = (
            '<div class="code-card attach"><div class="code-head">'
            '<span class="role">attach</span>'
            '<span class="code-actions"><button class="copy-btn">Copy</button></span></div>'
            f'<pre class="sql">INSTALL vgi <span class="c-kw">FROM</span> community; '
            f'<span class="c-kw">LOAD</span> vgi;\n{stmt}</pre></div>'
        )
    return (
        f'<nav class="crumbs">{crumb}</nav>'
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="lede">{desc}</p>'
        f'<div class="chips">{chip_html}</div>'
        f"{attach}"
    )


_CSS = """
:root {
  --bg:#ffffff; --fg:#1c2024; --muted:#6b7280; --line:#e5e7eb; --soft:#f6f8fa;
  --accent:#3b6fed;
  /* Code cards are a fixed dark surface in both themes, so the syntax palette is
     theme-independent and tuned for strong contrast on that dark background. */
  --card:#11151f; --card-fg:#eef2f8;
  --kw:#e39bff; --str:#8ff0a0; --num:#ffb26b; --com:#aab4c2; --punc:#c7d0dc;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0d1117; --fg:#e6edf3; --muted:#9aa4b2; --line:#232a33; --soft:#161b22;
    --accent:#6a97ff;
    --card:#0a0e14;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.tutorial { max-width:760px; margin:0 auto; padding:56px 24px 96px; }
.crumbs { color:var(--muted); font-size:13px; letter-spacing:.02em; text-transform:uppercase; }
h1 { font-size:2.1rem; line-height:1.15; margin:.35em 0 .2em; letter-spacing:-.02em; }
.lede { font-size:1.15rem; color:var(--muted); margin:.2em 0 1.1em; }
h2 { font-size:1.5rem; margin:2.2em 0 .5em; letter-spacing:-.01em; }
h3 { font-size:1.15rem; margin:1.8em 0 .4em; }
p, li { color:var(--fg); }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
img { max-width:100%; height:auto; border-radius:10px; margin:1.2em 0; }
.chips { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 1.6em; }
.chip {
  font-size:12px; font-weight:600; padding:3px 10px; border-radius:999px;
  background:var(--soft); color:var(--muted); border:1px solid var(--line);
  text-transform:capitalize;
}
.banner {
  background:#fff4e5; color:#8a4b00; border:1px solid #ffd8a8; border-radius:8px;
  padding:10px 14px; margin-bottom:20px; font-size:14px;
}
.code-card {
  background:var(--card); border-radius:12px; overflow:hidden; margin:1.3em 0;
  border:1px solid var(--line); box-shadow:0 1px 2px rgba(0,0,0,.04);
}
.code-card.attach { margin:1.2em 0 0; }
.code-head {
  display:flex; align-items:center; justify-content:space-between;
  padding:8px 12px; background:rgba(255,255,255,.04); border-bottom:1px solid rgba(255,255,255,.06);
}
.code-actions { display:flex; gap:6px; }
.role {
  font-size:11px; font-weight:700; letter-spacing:.05em; text-transform:uppercase;
  color:#8b98a9;
}
.role-step { color:#6a97ff; } .role-setup { color:#7ee787; }
.role-teardown { color:#ffab70; } .role-illustrative { color:#d2a8ff; }
.copy-btn, .run-btn {
  font:600 12px/1 inherit; color:#c9d4e2; background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.10); border-radius:6px; padding:5px 9px; cursor:pointer;
}
.copy-btn:hover { background:rgba(255,255,255,.12); }
.run-btn[disabled] { opacity:.45; cursor:not-allowed; }
pre.sql, .code-card pre {
  margin:0; padding:14px 16px; overflow-x:auto; color:var(--card-fg);
  font:13.5px/1.6 ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
pre.sql.illus { opacity:.82; }
.c-kw { color:var(--kw); font-weight:600; } .c-str { color:var(--str); }
.c-num { color:var(--num); } .c-com { color:var(--com); font-style:italic; }
.result {
  margin:-.4em 0 1.4em; border:1px solid var(--line); border-top:none;
  border-radius:0 0 12px 12px; overflow-x:auto; background:var(--bg);
}
.result-label {
  display:block; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); padding:7px 14px; background:var(--soft); border-bottom:1px solid var(--line);
}
.result table { border-collapse:collapse; width:100%; font-size:13.5px; }
.result th, .result td {
  text-align:left; padding:7px 14px; border-bottom:1px solid var(--line);
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; white-space:nowrap;
}
.result th { color:var(--muted); font-weight:600; }
.result tr:last-child td { border-bottom:none; }
.swatch {
  display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:7px;
  vertical-align:-1px; border:1px solid rgba(128,128,128,.4);
}
.tut-footer { margin-top:4em; padding-top:1.4em; border-top:1px solid var(--line);
  color:var(--muted); font-size:13px; }
code { background:var(--soft); padding:1px 6px; border-radius:5px; font-size:.9em; }
blockquote { border-left:3px solid var(--line); margin:1.2em 0; padding:.2em 1em;
  color:var(--muted); }
/* series navigation */
.tut-nav { margin-top:3em; }
.series { border:1px solid var(--line); border-radius:12px; padding:14px 18px;
  background:var(--soft); }
.series-head { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); margin-bottom:8px; }
.series ol { margin:0; padding-left:1.3em; }
.series li { margin:3px 0; }
.series li.here span { color:var(--fg); font-weight:600; }
.series li.here::marker { color:var(--accent); }
.pager { display:flex; gap:12px; margin-top:14px; }
.pg { flex:1; border:1px solid var(--line); border-radius:10px; padding:10px 14px;
  display:flex; flex-direction:column; font-weight:600; }
.pg span { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
  color:var(--muted); }
.pg.next { text-align:right; }
.pg:hover { border-color:var(--accent); text-decoration:none; }
/* hub landing page */
.hub-list { display:flex; flex-direction:column; gap:12px; margin-top:1.5em; }
.hub-card { display:flex; gap:16px; align-items:flex-start; padding:16px 18px;
  border:1px solid var(--line); border-radius:12px; color:var(--fg); }
.hub-card:hover { border-color:var(--accent); text-decoration:none;
  box-shadow:0 2px 10px rgba(0,0,0,.05); }
.hub-num { font:700 15px ui-monospace,Menlo,monospace; color:var(--muted);
  padding-top:2px; }
.hub-main { display:flex; flex-direction:column; gap:4px; }
.hub-title { font-size:1.15rem; font-weight:650; color:var(--accent); }
.hub-sum { color:var(--muted); font-size:.95rem; }
.hub-card .chips { margin-top:6px; }
"""

_SCRIPT = """
document.querySelectorAll('.copy-btn').forEach(function(btn){
  btn.addEventListener('click', function(){
    var card = btn.closest('.code-card');
    var pre = card && card.querySelector('pre');
    if (!pre) return;
    navigator.clipboard.writeText(pre.innerText).then(function(){
      var t = btn.textContent; btn.textContent = 'Copied'; btn.disabled = true;
      setTimeout(function(){ btn.textContent = t; btn.disabled = false; }, 1200);
    });
  });
});
// Progressive enhancement: when the page was built with a worker endpoint,
// POST a step's SQL to it and render the returned {columns, rows}. The endpoint
// protocol is a deployment convention; without it the button stays disabled.
var endpoint = document.documentElement.getAttribute('data-wasm-endpoint');
if (endpoint) {
  document.querySelectorAll('.run-btn:not([disabled])').forEach(function(btn){
    btn.addEventListener('click', function(){
      var pre = btn.closest('.code-card').querySelector('pre');
      btn.textContent = '…';
      fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({sql: pre.innerText})})
        .then(function(r){ return r.json(); })
        .then(function(){ btn.textContent = '▶ Run'; })
        .catch(function(){ btn.textContent = 'Run failed'; });
    });
  });
}
"""


# --- CLI: python -m vgi_lint_check.tutorials.render FILE... [--out DIR] -----


def main(argv: list[str] | None = None) -> int:
    """Render tutorials to HTML (prototype entry point).

    Each path may be a ``*.vgi.md`` file or a directory. A directory containing
    an ``index.vgi.yaml`` hub is rendered as a *suite*: a hub landing page plus
    each spoke with series nav; otherwise its tutorials render standalone.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    out_dir = Path(".")
    paths: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_dir = Path(args[i + 1])
            i += 2
        else:
            paths.append(args[i])
            i += 1
    out_dir.mkdir(parents=True, exist_ok=True)

    for path in paths:
        p = Path(path)
        if p.is_dir():
            _render_dir(p, out_dir)
        else:
            _write(load_tutorial(p), out_dir, nav=None)
    return 0


def _render_dir(directory: Path, out_dir: Path) -> None:
    """Render a directory of tutorials, as a suite when it carries a hub."""
    docs = load_dir(directory)
    hub = find_hub(directory)
    if hub is not None:
        target = out_dir / "index.html"
        target.write_text(render_hub(hub, docs), encoding="utf-8")
        print(f"wrote {target}  (hub: {hub.title})")
        for doc in docs:
            _write(doc, out_dir, nav=nav_for(hub, doc.slug))
    else:
        for doc in docs:
            _write(doc, out_dir, nav=None)


def _write(doc: TutorialDoc, out_dir: Path, *, nav: TutorialNav | None) -> None:
    """Render one doc to ``<slug>.html`` in ``out_dir``."""
    target = out_dir / f"{doc.slug}.html"
    target.write_text(render_html(doc, nav=nav), encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
