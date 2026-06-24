"""VGI8xx — constraint validity.

Constraints are not required, but when present they must be valid: foreign keys
must reference real tables/columns, and every constraint must reference columns
that exist. CHECK expressions can additionally be bound against the worker
(opt-in, `--execute`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import ObjectKind
from ._util import is_filter_policy_error
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
    default_severity = Severity.WARNING
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
            try:
                con.execute(f"EXPLAIN SELECT 1 FROM {relation} WHERE ({expr}) LIMIT 0")
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
