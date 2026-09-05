# Add a semantic model to a VGI worker as a coding agent

This playbook is for a coding agent working with a human developer. For explanations, examples,
and design guidance, start with the [human authoring guide](semantic-model-authoring.md). The
[semantic model specification](semantic-model.md) is normative when either guide is ambiguous.

The agent's job is to inventory and encode known business semantics, expose uncertainties,
validate the result, and leave an auditable report. It must not invent business meaning merely to
make lint pass.

## Inputs

Before editing, read the worker's object definitions, tests, examples, existing VGI tags, and
constraints. Run `vgi-lint spec --format json` for the complete machine contract or
`vgi-lint spec --schema member` for one JSON Schema.

Determine whether the target DuckDB exposes column tags by inspecting the fields returned by
`duckdb_columns()`. Use native `vgi.semantic_member` column tags when supported. Retain a packed
`vgi.semantic_members` representation when the worker must support DuckDB 1.5. Do not infer support
from a version string.

## Workflow

1. Inventory tables, views, and table functions. Record physical columns, constraints, required
   filters, arguments, and examples. For table functions, inspect `vgi_function_arguments()` and
   preserve both `arg_position` and `field_index` in any consumer model.
2. Propose a globally stable `catalog_id`, optional `catalog_instance_id`, and `binding_key`.
3. Identify candidate entities and state the exact row grain of each.
4. Ask the human to confirm uncertain business names, definitions, grain, and identifiers.
5. Add dimensions and time dimensions. State timezone and allowed granularities explicitly.
6. Add base measures, derived measures, and conservative additivity.
7. Add relationships using stable endpoint/member IDs and directional cardinality. Prefer one
   assertion; use reciprocal assertions only when independently owned catalogs genuinely attest.
8. Put relationships between two foreign catalogs in a deliberately designated federation worker,
   never opportunistically in an unrelated worker.
9. Run a local lint, then lint a composed attachment set for cross-catalog resolution.
10. Compile representative requests with `compile_only: true`. Execute only against authorized test
    data.
11. Give the human a final report listing edits, confirmed assumptions, unresolved questions,
    validation commands, representative plans, and intentionally deferred relationships.

## Never invent silently

Stop and ask the human when source code and existing documentation do not establish:

- a business definition or user-facing name;
- row grain, identifier uniqueness, or relationship cardinality;
- timezone, week/calendar meaning, snapshot behavior, or additivity;
- equivalence of keys or entities across catalogs;
- a default required filter or table-function argument;
- whether a third-party worker is authorized to publish a federation assertion.

Physical foreign keys and matching column names are evidence, not permission to assert semantic
equivalence. Prefer an incomplete but honest model plus an explicit question over a complete-looking
model based on guesses.

## Table-function sources

Map physical arguments to semantic parameter names, but do not copy `is_named`, `is_positional`,
`arg_position`, `field_index`, or `input_from_args` into the tag. The compiler reads those facts from
`vgi_function_arguments()`.

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

Verify that every mapping resolves exactly once, every required physical scalar argument is mapped,
and every optional mapping has a physical default. The current compiler rejects overload ambiguity,
varargs, table inputs, and requests that create a positional hole. Flattened
`duckdb_functions().parameters` metadata is not sufficient to infer a safe call.

When a row-transform function reports `input_from_args = true`, demonstrate both scalar and
correlated use. Author a compile-only request with a small typed `inputs` row set, bind only
positional non-constant arguments with `input_column`, and verify that the plan preserves the input
grain. For entity-driven use, bind fully qualified physical members from one declared driver and set
a conservative `max_rows`. For a chained pipeline, give each function one binding and verify the
invocation order, accumulated effective grain, total estimated invocations, and cycle rejection.
Never represent these dataflow edges as `vgi.semantic_relationships`.

## Reusable task prompt

> Add VGI semantic metadata to this worker in collaboration with me. Read the human authoring guide,
> object metadata, implementation, and tests first. Use the schemas emitted by `vgi-lint spec`; do
> not invent business definitions, grain, keys, cardinality, timezone, additivity, or cross-catalog
> equivalence. Ask concise questions for anything the repository does not establish. Support the
> worker's actual DuckDB tag capabilities, lint the result, compile representative semantic requests
> without executing them, and finish with an assumptions/questions/validation report.

## Final report

- Modeled catalogs and entities, including each grain.
- Measures, dimensions, and time semantics added.
- Relationships and their asserting provider.
- Human-confirmed decisions.
- Unresolved or deliberately deferred semantics.
- Lint and compile-only commands with outcomes.
- Compatibility notes for packed versus native column carriers.
