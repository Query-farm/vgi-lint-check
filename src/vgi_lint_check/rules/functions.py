"""VGI3xx — function and macro documentation.

VGI301/302 split by whether the function exposes parameters so exactly one fires
per undocumented function (using ``duckdb_functions()``, which has no per-argument
docs). Per-argument descriptions ARE exposed by the ``vgi`` extension's
``vgi_function_arguments()`` table function (newer extensions only) — VGI312 lints
those. Table-functions' result columns are documented separately (VGI307).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from ..findings import Category, Finding, Severity
from ..model import (
    TAG_DOC_LLM,
    TAG_DOC_MD,
    TAG_RESULT_COLUMNS_SCHEMA,
    TAG_RESULT_DYNAMIC_COLUMNS_MD,
    Function,
    ObjectId,
    ObjectKind,
)
from ..sql_parse import canonical_type
from ._util import base_type as _base_type
from ._util import blank, is_trivial_echo
from .base import Rule, RuleContext
from .registry import register

FUNC = Category.FUNCTIONS

# DuckDB's placeholder names for unnamed/positional parameters.
_UNNAMED_PARAM = re.compile(r"^(col)?\d+$", re.IGNORECASE)


def _is_unnamed(param: str) -> bool:
    return blank(param) or bool(_UNNAMED_PARAM.match(param.strip()))


@register
class FunctionDescription(Rule):
    code = "VGI301"
    name = "function-description"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.MACRO, ObjectKind.AGGREGATE)
    summary = "Functions/macros without parameters still need a description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_functions():
            if not f.parameters and blank(f.description) and blank(f.comment):
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} has no description",
                    "add a description (or COMMENT) explaining what it returns",
                )


@register
class FunctionParametersUndocumented(Rule):
    code = "VGI302"
    name = "function-parameters-undocumented"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.MACRO, ObjectKind.AGGREGATE)
    summary = "A function that takes parameters must describe what it does."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_functions():
            if f.parameters and blank(f.description) and blank(f.comment):
                params = ", ".join(f.parameters)
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} takes parameters ({params}) but has no description",
                    "add a description covering the parameters and the return value",
                )


@register
class FunctionDescriptionQuality(Rule):
    code = "VGI304"
    name = "function-description-quality"
    category = FUNC
    default_severity = Severity.INFO
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.MACRO, ObjectKind.AGGREGATE)
    summary = "A function description should be substantive, not a stub or echo."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        minlen = ctx.config.options.min_description_chars
        for f in ctx.catalog.iter_functions():
            desc = f.description or f.comment
            if blank(desc):
                continue  # presence handled by VGI301/302
            if is_trivial_echo(desc, f.name):
                yield self.finding(
                    ctx,
                    f.id,
                    f"description just restates the name ({f.name!r})",
                    "describe what the function does and returns",
                )
            elif len((desc or "").strip()) < minlen:
                yield self.finding(
                    ctx,
                    f.id,
                    f"description is very short ({len((desc or '').strip())} < {minlen} chars)",
                    "expand the description so consumers understand the function",
                )


@register
class FunctionArgumentsNamed(Rule):
    code = "VGI305"
    name = "function-arguments-named"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.MACRO,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "All function/macro arguments should be named, not positional."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_all_functions():
            unnamed = [p for p in f.parameters if _is_unnamed(p)]
            if unnamed:
                shown = ", ".join(p or "<empty>" for p in unnamed)
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} has unnamed/positional argument(s): {shown}",
                    "give every parameter a descriptive name",
                )


@register
class FunctionExample(Rule):
    code = "VGI306"
    name = "function-example"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE)
    summary = "Scalar/aggregate functions should ship an example query."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_functions():
            if f.kind in (ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE) and not f.examples:
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} function has no example query",
                    "add a 'vgi.example_queries' tag showing the function in use",
                )


def _iter_table_functions(ctx: RuleContext) -> Iterator[Function]:
    """Every table function in the catalog."""
    for f in ctx.catalog.iter_all_functions():
        if f.kind is ObjectKind.TABLE_FUNCTION:
            yield f


def _col_label(name: str | None) -> str:
    """A display name for a result column that may be unnamed."""
    return repr(name) if name else "(unnamed)"


@register
class TableFunctionColumnsDocumented(Rule):
    code = "VGI307"
    name = "table-function-result-schema-documented"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = (
        "A table function (with no backing table) must declare its result schema: "
        "'vgi.result_columns_schema' when static, or 'vgi.result_dynamic_columns_md' "
        "when it varies by argument — exactly one."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        for f in _iter_table_functions(ctx):
            # A table function backed by a static table already exposes its columns
            # (and their types) through that table — no tag required.
            if cat.find_table_like(f.name, f.schema):
                continue
            has_static = f.tags.has(TAG_RESULT_COLUMNS_SCHEMA)
            has_dynamic = f.tags.has(TAG_RESULT_DYNAMIC_COLUMNS_MD)
            if has_static and has_dynamic:
                yield self.finding(
                    ctx,
                    f.id,
                    "table function declares both a static ('vgi.result_columns_schema') "
                    "and a dynamic ('vgi.result_dynamic_columns_md') result schema",
                    "pick one: a fixed result shape uses the JSON schema tag; a shape that "
                    "varies by argument uses the dynamic Markdown tag",
                )
            elif not has_static and not has_dynamic:
                yield self.finding(
                    ctx,
                    f.id,
                    "table function does not declare its result schema",
                    "add 'vgi.result_columns_schema' (a JSON array of "
                    "{name, type, description}) for a fixed schema, or "
                    "'vgi.result_dynamic_columns_md' (one Name|Type|Description table per "
                    "variant) when the columns vary by argument",
                )


@register
class ResultSchemaValid(Rule):
    code = "VGI321"
    name = "result-schema-valid"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = "'vgi.result_columns_schema' must be a JSON array of {name, type, description}."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in _iter_table_functions(ctx):
            if f.result_columns_parse_error:
                yield self.finding(
                    ctx,
                    f.id,
                    f"vgi.result_columns_schema is not valid: {f.result_columns_parse_error}",
                    'use a JSON array of {"name", "type", "description"} objects',
                )
                continue
            for i, col in enumerate(f.result_columns):
                if not (col.name or "").strip():
                    yield self.finding(
                        ctx,
                        f.id,
                        f"vgi.result_columns_schema entry #{i} has no 'name'",
                        "every result column needs a name matching what the function returns",
                    )


@register
class ResultDynamicSchemaValid(Rule):
    code = "VGI326"
    name = "result-dynamic-schema-valid"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = (
        "'vgi.result_dynamic_columns_md' must contain one or more "
        "Name|Type|Description column tables (one per variant)."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in _iter_table_functions(ctx):
            if not f.tags.has(TAG_RESULT_DYNAMIC_COLUMNS_MD):
                continue
            if not f.result_dynamic_tables:
                yield self.finding(
                    ctx,
                    f.id,
                    "vgi.result_dynamic_columns_md contains no result-column table",
                    "describe how the schema varies, then give one Markdown table per "
                    "variant with a 'Name | Type | Description' header",
                )
                continue
            for table in f.result_dynamic_tables:
                where = f" (variant {table.caption!r})" if table.caption else ""
                if not table.columns:
                    yield self.finding(
                        ctx,
                        f.id,
                        f"vgi.result_dynamic_columns_md has a variant table with no columns{where}",
                        "give each variant table at least one Name|Type|Description row",
                    )
                for col in table.columns:
                    if not (col.name or "").strip():
                        yield self.finding(
                            ctx,
                            f.id,
                            f"vgi.result_dynamic_columns_md has a column with no name{where}",
                            "every row needs a column name in the first cell",
                        )


@register
class ResultColumnTypeValid(Rule):
    code = "VGI322"
    name = "result-column-type-valid"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = "Every declared result-column type must be a real DuckDB type."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in _iter_table_functions(ctx):
            for col in f.all_result_columns():
                declared = (col.type or "").strip()
                if not declared:
                    yield self.finding(
                        ctx,
                        f.id,
                        f"result column {_col_label(col.name)} has no declared type",
                        "give every result column a DuckDB type, e.g. VARCHAR / BIGINT / DOUBLE",
                    )
                    continue
                # (None, None) means undecidable offline (an unknown type name that
                # may be worker-defined) — defer to the live check, don't flag here.
                _canonical, err = canonical_type(declared)
                if err:
                    yield self.finding(
                        ctx,
                        f.id,
                        f"result column {_col_label(col.name)} declares an invalid type "
                        f"{declared!r}: {err}",
                        "use a real DuckDB type (e.g. VARCHAR, BIGINT, DOUBLE, DECIMAL(18,3))",
                    )


@register
class ResultColumnDescribed(Rule):
    code = "VGI323"
    name = "result-column-described"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = "Every declared result column should carry a non-blank description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in _iter_table_functions(ctx):
            for col in f.all_result_columns():
                if not col.documented:
                    yield self.finding(
                        ctx,
                        f.id,
                        f"result column {_col_label(col.name)} has no description",
                        "describe what the column holds so an agent knows how to use it",
                    )


@register
class ResultSchemaMatchesBackingTable(Rule):
    code = "VGI324"
    name = "result-schema-matches-backing-table"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = (
        "A declared static result schema must match the backing table's real columns "
        "(name and type)."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        for f in _iter_table_functions(ctx):
            if not f.result_columns:
                continue
            matches = cat.find_table_like(f.name, f.schema)
            if not matches:
                continue
            table = matches[0]
            actual = {c.name: c.data_type for c in table.columns}
            for col in f.result_columns:
                name = (col.name or "").strip()
                if not name:
                    continue  # nameless entries are VGI321's concern
                if name not in actual:
                    yield self.finding(
                        ctx,
                        f.id,
                        f"result column {name!r} is not a column of the backing table "
                        f"{table.name!r}",
                        "align the declared schema with the table the function returns "
                        "(fix the name or remove the column)",
                    )
                    continue
                declared_canon, derr = canonical_type(col.type or "")
                if derr or declared_canon is None:
                    continue  # invalid/undecidable type -> VGI322, can't compare here
                actual_canon, _ = canonical_type(actual[name] or "")
                if actual_canon is not None and declared_canon != actual_canon:
                    yield self.finding(
                        ctx,
                        f.id,
                        f"result column {name!r} is declared {declared_canon} but the backing "
                        f"table has {actual_canon}",
                        "fix the declared type to match the backing table",
                    )


@register
class FunctionArgumentsUndocumented(Rule):
    code = "VGI312"
    name = "function-arguments-undocumented"
    category = FUNC
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
        ObjectKind.MACRO,
    )
    summary = (
        "Every function argument must have a description. Needs a vgi extension "
        "new enough to expose vgi_function_arguments(); silent on older ones."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_all_functions():
            if not f.arguments:  # no per-arg data (older vgi extension) -> nothing to lint
                continue
            # Dedup by name so distinct overloads' shared args aren't double-listed.
            seen: set[str] = set()
            undocumented: list[str] = []
            for a in f.arguments:
                if blank(a.description) and a.name not in seen:
                    seen.add(a.name)
                    undocumented.append(self._label(a))
            if undocumented:
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} has undocumented argument(s): {', '.join(undocumented)}",
                    "add a per-argument description (vgi_doc) so callers and LLMs "
                    "know what each argument means and how it's used",
                )

    @staticmethod
    def _label(arg: Any) -> str:
        """Argument name annotated with notable kinds (const/varargs/any/table)."""
        flags = [
            k
            for k, on in (
                ("const", arg.is_const),
                ("varargs", arg.is_varargs),
                ("any-type", arg.is_any_type),
                ("table", arg.is_table_input),
            )
            if on
        ]
        return f"{arg.name} ({', '.join(flags)})" if flags else arg.name


# Type tokens unambiguous enough to flag in any argument description (no common
# English meaning). Ambiguous ones (DOUBLE/REAL/DATE/TIME/INT/MAP/LIST/CHAR/TEXT/
# BLOB, and DECIMAL/NUMERIC/INTERVAL — cf. "decimal degrees", "numeric keypad",
# "interval training") are only flagged when they are the argument's OWN declared
# type, so an ordinary English phrase isn't misread as restating a type.
_UNAMBIGUOUS_TYPES = (
    "varchar",
    "bigint",
    "smallint",
    "tinyint",
    "hugeint",
    "ubigint",
    "usmallint",
    "utinyint",
    "uhugeint",
    "uinteger",
    "integer",
    "boolean",
    "timestamp",
    "timestamptz",
    "varint",
    "uuid",
    "varbinary",
    "bitstring",
)


def _mentions_type(description: str, arg_type: str | None) -> str | None:
    """Return the type token a description restates, or None.

    Flags any unambiguous type token, plus the argument's own declared base type
    (so ``the double value`` is caught for a DOUBLE arg but ``double the value``
    is not for a non-DOUBLE arg).
    """
    text = description.lower()
    tokens = set(_UNAMBIGUOUS_TYPES)
    own = _base_type(arg_type)
    if own:
        tokens.add(own)
    for tok in tokens:
        if re.search(rf"\b{re.escape(tok)}\b", text):
            return tok.upper()
    return None


@register
class ArgumentDescriptionStatesType(Rule):
    code = "VGI313"
    name = "argument-description-states-type"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
        ObjectKind.MACRO,
    )
    summary = "An argument description should not restate the data type (it's a separate field)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        # Collapse the common case where one templated argument (same name, type,
        # and description text) is reused across many functions: report it once
        # with a recurrence count instead of N near-identical findings.
        seen: dict[tuple[str, str, str], list[Any]] = {}
        for f in ctx.catalog.iter_all_functions():
            local: set[str] = set()
            for a in f.arguments:
                if blank(a.description) or a.name in local:
                    continue
                local.add(a.name)
                tok = _mentions_type(a.description or "", a.type)
                if not tok:
                    continue
                key = (a.name, tok, " ".join((a.description or "").split()).lower())
                if key in seen:
                    seen[key][1] += 1
                else:
                    seen[key] = [f.id, 1]
        for (name, tok, _desc), (oid, count) in seen.items():
            extra = f" — the same description is reused on {count} functions" if count > 1 else ""
            yield self.finding(
                ctx,
                oid,
                f"argument {name!r} description mentions the data type ({tok}){extra}",
                "describe what the argument means, not its type — the type is "
                "exposed separately, so restating it adds noise and can drift",
            )


@register
class MacroExample(Rule):
    code = "VGI303"
    name = "macro-example"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.MACRO,)
    summary = "Macros should ship at least one example query showing usage."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for m in ctx.catalog.iter_macros():
            if not m.examples:
                yield self.finding(
                    ctx,
                    m.id,
                    "macro has no example query",
                    "add a 'vgi.example_queries' tag showing the macro in use",
                )


@register
class AllScalarFunctionsVolatile(Rule):
    code = "VGI308"
    name = "all-scalar-functions-volatile"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "Every scalar function being VOLATILE usually means stability was never set."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        # Only scalar functions that report a stability (macros/table-functions
        # report None). VOLATILE disables constant-folding/caching, so a worker
        # whose scalars are *all* volatile most likely left the default unset.
        scalars = [
            f
            for f in ctx.catalog.iter_functions()
            if f.kind is ObjectKind.SCALAR_FUNCTION and f.stability
        ]
        if len(scalars) >= 2 and all(f.is_volatile for f in scalars):
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"all {len(scalars)} scalar functions are VOLATILE",
                "set each deterministic scalar function's stability to CONSISTENT "
                "(only truly non-deterministic ones — random/now — should be "
                "VOLATILE); a blanket VOLATILE usually means it was never set",
            )


@register
class VolatileScalarFunction(Rule):
    code = "VGI309"
    name = "volatile-scalar-function"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE)
    summary = "Flag each VOLATILE scalar/aggregate function for a stability audit."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_functions():
            if f.kind not in (ObjectKind.SCALAR_FUNCTION, ObjectKind.AGGREGATE):
                continue
            if f.is_volatile:
                yield self.finding(
                    ctx,
                    f.id,
                    f"{f.function_type} function is VOLATILE",
                    "confirm this function is genuinely non-deterministic; if it "
                    "always returns the same output for the same input, mark it "
                    "CONSISTENT so the engine can optimize it",
                )


@register
class FunctionOverusesAny(Rule):
    code = "VGI310"
    name = "function-overuses-any"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "A function whose every parameter is typed ANY usually means types weren't declared."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_all_functions():
            types = [str(t).strip() for t in f.parameter_types if str(t).strip()]
            # 2+ params, all ANY: the "didn't bother typing anything" smell. A
            # single ANY arg is often a legitimately type-generic function.
            if len(types) >= 2 and all(t.upper() == "ANY" for t in types):
                yield self.finding(
                    ctx,
                    f.id,
                    f"all {len(types)} parameters are typed ANY",
                    "declare concrete parameter types where you can — ANY on every "
                    "argument disables type checking and weakens validation. Use ANY "
                    "only for genuinely type-generic arguments.",
                )


@register
class ParameterlessTableFunction(Rule):
    code = "VGI311"
    name = "parameterless-table-function"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = "A parameterless table function should usually be exposed as a regular table."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        for f in cat.iter_all_functions():
            if f.kind is not ObjectKind.TABLE_FUNCTION or f.parameters:
                continue
            # Already exposed as a table (a duckdb_tables() row of the same name
            # scans this function) -> nothing to nudge.
            if cat.find_table_like(f.name, f.schema):
                continue
            yield self.finding(
                ctx,
                f.id,
                "table function takes no arguments and is not exposed as a table",
                "a parameterless table function always returns the same rows — "
                "expose it as a regular table that scans this function, so consumers "
                "can use SELECT * FROM schema.name (no parentheses)",
            )


def _norm_prose(text: str | None) -> str:
    """Lowercase + whitespace-collapsed text for substring comparison."""
    return " ".join((text or "").lower().split())


# A "documented parameter" line: an argument name at the start of a line (after
# an optional bullet / bold marker) followed by a description separator — em/en
# dash or colon. Matches `- **period** — …`, `* start_time / … — …`, `period: …`.
_ARG_DOC_LINE_SEP = "—–:"


def _documents_arg_as_list_item(text: str, name: str) -> bool:
    """True when ``text`` documents ``name`` as its own parameter-list line."""
    pat = re.compile(
        rf"(?im)^\s*[-*+]?\s*\*{{0,2}}\s*{re.escape(name)}\b[^\n]*?[{_ARG_DOC_LINE_SEP}]"
    )
    return bool(pat.search(text))


@register
class FunctionRestatesArgumentDocs(Rule):
    code = "VGI314"
    name = "function-restates-argument-docs"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "A function's description shouldn't re-document its arguments (they'd drift)."

    # Only a substantive argument doc counts as a verbatim duplicate — a short
    # gloss like "the year" appearing in prose is coincidence, not restatement.
    _MIN_ARG_DOC = 20

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_all_functions():
            args = [a for a in f.arguments if not blank(a.description)]
            if not args:
                continue
            # Raw multiline docs (for the line-anchored parameter-list detection).
            doc_text = "\n".join(x for x in (f.tags.get(TAG_DOC_LLM), f.tags.get(TAG_DOC_MD)) if x)
            prose = _norm_prose(" ".join(x for x in (f.description, f.comment, doc_text) if x))
            if not prose:
                continue
            # (a) an argument's whole doc copied verbatim into the function prose.
            hits = {
                a.name
                for a in args
                if len(_norm_prose(a.description)) >= self._MIN_ARG_DOC
                and _norm_prose(a.description) in prose
            }
            # (b) a parameter-reference list: >=2 args each written as their own
            #     "name — …" / "name: …" doc line — a manual re-doc of the args.
            listed = {a.name for a in args if _documents_arg_as_list_item(doc_text, a.name)}
            if len(listed) >= 2:
                hits |= listed
            if hits:
                names = ", ".join(repr(n) for n in sorted(hits))
                yield self.finding(
                    ctx,
                    f.id,
                    f"function description re-documents its argument(s) {names}",
                    "describe what the function does and returns; each argument is already "
                    "documented via vgi_function_arguments() — don't repeat a parameter list "
                    "in the function doc, where it drifts out of sync with the arg docs",
                )


@register
class ArgumentTypeConsistent(Rule):
    code = "VGI315"
    name = "argument-type-consistent"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG,)
    summary = "An argument name should map to one SQL type across all functions (no type drift)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        ignore = {n.lower() for n in ctx.config.options.type_consistency_ignore_names}
        # arg name -> {base type: [qualified function names]}
        by_name: dict[str, dict[str, list[str]]] = {}
        for f in ctx.catalog.iter_all_functions():
            for a in f.arguments:
                if a.is_any_type or blank(a.type) or a.name.lower() in ignore:
                    continue
                bt = _base_type(a.type)
                if not bt:
                    continue
                fns = by_name.setdefault(a.name.lower(), {}).setdefault(bt, [])
                label = f"{f.schema}.{f.name}"
                if label not in fns:
                    fns.append(label)
        for name, types in by_name.items():
            distinct_fns = {fn for fns in types.values() for fn in fns}
            # Need >=2 distinct base types across >=2 distinct FUNCTIONS — overloads
            # of one function (e.g. accept a path or raw bytes) legitimately vary a type.
            if len(types) < 2 or len(distinct_fns) < 2:
                continue
            detail = "; ".join(
                f"{bt.upper()} ({', '.join(sorted(fns))})" for bt, fns in sorted(types.items())
            )
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"argument {name!r} is declared with {len(types)} different types: {detail}",
                "use one consistent type for the same argument concept across functions — "
                "differing types make the API harder to learn (add it to "
                "options.type_consistency_ignore_names if the collision is intentional)",
            )


_ARRAY_DIM = re.compile(r"\[\d*\]")


def _is_table_like_array(sql_type: str | None) -> bool:
    """True when a SQL type is a nested array / list-of-struct (a table-in-a-scalar)."""
    t = (sql_type or "").upper()
    depth = len(_ARRAY_DIM.findall(t))
    if depth >= 2:  # BIGINT[][], INTEGER[3][3] — a matrix / list of rows
        return True
    if "STRUCT" in t and depth >= 1:  # STRUCT(...)[] — a list of typed rows
        return True
    return bool(re.search(r"LIST\s*\(\s*(LIST|STRUCT)", t))


@register
class ArrayArgumentCouldBeTable(Rule):
    code = "VGI316"
    name = "array-argument-could-be-table"
    category = FUNC
    default_severity = Severity.WARNING
    # Table functions only. The remedy ("expose a table/relation input so callers
    # pass a subquery FROM …") is only structurally possible for a table function:
    # a scalar or aggregate function returns one value per row and cannot take a
    # relation as an argument, and DuckDB macros take scalar expressions, not
    # relations — flagging any of them is a false positive with no achievable fix.
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = (
        "A table function with a single multi-dimensional-array argument should take a table input."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in _iter_table_functions(ctx):
            array_args = [
                a
                for a in f.arguments
                if not a.is_any_type and not blank(a.type) and _is_table_like_array(a.type)
            ]
            # Only the single-input case: DuckDB takes one table argument, so a
            # multi-matrix function can't be converted and shouldn't be flagged.
            if len(array_args) != 1:
                continue
            a = array_args[0]
            yield self.finding(
                ctx,
                f.id,
                f"argument {a.name!r} is a multi-dimensional array ({a.type}) — a whole "
                "table passed as one parameter",
                "expose it as a table function that takes a table/relation input, so callers "
                f"pass a subquery (FROM …) instead of hand-building a nested-array literal for "
                f"{a.name!r}",
            )


# Prose cues that an argument has a fixed vocabulary (→ declare choices) or a
# numeric range (→ declare ge/le). Kept specific so the rule doesn't fire on
# descriptions that merely mention a value illustratively.
_ENUM_CUES = re.compile(
    r"\b(one of|(?:valid|allowed|permitted|supported|accepted)\s+values?|must be one of)\b",
    re.IGNORECASE,
)
_QUOTED_LIST = re.compile(r"""(?:'[^']+'|"[^"]+")\s*,\s*(?:'[^']+'|"[^"]+")""")
# A "value-like" token: a single word possibly carrying digits/units/punctuation
# (e.g. 1mo, cm, add, 90m, ytd) — but no internal spaces, so prose words qualify
# too; the surrounding structure (below) is what makes it an enumeration.
_TOKEN = r"[A-Za-z0-9][\w./%+-]*"
# A list of 3+ comma-separated tokens introduced by a colon, e.g.
# "candle width: 1m, 2m, 5m". A colon then a comma-list of short tokens is a
# strong enumeration signal that prose rarely produces.
_COLON_LIST = re.compile(rf":\s*{_TOKEN}(?:\s*,\s*{_TOKEN}){{2,}}")
# A comma-list of 3+ tokens closed with ", or X" (e.g. "1d, 5d, 1mo, … or max").
# Restricted to "or" (not "and") because "and" is the common prose conjunction,
# which would over-match ordinary sentences.
_OR_LIST = re.compile(rf"\b{_TOKEN}(?:\s*,\s*{_TOKEN})+\s*,?\s+or\s+{_TOKEN}\b", re.IGNORECASE)
# A numeric range. The bare hyphen is the tricky separator: "0-100" is a range,
# but "ISO-3166-1", "UTF-8" and "x86-64" are identifiers, not ranges. So the
# hyphen branch requires both operands to be full numbers not glued to a word
# char or another hyphen (lookarounds), while the unambiguous separators
# (to / en-dash / em-dash / "between" / "at least" …) don't need that guard.
_RANGE_CUES = re.compile(
    r"\bbetween\s+-?\d"
    r"|-?\d+\s*(?:to|through|–|—)\s*-?\d+"  # "1 to 10", "0–100"
    r"|(?<![\w-])\d+\s*-\s*\d+(?![\w-])"  # "0-100", but not ISO-3166-1
    r"|\bat least\s+-?\d|\bat most\s+-?\d"
    r"|\bno (?:more|less) than\s+-?\d"
    r"|\bmust be (?:positive|non-negative|negative)\b"
    r"|\bin the range\b",
    re.IGNORECASE,
)


def _constraint_prose_kind(description: str) -> str | None:
    """``"enum"`` if the text enumerates a fixed vocabulary, ``"range"`` for a numeric range."""
    if (
        _ENUM_CUES.search(description)
        or _QUOTED_LIST.search(description)
        or _COLON_LIST.search(description)
        or _OR_LIST.search(description)
    ):
        return "enum"
    if _RANGE_CUES.search(description):
        return "range"
    return None


@register
class ConstrainedArgumentNotDiscoverable(Rule):
    code = "VGI317"
    name = "constrained-argument-not-discoverable"
    category = FUNC
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
        ObjectKind.MACRO,
    )
    summary = (
        "An argument whose description enumerates allowed values or a numeric range should "
        "declare machine-readable constraints (choices / ge / le) so agents discover valid "
        "inputs. Needs a vgi extension exposing vgi_function_arguments(); silent on older ones."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        # Collapse a templated argument reused across many functions into one
        # finding with a recurrence count (mirrors VGI313).
        seen: dict[tuple[str, str, str], list[Any]] = {}
        for f in ctx.catalog.iter_all_functions():
            local: set[str] = set()
            for a in f.arguments:
                if blank(a.description) or a.name in local:
                    continue
                # Already machine-discoverable — the author declared it, nothing to nudge.
                if a.choices is not None or a.value_range is not None or a.pattern is not None:
                    continue
                local.add(a.name)
                kind = _constraint_prose_kind(a.description or "")
                if not kind:
                    continue
                key = (a.name, kind, " ".join((a.description or "").split()).lower())
                if key in seen:
                    seen[key][1] += 1
                else:
                    seen[key] = [f.id, 1]
        for (name, kind, _desc), (oid, count) in seen.items():
            extra = f" — the same description is reused on {count} functions" if count > 1 else ""
            states = "lists allowed values" if kind == "enum" else "states a numeric range"
            hint = (
                "declare choices=[…] on the argument"
                if kind == "enum"
                else "declare numeric bounds (ge/le) on the argument"
            )
            yield self.finding(
                ctx,
                oid,
                f"argument {name!r} description {states} but declares no "
                f"machine-readable constraint{extra}",
                f"{hint} so agents discover valid inputs via vgi_function_arguments() "
                "instead of learning them by trial-and-error",
            )


# ---------------------------------------------------------------------------
# Constraint-coherence rules (VGI318/319/320)
#
# Once a worker declares per-argument constraints (surfaced by
# vgi_function_arguments() as arg_default / arg_choices / arg_range /
# arg_pattern), these rules check the *declared metadata itself* is internally
# coherent — a deterministic, no-LLM complement to VGI317. All stay silent on
# older extensions that don't expose the columns (the fields are then None).
# ---------------------------------------------------------------------------

# Sentinel for "present but unparseable" so a malformed value isn't confused
# with a genuine JSON null.
_UNPARSED: Any = object()

# Interval notation as emitted by the framework's formatRange, e.g. "[0, 100]",
# "(0, +inf)", "[1, 10)", "(-inf, 5]".
_RANGE_NOTATION = re.compile(
    r"^([\[(])\s*(-inf|[-+]?[0-9][0-9.eE+-]*)\s*,\s*(\+inf|[-+]?[0-9][0-9.eE+-]*)\s*([\])])$"
)


def _decode_constraint(raw: str | None) -> Any:
    """Decode a JSON-encoded constraint value; ``_UNPARSED`` on malformed input."""
    if raw is None:
        return _UNPARSED
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return _UNPARSED


def _parse_range(notation: str) -> tuple[float | None, bool, float | None, bool] | None:
    """Parse interval notation into ``(low, low_inclusive, high, high_inclusive)``.

    ``low``/``high`` are None for an open (-inf/+inf) side. Returns None when the
    string isn't recognizable interval notation.
    """
    m = _RANGE_NOTATION.match(notation.strip())
    if not m:
        return None
    lb, lo_s, hi_s, rb = m.groups()
    try:
        low = None if lo_s == "-inf" else float(lo_s)
        high = None if hi_s == "+inf" else float(hi_s)
    except ValueError:
        return None
    return (low, lb == "[", high, rb == "]")


def _is_number(value: Any) -> bool:
    """True for a real numeric constraint value (bools are excluded)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _default_range_violation(default: Any, notation: str) -> str | None:
    """Return a message if ``default`` falls outside ``notation``, else None."""
    parsed = _parse_range(notation)
    if parsed is None or not _is_number(default):
        return None
    low, low_incl, high, high_incl = parsed
    d = float(default)
    if low is not None and (d < low or (d == low and not low_incl)):
        return f"default {default!r} is below the allowed range {notation}"
    if high is not None and (d > high or (d == high and not high_incl)):
        return f"default {default!r} is above the allowed range {notation}"
    return None


@register
class DefaultViolatesConstraint(Rule):
    code = "VGI318"
    name = "default-violates-constraint"
    category = FUNC
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
        ObjectKind.MACRO,
    )
    summary = (
        "An argument's declared default must satisfy its own constraints — be a member "
        "of choices, inside the numeric range, and match the pattern. Needs a vgi "
        "extension exposing vgi_function_arguments(); silent on older ones."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        seen: dict[tuple[str, str], list[Any]] = {}
        for f in ctx.catalog.iter_all_functions():
            local: set[str] = set()
            for a in f.arguments:
                if a.default is None or a.name in local:
                    continue
                default = _decode_constraint(a.default)
                if default is _UNPARSED or default is None:
                    continue  # no default / JSON null / unparseable — nothing to check
                local.add(a.name)
                for reason in self._violations(a, default):
                    key = (a.name, reason)
                    if key in seen:
                        seen[key][1] += 1
                    else:
                        seen[key] = [f.id, 1]
        for (name, reason), (oid, count) in seen.items():
            extra = f" (on {count} functions)" if count > 1 else ""
            yield self.finding(
                ctx,
                oid,
                f"argument {name!r}: {reason}{extra}",
                "fix the default so it satisfies the argument's own declared constraints "
                "— otherwise omitting the argument produces a value the function rejects",
            )

    @staticmethod
    def _violations(a: Any, default: Any) -> Iterator[str]:
        choices = _decode_constraint(a.choices)
        if isinstance(choices, list) and choices and default not in choices:
            yield f"default {default!r} is not one of the allowed values {choices}"
        if a.value_range is not None:
            msg = _default_range_violation(default, a.value_range)
            if msg:
                yield msg
        if a.pattern is not None and isinstance(default, str):
            try:
                if re.compile(a.pattern).search(default) is None:
                    yield f"default {default!r} does not match pattern {a.pattern!r}"
            except re.error:
                pass  # invalid regex is VGI319's concern, not this rule's


@register
class InvalidConstraint(Rule):
    code = "VGI319"
    name = "invalid-constraint"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
        ObjectKind.MACRO,
    )
    summary = (
        "A declared constraint must be well-formed: the pattern must be a valid regex "
        "and the numeric range must be non-empty. Needs a vgi extension exposing "
        "vgi_function_arguments()."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        seen: dict[tuple[str, str], list[Any]] = {}
        for f in ctx.catalog.iter_all_functions():
            local: set[str] = set()
            for a in f.arguments:
                if a.name in local:
                    continue
                reasons = list(self._problems(a))
                if reasons:
                    local.add(a.name)
                for reason in reasons:
                    key = (a.name, reason)
                    if key in seen:
                        seen[key][1] += 1
                    else:
                        seen[key] = [f.id, 1]
        for (name, reason), (oid, count) in seen.items():
            extra = f" (on {count} functions)" if count > 1 else ""
            yield self.finding(
                ctx,
                oid,
                f"argument {name!r}: {reason}{extra}",
                "correct the declared constraint — a malformed pattern or empty range "
                "can never be satisfied, so the metadata misleads callers and agents",
            )

    @staticmethod
    def _problems(a: Any) -> Iterator[str]:
        if a.pattern is not None:
            try:
                re.compile(a.pattern)
            except re.error as exc:
                yield f"pattern {a.pattern!r} is not a valid regex ({exc})"
        if a.value_range is not None:
            parsed = _parse_range(a.value_range)
            if parsed is not None:
                low, low_incl, high, high_incl = parsed
                if low is not None and high is not None:
                    empty = low > high or (low == high and not (low_incl and high_incl))
                    if empty:
                        yield f"range {a.value_range} is empty (no value can satisfy it)"


@register
class DegenerateChoices(Rule):
    code = "VGI320"
    name = "degenerate-choices"
    category = FUNC
    default_severity = Severity.INFO
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.TABLE_FUNCTION,
        ObjectKind.MACRO,
    )
    summary = (
        "A choices set should offer a real choice — 0 or 1 allowed value is pointless "
        "(drop it, or use a fixed value). Needs a vgi extension exposing "
        "vgi_function_arguments()."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        seen: dict[tuple[str, int], list[Any]] = {}
        for f in ctx.catalog.iter_all_functions():
            local: set[str] = set()
            for a in f.arguments:
                if a.choices is None or a.name in local:
                    continue
                choices = _decode_constraint(a.choices)
                if not isinstance(choices, list) or len(choices) >= 2:
                    continue
                local.add(a.name)
                key = (a.name, len(choices))
                if key in seen:
                    seen[key][1] += 1
                else:
                    seen[key] = [f.id, 1]
        for (name, n), (oid, count) in seen.items():
            extra = f" (on {count} functions)" if count > 1 else ""
            what = "an empty choices set" if n == 0 else "a single-value choices set"
            yield self.finding(
                ctx,
                oid,
                f"argument {name!r} declares {what}{extra}",
                "drop the choices constraint or give it two or more values — one option "
                "is not a choice and just adds noise to discovery",
            )


# A column set that identifies a view/table cataloguing the worker's own
# functions (the thing DuckDB's own duckdb_functions() already provides).
_FUNCTION_NAME_COLS = {"function_name", "function", "func_name"}
_FUNCTION_DESCRIPTOR_COLS = {
    "category",
    "kind",
    "function_kind",
    "summary",
    "signature",
    "returns",
    "purpose",
    "description",
}


def _is_function_catalog(name: str | None, cols: set[str]) -> bool:
    # A view/table literally named function_registry, or one whose columns are a
    # function catalogue: a function-name column plus two-plus function
    # descriptors (category / kind / summary / ...).
    if (name or "").lower() == "function_registry":
        return True
    has_name_col = bool(cols & _FUNCTION_NAME_COLS)
    return has_name_col and len(cols & _FUNCTION_DESCRIPTOR_COLS) >= 2


@register
class NoFunctionRegistry(Rule):
    code = "VGI327"
    name = "no-function-registry"
    category = FUNC
    default_severity = Severity.ERROR
    targets = (ObjectKind.VIEW, ObjectKind.TABLE, ObjectKind.TABLE_FUNCTION)
    summary = (
        "A view/table cataloguing the worker's own functions duplicates "
        "duckdb_functions(); drop it and query duckdb_functions() instead."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        candidates: list[tuple[ObjectId, str | None, set[str]]] = []
        for v in cat.iter_views():
            candidates.append((v.id, v.name, {(c.name or "").lower() for c in v.columns}))
        for t in cat.iter_tables():
            candidates.append((t.id, t.name, {(c.name or "").lower() for c in t.columns}))
        for f in cat.iter_all_functions():
            if f.kind is ObjectKind.TABLE_FUNCTION:
                cols = {(c.name or "").lower() for c in f.all_result_columns()}
                candidates.append((f.id, f.name, cols))
        for obj_id, name, cols in candidates:
            if _is_function_catalog(name, cols):
                yield self.finding(
                    ctx,
                    obj_id,
                    f"{name!r} re-catalogues this worker's own functions, "
                    "duplicating duckdb_functions()",
                    "remove it and query duckdb_functions() (filtered to this catalog) "
                    "instead — it is always accurate, whereas a hand-maintained registry "
                    "drifts as functions change. Put the category/summary on each "
                    "function's vgi.category / vgi.doc_md tags, which the platform already "
                    "surfaces for discovery.",
                )
