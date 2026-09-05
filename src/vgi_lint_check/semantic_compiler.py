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
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast

from .model import TAG_REQUIRED_FILTERS, Argument, Catalog, Function, Table
from .semantic_federation import (
    FederatedRelationship,
    FederatedSemanticModel,
    build_federated_semantic_model,
)
from .semantic_model import SemanticEntity, build_semantic_model, schema_diagnostics
from .semantic_schema import validate_instance

DiagnosticStage = Literal[
    "request_validation",
    "model_resolution",
    "multi_fact_not_supported",
    "catalog_binding",
    "relationship_resolution",
    "type_check",
    "fanout",
    "required_filter",
    "sql_generation",
    "duckdb_execution",
]

_SAFE_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(?:\([0-9]+(?:,[0-9]+)?\))?(?:\[\])?$")
_BINARY_OPERATORS = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}
_FILTER_OPERATORS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


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


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ref_key(value: dict[str, Any]) -> tuple[str, str]:
    return str(value.get("catalog_id", "")), str(value.get("entity_id", ""))


def _entity_marker(entity: SemanticEntity) -> str:
    return f"{entity.host.database}:{entity.catalog_id}::{entity.entity_id}"


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
        parameters.append(supplied[parameter])
        args.append(f"{_quote_ident(argument.name)} := ?" if argument.is_named else "?")
    return f"{qualified}({', '.join(args)})"


def _member_sql(
    entity: SemanticEntity, member: dict[str, Any], alias: str, stack: tuple[str, ...] = ()
) -> str:
    member_id = str(member.get("member_id", ""))
    if member_id in stack:
        _fail("type_check", "expression_cycle", f"Cyclic semantic expression at {member_id!r}")
    if member.get("column"):
        sql = f"{alias}.{_quote_ident(str(member['column']))}"
    elif isinstance(member.get("expression"), dict):
        sql = _expression_sql(entity, member["expression"], alias, (*stack, member_id), False)
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


def _expression_sql(
    entity: SemanticEntity,
    expression: dict[str, Any],
    alias: str,
    stack: tuple[str, ...],
    aggregate_members: bool,
) -> str:
    op = str(expression.get("op", ""))
    if op == "member":
        member_id = str(expression.get("member", ""))
        member = entity.members.get(member_id)
        if member is None:
            _fail("type_check", "unknown_expression_member", f"Unknown member {member_id!r}")
        if aggregate_members and member.get("kind") == "measure":
            return _aggregate_sql(entity, member, alias, stack)
        return _member_sql(entity, member, alias, stack)
    if op == "literal":
        return _literal_sql(expression.get("value"))
    if op in (*_BINARY_OPERATORS, "safe_divide"):
        left = _expression_sql(entity, expression["left"], alias, stack, aggregate_members)
        right = _expression_sql(entity, expression["right"], alias, stack, aggregate_members)
        if op == "safe_divide":
            return f"({left} / NULLIF({right}, 0))"
        return f"({left} {_BINARY_OPERATORS[op]} {right})"
    if op == "coalesce":
        args = [
            _expression_sql(entity, item, alias, stack, aggregate_members)
            for item in expression.get("args", [])
        ]
        return f"COALESCE({', '.join(args)})"
    if op == "nullif":
        left = _expression_sql(entity, expression["value"], alias, stack, aggregate_members)
        other = expression.get("other", {"op": "literal", "value": 0})
        right = _expression_sql(entity, other, alias, stack, aggregate_members)
        return f"NULLIF({left}, {right})"
    if op == "cast":
        value = _expression_sql(entity, expression["value"], alias, stack, aggregate_members)
        return f"CAST({value} AS {_safe_type(str(expression['type']))})"
    if op == "case":
        when = _expression_sql(entity, expression["when"], alias, stack, aggregate_members)
        then = _expression_sql(entity, expression["then"], alias, stack, aggregate_members)
        otherwise = expression.get("else")
        suffix = (
            ""
            if otherwise is None
            else f" ELSE {_expression_sql(entity, otherwise, alias, stack, aggregate_members)}"
        )
        return f"CASE WHEN {when} THEN {then}{suffix} END"
    _fail("sql_generation", "unsupported_expression", "Unsupported semantic expression")
    raise AssertionError("unreachable")


def _aggregate_sql(
    entity: SemanticEntity, member: dict[str, Any], alias: str, stack: tuple[str, ...] = ()
) -> str:
    member_id = str(member.get("member_id", ""))
    if member_id in stack:
        _fail("type_check", "measure_cycle", f"Cyclic derived measure at {member_id!r}")
    expression = member.get("expression")
    if isinstance(expression, dict):
        sql = _expression_sql(entity, expression, alias, (*stack, member_id), True)
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
            value = _member_sql(entity, input_member, alias)
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
    value: Any, lookup: dict[str, tuple[SemanticEntity, dict[str, Any], str]], parameters: list[Any]
) -> str | None:
    if value is None:
        return None
    if "and" in value:
        parts = [cast(str, _compile_filter(item, lookup, parameters)) for item in value["and"]]
        return f"({' AND '.join(parts)})"
    if "or" in value:
        parts = [cast(str, _compile_filter(item, lookup, parameters)) for item in value["or"]]
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
        _aggregate_sql(entity, member, alias)
        if member.get("kind") == "measure"
        else _member_sql(entity, member, alias)
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
        aliases: dict[str, str] = {_entity_marker(root): "_e0"}
        joins: list[tuple[_Edge, str]] = []

        def add_path(entity: SemanticEntity, requested: list[str] | None = None) -> None:
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
        participating = [root, *(edge.target for edge, _alias in joins)]
        lookup: dict[str, tuple[SemanticEntity, dict[str, Any], str]] = {}
        ambiguous: set[str] = set()
        for entity in participating:
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
        for item in dimension_items:
            if item.member.get("kind") == "measure":
                _fail(
                    "type_check",
                    "not_a_dimension",
                    f"{item.member.get('member_id')!r} is a measure",
                )
            sql = _member_sql(item.entity, item.member, item.alias)
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
            groups.append(sql)
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
            selects.append(
                f"{_aggregate_sql(item.entity, item.member, item.alias)} AS {_quote_ident(name)}"
            )

        from_sql = f"FROM {_source_sql(root, query, parameters)} AS _e0"
        join_lines: list[str] = []
        for edge, alias in joins:
            definition = edge.relationship.definition
            left_alias = aliases[_entity_marker(edge.source)]
            pairs: list[str] = []
            for pair in definition.get("predicate", []):
                left_id = pair["from_member"] if edge.forward else pair["to_member"]
                right_id = pair["to_member"] if edge.forward else pair["from_member"]
                left = edge.source.members.get(str(left_id))
                right = edge.target.members.get(str(right_id))
                if left is None or right is None:
                    _fail(
                        "relationship_resolution",
                        "unresolved_relationship_member",
                        f"Relationship {edge.relationship.relationship_id!r} references "
                        "an unknown member",
                    )
                operator = (
                    "IS NOT DISTINCT FROM" if pair.get("nulls", "not_equal") == "equal" else "="
                )
                left_sql = _member_sql(edge.source, left, left_alias)
                right_sql = _member_sql(edge.target, right, alias)
                pairs.append(f"{left_sql} {operator} {right_sql}")
            cardinality = definition["to_cardinality" if edge.forward else "from_cardinality"]
            join_type = "INNER" if cardinality.get("min") == 1 else "LEFT"
            target_sql = _source_sql(edge.target, query, parameters)
            join_lines.append(f"{join_type} JOIN {target_sql} AS {alias} ON {' AND '.join(pairs)}")

        where = _compile_filter(query.get("filters"), lookup, parameters)
        having = _compile_filter(query.get("measure_filters"), lookup, parameters)
        filtered = [lookup.get(_filter_key(item)) for item in _filter_members(query.get("filters"))]
        for entity in participating:
            for group in _required_filters(catalogs, entity):
                locally_filtered = any(
                    member.get("column") == column
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
                if not locally_filtered and not argument_supplied:
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
                    "entities": list(aliases),
                }
            ],
            "sql": sql,
            "parameters": parameters,
            "validation_scope": "semantic",
            "warnings": warnings,
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
