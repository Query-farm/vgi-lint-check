"""VGI3xx — function and macro documentation.

VGI301/302 split by whether the function exposes parameters so exactly one fires
per undocumented function (using ``duckdb_functions()``, which has no per-argument
docs). Per-argument descriptions ARE exposed by the ``vgi`` extension's
``vgi_function_arguments()`` table function (newer extensions only) — VGI312 lints
those. Table-functions' result columns are documented separately (VGI307).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from ..findings import Category, Finding, Severity
from ..model import TAG_DOC_LLM, TAG_DOC_MD, TAG_RESULT_COLUMNS_MD, ObjectKind
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


@register
class TableFunctionColumnsDocumented(Rule):
    code = "VGI307"
    name = "table-function-columns-documented"
    category = FUNC
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE_FUNCTION,)
    summary = (
        "A table function with a dynamic schema (no backing table) must document "
        "its returned columns in a 'vgi.result_columns_md' tag."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        for f in cat.iter_all_functions():
            if f.kind is not ObjectKind.TABLE_FUNCTION:
                continue
            # A table function backed by a static table already has its columns
            # documented via that table's column comments.
            if cat.find_table_like(f.name, f.schema):
                continue
            if not f.tags.has(TAG_RESULT_COLUMNS_MD):
                yield self.finding(
                    ctx,
                    f.id,
                    f"table function has no documented return columns ('{TAG_RESULT_COLUMNS_MD}')",
                    "DuckDB can't expose a dynamic table-function schema — add a "
                    "'vgi.result_columns_md' tag with a Markdown table of the returned "
                    "columns (note any columns that vary by argument)",
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
# BLOB) are only flagged when they are the argument's OWN declared type.
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
    "decimal",
    "numeric",
    "varint",
    "uuid",
    "interval",
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
    targets = (
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "A function with a single multi-dimensional-array argument should take a table input."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for f in ctx.catalog.iter_all_functions():
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
_RANGE_CUES = re.compile(
    r"\b(between\s+-?\d|-?\d+\s*(?:to|-|–|—)\s*-?\d|at least\s+-?\d|at most\s+-?\d|"
    r"no (?:more|less) than\s+-?\d|must be (?:positive|non-negative|negative)|in the range)\b",
    re.IGNORECASE,
)


def _constraint_prose_kind(description: str) -> str | None:
    """``"enum"`` if the text enumerates a fixed vocabulary, ``"range"`` for a numeric range."""
    if _ENUM_CUES.search(description) or _QUOTED_LIST.search(description):
        return "enum"
    if _RANGE_CUES.search(description):
        return "range"
    return None


@register
class ConstrainedArgumentNotDiscoverable(Rule):
    code = "VGI317"
    name = "constrained-argument-not-discoverable"
    category = FUNC
    default_severity = Severity.INFO
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
