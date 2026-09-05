# Add a semantic model to a VGI worker

This playbook is written for a coding agent working with a human developer. The agent's job is to
inventory and encode known business semantics, expose uncertainties, validate the result and leave
an auditable report. It must not guess business meaning merely to make lint pass.

## Inputs

Before editing, read the worker's object definitions, tests, examples, existing VGI tags and
constraints. Run `vgi-lint spec --format json` for the complete machine contract or
`vgi-lint spec --schema member` for one JSON Schema. Read
[`semantic-model.md`](semantic-model.md) for the normative behavior.

Determine whether the target DuckDB exposes column tags by inspecting the fields returned by
`duckdb_columns()`. Use native `vgi.semantic_member` column tags when supported. Always retain a
packed `vgi.semantic_members` representation when the worker must support DuckDB 1.5. Do not infer
support from a version string.

## Authoring workflow

1. Inventory tables, views and table functions; record their physical columns, constraints,
   required filters, arguments and examples. For table functions, inspect
   `vgi_function_arguments()` and preserve both `arg_position` and `field_index`.
2. Propose a globally stable `catalog_id`, optional `catalog_instance_id`, and `binding_key`.
3. Identify candidate entities and state the exact row grain of each.
4. Ask the human to confirm business names, definitions, grain and identifiers before encoding
   uncertain semantics.
5. Add dimensions and time dimensions. State timezone and allowed granularities explicitly.
6. Add base measures, derived measures and conservative additivity.
7. Add relationships using stable endpoint/member IDs and directional cardinality. Prefer one
   assertion; use reciprocal assertions only when independently owned catalogs genuinely attest.
8. Put relationships between two foreign catalogs in a deliberately designated federation worker,
   never opportunistically in an unrelated worker.
9. Run a local lint. Then lint a composed attachment set for cross-catalog resolution.
10. Compile representative requests with `compile_only: true`; execute them only when test data and
    authorization make that appropriate.
11. Give the human a final report listing edits, assumptions confirmed, unresolved questions,
    validation commands, representative SQL plans and intentionally deferred relationships.

## Never invent silently

Stop and ask the human when source code and existing documentation do not establish:

- a business definition or user-facing name;
- row grain, identifier uniqueness or relationship cardinality;
- timezone, week/calendar meaning, snapshot behavior or additivity;
- equivalence of keys or entities across catalogs;
- a default required filter or table-function argument;
- whether a third-party worker is authorized to publish a federation assertion.

Physical foreign keys and matching column names are useful evidence, but are not permission to
assert semantic equivalence. Prefer an incomplete but honest model plus an explicit question over a
complete-looking model based on guesses.

## Table-function sources

Map physical arguments to stable semantic parameter names, but do not copy `is_named`,
`is_positional`, `arg_position`, or `field_index` into the tag. The compiler reads those facts from
`vgi_function_arguments()` and renders the call. This keeps the semantic mapping coupled to the
worker's actual signature.

```json
{
  "entity_id":"events",
  "grain":["event_id"],
  "source":{"arguments":[
    {"argument":"since","parameter":"start"},
    {"argument":"region","parameter":"region","required":false}
  ]}
}
```

Before publishing this model, verify that every mapping resolves exactly once, every required
physical scalar argument is mapped, and every optional mapping has a physical default. The current
compiler rejects overload ambiguity, varargs, table inputs, and requests that create a positional
hole. A worker supporting semantic compilation must expose `vgi_function_arguments()` metadata;
flattened `duckdb_functions().parameters` is not enough to infer a safe call.

## Minimal templates

Catalog tag value:

```json
{"catalog_id":"com.example.sales","binding_key":"sales","title":"Sales"}
```

Entity and packed members on `orders`:

```json
{"entity_id":"orders","grain":["order_id"],"default_time_dimension":"ordered_at"}
```

```json
[
  {"member_id":"order_id","kind":"identifier","column":"order_id","description":"Unique order identifier"},
  {"member_id":"customer_id","kind":"dimension","column":"customer_id","description":"Customer that placed the order"},
  {"member_id":"ordered_at","kind":"time_dimension","column":"ordered_at","timezone":"UTC","granularities":["day","week","month"],"week_start":"monday"},
  {"member_id":"amount","kind":"dimension","column":"amount","data_type":"DECIMAL(18,2)"},
  {"member_id":"revenue","kind":"measure","aggregation":"sum","member":"amount","additivity":"additive","description":"Gross order revenue"}
]
```

Cross-catalog relationship:

```json
[
  {
    "relationship_id":"com.example.sales.order_customer",
    "from":{"catalog_id":"com.example.sales","entity_id":"orders"},
    "to":{"catalog_id":"com.example.crm","entity_id":"customers"},
    "from_cardinality":{"min":0,"max":"many","roles":["orders"]},
    "to_cardinality":{"min":1,"max":1,"roles":["customer"]},
    "predicate":[{"from_member":"customer_id","to_member":"customer_id","nulls":"not_equal"}]
  }
]
```

Compile-only tool request:

```json
{
  "measures":[{"catalog_id":"com.example.sales","entity_id":"orders","member_id":"revenue"}],
  "dimensions":[{"catalog_id":"com.example.crm","entity_id":"customers","member_id":"country","relationship_path":["com.example.sales.order_customer"]}],
  "bindings":{"sales":"sales_prod","crm":"crm_prod"},
  "compile_only":true
}
```

## Reusable agent task

> Add VGI semantic metadata to this worker in collaboration with me. Inspect all object metadata and
> tests first. Use the schemas emitted by `vgi-lint spec`; do not invent business definitions,
> grain, keys, cardinality, timezone, additivity or cross-catalog equivalence. Ask me concise
> questions for anything the repository does not establish. Support the worker's actual DuckDB tag
> capabilities, lint the finished worker, compile representative semantic requests without
> executing them, and finish with an assumptions/questions/validation report.

## Final report format

- Modeled catalogs and entities, including each grain.
- Measures, dimensions and time semantics added.
- Relationships and their asserting provider.
- Human-confirmed decisions.
- Unresolved or deliberately deferred semantics.
- Lint and compile-only commands with outcomes.
- Compatibility notes for packed versus native column carriers.
