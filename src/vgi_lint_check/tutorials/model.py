"""Immutable data model for a parsed tutorial.

The types here mirror the frozen-dataclass style of
:mod:`vgi_lint_check.model` (e.g. ``ExecutableExample`` / ``ExampleStatement``).
Loading is defensive: a malformed tutorial never raises — problems are recorded
on :class:`TutorialDoc` (``parse_error`` / ``fm_errors``) so they can become
lintable findings rather than crashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Fence roles, in execution order. ``illustrative`` blocks are rendered but
# never run.
ROLE_SETUP = "setup"
ROLE_STEP = "step"
ROLE_TEARDOWN = "teardown"
ROLE_ILLUSTRATIVE = "illustrative"
ROLES = (ROLE_SETUP, ROLE_STEP, ROLE_TEARDOWN, ROLE_ILLUSTRATIVE)

# Declared result-shape expectations for a step whose exact output is noisy.
EXPECT_KINDS = ("rows", "scalar", "error", "empty")

# Asset kinds and their allowed extensions (validated in a later phase).
ASSET_KINDS = ("data", "image", "media")


@dataclass(frozen=True)
class AttachSpec:
    """How to attach one worker for a tutorial.

    Names a worker *identity* plus an optional pinned **data** version; the
    actual ``LOCATION`` is supplied at run time, and the worker *code* is always
    the latest (``FORCE INSTALL vgi FROM community``). This is the "attach the
    latest worker, pin its data version" contract.
    """

    worker: str
    data_version: str | None = None
    alias: str | None = None


@dataclass(frozen=True)
class TutorialAsset:
    """A small static file shipped with a tutorial (data/image/media).

    ``size_bytes`` is filled by the loader (``stat``); ``referenced`` records
    whether the loader found a use of this asset in a SQL step or the body.
    """

    path: str
    kind: str
    alt: str | None = None
    provenance: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    referenced: bool = False


@dataclass(frozen=True)
class TutorialFrontMatter:
    """The YAML front-matter of a tutorial, coerced to typed fields.

    ``raw`` keeps the original mapping so a rule can inspect keys that aren't
    modelled here. Missing/invalid fields do not raise; they surface as
    ``TutorialDoc.fm_errors``.
    """

    title: str | None = None
    workers: list[str] = field(default_factory=list)
    description: str | None = None
    slug: str | None = None
    keywords: list[str] = field(default_factory=list)
    difficulty: str | None = None
    est_minutes: int | None = None
    dataset: object = None
    date_published: str | None = None
    date_modified: str | None = None
    tier: str | None = None
    attach: list[AttachSpec] = field(default_factory=list)
    runtime: dict[str, object] = field(default_factory=dict)
    assets: list[TutorialAsset] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TutorialStep:
    """One annotated SQL fence.

    ``expected_result`` is the parsed adjacent ```result``` block (``rows`` +
    ``columns``); ``has_expected`` distinguishes a declared-empty expectation
    from an omitted one, mirroring ``ExampleStatement``.
    """

    index: int
    role: str
    expect: str | None
    sql: str
    lang: str = "sql"
    expected_result: ResultBlock | None = None
    has_expected: bool = False
    line: int = 0


@dataclass(frozen=True)
class ResultBlock:
    """A pinned expected result: column names plus string-rendered rows."""

    columns: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class TutorialDoc:
    """A fully parsed tutorial.

    ``parse_error`` is a fatal, whole-file problem (unreadable / no
    front-matter / broken fence structure); ``fm_errors`` are per-field
    front-matter problems. Either way the doc is still returned so the renderer
    and linter can degrade gracefully.
    """

    path: str
    front_matter: TutorialFrontMatter | None
    steps: list[TutorialStep]
    body_md: str
    assets: list[TutorialAsset] = field(default_factory=list)
    parse_error: str | None = None
    fm_errors: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        """Best-effort slug for anchoring findings and naming output files."""
        if self.front_matter and self.front_matter.slug:
            return self.front_matter.slug
        return self.path.rsplit("/", 1)[-1].removesuffix(".vgi.md")


@dataclass(frozen=True)
class HubEntry:
    """One tutorial's place in a worker's suite (ordered series member)."""

    slug: str
    title: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class TutorialHub:
    """A worker's tutorial suite: the hub page copy plus the ordered series.

    Loaded from ``tutorials/index.vgi.yaml``. The hub owns suite-level
    structure — ordering and cross-links — so individual tutorials don't
    hand-wire links to each other.
    """

    worker: str
    title: str
    description: str
    entries: list[HubEntry]
    path: str = ""
    parse_error: str | None = None


@dataclass(frozen=True)
class TutorialNav:
    """Prev/next/hub links injected into a spoke page, computed from the hub."""

    hub_title: str | None = None
    prev_slug: str | None = None
    prev_title: str | None = None
    next_slug: str | None = None
    next_title: str | None = None
    siblings: list[HubEntry] = field(default_factory=list)


@dataclass(frozen=True)
class StepResult:
    """The outcome of running one step (populated by ``verify`` in a later phase)."""

    index: int
    ok: bool
    error: str | None
    cols: list[str]
    rows: list[object]
    matched: bool | None
    elapsed: float
