"""Compile and execute the VGI single-fact semantic-query contract.

This is the Python reference implementation used by ``vgi-lint simulate`` and
the committed end-to-end fixtures.  It deliberately implements the same
conservative boundary as Cupola: one measure-owning root, to-one enrichment,
typed expressions, positional parameters, and no silent SQL fallback.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast

from .model import TAG_REQUIRED_FILTERS, Argument, Catalog, Function, Table
from .semantic_federation import (
    FederatedRelationship,
    FederatedSemanticModel,
    build_federated_semantic_model,
)
from .semantic_model import (
    SemanticEntity,
    _resolve_nested_type,
    build_semantic_model,
    schema_diagnostics,
)
from .semantic_schema import validate_instance

DiagnosticStage = Literal[
    "request_validation",
    "model_resolution",
    "multi_fact_not_supported",
    "catalog_binding",
    "relationship_resolution",
    "source_binding",
    "unit_resolution",
    "execution_limit",
    "type_check",
    "fanout",
    "required_filter",
    "sql_generation",
    "duckdb_execution",
]

_MAX_INLINE_ROWS = 100
_MAX_INLINE_COLUMNS = 32
_MAX_INLINE_CELLS = 3_200
_MAX_INLINE_BYTES = 1_000_000
_DEFAULT_MAX_INVOCATIONS = 100
_HARD_MAX_INVOCATIONS = 1_000
_DEFAULT_MAX_STAGE_ROWS = 10_000

_SAFE_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(?:\([0-9]+(?:,[0-9]+)?\))?(?:\[\])?$")
_BINARY_OPERATORS = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}
_FILTER_OPERATORS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_MISSING = object()

_SourceArgumentRenderer = Callable[[SemanticEntity, dict[str, Any]], str]


@dataclass(frozen=True)
class _Resolved:
    entity: SemanticEntity
    member: dict[str, Any]
    alias: str
    selection: dict[str, Any]


@dataclass(frozen=True)
class _Edge:
    relationship: FederatedRelationship
    source: SemanticEntity
    target: SemanticEntity
    forward: bool


@dataclass(frozen=True)
class _InvocationBinding:
    entity: SemanticEntity
    driver_entity: SemanticEntity | None
    input_id: str | None
    definition: dict[str, Any]


class _CompileFailure(Exception):
    def __init__(
        self,
        stage: DiagnosticStage,
        code: str,
        message: str,
        *,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic: dict[str, Any] = {"stage": stage, "code": code, "message": message}
        if path is not None:
            self.diagnostic["path"] = path
        if details is not None:
            self.diagnostic["details"] = details


def _fail(stage: DiagnosticStage, code: str, message: str) -> NoReturn:
    raise _CompileFailure(stage, code, message)


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _column_sql(alias: str, value: str) -> str:
    """Render one physical column name, including names that contain dots."""
    return f"{alias}.{_quote_ident(value)}"


def _member_column_path(member: dict[str, Any]) -> list[str]:
    path = member.get("column_path")
    if isinstance(path, list):
        return [str(segment) for segment in path]
    column = member.get("column")
    return [str(column)] if column else []


def _member_column_sql(alias: str, member: dict[str, Any]) -> str:
    path = _member_column_path(member)
    if not path:
        raise ValueError("member is not column-backed")
    return ".".join([alias, *(_quote_ident(segment) for segment in path)])


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ref_key(value: dict[str, Any]) -> tuple[str, str]:
    return str(value.get("catalog_id", "")), str(value.get("entity_id", ""))


def _entity_marker(entity: SemanticEntity) -> str:
    return f"{entity.host.database}:{entity.catalog_id}::{entity.entity_id}"


def _unique_entities(values: list[SemanticEntity]) -> list[SemanticEntity]:
    seen: set[str] = set()
    result: list[SemanticEntity] = []
    for entity in values:
        marker = _entity_marker(entity)
        if marker not in seen:
            seen.add(marker)
            result.append(entity)
    return result


def _safe_type(value: str) -> str:
    if not _SAFE_TYPE.fullmatch(value):
        _fail("type_check", "invalid_output_type", f"Unsafe or unsupported DuckDB type {value!r}")
    return value


def _literal_sql(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            _fail("type_check", "invalid_number", "Expression literals must be finite numbers")
        return str(value)
    if isinstance(value, str):
        return _quote_literal(value)
    _fail("type_check", "invalid_literal", "Expression literals must be scalar JSON values")
    raise AssertionError("unreachable")


def _source_arguments(entity: SemanticEntity) -> list[dict[str, Any]]:
    source = entity.definition.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("arguments"), list):
        return []
    return [cast(dict[str, Any], item) for item in source["arguments"] if isinstance(item, dict)]


def _resolved_source_arguments(
    entity: SemanticEntity,
) -> list[tuple[dict[str, Any], Argument]]:
    """Resolve semantic mappings to one unambiguous physical signature."""
    mappings = _source_arguments(entity)
    if not mappings:
        return []
    if not entity.function_arguments:
        _fail(
            "model_resolution",
            "missing_function_argument_metadata",
            f"Table function {entity.entity_id!r} requires vgi_function_arguments() metadata",
        )
    field_indexes = [
        argument.field_index
        for argument in entity.function_arguments
        if argument.field_index is not None
    ]
    if entity.function_overload_count > 1 or len(field_indexes) != len(set(field_indexes)):
        _fail(
            "model_resolution",
            "ambiguous_function_overload",
            f"Table function {entity.entity_id!r} has ambiguous overload metadata",
        )
    if any(argument.is_varargs for argument in entity.function_arguments):
        _fail(
            "model_resolution",
            "unsupported_function_varargs",
            f"Table function {entity.entity_id!r} uses unsupported varargs",
        )
    if any(argument.is_table_input for argument in entity.function_arguments):
        _fail(
            "model_resolution",
            "unsupported_table_input",
            f"Table function {entity.entity_id!r} requires an unsupported table input",
        )
    by_name: dict[str, list[Argument]] = {}
    for argument in entity.function_arguments:
        by_name.setdefault(argument.name, []).append(argument)
    resolved: list[tuple[dict[str, Any], Argument]] = []
    for mapping in mappings:
        name = str(mapping.get("argument", ""))
        matches = by_name.get(name, [])
        if not matches:
            _fail(
                "model_resolution",
                "unknown_source_argument",
                f"Source mapping references unknown function argument {name!r}",
            )
        if len(matches) > 1:
            _fail(
                "model_resolution",
                "ambiguous_source_argument",
                f"Source argument {name!r} resolves to more than one overload",
            )
        argument = matches[0]
        if argument.is_named == argument.is_positional:
            _fail(
                "model_resolution",
                "invalid_source_argument_kind",
                f"Source argument {name!r} is not unambiguously named or positional",
            )
        if argument.is_positional and argument.position is None:
            _fail(
                "model_resolution",
                "missing_source_argument_position",
                f"Positional source argument {name!r} has no arg_position",
            )
        resolved.append((mapping, argument))
    return resolved


def _source_sql(entity: SemanticEntity, query: dict[str, Any], parameters: list[Any]) -> str:
    qualified = ".".join(
        (
            _quote_ident(entity.host.database),
            _quote_ident(entity.host.schema or "main"),
            _quote_ident(entity.host.name or ""),
        )
    )
    if entity.source_kind == "relation":
        return qualified
    supplied: dict[str, Any] = (
        query["parameters"] if isinstance(query.get("parameters"), dict) else {}
    )
    bindings = _resolved_source_arguments(entity)
    positional = sorted(
        (item for item in bindings if item[1].is_positional),
        key=lambda item: cast(int, item[1].position),
    )
    named = sorted(
        (item for item in bindings if item[1].is_named),
        key=lambda item: (
            item[1].field_index is None,
            item[1].field_index if item[1].field_index is not None else 0,
            item[1].name,
        ),
    )
    supplied_positions = {
        cast(int, argument.position)
        for mapping, argument in positional
        if str(mapping.get("parameter", "")) in supplied
    }
    if supplied_positions:
        highest = max(supplied_positions)
        physical_by_position = {
            argument.position: argument
            for argument in entity.function_arguments
            if argument.is_positional and argument.position is not None
        }
        missing = [
            physical_by_position[position].name
            for position in sorted(physical_by_position)
            if position < highest and position not in supplied_positions
        ]
        if missing:
            _fail(
                "model_resolution",
                "optional_positional_hole",
                "Cannot supply a later positional table-function argument while omitting "
                f"earlier arguments {missing!r}",
            )
    args: list[str] = []
    for mapping, argument in [*positional, *named]:
        parameter = str(mapping.get("parameter", ""))
        if parameter not in supplied:
            if mapping.get("required", True) is not False:
                _fail(
                    "required_filter",
                    "missing_source_parameter",
                    f"Table function argument {argument.name!r} requires semantic "
                    f"parameter {parameter!r}",
                )
            continue
        if not _value_compatible(supplied[parameter], argument.type):
            _fail(
                "type_check",
                "incompatible_parameter_type",
                f"Parameter {parameter!r} is incompatible with argument {argument.name!r}",
            )
        parameters.append(supplied[parameter])
        args.append(f"{_quote_ident(argument.name)} := ?" if argument.is_named else "?")
    return f"{qualified}({', '.join(args)})"


def _qualified_source(entity: SemanticEntity) -> str:
    return ".".join(
        (
            _quote_ident(entity.host.database),
            _quote_ident(entity.host.schema or "main"),
            _quote_ident(entity.host.name or ""),
        )
    )


def _path_alias(base: str, path: tuple[str, ...]) -> str:
    return base + "".join(f".{_quote_ident(part)}" for part in path)


def _normalized_type(value: str | None) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip().upper())
    aliases = {
        "STRING": "VARCHAR",
        "TEXT": "VARCHAR",
        "INT": "INTEGER",
        "INT4": "INTEGER",
        "INT8": "BIGINT",
        "FLOAT": "REAL",
        "FLOAT8": "DOUBLE",
        "BOOL": "BOOLEAN",
    }
    return aliases.get(raw, raw)


def _types_compatible(source: str | None, target: str | None) -> bool:
    left, right = _normalized_type(source), _normalized_type(target)
    if not left or not right or right == "ANY":
        return True
    if left == right:
        return True
    numeric = ["TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "REAL", "DOUBLE"]
    return left in numeric and right in numeric and numeric.index(left) <= numeric.index(right)


def _value_compatible(value: Any, target: str | None) -> bool:
    """Conservatively check JSON scalar/container shape against a DuckDB type."""
    if value is None or not target:
        return True
    normalized = _normalized_type(target)
    if normalized == "ANY":
        return True
    if normalized == "BOOLEAN":
        return isinstance(value, bool)
    if normalized.startswith(("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT")):
        return isinstance(value, int) and not isinstance(value, bool)
    if normalized.startswith(("REAL", "DOUBLE", "DECIMAL", "NUMERIC")):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if normalized.startswith(("VARCHAR", "CHAR", "TEXT", "DATE", "TIME", "TIMESTAMP", "UUID")):
        return isinstance(value, str)
    if normalized.endswith("[]"):
        return isinstance(value, list)
    if normalized.startswith(("STRUCT", "MAP", "JSON")):
        return isinstance(value, (dict, list, str))
    return True


def _member_type(entity: SemanticEntity, member: dict[str, Any]) -> str | None:
    explicit = member.get("output_type") or member.get("data_type")
    if explicit:
        return str(explicit)
    path = _member_column_path(member)
    if not path:
        return None
    physical_type = entity.physical_column_types.get(path[0])
    if len(path) == 1 or physical_type is None:
        return physical_type
    return _resolve_nested_type(physical_type, path[1:])


def _member_unit_definition(
    entity: SemanticEntity,
    member: dict[str, Any],
    visited: frozenset[str] = frozenset(),
) -> tuple[str, Any] | None:
    """Return a declared unit, conservatively inheriting through safe aggregations."""
    member_id = str(member.get("member_id", ""))
    if member_id in visited:
        return None
    if "unit" in member:
        return "static", member["unit"]
    if "unit_parameter" in member:
        return "dynamic", member["unit_parameter"]
    if (
        member.get("kind") == "measure"
        and member.get("aggregation") in {"sum", "min", "max", "avg"}
        and member.get("member")
    ):
        source = entity.members.get(str(member["member"]))
        if source is not None:
            return _member_unit_definition(entity, source, visited | {member_id})
    return None


def _member_uses_source_argument(
    entity: SemanticEntity,
    member: dict[str, Any],
    visited: frozenset[str] = frozenset(),
) -> bool:
    member_id = str(member.get("member_id", ""))
    if member_id in visited:
        return False
    if member.get("source_argument") is not None:
        return True

    def expression_refs(value: Any) -> set[str]:
        if isinstance(value, dict):
            refs = {str(value["member"])} if value.get("op") == "member" else set()
            for child in value.values():
                refs.update(expression_refs(child))
            return refs
        if isinstance(value, list):
            return {ref for child in value for ref in expression_refs(child)}
        return set()

    return any(
        _member_uses_source_argument(entity, entity.members[ref], visited | {member_id})
        for ref in expression_refs(member.get("expression"))
        if ref in entity.members
    )


def _source_binding_argument(
    entity: SemanticEntity, query: dict[str, Any], argument_name: str
) -> dict[str, Any] | None:
    matches = [
        binding
        for binding in query.get("source_bindings", [])
        if _ref_key(binding.get("entity", {})) == (entity.catalog_id, entity.entity_id)
    ]
    if not matches:
        return None
    value = matches[0].get("arguments", {}).get(argument_name)
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _source_binding_definition(
    entity: SemanticEntity, query: dict[str, Any]
) -> dict[str, Any] | None:
    matches = [
        binding
        for binding in query.get("source_bindings", [])
        if _ref_key(binding.get("entity", {})) == (entity.catalog_id, entity.entity_id)
    ]
    return cast(dict[str, Any], matches[0]) if len(matches) == 1 else None


def _decoded_argument_default(argument: Argument) -> Any:
    if argument.default is None:
        return _MISSING
    try:
        return json.loads(argument.default)
    except (TypeError, ValueError):
        _fail(
            "unit_resolution",
            "unit_parameter_default_invalid",
            f"Function argument {argument.name!r} has an invalid JSON default",
        )


def _unit_value_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _resolve_output_unit(
    entity: SemanticEntity,
    member: dict[str, Any],
    output_name: str,
    query: dict[str, Any],
) -> tuple[bool, str | None, dict[str, Any] | None]:
    definition = _member_unit_definition(entity, member)
    if definition is None:
        return False, None, None
    kind, value = definition
    if kind == "static":
        return True, str(value), None
    unit_parameter = cast(dict[str, Any], value)
    argument_name = str(unit_parameter["argument"])
    arguments = [item for item in entity.function_arguments if item.name == argument_name]
    if len(arguments) != 1:
        _fail(
            "unit_resolution",
            "unit_parameter_argument_unavailable",
            f"Cannot resolve unit argument {argument_name!r} for output {output_name!r}",
        )
    mapping = next(
        (item for item in _source_arguments(entity) if item.get("argument") == argument_name),
        None,
    )
    if mapping is None:
        _fail(
            "unit_resolution",
            "unit_parameter_source_unmapped",
            f"Unit argument {argument_name!r} is not exposed by the semantic source",
        )
    binding = _source_binding_argument(entity, query, argument_name)
    parameter_name = str((binding or mapping).get("parameter", ""))
    supplied = query.get("parameters", {}) if isinstance(query.get("parameters"), dict) else {}
    effective: Any = supplied.get(parameter_name, _MISSING) if parameter_name else _MISSING
    correlated_binding = binding is not None and ("input_column" in binding or "member" in binding)
    if correlated_binding:
        effective = _MISSING
    if effective is _MISSING and not correlated_binding:
        effective = _decoded_argument_default(arguments[0])
    path = f"{entity.catalog_id}::{entity.entity_id}::{member.get('member_id')}"
    if effective is _MISSING:
        return (
            True,
            None,
            {
                "stage": "unit_resolution",
                "code": "unit_parameter_value_unresolved",
                "message": f"Unit for output {output_name!r} depends on argument "
                f"{argument_name!r}, whose effective value is unavailable",
                "path": path,
            },
        )
    key = _unit_value_key(effective)
    values = cast(dict[str, str], unit_parameter["values"])
    if key not in values:
        _fail(
            "unit_resolution",
            "unit_parameter_value_unmapped",
            f"Unit argument {argument_name!r} has unmapped effective value {key!r}",
        )
    return True, values[key], None


def _validate_inputs(query: dict[str, Any]) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}
    total_cells = 0
    total_bytes = 0
    for item in query.get("inputs", []):
        input_id = str(item.get("input_id", ""))
        if input_id in inputs:
            _fail("source_binding", "duplicate_input", f"Duplicate input_id {input_id!r}")
        columns = list(item.get("columns", []))
        rows = list(item.get("rows", []))
        if len(columns) > _MAX_INLINE_COLUMNS or len(rows) > _MAX_INLINE_ROWS:
            _fail(
                "execution_limit", "inline_input_limit", f"Input {input_id!r} exceeds inline limits"
            )
        names = [str(column.get("name", "")) for column in columns]
        if len(names) != len(set(names)):
            _fail(
                "source_binding",
                "duplicate_input_column",
                f"Input {input_id!r} has duplicate columns",
            )
        grain = [str(value) for value in item.get("grain", [])]
        missing_grain = sorted(set(grain) - set(names))
        if missing_grain:
            _fail(
                "source_binding",
                "unknown_input_grain",
                f"Input {input_id!r} grain references {missing_grain!r}",
            )
        indexes = [names.index(name) for name in grain]
        grain_values: set[tuple[Any, ...]] = set()
        for row_index, row in enumerate(rows):
            if len(row) != len(columns):
                _fail(
                    "source_binding",
                    "input_row_width",
                    f"Input {input_id!r} row {row_index} has the wrong width",
                )
            for index, value in enumerate(row):
                if value is None and columns[index].get("nullable", False) is not True:
                    _fail(
                        "type_check",
                        "null_input_value",
                        f"Input {input_id!r} column {names[index]!r} is not nullable",
                    )
                if value is not None and not _value_compatible(
                    value, str(columns[index].get("type", ""))
                ):
                    _fail(
                        "type_check",
                        "incompatible_input_value",
                        f"Input {input_id!r} row {row_index} column {names[index]!r} "
                        f"does not match {columns[index].get('type')!r}",
                    )
            key = tuple(row[index] for index in indexes)
            if any(value is None for value in key):
                _fail(
                    "source_binding",
                    "null_input_grain",
                    f"Input {input_id!r} grain cannot contain NULL",
                )
            if key in grain_values:
                _fail(
                    "source_binding",
                    "duplicate_input_grain",
                    f"Input {input_id!r} grain is not unique",
                )
            grain_values.add(key)
        total_cells += len(columns) * len(rows)
        total_bytes += len(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
        inputs[input_id] = item
    if total_cells > _MAX_INLINE_CELLS or total_bytes > _MAX_INLINE_BYTES:
        _fail(
            "execution_limit",
            "inline_input_payload_limit",
            "Inline inputs exceed the request payload limit",
        )
    return inputs


def _resolve_invocation_chain(
    graph: FederatedSemanticModel,
    identities: dict[str, dict[str, Any]],
    root: SemanticEntity,
    query: dict[str, Any],
    bindings: dict[str, str],
    inputs: dict[str, dict[str, Any]],
) -> list[_InvocationBinding]:
    by_target: dict[str, _InvocationBinding] = {}
    for definition in query.get("source_bindings", []):
        entity = _resolve_entity(graph, identities, definition["entity"], bindings)
        marker = _entity_marker(entity)
        if marker in by_target:
            _fail(
                "source_binding",
                "duplicate_source_binding",
                f"Entity {entity.entity_id!r} has multiple source bindings",
            )
        if entity.source_kind != "table_function":
            _fail(
                "source_binding",
                "binding_target_not_function",
                f"Entity {entity.entity_id!r} is not a table function",
            )
        driver = definition["driver"]
        driver_entity: SemanticEntity | None = None
        input_id: str | None = None
        if "input_id" in driver:
            input_id = str(driver["input_id"])
            if input_id not in inputs:
                _fail("source_binding", "unknown_input", f"Unknown input_id {input_id!r}")
        else:
            driver_entity = _resolve_entity(graph, identities, driver["entity"], bindings)
        by_target[marker] = _InvocationBinding(entity, driver_entity, input_id, definition)

    chain: list[_InvocationBinding] = []
    visiting: set[str] = set()
    used: set[str] = set()

    def visit(entity: SemanticEntity) -> None:
        marker = _entity_marker(entity)
        binding = by_target.get(marker)
        if binding is None:
            return
        if marker in visiting:
            _fail(
                "source_binding",
                "correlation_cycle",
                f"Correlation cycle includes {entity.entity_id!r}",
            )
        visiting.add(marker)
        if binding.driver_entity is not None:
            visit(binding.driver_entity)
        visiting.remove(marker)
        used.add(marker)
        chain.append(binding)

    visit(root)
    unused = sorted(set(by_target) - used)
    if unused:
        _fail(
            "source_binding",
            "unused_source_binding",
            f"Source bindings are not on the root invocation path: {unused!r}",
        )
    used_inputs = {item.input_id for item in chain if item.input_id is not None}
    unused_inputs = sorted(set(inputs) - used_inputs)
    if unused_inputs:
        _fail(
            "source_binding",
            "unused_input",
            f"Inputs are not used by the root invocation path: {unused_inputs!r}",
        )
    if len(used_inputs) > 1:
        _fail(
            "source_binding",
            "multiple_driving_inputs",
            "One invocation path may use only one inline input",
        )
    return chain


def _correlated_call_sql(
    binding: _InvocationBinding,
    query: dict[str, Any],
    parameters: list[Any],
    driver_alias: str,
    driver_paths: dict[str, tuple[str, ...]],
    input_columns: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    entity = binding.entity
    resolved = _resolved_source_arguments(entity)
    mappings = {str(mapping.get("argument", "")): mapping for mapping, _argument in resolved}
    arguments = {argument.name: argument for argument in entity.function_arguments}
    overrides = cast(dict[str, dict[str, Any]], binding.definition.get("arguments", {}))
    unknown = sorted(set(overrides) - set(arguments))
    if unknown:
        _fail(
            "source_binding",
            "unknown_bound_argument",
            f"Bindings reference unknown arguments {unknown!r}",
        )
    correlated = False
    rendered: dict[str, tuple[str, dict[str, Any], Any]] = {}
    supplied = query.get("parameters", {}) if isinstance(query.get("parameters"), dict) else {}
    for name, argument in arguments.items():
        override = overrides.get(name)
        if override is not None and ("input_column" in override or "member" in override):
            correlated = True
            if entity.input_from_args is None:
                _fail(
                    "source_binding",
                    "correlated_input_capability_unknown",
                    f"Function {entity.entity_id!r} was loaded without input_from_args "
                    "capability metadata; upgrade the VGI extension/runtime",
                )
            if entity.input_from_args is False:
                _fail(
                    "source_binding",
                    "correlated_input_not_supported",
                    f"Function {entity.entity_id!r} does not advertise input_from_args",
                )
            if not argument.is_positional or argument.position is None or argument.is_const:
                _fail(
                    "source_binding",
                    "invalid_correlated_argument",
                    f"Argument {name!r} cannot be column-bound",
                )
            if "input_column" in override:
                if binding.input_id is None:
                    _fail(
                        "source_binding",
                        "input_binding_wrong_driver",
                        f"Argument {name!r} requires an inline-input driver",
                    )
                column_name = str(override["input_column"])
                column = input_columns.get(column_name)
                if column is None:
                    _fail(
                        "source_binding",
                        "unknown_input_column",
                        f"Unknown input column {column_name!r}",
                    )
                if not _types_compatible(str(column.get("type", "")), argument.type):
                    _fail(
                        "type_check",
                        "incompatible_argument_type",
                        f"Input column {column_name!r} is incompatible with argument {name!r}",
                    )
                sql = f"{driver_alias}.{_quote_ident(column_name)}"
                detail: dict[str, Any] = {
                    "argument": name,
                    "kind": "input_column",
                    "source": column_name,
                }
            else:
                if binding.driver_entity is None:
                    _fail(
                        "source_binding",
                        "member_binding_wrong_driver",
                        f"Argument {name!r} requires an entity driver",
                    )
                ref = cast(dict[str, Any], override["member"])
                if _ref_key(ref) != (
                    binding.driver_entity.catalog_id,
                    binding.driver_entity.entity_id,
                ):
                    _fail(
                        "source_binding",
                        "member_not_on_driver",
                        f"Argument {name!r} references a member outside its driver",
                    )
                member_id = str(ref.get("member_id", ""))
                member = binding.driver_entity.members.get(member_id)
                if member is None:
                    _fail(
                        "source_binding",
                        "unknown_driver_member",
                        f"Unknown driver member {member_id!r}",
                    )
                if not _member_column_path(member):
                    _fail(
                        "source_binding",
                        "derived_driver_member",
                        f"Driver member {member_id!r} must be column-backed",
                    )
                if not _types_compatible(
                    _member_type(binding.driver_entity, member), argument.type
                ):
                    _fail(
                        "type_check",
                        "incompatible_argument_type",
                        f"Driver member {member_id!r} is incompatible with argument {name!r}",
                    )
                marker = _entity_marker(binding.driver_entity)
                sql = _member_sql(
                    binding.driver_entity, member, _path_alias(driver_alias, driver_paths[marker])
                )
                detail = {"argument": name, "kind": "member", "source": ref}
            rendered[name] = (sql, detail, None)
            continue

        parameter_name = str((override or mappings.get(name) or {}).get("parameter", ""))
        required = (mappings.get(name) or {}).get("required", True) is not False
        if parameter_name and parameter_name in supplied:
            if not _value_compatible(supplied[parameter_name], argument.type):
                _fail(
                    "type_check",
                    "incompatible_parameter_type",
                    f"Parameter {parameter_name!r} is incompatible with argument {name!r}",
                )
            rendered[name] = (
                "?",
                {"argument": name, "kind": "parameter", "source": parameter_name},
                supplied[parameter_name],
            )
        elif argument.default is None and required:
            _fail(
                "required_filter",
                "missing_source_parameter",
                f"Table function argument {name!r} requires semantic parameter {parameter_name!r}",
            )

    if not correlated:
        _fail(
            "source_binding",
            "missing_correlated_argument",
            f"Source binding for {entity.entity_id!r} has no column-driven argument",
        )
    positions = {
        cast(int, arguments[name].position) for name in rendered if arguments[name].is_positional
    }
    if positions:
        highest = max(positions)
        physical = {
            argument.position: argument
            for argument in arguments.values()
            if argument.is_positional and argument.position is not None
        }
        holes = [
            physical[index].name
            for index in sorted(physical)
            if index < highest and index not in positions
        ]
        if holes:
            _fail(
                "source_binding",
                "optional_positional_hole",
                f"Cannot omit earlier positional arguments {holes!r}",
            )
    ordered = sorted(
        ((arguments[name], sql, detail, value) for name, (sql, detail, value) in rendered.items()),
        key=lambda item: (
            (0, cast(int, item[0].position))
            if item[0].is_positional
            else (1, item[0].field_index or 0)
        ),
    )
    sql_args: list[str] = []
    details: list[dict[str, Any]] = []
    for argument, sql, detail, value in ordered:
        sql_args.append(
            sql if argument.is_positional else f"{_quote_ident(argument.name)} := {sql}"
        )
        details.append(detail)
        if detail["kind"] == "parameter":
            parameters.append(value)
    return f"{_qualified_source(entity)}({', '.join(sql_args)})", details


def _compile_invocation_source(
    catalogs: dict[str, Catalog],
    graph: FederatedSemanticModel,
    identities: dict[str, dict[str, Any]],
    root: SemanticEntity,
    query: dict[str, Any],
    bindings: dict[str, str],
    parameters: list[Any],
) -> dict[str, Any] | None:
    inputs = _validate_inputs(query)
    chain = _resolve_invocation_chain(graph, identities, root, query, bindings, inputs)
    if not chain:
        if inputs or query.get("source_bindings"):
            _fail(
                "source_binding",
                "missing_root_source_binding",
                "Inputs and source bindings must drive the root entity",
            )
        return None

    requested_limit = int(
        (query.get("execution_limits") or {}).get("max_invocations", _DEFAULT_MAX_INVOCATIONS)
    )
    max_invocations = min(_HARD_MAX_INVOCATIONS, requested_limit)
    ctes: list[str] = []
    paths: dict[str, tuple[str, ...]] = {}
    previous_stage: str | None = None
    prevalidated_required_entities: set[str] = set()
    invocation_plans: list[dict[str, Any]] = []
    total_invocations = 0

    for index, binding in enumerate(chain):
        driver = binding.definition["driver"]
        driver_paths: dict[str, tuple[str, ...]]
        if binding.input_id is not None:
            if index != 0:
                _fail(
                    "source_binding",
                    "inline_driver_not_leaf",
                    "An inline input may only begin an invocation path",
                )
            item = inputs[binding.input_id]
            input_alias = f"_input{index}"
            column_names = [str(column["name"]) for column in item["columns"]]
            row_sql: list[str] = []
            for row in item["rows"]:
                values: list[str] = []
                for column, value in zip(item["columns"], row, strict=True):
                    parameters.append(value)
                    values.append(f"CAST(? AS {_safe_type(str(column['type']))})")
                row_sql.append(f"({', '.join(values)})")
            names_sql = ", ".join(_quote_ident(name) for name in column_names)
            values_sql = ", ".join(row_sql)
            ctes.append(f"{_quote_ident(input_alias)}({names_sql}) AS (VALUES {values_sql})")
            driver_source = _quote_ident(input_alias)
            driver_paths = {f"input:{binding.input_id}": ()}
            driver_count = len(item["rows"])
            input_columns = {str(column["name"]): column for column in item["columns"]}
            driver_label = binding.input_id
            driver_kind = "inline_input"
        else:
            assert binding.driver_entity is not None
            driver_marker = _entity_marker(binding.driver_entity)
            driver_count = int(driver["max_rows"])
            base_source: str
            if previous_stage is None:
                if binding.driver_entity.source_kind != "relation":
                    _fail(
                        "source_binding",
                        "unbound_function_driver",
                        "Function driver "
                        f"{binding.driver_entity.entity_id!r} needs its own source binding",
                    )
                qualified_driver = _qualified_source(binding.driver_entity)
                base_source = qualified_driver
                driver_paths = {driver_marker: ()}
            else:
                base_source = _quote_ident(previous_stage)
                driver_paths = paths
            source_alias = '"_source"'
            driver_member_alias = _path_alias(source_alias, driver_paths[driver_marker])
            driver_lookup: dict[str, tuple[SemanticEntity, dict[str, Any], str]] = {}
            for member_id, member in binding.driver_entity.members.items():
                found = (binding.driver_entity, member, driver_member_alias)
                driver_lookup[member_id] = found
                driver_lookup[
                    f"{binding.driver_entity.catalog_id}::{binding.driver_entity.entity_id}::{member_id}"
                ] = found
            driver_filter = driver.get("filters")
            filter_sql = _compile_filter(driver_filter, driver_lookup, parameters)
            filtered_ids = {
                str(ref if isinstance(ref, str) else ref.get("member_id", ""))
                for ref in _filter_members(driver_filter)
            }
            driver_required_filters = (
                _required_filters(catalogs, binding.driver_entity)
                if binding.driver_entity.source_kind == "relation"
                else []
            )
            for group in driver_required_filters:
                satisfied = any(
                    member_id in filtered_ids
                    and binding.driver_entity.members.get(member_id, {}).get("column") == column
                    for column in group
                    for member_id in binding.driver_entity.members
                )
                if not satisfied:
                    _fail(
                        "required_filter",
                        "driver_required_filter_missing",
                        f"Driver {binding.driver_entity.entity_id!r} requires a pre-invocation "
                        f"filter on one of: {', '.join(group)}",
                    )
            if driver_required_filters:
                prevalidated_required_entities.add(driver_marker)
            order_sql: list[str] = []
            for order in driver.get("order", []):
                member_id = str(order.get("member_id", ""))
                order_member = binding.driver_entity.members.get(member_id)
                if order_member is None:
                    _fail(
                        "source_binding",
                        "unknown_driver_order_member",
                        f"Unknown driver order member {member_id!r}",
                    )
                order_sql.append(
                    f"{_member_sql(binding.driver_entity, order_member, driver_member_alias)} "
                    f"{str(order['direction']).upper()}"
                )
            driver_source = " ".join(
                part
                for part in (
                    f"(SELECT * FROM {base_source} AS {source_alias}",
                    f"WHERE {filter_sql}" if filter_sql else "",
                    f"ORDER BY {', '.join(order_sql)}" if order_sql else "",
                    f"LIMIT {driver_count})",
                )
                if part
            )
            input_columns = {}
            driver_label = f"{binding.driver_entity.catalog_id}::{binding.driver_entity.entity_id}"
            driver_kind = "entity"
        total_invocations += driver_count
        if total_invocations > max_invocations:
            _fail(
                "execution_limit",
                "invocation_limit",
                f"Invocation path may execute {total_invocations} function rows, "
                f"above limit {max_invocations}",
            )
        call_sql, argument_plan = _correlated_call_sql(
            binding, query, parameters, '"_driver"', driver_paths, input_columns
        )
        stage = f"_stage{index}"
        stage_limit = min(
            _DEFAULT_MAX_STAGE_ROWS,
            int(binding.definition.get("max_output_rows", _DEFAULT_MAX_STAGE_ROWS)),
        )
        ctes.append(
            f'{_quote_ident(stage)} AS (SELECT "_driver" AS "driver", "_fn" AS "entity" '
            f'FROM {driver_source} AS "_driver" CROSS JOIN LATERAL {call_sql} '
            f'AS "_fn" LIMIT {stage_limit})'
        )
        paths = {marker: ("driver", *path) for marker, path in driver_paths.items()}
        paths[_entity_marker(binding.entity)] = ("entity",)
        invocation_plans.append(
            {
                "entity": {
                    "catalog_id": binding.entity.catalog_id,
                    "entity_id": binding.entity.entity_id,
                },
                "driver_kind": driver_kind,
                "driver": driver_label,
                "argument_bindings": argument_plan,
                "estimated_invocations": driver_count,
            }
        )
        previous_stage = stage

    assert previous_stage is not None
    driving_grain: list[dict[str, Any]] = []
    for marker, path in paths.items():
        if marker == _entity_marker(root):
            continue
        if marker.startswith("input:"):
            input_id = marker.split(":", 1)[1]
            for column in inputs[input_id]["grain"]:
                driving_grain.append(
                    {"source": input_id, "member": str(column), "path": path, "input": True}
                )
            continue
        entity = next(
            (
                item.driver_entity
                for item in chain
                if item.driver_entity is not None and _entity_marker(item.driver_entity) == marker
            ),
            None,
        )
        if entity is None:
            continue
        for member_id in entity.definition.get("grain", []):
            driving_grain.append(
                {
                    "source": f"{entity.catalog_id}::{entity.entity_id}",
                    "member": str(member_id),
                    "path": path,
                    "entity": entity,
                }
            )
    effective = list(driving_grain)
    root_path = paths[_entity_marker(root)]
    for member_id in root.definition.get("grain", []):
        effective.append(
            {
                "source": f"{root.catalog_id}::{root.entity_id}",
                "member": str(member_id),
                "path": root_path,
                "entity": root,
            }
        )
    entity_by_marker: dict[str, SemanticEntity] = {}
    for chain_binding in chain:
        entity_by_marker[_entity_marker(chain_binding.entity)] = chain_binding.entity
        if chain_binding.driver_entity is not None:
            entity_by_marker[_entity_marker(chain_binding.driver_entity)] = (
                chain_binding.driver_entity
            )
    return {
        "with": f"WITH {', '.join(ctes)}",
        "source": _quote_ident(previous_stage),
        "root_alias": _path_alias("_e0", root_path),
        "paths": paths,
        "entities": entity_by_marker,
        "invocation_entities": [item.entity for item in chain],
        "prevalidated_required_entities": prevalidated_required_entities,
        "driving_grain": driving_grain,
        "effective_grain": effective,
        "invocations": invocation_plans,
        "estimated_invocations": total_invocations,
    }


def _member_sql(
    entity: SemanticEntity,
    member: dict[str, Any],
    alias: str,
    stack: tuple[str, ...] = (),
    source_argument_renderer: _SourceArgumentRenderer | None = None,
) -> str:
    member_id = str(member.get("member_id", ""))
    if member_id in stack:
        _fail("type_check", "expression_cycle", f"Cyclic semantic expression at {member_id!r}")
    if _member_column_path(member):
        sql = _member_column_sql(alias, member)
    elif member.get("source_argument") is not None:
        if source_argument_renderer is None:
            _fail(
                "source_binding",
                "source_argument_value_unavailable",
                f"Source-argument member {member_id!r} is unavailable in this query context",
            )
        sql = source_argument_renderer(entity, member)
    elif isinstance(member.get("expression"), dict):
        sql = _expression_sql(
            entity,
            member["expression"],
            alias,
            (*stack, member_id),
            False,
            source_argument_renderer,
        )
    else:
        _fail(
            "type_check",
            "missing_member_source",
            f"Member {member_id!r} has no column or expression",
        )
        raise AssertionError("unreachable")
    if member.get("output_type"):
        return f"CAST({sql} AS {_safe_type(str(member['output_type']))})"
    return sql


def _list_element_sql(value_sql: str, path: list[Any]) -> str:
    if not path:
        return value_sql
    access = ".".join(["_item", *(_quote_ident(str(segment)) for segment in path)])
    return f"list_transform({value_sql}, _item -> {access})"


def _relationship_predicate_sql(
    edge: _Edge,
    pair: dict[str, Any],
    source_alias: str,
    target_alias: str,
) -> str:
    if edge.forward:
        from_entity, from_alias = edge.source, source_alias
        to_entity, to_alias = edge.target, target_alias
    else:
        from_entity, from_alias = edge.target, target_alias
        to_entity, to_alias = edge.source, source_alias
    from_member = from_entity.members.get(str(pair.get("from_member", "")))
    to_member = to_entity.members.get(str(pair.get("to_member", "")))
    if from_member is None or to_member is None:
        _fail(
            "relationship_resolution",
            "unresolved_relationship_member",
            f"Relationship {edge.relationship.relationship_id!r} references an unknown member",
        )
    from_sql = _member_sql(from_entity, from_member, from_alias)
    to_sql = _member_sql(to_entity, to_member, to_alias)
    operator = pair.get("operator", "equal")
    if operator == "equal":
        equality = "IS NOT DISTINCT FROM" if pair.get("nulls", "not_equal") == "equal" else "="
        return f"{from_sql} {equality} {to_sql}"
    if operator == "spatial_contains":
        return f"ST_Contains({from_sql}, {to_sql})"
    if operator == "spatial_within":
        return f"ST_Within({from_sql}, {to_sql})"
    if operator == "spatial_intersects":
        return f"ST_Intersects({from_sql}, {to_sql})"
    if operator == "list_contains":
        if "from_element_path" in pair:
            return (
                f"list_contains({_list_element_sql(from_sql, pair['from_element_path'])}, {to_sql})"
            )
        return f"list_contains({_list_element_sql(to_sql, pair['to_element_path'])}, {from_sql})"
    _fail(
        "relationship_resolution",
        "unsupported_relationship_operator",
        f"Relationship {edge.relationship.relationship_id!r} uses unsupported "
        f"operator {operator!r}",
    )


def _relationship_condition_sql(
    edge: _Edge,
    condition: dict[str, Any],
    source_alias: str,
    target_alias: str,
    parameters: list[Any],
) -> str:
    from_entity = edge.source if edge.forward else edge.target
    to_entity = edge.target if edge.forward else edge.source
    from_alias = source_alias if edge.forward else target_alias
    to_alias = target_alias if edge.forward else source_alias
    if condition.get("side") == "from":
        entity, alias = from_entity, from_alias
    else:
        entity, alias = to_entity, to_alias
    member = entity.members.get(str(condition.get("member", "")))
    if member is None:
        _fail(
            "relationship_resolution",
            "unresolved_relationship_condition_member",
            f"Relationship {edge.relationship.relationship_id!r} condition references an "
            "unknown member",
        )
    value = condition.get("value")
    if not _value_compatible(value, _member_type(entity, member)):
        _fail(
            "type_check",
            "incompatible_relationship_condition_value",
            f"Relationship {edge.relationship.relationship_id!r} condition value is incompatible "
            f"with member {member.get('member_id')!r}",
        )
    parameters.append(value)
    return f"{_member_sql(entity, member, alias)} IS NOT DISTINCT FROM ?"


def _expression_sql(
    entity: SemanticEntity,
    expression: dict[str, Any],
    alias: str,
    stack: tuple[str, ...],
    aggregate_members: bool,
    source_argument_renderer: _SourceArgumentRenderer | None = None,
) -> str:
    op = str(expression.get("op", ""))
    if op == "member":
        member_id = str(expression.get("member", ""))
        member = entity.members.get(member_id)
        if member is None:
            _fail("type_check", "unknown_expression_member", f"Unknown member {member_id!r}")
        if aggregate_members and member.get("kind") == "measure":
            return _aggregate_sql(entity, member, alias, stack, source_argument_renderer)
        return _member_sql(entity, member, alias, stack, source_argument_renderer)
    if op == "literal":
        return _literal_sql(expression.get("value"))
    if op in (*_BINARY_OPERATORS, "safe_divide"):
        left = _expression_sql(
            entity, expression["left"], alias, stack, aggregate_members, source_argument_renderer
        )
        right = _expression_sql(
            entity, expression["right"], alias, stack, aggregate_members, source_argument_renderer
        )
        if op == "safe_divide":
            return f"({left} / NULLIF({right}, 0))"
        return f"({left} {_BINARY_OPERATORS[op]} {right})"
    if op == "coalesce":
        args = [
            _expression_sql(entity, item, alias, stack, aggregate_members, source_argument_renderer)
            for item in expression.get("args", [])
        ]
        return f"COALESCE({', '.join(args)})"
    if op == "nullif":
        left = _expression_sql(
            entity, expression["value"], alias, stack, aggregate_members, source_argument_renderer
        )
        other = expression.get("other", {"op": "literal", "value": 0})
        right = _expression_sql(
            entity, other, alias, stack, aggregate_members, source_argument_renderer
        )
        return f"NULLIF({left}, {right})"
    if op == "cast":
        value = _expression_sql(
            entity, expression["value"], alias, stack, aggregate_members, source_argument_renderer
        )
        return f"CAST({value} AS {_safe_type(str(expression['type']))})"
    if op == "case":
        when = _expression_sql(
            entity, expression["when"], alias, stack, aggregate_members, source_argument_renderer
        )
        then = _expression_sql(
            entity, expression["then"], alias, stack, aggregate_members, source_argument_renderer
        )
        otherwise = expression.get("else")
        suffix = (
            ""
            if otherwise is None
            else " ELSE "
            + _expression_sql(
                entity,
                otherwise,
                alias,
                stack,
                aggregate_members,
                source_argument_renderer,
            )
        )
        return f"CASE WHEN {when} THEN {then}{suffix} END"
    _fail("sql_generation", "unsupported_expression", "Unsupported semantic expression")
    raise AssertionError("unreachable")


def _aggregate_sql(
    entity: SemanticEntity,
    member: dict[str, Any],
    alias: str,
    stack: tuple[str, ...] = (),
    source_argument_renderer: _SourceArgumentRenderer | None = None,
) -> str:
    member_id = str(member.get("member_id", ""))
    if member_id in stack:
        _fail("type_check", "measure_cycle", f"Cyclic derived measure at {member_id!r}")
    expression = member.get("expression")
    if isinstance(expression, dict):
        sql = _expression_sql(
            entity,
            expression,
            alias,
            (*stack, member_id),
            True,
            source_argument_renderer,
        )
    else:
        aggregation = str(member.get("aggregation", ""))
        if not aggregation:
            _fail(
                "type_check",
                "invalid_measure",
                f"Measure {member_id!r} has no aggregation or expression",
            )
        if aggregation == "count_rows":
            sql = "COUNT(*)"
        else:
            input_member = entity.members.get(str(member.get("member", "")))
            if input_member is None:
                _fail(
                    "type_check",
                    "unknown_measure_input",
                    f"Measure {member_id!r} has unknown input {member.get('member')!r}",
                )
            value = _member_sql(
                entity, input_member, alias, source_argument_renderer=source_argument_renderer
            )
            sql = (
                f"COUNT(DISTINCT {value})"
                if aggregation == "count_distinct"
                else f"{aggregation.upper()}({value})"
            )
    if member.get("output_type"):
        return f"CAST({sql} AS {_safe_type(str(member['output_type']))})"
    return sql


def _catalog_identities(catalogs: dict[str, Catalog]) -> dict[str, dict[str, Any]]:
    return {
        alias: model.catalog or {}
        for alias, catalog in catalogs.items()
        if (model := build_semantic_model(catalog)).catalog is not None
    }


def _resolve_entity(
    graph: FederatedSemanticModel,
    identities: dict[str, dict[str, Any]],
    ref: dict[str, Any],
    bindings: dict[str, str],
    anchor_alias: str | None = None,
) -> SemanticEntity:
    candidates = list(graph.entities.get(_ref_key(ref), []))
    if anchor_alias and any(item.host.database == anchor_alias for item in candidates):
        candidates = [item for item in candidates if item.host.database == anchor_alias]
    selected_alias: str | None = None
    for candidate in candidates:
        identity = identities.get(candidate.host.database, {})
        binding_key = str(identity.get("binding_key") or candidate.catalog_id)
        selected_alias = bindings.get(binding_key) or bindings.get(candidate.catalog_id)
        if selected_alias:
            break
    if selected_alias:
        candidates = [item for item in candidates if item.host.database == selected_alias]
    if len(candidates) == 1:
        return candidates[0]
    code = "ambiguous_catalog_binding" if candidates else "unresolved_catalog_binding"
    ref_name = f"{ref.get('catalog_id')!r}.{ref.get('entity_id')!r}"
    message = (
        f"Entity {ref_name} matches multiple attachments; provide bindings"
        if candidates
        else f"Entity {ref_name} is not attached"
    )
    raise _CompileFailure(
        "catalog_binding",
        code,
        message,
        details={"candidates": [item.host.database for item in candidates]},
    )


def _find_path(
    graph: FederatedSemanticModel,
    identities: dict[str, dict[str, Any]],
    root: SemanticEntity,
    target: SemanticEntity,
    requested: list[str] | None,
    bindings: dict[str, str],
) -> list[_Edge]:
    if _entity_marker(root) == _entity_marker(target):
        return []
    allowed = set(requested or ())
    relationships = [
        item
        for item in graph.relationships.values()
        if not allowed or item.relationship_id in allowed
    ]
    queue: deque[tuple[SemanticEntity, list[_Edge]]] = deque([(root, [])])
    found: list[list[_Edge]] = []
    seen: set[tuple[str, int]] = set()
    while queue:
        current, path = queue.popleft()
        marker = (_entity_marker(current), len(path))
        if marker in seen:
            continue
        seen.add(marker)
        if _entity_marker(current) == _entity_marker(target):
            found.append(path)
            continue
        if len(path) >= 8:
            continue
        used = {edge.relationship.relationship_id for edge in path}
        for relationship in relationships:
            if relationship.resolution_status not in {"resolved", "ambiguous"}:
                continue
            if relationship.relationship_id in used:
                continue
            definition = relationship.definition
            forward = _ref_key(definition.get("from", {})) == (
                current.catalog_id,
                current.entity_id,
            )
            backward = _ref_key(definition.get("to", {})) == (current.catalog_id, current.entity_id)
            if not forward and not backward:
                continue
            next_ref = cast(dict[str, Any], definition["to"] if forward else definition["from"])
            anchor = (
                current.host.database
                if current.host.database in relationship.host_aliases
                else None
            )
            try:
                next_entity = _resolve_entity(graph, identities, next_ref, bindings, anchor)
            except _CompileFailure:
                continue
            queue.append((next_entity, [*path, _Edge(relationship, current, next_entity, forward)]))
    exact = found
    if requested:
        exact = [
            path
            for path in found
            if [edge.relationship.relationship_id for edge in path] == requested
        ]
    if len(exact) != 1:
        _fail(
            "relationship_resolution",
            "ambiguous_relationship_path" if exact else "relationship_path_not_found",
            (
                f"Multiple relationship paths reach {target.entity_id!r}; specify relationship_path"
                if exact
                else f"No relationship path reaches {target.entity_id!r}"
            ),
        )
    return exact[0]


def _filter_members(value: Any, depth: int = 0) -> list[Any]:
    if value is None:
        return []
    if depth >= 8:
        _fail("request_validation", "filter_depth", "Filter nesting may not exceed 8 levels")
    if isinstance(value, dict) and "and" in value:
        return [item for child in value["and"] for item in _filter_members(child, depth + 1)]
    if isinstance(value, dict) and "or" in value:
        return [item for child in value["or"] for item in _filter_members(child, depth + 1)]
    return [value.get("member")] if isinstance(value, dict) else []


def _filter_key(member: Any) -> str:
    if isinstance(member, str):
        return member
    if isinstance(member, dict):
        catalog_id, entity_id = _ref_key(member)
        return f"{catalog_id}::{entity_id}::{member.get('member_id', '')}"
    return ""


def _compile_filter(
    value: Any,
    lookup: dict[str, tuple[SemanticEntity, dict[str, Any], str]],
    parameters: list[Any],
    source_argument_renderer: _SourceArgumentRenderer | None = None,
) -> str | None:
    if value is None:
        return None
    if "and" in value:
        parts = [
            cast(str, _compile_filter(item, lookup, parameters, source_argument_renderer))
            for item in value["and"]
        ]
        return f"({' AND '.join(parts)})"
    if "or" in value:
        parts = [
            cast(str, _compile_filter(item, lookup, parameters, source_argument_renderer))
            for item in value["or"]
        ]
        return f"({' OR '.join(parts)})"
    found = lookup.get(_filter_key(value.get("member")))
    if found is None:
        member = value.get("member")
        label = member if isinstance(member, str) else member.get("member_id", "")
        _fail(
            "type_check", "unknown_filter_member", f"Unknown or ambiguous filter member {label!r}"
        )
    entity, member, alias = found
    lhs = (
        _aggregate_sql(entity, member, alias, source_argument_renderer=source_argument_renderer)
        if member.get("kind") == "measure"
        else _member_sql(entity, member, alias, source_argument_renderer=source_argument_renderer)
    )
    operator = str(value.get("operator", ""))
    if operator == "is_null":
        return f"{lhs} IS NULL"
    if operator == "is_not_null":
        return f"{lhs} IS NOT NULL"
    values = list(value.get("values", [])) if "values" in value else [value.get("value")]
    parameters.extend(values)
    if operator in {"in", "not_in"}:
        placeholders = ", ".join("?" for _ in values)
        return f"{lhs} {'IN' if operator == 'in' else 'NOT IN'} ({placeholders})"
    if operator == "between":
        return f"{lhs} BETWEEN ? AND ?"
    sql_operator = _FILTER_OPERATORS.get(operator)
    if sql_operator is None:
        _fail(
            "request_validation", "invalid_filter_operator", f"Unknown filter operator {operator!r}"
        )
    return f"{lhs} {sql_operator} ?"


def _required_filters(catalogs: dict[str, Catalog], entity: SemanticEntity) -> list[list[str]]:
    catalog = catalogs.get(entity.host.database)
    if catalog is None:
        return []
    objects: list[Table | Function] = [*catalog.iter_table_like(), *catalog.iter_all_functions()]
    host = next((item for item in objects if item.id == entity.host), None)
    if host is None:
        return []
    raw = host.tags.get(TAG_REQUIRED_FILTERS)
    if raw is None:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [list(map(str, group)) for group in decoded if isinstance(group, list)]


def compile_semantic_query(catalogs: dict[str, Catalog], query: dict[str, Any]) -> dict[str, Any]:
    """Compile one semantic request into deterministic, parameterized DuckDB SQL.

    Args:
        catalogs: Runtime attachment alias to normalized VGI catalog.
        query: A value conforming to the packaged ``query.json`` schema.

    Returns:
        A ``result.json``-shaped success or structured diagnostic object.
    """
    try:
        request_errors = validate_instance("query", query)
        if request_errors:
            return {
                "ok": False,
                "diagnostics": [
                    {"stage": "request_validation", "code": "query_schema", "message": item}
                    for item in request_errors
                ],
            }
        model_schema_errors = [
            (alias, diagnostic)
            for alias, catalog in catalogs.items()
            for diagnostic in schema_diagnostics(catalog)
        ]
        if model_schema_errors:
            return {
                "ok": False,
                "diagnostics": [
                    {
                        "stage": "model_resolution",
                        "code": "semantic_schema",
                        "message": diagnostic.message,
                        "path": f"{alias}:{diagnostic.object_id.qualified()}",
                    }
                    for alias, diagnostic in model_schema_errors
                ],
            }
        graph = build_federated_semantic_model(catalogs)
        blocking = [
            item for item in graph.diagnostics if item.code != "duplicate_relationship_candidate"
        ]
        if blocking:
            return {
                "ok": False,
                "diagnostics": [
                    {
                        "stage": "relationship_resolution"
                        if "relationship" in item.code
                        else "model_resolution",
                        "code": item.code,
                        "message": item.message,
                        "path": item.object_id.qualified(),
                    }
                    for item in blocking
                ],
            }
        identities = _catalog_identities(catalogs)
        measures = list(query.get("measures", []))
        dimensions = list(query.get("dimensions", []))
        if not measures and not dimensions:
            _fail(
                "request_validation", "empty_selection", "Select at least one measure or dimension"
            )
        filter_refs = [
            *_filter_members(query.get("filters")),
            *_filter_members(query.get("measure_filters")),
        ]
        if len(filter_refs) > 100:
            _fail(
                "request_validation",
                "filter_node_limit",
                "At most 100 filter predicates are allowed",
            )
        roots = {_ref_key(item) for item in measures}
        if len(roots) > 1:
            _fail(
                "multi_fact_not_supported",
                "multi_fact_not_supported",
                "Measures from multiple root entities are not supported yet",
            )
        root_ref = measures[0] if measures else query.get("root_entity")
        if not isinstance(root_ref, dict):
            _fail(
                "request_validation",
                "root_entity_required",
                "Dimension-only queries require root_entity",
            )
        bindings = cast(dict[str, str], query.get("bindings") or {})
        root = _resolve_entity(graph, identities, root_ref, bindings)
        parameters: list[Any] = []
        invocation = _compile_invocation_source(
            catalogs, graph, identities, root, query, bindings, parameters
        )

        def render_source_argument_member(entity: SemanticEntity, member: dict[str, Any]) -> str:
            argument_name = str(member.get("source_argument", ""))
            physical = [
                argument for argument in entity.function_arguments if argument.name == argument_name
            ]
            mappings = [
                mapping
                for mapping in _source_arguments(entity)
                if mapping.get("argument") == argument_name
            ]
            if len(physical) != 1 or len(mappings) != 1:
                _fail(
                    "model_resolution",
                    "source_argument_member_unresolved",
                    f"Cannot resolve source argument {argument_name!r} for member "
                    f"{member.get('member_id')!r}",
                )
            argument = physical[0]
            mapping = mappings[0]
            source_binding = _source_binding_definition(entity, query)
            override = (
                source_binding.get("arguments", {}).get(argument_name)
                if source_binding is not None
                else None
            )
            if isinstance(override, dict) and "input_column" in override:
                assert source_binding is not None
                if invocation is None:
                    _fail(
                        "source_binding",
                        "source_argument_value_unavailable",
                        f"Source argument {argument_name!r} has no compiled input driver",
                    )
                driver = cast(dict[str, Any], source_binding.get("driver", {}))
                input_id = str(driver.get("input_id", ""))
                path = invocation["paths"].get(f"input:{input_id}")
                if path is None:
                    _fail(
                        "source_binding",
                        "source_argument_value_unavailable",
                        f"Input driver {input_id!r} is unavailable for member "
                        f"{member.get('member_id')!r}",
                    )
                return f"{_path_alias('_e0', path)}.{_quote_ident(str(override['input_column']))}"
            if isinstance(override, dict) and "member" in override:
                if invocation is None:
                    _fail(
                        "source_binding",
                        "source_argument_value_unavailable",
                        f"Source argument {argument_name!r} has no compiled entity driver",
                    )
                ref = cast(dict[str, Any], override["member"])
                driver_entity = _resolve_entity(graph, identities, ref, bindings)
                driver_member = driver_entity.members.get(str(ref.get("member_id", "")))
                path = invocation["paths"].get(_entity_marker(driver_entity))
                if driver_member is None or path is None:
                    _fail(
                        "source_binding",
                        "source_argument_value_unavailable",
                        f"Entity driver value is unavailable for member "
                        f"{member.get('member_id')!r}",
                    )
                return _member_sql(driver_entity, driver_member, _path_alias("_e0", path))

            parameter_name = str(
                (override if isinstance(override, dict) else mapping).get("parameter", "")
            )
            supplied = query.get("parameters", {})
            value: Any = (
                supplied.get(parameter_name, _MISSING)
                if isinstance(supplied, dict) and parameter_name
                else _MISSING
            )
            if value is _MISSING and argument.default is not None:
                try:
                    value = json.loads(argument.default)
                except (TypeError, ValueError):
                    _fail(
                        "source_binding",
                        "source_argument_default_invalid",
                        f"Function argument {argument_name!r} has an invalid JSON default",
                    )
            if value is _MISSING:
                _fail(
                    "required_filter"
                    if mapping.get("required", True) is not False
                    else "source_binding",
                    "missing_source_parameter"
                    if mapping.get("required", True) is not False
                    else "source_argument_value_unavailable",
                    f"Source argument {argument_name!r} has no effective value for member "
                    f"{member.get('member_id')!r}",
                )
            if not _value_compatible(value, argument.type):
                _fail(
                    "type_check",
                    "incompatible_parameter_type",
                    f"Parameter {parameter_name!r} is incompatible with argument {argument_name!r}",
                )
            parameters.append(value)
            member_type = str(member.get("data_type") or member.get("output_type"))
            return f"CAST(? AS {_safe_type(member_type)})"

        aliases: dict[str, str] = (
            {
                marker: _path_alias("_e0", path)
                for marker, path in invocation["paths"].items()
                if not marker.startswith("input:")
            }
            if invocation
            else {_entity_marker(root): "_e0"}
        )
        joins: list[tuple[_Edge, str]] = []

        def add_path(entity: SemanticEntity, requested: list[str] | None = None) -> None:
            if _entity_marker(entity) in aliases:
                return
            for edge in _find_path(graph, identities, root, entity, requested, bindings):
                definition = edge.relationship.definition
                cardinality = definition["to_cardinality" if edge.forward else "from_cardinality"]
                if cardinality.get("max") == "many":
                    _fail(
                        "fanout",
                        "fanout_unsafe",
                        f"Relationship {edge.relationship.relationship_id!r} traverses "
                        "into a many side",
                    )
                if edge.target.source_kind == "table_function" and _source_arguments(edge.target):
                    _fail(
                        "relationship_resolution",
                        "parameterized_joined_function",
                        f"Joined table function {edge.target.entity_id!r} must be zero-argument",
                    )
                marker = _entity_marker(edge.target)
                if marker not in aliases:
                    alias = f"_e{len(aliases)}"
                    aliases[marker] = alias
                    joins.append((edge, alias))

        resolved: list[_Resolved] = []
        for selection in [*dimensions, *measures]:
            entity = _resolve_entity(graph, identities, selection, bindings)
            requested = selection.get("relationship_path")
            add_path(entity, list(requested) if isinstance(requested, list) else None)
            member_id = str(selection.get("member_id", ""))
            member = entity.members.get(member_id)
            if member is None:
                _fail(
                    "model_resolution",
                    "unknown_member",
                    f"Unknown member {member_id!r} on {entity.entity_id!r}",
                )
            resolved.append(_Resolved(entity, member, aliases[_entity_marker(entity)], selection))
        for member_ref in filter_refs:
            if not isinstance(member_ref, dict):
                continue
            entity = _resolve_entity(graph, identities, member_ref, bindings)
            requested = member_ref.get("relationship_path")
            add_path(entity, list(requested) if isinstance(requested, list) else None)

        dimension_items = resolved[: len(dimensions)]
        measure_items = resolved[len(dimensions) :]
        participating = _unique_entities(
            [
                root,
                *((invocation or {}).get("entities", {}).values()),
                *(edge.target for edge, _alias in joins),
            ]
        )
        lookup: dict[str, tuple[SemanticEntity, dict[str, Any], str]] = {}
        ambiguous: set[str] = set()
        required_entities = _unique_entities(
            [
                root,
                *((invocation or {}).get("invocation_entities", [])),
                *(edge.target for edge, _alias in joins),
            ]
        )
        for entity in required_entities:
            alias = aliases[_entity_marker(entity)]
            for member_id, member in entity.members.items():
                qualified = f"{entity.catalog_id}::{entity.entity_id}::{member_id}"
                lookup[qualified] = (entity, member, alias)
                if member_id in lookup or member_id in ambiguous:
                    lookup.pop(member_id, None)
                    ambiguous.add(member_id)
                else:
                    lookup[member_id] = (entity, member, alias)

        selects: list[str] = []
        groups: list[str] = []
        output_names: set[str] = set()
        result_grain: list[str] = []
        driving_plan_grain: list[dict[str, str]] = []
        output_units: dict[str, str | None] = {}
        unit_diagnostics: list[dict[str, Any]] = []

        def record_unit(item: _Resolved, output_name: str) -> None:
            declared, unit, diagnostic = _resolve_output_unit(
                item.entity, item.member, output_name, query
            )
            if declared:
                output_units[output_name] = unit
            if diagnostic is not None:
                unit_diagnostics.append(diagnostic)

        if invocation and not query.get("allow_driving_grain_reduction", False):
            selected_refs = {
                (*_ref_key(item), str(item.get("member_id", ""))) for item in dimensions
            }
            for grain in invocation["driving_grain"]:
                base_name = str(grain["member"])
                if grain.get("entity") is not None:
                    entity = cast(SemanticEntity, grain["entity"])
                    ref = (entity.catalog_id, entity.entity_id, base_name)
                    member = entity.members.get(base_name)
                    if member is None:
                        _fail(
                            "source_binding",
                            "unknown_driver_grain",
                            f"Driver grain member {base_name!r} does not exist",
                        )
                    sql = _member_sql(entity, member, _path_alias("_e0", grain["path"]))
                    if ref in selected_refs:
                        continue
                else:
                    sql = f"{_path_alias('_e0', grain['path'])}.{_quote_ident(base_name)}"
                name = base_name
                if name in output_names or any(
                    str(item.get("alias") or item.get("member_id")) == name for item in dimensions
                ):
                    name = f"{str(grain['source']).split('::')[-1]}__{base_name}"
                if name in output_names:
                    _fail(
                        "source_binding",
                        "driving_grain_name_collision",
                        f"Driving grain output {name!r} is ambiguous",
                    )
                output_names.add(name)
                selects.append(f"{sql} AS {_quote_ident(name)}")
                groups.append(sql)
                result_grain.append(name)
                driving_plan_grain.append(
                    {"source": str(grain["source"]), "member": base_name, "output_name": name}
                )
        for item in dimension_items:
            if item.member.get("kind") == "measure":
                _fail(
                    "type_check",
                    "not_a_dimension",
                    f"{item.member.get('member_id')!r} is a measure",
                )
            sql = _member_sql(
                item.entity,
                item.member,
                item.alias,
                source_argument_renderer=render_source_argument_member,
            )
            granularity = item.selection.get("granularity")
            if granularity:
                allowed = item.member.get("granularities", [])
                if item.member.get("kind") != "time_dimension" or granularity not in allowed:
                    _fail(
                        "type_check",
                        "invalid_time_granularity",
                        f"Granularity {granularity!r} is not allowed for "
                        f"{item.member.get('member_id')!r}",
                    )
                timezone = str(item.member.get("timezone") or "UTC")
                granularity_sql = _quote_literal(str(granularity))
                sql = (
                    f"date_trunc({granularity_sql}, {sql} AT TIME ZONE {_quote_literal(timezone)})"
                )
            name = str(item.selection.get("alias") or item.member.get("member_id"))
            if name in output_names:
                _fail("request_validation", "duplicate_output", f"Duplicate output name {name!r}")
            output_names.add(name)
            selects.append(f"{sql} AS {_quote_ident(name)}")
            groups.append(
                str(len(selects)) if _member_uses_source_argument(item.entity, item.member) else sql
            )
            result_grain.append(name)
            record_unit(item, name)
        selected_dimension_ids = {str(item.member.get("member_id")) for item in dimension_items}
        for item in measure_items:
            if item.member.get("kind") != "measure":
                _fail(
                    "type_check",
                    "not_a_measure",
                    f"{item.member.get('member_id')!r} is not a measure",
                )
            additivity = item.member.get("additivity")
            if isinstance(additivity, dict):
                prohibited = selected_dimension_ids.intersection(
                    additivity.get("prohibited_dimensions", [])
                )
                if prohibited:
                    _fail(
                        "type_check",
                        "semi_additive_dimension",
                        f"Measure {item.member.get('member_id')!r} cannot be grouped by "
                        f"{', '.join(sorted(prohibited))}",
                    )
            name = str(item.selection.get("alias") or item.member.get("member_id"))
            if name in output_names:
                _fail("request_validation", "duplicate_output", f"Duplicate output name {name!r}")
            output_names.add(name)
            aggregate = _aggregate_sql(
                item.entity,
                item.member,
                item.alias,
                source_argument_renderer=render_source_argument_member,
            )
            selects.append(f"{aggregate} AS {_quote_ident(name)}")
            record_unit(item, name)

        from_sql = (
            f"FROM {invocation['source']} AS _e0"
            if invocation
            else f"FROM {_source_sql(root, query, parameters)} AS _e0"
        )
        join_lines: list[str] = []
        for edge, alias in joins:
            definition = edge.relationship.definition
            left_alias = aliases[_entity_marker(edge.source)]
            target_sql = _source_sql(edge.target, query, parameters)
            pairs: list[str] = []
            for pair in definition.get("predicate", []):
                pairs.append(_relationship_predicate_sql(edge, pair, left_alias, alias))
            for condition in definition.get("conditions", []):
                pairs.append(
                    _relationship_condition_sql(edge, condition, left_alias, alias, parameters)
                )
            cardinality = definition["to_cardinality" if edge.forward else "from_cardinality"]
            join_type = "INNER" if cardinality.get("min") == 1 else "LEFT"
            join_lines.append(f"{join_type} JOIN {target_sql} AS {alias} ON {' AND '.join(pairs)}")

        where = _compile_filter(
            query.get("filters"), lookup, parameters, render_source_argument_member
        )
        having = _compile_filter(
            query.get("measure_filters"), lookup, parameters, render_source_argument_member
        )
        filtered = [lookup.get(_filter_key(item)) for item in _filter_members(query.get("filters"))]
        for entity in participating:
            if _entity_marker(entity) in (
                (invocation or {}).get("prevalidated_required_entities", set())
            ):
                continue
            for group in _required_filters(catalogs, entity):
                locally_filtered = any(
                    ".".join(_member_column_path(member)) == column
                    and any(
                        item is not None and item[0] is entity and item[1] is member
                        for item in filtered
                    )
                    for column in group
                    for member in entity.members.values()
                )
                supplied_parameters: dict[str, Any] = (
                    query["parameters"] if isinstance(query.get("parameters"), dict) else {}
                )
                argument_supplied = any(
                    mapping.get("argument") == column
                    and mapping.get("parameter") in supplied_parameters
                    for column in group
                    for mapping in _source_arguments(entity)
                )
                correlated_argument_supplied = any(
                    _ref_key(source_binding.get("entity", {}))
                    == (entity.catalog_id, entity.entity_id)
                    and isinstance((bound := source_binding.get("arguments", {}).get(column)), dict)
                    and (
                        "input_column" in bound
                        or "member" in bound
                        or ("parameter" in bound and bound["parameter"] in supplied_parameters)
                    )
                    for column in group
                    for source_binding in query.get("source_bindings", [])
                )
                if (
                    not locally_filtered
                    and not argument_supplied
                    and not correlated_argument_supplied
                ):
                    _fail(
                        "required_filter",
                        "required_filter_missing",
                        f"Entity {entity.entity_id!r} requires a source-local filter "
                        f"on one of: {', '.join(group)}",
                    )

        order_lines: list[str] = []
        for item in query.get("order", []):
            order_member = str(item.get("member", ""))
            if order_member not in output_names:
                _fail(
                    "request_validation",
                    "invalid_order_member",
                    f"ORDER BY {order_member!r} is not a selected output",
                )
            order_lines.append(f"{_quote_ident(order_member)} {str(item['direction']).upper()}")
        limit = min(10_000, max(1, int(query.get("limit", 1000))))
        sql = "\n".join(
            item
            for item in (
                invocation["with"] if invocation else "",
                f"SELECT {', '.join(selects)}",
                from_sql,
                *join_lines,
                f"WHERE {where}" if where else "",
                f"GROUP BY {', '.join(groups)}" if groups else "",
                f"HAVING {having}" if having else "",
                f"ORDER BY {', '.join(order_lines)}" if order_lines else "",
                f"LIMIT {limit}",
            )
            if item
        )
        warnings = [
            item.message
            for item in graph.diagnostics
            if item.code == "duplicate_relationship_candidate"
        ]
        plan = {
            "fact_branches": [
                {
                    "root": {"catalog_id": root.catalog_id, "entity_id": root.entity_id},
                    "attachment_alias": root.host.database,
                    "entities": list(
                        dict.fromkeys(
                            [
                                *(
                                    marker
                                    for marker in (invocation or {}).get("paths", {})
                                    if not marker.startswith("input:")
                                ),
                                *aliases,
                            ]
                        )
                    ),
                    **(
                        {
                            "driver": {
                                "kind": invocation["invocations"][0]["driver_kind"],
                                "source": invocation["invocations"][0]["driver"],
                            }
                        }
                        if invocation
                        else {}
                    ),
                    "invocations": invocation["invocations"] if invocation else [],
                    "effective_source_grain": [
                        {
                            "source": str(item["source"]),
                            "member": str(item["member"]),
                            "output_name": next(
                                (
                                    grain["output_name"]
                                    for grain in driving_plan_grain
                                    if grain["source"] == str(item["source"])
                                    and grain["member"] == str(item["member"])
                                ),
                                str(item["member"]),
                            ),
                        }
                        for item in (
                            invocation["effective_grain"]
                            if invocation
                            else [
                                {
                                    "source": f"{root.catalog_id}::{root.entity_id}",
                                    "member": str(member_id),
                                }
                                for member_id in root.definition.get("grain", [])
                            ]
                        )
                    ],
                    "result_grain": result_grain,
                    "estimated_invocations": invocation["estimated_invocations"]
                    if invocation
                    else 0,
                    "driving_grain_reduced": bool(
                        invocation and query.get("allow_driving_grain_reduction", False)
                    ),
                }
            ],
            "sql": sql,
            "parameters": parameters,
            "validation_scope": "semantic",
            "warnings": warnings,
            **({"output_units": output_units} if output_units else {}),
            **({"unit_diagnostics": unit_diagnostics} if unit_diagnostics else {}),
        }
        plan_errors = validate_instance("plan", plan)
        if plan_errors:
            _fail("sql_generation", "invalid_plan", "; ".join(plan_errors))
        return {"ok": True, "plan": plan}
    except _CompileFailure as exc:
        return {"ok": False, "diagnostics": [exc.diagnostic]}
    except Exception as exc:  # noqa: BLE001 - compiler failures are always structured
        return {
            "ok": False,
            "diagnostics": [
                {
                    "stage": "sql_generation",
                    "code": "internal_compiler_error",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
        }


def execute_semantic_query(
    catalogs: dict[str, Catalog], connection: Any, query: dict[str, Any]
) -> dict[str, Any]:
    """Compile and, unless ``compile_only``, execute a semantic request.

    Args:
        catalogs: Runtime attachment alias to normalized VGI catalog.
        connection: DuckDB-compatible connection or cursor.
        query: Semantic query request.

    Returns:
        The compiler result, with a rendered ``result`` on execution success.
    """
    compiled = compile_semantic_query(catalogs, query)
    if not compiled.get("ok") or query.get("compile_only") is True:
        return compiled
    plan = compiled["plan"]
    try:
        result = connection.execute(plan["sql"], plan["parameters"])
        columns = [str(item[0]) for item in result.description] if result.description else []
        rows = list(result.fetchall() or [])
    except Exception as exc:  # noqa: BLE001 - returned to an agent as a typed diagnostic
        return {
            "ok": False,
            "plan": plan,
            "diagnostics": [
                {
                    "stage": "duckdb_execution",
                    "code": "duckdb_execution_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
        }
    return {
        "ok": True,
        "plan": plan,
        "result": {"columns": columns, "rows": rows, "row_count": len(rows)},
    }
