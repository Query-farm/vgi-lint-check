"""VGI8xx — constraint validity.

Constraints are not required, but when present they must be valid: foreign keys
must reference real tables/columns, and every constraint must reference columns
that exist. CHECK expressions can additionally be bound against the worker
(opt-in, `--execute`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from ..findings import Category, Finding, Severity
from ..model import Catalog, ObjectKind, Table, View
from ._util import is_filter_policy_error, map_queries, run_with_timeout
from .base import Rule, RuleContext
from .registry import register

CON = Category.CONSTRAINTS

# Matches a leading ``CHECK(`` token (not ``CHECKSUM(`` etc.).
_CHECK_PREFIX = re.compile(r"^\s*CHECK\s*\(", re.IGNORECASE)


@register
class ForeignKeyReferenceValid(Rule):
    code = "VGI801"
    name = "foreign-key-reference-valid"
    category = CON
    default_severity = Severity.ERROR
    targets = (ObjectKind.TABLE,)
    summary = "A foreign key must reference a table and columns that exist."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        for table, c in cat.iter_constraints():
            if c.constraint_type != "FOREIGN KEY":
                continue
            # local columns must exist on this table
            cols = table.column_names()
            missing_local = [col for col in c.columns if col not in cols]
            if missing_local:
                yield self.finding(
                    ctx,
                    table.id,
                    f"foreign key references local column(s) not on the table: "
                    f"{', '.join(missing_local)}",
                    "fix the foreign-key column list to match the table's columns",
                )
            if not c.referenced_table:
                continue
            targets = cat.find_table_like(c.referenced_table)
            if not targets:
                yield self.finding(
                    ctx,
                    table.id,
                    f"foreign key references unknown table {c.referenced_table!r}",
                    "point the foreign key at a table that exists in the catalog",
                )
                continue
            ref_cols: set[str] = set()
            for t in targets:
                ref_cols |= t.column_names()
            missing_ref = [col for col in c.referenced_columns if col not in ref_cols]
            if missing_ref:
                yield self.finding(
                    ctx,
                    table.id,
                    f"foreign key references column(s) not on "
                    f"{c.referenced_table!r}: {', '.join(missing_ref)}",
                    "reference columns that exist on the target table",
                )


@register
class ConstraintColumnsExist(Rule):
    code = "VGI802"
    name = "constraint-columns-exist"
    category = CON
    default_severity = Severity.ERROR
    targets = (ObjectKind.TABLE,)
    summary = "Every constraint must reference columns that exist on the table."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for table, c in ctx.catalog.iter_constraints():
            if c.constraint_type == "FOREIGN KEY":
                continue  # local FK columns handled by VGI801
            cols = table.column_names()
            missing = [col for col in c.columns if col not in cols]
            if missing:
                label = c.constraint_type.lower()
                yield self.finding(
                    ctx,
                    table.id,
                    f"{label} constraint references column(s) not on the table: "
                    f"{', '.join(missing)}",
                    "fix the constraint to reference existing columns",
                )


def _constraint_counts(ctx: RuleContext) -> tuple[bool, int, int, int]:
    """(has_columns, total_constraints, primary_keys, not_nulls) for the catalog."""
    has_columns = any(t.columns for t in ctx.catalog.iter_tables())
    total = pk = not_null = 0
    for _t, c in ctx.catalog.iter_constraints():
        total += 1
        if c.constraint_type == "PRIMARY KEY":
            pk += 1
        elif c.constraint_type == "NOT NULL":
            not_null += 1
    return has_columns, total, pk, not_null


@register
class NoConstraintsAtAll(Rule):
    code = "VGI806"
    name = "no-constraints"
    category = CON
    default_severity = Severity.INFO
    targets = (ObjectKind.CATALOG,)
    summary = "A worker that declares no constraints at all is likely incomplete."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        has_columns, total, _pk, _nn = _constraint_counts(ctx)
        if has_columns and total == 0:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                "no constraints of any kind are declared on any table",
                "declare primary keys, NOT NULL, foreign keys, etc. — their "
                "absence usually means the metadata is unfinished",
            )


@register
class NoPrimaryKeys(Rule):
    code = "VGI805"
    name = "no-primary-keys"
    category = CON
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "A worker with constraints but no primary keys likely forgot them."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        has_columns, total, pk, _nn = _constraint_counts(ctx)
        # When there are no constraints at all, VGI806 reports the broader gap.
        if has_columns and total > 0 and pk == 0:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                "no primary keys on any table (other constraints exist)",
                "declare a primary key on tables that have a stable identity so "
                "agents know each row's key (or confirm none applies)",
            )


@register
class NotNullConstraintsPresent(Rule):
    code = "VGI804"
    name = "not-null-constraints-present"
    category = CON
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "A worker with constraints but no NOT NULL on any column likely forgot them."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        has_columns, total, _pk, not_null = _constraint_counts(ctx)
        # When there are no constraints at all, VGI806 reports the broader gap.
        if has_columns and total > 0 and not_null == 0:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                "no NOT NULL constraints on any table (other constraints exist)",
                "declare NOT NULL on columns that are always populated — agents "
                "rely on nullability to write correct queries (or confirm they "
                "were intentionally omitted)",
            )


@register
class CheckConstraintBinds(Rule):
    code = "VGI803"
    name = "check-constraint-binds"
    category = CON
    default_severity = Severity.ERROR
    targets = (ObjectKind.TABLE,)
    requires_connection = True
    summary = "CHECK constraint expressions should bind against the worker."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        qualifier = ctx.catalog.qualifier
        for table, c in ctx.catalog.iter_constraints():
            if c.constraint_type != "CHECK" or not c.expression:
                continue
            expr = _check_expression(c.expression)
            relation = f'"{qualifier}"."{table.schema}"."{table.name}"'
            sql = f"EXPLAIN SELECT 1 FROM {relation} WHERE ({expr}) LIMIT 0"
            try:
                run_with_timeout(con, lambda q=sql: con.execute(q), ctx.config.execute_timeout)
            except Exception as e:  # noqa: BLE001
                # The probe adds no required filter; a mandatory-filter rejection
                # is the worker's scan policy, not a bad CHECK expression.
                if is_filter_policy_error(e):
                    continue
                yield self.finding(
                    ctx,
                    table.id,
                    f"CHECK constraint does not bind: {type(e).__name__}: {e}",
                    f"fix the CHECK expression: {expr[:120]}",
                )


def _check_expression(text: str) -> str:
    """Strip a leading ``CHECK(...)`` wrapper if present, leaving the predicate.

    Anchors on ``CHECK(`` as a whole token (so ``CHECKSUM(x) > 0`` is left alone)
    and balance-matches the wrapper's parentheses (so ``CHECK((a) AND (b))``
    yields ``(a) AND (b)`` rather than over-/under-capturing with ``rfind``).
    """
    m = _CHECK_PREFIX.match(text)
    if not m:
        return text.strip()
    open_paren = m.end() - 1
    depth = 0
    for i in range(open_paren, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : i].strip()
    return text[open_paren + 1 :].strip()


@register
class TableHasPrimaryKey(Rule):
    code = "VGI807"
    name = "table-has-primary-key"
    category = CON
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE,)
    summary = "Each table should declare a primary key so agents know each row's identity."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for t in ctx.catalog.iter_tables():
            if not t.columns:
                continue
            if any(c.constraint_type == "PRIMARY KEY" for c in t.constraints):
                continue
            yield self.finding(
                ctx,
                t.id,
                "table has no primary key",
                "declare a primary key on the column(s) that uniquely identify a "
                "row so agents can reference rows (or confirm none applies)",
            )


# Matches a foreign-key-shaped column name: <base>_id / <base>id.
_FK_NAME = re.compile(r"^(?P<base>.+?)_?id$", re.IGNORECASE)


@register
class ForeignKeySuggested(Rule):
    code = "VGI808"
    name = "foreign-key-suggested"
    category = CON
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE,)
    summary = "A column named like a key (<table>_id) with no FK likely needs one declared."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        for t in cat.iter_tables():
            fk_cols = {
                col.lower()
                for c in t.constraints
                if c.constraint_type == "FOREIGN KEY"
                for col in c.columns
            }
            for col in t.columns:
                m = _FK_NAME.match(col.name or "")
                if not m or col.name.lower() in fk_cols:
                    continue
                base = m.group("base").lower().rstrip("_")
                if not base or base == t.name.lower():
                    continue
                target = self._target_table(cat, base, t.schema)
                if target is not None:
                    yield self.finding(
                        ctx,
                        t.id,
                        f"column {col.name!r} looks like a foreign key to "
                        f"{target!r} but no FK is declared",
                        f"declare a FOREIGN KEY from {col.name} to {target} so the "
                        "join is discoverable, or rename it if it isn't a reference",
                    )

    def _target_table(self, cat: Catalog, base: str, schema: str) -> str | None:
        # Match a table named like the column base (singular or plural).
        for cand in (base, base + "s", base.rstrip("s")):
            for t in cat.find_table_like(cand):
                if t.name.lower() == cand:
                    return str(t.name)
        return None


# A column name shaped like a key/reference (prefixed, so not a bare per-table id).
_REF_COLUMN = re.compile(r".+_(id|key|code|uuid|guid|ref|fk)$", re.IGNORECASE)


@register
class SharedColumnSuggestsRelationship(Rule):
    code = "VGI809"
    name = "shared-column-suggests-relationship"
    category = CON
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE,)
    summary = (
        "A key-shaped column shared by several tables with no FK may be a missing relationship."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        # Columns already participating in a declared foreign key (local or referenced).
        fk_cols: set[str] = set()
        for _t, c in cat.iter_constraints():
            if c.constraint_type == "FOREIGN KEY":
                for fk_col in (*c.columns, *c.referenced_columns):
                    fk_cols.add(fk_col.lower())
        # Group reference-shaped columns by name across tables.
        by_col: dict[str, list[Any]] = defaultdict(list)
        for t in cat.iter_tables():
            for column in t.columns:
                if _REF_COLUMN.match(column.name or ""):
                    by_col[column.name.lower()].append(t)
        for col_name, tables in by_col.items():
            names = sorted({t.name for t in tables})
            if len(names) < 2 or col_name in fk_cols:
                continue
            # If the column's base matches a real table, VGI808 already suggests
            # the FK target — don't double-flag here.
            base = re.sub(r"_(id|key|code|uuid|guid|ref|fk)$", "", col_name, flags=re.IGNORECASE)
            if base and (cat.find_table_like(base) or cat.find_table_like(base + "s")):
                continue
            for t in tables:
                others = ", ".join(n for n in names if n != t.name)
                yield self.finding(
                    ctx,
                    t.id,
                    f"column {col_name!r} also appears in {others} with no foreign key declared",
                    "if these tables relate on this column, declare a FOREIGN KEY so "
                    "the join is discoverable to agents; otherwise this is fine to ignore",
                )


def _ident(name: str) -> str:
    """Quote an identifier as a DuckDB double-quoted name (injection-safe)."""
    return '"' + str(name).replace('"', '""') + '"'


def _relation(qualifier: str, schema: str, name: str) -> str:
    return f"{_ident(qualifier)}.{_ident(schema)}.{_ident(name)}"


def _values(rows: Any) -> list[Any]:
    """Flatten single-column rows to non-NULL scalar values."""
    return [r[0] for r in rows if r[0] is not None]


def _run_sample_ladder(
    cur: Any, sample_sql: str, limit_sql: str, timeout: float
) -> tuple[Any, bool] | None:
    """Run a random ``USING SAMPLE`` query, falling back to deterministic ``LIMIT``.

    Returns ``(rows, used_limit)``. ``None`` means skip — either a mandatory-filter
    scan policy (both rungs would hit it) or a probe failure/timeout we won't
    false-positive on. The fallback fires on any non-policy rung-1 error, so a
    worker that doesn't support ``USING SAMPLE`` still gets a deterministic sample.
    """
    try:
        rows = run_with_timeout(cur, lambda q=sample_sql: cur.execute(q).fetchall(), timeout)
        return rows, False
    except Exception as e:  # noqa: BLE001 - degrade to the deterministic rung
        if is_filter_policy_error(e):
            return None
    try:
        rows = run_with_timeout(cur, lambda q=limit_sql: cur.execute(q).fetchall(), timeout)
        return rows, True
    except Exception:  # noqa: BLE001 - give up rather than emit a false finding
        return None


# One single-column FK to probe: (child table, local col, parent, referenced col).
_FkProbe = tuple[Table, str, "Table | View", str]


@register
class ForeignKeyReferencesResolve(Rule):
    code = "VGI810"
    name = "foreign-key-references-resolve"
    category = CON
    # Sampling can only *find* a broken reference, never *prove* integrity — so an
    # orphan is a warning (investigate), not a hard error, and finding nothing is
    # not a guarantee. VGI801 still validates the FK metadata as an error.
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE,)
    requires_connection = True
    summary = "Sampled foreign-key values should resolve to a row in the referenced table."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        items = list(self._probes(ctx.catalog))
        if not items:
            return
        n = max(1, ctx.config.sample_size)
        timeout = ctx.config.sample_timeout

        def work(item: _FkProbe, cur: Any) -> Finding | None:
            return self._probe_one(ctx, cur, item, n, timeout)

        results = map_queries(con, items, work, ctx.config.execute_concurrency)
        yield from (f for f in results if f is not None)

    def _probes(self, cat: Catalog) -> Iterator[_FkProbe]:
        """Single-column FKs whose parent is unambiguously resolvable.

        Composite FKs and any metadata defect (missing local/ref column, unknown
        or ambiguous parent) are left to VGI801 — this rule only probes *data* for
        FKs it can target without guessing.
        """
        for table, c in cat.iter_constraints():
            if c.constraint_type != "FOREIGN KEY":
                continue
            if len(c.columns) != 1 or len(c.referenced_columns) != 1 or not c.referenced_table:
                continue
            local, ref_col = c.columns[0], c.referenced_columns[0]
            if local not in table.column_names():
                continue
            targets = [
                t for t in cat.find_table_like(c.referenced_table) if ref_col in t.column_names()
            ]
            if len(targets) != 1:
                continue
            yield table, local, targets[0], ref_col

    def _probe_one(
        self, ctx: RuleContext, cur: Any, item: _FkProbe, n: int, timeout: float
    ) -> Finding | None:
        table, local, parent, ref_col = item
        qual = ctx.catalog.qualifier
        child_rel = _relation(qual, table.schema, table.name)
        parent_rel = _relation(qual, parent.schema, parent.name)

        sampled = self._sample_child(cur, child_rel, _ident(local), n, timeout)
        if sampled is None:
            return None  # worker scan policy / timeout / unsupported — can't probe
        values, method = sampled
        if not values:
            return None  # column is all-NULL or empty — nothing to resolve

        present = self._probe_parent(cur, parent_rel, _ident(ref_col), values, timeout)
        if present is None:
            return None  # parent probe failed — never false-positive on no evidence
        orphans = [v for v in values if v not in present]
        if not orphans:
            return None
        examples = ", ".join(repr(v) for v in orphans[:5])
        return self.finding(
            ctx,
            table.id,
            f"{len(orphans)} of {len(values)} foreign-key value(s) in "
            f'"{table.name}".{local} have no matching "{parent.name}".{ref_col} row '
            f"({method}): {examples}",
            "every foreign-key value should resolve to a parent row — investigate the "
            "orphaned references or correct the data/constraint",
        )

    def _sample_child(
        self, cur: Any, child_rel: str, lcol: str, n: int, timeout: float
    ) -> tuple[list[Any], str] | None:
        """Sample distinct non-NULL child values, with a graceful probe ladder.

        Rung 1 is a true random ``USING SAMPLE``; on any non-policy failure
        (unsupported clause, timeout) it falls back to a deterministic ``LIMIT``
        (non-random, but exact when it under-fills the limit). A mandatory-filter
        scan policy short-circuits to ``None`` — both rungs would hit it.
        """
        base = f"SELECT DISTINCT {lcol} AS v FROM {child_rel} WHERE {lcol} IS NOT NULL"
        res = _run_sample_ladder(
            cur, f"{base} USING SAMPLE {int(n)} ROWS", f"{base} LIMIT {int(n)}", timeout
        )
        if res is None:
            return None
        rows, used_limit = res
        values = _values(rows)
        if not used_limit:
            return values, "random sample"
        # Under-filling the LIMIT means the distinct values were exhausted — exact.
        method = (
            f"all {len(values)} distinct values" if len(values) < n else f"first {n}, unsampled"
        )
        return values, method

    def _probe_parent(
        self, cur: Any, parent_rel: str, rcol: str, values: list[Any], timeout: float
    ) -> set[Any] | None:
        """Which sampled values exist in the parent — a pushable ``IN`` lookup."""
        placeholders = ", ".join("?" for _ in values)
        sql = f"SELECT DISTINCT {rcol} AS v FROM {parent_rel} WHERE {rcol} IN ({placeholders})"
        try:
            rows = run_with_timeout(
                cur, lambda q=sql, p=list(values): cur.execute(q, p).fetchall(), timeout
            )
        except Exception:  # noqa: BLE001 - no evidence -> skip, never false-positive
            return None
        return set(_values(rows))


# One CHECK constraint to probe: (owning table, bound predicate expression).
_CheckProbe = tuple[Table, str]


@register
class CheckConstraintHolds(Rule):
    code = "VGI811"
    name = "check-constraint-holds"
    category = CON
    # The data-level complement to VGI803 (which only checks the expression
    # *binds*). Sampling finds counterexamples but can't prove the invariant
    # holds, so a violation is a warning, not an error.
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE,)
    requires_connection = True
    summary = "Sampled rows should satisfy the table's declared CHECK constraints."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        con = ctx.connection
        if con is None:
            return
        items = list(self._probes(ctx.catalog))
        if not items:
            return
        n = max(1, ctx.config.sample_size)
        timeout = ctx.config.sample_timeout

        def work(item: _CheckProbe, cur: Any) -> Finding | None:
            return self._probe_one(ctx, cur, item, n, timeout)

        results = map_queries(con, items, work, ctx.config.execute_concurrency)
        yield from (f for f in results if f is not None)

    def _probes(self, cat: Catalog) -> Iterator[_CheckProbe]:
        """Every CHECK constraint that carries a non-empty predicate."""
        for table, c in cat.iter_constraints():
            if c.constraint_type != "CHECK" or not c.expression:
                continue
            expr = _check_expression(c.expression)
            if expr:
                yield table, expr

    def _probe_one(
        self, ctx: RuleContext, cur: Any, item: _CheckProbe, n: int, timeout: float
    ) -> Finding | None:
        table, expr = item
        rel = _relation(ctx.catalog.qualifier, table.schema, table.name)
        # A CHECK passes unless the predicate is FALSE — NULL/UNKNOWN satisfies it
        # (SQL CHECK semantics), so a true violation is exactly ``(expr) IS FALSE``.
        inner = f"SELECT (({expr}) IS FALSE) AS bad FROM {rel}"
        agg = "SELECT count(*) AS n, count(*) FILTER (WHERE bad) AS bad"
        res = _run_sample_ladder(
            cur,
            f"{agg} FROM ({inner} USING SAMPLE {int(n)} ROWS) t",
            f"{agg} FROM ({inner} LIMIT {int(n)}) t",
            timeout,
        )
        if res is None:
            return None  # scan policy / runtime failure — can't probe
        rows, used_limit = res
        if not rows:
            return None
        total, bad = int(rows[0][0]), int(rows[0][1])
        if total == 0 or bad == 0:
            return None
        if not used_limit:
            method = f"random sample of {total} rows"
        else:
            method = f"all {total} rows" if total < n else f"first {n} rows"
        return self.finding(
            ctx,
            table.id,
            f'{bad} of {total} sampled rows in "{table.name}" violate CHECK '
            f"({expr[:80]}) ({method})",
            "rows in the worker break this declared CHECK invariant — fix the data, "
            "or correct/remove the constraint so the metadata matches reality",
        )
