"""Jev judgments for vgi-lint-check (opt-in, advisory).

Jev is TypeSafe's System One model. It answers a question about a value with a
typed result instead of prose: ``noul`` (yes/no as a probability), ``choice`` (one
label from a set, with a confidence) or ``score``. A rule can therefore act only
when the answer is confident and stay silent otherwise, which a free-form LLM
verdict can't offer.

Jev runs through the vgi-typesafe worker, attached the way any VGI worker is, so
the questions are SQL: a table of questions joined ``LATERAL`` against
``noul``/``choice``. The per-question text goes in the correlated first argument
and the instructions/criteria stay constant per batch, so this works on stock
DuckDB 2.0.0 (which can't pass named arguments to a correlated call).

The TypeSafe key comes from a DuckDB secret of type ``typesafe`` (persistent
secrets load automatically) or the ``TYPESAFE_API_KEY`` environment variable;
this module never sees it. Answers are cached by content hash, so an unchanged
catalog is never asked twice.

Questions (and the rules that read the answers):

* VGI190: does each example's description say what its SQL does? (``choice``)
* VGI191: does each object's ``vgi.category`` fit it best among its schema's
  categories? (``choice``)
* VGI920's soft grader: did the simulated analyst meet the task's
  ``success_criteria``? (``noul``, via :meth:`JevClient.noul`)

A question about whether the catalog comment describes the worker's purpose was
tried and dropped: on the fleet it never answered confidently, even for a
3,000-character comment listing sixty functions, so the deterministic VGI107
does that job.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Where the vgi-typesafe worker is fetched from (pinned to a release tag).
DEFAULT_LOCATION = "uvx --from git+https://github.com/Query-farm/vgi-typesafe@v0.3.0 vgi-typesafe"
DEFAULT_MODEL = "jev-latest"
DEFAULT_CACHE = ".vgi-jev-cache.json"
_ALIAS = "vgi_lint_jev"

# Bump when a question's wording changes so cached answers to the old wording
# are not reused.
QUESTION_REVISION = "2"

# Calibrated on hand-labelled fleet examples (2026-11-14): the plain "does the
# query do what the description says?" yes/no flagged claim-style descriptions,
# LIMIT 0 binds and capability descriptions as mismatches; this wording, asked
# as a choice, kept every real mismatch and cut those false alarms by two thirds.
EXAMPLE_INSTRUCTIONS = (
    "This is a SQL example from a data worker's documentation: what the object it "
    "calls is, the example's description, then the query. Judge only whether the "
    "description misstates what the query does. These all count as accurate: a "
    'description phrased as a claim that the query checks ("X has exactly one row" '
    "with SELECT count(*) = 1); a query with LIMIT 0 that only binds the function to "
    "show its columns, when the description says so; a description of the function's "
    "general capability when the query demonstrates it. It is inaccurate only if the "
    "description promises a different calculation, filter, ordering, grouping, time "
    "span, granularity or output than the query produces."
)
EXAMPLE_CRITERIA: tuple[tuple[str, str], ...] = (
    ("accurate", "The description accurately describes what the query does"),
    (
        "inaccurate",
        "The description misstates what the query does: a different calculation, filter, "
        "ordering, grouping, time span, granularity or output",
    ),
)

CATEGORY_INSTRUCTIONS = (
    "This is one object (table or function) from a data worker. Which of the "
    "worker's categories does it belong to?"
)


@dataclass(frozen=True)
class Question:
    """One question for Jev. ``key`` identifies it within a run."""

    key: str
    kind: str  # "noul" | "choice"
    text: str
    instructions: str
    criteria: tuple[tuple[str, str], ...] = ()

    def cache_key(self, model: str) -> str:
        """Content hash: same model, wording and text → same answer."""
        payload = json.dumps(
            [QUESTION_REVISION, model, self.kind, self.instructions, self.criteria, self.text]
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Answer:
    """Jev's typed answer. ``noul`` for yes/no, ``choice``/``confidence`` for choice."""

    noul: float | None = None
    choice: str | None = None
    confidence: float | None = None


@dataclass
class JevReport:
    """Every answer from one run, keyed by :attr:`Question.key`."""

    answers: dict[str, Answer] = field(default_factory=dict)
    questions: dict[str, Question] = field(default_factory=dict)
    examples: dict[str, ExampleItem] = field(default_factory=dict)
    categories: dict[str, CategoryItem] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def get(self, key: str) -> Answer | None:
        """The answer to ``key``, or None when it wasn't asked or failed."""
        return self.answers.get(key)


class JevClient:
    """Asks Jev questions through the vgi-typesafe worker (lazily attached)."""

    def __init__(
        self,
        *,
        location: str = DEFAULT_LOCATION,
        model: str = DEFAULT_MODEL,
        concurrency: int = 16,
        cache_path: str | os.PathLike[str] | None = DEFAULT_CACHE,
        connection: Any | None = None,
    ) -> None:
        """Configure the client; nothing is attached until the first question."""
        self.location = location
        self.model = model
        self.concurrency = max(1, min(64, concurrency))
        self.cache_path = Path(cache_path) if cache_path else None
        self._con = connection
        self._attached = False
        self._cache: dict[str, dict[str, Any]] = {}
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (OSError, ValueError):
                self._cache = {}

    # -- connection ---------------------------------------------------------------
    def _connection(self) -> Any:
        if self._con is None:
            import haybarn

            self._con = haybarn.connect()
        if not self._attached:
            self._con.execute("LOAD vgi")
            loc = self.location.replace("'", "''")
            self._con.execute(
                f"ATTACH IF NOT EXISTS 'typesafe' AS {_ALIAS} (TYPE vgi, LOCATION '{loc}')"
            )
            self._attached = True
        return self._con

    def save(self) -> None:
        """Persist the answer cache (no-op without a cache path)."""
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self._cache, sort_keys=True))

    # -- asking -------------------------------------------------------------------
    def noul(self, text: str, instructions: str) -> float | None:
        """Ask one yes/no question; near 1 is yes. None if Jev didn't answer."""
        q = Question("single", "noul", text, instructions)
        return self.ask([q]).get("single", Answer()).noul

    def ask(self, questions: Iterable[Question]) -> dict[str, Answer]:
        """Answer every question, from the cache where possible, in batched SQL."""
        out: dict[str, Answer] = {}
        pending: dict[tuple[str, str, tuple[tuple[str, str], ...]], list[Question]] = {}
        for q in questions:
            hit = self._cache.get(q.cache_key(self.model))
            if hit is not None:
                out[q.key] = Answer(**hit)
            else:
                pending.setdefault((q.kind, q.instructions, q.criteria), []).append(q)
        for (kind, instructions, criteria), batch in pending.items():
            for q, ans in zip(
                batch, self._run_batch(kind, instructions, criteria, batch), strict=True
            ):
                if ans is None:
                    continue
                out[q.key] = ans
                self._cache[q.cache_key(self.model)] = {
                    "noul": ans.noul,
                    "choice": ans.choice,
                    "confidence": ans.confidence,
                }
        self.save()
        return out

    def _run_batch(
        self,
        kind: str,
        instructions: str,
        criteria: tuple[tuple[str, str], ...],
        batch: list[Question],
    ) -> list[Answer | None]:
        con = self._connection()
        table = f"_jev_q_{abs(hash((kind, instructions, criteria))) % 10**8}"
        con.execute(f"CREATE OR REPLACE TEMP TABLE {table} (i INTEGER, t VARCHAR)")
        con.executemany(
            f"INSERT INTO {table} VALUES (?, ?)", [(i, q.text) for i, q in enumerate(batch)]
        )
        named = [
            "instructions => ?",
            f"model => '{self.model}'",
            f"concurrency => {self.concurrency}",
        ]
        params: list[Any] = [instructions]
        if kind == "choice":
            pairs = ", ".join(f"{_lit(k)}: {_lit(v)}" for k, v in criteria)
            named.insert(1, f"criteria => MAP {{{pairs}}}")
            select = "q.i, a.choice, a.confidence"
        else:
            select = "q.i, a.noul"
        sql = (
            f"SELECT {select} FROM {table} q, "
            f"LATERAL {_ALIAS}.main.{kind}(q.t, {', '.join(named)}) a"
        )
        rows = con.execute(sql, params).fetchall()
        con.execute(f"DROP TABLE IF EXISTS {table}")
        answers: list[Answer | None] = [None] * len(batch)
        for row in rows:
            if kind == "choice":
                answers[row[0]] = Answer(choice=row[1], confidence=row[2])
            else:
                answers[row[0]] = Answer(noul=row[1])
        return answers


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# -- building questions -----------------------------------------------------------
@dataclass(frozen=True)
class ExampleItem:
    """An example with both a description and SQL, wherever it was declared."""

    key: str
    where: str  # human locator, e.g. "main.observations example 2"
    description: str
    sql: str
    context: str = ""  # "name: one-line comment" of the object the example belongs to
    object_id: Any = None  # model.ObjectId when built from a live catalog


@dataclass(frozen=True)
class CategoryItem:
    """An object with a declared ``vgi.category`` and its schema's category registry."""

    key: str
    where: str
    text: str  # name + comment
    declared: str
    registry: tuple[tuple[str, str], ...]  # (name, description)
    object_id: Any = None


def example_text(description: str, sql: str, context: str = "") -> str:
    """How an example is shown to Jev."""
    about = f"Object: {context.strip()}\n\n" if context.strip() else ""
    return f"{about}Description: {description.strip()}\n\nQuery:\n{sql.strip()}"


def example_questions(items: Iterable[ExampleItem]) -> Iterator[Question]:
    """VGI190 questions."""
    for it in items:
        yield Question(
            it.key,
            "choice",
            example_text(it.description, it.sql, it.context),
            EXAMPLE_INSTRUCTIONS,
            EXAMPLE_CRITERIA,
        )


def category_questions(items: Iterable[CategoryItem]) -> Iterator[Question]:
    """VGI191 questions (one batch per distinct registry)."""
    for it in items:
        yield Question(it.key, "choice", it.text, CATEGORY_INSTRUCTIONS, it.registry)


def catalog_items(catalog: Any) -> tuple[list[ExampleItem], list[CategoryItem]]:
    """Examples and categorised objects from a built :class:`model.Catalog`."""
    examples: list[ExampleItem] = []
    categories: list[CategoryItem] = []
    for s in catalog.iter_schemas():
        registry = tuple(
            (c.name, (c.description or c.title or c.name).strip()) for c in s.categories
        )
        for obj in (*s.tables, *s.views, *s.functions):
            where = f"{s.name}.{obj.name}"
            ctx = f"{obj.name}: {(obj.comment or '').strip()}"
            for ex in obj.examples:
                if (ex.description or "").strip() and (ex.sql or "").strip():
                    examples.append(
                        ExampleItem(
                            f"example:{where}:{ex.index}",
                            f"{where} example {ex.index + 1}",
                            ex.description or "",
                            ex.sql or "",
                            ctx,
                            obj.id,
                        )
                    )
            for ex in obj.executable_examples:
                sql = ";\n".join(
                    (st.sql or "").strip().rstrip(";")
                    for st in ex.statements
                    if (st.sql or "").strip()
                )
                if (ex.description or "").strip() and sql:
                    examples.append(
                        ExampleItem(
                            f"exec:{where}:{ex.index}",
                            f"{where} executable example {ex.name or ex.index + 1}",
                            ex.description or "",
                            sql,
                            ctx,
                            obj.id,
                        )
                    )
            declared = getattr(obj, "category", None)
            if declared and len(registry) >= 2:
                text = f"{obj.name}: {(obj.comment or '').strip()}"
                categories.append(
                    CategoryItem(f"category:{where}", where, text, declared, registry, obj.id)
                )
    return examples, categories


def dump_items(dump: dict[str, Any]) -> tuple[list[ExampleItem], list[CategoryItem]]:
    """Same as :func:`catalog_items`, from a ``vgi-lint dump`` JSON document."""
    cat = dump.get("catalog") or {}
    examples: list[ExampleItem] = []
    categories: list[CategoryItem] = []
    seen: set[tuple[str, str]] = set()
    for s in cat.get("schemas") or []:
        for i, ex in enumerate((s.get("tags") or {}).get("vgi.example_queries") or []):
            _add_example(
                examples,
                seen,
                f"example:{s.get('name')}:{i}",
                f"schema {s.get('name')} example {i + 1}",
                ex,
            )
        raw_reg = (s.get("tags") or {}).get("vgi.categories") or []
        registry = tuple(
            (
                str(c.get("name")),
                str(c.get("description") or c.get("title") or c.get("name")).strip(),
            )
            for c in raw_reg
            if isinstance(c, dict) and c.get("name")
        )
        for kind in ("tables", "views", "functions"):
            for obj in s.get(kind) or []:
                where = f"{s.get('name')}.{obj.get('name')}"
                tagged = (obj.get("tags") or {}).get("vgi.example_queries") or []
                ctx = f"{obj.get('name')}: {(obj.get('comment') or '').strip()}"
                for i, ex in enumerate(tagged):
                    _add_example(
                        examples, seen, f"example:{where}:{i}", f"{where} example {i + 1}", ex, ctx
                    )
                declared = ((obj.get("tags") or {}).get("vgi.category") or "").strip()
                if declared and len(registry) >= 2:
                    text = f"{obj.get('name')}: {(obj.get('comment') or '').strip()}"
                    categories.append(
                        CategoryItem(f"category:{where}", where, text, declared, registry)
                    )
    for i, ex in enumerate(cat.get("example_queries") or []):
        _add_example(examples, seen, f"example:catalog:{i}", f"catalog example {i + 1}", ex)
    return examples, categories


def _add_example(
    out: list[ExampleItem],
    seen: set[tuple[str, str]],
    key: str,
    where: str,
    ex: Any,
    context: str = "",
) -> None:
    if not isinstance(ex, dict):
        return
    desc = str(ex.get("description") or "").strip()
    sql = str(ex.get("sql") or "").strip()
    if desc and sql and (desc, sql) not in seen:
        seen.add((desc, sql))
        out.append(ExampleItem(key, where, desc, sql, context))


def run_questions(
    client: Any,
    examples: list[ExampleItem],
    categories: list[CategoryItem],
) -> JevReport:
    """Ask every question for one catalog and collect a report."""
    qs: list[Question] = list(example_questions(examples))
    qs.extend(category_questions(categories))
    report = JevReport(
        questions={q.key: q for q in qs},
        examples={it.key: it for it in examples},
        categories={it.key: it for it in categories},
    )
    try:
        report.answers = client.ask(qs)
    except Exception as e:  # noqa: BLE001 - an advisory pass must not crash the lint
        report.errors.append(f"{type(e).__name__}: {e}")
    return report


def dump_findings(
    client: Any, path: Path, *, min_example: float = 0.9, min_category: float = 0.98
) -> dict[str, Any]:
    """Ask the VGI190/VGI191 questions about one dump file and collect the findings.

    Args:
        client: A :class:`JevClient`.
        path: A ``vgi-lint dump`` JSON file.
        min_example: Confidence floor for an "inaccurate" example verdict.
        min_category: Confidence floor for a different category.

    Returns:
        ``{worker, dump, examples, categorised, errors, findings}``.
    """
    dump = json.loads(path.read_text())
    name = str((dump.get("catalog") or {}).get("catalog") or path.stem)
    examples, categories = dump_items(dump)
    report = run_questions(client, examples, categories)
    findings: list[dict[str, Any]] = []
    for key, item in report.examples.items():
        ans = report.get(key)
        if ans and ans.choice == "inaccurate" and (ans.confidence or 0) >= min_example:
            findings.append(
                {
                    "code": "VGI190",
                    "where": item.where,
                    "confidence": ans.confidence,
                    "description": item.description,
                    "sql": item.sql,
                }
            )
    for key, cat in report.categories.items():
        ans = report.get(key)
        if not (ans and ans.choice) or ans.choice == cat.declared:
            continue
        if (ans.confidence or 0) >= min_category:
            findings.append(
                {
                    "code": "VGI191",
                    "where": cat.where,
                    "confidence": ans.confidence,
                    "declared": cat.declared,
                    "suggested": ans.choice,
                }
            )
    return {
        "worker": name,
        "dump": str(path),
        "examples": len(examples),
        "categorised": len(categories),
        "errors": report.errors,
        "findings": findings,
    }
