"""VGI4xx — tag presence and validity."""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable, Iterator

from ..findings import Category, Finding, Severity
from ..model import (
    RESERVED_TAG_KEYS,
    TAG_CATEGORIES,
    TAG_CATEGORY,
    TAG_CLASSIFICATION_TAGS,
    TAG_REQUIRED_FILTERS,
    Catalog,
    ObjectId,
    ObjectKind,
    TagSet,
)
from ..tags import decode_string_list
from .base import Rule, RuleContext
from .registry import register

# Object kinds that may carry a primary vgi.category (everything but schema/catalog).
_CATEGORIZABLE_KINDS = (
    ObjectKind.TABLE,
    ObjectKind.VIEW,
    ObjectKind.SCALAR_FUNCTION,
    ObjectKind.AGGREGATE,
    ObjectKind.MACRO,
    ObjectKind.TABLE_FUNCTION,
)

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
class AgentTestTasksValid(Rule):
    code = "VGI407"
    name = "agent-test-tasks-valid"
    category = TAGS
    default_severity = Severity.ERROR
    targets = (ObjectKind.CATALOG,)
    summary = (
        "vgi.agent_test_tasks must be a JSON array of {name, prompt} task objects "
        "(see `vgi-lint simulate`)."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        err = ctx.catalog.agent_test_tasks_parse_error
        if err:
            yield self.finding(
                ctx,
                ctx.catalog.id,
                f"vgi.agent_test_tasks is not valid: {err}",
                'use a JSON array of {"name","prompt", "reference_sql"?, '
                '"success_criteria"?, "check_sql"?} task objects. Only "prompt" is '
                "shown to the analyst — reference_sql/success_criteria/check_sql are "
                "grader-only and must never leak into the prompt or any description",
            )


@register
class ClassificationTagsValid(Rule):
    code = "VGI406"
    name = "classification-tags-valid"
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
    summary = (
        "vgi.classification_tags must be a JSON array of strings, on any object except the catalog."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        # Reads through the canonical key, so a worker still emitting the deprecated
        # vgi.category_tags is validated here (and separately nudged by VGI405).
        for oid, tags in _all_objects(ctx.catalog):
            value = tags.get(TAG_CLASSIFICATION_TAGS)
            if value is None or not str(value).strip():
                continue
            if oid.kind is ObjectKind.CATALOG:
                yield self.finding(
                    ctx,
                    oid,
                    "vgi.classification_tags is not allowed on the catalog",
                    "classify the catalog's objects (schemas/tables/functions), "
                    "not the catalog itself — remove it here",
                )
                continue
            _items, err = decode_string_list(value)
            if err:
                yield self.finding(
                    ctx,
                    oid,
                    f"vgi.classification_tags is not valid: {err}",
                    'use a JSON array of strings, e.g. ["geospatial", "timeseries"]',
                )


def _validate_required_filters(value: str | None) -> str | None:
    """Return an error string if ``value`` is not valid CNF JSON, else None.

    Valid = a list of non-empty lists of non-empty strings (an AND of OR-groups
    of dotted column paths).
    """
    if value is None or not str(value).strip():
        return None
    try:
        data = json.loads(str(value))
    except (ValueError, TypeError) as e:
        return f"invalid JSON: {e}"
    if not isinstance(data, list):
        return f"expected a JSON array of arrays, got {type(data).__name__}"
    if not data:
        return "must not be empty"
    for group in data:
        if not isinstance(group, list):
            return "each group must be a JSON array (an OR-group of column paths)"
        if not group:
            return "must not contain empty groups"
        for path in group:
            if not isinstance(path, str):
                return "every path must be a string"
            if not path.strip():
                return "must not contain empty strings"
    return None


@register
class RequiredFiltersTagValid(Rule):
    code = "VGI415"
    name = "required-filters-tag-valid"
    category = TAGS
    default_severity = Severity.ERROR
    targets = (ObjectKind.TABLE, ObjectKind.VIEW)
    summary = (
        "The extension-injected vgi_required_filters tag must be a JSON array of "
        "non-empty arrays of non-empty strings (an AND of OR-groups)."
    )

    # Common near-miss spellings of the canonical key — a worker that hand-sets
    # one of these instead of the reserved name gets a nudge.
    _NEAR_MISS = (
        "vgi.required_filters",
        "required_filters",
        "requiredfilters",
        "required_field_filter_paths",
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for oid, tags in _all_objects(ctx.catalog):
            if oid.kind not in self.targets:
                continue
            if not tags.has(TAG_REQUIRED_FILTERS):
                # Tag-name check: a near-miss key used in place of the canonical one.
                for miss in self._NEAR_MISS:
                    if tags.has(miss):
                        yield self.finding(
                            ctx,
                            oid,
                            f"tag '{miss}' looks like a typo for '{TAG_REQUIRED_FILTERS}'",
                            f"the required-filter tag is named '{TAG_REQUIRED_FILTERS}'; it is "
                            "injected by the VGI extension from Table.required_filters — set the "
                            "declarative field rather than the tag directly",
                        )
                continue
            err = _validate_required_filters(tags.get(TAG_REQUIRED_FILTERS))
            if err:
                yield self.finding(
                    ctx,
                    oid,
                    f"{TAG_REQUIRED_FILTERS} is not valid: {err}",
                    "use a JSON array of non-empty arrays of non-empty strings, "
                    'e.g. [["accession_number"],["ticker","cik"]]',
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
                    "will stop being recognized in v1.0",
                )


@register
class RetiredTagKey(Rule):
    code = "VGI414"
    name = "retired-tag-key"
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
    summary = "Retired tag keys must be migrated — they are no longer recognized."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        objects: list[tuple[ObjectId, TagSet]] = [(cat.id, cat.tags)]
        for s in cat.iter_schemas():
            objects.append((s.id, s.tags))
        for t in cat.iter_table_like():
            objects.append((t.id, t.tags))
        for f in cat.iter_all_functions():
            objects.append((f.id, f.tags))
        for oid, tags in objects:
            for old, hint in tags.retired_keys().items():
                yield self.finding(
                    ctx,
                    oid,
                    f"tag {old!r} is retired and no longer read",
                    hint,
                )


# --- VGI408-412: the vgi.category / vgi.categories navigation layer ---------
#
# These are opt-in: a schema that neither declares a vgi.categories registry nor
# carries any object-level vgi.category produces no findings at all.


@register
class CategoriesRegistryValid(Rule):
    code = "VGI408"
    name = "categories-registry-valid"
    category = TAGS
    default_severity = Severity.ERROR
    targets = (ObjectKind.CATALOG, ObjectKind.SCHEMA)
    summary = (
        "vgi.categories must be a well-formed JSON registry on a schema (not the catalog), "
        "and vgi.category belongs on objects."
    )

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        cat = ctx.catalog
        # The registry and primary-category tags are misplaced on the catalog.
        if cat.tags.has(TAG_CATEGORIES):
            yield self.finding(
                ctx,
                cat.id,
                "vgi.categories is not allowed on the catalog",
                "declare the category registry on a schema, not the catalog",
            )
        if cat.tags.has(TAG_CATEGORY):
            yield self.finding(
                ctx,
                cat.id,
                "vgi.category is not allowed on the catalog",
                "vgi.category goes on tables/views/functions, not the catalog",
            )
        for s in cat.iter_schemas():
            if s.categories_parse_error:
                yield self.finding(
                    ctx,
                    s.id,
                    f"vgi.categories is not valid: {s.categories_parse_error}",
                    'use a JSON array of {"name", "description"?, "title"?} objects in '
                    "display order, each with a unique name",
                )
            if s.tags.has(TAG_CATEGORY):
                yield self.finding(
                    ctx,
                    s.id,
                    "vgi.category is not allowed on a schema",
                    "vgi.category goes on tables/views/functions; a schema declares its "
                    "categories via the vgi.categories registry",
                )


@register
class CategoryDefinedInRegistry(Rule):
    code = "VGI409"
    name = "category-defined-in-registry"
    category = TAGS
    default_severity = Severity.ERROR
    targets = _CATEGORIZABLE_KINDS
    summary = "An object's vgi.category must be a name defined in its schema's vgi.categories."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            names = [c.name for c in s.categories]
            nameset = set(names)
            for obj in s.iter_categorizable():
                raw = (obj.tags.get(TAG_CATEGORY) or "").strip()
                if not raw:
                    continue
                if raw.startswith("["):
                    yield self.finding(
                        ctx,
                        obj.id,
                        "vgi.category must be a single category name, not a list",
                        "use one primary category here; put cross-cutting facets in "
                        "vgi.classification_tags",
                    )
                    continue
                if raw in nameset:
                    continue
                if not s.categories:
                    yield self.finding(
                        ctx,
                        obj.id,
                        f"vgi.category {raw!r} but schema {s.name!r} declares no "
                        "vgi.categories registry",
                        f"add a vgi.categories registry to schema {s.name!r} that defines {raw!r}",
                    )
                    continue
                hint = difflib.get_close_matches(raw, names, n=1, cutoff=0.6)
                did = f"; did you mean {hint[0]!r}?" if hint else ""
                yield self.finding(
                    ctx,
                    obj.id,
                    f"vgi.category {raw!r} is not defined in schema {s.name!r}'s "
                    f"vgi.categories{did}",
                    f"use one of the schema's categories ({', '.join(names)}) or add "
                    f"{raw!r} to the registry",
                )


@register
class CategoryDescribed(Rule):
    code = "VGI410"
    name = "category-described"
    category = TAGS
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCHEMA,)
    summary = "Every category in a vgi.categories registry should carry a description."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            for c in s.categories:
                if not (c.description or "").strip():
                    yield self.finding(
                        ctx,
                        s.id,
                        f"category {c.name!r} has no description",
                        "add a one-line description so the category is a real, navigable "
                        "section — not an opaque label",
                    )


@register
class CategoryCoverage(Rule):
    code = "VGI411"
    name = "category-coverage"
    category = TAGS
    default_severity = Severity.WARNING
    targets = _CATEGORIZABLE_KINDS
    summary = "Objects in a schema that declares categories should each carry a vgi.category."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if not s.categories:  # only registry-bearing schemas expect full coverage
                continue
            names = ", ".join(c.name for c in s.categories)
            for obj in s.iter_categorizable():
                if not obj.category:
                    yield self.finding(
                        ctx,
                        obj.id,
                        "object is not assigned to a category",
                        f"add a vgi.category from schema {s.name!r}'s registry ({names})",
                    )


@register
class CategoryUnused(Rule):
    code = "VGI412"
    name = "category-empty"
    category = TAGS
    default_severity = Severity.ERROR
    targets = (ObjectKind.SCHEMA,)
    summary = "A category declared in vgi.categories must contain at least one member object."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if not s.categories:
                continue
            for c, objs in s.iter_by_category():
                if c is not None and not objs:
                    yield self.finding(
                        ctx,
                        s.id,
                        f"category {c.name!r} has no objects",
                        "an empty category is a dead navigation section (bad for listing/SEO) — "
                        "remove it, or assign objects to it via vgi.category",
                    )


@register
class SchemaCategoriesRequired(Rule):
    code = "VGI413"
    name = "schema-categories-required"
    category = TAGS
    default_severity = Severity.WARNING
    targets = (ObjectKind.SCHEMA,)
    summary = "Every schema with objects must declare a 'vgi.categories' registry (navigation/SEO)."

    def check(self, ctx: RuleContext) -> Iterator[Finding]:
        for s in ctx.catalog.iter_schemas():
            if s.categories or s.categories_parse_error:
                continue  # present (validated by VGI408/410/412) or malformed (VGI408)
            if not s.iter_categorizable():
                continue  # nothing to categorize
            yield self.finding(
                ctx,
                s.id,
                "schema declares no 'vgi.categories' registry",
                "add a 'vgi.categories' tag — an ordered JSON array of "
                '{"name","description"} category objects — then tag each table/view/'
                "function with a 'vgi.category' naming one of them. Categories drive the "
                "worker's navigation, listing sections, and SEO descriptions",
            )
