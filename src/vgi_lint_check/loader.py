"""Build the normalized ``Catalog`` model from a post-attach ``Snapshot``.

Responsibilities:
- scope catalog objects by ``database_name == alias`` (VGI marks worker objects
  ``internal=true``, so we keep them; built-ins live outside the alias);
- normalize ``tags`` and decode ``vgi.example_queries``;
- correlate each ``function_type='table'`` row to its table so table-functions
  are not flagged for tags that live on the table row;
- attach diff-scoped settings and pragmas (which carry no catalog qualifier).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .model import (
    Argument,
    AttachOption,
    Catalog,
    Column,
    Constraint,
    ExampleQuery,
    Function,
    ObjectId,
    ObjectKind,
    Pragma,
    Release,
    Schema,
    Setting,
    Table,
    View,
)
from .tags import (
    decode_agent_test_tasks,
    decode_categories,
    decode_example_queries,
    decode_executable_examples,
    decode_result_columns_schema,
    decode_result_dynamic_columns_md,
    to_tagset,
)

if TYPE_CHECKING:
    from .snapshot import Snapshot


def _scoped(rows: list[dict[str, Any]], alias: str) -> list[Any]:
    """Rows belonging to the attached catalog.

    Scoping is by ``database_name == alias`` only. We do NOT drop
    ``internal=true`` rows: the VGI extension marks the worker's own objects
    internal, while genuine DuckDB built-ins live in the ``system``/``temp``
    catalogs and are already excluded by the alias filter.
    """
    return [r for r in rows if r.get("database_name") == alias]


def _native_examples(raw: Any) -> list[ExampleQuery]:
    """Build ``ExampleQuery`` objects from the native examples column.

    The native ``duckdb_functions().examples`` column is a ``VARCHAR[]`` of SQL
    strings. The VGI extension surfaces a function's ``Meta.examples`` into this
    native column for every function type, while the ``vgi.example_queries`` tag
    is the alternative carrier. These SQL strings have no per-example description.
    """
    sqls = [str(sql) for sql in (raw or []) if sql is not None]
    return [ExampleQuery(index=i, description=None, sql=sql) for i, sql in enumerate(sqls)]


def _merge_examples(tagged: list[ExampleQuery], native: list[ExampleQuery]) -> list[ExampleQuery]:
    """Union tag-carried and native ``Meta.examples``, deduped by SQL, reindexed.

    The ``vgi.example_queries`` tag and the native ``duckdb_functions().examples``
    column are independent carriers — a function may use either or both. We keep
    every distinct query (tag entries first, since they can carry a description)
    so ``--execute`` runs them all. SQL strings are compared case- and
    whitespace-insensitively so the same query surfaced by both carriers runs once.
    """
    merged: list[ExampleQuery] = []
    seen: set[str] = set()
    for ex in [*tagged, *native]:
        key = " ".join((ex.sql or "").split()).lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(ExampleQuery(index=len(merged), description=ex.description, sql=ex.sql))
    return merged


def _build_attach_options(alias: str, infos: list[Any] | None) -> list[AttachOption]:
    """Normalize ``vgi_catalogs()`` attach-option structs into the model type.

    Duck-typed on ``name``/``description``/``type``/``default`` so the discovery
    dataclass and any equivalent row shape both work; entries without a name are
    unusable at ATTACH time and are dropped.
    """
    out: list[AttachOption] = []
    info: Any
    for info in infos or []:
        name = getattr(info, "name", None)
        if not name:
            continue
        out.append(
            AttachOption(
                id=ObjectId(alias, ObjectKind.ATTACH_OPTION, name=name),
                name=name,
                description=getattr(info, "description", None),
                type=getattr(info, "type", None),
                default=getattr(info, "default", None),
            )
        )
    return out


def _optional_int(value: Any) -> int | None:
    """Normalize an optional integer-valued diagnostic column."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _argument_function_type(value: Any) -> Any:
    """Match vgi_function_arguments() macro names to duckdb_functions()."""
    return "macro" if value == "scalar_macro" else value


def build_catalog(
    snapshot: Snapshot,
    alias: str,
    location: str,
    *,
    vgi_version: str | None = None,
    data_version: str | None = None,
    catalog_name: str | None = None,
    source_url: str | None = None,
    implementation_version: str | None = None,
    data_version_spec: str | None = None,
    default_schema: str | None = None,
    releases: list[Release] | None = None,
    setting_rows: list[dict[str, Any]] | None = None,
    pragma_rows: list[dict[str, Any]] | None = None,
    attach_options: list[Any] | None = None,
    advertised_catalogs: list[str] | None = None,
    advertised_attach_options: dict[str, list[Any]] | None = None,
    argument_rows: list[dict[str, Any]] | None = None,
    copy_handler_rows: list[dict[str, Any]] | None = None,
) -> Catalog:
    """Build a normalized :class:`Catalog` from a post-attach snapshot."""
    # COPY-format handler function names (from vgi_copy_formats()). These table
    # functions bind only inside a COPY (FORMAT 'x') statement, so they can never
    # be covered by example/test SQL; corpus coverage excludes them.
    copy_handlers = frozenset(
        str(r["handler"]) for r in (copy_handler_rows or []) if r.get("handler")
    )
    # Per-argument metadata (vgi_function_arguments()), grouped by function type
    # as well as name. A name with several overloads of one type keeps all its
    # arguments so semantic compilation can reject the ambiguity explicitly.
    args_by_key: dict[tuple[Any, Any, Any], list[Argument]] = {}
    input_from_args_by_key: dict[tuple[Any, Any, Any], bool] = {}
    for r in argument_rows or []:
        key = (
            r.get("schema_name"),
            r.get("function_name"),
            _argument_function_type(r.get("function_type")),
        )
        name = r.get("arg_name")
        if not name:
            continue
        input_from_args_by_key[key] = input_from_args_by_key.get(key, False) or bool(
            r.get("input_from_args")
        )
        args_by_key.setdefault(key, []).append(
            Argument(
                name=str(name),
                position=_optional_int(r.get("arg_position")),
                field_index=_optional_int(r.get("field_index")),
                type=r.get("arg_type"),
                description=r.get("arg_description"),
                is_const=bool(r.get("is_const")),
                is_named=bool(r.get("is_named")),
                is_positional=bool(r.get("is_positional")),
                is_varargs=bool(r.get("is_varargs")),
                is_table_input=bool(r.get("is_table_input")),
                is_any_type=bool(r.get("is_any_type")),
                default=r.get("arg_default"),
                choices=r.get("arg_choices"),
                value_range=r.get("arg_range"),
                pattern=r.get("arg_pattern"),
            )
        )
    schemas: dict[str, Schema] = {}

    # Catalog-level metadata from duckdb_databases() (the worker's "listing").
    catalog_comment: str | None = None
    catalog_tags = to_tagset(None)
    for r in _scoped(snapshot.databases, alias):
        catalog_comment = r.get("comment")
        catalog_tags = to_tagset(r.get("tags"))
        break
    catalog_exec_ex, catalog_exec_err = decode_executable_examples(catalog_tags)
    catalog_tasks, catalog_tasks_err = decode_agent_test_tasks(catalog_tags)

    def get_schema(name: str) -> Schema:
        if name not in schemas:
            schemas[name] = Schema(
                id=ObjectId(alias, ObjectKind.SCHEMA, schema=name),
                database=alias,
                name=name,
            )
        return schemas[name]

    # --- schemas ----------------------------------------------------------
    for r in _scoped(snapshot.schemas, alias):
        name = r.get("schema_name")
        if name is None:
            continue
        s = get_schema(name)
        s.comment = r.get("comment")
        s.tags = to_tagset(r.get("tags"))
        s.executable_examples, s.executable_examples_parse_error = decode_executable_examples(
            s.tags
        )
        s.categories, s.categories_parse_error = decode_categories(s.tags)

    # --- tables -----------------------------------------------------------
    tables_by_key: dict[tuple[str, str], Table] = {}
    for r in _scoped(snapshot.tables, alias):
        sname, tname = r.get("schema_name"), r.get("table_name")
        tags = to_tagset(r.get("tags"))
        examples, err = decode_example_queries(tags)
        exec_ex, exec_err = decode_executable_examples(tags)
        t = Table(
            id=ObjectId(alias, ObjectKind.TABLE, schema=sname, name=tname),
            schema=sname,
            name=tname,
            comment=r.get("comment"),
            tags=tags,
            column_count=r.get("column_count") or 0,
            estimated_size=r.get("estimated_size"),
            examples=examples,
            examples_parse_error=err,
            executable_examples=exec_ex,
            executable_examples_parse_error=exec_err,
        )
        tables_by_key[(sname, tname)] = t
        get_schema(sname).tables.append(t)

    # --- views ------------------------------------------------------------
    views_by_key: dict[tuple[str, str], View] = {}
    for r in _scoped(snapshot.views, alias):
        sname, vname = r.get("schema_name"), r.get("view_name")
        tags = to_tagset(r.get("tags"))
        examples, err = decode_example_queries(tags)
        exec_ex, exec_err = decode_executable_examples(tags)
        v = View(
            id=ObjectId(alias, ObjectKind.VIEW, schema=sname, name=vname),
            schema=sname,
            name=vname,
            comment=r.get("comment"),
            tags=tags,
            column_count=r.get("column_count") or 0,
            examples=examples,
            examples_parse_error=err,
            executable_examples=exec_ex,
            executable_examples_parse_error=exec_err,
            sql_definition=r.get("sql"),
        )
        views_by_key[(sname, vname)] = v
        get_schema(sname).views.append(v)

    # --- columns ----------------------------------------------------------
    # DuckDB 2.0 may expose table-function output columns here too. Functions
    # are loaded below, so retain any row that is not owned by a relation and
    # attach it after the corresponding function row is built.
    unclaimed_columns: dict[tuple[str, str], list[Column]] = {}
    for r in _scoped(snapshot.columns, alias):
        sname = r.get("schema_name")
        tname = r.get("table_name") or r.get("function_name")
        cname = r.get("column_name")
        column = Column(
            id=ObjectId(alias, ObjectKind.COLUMN, schema=sname, name=tname, column=cname),
            name=cname,
            data_type=r.get("data_type"),
            comment=r.get("comment"),
            tags=to_tagset(r.get("tags")),
        )
        target = tables_by_key.get((sname, tname)) or views_by_key.get((sname, tname))
        if target is None:
            unclaimed_columns.setdefault((sname, tname), []).append(column)
            continue
        target.columns.append(column)

    # --- functions / macros (table-functions correlated to their table) ---
    for r in _scoped(snapshot.functions, alias):
        sname, fname = r.get("schema_name"), r.get("function_name")
        ftype = r.get("function_type") or "scalar"
        # Pragmas are handled via the diff-scoped catalog.pragmas list (they may
        # register globally without a catalog qualifier); skip here to avoid
        # representing them twice.
        if ftype == "pragma":
            continue
        tags = to_tagset(r.get("tags"))
        examples, err = decode_example_queries(tags)
        # Merge in the native duckdb_functions().examples column, which the VGI
        # extension populates from the worker's Meta.examples. The tag and the
        # native column are independent carriers — run examples from both so
        # Meta.examples are exercised by --execute, not just tag-carried ones.
        examples = _merge_examples(examples, _native_examples(r.get("examples")))
        exec_ex, exec_err = decode_executable_examples(tags)
        result_cols, result_cols_err = decode_result_columns_schema(tags)
        dyn_tables, dyn_err = decode_result_dynamic_columns_md(tags)
        fn = Function(
            id=ObjectId(alias, _function_objectkind(ftype), schema=sname, name=fname),
            schema=sname,
            name=fname,
            function_type=ftype,
            description=r.get("description"),
            comment=r.get("comment"),
            tags=tags,
            parameters=list(r.get("parameters") or []),
            parameter_types=list(r.get("parameter_types") or []),
            examples=examples,
            examples_parse_error=err,
            executable_examples=exec_ex,
            executable_examples_parse_error=exec_err,
            result_columns=result_cols,
            native_result_columns=(
                unclaimed_columns.get((sname, fname), [])
                if _function_objectkind(ftype) is ObjectKind.TABLE_FUNCTION
                else []
            ),
            result_columns_parse_error=result_cols_err,
            result_dynamic_tables=dyn_tables,
            result_dynamic_parse_error=dyn_err,
            macro_definition=r.get("macro_definition"),
            stability=r.get("stability"),
            arguments=args_by_key.get(
                (sname, fname, ftype),
                # Compatibility with synthetic snapshots that predate the
                # function_type column on argument rows.
                args_by_key.get((sname, fname, None), []),
            ),
            input_from_args=input_from_args_by_key.get(
                (sname, fname, ftype), input_from_args_by_key.get((sname, fname, None), False)
            ),
        )
        # Correlate a table-function to its table so column/desc rules use the
        # richer table row rather than flagging the bare function.
        if fn.kind is ObjectKind.TABLE_FUNCTION:
            backing = tables_by_key.get((sname, fname))
            if backing is not None:
                backing.backing_function = fn
        get_schema(sname).functions.append(fn)

    # --- constraints (attached to their owning table) ---------------------
    for r in _scoped(snapshot.constraints, alias):
        sname, tname = r.get("schema_name"), r.get("table_name")
        owner = tables_by_key.get((sname, tname))
        if owner is None:
            continue
        owner.constraints.append(
            Constraint(
                id=owner.id,
                schema=sname,
                table=tname,
                constraint_type=(r.get("constraint_type") or "").upper(),
                columns=list(r.get("constraint_column_names") or []),
                referenced_table=r.get("referenced_table") or None,
                referenced_columns=list(r.get("referenced_column_names") or []),
                expression=r.get("expression") or r.get("constraint_text"),
                name=r.get("constraint_name"),
            )
        )

    # --- settings (diff-scoped; no catalog qualifier) ---------------------
    settings: list[Setting] = []
    setting_row: Any
    for setting_row in setting_rows or []:
        r = setting_row
        name = r.get("name")
        settings.append(
            Setting(
                id=ObjectId(alias, ObjectKind.SETTING, name=name),
                name=name,
                description=r.get("description"),
                input_type=r.get("input_type"),
                scope=r.get("scope"),
                value=None if r.get("value") is None else str(r.get("value")),
            )
        )

    # --- pragmas (diff-scoped; from duckdb_functions where type='pragma') -
    pragmas: list[Pragma] = []
    pragma_row: Any
    for pragma_row in pragma_rows or []:
        r = pragma_row
        name = r.get("function_name")
        pragmas.append(
            Pragma(
                id=ObjectId(alias, ObjectKind.PRAGMA, name=name),
                name=name,
                description=r.get("description"),
                tags=to_tagset(r.get("tags")),
            )
        )

    # --- attach options (from vgi_catalogs() discovery; pre-attach) -------
    opts = _build_attach_options(alias, attach_options)
    advertised_opts = {
        name: _build_attach_options(alias, infos)
        for name, infos in (advertised_attach_options or {}).items()
    }

    return Catalog(
        database=alias,
        location=location,
        vgi_version=vgi_version,
        data_version=data_version,
        catalog_name=catalog_name or alias,
        comment=catalog_comment,
        tags=catalog_tags,
        source_url=source_url,
        implementation_version=implementation_version,
        data_version_spec=data_version_spec,
        default_schema=default_schema,
        releases=list(releases or []),
        schemas=list(schemas.values()),
        settings=settings,
        pragmas=pragmas,
        attach_options=opts,
        advertised_catalogs=list(advertised_catalogs or []),
        advertised_attach_options=advertised_opts,
        executable_examples=catalog_exec_ex,
        executable_examples_parse_error=catalog_exec_err,
        agent_test_tasks=catalog_tasks,
        agent_test_tasks_parse_error=catalog_tasks_err,
        copy_handlers=copy_handlers,
    )


# duckdb function_type -> ObjectKind for the ObjectId.kind of a Function.
def _function_objectkind(ftype: str) -> ObjectKind:
    from .model import FUNCTION_TYPE_KIND

    return FUNCTION_TYPE_KIND.get(ftype, ObjectKind.SCALAR_FUNCTION)
