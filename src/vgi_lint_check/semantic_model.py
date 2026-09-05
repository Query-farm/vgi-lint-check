"""Decode and validate semantic metadata carried in DuckDB object tags.

JSON Schema owns local value shape. This module owns the checks that require
catalog context: carriers, IDs, grains, member references and relationships.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .model import (
    TAG_SEMANTIC_CATALOG,
    TAG_SEMANTIC_ENTITY,
    TAG_SEMANTIC_MEMBER,
    TAG_SEMANTIC_MEMBERS,
    TAG_SEMANTIC_RELATIONSHIPS,
    Argument,
    Catalog,
    Function,
    ObjectId,
    Table,
)
from .semantic_schema import validate_instance

SEMANTIC_TAG_SCHEMAS = {
    TAG_SEMANTIC_CATALOG: "catalog",
    TAG_SEMANTIC_ENTITY: "entity",
    TAG_SEMANTIC_MEMBER: "member",
    TAG_SEMANTIC_MEMBERS: "members",
    TAG_SEMANTIC_RELATIONSHIPS: "relationships",
}


@dataclass(frozen=True)
class SemanticDiagnostic:
    """One semantic-model issue anchored to its tag-bearing object."""

    object_id: ObjectId
    code: str
    message: str


@dataclass
class SemanticEntity:
    """Normalized entity with packed and native member carriers merged."""

    catalog_id: str
    entity_id: str
    host: ObjectId
    source_kind: str
    definition: dict[str, Any]
    function_parameters: list[str] = field(default_factory=list)
    function_arguments: list[Argument] = field(default_factory=list)
    function_overload_count: int = 1
    input_from_args: bool = False
    members: dict[str, dict[str, Any]] = field(default_factory=dict)
    physical_columns: set[str] = field(default_factory=set)
    physical_column_types: dict[str, str] = field(default_factory=dict)


@dataclass
class SemanticModel:
    """Normalized semantic model for one attached catalog."""

    attachment_alias: str
    catalog: dict[str, Any] | None
    entities: dict[tuple[str, str], SemanticEntity]
    relationships: dict[str, dict[str, Any]]
    diagnostics: list[SemanticDiagnostic]


def _decode(raw: str | None) -> Any | None:
    if raw is None or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def iter_semantic_tags(catalog: Catalog) -> list[tuple[ObjectId, str, str]]:
    """Return every semantic tag value with its host object."""
    values: list[tuple[ObjectId, str, str]] = []
    for key in SEMANTIC_TAG_SCHEMAS:
        raw = catalog.tags.get(key)
        if raw is not None:
            values.append((catalog.id, key, raw))
    for table in catalog.iter_table_like():
        for key in (TAG_SEMANTIC_ENTITY, TAG_SEMANTIC_MEMBERS, TAG_SEMANTIC_RELATIONSHIPS):
            raw = table.tags.get(key)
            if raw is not None:
                values.append((table.id, key, raw))
        for column in table.columns:
            raw = column.tags.get(TAG_SEMANTIC_MEMBER)
            if raw is not None:
                values.append((column.id, TAG_SEMANTIC_MEMBER, raw))
    for function in catalog.iter_all_functions():
        for key in (TAG_SEMANTIC_ENTITY, TAG_SEMANTIC_MEMBERS, TAG_SEMANTIC_RELATIONSHIPS):
            raw = function.tags.get(key)
            if raw is not None:
                values.append((function.id, key, raw))
        for column in function.native_result_columns:
            raw = column.tags.get(TAG_SEMANTIC_MEMBER)
            if raw is not None:
                values.append((column.id, TAG_SEMANTIC_MEMBER, raw))
    return values


def schema_diagnostics(catalog: Catalog) -> list[SemanticDiagnostic]:
    """Validate every semantic tag against its canonical JSON Schema."""
    diagnostics: list[SemanticDiagnostic] = []
    for host, key, raw in iter_semantic_tags(catalog):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            diagnostics.append(SemanticDiagnostic(host, "invalid_json", f"{key}: {exc}"))
            continue
        for message in validate_instance(SEMANTIC_TAG_SCHEMAS[key], value):
            diagnostics.append(SemanticDiagnostic(host, "schema", f"{key} {message}"))
    return diagnostics


def build_semantic_model(catalog: Catalog) -> SemanticModel:
    """Build one catalog model and report cross-object semantic errors."""
    diagnostics: list[SemanticDiagnostic] = []
    identity = _decode(catalog.tags.get(TAG_SEMANTIC_CATALOG))
    semantic_tags = iter_semantic_tags(catalog)
    if semantic_tags and not isinstance(identity, dict):
        diagnostics.append(
            SemanticDiagnostic(
                catalog.id,
                "missing_catalog_identity",
                "semantic metadata requires catalog tag 'vgi.semantic_catalog'",
            )
        )
        catalog_id = ""
    else:
        catalog_id = str((identity or {}).get("catalog_id", ""))

    entities: dict[tuple[str, str], SemanticEntity] = {}
    relation_entity_ids = {
        (host.schema, host.name, str(definition.get("entity_id", "")))
        for host in catalog.iter_table_like()
        if isinstance((definition := _decode(host.tags.get(TAG_SEMANTIC_ENTITY))), dict)
    }
    functions = []
    function_overload_counts: dict[tuple[str, str, str], int] = {}
    for function in catalog.iter_all_functions():
        overload_key = (function.schema, function.name, function.function_type)
        function_overload_counts[overload_key] = function_overload_counts.get(overload_key, 0) + 1
    for function in catalog.iter_all_functions():
        definition = _decode(function.tags.get(TAG_SEMANTIC_ENTITY))
        function_identity = (
            function.schema,
            function.name,
            str(definition.get("entity_id", "")) if isinstance(definition, dict) else "",
        )
        if function_identity not in relation_entity_ids:
            functions.append(function)
    hosts: list[Any] = [*catalog.iter_table_like(), *functions]
    for host in hosts:
        entity_def = _decode(host.tags.get(TAG_SEMANTIC_ENTITY))
        if not isinstance(entity_def, dict):
            continue
        entity_id = str(entity_def.get("entity_id", ""))
        key = (catalog_id, entity_id)
        if key in entities:
            diagnostics.append(
                SemanticDiagnostic(
                    host.id, "duplicate_entity", f"duplicate entity_id {entity_id!r}"
                )
            )
            continue
        source_kind = "table_function" if hasattr(host, "function_type") else "relation"
        entity = SemanticEntity(catalog_id, entity_id, host.id, source_kind, entity_def)
        if isinstance(host, Function):
            entity.function_parameters = list(host.parameters)
            entity.function_arguments = list(host.arguments)
            entity.function_overload_count = function_overload_counts[
                (host.schema, host.name, host.function_type)
            ]
            entity.input_from_args = host.input_from_args
        if isinstance(host, Table):
            entity.physical_columns = {column.name for column in host.columns}
            entity.physical_column_types = {
                column.name: str(column.data_type)
                for column in host.columns
                if column.data_type is not None
            }
        else:
            entity.physical_columns = {
                str(column.name) for column in host.native_result_columns
            } or {str(column.name) for column in host.result_columns if column.name is not None}
            result_columns = host.native_result_columns or host.result_columns
            entity.physical_column_types = {
                str(column.name): str(
                    getattr(column, "data_type", None) or getattr(column, "type", None)
                )
                for column in result_columns
                if column.name is not None
                and (getattr(column, "data_type", None) or getattr(column, "type", None))
            }
        packed = _decode(host.tags.get(TAG_SEMANTIC_MEMBERS))
        if isinstance(packed, list):
            for member in packed:
                if isinstance(member, dict):
                    _add_member(entity, member, host.id, diagnostics)
        if isinstance(host, Table):
            for column in host.columns:
                native = _decode(column.tags.get(TAG_SEMANTIC_MEMBER))
                if not isinstance(native, dict):
                    continue
                member = dict(native)
                member.setdefault("column", column.name)
                _add_member(entity, member, column.id, diagnostics)
        else:
            for column in host.native_result_columns:
                native = _decode(column.tags.get(TAG_SEMANTIC_MEMBER))
                if not isinstance(native, dict):
                    continue
                member = dict(native)
                member.setdefault("column", column.name)
                _add_member(entity, member, column.id, diagnostics)
        entities[key] = entity

    for entity in entities.values():
        _validate_entity(entity, diagnostics)

    relationships: dict[str, dict[str, Any]] = {}
    for host, tag_key, raw in semantic_tags:
        if tag_key != TAG_SEMANTIC_RELATIONSHIPS:
            continue
        decoded = _decode(raw)
        if not isinstance(decoded, list):
            continue
        for relationship in decoded:
            if not isinstance(relationship, dict):
                continue
            relationship_id = str(relationship.get("relationship_id", ""))
            previous = relationships.get(relationship_id)
            normalized = _normalize_relationship(relationship)
            if previous is None:
                relationships[relationship_id] = {**relationship, "_host": host}
            elif _normalize_relationship(previous) != normalized:
                diagnostics.append(
                    SemanticDiagnostic(
                        host,
                        "relationship_conflict",
                        f"relationship_id {relationship_id!r} has incompatible declarations",
                    )
                )

    _validate_relationships(catalog_id, entities, relationships, diagnostics)
    _warn_duplicate_relationships(relationships, diagnostics)
    return SemanticModel(catalog.database, identity, entities, relationships, diagnostics)


def _add_member(
    entity: SemanticEntity,
    member: dict[str, Any],
    host: ObjectId,
    diagnostics: list[SemanticDiagnostic],
) -> None:
    member_id = str(member.get("member_id", ""))
    previous = entity.members.get(member_id)
    if previous is not None and previous != member:
        diagnostics.append(
            SemanticDiagnostic(
                host,
                "member_carrier_conflict",
                f"member {member_id!r} differs between packed and native column carriers",
            )
        )
    elif previous is None:
        entity.members[member_id] = member


def _expression_refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        refs = {str(value["member"])} if value.get("op") == "member" else set()
        for child in value.values():
            refs.update(_expression_refs(child))
        return refs
    if isinstance(value, list):
        list_refs: set[str] = set()
        for child in value:
            list_refs.update(_expression_refs(child))
        return list_refs
    return set()


def _validate_entity(entity: SemanticEntity, diagnostics: list[SemanticDiagnostic]) -> None:
    source = entity.definition.get("source", {})
    arguments = source.get("arguments", []) if isinstance(source, dict) else []
    if arguments and entity.source_kind != "table_function":
        diagnostics.append(
            SemanticDiagnostic(
                entity.host,
                "relation_source_arguments",
                "only table-function entities may declare source arguments",
            )
        )
    if isinstance(arguments, list):
        argument_names = [
            str(item.get("argument", "")) for item in arguments if isinstance(item, dict)
        ]
        parameter_names = [
            str(item.get("parameter", "")) for item in arguments if isinstance(item, dict)
        ]
        if len(argument_names) != len(set(argument_names)):
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "duplicate_source_argument",
                    "a table-function source argument is mapped more than once",
                )
            )
        if len(parameter_names) != len(set(parameter_names)):
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "duplicate_source_parameter",
                    "a semantic source parameter is mapped more than once",
                )
            )
        declared_names = {argument.name for argument in entity.function_arguments}
        unknown_arguments = sorted(set(argument_names) - declared_names)
        if (arguments or entity.function_parameters) and not entity.function_arguments:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "missing_function_argument_metadata",
                    "table-function source arguments require vgi_function_arguments() metadata",
                )
            )
        elif unknown_arguments:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "unknown_source_argument",
                    f"source mappings reference unknown function arguments {unknown_arguments!r}",
                )
            )
        field_indexes = [
            argument.field_index
            for argument in entity.function_arguments
            if argument.field_index is not None
        ]
        if entity.function_overload_count > 1 or len(field_indexes) != len(set(field_indexes)):
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "ambiguous_function_overload",
                    "table-function argument metadata contains multiple overload signatures",
                )
            )
        if any(argument.is_varargs for argument in entity.function_arguments):
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "unsupported_function_varargs",
                    "semantic table-function sources do not support varargs",
                )
            )
        matches_by_name: dict[str, list[Argument]] = {}
        for argument in entity.function_arguments:
            matches_by_name.setdefault(argument.name, []).append(argument)
        if any(len(matches_by_name.get(name, [])) > 1 for name in argument_names):
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "ambiguous_source_argument",
                    "a source mapping resolves to more than one physical function argument",
                )
            )
        mapped_names = set(argument_names)
        unsupported = sorted(
            argument.name for argument in entity.function_arguments if argument.is_table_input
        )
        if unsupported:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "unsupported_table_input",
                    f"semantic table-function sources cannot accept table inputs {unsupported!r}",
                )
            )
        invalid_call_kinds = sorted(
            argument.name
            for argument in entity.function_arguments
            if argument.name in mapped_names
            and argument.is_named == argument.is_positional
            and not argument.is_varargs
        )
        if invalid_call_kinds:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "invalid_source_argument_kind",
                    "source arguments must resolve to exactly one of named or positional: "
                    f"{invalid_call_kinds!r}",
                )
            )
        missing_positions = sorted(
            argument.name
            for argument in entity.function_arguments
            if argument.name in mapped_names
            and argument.is_positional
            and argument.position is None
        )
        if missing_positions:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "missing_source_argument_position",
                    f"positional source arguments have no arg_position {missing_positions!r}",
                )
            )
        optional_without_default = sorted(
            str(item.get("argument", ""))
            for item in arguments
            if isinstance(item, dict)
            and item.get("required", True) is False
            and len(matches_by_name.get(str(item.get("argument", "")), [])) == 1
            and matches_by_name[str(item.get("argument", ""))][0].default is None
        )
        if optional_without_default:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "optional_source_argument_without_default",
                    "optional semantic source mappings require physical defaults: "
                    f"{optional_without_default!r}",
                )
            )
        unmapped_required = sorted(
            argument.name
            for argument in entity.function_arguments
            if argument.name not in mapped_names
            and not argument.is_varargs
            and not argument.is_table_input
            and argument.default is None
        )
        if unmapped_required:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "unmapped_required_source_argument",
                    f"required function arguments lack semantic mappings {unmapped_required!r}",
                )
            )
    grain = entity.definition.get("grain", [])
    for member_id in grain if isinstance(grain, list) else []:
        member = entity.members.get(str(member_id))
        if member is None:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "unknown_grain_member",
                    f"grain references unknown member {member_id!r}",
                )
            )
        elif member.get("kind") != "identifier":
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "invalid_grain_member",
                    f"grain member {member_id!r} must be an identifier",
                )
            )
    default_time = entity.definition.get("default_time_dimension")
    if (
        default_time is not None
        and entity.members.get(str(default_time), {}).get("kind") != "time_dimension"
    ):
        diagnostics.append(
            SemanticDiagnostic(
                entity.host,
                "invalid_default_time",
                f"default_time_dimension {default_time!r} is not a time dimension",
            )
        )
    for member_id, member in entity.members.items():
        if (
            member.get("kind") != "measure"
            and not member.get("column")
            and not member.get("expression")
        ):
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "missing_member_source",
                    f"member {member_id!r} needs a column or typed expression",
                )
            )
        column = member.get("column")
        if column and entity.physical_columns and str(column) not in entity.physical_columns:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "unknown_physical_column",
                    f"member {member_id!r} references unknown column {column!r}",
                )
            )
        refs = _expression_refs(member.get("expression"))
        unknown = sorted(refs - entity.members.keys())
        if unknown:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "unknown_member_reference",
                    f"member {member_id!r} references unknown members {unknown!r}",
                )
            )
        if (
            member.get("kind") == "measure"
            and member.get("aggregation") not in (None, "count_rows")
            and not member.get("member")
        ):
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "missing_measure_input",
                    f"measure {member_id!r} requires an input member",
                )
            )
        source = member.get("member")
        if source is not None and str(source) not in entity.members:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "unknown_measure_input",
                    f"measure {member_id!r} references unknown input {source!r}",
                )
            )
        if member.get("kind") == "measure":
            aggregation = member.get("aggregation")
            inherently_non_additive = member.get("expression") is not None or aggregation in {
                "count_distinct",
                "avg",
                "min",
                "max",
            }
            if inherently_non_additive and member.get("additivity") != "non_additive":
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "invalid_measure_additivity",
                        f"measure {member_id!r} must be non_additive for "
                        f"{aggregation or 'a derived expression'}",
                    )
                )
    dependencies = {
        member_id: _expression_refs(member.get("expression"))
        for member_id, member in entity.members.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(member_id: str) -> None:
        if member_id in visiting:
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "expression_cycle",
                    f"semantic expression cycle includes member {member_id!r}",
                )
            )
            return
        if member_id in visited:
            return
        visiting.add(member_id)
        for dependency in dependencies.get(member_id, set()):
            if dependency in dependencies:
                visit(dependency)
        visiting.remove(member_id)
        visited.add(member_id)

    for member_id in dependencies:
        visit(member_id)


def _normalize_relationship(value: dict[str, Any]) -> dict[str, Any]:
    predicates: list[dict[str, Any]] = sorted(
        [
            {
                "from_member": pair.get("from_member"),
                "to_member": pair.get("to_member"),
                "nulls": pair.get("nulls", "not_equal"),
            }
            for pair in value.get("predicate", [])
        ],
        key=lambda pair: (
            str(pair["from_member"]),
            str(pair["to_member"]),
            str(pair["nulls"]),
        ),
    )

    def cardinality(raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        return {
            **raw,
            **({"roles": sorted(raw["roles"])} if isinstance(raw.get("roles"), list) else {}),
        }

    direct = {
        "from": value.get("from"),
        "to": value.get("to"),
        "from_cardinality": cardinality(value.get("from_cardinality")),
        "to_cardinality": cardinality(value.get("to_cardinality")),
        "predicate": predicates,
    }
    reversed_value = {
        "from": direct["to"],
        "to": direct["from"],
        "from_cardinality": direct["to_cardinality"],
        "to_cardinality": direct["from_cardinality"],
        "predicate": sorted(
            [
                {
                    "from_member": pair["to_member"],
                    "to_member": pair["from_member"],
                    "nulls": pair["nulls"],
                }
                for pair in predicates
            ],
            key=lambda pair: (
                str(pair["from_member"]),
                str(pair["to_member"]),
                str(pair["nulls"]),
            ),
        ),
    }
    direct_json = json.dumps(direct, sort_keys=True, separators=(",", ":"))
    reversed_json = json.dumps(reversed_value, sort_keys=True, separators=(",", ":"))
    return direct if direct_json <= reversed_json else reversed_value


def _validate_relationships(
    catalog_id: str,
    entities: dict[tuple[str, str], SemanticEntity],
    relationships: dict[str, dict[str, Any]],
    diagnostics: list[SemanticDiagnostic],
) -> None:
    for relationship in relationships.values():
        host = relationship.get("_host")
        if not isinstance(host, ObjectId):
            continue
        endpoints: list[SemanticEntity | None] = []
        for side in ("from", "to"):
            ref = relationship.get(side, {})
            key = (str(ref.get("catalog_id", "")), str(ref.get("entity_id", "")))
            entity = entities.get(key)
            endpoints.append(entity)
            if key[0] == catalog_id and entity is None:
                diagnostics.append(
                    SemanticDiagnostic(
                        host,
                        "unresolved_local_entity",
                        f"relationship {side} endpoint {key!r} does not exist",
                    )
                )
        for pair in relationship.get("predicate", []):
            for index, member_field in ((0, "from_member"), (1, "to_member")):
                entity = endpoints[index]
                if entity is None:
                    continue
                member_id = str(pair.get(member_field, ""))
                member = entity.members.get(member_id)
                if member is None:
                    diagnostics.append(
                        SemanticDiagnostic(
                            host,
                            "unresolved_relationship_member",
                            f"relationship references unknown {member_field} {member_id!r}",
                        )
                    )
                elif (
                    member.get("kind") not in {"identifier", "dimension", "time_dimension"}
                    or "expression" in member
                ):
                    diagnostics.append(
                        SemanticDiagnostic(
                            host,
                            "invalid_relationship_key",
                            f"relationship key {member_id!r} must be a physical "
                            "identifier/dimension",
                        )
                    )


def _warn_duplicate_relationships(
    relationships: dict[str, dict[str, Any]], diagnostics: list[SemanticDiagnostic]
) -> None:
    structures: dict[str, str] = {}
    for relationship_id, relationship in relationships.items():
        structure = json.dumps(
            _normalize_relationship(relationship), sort_keys=True, separators=(",", ":")
        )
        previous = structures.get(structure)
        if previous is not None:
            diagnostics.append(
                SemanticDiagnostic(
                    relationship["_host"],
                    "duplicate_relationship_candidate",
                    f"relationships {previous!r} and {relationship_id!r} describe the same edge",
                )
            )
        else:
            structures[structure] = relationship_id
