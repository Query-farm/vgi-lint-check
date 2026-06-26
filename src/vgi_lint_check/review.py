"""LLM-as-judge review of documentation quality (opt-in, advisory).

This is a coaching layer separate from the deterministic linter. It grades the
things rules can't — accuracy, clarity, completeness, audience-fit — by sending
each object's descriptions *plus its real structural facts* (columns, types,
constraints, examples) to an LLM with a rubric.

The default backend is the local ``claude`` CLI in headless mode (``claude -p``),
so the judging runs on a Claude Pro/Max subscription rather than per-token API
billing. Verdicts are cached by content hash so unchanged docs aren't re-judged.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .model import Catalog, ObjectKind

SCORE_KEYS = ("accuracy", "clarity", "completeness", "audience_fit")
_RUBRIC = (
    "You are reviewing the documentation quality of objects exposed by a data "
    "worker, for use by LLM/agent consumers. For EACH object below, judge its "
    "descriptions AGAINST its actual structure (columns, types, constraints, "
    "examples). Score 1-5 (5 = excellent) on: accuracy (does the prose match the "
    "structure?), clarity, completeness (units, caveats, relationships, when to "
    "use it), and audience_fit (useful for an agent selecting/using this object). "
    "Give 1-3 concrete, specific suggestions (not generic advice). "
    "Return ONLY a JSON array, one item per object: "
    '[{"object": "<qualified id>", "scores": {"accuracy": n, "clarity": n, '
    '"completeness": n, "audience_fit": n}, "suggestions": ["..."], '
    '"summary": "one line"}]. No prose outside the JSON.'
)


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------
class ReviewBackend(Protocol):
    """Something that turns a prompt into a model completion string."""

    def complete(self, prompt: str) -> str:
        """Return the model's completion for ``prompt``."""
        ...  # pragma: no cover


class Conversation(Protocol):
    """A multi-turn exchange. ``send`` returns the model's reply for one message.

    The first ``send`` establishes context; later ``send``s may carry only the
    new turn (a delta) when the backend keeps the conversation server-side.
    """

    def send(self, message: str) -> str:
        """Send one message and return the model's reply."""
        ...  # pragma: no cover


class _ResendConversation:
    """Fallback conversation for any ``complete()``-only backend.

    Keeps no server-side state — it accumulates the transcript and re-sends the
    whole thing each turn, reproducing the original stateless behavior.
    """

    def __init__(self, backend: ReviewBackend) -> None:
        """Wrap ``backend`` and start an empty transcript."""
        self._backend = backend
        self._log: list[str] = []

    def send(self, message: str) -> str:
        """Append ``message``, re-send the whole transcript, record the reply."""
        self._log.append(message)
        reply = self._backend.complete("\n\n".join(self._log))
        self._log.append(f"(your previous reply)\n{reply}")
        return reply


@dataclass
class ClaudeCliBackend:
    """Runs the local ``claude`` CLI in headless mode (uses your subscription)."""

    model: str | None = None
    timeout: float = 180.0

    def _run(self, args: list[str]) -> str:
        """Run ``claude -p`` with ``args`` and return stdout (raise on failure)."""
        if shutil.which("claude") is None:
            raise RuntimeError(
                "the 'claude' CLI is not on PATH — install Claude Code and sign in "
                "with your subscription, or use --review-backend api with an API key"
            )
        cmd = ["claude", "-p", *args]
        if self.model:
            cmd += ["--model", self.model]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def complete(self, prompt: str) -> str:
        """Run a single ``claude -p`` call and return its stdout."""
        return self._run([prompt])

    def conversation(self) -> Conversation:
        """A real ``claude`` session: turn 1 sets a session id, later turns resume it."""
        return _ClaudeSession(self)


class _ClaudeSession:
    """A ``claude`` CLI session: ``--session-id`` on the first turn, ``--resume`` after.

    Subsequent turns send only the new message; the CLI restores prior context
    server-side, so the growing transcript isn't re-transmitted each turn.
    """

    def __init__(self, backend: ClaudeCliBackend) -> None:
        """Bind to ``backend`` and allocate a fresh session id."""
        self._backend = backend
        self._session_id = str(uuid.uuid4())
        self._resume = False

    def send(self, message: str) -> str:
        """Send ``message`` on this session (starting or resuming it)."""
        flag = "--resume" if self._resume else "--session-id"
        out = self._backend._run([flag, self._session_id, message])
        self._resume = True
        return out


@dataclass
class AnthropicApiBackend:
    """Calls the Anthropic API (pay-per-token; needs ANTHROPIC_API_KEY)."""

    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096

    def _complete_messages(self, messages: list[dict[str, str]]) -> str:
        """Send a full message list to the API and return the text response."""
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - optional dep
            raise RuntimeError(
                "the 'anthropic' package is required for --review-backend api"
            ) from e
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=self.model, max_tokens=self.max_tokens, messages=messages
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    def complete(self, prompt: str) -> str:
        """Call the Anthropic API with a single user turn and return the text."""
        return self._complete_messages([{"role": "user", "content": prompt}])

    def conversation(self) -> Conversation:
        """The API is stateless, so accumulate the message list and resend it."""
        return _ApiConversation(self)


class _ApiConversation:
    """Accumulates the message list (the API has no server-side session)."""

    def __init__(self, backend: AnthropicApiBackend) -> None:
        """Bind to ``backend`` and start an empty message list."""
        self._backend = backend
        self._messages: list[dict[str, str]] = []

    def send(self, message: str) -> str:
        """Append the user turn, call the API, record and return the reply."""
        self._messages.append({"role": "user", "content": message})
        reply = self._backend._complete_messages(self._messages)
        self._messages.append({"role": "assistant", "content": reply})
        return reply


def make_conversation(backend: ReviewBackend, sessions: bool = True) -> Conversation:
    """A Conversation for ``backend`` — a native session when available, else re-send.

    ``sessions=False`` forces the stateless re-send fallback (the original behavior).
    """
    factory = getattr(backend, "conversation", None)
    if sessions and callable(factory):
        return factory()  # type: ignore[no-any-return]
    return _ResendConversation(backend)


def make_backend(name: str, model: str | None = None) -> ReviewBackend:
    """Construct a backend by name (``claude`` default, or ``api``)."""
    if name == "claude":
        return ClaudeCliBackend(model=model)
    if name == "api":
        return AnthropicApiBackend(model=model or "claude-sonnet-4-6")
    raise ValueError(f"unknown review backend {name!r} (expected 'claude' or 'api')")


# --------------------------------------------------------------------------
# Items: the grounded material judged per object
# --------------------------------------------------------------------------
def _doc(tags: Any) -> dict[str, str]:
    from .model import TAG_DOC_LLM, TAG_DOC_MD

    return {"doc_llm": tags.get(TAG_DOC_LLM) or "", "doc_md": tags.get(TAG_DOC_MD) or ""}


def build_items(catalog: Catalog) -> list[dict[str, Any]]:
    """One grounded record per reviewable object (catalog/schema/table/view/function)."""
    items: list[dict[str, Any]] = []
    items.append(
        {
            "object": catalog.id.qualified(),
            "kind": "catalog",
            "comment": catalog.comment or "",
            **_doc(catalog.tags),
        }
    )
    for s in catalog.iter_schemas():
        items.append(
            {
                "object": s.id.qualified(),
                "kind": "schema",
                "comment": s.comment or "",
                **_doc(s.tags),
            }
        )
    for t in catalog.iter_table_like():
        items.append(
            {
                "object": t.id.qualified(),
                "kind": str(t.kind),
                "comment": t.comment or "",
                **_doc(t.tags),
                "columns": [
                    {"name": c.name, "type": c.data_type, "comment": c.comment or ""}
                    for c in t.columns
                ],
                "examples": [e.sql for e in t.examples if e.sql],
            }
        )
    for f in catalog.iter_all_functions():
        if f.kind is ObjectKind.TABLE_FUNCTION and catalog.find_table_like(f.name, f.schema):
            continue  # documented via its backing table
        items.append(
            {
                "object": f.id.qualified(),
                "kind": str(f.kind),
                "description": f.description or "",
                "comment": f.comment or "",
                **_doc(f.tags),
                "parameters": list(f.parameters),
                "parameter_types": list(f.parameter_types),
                "examples": [e.sql for e in f.examples if e.sql],
            }
        )
    return items


def content_hash(item: dict[str, Any]) -> str:
    """Stable hash of the material judged, for the verdict cache."""
    blob = json.dumps(item, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_prompt(items: list[dict[str, Any]]) -> str:
    """The grounded review prompt for a batch of objects."""
    return _RUBRIC + "\n\nOBJECTS:\n" + json.dumps(items, indent=2, default=str)


# --------------------------------------------------------------------------
# Parsing model output
# --------------------------------------------------------------------------
def _extract_json_array(text: str) -> Any:
    start = text.find("[")
    if start < 0:
        raise ValueError("no JSON array in model output")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unterminated JSON array in model output")


@dataclass
class ObjectReview:
    """One object's LLM verdict."""

    object: str
    kind: str
    scores: dict[str, int]
    suggestions: list[str]
    summary: str

    @property
    def overall(self) -> float:
        """Mean of the four sub-scores (0 if none)."""
        vals = [self.scores.get(k) for k in SCORE_KEYS]
        nums = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(nums) / len(nums), 1) if nums else 0.0


def parse_reviews(raw: str, items: list[dict[str, Any]]) -> list[ObjectReview]:
    """Parse a backend completion into ObjectReviews, matched back to items by id."""
    data = _extract_json_array(raw)
    by_id = {it["object"]: it for it in items}
    out: list[ObjectReview] = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        oid = str(entry.get("object", ""))
        item = by_id.get(oid)
        scores = entry.get("scores") or {}
        out.append(
            ObjectReview(
                object=oid,
                kind=str(item["kind"]) if item else "",
                scores={
                    k: int(scores[k]) for k in SCORE_KEYS if isinstance(scores.get(k), (int, float))
                },
                suggestions=[str(s) for s in (entry.get("suggestions") or [])],
                summary=str(entry.get("summary") or ""),
            )
        )
    return out


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
@dataclass
class ReviewCache:
    """A content-hash keyed cache of verdicts, persisted as JSON."""

    path: Path
    _data: dict[str, dict[str, Any]] = field(default_factory=dict)

    def load(self) -> ReviewCache:
        """Read the cache file (no-op if missing/corrupt)."""
        if self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text())
            except (ValueError, OSError):
                self._data = {}
        return self

    def get(self, key: str) -> ObjectReview | None:
        """Return the cached verdict for ``key``, or None."""
        d = self._data.get(key)
        return ObjectReview(**d) if d else None

    def put(self, key: str, review: ObjectReview) -> None:
        """Store ``review`` under ``key``."""
        self._data[key] = asdict(review)

    def save(self) -> None:
        """Persist the cache to disk."""
        self.path.write_text(json.dumps(self._data, indent=2))


# --------------------------------------------------------------------------
# Report + runner
# --------------------------------------------------------------------------
@dataclass
class ReviewReport:
    """Result of a documentation review."""

    location: str
    backend: str
    reviews: list[ObjectReview]
    judged: int
    cached: int

    @property
    def score(self) -> float:
        """Mean overall doc-quality score across reviewed objects."""
        if not self.reviews:
            return 0.0
        return round(sum(r.overall for r in self.reviews) / len(self.reviews), 1)


def review_catalog(
    catalog: Catalog,
    backend: ReviewBackend,
    *,
    backend_name: str = "claude",
    cache: ReviewCache | None = None,
    batch_size: int = 8,
) -> ReviewReport:
    """Review every object's docs; reuse cached verdicts for unchanged content."""
    items = build_items(catalog)
    reviews: list[ObjectReview] = []
    to_judge: list[dict[str, Any]] = []
    cached = 0
    for item in items:
        hit = cache.get(content_hash(item)) if cache else None
        if hit is not None:
            reviews.append(hit)
            cached += 1
        else:
            to_judge.append(item)

    judged = 0
    for i in range(0, len(to_judge), batch_size):
        batch = to_judge[i : i + batch_size]
        parsed = parse_reviews(backend.complete(build_prompt(batch)), batch)
        by_id = {r.object: r for r in parsed}
        for item in batch:
            r = by_id.get(item["object"])
            if r is None:
                continue
            reviews.append(r)
            judged += 1
            if cache:
                cache.put(content_hash(item), r)
    if cache:
        cache.save()

    order = {it["object"]: n for n, it in enumerate(items)}
    reviews.sort(key=lambda r: order.get(r.object, 1 << 30))
    return ReviewReport(catalog.location, backend_name, reviews, judged, cached)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def render_terminal(report: ReviewReport) -> str:
    """Human-readable review report."""
    out = [
        f"doc review  {report.location}  ·  backend={report.backend}  ·  "
        f"judged {report.judged} · cached {report.cached}",
        f"overall doc-quality score: {report.score}/5",
        "",
    ]
    for r in report.reviews:
        scores = " ".join(f"{k[:4]}={r.scores.get(k, '-')}" for k in SCORE_KEYS)
        out.append(f"{r.object} ({r.kind})  [{scores}]  avg {r.overall}")
        if r.summary:
            out.append(f"  ↳ {r.summary}")
        for s in r.suggestions:
            out.append(f"  · {s}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_json(report: ReviewReport) -> str:
    """Machine-readable review report."""
    return json.dumps(
        {
            "tool": "vgi-lint review",
            "location": report.location,
            "backend": report.backend,
            "score": report.score,
            "judged": report.judged,
            "cached": report.cached,
            "reviews": [asdict(r) for r in report.reviews],
        },
        indent=2,
    )
