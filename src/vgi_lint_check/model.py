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

# Reserved tag keys VGI workers use as documentation/discovery channels.
TAG_DOC_LLM = "vgi.doc_llm"  # LLM-oriented narrative doc (canonical)
TAG_DOC_MD = "vgi.doc_md"  # Markdown narrative doc (canonical)
TAG_DOC_LINKS = "vgi.doc_links"  # JSON array of {title?, url} links to more docs
TAG_EXAMPLE_QUERIES = "vgi.example_queries"
TAG_EXECUTABLE_EXAMPLES = "vgi.executable_examples"  # self-contained, must-run examples
TAG_AGENT_TEST_TASKS = "vgi.agent_test_tasks"  # fixed analyst-task suite for `simulate`
TAG_TITLE = "vgi.title"  # human/marketing display name (vs the machine name)
TAG_KEYWORDS = "vgi.keywords"  # JSON array of search keywords / synonyms
TAG_CATEGORY = "vgi.category"  # single primary category an object belongs to (a registry name)
TAG_CATEGORIES = "vgi.categories"  # schema-level registry: ordered array of category objects
TAG_CLASSIFICATION_TAGS = "vgi.classification_tags"  # JSON array of cross-cutting facet labels
TAG_RESULT_COLUMNS_MD = "vgi.result_columns_md"  # Markdown doc of a table fn's result columns
TAG_SOURCE_URL = "vgi.source_url"  # link to where this object is implemented (repo/file)
TAG_AUTHOR = "vgi.author"  # author / maintainer attribution
TAG_COPYRIGHT = "vgi.copyright"  # copyright notice
TAG_LICENSE = "vgi.license"  # license name or SPDX identifier
TAG_SUPPORT_CONTACT = "vgi.support_contact"  # where to report issues/bugs (email or URL)
TAG_SUPPORT_POLICY_URL = "vgi.support_policy_url"  # link to the support/SLA policy

# Deprecated tag keys kept working for back-compat (old key -> canonical key).
TAG_DESCRIPTION_LLM = "vgi.description_llm"  # deprecated: use vgi.doc_llm
TAG_DESCRIPTION_MD = "vgi.description_md"  # deprecated: use vgi.doc_md
TAG_COLUMNS_MD = "vgi.columns_md"  # deprecated: use vgi.result_columns_md
TAG_CATEGORY_TAGS = "vgi.category_tags"  # deprecated: use vgi.classification_tags
DEPRECATED_TAG_ALIASES = {
    TAG_DESCRIPTION_LLM: TAG_DOC_LLM,
    TAG_DESCRIPTION_MD: TAG_DOC_MD,
    TAG_COLUMNS_MD: TAG_RESULT_COLUMNS_MD,
    TAG_CATEGORY_TAGS: TAG_CLASSIFICATION_TAGS,
}
# canonical key -> tuple of deprecated keys that resolve to it
_ALIASES_OF: dict[str, tuple[str, ...]] = {}
for _old, _new in DEPRECATED_TAG_ALIASES.items():
    _ALIASES_OF[_new] = (*_ALIASES_OF.get(_new, ()), _old)

RESERVED_TAG_KEYS = frozenset(
    {
        TAG_DOC_LLM,
        TAG_DOC_MD,
        TAG_DOC_LINKS,
        TAG_EXAMPLE_QUERIES,
        TAG_EXECUTABLE_EXAMPLES,
        TAG_AGENT_TEST_TASKS,
        TAG_TITLE,
        TAG_KEYWORDS,
        TAG_CATEGORY,
        TAG_CATEGORIES,
        TAG_CLASSIFICATION_TAGS,
        TAG_RESULT_COLUMNS_MD,
        TAG_SOURCE_URL,
        TAG_AUTHOR,
        TAG_COPYRIGHT,
        TAG_LICENSE,
        TAG_SUPPORT_CONTACT,
        TAG_SUPPORT_POLICY_URL,
        *DEPRECATED_TAG_ALIASES,
    }
)


class ObjectKind(StrEnum):
    """Kind of catalog object a finding or model node refers to."""

    CATALOG = "catalog"
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
    ATTACH_OPTION = "attach_option"
    # File-sourced tutorials (not catalog objects); used to anchor VGI13xx findings.
    TUTORIAL = "tutorial"
    TUTORIAL_STEP = "tutorial_step"


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
        """Return the value for ``key``, falling back to a deprecated alias."""
        v = self.raw.get(key)
        if v is not None:
            return v
        for old in _ALIASES_OF.get(key, ()):
            ov = self.raw.get(old)
            if ov is not None:
                return ov
        return None

    def has(self, key: str) -> bool:
        """True when ``key`` (or a deprecated alias) is present and non-blank."""
        return bool((self.get(key) or "").strip())

    def deprecated_keys(self) -> dict[str, str]:
        """Present deprecated tag keys mapped to their canonical replacement."""
        return {k: DEPRECATED_TAG_ALIASES[k] for k in self.raw if k in DEPRECATED_TAG_ALIASES}

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
class DocLink:
    """One entry of ``vgi.doc_links`` — a link to additional documentation."""

    title: str | None
    url: str | None


@dataclass(frozen=True)
class Category:
    """One entry of a schema's ``vgi.categories`` registry.

    ``name`` is the stable slug an object's ``vgi.category`` references; ``title``
    is the (optional) human display label. Registry order is the display order.
    """

    name: str
    title: str | None = None
    description: str | None = None
    keywords: list[str] = field(default_factory=list)
    doc_md: str | None = None

    @property
    def display_title(self) -> str:
        """The human label, falling back to a title-cased ``name``."""
        return self.title or self.name.replace("_", " ").replace("-", " ").title()


@dataclass(frozen=True)
class ExampleStatement:
    """One SQL step of an executable example (run in order).

    ``expected_result`` is an optional JSON value to assert this statement's
    output against (``has_expected`` records whether the key was present, so a
    declared ``null`` expectation is distinguishable from an omitted one).
    """

    description: str | None
    sql: str | None
    expected_result: object = None
    has_expected: bool = False


@dataclass(frozen=True)
class ExecutableExample:
    """A self-contained, must-run example from ``vgi.executable_examples``.

    Unlike :class:`ExampleQuery` (illustrative), every statement here must
    execute against the live worker, and any statement may carry an
    ``expected_result`` to assert its output.
    """

    index: int
    name: str | None
    description: str | None
    statements: list[ExampleStatement]
    raw: object = None


@dataclass(frozen=True)
class AgentTask:
    """One fixed analyst task from ``vgi.agent_test_tasks`` (catalog-level).

    Only ``prompt`` is shown to the simulated analyst; ``success_criteria``,
    ``reference_statements`` (the canonical solution sequence), and ``check_sql``
    are grader-only and must never leak into the actor's context.
    """

    name: str
    prompt: str
    success_criteria: str | None = None
    reference_statements: list[ExampleStatement] = field(default_factory=list)
    check_sql: str | None = None
    unordered: bool = False
    ignore_column_names: bool = False
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
    executable_examples: list[ExecutableExample] = field(default_factory=list)
    executable_examples_parse_error: str | None = None
    constraints: list[Constraint] = field(default_factory=list)
    # The duckdb_functions() table-function row backing this table, if any.
    backing_function: Function | None = None
    kind: ObjectKind = ObjectKind.TABLE

    def column_names(self) -> set[str]:
        """Set of this object's column names."""
        return {c.name for c in self.columns}

    @property
    def description_llm(self) -> str | None:
        """The ``vgi.doc_llm`` tag value, if any."""
        return self.tags.get(TAG_DOC_LLM)

    @property
    def description_md(self) -> str | None:
        """The ``vgi.doc_md`` tag value, if any."""
        return self.tags.get(TAG_DOC_MD)

    @property
    def category(self) -> str | None:
        """The object's primary ``vgi.category`` (a schema-registry name), if set."""
        return (self.tags.get(TAG_CATEGORY) or "").strip() or None


@dataclass
class View(Table):
    """A worker view (a Table with a SQL definition)."""

    sql_definition: str | None = None
    kind: ObjectKind = ObjectKind.VIEW


@dataclass(frozen=True)
class Argument:
    """One declared argument of a function (from ``vgi_function_arguments()``)."""

    name: str
    type: str | None = None
    description: str | None = None
    is_const: bool = False
    is_named: bool = False
    is_positional: bool = False
    is_varargs: bool = False
    is_table_input: bool = False
    is_any_type: bool = False
    # Discovery-facing constraint metadata (newer vgi extensions only; None when
    # absent). ``default``/``choices`` are JSON-encoded text, ``value_range`` is
    # interval notation (e.g. ``"[0, 100]"``), ``pattern`` is a raw regex.
    default: str | None = None
    choices: str | None = None
    value_range: str | None = None
    pattern: str | None = None


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
    executable_examples: list[ExecutableExample] = field(default_factory=list)
    executable_examples_parse_error: str | None = None
    macro_definition: str | None = None
    # Per-argument metadata from vgi_function_arguments() (empty on older vgi
    # extensions that don't expose it — the rule then emits nothing).
    arguments: list[Argument] = field(default_factory=list)
    # DuckDB function stability: CONSISTENT (deterministic), VOLATILE, or
    # CONSISTENT_WITHIN_QUERY. None for macros/table-functions (not applicable).
    stability: str | None = None

    @property
    def is_volatile(self) -> bool:
        """True when the function is declared VOLATILE (non-deterministic)."""
        return (self.stability or "").upper() == "VOLATILE"

    @property
    def category(self) -> str | None:
        """The function's primary ``vgi.category`` (a schema-registry name), if set."""
        return (self.tags.get(TAG_CATEGORY) or "").strip() or None

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
class AttachOption:
    """A declarative attach-time option a worker advertises via ``vgi_catalogs``.

    Discoverable *before* attach. ``required`` is not signalled on the wire —
    it is inferred from the absence of a default (``default is None``).
    """

    id: ObjectId
    name: str
    description: str | None = None
    type: str | None = None
    default: str | None = None

    @property
    def required(self) -> bool:
        """True when the option has no default, so a value must be supplied."""
        return self.default is None


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
    executable_examples: list[ExecutableExample] = field(default_factory=list)
    executable_examples_parse_error: str | None = None
    # Decoded vgi.categories registry (ordered) + its parse error, if malformed.
    categories: list[Category] = field(default_factory=list)
    categories_parse_error: str | None = None

    def iter_categorizable(self) -> list[Table | View | Function]:
        """Objects that may carry a ``vgi.category`` (tables, views, non-pragma functions)."""
        return [
            *self.tables,
            *self.views,
            *[f for f in self.functions if f.kind is not ObjectKind.PRAGMA],
        ]

    def opts_into_categories(self) -> bool:
        """True when this schema uses the category system (a registry, or any filed object)."""
        return bool(self.categories) or any(o.category for o in self.iter_categorizable())

    def iter_by_category(
        self,
    ) -> Iterator[tuple[Category | None, list[Table | View | Function]]]:
        """Yield ``(category, objects)`` in registry order, then ``(None, remainder)``.

        Objects whose ``vgi.category`` is not a defined registry name fall into the
        trailing uncategorized bucket — the orphan reference is reported separately by
        VGI409, not rendered here. Categories with no members still yield an empty list
        (so unused entries are visible to VGI412).
        """
        valid = {c.name for c in self.categories}
        by_name: dict[str, list[Table | View | Function]] = {c.name: [] for c in self.categories}
        uncategorized: list[Table | View | Function] = []
        for obj in self.iter_categorizable():
            cat = obj.category
            if cat and cat in valid:
                by_name[cat].append(obj)
            else:
                uncategorized.append(obj)
        for c in self.categories:
            yield c, by_name[c.name]
        if uncategorized:
            yield None, uncategorized


@dataclass(frozen=True)
class Release:
    """One published data-version release (from ``vgi_catalogs`` discovery)."""

    version: str
    released_at: str | None = None
    summary: str = ""
    notes_url: str | None = None


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
    # Catalog-level metadata (the worker's "listing"): comment + tags from
    # duckdb_databases(); source_url/releases from vgi_catalogs() discovery.
    comment: str | None = None
    tags: TagSet = field(default_factory=TagSet)
    source_url: str | None = None
    implementation_version: str | None = None
    data_version_spec: str | None = None  # semver range the catalog serves
    default_schema: str | None = None  # the worker's declared default schema
    releases: list[Release] = field(default_factory=list)
    schemas: list[Schema] = field(default_factory=list)
    settings: list[Setting] = field(default_factory=list)
    pragmas: list[Pragma] = field(default_factory=list)
    # Attach-time options advertised via vgi_catalogs() (discoverable pre-attach).
    attach_options: list[AttachOption] = field(default_factory=list)
    # Names of every catalog vgi_catalogs() advertised at this location.
    advertised_catalogs: list[str] = field(default_factory=list)
    # Catalog-level vgi.executable_examples (walkthroughs spanning the catalog).
    executable_examples: list[ExecutableExample] = field(default_factory=list)
    executable_examples_parse_error: str | None = None
    # Catalog-level vgi.agent_test_tasks (the fixed analyst-task suite).
    agent_test_tasks: list[AgentTask] = field(default_factory=list)
    agent_test_tasks_parse_error: str | None = None
    # Lazily-built {name: [tables/views]} index for FK reference resolution.
    _name_index: dict[str, list[Table | View]] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def id(self) -> ObjectId:
        """Identity of the catalog object itself."""
        return ObjectId(self.database, ObjectKind.CATALOG)

    @property
    def qualifier(self) -> str:
        """Catalog name used to qualify references in example queries."""
        return self.catalog_name or self.database

    @property
    def description_llm(self) -> str | None:
        """The catalog's ``vgi.doc_llm`` tag value, if any."""
        return self.tags.get(TAG_DOC_LLM)

    @property
    def description_md(self) -> str | None:
        """The catalog's ``vgi.doc_md`` tag value, if any."""
        return self.tags.get(TAG_DOC_MD)

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

    def iter_attach_options(self) -> Iterator[AttachOption]:
        """Iterate the catalog's advertised attach-time options."""
        return iter(self.attach_options)

    def iter_executable_example_hosts(
        self,
    ) -> Iterator[tuple[ObjectId, list[ExecutableExample], str | None]]:
        """Yield (object_id, executable_examples, parse_error) for every host.

        Executable examples can live on the catalog, any schema, table, view, or
        function — every object that carries tags.
        """
        yield self.id, self.executable_examples, self.executable_examples_parse_error
        for s in self.schemas:
            yield s.id, s.executable_examples, s.executable_examples_parse_error
            for t in s.tables:
                yield t.id, t.executable_examples, t.executable_examples_parse_error
            for v in s.views:
                yield v.id, v.executable_examples, v.executable_examples_parse_error
            for f in s.functions:
                yield f.id, f.executable_examples, f.executable_examples_parse_error

    def has_objects(self) -> bool:
        """True when the catalog exposes at least one table, view, or function."""
        return any(s.tables or s.views or s.functions for s in self.schemas)

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
