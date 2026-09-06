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

_MAX_EXPANDED_MEMBERS = 500


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
    input_from_args: bool | None = None
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
        source_kind = "relation"
        if isinstance(host, Function):
            source_kind = (
                "table_function"
                if host.function_type.lower()
                in {"table", "table_function", "table_buffering", "table_macro"}
                else "unsupported_function"
            )
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
            for member in _expand_member_templates(packed, host.id, diagnostics):
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


def _expand_member_templates(
    values: list[Any], host: ObjectId, diagnostics: list[SemanticDiagnostic]
) -> list[dict[str, Any]]:
    """Expand bounded packed-member defaults into ordinary member definitions."""
    expanded: list[dict[str, Any]] = []
    template_ids: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        if "template_id" not in value:
            expanded.append(value)
            continue
        template_id = str(value.get("template_id", ""))
        if template_id in template_ids:
            diagnostics.append(
                SemanticDiagnostic(
                    host,
                    "duplicate_member_template",
                    f"member template {template_id!r} is declared more than once",
                )
            )
        template_ids.add(template_id)
        defaults = value.get("template")
        members = value.get("members")
        if not isinstance(defaults, dict) or not isinstance(members, list):
            continue
        for index, override in enumerate(members):
            if not isinstance(override, dict):
                continue
            member = {**defaults, **override}
            errors = validate_instance("member", member)
            if errors:
                diagnostics.append(
                    SemanticDiagnostic(
                        host,
                        "invalid_expanded_member",
                        f"member template {template_id!r} entry {index} is invalid: "
                        + "; ".join(errors),
                    )
                )
                continue
            expanded.append(member)
    if len(expanded) > _MAX_EXPANDED_MEMBERS:
        diagnostics.append(
            SemanticDiagnostic(
                host,
                "member_template_expansion_limit",
                f"packed semantic members expand to {len(expanded)} entries; "
                f"the limit is {_MAX_EXPANDED_MEMBERS}",
            )
        )
        return expanded[:_MAX_EXPANDED_MEMBERS]
    return expanded


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


def _unit_choice_key(value: Any) -> str:
    """Use JSON object-key spelling for non-string discovered choices."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _member_column_path(member: dict[str, Any]) -> list[str]:
    path = member.get("column_path")
    if isinstance(path, list):
        return [str(segment) for segment in path]
    column = member.get("column")
    return [str(column)] if column else []


def _split_struct_fields(value: str) -> list[str]:
    fields: list[str] = []
    start = 0
    depth = 0
    quoted = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == '"':
            if quoted and index + 1 < len(value) and value[index + 1] == '"':
                index += 1
            else:
                quoted = not quoted
        elif not quoted:
            if char in "(<[":
                depth += 1
            elif char in ")>]":
                depth -= 1
            elif char == "," and depth == 0:
                fields.append(value[start:index].strip())
                start = index + 1
        index += 1
    fields.append(value[start:].strip())
    return fields


def _struct_fields(data_type: str) -> dict[str, str] | None:
    stripped = data_type.strip()
    if not stripped.upper().startswith("STRUCT(") or not stripped.endswith(")"):
        return None
    result: dict[str, str] = {}
    for field_definition in _split_struct_fields(stripped[7:-1]):
        if field_definition.startswith('"'):
            end = 1
            while end < len(field_definition):
                if field_definition[end] == '"':
                    if end + 1 < len(field_definition) and field_definition[end + 1] == '"':
                        end += 2
                        continue
                    break
                end += 1
            if end >= len(field_definition):
                return None
            name = field_definition[1:end].replace('""', '"')
            child_type = field_definition[end + 1 :].strip()
        else:
            pieces = field_definition.split(None, 1)
            if len(pieces) != 2:
                return None
            name, child_type = pieces
        result[name] = child_type
    return result


def _resolve_nested_type(data_type: str, path: list[str]) -> str | None:
    current = data_type
    for segment in path:
        fields = _struct_fields(current)
        if fields is None or segment not in fields:
            return None
        current = fields[segment]
    return current


def _member_physical_type(entity: SemanticEntity, member: dict[str, Any]) -> str | None:
    explicit = member.get("output_type") or member.get("data_type")
    if explicit:
        return str(explicit)
    path = _member_column_path(member)
    if not path:
        return None
    data_type = entity.physical_column_types.get(path[0])
    if data_type is None or len(path) == 1:
        return data_type
    return _resolve_nested_type(data_type, path[1:])


def _normalized_type(value: str | None) -> str:
    raw = " ".join(str(value or "").strip().upper().split())
    return {
        "STRING": "VARCHAR",
        "TEXT": "VARCHAR",
        "INT": "INTEGER",
        "INT4": "INTEGER",
        "INT8": "BIGINT",
        "FLOAT": "REAL",
        "FLOAT8": "DOUBLE",
        "BOOL": "BOOLEAN",
    }.get(raw, raw)


def _types_compatible(source: str | None, target: str | None) -> bool:
    left, right = _normalized_type(source), _normalized_type(target)
    if not left or not right or left == "ANY" or right == "ANY":
        return True
    if left == right:
        return True
    numeric = ["TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "REAL", "DOUBLE"]
    return left in numeric and right in numeric


def _list_element_type(data_type: str, path: list[str]) -> str | None:
    value = data_type.strip()
    if value.endswith("[]"):
        element = value[:-2].strip()
    elif value.upper().startswith("LIST(") and value.endswith(")"):
        element = value[5:-1].strip()
    else:
        return None
    return _resolve_nested_type(element, path) if path else element


def _validate_unit_parameter(
    entity: SemanticEntity,
    member_id: str,
    member: dict[str, Any],
    source_arguments: list[Any],
    diagnostics: list[SemanticDiagnostic],
) -> None:
    definition = member.get("unit_parameter")
    if not isinstance(definition, dict):
        return
    argument_name = str(definition.get("argument", ""))
    matches = [argument for argument in entity.function_arguments if argument.name == argument_name]
    if not matches:
        diagnostics.append(
            SemanticDiagnostic(
                entity.host,
                "unit_parameter_argument_missing",
                f"member {member_id!r} unit_parameter references missing function argument "
                f"{argument_name!r}",
            )
        )
        return
    if len(matches) != 1 or entity.function_overload_count > 1:
        diagnostics.append(
            SemanticDiagnostic(
                entity.host,
                "unit_parameter_argument_ambiguous",
                f"member {member_id!r} unit_parameter argument {argument_name!r} is ambiguous",
            )
        )
        return
    mappings = [
        item
        for item in source_arguments
        if isinstance(item, dict) and item.get("argument") == argument_name
    ]
    if len(mappings) != 1:
        diagnostics.append(
            SemanticDiagnostic(
                entity.host,
                "unit_parameter_source_unmapped",
                f"member {member_id!r} unit_parameter argument {argument_name!r} must be "
                "exposed by exactly one semantic source-argument mapping",
            )
        )
    choices = matches[0].choices
    if choices is None:
        return
    try:
        decoded = json.loads(choices)
    except (TypeError, ValueError):
        decoded = None
    if not isinstance(decoded, list):
        diagnostics.append(
            SemanticDiagnostic(
                entity.host,
                "unit_parameter_choices_invalid",
                f"function argument {argument_name!r} advertises invalid JSON choices",
            )
        )
        return
    values = definition.get("values")
    if not isinstance(values, dict):
        return
    missing = sorted(
        _unit_choice_key(choice) for choice in decoded if _unit_choice_key(choice) not in values
    )
    if missing:
        diagnostics.append(
            SemanticDiagnostic(
                entity.host,
                "unit_parameter_choices_incomplete",
                f"member {member_id!r} unit mapping does not cover advertised choices {missing!r}",
            )
        )


def _validate_entity(entity: SemanticEntity, diagnostics: list[SemanticDiagnostic]) -> None:
    if entity.source_kind == "unsupported_function":
        diagnostics.append(
            SemanticDiagnostic(
                entity.host,
                "invalid_semantic_function_kind",
                "semantic entities on functions require a table function or table macro",
            )
        )
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
        _validate_unit_parameter(entity, member_id, member, arguments, diagnostics)
        source_argument = member.get("source_argument")
        if source_argument is not None:
            source_name = str(source_argument)
            source_matches = [
                argument for argument in entity.function_arguments if argument.name == source_name
            ]
            source_mappings = [
                item
                for item in arguments
                if isinstance(item, dict) and item.get("argument") == source_name
            ]
            if entity.source_kind != "table_function":
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "source_argument_member_on_relation",
                        f"member {member_id!r} uses source_argument on a relation",
                    )
                )
            elif not source_matches:
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "source_argument_member_missing",
                        f"member {member_id!r} references missing function argument "
                        f"{source_name!r}",
                    )
                )
            elif len(source_matches) != 1 or entity.function_overload_count > 1:
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "source_argument_member_ambiguous",
                        f"member {member_id!r} source argument {source_name!r} is ambiguous",
                    )
                )
            elif len(source_mappings) != 1:
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "source_argument_member_unmapped",
                        f"member {member_id!r} source argument {source_name!r} must be "
                        "exposed by exactly one semantic source-argument mapping",
                    )
                )
            declared_type = member.get("data_type") or member.get("output_type")
            if not declared_type:
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "source_argument_member_type_required",
                        f"member {member_id!r} backed by a source argument needs data_type "
                        "or output_type",
                    )
                )
            elif len(source_matches) == 1 and not _types_compatible(
                str(declared_type), source_matches[0].type
            ):
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "source_argument_member_type_mismatch",
                        f"member {member_id!r} type {declared_type!r} is incompatible with "
                        f"function argument {source_name!r} type {source_matches[0].type!r}",
                    )
                )
        if (
            member.get("kind") != "measure"
            and not _member_column_path(member)
            and not member.get("expression")
            and source_argument is None
        ):
            diagnostics.append(
                SemanticDiagnostic(
                    entity.host,
                    "missing_member_source",
                    f"member {member_id!r} needs a column or typed expression",
                )
            )
        column_path = _member_column_path(member)
        if column_path:
            if entity.physical_columns and column_path[0] not in entity.physical_columns:
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "unknown_physical_column",
                        f"member {member_id!r} references unknown column root {column_path[0]!r}",
                    )
                )
            elif (
                len(column_path) > 1
                and (root_type := entity.physical_column_types.get(column_path[0]))
                and _resolve_nested_type(root_type, column_path[1:]) is None
            ):
                diagnostics.append(
                    SemanticDiagnostic(
                        entity.host,
                        "unknown_physical_column_path",
                        f"member {member_id!r} references unknown nested field path "
                        f"{'.'.join(column_path)!r} in {root_type}",
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
                "operator": pair.get("operator", "equal"),
                "nulls": pair.get("nulls", "not_equal"),
                **(
                    {"from_element_path": pair["from_element_path"]}
                    if "from_element_path" in pair
                    else {}
                ),
                **(
                    {"to_element_path": pair["to_element_path"]}
                    if "to_element_path" in pair
                    else {}
                ),
            }
            for pair in value.get("predicate", [])
        ],
        key=lambda pair: (
            str(pair["from_member"]),
            str(pair["to_member"]),
            str(pair["operator"]),
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

    conditions = sorted(
        [
            {
                "side": item.get("side"),
                "member": item.get("member"),
                "operator": item.get("operator", "equal"),
                "value": item.get("value"),
            }
            for item in value.get("conditions", [])
        ],
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )

    direct = {
        "from": value.get("from"),
        "to": value.get("to"),
        "from_cardinality": cardinality(value.get("from_cardinality")),
        "to_cardinality": cardinality(value.get("to_cardinality")),
        "predicate": predicates,
        "conditions": conditions,
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
                    "operator": {
                        "spatial_contains": "spatial_within",
                        "spatial_within": "spatial_contains",
                    }.get(pair["operator"], pair["operator"]),
                    "nulls": pair["nulls"],
                    **(
                        {"to_element_path": pair["from_element_path"]}
                        if "from_element_path" in pair
                        else {}
                    ),
                    **(
                        {"from_element_path": pair["to_element_path"]}
                        if "to_element_path" in pair
                        else {}
                    ),
                }
                for pair in predicates
            ],
            key=lambda pair: (
                str(pair["from_member"]),
                str(pair["to_member"]),
                str(pair["operator"]),
                str(pair["nulls"]),
            ),
        ),
        "conditions": sorted(
            [
                {
                    **item,
                    "side": "to" if item["side"] == "from" else "from",
                }
                for item in conditions
            ],
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
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
            pair_members: list[dict[str, Any] | None] = []
            for index, member_field in ((0, "from_member"), (1, "to_member")):
                entity = endpoints[index]
                if entity is None:
                    pair_members.append(None)
                    continue
                member_id = str(pair.get(member_field, ""))
                member = entity.members.get(member_id)
                pair_members.append(member)
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
            operator = pair.get("operator", "equal")
            if None in endpoints or None in pair_members:
                continue
            from_entity, to_entity = endpoints
            from_member, to_member = pair_members
            assert from_entity is not None and to_entity is not None
            assert from_member is not None and to_member is not None
            types = [
                _member_physical_type(from_entity, from_member),
                _member_physical_type(to_entity, to_member),
            ]
            if str(operator).startswith("spatial_"):
                incompatible = [
                    data_type
                    for data_type in types
                    if data_type is not None and not data_type.upper().startswith("GEOMETRY")
                ]
                if incompatible:
                    diagnostics.append(
                        SemanticDiagnostic(
                            host,
                            "invalid_spatial_relationship_type",
                            f"spatial relationship members must be GEOMETRY, got {types!r}",
                        )
                    )
            elif operator == "list_contains":
                collection_index = 0 if "from_element_path" in pair else 1
                path = pair.get(
                    "from_element_path" if collection_index == 0 else "to_element_path", []
                )
                collection_type = types[collection_index]
                if (
                    collection_type is not None
                    and _list_element_type(collection_type, [str(item) for item in path]) is None
                ):
                    diagnostics.append(
                        SemanticDiagnostic(
                            host,
                            "invalid_list_relationship_type",
                            f"list_contains collection member must be a LIST with a valid "
                            f"element path, got {collection_type!r}",
                        )
                    )
        for condition in relationship.get("conditions", []):
            index = 0 if condition.get("side") == "from" else 1
            entity = endpoints[index]
            if entity is None:
                continue
            member_id = str(condition.get("member", ""))
            member = entity.members.get(member_id)
            if member is None:
                diagnostics.append(
                    SemanticDiagnostic(
                        host,
                        "unresolved_relationship_condition_member",
                        f"relationship condition references unknown member {member_id!r}",
                    )
                )
            elif (
                member.get("kind") not in {"identifier", "dimension", "time_dimension"}
                or "expression" in member
            ):
                diagnostics.append(
                    SemanticDiagnostic(
                        host,
                        "invalid_relationship_condition_member",
                        f"relationship condition member {member_id!r} must be a physical "
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
