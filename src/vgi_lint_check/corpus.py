"""Worker SQL-corpus coverage.

Which catalog objects the worker's own examples and agent-test tasks actually
*call*, plus worker-qualified references that do not resolve.

Built on :mod:`vgi_lint_check.sql_parse` (AST via ``json_serialize_sql``, fully
offline). Shared by the VGI5xx coverage rules **and** by
:mod:`vgi_lint_check.scoring`, so a finding and the score never disagree about
what "covered" means.

Coverage is measured over the worker's callable + relational *surface* — every
function (scalar/aggregate/macro/table-function) plus every table/view. An object
is:

* **demonstrated** — called by ≥1 illustrative or executable example;
* **runnable** — called by ≥1 executable example (the guaranteed-to-run subset);
* **tested** — called by ≥1 ``vgi.agent_test_tasks`` reference/check statement.

A reference that is explicitly worker-qualified (``catalog.schema.name`` where
``catalog`` is the worker's) but resolves to no object is **broken** — a typo or a
statement left stale after a rename. Bare unresolved names (built-ins, columns,
session-temp tables, another worker's objects in a composition) are deliberately
ignored: only an unambiguous worker reference can be "broken".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from .model import Catalog, ExampleStatement, ObjectId, ObjectKind
from .sql_parse import Ref, parse_refs

__all__ = ["Broken", "CorpusCoverage", "compute_corpus_coverage"]


@dataclass(frozen=True)
class Broken:
    """A worker-qualified reference that resolves to no catalog object."""

    reference: str  # e.g. "open_meteo.main.no_such_fn"
    origin: ObjectId  # the object whose SQL contains the reference
    where: str  # human location, e.g. "example #2" / "task 'busiest-day' reference"
    source: str  # "doc" (example/executable) or "test" (agent-task SQL)


@dataclass
class CorpusCoverage:
    """Coverage of the worker surface by its own example/test SQL."""

    universe: dict[str, ObjectId] = field(default_factory=dict)
    demonstrated: set[str] = field(default_factory=set)
    runnable: set[str] = field(default_factory=set)
    tested: set[str] = field(default_factory=set)
    has_test_suite: bool = False
    broken: list[Broken] = field(default_factory=list)
    # Macro key -> id for macros that are called only with literal-constant args
    # (never a column/expression) across every example — demonstrated in shape
    # but never shown decoding real data.
    macro_const_only: dict[str, ObjectId] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Number of objects in the covered surface."""
        return len(self.universe)

    def undemonstrated(self) -> list[ObjectId]:
        """Objects no example calls, in stable qualified order."""
        return [oid for k, oid in sorted(self.universe.items()) if k not in self.demonstrated]

    def untested(self) -> list[ObjectId]:
        """Objects no agent task exercises, in stable qualified order."""
        return [oid for k, oid in sorted(self.universe.items()) if k not in self.tested]

    def doc_ratio(self) -> float | None:
        """Fraction of the surface demonstrated by ≥1 example (None if no surface)."""
        return len(self.demonstrated) / self.total if self.total else None

    def test_ratio(self) -> float | None:
        """Fraction exercised by ≥1 agent task, or None when no suite is declared."""
        if not self.has_test_suite or not self.total:
            return None
        return len(self.tested) / self.total


def _key(schema: str | None, name: str | None, default_schema: str) -> str:
    return f"{(schema or default_schema).lower()}.{(name or '').lower()}"


def _build_universe(catalog: Catalog, default_schema: str) -> dict[str, ObjectId]:
    # COPY-format handler table functions (from vgi_copy_formats()) bind only
    # inside a COPY (FORMAT 'x') statement — they hard-error on direct invocation,
    # and json_serialize_sql cannot serialize COPY, so no example/test SQL can ever
    # cover them. They are not part of the coverable surface. Use the extension's
    # own handler list, never a name-prefix guess.
    copy_handlers = {h.lower() for h in catalog.copy_handlers}
    universe: dict[str, ObjectId] = {}
    for f in catalog.iter_all_functions():
        if f.kind is ObjectKind.TABLE_FUNCTION and (f.name or "").lower() in copy_handlers:
            continue
        universe[_key(f.schema, f.name, default_schema)] = f.id
    for t in catalog.iter_table_like():
        universe.setdefault(_key(t.schema, t.name, default_schema), t.id)
    return universe


def _classify(ref: Ref, qualifier: str, default_schema: str, universe: dict[str, ObjectId]) -> str:
    """Resolve one reference against the worker surface.

    Returns ``key`` if ``ref`` hits a worker object, ``"!"+key`` if it is
    worker-qualified but unresolved (broken), or ``""`` to ignore.
    """
    cat = ref.catalog.lower()
    qual = qualifier.lower()
    if cat and cat != qual:
        return ""  # a reference to another catalog (composition) — not ours to judge
    key = _key(ref.schema, ref.name, default_schema)
    if key in universe:
        return key
    if cat == qual:  # explicitly named this worker, yet nothing matches
        return "!" + key
    return ""  # bare unresolved: a built-in, a column, a session-temp table


def _macro_keys(catalog: Catalog, default_schema: str) -> set[str]:
    return {_key(m.schema, m.name, default_schema) for m in catalog.iter_macros()}


def _iter_doc_sql(catalog: Catalog) -> Iterator[tuple[str, ObjectId, str]]:
    """Illustrative example SQL: (sql, origin, where) for every example-bearing object."""
    for fn in catalog.iter_all_functions():
        for ex in fn.examples:
            if ex.sql and ex.sql.strip():
                yield ex.sql, fn.id, f"example #{ex.index}"
    for tbl in catalog.iter_table_like():
        for ex in tbl.examples:
            if ex.sql and ex.sql.strip():
                yield ex.sql, tbl.id, f"example #{ex.index}"


def _iter_exec_sql(catalog: Catalog) -> Iterator[tuple[str, ObjectId, str]]:
    """Executable-example statement SQL across every host."""
    for host_id, examples, _err in catalog.iter_executable_example_hosts():
        for ex in examples:
            for st in ex.statements:
                if st.sql and st.sql.strip():
                    label = ex.name or (ex.description or f"#{ex.index}")
                    yield st.sql, host_id, f"executable example {label!r}"


def _iter_test_sql(catalog: Catalog) -> Iterator[tuple[str, ObjectId, str]]:
    """Agent-task reference + check SQL (grader-only, but still worker references)."""
    for task in catalog.agent_test_tasks:
        stmts: list[tuple[ExampleStatement | None, str]] = [
            (st, f"task {task.name!r} reference") for st in task.reference_statements
        ]
        for st, where in stmts:
            if st and st.sql and st.sql.strip():
                yield st.sql, catalog.id, where
        if task.check_sql and task.check_sql.strip():
            yield task.check_sql, catalog.id, f"task {task.name!r} check"


def compute_corpus_coverage(catalog: Catalog) -> CorpusCoverage:
    """Parse the worker's example/test SQL and report what it covers."""
    default_schema = (catalog.default_schema or "main").lower()
    qualifier = catalog.qualifier
    universe = _build_universe(catalog, default_schema)
    macro_keys = _macro_keys(catalog, default_schema)

    cov = CorpusCoverage(universe=universe, has_test_suite=bool(catalog.agent_test_tasks))

    # Per-macro call bookkeeping for the trivial-args signal.
    macro_called: set[str] = set()
    macro_live: set[str] = set()

    def scan(items: Iterator[tuple[str, ObjectId, str]], hit_set: set[str], source: str) -> None:
        for sql, origin, where in items:
            parsed = parse_refs(sql)
            if parsed is None:
                continue
            for ref in parsed.all:
                verdict = _classify(ref, qualifier, default_schema, universe)
                if not verdict:
                    continue
                if verdict.startswith("!"):
                    cov.broken.append(
                        Broken(reference=verdict[1:], origin=origin, where=where, source=source)
                    )
                    continue
                hit_set.add(verdict)
                if ref.is_function and verdict in macro_keys:
                    macro_called.add(verdict)
                    if not ref.const_only_args:
                        macro_live.add(verdict)

    # Documentation corpus: illustrative + executable examples both demonstrate.
    scan(_iter_doc_sql(catalog), cov.demonstrated, source="doc")
    scan(_iter_exec_sql(catalog), cov.runnable, source="doc")
    cov.demonstrated |= cov.runnable

    # Test corpus: agent-task reference/check SQL.
    scan(_iter_test_sql(catalog), cov.tested, source="test")

    # A macro called only with constants (and never live) is under-demonstrated.
    for key in macro_called - macro_live:
        cov.macro_const_only[key] = universe[key]

    return cov
