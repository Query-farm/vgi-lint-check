# Author a VGI semantic model

This guide explains how a human developer can add a useful semantic model to a VGI worker. It starts
with ordinary database objects and ends with a model that an agent or application can inspect,
validate, and query without inventing joins or business calculations.

If you only want the exact contract, read the [semantic model specification](semantic-model.md).
If you are directing a coding agent, give it the
[coding-agent playbook](semantic-model-agent-authoring.md) after you understand the choices in this
guide. Complete, executable sample metadata lives in
[`examples/semantic/ecommerce-workers.json`](../examples/semantic/ecommerce-workers.json).

## What you are building

A semantic model gives stable business meaning to physical DuckDB objects:

| Layer | Example | Purpose |
| --- | --- | --- |
| Catalog | `com.example.sales` | Identifies the provider independently of its attachment name |
| Entity | `orders` | Describes a queryable row set and its grain |
| Member | `revenue`, `country`, `ordered_at` | Names keys, dimensions, time dimensions, and measures |
| Relationship | `com.example.sales.order_customer` | States how two entities relate and with what cardinality |
| Query | revenue by customer country | Selects modeled members; the compiler produces parameterized SQL |

The model is stored alongside the objects it describes, in DuckDB tags. That proximity is the main
benefit: changing a table, function, or column makes inconsistencies visible to the linter instead
of leaving a separate modeling service silently stale.

The model does not replace ordinary SQL. It provides a governed path for modeled questions and
better context for agents that are still allowed to write SQL directly.

## Before you begin

You need to know the following facts about the data. If any are unclear, ask the data owner before
writing metadata:

- What does one row represent?
- Which columns uniquely identify that row?
- Which numeric definitions are official business measures?
- Which attributes may safely group or filter those measures?
- What timezone and calendar rules apply to timestamps?
- Which joins are valid, and can either side contain more than one matching row?
- Which required filters or table-function arguments constrain access?

Matching column names are useful evidence, but they do not prove that two keys have the same
meaning. A foreign key proves a physical constraint, but it does not by itself define the business
role or authorize a cross-provider relationship.

Inspect what the worker actually exposes:

```sql
SELECT * FROM duckdb_databases();
SELECT * FROM duckdb_tables();
SELECT * FROM duckdb_columns();
SELECT * FROM duckdb_functions();
SELECT * FROM vgi_function_arguments();
```

All semantic tag values are JSON serialized into DuckDB's `MAP(VARCHAR, VARCHAR)` tags property.
The examples below show the decoded JSON value for readability. In a worker implementation, encode
the object or array as a JSON string before assigning it to the tag map.

There is no separate semantic-model server to configure. Add the values to the same metadata
definitions that already publish the catalog, table/view/table-function, and column tags. The exact
constructor names differ between the Python, Go, Rust, Java, and TypeScript VGI SDKs, but the
language-independent operation is:

```python
import json

catalog_tags["vgi.semantic_catalog"] = json.dumps(catalog_definition)
orders_tags["vgi.semantic_entity"] = json.dumps(entity_definition)
orders_tags["vgi.semantic_members"] = json.dumps(member_definitions)
orders_tags["vgi.semantic_relationships"] = json.dumps(relationship_definitions)
```

Treat this as pseudocode and use your SDK's existing `tags` property. Do not double-encode values:
the DuckDB tag value should be one JSON string that parses to the object or array shown in this
guide. Query `duckdb_databases()`, `duckdb_tables()`, or `duckdb_functions()` after attaching the
worker to confirm that the value surfaced where expected.

## The worked example

Assume a sales worker exposes this table:

```text
orders(
  order_id    INTEGER,
  customer_id VARCHAR,
  ordered_at  TIMESTAMPTZ,
  amount      DECIMAL(18,2)
)
```

One row represents one completed order. `order_id` is unique. `amount` is the booked gross amount
in USD. A different CRM worker exposes one row per customer.

### 1. Give the catalog a stable identity

Put `vgi.semantic_catalog` on the catalog:

```json
{
  "catalog_id": "com.example.sales",
  "catalog_instance_id": "production",
  "binding_key": "sales",
  "title": "Sales",
  "description": "Completed orders and their booked revenue."
}
```

Choose `catalog_id` as a durable, globally namespaced identifier. Do not use the DuckDB attachment
alias: one user may attach this worker as `sales`, another as `sales_prod`, and a third may attach
two instances simultaneously.

- `catalog_id` identifies the logical provider and model.
- `catalog_instance_id` optionally distinguishes a tenant, account, or snapshot.
- `binding_key` is a short stable name a query can use to choose a runtime attachment explicitly.
- `title`, `description`, and `default_timezone` are optional presentation/default metadata.

Do not put a semantic contract version in the worker. `vgi-lint-check` validates against the latest
published contract.

### 2. Define an entity and its grain

Put `vgi.semantic_entity` on the `orders` table:

```json
{
  "entity_id": "orders",
  "grain": ["order_id"],
  "default_time_dimension": "ordered_at",
  "description": "Completed orders at one row per order_id."
}
```

An entity is a queryable business row set backed by a table, view, or table function. Its identity is
the pair `(catalog_id, entity_id)`.

The `grain` is the most important declaration in the model. It answers “what makes one row unique?”
using semantic member IDs, not raw column names. Every grain member must be an `identifier`. Use
multiple identifiers for a composite grain.

Do not describe the desired report grain here. If `orders` contains one row per line item, its grain
must be `order_id + line_id`, even if users often aggregate it to one row per order.

### 3. Add members

For DuckDB 1.5 compatibility, put the full array in `vgi.semantic_members` on the `orders` table.
More generally, this means the same table, view, or table function that carries
`vgi.semantic_entity`—there is no separate “entity host” object:

```json
[
  {
    "member_id": "order_id",
    "kind": "identifier",
    "column": "order_id",
    "description": "Unique completed-order identifier."
  },
  {
    "member_id": "customer_id",
    "kind": "identifier",
    "column": "customer_id",
    "description": "Customer that placed the order."
  },
  {
    "member_id": "ordered_at",
    "kind": "time_dimension",
    "column": "ordered_at",
    "timezone": "UTC",
    "granularities": ["day", "week", "month", "quarter", "year"],
    "week_start": "monday",
    "description": "UTC time at which the completed order was booked."
  },
  {
    "member_id": "amount",
    "kind": "dimension",
    "column": "amount",
    "data_type": "DECIMAL(18,2)",
    "description": "Booked gross order amount in USD."
  },
  {
    "member_id": "revenue",
    "kind": "measure",
    "aggregation": "sum",
    "member": "amount",
    "additivity": "additive",
    "output_type": "DECIMAL(18,2)",
    "description": "Sum of booked gross order amount."
  },
  {
    "member_id": "order_count",
    "kind": "measure",
    "aggregation": "count_rows",
    "additivity": "additive",
    "description": "Number of completed orders."
  }
]
```

Choose the kind by meaning:

| Kind | Use it for | Important rules |
| --- | --- | --- |
| `identifier` | Primary/composite keys and relationship keys | Required for entity grain; may also be selected or filtered |
| `dimension` | Categories, labels, flags, and other grouping/filtering values | May reference a column or a typed expression |
| `time_dimension` | Dates and timestamps used for time grouping | Must declare timezone and allowed granularities |
| `measure` | Aggregated business values | Must declare aggregation or a derived expression, plus additivity |

`member_id` is the stable API name. `column` is the physical implementation. Keeping them separate
lets a physical column be renamed without changing every semantic query.

Use `title` for a display label and `description` for business meaning. `hidden: true` can keep an
implementation member available to expressions and relationships without advertising it as a
normal choice.

`output_type` is operational: the compiler emits a DuckDB `CAST`. Do not use it merely as prose.

### 4. Define measures deliberately

Base measures support these aggregations:

| Aggregation | Meaning | Input member required? |
| --- | --- | --- |
| `count_rows` | Count source rows | No |
| `count` | Count non-null input values | Yes |
| `count_distinct` | Count distinct input values | Yes |
| `sum`, `avg`, `min`, `max` | DuckDB aggregate over the input | Yes |

Every measure also declares additivity:

- `additive` means the result can be summed across every modeled dimension.
- `non_additive` means it must be recalculated at the requested grain. Averages, minima, maxima,
  distinct counts, and derived ratios are inherently non-additive.
- `{"kind":"semi_additive","prohibited_dimensions":[...]}` names dimensions across which summing
  is invalid, such as summing an account balance across time.

When uncertain, choose the more restrictive declaration. Incorrectly calling a measure additive can
produce plausible but wrong totals.

Derived measures use a small typed expression tree rather than raw SQL. For example, average order
value can reference the two measures above:

```json
{
  "member_id": "average_order_value",
  "kind": "measure",
  "expression": {
    "op": "safe_divide",
    "left": {"op": "member", "member": "revenue"},
    "right": {"op": "member", "member": "order_count"}
  },
  "additivity": "non_additive",
  "output_type": "DECIMAL(18,2)",
  "description": "Revenue divided by completed-order count."
}
```

Supported expression operations are `member`, `literal`, arithmetic, `safe_divide`, `coalesce`,
`nullif`, `cast`, and `case`. Raw SQL and arbitrary function calls are intentionally excluded. If a
definition cannot be represented safely, publish a normalized view or physical column and model
that instead.

### 5. Treat time as business semantics

A timestamp column alone does not say how it should be grouped. A `time_dimension` declares:

- the source `column` or typed expression;
- an explicit `timezone`;
- exactly which `granularities` callers may request;
- `week_start: "monday"` when week is supported.

The compiler applies `date_trunc` using the declared timezone. Confirm whether the source represents
an event time, processing time, snapshot date, or business-effective date. Those concepts should
usually be different members even when they currently share a physical column.

### 6. Add relationships with cardinality

The CRM worker models a `customers` entity at grain `customer_id`. To allow revenue by customer
country, put `vgi.semantic_relationships` on either endpoint catalog/entity:

```json
[
  {
    "relationship_id": "com.example.sales.order_customer",
    "title": "Order customer",
    "from": {"catalog_id": "com.example.sales", "entity_id": "orders"},
    "to": {"catalog_id": "com.example.crm", "entity_id": "customers"},
    "from_cardinality": {"min": 0, "max": "many", "roles": ["orders"]},
    "to_cardinality": {"min": 1, "max": 1, "roles": ["customer"]},
    "predicate": [
      {
        "from_member": "customer_id",
        "to_member": "customer_id",
        "nulls": "not_equal"
      }
    ]
  }
]
```

Read the cardinality from the opposite row's perspective: one customer may have zero-to-many orders
(`from_cardinality`), while each modeled order has exactly one customer (`to_cardinality`). Entering
an endpoint whose `max` is `many` can multiply rows, so the current compiler rejects that traversal.
Entering an endpoint with `max: 1` is safe enrichment. `min: 1` produces an inner join; `min: 0`
produces a left join.

Use multiple predicate pairs for composite keys. Pairs are combined with `AND`. The default
`nulls: "not_equal"` uses `=`; `nulls: "equal"` uses `IS NOT DISTINCT FROM`.

Relationship endpoints always use stable catalog and entity IDs—never runtime attachment aliases.
One assertion is enough to navigate in either direction. If both providers independently publish the
same relationship ID and structurally reversed definition, it becomes `corroborated`. A relationship
published by a separate federation worker is `third_party`. These labels describe provenance, not
automatic trust or certification.

For many-to-many data, publish a bridge entity and two relationships. Do not hide the fanout with an
incorrect one-to-one declaration.

### 7. Model a table-function source

A parameterized table function can be an entity. Its entity tag maps physical argument names to
stable semantic request parameters:

```json
{
  "entity_id": "events",
  "grain": ["event_id"],
  "source": {
    "arguments": [
      {"argument": "since", "parameter": "start"},
      {"argument": "region", "parameter": "region", "required": false}
    ]
  }
}
```

Do not put `named`, `positional`, `arg_position`, or `field_index` in the semantic tag. The compiler
resolves each mapping against `vgi_function_arguments()`:

```sql
SELECT arg_name, arg_position, field_index, is_positional, is_named, is_varargs,
       is_table_input, input_from_args, arg_default
FROM vgi_function_arguments()
WHERE catalog_name = 'events_runtime'
  AND schema_name = 'main'
  AND function_name = 'events'
ORDER BY field_index;
```

If `since` is positional and `region` is named, the compiler emits:

```sql
events_runtime.main.events(?, "region" := ?)
```

It orders positional values by `arg_position` and named values by `field_index`. A mapping with
`required: false` is omitted when its semantic parameter is absent, allowing the function's physical
default to apply.

The current compiler rejects missing detailed argument metadata, ambiguous overloads, varargs,
table inputs, unmapped required arguments, optional mappings without physical defaults, and calls
that would leave a positional hole. These are explicit current boundaries, not extra fields you
should add to the tag.

If the function was created with `defineRowTransformFunction()`, discovery reports
`input_from_args = true`. That runtime capability lets a semantic query bind positional arguments to
columns and invoke the function once per driving row. Do not copy it into `vgi.semantic_entity`.

For example, a caller can provide a typed location batch and bind it to a forecast entity:

```json
{
  "measures": [{"catalog_id":"farm.query.open_meteo","entity_id":"forecast_hourly","member_id":"average_temperature"}],
  "inputs": [{
    "input_id": "locations",
    "grain": ["location_id"],
    "columns": [
      {"name":"location_id","type":"VARCHAR"},
      {"name":"latitude","type":"DOUBLE"},
      {"name":"longitude","type":"DOUBLE"}
    ],
    "rows": [["berlin",52.52,13.41],["tokyo",35.69,139.69]]
  }],
  "source_bindings": [{
    "entity": {"catalog_id":"farm.query.open_meteo","entity_id":"forecast_hourly"},
    "driver": {"input_id":"locations"},
    "arguments": {
      "latitude": {"input_column":"latitude"},
      "longitude": {"input_column":"longitude"},
      "forecast_days": {"parameter":"forecast_days"}
    }
  }],
  "parameters": {"forecast_days":3}
}
```

To drive the function from a modeled `sites` table or view, replace the driver with
`{"entity":{"catalog_id":"com.example.assets","entity_id":"sites"},"max_rows":100,
"filters":{"member":"active","operator":"eq","value":true}}` and use
fully qualified `{member: ...}` bindings. A function may also drive a later function, producing a
bounded pipeline such as locations → geocoding → weather. Every intermediate function needs its own
source binding and `input_from_args` capability.

Put entity-driver filters and optional member ordering inside `driver`. They run before the lateral
function and before `max_rows` is applied. Any `vgi_required_filters` declared by that driver must be
satisfied there; a top-level post-expansion filter is deliberately not accepted as a substitute.

The compiler automatically retains the input/entity grain. Only set
`allow_driving_grain_reduction: true` when combining different driver rows is the intended business
result. Set `execution_limits.max_invocations` to a request-specific bound when the default of 100 is
too high; requests cannot raise the hard ceiling.

## DuckDB 1.5 and 2.0 member carriers

DuckDB 1.5 exposes semantic tags on tables and table functions, but not native output-column tags.
Store all members in the packed `vgi.semantic_members` array on the same table or view that carries
`vgi.semantic_entity` (or on the table function when that carrier is supported).

When DuckDB exposes column tags, you may also put one `vgi.semantic_member` value directly on each
column. A native member may omit `column`; the hosting column supplies it. Consumers merge packed and
native representations by `member_id`:

- identical definitions deduplicate;
- different definitions for the same member are an error;
- feature support must be detected from metadata shape, not a version string.

Keep the packed carrier as long as the worker must support DuckDB 1.5. Do not maintain intentionally
different semantics in the two representations.

## Validate the model

### Inspect the schemas

The JSON Schemas are the source of truth for tag shapes:

```bash
vgi-lint spec --schema catalog
vgi-lint spec --schema entity
vgi-lint spec --schema member
vgi-lint spec --schema relationship
vgi-lint spec --schema query
```

Use `vgi-lint spec --format json` to export the complete tag contract and semantic schema bundle for
another application.

### Lint one worker

```bash
uv run vgi-lint 'uv run my_worker.py' --no-execute
```

The semantic rules are:

| Rule | What it checks |
| --- | --- |
| `VGI418` | Each semantic JSON value conforms to its schema |
| `VGI419` | IDs, grains, members, expressions, function arguments, and relationships are consistent |
| `VGI420` | Entities and members contain enough business description |
| `VGI421` | Each semantic tag is carried by an allowed object kind |

To focus only on this layer while authoring:

```bash
uv run vgi-lint 'uv run my_worker.py' \
  --no-execute --no-check-links \
  --select VGI418,VGI419,VGI420,VGI421
```

### Resolve several workers together

A local lint can validate a relationship's shape, but it cannot fully resolve an endpoint supplied
by another worker. Attach the participating workers together:

```bash
uv run vgi-lint semantic \
  'uv run sales_worker.py' 'uv run crm_worker.py' \
  --as sales_runtime --as crm_runtime \
  --require-resolved
```

Use a separate federation worker in the command when it owns relationships between two foreign
catalogs.

## Exercise the compiler

A semantic request for revenue by customer country looks like this:

```json
{
  "measures": [
    {
      "catalog_id": "com.example.sales",
      "entity_id": "orders",
      "member_id": "revenue"
    }
  ],
  "dimensions": [
    {
      "catalog_id": "com.example.crm",
      "entity_id": "customers",
      "member_id": "country"
    }
  ],
  "order": [{"member": "revenue", "direction": "desc"}],
  "compile_only": true
}
```

`compile_only: true` validates and returns a parameterized SQL plan without preparing, explaining,
executing, or caching it. Remove that property—or set it to false—only when execution is intended.

If more than one attachment has the same logical `catalog_id`, bind a catalog's `binding_key` to the
desired runtime alias:

```json
{
  "bindings": {
    "sales": "sales_production",
    "crm": "crm_us"
  }
}
```

Filters use parameters rather than interpolated values. Dimension filters compile to `WHERE` and
measure filters to `HAVING`. Ordering can reference selected output names only. Dimension-only
queries must specify `root_entity` so the compiler knows which grain anchors the query.

The compiler currently supports one measure-owning fact root plus safe to-one enrichment. It rejects
multiple fact roots and traversal into a many side rather than generating SQL that may double-count.
That restriction does not make the model wasted work: the same entities, members, and relationships
remain useful for discovery, direct-SQL agents, and a future multi-fact compiler.

In Cupola, the `query_semantic_model` tool accepts this request directly. In this repository, the
reference compiler and executable examples are exercised by the tests in
[`tests/unit/test_semantic_end_to_end.py`](../tests/unit/test_semantic_end_to_end.py).
That suite includes inline location batches, a filtered `sites` entity driving weather, and an
executed locations → geocoding → weather chain, plus capability, type, grain, cycle and limit
failures.

## Test whether an agent can use it

Correct metadata can still be hard to discover. Add public `vgi.agent_test_tasks` prompts and keep
reference SQL or other grader details in a private sidecar. Then run:

```bash
vgi-lint semantic-simulate <sales-worker> <crm-worker> \
  --as sales_runtime --as crm_runtime \
  --agent-tasks-file vgi-agent-tests.yaml
```

Set `required_tools: [query_semantic_model]` in the private grader when using the semantic compiler
is itself part of the acceptance criterion. The agent receives public metadata and tools, not the
hidden reference answer.

## Common failures

| Symptom or diagnostic | Likely cause | Fix |
| --- | --- | --- |
| `missing_catalog_identity` | Semantic tags exist without `vgi.semantic_catalog` | Add a stable catalog identity |
| `unknown_grain_member` | `grain` names a member that was not defined | Add/fix the member ID |
| `invalid_grain_member` | A grain member is not an identifier | Correct its kind or the grain |
| `unknown_physical_column` | A member's `column` does not exist | Fix the physical mapping |
| `invalid_measure_additivity` | A ratio, average, min/max, or distinct count is marked additive | Use `non_additive` or a valid semi-additive declaration |
| `unresolved_local_entity` | A relationship points to a missing entity in the same catalog | Fix the endpoint ID |
| `ambiguous_catalog_binding` | The same logical catalog is attached more than once | Supply a `binding_key -> alias` binding |
| `fanout_unsafe` | The requested path enters a many endpoint | Change the model/query or introduce a safe bridge/pre-aggregated entity |
| `missing_function_argument_metadata` | The compiler cannot see `vgi_function_arguments()` details | Upgrade/fix the worker or extension metadata |
| `ambiguous_function_overload` | One semantic source name has several physical overloads | Publish a uniquely named wrapper function/view |
| `optional_positional_hole` | A later positional value was supplied while an earlier one was omitted | Supply the prefix, use a named physical argument, or publish a wrapper |

## Completion checklist

- Every modeled entity states its real physical grain.
- Every grain member is a physical identifier.
- Measures describe business meaning, input, aggregation, and conservative additivity.
- Time dimensions state timezone and allowed granularities.
- Relationship endpoints use stable IDs, predicates use compatible physical members, and
  cardinality has been confirmed from data ownership—not guessed from names.
- Table-function mappings resolve against `vgi_function_arguments()` without duplicating calling
  convention in tags.
- Packed/native member carriers agree, and the packed form remains where DuckDB 1.5 is supported.
- `VGI418` through `VGI421` pass locally.
- Cross-catalog relationships resolve in a composed attachment test.
- Representative requests compile, and authorized test-data requests return expected results.
- Any unresolved business questions are documented rather than encoded as assumptions.
