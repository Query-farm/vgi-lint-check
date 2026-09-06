# VGI semantic model

This document is the normative human-readable companion to the JSON Schemas in
`src/vgi_lint_check/schema/semantic`. The schemas define JSON shape. This document defines
meaning, resolution and compiler behavior. VGI publishes the latest contract; workers do not
select or pin a semantic-contract version.

For a step-by-step explanation and complete worked example, start with the
[human authoring guide](semantic-model-authoring.md). Coding agents should additionally follow the
[agent authoring playbook](semantic-model-agent-authoring.md).

## Storage and carriers

DuckDB tags are `MAP(VARCHAR, VARCHAR)`, so every semantic value is serialized JSON. The
reserved keys are:

| Key | Host | Meaning |
| --- | --- | --- |
| `vgi.semantic_catalog` | catalog | Stable logical identity and runtime binding hints |
| `vgi.semantic_entity` | table, view, table function | Entity identity, grain and source arguments |
| `vgi.semantic_members` | table, view, table function | Packed member array, available on DuckDB 1.5 |
| `vgi.semantic_member` | column | Native member metadata, available when DuckDB exposes column tags |
| `vgi.semantic_relationships` | catalog, or a table/view/table function carrying `vgi.semantic_entity` | Relationship assertions |

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

An entity has a globally meaningful `(catalog_id, entity_id)` identity and an explicit grain. Its
physical carrier—the object tagged with `vgi.semantic_entity`—is a table, view, table function, or
fixed-schema table macro.
This carrier is sometimes called the entity host in implementation code; it is not a separate kind
of semantic object. Every grain member must be a physical `identifier`. Members have stable IDs
independent of physical column names:

- `identifier`: a key that may participate in grain or relationships.
- `dimension`: a physical or calculated grouping/filtering attribute.
- `time_dimension`: a dimension with an explicit timezone and supported granularities. Weeks begin
  on Monday.
- `measure`: an aggregate or a derived expression over measures.

`column` always names one physical column literally. A physical name containing a dot is therefore
quoted as one identifier. Nested DuckDB `STRUCT` access uses `column_path`, an array of two or more
identifier segments such as `["bbox", "xmin"]`. This distinction is deliberate: consumers never
guess whether punctuation in a physical name is syntax. Each path segment is quoted independently,
and model validation checks the complete path when discovery provides a detailed `STRUCT` type.

A dimension or time dimension on a table-function entity may use `source_argument` instead of a
column, column path, or expression. Its value is the effective physical function argument selected
by the existing `source.arguments` mapping. The member must declare `data_type` or `output_type`,
and the named physical argument must resolve exactly once and be exposed exactly once. For scalar
calls the compiler emits a typed parameter containing the supplied semantic value or discovered
physical default. For correlated calls it projects the bound input column or driver member. This
allows requested latitude, longitude, unit choice, model, or similar invocation context to be
selected, filtered, grouped, and referenced by typed expressions even when the function does not
repeat that value in every output row. `source_argument` is metadata-backed and cannot be combined
with `column`, `column_path`, or `expression`.

The packed `vgi.semantic_members` array may contain a member template alongside concrete members:

```json
{
  "template_id": "ensemble_temperatures",
  "template": {"kind": "dimension", "data_type": "DOUBLE", "unit": "Cel"},
  "members": [
    {"member_id": "temperature_gfs", "column": "temperature_gfs"},
    {"member_id": "temperature_ecmwf", "column": "temperature_ecmwf"}
  ]
}
```

Each entry is shallow-merged over `template`, then validated as an ordinary member before model
resolution. Entry fields override defaults. Template IDs must be unique within one carrier, every
entry must provide `member_id`, and one packed document may expand to at most 500 members. Templates
cannot nest and perform no interpolation or runtime code execution. Consumers expose only the
expanded concrete members, so queries, plans, native column members, and downstream agents need no
template-specific behavior.

Base measures support `count_rows`, `count`, `count_distinct`, `sum`, `min`, `max` and `avg`.
Derived members use the typed expression AST; raw SQL, arbitrary functions, windows and nested
aggregates are not part of the contract. `output_type` is optional and means an enforced DuckDB
`CAST`; it is not descriptive metadata. Otherwise the compiler infers the type.

Additivity is `additive`, `non_additive`, or `semi_additive` with prohibited dimensions. An explicit
annotation may only be more restrictive than what an aggregation implies.

Dimensions, time dimensions where a physical unit is meaningful, and measures may declare one of
two mutually exclusive unit forms. `"unit": "Cel"` is a static unit. A dynamic unit names a
physical function argument and maps each effective argument value to an output unit:

```json
{
  "unit_parameter": {
    "argument": "temperature_unit",
    "values": {"celsius": "Cel", "fahrenheit": "[degF]"}
  }
}
```

Unit strings are non-empty opaque identifiers; the contract deliberately has no unit-ontology
dependency. UCUM strings are recommended when a UCUM representation exists. The argument must
resolve unambiguously on the entity's table function and be exposed by exactly one
`source.arguments` mapping. When discovery advertises argument choices, `values` must cover every
choice. Extra values are permitted for runtimes that accept more than their advertised common set.

For `sum`, `min`, `max`, and `avg`, a base measure without its own unit inherits the unit declaration
of the member it aggregates. Counts are unitless. The compiler does not infer units through derived
arithmetic expressions; authors must declare a unit explicitly when it is semantically correct.

Parameterized table functions map source argument names to semantic query parameters. A query may
override selected mappings with query-local input columns or members of one explicit driving entity.
This creates a correlated `CROSS JOIN LATERAL` invocation edge. It is dataflow that constructs fact
rows, not a semantic relationship. An absent optional scalar mapping is omitted from the call so the
function's own default remains effective.

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

Column-driven bindings additionally require the function-level `input_from_args` capability exposed
by `vgi_function_arguments()`. Workers using `defineRowTransformFunction()` advertise it through
`FunctionInfo`; semantic authors must not duplicate it in a tag. Capability discovery is tri-state:
`true` permits correlated positional bindings, explicit `false` rejects them as unsupported, and
`null`/unavailable rejects them with an upgrade diagnostic because an older extension did not
expose the capability column. Unknown is never reported as false. Scalar parameter calls remain
usable when capability discovery is unavailable. Only positional, non-constant arguments may be
column-driven. Scalar parameters may bind compatible positional or named arguments.

A table macro may carry an entity when its result schema is fixed and published through
`vgi.result_columns_schema`. Its source arguments follow the same discovered positional/named
binding rules as table functions. Scalar macro calls do not require correlated-input capability;
column-driven LATERAL macro invocation is not implied. Do not model a macro whose output schema or
row grain changes with its arguments.

## Relationships and federation

A relationship names two stable entity references, directional cardinality, optional role names,
and one or more typed predicate pairs. Predicate pairs are ANDed. Omitting `operator` is backward-
compatible equality; `nulls: "not_equal"` compiles to `=`, while `nulls: "equal"` compiles to
`IS NOT DISTINCT FROM`. Spatial operators are `spatial_contains`, `spatial_within`, and
`spatial_intersects`; they compile to the corresponding DuckDB spatial functions and require
geometry members. `list_contains` relates a scalar member to a LIST member. Exactly one side names
an `*_element_path`, which is compiled as a safely quoted `list_transform` followed by
`list_contains`. No predicate form accepts raw SQL. Temporal predicates remain outside this
contract; publish a normalized view or bridge entity for them.

An optional `conditions` array qualifies a polymorphic relationship with model-owned literal
discriminators. Each condition identifies `side`, a physical `member`, an optional `operator` whose
only current value is `equal`, and a JSON scalar `value`. The compiler always binds that value as a
positional parameter and emits `IS NOT DISTINCT FROM ?`; model text is never interpolated as SQL.
For example, a shared registry can be related to one feature family with
`{"side":"to","member":"path","value":"theme=places/type=place"}`.

Predicate expressiveness does not weaken cardinality safety. A spatial or repeated-field edge may
be declared and discovered even when it is many-to-many, but the single-fact compiler still rejects
a traversal into a `many` endpoint. To make such enrichment compilable, publish an honest to-one
relationship (for example, a preselected administrative level) or normalize it through a bridge.

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

### Correlated inputs and invocation pipelines

`inputs` declares bounded, typed, query-local row sets. Every input has an `input_id`, typed columns,
one or more grain columns, and rows. Row widths must match; grain values must be non-null and unique.
The compiler casts every placeholder to its declared DuckDB type. A request is limited to 100 rows
per input, 32 columns, 3,200 total cells and one megabyte of serialized row data.

`source_bindings` is an ordered-independent, acyclic dataflow graph. Each entry identifies a table-
function entity, exactly one driver, and physical-argument overrides. A driver is either an inline
`input_id` or a semantic entity with a required `max_rows` bound. An entity driver may declare
semantic `filters` and member `order`; both are compiled inside the bounded driver subquery before
the lateral call. A required filter on a driver must be satisfied there, not after expansion.
Argument bindings are exactly one
of `{parameter}`, `{input_column}`, or `{member}`. Member references must belong to the declared
driver and initially must be physical column-backed members with a known compatible type.

Each table function has at most one driver. Every binding must lie on the selected fact root's
invocation path. The current single-fact compiler supports one linear path of up to ten functions;
that is sufficient for input → forecast, sites → forecast, and input → geocoding → forecast. Cycles,
unbound function drivers, multiple inline roots, unrelated bindings, named/constant correlated
arguments, and incompatible types fail closed.

The compiler emits one bounded CTE per stage and preserves earlier rows as nested structs. This
keeps member provenance unambiguous across catalogs and lets a later function consume a prior
function's output without inventing a business relationship. Dimensions on any entity along the
selected invocation path may be selected directly; no semantic relationship is required between a
driver and the function it invokes. `max_output_rows` bounds each lateral stage and defaults to
10,000. `execution_limits.max_invocations` defaults to 100; an explicit request
may raise or lower it but never above the hard ceiling of 1,000. It counts correlated input rows,
not provider HTTP requests.

For a correlated function:

```text
effective source grain = all upstream driving grains + function output grain
```

Driving grain columns are automatically selected and grouped by default. For locations driving an
hourly forecast, that means `location_id + time_key`; time alone cannot identify a row across
locations. `allow_driving_grain_reduction: true` explicitly permits an aggregation to remove the
upstream grain. The plan reports `effective_source_grain`, final `result_grain`, every invocation and
its argument bindings, estimated invocations, and whether driving grain was reduced.

When selected outputs declare units, the plan also contains a compact `output_units` object keyed by
the final output name (including request aliases). Values are strings or `null`; no SQL expression is
added merely to carry this metadata. Dynamic units resolve from an explicitly supplied semantic
parameter first, otherwise from the discovered physical argument default. An effective value not in
the declared map fails with `unit_parameter_value_unmapped`. If one valid SQL plan can still be
formed but the effective value is unavailable—for example, it varies by correlated row—the output
unit is `null` and `unit_diagnostics` contains `unit_parameter_value_unresolved`. If the compiler
cannot establish that omitting a scalar argument is valid, existing source-argument validation fails
closed instead of guessing a unit or a physical default.

Ordinary semantic relationships may enrich the final fact root after invocation and retain all
existing cardinality/fanout checks. They do not drive table-function arguments.

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
`multi_fact_not_supported`, `catalog_binding`, `relationship_resolution`, `source_binding`,
`unit_resolution`, `execution_limit`, `type_check`, `fanout`,
`required_filter`, `sql_generation`, and `duckdb_execution`. The tool never silently falls back to
`run_sql`.

`vgi-lint-check` includes a Python reference implementation of this compiler. Cupola retains its
TypeScript implementation for browser execution; both consume the same packaged schemas and use
the same deterministic plan shape. The committed `examples/semantic/ecommerce-workers.json`
fixture drives metadata loading, linting, federation, compilation and DuckDB result assertions.

The supported compile-only CLI attaches workers through the same discovery path and always forces
`compile_only: true`, regardless of the input document:

```bash
vgi-lint semantic-compile <sales-worker> <crm-worker> \
  --as sales_runtime --as crm_runtime --request request.json

# A request can also be piped without creating a file.
printf '%s' '{"measures":[{"catalog_id":"com.example.sales","entity_id":"orders","member_id":"revenue"}]}' \
  | vgi-lint semantic-compile <sales-worker> --as sales_runtime --request -
```

The JSON response contains the complete plan: parameterized SQL, positional parameter values,
invocation metadata, result grain, unit metadata, warnings, or structured diagnostics. The command
never executes generated SQL. Exit status is 0 for `ok: true`, 2 for semantic diagnostics, 3 for an
attachment/connection failure, and 1 for malformed input or CLI/tool errors.

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
