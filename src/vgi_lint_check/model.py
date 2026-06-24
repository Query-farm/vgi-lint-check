"""Normalized catalog model that lint rules operate on.

This is decoupled from the raw DuckDB system-table rows: ``loader.py`` builds
these dataclasses from the rows so rules never touch SQL or worry about the
table/table-function asymmetry, the ``tags`` MAP shape, or JSON-encoded
example queries.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

# Reserved tag keys VGI workers use as documentation channels.
TAG_DESCRIPTION_LLM = "vgi.description_llm"
TAG_DESCRIPTION_MD = "vgi.description_md"
TAG_EXAMPLE_QUERIES = "vgi.example_queries"
RESERVED_TAG_KEYS = frozenset({TAG_DESCRIPTION_LLM, TAG_DESCRIPTION_MD, TAG_EXAMPLE_QUERIES})


class ObjectKind(StrEnum):
    """Kind of catalog object a finding or model node refers to."""

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
        """Dotted ``database.schema.name[.column]`` identifier."""
        parts = [p for p in (self.database, self.schema, self.name, self.column) if p]
        return ".".join(parts)

    def __str__(self) -> str:
        """Render as ``<qualified> (<kind>)`` for human-facing output."""
        return f"{self.qualified()} ({self.kind})"


@dataclass(frozen=True)
class TagSet:
    """Normalized view over a DuckDB ``tags`` MAP (already a Python dict)."""

    raw: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        """Return the raw value for ``key`` (or None)."""
        return self.raw.get(key)

    def has(self, key: str) -> bool:
        """True when ``key`` is present with a non-blank value."""
        return bool((self.raw.get(key) or "").strip())

    @property
    def plain(self) -> dict[str, str]:
        """Non-reserved tags (the user-facing key/value tags)."""
        return {k: v for k, v in self.raw.items() if k not in RESERVED_TAG_KEYS}


@dataclass(frozen=True)
class ExampleQuery:
    """One curated example query (from ``vgi.example_queries`` or native examples)."""

    index: int
    description: str | None
    sql: str | None
    raw: object = None


@dataclass(frozen=True)
class Column:
    """A column of a table or view."""

    id: ObjectId
    name: str
    data_type: str | None = None
    comment: str | None = None

    @property
    def documented(self) -> bool:
        """True when the column has a non-blank comment."""
        return bool((self.comment or "").strip())


@dataclass
class Table:
    """A worker table, with its columns, tags, examples, and constraints."""

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
    constraints: list[Constraint] = field(default_factory=list)
    # The duckdb_functions() table-function row backing this table, if any.
    backing_function: Function | None = None
    kind: ObjectKind = ObjectKind.TABLE

    def column_names(self) -> set[str]:
        """Set of this object's column names."""
        return {c.name for c in self.columns}

    @property
    def description_llm(self) -> str | None:
        """The ``vgi.description_llm`` tag value, if any."""
        return self.tags.get(TAG_DESCRIPTION_LLM)

    @property
    def description_md(self) -> str | None:
        """The ``vgi.description_md`` tag value, if any."""
        return self.tags.get(TAG_DESCRIPTION_MD)


@dataclass
class View(Table):
    """A worker view (a Table with a SQL definition)."""

    sql_definition: str | None = None
    kind: ObjectKind = ObjectKind.VIEW


@dataclass
class Function:
    """A scalar/aggregate function, macro, table-function, or pragma."""

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
        """Object kind derived from ``function_type``."""
        return FUNCTION_TYPE_KIND.get(self.function_type, ObjectKind.SCALAR_FUNCTION)

    @property
    def is_macro(self) -> bool:
        """True for SQL macros."""
        return self.kind is ObjectKind.MACRO

    @property
    def is_pragma(self) -> bool:
        """True for pragma functions."""
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
    """A schema and the objects it contains."""

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
    """A normalized view of everything one worker catalog contributes."""

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
    # Lazily-built {name: [tables/views]} index for FK reference resolution.
    _name_index: dict[str, list[Table | View]] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def qualifier(self) -> str:
        """Catalog name used to qualify references in example queries."""
        return self.catalog_name or self.database

    # ---- iteration helpers used by rules ---------------------------------
    def iter_schemas(self) -> Iterator[Schema]:
        """Iterate the catalog's schemas."""
        return iter(self.schemas)

    def iter_tables(self) -> Iterator[Table]:
        """Iterate every table across all schemas."""
        for s in self.schemas:
            yield from s.tables

    def iter_views(self) -> Iterator[View]:
        """Iterate every view across all schemas."""
        for s in self.schemas:
            yield from s.views

    def iter_table_like(self) -> Iterator[Table | View]:
        """Iterate tables and views — both carry comment/tags/columns/examples."""
        for s in self.schemas:
            yield from s.tables
            yield from s.views

    def iter_columns(self) -> Iterator[Column]:
        """Iterate every column of every table and view."""
        for t in self.iter_table_like():
            yield from t.columns

    def iter_functions(self) -> Iterator[Function]:
        """Iterate scalar/aggregate functions and macros (not table-functions)."""
        for s in self.schemas:
            for f in s.functions:
                if f.kind is not ObjectKind.TABLE_FUNCTION:
                    yield f

    def iter_all_functions(self) -> Iterator[Function]:
        """Iterate every function, including table-functions."""
        for s in self.schemas:
            yield from s.functions

    def iter_constraints(self) -> Iterator[tuple[Table, Constraint]]:
        """Iterate ``(table, constraint)`` pairs across the catalog."""
        for t in self.iter_tables():
            for c in t.constraints:
                yield t, c

    def find_table_like(self, name: str, schema: str | None = None) -> list[Table | View]:
        """Tables/views matching a name (any schema unless one is given).

        Uses a memoized name index so foreign-key resolution stays O(1) per
        lookup instead of O(tables) — important on large catalogs.
        """
        if self._name_index is None:
            index: dict[str, list[Table | View]] = {}
            for t in self.iter_table_like():
                index.setdefault(t.name, []).append(t)
            self._name_index = index
        matches = self._name_index.get(name, [])
        if schema is not None:
            return [t for t in matches if t.schema == schema]
        return list(matches)

    def iter_macros(self) -> Iterator[Function]:
        """Iterate macro functions."""
        for f in self.iter_functions():
            if f.is_macro:
                yield f

    def iter_pragmas_fn(self) -> Iterator[Function]:
        """Iterate pragma functions."""
        for f in self.iter_functions():
            if f.is_pragma:
                yield f
