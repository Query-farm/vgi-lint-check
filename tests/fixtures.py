"""Hand-built catalog fixtures for offline rule tests."""

from __future__ import annotations

from vgi_lint_check.model import (
    Argument,
    AttachOption,
    Catalog,
    Column,
    Constraint,
    ExampleQuery,
    ExampleStatement,
    ExecutableExample,
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


def exec_example(index, description, statements, *, name=None):
    """Build an ExecutableExample.

    ``statements`` is a list of (description, sql) or (description, sql, expected)
    tuples — a 3-tuple sets that statement's expected_result.
    """
    stmts = []
    for step in statements:
        if len(step) == 3:
            d, q, exp = step
            stmts.append(
                ExampleStatement(description=d, sql=q, expected_result=exp, has_expected=True)
            )
        else:
            d, q = step
            stmts.append(ExampleStatement(description=d, sql=q))
    return ExecutableExample(index=index, name=name, description=description, statements=stmts)


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
    stability=None,
    executable_examples=(),
    exec_parse_error=None,
    arguments=(),
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
        stability=stability,
        executable_examples=list(executable_examples),
        executable_examples_parse_error=exec_parse_error,
        arguments=list(arguments),
    )


def arg(name, type="VARCHAR", description=None, **flags):
    return Argument(name=name, type=type, description=description, **flags)


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


def attach_option(name, description=None, type="VARCHAR", default="x"):
    return AttachOption(
        id=ObjectId("v", ObjectKind.ATTACH_OPTION, name=name),
        name=name,
        description=description,
        type=type,
        default=default,
    )


def catalog(
    *schemas,
    settings=(),
    pragmas=(),
    comment="A test catalog of things.",
    tags=None,
    source_url="https://example.com",
    releases=(),
    attach_options=(),
    advertised_catalogs=None,
):
    # Default catalog metadata satisfies the VGI0xx required rules so per-object
    # rule tests aren't polluted; pass comment=None / tags={} to test VGI00x.
    if tags is None:
        tags = {
            # Long enough to satisfy VGI103 (catalog min 300 chars).
            "vgi.doc_llm": (
                "A comprehensive test catalog used by the unit tests to exercise "
                "the rule engine. It covers several schemas of animals, attributes, "
                "and the sounds they make, and documents how an agent should query "
                "them, when each object is useful, and how the pieces relate. "
            )
            * 2,
            "vgi.doc_md": (
                "## Test catalog\n\nA detailed Markdown overview used by the unit "
                "tests, describing the catalog's schemas, tables, functions, and how "
                "to use them in practice with worked examples and guidance. "
            )
            * 3,
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
        attach_options=list(attach_options),
        # A real worker always advertises >=1 catalog; default to one so VGI012
        # doesn't pollute unrelated tests. Pass [] explicitly to test the gap.
        advertised_catalogs=["v"] if advertised_catalogs is None else list(advertised_catalogs),
    )
