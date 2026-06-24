"""VGI8xx — constraint validity.

Constraints are not required, but when present they must be valid: foreign keys
must reference real tables/columns, and every constraint must reference columns
that exist. CHECK expressions can additionally be bound against the worker
(opt-in, `--execute`).
"""

from __future__ import annotations

from ..findings import Category, Severity
from ..model import ObjectKind
from .base import Rule
from .registry import register

CON = Category.CONSTRAINTS


@register
class ForeignKeyReferenceValid(Rule):
    code = "VGI801"
    name = "foreign-key-reference-valid"
    category = CON
    default_severity = Severity.ERROR
    targets = (ObjectKind.TABLE,)
    summary = "A foreign key must reference a table and columns that exist."

    def check(self, ctx):
        cat = ctx.catalog
        for table, c in cat.iter_constraints():
            if c.constraint_type != "FOREIGN KEY":
                continue
            # local columns must exist on this table
            cols = table.column_names()
            missing_local = [col for col in c.columns if col not in cols]
            if missing_local:
                yield self.finding(
                    ctx, table.id,
                    f"foreign key references local column(s) not on the table: "
                    f"{', '.join(missing_local)}",
                    "fix the foreign-key column list to match the table's columns",
                )
            if not c.referenced_table:
                continue
            targets = cat.find_table_like(c.referenced_table)
            if not targets:
                yield self.finding(
                    ctx, table.id,
                    f"foreign key references unknown table "
                    f"{c.referenced_table!r}",
                    "point the foreign key at a table that exists in the catalog",
                )
                continue
            ref_cols: set[str] = set()
            for t in targets:
                ref_cols |= t.column_names()
            missing_ref = [col for col in c.referenced_columns if col not in ref_cols]
            if missing_ref:
                yield self.finding(
                    ctx, table.id,
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

    def check(self, ctx):
        for table, c in ctx.catalog.iter_constraints():
            if c.constraint_type == "FOREIGN KEY":
                continue  # local FK columns handled by VGI801
            cols = table.column_names()
            missing = [col for col in c.columns if col not in cols]
            if missing:
                label = c.constraint_type.lower()
                yield self.finding(
                    ctx, table.id,
                    f"{label} constraint references column(s) not on the table: "
                    f"{', '.join(missing)}",
                    "fix the constraint to reference existing columns",
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

    def check(self, ctx):
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
                yield self.finding(
                    ctx, table.id,
                    f"CHECK constraint does not bind: {type(e).__name__}: {e}",
                    f"fix the CHECK expression: {expr[:120]}",
                )


def _check_expression(text: str) -> str:
    """Strip a leading ``CHECK(...)`` wrapper if present, leaving the predicate."""
    t = text.strip()
    upper = t.upper()
    if upper.startswith("CHECK") and "(" in t:
        inner = t[t.index("(") + 1 : t.rfind(")")]
        return inner.strip()
    return t
