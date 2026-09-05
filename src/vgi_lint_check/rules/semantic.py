"""VGI42x — semantic model tag validation."""

from __future__ import annotations

from collections.abc import Iterator

from ..findings import Category, Finding, Severity
from ..model import (
    TAG_SEMANTIC_CATALOG,
    TAG_SEMANTIC_ENTITY,
    TAG_SEMANTIC_MEMBER,
    TAG_SEMANTIC_MEMBERS,
    TAG_SEMANTIC_RELATIONSHIPS,
    ObjectKind,
)
from ..semantic_model import build_semantic_model, schema_diagnostics
from .base import Rule, RuleContext
from .registry import register


@register
class SemanticTagSchema(Rule):
    code = "VGI418"
    name = "semantic-tag-schema"
    category = Category.TAGS
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.COLUMN,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "Semantic JSON tags must conform to the published JSON Schemas."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for diagnostic in schema_diagnostics(ctx.catalog):
            yield self.finding(
                ctx,
                diagnostic.object_id,
                diagnostic.message,
                "update the JSON value to match `vgi-lint spec --schema ...`",
            )


@register
class SemanticModelConsistency(Rule):
    code = "VGI419"
    name = "semantic-model-consistency"
    category = Category.TAGS
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.COLUMN,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "Semantic identities, grains, members and relationships must resolve consistently."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        if schema_diagnostics(ctx.catalog):
            return
        for diagnostic in build_semantic_model(ctx.catalog).diagnostics:
            yield self.finding(
                ctx,
                diagnostic.object_id,
                diagnostic.message,
                f"correct semantic model invariant `{diagnostic.code}`",
            )


@register
class SemanticModelCompleteness(Rule):
    code = "VGI420"
    name = "semantic-model-completeness"
    category = Category.TAGS
    default_severity = Severity.WARNING
    targets = (ObjectKind.CATALOG, ObjectKind.TABLE, ObjectKind.VIEW, ObjectKind.TABLE_FUNCTION)
    summary = "Modeled entities and members should explain their business meaning."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        if schema_diagnostics(ctx.catalog):
            return
        model = build_semantic_model(ctx.catalog)
        for entity in model.entities.values():
            if not str(entity.definition.get("description", "")).strip():
                yield self.finding(
                    ctx,
                    entity.host,
                    f"semantic entity {entity.entity_id!r} has no description",
                    "describe the entity's business meaning and row grain",
                )
            for member_id, member in entity.members.items():
                if not str(member.get("description", "")).strip():
                    yield self.finding(
                        ctx,
                        entity.host,
                        f"semantic member {member_id!r} has no description",
                        "describe the member's business meaning for agents and developers",
                    )


@register
class SemanticTagScope(Rule):
    code = "VGI421"
    name = "semantic-tag-scope"
    category = Category.TAGS
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.COLUMN,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "Semantic tags must be carried by the object kinds that give them context."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        objects = [(ctx.catalog.id, ctx.catalog.tags)]
        objects.extend((schema.id, schema.tags) for schema in ctx.catalog.iter_schemas())
        for table in ctx.catalog.iter_table_like():
            objects.append((table.id, table.tags))
            objects.extend((column.id, column.tags) for column in table.columns)
        objects.extend(
            (function.id, function.tags) for function in ctx.catalog.iter_all_functions()
        )
        objects.extend(
            (column.id, column.tags)
            for function in ctx.catalog.iter_all_functions()
            for column in function.native_result_columns
        )
        allowed = {
            TAG_SEMANTIC_CATALOG: {ObjectKind.CATALOG},
            TAG_SEMANTIC_ENTITY: {
                ObjectKind.TABLE,
                ObjectKind.VIEW,
                ObjectKind.TABLE_FUNCTION,
            },
            TAG_SEMANTIC_MEMBERS: {
                ObjectKind.TABLE,
                ObjectKind.VIEW,
                ObjectKind.TABLE_FUNCTION,
            },
            TAG_SEMANTIC_MEMBER: {ObjectKind.COLUMN},
            TAG_SEMANTIC_RELATIONSHIPS: {
                ObjectKind.CATALOG,
                ObjectKind.TABLE,
                ObjectKind.VIEW,
                ObjectKind.TABLE_FUNCTION,
            },
        }
        for object_id, tags in objects:
            for key, kinds in allowed.items():
                if key in tags.raw and object_id.kind not in kinds:
                    yield self.finding(
                        ctx,
                        object_id,
                        f"semantic tag {key!r} is not valid on {object_id.kind}",
                        f"move {key!r} to one of: {', '.join(sorted(map(str, kinds)))}",
                    )
