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
from ..model import Catalog, ObjectKind
from ._util import is_filter_policy_error, run_with_timeout
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
