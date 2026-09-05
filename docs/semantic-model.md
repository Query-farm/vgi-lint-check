# VGI semantic model

This document is the normative human-readable companion to the JSON Schemas in
`src/vgi_lint_check/schema/semantic`. The schemas define JSON shape. This document defines
meaning, resolution and compiler behavior. VGI publishes the latest contract; workers do not
select or pin a semantic-contract version.

## Storage and carriers

DuckDB tags are `MAP(VARCHAR, VARCHAR)`, so every semantic value is serialized JSON. The
reserved keys are:

| Key | Host | Meaning |
| --- | --- | --- |
| `vgi.semantic_catalog` | catalog | Stable logical identity and runtime binding hints |
| `vgi.semantic_entity` | table, view, table function | Entity identity, grain and source arguments |
| `vgi.semantic_members` | table, view, table function | Packed member array, available on DuckDB 1.5 |
| `vgi.semantic_member` | column | Native member metadata, available when DuckDB exposes column tags |
| `vgi.semantic_relationships` | catalog, entity host | Relationship assertions |

Consumers must feature-detect whether `duckdb_columns()` returns `tags`; they must not branch on
a DuckDB version string. When a packed and native carrier define the same member, identical values
deduplicate and incompatible values are an error.

## Identity and attachment

`catalog_id` is the stable logical identity of a model provider. An attached DuckDB database name
is only a runtime alias and must never appear in relationship endpoints. `catalog_instance_id`
optionally distinguishes tenant, snapshot or account instances of the same logical catalog.
`binding_key` gives callers a stable key for an explicit runtime binding.

A query resolves `catalog_id` to the attached alias automatically only when there is one candidate.
Zero candidates is unresolved. Multiple candidates is ambiguous until the request supplies a
`binding_key -> attachment alias` binding. Attaching one instance twice is still ambiguous.

An assertion stored on an endpoint catalog anchors that endpoint to the hosting attachment. This
prevents an assertion from silently joining one tenant's fact table to another tenant's dimension.
A federation catalog may assert a relationship between two catalogs it does not own.

## Entities and members

An entity has a globally meaningful `(catalog_id, entity_id)` identity and an explicit grain. Every
grain member must be a physical `identifier`. Members have stable IDs independent of physical
column names:

- `identifier`: a key that may participate in grain or relationships.
- `dimension`: a physical or calculated grouping/filtering attribute.
- `time_dimension`: a dimension with an explicit timezone and supported granularities. Weeks begin
  on Monday.
- `measure`: an aggregate or a derived expression over measures.

Base measures support `count_rows`, `count`, `count_distinct`, `sum`, `min`, `max` and `avg`.
Derived members use the typed expression AST; raw SQL, arbitrary functions, windows and nested
aggregates are not part of the contract. `output_type` is optional and means an enforced DuckDB
`CAST`; it is not descriptive metadata. Otherwise the compiler infers the type.

Additivity is `additive`, `non_additive`, or `semi_additive` with prohibited dimensions. An explicit
annotation may only be more restrictive than what an aggregation implies.

Parameterized root table functions map source argument names to semantic query parameters. A table
function reached through a relationship must initially be zero-argument; correlated/per-row table
functions are not supported. An absent optional mapping is omitted from the call so the function's
own default remains effective.

The semantic tag does not declare whether an argument is positional or named. The compiler resolves
each `source.arguments[].argument` against the table function's live
`vgi_function_arguments()` rows. It emits positional bindings as `?`, ordered by `arg_position`,
then emits named bindings as `"argument" := ?`, ordered by `field_index`. Consequently, changing a
worker's physical signature cannot leave a second calling-convention declaration stale in its tags.

Compilation fails closed when argument metadata is unavailable, a mapping does not resolve exactly
once, overload rows make the signature ambiguous, the function uses varargs or a table input, or a
request would omit an earlier optional positional argument while supplying a later one. Every
required physical argument must have a semantic mapping. A mapping marked `required: false` is only
valid when the physical argument has a default. These restrictions can be relaxed later without
changing the tag representation.

## Relationships and federation

A relationship names two stable entity references, directional cardinality, optional role names,
and one or more equality pairs. Predicate pairs are ANDed. `nulls: "not_equal"` compiles to `=`;
`nulls: "equal"` compiles to `IS NOT DISTINCT FROM`. Temporal, spatial and raw-SQL predicates are
outside the initial contract; publish a normalized view or bridge entity instead.

One assertion is navigable in both directions. Reciprocal declarations using the same globally
namespaced `relationship_id` merge when reversing endpoints, cardinalities and predicate pairs makes
them structurally equal. A mismatch is a conflict. Structurally equal declarations with different
IDs remain separate and produce a duplicate-candidate warning. There is no tag-controlled override
or `supersedes`: a provider cannot grant its own assertion extra authority.

Resolution and trust are separate:

- `resolution_status`: `resolved`, `unresolved`, `ambiguous`, `conflicted` or `unavailable`.
- `attestation`: `unilateral`, `corroborated` or `third_party`.

Corroboration means both endpoint providers independently published the compatible assertion. It
does not mean the relationship is certified. Physical foreign keys are evidence and UI affordances,
not semantic assertions.

Directional cardinality uses `{min: 0|1, max: 1|"many"}` on each endpoint. Required to-one
traversal compiles to `INNER JOIN`; optional to-one traversal compiles to `LEFT JOIN`. Many-to-many
models use a bridge entity and two relationships.

## Query compiler

`query_semantic_model` accepts fully qualified measure and dimension references. It compiles and
executes by default. `compile_only: true` returns the plan and SQL and performs no DuckDB prepare,
bind, `EXPLAIN`, execution or cache operation. Its validation scope is `semantic`.

The initial compiler is single-root-grain: every selected measure must belong to one root entity.
Cross-catalog to-one dimension enrichment is supported. A traversal into a `many` endpoint is
rejected for every aggregation, including `count_distinct`; the compiler never hides fanout with
`DISTINCT` or implicit pre-aggregation. Multi-root requests return `multi_fact_not_supported`.

The plan IR always contains a `fact_branches` array with exactly one branch today. A future
multi-fact compiler can independently aggregate branches, require conformed dimensions/time
semantics, full-outer-join at a common grain, apply an explicit missing-value policy, and then
calculate cross-fact measures without changing the request shape.

Generated SQL uses deterministic aliases (`_e0`, `_e1`, ...), quoted identifiers and positional
parameters. Filters on dimensions are applied before aggregation; measure filters use `HAVING`.
Ordering may reference selected output names only. Limit defaults to 1,000 and is capped at 10,000.
There is no offset. Filter nesting is capped at eight levels and 100 predicates.

A filter member may be a bare member ID when it is unique among the participating entities, or a
fully qualified `{catalog_id, entity_id, member_id, relationship_path?}` reference. A qualified
filter may bring a related entity into the plan even when no member from that entity is selected.

Required filters must be satisfied on the source that declares them, either by a source-local
semantic filter or an explicitly mapped source argument. A join predicate or `HAVING` condition
does not satisfy a source required filter.

Failures are structured by stage: `request_validation`, `model_resolution`,
`multi_fact_not_supported`, `catalog_binding`, `relationship_resolution`, `type_check`, `fanout`,
`required_filter`, `sql_generation`, and `duckdb_execution`. The tool never silently falls back to
`run_sql`.

`vgi-lint-check` includes a Python reference implementation of this compiler. Cupola retains its
TypeScript implementation for browser execution; both consume the same packaged schemas and use
the same deterministic plan shape. The committed `examples/semantic/ecommerce-workers.json`
fixture drives metadata loading, linting, federation, compilation and DuckDB result assertions.

For agent acceptance testing, run `vgi-lint semantic-simulate` over all participating workers.
This uses the same Claude CLI or Anthropic API backend, bounded tool loop, hidden reference SQL,
result grading and cache as `vgi-lint simulate`, while exposing `query_semantic_model`. A private
task sidecar may declare `required_tools: [query_semantic_model]` to make semantic-tool use—not
merely the final answer—a test requirement.

## Validation responsibilities

JSON Schema validation runs first. The linter then validates cross-object invariants such as unique
IDs, valid grains, expression references, carrier conflicts, endpoints and predicate members. A
normal lint always validates the bundled contract and semantic tag values. Composed-catalog linting
can additionally resolve federated endpoints; execution remains an explicit `--execute` concern.
