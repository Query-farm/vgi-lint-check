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

from .model import (
    Catalog,
    Column,
    Function,
    ObjectId,
    ObjectKind,
    Pragma,
    Schema,
    Setting,
    Table,
    View,
)
from .tags import decode_example_queries, to_tagset


def _scoped(rows, alias):
    """Rows belonging to the attached catalog.

    Scoping is by ``database_name == alias`` only. We do NOT drop
    ``internal=true`` rows: the VGI extension marks the worker's own objects
    internal, while genuine DuckDB built-ins live in the ``system``/``temp``
    catalogs and are already excluded by the alias filter.
    """
    return [r for r in rows if r.get("database_name") == alias]


def build_catalog(
    snapshot,
    alias: str,
    location: str,
    *,
    vgi_version: str | None = None,
    data_version: str | None = None,
    catalog_name: str | None = None,
    setting_rows: list[dict] | None = None,
    pragma_rows: list[dict] | None = None,
) -> Catalog:
    schemas: dict[str, Schema] = {}

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

    # --- tables -----------------------------------------------------------
    tables_by_key: dict[tuple[str, str], Table] = {}
    for r in _scoped(snapshot.tables, alias):
        sname, tname = r.get("schema_name"), r.get("table_name")
        tags = to_tagset(r.get("tags"))
        examples, err = decode_example_queries(tags)
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
        )
        tables_by_key[(sname, tname)] = t
        get_schema(sname).tables.append(t)

    # --- views ------------------------------------------------------------
    views_by_key: dict[tuple[str, str], View] = {}
    for r in _scoped(snapshot.views, alias):
        sname, vname = r.get("schema_name"), r.get("view_name")
        tags = to_tagset(r.get("tags"))
        examples, err = decode_example_queries(tags)
        v = View(
            id=ObjectId(alias, ObjectKind.VIEW, schema=sname, name=vname),
            schema=sname,
            name=vname,
            comment=r.get("comment"),
            tags=tags,
            column_count=r.get("column_count") or 0,
            examples=examples,
            examples_parse_error=err,
            sql_definition=r.get("sql"),
        )
        views_by_key[(sname, vname)] = v
        get_schema(sname).views.append(v)

    # --- columns ----------------------------------------------------------
    for r in _scoped(snapshot.columns, alias):
        sname, tname, cname = (
            r.get("schema_name"),
            r.get("table_name"),
            r.get("column_name"),
        )
        target = tables_by_key.get((sname, tname)) or views_by_key.get((sname, tname))
        if target is None:
            continue
        target.columns.append(
            Column(
                id=ObjectId(
                    alias, ObjectKind.COLUMN, schema=sname, name=tname, column=cname
                ),
                name=cname,
                data_type=r.get("data_type"),
                comment=r.get("comment"),
            )
        )

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
            macro_definition=r.get("macro_definition"),
        )
        # Correlate a table-function to its table so column/desc rules use the
        # richer table row rather than flagging the bare function.
        if fn.kind is ObjectKind.TABLE_FUNCTION:
            t = tables_by_key.get((sname, fname))
            if t is not None:
                t.backing_function = fn
        get_schema(sname).functions.append(fn)

    # --- settings (diff-scoped; no catalog qualifier) ---------------------
    settings: list[Setting] = []
    for r in setting_rows or []:
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
    for r in pragma_rows or []:
        name = r.get("function_name")
        pragmas.append(
            Pragma(
                id=ObjectId(alias, ObjectKind.PRAGMA, name=name),
                name=name,
                description=r.get("description"),
                tags=to_tagset(r.get("tags")),
            )
        )

    return Catalog(
        database=alias,
        location=location,
        vgi_version=vgi_version,
        data_version=data_version,
        catalog_name=catalog_name or alias,
        schemas=list(schemas.values()),
        settings=settings,
        pragmas=pragmas,
    )


# duckdb function_type -> ObjectKind for the ObjectId.kind of a Function.
def _function_objectkind(ftype: str) -> ObjectKind:
    from .model import FUNCTION_TYPE_KIND

    return FUNCTION_TYPE_KIND.get(ftype, ObjectKind.SCALAR_FUNCTION)
