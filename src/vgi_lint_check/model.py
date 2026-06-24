"""Normalized catalog model that lint rules operate on.

This is decoupled from the raw DuckDB system-table rows: ``loader.py`` builds
these dataclasses from the rows so rules never touch SQL or worry about the
table/table-function asymmetry, the ``tags`` MAP shape, or JSON-encoded
example queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum

# Reserved tag keys VGI workers use as documentation channels.
TAG_DESCRIPTION_LLM = "vgi.description_llm"
TAG_DESCRIPTION_MD = "vgi.description_md"
TAG_EXAMPLE_QUERIES = "vgi.example_queries"
RESERVED_TAG_KEYS = frozenset(
    {TAG_DESCRIPTION_LLM, TAG_DESCRIPTION_MD, TAG_EXAMPLE_QUERIES}
)


class ObjectKind(str, Enum):
    SCHEMA = "schema"
    TABLE = "table"
    VIEW = "view"
    COLUMN = "column"
    TABLE_FUNCTION = "table_function"
    SCALAR_FUNCTION = "scalar_function"
    AGGREGATE = "aggregate"
    MACRO = "macro"
    PRAGMA = "pragma"
    SETTING = "setting"

    def __str__(self) -> str:  # nicer in f-strings / reports
        return self.value


# Maps duckdb_functions().function_type -> our ObjectKind.
FUNCTION_TYPE_KIND = {
    "table": ObjectKind.TABLE_FUNCTION,
    "scalar": ObjectKind.SCALAR_FUNCTION,
    "aggregate": ObjectKind.AGGREGATE,
    "macro": ObjectKind.MACRO,
    "table_macro": ObjectKind.MACRO,
    "pragma": ObjectKind.PRAGMA,
}


@dataclass(frozen=True)
class ObjectId:
    """Stable identity for a catalog object, used in findings and reports."""

    database: str
    kind: ObjectKind
    schema: str | None = None
    name: str | None = None
    column: str | None = None

    def qualified(self) -> str:
        parts = [p for p in (self.database, self.schema, self.name, self.column) if p]
        return ".".join(parts)

    def __str__(self) -> str:
        return f"{self.qualified()} ({self.kind})"


@dataclass(frozen=True)
class TagSet:
    """Normalized view over a DuckDB ``tags`` MAP (already a Python dict)."""

    raw: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.raw.get(key)

    def has(self, key: str) -> bool:
        return bool((self.raw.get(key) or "").strip())

    @property
    def plain(self) -> dict[str, str]:
        """Non-reserved tags (the user-facing key/value tags)."""
        return {k: v for k, v in self.raw.items() if k not in RESERVED_TAG_KEYS}


@dataclass(frozen=True)
class ExampleQuery:
    index: int
    description: str | None
    sql: str | None
    raw: object = None


@dataclass(frozen=True)
class Column:
    id: ObjectId
    name: str
    data_type: str | None = None
    comment: str | None = None

    @property
    def documented(self) -> bool:
        return bool((self.comment or "").strip())


@dataclass
class Table:
    id: ObjectId
    schema: str
    name: str
    comment: str | None = None
    tags: TagSet = field(default_factory=TagSet)
    columns: list[Column] = field(default_factory=list)
    column_count: int = 0
    estimated_size: int | None = None
    examples: list[ExampleQuery] = field(default_factory=list)
    examples_parse_error: str | None = None
    constraints: list["Constraint"] = field(default_factory=list)
    # The duckdb_functions() table-function row backing this table, if any.
    backing_function: "Function | None" = None
    kind: ObjectKind = ObjectKind.TABLE

    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    @property
    def description_llm(self) -> str | None:
        return self.tags.get(TAG_DESCRIPTION_LLM)

    @property
    def description_md(self) -> str | None:
        return self.tags.get(TAG_DESCRIPTION_MD)


@dataclass
class View(Table):
    sql_definition: str | None = None
    kind: ObjectKind = ObjectKind.VIEW


@dataclass
class Function:
    id: ObjectId
    schema: str
    name: str
    function_type: str
    description: str | None = None
    comment: str | None = None
    tags: TagSet = field(default_factory=TagSet)
    parameters: list[str] = field(default_factory=list)
    parameter_types: list[str] = field(default_factory=list)
    examples: list[ExampleQuery] = field(default_factory=list)
    examples_parse_error: str | None = None
    macro_definition: str | None = None

    @property
    def kind(self) -> ObjectKind:
        return FUNCTION_TYPE_KIND.get(self.function_type, ObjectKind.SCALAR_FUNCTION)

    @property
    def is_macro(self) -> bool:
        return self.kind is ObjectKind.MACRO

    @property
    def is_pragma(self) -> bool:
        return self.kind is ObjectKind.PRAGMA


@dataclass(frozen=True)
class Setting:
    """A worker-contributed DuckDB setting (scoped via the attach diff)."""

    id: ObjectId
    name: str
    description: str | None = None
    input_type: str | None = None
    scope: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class Pragma:
    """A worker-contributed pragma function (scoped via the attach diff)."""

    id: ObjectId
    name: str
    description: str | None = None
    tags: TagSet = field(default_factory=TagSet)


@dataclass(frozen=True)
class Constraint:
    """A table constraint from duckdb_constraints().

    ``referenced_table`` carries no schema qualifier — FKs may reference a table
    in another schema, resolved catalog-wide by name.
    """

    id: ObjectId  # the owning table's id
    schema: str
    table: str
    constraint_type: str  # PRIMARY KEY | FOREIGN KEY | CHECK | UNIQUE | NOT NULL
    columns: list[str] = field(default_factory=list)
    referenced_table: str | None = None
    referenced_columns: list[str] = field(default_factory=list)
    expression: str | None = None
    name: str | None = None


@dataclass
class Schema:
    id: ObjectId
    database: str
    name: str
    comment: str | None = None
    tags: TagSet = field(default_factory=TagSet)
    tables: list[Table] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)


@dataclass
class Catalog:
    database: str  # local attach alias (database_name in the system tables)
    location: str
    vgi_version: str | None = None
    data_version: str | None = None
    # The worker's own catalog name (what example authors qualify with). Falls
    # back to the alias when not separately known.
    catalog_name: str | None = None
    schemas: list[Schema] = field(default_factory=list)
    settings: list[Setting] = field(default_factory=list)
    pragmas: list[Pragma] = field(default_factory=list)

    @property
    def qualifier(self) -> str:
        """Catalog name used to qualify references in example queries."""
        return self.catalog_name or self.database

    # ---- iteration helpers used by rules ---------------------------------
    def iter_schemas(self):
        return iter(self.schemas)

    def iter_tables(self):
        for s in self.schemas:
            yield from s.tables

    def iter_views(self):
        for s in self.schemas:
            yield from s.views

    def iter_table_like(self):
        """Tables and views — both carry comment/tags/columns/examples."""
        for s in self.schemas:
            yield from s.tables
            yield from s.views

    def iter_columns(self):
        for t in self.iter_table_like():
            yield from t.columns

    def iter_functions(self):
        """Scalar/aggregate functions, macros, and pragmas (not table-functions)."""
        for s in self.schemas:
            for f in s.functions:
                if f.kind is not ObjectKind.TABLE_FUNCTION:
                    yield f

    def iter_all_functions(self):
        """Every function, including table-functions."""
        for s in self.schemas:
            yield from s.functions

    def iter_constraints(self):
        for t in self.iter_tables():
            for c in t.constraints:
                yield t, c

    def find_table_like(self, name, schema=None):
        """Tables/views matching a name (any schema unless one is given)."""
        return [
            t
            for t in self.iter_table_like()
            if t.name == name and (schema is None or t.schema == schema)
        ]

    def iter_macros(self):
        for f in self.iter_functions():
            if f.is_macro:
                yield f

    def iter_pragmas_fn(self):
        for f in self.iter_functions():
            if f.is_pragma:
                yield f
