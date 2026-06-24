"""Hand-built catalog fixtures for offline rule tests."""

from __future__ import annotations

from vgi_lint_check.model import (
    Catalog,
    Column,
    Constraint,
    ExampleQuery,
    Function,
    ObjectId,
    ObjectKind,
    Pragma,
    Schema,
    Setting,
    Table,
    TagSet,
    View,
)


def col(schema, table, name, comment=None, dtype="VARCHAR"):
    return Column(
        id=ObjectId("v", ObjectKind.COLUMN, schema=schema, name=table, column=name),
        name=name,
        data_type=dtype,
        comment=comment,
    )


def table(
    schema,
    name,
    *,
    comment=None,
    tags=None,
    columns=(),
    examples=(),
    parse_error=None,
    constraints=(),
):
    return Table(
        id=ObjectId("v", ObjectKind.TABLE, schema=schema, name=name),
        schema=schema,
        name=name,
        comment=comment,
        tags=TagSet(dict(tags or {})),
        columns=list(columns),
        column_count=len(columns),
        examples=list(examples),
        examples_parse_error=parse_error,
        constraints=list(constraints),
    )


def constraint(
    schema, tbl, ctype, columns=(), referenced_table=None, referenced_columns=(), expression=None
):
    return Constraint(
        id=ObjectId("v", ObjectKind.TABLE, schema=schema, name=tbl),
        schema=schema,
        table=tbl,
        constraint_type=ctype,
        columns=list(columns),
        referenced_table=referenced_table,
        referenced_columns=list(referenced_columns),
        expression=expression,
    )


def view(schema, name, *, comment=None, tags=None, columns=(), examples=()):
    return View(
        id=ObjectId("v", ObjectKind.VIEW, schema=schema, name=name),
        schema=schema,
        name=name,
        comment=comment,
        tags=TagSet(dict(tags or {})),
        columns=list(columns),
        examples=list(examples),
    )


def func(
    schema,
    name,
    ftype="scalar",
    *,
    description=None,
    comment=None,
    parameters=(),
    examples=(),
    tags=None,
):
    return Function(
        id=ObjectId("v", ObjectKind.SCALAR_FUNCTION, schema=schema, name=name),
        schema=schema,
        name=name,
        function_type=ftype,
        description=description,
        comment=comment,
        tags=TagSet(dict(tags or {})),
        parameters=list(parameters),
        examples=list(examples),
    )


def example(i, description, sql):
    return ExampleQuery(index=i, description=description, sql=sql, raw={})


def setting(name, description=None):
    return Setting(
        id=ObjectId("v", ObjectKind.SETTING, name=name), name=name, description=description
    )


def pragma(name, description=None):
    return Pragma(
        id=ObjectId("v", ObjectKind.PRAGMA, name=name), name=name, description=description
    )


def schema(name, *, comment=None, tags=None, tables=(), views=(), functions=()):
    return Schema(
        id=ObjectId("v", ObjectKind.SCHEMA, schema=name),
        database="v",
        name=name,
        comment=comment,
        tags=TagSet(dict(tags or {})),
        tables=list(tables),
        views=list(views),
        functions=list(functions),
    )


def catalog(
    *schemas,
    settings=(),
    pragmas=(),
    comment="A test catalog of things.",
    tags=None,
    source_url="https://example.com",
    releases=(),
):
    # Default catalog metadata satisfies the VGI0xx required rules so per-object
    # rule tests aren't polluted; pass comment=None / tags={} to test VGI00x.
    if tags is None:
        tags = {
            "vgi.description_llm": "A test catalog used by the unit tests, etc. " * 2,
            "vgi.description_md": "## Test catalog\nUsed by the unit tests. " * 3,
            "vgi.author": "Test Author",
            "vgi.copyright": "(c) 2026 Test",
            "vgi.license": "MIT",
            "vgi.support_contact": "support@example.com",
            "vgi.support_policy_url": "https://example.com/support",
        }
    return Catalog(
        database="v",
        location="loc",
        comment=comment,
        tags=TagSet(dict(tags)),
        source_url=source_url,
        releases=list(releases),
        schemas=list(schemas),
        settings=list(settings),
        pragmas=list(pragmas),
    )
