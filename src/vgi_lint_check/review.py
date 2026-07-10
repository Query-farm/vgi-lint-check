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
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .model import Catalog, ObjectKind

SCORE_KEYS = ("accuracy", "clarity", "completeness", "audience_fit")

# Bump when the rubric, the actor preamble, or the CLI system prompt changes, so
# cached verdicts produced by the old wording are not silently reused. The backend
# fingerprint (model + this revision) salts every cache key.
PROMPT_REVISION = "2"
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


# The `claude` CLI is an agent, not a completion endpoint: every `claude -p` call
# loads the full Claude Code system prompt, all built-in tool schemas, the cwd's
# CLAUDE.md, user settings, and any configured MCP servers. Measured against a live
# worker that is ~20,600 input tokens *per call*, before a byte of our own prompt —
# and `--resume` does not amortize it (the harness prompt is re-sent every turn), so
# a 12-turn `simulate` task paid it twelve times.
#
# These flags strip the agent back to a plain completion: no tools, no MCP servers,
# no settings/CLAUDE.md discovery, and our own system prompt. Set
# VGI_LINT_AI_INHERIT_CONTEXT=1 to opt back in (e.g. to let a worker repo's CLAUDE.md
# inform the judge) at ~100x the token cost, and note that doing so makes verdicts
# depend on the directory the lint happens to run from.
_PRUNE_FLAGS = [
    "--tools",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
]
_CLI_SYSTEM_PROMPT = (
    "You are a precise evaluator working for a metadata linter. Follow the user's "
    "instructions exactly and return only what they ask for. When they ask for JSON, "
    "emit only the JSON — no prose, no commentary, no code fences."
)

# The interactive Claude Code default is whatever the user last selected — often a
# premium 1M-context Opus tier. Pin a model so the "runs on your subscription" backend
# does not silently bill an order of magnitude more than the pay-per-token one.
DEFAULT_CLI_MODEL = "sonnet"


def _inherit_context() -> bool:
    """True when the user opted into the full Claude Code agent context (expensive)."""
    return os.environ.get("VGI_LINT_AI_INHERIT_CONTEXT", "") not in ("", "0")


@dataclass
class ClaudeCliBackend:
    """Runs the local ``claude`` CLI in headless mode (uses your subscription)."""

    model: str | None = None
    timeout: float = 180.0

    def fingerprint(self) -> str:
        """Identity of the judge, for salting verdict caches."""
        return f"claude:{self.model or DEFAULT_CLI_MODEL}:{PROMPT_REVISION}"

    def _run(self, args: list[str], prompt: str) -> str:
        """Run ``claude -p`` with ``args``, feeding ``prompt`` on stdin.

        The prompt goes on stdin, not argv: ``--tools`` is variadic and would swallow a
        trailing positional prompt, and a batched review prompt can be large.
        """
        if shutil.which("claude") is None:
            raise RuntimeError(
                "the 'claude' CLI is not on PATH — install Claude Code and sign in "
                "with your subscription, or use --review-backend api with an API key"
            )
        cmd = ["claude", "-p", *args]
        if not _inherit_context():
            cmd += [*_PRUNE_FLAGS, "--system-prompt", _CLI_SYSTEM_PROMPT]
        cmd += ["--model", self.model or DEFAULT_CLI_MODEL]
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def complete(self, prompt: str) -> str:
        """Run a single ``claude -p`` call and return its stdout."""
        return self._run([], prompt)

    def conversation(self) -> Conversation:
        """A real ``claude`` session: turn 1 sets a session id, later turns resume it."""
        return _ClaudeSession(self)


class _ClaudeSession:
    """A ``claude`` CLI session: ``--session-id`` on the first turn, ``--resume`` after.

    Subsequent turns send only the new message; the CLI restores the prior transcript,
    so the growing conversation isn't re-transmitted. The fixed system prompt *is* re-sent
    every turn, which is why keeping it small (see ``_PRUNE_FLAGS``) matters most here.
    """

    def __init__(self, backend: ClaudeCliBackend) -> None:
        """Bind to ``backend`` and allocate a fresh session id."""
        self._backend = backend
        self._session_id = str(uuid.uuid4())
        self._resume = False

    def send(self, message: str) -> str:
        """Send ``message`` on this session (starting or resuming it)."""
        flag = "--resume" if self._resume else "--session-id"
        out = self._backend._run([flag, self._session_id], message)
        self._resume = True
        return out


@dataclass
class AnthropicApiBackend:
    """Calls the Anthropic API (pay-per-token; needs ANTHROPIC_API_KEY)."""

    model: str = "claude-sonnet-5"
    # A doc-review batch is `batch_size` objects x up to 3 suggestions each; 4096 was
    # tight enough that truncation was routine (hence the salvage path in
    # `_extract_json_array`, which still covers the tail case).
    max_tokens: int = 8192

    def fingerprint(self) -> str:
        """Identity of the judge, for salting verdict caches."""
        return f"api:{self.model}:{PROMPT_REVISION}"

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
        return AnthropicApiBackend(model=model or "claude-sonnet-5")
    raise ValueError(f"unknown review backend {name!r} (expected 'claude' or 'api')")


def backend_fingerprint(backend: ReviewBackend) -> str:
    """Cache salt identifying the judge (model + prompt revision).

    Verdicts are only comparable across runs that used the same judge, so this salts
    every cache key. Without it, switching model or editing a prompt silently reuses
    verdicts produced by a different judge.
    """
    fn = getattr(backend, "fingerprint", None)
    return fn() if callable(fn) else f"{type(backend).__name__}:{PROMPT_REVISION}"


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
        # Prefer the per-argument metadata (name, type, description) from
        # vgi_function_arguments() so the reviewer can judge whether each argument is
        # actually documented; fall back to bare parameter names on older extensions
        # that don't expose it.
        if f.arguments:
            parameters: Any = [
                {"name": a.name, "type": a.type, "description": a.description or ""}
                for a in f.arguments
            ]
        else:
            parameters = list(f.parameters)
        items.append(
            {
                "object": f.id.qualified(),
                "kind": str(f.kind),
                "description": f.description or "",
                "comment": f.comment or "",
                **_doc(f.tags),
                "parameters": parameters,
                "parameter_types": list(f.parameter_types),
                "examples": [e.sql for e in f.examples if e.sql],
            }
        )
    return items


def content_hash(item: dict[str, Any], salt: str = "") -> str:
    """Stable hash of the material judged (and the judge), for the verdict cache."""
    blob = json.dumps([salt, item], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_prompt(items: list[dict[str, Any]]) -> str:
    """The grounded review prompt for a batch of objects."""
    return _RUBRIC + "\n\nOBJECTS:\n" + json.dumps(items, indent=2, default=str)


# --------------------------------------------------------------------------
# Parsing model output
# --------------------------------------------------------------------------
def _extract_json_array(text: str) -> Any:
    """Extract the first JSON array from model output, tolerant of real LLM output.

    Brackets inside string values are skipped (so a suggestion containing ``[`` /
    ``]`` doesn't break matching), and a truncated response is salvaged by closing
    the array after its last complete object — so partial output still yields the
    entries that did arrive rather than the whole pass failing.
    """
    start = text.find("[")
    if start < 0:
        raise ValueError("no JSON array in model output")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    # Unterminated (typically a truncated response): salvage the complete leading
    # entries by closing the array after the last finished object.
    cut = text.rfind("}")
    if cut > start:
        try:
            return json.loads(text[start : cut + 1] + "]")
        except ValueError:
            pass
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
    concurrency: int = 4,
) -> ReviewReport:
    """Review every object's docs; reuse cached verdicts for unchanged content.

    Cache-miss objects are judged in batches (``batch_size``) run **in parallel**
    across ``concurrency`` threads — each a separate backend call — so a large
    catalog's batches overlap instead of running one after another. A batch that
    fails (backend/parse error) leaves its objects unreviewed rather than sinking
    the whole pass; the cache is written once on the main thread.
    """
    items = build_items(catalog)
    salt = backend_fingerprint(backend)
    reviews: list[ObjectReview] = []
    to_judge: list[dict[str, Any]] = []
    cached = 0
    for item in items:
        hit = cache.get(content_hash(item, salt)) if cache else None
        if hit is not None:
            reviews.append(hit)
            cached += 1
        else:
            to_judge.append(item)

    batches = [to_judge[i : i + batch_size] for i in range(0, len(to_judge), batch_size)]

    def judge(batch: list[dict[str, Any]]) -> list[tuple[dict[str, Any], ObjectReview | None]]:
        try:
            parsed = parse_reviews(backend.complete(build_prompt(batch)), batch)
        except Exception:  # noqa: BLE001 - a flaky batch leaves its objects unreviewed
            return [(item, None) for item in batch]
        by_id = {r.object: r for r in parsed}
        return [(item, by_id.get(item["object"])) for item in batch]

    workers = max(1, min(concurrency, len(batches)))
    if workers <= 1 or len(batches) <= 1:
        batch_results = [judge(b) for b in batches]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            batch_results = list(ex.map(judge, batches))

    judged = 0
    for batch_result in batch_results:
        for item, r in batch_result:
            if r is None:
                continue
            reviews.append(r)
            judged += 1
            if cache:
                cache.put(content_hash(item, salt), r)
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
