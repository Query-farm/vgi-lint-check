"""VGI13xx — tutorial quality rules (a parallel engine over ``*.vgi.md`` files).

Tutorials are file-sourced (not catalog objects), so they get their own thin
harness rather than riding the catalog-centric ``RuleContext`` loop. Rules here
subclass :class:`~vgi_lint_check.rules.base.Rule` (to reuse ``Finding`` /
severity resolution) but run via :func:`run_tutorial_rules` against a
:class:`TutorialContext`, and register into a dedicated ``TUTORIAL_REGISTRY`` so
they never leak into ``vgi-lint lint`` and vice-versa.

Static rules need no worker connection. Execution/catalog rules
(``requires_connection``, VGI1340–1343) are auto-disabled unless ``verify
--execute`` attaches the worker (the same capability gate as VGI9xx); the LLM
narrative judge (VGI1370, ``requires_review``) runs only under ``--judge``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from ..config import Config
from ..findings import Category, Finding, Severity
from ..model import Catalog, ObjectId, ObjectKind
from ..tutorials.model import (
    ASSET_KINDS,
    EXPECT_KINDS,
    ROLE_ILLUSTRATIVE,
    ROLE_STEP,
    ROLES,
    StepResult,
    TutorialDoc,
    TutorialHub,
)
from ..tutorials.wasm import non_wasm_reasons
from .base import Rule, RuleContext

_MD = MarkdownIt("commonmark")

DIFFICULTIES = ("beginner", "intermediate", "advanced")
TIERS = ("quickstart", "recipe", "composition")
_REQUIRED_FM = (
    "title",
    "workers",
    "description",
    "slug",
    "keywords",
    "difficulty",
    "est_minutes",
    "dataset",
    "date_published",
    "date_modified",
    "tier",
)
_ASSET_EXTS = {
    "data": {".parquet", ".csv", ".json", ".tsv", ".ndjson"},
    "image": {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"},
    "media": {".mp3", ".mp4", ".wav", ".webm", ".ogg"},
}
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEARCH_PATH = re.compile(r"\bset\s+search_path\b", re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"\bfoo\b|\bbar\b|\bbaz\b|\bqux\b|lorem ipsum|example@example|john doe|123-45-6789",
    re.IGNORECASE,
)
_SUPERLATIVE = re.compile(
    r"\bblazing(?:ly)?\b|\brevolutionary\b|\bseamless(?:ly)?\b|\beffortless(?:ly)?\b"
    r"|\bgame.?changer\b|\bworld.?class\b|\bcutting.?edge\b|\b10x\b|\bmagical?\b",
    re.IGNORECASE,
)
_REF_TITLE = re.compile(r"\b(reference|api|function list|cheat.?sheet)\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
@dataclass
class TutorialContext:
    """Per-run context for tutorial rules (analogous to ``RuleContext``)."""

    docs: list[TutorialDoc]
    config: Config
    hub: TutorialHub | None = None
    # Populated only under ``verify --execute``.
    catalogs: dict[str, Catalog] | None = None
    connection: Any | None = None
    results: dict[str, Any] | None = None
    # An LLM backend, present only under ``--judge`` (the requires_review rule).
    backend: Any | None = None
    severity: Severity = Severity.WARNING


class TutorialRule(Rule):
    """Base for VGI13xx rules. The ``RuleContext`` ``check`` path is unused."""

    category = Category.TUTORIAL

    def check(self, ctx: RuleContext) -> Iterable[Finding]:  # pragma: no cover
        raise NotImplementedError("tutorial rules run via run_tutorial_rules()")

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        yield from ()

    def _finding(
        self,
        ctx: TutorialContext,
        doc: TutorialDoc,
        message: str,
        hint: str,
        *,
        step: int | None = None,
    ) -> Finding:
        workers = doc.front_matter.workers if doc.front_matter else []
        oid = ObjectId(
            database=workers[0] if workers else "tutorial",
            kind=ObjectKind.TUTORIAL,
            name=Path(doc.path).name,
            column=f"step{step}" if step is not None else None,
        )
        return Finding(
            code=self.code,
            severity=ctx.severity,
            category=Category.TUTORIAL,
            object_id=oid,
            message=message,
            hint=hint,
        )


TUTORIAL_REGISTRY: dict[str, type[TutorialRule]] = {}


def register_tutorial(cls: type[TutorialRule]) -> type[TutorialRule]:
    """Register a tutorial rule (collision-detected), like ``@register``."""
    code = getattr(cls, "code", None)
    if not code:
        raise ValueError(f"tutorial rule {cls.__name__} has no code")
    if code in TUTORIAL_REGISTRY:
        raise ValueError(f"duplicate tutorial rule code {code}")
    TUTORIAL_REGISTRY[code] = cls
    return cls


def all_tutorial_rule_classes() -> list[type[TutorialRule]]:
    """All registered tutorial rule classes, sorted by code."""
    return [TUTORIAL_REGISTRY[c] for c in sorted(TUTORIAL_REGISTRY)]


def tutorial_rules() -> list[TutorialRule]:
    """Instantiate every tutorial rule."""
    return [cls() for cls in all_tutorial_rule_classes()]


def run_tutorial_rules(rules: list[TutorialRule], ctx: TutorialContext) -> list[Finding]:
    """Run tutorial rules, resolving severity/select/ignore via the shared Config."""
    out: list[Finding] = []
    for rule in rules:
        sev = ctx.config.effective_severity(rule)
        if sev is Severity.OFF:
            continue
        ctx.severity = sev
        for f in rule.evaluate(ctx):
            if not ctx.config.is_object_ignored(f.object_id, f.code):
                out.append(f)
    out.sort(key=lambda f: f.sort_key())
    return out


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def _prose(doc: TutorialDoc) -> str:
    """Body markdown with fenced code blocks removed (for prose analysis)."""
    return re.sub(r"(?ms)^[ \t]*(```+|~~~+).*?^[ \t]*\1[ \t]*$", " ", doc.body_md)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text)


def _links(doc: TutorialDoc) -> list[str]:
    """Href targets of every Markdown link in the body."""
    out: list[str] = []
    for tok in _MD.parse(doc.body_md):
        for child in tok.children or []:
            if child.type == "link_open":
                out.append(str(child.attrGet("href") or ""))
    return out


def _image_srcs(doc: TutorialDoc) -> list[str]:
    out: list[str] = []
    for tok in _MD.parse(doc.body_md):
        for child in tok.children or []:
            if child.type == "image":
                out.append(str(child.attrGet("src") or ""))
    return out


def _first_code_offset(body: str) -> int:
    m = re.search(r"(?m)^[ \t]*(```|~~~)", body)
    return m.start() if m else len(body)


def _normalized(doc: TutorialDoc) -> str:
    return re.sub(r"\s+", " ", _prose(doc).lower()).strip()


# --------------------------------------------------------------------------
# Structure / front-matter (VGI1300–1303)
# --------------------------------------------------------------------------
@register_tutorial
class TutorialParses(TutorialRule):
    code = "VGI1300"
    name = "tutorial-parses"
    default_severity = Severity.ERROR
    summary = "A .vgi.md tutorial must parse (front-matter + fenced steps)."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            if doc.parse_error:
                yield self._finding(
                    ctx,
                    doc,
                    f"tutorial does not parse: {doc.parse_error}",
                    "fix the front-matter/fence structure so the tutorial can be loaded",
                )


@register_tutorial
class TutorialRequiredFrontMatter(TutorialRule):
    code = "VGI1301"
    name = "tutorial-frontmatter-required"
    default_severity = Severity.ERROR
    summary = "A tutorial must declare the full required front-matter set."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            fm = doc.front_matter
            if fm is None:
                continue  # VGI1300 owns the no-front-matter case
            missing = [k for k in _REQUIRED_FM if not getattr(fm, k, None)]
            for msg in doc.fm_errors:
                yield self._finding(ctx, doc, f"front-matter invalid: {msg}", "fix the field")
            if missing:
                yield self._finding(
                    ctx,
                    doc,
                    f"front-matter is missing required keys: {', '.join(missing)}",
                    "add the missing keys (run `vgi-lint tutorials init` for a compliant skeleton)",
                )


@register_tutorial
class TutorialFieldValidity(TutorialRule):
    code = "VGI1302"
    name = "tutorial-frontmatter-valid"
    default_severity = Severity.ERROR
    summary = "Front-matter fields must be well-formed (enums, ISO dates, slug)."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            fm = doc.front_matter
            if fm is None:
                continue
            if fm.difficulty and fm.difficulty not in DIFFICULTIES:
                yield self._finding(
                    ctx,
                    doc,
                    f"difficulty {fm.difficulty!r} is not one of {DIFFICULTIES}",
                    "use beginner, intermediate, or advanced",
                )
            if fm.tier and fm.tier not in TIERS:
                yield self._finding(
                    ctx,
                    doc,
                    f"tier {fm.tier!r} is not one of {TIERS}",
                    "use quickstart, recipe, or composition",
                )
            if fm.slug:
                if not _SLUG.match(fm.slug):
                    yield self._finding(
                        ctx,
                        doc,
                        f"slug {fm.slug!r} is not kebab-case",
                        "use lowercase words joined by hyphens",
                    )
                stem = Path(doc.path).name.removesuffix(".vgi.md")
                if fm.slug != stem:
                    yield self._finding(
                        ctx,
                        doc,
                        f"slug {fm.slug!r} does not match filename {stem!r}",
                        "make the slug equal the filename stem so URLs are predictable",
                    )
            dp, dm = self._parse_date(fm.date_published), self._parse_date(fm.date_modified)
            for label, raw in (
                ("datePublished", fm.date_published),
                ("dateModified", fm.date_modified),
            ):
                if raw and not _ISO_DATE.match(raw):
                    yield self._finding(
                        ctx, doc, f"{label} {raw!r} is not ISO YYYY-MM-DD", "use ISO dates"
                    )
            if dp and dm and dm < dp:
                yield self._finding(
                    ctx,
                    doc,
                    "dateModified is before datePublished",
                    "dateModified must be on or after datePublished",
                )

    @staticmethod
    def _parse_date(raw: str | None) -> date | None:
        if not raw or not _ISO_DATE.match(raw):
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None


@register_tutorial
class TutorialAttachDirective(TutorialRule):
    code = "VGI1303"
    name = "tutorial-attach-directive"
    default_severity = Severity.ERROR
    summary = "A tutorial must name at least one worker to attach."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            fm = doc.front_matter
            if fm is None:
                continue
            if not fm.attach:
                yield self._finding(
                    ctx,
                    doc,
                    "no worker declared (need `worker:` or an `attach:` list)",
                    "declare the worker identity so the runner can attach it at run time",
                )


# --------------------------------------------------------------------------
# Fences / steps (VGI1310–1314)
# --------------------------------------------------------------------------
@register_tutorial
class TutorialFenceAttrs(TutorialRule):
    code = "VGI1310"
    name = "tutorial-fence-attrs"
    default_severity = Severity.ERROR
    summary = "Every SQL fence must use a known role and expect kind."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            for s in doc.steps:
                if s.role not in ROLES:
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {s.index} has unknown role {s.role!r}",
                        f"use one of {ROLES}",
                        step=s.index,
                    )
                if s.expect is not None and s.expect not in EXPECT_KINDS:
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {s.index} has unknown expect {s.expect!r}",
                        f"use one of {EXPECT_KINDS}",
                        step=s.index,
                    )


@register_tutorial
class TutorialProblemBeforeCode(TutorialRule):
    code = "VGI1311"
    name = "tutorial-problem-before-code"
    default_severity = Severity.WARNING
    summary = "A tutorial must state the problem in prose before its first code block."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            before = doc.body_md[: _first_code_offset(doc.body_md)]
            prose_words = _words(re.sub(r"(?m)^#{1,6}\s.*$", " ", before))
            if len(prose_words) < 20:
                yield self._finding(
                    ctx,
                    doc,
                    "no problem statement before the first code block",
                    "open with a paragraph naming the real task, before any SQL",
                )
            first_step = next((s for s in doc.steps if s.role == ROLE_STEP), None)
            if first_step and not re.match(r"\s*(select|with)\b", first_step.sql, re.IGNORECASE):
                yield self._finding(
                    ctx,
                    doc,
                    "the first step does not return rows (starts with non-SELECT)",
                    "lead with a query that produces a visible result (the fast 'aha')",
                    step=first_step.index,
                )


@register_tutorial
class TutorialExpectResultCoherent(TutorialRule):
    code = "VGI1312"
    name = "tutorial-expect-result-coherent"
    default_severity = Severity.WARNING
    summary = "A step's expect kind must agree with its pinned result block."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            for s in doc.steps:
                if s.expect == "error" and s.has_expected:
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {s.index} expects an error but pins a result table",
                        "an error step has no rows — drop the ```result``` block",
                        step=s.index,
                    )


@register_tutorial
class TutorialNoSearchPath(TutorialRule):
    code = "VGI1313"
    name = "tutorial-no-search-path"
    default_severity = Severity.ERROR
    summary = "Tutorial SQL must not SET search_path; use fully-qualified names."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            for s in doc.steps:
                if _SEARCH_PATH.search(s.sql):
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {s.index} uses SET search_path",
                        "drop it and fully-qualify calls (catalog.schema.fn) so each step is "
                        "self-contained and multi-worker compositions work",
                        step=s.index,
                    )


@register_tutorial
class TutorialIllustrativeNoPinnedResult(TutorialRule):
    code = "VGI1314"
    name = "tutorial-illustrative-not-verified"
    default_severity = Severity.WARNING
    summary = "An illustrative (non-run) step should not pin a result that can't be verified."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            for s in doc.steps:
                if s.role == ROLE_ILLUSTRATIVE and s.has_expected and s.expect != "error":
                    yield self._finding(
                        ctx,
                        doc,
                        f"illustrative step {s.index} pins a result but is never run",
                        "make it a `role=step` so `verify` checks the result, or drop the block",
                        step=s.index,
                    )


# --------------------------------------------------------------------------
# SEO / marketing (VGI1320–1326)
# --------------------------------------------------------------------------
@register_tutorial
class TutorialTitleShape(TutorialRule):
    code = "VGI1320"
    name = "tutorial-title-task-shaped"
    default_severity = Severity.WARNING
    summary = "The title should be task-shaped and a searchable length, not a reference label."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        opts = ctx.config.options
        for doc in ctx.docs:
            fm = doc.front_matter
            if fm is None or not fm.title:
                continue
            n = len(fm.title)
            if n < opts.tutorial_title_min or n > opts.tutorial_title_max:
                yield self._finding(
                    ctx,
                    doc,
                    f"title is {n} chars "
                    f"(want {opts.tutorial_title_min}-{opts.tutorial_title_max})",
                    "write a task-shaped title someone would actually search for",
                )
            if _REF_TITLE.search(fm.title):
                yield self._finding(
                    ctx,
                    doc,
                    "title reads like an API reference, not a task",
                    "name the job (verb + real-world noun), not the function surface",
                )


@register_tutorial
class TutorialDescription(TutorialRule):
    code = "VGI1321"
    name = "tutorial-description"
    default_severity = Severity.WARNING
    summary = "The description should be a unique meta-description length."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        opts = ctx.config.options
        seen: dict[str, str] = {}
        for doc in ctx.docs:
            fm = doc.front_matter
            if fm is None or not fm.description:
                continue
            n = len(fm.description)
            if n < opts.tutorial_description_min or n > opts.tutorial_description_max:
                yield self._finding(
                    ctx,
                    doc,
                    f"description is {n} chars (want {opts.tutorial_description_min}-"
                    f"{opts.tutorial_description_max})",
                    "write a self-contained meta description of the outcome",
                )
            prior = seen.get(fm.description)
            if prior:
                yield self._finding(
                    ctx,
                    doc,
                    f"description is identical to {prior}",
                    "give every tutorial a distinct description (duplicate = thin content)",
                )
            else:
                seen[fm.description] = Path(doc.path).name


@register_tutorial
class TutorialNoPlaceholderData(TutorialRule):
    code = "VGI1322"
    name = "tutorial-no-placeholder-data"
    default_severity = Severity.WARNING
    summary = "Tutorials should use real, recognizable data — not foo/bar placeholders."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            hay = doc.body_md
            m = _PLACEHOLDER.search(hay)
            if m:
                yield self._finding(
                    ctx,
                    doc,
                    f"placeholder data present (e.g. {m.group(0)!r})",
                    "use a real, named dataset a reader recognizes",
                )


@register_tutorial
class TutorialNoSuperlatives(TutorialRule):
    code = "VGI1323"
    name = "tutorial-no-superlatives"
    default_severity = Severity.WARNING
    summary = "Avoid unsubstantiated superlatives (blazing-fast, revolutionary, …)."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            m = _SUPERLATIVE.search(_prose(doc))
            if m:
                yield self._finding(
                    ctx,
                    doc,
                    f"unsubstantiated superlative {m.group(0)!r}",
                    "show, don't claim — back performance claims with a runnable query",
                )


@register_tutorial
class TutorialNextStepsLinks(TutorialRule):
    code = "VGI1324"
    name = "tutorial-next-steps-links"
    default_severity = Severity.WARNING
    summary = "A tutorial should link out (next steps / related) — at least two links."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            if len(_links(doc)) < 2:
                yield self._finding(
                    ctx,
                    doc,
                    "fewer than two outbound links",
                    "add a next-steps/related section linking to docs and a related tutorial",
                )


@register_tutorial
class TutorialKeywordPlacement(TutorialRule):
    code = "VGI1325"
    name = "tutorial-keyword-placement"
    default_severity = Severity.INFO
    summary = "The primary keyword should appear in the title, opening, and description."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            fm = doc.front_matter
            if fm is None or not fm.keywords:
                continue
            kw = fm.keywords[0].lower()
            opening = " ".join(_words(_prose(doc))[:100]).lower()
            missing = [
                where
                for where, text in (
                    ("title", (fm.title or "").lower()),
                    ("opening", opening),
                    ("description", (fm.description or "").lower()),
                )
                if kw not in text
            ]
            if missing:
                yield self._finding(
                    ctx,
                    doc,
                    f"primary keyword {kw!r} missing from: {', '.join(missing)}",
                    "work the primary keyword into the title, first 100 words, and description",
                )


@register_tutorial
class TutorialAntiSameness(TutorialRule):
    code = "VGI1326"
    name = "tutorial-anti-sameness"
    default_severity = Severity.WARNING
    summary = "Tutorials must not be near-duplicates of each other (doorway-page risk)."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        limit = ctx.config.options.tutorial_similarity_max
        docs = [d for d in ctx.docs if d.body_md.strip()]
        norm = {id(d): _normalized(d) for d in docs}
        for i, a in enumerate(docs):
            for b in docs[i + 1 :]:
                ratio = SequenceMatcher(None, norm[id(a)], norm[id(b)]).ratio()
                if ratio >= limit:
                    yield self._finding(
                        ctx,
                        a,
                        f"{int(ratio * 100)}% similar to {Path(b.path).name} (formulaic)",
                        "vary the arc, dataset, and ending — each tutorial should teach a "
                        "distinct job, not a templated fill-in",
                    )


# --------------------------------------------------------------------------
# Assets (VGI1330–1334)
# --------------------------------------------------------------------------
@register_tutorial
class TutorialAssetsResolve(TutorialRule):
    code = "VGI1330"
    name = "tutorial-assets-resolve"
    default_severity = Severity.ERROR
    summary = "Referenced assets must exist on disk and be declared in front-matter."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            declared = {a.path for a in doc.assets}
            for a in doc.assets:
                if a.size_bytes is None:
                    yield self._finding(
                        ctx,
                        doc,
                        f"declared asset {a.path!r} is missing on disk",
                        "commit the file or remove the declaration",
                    )
            for src in _image_srcs(doc):
                if src.startswith(("http://", "https://", "data:")):
                    continue
                if src not in declared:
                    yield self._finding(
                        ctx,
                        doc,
                        f"image {src!r} is used but not declared in front-matter assets",
                        "declare every referenced asset so it is validated and embedded",
                    )


@register_tutorial
class TutorialAssetBudget(TutorialRule):
    code = "VGI1331"
    name = "tutorial-asset-budget"
    default_severity = Severity.ERROR
    summary = "Assets must fit the git size budget (per-file and per-tutorial total)."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        opts = ctx.config.options
        for doc in ctx.docs:
            total = 0
            for a in doc.assets:
                if a.size_bytes is None:
                    continue
                total += a.size_bytes
                if a.size_bytes > opts.tutorial_max_asset_bytes:
                    yield self._finding(
                        ctx,
                        doc,
                        f"asset {a.path!r} is {a.size_bytes} B "
                        f"(> {opts.tutorial_max_asset_bytes} B)",
                        "shrink it or use inline VALUES — assets live in git forever",
                    )
            if total > opts.tutorial_max_assets_total_bytes:
                yield self._finding(
                    ctx,
                    doc,
                    f"assets total {total} B (> {opts.tutorial_max_assets_total_bytes} B)",
                    "trim the tutorial's assets to fit the git budget",
                )


@register_tutorial
class TutorialAssetKinds(TutorialRule):
    code = "VGI1332"
    name = "tutorial-asset-kinds"
    default_severity = Severity.WARNING
    summary = "An asset's kind must be known and match its file extension."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            for a in doc.assets:
                if a.kind not in ASSET_KINDS:
                    yield self._finding(
                        ctx,
                        doc,
                        f"asset {a.path!r} has unknown kind {a.kind!r}",
                        f"use one of {ASSET_KINDS}",
                    )
                    continue
                ext = Path(a.path).suffix.lower()
                if ext not in _ASSET_EXTS[a.kind]:
                    yield self._finding(
                        ctx,
                        doc,
                        f"asset {a.path!r} ({a.kind}) has unexpected extension {ext!r}",
                        f"a {a.kind} asset should be one of {sorted(_ASSET_EXTS[a.kind])}",
                    )


@register_tutorial
class TutorialAssetMetadata(TutorialRule):
    code = "VGI1333"
    name = "tutorial-asset-metadata"
    default_severity = Severity.WARNING
    summary = "Images need alt text; data fixtures need provenance."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            for a in doc.assets:
                if a.kind == "image" and not (a.alt or "").strip():
                    yield self._finding(
                        ctx,
                        doc,
                        f"image asset {a.path!r} has no alt text",
                        "add descriptive alt text (accessibility + SEO)",
                    )
                if a.kind == "data" and not (a.provenance or "").strip():
                    yield self._finding(
                        ctx,
                        doc,
                        f"data asset {a.path!r} has no provenance",
                        "record where the fixture came from (synthetic, source URL, …)",
                    )


@register_tutorial
class TutorialAssetOrphans(TutorialRule):
    code = "VGI1334"
    name = "tutorial-asset-orphans"
    default_severity = Severity.WARNING
    summary = "A declared asset that is never referenced is dead weight."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            for a in doc.assets:
                if not a.referenced:
                    yield self._finding(
                        ctx,
                        doc,
                        f"declared asset {a.path!r} is never referenced",
                        "reference it from a step or the body, or remove the declaration",
                    )


# --------------------------------------------------------------------------
# wasm (VGI1350) — static; gates the in-browser Run button
# --------------------------------------------------------------------------
@register_tutorial
class TutorialWasmSubset(TutorialRule):
    code = "VGI1350"
    name = "tutorial-wasm-subset"
    default_severity = Severity.WARNING
    summary = "A wasm-enabled tutorial's steps must stay in the duckdb-wasm subset."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        for doc in ctx.docs:
            fm = doc.front_matter
            wasm = str((fm.runtime or {}).get("wasm", "never")) if fm else "never"
            if wasm == "never":
                continue
            for step in doc.steps:
                if step.role == ROLE_ILLUSTRATIVE:
                    continue
                for reason in non_wasm_reasons(step.sql):
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {step.index} can't run in duckdb-wasm: {reason}",
                        "set runtime.wasm: never, or avoid the non-wasm feature "
                        "(e.g. inline VALUES instead of reading a file)",
                        step=step.index,
                    )


# --------------------------------------------------------------------------
# Coverage / uniqueness (VGI1360, VGI1362)
# --------------------------------------------------------------------------
@register_tutorial
class TutorialSuiteHasQuickstart(TutorialRule):
    code = "VGI1360"
    name = "tutorial-suite-has-quickstart"
    default_severity = Severity.WARNING
    summary = "A worker with tutorials should have at least one quickstart."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        docs = [d for d in ctx.docs if d.front_matter]
        if not docs:
            return
        if not any(d.front_matter and d.front_matter.tier == "quickstart" for d in docs):
            yield self._finding(
                ctx,
                docs[0],
                "this tutorial suite has no quickstart",
                "add a quickstart tier tutorial — the fast first win for newcomers",
            )


@register_tutorial
class TutorialSlugUnique(TutorialRule):
    code = "VGI1362"
    name = "tutorial-slug-unique"
    default_severity = Severity.ERROR
    summary = "Tutorial slugs must be unique within a suite."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        seen: dict[str, str] = {}
        for doc in ctx.docs:
            prior = seen.get(doc.slug)
            if prior:
                yield self._finding(
                    ctx,
                    doc,
                    f"slug {doc.slug!r} is also used by {prior}",
                    "give every tutorial a unique slug (it's the URL)",
                )
            else:
                seen[doc.slug] = Path(doc.path).name


# --------------------------------------------------------------------------
# Execution / catalog (VGI1340–1343) — require a live worker (`verify --execute`)
# --------------------------------------------------------------------------
_QUALIFIED = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\.([A-Za-z_]\w*)")
_BARE_CALL = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\s*\(")


def _results_for(ctx: TutorialContext, doc: TutorialDoc) -> list[StepResult]:
    return list((ctx.results or {}).get(doc.slug) or [])


@register_tutorial
class TutorialRefsResolve(TutorialRule):
    code = "VGI1340"
    name = "tutorial-refs-resolve"
    default_severity = Severity.ERROR
    requires_connection = True
    summary = "Step SQL must reference real, fully-qualified worker objects."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        if not ctx.catalogs:
            return
        aliases = set(ctx.catalogs)
        bare_fns: set[str] = set()
        qualified: set[str] = set()
        for cat in ctx.catalogs.values():
            for fn in cat.iter_all_functions():
                bare_fns.add(fn.name)
                qualified.add(fn.id.qualified())
            for tbl in cat.iter_table_like():
                qualified.add(tbl.id.qualified())
        for doc in ctx.docs:
            for step in doc.steps:
                if step.role == ROLE_ILLUSTRATIVE:
                    continue
                for w, sch, nm in _QUALIFIED.findall(step.sql):
                    if w in aliases and f"{w}.{sch}.{nm}" not in qualified:
                        yield self._finding(
                            ctx,
                            doc,
                            f"step {step.index} references unknown object {w}.{sch}.{nm}",
                            "the worker has no such object — check the name against the catalog",
                            step=step.index,
                        )
                for nm in _BARE_CALL.findall(step.sql):
                    if nm in bare_fns:
                        yield self._finding(
                            ctx,
                            doc,
                            f"step {step.index} calls worker function {nm}() unqualified",
                            "qualify it as catalog.schema.fn so the step is self-contained",
                            step=step.index,
                        )


@register_tutorial
class TutorialStepsRun(TutorialRule):
    code = "VGI1341"
    name = "tutorial-steps-run"
    default_severity = Severity.ERROR
    requires_connection = True
    summary = "Every runnable step must execute (error steps must actually error)."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        if ctx.results is None:
            return
        for doc in ctx.docs:
            by_index = {s.index: s for s in doc.steps}
            for r in _results_for(ctx, doc):
                step = by_index.get(r.index)
                expect_error = step is not None and step.expect == "error"
                if expect_error and r.ok:
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {r.index} expected an error but succeeded",
                        "fix the expectation or the query",
                        step=r.index,
                    )
                elif not expect_error and not r.ok:
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {r.index} failed to run: {r.error}",
                        "fix the SQL so the step runs against the worker",
                        step=r.index,
                    )


@register_tutorial
class TutorialResultMatches(TutorialRule):
    code = "VGI1342"
    name = "tutorial-result-matches"
    default_severity = Severity.ERROR
    requires_connection = True
    summary = "A step's live output must match its pinned result block."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        if ctx.results is None:
            return
        for doc in ctx.docs:
            for r in _results_for(ctx, doc):
                if r.matched is False:
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {r.index} output does not match the pinned result",
                        "re-run and update the ```result``` block to the worker's actual output",
                        step=r.index,
                    )


@register_tutorial
class TutorialSlowStep(TutorialRule):
    code = "VGI1343"
    name = "tutorial-slow-step"
    default_severity = Severity.INFO
    requires_connection = True
    summary = "A step slower than options.slow_example_seconds bloats CI."

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        if ctx.results is None:
            return
        limit = ctx.config.slow_example_seconds
        if limit <= 0:
            return
        for doc in ctx.docs:
            for r in _results_for(ctx, doc):
                if r.elapsed > limit:
                    yield self._finding(
                        ctx,
                        doc,
                        f"step {r.index} took {r.elapsed:.1f}s (> {limit}s)",
                        "simplify the query or reduce the data it scans",
                        step=r.index,
                    )


# --------------------------------------------------------------------------
# LLM narrative judge (VGI1370) — requires `--judge`
# --------------------------------------------------------------------------
_JUDGE = (
    "Grade a DuckDB worker TUTORIAL for narrative quality. Score each 1-5: "
    "accuracy (SQL/claims correct), clarity (easy to follow), aha (delivers a "
    "concrete win fast), voice (anti-hype, shows-not-tells, no filler). Return ONE "
    'JSON object {{"accuracy":n,"clarity":n,"aha":n,"voice":n,"issue":"one line"}}.\n\n'
    "TITLE: {title}\nDESCRIPTION: {description}\n\nBODY:\n{body}"
)


@register_tutorial
class TutorialNarrativeJudge(TutorialRule):
    code = "VGI1370"
    name = "tutorial-narrative-quality"
    default_severity = Severity.WARNING
    requires_review = True
    summary = (
        "A tutorial's narrative should pass an LLM quality review (accuracy/clarity/aha/voice)."
    )

    def evaluate(self, ctx: TutorialContext) -> Iterator[Finding]:
        if ctx.backend is None:
            return
        from ..simulate import _extract_json

        threshold = ctx.config.options.doc_quality_min
        for doc in ctx.docs:
            fm = doc.front_matter
            if fm is None:
                continue
            prompt = _JUDGE.format(
                title=fm.title or "", description=fm.description or "", body=doc.body_md[:6000]
            )
            data = _extract_json(ctx.backend.complete(prompt))
            if not isinstance(data, dict):
                continue
            scores = [
                float(data[k])
                for k in ("accuracy", "clarity", "aha", "voice")
                if isinstance(data.get(k), int | float)
            ]
            if scores and sum(scores) / len(scores) < threshold:
                mean = sum(scores) / len(scores)
                yield self._finding(
                    ctx,
                    doc,
                    f"narrative quality below bar (mean {mean:.1f} < {threshold}): "
                    f"{data.get('issue', '')}",
                    "tighten the story — real problem, fast aha, anti-hype voice, accurate SQL",
                )


# --------------------------------------------------------------------------
# Rendering findings (tutorials lint output)
# --------------------------------------------------------------------------
def render_findings(findings: list[Finding], fmt: str = "terminal") -> str:
    """Render tutorial findings as grouped terminal text or JSON."""
    if fmt == "json":
        import json

        return json.dumps(
            [
                {
                    "code": f.code,
                    "severity": f.severity.label,
                    "file": f.object_id.name,
                    "step": f.object_id.column,
                    "message": f.message,
                    "hint": f.hint,
                }
                for f in findings
            ],
            indent=2,
        )
    if not findings:
        return "✓ no tutorial findings"
    by_code: dict[str, list[Finding]] = {}
    for f in findings:
        by_code.setdefault(f.code, []).append(f)
    lines: list[str] = []
    for code in sorted(by_code):
        group = by_code[code]
        cls = TUTORIAL_REGISTRY.get(code)
        head = f"{code} · {cls.name if cls else ''} — {cls.summary if cls else ''}"
        lines.append(head)
        for f in group:
            loc = f.object_id.name or "?"
            if f.object_id.column:
                loc = f"{loc}:{f.object_id.column}"
            lines.append(f"  [{f.severity.label}] {loc}  {f.message}")
            lines.append(f"      ↳ {f.hint}")
        lines.append("")
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity.label] = counts.get(f.severity.label, 0) + 1
    summary = ", ".join(f"{n} {label}" for label, n in sorted(counts.items()))
    lines.append(f"{len(findings)} finding(s): {summary}")
    return "\n".join(lines)
