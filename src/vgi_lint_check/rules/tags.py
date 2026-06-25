"""VGI4xx — tag presence and validity."""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Iterator

from ..findings import Category, Finding, Severity
from ..model import RESERVED_TAG_KEYS, TAG_CATEGORY_TAGS, Catalog, ObjectId, ObjectKind, TagSet
from ..tags import decode_string_list
from .base import Rule, RuleContext
from .registry import register

TAGS = Category.TAGS
# The framework owns the ``vgi.`` namespace; an unknown key in it is a mistake.
_RESERVED_SORTED = sorted(RESERVED_TAG_KEYS)


def _tagged_objects(catalog: Catalog) -> Iterator[tuple[ObjectId, TagSet]]:
    """Yield (object_id, TagSet) for every object that carries tags."""
    for s in catalog.iter_schemas():
        yield s.id, s.tags
    for t in catalog.iter_table_like():
        yield t.id, t.tags
    for f in catalog.iter_functions():
        yield f.id, f.tags


def _all_objects(catalog: Catalog) -> Iterator[tuple[ObjectId, TagSet]]:
    """(id, tags) for every object including the catalog and table-functions."""
    yield catalog.id, catalog.tags
    for s in catalog.iter_schemas():
        yield s.id, s.tags
    for t in catalog.iter_table_like():
        yield t.id, t.tags
    for f in catalog.iter_all_functions():
        yield f.id, f.tags


@register
class RequiredTags(Rule):
    code = "VGI401"
    name = "required-tags"
    category = TAGS
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCHEMA, ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "Schemas/tables must carry the configured required tag keys."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        schema_req = ctx.config.options.required_schema_tags
        table_req = ctx.config.options.required_table_tags
        for s in ctx.catalog.iter_schemas():
            yield from self._missing(ctx, s.id, s.tags, schema_req, "schema")
        for t in ctx.catalog.iter_table_like():
            yield from self._missing(ctx, t.id, t.tags, table_req, str(t.id.kind))

    def _missing(
        self,
        ctx: RuleContext,
        oid: ObjectId,
        tags: TagSet,
        required: Iterable[str],
        kind: str,
    ) -> Iterator[Finding]:
        for key in required:
            if not tags.has(key):
                yield self.finding(
                    ctx,
                    oid,
                    f"{kind} missing required tag {key!r}",
                    f"add a {key!r} tag",
                )


@register
class ReservedTagNotEmpty(Rule):
    code = "VGI402"
    name = "reserved-tag-not-empty"
    category = TAGS
    default_severity = Severity.WARNING
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "A reserved vgi.* tag must not be present with an empty value."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags in _tagged_objects(ctx.catalog):
            for key in RESERVED_TAG_KEYS:
                if key in tags.raw and not tags.has(key):
                    yield self.finding(
                        ctx,
                        oid,
                        f"reserved tag {key!r} is present but empty",
                        f"give {key!r} a value or remove the tag",
                    )


@register
class UnknownVgiTagKey(Rule):
    code = "VGI404"
    name = "unknown-vgi-tag-key"
    category = TAGS
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "A 'vgi.*' tag key that isn't a recognized reserved key is likely a typo."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags in _all_objects(ctx.catalog):
            for key in tags.raw:
                # Only the reserved vgi.* namespace — non-vgi keys are user
                # extensibility (provider/domain/...) and are VGI403's concern.
                if not key.startswith("vgi.") or key in RESERVED_TAG_KEYS:
                    continue
                near = difflib.get_close_matches(key, _RESERVED_SORTED, n=1, cutoff=0.6)
                hint = (
                    f"did you mean {near[0]!r}? " if near else "use a recognized reserved key, or "
                )
                yield self.finding(
                    ctx,
                    oid,
                    f"unknown reserved-namespace tag key {key!r}",
                    f"{hint}the 'vgi.' namespace is reserved — move custom metadata "
                    "to an unprefixed key (e.g. 'team', 'domain')",
                )


@register
class UnknownTagKey(Rule):
    code = "VGI403"
    name = "unknown-tag-key"
    category = TAGS
    default_severity = Severity.INFO
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = "When an allow-list is configured, flag tag keys outside it."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        allowed = set(ctx.config.options.allowed_tag_keys)
        if not allowed:
            return
        allowed |= RESERVED_TAG_KEYS
        for oid, tags in _tagged_objects(ctx.catalog):
            for key in tags.raw:
                if key not in allowed:
                    yield self.finding(
                        ctx,
                        oid,
                        f"unknown tag key {key!r}",
                        "remove the tag or add the key to allowed_tag_keys",
                    )


@register
class CategoryTagsValid(Rule):
    code = "VGI406"
    name = "category-tags-valid"
    category = TAGS
    default_severity = Severity.ERROR
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = "vgi.category_tags must be a JSON array of strings, on any object except the catalog."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags in _all_objects(ctx.catalog):
            value = tags.get(TAG_CATEGORY_TAGS)
            if value is None or not str(value).strip():
                continue
            if oid.kind is ObjectKind.CATALOG:
                yield self.finding(
                    ctx,
                    oid,
                    "vgi.category_tags is not allowed on the catalog",
                    "categorize the catalog's objects (schemas/tables/functions), "
                    "not the catalog itself — remove it here",
                )
                continue
            _items, err = decode_string_list(value)
            if err:
                yield self.finding(
                    ctx,
                    oid,
                    f"vgi.category_tags is not valid: {err}",
                    'use a JSON array of strings, e.g. ["geospatial", "timeseries"]',
                )


@register
class DeprecatedTagKey(Rule):
    code = "VGI405"
    name = "deprecated-tag-key"
    category = TAGS
    default_severity = Severity.WARNING
    targets = (
        ObjectKind.CATALOG,
        ObjectKind.SCHEMA,
        ObjectKind.TABLE,
        ObjectKind.VIEW,
        ObjectKind.SCALAR_FUNCTION,
        ObjectKind.AGGREGATE,
        ObjectKind.MACRO,
        ObjectKind.TABLE_FUNCTION,
    )
    summary = (
        "Migrate deprecated tag keys (e.g. vgi.description_md -> vgi.doc_md) to the new names."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        objects: list[tuple[ObjectId, TagSet]] = [(cat.id, cat.tags)]
        for s in cat.iter_schemas():
            objects.append((s.id, s.tags))
        for t in cat.iter_table_like():
            objects.append((t.id, t.tags))
        for f in cat.iter_all_functions():  # include table-functions (columns_md)
            objects.append((f.id, f.tags))
        for oid, tags in objects:
            for old, new in tags.deprecated_keys().items():
                yield self.finding(
                    ctx,
                    oid,
                    f"tag {old!r} is deprecated",
                    f"rename the tag to {new!r} — the old key still works for now but "
                    "will stop being recognized in a future version",
                )
